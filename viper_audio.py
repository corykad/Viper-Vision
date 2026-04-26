import asyncio
import base64
import os
import time
import logging
import wave
import requests
import socket
import http.server
import socketserver
import threading
import shutil
import soco
import win32com.client
import queue
from concurrent.futures import ThreadPoolExecutor
from gtts import gTTS
import edge_tts
from google import genai
from google.genai import types

from urllib.parse import quote as _url_quote
import viper_config as cfg

# ==========================================
# FILE CLEANUP & WARMUP
# ==========================================
AUDIO_EXTENSIONS = {".mp3", ".wav"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Prefixes used for auto-generated audio files — used by targeted cleanup.
_GENERATED_AUDIO_PREFIXES = ("dispatch_", "verdict_", "utility_", "warmup")
_STATIC_AUDIO_PREFIXES = ("static_phrase_",)
# Prefixes used for auto-generated image files — used by targeted cleanup.
_GENERATED_IMAGE_PREFIXES = ("fast_", "latest_")


def _is_cleanup_candidate(file_path):
    if not file_path.is_file():
        return False
    lowered = file_path.suffix.lower()
    if lowered in AUDIO_EXTENSIONS:
        return True
    if lowered in IMAGE_EXTENSIONS:
        stem = file_path.stem.lower()
        return stem.startswith(_GENERATED_IMAGE_PREFIXES)
    return False


def startup_cleanup():
    """Sweeps old generated media files. gTTS warmup and static phrase
    pre-generation both run on background threads so app launch isn't blocked."""
    try:
        logging.info("Sweeping for stale generated media files...")
        # Targeted scan: only check files with known generated prefixes rather
        # than statting every file in the directory.
        for prefix in _GENERATED_AUDIO_PREFIXES + _STATIC_AUDIO_PREFIXES + _GENERATED_IMAGE_PREFIXES:
            for ext in (".mp3", ".wav", ".jpg", ".jpeg", ".png", ".webp"):
                for file_path in cfg.SONOS_AUDIO_DIR.glob(f"{prefix}*{ext}"):
                    try:
                        file_path.unlink()
                        logging.info(f"Deleted old generated file: {file_path.name}")
                    except Exception as e:
                        logging.error(f"[CLEANUP ERROR] Could not delete {file_path.name}: {e}")
    except Exception as e:
        logging.error(f"[STARTUP SWEEP ERROR] {e}")

    threading.Thread(target=_warmup_and_precache, daemon=True).start()
    # Warm up the Gemini HTTP/2 connection pool in the background so the first
    # doorbell event skips TCP + TLS negotiation.
    threading.Thread(target=_warmup_gemini_connection, daemon=True).start()
    gemini_tts_connection.start()


def _warmup_gemini_connection():
    """Delegate to viper_vision.warmup_gemini() — imported lazily to avoid
    a circular import (viper_vision imports viper_audio)."""
    try:
        import viper_vision as vision
        vision.warmup_gemini()
    except Exception as e:
        logging.warning("[GEMINI WARMUP RELAY] %s", e)


def _warmup_and_precache():
    """Runs in background at startup: warms up gTTS then pre-generates
    the static phrase cache so common fixed messages play instantly."""
    _warmup_gtts()
    _build_phrase_cache()


def _warmup_gtts():
    try:
        logging.info("[SYSTEM] Warming up gTTS connection...")
        tts = gTTS(text="Viper Vision Ready", lang="en")
        temp = cfg.SONOS_AUDIO_DIR / "warmup.mp3"
        tts.save(str(temp))
        temp.unlink(missing_ok=True)
        logging.info("[SYSTEM] gTTS warmup complete.")
    except Exception as e:
        logging.error(f"[GTTS WARMUP ERROR] {e}")


# ==========================================
# STATIC PHRASE CACHE
# ==========================================
# Maps message text → pre-generated audio file URL so frequently repeated
# fixed phrases (arm/disarm confirmations, etc.) skip TTS generation entirely.
# The cache_key tracks which engine+voice the files were generated for;
# if settings change, the cache is transparently rebuilt on next access.
_phrase_cache: dict[str, str] = {}
_phrase_cache_key: str = ""
_phrase_cache_lock = threading.Lock()
_phrase_cache_refresh_inflight = False
_generated_tts_cache: dict[str, tuple[str, float]] = {}
_generated_tts_cache_lock = threading.Lock()
_gemini_tts_client = None
_gemini_tts_client_key = None
_gemini_tts_client_lock = threading.Lock()
_gemini_tts_generation_lock = threading.Lock()
_gemini_tts_last_call = 0.0
_gemini_tts_unavailable_until = 0.0

GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_TTS_MIN_INTERVAL_SECONDS = 0.0

# Phrases to pre-generate. These are short, fixed strings that appear on
# every arm/disarm cycle and are worth having ready immediately.
_STATIC_PHRASES = [
    "Viper Vision Armed",
    "Viper Vision Disarmed",
]


class GeminiTTSConnectionManager:
    """Keeps one Gemini client available and optionally sends warm heartbeats.

    Heartbeats are real Gemini requests, so the default config leaves them off.
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.warm_status = False
        self.last_heartbeat_at = 0.0
        self.last_error = ""

    def get_client(self):
        return _get_gemini_tts_client()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()

    def status(self):
        return {
            "enabled": cfg.load_config().get("gemini_tts_keep_warm", False),
            "warm": self.warm_status,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_error": self.last_error,
        }

    def warm_once(self, profile=None):
        global _gemini_tts_last_call
        try:
            config = cfg.load_config()
            profile = profile or config.get("tts_profiles", {}).get("doorbell", {})
            model = profile.get("model") or config.get("gemini_tts_model") or GEMINI_TTS_MODEL
            voice = profile.get("voice") or config.get("gemini_tts_voice", "Sulafat")
            client = self.get_client()
            response = client.models.generate_content(
                model=model,
                contents=(
                    "Internal connection warmup for Viper Vision. Generate the shortest "
                    "neutral audio possible. This response is discarded and must not be "
                    "announced or emotionally styled."
                ),
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                        )
                    ),
                ),
            )
            if response and response.candidates:
                self.warm_status = True
                self.last_error = ""
                self.last_heartbeat_at = time.time()
                logging.info("[GEMINI TTS WARMUP] Warm request completed for %s.", model)
                return True
        except Exception as e:
            self.warm_status = False
            self.last_error = str(e)
            logging.warning("[GEMINI TTS WARMUP] %s", e)
        return False

    def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            config = cfg.load_config()
            if not config.get("gemini_tts_keep_warm", False):
                self.warm_status = False
                if self._stop_event.wait(15):
                    return
                continue
            interval = max(60, int(config.get("gemini_tts_heartbeat_seconds", 240)))
            now = time.time()
            if now - self.last_heartbeat_at >= interval:
                self.warm_once()
            if self._stop_event.wait(15):
                return


gemini_tts_connection = GeminiTTSConnectionManager()


def _phrase_cache_key_for(config: dict) -> str:
    engine = config.get("tts_engine", "Edge TTS (Natural)")
    voice = config.get("edge_tts_voice", "en-US-AriaNeural")
    gemini_voice = config.get("gemini_tts_voice", "Sulafat")
    tld = config.get("google_tts_tld", "com")
    return f"{engine}|{voice}|{gemini_voice}|{tld}"


def _generated_tts_cache_key(message, prefix, config, tts_profile=None, speed_override=None):
    tts_engine = config.get("tts_engine", "Edge TTS (Natural)")
    profile_bits = ""
    if tts_profile:
        profile_bits = "|".join(
            str(tts_profile.get(k, ""))
            for k in ("model", "voice", "style", "dynamic_mood", "speed")
        )
    return "|".join([
        prefix,
        tts_engine,
        config.get("edge_tts_voice", ""),
        config.get("gemini_tts_voice", ""),
        config.get("google_tts_tld", ""),
        str(config.get("local_voice_index", "")),
        str(speed_override or ""),
        profile_bits,
        message,
    ])


def _get_generated_tts_cache(message, prefix, config, tts_profile=None, speed_override=None):
    key = _generated_tts_cache_key(message, prefix, config, tts_profile, speed_override)
    with _generated_tts_cache_lock:
        cached = _generated_tts_cache.get(key)
    if not cached:
        return None
    file_name, created_at = cached
    file_path = cfg.SONOS_AUDIO_DIR / file_name
    if time.time() - created_at > 270 or not file_path.exists():
        with _generated_tts_cache_lock:
            _generated_tts_cache.pop(key, None)
        return None
    logging.info("[TTS CACHE] Reusing generated audio for %r", message)
    return file_name


def _put_generated_tts_cache(message, prefix, config, file_name, tts_profile=None, speed_override=None):
    key = _generated_tts_cache_key(message, prefix, config, tts_profile, speed_override)
    with _generated_tts_cache_lock:
        _generated_tts_cache[key] = (file_name, time.time())


def _build_phrase_cache():
    """Pre-generate TTS files for all static phrases using the current engine.
    Called once at startup and again automatically if the engine/voice changes."""
    global _phrase_cache, _phrase_cache_key
    config = cfg.load_config()
    new_key = _phrase_cache_key_for(config)
    tts_engine = config.get("tts_engine", "Edge TTS (Natural)")

    new_cache: dict[str, str] = {}

    if tts_engine == "Gemini TTS":
        # Gemini TTS has a low per-minute request quota. Do not spend it on
        # startup/cache rebuilds; reserve calls for actual announcements.
        with _phrase_cache_lock:
            _phrase_cache = {}
            _phrase_cache_key = new_key
        logging.info("[PHRASE CACHE] Skipped pre-generation for Gemini TTS.")
        return

    for phrase in _STATIC_PHRASES:
        try:
            suffix = _tts_file_suffix(tts_engine)
            file_name = f"static_phrase_{abs(hash(phrase + new_key))}{suffix}"
            file_path = cfg.SONOS_AUDIO_DIR / file_name
            if not file_path.exists() and not _generate_tts_to_path(phrase, file_path, config):
                continue
            new_cache[phrase] = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"

            logging.info("[PHRASE CACHE] Pre-generated: %r", phrase)
        except Exception as e:
            logging.error(f"[PHRASE CACHE ERROR] Failed to pre-generate {phrase!r}: {e}")

    with _phrase_cache_lock:
        _phrase_cache = new_cache
        _phrase_cache_key = new_key

    logging.info("[PHRASE CACHE] Built %d static phrases for engine=%s", len(new_cache), tts_engine)



def _get_cached_url(message: str, config: dict) -> str | None:
    """Return a pre-generated URL for message if available and current, else None.

    If the TTS engine/voice has changed, trigger one asynchronous rebuild so the
    next request can hit cache again without blocking the caller.
    """
    global _phrase_cache_refresh_inflight
    expected_key = _phrase_cache_key_for(config)
    with _phrase_cache_lock:
        cache_is_current = (_phrase_cache_key == expected_key)
        cached = _phrase_cache.get(message) if cache_is_current else None
        should_refresh = (not cache_is_current) and (not _phrase_cache_refresh_inflight)
        if should_refresh:
            _phrase_cache_refresh_inflight = True

    if should_refresh:
        def _refresh():
            global _phrase_cache_refresh_inflight
            try:
                _build_phrase_cache()
            finally:
                with _phrase_cache_lock:
                    _phrase_cache_refresh_inflight = False
        threading.Thread(target=_refresh, daemon=True).start()

    return cached

def invalidate_phrase_cache():

    """Call this after saving a TTS engine/voice change so the cache rebuilds."""
    threading.Thread(target=_build_phrase_cache, daemon=True).start()


# ==========================================
# MEDIA GARBAGE COLLECTOR
# ==========================================
def auto_cleanup_worker():
    logging.info("[SYSTEM] 300-second Media Garbage Collector started.")
    while True:
        try:
            now = time.time()
            # Scan only known generated-file prefixes rather than every file in
            # the directory — avoids statting config files, chimes, etc.
            for prefix in _GENERATED_AUDIO_PREFIXES:
                for ext in (".mp3", ".wav"):
                    for file_path in cfg.SONOS_AUDIO_DIR.glob(f"{prefix}*{ext}"):
                        try:
                            if now - file_path.stat().st_mtime > 300:
                                file_path.unlink()
                                logging.info(f"[CLEANUP] Purged 300s-old file: {file_path.name}")
                        except Exception as e:
                            logging.error(f"[CLEANUP ERROR] Could not purge {file_path.name}: {e}")
            for prefix in _GENERATED_IMAGE_PREFIXES:
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    for file_path in cfg.SONOS_AUDIO_DIR.glob(f"{prefix}*{ext}"):
                        try:
                            if now - file_path.stat().st_mtime > 300:
                                file_path.unlink()
                                logging.info(f"[CLEANUP] Purged 300s-old file: {file_path.name}")
                        except Exception as e:
                            logging.error(f"[CLEANUP ERROR] Could not purge {file_path.name}: {e}")
        except Exception as e:
            logging.error(f"[CLEANUP ERROR] Worker encountered issue: {e}")
        time.sleep(60)

threading.Thread(target=auto_cleanup_worker, daemon=True).start()

# ==========================================
# SONOS HTTP SERVER
# ==========================================
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(cfg.SONOS_AUDIO_DIR), **kwargs)

    def log_message(self, format, *args): pass

def start_local_server():
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("", cfg.SONOS_PORT), QuietHandler) as httpd:
            logging.info(f"[SYSTEM] Background Sonos audio server online (Port {cfg.SONOS_PORT})")
            httpd.serve_forever()
    except Exception as e:
        logging.error(f"Failed to start local audio server: {e}")

# ==========================================
# AUDIO ROUTING FUNCTIONS
# ==========================================
_sonos_cache: dict[str, float] = {}
_sonos_cache_lock = threading.Lock()


def _sonos_probe(ip: str) -> bool:
    """Return True if the Sonos speaker at ip is reachable on port 1400."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((ip, 1400))
        s.close()
        return True
    except Exception:
        return False


def _sonos_keepalive():
    """Background thread: probe each Sonos speaker every 45 seconds and refresh
    the cache timestamp. Never touches volume, mute, or group state so it cannot
    interrupt content that is already playing."""
    while True:
        time.sleep(45)
        now = time.time()
        with cfg.globals_lock:
            ips = list(cfg.SONOS_IPS)
        for ip in ips:
            try:
                if _sonos_probe(ip):
                    with _sonos_cache_lock:
                        _sonos_cache[ip] = now
                    logging.debug("[SONOS KEEPALIVE] %s is alive", ip)
                else:
                    logging.warning("[SONOS KEEPALIVE] %s is unreachable", ip)
            except Exception as e:
                logging.debug("[SONOS KEEPALIVE ERROR] %s: %s", ip, e)

threading.Thread(target=_sonos_keepalive, daemon=True).start()



def prep_sonos_speakers(force_prep: bool = False, target_ips: list[str] | None = None, take_over: bool = False):
    cfg.sync_globals_from_config()
    active_speakers = []
    now = time.time()

    with cfg.globals_lock:
        configured_ips = list(cfg.SONOS_IPS)

    ips_to_check = list(target_ips) if target_ips is not None else configured_ips

    for ip in ips_to_check:
        try:
            # Check cache BEFORE creating the SoCo object. If the keepalive has
            # confirmed this speaker is healthy recently, skip the full probe.
            with _sonos_cache_lock:
                cache_age = now - _sonos_cache.get(ip, 0)

            if not force_prep and cache_age < 60:
                active_speakers.append(soco.SoCo(ip))
                continue

            if not _sonos_probe(ip):
                logging.warning(f"[SONOS] Timed out reaching {ip}")
                continue

            speaker = soco.SoCo(ip)
            if take_over:
                try:
                    speaker.unjoin()
                except Exception:
                    pass
                try:
                    speaker.mute = False
                except Exception:
                    pass
                try:
                    speaker.volume = 45
                except Exception:
                    pass
            with _sonos_cache_lock:
                _sonos_cache[ip] = now
            active_speakers.append(speaker)
        except Exception as e:
            logging.error(f"[SONOS ERROR] Failed to connect to {ip}: {e}")

    return active_speakers

# ==========================================
# VOICE GENERATION HELPERS
# ==========================================
def get_available_windows_voices():
    """Returns a list of installed PC voice names for the UI."""
    try:
        speaker = win32com.client.Dispatch("SAPI.SPVoice")
        voices = speaker.GetVoices()
        return [v.GetDescription() for v in voices]
    except Exception:
        return ["Default System Voice"]

def speak_hd_pc(message):
    """Uses the specific voice index saved in config to speak on the physical PC."""
    def _run_speak():
        try:
            config = cfg.load_config()
            if config.get("mute_local_pc", False):
                return
            voice_idx = config.get("local_voice_index", 1)
            speaker = win32com.client.Dispatch("SAPI.SPVoice")
            voices = speaker.GetVoices()
            if voice_idx < voices.Count:
                speaker.Voice = voices.Item(voice_idx)
            speaker.Rate = 1
            speaker.Speak(message)
        except Exception as e:
            logging.error(f"[HD TTS ERROR] Windows Voice failed: {e}")
    threading.Thread(target=_run_speak, daemon=True).start()

def _sapi_rate_for_speed(speed):
    speed = (speed or "normal").strip().lower()
    return {
        "relaxed": -2,
        "normal": 0,
        "brisk": 1,
        "fast": 2,
        "very_fast": 4,
    }.get(speed, 0)


def generate_hd_wav(message, file_path, speed="normal", voice_idx=None):
    """Silently generates a WAV file using the Windows HD Voice for Sonos/HA to play."""
    try:
        config = cfg.load_config()
        voice_idx = config.get("local_voice_index", 1) if voice_idx is None else voice_idx
        speaker = win32com.client.Dispatch("SAPI.SPVoice")
        voices = speaker.GetVoices()
        if voice_idx < voices.Count:
            speaker.Voice = voices.Item(voice_idx)
        speaker.Rate = _sapi_rate_for_speed(speed)
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(str(file_path), 3, False)
        speaker.AudioOutputStream = stream
        speaker.Speak(message)
        stream.Close()
        return True
    except Exception as e:
        logging.error(f"[HD WAV GENERATION ERROR]: {e}")
        return False

def _edge_rate_for_speed(speed):
    speed = (speed or "normal").strip().lower()
    return {
        "relaxed": "-15%",
        "normal": "+0%",
        "brisk": "+10%",
        "fast": "+20%",
        "very_fast": "+35%",
    }.get(speed, "+0%")


def generate_edge_tts_mp3(message, file_path, voice="en-US-AriaNeural", speed="normal"):
    """Uses the edge-tts library in-process (no subprocess) for lower latency."""
    try:
        async def _run():
            communicate = edge_tts.Communicate(message, voice, rate=_edge_rate_for_speed(speed))
            await communicate.save(str(file_path))
        asyncio.run(_run())
        return True
    except Exception as e:
        logging.error(f"[EDGE TTS ERROR]: {e}")
        return False


def _get_gemini_tts_client():
    global _gemini_tts_client, _gemini_tts_client_key
    api_key = cfg.get_api_settings(include_env=True).get("gemini_api_key") or cfg.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("Gemini API key is not configured.")
    with _gemini_tts_client_lock:
        if _gemini_tts_client is None or _gemini_tts_client_key != api_key:
            _gemini_tts_client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(api_version="v1beta"),
            )
            _gemini_tts_client_key = api_key
        return _gemini_tts_client


def _write_pcm_wave(file_path, pcm, channels=1, rate=24000, sample_width=2):
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def get_speech_style(text):
    """Return Gemini TTS delivery instructions inferred from alert text."""
    lowered = (text or "").lower()
    warning_terms = (
        "fridge door open", "freezer door open", "door open for", "open for",
        "warning", "unreachable", "failed", "failure", "error", "offline",
        "leak", "smoke", "carbon monoxide", "battery low",
    )
    excited_terms = (
        " is home", " came home", "won", "winner", "great job", "congratulations",
        "birthday", "party", "celebrate", "awesome", "hooray",
    )
    urgent_terms = (
        "person at door", "someone is at", "someone at", "motion detected",
        "front door", "back door", "doorbell", "package", "visitor",
        "security alert", "detected",
    )

    if any(term in lowered for term in warning_terms):
        return {
            "mood": "warning",
            "tags": '<prosody rate="slow" pitch="-2st">',
            "close_tags": "</prosody>",
            "instruction": (
                "Use a deeper, slower, authoritative home-assistant tone. "
                "Sound stern but calm, with clear articulation."
            ),
        }
    if any(term in lowered for term in excited_terms) or lowered.endswith("!"):
        return {
            "mood": "excited",
            "tags": '<prosody pitch="+5st">',
            "close_tags": "</prosody>",
            "instruction": (
                "Use an excited, upbeat inflection while staying intelligible. "
                "Keep the delivery brisk."
            ),
        }
    if any(term in lowered for term in urgent_terms):
        return {
            "mood": "urgent",
            "tags": '<prosody rate="fast" pitch="+2st">',
            "close_tags": "</prosody>",
            "instruction": (
                "Use high energy and urgency for a security alert. "
                "Speak decently fast and make the first words easy to catch."
            ),
        }
    return {
        "mood": "neutral",
        "tags": "",
        "close_tags": "",
        "instruction": (
            "Use a helpful, calm home-assistant tone. Speak decently fast "
            "with crisp screen-reader-friendly articulation."
        ),
    }


def _speech_speed_instruction(speed):
    speed = (speed or "normal").strip().lower()
    return {
        "relaxed": "Speak at a relaxed pace, slower than normal but still crisp.",
        "normal": "Speak at a normal conversational pace with crisp articulation.",
        "brisk": "Speak briskly, a little faster than normal while staying clear.",
        "fast": "Speak fast and clearly, suitable for time-sensitive home alerts.",
        "very_fast": "Speak very fast but remain intelligible and screen-reader friendly.",
    }.get(speed, "Speak at a normal conversational pace with crisp articulation.")


def _gemini_tts_prompt(message, style="", dynamic_mood=False, speed="normal"):
    style = (style or "").strip()
    mood = get_speech_style(message) if dynamic_mood else None
    mood_instruction = mood["instruction"] if mood else ""
    mood_tags = mood["tags"] if mood else ""
    mood_close_tags = mood["close_tags"] if mood else ""
    transcript = f"{mood_tags}{message}{mood_close_tags}" if mood_tags else message
    instructions = [
        "You are Viper Vision, a home assistant voice.",
        "Read only the transcript text. Treat XML-style prosody tags and bracketed notes as delivery instructions, never as spoken words.",
        "Keep speech decently fast and highly intelligible for a screen reader user.",
        f"Requested speed: {_speech_speed_instruction(speed)}",
    ]
    if style:
        instructions.append(f"Category delivery style: {style}.")
    if mood_instruction:
        instructions.append(f"Dynamic mood instruction: {mood_instruction}")
    instructions.append(f"Transcript: {transcript}")
    return "\n".join(instructions)


def _legacy_gemini_tts_prompt(message, style=""):
    if style:
        return f"Say in a clear home announcement voice, following this delivery style {style}: {message}"
    return f"Say in a warm, clear home announcement voice: {message}"


def generate_gemini_tts_wav(message, file_path, voice="Sulafat", model=None, style="", dynamic_mood=False, speed="normal"):
    """Generate natural speech through Gemini TTS and save it as a WAV file."""
    global _gemini_tts_last_call, _gemini_tts_unavailable_until
    try:
        with _gemini_tts_generation_lock:
            now = time.time()
            if now < _gemini_tts_unavailable_until:
                logging.warning(
                    "[GEMINI TTS] Cooling down for %.1fs after quota response.",
                    _gemini_tts_unavailable_until - now,
                )
                return False

            min_interval = float(cfg.load_config().get("gemini_tts_min_interval_seconds", GEMINI_TTS_MIN_INTERVAL_SECONDS))
            elapsed = now - _gemini_tts_last_call
            if _gemini_tts_last_call and min_interval > 0 and elapsed < min_interval:
                wait_for = min_interval - elapsed
                logging.info("[GEMINI TTS] Waiting %.1fs to respect rate limit.", wait_for)
                time.sleep(wait_for)

            client = _get_gemini_tts_client()
            model = model or GEMINI_TTS_MODEL
            request_started = time.time()
            response = client.models.generate_content(
                model=model,
                contents=_gemini_tts_prompt(message, style, dynamic_mood, speed),
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice,
                            )
                        )
                    ),
                ),
            )
            _gemini_tts_last_call = time.time()
            logging.info("[TIMING] Gemini TTS API response took: %.2fs", _gemini_tts_last_call - request_started)

        write_started = time.time()
        data = response.candidates[0].content.parts[0].inline_data.data
        if isinstance(data, str):
            data = base64.b64decode(data)
        _write_pcm_wave(file_path, data)
        logging.info("[TIMING] Gemini TTS WAV write took: %.2fs", time.time() - write_started)
        return True
    except Exception as e:
        text = str(e)
        if "429" in text or "RESOURCE_EXHAUSTED" in text:
            _gemini_tts_unavailable_until = time.time() + 60
        logging.error(f"[GEMINI TTS ERROR]: {e}")
        return False


def _tts_file_suffix(tts_engine):
    return ".wav" if tts_engine in {"Local PC SAPI", "Gemini TTS"} else ".mp3"


def _generate_tts_to_path(message, file_path, config):
    tts_engine = config.get("tts_engine", "Edge TTS (Natural)")
    t_start = time.time()
    if tts_engine == "Edge TTS (Natural)":
        voice = config.get("edge_tts_voice", "en-US-AriaNeural")
        speed = config.get("tts_simple", {}).get("speeds", {}).get("manual", "normal")
        success = generate_edge_tts_mp3(message, file_path, voice, speed)
    elif tts_engine == "Local PC SAPI":
        speed = config.get("tts_simple", {}).get("speeds", {}).get("manual", "normal")
        success = generate_hd_wav(message, file_path, speed, config.get("local_voice_index", 1))
    elif tts_engine == "Gemini TTS":
        voice = config.get("gemini_tts_voice", "Sulafat")
        model = config.get("gemini_tts_model") or GEMINI_TTS_MODEL
        success = generate_gemini_tts_wav(message, file_path, voice, model)
    else:
        try:
            tld = config.get("google_tts_tld", "com")
            tts = gTTS(text=message, lang="en", tld=tld)
            tts.save(str(file_path))
            success = True
        except Exception as e:
            logging.error(f"[gTTS GENERATION ERROR]: {e}")
            success = False
    if success:
        logging.info("[TIMING] %s generation took: %.2fs", tts_engine, time.time() - t_start)
    return success


def _generate_network_tts_file(message, prefix, config, tts_profile=None, speed_override=None):
    started = time.time()
    cached_file = _get_generated_tts_cache(message, prefix, config, tts_profile, speed_override)
    if cached_file:
        logging.info("[TTS TIMING] prefix=%s cache_hit file=%s elapsed=%.2fs", prefix, cached_file, time.time() - started)
        return cached_file

    tts_engine = config.get("tts_engine", "Edge TTS (Natural)")
    logging.info(
        "[TTS TIMING] prefix=%s cache_miss engine=%s speed=%s chars=%d",
        prefix, tts_engine, speed_override or (tts_profile or {}).get("speed", ""), len(message or ""),
    )
    if tts_profile or tts_engine == "Gemini TTS":
        file_name = f"{prefix}_{int(time.time() * 1000)}.wav"
        file_path = cfg.SONOS_AUDIO_DIR / file_name
        tts_profile = tts_profile or {}
        voice = tts_profile.get("voice") or config.get("gemini_tts_voice", "Sulafat")
        model = tts_profile.get("model") or config.get("gemini_tts_model") or GEMINI_TTS_MODEL
        style = tts_profile.get("style", "")
        dynamic_mood = bool(tts_profile.get("dynamic_mood", False))
        speed = tts_profile.get("speed", "normal")
        if generate_gemini_tts_wav(message, file_path, voice, model, style, dynamic_mood, speed):
            logging.info("[TIMING] Gemini TTS generated network audio in %.2fs file=%s.", time.time() - started, file_name)
            _put_generated_tts_cache(message, prefix, config, file_name, tts_profile, speed_override)
            return file_name

        fallback_name = f"{prefix}_{int(time.time() * 1000)}_edge_fallback.mp3"
        fallback_path = cfg.SONOS_AUDIO_DIR / fallback_name
        fallback_voice = config.get("edge_tts_voice", "en-US-AriaNeural")
        logging.warning("[GEMINI TTS] Falling back to Edge TTS for this announcement.")
        if generate_edge_tts_mp3(message, fallback_path, fallback_voice, speed):
            logging.info("[TTS TIMING] Gemini fallback Edge generated in %.2fs file=%s", time.time() - started, fallback_name)
            _put_generated_tts_cache(message, prefix, config, fallback_name, tts_profile, speed_override)
            return fallback_name
        return None

    file_name = f"{prefix}_{int(time.time() * 1000)}{_tts_file_suffix(tts_engine)}"
    file_path = cfg.SONOS_AUDIO_DIR / file_name
    if speed_override and tts_engine in {"Edge TTS (Natural)", "Local PC SAPI"}:
        t_start = time.time()
        if tts_engine == "Local PC SAPI":
            success = generate_hd_wav(message, file_path, speed_override, config.get("local_voice_index", 1))
        else:
            voice = config.get("edge_tts_voice", "en-US-AriaNeural")
            success = generate_edge_tts_mp3(message, file_path, voice, speed_override)
        if success:
            logging.info("[TIMING] %s generation took: %.2fs", tts_engine, time.time() - t_start)
            logging.info("[TTS TIMING] prefix=%s generated file=%s elapsed=%.2fs", prefix, file_name, time.time() - started)
            _put_generated_tts_cache(message, prefix, config, file_name, tts_profile, speed_override)
            return file_name
        return None
    if _generate_tts_to_path(message, file_path, config):
        logging.info("[TTS TIMING] prefix=%s generated file=%s elapsed=%.2fs", prefix, file_name, time.time() - started)
        _put_generated_tts_cache(message, prefix, config, file_name, tts_profile, speed_override)
        return file_name
    logging.warning("[TTS TIMING] prefix=%s generation_failed elapsed=%.2fs", prefix, time.time() - started)
    return None

# ==========================================
# SHARED HA SESSION
# ==========================================
# A single requests.Session with connection pooling for all HA calls — avoids
# re-establishing a TCP connection on every request to the same host.
# connect timeout = 2s (fail fast if HA is unreachable)
# read timeout   = 5s (normal response time budget)
_ha_session = requests.Session()
_ha_session.headers.update({"Content-Type": "application/json"})
_ha_session.mount(
    "http://",
    requests.adapters.HTTPAdapter(
        max_retries=requests.adapters.Retry(total=1, backoff_factor=0.3)
    ),
)
_HA_TIMEOUT = (2, 5)  # (connect_timeout, read_timeout) in seconds
_BUSY_CHECK_POOL = ThreadPoolExecutor(max_workers=8)
_PUSHOVER_SESSION = requests.Session()


def _ha_auth_headers() -> dict:
    return {"Authorization": f"Bearer {cfg.HA_TOKEN}", "Content-Type": "application/json"}


# --- SAFE THREAD HELPERS ---
def _safe_sonos_play(speaker, uri, tag="SONOS"):
    started = time.time()
    try:
        speaker.play_uri(uri)
        logging.info("[%s TIMING - %s] play_uri submitted in %.2fs", tag, speaker.ip_address, time.time() - started)
    except Exception as e:
        logging.error(f"[{tag} ERROR - {speaker.ip_address}]: {e}")


def _infer_media_content_type(url: str) -> str:
    lowered = url.lower()
    if lowered.endswith('.wav'):
        return 'audio/wav'
    if lowered.endswith('.mp3'):
        return 'audio/mp3'
    return 'music'


def _safe_ha_play(entity, url, headers):
    started = time.time()
    try:
        payload = {
            "entity_id": entity,
            "media_content_id": url,
            "media_content_type": _infer_media_content_type(url),
        }
        response = _ha_session.post(
            f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/services/media_player/play_media",
            headers=headers,
            json=payload,
            timeout=_HA_TIMEOUT,
        )
        logging.info(
            "[HA PLAY TIMING - %s] status=%s submitted in %.2fs media_type=%s",
            entity, response.status_code, time.time() - started, payload["media_content_type"],
        )
    except Exception as e:
        logging.error(f"[HA PLAY ERROR - {entity}]: {e}")


def _safe_alexa_play(entity, url, headers):
    logging.info("[ALEXA PLAY SKIP - %s]: Alexa uses announce, not direct media playback.", entity)


def _ha_entity_is_busy(entity_id: str, headers: dict) -> bool:
    try:
        response = _ha_session.get(
            f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/states/{entity_id}",
            headers=headers,
            timeout=_HA_TIMEOUT,
        )
        if not response.ok:
            return False
        state = str(response.json().get('state', '')).lower()
        return state in {'playing', 'buffering'}
    except Exception as e:
        logging.debug(f"[HA STATE CHECK ERROR - {entity_id}]: {e}")
        return False


def _sonos_ip_is_busy(ip: str) -> bool:
    try:
        speaker = soco.SoCo(ip)
        transport = speaker.get_current_transport_info()
        state = str(transport.get('current_transport_state', '')).upper()
        return state in {'PLAYING', 'TRANSITIONING'}
    except Exception as e:
        logging.debug(f"[SONOS STATE CHECK ERROR - {ip}]: {e}")
        return False



def any_target_speakers_busy(targets: dict | None = None) -> bool:
    """Check only the relevant targets in parallel to minimise polling latency."""
    headers = _ha_auth_headers()

    if targets is None:
        with cfg.globals_lock:
            ha_entities = list(cfg.TARGET_SPEAKERS) + list(cfg.ALEXA_DEVICES)
            sonos_ips = list(cfg.SONOS_IPS)
    else:
        ha_entities = list(targets.get("ha", [])) + list(targets.get("alexa", []))
        sonos_ips = list(targets.get("sonos", []))

    if not ha_entities and not sonos_ips:
        return False

    futures = [_BUSY_CHECK_POOL.submit(_ha_entity_is_busy, e, headers) for e in ha_entities]
    futures += [_BUSY_CHECK_POOL.submit(_sonos_ip_is_busy, ip) for ip in sonos_ips]
    return any(f.result() for f in futures)

def _estimate_speech_seconds(message: str) -> float:
    """Estimate audio duration from word count.
    Google/Edge TTS runs roughly 150 WPM = 2.5 words per second.
    We give 2× the estimate plus a 4s buffer, capped at 45s."""
    words = max(1, len(message.split()))
    return min(45.0, (words / 2.5) * 2 + 4.0)



def wait_for_speakers_to_finish(
    message: str = "",
    max_wait: float = 45.0,
    poll_interval: float = 0.25,
    startup_grace: float = 0.35,
    targets: dict | None = None,
) -> bool:
    # Cap max_wait based on estimated speech duration so a Nest speaker stuck
    # in 'playing' state for 30s cannot block the queue for short messages.
    if message:
        max_wait = min(max_wait, _estimate_speech_seconds(message))
        logging.debug("[SPEAKER WAIT] Dynamic max_wait=%.1fs for %d-word message", max_wait, len(message.split()))

    logging.info(
        "[SPEAKER WAIT] Starting wait max=%.1fs poll=%.2fs grace=%.2fs targets ha=%d sonos=%d alexa=%d",
        max_wait,
        poll_interval,
        startup_grace,
        len((targets or {}).get("ha", [])) if targets else 0,
        len((targets or {}).get("sonos", [])) if targets else 0,
        len((targets or {}).get("alexa", [])) if targets else 0,
    )
    time.sleep(startup_grace)
    start = time.time()
    polls = 0

    while time.time() - start < max_wait:
        polls += 1
        if not any_target_speakers_busy(targets=targets):
            logging.info("[SPEAKER WAIT] Idle after %.2fs polls=%d", time.time() - start, polls)
            return True
        time.sleep(poll_interval)

    logging.info("[SPEAKER WAIT] Timed out after %.2fs polls=%d", time.time() - start, polls)
    return False


def _safe_alexa_announce(msg, targets, headers):
    config = cfg.load_config()
    if not config.get("enable_alexa", False):
        return
    if not targets:
        return
    started = time.time()
    try:
        response = _ha_session.post(
            f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/services/notify/alexa_media",
            headers=headers,
            json={"message": msg, "data": {"type": "announce"}, "target": targets},
            timeout=_HA_TIMEOUT,
        )
        logging.info("[ALEXA ANNOUNCE TIMING] status=%s targets=%d submitted in %.2fs", response.status_code, len(targets), time.time() - started)
    except Exception as e:
        logging.error(f"[ALEXA ANNOUNCE ERROR]: {e}")


def _send_text_pushover(title: str, message: str):
    if not cfg.PUSHOVER_API_TOKEN or not cfg.PUSHOVER_USER_KEY:
        return
    try:
        payload = {
            "token": cfg.PUSHOVER_API_TOKEN,
            "user": cfg.PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
        }
        _PUSHOVER_SESSION.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
        logging.info("[PUSHOVER] Text Pushover sent: %s", title)
    except Exception as e:
        logging.error(f"[PUSHOVER ERROR]: {e}")



def _chime_url(filename: str) -> str:
    """Build a correctly percent-encoded URL for a chime file so that filenames
    containing spaces or special characters play correctly on Sonos and HA.
    Also verifies the file exists and attempts to auto-convert unsupported
    formats (non-PCM WAV, unusual encodings) to a safe MP3 using FFmpeg."""
    from pathlib import Path as _Path
    import shutil as _shutil

    file_path = cfg.CHIMES_DIR / filename
    if not file_path.exists():
        logging.warning("[CHIME] File not found: %s", filename)
        return ""

    # Auto-convert WAV files that may not be PCM (Sonos only supports PCM WAV).
    # We convert to MP3 once and cache it alongside the original.
    if file_path.suffix.lower() == ".wav":
        converted = file_path.with_suffix(".chime.mp3")
        if not converted.exists():
            ffmpeg = _shutil.which("ffmpeg") or "ffmpeg"
            try:
                import subprocess as _sub
                result = _sub.run(
                    [ffmpeg, "-y", "-i", str(file_path),
                     "-ar", "44100", "-ab", "192k", str(converted)],
                    stdout=_sub.DEVNULL, stderr=_sub.DEVNULL, timeout=30
                )
                if result.returncode == 0 and converted.exists():
                    logging.info("[CHIME] Converted %s → %s", filename, converted.name)
                    filename = converted.name
                else:
                    logging.warning("[CHIME] FFmpeg conversion failed for %s — using original", filename)
            except Exception as e:
                logging.warning("[CHIME] Auto-convert error for %s: %s", filename, e)
        else:
            filename = converted.name

    # Percent-encode the filename so spaces and special chars don't break the URL.
    encoded = _url_quote(filename, safe="")
    return f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/chimes/{encoded}"

# --- TEST CHIME ---

def play_broadcast_chime(filename, channel="fridge"):
    """Play a fridge/freezer channel chime on speakers that allow that category."""
    if filename == "(Default)" or not filename:
        chime_url = "http://codeskulptor-demos.commondatastorage.googleapis.com/descent/gotitem.mp3"
    else:
        chime_url = _chime_url(filename)
        if not chime_url:
            logging.warning("[CHIME] Could not resolve broadcast chime: %r", filename)
            return

    config = cfg.load_config()
    headers = _ha_auth_headers()
    context = {"channel": channel}
    ha_targets, sonos_targets, _alexa_targets, _category, _quiet = _collect_targets_for_context(config, context)

    for ip in sonos_targets:
        try:
            s = soco.SoCo(ip)
            threading.Thread(target=_safe_sonos_play, args=(s, chime_url, "SONOS BROADCAST CHIME"), daemon=True).start()
        except Exception as e:
            logging.error(f"[SONOS BROADCAST CHIME ERROR - {ip}]: {e}")

    for entity in ha_targets:
        threading.Thread(target=_safe_ha_play, args=(entity, chime_url, headers), daemon=True).start()

def test_specific_chime(filename, door_type):
    if filename == "(Default)" or not filename:
        chime_url = "http://codeskulptor-demos.commondatastorage.googleapis.com/pang/pop.mp3" if door_type == "back" else "http://codeskulptor-demos.commondatastorage.googleapis.com/descent/gotitem.mp3"
    else:
        chime_url = _chime_url(filename)
        if not chime_url:
            return

    speakers = prep_sonos_speakers()
    for s in speakers:
        threading.Thread(target=_safe_sonos_play, args=(s, chime_url, "SONOS CHIME"), daemon=True).start()

    cfg.sync_globals_from_config()
    headers = _ha_auth_headers()
    for entity in cfg.TARGET_SPEAKERS:
        threading.Thread(target=_safe_ha_play, args=(entity, chime_url, headers), daemon=True).start()
    for entity in cfg.ALEXA_DEVICES:
        threading.Thread(target=_safe_alexa_play, args=(entity, chime_url, headers), daemon=True).start()


# --- INSTANT CHIME ---
def sonos_instant_chime(location="front door", trace_id=None):
    started = time.time()
    config = cfg.load_config()
    if "back" in location.lower():
        custom_file = config.get("back_chime", "")
        fallback_url = "http://codeskulptor-demos.commondatastorage.googleapis.com/pang/pop.mp3"
    else:
        custom_file = config.get("front_chime", "")
        fallback_url = "http://codeskulptor-demos.commondatastorage.googleapis.com/descent/gotitem.mp3"

    if custom_file and (cfg.CHIMES_DIR / custom_file).exists():
        chime_url = _chime_url(custom_file) or fallback_url
    else:
        chime_url = fallback_url

    _ha_targets, sonos_targets, _alexa_targets, _category, _quiet = _collect_targets_for_context(
        config,
        {"channel": "doorbell", "event": "back" if "back" in location.lower() else "front"},
    )
    speakers = prep_sonos_speakers(target_ips=sonos_targets)
    logging.info(
        "[CHIME TIMING] trace=%s location=%s prep_targets=%d prep_elapsed=%.2fs",
        trace_id or "", location, len(speakers), time.time() - started,
    )
    for s in speakers:
        threading.Thread(target=_safe_sonos_play, args=(s, chime_url, "SONOS INSTANT CHIME"), daemon=True).start()
    logging.info("[CHIME TIMING] trace=%s location=%s submitted elapsed=%.2fs", trace_id or "", location, time.time() - started)


# ==========================================
# SHARED SONOS DISPATCH HELPER
# ==========================================

def _dispatch_to_sonos(url, tag="SONOS DISPATCH", targets=None):
    """Fire-and-forget: prep target speakers then play the given URL."""
    def sonos_worker():
        started = time.time()
        try:
            speakers = prep_sonos_speakers(force_prep=False, target_ips=targets)
            logging.info("[%s TIMING] prep complete speakers=%d elapsed=%.2fs", tag, len(speakers), time.time() - started)
            for s in speakers:
                threading.Thread(target=_safe_sonos_play, args=(s, url, tag), daemon=True).start()
            logging.info("[%s TIMING] submitted speakers=%d elapsed=%.2fs", tag, len(speakers), time.time() - started)
        except Exception as e:
            logging.error(f"[SONOS WORKER ERROR]: {e}")
    threading.Thread(target=sonos_worker, daemon=True).start()


def sonos_speak_verdict(message):
    config = cfg.load_config()
    file_name = _generate_network_tts_file(message, "verdict", config)
    if not file_name:
        return

    local_url = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"
    _dispatch_to_sonos(local_url, "SONOS VERDICT")


# --- SPECIFIC SPEAKER (Fast Utility Speech) ---
def announce_specific_speaker(spk_type, spk_id, message):
    headers = _ha_auth_headers()
    config = cfg.load_config()

    if spk_type == "alexa":
        headers = _ha_auth_headers()
        file_name = _generate_network_tts_file(message, "utility", config)
        if file_name:
            url = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"
            threading.Thread(target=_safe_alexa_play, args=(spk_id, url, headers), daemon=True).start()

    elif spk_type == "ha":
        file_name = _generate_network_tts_file(message, "utility", config)
        if file_name:
            url = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"
            threading.Thread(target=_safe_ha_play, args=(spk_id, url, headers), daemon=True).start()

    elif spk_type == "sonos":
        threading.Thread(target=sonos_speak_verdict, args=(message,), daemon=True).start()


# ==========================================
# NETWORK AUDIO SPOOLER (QUEUE)
# ==========================================
network_speech_queue = queue.PriorityQueue()
_spooler_counter = 0
spooler_lock = threading.Lock()


def _parse_hhmm(value: str):
    try:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2:
            return None
        h = int(parts[0]); m = int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None

def _is_quiet_hours_active(config: dict) -> bool:
    if not config.get("quiet_hours_enabled", False):
        return False
    start = _parse_hhmm(config.get("quiet_hours_start", "22:00"))
    end = _parse_hhmm(config.get("quiet_hours_end", "07:00"))
    if not start or not end:
        return False
    now = time.localtime()
    now_m = now.tm_hour * 60 + now.tm_min
    start_m = start[0] * 60 + start[1]
    end_m = end[0] * 60 + end[1]
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m

def _context_category(context: dict | None) -> str:
    if not context:
        return "utilities"
    if context.get("manual"):
        return "manual"
    channel = (context.get("channel") or "utilities").lower()
    if channel == "doorbell":
        return "doorbell"
    if channel.startswith("fridge") or channel.startswith("freezer"):
        return "fridge"
    if channel == "cinderella":
        return "utilities"
    if channel in {"broadcast", "manual"}:
        return "manual"
    return "utilities"

def _speaker_enabled_for_context(spk: dict, category: str, quiet_active: bool) -> bool:
    if not spk.get("enabled", True):
        return False
    if category == "manual":
        return True
    if category == "doorbell" and not spk.get("doorbell", True):
        return False
    if category == "utilities" and not spk.get("utilities", True):
        return False
    if category == "fridge" and not spk.get("fridge", True):
        return False
    if quiet_active and category == "utilities" and not spk.get("quiet_hours_exempt", False):
        return False
    return True


def _speaker_matches_profile_target(name: str, spk: dict, target: str) -> bool:
    target = (target or "configured").strip().lower()
    if target in {"", "configured", "route", "routing"}:
        return True
    if target in {"all", "all enabled", "whole house"}:
        return True
    return target in {
        name.lower(),
        str(spk.get("id", "")).lower(),
        str(spk.get("type", "")).lower(),
    }

def _collect_targets_for_context(config: dict, context: dict | None):
    category = _context_category(context)
    quiet_active = _is_quiet_hours_active(config)
    profile_target = (context or {}).get("target_speaker", "configured")
    target_all = str(profile_target or "").strip().lower() in {"all", "all enabled", "whole house"}
    ha_targets, alexa_targets, sonos_targets = [], [], []
    for name, spk in config.get("speakers", {}).items():
        if target_all:
            if not spk.get("enabled", True):
                continue
        elif not _speaker_enabled_for_context(spk, category, quiet_active):
            continue
        if not _speaker_matches_profile_target(name, spk, profile_target):
            continue
        t = spk.get("type")
        if t == "ha":
            ha_targets.append(spk["id"])
        elif t == "sonos":
            sonos_targets.append(spk["id"])
        elif t == "alexa" and config.get("enable_alexa", False):
            alexa_targets.append(spk["id"])
    return ha_targets, sonos_targets, alexa_targets, category, quiet_active


def _context_label(context: dict | None) -> str:
    if not context:
        return "general"
    channel = context.get("channel") or "general"
    if channel == "cinderella":
        event = context.get("event") or "unknown"
        source = context.get("source") or "vacuum"
        error = context.get("error") or "none"
        return f"cinderella:{event}:{source}:{error}"
    if channel == "doorbell":
        event = context.get("event") or "unknown"
        return f"doorbell:{event}"
    return str(channel)


def network_speech_worker():
    while True:
        try:
            priority, queued_ts, _, message, urgent, context = network_speech_queue.get()
            label = _context_label(context)
            queue_wait = time.time() - queued_ts
            logging.info(
                "[AUDIO QUEUE] Dequeued %s urgent=%s queue_wait=%.2fs remaining=%s",
                label, urgent, queue_wait, network_speech_queue.qsize(),
            )
            dispatch_start = time.time()
            dispatch_info = _execute_announce_all(message, urgent, context) or {}
            logging.info(
                "[AUDIO QUEUE] Dispatch complete for %s in %.2fs; waiting for speakers to go idle",
                label, time.time() - dispatch_start,
            )
            if dispatch_info.get("wait_for_idle"):
                wait_started = time.time()
                wait_for_speakers_to_finish(message=message, targets=dispatch_info)
                logging.info(
                    "[AUDIO QUEUE] Speakers idle for %s after %.2fs",
                    label, time.time() - wait_started,
                )
            else:
                logging.info("[AUDIO QUEUE] No active playback targets for %s", label)
            time.sleep(0.1)
            network_speech_queue.task_done()
        except Exception as e:
            logging.error(f"[SPOOLER ERROR] {e}")

threading.Thread(target=network_speech_worker, daemon=True).start()


def announce_all(message, urgent=False, context=None):
    """Wrapper that normally queues announcements, but lets urgent doorbell
    alerts bypass routine queue traffic."""
    label = _context_label(context)
    channel = (context or {}).get("channel") if context else None

    if urgent and channel == "doorbell":
        logging.info(
            "[AUDIO QUEUE] Immediate dispatch for %s urgent=%s message=%r",
            label, urgent, message,
        )
        immediate_started = time.time()
        def _immediate_dispatch():
            dispatch_info = _execute_announce_all(message, urgent, context) or {}
            logging.info(
                "[AUDIO QUEUE] Immediate dispatch complete for %s in %.2fs wait_for_idle=%s",
                label, time.time() - immediate_started, dispatch_info.get("wait_for_idle"),
            )
        threading.Thread(
            target=_immediate_dispatch,
            daemon=True,
        ).start()
        return

    global _spooler_counter
    with spooler_lock:
        _spooler_counter += 1
        priority = 1 if urgent else 5
        queued_ts = time.time()
        network_speech_queue.put((priority, queued_ts, _spooler_counter, message, urgent, context))
        logging.info(
            "[AUDIO QUEUE] Enqueued %s urgent=%s queue_size=%s message=%r",
            label, urgent, network_speech_queue.qsize(), message,
        )


def _engine_to_tts_engine(engine):
    return {
        "gemini": "Gemini TTS",
        "edge": "Edge TTS (Natural)",
        "google": "Google Cloud",
        "sapi": "Local PC SAPI",
    }.get((engine or "gemini").strip().lower(), "Gemini TTS")


def resolve_tts_settings(category, config):
    category = category if category in {"doorbell", "utilities", "manual"} else "utilities"
    defaults = dict(config.get("tts_defaults", {}))
    alert = dict(config.get("tts_alerts", {}).get(category, {}))
    if alert.get("use_defaults", True):
        return {**defaults, "category": category, "use_defaults": True}
    merged = {**defaults, **alert}
    merged["category"] = category
    merged["use_defaults"] = False
    return merged


def play_notification(category, text, push=False):
    """Route category-based notifications through defaults or per-alert overrides."""
    category = (category or "utilities").strip().lower()
    if category not in {"doorbell", "utilities", "manual"}:
        category = "utilities"

    message = (text or "").strip()
    if not message:
        return

    config = cfg.load_config()
    settings = resolve_tts_settings(category, config)
    logging.info(
        "[NOTIFICATION TIMING] category=%s engine=%s speed=%s chars=%d",
        category, settings.get("engine"), settings.get("speed"), len(message),
    )
    priority = "CRITICAL" if category == "doorbell" else ("MEDIUM" if category == "manual" else "LOW")
    urgent = category == "doorbell" or priority in {"CRITICAL", "HIGH"}
    channel = "broadcast" if category == "manual" else category
    is_gemini = settings.get("engine") == "gemini"
    profile = {
        "model": config.get("gemini_tts_model") or GEMINI_TTS_MODEL,
        "voice": settings.get("gemini_voice", config.get("gemini_tts_voice", "Sulafat")),
        "priority": priority,
        "target": "all" if category == "manual" else "configured",
        "style": {
            "doorbell": "[urgent, clear, very fast]",
            "utilities": "[calm, helpful, clear]",
            "manual": "[friendly, clear]",
        }.get(category, "[clear]"),
        "dynamic_mood": settings.get("dynamic_mood", True),
        "speed": settings.get("speed", "normal"),
    }
    context = {
        "channel": channel,
        "manual": category == "manual",
        "tts_settings": settings,
        "tts_profile": profile if is_gemini else None,
        "tts_speed": settings.get("speed", "normal"),
        "target_speaker": profile.get("target", "configured"),
        "priority": priority,
        "push": bool(push),
    }
    announce_all(message, urgent=urgent, context=context)


# --- ANNOUNCE ALL (THE ACTUAL EXECUTION) ---
def _execute_announce_all(message, urgent=False, context=None):
    """UNIFIED DISPATCH: Supports Edge-TTS, Google Cloud, and Local PC SAPI.

    For fixed phrases that were pre-generated at startup, skips TTS synthesis
    entirely and serves the cached file URL directly.
    """
    dispatch_started = time.time()
    # Single load_config() call — all branches share this snapshot.
    config = cfg.load_config()
    tts_settings = (context or {}).get("tts_settings")
    if not tts_settings and (context or {}).get("channel") == "cinderella":
        tts_settings = resolve_tts_settings("utilities", config)
        context = dict(context or {})
        context["tts_settings"] = tts_settings
        if tts_settings.get("engine") == "gemini":
            context["tts_profile"] = {
                "model": config.get("gemini_tts_model") or GEMINI_TTS_MODEL,
                "voice": tts_settings.get("gemini_voice", config.get("gemini_tts_voice", "Sulafat")),
                "priority": "LOW",
                "target": "configured",
                "style": "[short, playful, clear]",
                "dynamic_mood": tts_settings.get("dynamic_mood", True),
                "speed": tts_settings.get("speed", "normal"),
            }
        context["tts_speed"] = tts_settings.get("speed", "normal")
        context["skip_speaker_wait"] = True
    if tts_settings:
        config["tts_engine"] = _engine_to_tts_engine(tts_settings.get("engine"))
        config["gemini_tts_voice"] = tts_settings.get("gemini_voice", config.get("gemini_tts_voice", "Sulafat"))
        config["edge_tts_voice"] = tts_settings.get("edge_voice", config.get("edge_tts_voice", "en-US-AriaNeural"))
        config["google_tts_tld"] = tts_settings.get("google_tld", config.get("google_tts_tld", "com"))
        config["local_voice_index"] = int(tts_settings.get("sapi_voice_index", config.get("local_voice_index", 1)))
        config["gemini_tts_min_interval_seconds"] = int(tts_settings.get("gemini_min_interval_seconds", config.get("gemini_tts_min_interval_seconds", 0)))
    tts_engine = config.get("tts_engine", "Edge TTS (Natural)")
    tts_profile = (context or {}).get("tts_profile")

    headers = _ha_auth_headers()
    label = _context_label(context)
    ha_targets, sonos_targets, alexa_targets, category, quiet_active = _collect_targets_for_context(config, context)

    logging.info(
        "[AUDIO DISPATCH] Starting %s urgent=%s targets ha=%d sonos=%d alexa=%d engine=%s config_load_to_targets=%.2fs",
        label, urgent, len(ha_targets), len(sonos_targets), len(alexa_targets), tts_engine, time.time() - dispatch_started,
    )

    if (context or {}).get("push"):
        push_title = "Viper Vision"
        if (context or {}).get("channel") == "broadcast":
            push_title = "Home Alert"
        threading.Thread(target=_send_text_pushover, args=(push_title, message), daemon=True).start()

    if not (quiet_active and category == "utilities"):
        speak_hd_pc(message)

    if config.get("enable_alexa", False) and alexa_targets:
        threading.Thread(target=_safe_alexa_announce, args=(message, alexa_targets, headers), daemon=True).start()

    # Check static phrase cache first — skip TTS generation entirely if we have
    # a pre-built file for this message and the engine/voice hasn't changed.
    cached_url = None if tts_profile else _get_cached_url(message, config)
    if cached_url:
        playback_started = time.time()
        logging.info("[AUDIO DISPATCH] Using pre-cached audio for: %r", message)
        for entity in ha_targets:
            threading.Thread(target=_safe_ha_play, args=(entity, cached_url, headers), daemon=True).start()
        if sonos_targets:
            _dispatch_to_sonos(cached_url, targets=sonos_targets)
        logging.info("[AUDIO DISPATCH] Submitted cached playback jobs for %s in %.2fs total=%.2fs", label, time.time() - playback_started, time.time() - dispatch_started)
        return {
            "ha": ha_targets,
            "sonos": sonos_targets,
            "alexa": alexa_targets,
            "wait_for_idle": bool(ha_targets or sonos_targets or alexa_targets) and not (context or {}).get("skip_speaker_wait", False),
        }

    # --- Standard TTS generation path ---
    tts_started = time.time()
    file_name = _generate_network_tts_file(
        message,
        "dispatch",
        config,
        tts_profile=tts_profile,
        speed_override=(context or {}).get("tts_speed"),
    )
    logging.info("[AUDIO DISPATCH] TTS path for %s completed in %.2fs file=%s", label, time.time() - tts_started, file_name)
    if file_name:
        playback_started = time.time()
        url = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"
        for entity in ha_targets:
            threading.Thread(target=_safe_ha_play, args=(entity, url, headers), daemon=True).start()
        if sonos_targets:
            _dispatch_to_sonos(url, targets=sonos_targets)
        logging.info("[AUDIO DISPATCH] Playback submission started for %s in %.2fs", label, time.time() - playback_started)

    logging.info("[AUDIO DISPATCH] Submitted playback jobs for %s total=%.2fs", label, time.time() - dispatch_started)
    return {
        "ha": ha_targets,
        "sonos": sonos_targets,
        "alexa": alexa_targets,
        "wait_for_idle": bool(ha_targets or sonos_targets or alexa_targets) and not (context or {}).get("skip_speaker_wait", False),
    }
