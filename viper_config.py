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

import viper_secrets

# ==========================================
# PATH LOGIC (EXE VS SCRIPT)
# ==========================================
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.absolute()

def get_user_data_dir():
    root = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(root) / "viper_vision_1.0"


def get_data_dir():
    app_data = get_user_data_dir()
    if getattr(sys, 'frozen', False):
        app_data.mkdir(parents=True, exist_ok=True)
        return app_data

    source_dir = Path(__file__).parent.absolute()
    use_appdata = os.getenv("VIPER_USE_APPDATA_CONFIG", "").strip().lower() in {"1", "true", "yes", "on"}
    if use_appdata or (app_data / "viper_config.json").exists():
        app_data.mkdir(parents=True, exist_ok=True)
        return app_data
    return source_dir

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
HA_IP = os.getenv("HA_IP", "")
HA_PORT = os.getenv("HA_PORT", "8123")


def _detect_lan_ip() -> str:
    """Return the LAN address other devices should use to reach Viper."""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


PC_IP = os.getenv("PC_IP", _detect_lan_ip())

FRONT_CAMERA_ID = os.getenv("FRONT_CAMERA_ID", "")
BACK_CAMERA_ID = os.getenv("BACK_CAMERA_ID", "")
RING_TOPIC_ROOT = os.getenv("RING_TOPIC_ROOT") or os.getenv("RING_LOCATION_ID", "")
FRONT_DOORBELL_MQTT_TOPIC = os.getenv("FRONT_DOORBELL_MQTT_TOPIC", "")
BACK_DOORBELL_MQTT_TOPIC = os.getenv("BACK_DOORBELL_MQTT_TOPIC", "")

RTSP_FRONT = os.getenv("RTSP_FRONT", f"rtsp://{HA_IP}:8554/{FRONT_CAMERA_ID}_live" if FRONT_CAMERA_ID else "")
RTSP_BACK = os.getenv("RTSP_BACK", f"rtsp://{HA_IP}:8554/{BACK_CAMERA_ID}_live" if BACK_CAMERA_ID else "")

def _resolve_ffmpeg_bin():
    explicit = os.getenv("FFMPEG_BIN")
    if explicit:
        return explicit
    candidates = [APP_DIR / "ffmpeg.exe"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "ffmpeg.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"


FFMPEG_BIN = _resolve_ffmpeg_bin()
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
FRONT_MIN_FRAME_BYTES = int(os.getenv("FRONT_MIN_FRAME_BYTES", str(14_000)))
BACK_MIN_FRAME_BYTES  = int(os.getenv("BACK_MIN_FRAME_BYTES",  str(14_000)))

HA_TOKEN = os.getenv("HA_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_KEY")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_TOKEN")

SECRET_ENV_VARS = {
    "ha_token": "HA_TOKEN",
    "gemini_api_key": "GEMINI_KEY",
    "pushover_user_key": "PUSHOVER_USER",
    "pushover_api_token": "PUSHOVER_TOKEN",
    "mqtt_password": "MQTT_PASSWORD",
}

# Refrigerator / ice maker entities
ICE_MAKER_SWITCH_ENTITY = os.getenv("ICE_MAKER_SWITCH_ENTITY", "switch.refrigerator_cubed_ice")
ICE_MAKER_KEEP_ON_ENTITY = os.getenv("ICE_MAKER_KEEP_ON_ENTITY", "input_boolean.keep_ice_maker_on")
ICE_MAKER_COUNTER_ENTITY = os.getenv("ICE_MAKER_COUNTER_ENTITY", "counter.ice_usage_counter")

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

AI_DESCRIPTION_JOBS = ("front_photo", "back_photo", "manual_video", "smart_video", "detailed_video")
AI_DESCRIPTION_STYLES = ("balanced", "fast_security", "people_movement", "packages_deliveries", "detailed_blind", "custom")
DEFAULT_AI_DESCRIPTION_STYLES = {
    "front_photo": "balanced",
    "back_photo": "balanced",
    "manual_video": "detailed_blind",
    "smart_video": "fast_security",
    "detailed_video": "detailed_blind",
}

AI_DESCRIPTION_STYLE_PROMPTS = {
    "balanced": {
        "photo": "Describe the doorbell camera image in one natural sentence. Mention people, actions, vehicles, animals, packages, and anything unusual. Keep it under 30 words.",
        "video": "Describe the doorbell video in two or three complete sentences. Mention people, movement, direction of travel, vehicles, animals, packages, and anything that needs attention.",
    },
    "fast_security": {
        "photo": "Give a fast security summary of this doorbell image. Say who or what is present and what they are doing. Keep it under 20 words.",
        "video": "Give a fast security summary of this doorbell video. Focus on people, movement, direction of travel, and anything urgent. Use one or two complete sentences.",
    },
    "people_movement": {
        "photo": "Describe any people in this doorbell image, including position, clothing, movement, and whether they are approaching or leaving. Keep it concise.",
        "video": "Describe people and movement in this doorbell video. Include direction of travel, what changed, and whether anyone approaches the door. Use complete sentences.",
    },
    "packages_deliveries": {
        "photo": "Check this doorbell image for deliveries, packages, bags, vehicles, and people carrying items. Mention if no package is visible.",
        "video": "Check this doorbell video for deliveries, packages, bags, vehicles, and people carrying items. Say whether something was dropped off or picked up.",
    },
    "detailed_blind": {
        "photo": "Describe this doorbell image for a blind homeowner. Include useful spatial details, people, vehicles, packages, animals, movement clues, and safety concerns. Keep it under 45 words.",
        "video": "Describe this doorbell video for a blind homeowner. Include people, vehicles, animals, packages, motion, direction of travel, spatial details, and safety concerns. Use two to four complete sentences, about 35 to 90 words.",
    },
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
        "global_mute": False,
        "mute_local_pc": False,
        "enable_alexa": False,
        "ha_ip": "",
        "ha_port": HA_PORT,
        "ha_token": "",
        "ha_vm_ram_mb": 4096,
        "ha_vm_disk_gb": 32,
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
        "show_advanced_ring_mqtt": False,
        "ha_listener_enabled": True,
        "ice_maker_switch_entity": ICE_MAKER_SWITCH_ENTITY,
        "ice_maker_keep_on_entity": ICE_MAKER_KEEP_ON_ENTITY,
        "ice_maker_counter_entity": ICE_MAKER_COUNTER_ENTITY,
        "setup_progress": {
            "active": False,
            "phase": "",
            "phase_label": "",
            "status": "",
            "detail": "",
            "percent": None,
            "started_at": "",
            "updated_at": "",
            "last_error": "",
            "next_action": "",
        },
        "setup_skips": {
            "gemini": False,
            "pushover": False,
            "fridge": False,
            "vacuum": False,
        },
        "doorbell_triggers": {
            "front": {
                "enabled": False,
                "source": "ha_state",
                "trigger_entity_id": "",
                "active_states": ["on", "true", "detected", "motion", "ding", "pressed", "open"],
                "rtsp_url": "",
                "camera_id": "",
                "mqtt_topic": "",
            },
            "back": {
                "enabled": False,
                "source": "ha_state",
                "trigger_entity_id": "",
                "active_states": ["on", "true", "detected", "motion", "ding", "pressed", "open"],
                "rtsp_url": "",
                "camera_id": "",
                "mqtt_topic": "",
            },
        },
        "doorbell_video_analysis": {
            "mode": "fast",
            "model": "gemini-3-flash-preview",
            "smart_clip_seconds": 3,
            "detailed_clip_seconds": 5,
            "manual_clip_seconds": 6,
            "max_manual_clip_seconds": 15,
            "fps": 2,
            "speak_followups": True,
            "smart_cooldown_seconds": 60,
        },
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
        "vacuum_rooms": {},
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
        "doorbell_prompt_profiles": {
            "front": "",
            "back": "",
        },
        "active_video_prompt": "Manual Outside Check",
        "video_prompts": {
            "Manual Outside Check": (
                "You are helping a blind homeowner understand what is happening outside. "
                "Analyze this live doorbell video carefully. Mention people, vehicles, animals, packages, movement, direction of travel, and anything that may need attention. "
                "Be concise but include enough detail to be useful. Reply with two to four complete sentences, about 35 to 90 words, and end with punctuation."
            ),
            "Smart Follow Up": (
                "The first still image said: {first_description}. You are helping a blind homeowner understand the {location}. "
                "Use this short video only to add missing useful details. If nothing meaningful changes, say: No extra detail from the video. Use complete sentences."
            ),
            "Detailed Doorbell Video": (
                "You are helping a blind homeowner understand the {location}. Analyze this short security video. "
                "Mention people, packages, vehicles, animals, movement, direction, spatial details, and safety concerns. Use two to four complete sentences, about 35 to 90 words."
            ),
        },
        "doorbell_video_prompt_profiles": {
            "manual": "Manual Outside Check",
            "smart": "Smart Follow Up",
            "detailed": "Detailed Doorbell Video",
        },
        "ai_description_styles": deepcopy(DEFAULT_AI_DESCRIPTION_STYLES),
        "ai_custom_descriptions": {
            "front_photo": "",
            "back_photo": "",
            "manual_video": "",
            "smart_video": "",
            "detailed_video": "",
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
        "unknown_keys": "discard",
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


def _normalize_broadcast_mode_value(value, default="speak"):
    text = _as_str(value, default).strip().lower()
    text = text.replace("-", " ").replace("_", " ")
    aliases = {
        "speak": "speak",
        "voice": "speak",
        "spoken": "speak",
        "speak message": "speak",
        "chime": "chime",
        "chime only": "chime",
        "sound": "chime",
        "sound only": "chime",
        "tone": "chime",
        "tone only": "chime",
        "silent": "silent",
        "mute": "silent",
        "muted": "silent",
        "off": "silent",
        "log only": "silent",
    }
    return aliases.get(text, default if default in BROADCAST_MODES else "speak")


def _normalize_prompt_map(value, default):
    if not isinstance(value, dict):
        return deepcopy(default)
    normalized = {}
    for name, prompt in value.items():
        key = _as_str(name).strip()
        if key:
            normalized[key] = _as_str(prompt)
    return normalized or deepcopy(default)


def _normalize_prompt_profiles(value, default, prompts, allowed_keys):
    raw = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, raw)
    first_prompt = next(iter(prompts), "")
    for key in allowed_keys:
        selected = _as_str(normalized.get(key), "").strip()
        normalized[key] = selected if selected in prompts else (default.get(key) if default.get(key) in prompts else first_prompt)
    return normalized


def _known_ai_prompt_texts(defaults):
    known = set()
    for value in defaults.get("prompts", {}).values():
        known.add(_as_str(value, "").strip())
    for value in defaults.get("video_prompts", {}).values():
        known.add(_as_str(value, "").strip())
    for style_prompts in AI_DESCRIPTION_STYLE_PROMPTS.values():
        known.add(style_prompts["photo"].strip())
        known.add(style_prompts["video"].strip())
    return {text for text in known if text}


def _legacy_photo_prompt_for_job(normalized, job):
    prompts = normalized.get("prompts", {})
    profiles = normalized.get("doorbell_prompt_profiles", {})
    side = "back" if job == "back_photo" else "front"
    selected = profiles.get(side) or normalized.get("active_prompt") or next(iter(prompts), "")
    return _as_str(prompts.get(selected), "").strip()


def _legacy_video_prompt_for_job(normalized, job):
    prompts = normalized.get("video_prompts", {})
    profiles = normalized.get("doorbell_video_prompt_profiles", {})
    mode = {"manual_video": "manual", "smart_video": "smart", "detailed_video": "detailed"}[job]
    selected = profiles.get(mode) or normalized.get("active_video_prompt") or next(iter(prompts), "")
    return _as_str(prompts.get(selected), "").strip()


def _normalize_ai_descriptions(normalized, defaults):
    styles_raw = normalized.get("ai_description_styles") if isinstance(normalized.get("ai_description_styles"), dict) else {}
    custom_raw = normalized.get("ai_custom_descriptions") if isinstance(normalized.get("ai_custom_descriptions"), dict) else {}
    styles = deepcopy(defaults["ai_description_styles"])
    custom = deepcopy(defaults["ai_custom_descriptions"])
    known = _known_ai_prompt_texts(defaults)

    for job in AI_DESCRIPTION_JOBS:
        style = _as_str(styles_raw.get(job), styles.get(job, "balanced")).strip().lower()
        styles[job] = style if style in AI_DESCRIPTION_STYLES else DEFAULT_AI_DESCRIPTION_STYLES.get(job, "balanced")
        custom[job] = _as_str(custom_raw.get(job), "").strip()

        if not custom[job]:
            legacy_text = _legacy_photo_prompt_for_job(normalized, job) if job.endswith("_photo") else _legacy_video_prompt_for_job(normalized, job)
            if legacy_text and legacy_text not in known:
                custom[job] = legacy_text
                styles[job] = "custom"

    normalized["ai_description_styles"] = styles
    normalized["ai_custom_descriptions"] = custom
    return normalized


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
        mode = _normalize_broadcast_mode_value(settings.get("mode", fallback["mode"]), fallback["mode"])
        if mode not in BROADCAST_MODES:
            mode = fallback["mode"]
        normalized[channel] = {
            **settings,
            "mode": mode,
            "chime": _as_str(settings.get("chime", fallback["chime"])).strip(),
        }
    return normalized


def _normalize_vacuum_rooms(value):
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for entity_id, rooms in value.items():
        key = _as_str(entity_id).strip()
        if not key or not isinstance(rooms, list):
            continue
        cleaned = []
        for room in rooms:
            if not isinstance(room, dict):
                continue
            try:
                segment = int(room.get("segment"))
            except (TypeError, ValueError):
                continue
            name = _as_str(room.get("name"), f"Room {segment}").strip() or f"Room {segment}"
            map_name = _as_str(room.get("map"), "Current map").strip() or "Current map"
            label = _as_str(room.get("label"), "").strip()
            if not label:
                label = f"{name} ({segment})" if map_name == "Current map" else f"{name} on {map_name} ({segment})"
            cleaned.append({"label": label, "name": name, "map": map_name, "segment": segment})
        if cleaned:
            normalized[key] = sorted(cleaned, key=lambda item: item["label"].lower())
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


def _normalize_active_states(value, default):
    states = value if isinstance(value, list) else default
    cleaned = []
    for item in states:
        text = _as_str(item).strip().lower()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned or deepcopy(default)


def _normalize_doorbell_trigger(value, default, side, config_data):
    trigger = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, trigger)
    source = _as_str(normalized.get("source"), default["source"]).strip().lower()
    if source not in {"ha_state", "mqtt", "webhook"}:
        source = default["source"]

    camera_id = _as_str(normalized.get("camera_id"), "").strip()
    rtsp_url = _as_str(normalized.get("rtsp_url"), "").strip()
    mqtt_topic = _as_str(normalized.get("mqtt_topic"), "").strip()

    return {
        **normalized,
        "enabled": _as_bool(normalized.get("enabled"), bool(rtsp_url)),
        "source": source,
        "trigger_entity_id": _as_str(normalized.get("trigger_entity_id"), "").strip(),
        "active_states": _normalize_active_states(normalized.get("active_states"), default["active_states"]),
        "rtsp_url": rtsp_url,
        "camera_id": camera_id,
        "mqtt_topic": mqtt_topic,
    }


def _normalize_doorbell_triggers(value, default, config_data):
    raw = value if isinstance(value, dict) else {}
    return {
        "front": _normalize_doorbell_trigger(raw.get("front"), default["front"], "front", config_data),
        "back": _normalize_doorbell_trigger(raw.get("back"), default["back"], "back", config_data),
    }


def _normalize_doorbell_video_analysis(value, default):
    raw = value if isinstance(value, dict) else {}
    normalized = _deep_merge(default, raw)
    mode = _as_str(normalized.get("mode"), default["mode"]).strip().lower()
    if mode not in {"fast", "smart", "detailed", "manual"}:
        mode = default["mode"]
    normalized["mode"] = mode
    normalized["model"] = _as_str(normalized.get("model"), default["model"]).strip() or default["model"]
    normalized["smart_clip_seconds"] = min(8, _as_int(normalized.get("smart_clip_seconds"), default["smart_clip_seconds"], minimum=2))
    normalized["detailed_clip_seconds"] = min(10, _as_int(normalized.get("detailed_clip_seconds"), default["detailed_clip_seconds"], minimum=2))
    max_manual = min(30, _as_int(normalized.get("max_manual_clip_seconds"), default["max_manual_clip_seconds"], minimum=5))
    normalized["max_manual_clip_seconds"] = max_manual
    normalized["manual_clip_seconds"] = min(
        max_manual,
        max(2, _as_int(normalized.get("manual_clip_seconds"), default["manual_clip_seconds"], minimum=2)),
    )
    normalized["fps"] = min(5, _as_int(normalized.get("fps"), default["fps"], minimum=1))
    normalized["speak_followups"] = _as_bool(normalized.get("speak_followups"), default["speak_followups"])
    normalized["smart_cooldown_seconds"] = min(
        300,
        _as_int(normalized.get("smart_cooldown_seconds"), default["smart_cooldown_seconds"], minimum=15),
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
    """Merge user config with schema defaults and normalize known current-version fields."""
    defaults = get_default_config()
    if not isinstance(config_data, dict):
        config_data = {}

    normalized = _deep_merge(defaults, config_data)
    # Current config is strict-schema: runtime/source markers and old unknown
    # keys should not be written back into viper_config.json.
    normalized = {key: normalized.get(key, deepcopy(default)) for key, default in defaults.items()}
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
    normalized["global_mute"] = _as_bool(normalized.get("global_mute"), defaults["global_mute"])
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
    normalized["show_advanced_ring_mqtt"] = _as_bool(
        normalized.get("show_advanced_ring_mqtt"),
        defaults["show_advanced_ring_mqtt"],
    )
    normalized["ha_listener_enabled"] = _as_bool(normalized.get("ha_listener_enabled"), defaults["ha_listener_enabled"])
    normalized["ice_maker_switch_entity"] = _as_str(normalized.get("ice_maker_switch_entity"), defaults["ice_maker_switch_entity"]).strip() or defaults["ice_maker_switch_entity"]
    normalized["ice_maker_keep_on_entity"] = _as_str(normalized.get("ice_maker_keep_on_entity"), defaults["ice_maker_keep_on_entity"]).strip() or defaults["ice_maker_keep_on_entity"]
    normalized["ice_maker_counter_entity"] = _as_str(normalized.get("ice_maker_counter_entity"), defaults["ice_maker_counter_entity"]).strip() or defaults["ice_maker_counter_entity"]
    normalized["doorbell_triggers"] = _normalize_doorbell_triggers(
        normalized.get("doorbell_triggers"),
        defaults["doorbell_triggers"],
        normalized,
    )
    normalized["doorbell_video_analysis"] = _normalize_doorbell_video_analysis(
        normalized.get("doorbell_video_analysis"),
        defaults["doorbell_video_analysis"],
    )
    normalized["quiet_hours_enabled"] = _as_bool(normalized.get("quiet_hours_enabled"), defaults["quiet_hours_enabled"])
    normalized["quiet_hours_start"] = _normalize_time(normalized.get("quiet_hours_start"), defaults["quiet_hours_start"])
    normalized["quiet_hours_end"] = _normalize_time(normalized.get("quiet_hours_end"), defaults["quiet_hours_end"])
    normalized["broadcast_channels"] = _normalize_broadcast_channels(normalized.get("broadcast_channels"), defaults["broadcast_channels"])
    normalized["vacuum_rooms"] = _normalize_vacuum_rooms(normalized.get("vacuum_rooms"))
    normalized["cinderella_enabled"] = _as_bool(normalized.get("cinderella_enabled"), defaults["cinderella_enabled"])
    normalized["cinderella_ai_mode"] = _as_bool(normalized.get("cinderella_ai_mode"), defaults["cinderella_ai_mode"])
    normalized["cinderella_ai_prompt"] = _as_str(normalized.get("cinderella_ai_prompt"), defaults["cinderella_ai_prompt"]) or defaults["cinderella_ai_prompt"]
    normalized["prompts"] = _normalize_prompt_map(normalized.get("prompts"), defaults["prompts"])
    if normalized["active_prompt"] not in normalized["prompts"]:
        normalized["active_prompt"] = next(iter(normalized["prompts"]))
    normalized["doorbell_prompt_profiles"] = _normalize_prompt_profiles(
        normalized.get("doorbell_prompt_profiles"),
        defaults["doorbell_prompt_profiles"],
        normalized["prompts"],
        ("front", "back"),
    )
    normalized["video_prompts"] = _normalize_prompt_map(normalized.get("video_prompts"), defaults["video_prompts"])
    normalized["active_video_prompt"] = _as_str(
        normalized.get("active_video_prompt"),
        defaults["active_video_prompt"],
    ).strip() or defaults["active_video_prompt"]
    if normalized["active_video_prompt"] not in normalized["video_prompts"]:
        normalized["active_video_prompt"] = next(iter(normalized["video_prompts"]))
    normalized["doorbell_video_prompt_profiles"] = _normalize_prompt_profiles(
        normalized.get("doorbell_video_prompt_profiles"),
        defaults["doorbell_video_prompt_profiles"],
        normalized["video_prompts"],
        ("manual", "smart", "detailed"),
    )
    normalized = _normalize_ai_descriptions(normalized, defaults)
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
        raw = read_config_file()
        normalized = validate_and_normalize_config(raw)
        protected = protect_config_secrets(normalized)
        if protected != raw:
            try:
                write_config_file(protected)
            except Exception:
                logging.warning("Could not write normalized current-version config.", exc_info=True)
        _config_cache = protected
        return deepcopy(_config_cache)


def read_config():
    """Public safe-read helper. Kept separate from load_config for newer callers."""
    return load_config()


def _secret_from_env(name):
    env_name = SECRET_ENV_VARS.get(name, "")
    return (os.getenv(env_name, "") if env_name else "").strip()


def get_secret_value(name, config_data=None, *, include_env=True):
    """Resolve a secret without requiring it to live in viper_config.json."""
    original_data = config_data if isinstance(config_data, dict) else {}
    if config_data is None:
        try:
            original_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        except Exception:
            original_data = {}
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    if include_env:
        env_value = _secret_from_env(name)
        if env_value:
            return env_value
    stored = viper_secrets.get_secret(name)
    if stored:
        return stored
    return data.get(name) or ""


def protect_config_secrets(config_data):
    """Move plain config secrets into Windows Credential Manager when possible."""
    protected = deepcopy(config_data)
    for name in SECRET_ENV_VARS:
        value = (protected.get(name) or "").strip()
        if not value:
            continue
        if viper_secrets.set_secret(name, value):
            protected[name] = ""
    return protected


def secret_storage_status(config_data=None):
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    status = viper_secrets.storage_status()
    status["config_plaintext"] = {
        name: bool((data.get(name) or "").strip())
        for name in SECRET_ENV_VARS
    }
    status["environment"] = {
        name: bool(_secret_from_env(name))
        for name in SECRET_ENV_VARS
    }
    return status


def get_ha_settings(config_data=None, *, include_env=True):
    """Return Home Assistant connection settings from config, with env-backed defaults."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    if not include_env:
        return {
            "ha_ip": data.get("ha_ip") or "",
            "ha_port": data.get("ha_port") or "8123",
            "ha_token": get_secret_value("ha_token", data, include_env=False),
        }
    return {
        "ha_ip": data.get("ha_ip") or HA_IP,
        "ha_port": data.get("ha_port") or HA_PORT,
        "ha_token": get_secret_value("ha_token", data, include_env=True),
    }


def get_api_settings(config_data=None, *, include_env=True):
    """Return Gemini and Pushover settings. Pushover is optional."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    if not include_env:
        user_key = get_secret_value("pushover_user_key", data, include_env=False)
        api_token = get_secret_value("pushover_api_token", data, include_env=False)
        return {
            "gemini_api_key": get_secret_value("gemini_api_key", data, include_env=False),
            "pushover_enabled": bool(data.get("pushover_enabled") or (user_key and api_token)),
            "pushover_user_key": user_key,
            "pushover_api_token": api_token,
        }
    user_key = get_secret_value("pushover_user_key", data, include_env=True)
    api_token = get_secret_value("pushover_api_token", data, include_env=True)
    return {
        "gemini_api_key": get_secret_value("gemini_api_key", data, include_env=True),
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
    original_data = config_data if isinstance(config_data, dict) else {}
    if config_data is None:
        try:
            original_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        except Exception:
            original_data = {}
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
    triggers = data.get("doorbell_triggers") if isinstance(data.get("doorbell_triggers"), dict) else {}
    front_trigger = triggers.get("front") if isinstance(triggers.get("front"), dict) else {}
    back_trigger = triggers.get("back") if isinstance(triggers.get("back"), dict) else {}
    original_triggers = original_data.get("doorbell_triggers") if isinstance(original_data.get("doorbell_triggers"), dict) else {}
    original_front_trigger = original_triggers.get("front") if isinstance(original_triggers.get("front"), dict) else {}
    original_back_trigger = original_triggers.get("back") if isinstance(original_triggers.get("back"), dict) else {}

    configured_rtsp_front = (
        original_front_trigger.get("rtsp_url")
        or original_data.get("rtsp_front")
        or env_rtsp_front
        or ""
    )
    configured_rtsp_back = (
        original_back_trigger.get("rtsp_url")
        or original_data.get("rtsp_back")
        or env_rtsp_back
        or ""
    )

    resolved_rtsp_front = (
        configured_rtsp_front
        or _derive_rtsp_url(ha_ip, front_camera_id)
        or ""
    )
    resolved_rtsp_back = (
        configured_rtsp_back
        or _derive_rtsp_url(ha_ip, back_camera_id)
        or ""
    )
    resolved_front_topic = (
        front_trigger.get("mqtt_topic")
        or data.get("front_doorbell_mqtt_topic")
        or env_front_topic
        or _derive_ring_topic(ring_topic_root, front_camera_id)
        or ""
    )
    resolved_back_topic = (
        back_trigger.get("mqtt_topic")
        or data.get("back_doorbell_mqtt_topic")
        or env_back_topic
        or _derive_ring_topic(ring_topic_root, back_camera_id)
        or ""
    )

    return {
        "rtsp_front": resolved_rtsp_front,
        "rtsp_back": resolved_rtsp_back,
        "configured_rtsp_front": configured_rtsp_front,
        "configured_rtsp_back": configured_rtsp_back,
        "front_doorbell_mqtt_topic": resolved_front_topic,
        "back_doorbell_mqtt_topic": resolved_back_topic,
        "front_camera_id": front_camera_id or "",
        "back_camera_id": back_camera_id or "",
        "ring_topic_root": ring_topic_root or "",
        "mqtt_host": data.get("mqtt_host") or (ha_ip if include_env else "") or "",
        "mqtt_port": data.get("mqtt_port") or "1883",
        "mqtt_username": data.get("mqtt_username") or "",
        "mqtt_password": get_secret_value("mqtt_password", data, include_env=include_env),
        "raw_rtsp_front": data.get("rtsp_front") or "",
        "raw_rtsp_back": data.get("rtsp_back") or "",
        "front_trigger_entity_id": front_trigger.get("trigger_entity_id", ""),
        "back_trigger_entity_id": back_trigger.get("trigger_entity_id", ""),
        "front_trigger_source": front_trigger.get("source", "ha_state"),
        "back_trigger_source": back_trigger.get("source", "ha_state"),
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


def get_doorbell_photo_prompt(config_data=None, side="front"):
    job = "back_photo" if side == "back" else "front_photo"
    location = "back door" if side == "back" else "front door"
    return get_ai_description_prompt(config_data, job, side=side, location=location)


def get_doorbell_video_prompt(config_data=None, mode="manual", **context):
    mode_key = (mode or "manual").strip().lower()
    job = {
        "smart": "smart_video",
        "detailed": "detailed_video",
    }.get(mode_key, "manual_video")
    return get_ai_description_prompt(config_data, job, **context)


def get_ai_description_prompt(config_data=None, job="front_photo", **context):
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    job = job if job in AI_DESCRIPTION_JOBS else "front_photo"
    styles = data.get("ai_description_styles", {})
    custom = data.get("ai_custom_descriptions", {})
    style = styles.get(job, DEFAULT_AI_DESCRIPTION_STYLES.get(job, "balanced"))
    if style == "custom":
        template = custom.get(job, "").strip()
        if not template:
            style = DEFAULT_AI_DESCRIPTION_STYLES.get(job, "balanced")
    if style != "custom":
        kind = "photo" if job.endswith("_photo") else "video"
        template = AI_DESCRIPTION_STYLE_PROMPTS.get(style, AI_DESCRIPTION_STYLE_PROMPTS["balanced"])[kind]
    safe_context = {
        "location": context.get("location") or "the door",
        "first_description": context.get("first_description") or "",
        "side": context.get("side") or "",
    }
    try:
        return template.format(**safe_context)
    except Exception:
        return template


def get_speaker_settings(config_data=None, *, include_env=True):
    """Return normalized speaker routing and quiet-hours settings.

    This is the public read path for speaker UI and diagnostics. It keeps the
    raw config shape intact for compatibility, but callers get predictable
    speaker dictionaries and useful counts.
    """
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    speakers = deepcopy(data.get("speakers") if isinstance(data.get("speakers"), dict) else {})
    enabled = {
        name: speaker
        for name, speaker in speakers.items()
        if isinstance(speaker, dict) and speaker.get("enabled", True)
    }
    return {
        "speakers": speakers,
        "enabled_speakers": enabled,
        "speaker_count": len(speakers),
        "enabled_count": len(enabled),
        "quiet_hours_enabled": bool(data.get("quiet_hours_enabled", False)),
        "quiet_hours_start": data.get("quiet_hours_start") or "22:00",
        "quiet_hours_end": data.get("quiet_hours_end") or "07:00",
        "routes": {
            "doorbell": [name for name, speaker in enabled.items() if speaker.get("doorbell", True)],
            "utilities": [name for name, speaker in enabled.items() if speaker.get("utilities", True)],
            "fridge": [name for name, speaker in enabled.items() if speaker.get("fridge", True)],
            "quiet_hours_exempt": [name for name, speaker in enabled.items() if speaker.get("quiet_hours_exempt", False)],
        },
    }


def get_audio_settings(config_data=None, *, include_env=True):
    """Return normalized TTS, chime, and speaker-routing settings."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    api = get_api_settings(data, include_env=include_env)
    speaker = get_speaker_settings(data, include_env=include_env)
    tts_defaults = deepcopy(data.get("tts_defaults", {}))
    tts_alerts = deepcopy(data.get("tts_alerts", {}))
    effective_alerts = {}
    for category in TTS_PROFILE_CATEGORIES:
        alert = deepcopy(tts_alerts.get(category, {}))
        effective = deepcopy(tts_defaults)
        if alert.get("use_defaults", True):
            # Keep category-specific speed/mood useful while inheriting engine
            # and voice parameters from the default profile.
            for key in ("speed", "dynamic_mood"):
                if key in alert:
                    effective[key] = alert[key]
        else:
            effective.update({k: v for k, v in alert.items() if k != "use_defaults"})
        effective_alerts[category] = effective
    return {
        "tts_engine": data.get("tts_engine"),
        "tts_simple": deepcopy(data.get("tts_simple", {})),
        "tts_defaults": tts_defaults,
        "tts_alerts": tts_alerts,
        "effective_tts_alerts": effective_alerts,
        "tts_profiles": deepcopy(data.get("tts_profiles", {})),
        "edge_tts_voice": data.get("edge_tts_voice"),
        "gemini_tts_voice": data.get("gemini_tts_voice"),
        "gemini_tts_model": data.get("gemini_tts_model"),
        "gemini_tts_keep_warm": bool(data.get("gemini_tts_keep_warm", False)),
        "gemini_tts_heartbeat_seconds": data.get("gemini_tts_heartbeat_seconds"),
        "gemini_tts_min_interval_seconds": data.get("gemini_tts_min_interval_seconds"),
        "google_tts_tld": data.get("google_tts_tld"),
        "local_voice_index": data.get("local_voice_index"),
        "front_chime": data.get("front_chime") or "",
        "back_chime": data.get("back_chime") or "",
        "global_mute": bool(data.get("global_mute", False)),
        "mute_local_pc": bool(data.get("mute_local_pc", False)),
        "enable_alexa": bool(data.get("enable_alexa", False)),
        "gemini_api_key_configured": bool(api.get("gemini_api_key")),
        "speakers": speaker,
    }


def get_fridge_settings(config_data=None, *, include_env=True):
    """Return normalized refrigerator, freezer, and ice-maker settings."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    channels = deepcopy(data.get("broadcast_channels") if isinstance(data.get("broadcast_channels"), dict) else {})
    return {
        "channels": channels,
        "fridge_open": deepcopy(channels.get("fridge_open", {})),
        "fridge_closed": deepcopy(channels.get("fridge_closed", {})),
        "freezer_open": deepcopy(channels.get("freezer_open", {})),
        "freezer_closed": deepcopy(channels.get("freezer_closed", {})),
        "default_channel": deepcopy(channels.get("default", {})),
        "ice_maker_switch_entity": data.get("ice_maker_switch_entity") or ICE_MAKER_SWITCH_ENTITY,
        "ice_maker_keep_on_entity": data.get("ice_maker_keep_on_entity") or ICE_MAKER_KEEP_ON_ENTITY,
        "ice_maker_counter_entity": data.get("ice_maker_counter_entity") or ICE_MAKER_COUNTER_ENTITY,
    }


def get_vacuum_settings(config_data=None, *, include_env=True):
    """Return normalized Roborock/Cinderella settings."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    return {
        "enabled": bool(data.get("cinderella_enabled", True)),
        "ai_mode": bool(data.get("cinderella_ai_mode", False)),
        "ai_prompt": data.get("cinderella_ai_prompt") or "",
        "messages": deepcopy(data.get("cinderella_messages", {})),
        "rooms": deepcopy(data.get("vacuum_rooms", {})),
        "vacuum_error_codes": list(ROBOROCK_VACUUM_ERROR_CODES),
        "dock_error_codes": list(ROBOROCK_DOCK_ERROR_CODES),
    }


def get_runtime_settings(config_data=None, *, include_env=True):
    """Return the product-area settings bundle used by UI and diagnostics."""
    data = validate_and_normalize_config(config_data) if config_data is not None else load_config()
    return {
        "home_assistant": get_ha_settings(data, include_env=include_env),
        "api": get_api_settings(data, include_env=include_env),
        "doorbell": get_doorbell_settings(data, include_env=include_env),
        "audio": get_audio_settings(data, include_env=include_env),
        "speakers": get_speaker_settings(data, include_env=include_env),
        "fridge": get_fridge_settings(data, include_env=include_env),
        "vacuum": get_vacuum_settings(data, include_env=include_env),
    }


def write_config_file(config_data: dict, path=CONFIG_FILE):
    """Validate and atomically write config to disk. Returns the normalized config."""
    normalized = validate_and_normalize_config(config_data)
    normalized = protect_config_secrets(normalized)
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
    config_data = protect_config_secrets(config_data)
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
    config_data = protect_config_secrets(config_data)
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
            HA_TOKEN = get_secret_value("ha_token", data, include_env=True) or HA_TOKEN
            GEMINI_API_KEY = get_secret_value("gemini_api_key", data, include_env=True) or GEMINI_API_KEY
            doorbell = get_resolved_doorbell_settings(data, include_env=True)
            FRONT_CAMERA_ID = doorbell.get("front_camera_id") or FRONT_CAMERA_ID
            BACK_CAMERA_ID = doorbell.get("back_camera_id") or BACK_CAMERA_ID
            RING_TOPIC_ROOT = doorbell.get("ring_topic_root") or RING_TOPIC_ROOT
            RTSP_FRONT = doorbell.get("rtsp_front") or ""
            RTSP_BACK = doorbell.get("rtsp_back") or ""
            if data.get("pushover_enabled"):
                PUSHOVER_USER_KEY = get_secret_value("pushover_user_key", data, include_env=True) or PUSHOVER_USER_KEY
                PUSHOVER_API_TOKEN = get_secret_value("pushover_api_token", data, include_env=True) or PUSHOVER_API_TOKEN
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
