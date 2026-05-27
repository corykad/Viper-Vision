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
_GREEN_ARTIFACT_PIXEL_FRACTION = 0.03
_BACKGROUND_REFINEMENT_FRAME_COUNT = 2
_BACKGROUND_REFINEMENT_FRAME_DELAY = 0.35

VIDEO_ANALYSIS_MODES = ("fast", "smart", "detailed", "manual")
VIDEO_ANALYSIS_LABELS = {
    "fast": "Fast mode: still image only",
    "smart": "Smart mode: fast still image first, bounded video follow-up only when needed",
    "detailed": "Detailed mode: fast still image first, video follow-up on every alert",
    "manual": "Manual mode: video only when you press an analyze button",
}
GEMINI_VISION_MODEL = "gemini-3.5-flash"
VIDEO_ANALYSIS_MODEL = GEMINI_VISION_MODEL
LEGACY_GEMINI_VISION_MODELS = {
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
}
_video_followup_last = {"front": 0.0, "back": 0.0}
_video_followup_lock = threading.Lock()


def normalize_video_analysis_settings(config_data=None):
    defaults = cfg.get_default_config().get("doorbell_video_analysis", {})
    raw_config = config_data if isinstance(config_data, dict) else cfg.load_config()
    raw = raw_config.get("doorbell_video_analysis") if isinstance(raw_config.get("doorbell_video_analysis"), dict) else {}
    settings = {**defaults, **raw}
    mode = str(settings.get("mode") or defaults.get("mode", "fast")).strip().lower()
    if mode not in VIDEO_ANALYSIS_MODES:
        mode = "fast"

    def bounded_int(name, default, minimum, maximum):
        try:
            value = int(settings.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    max_manual = bounded_int("max_manual_clip_seconds", 15, 5, 30)
    model = str(settings.get("model") or VIDEO_ANALYSIS_MODEL).strip() or VIDEO_ANALYSIS_MODEL
    if model in LEGACY_GEMINI_VISION_MODELS:
        model = VIDEO_ANALYSIS_MODEL

    return {
        "mode": mode,
        "model": model,
        "smart_clip_seconds": bounded_int("smart_clip_seconds", 3, 2, 8),
        "detailed_clip_seconds": bounded_int("detailed_clip_seconds", 5, 2, 10),
        "manual_clip_seconds": max(2, min(max_manual, bounded_int("manual_clip_seconds", 6, 2, max_manual))),
        "max_manual_clip_seconds": max_manual,
        "fps": bounded_int("fps", 2, 1, 5),
        "speak_followups": bool(settings.get("speak_followups", True)),
        "smart_cooldown_seconds": bounded_int("smart_cooldown_seconds", 60, 15, 300),
    }


def clamp_manual_video_seconds(value, config_data=None):
    settings = normalize_video_analysis_settings(config_data)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = settings["manual_clip_seconds"]
    return max(2, min(settings["max_manual_clip_seconds"], seconds))


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
                if output_file.stat().st_size > 5000 and _frame_passes_sanity_check(output_file):
                    logging.info("[RTSP] Single-frame fallback succeeded! (%s)", output_file)
                    return str(output_file)
            except Exception:
                pass
    except Exception as e:
        logging.error("[RTSP FALLBACK ERROR] %s", e)

    return None


def _frame_has_green_artifacts(image: Image.Image) -> bool:
    """Detect neon-green decoder corruption without rejecting normal lawns."""
    sample = image.convert("RGB")
    sample.thumbnail((160, 90))
    data = sample.tobytes()
    total = 0
    suspicious = 0
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        total += 1
        if g >= 150 and g - r >= 70 and g - b >= 70:
            suspicious += 1
    return bool(total and (suspicious / total) >= _GREEN_ARTIFACT_PIXEL_FRACTION)


def _frame_passes_sanity_check(frame_path: Path) -> bool:
    try:
        with Image.open(frame_path) as image:
            image.load()
            width, height = image.size
            if width < 160 or height < 90:
                logging.info("[RTSP] Skipping tiny frame %s size=%sx%s", frame_path.name, width, height)
                return False
            if _frame_has_green_artifacts(image):
                logging.warning("[RTSP] Skipping likely corrupted green-artifact frame: %s", frame_path.name)
                return False
            return True
    except Exception as e:
        logging.warning("[RTSP] Skipping unreadable frame %s: %s", frame_path.name, e)
        return False


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
            model=GEMINI_VISION_MODEL,
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
    sanity_checked_frames = {}

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
                            usable = sanity_checked_frames.get(str(frame))
                            if usable is None:
                                usable = _frame_passes_sanity_check(frame)
                                sanity_checked_frames[str(frame)] = usable
                            if usable:
                                largest_size = size
                                largest_frame = str(frame)
                        if size >= min_bytes:
                            usable = sanity_checked_frames.get(str(frame))
                            if usable is None:
                                usable = _frame_passes_sanity_check(frame)
                                sanity_checked_frames[str(frame)] = usable
                            if not usable:
                                continue
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


def _cleanup_old_video_clips(output_dir: Path, max_age_seconds: int = 3600):
    now = time.time()
    for path in output_dir.glob("video_*.mp4"):
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def capture_video_clip(
    rtsp_url: str,
    output_dir: Path,
    prefix: str,
    seconds: int = 4,
    fps: int = 2,
    scale_width: int = 640,
    timeout: float | None = None,
) -> str | None:
    """Capture a short, bounded RTSP clip for Gemini video analysis."""
    seconds = max(2, min(30, int(seconds or 4)))
    fps = max(1, min(5, int(fps or 2)))
    if timeout is None:
        timeout = max(float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS) + seconds + 8.0, seconds + 12.0)
    output_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_video_clips(output_dir)
    output_file = output_dir / f"video_{prefix}_{int(time.time() * 1000)}.mp4"
    vf_filter = f"fps={fps},scale={scale_width}:-2"
    cmd = [
        _FFMPEG_BIN, "-y",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(seconds),
        "-an",
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(output_file),
    ]
    started = time.time()
    logging.info("[VIDEO ANALYSIS] capture_start prefix=%s seconds=%s fps=%s", prefix, seconds, fps)
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        elapsed = time.time() - started
        size = output_file.stat().st_size if output_file.exists() else 0
        logging.info(
            "[VIDEO ANALYSIS] capture_done prefix=%s success=%s elapsed=%.2fs bytes=%s",
            prefix, result.returncode == 0 and size > 5000, elapsed, size,
        )
        if result.returncode == 0 and size > 5000:
            return str(output_file)
    except Exception as e:
        logging.error("[VIDEO ANALYSIS] capture_failed prefix=%s after %.2fs: %s", prefix, time.time() - started, e)
    try:
        output_file.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def analyze_video_clip(video_path: str, prompt: str, model_name: str = VIDEO_ANALYSIS_MODEL, fps: int = 2) -> str:
    started = time.time()
    try:
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        prompt = _enrich_video_analysis_prompt(prompt)
        contents = types.Content(
            parts=[
                types.Part(
                    inline_data=types.Blob(mime_type="video/mp4", data=video_bytes),
                    video_metadata=types.VideoMetadata(fps=max(1, min(5, int(fps or 2)))),
                ),
                types.Part(text=prompt),
            ]
        )
        res = _generate_video_description(contents, model_name, len(video_bytes), max_output_tokens=448)
        text = res.text.strip() if res and res.text else ""
        if _looks_like_cut_off_video_response(text) or _looks_like_low_detail_video_response(text):
            reason = "incomplete" if _looks_like_cut_off_video_response(text) else "too_brief"
            logging.warning("[VIDEO ANALYSIS] weak_response reason=%s text=%r; retrying once with richer prompt", reason, text)
            retry_contents = types.Content(
                parts=[
                    contents.parts[0],
                    types.Part(
                        text=(
                            prompt
                            + "\n\nThe previous answer was too short or incomplete: "
                              f"{text!r}. Rewatch the same video and give a more useful description. "
                              "Use 2 to 4 complete sentences and about 35 to 90 words unless there is truly nothing visible. "
                              "Include who or what is visible, where they are in the frame, what is moving, direction of travel, "
                              "important objects such as packages or vehicles, and any safety concern. End with punctuation."
                        )
                    ),
                ]
            )
            res = _generate_video_description(retry_contents, model_name, len(video_bytes), max_output_tokens=640)
            text = res.text.strip() if res and res.text else text
        return text or "I could not describe the video."
    except Exception as e:
        logging.error("[VIDEO ANALYSIS] analyze_failed model=%s after %.2fs: %s", model_name, time.time() - started, e)
        return f"Video analysis failed: {e}"


def _enrich_video_analysis_prompt(prompt: str) -> str:
    base = (prompt or "").strip()
    return (
        base
        + "\n\nVideo answer requirements: describe this for a blind homeowner. "
          "Do not answer with only a few words. Prefer 2 to 4 complete sentences and about 35 to 90 words for manual or detailed video checks. "
          "Mention people, vehicles, animals, packages, spatial position, movement direction, and safety concerns when visible. "
          "If the original prompt explicitly allows 'No extra detail from the video' and nothing meaningful changes, that exact short answer is allowed."
    )


def _generate_video_description(contents, model_name: str, byte_count: int, max_output_tokens: int = 256):
    started = time.time()
    res = get_gemini_client().models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You describe home security camera video for a blind homeowner. "
                    "Be concrete and useful. Include visible subjects, spatial position, motion or direction, "
                    "notable objects, and safety concerns. Avoid terse answers unless there is truly no useful detail. "
                    "Finish every sentence."
                ),
                temperature=0.2,
                max_output_tokens=max_output_tokens,
            ),
        )
    elapsed = time.time() - started
    finish_reason = ""
    try:
        if res and res.candidates:
            finish_reason = str(getattr(res.candidates[0], "finish_reason", "") or "")
    except Exception as e:
        logging.debug("Could not read video analysis finish reason: %s", e)
    logging.info(
        "[VIDEO ANALYSIS] model=%s took %.2fs bytes=%s finish_reason=%s output_words=%s",
        model_name,
        elapsed,
        byte_count,
        finish_reason,
        len((res.text or "").split()) if res and res.text else 0,
    )
    if res and res.usage_metadata:
        log_api_usage(res.usage_metadata)
    return res


def _looks_like_cut_off_video_response(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if len(cleaned.split()) < 6 and not cleaned.endswith((".", "!", "?")):
        return True
    if cleaned[-1] not in ".!?":
        tail = cleaned.lower().split()[-1]
        if tail in {"a", "an", "the", "your", "front", "back", "with", "near", "on", "in", "at", "to", "and", "or", "of"}:
            return True
    return False


def _looks_like_low_detail_video_response(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    lowered = cleaned.lower().strip()
    allowed_short = {
        "no extra detail from the video.",
        "no extra detail from the video",
    }
    if lowered in allowed_short:
        return False
    words = cleaned.split()
    word_count = len(words)
    if word_count < 10:
        return True
    detail_markers = {
        "left", "right", "front", "back", "porch", "door", "driveway", "yard", "walkway",
        "approach", "approaching", "leaving", "walking", "standing", "moving", "direction",
        "person", "people", "vehicle", "car", "truck", "package", "delivery", "animal",
        "dog", "cat", "carrying", "wearing", "near", "beside", "behind", "toward", "away",
    }
    marker_hits = sum(1 for word in words if word.strip(".,!?;:()[]{}\"'").lower() in detail_markers)
    generic_phrases = (
        "a person walks by",
        "someone is outside",
        "there is motion",
        "motion detected",
        "a person is visible",
        "nothing much happens",
    )
    if any(phrase in lowered for phrase in generic_phrases):
        return True
    return word_count < 22 and marker_hits < 2


def analyze_rtsp_video(
    rtsp_url: str,
    side: str = "front",
    seconds: int | None = None,
    prompt: str | None = None,
    config_data=None,
    trace_id: str | None = None,
):
    settings = normalize_video_analysis_settings(config_data)
    seconds = clamp_manual_video_seconds(seconds, config_data) if seconds is not None else settings["manual_clip_seconds"]
    prompt = prompt or (
        "You are helping a blind homeowner understand what is happening outside. "
        "Describe people, vehicles, animals, packages, motion, direction of travel, and safety concerns. "
        "Be clear and useful. Reply with one or two complete sentences. If nothing important happens, say so."
    )
    trace = trace_id or f"manual-video-{side}-{int(time.time() * 1000)}"
    started = time.time()
    clip_path = capture_video_clip(
        rtsp_url,
        cfg.DATA_DIR,
        prefix=f"{side}_{trace}",
        seconds=seconds,
        fps=settings["fps"],
    )
    if not clip_path:
        return {
            "ok": False,
            "description": "I could not capture live video from that camera.",
            "elapsed": time.time() - started,
            "clip_path": "",
        }
    description = analyze_video_clip(clip_path, prompt, model_name=settings["model"], fps=settings["fps"])
    return {
        "ok": not description.lower().startswith("video analysis failed"),
        "description": description,
        "elapsed": time.time() - started,
        "clip_path": clip_path,
    }


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


def _load_images_for_gemini(image_paths):
    return [_load_image_for_gemini(str(path)) for path in image_paths if path]


def get_gemini_description(image_path, prompt, model_name=GEMINI_VISION_MODEL):
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


def get_gemini_multi_image_description(image_paths, prompt, model_name=GEMINI_VISION_MODEL):
    started = time.time()
    try:
        images = _load_images_for_gemini(image_paths)
        if not images:
            return "The video feed is unavailable."

        res = get_gemini_client().models.generate_content(
            model=model_name,
            contents=images,
            config=types.GenerateContentConfig(
                system_instruction=prompt,
                temperature=0.2,
            ),
        )

        elapsed = time.time() - started
        logging.info("[AI TIMING] multi_image model=%s images=%s took %.2fs", model_name, len(images), elapsed)
        if res and res.usage_metadata:
            log_api_usage(res.usage_metadata)
        return res.text.strip() if res and res.text else "Activity detected."
    except Exception as e:
        elapsed = time.time() - started
        logging.error("[AI ERROR] multi_image model=%s after %.2fs: %s", model_name, elapsed, e)
        return "The AI service is currently unavailable."


def _background_refinement_prompt(location: str, first_description: str, base_prompt: str) -> str:
    return (
        f"{base_prompt or 'Describe the scene.'}\n\n"
        f"This is a delayed background refinement for the {location}. "
        f"The fast first description was: {first_description}. "
        "Compare the provided still frames and give the best corrected description for a blind homeowner. "
        "Focus on people, packages, vehicles, animals, direction of movement, and anything safety relevant. "
        "If the fast description was already correct, improve it only if the later frames add useful detail. "
        "Keep the answer under 35 words."
    )


def capture_background_refinement_frames(
    rtsp_url: str,
    output_dir: Path,
    prefix: str,
    min_bytes: int,
    count: int = _BACKGROUND_REFINEMENT_FRAME_COUNT,
) -> list[str]:
    frames: list[str] = []
    for index in range(count):
        time.sleep(_BACKGROUND_REFINEMENT_FRAME_DELAY)
        frame = grab_frame(
            rtsp_url,
            output_dir=output_dir,
            prefix=f"{prefix}_{index + 1}",
            min_bytes=min_bytes,
            fast_mode=True,
            timeout=max(3.0, min(float(cfg.RTSP_CONNECT_TIMEOUT_SECONDS), 6.0)),
        )
        if frame and Path(frame).exists():
            frames.append(frame)
    logging.info("[AI REFINE] Captured %s background still frames for %s", len(frames), prefix)
    return frames


def refine_weak_doorbell_still_description(
    rtsp_url: str,
    output_dir: Path,
    prefix: str,
    min_bytes: int,
    location: str,
    first_description: str,
    base_prompt: str,
    initial_frame: str | None = None,
) -> tuple[str, list[str]]:
    frames = [initial_frame] if initial_frame and Path(initial_frame).exists() else []
    frames.extend(capture_background_refinement_frames(rtsp_url, output_dir, prefix, min_bytes))
    if not frames:
        return first_description, []

    prompt = _background_refinement_prompt(location, first_description, base_prompt)
    if len(frames) == 1:
        refined = get_gemini_description(frames[0], prompt, model_name=GEMINI_VISION_MODEL)
    else:
        refined = get_gemini_multi_image_description(frames, prompt, model_name=GEMINI_VISION_MODEL)

    if not _description_is_weak(refined):
        logging.info("[AI REFINE] Background still refinement improved weak first pass.")
        return refined, frames

    logging.info("[AI REFINE] Background still refinement remained weak; keeping first pass.")
    return first_description, frames


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

    corruption_markers = (
        "green artifact", "green artifacts", "digital corruption",
        "digital noise", "green and grey bands", "green and gray bands",
        "corrupted", "heavily distorted", "distorted by",
    )
    if any(m in lowered for m in corruption_markers):
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


def _description_is_service_unavailable(text: str) -> bool:
    return (text or "").strip().lower() == "the ai service is currently unavailable."


def _description_needs_video_followup(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if _description_is_weak(lowered) or _description_is_service_unavailable(lowered):
        return True
    escalation_markers = (
        "no one", "no person", "no people", "nothing important", "unable",
        "unavailable", "cannot determine", "can't determine", "not visible",
        "dark", "blurred", "partial", "obscured", "motion", "movement",
        "unclear", "hard to tell", "can't tell", "cannot tell",
    )
    return any(marker in lowered for marker in escalation_markers)


def should_run_automatic_video_followup(mode: str, description: str, side: str, config_data=None) -> bool:
    settings = normalize_video_analysis_settings(config_data)
    mode = (mode or settings["mode"]).strip().lower()
    if mode in {"fast", "manual"}:
        return False
    if mode == "detailed":
        return True
    if mode != "smart":
        return False
    if not _description_needs_video_followup(description):
        return False
    now = time.time()
    cooldown = settings["smart_cooldown_seconds"]
    key = "back" if side == "back" else "front"
    with _video_followup_lock:
        if now - _video_followup_last.get(key, 0.0) < cooldown:
            logging.info("[VIDEO ANALYSIS] smart_followup_suppressed side=%s cooldown=%ss", key, cooldown)
            return False
        _video_followup_last[key] = now
    return True


def _video_followup_prompt(location: str, first_description: str, mode: str, config_data=None) -> str:
    configured = cfg.get_doorbell_video_prompt(
        config_data,
        mode="detailed" if mode == "detailed" else "smart",
        location=location,
        first_description=first_description,
    )
    if configured:
        return configured
    if mode == "detailed":
        return (
            f"You are helping a blind homeowner understand the {location}. "
            "Analyze this short security video. Mention people, packages, vehicles, animals, movement, direction, and safety concerns. "
            "Keep it under 45 words and use complete sentences."
        )
    return (
        f"The first still image said: {first_description}. "
        f"You are helping a blind homeowner understand the {location}. "
        "Use this short video only to add missing useful details. "
        "If nothing meaningful changes, say: No extra detail from the video. Use complete sentences."
    )


def _video_followup_adds_value(first_description: str, followup: str) -> bool:
    lowered = (followup or "").strip().lower()
    if not lowered:
        return False
    if lowered in {"no extra detail from the video.", "no extra detail from the video"}:
        return False
    if "no extra detail" in lowered and not _description_is_weak(first_description):
        return False
    return True


def get_best_gemini_description(image_path, prompt):
    """Return the fastest strong Gemini description.

    Fast path:
      1. Ask the current Gemini vision model first.
      2. If that result is strong, return immediately.
      3. Retry once with the same model when the first answer is weak.

    This keeps the common-case doorbell path low-latency instead of waiting for
    the slower backup model every time.
    """
    first_model = GEMINI_VISION_MODEL
    second_model = GEMINI_VISION_MODEL

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
            model=GEMINI_VISION_MODEL,
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

        sys_prompt = cfg.get_doorbell_photo_prompt(current_config, key) or "Describe the scene."

        if fast_frame:
            first_model = GEMINI_VISION_MODEL
            description = get_gemini_description(fast_frame, sys_prompt, model_name=first_model)
            weak_first_pass = _description_is_weak(description)
            if _description_is_service_unavailable(description):
                logging.warning("[AI SELECT] Fast-pass AI service unavailable; trying backup model before speech")
                refined_description = get_gemini_description(fast_frame, sys_prompt, model_name=GEMINI_VISION_MODEL)
                if not _description_is_weak(refined_description):
                    description = refined_description
                    weak_first_pass = False
                    logging.info("[AI SELECT] Backup model recovered doorbell speech before notification")
                else:
                    weak_first_pass = True
                    logging.warning("[AI SELECT] Backup model did not recover before speech")
            elif weak_first_pass:
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

        video_settings = normalize_video_analysis_settings(current_config)
        video_mode = video_settings["mode"]

        def background_video_followup_pipeline():
            try:
                seconds = (
                    video_settings["detailed_clip_seconds"]
                    if video_mode == "detailed"
                    else video_settings["smart_clip_seconds"]
                )
                prompt = _video_followup_prompt(location, description, video_mode, current_config)
                result = analyze_rtsp_video(
                    rtsp_url,
                    side=key,
                    seconds=seconds,
                    prompt=prompt,
                    config_data=current_config,
                    trace_id=trace_id,
                )
                followup = result.get("description") or "Video follow-up did not return a description."
                logging.info(
                    "[VIDEO ANALYSIS] followup_done trace=%s event=%s mode=%s ok=%s elapsed=%.2fs text=%r",
                    trace_id, key, video_mode, result.get("ok"), result.get("elapsed", 0.0), followup,
                )
                if dash_app and hasattr(dash_app, "record_video_analysis_result"):
                    dash_app.record_video_analysis_result(key, followup, result, source=f"automatic {video_mode}")
                if video_settings.get("speak_followups", True) and _video_followup_adds_value(description, followup):
                    audio.play_notification("doorbell", f"Video follow-up: {followup}")
                    if dash_app:
                        dash_app.notify(f"Video follow-up: {followup}", priority=1, interrupt=True)
            except Exception:
                logging.exception("[VIDEO ANALYSIS] followup_failed trace=%s event=%s", trace_id, key)

        if should_run_automatic_video_followup(video_mode, description, key, current_config):
            logging.info("[VIDEO ANALYSIS] followup_submitted trace=%s event=%s mode=%s", trace_id, key, video_mode)
            executor.submit(background_video_followup_pipeline)
        else:
            logging.info("[VIDEO ANALYSIS] automatic_followup_skipped trace=%s event=%s mode=%s", trace_id, key, video_mode)

        # ── STAGE 2: BACKGROUND PUSHOVER / HD REFINEMENT ──────────────────
        def background_pushover_pipeline():
            hd_frame = None
            refinement_frames = []
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

                # First pass was weak — refine in the background. Speech has
                # already happened, so this path can spend a little extra time
                # on multiple still frames without delaying the doorbell alert.
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
                    refined_description, refinement_frames = refine_weak_doorbell_still_description(
                        rtsp_url,
                        output_dir=cfg.DATA_DIR,
                        prefix=f"refine_{key}_{unique_id}",
                        min_bytes=min_bytes,
                        location=location,
                        first_description=description,
                        base_prompt=sys_prompt,
                        initial_frame=target_frame,
                    )
                    if not _description_is_weak(refined_description):
                        pushover_description = refined_description
                        logging.info("[PUSHOVER] Using multi-frame refined description for %s", key)
                        if refined_description != description and _video_followup_adds_value(description, refined_description):
                            audio.play_notification("doorbell", f"Update: {refined_description}")
                            if dash_app:
                                dash_app.notify(f"Update: {refined_description}", priority=1, interrupt=True)
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
                for path_str in refinement_frames:
                    try: Path(path_str).unlink(missing_ok=True)
                    except Exception: pass

        executor.submit(background_pushover_pipeline)

    except Exception:
        logging.exception("Unhandled error in process_doorbell")
