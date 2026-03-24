import os
import time
import logging
import requests
import socket
import http.server
import socketserver
import threading
import soco
from gtts import gTTS

import viper_config as cfg

# ==========================================
# FILE CLEANUP LOGIC (GARBAGE COLLECTOR)
# ==========================================
def startup_cleanup():
    """Cleans all MP3s on initial boot."""
    try:
        logging.info("Sweeping for stale audio files...")
        for file_path in cfg.SONOS_AUDIO_DIR.glob("*.mp3"):
            try:
                file_path.unlink()
                logging.info(f"Deleted old audio: {file_path.name}")
            except Exception as e:
                logging.error(f"[CLEANUP ERROR] Could not delete {file_path.name}: {e}")
    except Exception as e:
        logging.error(f"[CLEANUP ERROR] Failed to access directory: {e}")

def auto_cleanup_worker():
    """Background thread that deletes MP3s older than 90 seconds."""
    logging.info("[SYSTEM] 90-second Audio Garbage Collector started.")
    while True:
        try:
            now = time.time()
            for file_path in cfg.SONOS_AUDIO_DIR.glob("*.mp3"):
                # Check the 'Modified Time' of the file
                file_age = now - file_path.stat().st_mtime
                if file_age > 90:
                    try:
                        file_path.unlink()
                        logging.info(f"[CLEANUP] Purged 90s-old file: {file_path.name}")
                    except Exception:
                        pass # File might be currently in use by the server
        except Exception as e:
            logging.error(f"[CLEANUP ERROR] Worker encountered issue: {e}")
        
        # Wake up and check every 60 seconds
        time.sleep(60)

# Start the background cleaner immediately when this module is imported
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
def prep_sonos_speakers():
    cfg.sync_globals_from_config()
    active_speakers = []
    
    with cfg.globals_lock:
        ips_to_check = list(cfg.SONOS_IPS)

    for ip in ips_to_check:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((ip, 1400))
            s.close()
            
            speaker = soco.SoCo(ip)
            speaker.unjoin()
            speaker.mute = False
            speaker.volume = 45
            active_speakers.append(speaker)
        except socket.timeout:
            logging.warning(f"[SONOS] Timed out reaching {ip}")
        except Exception as e:
            logging.error(f"[SONOS ERROR] Failed to connect to {ip}: {e}")
    return active_speakers

def sonos_instant_chime():
    speakers = prep_sonos_speakers()
    chime_url = "http://codeskulptor-demos.commondatastorage.googleapis.com/descent/gotitem.mp3"
    for s in speakers:
        try:
            s.play_uri(chime_url)
        except Exception as e:
            logging.error(f"[SONOS CHIME ERROR - {s.ip_address}]: {e}")

def sonos_speak_verdict(message):
    file_name = f"verdict_{int(time.time())}.mp3"
    file_path = cfg.SONOS_AUDIO_DIR / file_name
    try:
        tts = gTTS(text=message, lang='en')
        tts.save(str(file_path))
    except Exception as e:
        logging.error(f"[SONOS TTS ERROR] Failed to generate local voice file: {e}")
        return

    speakers = prep_sonos_speakers()
    local_url = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"
    for s in speakers:
        try:
            s.play_uri(local_url)
        except Exception as e:
            logging.error(f"[SONOS VOICE ERROR - {s.ip_address}]: {e}")

def announce_specific_speaker(spk_type, spk_id, message):
    headers = {"Authorization": f"Bearer {cfg.HA_TOKEN}", "Content-Type": "application/json"}
    if spk_type == "alexa":
        try:
            url = f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/services/notify/alexa_media"
            requests.post(url, headers=headers, json={"message": message, "data": {"type": "announce"}, "target": [spk_id]}, timeout=5)
        except requests.RequestException as e: logging.error(f"Alexa error: {e}")
    elif spk_type == "ha":
        try:
            payload = {"entity_id": "tts.google_translate_en_com", "media_player_entity_id": spk_id, "message": message}
            requests.post(f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/services/tts/speak", headers=headers, json=payload, timeout=5)
        except requests.RequestException as e: logging.error(f"HA TTS error: {e}")
    elif spk_type == "sonos":
        file_name = f"alert_{int(time.time())}.mp3"
        file_path = cfg.SONOS_AUDIO_DIR / file_name
        try:
            tts = gTTS(text=message, lang='en')
            tts.save(str(file_path))
            speaker = soco.SoCo(spk_id)
            speaker.unjoin()
            speaker.mute = False
            speaker.volume = 45
            local_url = f"http://{cfg.PC_IP}:{cfg.SONOS_PORT}/{file_name}"
            speaker.play_uri(local_url)
        except Exception as e: logging.error(f"Sonos local announce error: {e}")

def announce_all(message):
    cfg.sync_globals_from_config()
    headers = {"Authorization": f"Bearer {cfg.HA_TOKEN}", "Content-Type": "application/json"}
    
    with cfg.globals_lock:
        alexa_targets = list(cfg.ALEXA_DEVICES)
        ha_targets = list(cfg.TARGET_SPEAKERS)

    if alexa_targets:
        try:
            url = f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/services/notify/alexa_media"
            requests.post(url, headers=headers, json={"message": message, "data": {"type": "announce"}, "target": alexa_targets}, timeout=5)
        except requests.RequestException as e: logging.error(f"Alexa broadast failed: {e}")
    
    for entity in ha_targets:
        try:
            payload = {"entity_id": "tts.google_translate_en_com", "media_player_entity_id": entity, "message": message}
            requests.post(f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/services/tts/speak", headers=headers, json=payload, timeout=5)
        except Exception as e:
            logging.error(f"[AUDIO ERROR on {entity}]: {e}")