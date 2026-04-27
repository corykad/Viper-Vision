import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from queue import Empty, PriorityQueue
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import soco
import wx
import wx.adv
from accessible_output2.outputs import auto
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from waitress import serve

import viper_audio as audio
import viper_config as cfg
import viper_discovery as discovery
import viper_diagnostics as diagnostics
import viper_ha_listener as ha_listener
import viper_ha_package as ha_package
import viper_ring_discovery as ring_discovery
import viper_vision as vision

# --- THREAD POOL & SAFE SHUTDOWN LOGIC ---
executor = ThreadPoolExecutor(max_workers=12)
is_shutting_down = threading.Event()

def safe_submit(fn, *args, **kwargs):
    """
    Centralized, thread-safe task submitter. 
    Prevents the app from throwing RuntimeErrors if a webhook, UI button, 
    or background loop tries to fire while the app is closing.
    """
    if is_shutting_down.is_set():
        logging.debug(f"Ignored task {fn.__name__}: System is shutting down.")
        return None
    try:
        return executor.submit(fn, *args, **kwargs)
    except RuntimeError as e:
        logging.warning(f"Executor rejected task {fn.__name__} during shutdown: {e}")
        return None

# --- FLASK INIT ---
def _resolve_template_dir() -> str:
    preferred = cfg.APP_DIR / "templates"
    fallback = cfg.APP_DIR
    if (preferred / "remote.html").exists():
        return str(preferred)
    if (fallback / "remote.html").exists():
        return str(fallback)
    return str(preferred)


def _help_file(topic="index"):
    topic = re.sub(r"[^a-zA-Z0-9_-]", "", topic or "index") or "index"
    preferred = cfg.APP_DIR / "help" / f"{topic}.html"
    fallback = Path(__file__).parent.absolute() / "help" / f"{topic}.html"
    if preferred.exists():
        return preferred
    if fallback.exists():
        return fallback
    return cfg.APP_DIR / "help" / "index.html"


def open_help(topic="index"):
    path = _help_file(topic)
    if path.exists():
        webbrowser.open(path.resolve().as_uri())
        return True
    logging.warning("Help file not found for topic=%s path=%s", topic, path)
    return False


OFFICIAL_LINKS = {
    "ha_windows": "https://www.home-assistant.io/installation/windows/",
    "ha_install": "https://www.home-assistant.io/installation/",
    "ha_tokens": "https://developers.home-assistant.io/docs/auth_api/",
    "virtualbox": "https://www.virtualbox.org/wiki/Downloads",
    "ha_os_releases": "https://github.com/home-assistant/operating-system/releases/latest",
    "mosquitto_docs": "https://github.com/home-assistant/addons/blob/master/mosquitto/DOCS.md",
    "ring_mqtt_addon": "https://github.com/tsightler/ring-mqtt-ha-addon",
}


def open_official_link(key):
    url = OFFICIAL_LINKS.get(key)
    if not url:
        return False
    webbrowser.open(url)
    return True


def find_vboxmanage():
    candidates = [
        shutil.which("VBoxManage.exe"),
        shutil.which("VBoxManage"),
        r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
        r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return ""


def get_virtualbox_status():
    exe = find_vboxmanage()
    if not exe:
        return {"installed": False, "path": "", "version": "", "message": "VirtualBox was not found on this PC."}
    version = ""
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        version = (result.stdout or result.stderr or "").strip()
    except Exception as e:
        version = f"version check failed: {e}"
    return {"installed": True, "path": exe, "version": version, "message": f"VirtualBox found at {exe}."}

app = Flask(__name__, template_folder=_resolve_template_dir())
app.secret_key = os.getenv("VIPER_SECRET_KEY", "viper_vision_secure_key")
dash_app = None
activity_logs = []

RUNNING_SENTINEL = cfg.DATA_DIR / "viper_running.sentinel"
CRASH_LOG_PATH = cfg.DATA_DIR / "viper_last_crash.txt"
previous_run_unclean = RUNNING_SENTINEL.exists()


def _write_crash_report(exc_type, exc_value, exc_traceback, *, source="main"):
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    try:
        CRASH_LOG_PATH.write_text(
            f"Viper Vision unhandled exception from {source} at {datetime.now().isoformat(timespec='seconds')}\n\n{text}",
            encoding="utf-8",
        )
    except OSError:
        pass
    logging.critical("Unhandled exception from %s:\n%s", source, text)
    return text


def install_crash_hooks():
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        _write_crash_report(exc_type, exc_value, exc_traceback, source="main")
        try:
            if wx.GetApp():
                wx.CallAfter(wx.MessageBox, "Viper hit an error. A diagnostic log was saved.", "Viper Vision Error", wx.OK | wx.ICON_ERROR)
        except Exception:
            pass

    def handle_thread_exception(args):
        _write_crash_report(args.exc_type, args.exc_value, args.exc_traceback, source=f"thread:{args.thread.name if args.thread else 'unknown'}")

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


def mark_app_running():
    try:
        RUNNING_SENTINEL.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


def mark_app_clean_shutdown():
    try:
        RUNNING_SENTINEL.unlink(missing_ok=True)
    except OSError:
        pass

# --- VOICE MAPPINGS ---
EDGE_VOICES = {
    "Andrew (Natural Male)": "en-US-AndrewNeural",
    "Ava (Natural Female)": "en-US-AvaNeural",
    "Aria (Female)": "en-US-AriaNeural",
    "Guy (Male)": "en-US-GuyNeural",
    "Jenny (Female)": "en-US-JennyNeural",
    "Emma (Natural Female)": "en-US-EmmaNeural",
    "Brian (Natural Male)": "en-US-BrianNeural",
    "Sonia (UK Female)": "en-GB-SoniaNeural"
}

GEMINI_TTS_VOICES = {
    "Zephyr (Bright)": "Zephyr",
    "Puck (Upbeat)": "Puck",
    "Charon (Informative)": "Charon",
    "Kore (Firm)": "Kore",
    "Fenrir (Excitable)": "Fenrir",
    "Leda (Youthful)": "Leda",
    "Orus (Firm)": "Orus",
    "Aoede (Breezy)": "Aoede",
    "Callirrhoe (Easy-going)": "Callirrhoe",
    "Autonoe (Bright)": "Autonoe",
    "Enceladus (Breathy)": "Enceladus",
    "Iapetus (Clear)": "Iapetus",
    "Umbriel (Easy-going)": "Umbriel",
    "Algieba (Smooth)": "Algieba",
    "Despina (Smooth)": "Despina",
    "Erinome (Clear)": "Erinome",
    "Algenib (Gravelly)": "Algenib",
    "Rasalgethi (Informative)": "Rasalgethi",
    "Laomedeia (Upbeat)": "Laomedeia",
    "Achernar (Soft)": "Achernar",
    "Alnilam (Firm)": "Alnilam",
    "Schedar (Even)": "Schedar",
    "Gacrux (Mature)": "Gacrux",
    "Pulcherrima (Forward)": "Pulcherrima",
    "Achird (Friendly)": "Achird",
    "Zubenelgenubi (Casual)": "Zubenelgenubi",
    "Vindemiatrix (Gentle)": "Vindemiatrix",
    "Sadachbia (Lively)": "Sadachbia",
    "Sadaltager (Knowledgeable)": "Sadaltager",
    "Sulafat (Warm)": "Sulafat",
}

GEMINI_TTS_MODELS = [
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
]

VOICE_BEHAVIOR_MODES = {
    "Fast reliable voice, uses Microsoft Edge TTS": "fast_reliable",
    "Natural emotional voice, uses Gemini cloud TTS": "natural_gemini",
    "Regular Google TTS voice, uses Google Translate speech": "google_regular",
    "Offline fallback voice, uses Windows local speech": "offline_fallback",
}

VOICE_PERSONALITIES = {
    "Warm friendly voice": {"key": "warm", "voice": "Sulafat", "style": "[warm, clear, friendly]"},
    "Clear crisp voice": {"key": "clear", "voice": "Iapetus", "style": "[clear, crisp, decently fast]"},
    "Firm authoritative voice": {"key": "firm", "voice": "Kore", "style": "[firm, authoritative, clear]"},
    "Upbeat bright voice": {"key": "upbeat", "voice": "Puck", "style": "[upbeat, bright, clear]"},
}

VOICE_SPEEDS = {
    "Relaxed, slower than normal": "relaxed",
    "Normal conversational speed": "normal",
    "Brisk, slightly faster": "brisk",
    "Fast alert speed": "fast",
    "Very fast but still clear": "very_fast",
}

TTS_PROFILE_LABELS = {
    "doorbell": "Doorbell Alerts",
    "utilities": "Utilities",
    "manual": "Manual Broadcasts",
}

CINDERELLA_STATUS_EVENT_MAP = {
    "starting": "departure",
    "cleaning": "departure",
    "spot_cleaning": "departure",
    "zoned_cleaning": "departure",
    "zone_cleaning": "departure",
    "segment_cleaning": "departure",
    "mapping": "departure",
    "patrol": "departure",
    "robot_status_mopping": "departure",
    "clean_mop_cleaning": "departure",
    "clean_mop_mopping": "departure",
    "segment_mopping": "departure",
    "segment_clean_mop_cleaning": "departure",
    "segment_clean_mop_mopping": "departure",
    "zoned_mopping": "departure",
    "zoned_clean_mop_cleaning": "departure",
    "zoned_clean_mop_mopping": "departure",
    "washing_mop": "washing",
    "washing_the_mop": "washing",
    "washing_the_mop_2": "washing",
    "back_to_dock_washing_duster": "washing",
    "emptying": "emptying",
    "emptying_bin": "emptying",
    "emptying_dustbin": "emptying",
    "emptying_the_bin": "emptying",
    "returning": "returning",
    "returning_home": "returning",
    "docking": "returning",
    "going_to_target": "returning",
    "going_to_wash_the_mop": "returning",
    "attaching_the_mop": "returning",
    "detaching_the_mop": "returning",
    "air_drying_stopping": "returning",
    "charging": "victory",
    "docked": "victory",
    "charging_complete": "victory",
    "idle": "victory",
    "paused": "paused",
    "remote_control_active": "paused",
    "manual_mode": "paused",
    "in_call": "paused",
    "locked": "paused",
    "unknown": "status_update",
    "charger_disconnected": "status_update",
    "charging_problem": "status_update",
    "error": "status_update",
    "shutting_down": "status_update",
    "updating": "status_update",
    "device_offline": "status_update",
    "egg_attack": "status_update",
}

DIALECTS = {
    "American": "com", 
    "British": "co.uk", 
    "Australian": "com.au", 
    "Indian": "co.in"
}

# --- HEALTH MONITOR ---
def _resolve_host(host: str) -> str:
    try: return socket.gethostbyname(host)
    except Exception: return host

def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    resolved = _resolve_host(host)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((resolved, port)) == 0

def monitor_plumbing():
    last_state = {"mqtt": None, "camera": None}
    
    while not is_shutting_down.wait(60.0):
        try:
            mqtt_ok = _port_open(cfg.HA_IP, 1883)
            camera_ok = _port_open(cfg.HA_IP, 8554)

            if last_state["mqtt"] is None:
                last_state["mqtt"] = mqtt_ok
            elif not mqtt_ok and last_state["mqtt"]:
                safe_submit(audio.play_notification, "utilities", "System alert: The MQTT broker is unreachable. I am no longer listening for the doorbell.")
                last_state["mqtt"] = False
            elif mqtt_ok and not last_state["mqtt"]:
                safe_submit(audio.play_notification, "utilities", "System alert cleared. MQTT broker is back online.")
                last_state["mqtt"] = True

            if last_state["camera"] is None:
                last_state["camera"] = camera_ok
            elif not camera_ok and last_state["camera"]:
                safe_submit(audio.play_notification, "utilities", "System alert: The camera stream is unreachable. I can hear the doorbell, but I cannot see who is there.")
                last_state["camera"] = False
            elif camera_ok and not last_state["camera"]:
                safe_submit(audio.play_notification, "utilities", "System alert cleared. The camera stream is back online.")
                last_state["camera"] = True
        except Exception as e:
            logging.error(f"Health Monitor Error: {e}")

threading.Thread(target=monitor_plumbing, daemon=True).start()

# --- WEB HELPERS ---


def ensure_cinderella_message_config(config_obj: dict) -> dict:
    default_messages = cfg.get_default_config().get("cinderella_messages", {})
    current = config_obj.get("cinderella_messages", {}) if isinstance(config_obj, dict) else {}
    merged = cfg._deep_merge(default_messages, current)
    config_obj["cinderella_messages"] = merged
    return merged


def choose_cinderella_message(event: str, error: str = "", source: str = "vacuum") -> str:
    if dash_app is None:
        config_obj = cfg.load_config()
    else:
        config_obj = dash_app.config
    messages = ensure_cinderella_message_config(config_obj)
    error_key = (error or "").strip().lower().replace(" ", "_").replace("-", "_")
    cleaned_error = error_key.replace("_", " ") if error_key else ""

    event_key = (event or "").strip().lower().replace(" ", "_").replace("-", "_")
    if event_key != "error":
        event_key = CINDERELLA_STATUS_EVENT_MAP.get(event_key, event_key)

    if event_key == "error":
        specific = messages.get("specific_errors", {})
        specific_keys = [f"dock_{error_key}", error_key] if source == "dock" else [error_key]
        for specific_key in specific_keys:
            if specific_key and specific_key in specific and specific[specific_key]:
                return random.choice([m for m in specific[specific_key] if str(m).strip()])
        template_key = "dock_error_templates" if source == "dock" else "vacuum_error_templates"
        templates = [m for m in messages.get(template_key, []) if str(m).strip()]
        if not templates:
            templates = ["Cinderella has an issue: {error}."]
        return random.choice(templates).format(error=cleaned_error or "unknown issue")

    choices = [m for m in messages.get(event_key, []) if str(m).strip()]
    if choices:
        return random.choice(choices)
    return ""

def _json_or_redirect(message: str, ok: bool = True, status_code: int = 200):
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"
    if wants_json:
        return jsonify({"ok": ok, "message": message}), status_code
    flash(message)
    return redirect(url_for("remote_ui"))

def _resolve_channel_settings(channel: str, config: dict) -> dict:
    """Return mode+chime for the given channel with a graceful fallback chain.

    Fallback order:
      fridge_open / fridge_closed  →  fridge  →  default
      freezer_open / freezer_closed →  freezer →  default
      anything else                →  default
    """
    channels = config.get("broadcast_channels", {})
    ch_key = (channel or "").lower()

    # Walk the fallback chain
    candidates = [ch_key]
    if ch_key in ("fridge_open", "fridge_closed"):
        candidates.append("fridge")
    elif ch_key in ("freezer_open", "freezer_closed"):
        candidates.append("freezer")
    candidates.append("default")

    for key in candidates:
        if key in channels:
            entry = channels[key]
            return {"mode": entry.get("mode", "speak"), "chime": entry.get("chime", "")}

    return {"mode": "speak", "chime": ""}


def _broadcast_message(raw_message: str, push: bool = False, channel: str = ""):
    """Dispatch a broadcast according to its channel's configured behaviour.

    channel=""/"default" → global default
    channel="fridge_open" etc → per-state fridge/freezer control
    channel="manual"          → always speak (GUI / manual web UI)
    """
    if dash_app is None or is_shutting_down.is_set():
        return _json_or_redirect("System not ready or shutting down.", ok=False, status_code=503)

    msg = (raw_message or "").strip()
    if not msg:
        return _json_or_redirect("No message provided.", ok=False, status_code=400)

    try:
        config = dash_app.config

        # Manual broadcasts always speak regardless of channel settings
        if channel == "manual":
            ch_settings = {"mode": "speak", "chime": ""}
        else:
            ch_settings = _resolve_channel_settings(channel, config)

        mode  = ch_settings["mode"]
        chime = ch_settings["chime"]

        wx.CallAfter(
            dash_app.notify,
            f"Broadcast [{channel or 'default'}] [{mode}]: {msg}",
            priority=3, interrupt=True,
        )

        if mode == "silent":
            logging.info("[BROADCAST] Silent channel=%r — logged only: %r", channel, msg)
            return _json_or_redirect(f"Broadcast logged (silent): {msg}")

        if mode == "chime":
            future = safe_submit(audio.play_broadcast_chime, chime, channel)
            if future is None:
                return _json_or_redirect("System shutting down.", ok=False, status_code=503)
            logging.info("[BROADCAST] Chime channel=%r chime=%r for: %r", channel, chime, msg)
            return _json_or_redirect(f"Chime played for: {msg}")

        # speak
        broadcast_context = {
            "channel": "broadcast",
            "push": push,
            "received_ts": time.time(),
            "received_iso": datetime.now().isoformat(timespec="seconds"),
        }
        future = safe_submit(audio.play_notification, "manual", msg, push)
        if future is None:
            return _json_or_redirect("Broadcast rejected — system shutting down.", ok=False, status_code=503)
        return _json_or_redirect(f"Broadcast sent: {msg}")

    except Exception as e:
        logging.exception("Broadcast route failed")
        return _json_or_redirect(f"Broadcast failed: {e}", ok=False, status_code=500)

def _doorbell_rtsp_for_key(key: str):
    settings = cfg.get_resolved_doorbell_settings(include_env=True)
    if key == "back":
        return settings.get("rtsp_back") or ""
    return settings.get("rtsp_front") or ""


def _handle_doorbell(location: str, rtsp_url: str, key: str):
    received_ts = time.time()
    if dash_app is None or is_shutting_down.is_set():
        return "System not ready or shutting down", 503
    rtsp_url = rtsp_url or _doorbell_rtsp_for_key(key)
    if not rtsp_url:
        settings = cfg.get_resolved_doorbell_settings(include_env=True)
        logging.warning("Doorbell webhook ignored for %s: no RTSP URL configured", location)
        logging.warning(
            "Doorbell resolver state for %s: ha_ip=%r front_camera_id=%r back_camera_id=%r raw_front=%r raw_back=%r",
            key,
            cfg.get_ha_settings(include_env=True).get("ha_ip"),
            settings.get("front_camera_id"),
            settings.get("back_camera_id"),
            settings.get("raw_rtsp_front"),
            settings.get("raw_rtsp_back"),
        )
        return "No RTSP URL configured for this door", 409
    try:
        trace_id = f"doorbell-{key}-{int(received_ts * 1000)}"
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s webhook_received location=%s rtsp_configured=%s",
            trace_id, key, location, bool(rtsp_url),
        )
        future = safe_submit(vision.process_doorbell, location, rtsp_url, key, dash_app, executor, trace_id, received_ts)
        if future is None:
            return "Rejected during shutdown", 503
        logging.info(
            "[DOORBELL TIMING] trace=%s event=%s process_submitted=%.3fs",
            trace_id, key, time.time() - received_ts,
        )
        return "OK", 200
    except Exception as e:
        logging.exception("Doorbell webhook failed")
        return f"ERROR: {e}", 500


def _dispatch_cinderella_event(event: str, error: str = "", source: str = "vacuum"):
    if dash_app is None or is_shutting_down.is_set():
        return False
    event = (event or "").strip().lower().replace(" ", "_").replace("-", "_")
    error = (error or "").strip().lower().replace(" ", "_").replace("-", "_")
    source = (source or "vacuum").strip().lower()
    if not dash_app.config.get("cinderella_enabled", True):
        return False
    message = ""
    if dash_app.config.get("cinderella_ai_mode"):
        try:
            message = vision.generate_cinderella_message(event, source, error)
        except Exception as e:
            logging.warning("[CINDERELLA AI] listener generation failed: %s", e)
    if not message:
        message = choose_cinderella_message(event, error=error, source=source)
    if not message:
        return False
    logging.info("[HA LISTENER] Cinderella event=%s source=%s error=%s message=%r", event, source, error, message)
    safe_submit(audio.play_notification, "utilities", message)
    wx.CallAfter(dash_app.notify, message, 3)
    return True


def _handle_ha_listener_action(action: dict):
    if dash_app is None or is_shutting_down.is_set():
        return
    action_type = (action or {}).get("type")
    if action_type == "doorbell":
        side = action.get("side") or "front"
        location = action.get("location") or ("back door" if side == "back" else "front door")
        status, code = _handle_doorbell(location, action.get("rtsp_url") or "", side)
        logging.info("[HA LISTENER] doorbell action side=%s code=%s status=%s", side, code, status)
    elif action_type == "cinderella":
        _dispatch_cinderella_event(action.get("event", ""), action.get("error", ""), action.get("source", "vacuum"))
    elif action_type == "broadcast":
        message = (action.get("message") or "").strip()
        channel = (action.get("channel") or "default").strip()
        if message:
            logging.info("[HA LISTENER] broadcast channel=%s message=%r", channel, message)
            safe_submit(audio.play_notification, "utilities", message)
            wx.CallAfter(dash_app.notify, message, 5)

# ==========================================
# FLASK ROUTES & WEBHOOKS
# ==========================================
@app.route("/")
def index():
    return redirect(url_for("remote_ui"))

@app.route("/doorbell-webhook", methods=["POST"])
def handle_front():
    return _handle_doorbell("front door", _doorbell_rtsp_for_key("front"), "front")

@app.route("/doorbell-webhook/back", methods=["POST"])
def handle_back():
    return _handle_doorbell("back door", _doorbell_rtsp_for_key("back"), "back")

@app.route("/remote", methods=["GET", "POST"])
@app.route("/remote/", methods=["GET", "POST"])
def remote_ui():
    if request.method == "POST":
        return _broadcast_message(request.form.get("broadcast_text", ""))
    if dash_app is None:
        return "System initializing, please refresh...", 503
    ensure_cinderella_message_config(dash_app.config)
    vacuum = _build_web_vacuum_context()
    chime_files = ["(Default)"]
    if cfg.CHIMES_DIR.exists():
        for f in cfg.CHIMES_DIR.iterdir():
            if f.suffix.lower() in [".mp3", ".wav"]:
                chime_files.append(f.name)
    return render_template(
        "remote.html",
        config=dash_app.config,
        activity_logs=activity_logs,
        chimes=chime_files,
        edge_voices=EDGE_VOICES,
        gemini_tts_voices=GEMINI_TTS_VOICES,
        dialects=DIALECTS,
        vacuum=vacuum,
    )


def _current_diagnostics(*, check_ha=False):
    if dash_app is None:
        return diagnostics.collect_diagnostics({})
    ha_connection = {"checked": False}
    if check_ha:
        ha_settings = cfg.get_ha_settings(dash_app.config, include_env=True)
        result = discovery.test_ha_connection(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        ha_connection = {
            "checked": True,
            "ok": bool(result.get("ok")),
            "message": result.get("message") or result.get("error") or "",
            "entity_count": result.get("entity_count"),
        }
    listener_status = dash_app.ha_listener.status() if hasattr(dash_app, "ha_listener") else {}
    return diagnostics.collect_diagnostics(
        dash_app.config,
        ha_listener_status=listener_status,
        ha_connection=ha_connection,
    )


@app.route("/remote/diagnostics", methods=["GET"])
def web_diagnostics():
    if dash_app is None:
        return "System initializing, please refresh...", 503
    diag = _current_diagnostics(check_ha=request.args.get("check_ha") == "1")
    wants_json = request.accept_mimetypes.best == "application/json" or request.args.get("format") == "json"
    if wants_json:
        return jsonify(diag)
    return "<pre>" + diagnostics.diagnostics_text(diag).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>"


@app.route("/remote/diagnostics/support_bundle", methods=["POST"])
def web_support_bundle():
    if dash_app is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        diag = _current_diagnostics(check_ha=True)
        result = diagnostics.create_support_bundle(
            dash_app.config,
            ha_listener_status=diag.get("ha_listener", {}),
            ha_connection=diag.get("ha_connection", {}),
        )
        flash(f"Support bundle created: {result['path']}")
    except Exception as e:
        logging.exception("Support bundle creation failed")
        flash(f"Support bundle failed: {e}")
    return redirect(url_for("remote_ui"))

def _ha_domain_from_entity_id(entity_id):
    return entity_id.split(".", 1)[0] if isinstance(entity_id, str) and "." in entity_id else ""

def _web_entity_name(entity):
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    return str(attrs.get("friendly_name") or entity.get("entity_id") or "")

def _web_short_entity_label(entity):
    name = _web_entity_name(entity)
    entity_id = entity.get("entity_id", "")
    return f"{name} ({entity_id})" if name and name != entity_id else entity_id

def _web_vacuum_tokens(entity_id):
    tokens = {"roborock", "cinderella", "saros", "qrevo", "q revo"}
    if entity_id and "." in entity_id:
        base = entity_id.split(".", 1)[1]
        tokens.add(base.lower())
        tokens.update(part for part in re.split(r"[_\s-]+", base.lower()) if len(part) >= 4)
    return tokens

def _web_looks_like_roborock(entity):
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    text = " ".join(
        str(part).lower()
        for part in [
            entity.get("entity_id"),
            attrs.get("friendly_name"),
            attrs.get("manufacturer"),
            attrs.get("model"),
            attrs.get("platform"),
            attrs.get("integration"),
        ]
    )
    return any(token in text for token in ["roborock", "cinderella", "saros", "qrevo", "q revo", "s7", "s8"])

def _web_show_vacuum_setting(entity):
    entity_id = entity.get("entity_id", "")
    domain = _ha_domain_from_entity_id(entity_id)
    if domain in {"select", "number"}:
        return True
    if domain == "switch" and "child_lock" in entity_id:
        return True
    return False

def _build_web_vacuum_context():
    empty = {
        "ok": False,
        "message": "Home Assistant is not ready.",
        "vacuums": [],
        "selected": "",
        "selected_entity": None,
        "status_lines": [],
        "fan_speeds": [],
        "settings": [],
        "rooms": [],
    }
    if not dash_app:
        return empty
    ha_settings = cfg.get_ha_settings(dash_app.config, include_env=True)
    result = discovery.get_ha_states(
        token=ha_settings.get("ha_token"),
        ha_ip=ha_settings.get("ha_ip"),
        ha_port=ha_settings.get("ha_port"),
        timeout=8,
    )
    if not result.get("ok"):
        empty["message"] = result.get("message") or result.get("error") or "Home Assistant scan failed."
        return empty
    states = result.get("states", [])
    vacuums = [entity for entity in states if _ha_domain_from_entity_id(entity.get("entity_id", "")) == "vacuum"]
    roborock_vacuums = [entity for entity in vacuums if _web_looks_like_roborock(entity)]
    vacuums = roborock_vacuums or vacuums
    selected = request.values.get("vacuum_entity", "")
    if selected and not any(entity.get("entity_id") == selected for entity in vacuums):
        selected = ""
    selected = selected or (vacuums[0].get("entity_id") if vacuums else "")
    selected_entity = next((entity for entity in vacuums if entity.get("entity_id") == selected), None)
    tokens = _web_vacuum_tokens(selected)
    related = []
    for entity in states:
        entity_id = entity.get("entity_id", "")
        domain = _ha_domain_from_entity_id(entity_id)
        if domain not in {"vacuum", "select", "number", "switch", "button", "sensor", "binary_sensor"}:
            continue
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        text = " ".join(str(part).lower() for part in [entity_id, attrs.get("friendly_name"), attrs.get("manufacturer"), attrs.get("model")])
        if entity_id == selected or any(token and token in text for token in tokens):
            related.append(entity)
    related = sorted(related, key=lambda e: (_ha_domain_from_entity_id(e.get("entity_id", "")), _web_entity_name(e).lower()))
    attrs = selected_entity.get("attributes") if selected_entity and isinstance(selected_entity.get("attributes"), dict) else {}
    status_lines = []
    if selected_entity:
        battery = attrs.get("battery_level")
        if battery is None:
            battery_entity = next((entity for entity in related if entity.get("entity_id", "").endswith("_battery")), None)
            battery = battery_entity.get("state") if battery_entity else "unknown"
        status_lines.extend([
            f"Selected: {_web_short_entity_label(selected_entity)}",
            f"State: {selected_entity.get('state', 'unknown')}",
            f"Battery: {battery}",
            f"Current suction speed: {attrs.get('fan_speed', 'unknown')}",
        ])
        for entity in related:
            domain = _ha_domain_from_entity_id(entity.get("entity_id", ""))
            if domain in {"sensor", "binary_sensor"}:
                status_lines.append(f"{_web_short_entity_label(entity)}: {entity.get('state', 'unknown')}")
    settings = []
    for entity in related:
        if not _web_show_vacuum_setting(entity):
            continue
        entity_attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        settings.append({
            "entity_id": entity.get("entity_id", ""),
            "domain": _ha_domain_from_entity_id(entity.get("entity_id", "")),
            "label": _web_short_entity_label(entity),
            "state": entity.get("state", ""),
            "options": [str(option) for option in entity_attrs.get("options", [])] if isinstance(entity_attrs.get("options"), list) else [],
            "min": entity_attrs.get("min", 0),
            "max": entity_attrs.get("max", 100),
            "step": entity_attrs.get("step", 1),
        })
    rooms = dash_app.config.get("vacuum_rooms", {}).get(selected, [])
    return {
        "ok": bool(vacuums),
        "message": "Vacuum controls loaded." if vacuums else "No vacuum entities found in Home Assistant.",
        "vacuums": [{"entity_id": entity.get("entity_id", ""), "label": _web_short_entity_label(entity), "state": entity.get("state", "unknown")} for entity in vacuums],
        "selected": selected,
        "selected_entity": selected_entity,
        "status_lines": status_lines,
        "fan_speeds": [str(speed) for speed in attrs.get("fan_speed_list", [])] if isinstance(attrs.get("fan_speed_list"), list) else [],
        "current_fan_speed": str(attrs.get("fan_speed", "")),
        "settings": settings,
        "rooms": rooms,
    }

@app.route("/remote/tts_engine", methods=["POST"])
def web_set_tts_engine():
    if dash_app:
        new_engine = request.form.get("tts_engine")
        new_edge = request.form.get("edge_tts_voice")
        new_gemini = request.form.get("gemini_tts_voice")
        new_tld = request.form.get("google_tts_tld")
        
        dash_app.config["tts_engine"] = new_engine
        if new_edge: dash_app.config["edge_tts_voice"] = new_edge
        if new_gemini: dash_app.config["gemini_tts_voice"] = new_gemini
        if new_tld: dash_app.config["google_tts_tld"] = new_tld
        
        dash_app.save_config()
        wx.CallAfter(dash_app.tts_engine_choice.SetStringSelection, new_engine)
        wx.CallAfter(dash_app._update_secondary_voice_ui)
        flash(f"Voice Settings Saved. TTS set to {new_engine}")
    return redirect(url_for("remote_ui"))

@app.route("/remote/chimes/test/<door>", methods=["POST"])
def web_test_chime(door):
    if dash_app:
        chime_file = request.form.get(f"{door}_chime")
        safe_submit(audio.test_specific_chime, chime_file, door)
        flash(f"Sent test chime to {door} door.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/chimes/save", methods=["POST"])
def web_save_chimes():
    if dash_app:
        f_val = request.form.get("front_chime")
        b_val = request.form.get("back_chime")
        dash_app.config["front_chime"] = "" if f_val == "(Default)" else f_val
        dash_app.config["back_chime"] = "" if b_val == "(Default)" else b_val
        dash_app.save_config()
        wx.CallAfter(dash_app._populate_chimes)
        flash("Custom chimes saved successfully.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/broadcast", methods=["POST"])
def web_broadcast():
    payload = request.get_json(silent=True) or {}
    msg     = payload.get("broadcast_text") or request.form.get("broadcast_text", "")
    channel = payload.get("channel")        or request.form.get("channel", "manual")
    return _broadcast_message(msg, push=False, channel=channel)

@app.route("/remote/broadcast_push", methods=["POST"])
def web_broadcast_push():
    payload = request.get_json(silent=True) or {}
    msg     = payload.get("broadcast_text") or request.form.get("broadcast_text", "")
    channel = payload.get("channel")        or request.form.get("channel", "manual")
    return _broadcast_message(msg, push=True, channel=channel)

@app.route("/remote/utils/engine", methods=["POST"])
def web_set_engine():
    if dash_app:
        new_engine = request.form.get("engine_name")
        dash_app.config["vision_engine"] = new_engine
        dash_app.save_config()
        wx.CallAfter(dash_app.engine_choice.SetStringSelection, new_engine)
        flash(f"Vision Engine switched to {new_engine}")
    return redirect(url_for("remote_ui"))

@app.route("/remote/toggle", methods=["POST"])
def web_toggle_arm():
    if dash_app:
        dash_app.on_toggle_arm(None)
        status = "Armed" if dash_app.is_armed else "Disarmed"
        flash(f"System {status} successfully.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/speaker/toggle/<name>", methods=["POST"])
def web_speaker_toggle(name):
    if dash_app and name in dash_app.config["speakers"]:
        current = dash_app.config["speakers"][name]["enabled"]
        new_state = not current
        dash_app.config["speakers"][name]["enabled"] = new_state
        dash_app.save_config()
        status_msg = f"{name} {'enabled' if new_state else 'disabled'}"
        wx.CallAfter(dash_app.notify, f"{status_msg} via web", priority=10)
        wx.CallAfter(dash_app.refresh_speaker_list)
        spk_type = dash_app.config["speakers"][name]["type"]
        spk_id = dash_app.config["speakers"][name]["id"]
        safe_submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)
        flash(f"Speaker {status_msg}")
    return redirect(url_for("remote_ui"))

@app.route("/remote/speaker/test/<name>", methods=["POST"])
def web_speaker_test(name):
    if dash_app and name in dash_app.config["speakers"]:
        spk = dash_app.config["speakers"][name]
        status = f"Testing connection to {name}."
        wx.CallAfter(dash_app.notify, status, priority=10)
        safe_submit(audio.announce_specific_speaker, spk["type"], spk["id"], status)
        flash(f"Sent test chime to {name}")
    return redirect(url_for("remote_ui"))


@app.route("/remote/speaker/settings/<name>", methods=["POST"])
def web_speaker_settings(name):
    if dash_app and name in dash_app.config["speakers"]:
        spk = dash_app.config["speakers"][name]
        spk["doorbell"] = "doorbell" in request.form
        spk["utilities"] = "utilities" in request.form
        spk["fridge"] = "fridge" in request.form
        spk["quiet_hours_exempt"] = "quiet_hours_exempt" in request.form
        dash_app.save_config()
        wx.CallAfter(dash_app.refresh_speaker_list)
        wx.CallAfter(dash_app._sync_speaker_routing_controls)
        flash(f"Saved routing for {name}.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/settings/quiet_hours", methods=["POST"])
def web_save_quiet_hours():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    dash_app.config["quiet_hours_enabled"] = "quiet_hours_enabled" in request.form
    dash_app.config["quiet_hours_start"] = request.form.get("quiet_hours_start", "22:00").strip() or "22:00"
    dash_app.config["quiet_hours_end"] = request.form.get("quiet_hours_end", "07:00").strip() or "07:00"
    dash_app.save_config()
    wx.CallAfter(dash_app._sync_quiet_hours_controls)
    flash("Quiet hours settings saved.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/vacuum/action", methods=["POST"])
def web_vacuum_action():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    service = request.form.get("service", "").strip()
    allowed = {
        "vacuum/start",
        "vacuum/pause",
        "vacuum/stop",
        "vacuum/return_to_base",
        "vacuum/locate",
        "vacuum/clean_spot",
    }
    if not entity_id or service not in allowed:
        flash("Vacuum action was missing a vacuum or valid action.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = dash_app._call_ha_service_data(service, {"entity_id": entity_id})
    flash(f"Sent {service.replace('/', '.')} to {entity_id}." if ok else "Vacuum action failed. Check the Viper log.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@app.route("/remote/vacuum/fan_speed", methods=["POST"])
def web_vacuum_fan_speed():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    fan_speed = request.form.get("fan_speed", "").strip()
    if not entity_id or not fan_speed:
        flash("Choose a vacuum and suction speed first.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = dash_app._call_ha_service_data("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": fan_speed})
    flash(f"Set suction speed to {fan_speed}." if ok else "Could not set suction speed.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@app.route("/remote/vacuum/setting", methods=["POST"])
def web_vacuum_setting():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    vacuum_entity = request.form.get("vacuum_entity", "").strip()
    entity_id = request.form.get("entity_id", "").strip()
    domain = request.form.get("domain", "").strip()
    if not entity_id:
        flash("Vacuum setting was missing an entity.")
        return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
    if domain == "select":
        option = request.form.get("option", "").strip()
        ok = dash_app._call_ha_service_data("select/select_option", {"entity_id": entity_id, "option": option})
        flash(f"Set {entity_id} to {option}." if ok else f"Could not set {entity_id}.")
    elif domain == "number":
        raw_value = request.form.get("value", "").strip()
        try:
            value = float(raw_value)
        except ValueError:
            flash("Number setting must be a valid number.")
            return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
        ok = dash_app._call_ha_service_data("number/set_value", {"entity_id": entity_id, "value": value})
        flash(f"Set {entity_id} to {value}." if ok else f"Could not set {entity_id}.")
    elif domain == "switch":
        turn_on = request.form.get("turn_on") == "1"
        service = "switch/turn_on" if turn_on else "switch/turn_off"
        ok = dash_app._call_ha_service_data(service, {"entity_id": entity_id})
        flash(f"Turned {'on' if turn_on else 'off'} {entity_id}." if ok else f"Could not change {entity_id}.")
    else:
        flash("Unsupported vacuum setting type.")
    return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))

@app.route("/remote/vacuum/rooms", methods=["POST"])
def web_vacuum_rooms():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    if not entity_id:
        flash("Choose a vacuum first.")
        return redirect(url_for("remote_ui"))
    result = dash_app._call_ha_service_response("roborock/get_maps", {"entity_id": entity_id})
    if not result.get("ok"):
        flash(result.get("message") or "Could not load Roborock rooms.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    rooms = dash_app._parse_roborock_rooms(result.get("data"), entity_id)
    dash_app._save_vacuum_rooms(entity_id, rooms)
    flash(f"Loaded and saved {len(rooms)} room{'s' if len(rooms) != 1 else ''} for {entity_id}.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@app.route("/remote/vacuum/clean_rooms", methods=["POST"])
def web_vacuum_clean_rooms():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    raw_segments = request.form.getlist("segments")
    try:
        segments = [int(segment) for segment in raw_segments]
        repeat = int(request.form.get("repeat", "1"))
    except ValueError:
        flash("Room clean request had an invalid segment or repeat count.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    repeat = max(1, min(3, repeat))
    if not entity_id or not segments:
        flash("Choose a vacuum and at least one room first.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    payload = {"entity_id": entity_id, "command": "app_segment_clean", "params": [{"segments": segments, "repeat": repeat}]}
    ok = dash_app._call_ha_service_data("vacuum/send_command", payload)
    flash(f"Sent room clean request for {len(segments)} room{'s' if len(segments) != 1 else ''}." if ok else "Could not send room clean request.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@app.route("/remote/vacuum/advanced", methods=["POST"])
def web_vacuum_advanced():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    command = request.form.get("command", "").strip()
    params_text = request.form.get("params", "").strip()
    if not entity_id or not command:
        flash("Choose a vacuum and enter a command first.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    payload = {"entity_id": entity_id, "command": command}
    if params_text:
        try:
            payload["params"] = json.loads(params_text)
        except json.JSONDecodeError as exc:
            flash(f"Advanced command parameters are not valid JSON: {exc}")
            return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = dash_app._call_ha_service_data("vacuum/send_command", payload)
    flash(f"Sent advanced command {command}." if ok else "Could not send advanced command.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@app.route("/remote/vacuum/goto", methods=["POST"])
def web_vacuum_goto():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    try:
        x = int(request.form.get("x", "").strip())
        y = int(request.form.get("y", "").strip())
    except ValueError:
        flash("Roborock coordinates must be whole numbers.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = dash_app._call_ha_service_data("roborock/set_vacuum_goto_position", {"entity_id": entity_id, "x": x, "y": y})
    flash(f"Sent vacuum to coordinates {x}, {y}." if ok else "Could not send go-to-position request.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@app.route("/remote/ice/on", methods=["POST"])
def web_ice_maker_on():
    if dash_app:
        dash_app.on_ice_maker_on(None)
        flash("Ice maker forced on. Auto-off override enabled.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/ice/off", methods=["POST"])
def web_ice_maker_off():
    if dash_app:
        dash_app.on_ice_maker_off(None)
        flash("Ice maker turned off. Auto-off override cleared.")
    return redirect(url_for("remote_ui"))


@app.route("/remote/speaker/add", methods=["POST"])
def web_speaker_add():
    if dash_app:
        name = request.form.get("name")
        spk_type = request.form.get("type")
        spk_id = request.form.get("id")
        if name and spk_id:
            dash_app.config["speakers"][name] = {"id": spk_id, "type": spk_type, "enabled": True, "doorbell": True, "utilities": True, "fridge": True, "quiet_hours_exempt": False}
            dash_app.save_config()
            wx.CallAfter(dash_app.notify, f"Added speaker {name}")
            wx.CallAfter(dash_app.refresh_speaker_list)
            flash(f"Speaker {name} added.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/speaker/rename", methods=["POST"])
def web_speaker_rename():
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if not dash_app:
        return _json_or_redirect("System not ready.", ok=False, status_code=503)
    if not old_name or old_name not in dash_app.config["speakers"]:
        return _json_or_redirect("Original speaker was not found.", ok=False, status_code=404)
    if not new_name:
        return _json_or_redirect("New speaker name cannot be blank.", ok=False, status_code=400)
    if new_name != old_name and new_name in dash_app.config["speakers"]:
        return _json_or_redirect(f"A speaker named {new_name} already exists.", ok=False, status_code=409)

    data = dash_app.config["speakers"].pop(old_name)
    dash_app.config["speakers"][new_name] = data
    dash_app.save_config()
    wx.CallAfter(dash_app.notify, f"Renamed {old_name} to {new_name}")
    wx.CallAfter(dash_app.refresh_speaker_list)
    return _json_or_redirect(f"Renamed {old_name} to {new_name}")

@app.route("/remote/speaker/delete/<name>", methods=["POST"])
def web_speaker_delete(name):
    if dash_app and name in dash_app.config["speakers"]:
        del dash_app.config["speakers"][name]
        dash_app.save_config()
        wx.CallAfter(dash_app.notify, f"Removed speaker {name}")
        wx.CallAfter(dash_app.refresh_speaker_list)
        flash(f"Speaker {name} deleted.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/switch_prompt", methods=["POST"])
def web_switch_prompt():
    if dash_app:
        new_p = request.form.get("profile_name")
        dash_app.config["active_prompt"] = new_p
        wx.CallAfter(dash_app.prompt_choice.SetStringSelection, new_p)
        wx.CallAfter(dash_app.prompt_editor.SetValue, dash_app.config["prompts"][new_p])
        dash_app.save_config()
        flash(f"Switched to {new_p} profile.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/save_prompt", methods=["POST"])
def web_save_prompt():
    if dash_app:
        new_text = request.form.get("prompt_text")
        active_p = dash_app.config["active_prompt"]
        dash_app.config["prompts"][active_p] = new_text
        wx.CallAfter(dash_app.prompt_editor.SetValue, new_text)
        dash_app.save_config()
        flash("AI instructions saved.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/utils/api", methods=["POST"])
def web_api_check():
    if dash_app:
        dash_app.on_api(None)
        flash("API Check requested. Listen for announcement.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/utils/batt", methods=["POST"])
def web_batt_check():
    if dash_app:
        dash_app.on_batt(None)
        flash("Battery Check requested. Listen for announcement.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/utils/filter", methods=["POST"])
def web_filter_check():
    if dash_app:
        dash_app.on_filter(None)
        flash("Filter Check requested. Listen for announcement.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/utils/scan_sonos", methods=["POST"])
def web_scan_sonos():
    if dash_app:
        dash_app.on_scan_sonos(None)
        flash("Sonos scan started. Listen for results and check your phone.")
    return redirect(url_for("remote_ui"))

@app.route("/remote/utils/scan_ha", methods=["POST"])
def web_scan_ha():
    if dash_app:
        dash_app.on_scan_ha(None)
        flash("Home Assistant scan started. Check your PC screen.")
    return redirect(url_for("remote_ui"))



@app.route("/remote/cinderella/save/<bucket>", methods=["POST"])
def web_save_cinderella_bucket(bucket):
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(dash_app.config)
    raw_text = request.form.get("messages", "")
    values = [line.strip() for line in raw_text.splitlines() if line.strip()]
    valid_buckets = {"departure", "washing", "emptying", "drying", "returning", "victory", "paused", "status_update", "vacuum_error_templates", "dock_error_templates"}
    if bucket not in valid_buckets:
        flash("Unknown Cinderella message bucket.")
        return redirect(url_for("remote_ui"))
    messages[bucket] = values
    dash_app.save_config()
    flash(f"Saved Cinderella messages for {bucket.replace('_', ' ')}.")
    return redirect(url_for("remote_ui"))


@app.route("/remote/cinderella/error/add", methods=["POST"])
def web_add_cinderella_error_bucket():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(dash_app.config)
    error_name = (request.form.get("error_name", "") or "").strip().lower().replace(" ", "_")
    if not error_name:
        flash("Error bucket name cannot be blank.")
        return redirect(url_for("remote_ui"))
    specific = messages.setdefault("specific_errors", {})
    if error_name not in specific:
        specific[error_name] = [f"Cinderella has a very specific complaint: {error_name.replace('_', ' ')}."]
        dash_app.save_config()
        flash(f"Added Cinderella error bucket: {error_name}.")
    else:
        flash(f"Cinderella error bucket already exists: {error_name}.")
    return redirect(url_for("remote_ui"))


@app.route("/remote/cinderella/error/save/<error_name>", methods=["POST"])
def web_save_cinderella_error_bucket(error_name):
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(dash_app.config)
    specific = messages.setdefault("specific_errors", {})
    raw_text = request.form.get("messages", "")
    values = [line.strip() for line in raw_text.splitlines() if line.strip()]
    specific[error_name] = values
    dash_app.save_config()
    flash(f"Saved Cinderella messages for specific error {error_name}.")
    return redirect(url_for("remote_ui"))


@app.route("/remote/cinderella/error/delete/<error_name>", methods=["POST"])
def web_delete_cinderella_error_bucket(error_name):
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(dash_app.config)
    specific = messages.setdefault("specific_errors", {})
    if error_name in specific:
        del specific[error_name]
        dash_app.save_config()
        flash(f"Deleted Cinderella error bucket {error_name}.")
    else:
        flash(f"Cinderella error bucket {error_name} was not found.")
    return redirect(url_for("remote_ui"))


@app.route("/cinderella", methods=["POST"])
def cinderella_message_endpoint():
    if dash_app is None or is_shutting_down.is_set():
        return jsonify({"ok": False, "error": "System not ready or shutting down."}), 503

    request_started = time.time()
    payload = request.get_json(silent=True) or {}
    if not payload and request.form:
        payload = request.form.to_dict(flat=True)

    event = (payload.get("event") or "").strip().lower()
    error = (payload.get("error") or "").strip().lower()
    source = (payload.get("source") or "vacuum").strip().lower()

    if not event:
        return jsonify({"ok": False, "error": "Missing event."}), 400

    logging.info(
        "[CINDERELLA EVENT] received event=%s source=%s error=%s",
        event,
        source,
        error or "none",
    )

    message = choose_cinderella_message(event, error=error, source=source)
    if not message:
        logging.warning(
            "[CINDERELLA EVENT] no message configured for event=%s source=%s error=%s",
            event,
            source,
            error or "none",
        )
        return jsonify({"ok": False, "error": f"No message configured for event {event}."}), 404

    context = {
        "channel": "cinderella",
        "event": event,
        "source": source,
        "error": error,
        "received_ts": request_started,
        "received_iso": datetime.now().isoformat(timespec="seconds"),
    }
    logging.info(
        "[CINDERELLA EVENT] selected message for event=%s source=%s: %s",
        event,
        source,
        message,
    )

    future = safe_submit(audio.announce_all, message, False, context)
    if future is None:
        logging.warning(
            "[CINDERELLA EVENT] announcement rejected during shutdown for event=%s source=%s",
            event,
            source,
        )
        return jsonify({"ok": False, "error": "Announcement rejected during shutdown."}), 503

    logging.info(
        "[CINDERELLA EVENT] queued event=%s source=%s total_request_time=%.3fs",
        event,
        source,
        time.time() - request_started,
    )
    return jsonify({"ok": True, "message": message})


@app.route("/remote/settings/broadcast_channels", methods=["POST"])
def web_save_broadcast_channels():
    if not dash_app:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    channels = dash_app.config.setdefault("broadcast_channels", {})
    for key in request.form:
        if key.startswith("channel_") and key.endswith("_mode"):
            ch_name = key[len("channel_"):-len("_mode")]
            chime_val = request.form.get(f"channel_{ch_name}_chime", "")
            channels[ch_name] = {
                "mode":  request.form.get(f"channel_{ch_name}_mode", "speak"),
                "chime": "" if chime_val == "(Default)" else chime_val,
            }
    dash_app.config["broadcast_channels"] = channels
    dash_app.save_config()
    wx.CallAfter(dash_app._sync_fridge_controls)
    flash("Fridge & Freezer channel settings saved.")
    return redirect(url_for("remote_ui"))



@app.route("/remote/fridge/test/<channel>", methods=["POST"])
def web_test_fridge_chime(channel):
    """Play the current or saved chime for a fridge/freezer channel on all speakers."""
    if dash_app:
        posted_chime = request.form.get(f"channel_{channel}_chime", "")
        chime = "" if posted_chime in ("", "(Default)") else posted_chime

        if not chime:
            channels = dash_app.config.get("broadcast_channels", {})
            ch_data  = channels.get(channel, {})
            chime    = ch_data.get("chime", "")

        label = channel.replace("_", " ").title()
        safe_submit(audio.play_broadcast_chime, chime, channel)
        flash(f"Testing chime for: {label}")
    return redirect(url_for("remote_ui"))


def run_flask_server():
    serve(app, host="0.0.0.0", port=cfg.FLASK_PORT, threads=8)

class ViperTaskBarIcon(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
        self.SetIcon(icon, "Viper Vision")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_restore)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_restore)

    def on_restore(self, event):
        self.frame.Show(True)
        if self.frame.IsIconized():
            self.frame.Iconize(False)
        self.frame.Raise()


class HomeAssistantFirstRunAssistantDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Home Assistant Setup Assistant", size=(780, 720))
        self.parent = parent
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=(
                "This assistant helps brand-new users get from nothing to a working Viper setup. "
                "It does not bundle or silently install VirtualBox or Home Assistant. It opens official pages, checks this PC, finds Home Assistant, and then continues to Viper setup."
            ),
        )
        intro.Wrap(700)
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 12)

        self.status_txt = wx.TextCtrl(
            panel,
            value=self._initial_status(),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 300),
        )
        self._describe_control(
            self.status_txt,
            "Setup assistant status. This read only box explains what is detected and what to do next.",
        )
        sizer.Add(self.status_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        grid = wx.GridSizer(rows=0, cols=2, vgap=8, hgap=8)
        buttons = [
            ("Check This PC", self.on_check_pc, "Checks whether VirtualBox is installed and whether Home Assistant is reachable."),
            ("Find Home Assistant", self.on_find_ha, "Searches common Home Assistant addresses on your network."),
            ("Open HA Windows Guide", lambda _e: open_official_link("ha_windows"), "Opens the official Home Assistant Windows installation guide."),
            ("Open VirtualBox Download", lambda _e: open_official_link("virtualbox"), "Opens the official VirtualBox download page."),
            ("Open HA OS Download", lambda _e: open_official_link("ha_os_releases"), "Opens the official Home Assistant OS release downloads page."),
            ("Open Token Help", lambda _e: open_official_link("ha_tokens"), "Opens Home Assistant developer documentation for long lived access tokens."),
            ("Open Viper Help", lambda _e: open_help("ha-install"), "Opens Viper's local Home Assistant installation help page."),
            ("Continue To Viper Setup", self.on_continue, "Opens Viper's Home Assistant setup dialog."),
        ]
        for label, handler, help_text in buttons:
            btn = wx.Button(panel, label=label, size=(-1, 44))
            btn.Bind(wx.EVT_BUTTON, handler)
            self._describe_control(btn, help_text)
            grid.Add(btn, 0, wx.EXPAND)
        sizer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        close = wx.Button(panel, label="Close", size=(-1, 44))
        close.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_CANCEL))
        self._describe_control(close, "Close setup assistant button.")
        sizer.Add(close, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        wx.CallAfter(self.on_check_pc, None)

    def _describe_control(self, control, description):
        control.SetName(description)
        control.SetToolTip(description)

    def _initial_status(self):
        return "\n".join(
            [
                "Home Assistant Setup Assistant",
                "",
                "Recommended path:",
                "1. If you already have Home Assistant, press Find Home Assistant.",
                "2. If you do not have Home Assistant, open the HA Windows guide and VirtualBox download.",
                "3. Finish Home Assistant onboarding in your browser.",
                "4. Create a long-lived access token.",
                "5. Return here and continue to Viper setup.",
                "",
                "The easiest hardware path for many beginners is Home Assistant Green or a dedicated mini PC. VirtualBox is useful for trying Home Assistant on this Windows computer.",
            ]
        )

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("ha-install")
            return
        event.Skip()

    def on_check_pc(self, event):
        self.status_txt.SetValue("Checking this PC and looking for Home Assistant...")
        safe_submit(self._run_check_pc)

    def _run_check_pc(self):
        vbox = get_virtualbox_status()
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        found = discovery.find_home_assistant(
            token=ha_settings.get("ha_token") or None,
            seed_host=ha_settings.get("ha_ip") or "",
            seed_port=ha_settings.get("ha_port") or "8123",
            timeout=2,
        )
        wx.CallAfter(self._finish_check_pc, vbox, found)

    def _finish_check_pc(self, vbox, found):
        lines = ["Home Assistant Setup Assistant", ""]
        if vbox.get("installed"):
            lines.append(f"VirtualBox: found. {vbox.get('version') or vbox.get('path')}")
        else:
            lines.append("VirtualBox: not found. If you want Home Assistant OS on this Windows PC, install VirtualBox from the official download page.")

        if found.get("ok"):
            lines.append(f"Home Assistant: found at {found.get('ha_ip')}:{found.get('ha_port')}.")
            if found.get("auth_ok"):
                lines.append("Token: accepted by Home Assistant.")
            else:
                lines.append("Token: not tested or not accepted yet. You will create/paste one during Viper setup.")
            lines.append("")
            lines.append("Next step: press Continue To Viper Setup.")
        else:
            lines.append("Home Assistant: not found automatically.")
            lines.append("")
            lines.append("If Home Assistant is already installed, make sure it is powered on and reachable at http://homeassistant.local:8123.")
            lines.append("If Home Assistant is not installed, use the official HA Windows guide and VirtualBox download buttons.")
        lines.append("")
        lines.append("Viper will not silently install VirtualBox or Home Assistant. This avoids admin, licensing, antivirus, and machine-specific VM problems.")
        self.status_txt.SetValue("\n".join(lines))

    def on_find_ha(self, event):
        self.status_txt.SetValue("Looking for Home Assistant...")
        safe_submit(self._run_find_ha)

    def _run_find_ha(self):
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        result = discovery.find_home_assistant(
            token=ha_settings.get("ha_token") or None,
            seed_host=ha_settings.get("ha_ip") or "",
            seed_port=ha_settings.get("ha_port") or "8123",
            timeout=2,
        )
        wx.CallAfter(self._finish_find_ha, result)

    def _finish_find_ha(self, result):
        if result.get("ok"):
            self.parent.config["ha_ip"] = result.get("ha_ip", "")
            self.parent.config["ha_port"] = result.get("ha_port", "8123")
            self.parent.save_config()
            self.status_txt.SetValue(
                f"Found Home Assistant at {result.get('ha_ip')}:{result.get('ha_port')}.\n\n"
                "Next step: press Continue To Viper Setup and paste your long-lived access token."
            )
        else:
            self.status_txt.SetValue(
                "Home Assistant was not found automatically.\n\n"
                "Try opening http://homeassistant.local:8123 in your browser. If that works, continue to Viper setup and enter homeassistant.local manually."
            )

    def on_continue(self, event):
        self.EndModal(wx.ID_OK)
        wx.CallAfter(self.parent.show_home_assistant_setup)


class HomeAssistantSetupDialog(wx.Dialog):
    def __init__(self, parent, *, use_env_prefill=True):
        super().__init__(parent, title="Viper Vision Setup", size=(820, 860))
        self.parent = parent
        self.discovery_result = None
        self.ring_listen_cancel = None
        self._doorbell_preview_updating = False
        self._last_derived_values = {}
        self._front_trigger_initial = ""
        self._back_trigger_initial = ""

        settings = cfg.get_ha_settings(parent.config, include_env=use_env_prefill)
        api_settings = cfg.get_api_settings(parent.config, include_env=use_env_prefill)
        doorbell_settings = cfg.get_doorbell_settings(parent.config, include_env=use_env_prefill)
        triggers = parent.config.get("doorbell_triggers", {})
        front_trigger = triggers.get("front", {}) if isinstance(triggers, dict) else {}
        back_trigger = triggers.get("back", {}) if isinstance(triggers, dict) else {}
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label="Connect Viper Vision to Home Assistant. Viper can listen to Home Assistant directly, so new users do not need to edit YAML automations. Doorbell vision uses live RTSP URLs.",
        )
        intro.Wrap(560)
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 12)

        grid = wx.FlexGridSizer(rows=23, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Home Assistant IP / host"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.ha_ip_txt = wx.TextCtrl(panel, value=settings.get("ha_ip") or "")
        grid.Add(self.ha_ip_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Port"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.ha_port_txt = wx.TextCtrl(panel, value=settings.get("ha_port") or "8123")
        grid.Add(self.ha_port_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Long-lived access token"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.ha_token_txt = wx.TextCtrl(panel, value=settings.get("ha_token") or "", style=wx.TE_PASSWORD)
        self._describe_control(self.ha_token_txt, "Home Assistant long lived access token. Create this in your Home Assistant user profile.")
        grid.Add(self.ha_token_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Gemini API key"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.gemini_key_txt = wx.TextCtrl(panel, value=api_settings.get("gemini_api_key") or "", style=wx.TE_PASSWORD)
        self._describe_control(self.gemini_key_txt, "Gemini API key for live doorbell image analysis and Gemini speech.")
        grid.Add(self.gemini_key_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Viper listens to Home Assistant events"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.ha_listener_chk = wx.CheckBox(panel, label="Enable direct Home Assistant listener")
        self.ha_listener_chk.SetValue(bool(parent.config.get("ha_listener_enabled", True)))
        self._describe_control(
            self.ha_listener_chk,
            "Direct Home Assistant listener checkbox. Keep this checked for the beginner setup. It lets Viper react to Home Assistant state changes without YAML automations.",
        )
        grid.Add(self.ha_listener_chk, 0, wx.ALIGN_CENTER_VERTICAL)

        grid.Add(wx.StaticText(panel, label="Use Pushover notifications"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pushover_enabled_chk = wx.CheckBox(panel)
        self.pushover_enabled_chk.SetValue(bool(api_settings.get("pushover_enabled")))
        self.pushover_enabled_chk.Bind(wx.EVT_CHECKBOX, self.on_pushover_toggle)
        grid.Add(self.pushover_enabled_chk, 0, wx.ALIGN_CENTER_VERTICAL)

        grid.Add(wx.StaticText(panel, label="Pushover user key"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pushover_user_txt = wx.TextCtrl(panel, value=api_settings.get("pushover_user_key") or "", style=wx.TE_PASSWORD)
        grid.Add(self.pushover_user_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Pushover app token"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pushover_token_txt = wx.TextCtrl(panel, value=api_settings.get("pushover_api_token") or "", style=wx.TE_PASSWORD)
        grid.Add(self.pushover_token_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Front Ring camera ID"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.front_camera_id_txt = wx.TextCtrl(panel, value=doorbell_settings.get("front_camera_id") or "")
        grid.Add(self.front_camera_id_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Back Ring camera ID"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.back_camera_id_txt = wx.TextCtrl(panel, value=doorbell_settings.get("back_camera_id") or "")
        grid.Add(self.back_camera_id_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Ring topic root / location ID"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.ring_topic_root_txt = wx.TextCtrl(panel, value=doorbell_settings.get("ring_topic_root") or "")
        grid.Add(self.ring_topic_root_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Front door RTSP URL"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rtsp_front_txt = wx.TextCtrl(panel, value=doorbell_settings.get("rtsp_front") or "")
        self._describe_control(self.rtsp_front_txt, "Front door live RTSP URL. This must be current video, not a Home Assistant snapshot.")
        grid.Add(self.rtsp_front_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Back door RTSP URL"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rtsp_back_txt = wx.TextCtrl(panel, value=doorbell_settings.get("rtsp_back") or "")
        self._describe_control(self.rtsp_back_txt, "Back door live RTSP URL. This must be current video, not a Home Assistant snapshot.")
        grid.Add(self.rtsp_back_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Front door HA trigger entity"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.front_trigger_choice = wx.Choice(panel, choices=[])
        self._front_trigger_initial = front_trigger.get("trigger_entity_id") or ""
        self._describe_control(
            self.front_trigger_choice,
            "Front door Home Assistant trigger entity. Choose the binary sensor or sensor that changes when the front doorbell or motion event fires.",
        )
        grid.Add(self.front_trigger_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Back door HA trigger entity"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.back_trigger_choice = wx.Choice(panel, choices=[])
        self._back_trigger_initial = back_trigger.get("trigger_entity_id") or ""
        self._describe_control(
            self.back_trigger_choice,
            "Back door Home Assistant trigger entity. Choose the binary sensor or sensor that changes when the back doorbell or motion event fires.",
        )
        grid.Add(self.back_trigger_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Front Ring MQTT topic"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.front_mqtt_txt = wx.TextCtrl(panel, value=doorbell_settings.get("front_doorbell_mqtt_topic") or "")
        grid.Add(self.front_mqtt_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Back Ring MQTT topic"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.back_mqtt_txt = wx.TextCtrl(panel, value=doorbell_settings.get("back_doorbell_mqtt_topic") or "")
        grid.Add(self.back_mqtt_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="MQTT host"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mqtt_host_txt = wx.TextCtrl(panel, value=doorbell_settings.get("mqtt_host") or settings.get("ha_ip") or "")
        grid.Add(self.mqtt_host_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="MQTT port"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mqtt_port_txt = wx.TextCtrl(panel, value=doorbell_settings.get("mqtt_port") or "1883")
        grid.Add(self.mqtt_port_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="MQTT username"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mqtt_user_txt = wx.TextCtrl(panel, value=doorbell_settings.get("mqtt_username") or "")
        grid.Add(self.mqtt_user_txt, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="MQTT password"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mqtt_password_txt = wx.TextCtrl(panel, value=doorbell_settings.get("mqtt_password") or "", style=wx.TE_PASSWORD)
        grid.Add(self.mqtt_password_txt, 1, wx.EXPAND)

        sizer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        self.status_txt = wx.TextCtrl(panel, value="Ready to test Home Assistant.", style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(self.status_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_find_ha = wx.Button(panel, label="Find HA")
        self.btn_test = wx.Button(panel, label="Test & Discover")
        self.btn_test_front_rtsp = wx.Button(panel, label="Test Front RTSP")
        self.btn_test_back_rtsp = wx.Button(panel, label="Test Back RTSP")
        self.btn_mqtt = wx.Button(panel, label="Test MQTT")
        self.btn_ring = wx.Button(panel, label="Find Ring Topics")
        self.btn_ring_help = wx.Button(panel, label="Ring Setup Assistant")
        self.btn_help = wx.Button(panel, label="Help")
        self.btn_save = wx.Button(panel, label="Save")
        self.btn_close = wx.Button(panel, label="Close")
        self.btn_find_ha.Bind(wx.EVT_BUTTON, self.on_find_ha)
        self.btn_test.Bind(wx.EVT_BUTTON, self.on_test)
        self.btn_test_front_rtsp.Bind(wx.EVT_BUTTON, lambda event: self.on_test_rtsp(event, "front"))
        self.btn_test_back_rtsp.Bind(wx.EVT_BUTTON, lambda event: self.on_test_rtsp(event, "back"))
        self.btn_mqtt.Bind(wx.EVT_BUTTON, self.on_test_mqtt)
        self.btn_ring.Bind(wx.EVT_BUTTON, self.on_find_ring_topics)
        self.btn_ring_help.Bind(wx.EVT_BUTTON, self.on_ring_setup_assistant)
        self.btn_help.Bind(wx.EVT_BUTTON, lambda _event: open_help("index"))
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CANCEL))
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        btn_sizer.Add(self.btn_find_ha, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_test, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_test_front_rtsp, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_test_back_rtsp, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_mqtt, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_ring, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_ring_help, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_help, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_save, 1, wx.ALL | wx.EXPAND, 5)
        btn_sizer.Add(self.btn_close, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(btn_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 7)

        panel.SetSizer(sizer)
        self.on_pushover_toggle(None)
        for ctrl in (self.ha_ip_txt, self.front_camera_id_txt, self.back_camera_id_txt, self.ring_topic_root_txt):
            ctrl.Bind(wx.EVT_TEXT, self.on_doorbell_derivation_change)
        self._refresh_derived_doorbell_preview()
        self._populate_trigger_choices_from_config(front_trigger.get("trigger_entity_id", ""), back_trigger.get("trigger_entity_id", ""))

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("ring-setup")
            return
        event.Skip()

    def _describe_control(self, control, description):
        control.SetName(description)
        control.SetToolTip(description)

    def _settings(self):
        front_trigger_entity = self._choice_entity_id(self.front_trigger_choice)
        back_trigger_entity = self._choice_entity_id(self.back_trigger_choice)
        return {
            "ha_ip": self.ha_ip_txt.GetValue().strip(),
            "ha_port": self.ha_port_txt.GetValue().strip() or "8123",
            "ha_token": self.ha_token_txt.GetValue().strip(),
            "gemini_api_key": self.gemini_key_txt.GetValue().strip(),
            "ha_listener_enabled": self.ha_listener_chk.GetValue(),
            "pushover_enabled": self.pushover_enabled_chk.GetValue(),
            "pushover_user_key": self.pushover_user_txt.GetValue().strip(),
            "pushover_api_token": self.pushover_token_txt.GetValue().strip(),
            "front_camera_id": self.front_camera_id_txt.GetValue().strip(),
            "back_camera_id": self.back_camera_id_txt.GetValue().strip(),
            "ring_topic_root": self.ring_topic_root_txt.GetValue().strip().strip("/"),
            "rtsp_front": self.rtsp_front_txt.GetValue().strip(),
            "rtsp_back": self.rtsp_back_txt.GetValue().strip(),
            "front_doorbell_mqtt_topic": self.front_mqtt_txt.GetValue().strip(),
            "back_doorbell_mqtt_topic": self.back_mqtt_txt.GetValue().strip(),
            "mqtt_host": self.mqtt_host_txt.GetValue().strip(),
            "mqtt_port": self.mqtt_port_txt.GetValue().strip() or "1883",
            "mqtt_username": self.mqtt_user_txt.GetValue().strip(),
            "mqtt_password": self.mqtt_password_txt.GetValue().strip(),
            "front_trigger_entity_id": front_trigger_entity,
            "back_trigger_entity_id": back_trigger_entity,
        }

    def _entity_choice_label(self, entity):
        entity_id = entity.get("entity_id", "")
        name = entity.get("friendly_name") or entity_id
        state = entity.get("state", "unknown")
        return f"{name} ({entity_id}, state {state})"

    def _choice_entity_id(self, choice):
        idx = choice.GetSelection()
        if idx == wx.NOT_FOUND:
            if choice is self.front_trigger_choice:
                return self._front_trigger_initial
            if choice is self.back_trigger_choice:
                return self._back_trigger_initial
            return ""
        return choice.GetClientData(idx) or ""

    def _populate_trigger_choices_from_config(self, front_entity="", back_entity=""):
        choices = []
        if self.discovery_result and self.discovery_result.get("ok"):
            categories = self.discovery_result.get("categories", {})
            seen = set()
            candidates = []
            for category in ("door_sensors", "ring_cameras", "cameras"):
                for entity in categories.get(category, []):
                    entity_id = entity.get("entity_id")
                    if entity_id and entity_id not in seen:
                        seen.add(entity_id)
                        candidates.append(entity)
            for entity in self.discovery_result.get("all_entities", []):
                entity_id = entity.get("entity_id", "")
                text = " ".join(
                    str(part).lower()
                    for part in [
                        entity_id,
                        entity.get("friendly_name"),
                        entity.get("domain"),
                        entity.get("device_class"),
                        entity.get("platform"),
                    ]
                )
                if entity.get("domain") in {"binary_sensor", "sensor"} and any(
                    token in text for token in ["ring", "doorbell", "motion", "ding", "front door", "back door"]
                ):
                    if entity_id and entity_id not in seen:
                        seen.add(entity_id)
                        candidates.append(entity)
            for entity in candidates:
                choices.append((self._entity_choice_label(entity), entity.get("entity_id")))
        for entity_id in [front_entity, back_entity]:
            if entity_id and entity_id not in [item[1] for item in choices]:
                choices.append((entity_id, entity_id))
        labels = [item[0] for item in choices]
        for choice, current in [(self.front_trigger_choice, front_entity), (self.back_trigger_choice, back_entity)]:
            choice.Set(labels)
            for idx, (_label, entity_id) in enumerate(choices):
                choice.SetClientData(idx, entity_id)
            if current:
                match = next((idx for idx, item in enumerate(choices) if item[1] == current), wx.NOT_FOUND)
                if match != wx.NOT_FOUND:
                    choice.SetSelection(match)
                    continue
            if labels:
                choice.SetSelection(0)

    def on_doorbell_derivation_change(self, event):
        if not self._doorbell_preview_updating:
            self._refresh_derived_doorbell_preview()
        if event:
            event.Skip()

    def _derived_doorbell_values(self):
        settings = self._settings()
        ha_ip = settings["ha_ip"]
        front_camera_id = settings["front_camera_id"]
        back_camera_id = settings["back_camera_id"]
        ring_root = settings["ring_topic_root"]
        return {
            "rtsp_front": f"rtsp://{ha_ip}:8554/{front_camera_id}_live" if ha_ip and front_camera_id else "",
            "rtsp_back": f"rtsp://{ha_ip}:8554/{back_camera_id}_live" if ha_ip and back_camera_id else "",
            "front_doorbell_mqtt_topic": f"ring/{ring_root}/camera/{front_camera_id}/motion/state" if ring_root and front_camera_id else "",
            "back_doorbell_mqtt_topic": f"ring/{ring_root}/camera/{back_camera_id}/motion/state" if ring_root and back_camera_id else "",
        }

    def _set_text_if_blank_or_previous_preview(self, ctrl, key, derived):
        current = ctrl.GetValue().strip()
        previous = self._last_derived_values.get(key, "")
        if current == "" or current == previous:
            ctrl.SetValue(derived)

    def _refresh_derived_doorbell_preview(self):
        derived = self._derived_doorbell_values()
        self._doorbell_preview_updating = True
        try:
            self._set_text_if_blank_or_previous_preview(self.rtsp_front_txt, "rtsp_front", derived["rtsp_front"])
            self._set_text_if_blank_or_previous_preview(self.rtsp_back_txt, "rtsp_back", derived["rtsp_back"])
            self._set_text_if_blank_or_previous_preview(self.front_mqtt_txt, "front_doorbell_mqtt_topic", derived["front_doorbell_mqtt_topic"])
            self._set_text_if_blank_or_previous_preview(self.back_mqtt_txt, "back_doorbell_mqtt_topic", derived["back_doorbell_mqtt_topic"])
        finally:
            self._last_derived_values = derived
            self._doorbell_preview_updating = False

    def on_pushover_toggle(self, event):
        enabled = self.pushover_enabled_chk.GetValue()
        self.pushover_user_txt.Enable(enabled)
        self.pushover_token_txt.Enable(enabled)

    def _set_busy(self, busy):
        self.btn_find_ha.Enable(not busy)
        self.btn_test.Enable(not busy)
        self.btn_test_front_rtsp.Enable(not busy)
        self.btn_test_back_rtsp.Enable(not busy)
        self.btn_mqtt.Enable(not busy)
        self.btn_ring.Enable(not busy)
        self.btn_ring_help.Enable(not busy)
        self.btn_help.Enable(not busy)
        self.btn_save.Enable(not busy)

    def on_find_ha(self, event):
        settings = self._settings()
        self._set_busy(True)
        self.status_txt.SetValue("Looking for Home Assistant...")
        safe_submit(self._run_find_ha, settings)

    def _run_find_ha(self, settings):
        result = discovery.find_home_assistant(
            token=settings.get("ha_token") or None,
            seed_host=settings.get("ha_ip") or "",
            seed_port=settings.get("ha_port") or "8123",
            timeout=2,
        )
        wx.CallAfter(self._finish_find_ha, result)

    def _finish_find_ha(self, result):
        self._set_busy(False)
        if result.get("ok"):
            self.ha_ip_txt.SetValue(result.get("ha_ip", ""))
            self.ha_port_txt.SetValue(result.get("ha_port", "8123"))
            auth_note = "Token accepted." if result.get("auth_ok") else "Host found. Token still needs to be tested."
            self.status_txt.SetValue(f"Found Home Assistant at {result.get('ha_ip')}:{result.get('ha_port')}. {auth_note}")
            self._refresh_derived_doorbell_preview()
            return
        attempts = result.get("attempts", [])
        self.status_txt.SetValue(
            "Home Assistant was not found automatically. Enter the host manually, usually homeassistant.local or the HA IP address.\n"
            f"Attempts made: {len(attempts)}"
        )

    def on_test_rtsp(self, event, side):
        settings = self._settings()
        rtsp_url = settings["rtsp_back"] if side == "back" else settings["rtsp_front"]
        if not rtsp_url:
            self.status_txt.SetValue(f"Enter the {side} door RTSP URL before testing it.")
            return
        self._set_busy(True)
        self.status_txt.SetValue(f"Testing {side} door RTSP. This checks for a live video frame.")
        safe_submit(self._run_test_rtsp, side, rtsp_url)

    def _run_test_rtsp(self, side, rtsp_url):
        try:
            test_dir = cfg.DATA_DIR / "rtsp_test"
            test_dir.mkdir(parents=True, exist_ok=True)
            min_bytes = cfg.BACK_MIN_FRAME_BYTES if side == "back" else cfg.FRONT_MIN_FRAME_BYTES
            frame = vision.grab_frame(rtsp_url, test_dir, f"setup_{side}", min_bytes=min_bytes, timeout=8)
            result = {"ok": bool(frame), "frame": frame}
        except Exception as e:
            result = {"ok": False, "message": str(e)}
        wx.CallAfter(self._finish_test_rtsp, side, result)

    def _finish_test_rtsp(self, side, result):
        self._set_busy(False)
        if result.get("ok"):
            self.status_txt.SetValue(f"{side.title()} door RTSP test passed. Viper captured a live frame from the stream.")
        else:
            self.status_txt.SetValue(
                f"{side.title()} door RTSP test failed. Check go2rtc, Ring camera ID, and the RTSP URL. "
                f"{result.get('message') or ''}"
            )

    def on_ring_setup_assistant(self, event):
        settings = self._settings()
        lines = [
            "Ring Setup Assistant",
            "",
            "Viper itself does not require Mosquitto or ring-mqtt. Viper needs Home Assistant access, a trigger entity, and a live RTSP URL.",
            "",
        ]
        if not self.discovery_result:
            lines.append("Next step: press Test & Discover so Viper can look for Ring, doorbell, motion, camera, and speaker entities.")
            self.status_txt.SetValue("\n".join(lines))
            open_help("ring-setup")
            return

        categories = self.discovery_result.get("categories", {}) if self.discovery_result.get("ok") else {}
        ring_cameras = len(categories.get("ring_cameras", []))
        cameras = len(categories.get("cameras", []))
        door_sensors = len(categories.get("door_sensors", []))
        front_trigger = settings.get("front_trigger_entity_id", "")
        back_trigger = settings.get("back_trigger_entity_id", "")
        front_rtsp = settings.get("rtsp_front", "")
        back_rtsp = settings.get("rtsp_back", "")
        mqtt_topics = [settings.get("front_doorbell_mqtt_topic", ""), settings.get("back_doorbell_mqtt_topic", "")]

        lines.extend([
            f"Ring cameras found in Home Assistant: {ring_cameras}",
            f"Total camera entities found: {cameras}",
            f"Door or motion-style sensors found: {door_sensors}",
            f"Front trigger selected: {front_trigger or 'no'}",
            f"Back trigger selected: {back_trigger or 'no'}",
            f"Front RTSP URL entered: {'yes' if front_rtsp else 'no'}",
            f"Back RTSP URL entered: {'yes' if back_rtsp else 'no'}",
            f"Ring MQTT topics entered: {'yes' if any(mqtt_topics) else 'no'}",
            "",
        ])

        if (front_trigger or back_trigger) and (front_rtsp or back_rtsp):
            lines.append("Likely status: Viper can use the beginner setup. Test each RTSP URL, save, then trigger the doorbell.")
        elif ring_cameras or cameras or door_sensors:
            lines.append("Likely status: Home Assistant has some useful entities, but the doorbell setup is incomplete.")
            if not (front_trigger or back_trigger):
                lines.append("Choose the Home Assistant entity that changes when Ring motion or a doorbell press happens.")
            if not (front_rtsp or back_rtsp):
                lines.append("Add a live RTSP URL. Use go2rtc or ring-mqtt video streaming if Home Assistant only has stale snapshots.")
        else:
            lines.append("Likely status: Home Assistant does not expose Ring trigger entities yet.")
            lines.append("Install the Ring integration if it gives you motion/ding entities. If not, install Mosquitto Broker and ring-mqtt.")

        lines.extend([
            "",
            "If you need ring-mqtt: install Mosquitto Broker, create an MQTT user, add the ring-mqtt repository, sign in to Ring, enable video streaming, then return here and press Test & Discover.",
            "The full step-by-step guide has been opened.",
        ])
        self.status_txt.SetValue("\n".join(lines))
        open_help("ring-setup")

    def on_test(self, event):
        settings = self._settings()
        if not settings["ha_ip"] or not settings["ha_token"]:
            self.status_txt.SetValue("Enter the Home Assistant host and access token first.")
            return
        if not settings["gemini_api_key"]:
            self.status_txt.SetValue("Enter the Gemini API key before continuing. Pushover can be left off.")
            return
        if settings["pushover_enabled"] and (not settings["pushover_user_key"] or not settings["pushover_api_token"]):
            self.status_txt.SetValue("Pushover is optional. Either enter both Pushover values or turn it off.")
            return

        self._set_busy(True)
        self.status_txt.SetValue("Testing connection and discovering entities...")
        safe_submit(self._run_discovery_test, settings)

    def _run_discovery_test(self, settings):
        result = discovery.discover_ha_entities(
            ha_ip=settings["ha_ip"],
            ha_port=settings["ha_port"],
            token=settings["ha_token"],
            timeout=8,
        )
        wx.CallAfter(self._finish_discovery_test, result)

    def _finish_discovery_test(self, result):
        self._set_busy(False)
        self.discovery_result = result if result.get("ok") else None
        if not result.get("ok"):
            self.status_txt.SetValue(result.get("message") or "Home Assistant discovery failed.")
            return
        self._populate_trigger_choices_from_config(
            self._choice_entity_id(self.front_trigger_choice),
            self._choice_entity_id(self.back_trigger_choice),
        )

        counts = result.get("counts", {})
        lines = [
            f"Connected. Found {result.get('entity_count', 0)} entities.",
            f"Media players: {counts.get('media_players', 0)}",
            f"Door sensors: {counts.get('door_sensors', 0)}",
            f"Cameras: {counts.get('cameras', 0)}",
            f"Fridge sensors: {counts.get('fridge_sensors', 0)}",
            f"Freezer sensors: {counts.get('freezer_sensors', 0)}",
            f"Ice maker candidates: {counts.get('ice_maker_candidates', 0)}",
            f"Filter sensors: {counts.get('filter_sensors', 0)}",
            f"Vacuums: {counts.get('vacuum_entities', 0)}",
            "",
            "Choose a Home Assistant trigger entity for each doorbell and test the matching RTSP URL.",
        ]
        self.status_txt.SetValue("\n".join(lines))

    def on_test_mqtt(self, event):
        settings = self._settings()
        mqtt_host = settings["mqtt_host"] or settings["ha_ip"]
        if not mqtt_host:
            self.status_txt.SetValue("Enter Home Assistant or MQTT host before testing MQTT.")
            return
        self._set_busy(True)
        self.status_txt.SetValue("Testing MQTT connection...")
        safe_submit(self._run_mqtt_test, settings)

    def _run_mqtt_test(self, settings):
        result = ring_discovery.test_mqtt_connection(
            mqtt_host=settings["mqtt_host"] or settings["ha_ip"],
            mqtt_port=settings["mqtt_port"],
            mqtt_username=settings["mqtt_username"],
            mqtt_password=settings["mqtt_password"],
            timeout=8,
        )
        wx.CallAfter(self._finish_mqtt_test, result)

    def _finish_mqtt_test(self, result):
        self._set_busy(False)
        if result.get("ok"):
            self.status_txt.SetValue(
                "MQTT connected successfully.\n"
                "Now click Find Ring Topics and trigger motion at the door."
            )
            return
        error = result.get("error")
        if error in {"bad_mqtt_credentials", "not_authorized"}:
            self.status_txt.SetValue(
                f"{result.get('message')}\n\n"
                "If you use the Mosquitto add-on, enter the MQTT username and password configured for that broker. "
                "These are separate from the Home Assistant long-lived token."
            )
        else:
            self.status_txt.SetValue(result.get("message") or "MQTT test failed.")

    def on_find_ring_topics(self, event):
        if self.ring_listen_cancel is not None:
            self.ring_listen_cancel.set()
            self.status_txt.SetValue("Stopping Ring topic listener...")
            return

        settings = self._settings()
        mqtt_host = settings["mqtt_host"] or settings["ha_ip"]
        if not mqtt_host:
            self.status_txt.SetValue("Enter Home Assistant or MQTT host before listening for Ring topics.")
            return
        self.ring_listen_cancel = threading.Event()
        self._set_busy(True)
        self.btn_ring.Enable(True)
        self.btn_ring.SetLabel("Cancel Ring Listen")
        self.status_txt.SetValue(
            "Listening to MQTT topic ring/# until a Ring topic is found.\n"
            "Walk in front of the camera or press the doorbell now.\n"
            "Click Cancel Ring Listen to stop."
        )
        safe_submit(self._run_ring_topic_discovery, settings, self.ring_listen_cancel)

    def _run_ring_topic_discovery(self, settings, stop_event):
        result = ring_discovery.listen_for_ring_topics(
            mqtt_host=settings["mqtt_host"] or settings["ha_ip"],
            mqtt_port=settings["mqtt_port"],
            mqtt_username=settings["mqtt_username"],
            mqtt_password=settings["mqtt_password"],
            duration=None,
            rtsp_host=settings["ha_ip"] or settings["mqtt_host"],
            stop_event=stop_event,
            stop_on_first=True,
        )
        wx.CallAfter(self._finish_ring_topic_discovery, result)

    def _finish_ring_topic_discovery(self, result):
        self.ring_listen_cancel = None
        self._set_busy(False)
        self.btn_ring.SetLabel("Find Ring Topics")
        if not result.get("ok"):
            self.status_txt.SetValue(result.get("message") or "Ring MQTT discovery failed.")
            return
        if result.get("cancelled"):
            self.status_txt.SetValue("Ring topic listening was cancelled.")
            return
        suggestions = result.get("suggestions", [])
        if not suggestions:
            self.status_txt.SetValue("No Ring motion/ding topics were detected. Check MQTT credentials and try again.")
            return
        found = suggestions[0]
        assigned = "Front"
        if not self.front_mqtt_txt.GetValue().strip():
            self.front_mqtt_txt.SetValue(found["topic"])
            if found.get("camera_id"):
                self.front_camera_id_txt.SetValue(found["camera_id"])
            if found.get("rtsp_url"):
                self.rtsp_front_txt.SetValue(found["rtsp_url"])
        elif not self.back_mqtt_txt.GetValue().strip():
            assigned = "Back"
            self.back_mqtt_txt.SetValue(found["topic"])
            if found.get("camera_id"):
                self.back_camera_id_txt.SetValue(found["camera_id"])
            if found.get("rtsp_url"):
                self.rtsp_back_txt.SetValue(found["rtsp_url"])
        else:
            assigned = "Neither field was empty"
        if found.get("ring_topic_root") and not self.ring_topic_root_txt.GetValue().strip():
            self.ring_topic_root_txt.SetValue(found["ring_topic_root"])
        self._refresh_derived_doorbell_preview()
        lines = [f"Detected {len(suggestions)} Ring topic candidate(s):"]
        for item in suggestions[:8]:
            lines.append(f"- {item['topic']} payload={item.get('payload', '')}")
            if item.get("rtsp_url"):
                lines.append(f"  RTSP: {item['rtsp_url']}")
        lines.append("")
        lines.append(f"Assigned to: {assigned}.")
        self.status_txt.SetValue("\n".join(lines))

    def on_save(self, event):
        settings = self._settings()
        if not settings["ha_ip"] or not settings["ha_token"]:
            self.status_txt.SetValue("Enter the Home Assistant host and access token before saving.")
            return
        if not settings["gemini_api_key"]:
            self.status_txt.SetValue("Gemini API key is required before saving.")
            return
        if settings["pushover_enabled"] and (not settings["pushover_user_key"] or not settings["pushover_api_token"]):
            self.status_txt.SetValue("Pushover is optional. Either enter both Pushover values or turn it off.")
            return

        self.parent.config["ha_ip"] = settings["ha_ip"]
        self.parent.config["ha_port"] = settings["ha_port"]
        self.parent.config["ha_token"] = settings["ha_token"]
        self.parent.config["gemini_api_key"] = settings["gemini_api_key"]
        self.parent.config["pushover_enabled"] = settings["pushover_enabled"]
        self.parent.config["pushover_user_key"] = settings["pushover_user_key"] if settings["pushover_enabled"] else ""
        self.parent.config["pushover_api_token"] = settings["pushover_api_token"] if settings["pushover_enabled"] else ""
        self.parent.config["front_camera_id"] = settings["front_camera_id"]
        self.parent.config["back_camera_id"] = settings["back_camera_id"]
        self.parent.config["ring_topic_root"] = settings["ring_topic_root"]
        self.parent.config["rtsp_front"] = settings["rtsp_front"]
        self.parent.config["rtsp_back"] = settings["rtsp_back"]
        self.parent.config["front_doorbell_mqtt_topic"] = settings["front_doorbell_mqtt_topic"]
        self.parent.config["back_doorbell_mqtt_topic"] = settings["back_doorbell_mqtt_topic"]
        self.parent.config["mqtt_host"] = settings["mqtt_host"]
        self.parent.config["mqtt_port"] = settings["mqtt_port"]
        self.parent.config["mqtt_username"] = settings["mqtt_username"]
        self.parent.config["mqtt_password"] = settings["mqtt_password"]
        self.parent.config["ha_listener_enabled"] = settings["ha_listener_enabled"]
        self.parent.config["doorbell_triggers"] = {
            "front": {
                "enabled": bool(settings["rtsp_front"] and settings["front_trigger_entity_id"]),
                "source": "ha_state",
                "trigger_entity_id": settings["front_trigger_entity_id"],
                "active_states": list(ha_listener.DEFAULT_ACTIVE_STATES),
                "rtsp_url": settings["rtsp_front"],
                "camera_id": settings["front_camera_id"],
                "mqtt_topic": settings["front_doorbell_mqtt_topic"],
            },
            "back": {
                "enabled": bool(settings["rtsp_back"] and settings["back_trigger_entity_id"]),
                "source": "ha_state",
                "trigger_entity_id": settings["back_trigger_entity_id"],
                "active_states": list(ha_listener.DEFAULT_ACTIVE_STATES),
                "rtsp_url": settings["rtsp_back"],
                "camera_id": settings["back_camera_id"],
                "mqtt_topic": settings["back_doorbell_mqtt_topic"],
            },
        }
        self.parent.save_config()
        cfg.sync_globals_from_config()
        self.parent.notify("Home Assistant settings saved.", priority=10)
        self.EndModal(wx.ID_OK)

# ==========================================
# WXPYTHON GUI DASHBOARD
# ==========================================
class ViperDashboard(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Viper Vision Control Panel", size=(800, 750))
        global dash_app
        dash_app = self

        self.running = True
        self.first_run = not cfg.CONFIG_FILE.exists()
        self.clean_first_run_test = os.getenv("VIPER_CLEAN_FIRST_RUN_TEST", "").strip().lower() in {"1", "true", "yes", "on"}
        self.config = cfg.load_config()
        ensure_cinderella_message_config(self.config)
        self.is_armed = self.config.get("is_armed", True)
        cfg.sync_globals_from_config()

        try:
            self.sr = auto.Auto()
            logging.info("Screen Reader Bridge established.")
        except Exception as e:
            self.sr = None
            logging.error(f"Screen Reader Bridge failed: {e}")

        self.speech_queue = PriorityQueue()
        self.speech_lock = threading.Lock()
        self._msg_counter = 0
        self.ha_listener = ha_listener.HomeAssistantEventListener(
            lambda: cfg.load_config(),
            _handle_ha_listener_action,
            self._on_ha_listener_status,
            is_shutting_down,
        )

        self.panel = wx.Panel(self)
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tb_icon = ViperTaskBarIcon(self)

        self.status_display = wx.TextCtrl(self.panel, value="Viper Vision Online", style=wx.TE_READONLY | wx.TE_CENTRE | wx.NO_BORDER)
        self.status_display.SetBackgroundColour(self.panel.GetBackgroundColour())
        font = self.status_display.GetFont()
        font.SetPointSize(12)
        self.status_display.SetFont(font)
        self.main_sizer.Add(self.status_display, 0, wx.ALL | wx.EXPAND, 10)

        self.setup_notebook()

        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_min = wx.Button(self.panel, label="Minimize to Tray", size=(-1, 40))
        self.btn_min.Bind(wx.EVT_BUTTON, self.on_minimize)
        self.btn_exit = wx.Button(self.panel, label="Exit Application", size=(-1, 40))
        self.btn_exit.Bind(wx.EVT_BUTTON, self.on_quit)
        bottom_sizer.Add(self.btn_min, 1, wx.ALL, 5)
        bottom_sizer.Add(self.btn_exit, 1, wx.ALL, 5)
        self.main_sizer.Add(bottom_sizer, 0, wx.ALL | wx.EXPAND, 5)

        self.panel.SetSizer(self.main_sizer)
        self.Bind(wx.EVT_CLOSE, self.on_minimize)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        self.Center()
        self.Show()
        env_prefill_allowed = not (self.first_run or self.clean_first_run_test)
        ha_settings = cfg.get_ha_settings(self.config, include_env=env_prefill_allowed)
        api_settings = cfg.get_api_settings(self.config, include_env=env_prefill_allowed)
        if not ha_settings.get("ha_token") or not api_settings.get("gemini_api_key"):
            wx.CallAfter(self.show_new_user_setup_assistant)
        if previous_run_unclean:
            wx.CallAfter(
                self.notify,
                "Viper may not have shut down cleanly last time. Use Run Diagnostics or Create Support Bundle if anything seems wrong.",
                priority=10,
            )

        threading.Thread(target=self.speech_worker, daemon=True).start()
        self.ha_listener.start()

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            page = "index"
            if hasattr(self, "notebook"):
                current = self.notebook.GetPageText(self.notebook.GetSelection())
                page = {
                    "Voice Behavior": "tts",
                    "Devices & Chimes": "speakers",
                    "Utilities": "ha-install",
                    "Fridge": "scenarios",
                    "Vacuum": "vacuum",
                    "Speed": "troubleshooting",
                    "HA Status": "setup",
                }.get(current, "index")
            if not open_help(page):
                self.notify("Help file not found.", priority=10)
            return
        event.Skip()

    def _on_ha_listener_status(self, status):
        if not hasattr(self, "ha_listener_status_txt"):
            return
        if not status.get("running"):
            label = "HA listener: stopped"
        elif status.get("connected"):
            label = f"HA listener: connected to {status.get('last_host') or 'Home Assistant'}"
        else:
            err = status.get("last_error") or "connecting"
            label = f"HA listener: not connected. {err}"
        wx.CallAfter(self.ha_listener_status_txt.SetLabel, label)

    def save_config(self):
        old_config = cfg.load_config()
        cfg.save_config(self.config)
        old_key = (
            old_config.get("tts_engine", "Edge TTS (Natural)"),
            old_config.get("edge_tts_voice", "en-US-AriaNeural"),
            old_config.get("gemini_tts_voice", "Sulafat"),
            old_config.get("google_tts_tld", "com"),
            old_config.get("local_voice_index", 1),
        )
        new_key = (
            self.config.get("tts_engine", "Edge TTS (Natural)"),
            self.config.get("edge_tts_voice", "en-US-AriaNeural"),
            self.config.get("gemini_tts_voice", "Sulafat"),
            self.config.get("gemini_tts_model", "gemini-3.1-flash-tts-preview"),
            self.config.get("google_tts_tld", "com"),
            self.config.get("local_voice_index", 1),
        )
        if old_key != new_key:
            audio.invalidate_phrase_cache()

    def setup_notebook(self):
        self.notebook = wx.Notebook(self.panel)
        self.tab_dash = wx.Panel(self.notebook)
        self.tab_tts = wx.ScrolledWindow(self.notebook)
        self.tab_tts.SetScrollRate(0, 20)
        self.tab_dev = wx.Panel(self.notebook)
        self.tab_util = wx.Panel(self.notebook)
        self.tab_fridge = wx.ScrolledWindow(self.notebook)
        self.tab_fridge.SetScrollRate(0, 20)
        self.tab_vacuum = wx.ScrolledWindow(self.notebook)
        self.tab_vacuum.SetScrollRate(0, 20)
        self.tab_speed = wx.ScrolledWindow(self.notebook)
        self.tab_speed.SetScrollRate(0, 20)
        self.tab_ha_status = wx.ScrolledWindow(self.notebook)
        self.tab_ha_status.SetScrollRate(0, 20)

        self.notebook.AddPage(self.tab_dash, "Dashboard")
        self.notebook.AddPage(self.tab_tts, "Voice Behavior")
        self.notebook.AddPage(self.tab_dev, "Devices & Chimes")
        self.notebook.AddPage(self.tab_util, "Utilities")
        self.notebook.AddPage(self.tab_fridge, "Fridge")
        self.notebook.AddPage(self.tab_vacuum, "Vacuum")
        self.notebook.AddPage(self.tab_speed, "Speed")
        self.notebook.AddPage(self.tab_ha_status, "HA Status")

        self.setup_hidden_ai_voice_compat_controls()
        self.setup_dash_tab()
        self.setup_tts_config_tab()
        self.setup_devices_tab()
        self.setup_utils_tab()
        self.setup_fridge_tab()
        self.setup_vacuum_tab()
        self.setup_speed_tab()
        self.setup_ha_status_tab()

        self.main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)

    def setup_hidden_ai_voice_compat_controls(self):
        self.voice_list = audio.get_available_windows_voices()
        self.engine_choice = wx.Choice(self.panel, choices=["Gemini (Cloud)", "Ollama (Local)", "Dual (Comparison)"])
        self.engine_choice.SetStringSelection(self.config.get("vision_engine", "Gemini (Cloud)"))
        self.engine_choice.Hide()

        self.tts_engine_choice = wx.Choice(self.panel, choices=["Edge TTS (Natural)", "Gemini TTS", "Google Cloud", "Local PC SAPI"])
        self.tts_engine_choice.SetStringSelection(self.config.get("tts_engine", "Edge TTS (Natural)"))
        self.tts_engine_choice.Hide()

        self.secondary_voice_label = wx.StaticText(self.panel, label="")
        self.secondary_voice_label.Hide()
        self.secondary_voice_choice = wx.Choice(self.panel, choices=[])
        self.secondary_voice_choice.Hide()
        self.btn_refresh_v = wx.Button(self.panel, label="Force Refresh Natural Voices")
        self.btn_refresh_v.Hide()

        self.voice_choice = wx.Choice(self.panel, choices=self.voice_list)
        current_voice_idx = self.config.get("local_voice_index", 1)
        if self.voice_list and current_voice_idx < len(self.voice_list):
            self.voice_choice.SetSelection(current_voice_idx)
        elif self.voice_list:
            self.voice_choice.SetSelection(0)
        self.voice_choice.Hide()

        prompt_names = list(self.config.get("prompts", {}).keys()) or ["Standard"]
        self.prompt_choice = wx.Choice(self.panel, choices=prompt_names)
        self.prompt_choice.SetStringSelection(self.config.get("active_prompt", prompt_names[0]))
        self.prompt_choice.Hide()
        self.prompt_editor = wx.TextCtrl(self.panel, style=wx.TE_MULTILINE)
        self.prompt_editor.SetValue(self.config.get("prompts", {}).get(self.config.get("active_prompt", prompt_names[0]), ""))
        self.prompt_editor.Hide()

    def setup_dash_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.btn_arm = wx.Button(self.tab_dash, label="Disarm System" if self.is_armed else "Arm System", size=(-1, 60))
        font = self.btn_arm.GetFont()
        font.SetPointSize(14)
        self.btn_arm.SetFont(font)
        self.btn_arm.Bind(wx.EVT_BUTTON, self.on_toggle_arm)
        sizer.Add(self.btn_arm, 0, wx.ALL | wx.EXPAND, 15)

        cbox = wx.StaticBox(self.tab_dash, label="Manual Intercom Broadcast")
        csizer = wx.StaticBoxSizer(cbox, wx.HORIZONTAL)
        self.broadcast_input = wx.TextCtrl(self.tab_dash, style=wx.TE_PROCESS_ENTER, size=(-1, 40))
        self.broadcast_btn = wx.Button(self.tab_dash, label="Speak", size=(-1, 40))
        self.broadcast_input.Bind(wx.EVT_TEXT_ENTER, self.on_broadcast)
        self.broadcast_btn.Bind(wx.EVT_BUTTON, self.on_broadcast)
        csizer.Add(self.broadcast_input, 1, wx.EXPAND | wx.ALL, 5)
        csizer.Add(self.broadcast_btn, 0, wx.ALL, 5)
        sizer.Add(csizer, 0, wx.ALL | wx.EXPAND, 15)

        self.tab_dash.SetSizer(sizer)

    def setup_ai_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        ebox = wx.StaticBox(self.tab_ai, label="Vision Engine")
        esizer = wx.StaticBoxSizer(ebox, wx.HORIZONTAL)
        engines = ["Gemini (Cloud)", "Ollama (Local)", "Dual (Comparison)"]
        self.engine_choice = wx.Choice(self.tab_ai, choices=engines)
        self.engine_choice.SetStringSelection(self.config.get("vision_engine", "Gemini (Cloud)"))
        self.engine_choice.Bind(wx.EVT_CHOICE, self.on_engine_change)
        esizer.Add(self.engine_choice, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(esizer, 0, wx.ALL | wx.EXPAND, 5)

        vbox = wx.StaticBox(self.tab_ai, label="Offline Voice")
        self.vsizer = wx.StaticBoxSizer(vbox, wx.VERTICAL)

        self.tts_engine_choice = wx.Choice(self.tab_ai, choices=["Edge TTS (Natural)", "Gemini TTS", "Google Cloud", "Local PC SAPI"])
        self.tts_engine_choice.SetStringSelection(self.config.get("tts_engine", "Edge TTS (Natural)"))
        self.tts_engine_choice.Bind(wx.EVT_CHOICE, self.on_tts_engine_change)
        self.tts_engine_choice.Hide()

        self.secondary_voice_label = wx.StaticText(self.tab_ai, label="Network Speaker Voice:")
        self.secondary_voice_choice = wx.Choice(self.tab_ai, choices=[])
        self.secondary_voice_choice.Bind(wx.EVT_CHOICE, self.on_secondary_voice_change)
        self.secondary_voice_label.Hide()
        self.secondary_voice_choice.Hide()

        self.btn_refresh_v = wx.Button(self.tab_ai, label="Force Refresh Natural Voices")
        self.btn_refresh_v.Bind(wx.EVT_BUTTON, self.on_refresh_edge_voices)
        self.btn_refresh_v.Hide()

        self.voice_list = audio.get_available_windows_voices()
        self.voice_choice = wx.Choice(self.tab_ai, choices=self.voice_list)
        current_voice_idx = self.config.get("local_voice_index", 1)
        if self.voice_list and current_voice_idx < len(self.voice_list): self.voice_choice.SetSelection(current_voice_idx)
        elif self.voice_list: self.voice_choice.SetSelection(0)
        self.voice_choice.Bind(wx.EVT_CHOICE, self.on_voice_change)
        self.vsizer.Add(wx.StaticText(self.tab_ai, label="Offline PC Voice for computer speakers and fallback:"), 0, wx.LEFT | wx.RIGHT, 5)
        self.vsizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 5)
        self._describe_control(
            self.voice_choice,
            "Offline PC voice selector. This only changes speech from the computer speakers and the offline fallback mode. It does not change Gemini cloud voices.",
        )

        sizer.Add(self.vsizer, 0, wx.ALL | wx.EXPAND, 5)

        pbox = wx.StaticBox(self.tab_ai, label="AI Prompt Editor")
        psizer = wx.StaticBoxSizer(pbox, wx.VERTICAL)
        self.prompt_choice = wx.Choice(self.tab_ai, choices=list(self.config["prompts"].keys()))
        active_p = self.config.get("active_prompt", "Standard")
        self.prompt_choice.SetStringSelection(active_p)
        self.prompt_choice.Bind(wx.EVT_CHOICE, self.on_prompt_change)
        psizer.Add(self.prompt_choice, 0, wx.EXPAND | wx.ALL, 5)
        self.prompt_editor = wx.TextCtrl(self.tab_ai, style=wx.TE_MULTILINE, size=(-1, 70))
        self.prompt_editor.SetValue(self.config["prompts"].get(active_p, ""))
        psizer.Add(self.prompt_editor, 0, wx.EXPAND | wx.ALL, 5)
        pbtn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_save_prompt = wx.Button(self.tab_ai, label="Save Current")
        self.btn_save_prompt.Bind(wx.EVT_BUTTON, self.on_save_prompt)
        self.btn_new_prompt = wx.Button(self.tab_ai, label="New Profile")
        self.btn_new_prompt.Bind(wx.EVT_BUTTON, self.on_new_prompt)
        self.btn_del_prompt = wx.Button(self.tab_ai, label="Delete Profile")
        self.btn_del_prompt.Bind(wx.EVT_BUTTON, self.on_del_prompt)
        pbtn_sizer.Add(self.btn_save_prompt, 1, wx.ALL, 2)
        pbtn_sizer.Add(self.btn_new_prompt, 1, wx.ALL, 2)
        pbtn_sizer.Add(self.btn_del_prompt, 1, wx.ALL, 2)
        psizer.Add(pbtn_sizer, 0, wx.EXPAND | wx.ALL, 0)
        sizer.Add(psizer, 0, wx.ALL | wx.EXPAND, 5)

        self.tab_ai.SetSizer(sizer)

        self._update_secondary_voice_ui()
        self.tts_engine_choice.Hide()
        self.secondary_voice_label.Hide()
        self.secondary_voice_choice.Hide()
        self.btn_refresh_v.Hide()

    def setup_tts_config_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.default_tts_controls = self._add_tts_settings_box(
            sizer,
            "Default TTS settings for all alerts",
            self.config.get("tts_defaults", {}),
            "default",
            include_use_default=False,
        )
        self.alert_tts_controls = {}
        labels = {
            "doorbell": "Doorbell alerts",
            "utilities": "Utility alerts",
            "manual": "Manual broadcasts",
        }
        for category, title in labels.items():
            controls = self._add_tts_settings_box(
                sizer,
                title,
                self.config.get("tts_alerts", {}).get(category, {}),
                category,
                include_use_default=True,
            )
            self.alert_tts_controls[category] = controls

        self.gemini_warm_status = wx.StaticText(self.tab_tts, label=self._format_gemini_warm_status())
        sizer.Add(self.gemini_warm_status, 0, wx.ALL | wx.EXPAND, 10)

        self.btn_save_voice_behavior = wx.Button(self.tab_tts, label="Save TTS settings")
        self.btn_save_voice_behavior.Bind(wx.EVT_BUTTON, self.on_save_voice_behavior)
        self._describe_control(
            self.btn_save_voice_behavior,
            "Save TTS settings button. Saves default TTS settings and any per-alert overrides.",
        )
        sizer.Add(self.btn_save_voice_behavior, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_tts.SetSizer(sizer)

    def _add_tts_settings_box(self, parent_sizer, title, settings, category, include_use_default=False):
        box = wx.StaticBox(self.tab_tts, label=title)
        box_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        controls = {}

        if include_use_default:
            use_default = wx.CheckBox(self.tab_tts, label=f"{title}: use default TTS settings")
            use_default.SetValue(bool(settings.get("use_defaults", True)))
            self._describe_control(
                use_default,
                f"{title} use default TTS settings checkbox. When checked, {title.lower()} use the default engine, voice, speed, and mood settings. Uncheck to customize this alert type.",
            )
            box_sizer.Add(use_default, 0, wx.ALL, 5)
            controls["use_defaults"] = use_default

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        engine_choice = wx.Choice(self.tab_tts, choices=list(VOICE_BEHAVIOR_MODES.keys()))
        engine_value = settings.get("engine", "gemini")
        engine_key = {"gemini": "natural_gemini", "edge": "fast_reliable", "google": "google_regular", "sapi": "offline_fallback"}.get(engine_value, "natural_gemini")
        engine_label = next((label for label, key in VOICE_BEHAVIOR_MODES.items() if key == engine_key), "Natural emotional voice, uses Gemini cloud TTS")
        engine_choice.SetStringSelection(engine_label)
        self._describe_control(
            engine_choice,
            f"{title} TTS engine. This chooses which speech engine is used for {title.lower()}.",
        )

        gemini_voice = wx.Choice(self.tab_tts, choices=list(GEMINI_TTS_VOICES.keys()))
        self._set_voice_choice_from_value(gemini_voice, settings.get("gemini_voice", "Sulafat"))
        self._describe_control(gemini_voice, f"{title} Gemini voice. Used when this alert type uses Gemini cloud TTS.")

        edge_voice = wx.Choice(self.tab_tts, choices=list(EDGE_VOICES.keys()))
        edge_label = next((label for label, voice in EDGE_VOICES.items() if voice == settings.get("edge_voice", "en-US-AriaNeural")), "Aria (Female)")
        edge_voice.SetStringSelection(edge_label)
        self._describe_control(edge_voice, f"{title} Microsoft Edge TTS voice. Used when this alert type uses Edge TTS or when Gemini falls back to Edge.")

        google_tld = wx.Choice(self.tab_tts, choices=list(DIALECTS.keys()))
        google_label = next((label for label, tld in DIALECTS.items() if tld == settings.get("google_tld", "com")), "American")
        google_tld.SetStringSelection(google_label)
        self._describe_control(google_tld, f"{title} regular Google TTS accent. Used when this alert type uses regular Google TTS.")

        sapi_voice = wx.Choice(self.tab_tts, choices=self.voice_list)
        sapi_idx = int(settings.get("sapi_voice_index", self.config.get("local_voice_index", 1)))
        if self.voice_list:
            sapi_voice.SetSelection(sapi_idx if sapi_idx < len(self.voice_list) else 0)
        self._describe_control(sapi_voice, f"{title} Windows offline voice. Used when this alert type uses Windows offline speech.")

        speed_choice = self._make_speed_choice(settings.get("speed", "normal"))
        self._describe_control(speed_choice, f"{title} speech speed. Controls how fast this alert type is spoken.")

        mood_chk = wx.CheckBox(self.tab_tts, label=f"{title}: use dynamic mood")
        mood_chk.SetValue(bool(settings.get("dynamic_mood", True)))
        self._describe_control(mood_chk, f"{title} dynamic mood checkbox. When checked, Viper detects urgent, excited, or warning wording and adjusts Gemini delivery.")

        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} TTS engine"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(engine_choice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} Gemini voice"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(gemini_voice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} Edge voice"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(edge_voice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} regular Google TTS accent"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(google_tld, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} Windows offline voice"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(sapi_voice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} speech speed"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(speed_choice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} dynamic mood"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(mood_chk, 1, wx.EXPAND)

        controls.update({
            "engine": engine_choice,
            "gemini_voice": gemini_voice,
            "edge_voice": edge_voice,
            "google_tld": google_tld,
            "sapi_voice": sapi_voice,
            "speed": speed_choice,
            "dynamic_mood": mood_chk,
        })

        if category == "default":
            keep_warm = wx.CheckBox(self.tab_tts, label="Default: reduce Gemini first-alert delay with warmup")
            keep_warm.SetValue(bool(settings.get("keep_warm", False)))
            self._describe_control(keep_warm, "Default Gemini warmup checkbox. When checked, Viper sends a small Gemini request every four minutes. These requests may be billed.")
            min_interval = wx.SpinCtrl(self.tab_tts, min=0, max=10, initial=int(settings.get("gemini_min_interval_seconds", 0)))
            self._describe_control(min_interval, "Default Gemini minimum seconds between requests. Zero is fastest. Increase only if Gemini returns quota errors.")
            grid.Add(wx.StaticText(self.tab_tts, label="Default Gemini warmup"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(keep_warm, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self.tab_tts, label="Default Gemini request spacing seconds"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(min_interval, 0, wx.EXPAND)
            controls["keep_warm"] = keep_warm
            controls["gemini_min_interval"] = min_interval

        box_sizer.Add(grid, 0, wx.ALL | wx.EXPAND, 8)

        if category != "default":
            test_btn = wx.Button(self.tab_tts, label=f"Test {title}")
            test_btn.Bind(wx.EVT_BUTTON, lambda evt, c=category: self.on_test_voice_behavior(evt, c))
            self._describe_control(test_btn, f"Test {title} button. Saves current TTS settings and plays a sample for {title.lower()}.")
            box_sizer.Add(test_btn, 0, wx.ALL | wx.EXPAND, 5)
            controls["test"] = test_btn
            controls["use_defaults"].Bind(wx.EVT_CHECKBOX, lambda evt, c=controls: self._sync_tts_override_controls(c))
            engine_choice.Bind(wx.EVT_CHOICE, lambda evt, c=controls: self._sync_tts_voice_controls(c))
            self._sync_tts_override_controls(controls)
        else:
            engine_choice.Bind(wx.EVT_CHOICE, lambda evt, c=controls: self._sync_tts_voice_controls(c))
            self._sync_tts_voice_controls(controls)

        parent_sizer.Add(box_sizer, 0, wx.ALL | wx.EXPAND, 10)
        return controls

    def _sync_tts_override_controls(self, controls):
        enabled = not controls["use_defaults"].GetValue()
        controls["engine"].Enable(enabled)
        controls["speed"].Enable(enabled)
        controls["dynamic_mood"].Enable(enabled)
        self._sync_tts_voice_controls(controls, parent_enabled=enabled)

    def _sync_tts_voice_controls(self, controls, parent_enabled=True):
        engine = self._engine_value_from_choice(controls["engine"])
        if "use_defaults" in controls and controls["use_defaults"].GetValue():
            parent_enabled = False
        controls["gemini_voice"].Enable(parent_enabled and engine == "gemini")
        controls["edge_voice"].Enable(parent_enabled and engine == "edge")
        controls["google_tld"].Enable(parent_enabled and engine == "google")
        controls["sapi_voice"].Enable(parent_enabled and engine == "sapi")

    def _make_speed_choice(self, speed_key):
        choice = wx.Choice(self.tab_tts, choices=list(VOICE_SPEEDS.keys()))
        label = next((label for label, key in VOICE_SPEEDS.items() if key == speed_key), "Normal conversational speed")
        choice.SetStringSelection(label)
        return choice

    def _describe_control(self, control, description):
        control.SetName(description)
        control.SetToolTip(description)
        control.Bind(wx.EVT_SET_FOCUS, lambda event, text=description: self._announce_focus_help(event, text))

    def _announce_focus_help(self, event, text):
        wx.CallAfter(self._safe_speak, text)
        event.Skip()

    def _tts_target_choices(self):
        choices = ["configured", "all"]
        choices.extend(self.config.get("speakers", {}).keys())
        return choices

    def _set_voice_choice_from_value(self, choice, voice_value):
        label = "Sulafat (Warm)"
        for item_label, item_value in GEMINI_TTS_VOICES.items():
            if item_value == voice_value:
                label = item_label
                break
        choice.SetStringSelection(label)

    def _refresh_tts_target_choices(self):
        if not hasattr(self, "tts_profile_controls"):
            return
        choices = self._tts_target_choices()
        for controls in self.tts_profile_controls.values():
            target_choice = controls["target"]
            current = target_choice.GetStringSelection() or "configured"
            new_choices = list(choices)
            if current not in new_choices:
                new_choices.append(current)
            target_choice.Set(new_choices)
            target_choice.SetStringSelection(current)

    def _format_gemini_warm_status(self):
        status = audio.gemini_tts_connection.status()
        if status.get("warm"):
            stamp = datetime.fromtimestamp(status.get("last_heartbeat_at", 0)).strftime("%H:%M:%S")
            return f"Warm: yes, last heartbeat {stamp}"
        err = status.get("last_error")
        if err:
            return f"Warm: no, last error: {err[:90]}"
        return "Warm: not yet"

    def _update_secondary_voice_ui(self):
        engine = self.tts_engine_choice.GetStringSelection()
        
        if engine == "Edge TTS (Natural)":
            self.secondary_voice_label.SetLabel("Microsoft TTS Voice:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(list(EDGE_VOICES.keys()))
            
            current_edge = self.config.get("edge_tts_voice", "en-US-AriaNeural")
            label_e = "Aria (Female)"
            for k, v in EDGE_VOICES.items():
                if v == current_edge: label_e = k
            self.secondary_voice_choice.SetStringSelection(label_e)
            self.secondary_voice_choice.Enable(True)
            self.btn_refresh_v.Show()

        elif engine == "Gemini TTS":
            self.secondary_voice_label.SetLabel("Gemini TTS Voice:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(list(GEMINI_TTS_VOICES.keys()))

            current_voice = self.config.get("gemini_tts_voice", "Sulafat")
            label_g = "Sulafat (Warm)"
            for k, v in GEMINI_TTS_VOICES.items():
                if v == current_voice:
                    label_g = k
            self.secondary_voice_choice.SetStringSelection(label_g)
            self.secondary_voice_choice.Enable(True)
            self.btn_refresh_v.Hide()

        elif engine == "Google Cloud":
            self.secondary_voice_label.SetLabel("Google Assistant Accent:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(list(DIALECTS.keys()))
            
            current_tld = self.config.get("google_tts_tld", "com")
            label_d = "American"
            for k, v in DIALECTS.items():
                if v == current_tld: label_d = k
            self.secondary_voice_choice.SetStringSelection(label_d)
            self.secondary_voice_choice.Enable(True)
            self.btn_refresh_v.Hide()

        else: # Local PC SAPI
            self.secondary_voice_label.SetLabel("Network Speaker Voice:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(["Uses Offline PC Voice setting below"])
            self.secondary_voice_choice.SetSelection(0)
            self.secondary_voice_choice.Enable(False)
            self.btn_refresh_v.Hide()

        self.tts_engine_choice.Hide()
        self.secondary_voice_label.Hide()
        self.secondary_voice_choice.Hide()
        self.btn_refresh_v.Hide()
        if hasattr(self, "tab_ai"):
            self.tab_ai.Layout()

    def setup_devices_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        sbox = wx.StaticBox(self.tab_dev, label="Speaker Targets (Spacebar to Toggle)")
        ssizer = wx.StaticBoxSizer(sbox, wx.VERTICAL)
        self.speaker_list = wx.CheckListBox(self.tab_dev, choices=[], size=(-1, 150))
        self.speaker_list.Bind(wx.EVT_CHECKLISTBOX, self.on_speaker_toggle)
        self.speaker_list.Bind(wx.EVT_LISTBOX, self.on_speaker_select)
        self.speaker_list.Bind(wx.EVT_SET_FOCUS, self.on_speaker_focus)
        ssizer.Add(self.speaker_list, 1, wx.EXPAND | wx.ALL, 5)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add_spk = wx.Button(self.tab_dev, label="Add Speaker")
        self.btn_add_spk.Bind(wx.EVT_BUTTON, self.on_add_speaker)
        self.btn_ren_spk = wx.Button(self.tab_dev, label="Rename Selected")
        self.btn_ren_spk.Bind(wx.EVT_BUTTON, self.on_rename_speaker)
        self.btn_rem_spk = wx.Button(self.tab_dev, label="Remove Selected")
        self.btn_rem_spk.Bind(wx.EVT_BUTTON, self.on_remove_speaker)
        btn_sizer.Add(self.btn_add_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_ren_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_rem_spk, 1, wx.ALL, 5)
        ssizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 0)
        sizer.Add(ssizer, 1, wx.ALL | wx.EXPAND, 10)

        rbox = wx.StaticBox(self.tab_dev, label="Selected Speaker Routing")
        rsizer = wx.StaticBoxSizer(rbox, wx.VERTICAL)
        self.chk_route_doorbell = wx.CheckBox(self.tab_dev, label="Doorbell Alerts")
        self.chk_route_utilities = wx.CheckBox(self.tab_dev, label="Utilities Spoken")
        self.chk_route_fridge = wx.CheckBox(self.tab_dev, label="Fridge / Freezer")
        self.chk_route_qhexempt = wx.CheckBox(self.tab_dev, label="Ignore Quiet Hours")
        for _chk in [self.chk_route_doorbell, self.chk_route_utilities, self.chk_route_fridge, self.chk_route_qhexempt]:
            _chk.Bind(wx.EVT_CHECKBOX, self.on_speaker_route_change)
            rsizer.Add(_chk, 0, wx.ALL, 5)
        sizer.Add(rsizer, 0, wx.ALL | wx.EXPAND, 10)

        self.refresh_speaker_list()
        self._sync_speaker_routing_controls()

        cbox_chimes = wx.StaticBox(self.tab_dev, label="Custom Doorbell Chimes")
        csizer_chimes = wx.StaticBoxSizer(cbox_chimes, wx.VERTICAL)
        front_sizer = wx.BoxSizer(wx.HORIZONTAL)
        front_lbl = wx.StaticText(self.tab_dev, label="Front Door:")
        self.front_chime_choice = wx.Choice(self.tab_dev)
        self.btn_test_front = wx.Button(self.tab_dev, label="Test")
        self.btn_test_front.Bind(wx.EVT_BUTTON, self.on_test_front)
        front_sizer.Add(front_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        front_sizer.Add(self.front_chime_choice, 1, wx.ALL, 5)
        front_sizer.Add(self.btn_test_front, 0, wx.ALL, 5)
        back_sizer = wx.BoxSizer(wx.HORIZONTAL)
        back_lbl = wx.StaticText(self.tab_dev, label="Back Door:")
        self.back_chime_choice = wx.Choice(self.tab_dev)
        self.btn_test_back = wx.Button(self.tab_dev, label="Test")
        self.btn_test_back.Bind(wx.EVT_BUTTON, self.on_test_back)
        back_sizer.Add(back_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        back_sizer.Add(self.back_chime_choice, 1, wx.ALL, 5)
        back_sizer.Add(self.btn_test_back, 0, wx.ALL, 5)
        chime_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_chimes = wx.Button(self.tab_dev, label="Refresh Folder")
        self.btn_save_chimes = wx.Button(self.tab_dev, label="Save Chimes")
        self.btn_refresh_chimes.Bind(wx.EVT_BUTTON, self.on_refresh_chimes)
        self.btn_save_chimes.Bind(wx.EVT_BUTTON, self.on_save_chimes)
        chime_btn_sizer.Add(self.btn_refresh_chimes, 1, wx.ALL, 5)
        chime_btn_sizer.Add(self.btn_save_chimes, 1, wx.ALL, 5)
        csizer_chimes.Add(front_sizer, 0, wx.EXPAND)
        csizer_chimes.Add(back_sizer, 0, wx.EXPAND)
        csizer_chimes.Add(chime_btn_sizer, 0, wx.EXPAND)
        sizer.Add(csizer_chimes, 0, wx.ALL | wx.EXPAND, 10)
        self._populate_chimes()

        self.tab_dev.SetSizer(sizer)

    def setup_utils_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        ubox = wx.StaticBox(self.tab_util, label="System Utilities")
        usizer = wx.StaticBoxSizer(ubox, wx.VERTICAL)

        self.btn_api = wx.Button(self.tab_util, label="Check API Cost", size=(-1, 40))
        self.btn_api.Bind(wx.EVT_BUTTON, self.on_api)
        self.btn_batt = wx.Button(self.tab_util, label="Check Doorbell Batteries", size=(-1, 40))
        self.btn_batt.Bind(wx.EVT_BUTTON, self.on_batt)
        self.btn_filter = wx.Button(self.tab_util, label="Check Refrigerator Filter", size=(-1, 40))
        self.btn_filter.Bind(wx.EVT_BUTTON, self.on_filter)
        self.btn_diagnostics = wx.Button(self.tab_util, label="Run Diagnostics", size=(-1, 40))
        self.btn_diagnostics.Bind(wx.EVT_BUTTON, self.on_run_diagnostics)
        self.btn_support_bundle = wx.Button(self.tab_util, label="Create Support Bundle", size=(-1, 40))
        self.btn_support_bundle.Bind(wx.EVT_BUTTON, self.on_create_support_bundle)
        self.btn_new_user_setup = wx.Button(self.tab_util, label="New User Setup Assistant", size=(-1, 40))
        self.btn_new_user_setup.Bind(wx.EVT_BUTTON, self.on_new_user_setup)
        self.btn_ha_setup = wx.Button(self.tab_util, label="Home Assistant Setup", size=(-1, 40))
        self.btn_ha_setup.Bind(wx.EVT_BUTTON, self.on_home_assistant_setup)
        self.btn_ha_package = wx.Button(self.tab_util, label="Advanced: Export HA YAML Package", size=(-1, 40))
        self.btn_ha_package.Bind(wx.EVT_BUTTON, self.on_generate_ha_package)
        self.btn_scan = wx.Button(self.tab_util, label="Scan Network for Sonos", size=(-1, 40))
        self.btn_scan.Bind(wx.EVT_BUTTON, self.on_scan_sonos)
        self.btn_scan_ha = wx.Button(self.tab_util, label="Scan HA for Speakers", size=(-1, 40))
        self.btn_scan_ha.Bind(wx.EVT_BUTTON, self.on_scan_ha)

        usizer.Add(self.btn_api, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_batt, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_filter, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_diagnostics, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_support_bundle, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_new_user_setup, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_ha_setup, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_ha_package, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_scan, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_scan_ha, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(usizer, 1, wx.ALL | wx.EXPAND, 10)

        qbox = wx.StaticBox(self.tab_util, label="Quiet Hours")
        qsizer = wx.StaticBoxSizer(qbox, wx.VERTICAL)
        self.quiet_hours_enable_chk = wx.CheckBox(self.tab_util, label="Enable quiet hours (suppresses utilities)")
        self.quiet_hours_enable_chk.SetValue(self.config.get("quiet_hours_enabled", False))
        self.quiet_hours_enable_chk.Bind(wx.EVT_CHECKBOX, self.on_quiet_hours_change)
        qsizer.Add(self.quiet_hours_enable_chk, 0, wx.ALL, 5)

        qrow = wx.BoxSizer(wx.HORIZONTAL)
        qrow.Add(wx.StaticText(self.tab_util, label="Start (HH:MM):"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.quiet_hours_start_txt = wx.TextCtrl(self.tab_util, value=self.config.get("quiet_hours_start", "22:00"))
        qrow.Add(self.quiet_hours_start_txt, 1, wx.ALL, 5)
        qrow.Add(wx.StaticText(self.tab_util, label="End (HH:MM):"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.quiet_hours_end_txt = wx.TextCtrl(self.tab_util, value=self.config.get("quiet_hours_end", "07:00"))
        qrow.Add(self.quiet_hours_end_txt, 1, wx.ALL, 5)
        qsizer.Add(qrow, 0, wx.EXPAND)

        self.btn_save_quiet_hours = wx.Button(self.tab_util, label="Save Quiet Hours", size=(-1, 40))
        self.btn_save_quiet_hours.Bind(wx.EVT_BUTTON, self.on_quiet_hours_change)
        qsizer.Add(self.btn_save_quiet_hours, 0, wx.ALL | wx.EXPAND, 5)

        sizer.Add(qsizer, 0, wx.ALL | wx.EXPAND, 10)
        self.tab_util.SetSizer(sizer)

    def setup_vacuum_tab(self):
        self.vacuum_state_entities = []
        self.vacuum_control_entities = []
        self.vacuum_control_widgets = {}
        self.vacuum_rooms = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        top_box = wx.StaticBox(self.tab_vacuum, label="Roborock Vacuum Controls")
        top = wx.StaticBoxSizer(top_box, wx.VERTICAL)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self.tab_vacuum, label="Vacuum entity:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_choice = wx.Choice(self.tab_vacuum, choices=[])
        self.vacuum_choice.Bind(wx.EVT_CHOICE, self.on_vacuum_choice_change)
        self._describe_control(
            self.vacuum_choice,
            "Vacuum entity picker. Choose which Roborock vacuum Viper should control.",
        )
        row.Add(self.vacuum_choice, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_refresh_vacuum = wx.Button(self.tab_vacuum, label="Refresh vacuum controls", size=(-1, 40))
        self.btn_refresh_vacuum.Bind(wx.EVT_BUTTON, self.on_refresh_vacuum)
        self._describe_control(
            self.btn_refresh_vacuum,
            "Refresh vacuum controls button. Scans Home Assistant for Roborock vacuum controls, modes, switches, buttons, and status sensors.",
        )
        row.Add(self.btn_refresh_vacuum, 0, wx.ALL, 5)
        top.Add(row, 0, wx.EXPAND)

        self.vacuum_status_txt = wx.TextCtrl(
            self.tab_vacuum,
            value="Press Refresh vacuum controls to scan Home Assistant for Roborock controls.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 150),
        )
        self._describe_control(
            self.vacuum_status_txt,
            "Vacuum status. This read only box summarizes the selected vacuum state and nearby Roborock status sensors.",
        )
        top.Add(self.vacuum_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        actions = wx.GridSizer(rows=2, cols=3, vgap=6, hgap=6)
        for label, service, help_text in [
            ("Start cleaning", "vacuum/start", "Start cleaning button. Starts or resumes the selected Roborock vacuum."),
            ("Pause cleaning", "vacuum/pause", "Pause cleaning button. Pauses the selected Roborock vacuum."),
            ("Stop cleaning", "vacuum/stop", "Stop cleaning button. Stops the selected Roborock vacuum."),
            ("Return to dock", "vacuum/return_to_base", "Return to dock button. Sends the selected Roborock vacuum back to its dock."),
            ("Locate vacuum", "vacuum/locate", "Locate vacuum button. Makes the selected Roborock identify itself if Home Assistant supports locate."),
            ("Spot clean", "vacuum/clean_spot", "Spot clean button. Starts a spot cleaning cycle if Home Assistant supports it."),
        ]:
            btn = wx.Button(self.tab_vacuum, label=label, size=(-1, 40))
            btn.Bind(wx.EVT_BUTTON, lambda event, svc=service: self.on_vacuum_basic_action(event, svc))
            self._describe_control(btn, help_text)
            actions.Add(btn, 0, wx.EXPAND)
        top.Add(actions, 0, wx.ALL | wx.EXPAND, 5)

        fan_row = wx.BoxSizer(wx.HORIZONTAL)
        fan_row.Add(wx.StaticText(self.tab_vacuum, label="Vacuum suction speed:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_fan_choice = wx.Choice(self.tab_vacuum, choices=[])
        self._describe_control(
            self.vacuum_fan_choice,
            "Vacuum suction speed picker. Choose a fan speed from the selected vacuum, then press Set suction speed.",
        )
        fan_row.Add(self.vacuum_fan_choice, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_set_vacuum_fan = wx.Button(self.tab_vacuum, label="Set suction speed", size=(-1, 40))
        self.btn_set_vacuum_fan.Bind(wx.EVT_BUTTON, self.on_vacuum_set_fan_speed)
        self._describe_control(
            self.btn_set_vacuum_fan,
            "Set suction speed button. Sends the chosen suction or fan speed to the selected Roborock vacuum.",
        )
        fan_row.Add(self.btn_set_vacuum_fan, 0, wx.ALL, 5)
        top.Add(fan_row, 0, wx.EXPAND)
        sizer.Add(top, 0, wx.ALL | wx.EXPAND, 10)

        room_box = wx.StaticBox(self.tab_vacuum, label="Room Cleaning")
        room_outer = wx.StaticBoxSizer(room_box, wx.VERTICAL)
        room_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_vacuum_rooms = wx.Button(self.tab_vacuum, label="Refresh room list", size=(-1, 40))
        self.btn_refresh_vacuum_rooms.Bind(wx.EVT_BUTTON, self.on_refresh_vacuum_rooms)
        self._describe_control(
            self.btn_refresh_vacuum_rooms,
            "Refresh room list button. Asks Home Assistant for Roborock map rooms and fills the room checklist.",
        )
        room_buttons.Add(self.btn_refresh_vacuum_rooms, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_clean_vacuum_rooms = wx.Button(self.tab_vacuum, label="Clean selected rooms", size=(-1, 40))
        self.btn_clean_vacuum_rooms.Bind(wx.EVT_BUTTON, self.on_vacuum_clean_selected_rooms)
        self._describe_control(
            self.btn_clean_vacuum_rooms,
            "Clean selected rooms button. Sends the checked Roborock rooms to the selected vacuum.",
        )
        room_buttons.Add(self.btn_clean_vacuum_rooms, 1, wx.ALL | wx.EXPAND, 5)
        room_outer.Add(room_buttons, 0, wx.EXPAND)

        self.vacuum_room_list = wx.CheckListBox(self.tab_vacuum, choices=[], size=(-1, 140))
        self.vacuum_room_list.Bind(wx.EVT_KEY_DOWN, self.on_vacuum_room_key_down)
        self._describe_control(
            self.vacuum_room_list,
            "Roborock room checklist. Use arrow keys to move through rooms. Press Space to check or uncheck the focused room, then press Clean selected rooms.",
        )
        room_outer.Add(self.vacuum_room_list, 0, wx.ALL | wx.EXPAND, 5)

        repeat_row = wx.BoxSizer(wx.HORIZONTAL)
        repeat_row.Add(wx.StaticText(self.tab_vacuum, label="Room clean repeat count:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_room_repeat = wx.SpinCtrl(self.tab_vacuum, min=1, max=3, initial=1)
        self._describe_control(
            self.vacuum_room_repeat,
            "Room clean repeat count. Choose 1, 2, or 3 passes for selected rooms.",
        )
        repeat_row.Add(self.vacuum_room_repeat, 0, wx.ALL, 5)
        room_outer.Add(repeat_row, 0, wx.EXPAND)

        self.vacuum_room_status_txt = wx.TextCtrl(
            self.tab_vacuum,
            value="Press Refresh room list to load Roborock rooms from Home Assistant.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 80),
        )
        self._describe_control(
            self.vacuum_room_status_txt,
            "Room cleaning status. This read only box reports map and room discovery results.",
        )
        room_outer.Add(self.vacuum_room_status_txt, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(room_outer, 0, wx.ALL | wx.EXPAND, 10)

        dynamic_box = wx.StaticBox(self.tab_vacuum, label="Discovered Roborock Settings")
        dynamic_outer = wx.StaticBoxSizer(dynamic_box, wx.VERTICAL)
        self.vacuum_controls_panel = wx.Panel(self.tab_vacuum)
        self.vacuum_controls_sizer = wx.BoxSizer(wx.VERTICAL)
        self.vacuum_controls_panel.SetSizer(self.vacuum_controls_sizer)
        dynamic_outer.Add(self.vacuum_controls_panel, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(dynamic_outer, 0, wx.ALL | wx.EXPAND, 10)

        command_box = wx.StaticBox(self.tab_vacuum, label="Advanced Roborock Command")
        command = wx.StaticBoxSizer(command_box, wx.VERTICAL)
        command.Add(wx.StaticText(self.tab_vacuum, label="Command name:"), 0, wx.ALL, 5)
        self.vacuum_command_txt = wx.TextCtrl(self.tab_vacuum, value="")
        self._describe_control(
            self.vacuum_command_txt,
            "Advanced command name. Example: app_segment_clean. Leave blank unless you know the Roborock command to send.",
        )
        command.Add(self.vacuum_command_txt, 0, wx.ALL | wx.EXPAND, 5)
        command.Add(wx.StaticText(self.tab_vacuum, label="Parameters JSON, optional:"), 0, wx.ALL, 5)
        self.vacuum_params_txt = wx.TextCtrl(self.tab_vacuum, value="", style=wx.TE_MULTILINE, size=(-1, 90))
        self._describe_control(
            self.vacuum_params_txt,
            "Advanced command parameters JSON. Optional. Example: a JSON object or list for Home Assistant vacuum send command parameters.",
        )
        command.Add(self.vacuum_params_txt, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_send_vacuum_command = wx.Button(self.tab_vacuum, label="Send advanced vacuum command", size=(-1, 40))
        self.btn_send_vacuum_command.Bind(wx.EVT_BUTTON, self.on_vacuum_send_command)
        self._describe_control(
            self.btn_send_vacuum_command,
            "Send advanced vacuum command button. Calls Home Assistant vacuum send command for the selected Roborock vacuum.",
        )
        command.Add(self.btn_send_vacuum_command, 0, wx.ALL | wx.EXPAND, 5)

        command.Add(wx.StaticText(self.tab_vacuum, label="Home Assistant area IDs, comma separated, optional:"), 0, wx.ALL, 5)
        self.vacuum_area_ids_txt = wx.TextCtrl(self.tab_vacuum, value="")
        self._describe_control(
            self.vacuum_area_ids_txt,
            "Home Assistant area IDs for vacuum clean area. Enter comma separated area IDs only if your vacuum segments are mapped to Home Assistant areas.",
        )
        command.Add(self.vacuum_area_ids_txt, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_clean_vacuum_areas = wx.Button(self.tab_vacuum, label="Clean Home Assistant areas", size=(-1, 40))
        self.btn_clean_vacuum_areas.Bind(wx.EVT_BUTTON, self.on_vacuum_clean_areas)
        self._describe_control(
            self.btn_clean_vacuum_areas,
            "Clean Home Assistant areas button. Calls vacuum clean area using the comma separated area IDs.",
        )
        command.Add(self.btn_clean_vacuum_areas, 0, wx.ALL | wx.EXPAND, 5)

        goto_row = wx.BoxSizer(wx.HORIZONTAL)
        goto_row.Add(wx.StaticText(self.tab_vacuum, label="Go to X:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_goto_x_txt = wx.TextCtrl(self.tab_vacuum, value="25500")
        self._describe_control(
            self.vacuum_goto_x_txt,
            "Roborock go to X coordinate. Enter an integer coordinate. The dock is often near 25500.",
        )
        goto_row.Add(self.vacuum_goto_x_txt, 1, wx.ALL | wx.EXPAND, 5)
        goto_row.Add(wx.StaticText(self.tab_vacuum, label="Go to Y:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_goto_y_txt = wx.TextCtrl(self.tab_vacuum, value="25500")
        self._describe_control(
            self.vacuum_goto_y_txt,
            "Roborock go to Y coordinate. Enter an integer coordinate. The dock is often near 25500.",
        )
        goto_row.Add(self.vacuum_goto_y_txt, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_vacuum_goto = wx.Button(self.tab_vacuum, label="Send vacuum to coordinates", size=(-1, 40))
        self.btn_vacuum_goto.Bind(wx.EVT_BUTTON, self.on_vacuum_goto_position)
        self._describe_control(
            self.btn_vacuum_goto,
            "Send vacuum to coordinates button. Calls the Roborock go to position service for the selected vacuum.",
        )
        goto_row.Add(self.btn_vacuum_goto, 0, wx.ALL, 5)
        command.Add(goto_row, 0, wx.EXPAND)
        sizer.Add(command, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_vacuum.SetSizer(sizer)
        wx.CallAfter(self.on_refresh_vacuum, None)

    def on_refresh_vacuum(self, event):
        self.vacuum_status_txt.SetValue("Scanning Home Assistant for Roborock vacuum controls...")
        safe_submit(self._run_vacuum_refresh)

    def _run_vacuum_refresh(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        result = discovery.get_ha_states(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=8,
        )
        if not result.get("ok"):
            message = result.get("message") or result.get("error") or "Home Assistant scan failed."
            wx.CallAfter(self._finish_vacuum_refresh, [], [], f"Vacuum scan failed: {message}")
            return
        states = result.get("states", [])
        vacuums = [entity for entity in states if self._ha_domain(entity) == "vacuum"]
        roborock_vacuums = [entity for entity in vacuums if self._looks_like_roborock(entity)]
        selected_vacuums = roborock_vacuums or vacuums
        current = self._selected_vacuum_entity_id()
        if current and not any(e.get("entity_id") == current for e in selected_vacuums):
            current = ""
        selected = current or (selected_vacuums[0].get("entity_id") if selected_vacuums else "")
        controls = self._find_vacuum_related_controls(states, selected)
        summary = self._build_vacuum_summary(selected_vacuums, controls, selected)
        wx.CallAfter(self._finish_vacuum_refresh, selected_vacuums, controls, summary)

    def _finish_vacuum_refresh(self, vacuums, controls, summary):
        self.vacuum_state_entities = vacuums
        self.vacuum_control_entities = controls
        current = self._selected_vacuum_entity_id()
        vacuum_choices = [self._entity_choice_label(entity) for entity in vacuums]
        self.vacuum_choice.Set(vacuum_choices)
        if vacuum_choices:
            selected_label = next(
                (label for label, entity in zip(vacuum_choices, vacuums) if entity.get("entity_id") == current),
                vacuum_choices[0],
            )
            self.vacuum_choice.SetStringSelection(selected_label)
        self.vacuum_status_txt.SetValue(summary)
        self._populate_vacuum_fan_speed()
        self._finish_vacuum_room_refresh(
            self._get_saved_vacuum_rooms(self._selected_vacuum_entity_id()),
            "Saved room list loaded. Press Refresh room list to update it from Home Assistant.",
            save=False,
        )
        self._rebuild_vacuum_dynamic_controls()
        self.tab_vacuum.Layout()
        self.tab_vacuum.FitInside()

    def _selected_vacuum_entity_id(self):
        if not hasattr(self, "vacuum_choice"):
            return ""
        selection = self.vacuum_choice.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(getattr(self, "vacuum_state_entities", [])):
            return ""
        return self.vacuum_state_entities[selection].get("entity_id", "")

    def on_vacuum_choice_change(self, event):
        selected = self._selected_vacuum_entity_id()
        controls = self._find_vacuum_related_controls(
            getattr(self, "_last_vacuum_states", []) or getattr(self, "vacuum_control_entities", []),
            selected,
        )
        if not controls:
            controls = getattr(self, "vacuum_control_entities", [])
        self.vacuum_control_entities = controls
        self.vacuum_status_txt.SetValue(self._build_vacuum_summary(self.vacuum_state_entities, controls, selected))
        self._populate_vacuum_fan_speed()
        self._finish_vacuum_room_refresh(
            self._get_saved_vacuum_rooms(selected),
            "Saved room list loaded. Press Refresh room list to update it from Home Assistant.",
            save=False,
        )
        self._rebuild_vacuum_dynamic_controls()

    def _ha_domain(self, entity):
        entity_id = entity.get("entity_id", "")
        return entity_id.split(".", 1)[0] if "." in entity_id else ""

    def _ha_name(self, entity):
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        return str(attrs.get("friendly_name") or entity.get("entity_id") or "")

    def _looks_like_roborock(self, entity):
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        text = " ".join(
            str(part).lower()
            for part in [
                entity.get("entity_id"),
                attrs.get("friendly_name"),
                attrs.get("manufacturer"),
                attrs.get("model"),
                attrs.get("device_class"),
                attrs.get("platform"),
                attrs.get("integration"),
            ]
        )
        return any(token in text for token in ["roborock", "cinderella", "saros", "qrevo", "q revo", "s7", "s8"])

    def _vacuum_match_tokens(self, selected_entity_id):
        tokens = {"roborock", "cinderella", "saros", "qrevo", "q revo"}
        if selected_entity_id and "." in selected_entity_id:
            base = selected_entity_id.split(".", 1)[1]
            tokens.add(base.lower())
            tokens.update(part for part in re.split(r"[_\s-]+", base.lower()) if len(part) >= 4)
        return tokens

    def _find_vacuum_related_controls(self, states, selected_entity_id):
        if not states:
            return []
        self._last_vacuum_states = states
        control_domains = {"vacuum", "select", "number", "switch", "button", "sensor", "binary_sensor"}
        tokens = self._vacuum_match_tokens(selected_entity_id)
        related = []
        for entity in states:
            domain = self._ha_domain(entity)
            if domain not in control_domains:
                continue
            attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            text = " ".join(str(part).lower() for part in [entity.get("entity_id"), attrs.get("friendly_name"), attrs.get("manufacturer"), attrs.get("model")])
            if entity.get("entity_id") == selected_entity_id or any(token and token in text for token in tokens):
                related.append(entity)
        return sorted(related, key=lambda e: (self._ha_domain(e), self._ha_name(e).lower(), e.get("entity_id", "")))

    def _entity_choice_label(self, entity):
        name = self._ha_name(entity)
        entity_id = entity.get("entity_id", "")
        state = entity.get("state", "unknown")
        return f"{name} ({entity_id}, state {state})"

    def _short_entity_label(self, entity):
        name = self._ha_name(entity)
        entity_id = entity.get("entity_id", "")
        if name and name != entity_id:
            return f"{name} ({entity_id})"
        return entity_id

    def _build_vacuum_summary(self, vacuums, controls, selected):
        lines = ["Vacuum Controls", ""]
        if not vacuums:
            lines.append("No vacuum entities found in Home Assistant. Check the HA Status tab and your Home Assistant token.")
            return "\n".join(lines)
        selected_entity = next((entity for entity in vacuums if entity.get("entity_id") == selected), vacuums[0])
        attrs = selected_entity.get("attributes") if isinstance(selected_entity.get("attributes"), dict) else {}
        battery = attrs.get("battery_level", "unknown")
        if battery == "unknown" or battery is None:
            battery_entity = next((entity for entity in controls if entity.get("entity_id", "").endswith("_battery")), None)
            if battery_entity:
                battery = battery_entity.get("state", "unknown")
        lines.extend([
            f"Selected: {self._short_entity_label(selected_entity)}",
            f"State: {selected_entity.get('state', 'unknown')}",
            f"Battery: {battery}",
            f"Current suction speed: {attrs.get('fan_speed', 'unknown')}",
            f"Discovered related entities: {len(controls)}",
            "",
            "Interactive controls found:",
        ])
        counts = {}
        for entity in controls:
            domain = self._ha_domain(entity)
            counts[domain] = counts.get(domain, 0) + 1
        for domain in ["select", "number", "switch", "button", "sensor", "binary_sensor"]:
            if counts.get(domain):
                lines.append(f"{domain}: {counts[domain]}")
        if not any(self._ha_domain(entity) in {"select", "number", "switch", "button"} for entity in controls):
            lines.append("No extra Roborock select, number, switch, or button entities were found. Basic vacuum actions are still available.")
        sensor_entities = [entity for entity in controls if self._ha_domain(entity) in {"sensor", "binary_sensor"}]
        if sensor_entities:
            lines.extend(["", "Status snapshot:"])
            for entity in sensor_entities:
                lines.append(f"{self._short_entity_label(entity)}: {entity.get('state', 'unknown')}")
        return "\n".join(lines)

    def _populate_vacuum_fan_speed(self):
        selected = self._selected_vacuum_entity_id()
        entity = next((item for item in self.vacuum_state_entities if item.get("entity_id") == selected), None)
        attrs = entity.get("attributes") if entity and isinstance(entity.get("attributes"), dict) else {}
        speeds = attrs.get("fan_speed_list") if isinstance(attrs.get("fan_speed_list"), list) else []
        current = attrs.get("fan_speed")
        self.vacuum_fan_choice.Set([str(item) for item in speeds])
        if current and str(current) in [str(item) for item in speeds]:
            self.vacuum_fan_choice.SetStringSelection(str(current))
        elif speeds:
            self.vacuum_fan_choice.SetSelection(0)
        self.vacuum_fan_choice.Enable(bool(speeds))
        self.btn_set_vacuum_fan.Enable(bool(speeds))

    def _clear_sizer(self, sizer):
        while sizer.GetItemCount():
            item = sizer.GetItem(0)
            window = item.GetWindow()
            child_sizer = item.GetSizer()
            sizer.Detach(0)
            if window:
                window.Destroy()
            elif child_sizer:
                self._clear_sizer(child_sizer)

    def _rebuild_vacuum_dynamic_controls(self):
        self._clear_sizer(self.vacuum_controls_sizer)
        self.vacuum_control_widgets = {}
        interactive = [entity for entity in self.vacuum_control_entities if self._show_vacuum_setting(entity)]
        if not interactive:
            self.vacuum_controls_sizer.Add(
                wx.StaticText(self.vacuum_controls_panel, label="No discovered setting entities yet. Press Refresh vacuum controls after Home Assistant is connected."),
                0,
                wx.ALL | wx.EXPAND,
                5,
            )
            self.vacuum_controls_panel.Layout()
            return
        for entity in interactive:
            domain = self._ha_domain(entity)
            entity_id = entity.get("entity_id", "")
            attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{self._short_entity_label(entity)}:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            if domain == "select":
                options = [str(item) for item in attrs.get("options", [])] if isinstance(attrs.get("options"), list) else []
                choice = wx.Choice(self.vacuum_controls_panel, choices=options)
                if str(entity.get("state", "")) in options:
                    choice.SetStringSelection(str(entity.get("state")))
                elif options:
                    choice.SetSelection(0)
                btn = wx.Button(self.vacuum_controls_panel, label="Apply setting")
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_set_select(event, eid))
                self._describe_control(choice, f"{self._short_entity_label(entity)} picker. Choose a Roborock setting value, then press Apply setting.")
                self._describe_control(btn, f"Apply {self._short_entity_label(entity)} button. Sends the selected value to Home Assistant.")
                row.Add(choice, 1, wx.ALL | wx.EXPAND, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_control_widgets[entity_id] = choice
            elif domain == "number":
                minimum = attrs.get("min", 0)
                maximum = attrs.get("max", 100)
                step = attrs.get("step", 1)
                spin = wx.SpinCtrlDouble(self.vacuum_controls_panel, min=float(minimum), max=float(maximum), inc=float(step))
                try:
                    spin.SetValue(float(entity.get("state", minimum)))
                except (TypeError, ValueError):
                    spin.SetValue(float(minimum))
                btn = wx.Button(self.vacuum_controls_panel, label="Set number")
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_set_number(event, eid))
                self._describe_control(spin, f"{self._short_entity_label(entity)} numeric value. Adjust the value, then press Set number.")
                self._describe_control(btn, f"Set {self._short_entity_label(entity)} number button. Sends the numeric value to Home Assistant.")
                row.Add(spin, 1, wx.ALL | wx.EXPAND, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_control_widgets[entity_id] = spin
            elif domain == "switch":
                state = str(entity.get("state", "")).lower()
                btn_on = wx.Button(self.vacuum_controls_panel, label="Turn on")
                btn_off = wx.Button(self.vacuum_controls_panel, label="Turn off")
                btn_on.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_switch(event, eid, True))
                btn_off.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_switch(event, eid, False))
                self._describe_control(btn_on, f"Turn on {self._short_entity_label(entity)} button. Current state is {state or 'unknown'}.")
                self._describe_control(btn_off, f"Turn off {self._short_entity_label(entity)} button. Current state is {state or 'unknown'}.")
                row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"Current state {state or 'unknown'}"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
                row.Add(btn_on, 0, wx.ALL, 5)
                row.Add(btn_off, 0, wx.ALL, 5)
            elif domain == "button":
                btn = wx.Button(self.vacuum_controls_panel, label="Press button")
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_press_button(event, eid))
                self._describe_control(btn, f"Press {self._short_entity_label(entity)} button. Sends a Home Assistant button press for this Roborock control.")
                row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"Last state {entity.get('state', 'unknown')}"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
                row.Add(btn, 0, wx.ALL, 5)
            self.vacuum_controls_sizer.Add(row, 0, wx.EXPAND)
        self.vacuum_controls_panel.Layout()

    def _show_vacuum_setting(self, entity):
        entity_id = entity.get("entity_id", "")
        domain = self._ha_domain(entity)
        if domain in {"select", "number"}:
            return True
        if domain == "switch" and "child_lock" in entity_id:
            return True
        return False

    def on_vacuum_basic_action(self, event, service):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        self._run_ha_service_async(service, {"entity_id": entity_id}, f"Sent {service.replace('/', '.')} to {entity_id}.")

    def on_vacuum_set_fan_speed(self, event):
        entity_id = self._selected_vacuum_entity_id()
        speed = self.vacuum_fan_choice.GetStringSelection()
        if not entity_id or not speed:
            self.notify("Choose a vacuum and suction speed first.", priority=10)
            return
        self._run_ha_service_async("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": speed}, f"Set suction speed to {speed}.")

    def on_vacuum_set_select(self, event, entity_id):
        choice = self.vacuum_control_widgets.get(entity_id)
        option = choice.GetStringSelection() if choice else ""
        if not option:
            self.notify("Choose a setting value first.", priority=10)
            return
        self._run_ha_service_async("select/select_option", {"entity_id": entity_id, "option": option}, f"Set {entity_id} to {option}.")

    def on_vacuum_set_number(self, event, entity_id):
        spin = self.vacuum_control_widgets.get(entity_id)
        value = spin.GetValue() if spin else None
        if value is None:
            self.notify("Enter a number first.", priority=10)
            return
        self._run_ha_service_async("number/set_value", {"entity_id": entity_id, "value": value}, f"Set {entity_id} to {value}.")

    def on_vacuum_switch(self, event, entity_id, turn_on):
        service = "switch/turn_on" if turn_on else "switch/turn_off"
        label = "on" if turn_on else "off"
        self._run_ha_service_async(service, {"entity_id": entity_id}, f"Turned {label} {entity_id}.")

    def on_vacuum_press_button(self, event, entity_id):
        self._run_ha_service_async("button/press", {"entity_id": entity_id}, f"Pressed {entity_id}.")

    def on_refresh_vacuum_rooms(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        self.vacuum_room_status_txt.SetValue("Loading Roborock rooms from Home Assistant...")
        safe_submit(self._run_vacuum_room_refresh, entity_id)

    def _run_vacuum_room_refresh(self, entity_id):
        result = self._call_ha_service_response("roborock/get_maps", {"entity_id": entity_id})
        if not result.get("ok"):
            message = result.get("message") or result.get("error") or "Room discovery failed."
            wx.CallAfter(self._finish_vacuum_room_refresh, self._get_saved_vacuum_rooms(entity_id), f"Room discovery failed: {message}", False)
            return
        rooms = self._parse_roborock_rooms(result.get("data"), entity_id)
        if not rooms:
            wx.CallAfter(
                self._finish_vacuum_room_refresh,
                self._get_saved_vacuum_rooms(entity_id),
                "No rooms came back from Roborock maps. Open Home Assistant Developer Tools and confirm roborock.get_maps returns rooms for this vacuum.",
                False,
            )
            return
        wx.CallAfter(self._finish_vacuum_room_refresh, rooms, f"Loaded and saved {len(rooms)} Roborock room{'s' if len(rooms) != 1 else ''}.", True)

    def _parse_roborock_rooms(self, data, entity_id):
        service_response = data.get("service_response") if isinstance(data, dict) else None
        if not isinstance(service_response, dict):
            return []
        vacuum_payload = service_response.get(entity_id) or next(iter(service_response.values()), {})
        maps = vacuum_payload.get("maps") if isinstance(vacuum_payload, dict) else []
        rooms = []
        for map_info in maps if isinstance(maps, list) else []:
            map_name = str(map_info.get("name") or "Current map")
            room_map = map_info.get("rooms") if isinstance(map_info.get("rooms"), dict) else {}
            for room_id, room_name in room_map.items():
                label = f"{room_name} ({room_id})" if map_name == "Current map" else f"{room_name} on {map_name} ({room_id})"
                try:
                    segment_id = int(room_id)
                except (TypeError, ValueError):
                    continue
                rooms.append({"label": label, "name": str(room_name), "map": map_name, "segment": segment_id})
        return sorted(rooms, key=lambda room: room["label"].lower())

    def _finish_vacuum_room_refresh(self, rooms, message, save=False):
        self.vacuum_rooms = rooms
        self.vacuum_room_list.Set([room["label"] for room in rooms])
        self.vacuum_room_status_txt.SetValue(message)
        if save:
            self._save_vacuum_rooms(self._selected_vacuum_entity_id(), rooms)
        self.tab_vacuum.Layout()
        self.tab_vacuum.FitInside()

    def _sanitize_vacuum_rooms(self, rooms):
        cleaned = []
        for room in rooms if isinstance(rooms, list) else []:
            if not isinstance(room, dict):
                continue
            try:
                segment = int(room.get("segment"))
            except (TypeError, ValueError):
                continue
            name = str(room.get("name") or f"Room {segment}")
            map_name = str(room.get("map") or "Current map")
            label = str(room.get("label") or (f"{name} ({segment})" if map_name == "Current map" else f"{name} on {map_name} ({segment})"))
            cleaned.append({"label": label, "name": name, "map": map_name, "segment": segment})
        return sorted(cleaned, key=lambda room: room["label"].lower())

    def _get_saved_vacuum_rooms(self, entity_id):
        if not entity_id:
            return []
        return self._sanitize_vacuum_rooms(self.config.get("vacuum_rooms", {}).get(entity_id, []))

    def _save_vacuum_rooms(self, entity_id, rooms):
        if not entity_id:
            return
        sanitized = self._sanitize_vacuum_rooms(rooms)
        self.config.setdefault("vacuum_rooms", {})[entity_id] = sanitized
        self.save_config()

    def on_vacuum_room_key_down(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_SPACE, ord(" ")):
            index = self.vacuum_room_list.GetSelection()
            if index != wx.NOT_FOUND:
                checked = not self.vacuum_room_list.IsChecked(index)
                self.vacuum_room_list.Check(index, checked)
                label = self.vacuum_room_list.GetString(index)
                wx.CallAfter(self._safe_speak, f"{label} {'checked' if checked else 'unchecked'}")
                return
        event.Skip()

    def on_vacuum_clean_selected_rooms(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        checked = list(self.vacuum_room_list.GetCheckedItems())
        if not checked:
            self.notify("Check one or more rooms first.", priority=10)
            return
        segments = [self.vacuum_rooms[index]["segment"] for index in checked if index < len(self.vacuum_rooms)]
        repeat = self.vacuum_room_repeat.GetValue()
        payload = {
            "entity_id": entity_id,
            "command": "app_segment_clean",
            "params": [{"segments": segments, "repeat": repeat}],
        }
        self._run_ha_service_async(
            "vacuum/send_command",
            payload,
            f"Sent room clean request for {len(segments)} room{'s' if len(segments) != 1 else ''}.",
        )

    def on_vacuum_send_command(self, event):
        entity_id = self._selected_vacuum_entity_id()
        command = self.vacuum_command_txt.GetValue().strip()
        params_text = self.vacuum_params_txt.GetValue().strip()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        if not command:
            self.notify("Enter a command name first.", priority=10)
            return
        payload = {"entity_id": entity_id, "command": command}
        if params_text:
            try:
                payload["params"] = json.loads(params_text)
            except json.JSONDecodeError as e:
                self.notify(f"Vacuum command parameters are not valid JSON: {e}", priority=10)
                return
        self._run_ha_service_async("vacuum/send_command", payload, f"Sent vacuum command {command}.")

    def on_vacuum_clean_areas(self, event):
        entity_id = self._selected_vacuum_entity_id()
        area_ids = [item.strip() for item in self.vacuum_area_ids_txt.GetValue().split(",") if item.strip()]
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        if not area_ids:
            self.notify("Enter one or more Home Assistant area IDs first.", priority=10)
            return
        self._run_ha_service_async(
            "vacuum/clean_area",
            {"entity_id": entity_id, "cleaning_area_id": area_ids},
            f"Sent clean area request for {len(area_ids)} area{'s' if len(area_ids) != 1 else ''}.",
        )

    def on_vacuum_goto_position(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        try:
            x = int(self.vacuum_goto_x_txt.GetValue().strip())
            y = int(self.vacuum_goto_y_txt.GetValue().strip())
        except ValueError:
            self.notify("Roborock go to coordinates must be whole numbers.", priority=10)
            return
        self._run_ha_service_async(
            "roborock/set_vacuum_goto_position",
            {"entity_id": entity_id, "x": x, "y": y},
            f"Sent Roborock go to position {x}, {y}.",
        )

    def _run_ha_service_async(self, service, payload, success_message):
        def worker():
            ok = self._call_ha_service_data(service, payload)
            if ok:
                wx.CallAfter(lambda: self.notify(success_message, priority=10))
                wx.CallAfter(lambda: wx.CallLater(1200, self.on_refresh_vacuum, None))
        safe_submit(worker)

    def setup_speed_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.StaticBox(self.tab_speed, label="Speed Diagnostics")
        bsizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.btn_refresh_speed = wx.Button(self.tab_speed, label="Refresh speed diagnostics", size=(-1, 40))
        self.btn_refresh_speed.Bind(wx.EVT_BUTTON, self.on_refresh_speed)
        self._describe_control(
            self.btn_refresh_speed,
            "Refresh speed diagnostics button. Reads the latest Viper log and summarizes doorbell, TTS, speaker, and chime timing.",
        )
        bsizer.Add(self.btn_refresh_speed, 0, wx.ALL | wx.EXPAND, 5)

        self.speed_status_txt = wx.TextCtrl(
            self.tab_speed,
            value="Press Refresh speed diagnostics to read the latest timing log.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 420),
        )
        self._describe_control(
            self.speed_status_txt,
            "Speed diagnostics results. This read only box summarizes recent timing measurements from the Viper log.",
        )
        bsizer.Add(self.speed_status_txt, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(bsizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_speed.SetSizer(sizer)

    def setup_ha_status_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.StaticBox(self.tab_ha_status, label="Home Assistant Status")
        bsizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.btn_refresh_ha_status = wx.Button(self.tab_ha_status, label="Check Home Assistant status", size=(-1, 40))
        self.btn_refresh_ha_status.Bind(wx.EVT_BUTTON, self.on_refresh_ha_status)
        self._describe_control(
            self.btn_refresh_ha_status,
            "Check Home Assistant status button. Tests the Home Assistant connection and verifies configured speaker and automation entities.",
        )
        bsizer.Add(self.btn_refresh_ha_status, 0, wx.ALL | wx.EXPAND, 5)

        self.ha_listener_status_txt = wx.StaticText(self.tab_ha_status, label="HA listener: starting")
        self._describe_control(
            self.ha_listener_status_txt,
            "Home Assistant listener status. This tells whether Viper is directly listening for Home Assistant state changes.",
        )
        bsizer.Add(self.ha_listener_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        self.ha_status_txt = wx.TextCtrl(
            self.tab_ha_status,
            value="Press Check Home Assistant status to test Home Assistant and configured entities.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 420),
        )
        self._describe_control(
            self.ha_status_txt,
            "Home Assistant status results. This read only box lists connection status, entity checks, and useful counts.",
        )
        bsizer.Add(self.ha_status_txt, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(bsizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_ha_status.SetSizer(sizer)

    def on_refresh_speed(self, event):
        self.speed_status_txt.SetValue("Reading speed log...")
        safe_submit(self._run_speed_diagnostics)

    def _run_speed_diagnostics(self):
        log_path = cfg.DATA_DIR / "viper_full_debug.log"
        if not log_path.exists():
            wx.CallAfter(self.speed_status_txt.SetValue, f"No speed log found at {log_path}.")
            return
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            summary = self._build_speed_summary(text)
        except Exception as e:
            summary = f"Could not read speed log: {e}"
        wx.CallAfter(self.speed_status_txt.SetValue, summary)

    def _latest_trace_block(self, lines, trace):
        if not trace:
            return []
        start = next((i for i, line in enumerate(lines) if trace in line and "webhook_received" in line), None)
        if start is None:
            start = next((i for i, line in enumerate(lines) if trace in line), None)
        if start is None:
            return []
        end = min(len(lines), start + 180)
        return lines[start:end]

    def _first_float(self, pattern, text):
        match = re.search(pattern, text)
        return float(match.group(1)) if match else None

    def _last_float(self, pattern, text):
        matches = re.findall(pattern, text)
        return float(matches[-1]) if matches else None

    def _format_seconds(self, value):
        return f"{value:.2f} seconds" if value is not None else "not found"

    def _median(self, values):
        if not values:
            return None
        values = sorted(values)
        mid = len(values) // 2
        if len(values) % 2:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2

    def _build_speed_summary(self, text):
        lines = text.splitlines()
        traces = []
        for trace in re.findall(r"trace=(doorbell-[a-z]+-\d+)", text):
            if trace not in traces:
                traces.append(trace)

        output = ["Speed Diagnostics", f"Log: {cfg.DATA_DIR / 'viper_full_debug.log'}", ""]
        latest = ""
        for trace in reversed(traces):
            if "fast_capture=" in "\n".join(self._latest_trace_block(lines, trace)):
                latest = trace
                break
        if not latest and traces:
            latest = traces[-1]
        if not latest:
            output.append("No doorbell traces found yet.")
        else:
            block_lines = self._latest_trace_block(lines, latest)
            block = "\n".join(block_lines)
            output.extend([
                f"Latest doorbell trace: {latest}",
                f"RTSP capture: {self._format_seconds(self._first_float(r'fast_capture=([0-9.]+)s', block))}",
                f"Total to vision verdict: {self._format_seconds(self._first_float(r'total_to_verdict=([0-9.]+)s', block))}",
                f"Audio submitted: {self._format_seconds(self._first_float(r'audio_notification_submitted=([0-9.]+)s', block))}",
                f"Doorbell TTS path: {self._format_seconds(self._last_float(r'TTS path for doorbell:unknown completed in ([0-9.]+)s', block))}",
                f"Home Assistant play request: {self._format_seconds(self._last_float(r'HA PLAY TIMING .* submitted in ([0-9.]+)s', block))}",
                f"Sonos play request: {self._format_seconds(self._last_float(r'SONOS DISPATCH TIMING - .* submitted in ([0-9.]+)s', block))}",
                f"Pushover sent: {'yes' if '[PUSHOVER]' in block else 'not found'}",
            ])
            engine_match = re.search(r"category=doorbell engine=([a-z]+)", block)
            if engine_match:
                output.append(f"Doorbell TTS engine: {engine_match.group(1)}")

        output.append("")
        output.append("Recent medians from the whole log:")
        recent_doorbell_blocks = []
        for trace in reversed(traces):
            block = "\n".join(self._latest_trace_block(lines, trace))
            if "fast_capture=" in block:
                recent_doorbell_blocks.append(block)
            if len(recent_doorbell_blocks) >= 8:
                break
        capture_values = [self._first_float(r"fast_capture=([0-9.]+)s", b) for b in recent_doorbell_blocks]
        verdict_values = [self._first_float(r"total_to_verdict=([0-9.]+)s", b) for b in recent_doorbell_blocks]
        capture_values = [v for v in capture_values if v is not None]
        verdict_values = [v for v in verdict_values if v is not None]
        gemini_tts_values = [float(v) for v in re.findall(r"Gemini TTS API response took: ([0-9.]+)s", text)][-20:]
        ha_play_values = [float(v) for v in re.findall(r"HA PLAY TIMING .* submitted in ([0-9.]+)s", text)][-20:]
        sonos_values = [float(v) for v in re.findall(r"SONOS .* TIMING - .* submitted in ([0-9.]+)s", text)][-20:]
        output.extend([
            f"Doorbell RTSP capture median: {self._format_seconds(self._median(capture_values))}",
            f"Doorbell verdict median: {self._format_seconds(self._median(verdict_values))}",
            f"Gemini TTS API median: {self._format_seconds(self._median(gemini_tts_values))}",
            f"HA play request median: {self._format_seconds(self._median(ha_play_values))}",
            f"Sonos play request median: {self._format_seconds(self._median(sonos_values))}",
        ])
        output.append("")
        output.append("Notes:")
        output.append("If doorbell TTS engine says google, the latest doorbell did not use Gemini voice.")
        output.append("If HA play is above 1 second but Sonos is fast, the delay is likely Home Assistant media service response time.")
        return "\n".join(output)

    def on_refresh_ha_status(self, event):
        self.ha_status_txt.SetValue("Checking Home Assistant...")
        safe_submit(self._run_ha_status_check)

    def _run_ha_status_check(self):
        try:
            summary = self._build_ha_status_summary()
        except Exception as e:
            summary = f"Home Assistant status check failed: {e}"
        wx.CallAfter(self.ha_status_txt.SetValue, summary)

    def _build_ha_status_summary(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        lines = [
            "Home Assistant Status",
            f"Host: {ha_settings.get('ha_ip') or 'not configured'}:{ha_settings.get('ha_port') or '8123'}",
            "",
        ]
        listener_status = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        lines.extend([
            f"Viper HA listener enabled: {'yes' if self.config.get('ha_listener_enabled', True) else 'no'}",
            f"Viper HA listener connected: {'yes' if listener_status.get('connected') else 'no'}",
            f"Viper HA listener last error: {listener_status.get('last_error') or 'none'}",
            "",
        ])

        connection = discovery.test_ha_connection(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        if not connection.get("ok"):
            lines.append(f"Connection: failed. {connection.get('message') or connection.get('error')}")
            return "\n".join(lines)
        lines.append(f"Connection: ok. Entities visible: {connection.get('entity_count', 'unknown')}")

        scan = discovery.discover_ha_entities(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=8,
        )
        if scan.get("ok"):
            categories = scan.get("categories", {})
            lines.extend([
                "",
                "Discovery counts:",
                f"Media players: {len(categories.get('media_players', []))}",
                f"Ring cameras: {len(categories.get('ring_cameras', []))}",
                f"Door sensors: {len(categories.get('door_sensors', []))}",
                f"Fridge sensors: {len(categories.get('fridge_sensors', []))}",
                f"Freezer sensors: {len(categories.get('freezer_sensors', []))}",
                f"Roborock candidates: {len(categories.get('roborock_entities', []))}",
            ])
        else:
            lines.append(f"Discovery: failed. {scan.get('message') or scan.get('error')}")

        triggers = self.config.get("doorbell_triggers", {})
        lines.extend(["", "Doorbell RTSP triggers:"])
        for side in ("front", "back"):
            trigger = triggers.get(side, {}) if isinstance(triggers, dict) else {}
            label = "Front" if side == "front" else "Back"
            lines.append(
                f"{label}: enabled={bool(trigger.get('enabled'))}, source={trigger.get('source') or 'ha_state'}, "
                f"trigger entity={trigger.get('trigger_entity_id') or 'not selected'}, "
                f"RTSP={'set' if trigger.get('rtsp_url') else 'missing'}"
            )

        entity_ids = []
        for name, speaker in self.config.get("speakers", {}).items():
            if speaker.get("type") in {"ha", "alexa"} and speaker.get("id"):
                entity_ids.append((f"Speaker {name}", speaker["id"]))
        for label, entity_id in [
            ("Fridge door", "binary_sensor.refrigerator_fridge_door"),
            ("Freezer door", "binary_sensor.refrigerator_freezer_door"),
            ("Water filter", "sensor.refrigerator_water_filter_usage"),
            ("Ice maker switch", "switch.refrigerator_cubed_ice"),
            ("Cinderella status", "sensor.cinderella_status"),
            ("Cinderella vacuum error", "sensor.cinderella_vacuum_error"),
            ("Cinderella dock error", "sensor.cinderella_dock_dock_error"),
            ("Cinderella mop drying", "binary_sensor.cinderella_dock_mop_drying"),
        ]:
            entity_ids.append((label, entity_id))

        lines.append("")
        lines.append("Entity checks:")
        seen = set()
        for label, entity_id in entity_ids:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            result = discovery.validate_entity_exists(
                entity_id,
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port"),
                timeout=5,
            )
            if result.get("ok") and result.get("exists"):
                state = result.get("entity", {}).get("state", "unknown")
                lines.append(f"{label}: found. {entity_id}. State: {state}")
            elif result.get("ok"):
                lines.append(f"{label}: missing. {entity_id}")
            else:
                lines.append(f"{label}: check failed. {entity_id}. {result.get('message') or result.get('error')}")

        lines.append("")
        lines.append("Configured speakers:")
        speakers = self.config.get("speakers", {})
        if not speakers:
            lines.append("No speakers configured in Viper.")
        else:
            for name, speaker in speakers.items():
                enabled = "enabled" if speaker.get("enabled", True) else "disabled"
                lines.append(f"{name}: {speaker.get('type')} {speaker.get('id')} {enabled}")
        return "\n".join(lines)

    # --- UI EVENT HANDLERS ---
    def on_refresh_edge_voices(self, event):
        self.notify("Upgrading TTS definitions...", priority=10)
        os.system("pip install --upgrade edge-tts")
        self.notify("Definitions updated. Restart app if voices don't appear.")

    def on_tts_engine_change(self, event):
        selected = self.tts_engine_choice.GetStringSelection()
        self.config["tts_engine"] = selected
        self.save_config()
        self._update_secondary_voice_ui()
        self.notify(f"Home Speakers TTS set to: {selected}")

    def on_secondary_voice_change(self, event):
        engine = self.tts_engine_choice.GetStringSelection()
        label = self.secondary_voice_choice.GetStringSelection()
        
        if engine == "Edge TTS (Natural)":
            self.config["edge_tts_voice"] = EDGE_VOICES[label]
            self.save_config()
            self.notify(f"Microsoft TTS Voice set to: {label}")

        elif engine == "Gemini TTS":
            self.config["gemini_tts_voice"] = GEMINI_TTS_VOICES[label]
            self.save_config()
            self.notify(f"Gemini TTS Voice set to: {label}")
            
        elif engine == "Google Cloud":
            self.config["google_tts_tld"] = DIALECTS[label]
            self.save_config()
            self.notify(f"Google Assistant Accent set to: {label}")

    def on_tts_keep_warm_change(self, event):
        if not hasattr(self, "default_tts_controls"):
            return
        enabled = self.default_tts_controls["keep_warm"].GetValue()
        self.config["gemini_tts_keep_warm"] = enabled
        self.config["gemini_tts_heartbeat_seconds"] = 240
        self.save_config()
        if enabled:
            audio.gemini_tts_connection.start()
            self.notify("Gemini TTS keep-warm enabled. Heartbeats may count as billable API requests.", priority=10)
        else:
            self.notify("Gemini TTS keep-warm disabled.", priority=10)
        self.gemini_warm_status.SetLabel(self._format_gemini_warm_status())

    def on_tts_warm_now(self, event):
        self.notify("Warming Gemini TTS now.", priority=10)
        def _warm():
            ok = audio.gemini_tts_connection.warm_once()
            wx.CallAfter(self.gemini_warm_status.SetLabel, self._format_gemini_warm_status())
            wx.CallAfter(self.notify, "Gemini TTS warmup complete." if ok else "Gemini TTS warmup failed.", 10)
        safe_submit(_warm)

    def _engine_value_from_choice(self, choice):
        mode = VOICE_BEHAVIOR_MODES.get(choice.GetStringSelection(), "natural_gemini")
        return {"natural_gemini": "gemini", "fast_reliable": "edge", "google_regular": "google", "offline_fallback": "sapi"}.get(mode, "gemini")

    def _read_tts_settings_controls(self, controls, include_use_defaults=False):
        settings = {
            "engine": self._engine_value_from_choice(controls["engine"]),
            "gemini_voice": GEMINI_TTS_VOICES.get(controls["gemini_voice"].GetStringSelection(), "Sulafat"),
            "edge_voice": EDGE_VOICES.get(controls["edge_voice"].GetStringSelection(), "en-US-AriaNeural"),
            "google_tld": DIALECTS.get(controls["google_tld"].GetStringSelection(), "com"),
            "sapi_voice_index": controls["sapi_voice"].GetSelection() if self.voice_list else 0,
            "speed": VOICE_SPEEDS.get(controls["speed"].GetStringSelection(), "normal"),
            "dynamic_mood": controls["dynamic_mood"].GetValue(),
        }
        if include_use_defaults:
            settings["use_defaults"] = controls["use_defaults"].GetValue()
        if "keep_warm" in controls:
            settings["keep_warm"] = controls["keep_warm"].GetValue()
        if "gemini_min_interval" in controls:
            settings["gemini_min_interval_seconds"] = controls["gemini_min_interval"].GetValue()
        return settings

    def _apply_tts_settings(self):
        defaults = self._read_tts_settings_controls(self.default_tts_controls)
        alerts = {
            category: self._read_tts_settings_controls(controls, include_use_defaults=True)
            for category, controls in self.alert_tts_controls.items()
        }
        self.config["tts_defaults"] = defaults
        self.config["tts_alerts"] = alerts
        self.config["tts_engine"] = audio._engine_to_tts_engine(defaults["engine"])
        self.config["gemini_tts_voice"] = defaults["gemini_voice"]
        self.config["edge_tts_voice"] = defaults["edge_voice"]
        self.config["google_tts_tld"] = defaults["google_tld"]
        self.config["local_voice_index"] = defaults["sapi_voice_index"]
        self.config["gemini_tts_keep_warm"] = defaults.get("keep_warm", False)
        self.config["gemini_tts_min_interval_seconds"] = defaults.get("gemini_min_interval_seconds", 0)
        self.config["gemini_tts_heartbeat_seconds"] = 240

    def on_save_voice_behavior(self, event):
        self._apply_tts_settings()
        self.save_config()
        if self.config["tts_defaults"].get("keep_warm"):
            audio.gemini_tts_connection.start()
        self.gemini_warm_status.SetLabel(self._format_gemini_warm_status())
        self._update_secondary_voice_ui()
        self.notify("TTS settings saved.", priority=10)

    def on_test_voice_behavior(self, event, category):
        self.on_save_voice_behavior(event)
        sample = {
            "doorbell": "Someone is at the front door.",
            "utilities": "The refrigerator door has been open for two minutes.",
            "manual": "This is a whole house test broadcast.",
        }.get(category, "This is a Viper Vision test.")
        self.notify(f"Testing {TTS_PROFILE_LABELS[category]}.", priority=3)
        safe_submit(audio.play_notification, category, sample)

    def on_voice_change(self, event):
        self.config["local_voice_index"] = self.voice_choice.GetSelection()
        self.save_config()
        self.notify(f"PC Voice set to: {self.voice_choice.GetStringSelection()}")

    def _safe_speak(self, msg):
        if self.sr:
            try: self.sr.output(msg)
            except Exception: pass

    def on_engine_change(self, event):
        self.config["vision_engine"] = self.engine_choice.GetStringSelection()
        self.save_config()
        self.notify(f"Vision Engine set to {self.config['vision_engine']}")

    def on_prompt_change(self, event):
        new_prompt = self.prompt_choice.GetStringSelection()
        self.config["active_prompt"] = new_prompt
        self.save_config()
        self.prompt_editor.SetValue(self.config["prompts"][new_prompt])
        self.notify(f"Loaded {new_prompt} profile")

    def on_save_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        txt = self.prompt_editor.GetValue().strip()
        if txt:
            self.config["prompts"][name] = txt
            self.save_config()
            self.notify(f"Saved {name}")

    def on_new_prompt(self, event):
        name = wx.GetTextFromUser("New Prompt Name:", "New Profile")
        if name and name not in self.config["prompts"]:
            self.config["prompts"][name] = "Analyze frames for security."
            self.config["active_prompt"] = name
            self.save_config()
            self.prompt_choice.Append(name)
            self.prompt_choice.SetStringSelection(name)
            self.prompt_editor.SetValue(self.config["prompts"][name])
            self.notify(f"Created {name}")

    def on_del_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        if len(self.config["prompts"]) > 1:
            del self.config["prompts"][name]
            new_a = list(self.config["prompts"].keys())[0]
            self.config["active_prompt"] = new_a
            self.save_config()
            self.prompt_choice.Clear()
            self.prompt_choice.AppendItems(list(self.config["prompts"].keys()))
            self.prompt_choice.SetStringSelection(new_a)
            self.prompt_editor.SetValue(self.config["prompts"][new_a])

    def refresh_speaker_list(self):
        self.speaker_list.Clear()
        for name, data in self.config.get("speakers", {}).items():
            idx = self.speaker_list.Append(f"{name} ({data['type'].upper()})")
            self.speaker_list.Check(idx, data.get("enabled", True))
            self.speaker_list.SetClientData(idx, name)
        self._refresh_tts_target_choices()

    def on_speaker_select(self, event):
        idx = event.GetInt()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetString(idx)
            state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
            self._sync_speaker_routing_controls()
            wx.CallAfter(self._safe_speak, f"{name}, {state}")

    def on_speaker_focus(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetString(idx)
            state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
            self._sync_speaker_routing_controls()
            wx.CallAfter(self._safe_speak, f"Speaker Targets. {name}, {state}")

    def on_speaker_toggle(self, event):
        idx = event.GetInt()
        name = self.speaker_list.GetClientData(idx)
        is_chk = self.speaker_list.IsChecked(idx)
        self.config["speakers"][name]["enabled"] = is_chk
        self.save_config()
        self._sync_speaker_routing_controls()
        status_msg = f"{name} {'enabled' if is_chk else 'disabled'}"
        self.notify(status_msg, priority=10)
        spk_type = self.config["speakers"][name]["type"]
        spk_id = self.config["speakers"][name]["id"]
        safe_submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)

    def _sync_speaker_routing_controls(self):
        idx = self.speaker_list.GetSelection()
        enabled = idx != wx.NOT_FOUND
        for chk in [self.chk_route_doorbell, self.chk_route_utilities, self.chk_route_fridge, self.chk_route_qhexempt]:
            chk.Enable(enabled)
        if not enabled:
            for chk in [self.chk_route_doorbell, self.chk_route_utilities, self.chk_route_fridge, self.chk_route_qhexempt]:
                chk.SetValue(False)
            return
        name = self.speaker_list.GetClientData(idx)
        spk = self.config["speakers"].get(name, {})
        self.chk_route_doorbell.SetValue(spk.get("doorbell", True))
        self.chk_route_utilities.SetValue(spk.get("utilities", True))
        self.chk_route_fridge.SetValue(spk.get("fridge", True))
        self.chk_route_qhexempt.SetValue(spk.get("quiet_hours_exempt", False))

    def on_speaker_route_change(self, event):
        idx = self.speaker_list.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        name = self.speaker_list.GetClientData(idx)
        spk = self.config["speakers"].setdefault(name, {})
        spk["doorbell"] = self.chk_route_doorbell.GetValue()
        spk["utilities"] = self.chk_route_utilities.GetValue()
        spk["fridge"] = self.chk_route_fridge.GetValue()
        spk["quiet_hours_exempt"] = self.chk_route_qhexempt.GetValue()
        self.save_config()
        self.notify(f"Saved routing for {name}", priority=10)

    def on_quiet_hours_change(self, event):
        self.config["quiet_hours_enabled"] = self.quiet_hours_enable_chk.GetValue()
        self.config["quiet_hours_start"] = self.quiet_hours_start_txt.GetValue().strip() or "22:00"
        self.config["quiet_hours_end"] = self.quiet_hours_end_txt.GetValue().strip() or "07:00"
        self.save_config()
        self.notify("Quiet hours saved.", priority=10)

    def _sync_quiet_hours_controls(self):
        if hasattr(self, "quiet_hours_enable_chk"):
            self.quiet_hours_enable_chk.SetValue(self.config.get("quiet_hours_enabled", False))
            self.quiet_hours_start_txt.SetValue(self.config.get("quiet_hours_start", "22:00"))
            self.quiet_hours_end_txt.SetValue(self.config.get("quiet_hours_end", "07:00"))


    def on_add_speaker(self, event):
        name = wx.GetTextFromUser("Speaker Name:", "Add")
        if name:
            dlg = wx.SingleChoiceDialog(self, "Type:", "Add", ["sonos", "ha", "alexa"])
            if dlg.ShowModal() == wx.ID_OK:
                spk_type = dlg.GetStringSelection()
                spk_id = wx.GetTextFromUser("ID/IP:", "Add")
                if spk_id:
                    self.config["speakers"][name] = {"id": spk_id, "type": spk_type, "enabled": True, "doorbell": True, "utilities": True, "fridge": True, "quiet_hours_exempt": False}
                    self.save_config()
                    self.refresh_speaker_list()
                    self._sync_speaker_routing_controls()
            dlg.Destroy()

    def on_rename_speaker(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            old = self.speaker_list.GetClientData(idx)
            new = wx.GetTextFromUser(f"Rename {old}:", "Rename", old)
            if new and new != old:
                d = self.config["speakers"].pop(old)
                self.config["speakers"][new] = d
                self.save_config()
                self.refresh_speaker_list()
                self._sync_speaker_routing_controls()

    def on_remove_speaker(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetClientData(idx)
            del self.config["speakers"][name]
            self.save_config()
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()

    def _populate_chimes(self):
        chime_files = ["(Default)"]
        if cfg.CHIMES_DIR.exists():
            for f in cfg.CHIMES_DIR.iterdir():
                if f.suffix.lower() in [".mp3", ".wav"]: chime_files.append(f.name)
        current_front = self.config.get("front_chime", "")
        current_back = self.config.get("back_chime", "")
        self.front_chime_choice.Set(chime_files)
        self.back_chime_choice.Set(chime_files)
        if current_front in chime_files: self.front_chime_choice.SetStringSelection(current_front)
        else: self.front_chime_choice.SetStringSelection("(Default)")
        if current_back in chime_files: self.back_chime_choice.SetStringSelection(current_back)
        else: self.back_chime_choice.SetStringSelection("(Default)")

    def on_test_front(self, event):
        f_val = self.front_chime_choice.GetStringSelection()
        safe_submit(audio.test_specific_chime, f_val, "front")
        self.notify("Testing front chime.", priority=10)

    def on_test_back(self, event):
        b_val = self.back_chime_choice.GetStringSelection()
        safe_submit(audio.test_specific_chime, b_val, "back")
        self.notify("Testing back chime.", priority=10)

    def on_refresh_chimes(self, event):
        self._populate_chimes()
        self.notify("Chimes folder refreshed.", priority=10)

    def on_save_chimes(self, event):
        f_val = self.front_chime_choice.GetStringSelection()
        b_val = self.back_chime_choice.GetStringSelection()
        self.config["front_chime"] = "" if f_val == "(Default)" else f_val
        self.config["back_chime"] = "" if b_val == "(Default)" else b_val
        self.save_config()
        self.notify("Custom chimes saved.", priority=10)

    def notify(self, text, priority=5, interrupt=False):
        timestamp = datetime.now().strftime("%H:%M")
        activity_logs.insert(0, {"time": timestamp, "msg": text})
        if len(activity_logs) > 15: activity_logs.pop()
        wx.CallAfter(self._safe_speak, text)
        if priority <= 3 or self.speech_queue.qsize() < 2:
            wx.CallAfter(self.status_display.SetValue, text)
        with self.speech_lock:
            if interrupt:
                while not self.speech_queue.empty():
                    try: self.speech_queue.get_nowait()
                    except Empty: break
            self._msg_counter += 1
            self.speech_queue.put((priority, time.time(), self._msg_counter, text))

    def speech_worker(self):
        while self.running:
            try:
                p, _, _, msg = self.speech_queue.get(timeout=0.02)
                with self.speech_lock: q = list(self.speech_queue.queue)
                if p > 5 and (any(x[0] <= 2 for x in q) or len(q) > 5): continue
                if msg: wx.CallAfter(self._safe_speak, msg)
            except Empty: continue

    def on_toggle_arm(self, event):
        self.is_armed = not self.is_armed
        self.config["is_armed"] = self.is_armed
        self.save_config()
        self.btn_arm.SetLabel("Disarm System" if self.is_armed else "Arm System")
        msg = f"Viper Vision {'Armed' if self.is_armed else 'Disarmed'}"
        self.notify(msg, priority=1, interrupt=True)
        safe_submit(audio.play_notification, "utilities", msg)

    def on_broadcast(self, event):
        msg = self.broadcast_input.GetValue().strip()
        if msg:
            self.broadcast_input.Clear()
            self.notify(f"Broadcasting: {msg}", priority=3, interrupt=True)
            safe_submit(audio.play_notification, "manual", msg)

    def on_api(self, event): safe_submit(self._run_api)
    def _run_api(self):
        try:
            if not cfg.API_LOG_PATH.exists():
                self.notify("API log is currently empty.", priority=10)
                return
            with open(cfg.API_LOG_PATH, "r", encoding="utf-8") as f: data = json.load(f)
            reqs = data.get("total_requests", 0)
            cost = (data.get("prompt_tokens", 0) * cfg.COST_PER_INPUT_TOKEN) + (data.get("response_tokens", 0) * cfg.COST_PER_OUTPUT_TOKEN)
            projected = (cost / max(1, datetime.now().day)) * 30
            msg = f"API: {reqs} requests. Spent: ${cost:.4f}. Projected: ${projected:.2f}"
            self.notify(msg, priority=10)
        except Exception: self.notify("API log unavailable.", priority=10)

    def on_home_assistant_setup(self, event):
        self.show_home_assistant_setup()

    def on_new_user_setup(self, event):
        self.show_new_user_setup_assistant()

    def show_new_user_setup_assistant(self):
        dlg = HomeAssistantFirstRunAssistantDialog(self)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def show_home_assistant_setup(self):
        use_env_prefill = not (self.first_run or self.clean_first_run_test)
        dlg = HomeAssistantSetupDialog(self, use_env_prefill=use_env_prefill)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                self.config = cfg.load_config()
        finally:
            dlg.Destroy()

    def on_generate_ha_package(self, event):
        options = ha_package.package_options_from_config(self.config)
        bundle = ha_package.write_package_bundle(options)
        self.notify(
            f"HA package generated: {bundle['package'].name}. See ha_packages folder.",
            priority=10,
        )

    def on_batt(self, event):
        self.notify("Checking battery levels...", priority=10)
        safe_submit(self._run_batt)

    def _run_batt(self):
        try:
            r = requests.get(f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/states", headers={"Authorization": f"Bearer {cfg.HA_TOKEN}"}, timeout=10)
            r.raise_for_status()
            stats = []
            for s in r.json():
                eid = s.get("entity_id", "").lower()
                if any(k in eid for k in cfg.BATTERY_KEYWORDS) and ("front" in eid or "back" in eid):
                    friendly = s["attributes"].get("friendly_name", eid)
                    try: 
                        val = float(s.get("state", 0))
                        stats.append(f"{friendly}: {val:.0f}%")
                    except Exception: pass
            msg = "Battery Levels: " + (", ".join(stats) if stats else "No sensors found.")
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)
        except Exception as e:
            self.notify(f"Battery query failed: {e}", priority=10)

    def on_filter(self, event):
        self.notify("Checking refrigerator filter...", priority=10)
        safe_submit(self._run_filter)

    def _run_filter(self):
        try:
            entity_id = "sensor.refrigerator_water_filter_usage"
            r = requests.get(
                f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {cfg.HA_TOKEN}"},
                timeout=10,
            )
            r.raise_for_status()
            s = r.json()
            friendly = s.get("attributes", {}).get("friendly_name", "Refrigerator Water filter usage")
            raw_state = str(s.get("state", "")).strip()
            msg = f"{friendly}: {raw_state} percent."
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)
        except Exception as e:
            self.notify(f"Filter query failed: {e}", priority=10)

    def on_run_diagnostics(self, event):
        self.notify("Running diagnostics...", priority=10)
        safe_submit(self._run_diagnostics)

    def _run_diagnostics(self):
        try:
            diag = _current_diagnostics(check_ha=True)
            text = diagnostics.diagnostics_text(diag)
            wx.CallAfter(self._show_text_dialog, "Viper Vision Diagnostics", text)
        except Exception as e:
            logging.exception("Diagnostics failed")
            wx.CallAfter(self.notify, f"Diagnostics failed: {e}", priority=10)

    def on_create_support_bundle(self, event):
        self.notify("Creating support bundle...", priority=10)
        safe_submit(self._run_support_bundle)

    def _run_support_bundle(self):
        try:
            diag = _current_diagnostics(check_ha=True)
            result = diagnostics.create_support_bundle(
                self.config,
                ha_listener_status=diag.get("ha_listener", {}),
                ha_connection=diag.get("ha_connection", {}),
            )
            wx.CallAfter(self.notify, f"Support bundle created: {result['path']}", priority=10)
            wx.CallAfter(self._show_text_dialog, "Support Bundle Created", f"Created:\n{result['path']}\n\nThis zip is redacted, but you should still review it before sharing.")
        except Exception as e:
            logging.exception("Support bundle failed")
            wx.CallAfter(self.notify, f"Support bundle failed: {e}", priority=10)

    def _show_text_dialog(self, title, text):
        dlg = wx.Dialog(self, title=title, size=(760, 560))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.TextCtrl(panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        box.SetName(f"{title}. Read only diagnostic text.")
        sizer.Add(box, 1, wx.ALL | wx.EXPAND, 10)
        close = wx.Button(panel, label="Close")
        close.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        sizer.Add(close, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        panel.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def on_scan_sonos(self, event):
        self.notify("Scanning network for Sonos...", priority=10)
        safe_submit(self._run_scan_sonos)

    def _run_scan_sonos(self):
        try:
            speakers = soco.discover()
            if not speakers:
                self.notify("No Sonos found.", priority=10)
                return
            existing_ips = [data["id"] for data in self.config.get("speakers", {}).values() if data["type"] == "sonos"]
            new_speakers = [s for s in speakers if s.ip_address not in existing_ips]
            self.notify(f"Scan complete. Found {len(speakers)} speakers.", priority=10)
            if new_speakers: wx.CallAfter(self._prompt_add_sonos_speakers, new_speakers)
        except Exception: self.notify("Sonos scan failed.", priority=10)

    def _prompt_add_sonos_speakers(self, new_speakers):
        choices = [f"{spk.player_name} ({spk.ip_address})" for spk in new_speakers]
        dlg = wx.MultiChoiceDialog(self, "Select speakers to add:", "New Sonos Found", choices)
        if dlg.ShowModal() == wx.ID_OK:
            added = 0
            for idx in dlg.GetSelections():
                spk = new_speakers[idx]
                name = spk.player_name + " Sonos"
                self.config.setdefault("speakers", {})[name] = {
                    "id": spk.ip_address,
                    "type": "sonos",
                    "enabled": True,
                    "doorbell": True,
                    "utilities": True,
                    "fridge": True,
                    "quiet_hours_exempt": False,
                }
                added += 1
            self.config = cfg.write_config(self.config)
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()
            self.notify(f"Added {added} Sonos speaker{'s' if added != 1 else ''}.", priority=10)
        dlg.Destroy()

    def on_scan_ha(self, event):
        self.notify("Scanning HA for speakers...", priority=10)
        safe_submit(self._run_scan_ha)

    def _run_scan_ha(self):
        result = discovery.discover_ha_entities(timeout=5)
        if not result.get("ok"):
            msg = result.get("message") or "HA scan failed."
            wx.CallAfter(self.notify, msg, priority=10)
            return

        ha_speakers = [
            {
                "entity_id": entity["entity_id"],
                "state": entity.get("state"),
                "attributes": {
                    "friendly_name": entity.get("friendly_name", entity["entity_id"]),
                    "platform": entity.get("platform"),
                },
            }
            for entity in result["categories"]["media_players"]
        ]
        existing_ids = [data["id"] for data in self.config.get("speakers", {}).values()]
        new_speakers = [s for s in ha_speakers if s["entity_id"] not in existing_ids]
        if new_speakers:
            wx.CallAfter(self._prompt_add_ha_speakers, new_speakers)
        else:
            wx.CallAfter(self.notify, "All HA speakers configured.", priority=10)

    def _prompt_add_ha_speakers(self, new_speakers):
        choices = [f"{s.get('attributes', {}).get('friendly_name', s['entity_id'])} ({s['entity_id']})" for s in new_speakers]
        dlg = wx.MultiChoiceDialog(self, "Select HA speakers to add:", "New HA Found", choices)
        if dlg.ShowModal() == wx.ID_OK:
            added = 0
            for idx in dlg.GetSelections():
                spk = new_speakers[idx]
                raw_name = spk.get("attributes", {}).get("friendly_name", spk["entity_id"].replace("media_player.", ""))
                spk_type = "alexa" if "echo" in raw_name.lower() or "alexa" in spk["entity_id"].lower() else "ha"
                self.config.setdefault("speakers", {})[f"{raw_name} ({spk_type.upper()})"] = {
                    "id": spk["entity_id"],
                    "type": spk_type,
                    "enabled": True,
                    "doorbell": True,
                    "utilities": True,
                    "fridge": True,
                    "quiet_hours_exempt": False,
                }
                added += 1
            self.config = cfg.write_config(self.config)
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()
            self.notify(f"Added {added} Home Assistant speaker{'s' if added != 1 else ''}.", priority=10)
        dlg.Destroy()


    # ── Fridge Tab ────────────────────────────────────────────────────────────

    def setup_fridge_tab(self):
        """Fridge & Freezer door channel settings — speak / chime / silent
        with independent chime file selection per state."""
        outer = wx.BoxSizer(wx.VERTICAL)

        FRIDGE_CHANNELS = [
            ("fridge_open",    "Fridge Door Opens"),
            ("fridge_closed",  "Fridge Door Closes"),
            ("freezer_open",   "Freezer Door Opens"),
            ("freezer_closed", "Freezer Door Closes"),
        ]

        chime_list   = self._get_chime_list()
        channels_cfg = self.config.get("broadcast_channels", {})
        self._fridge_controls = {}  # ch_key -> {"mode": Choice, "chime": Choice}

        for ch_key, ch_label in FRIDGE_CHANNELS:
            ch_data = channels_cfg.get(ch_key, {"mode": "chime", "chime": ""})

            sbox   = wx.StaticBox(self.tab_fridge, label=ch_label)
            ssizer = wx.StaticBoxSizer(sbox, wx.VERTICAL)

            # Mode row
            mode_row = wx.BoxSizer(wx.HORIZONTAL)
            mode_row.Add(
                wx.StaticText(self.tab_fridge, label="When this happens:"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5,
            )
            mode_choice = wx.Choice(
                self.tab_fridge, choices=["speak", "chime", "silent"]
            )
            mode_choice.SetStringSelection(ch_data.get("mode", "chime"))
            mode_choice.Bind(
                wx.EVT_CHOICE,
                lambda e, k=ch_key: self._on_fridge_channel_change(k),
            )
            mode_row.Add(mode_choice, 0, wx.ALL, 5)
            ssizer.Add(mode_row, 0, wx.EXPAND)

            # Chime file row
            chime_row = wx.BoxSizer(wx.HORIZONTAL)
            chime_row.Add(
                wx.StaticText(self.tab_fridge, label="Play chime:"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5,
            )
            chime_choice = wx.Choice(self.tab_fridge, choices=chime_list)
            current_chime = ch_data.get("chime", "")
            chime_choice.SetStringSelection(
                current_chime if current_chime in chime_list else "(Default)"
            )
            chime_choice.Bind(
                wx.EVT_CHOICE,
                lambda e, k=ch_key: self._on_fridge_channel_change(k),
            )
            chime_row.Add(chime_choice, 1, wx.ALL, 5)
            ssizer.Add(chime_row, 0, wx.EXPAND)

            # Test button — plays the currently selected chime on all speakers
            btn_test = wx.Button(self.tab_fridge, label=f"Test {ch_label} Chime", size=(-1, 32))
            btn_test.Bind(
                wx.EVT_BUTTON,
                lambda e, k=ch_key: self._on_test_fridge_chime(k),
            )
            ssizer.Add(btn_test, 0, wx.EXPAND | wx.ALL, 5)

            self._fridge_controls[ch_key] = {
                "mode":  mode_choice,
                "chime": chime_choice,
                "test":  btn_test,
            }
            outer.Add(ssizer, 0, wx.ALL | wx.EXPAND, 10)


        ice_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_ice_on = wx.Button(self.tab_fridge, label="Turn Ice Maker On", size=(-1, 40))
        self.btn_ice_on.Bind(wx.EVT_BUTTON, self.on_ice_maker_on)
        self.btn_ice_off = wx.Button(self.tab_fridge, label="Turn Ice Maker Off", size=(-1, 40))
        self.btn_ice_off.Bind(wx.EVT_BUTTON, self.on_ice_maker_off)
        ice_row.Add(self.btn_ice_on, 1, wx.ALL, 5)
        ice_row.Add(self.btn_ice_off, 1, wx.ALL, 5)
        outer.Add(ice_row, 0, wx.ALL | wx.EXPAND, 10)

        btn_save = wx.Button(self.tab_fridge, label="Save All Fridge Settings", size=(-1, 40))
        btn_save.Bind(wx.EVT_BUTTON, self.on_save_fridge_settings)
        outer.Add(btn_save, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_fridge.SetSizer(outer)

    def _get_chime_list(self):
        files = ["(Default)"]
        if cfg.CHIMES_DIR.exists():
            files += [
                f.name for f in cfg.CHIMES_DIR.iterdir()
                if f.suffix.lower() in (".mp3", ".wav")
            ]
        return files

    def _on_fridge_channel_change(self, ch_key):
        """Live-save a single channel when either its control changes."""
        ctrl = self._fridge_controls.get(ch_key, {})
        if not ctrl:
            return
        chime = ctrl["chime"].GetStringSelection()
        channels = self.config.setdefault("broadcast_channels", {})
        channels[ch_key] = {
            "mode":  ctrl["mode"].GetStringSelection(),
            "chime": "" if chime == "(Default)" else chime,
        }
        self.save_config()

    def on_save_fridge_settings(self, event):
        channels = self.config.setdefault("broadcast_channels", {})
        for ch_key, ctrl in self._fridge_controls.items():
            chime = ctrl["chime"].GetStringSelection()
            channels[ch_key] = {
                "mode":  ctrl["mode"].GetStringSelection(),
                "chime": "" if chime == "(Default)" else chime,
            }
        self.config["broadcast_channels"] = channels
        self.save_config()
        self.notify("Fridge & Freezer settings saved.", priority=10)

    def _sync_fridge_controls(self):
        """Called after web UI saves to keep GUI in sync."""
        channels_cfg = self.config.get("broadcast_channels", {})
        chime_list   = self._get_chime_list()
        for ch_key, ctrl in self._fridge_controls.items():
            ch_data = channels_cfg.get(ch_key, {"mode": "chime", "chime": ""})
            ctrl["mode"].SetStringSelection(ch_data.get("mode", "chime"))
            current_chime = ch_data.get("chime", "")
            ctrl["chime"].Set(chime_list)
            ctrl["chime"].SetStringSelection(
                current_chime if current_chime in chime_list else "(Default)"
            )


    def _on_test_fridge_chime(self, ch_key: str):
        """Play the chime currently selected for the given channel on all speakers."""
        ctrl  = self._fridge_controls.get(ch_key, {})
        chime = ctrl["chime"].GetStringSelection() if ctrl else ""
        chime_file = "" if chime == "(Default)" else chime
        safe_submit(audio.play_broadcast_chime, chime_file)
        label = ch_key.replace("_", " ").title()
        self.notify(f"Testing {label} chime.", priority=10)

    def _call_ha_service(self, domain_service: str, entity_id: str):
        """Call a Home Assistant service for a single entity."""
        return self._call_ha_service_data(domain_service, {"entity_id": entity_id})

    def _call_ha_service_data(self, domain_service: str, data: dict):
        """Call a Home Assistant service with arbitrary JSON data."""
        entity_id = (data or {}).get("entity_id", "Home Assistant")
        try:
            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                raise RuntimeError("Home Assistant host or token is missing.")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            requests.post(
                f"http://{ha_ip}:{ha_port}/api/services/{domain_service}",
                headers=headers,
                json=data or {},
                timeout=10,
            ).raise_for_status()
            return True
        except Exception as e:
            self.notify(f"HA service failed for {entity_id}: {e}", priority=10)
            return False

    def _call_ha_service_response(self, domain_service: str, data: dict):
        """Call a Home Assistant service that returns response data."""
        entity_id = (data or {}).get("entity_id", "Home Assistant")
        try:
            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                raise RuntimeError("Home Assistant host or token is missing.")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            response = requests.post(
                f"http://{ha_ip}:{ha_port}/api/services/{domain_service}?return_response",
                headers=headers,
                json=data or {},
                timeout=15,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            return {"ok": True, "data": payload}
        except Exception as e:
            return {"ok": False, "message": f"HA service failed for {entity_id}: {e}"}

    def on_ice_maker_on(self, event):
        """Force the ice maker on and enable the helper so the 5-second auto-off
        automation does not shut it back off."""
        ok_helper = self._call_ha_service("input_boolean/turn_on", cfg.ICE_MAKER_KEEP_ON_ENTITY)
        ok_switch = self._call_ha_service("switch/turn_on", cfg.ICE_MAKER_SWITCH_ENTITY)
        if ok_helper and ok_switch:
            msg = "Ice maker turned on with refill override enabled."
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)

    def on_ice_maker_off(self, event):
        """Turn the ice maker off and clear the helper override."""
        ok_switch = self._call_ha_service("switch/turn_off", cfg.ICE_MAKER_SWITCH_ENTITY)
        ok_helper = self._call_ha_service("input_boolean/turn_off", cfg.ICE_MAKER_KEEP_ON_ENTITY)
        if ok_switch and ok_helper:
            msg = "Ice maker turned off and refill override cleared."
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)


    def on_minimize(self, event):
        if isinstance(event, wx.CloseEvent) and event.CanVeto(): event.Veto()
        wx.CallLater(500, self.Hide)

    def on_quit(self, event):
        self.running = False
        is_shutting_down.set()
        mark_app_clean_shutdown()
        if hasattr(self, "ha_listener"):
            self.ha_listener.stop()
        executor.shutdown(wait=False)
        self.tb_icon.RemoveIcon()
        self.tb_icon.Destroy()
        self.Destroy()
        os._exit(0)

if __name__ == "__main__":
    LOG_PATH = cfg.DATA_DIR / "viper_full_debug.log"
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[file_handler, logging.StreamHandler()], force=True)
    install_crash_hooks()
    mark_app_running()
    logging.info("===== VIPER VISION STARTING =====")
    cfg.ensure_default_assets()
    audio.startup_cleanup()
    threading.Thread(target=audio.start_local_server, daemon=True).start()
    threading.Thread(target=run_flask_server, daemon=True).start()
    # Flask routes all guard on 'dash_app is None', so no fixed sleep is needed.
    gui_app = wx.App(False)
    dash_app = ViperDashboard()
    gui_app.MainLoop()
