import os
import sys
import json
import logging
import re
import shutil
import tempfile
import threading
from copy import deepcopy
from pathlib import Path

# ==========================================
# PATH LOGIC (EXE VS SCRIPT)
# ==========================================
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.absolute()

def get_data_dir():
    if getattr(sys, 'frozen', False):
        app_data = Path(os.getenv("APPDATA")) / "viper_vision_1.0"
        app_data.mkdir(parents=True, exist_ok=True)
        return app_data
    return Path(__file__).parent.absolute()

APP_DIR = get_app_dir()
DATA_DIR = get_data_dir()
BASE_DIR = DATA_DIR

CONFIG_FILE = DATA_DIR / "viper_config.json"
API_LOG_PATH = DATA_DIR / "api_usage.json"
LOG_FILE = DATA_DIR / "viper_dashboard_log.txt"

SONOS_AUDIO_DIR = DATA_DIR
SONOS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

CHIMES_DIR = DATA_DIR / "chimes"
CHIMES_DIR.mkdir(parents=True, exist_ok=True)

ROBOROCK_VACUUM_ERROR_CODES = [
    "lidar_blocked", "bumper_stuck", "wheels_suspended", "cliff_sensor_error",
    "main_brush_jammed", "side_brush_jammed", "wheels_jammed", "robot_trapped",
    "no_dustbin", "strainer_error", "compass_error", "low_battery", "charging_error",
    "battery_error", "wall_sensor_dirty", "robot_tilted", "side_brush_error",
    "fan_error", "dock", "optical_flow_sensor_dirt", "vertical_bumper_pressed",
    "dock_locator_error", "return_to_dock_fail", "nogo_zone_detected",
    "visual_sensor", "light_touch", "vibrarise_jammed", "robot_on_carpet",
    "filter_blocked", "invisible_wall_detected", "cannot_cross_carpet",
    "internal_error", "collect_dust_error_3", "collect_dust_error_4",
    "mopping_roller_1", "mopping_roller_error_2", "clear_water_box_hoare",
    "dirty_water_box_hoare", "sink_strainer_hoare", "clear_water_box_exception",
    "clear_brush_exception", "clear_brush_exception_2", "filter_screen_exception",
    "mopping_roller_2", "up_water_exception", "drain_water_exception",
    "temperature_protection", "clean_carousel_exception", "clean_carousel_water_full",
    "water_carriage_drop", "check_clean_carouse", "audio_error",
]

ROBOROCK_DOCK_ERROR_CODES = [
    "no_dustbin_or_filter", "auto_empty_dock_fan_error", "duct_blockage",
    "auto_empty_dock_voltage_error", "water_empty", "waste_water_tank_full",
    "maintenance_brush_jammed", "dirty_tank_latch_open", "no_dustbin",
    "cleaning_tank_full_or_blocked",
]


def _friendly_error_name(error_name: str) -> str:
    return error_name.replace("_", " ")


def _build_roborock_specific_error_messages() -> dict:
    messages = {}
    for name in ROBOROCK_VACUUM_ERROR_CODES:
        label = _friendly_error_name(name)
        messages[name] = [
            f"Cinderella reports {label}. Please inspect the vacuum.",
            f"The floor mission is blocked by {label}.",
            f"Robot vacuum complaint detected: {label}.",
        ]
    for name in ROBOROCK_DOCK_ERROR_CODES:
        key = f"dock_{name}"
        label = _friendly_error_name(name)
        messages[key] = [
            f"The dock reports {label}. Cinderella needs help at base.",
            f"Dock issue detected: {label}.",
            f"The charging station has filed a complaint: {label}.",
        ]
    return messages


def ensure_default_assets():
    """Copy bundled first-run assets into the writable data directory."""
    bundled_chimes = APP_DIR / "chimes"
    if bundled_chimes.exists() and CHIMES_DIR.exists():
        for src in bundled_chimes.iterdir():
            if src.is_file() and src.suffix.lower() in {".mp3", ".wav"}:
                dst = CHIMES_DIR / src.name
                if not dst.exists():
                    try:
                        shutil.copy2(src, dst)
                    except Exception as e:
                        logging.warning("Could not copy bundled chime %s: %s", src.name, e)

# Network & APIs
FLASK_PORT = int(os.getenv("FLASK_PORT", "5050"))
SONOS_PORT = int(os.getenv("SONOS_PORT", "8090"))
HA_IP = os.getenv("HA_IP", "192.168.4.49")
HA_PORT = os.getenv("HA_PORT", "8123")
PC_IP = os.getenv("PC_IP", "192.168.4.56")

FRONT_CAMERA_ID = os.getenv("FRONT_CAMERA_ID", "")
BACK_CAMERA_ID = os.getenv("BACK_CAMERA_ID", "")
RING_TOPIC_ROOT = os.getenv("RING_TOPIC_ROOT") or os.getenv("RING_LOCATION_ID", "")
FRONT_DOORBELL_MQTT_TOPIC = os.getenv("FRONT_DOORBELL_MQTT_TOPIC", "")
BACK_DOORBELL_MQTT_TOPIC = os.getenv("BACK_DOORBELL_MQTT_TOPIC", "")

RTSP_FRONT = os.getenv("RTSP_FRONT", f"rtsp://{HA_IP}:8554/{FRONT_CAMERA_ID}_live" if FRONT_CAMERA_ID else "")
RTSP_BACK = os.getenv("RTSP_BACK", f"rtsp://{HA_IP}:8554/{BACK_CAMERA_ID}_live" if BACK_CAMERA_ID else "")

_LOCAL_FFMPEG = APP_DIR / "ffmpeg.exe"
FFMPEG_BIN = os.getenv("FFMPEG_BIN") or (str(_LOCAL_FFMPEG) if _LOCAL_FFMPEG.exists() else "ffmpeg")
TRIGGER_COOLDOWN_SECONDS = int(os.getenv("TRIGGER_COOLDOWN_SECONDS", "30"))
# 13s is the hard timeout — the size-based exit in grab_frame should fire well
# before this on healthy cameras. This is the true last resort.
RTSP_CONNECT_TIMEOUT_SECONDS = int(os.getenv("RTSP_CONNECT_TIMEOUT_SECONDS", "18"))

# Per-camera minimum frame size thresholds for the quality gate.
# Ring cameras produce blurry wake-up frames of 15–40KB; a sharp exposure-stable
# frame is reliably 50KB+ for the Doorbell 3 and 40KB+ for the 2nd Gen.
# Tune these down if you see the system consistently falling back to timeout.
#   Doorbell 3 (front, 1080p, faster processor): 50 KB threshold
#   Doorbell 2nd Gen (back, 1080p, slower processor): 40 KB threshold
FRONT_MIN_FRAME_BYTES = int(os.getenv("FRONT_MIN_FRAME_BYTES", str(30_000)))
BACK_MIN_FRAME_BYTES  = int(os.getenv("BACK_MIN_FRAME_BYTES",  str(14_000)))

HA_TOKEN = os.getenv("HA_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_KEY")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_TOKEN")

# Refrigerator / ice maker entities
ICE_MAKER_SWITCH_ENTITY = os.getenv("ICE_MAKER_SWITCH_ENTITY", "switch.refrigerator_cubed_ice")
ICE_MAKER_KEEP_ON_ENTITY = os.getenv("ICE_MAKER_KEEP_ON_ENTITY", "input_boolean.keep_ice_maker_on")

# Dynamic Battery Discovery Keywords
BATTERY_KEYWORDS = ["battery", "power_level"]

COST_PER_INPUT_TOKEN = 0.10 / 1_000_000
COST_PER_OUTPUT_TOKEN = 0.40 / 1_000_000

# ==========================================
# GLOBAL STATE & CONFIG MANAGEMENT
# ==========================================
TARGET_SPEAKERS = []
ALEXA_DEVICES = []
SONOS_IPS = []
globals_lock = threading.Lock()

# In-memory config cache — only reloaded from disk on first access.
# save_config() updates the cache immediately so callers never read stale data.
_config_cache = None
_config_cache_lock = threading.Lock()
_config_write_lock = threading.Lock()
_config_write_version = 0

SPEAKER_TYPES = {"ha", "sonos", "alexa"}
BROADCAST_MODES = {"speak", "chime", "silent"}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
TTS_PROFILE_CATEGORIES = ("doorbell", "utilities", "manual")
TTS_PROFILE_PRIORITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
TTS_SPEEDS = {"relaxed", "normal", "brisk", "fast", "very_fast"}
TTS_ENGINES = {"gemini", "edge", "google", "sapi"}
DEFAULT_GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"

DEFAULT_SPEAKER_CONFIG = {
    "id": "",
    "type": "ha",
    "enabled": True,
    "doorbell": True,
    "utilities": True,
    "fridge": True,
    "quiet_hours_exempt": False,
}

CONFIG_SCHEMA = {
    "version": 1,
    "defaults": {
        "is_armed": True,
        "active_prompt": "Standard",
        "vision_engine": "Gemini (Cloud)",
        "ollama_model": "llama3.2-vision",
        "tts_engine": "Edge TTS (Natural)",
        "edge_tts_voice": "en-US-AriaNeural",
        "gemini_tts_voice": "Sulafat",
        "gemini_tts_model": DEFAULT_GEMINI_TTS_MODEL,
        "gemini_tts_keep_warm": False,
        "gemini_tts_heartbeat_seconds": 240,
        "gemini_tts_min_interval_seconds": 0,
        "tts_simple": {
            "mode": "natural_gemini",
            "personality": "warm",
            "dynamic_mood": True,
            "keep_warm": False,
            "speeds": {
                "doorbell": "fast",
                "utilities": "normal",
                "manual": "normal",
            },
        },
        "tts_defaults": {
            "engine": "gemini",
            "gemini_voice": "Sulafat",
            "edge_voice": "en-US-AriaNeural",
            "google_tld": "com",
            "sapi_voice_index": 1,
            "speed": "normal",
            "dynamic_mood": True,
            "keep_warm": False,
            "gemini_min_interval_seconds": 0,
        },
        "tts_alerts": {
            "doorbell": {
                "use_defaults": True,
                "engine": "gemini",
                "gemini_voice": "Kore",
                "edge_voice": "en-US-AriaNeural",
                "google_tld": "com",
                "sapi_voice_index": 1,
                "speed": "fast",
                "dynamic_mood": True,
            },
            "utilities": {
                "use_defaults": True,
                "engine": "gemini",
                "gemini_voice": "Sulafat",
                "edge_voice": "en-US-AriaNeural",
                "google_tld": "com",
                "sapi_voice_index": 1,
                "speed": "normal",
                "dynamic_mood": True,
            },
            "manual": {
                "use_defaults": True,
                "engine": "gemini",
                "gemini_voice": "Puck",
                "edge_voice": "en-US-AriaNeural",
                "google_tld": "com",
                "sapi_voice_index": 1,
                "speed": "normal",
                "dynamic_mood": True,
            },
        },
        "tts_profiles": {
            "doorbell": {
                "model": DEFAULT_GEMINI_TTS_MODEL,
                "voice": "Kore",
                "priority": "CRITICAL",
                "target": "configured",
                "style": "[urgent, clear, very fast]",
                "dynamic_mood": True,
                "speed": "fast",
            },
            "utilities": {
                "model": DEFAULT_GEMINI_TTS_MODEL,
                "voice": "Sulafat",
                "priority": "LOW",
                "target": "configured",
                "style": "[calm, clear]",
                "dynamic_mood": True,
                "speed": "normal",
            },
            "manual": {
                "model": DEFAULT_GEMINI_TTS_MODEL,
                "voice": "Puck",
                "priority": "MEDIUM",
                "target": "all",
                "style": "[friendly, clear]",
                "dynamic_mood": True,
                "speed": "normal",
            },
        },
        "local_voice_index": 1,
        "google_tts_tld": "com",
        "front_chime": "",
        "back_chime": "",
        "mute_local_pc": False,
        "enable_alexa": False,
        "ha_ip": "",
        "ha_port": HA_PORT,
        "ha_token": "",
        "gemini_api_key": "",
        "pushover_enabled": False,
        "pushover_user_key": "",
        "pushover_api_token": "",
        "front_camera_id": "",
        "back_camera_id": "",
        "ring_topic_root": "",
        "rtsp_front": "",
        "rtsp_back": "",
        "front_doorbell_mqtt_topic": "",
        "back_doorbell_mqtt_topic": "",
        "mqtt_host": "",
        "mqtt_port": "1883",
        "mqtt_username": "",
        "mqtt_password": "",
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        # ── Per-channel broadcast behaviour ────────────────────────────────
        # Fallback chain: fridge_open → fridge → default
        # mode: "speak" | "chime" | "silent"
        # chime: filename from chimes folder, or "" for built-in default tone
        "broadcast_channels": {
            "default":        {"mode": "speak",  "chime": ""},
            "fridge_open":    {"mode": "chime",  "chime": ""},
            "fridge_closed":  {"mode": "chime",  "chime": ""},
            "freezer_open":   {"mode": "chime",  "chime": ""},
            "freezer_closed": {"mode": "chime",  "chime": ""},
        },
        # ── Roborock / Cinderella ──────────────────────────────────────────
        "cinderella_enabled": True,
        "cinderella_ai_mode": False,
        "cinderella_ai_prompt": (
            "You are a dry, witty narrator for a robot vacuum cleaner named Cinderella. "
            "Generate exactly ONE short, funny sentence (under 20 words) reacting to the "
            "following robot event. Be creative, unexpected, and self-aware. "
            "Event: {event}. Source: {source}. Error detail: {error}. "
            "Reply with the sentence only — no quotes, no punctuation after the period."
        ),
        "prompts": {
            "Standard": "Analyze this security camera frame. Describe people and actions in a natural sentence. Mention clothing/packages. Under 25 words.",
            "Detailed": "Analyze this security camera frame. Describe people, actions, clothing, and environment in detail. Strictly under 40 words."
        },
        "speakers": {},
        "cinderella_messages": {
            "departure": [
                "The floor goblin has been released.",
                "Dust, your time has come.",
                "Cinderella has chosen violence against dirt."
            ],
            "washing": [
                "Mop spa day has begun.",
                "She is rinsing off the evidence.",
                "The mop is being reborn."
            ],
            "emptying": [
                "Dumping today's bad decisions.",
                "The dirt vault is full.",
                "She is disposing of the evidence."
            ],
            "drying": [
                "Drying cycle engaged.",
                "The mop is becoming socially acceptable again.",
                "Moisture is being aggressively removed."
            ],
            "returning": [
                "Returning home like she pays rent.",
                "Cinderella is done being brave.",
                "Retreating with dignity... barely."
            ],
            "victory": [
                "The floor has been defeated.",
                "Cinderella demands recognition.",
                "Victory has been achieved."
            ],
            "paused": [
                "Paused for existential reasons.",
                "Cinderella is buffering.",
                "The robot has stopped and is judging silently."
            ],
            "status_update": [
                "Cinderella has entered a weird little robot state.",
                "The vacuum has changed modes and would like attention.",
                "Cinderella reports a status change from the floor front."
            ],
            "vacuum_error_templates": [
                "Cinderella has entered her villain arc. Error: {error}.",
                "The robot is having a moment. Error: {error}.",
                "This is not going well. Error: {error}."
            ],
            "dock_error_templates": [
                "The dock is being weird again. Problem: {error}.",
                "Cinderella's parking spot has opinions. Problem: {error}.",
                "Dock drama detected. Issue: {error}."
            ],
            "specific_errors": {
                **_build_roborock_specific_error_messages(),
                "water_carriage_drop": [
                    "Cinderella dropped the water carriage like a soap opera prop.",
                    "The water carriage has left the chat.",
                    "Hydration system drama. Water carriage drop."
                ]
            }
        }
    },
    "speakers": {
        "type": "mapping",
        "allowed_types": sorted(SPEAKER_TYPES),
        "defaults": deepcopy(DEFAULT_SPEAKER_CONFIG),
    },
    "validation": {
        "broadcast_modes": sorted(BROADCAST_MODES),
        "time_format": "HH:MM",
        "unknown_keys": "preserve",
    },
}


def get_config_schema():
    """Return the config schema and defaults used by the loader."""
    return deepcopy(CONFIG_SCHEMA)


def get_default_config():
    return deepcopy(CONFIG_SCHEMA["defaults"])


def _deep_merge(default, current):
    if not isinstance(default, dict) or not isinstance(current, dict):
        return current
    merged = deepcopy(default)
    for key, value in current.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_str(value, default=""):
    if value is None:
        return default
    return str(value)


def _as_int(value, default=0, minimum=None):
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    if minimum is not None:
        normalized = max(minimum, normalized)
    return normalized


def _normalize_time(value, default):
    value = _as_str(value, default).strip()
    return value if TIME_RE.match(value) else default


def _normalize_prompt_map(value, default):
    if not isinstance(value, dict):
        return deepcopy(default)
    normalized = {}
    for name, prompt in value.items():
        key = _as_str(name).strip()
        if key:
            normalized[key] = _as_str(prompt)
    return normalized or deepcopy(default)


def _normalize_string_list(value, default):
    if not isinstance(value, list):
        return deepcopy(default)
    normalized = [_as_str(item) for item in value if item is not None]
    return normalized or deepcopy(default)


def _normalize_cinderella_messages(value, default):
    if not isinstance(value, dict):
        return deepcopy(default)
    normalized = _deep_merge(default, value)
    for key, default_value in default.items():
        current = normalized.get(key)
        if isinstance(default_value, dict):
            if not isinstance(current, dict):
                normalized[key] = deepcopy(default_value)
                continue
            for subkey, subdefault in default_value.items():
                current[subkey] = _normalize_string_list(current.get(subkey), subdefault)
        else:
            normalized[key] = _normalize_string_list(current, default_value)
    return normalized


def _normalize_broadcast_channels(value, default):
    channels = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, channels)
    fallback = default["default"]
    for channel, settings in list(normalized.items()):
        if not isinstance(settings, dict):
            settings = {}
        mode = _as_str(settings.get("mode", fallback["mode"]), fallback["mode"]).strip().lower()
        if mode not in BROADCAST_MODES:
            mode = fallback["mode"]
        normalized[channel] = {
            **settings,
            "mode": mode,
            "chime": _as_str(settings.get("chime", fallback["chime"])).strip(),
        }
    return normalized


def _normalize_tts_profiles(value, default):
    profiles = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, profiles)
    for category in TTS_PROFILE_CATEGORIES:
        fallback = default[category]
        current = normalized.get(category)
        if not isinstance(current, dict):
            current = {}
        priority = _as_str(current.get("priority", fallback["priority"]), fallback["priority"]).strip().upper()
        if priority not in TTS_PROFILE_PRIORITIES:
            priority = fallback["priority"]
        normalized[category] = {
            **current,
            "model": _as_str(current.get("model", fallback["model"]), fallback["model"]).strip() or fallback["model"],
            "voice": _as_str(current.get("voice", fallback["voice"]), fallback["voice"]).strip() or fallback["voice"],
            "priority": priority,
            "target": _as_str(current.get("target", fallback["target"]), fallback["target"]).strip() or fallback["target"],
            "style": _as_str(current.get("style", fallback["style"]), fallback["style"]).strip(),
            "dynamic_mood": _as_bool(current.get("dynamic_mood"), fallback.get("dynamic_mood", True)),
            "speed": _as_str(current.get("speed", fallback.get("speed", "normal")), fallback.get("speed", "normal")).strip().lower()
            if _as_str(current.get("speed", fallback.get("speed", "normal")), fallback.get("speed", "normal")).strip().lower() in TTS_SPEEDS
            else fallback.get("speed", "normal"),
        }
    return normalized


def _normalize_tts_simple(value, default):
    simple = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, simple)
    modes = {"fast_reliable", "natural_gemini", "offline_fallback"}
    personalities = {"warm", "clear", "firm", "upbeat"}
    mode = _as_str(normalized.get("mode"), default["mode"]).strip().lower()
    personality = _as_str(normalized.get("personality"), default["personality"]).strip().lower()
    if mode not in modes:
        mode = default["mode"]
    if personality not in personalities:
        personality = default["personality"]
    normalized["mode"] = mode
    normalized["personality"] = personality
    normalized["dynamic_mood"] = _as_bool(normalized.get("dynamic_mood"), default["dynamic_mood"])
    normalized["keep_warm"] = _as_bool(normalized.get("keep_warm"), default["keep_warm"])
    raw_speeds = normalized.get("speeds") if isinstance(normalized.get("speeds"), dict) else {}
    default_speeds = default.get("speeds", {})
    speeds = {}
    for category in TTS_PROFILE_CATEGORIES:
        fallback = default_speeds.get(category, "normal")
        speed = _as_str(raw_speeds.get(category, fallback), fallback).strip().lower()
        speeds[category] = speed if speed in TTS_SPEEDS else fallback
    normalized["speeds"] = speeds
    return normalized


def _normalize_tts_settings(value, default, *, include_use_defaults=False):
    settings = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, settings)
    engine = _as_str(normalized.get("engine"), default["engine"]).strip().lower()
    speed = _as_str(normalized.get("speed"), default["speed"]).strip().lower()
    normalized["engine"] = engine if engine in TTS_ENGINES else default["engine"]
    normalized["gemini_voice"] = _as_str(normalized.get("gemini_voice"), default["gemini_voice"]).strip() or default["gemini_voice"]
    normalized["edge_voice"] = _as_str(normalized.get("edge_voice"), default["edge_voice"]).strip() or default["edge_voice"]
    normalized["google_tld"] = _as_str(normalized.get("google_tld"), default.get("google_tld", "com")).strip() or default.get("google_tld", "com")
    normalized["sapi_voice_index"] = _as_int(normalized.get("sapi_voice_index"), default["sapi_voice_index"], minimum=0)
    normalized["speed"] = speed if speed in TTS_SPEEDS else default["speed"]
    normalized["dynamic_mood"] = _as_bool(normalized.get("dynamic_mood"), default["dynamic_mood"])
    if "keep_warm" in default:
        normalized["keep_warm"] = _as_bool(normalized.get("keep_warm"), default["keep_warm"])
    if "gemini_min_interval_seconds" in default:
        normalized["gemini_min_interval_seconds"] = _as_int(
            normalized.get("gemini_min_interval_seconds"),
            default["gemini_min_interval_seconds"],
            minimum=0,
        )
    if include_use_defaults:
        normalized["use_defaults"] = _as_bool(normalized.get("use_defaults"), default.get("use_defaults", True))
    return normalized


def _normalize_tts_alerts(value, default):
    alerts = value if isinstance(value, dict) else {}
    normalized = {}
    for category in TTS_PROFILE_CATEGORIES:
        normalized[category] = _normalize_tts_settings(
            alerts.get(category),
            default[category],
            include_use_defaults=True,
        )
    return normalized


def normalize_speaker_settings(config_data: dict):
    """Backfill and validate speaker routing settings on older configs."""
    if not isinstance(config_data, dict):
        config_data = {}
    raw_speakers = config_data.get("speakers", {})
    if not isinstance(raw_speakers, dict):
        raw_speakers = {}

    speakers = {}
    for raw_name, raw_spk in raw_speakers.items():
        name = _as_str(raw_name).strip()
        if not name:
            continue

        if isinstance(raw_spk, str):
            spk = {"id": raw_spk, "type": "ha"}
        elif isinstance(raw_spk, dict):
            spk = deepcopy(raw_spk)
        else:
            spk = {}

        normalized = _deep_merge(DEFAULT_SPEAKER_CONFIG, spk)
        spk_type = _as_str(normalized.get("type"), DEFAULT_SPEAKER_CONFIG["type"]).strip().lower()
        if spk_type not in SPEAKER_TYPES:
            logging.warning("Invalid speaker type %r for %s; using ha", spk_type, name)
            spk_type = DEFAULT_SPEAKER_CONFIG["type"]

        normalized["id"] = _as_str(normalized.get("id"), "").strip()
        normalized["type"] = spk_type
        normalized["enabled"] = _as_bool(normalized.get("enabled"), True)
        normalized["doorbell"] = _as_bool(normalized.get("doorbell"), True)
        normalized["utilities"] = _as_bool(normalized.get("utilities"), True)
        normalized["fridge"] = _as_bool(normalized.get("fridge"), True)
        normalized["quiet_hours_exempt"] = _as_bool(normalized.get("quiet_hours_exempt"), False)
        speakers[name] = normalized

    config_data["speakers"] = speakers
    return config_data


def validate_and_normalize_config(config_data):
    """Merge user config with schema defaults, preserve unknown keys, and normalize known fields."""
    defaults = get_default_config()
    if not isinstance(config_data, dict):
        config_data = {}

    normalized = _deep_merge(defaults, config_data)
    normalized["is_armed"] = _as_bool(normalized.get("is_armed"), defaults["is_armed"])
    normalized["active_prompt"] = _as_str(normalized.get("active_prompt"), defaults["active_prompt"]).strip() or defaults["active_prompt"]
    normalized["vision_engine"] = _as_str(normalized.get("vision_engine"), defaults["vision_engine"]).strip() or defaults["vision_engine"]
    normalized["ollama_model"] = _as_str(normalized.get("ollama_model"), defaults["ollama_model"]).strip() or defaults["ollama_model"]
    normalized["tts_engine"] = _as_str(normalized.get("tts_engine"), defaults["tts_engine"]).strip() or defaults["tts_engine"]
    normalized["edge_tts_voice"] = _as_str(normalized.get("edge_tts_voice"), defaults["edge_tts_voice"]).strip() or defaults["edge_tts_voice"]
    normalized["gemini_tts_voice"] = _as_str(normalized.get("gemini_tts_voice"), defaults["gemini_tts_voice"]).strip() or defaults["gemini_tts_voice"]
    normalized["gemini_tts_model"] = _as_str(normalized.get("gemini_tts_model"), defaults["gemini_tts_model"]).strip() or defaults["gemini_tts_model"]
    normalized["gemini_tts_keep_warm"] = _as_bool(normalized.get("gemini_tts_keep_warm"), defaults["gemini_tts_keep_warm"])
    normalized["gemini_tts_heartbeat_seconds"] = _as_int(
        normalized.get("gemini_tts_heartbeat_seconds"),
        defaults["gemini_tts_heartbeat_seconds"],
        minimum=60,
    )
    normalized["gemini_tts_min_interval_seconds"] = _as_int(
        normalized.get("gemini_tts_min_interval_seconds"),
        defaults["gemini_tts_min_interval_seconds"],
        minimum=0,
    )
    normalized["tts_simple"] = _normalize_tts_simple(normalized.get("tts_simple"), defaults["tts_simple"])
    normalized["tts_defaults"] = _normalize_tts_settings(normalized.get("tts_defaults"), defaults["tts_defaults"])
    normalized["tts_alerts"] = _normalize_tts_alerts(normalized.get("tts_alerts"), defaults["tts_alerts"])
    normalized["tts_profiles"] = _normalize_tts_profiles(normalized.get("tts_profiles"), defaults["tts_profiles"])
    normalized["local_voice_index"] = _as_int(normalized.get("local_voice_index"), defaults["local_voice_index"], minimum=0)
    normalized["google_tts_tld"] = _as_str(normalized.get("google_tts_tld"), defaults["google_tts_tld"]).strip() or defaults["google_tts_tld"]
    normalized["front_chime"] = _as_str(normalized.get("front_chime"), defaults["front_chime"]).strip()
    normalized["back_chime"] = _as_str(normalized.get("back_chime"), defaults["back_chime"]).strip()
    normalized["mute_local_pc"] = _as_bool(normalized.get("mute_local_pc"), defaults["mute_local_pc"])
    normalized["enable_alexa"] = _as_bool(normalized.get("enable_alexa"), defaults["enable_alexa"])
    normalized["ha_ip"] = _as_str(normalized.get("ha_ip"), defaults["ha_ip"]).strip() or defaults["ha_ip"]
    normalized["ha_port"] = _as_str(normalized.get("ha_port"), defaults["ha_port"]).strip() or defaults["ha_port"]
    normalized["ha_token"] = _as_str(normalized.get("ha_token"), defaults["ha_token"]).strip()
    normalized["gemini_api_key"] = _as_str(normalized.get("gemini_api_key"), defaults["gemini_api_key"]).strip()
    normalized["pushover_enabled"] = _as_bool(normalized.get("pushover_enabled"), defaults["pushover_enabled"])
    normalized["pushover_user_key"] = _as_str(normalized.get("pushover_user_key"), defaults["pushover_user_key"]).strip()
    normalized["pushover_api_token"] = _as_str(normalized.get("pushover_api_token"), defaults["pushover_api_token"]).strip()
    normalized["front_camera_id"] = _as_str(normalized.get("front_camera_id"), defaults["front_camera_id"]).strip()
    normalized["back_camera_id"] = _as_str(normalized.get("back_camera_id"), defaults["back_camera_id"]).strip()
    normalized["ring_topic_root"] = _as_str(normalized.get("ring_topic_root"), defaults["ring_topic_root"]).strip().strip("/")
    normalized["rtsp_front"] = _as_str(normalized.get("rtsp_front"), defaults["rtsp_front"]).strip()
    normalized["rtsp_back"] = _as_str(normalized.get("rtsp_back"), defaults["rtsp_back"]).strip()
    normalized["front_doorbell_mqtt_topic"] = _as_str(normalized.get("front_doorbell_mqtt_topic"), defaults["front_doorbell_mqtt_topic"]).strip()
    normalized["back_doorbell_mqtt_topic"] = _as_str(normalized.get("back_doorbell_mqtt_topic"), defaults["back_doorbell_mqtt_topic"]).strip()
    normalized["mqtt_host"] = _as_str(normalized.get("mqtt_host"), defaults["mqtt_host"]).strip()
    normalized["mqtt_port"] = _as_str(normalized.get("mqtt_port"), defaults["mqtt_port"]).strip() or defaults["mqtt_port"]
    normalized["mqtt_username"] = _as_str(normalized.get("mqtt_username"), defaults["mqtt_username"]).strip()
    normalized["mqtt_password"] = _as_str(normalized.get("mqtt_password"), defaults["mqtt_password"]).strip()
    normalized["quiet_hours_enabled"] = _as_bool(normalized.get("quiet_hours_enabled"), defaults["quiet_hours_enabled"])
    normalized["quiet_hours_start"] = _normalize_time(normalized.get("quiet_hours_start"), defaults["quiet_hours_start"])
    normalized["quiet_hours_end"] = _normalize_time(normalized.get("quiet_hours_end"), defaults["quiet_hours_end"])
    normalized["broadcast_channels"] = _normalize_broadcast_channels(normalized.get("broadcast_channels"), defaults["broadcast_channels"])
    normalized["cinderella_enabled"] = _as_bool(normalized.get("cinderella_enabled"), defaults["cinderella_enabled"])
    normalized["cinderella_ai_mode"] = _as_bool(normalized.get("cinderella_ai_mode"), defaults["cinderella_ai_mode"])
    normalized["cinderella_ai_prompt"] = _as_str(normalized.get("cinderella_ai_prompt"), defaults["cinderella_ai_prompt"]) or defaults["cinderella_ai_prompt"]
    normalized["prompts"] = _normalize_prompt_map(normalized.get("prompts"), defaults["prompts"])
    if normalized["active_prompt"] not in normalized["prompts"]:
        normalized["active_prompt"] = next(iter(normalized["prompts"]))
    normalized["cinderella_messages"] = _normalize_cinderella_messages(normalized.get("cinderella_messages"), defaults["cinderella_messages"])
    return normalize_speaker_settings(normalized)

def read_config_file(path=CONFIG_FILE):
    """Safely read raw JSON from disk, returning defaults if the file is missing or invalid."""
    if not path.exists():
        return get_default_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logging.error("Config JSON is invalid in %s: %s", path, e)
        return get_default_config()
    except OSError as e:
        logging.error("Config read failed for %s: %s", path, e)
        return get_default_config()
    if not isinstance(data, dict):
        logging.error("Config root must be an object in %s", path)
        return get_default_config()
    return data


def load_config():
    """Return the normalized config, cached to avoid hot-path disk reads."""
    global _config_cache
    with _config_cache_lock:
        if _config_cache is not None:
            return deepcopy(_config_cache)
        _config_cache = validate_and_normalize_config(read_config_file())
        return deepcopy(_config_cache)


def read_config():
    """Public safe-read helper. Kept separate from load_config for newer callers."""
    return load_config()


def get_ha_settings(config_data=None, *, include_env=True):
    """Return Home Assistant connection settings from config, with env-backed defaults."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    if not include_env:
        return {
            "ha_ip": data.get("ha_ip") or "",
            "ha_port": data.get("ha_port") or "8123",
            "ha_token": data.get("ha_token") or "",
        }
    return {
        "ha_ip": data.get("ha_ip") or HA_IP,
        "ha_port": data.get("ha_port") or HA_PORT,
        "ha_token": data.get("ha_token") or HA_TOKEN,
    }


def get_api_settings(config_data=None, *, include_env=True):
    """Return Gemini and Pushover settings. Pushover is optional."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    if not include_env:
        return {
            "gemini_api_key": data.get("gemini_api_key") or "",
            "pushover_enabled": bool(data.get("pushover_enabled")),
            "pushover_user_key": data.get("pushover_user_key") or "",
            "pushover_api_token": data.get("pushover_api_token") or "",
        }
    user_key = data.get("pushover_user_key") or PUSHOVER_USER_KEY or ""
    api_token = data.get("pushover_api_token") or PUSHOVER_API_TOKEN or ""
    return {
        "gemini_api_key": data.get("gemini_api_key") or GEMINI_API_KEY or "",
        "pushover_enabled": bool(data.get("pushover_enabled") or (user_key and api_token)),
        "pushover_user_key": user_key,
        "pushover_api_token": api_token,
    }


def _derive_rtsp_url(ha_ip, camera_id):
    ha_ip = _as_str(ha_ip, "").strip()
    camera_id = _as_str(camera_id, "").strip()
    if not ha_ip or not camera_id:
        return ""
    return f"rtsp://{ha_ip}:8554/{camera_id}_live"


def _derive_ring_topic(ring_topic_root, camera_id):
    root = _as_str(ring_topic_root, "").strip().strip("/")
    camera_id = _as_str(camera_id, "").strip()
    if not root or not camera_id:
        return ""
    return f"ring/{root}/camera/{camera_id}/motion/state"


def get_resolved_doorbell_settings(config_data=None, *, include_env=True):
    """Return raw and resolved doorbell stream/MQTT settings.

    Resolution order keeps old installs working without forcing private values
    into clean first-run setup:
      RTSP: saved URL -> env URL -> HA host + camera ID -> blank
      MQTT topic: saved topic -> env topic -> Ring root + camera ID -> blank
    """
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    ha_settings = get_ha_settings(data, include_env=include_env)
    ha_ip = ha_settings.get("ha_ip") or ""

    front_camera_id = data.get("front_camera_id") or (FRONT_CAMERA_ID if include_env else "")
    back_camera_id = data.get("back_camera_id") or (BACK_CAMERA_ID if include_env else "")
    ring_topic_root = data.get("ring_topic_root") or (RING_TOPIC_ROOT if include_env else "")

    env_rtsp_front = os.getenv("RTSP_FRONT", "") if include_env else ""
    env_rtsp_back = os.getenv("RTSP_BACK", "") if include_env else ""
    env_front_topic = FRONT_DOORBELL_MQTT_TOPIC if include_env else ""
    env_back_topic = BACK_DOORBELL_MQTT_TOPIC if include_env else ""

    resolved_rtsp_front = (
        data.get("rtsp_front")
        or env_rtsp_front
        or _derive_rtsp_url(ha_ip, front_camera_id)
        or ""
    )
    resolved_rtsp_back = (
        data.get("rtsp_back")
        or env_rtsp_back
        or _derive_rtsp_url(ha_ip, back_camera_id)
        or ""
    )
    resolved_front_topic = (
        data.get("front_doorbell_mqtt_topic")
        or env_front_topic
        or _derive_ring_topic(ring_topic_root, front_camera_id)
        or ""
    )
    resolved_back_topic = (
        data.get("back_doorbell_mqtt_topic")
        or env_back_topic
        or _derive_ring_topic(ring_topic_root, back_camera_id)
        or ""
    )

    return {
        "rtsp_front": resolved_rtsp_front,
        "rtsp_back": resolved_rtsp_back,
        "front_doorbell_mqtt_topic": resolved_front_topic,
        "back_doorbell_mqtt_topic": resolved_back_topic,
        "front_camera_id": front_camera_id or "",
        "back_camera_id": back_camera_id or "",
        "ring_topic_root": ring_topic_root or "",
        "mqtt_host": data.get("mqtt_host") or (ha_ip if include_env else "") or "",
        "mqtt_port": data.get("mqtt_port") or "1883",
        "mqtt_username": data.get("mqtt_username") or "",
        "mqtt_password": data.get("mqtt_password") or "",
        "raw_rtsp_front": data.get("rtsp_front") or "",
        "raw_rtsp_back": data.get("rtsp_back") or "",
        "raw_front_doorbell_mqtt_topic": data.get("front_doorbell_mqtt_topic") or "",
        "raw_back_doorbell_mqtt_topic": data.get("back_doorbell_mqtt_topic") or "",
        "raw_front_camera_id": data.get("front_camera_id") or "",
        "raw_back_camera_id": data.get("back_camera_id") or "",
        "raw_ring_topic_root": data.get("ring_topic_root") or "",
        "derived_rtsp_front": _derive_rtsp_url(ha_ip, front_camera_id),
        "derived_rtsp_back": _derive_rtsp_url(ha_ip, back_camera_id),
        "derived_front_doorbell_mqtt_topic": _derive_ring_topic(ring_topic_root, front_camera_id),
        "derived_back_doorbell_mqtt_topic": _derive_ring_topic(ring_topic_root, back_camera_id),
    }


def get_doorbell_settings(config_data=None, *, include_env=True):
    """Return doorbell stream and MQTT trigger settings."""
    return get_resolved_doorbell_settings(config_data, include_env=include_env)


def write_config_file(config_data: dict, path=CONFIG_FILE):
    """Validate and atomically write config to disk. Returns the normalized config."""
    normalized = validate_and_normalize_config(config_data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.stem}_",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=4)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.remove(temp_name)
        except Exception:
            pass
    return normalized


def _write_config_to_disk(config_data: dict, version: int):
    """Write latest config atomically on a background thread."""
    global _config_write_version
    try:
        with _config_write_lock:
            if version != _config_write_version:
                return
            write_config_file(config_data)
    except Exception as e:
        logging.error("Failed to write config to disk: %s", e)


def save_config(config_data):
    """Update in-memory cache immediately, flush latest version to disk in the background."""
    global _config_cache, _config_write_version
    config_data = validate_and_normalize_config(config_data)
    with _config_cache_lock:
        _config_cache = deepcopy(config_data)
    with _config_write_lock:
        _config_write_version += 1
        version = _config_write_version
    sync_globals_from_config()
    threading.Thread(
        target=_write_config_to_disk,
        args=(deepcopy(config_data), version),
        daemon=True,
    ).start()


def write_config(config_data):
    """Synchronously validate, atomically write, update cache, and sync globals."""
    global _config_cache, _config_write_version
    config_data = validate_and_normalize_config(config_data)
    with _config_write_lock:
        _config_write_version += 1
        config_data = write_config_file(config_data)
    with _config_cache_lock:
        _config_cache = deepcopy(config_data)
    sync_globals_from_config()
    return deepcopy(config_data)


def update_config(updater, *, write=True):
    """Safely mutate config with a callback and optionally persist it.

    The updater receives a mutable config dict. It may either mutate in place
    and return None, or return a replacement dict.
    """
    current = load_config()
    result = updater(current)
    updated = current if result is None else result
    updated = validate_and_normalize_config(updated)
    if write:
        save_config(updated)
    else:
        global _config_cache
        with _config_cache_lock:
            _config_cache = deepcopy(updated)
        sync_globals_from_config()
    return deepcopy(updated)

def invalidate_config_cache():

    """Force next load_config() to re-read from disk."""
    global _config_cache
    with _config_cache_lock:
        _config_cache = None

def sync_globals_from_config():
    global TARGET_SPEAKERS, ALEXA_DEVICES, SONOS_IPS, HA_IP, HA_PORT, HA_TOKEN, GEMINI_API_KEY, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN
    global FRONT_CAMERA_ID, BACK_CAMERA_ID, RING_TOPIC_ROOT, RTSP_FRONT, RTSP_BACK
    with globals_lock:
        TARGET_SPEAKERS.clear()
        ALEXA_DEVICES.clear()
        SONOS_IPS.clear()
        try:
            data = load_config()
            HA_IP = data.get("ha_ip") or HA_IP
            HA_PORT = data.get("ha_port") or HA_PORT
            HA_TOKEN = data.get("ha_token") or HA_TOKEN
            GEMINI_API_KEY = data.get("gemini_api_key") or GEMINI_API_KEY
            doorbell = get_resolved_doorbell_settings(data, include_env=True)
            FRONT_CAMERA_ID = doorbell.get("front_camera_id") or FRONT_CAMERA_ID
            BACK_CAMERA_ID = doorbell.get("back_camera_id") or BACK_CAMERA_ID
            RING_TOPIC_ROOT = doorbell.get("ring_topic_root") or RING_TOPIC_ROOT
            RTSP_FRONT = doorbell.get("rtsp_front") or ""
            RTSP_BACK = doorbell.get("rtsp_back") or ""
            if data.get("pushover_enabled"):
                PUSHOVER_USER_KEY = data.get("pushover_user_key") or PUSHOVER_USER_KEY
                PUSHOVER_API_TOKEN = data.get("pushover_api_token") or PUSHOVER_API_TOKEN
            else:
                PUSHOVER_USER_KEY = ""
                PUSHOVER_API_TOKEN = ""
            for _, spk in data.get("speakers", {}).items():
                if spk.get("enabled", True):
                    t = spk.get("type")
                    if t == "ha":
                        TARGET_SPEAKERS.append(spk["id"])
                    elif t == "alexa" and data.get("enable_alexa", False):
                        ALEXA_DEVICES.append(spk["id"])
                    elif t == "sonos":
                        SONOS_IPS.append(spk["id"])
        except Exception:
            pass
