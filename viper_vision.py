import json
import logging
import io
import shutil
import subprocess
import time
import threading
import requests
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from PIL import Image
from google import genai
from google.genai import types

import viper_audio as audio
import viper_config as cfg

_pushover_session = requests.Session()

_gemini_client = None
_gemini_client_key = None
_gemini_client_lock = threading.Lock()


def get_gemini_client():
    global _gemini_client, _gemini_client_key
    api_key = cfg.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("Gemini API key is not configured.")
    with _gemini_client_lock:
        if _gemini_client is None or _gemini_client_key != api_key:
            _gemini_client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(api_version="v1beta")
            )
            _gemini_client_key = api_key
        return _gemini_client

# Cooldown tracking — guarded by a lock to prevent a race when front and back
# doorbells fire within the same window.
last_trigger = {"front": 0.0, "back": 0.0}
_trigger_lock = threading.Lock()

# Cache the ffmpeg binary path at import time — shutil.which does a PATH scan
# and the result will never change at runtime.
_FFMPEG_BIN: str = shutil.which(cfg.FFMPEG_BIN) or cfg.FFMPEG_BIN

# Serialise writes to the API usage log so concurrent doorbell events (front +
# back simultaneously) don't corrupt the JSON file.
_api_log_lock = threading.Lock()

# Maximum image dimension before we bother resizing for Gemini.
_MAX_IMAGE_DIM = 800

# Frame polling interval in seconds. 50ms gives 20 checks/second which is more
# than enough to catch a 5fps stream the moment a good frame lands.
_FRAME_POLL_INTERVAL = 0.05


def _capture_single_frame_fallback(rtsp_url: str, output_file: Path, timeout: float | None = None) -> str | None:
    """Last-resort frame grab: ask FFmpeg for one frame directly.
    This is slower than the live path but much more forgiving on slow-waking RTSP streams."""
    if timeout is None:
        timeout = max(10.0, float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS))

    try:
        output_file.unlink(missing_ok=True)
    except Exception:
        pass

    cmd = [
        _FFMPEG_BIN, "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-update", "1",
        str(output_file),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        if result.returncode == 0 and output_file.exists():
            try:
                if output_file.stat().st_size > 5000:
                    logging.info("[RTSP] Single-frame fallback succeeded! (%s)", output_file)
                    return str(output_file)
            except Exception:
                pass
    except Exception as e:
        logging.error("[RTSP FALLBACK ERROR] %s", e)

    return None


# ==========================================
# GEMINI CONNECTION WARMUP
# ==========================================
def warmup_gemini():
    """Send a minimal image to Gemini at startup to pre-warm the HTTP/2
    connection pool. The first real doorbell call then skips TCP + TLS
    negotiation entirely, saving 300–600ms on the first trigger."""
    try:
        logging.info("[GEMINI] Warming up connection pool...")
        # 4×4 grey JPEG — tiny enough that it costs almost nothing in tokens.
        img = Image.new("RGB", (4, 4), color=(100, 100, 100))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        warm_img = Image.open(buf)
        warm_img.load()
        get_gemini_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=[warm_img],
            config=types.GenerateContentConfig(
                system_instruction="Reply with one word.",
                temperature=0.0,
            ),
        )
        logging.info("[GEMINI] Connection pool warmed up.")
    except Exception as e:
        # Non-fatal — the real calls will still work, just slightly slower on
        # the first trigger.
        logging.warning("[GEMINI WARMUP] %s", e)


# ==========================================
# UNIFIED FRAME GRABBER
# ==========================================
def grab_frame(
    rtsp_url: str,
    output_dir: Path,
    prefix: str,
    min_bytes: int,
    fast_mode: bool = True,
    timeout: float | None = None,
) -> str | None:
    """Universal RTSP frame grabber for both the fast-pass and HD paths.

    This version is intentionally more forgiving than the ultra-aggressive
    startup path. It still exits early on a good frame, but if the live stream
    is slow to wake it will fall back to the largest frame seen and finally to
    a direct single-frame grab before giving up.
    """
    if timeout is None:
        timeout = float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS)

    glob_pattern = f"{prefix}_*.jpg"
    output_pattern = str(output_dir / f"{prefix}_%04d.jpg")

    for f in output_dir.glob(glob_pattern):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass

    vf_filter = "fps=4,scale=640:-1" if fast_mode else "fps=4"
    quality = "5" if fast_mode else "3"

    # Less aggressive than the previous version. The old -probesize 32 /
    # -analyzeduration 0 combo was faster on a perfect stream, but too brittle
    # for the slower back-door camera.
    cmd = [
        _FFMPEG_BIN, "-y",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vf", vf_filter,
        "-q:v", quality,
        output_pattern,
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start_time = time.time()
    best_frame = None
    largest_frame = None
    largest_size = 0
    logged_frame_sizes = {}

    try:
        while time.time() - start_time < timeout:
            frames = sorted(output_dir.glob(glob_pattern))

            if len(frames) >= 2:
                for frame in frames[:-1]:
                    try:
                        size = frame.stat().st_size
                        previous_logged_size = logged_frame_sizes.get(str(frame), 0)
                        if size >= 5000 and size - previous_logged_size >= 3000:
                            logged_frame_sizes[str(frame)] = size
                            logging.info(
                                "[RTSP CANDIDATE] %s size=%s bytes threshold=%s elapsed=%.2fs",
                                frame.name, size, min_bytes, time.time() - start_time,
                            )
                        if size > largest_size:
                            largest_size = size
                            largest_frame = str(frame)
                        if size >= min_bytes:
                            best_frame = str(frame)
                            logging.info(
                                "[RTSP] Quality threshold met: %s bytes >= %s bytes (%.2fs)",
                                size, min_bytes, time.time() - start_time,
                            )
                            break
                    except Exception:
                        pass

            if best_frame:
                break

            time.sleep(_FRAME_POLL_INTERVAL)

    finally:
        try:
            process.terminate()
            process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    time.sleep(0.05)

    chosen = best_frame or largest_frame

    # If the live path never produced anything usable, try the old direct
    # single-frame fallback before giving up.
    if not chosen:
        logging.warning("[RTSP] No usable live frame from %s after %.2fs; trying single-frame fallback...", rtsp_url, time.time() - start_time)
        fallback_path = output_dir / f"{prefix}_fallback.jpg"
        chosen = _capture_single_frame_fallback(rtsp_url, fallback_path, timeout=max(10.0, timeout))
        if chosen:
            return chosen
        logging.error("[RTSP] No frame received from %s after %.2fs", rtsp_url, time.time() - start_time)
        return None

    # If we never hit the threshold, still accept the largest frame as long as
    # it looks remotely real. This helps older/slower cameras that produce
    # smaller JPEGs but still usable images.
    if not best_frame:
        if largest_size >= 5000:
            logging.warning(
                "[RTSP] Threshold not met — using largest frame seen (%s bytes) after %.2fs",
                largest_size, time.time() - start_time,
            )
        else:
            logging.warning("[RTSP] Largest live frame was too small; trying single-frame fallback...")
            fallback_path = output_dir / f"{prefix}_fallback.jpg"
            fallback = _capture_single_frame_fallback(rtsp_url, fallback_path, timeout=max(10.0, timeout))
            if fallback:
                return fallback

    for f in output_dir.glob(glob_pattern):
        if str(f) != chosen:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    logging.info("[RTSP] Frame captured: %s (%.2fs)", Path(chosen).name, time.time() - start_time)
    return chosen


# ==========================================
# AI DESCRIPTION
# ==========================================
def get_ollama_description(image_path, prompt):
    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")

        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": cfg.load_config().get("ollama_model", "llama3.2-vision"),
                "prompt": prompt,
                "images": [img_data],
                "stream": False,
                "keep_alive": "24h"
            },
            timeout=60
        )
        return response.json().get("response", "Ollama failed to respond.").strip()
    except Exception as e:
        return f"Local AI Error: {str(e)}"


def _load_image_for_gemini(image_path: str):
    """Load image, only resizing if it actually exceeds the dimension limit.
    For fast-mode frames already scaled to 640px this is a no-op."""
    with open(image_path, "rb") as f:
        img_bytes = f.read()

    img = Image.open(io.BytesIO(img_bytes))
    if max(img.size) <= _MAX_IMAGE_DIM:
        img.load()
        return img

    img.load()
    img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM))
    return img


def get_gemini_description(image_path, prompt, model_name="gemini-2.5-flash"):
    started = time.time()
    try:
        img = _load_image_for_gemini(image_path)

        res = get_gemini_client().models.generate_content(
            model=model_name,
            contents=[img],
            config=types.GenerateContentConfig(
                system_instruction=prompt,
                temperature=0.2,
            ),
        )

        elapsed = time.time() - started
        logging.info("[AI TIMING] model=%s took %.2fs", model_name, elapsed)
        if res and res.usage_metadata:
            log_api_usage(res.usage_metadata)
        return res.text.strip() if res and res.text else "Activity detected."
    except Exception as e:
        elapsed = time.time() - started
        logging.error("[AI ERROR] model=%s after %.2fs: %s", model_name, elapsed, e)
        return "The AI service is currently unavailable."


def _description_is_weak(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True

    weak_markers = {
        "activity detected.",
        "the ai service is currently unavailable.",
        "the video feed is unavailable.",
        "video unavailable",
        "unknown",
    }
    if lowered in weak_markers:
        return True

    uncertainty_markers = (
        "unclear", "hard to tell", "cannot tell", "can't tell",
        "not sure", "maybe", "possibly", "appears to be", "seems to be",
    )
    if any(m in lowered for m in uncertainty_markers):
        return True

    tokens = (
        "person", "man", "woman", "child", "dog", "package", "box",
        "porch", "door", "railing", "planter", "tools", "siding",
        "yard", "steps", "stairs", "vehicle", "car",
    )
    concrete_hits = sum(1 for t in tokens if t in lowered)

    generic_scene_only = (
        "peaceful view", "quiet street", "cloudy sky", "neighborhood street",
        "overlooking a quiet street", "overlooks a neighborhood street",
        "houses and bare trees",
    )
    if any(p in lowered for p in generic_scene_only) and concrete_hits < 2:
        return True

    if len(lowered.split()) < 5:
        return True

    return concrete_hits == 0



def get_best_gemini_description(image_path, prompt):
    """Return the fastest strong Gemini description.

    Fast path:
      1. Ask gemini-2.5-flash first.
      2. If that result is strong, return immediately.
      3. Only escalate to gemini-3-flash-preview when the first answer is weak.

    This keeps the common-case doorbell path low-latency instead of waiting for
    the slower backup model every time.
    """
    first_model = "gemini-2.5-flash"
    second_model = "gemini-3-flash-preview"

    desc_fast = get_gemini_description(image_path, prompt, model_name=first_model)
    if not _description_is_weak(desc_fast):
        logging.info("[AI SELECT] Using fast-pass result from %s", first_model)
        return {"description": desc_fast, "model": first_model, "refined": False, "weak": False}

    logging.warning("[AI SELECT] Fast-pass result looked weak; escalating to %s", second_model)
    desc_refined = get_gemini_description(image_path, prompt, model_name=second_model)
    if not _description_is_weak(desc_refined):
        logging.info("[AI SELECT] Escalated result from %s replaced weak fast-pass", second_model)
        return {"description": desc_refined, "model": second_model, "refined": True, "weak": False}

    logging.warning("[AI SELECT] Both models returned weak results; keeping fast-pass")
    return {"description": desc_fast, "model": first_model, "refined": False, "weak": True}


# ==========================================
# CINDERELLA AI MESSAGE GENERATOR

# ==========================================
def generate_cinderella_message(event: str, source: str, error: str) -> str:
    """Call Gemini with a text-only prompt to generate a one-off funny Roborock
    announcement. No image is sent — this is pure language generation.

    Returns the generated message string, or an empty string on failure
    (the caller falls back to the configured message lists in that case).
    """
    try:
        config = cfg.load_config()
        prompt_template = config.get(
            "cinderella_ai_prompt",
            "Generate a short funny sentence under 20 words about a robot vacuum that is currently {event}. Reply with the sentence only.",
        )
        prompt = prompt_template.format(
            event=event or "doing something",
            source=source or "vacuum",
            error=error or "none",
        )

        res = get_gemini_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=1.0,   # high temp for creative variety
                max_output_tokens=60,
            ),
        )

        text = (res.text or "").strip()
        # Strip surrounding quotes if Gemini added them
        if text and text[0] in ('"', "'") and text[-1] == text[0]:
            text = text[1:-1].strip()

        if text:
            logging.info("[CINDERELLA AI] Generated: %r (event=%s source=%s)", text, event, source)
            return text

    except Exception as e:
        logging.warning("[CINDERELLA AI] Generation failed: %s", e)

    return ""

# ==========================================
# API USAGE LOGGING
# ==========================================
def log_api_usage(usage_metadata):
    current_month = datetime.now().strftime("%Y-%m")
    with _api_log_lock:
        data = {"period": current_month, "total_requests": 0, "prompt_tokens": 0, "response_tokens": 0}
        if cfg.API_LOG_PATH.exists():
            try:
                with open(cfg.API_LOG_PATH, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    if old_data.get("period") == current_month:
                        data = old_data
            except Exception:
                pass
        data["total_requests"] += 1
        data["prompt_tokens"]   += (usage_metadata.prompt_token_count or 0)
        data["response_tokens"] += (usage_metadata.candidates_token_count or 0)
        try:
            with open(cfg.API_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            logging.error(f"Failed to log API usage: {e}")


# ==========================================
# PUSHOVER
# ==========================================
def _send_pushover(location: str, description: str, image_bytes: bytes):
    if not cfg.PUSHOVER_API_TOKEN or not cfg.PUSHOVER_USER_KEY:
        return
    try:
        _pushover_session.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   cfg.PUSHOVER_API_TOKEN,
                "user":    cfg.PUSHOVER_USER_KEY,
                "title":   f"{location.title()} Activity",
                "message": description,
            },
            files={"attachment": ("snap.jpg", image_bytes, "image/jpeg")},
            timeout=10,
        )
    except Exception as e:
        logging.error(f"Pushover failure: {e}")


# ==========================================
# DOORBELL PROCESSING
# ==========================================
def process_doorbell(location, rtsp_url, key, dash_app, executor, trace_id=None, received_ts=None):
    try:
        time_start_total = time.time()
        trace_id = trace_id or f"doorbell-{key}-{int(time_start_total * 1000)}"
        received_ts = received_ts or time_start_total
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s worker_started=%.3fs",
            trace_id, key, time_start_total - received_ts,
        )

        # Thread-safe cooldown check.
        with _trigger_lock:
            if time_start_total - last_trigger[key] < cfg.TRIGGER_COOLDOWN_SECONDS:
                logging.info(
                    "[DOORBELL TIMING] trace=%s event=%s suppressed_by_cooldown elapsed=%.2fs",
                    trace_id, key, time_start_total - last_trigger[key],
                )
                return
            last_trigger[key] = time_start_total

        current_config = dash_app.config if dash_app else cfg.load_config()
        if not current_config.get("is_armed", True):
            logging.info("[DOORBELL TIMING] trace=%s event=%s ignored_disarmed", trace_id, key)
            return

        # Per-camera quality threshold — Doorbell 3 (front) vs 2nd Gen (back).
        min_bytes = cfg.FRONT_MIN_FRAME_BYTES if key == "front" else cfg.BACK_MIN_FRAME_BYTES
        hd_min_bytes = max(8000, min_bytes // 2)

        logging.info("--- MOTION DETECTED: %s trace=%s ---", location.upper(), trace_id)
        logging.info("[DOORBELL TIMING] trace=%s event=%s capture_pipeline_started min_bytes=%s hd_min_bytes=%s", trace_id, key, min_bytes, hd_min_bytes)
        if dash_app:
            dash_app.notify(f"Checking {location}...", priority=2, interrupt=True)
        chime_future = executor.submit(audio.sonos_instant_chime, location, trace_id)
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s instant_chime_submitted=%.3fs",
            trace_id, key, time.time() - time_start_total,
        )

        unique_id = int(time.time())

        # ── STAGE 1: FAST CAPTURE ──────────────────────────────────────────
        # grab_frame exits the moment a frame hits min_bytes — no fixed timeout
        # wait. fast_mode=True scales to 640px so files are smaller and cross
        # the threshold faster.
        capture_started = time.time()
        fast_frame = grab_frame(
            rtsp_url,
            output_dir = cfg.DATA_DIR,
            prefix     = f"fast_{key}_{unique_id}",
            min_bytes  = min_bytes,
            fast_mode  = True,
            timeout    = float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS),
        )
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s fast_capture=%.2fs total=%.2fs success=%s",
            trace_id, key, time.time() - capture_started, time.time() - time_start_total, bool(fast_frame),
        )

        active_p   = current_config.get("active_prompt", "Standard")
        sys_prompt = current_config["prompts"].get(active_p, "Describe the scene.")

        if fast_frame:
            first_model = "gemini-2.5-flash"
            description = get_gemini_description(fast_frame, sys_prompt, model_name=first_model)
            weak_first_pass = _description_is_weak(description)
            if weak_first_pass:
                logging.warning(
                    "[AI SELECT] Fast-pass result looked weak; speaking it now and refining in background"
                )
            else:
                logging.info("[AI SELECT] Using fast-pass result from %s", first_model)
        else:
            description     = "The video feed is unavailable."
            weak_first_pass = True

        logging.info("[AI VERDICT FAST] %s", description)
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s total_to_verdict=%.2fs",
            trace_id, key, time.time() - time_start_total,
        )

        executor.submit(audio.play_notification, "doorbell", description)
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s audio_notification_submitted=%.2fs",
            trace_id, key, time.time() - time_start_total,
        )
        if dash_app:
            dash_app.notify(description, priority=1, interrupt=True)

        # ── STAGE 2: BACKGROUND PUSHOVER / HD REFINEMENT ──────────────────
        def background_pushover_pipeline():
            hd_frame = None
            try:
                # If the first pass was strong and we still have the frame, use
                # it immediately — no need for another RTSP connection.
                if not weak_first_pass and fast_frame and Path(fast_frame).exists():
                    try:
                        with open(fast_frame, "rb") as f:
                            pushover_bytes = f.read()
                        _send_pushover(location, description, pushover_bytes)
                        logging.info("[PUSHOVER] Fast-pass Pushover sent for %s trace=%s.", key, trace_id)
                        return
                    except Exception as e:
                        logging.error("[PUSHOVER] Fast-pass send failed for %s: %s", key, e)

                # First pass was weak — refine in the background. Reuse the
                # fast frame first so speech is never blocked by the backup
                # model, and only capture HD if refinement still looks weak.
                logging.info("[PUSHOVER] First pass weak; refining in background for %s trace=%s", key, trace_id)
                target_frame = fast_frame
                if not target_frame or not Path(target_frame).exists():
                    hd_frame = grab_frame(
                        rtsp_url,
                        output_dir = cfg.DATA_DIR,
                        prefix     = f"latest_{key}_{unique_id}",
                        min_bytes  = hd_min_bytes,
                        fast_mode  = False,
                        timeout    = max(float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS), 20.0),
                    )
                    target_frame = hd_frame
                    if not target_frame or not Path(target_frame).exists():
                        logging.warning("[PUSHOVER] No usable frame for %s", key)
                        return

                pushover_description = description
                if weak_first_pass:
                    refined_description = get_gemini_description(target_frame, sys_prompt, model_name="gemini-3-flash-preview")
                    if not _description_is_weak(refined_description):
                        pushover_description = refined_description
                        logging.info("[PUSHOVER] Using refined description for %s", key)
                    else:
                        logging.info("[PUSHOVER] Refined still weak; keeping first-pass text for %s", key)
                        if not hd_frame:
                            logging.info("[PUSHOVER] Capturing HD frame for weak result photo %s trace=%s", key, trace_id)
                            hd_frame = grab_frame(
                                rtsp_url,
                                output_dir = cfg.DATA_DIR,
                                prefix     = f"latest_{key}_{unique_id}",
                                min_bytes  = hd_min_bytes,
                                fast_mode  = False,
                                timeout    = max(float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS), 20.0),
                            )
                            if hd_frame and Path(hd_frame).exists():
                                target_frame = hd_frame

                with open(target_frame, "rb") as f:
                    pushover_bytes = f.read()
                _send_pushover(location, pushover_description, pushover_bytes)
                logging.info("[TIMING] Pushover sent after HD/refinement path trace=%s total=%.2fs.", trace_id, time.time() - time_start_total)

            except Exception as e:
                logging.error(f"Pushover pipeline failed: {e}")
            finally:
                # Clean up all frames from this event.
                for pattern in (
                    f"fast_{key}_{unique_id}_*.jpg",
                    f"latest_{key}_{unique_id}_*.jpg",
                ):
                    for f in cfg.DATA_DIR.glob(pattern):
                        try: f.unlink(missing_ok=True)
                        except Exception: pass
                # Also clean up the single-file variants returned by grab_frame.
                for path_str in (fast_frame, hd_frame):
                    if path_str:
                        try: Path(path_str).unlink(missing_ok=True)
                        except Exception: pass

        executor.submit(background_pushover_pipeline)

    except Exception:
        logging.exception("Unhandled error in process_doorbell")
