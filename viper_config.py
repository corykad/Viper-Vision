import os
import sys
import json
import logging
import threading
from pathlib import Path

# ==========================================
# CONSTANTS & PATHS
# ==========================================
# FIXED: Moved everything to the new viper subfolder
BASE_DIR = Path(r"C:\scripts\viper")
BASE_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = BASE_DIR / "viper_config.json"
API_LOG_PATH = BASE_DIR / "api_usage.json"
LOG_FILE = BASE_DIR / "viper_dashboard_log.txt"
SONOS_AUDIO_DIR = BASE_DIR

# Network & APIs
FLASK_PORT = 5050
SONOS_PORT = 8090
HA_IP = "192.168.4.42"
HA_PORT = "8123"
PC_IP = "192.168.4.42"

HA_TOKEN = os.getenv("HA_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_KEY")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_TOKEN")

if not all([HA_TOKEN, GEMINI_API_KEY, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN]):
    logging.error("CRITICAL ERROR: Missing required Windows Environment Variables!")
    sys.exit(1)

# Devices & Streams
RTSP_FRONT = "rtsp://127.0.0.1:8554/343ea489ad6c_live"
RTSP_BACK = "rtsp://127.0.0.1:8554/343ea4745067_live"
TARGET_BATTERY_ENTITIES = ["sensor.front_door_battery", "sensor.back_door_battery"]

COST_PER_INPUT_TOKEN = 0.10 / 1_000_000
COST_PER_OUTPUT_TOKEN = 0.40 / 1_000_000

# ==========================================
# GLOBAL STATE & CONFIG MANAGEMENT
# ==========================================
TARGET_SPEAKERS = []
ALEXA_DEVICES = []
SONOS_IPS = []
globals_lock = threading.Lock()

def get_default_config():
    return {
        "is_armed": True,
        "active_prompt": "Standard",
        "prompts": {
            "Standard": "You are a helpful home security assistant. Analyze 3 frames. Describe people and actions in a natural, friendly sentence. Mention clothing/packages. If no one, describe the porch. Under 25 words.",
            "Detailed": "Analyze frames for security. Describe people, actions, clothing colors, packages, and environment in detail. Keep it strictly under 40 words."
        },
        "speakers": {
            "Kitchen HA": {"id": "media_player.kitchen", "type": "ha", "enabled": True},
            "Entryway HA": {"id": "media_player.entryway_speaker", "type": "ha", "enabled": True},
            "Loft Echo Alexa": {"id": "media_player.daisies_loft_echo", "type": "alexa", "enabled": True},
            "Master Bed Alexa": {"id": "media_player.daisy_cory_master_bedroom", "type": "alexa", "enabled": True},
            "Bathroom Alexa": {"id": "media_player.bathroom", "type": "alexa", "enabled": True},
            "Kitchen Echo Alexa": {"id": "media_player.kitchen_echo", "type": "alexa", "enabled": True},
            "Office Sonos": {"id": "192.168.4.34", "type": "sonos", "enabled": True},
            "New Speaker Sonos": {"id": "192.168.4.184", "type": "sonos", "enabled": True}
        }
    }

def load_config():
    default = get_default_config()
    if not CONFIG_FILE.exists(): 
        return default
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            for k, v in default.items():
                if k not in data: 
                    data[k] = v
            return data
    except Exception as e: 
        logging.error(f"Config load failed, using defaults: {e}")
        return default

def save_config(config_data):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_data, f, indent=4)
        sync_globals_from_config()
    except Exception as e:
        logging.error(f"Failed to save config: {e}")

def sync_globals_from_config():
    global TARGET_SPEAKERS, ALEXA_DEVICES, SONOS_IPS
    
    with globals_lock:
        TARGET_SPEAKERS.clear()
        ALEXA_DEVICES.clear()
        SONOS_IPS.clear()
        
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                for name, spk in data.get("speakers", {}).items():
                    if spk.get("enabled", True):
                        if spk["type"] == "ha": TARGET_SPEAKERS.append(spk["id"])
                        elif spk["type"] == "alexa": ALEXA_DEVICES.append(spk["id"])
                        elif spk["type"] == "sonos": SONOS_IPS.append(spk["id"])
        except Exception as e:
            logging.error(f"[SYNC ERROR] Failed to sync config: {e}")

        if not TARGET_SPEAKERS and not ALEXA_DEVICES and not SONOS_IPS:
            TARGET_SPEAKERS.extend(["media_player.kitchen", "media_player.entryway_speaker"])
            ALEXA_DEVICES.extend(["media_player.daisies_loft_echo", "media_player.daisy_cory_master_bedroom", "media_player.bathroom", "media_player.kitchen_echo"])
            SONOS_IPS.extend(["192.168.4.34", "192.168.4.184"])