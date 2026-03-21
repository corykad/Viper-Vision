import time
import logging
import subprocess
import json
import requests
from datetime import datetime
from pathlib import Path
from PIL import Image

from google import genai
from google.genai import types

import viper_config as cfg
import viper_audio as audio

client = genai.Client(
    api_key=cfg.GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1beta")
)

last_trigger = {"front": 0, "back": 0}

def grab_flipbook_frames(rtsp_url, base_path_str):
    pattern = f"{base_path_str}_%02d.jpg"
    cmd = ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", rtsp_url, "-frames:v", "3", "-r", "2", "-s", "640x360", "-q:v", "5", pattern]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except subprocess.TimeoutExpired:
        logging.error("FFmpeg process timed out capturing frames.")
        return []
    except Exception as e:
        logging.error(f"FFmpeg execution failed: {e}")
        return []

    frames = []
    for i in range(1, 4):
        f_path = Path(f"{base_path_str}_{i:02d}.jpg")
        if f_path.exists() and f_path.stat().st_size > 0:
            frames.append(str(f_path))
    return frames

def log_api_usage(usage_metadata):
    current_month = datetime.now().strftime("%Y-%m")
    data = {"period": current_month, "total_requests": 0, "prompt_tokens": 0, "response_tokens": 0}
    
    if cfg.API_LOG_PATH.exists():
        try:
            with open(cfg.API_LOG_PATH, "r") as f:
                old_data = json.load(f)
                if old_data.get("period") == current_month: data = old_data
        except Exception as e: 
            logging.warning(f"Could not read old API usage: {e}")

    data["total_requests"] += 1
    data["prompt_tokens"] += (usage_metadata.prompt_token_count or 0)
    data["response_tokens"] += (usage_metadata.candidates_token_count or 0)
    
    try:
        with open(cfg.API_LOG_PATH, "w") as f: json.dump(data, f)
    except Exception as e:
        logging.error(f"Failed to log API usage: {e}")

def process_doorbell(location, rtsp_url, key, dash_app, executor):
    now = time.time()
    if now - last_trigger[key] < 30: return 
    last_trigger[key] = now

    if dash_app and not dash_app.is_armed:
        logging.info(f"[{location.upper()}] Ignored: Viper Vision is Disarmed.")
        return

    logging.info(f"--- MOTION DETECTED: {location.upper()} ---")
    if dash_app: 
        dash_app.notify(f"Checking {location}.", priority=2, interrupt=True)
    
    executor.submit(audio.announce_all, f"Checking the {location}.")
    executor.submit(audio.sonos_instant_chime)

    base_path = str(cfg.BASE_DIR / f"latest_{key}")
    frames = grab_flipbook_frames(rtsp_url, base_path)
    
    if frames:
        imgs = []
        try:
            imgs = [Image.open(f) for f in frames]
            
            if dash_app:
                active_prompt_key = dash_app.config.get("active_prompt", "Standard")
                sys_prompt = dash_app.config["prompts"].get(active_prompt_key, "Analyze frames for security.")
            else:
                sys_prompt = "Analyze frames for security."

            res = None
            for attempt in range(3):
                try:
                    res = client.models.generate_content(
                        model='gemini-2.5-flash', 
                        contents=imgs,
                        config=types.GenerateContentConfig(system_instruction=sys_prompt, temperature=0.4)
                    )
                    break 
                except Exception as e:
                    if ("503" in str(e) or "overloaded" in str(e).lower()) and attempt < 2:
                        time.sleep((attempt + 1) * 2)
                        continue
                    raise e
            
            if res and res.usage_metadata: log_api_usage(res.usage_metadata)
            description = res.text.strip() if res and res.text else "Activity detected at the door."
            
        except Exception as e:
            logging.error(f"[AI ERROR] {e}")
            description = "The AI service is currently unavailable."
        finally:
            for img in imgs: 
                try: img.close()
                except: pass

        logging.info(f"[AI VERDICT] {description}")
        
        if dash_app:
            dash_app.notify(description, priority=1, interrupt=True)

        executor.submit(audio.announce_all, description)
        executor.submit(audio.sonos_speak_verdict, description)
        
        try:
            payload = {"token": cfg.PUSHOVER_API_TOKEN, "user": cfg.PUSHOVER_USER_KEY, "title": f"{location.title()} Activity", "message": description}
            with open(frames[-1], "rb") as f:
                requests.post("https://api.pushover.net/1/messages.json", data=payload, files={"attachment": ("snap.jpg", f, "image/jpeg")}, timeout=10)
        except Exception as e: 
            logging.error(f"Pushover failure: {e}")
        
        for f in frames:
            try: Path(f).unlink()
            except Exception as e: logging.error(f"Failed to delete frame {f}: {e}")
    else:
        failure_msg = f"The {location} video feed is unavailable."
        if dash_app: dash_app.notify(failure_msg, priority=1, interrupt=True)
        executor.submit(audio.announce_all, failure_msg)
        executor.submit(audio.sonos_speak_verdict, failure_msg)