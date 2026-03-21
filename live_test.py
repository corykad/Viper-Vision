import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor

# Import our local modules
import viper_config as cfg
import viper_audio as audio
import viper_vision as vision

# Temporary executor for the test script
test_executor = ThreadPoolExecutor(max_workers=3)

def print_header(title):
    print(f"\n{'='*50}\n   {title}\n{'='*50}")

def test_config_engine():
    print_header("TEST 1: Config Engine")
    try:
        data = cfg.load_config()
        print(f"  [OK] Config loaded. Active Prompt: {data.get('active_prompt')}")
        cfg.save_config(data)
        print("  [OK] Config saved successfully.")
    except Exception as e:
        print(f"  [FAIL] Config test failed: {e}")

def test_audio_routing():
    print_header("TEST 2: Audio Routing (LIVE AUDIO)")
    print("  -> Syncing globals...")
    cfg.sync_globals_from_config()
    
    # 1. Test Specific Speaker
    print("  -> Testing specific HA speaker (if available)...")
    if cfg.TARGET_SPEAKERS:
        audio.announce_specific_speaker("ha", cfg.TARGET_SPEAKERS[0], "Testing direct Home Assistant routing.")
        time.sleep(2)
    else:
        print("  [SKIP] No HA speakers enabled.")

    # 2. Test Instant Chime
    print("  -> Testing Sonos Instant Chime...")
    audio.sonos_instant_chime()
    time.sleep(2)

    # 3. Test Global Broadcast
    print("  -> Testing Global Broadcast (Alexa + HA)...")
    audio.announce_all("Testing global broadcast to all auxiliary speakers.")
    time.sleep(2)

    # 4. Test Sonos Verdict TTS
    print("  -> Testing Sonos Local TTS Generation...")
    audio.sonos_speak_verdict("Testing Sonos local text to speech server.")
    print("  [OK] Audio routing commands dispatched.")

def test_vision_engine():
    print_header("TEST 3: Vision Engine (FFmpeg Local Test)")
    print("  -> Grabbing 3 frames from Front Door RTSP...")
    
    base_path = str(cfg.BASE_DIR / "test_frame")
    frames = vision.grab_flipbook_frames(cfg.RTSP_FRONT, base_path)
    
    if frames:
        print(f"  [OK] Successfully grabbed {len(frames)} frames.")
        for f in frames:
            import os
            try:
                os.remove(f)
                print(f"    - Deleted cleanup frame: {f}")
            except: pass
    else:
        print("  [FAIL] Could not grab frames. Is the RTSP stream active?")

def test_flask_webhooks():
    print_header("TEST 4: Flask Webhooks & Full Pipeline")
    print("  NOTE: Your main Viper Vision app MUST be running for this to work.")
    
    try:
        print("  -> Firing Front Door Webhook...")
        res_front = requests.post(f"http://127.0.0.1:{cfg.FLASK_PORT}/doorbell-webhook", timeout=5)
        if res_front.status_code == 200:
            print("  [OK] Front door webhook accepted the payload. Check your dashboard/speakers!")
        else:
            print(f"  [FAIL] Front door webhook returned {res_front.status_code}")
            
        time.sleep(3) # Short pause before hitting the back door
        
        print("  -> Firing Back Door Webhook...")
        res_back = requests.post(f"http://127.0.0.1:{cfg.FLASK_PORT}/doorbell-webhook/back", timeout=5)
        if res_back.status_code == 200:
            print("  [OK] Back door webhook accepted the payload.")
        else:
             print(f"  [FAIL] Back door webhook returned {res_back.status_code}")
             
    except requests.exceptions.ConnectionError:
        print("  [FAIL] Connection refused. Is main.py running?")
    except Exception as e:
        print(f"  [FAIL] Webhook test error: {e}")

if __name__ == "__main__":
    print("\n--- VIPER VISION LIVE TEST SUITE ---")
    print("Ensure main.py is running in another window before starting.")
    input("Press ENTER to begin live tests (Warning: Audio will play)...")
    
    test_config_engine()
    test_vision_engine()
    test_audio_routing()
    
    print("\nPausing for 5 seconds before testing webhooks to allow audio to clear...")
    time.sleep(5)
    
    test_flask_webhooks()
    
    print("\n" + "="*50)
    print("   TESTING COMPLETE.")
    print("   If you heard the test phrases and saw the AI verdicts")
    print("   in your dashboard logs, your entire system is flawless.")
    print("="*50 + "\n")
    
    test_executor.shutdown(wait=False)