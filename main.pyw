import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import time
import zipfile
import webbrowser
from datetime import datetime
from queue import Empty, PriorityQueue, Queue
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import wx
import wx.adv
try:
    import wx.html2 as wxhtml2
except Exception:
    wxhtml2 = None
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import viper_audio as audio
import viper_broadcast as broadcast
import viper_cinderella as cinderella
import viper_config as cfg
import viper_discovery as discovery
import viper_ha_addons as ha_addons
import viper_diagnostics as diagnostics
import viper_ha_listener as ha_listener
import viper_health
import viper_ha_vm as ha_vm
import viper_ha_vm_delegates as ha_vm_delegates
import viper_ha_recovery as ha_recovery
import viper_matter
import viper_remote_api
import viper_remote_web
import viper_system_health
from viper_ui_dashboard import DashboardTabMixin
from viper_ui_device_tools import DeviceToolsMixin
from viper_ui_doorbell import DoorbellTabMixin
from viper_ui_fridge import FridgeTabMixin
from viper_ui_hvac import HvacTabMixin
from viper_ui_lifecycle import AppLifecycleMixin
from viper_ui_prompts import PromptEditorMixin
from viper_ui_speakers import SpeakerManagementMixin
from viper_ui_setup_status import SetupStatusMixin
from viper_ui_tts import DIALECTS, EDGE_VOICES, GEMINI_TTS_VOICES, TtsSettingsMixin
from viper_ui_vacuum import VacuumTabMixin
from viper_ui_diagnostics import DiagnosticsTabMixin
import viper_ui_common as ui_common
from viper_ui_setup_wizard import (
    HomeAssistantFirstRunAssistantDialog,
    HomeAssistantSetupDialog,
    HomeAssistantVmResourcesDialog,
    RingMqttLoginDialog,
    ViperSetupWizardDialog,
)
import viper_vacuum as vacuum
import viper_vision as vision
from viper_runtime import executor, is_shutting_down, mark_startup_phase, record_event, safe_submit

mark_startup_phase("main imports complete")
AccessibleStatusText = ui_common.AccessibleStatusText

HIDDEN_VACUUM_SETTING_SUFFIXES = vacuum.HIDDEN_VACUUM_SETTING_SUFFIXES
VACUUM_CLEANING_MODES = vacuum.VACUUM_CLEANING_MODES
VACUUM_CLEANING_MODE_ORDER = vacuum.VACUUM_CLEANING_MODE_ORDER
vacuum_basic_actions_for_state = vacuum.vacuum_basic_actions_for_state
vacuum_cleaning_mode_service_calls = vacuum.vacuum_cleaning_mode_service_calls
_ha_domain_from_entity_id = vacuum.ha_domain_from_entity_id
_web_entity_name = vacuum.web_entity_name
_web_short_entity_label = vacuum.web_short_entity_label
_web_vacuum_tokens = vacuum.web_vacuum_tokens
_web_looks_like_roborock = vacuum.web_looks_like_roborock
_web_show_vacuum_setting = vacuum.web_show_vacuum_setting
_is_hidden_vacuum_setting_entity_id = vacuum.is_hidden_vacuum_setting_entity_id
_normalize_vacuum_cleaning_mode = vacuum.normalize_vacuum_cleaning_mode

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
        open_url(path.resolve().as_uri())
        return True
    logging.warning("Help file not found for topic=%s path=%s", topic, path)
    return False


# --- HOME ASSISTANT VM / INSTALL ENGINE ---
OFFICIAL_LINKS = ha_vm.OFFICIAL_LINKS
RING_MQTT_ADDON_SLUG = ha_vm.RING_MQTT_ADDON_SLUG
HA_VM_NAME = ha_vm.HA_VM_NAME
HA_VM_BASE_DIR = ha_vm.HA_VM_BASE_DIR
HA_VM_DIR = ha_vm.HA_VM_DIR
HAOS_RELEASE_API = ha_vm.HAOS_RELEASE_API
SUPPORTED_HA_VM_ARCHITECTURES = ha_vm.SUPPORTED_HA_VM_ARCHITECTURES
DEFAULT_HA_VM_RAM_MB = ha_vm.DEFAULT_HA_VM_RAM_MB
MIN_HA_VM_RAM_MB = ha_vm.MIN_HA_VM_RAM_MB
MAX_HA_VM_RAM_MB = ha_vm.MAX_HA_VM_RAM_MB
DEFAULT_HA_VM_DISK_GB = ha_vm.DEFAULT_HA_VM_DISK_GB
MIN_HA_VM_DISK_GB = ha_vm.MIN_HA_VM_DISK_GB
MAX_HA_VM_DISK_GB = ha_vm.MAX_HA_VM_DISK_GB
SUPPORT_EMAIL = ha_vm.SUPPORT_EMAIL
SETUP_PROGRESS_PHASES = ha_vm.SETUP_PROGRESS_PHASES

def _call_ha_vm_with_overrides(name, override_names, *args, **kwargs):
    old_values = {}
    try:
        for override_name in override_names:
            value = globals().get(override_name)
            if value is None or getattr(value, '_ha_vm_delegate', None) == override_name:
                continue
            old_values[override_name] = getattr(ha_vm, override_name)
            setattr(ha_vm, override_name, value)
        return getattr(ha_vm, name)(*args, **kwargs)
    finally:
        for override_name, old_value in old_values.items():
            setattr(ha_vm, override_name, old_value)

ha_vm_delegates.install_simple_delegates(globals(), ha_vm)

def build_ha_install_preflight_summary(resources):
    ram_mb = normalize_ha_vm_ram_mb((resources or {}).get("ram_mb"))
    disk_gb = normalize_ha_vm_disk_gb((resources or {}).get("disk_gb"))
    platform_status = get_ha_vm_platform_status()
    vbox = get_virtualbox_status()
    virtualization = get_windows_virtualization_status()
    drive = get_ha_vm_drive_space_status(disk_gb)
    lines = [
        "Before Viper installs Home Assistant, review this summary.",
        "",
        f"RAM for Home Assistant: {ram_mb} MB.",
        f"Disk space for Home Assistant: {disk_gb} GB.",
        "CPU: 2 virtual CPUs.",
        f"Install folder: {HA_VM_DIR}",
        "",
        platform_status.get("message", ""),
        f"VirtualBox: {'found' if vbox.get('installed') else 'not found'}. {vbox.get('version') or vbox.get('message') or ''}",
        drive.get("message", ""),
    ]
    if virtualization.get("is_windows"):
        lines.append(virtualization.get("message", ""))
        if virtualization.get("needs_attention"):
            lines.append("Stability note: Optimize Windows For VirtualBox is recommended before relying on this VM long-term.")
    lines.extend([
        "",
        "Viper will download the official Home Assistant OS image, create a VirtualBox VM named Home Assistant, start it, and wait for first boot.",
        "The first boot can take up to 25 minutes while Home Assistant downloads and prepares Core.",
        "",
        "Continue with these settings?",
    ])
    return {
        "ok": bool(platform_status.get("supported") and vbox.get("installed")),
        "drive_ok": drive.get("ok"),
        "message": "\n".join(str(line) for line in lines if line is not None),
        "resources": {"ram_mb": ram_mb, "disk_gb": disk_gb},
    }
build_ha_install_preflight_summary._ha_vm_delegate = "build_ha_install_preflight_summary"

def wait_for_home_assistant_first_boot(*args, **kwargs):
    kwargs.setdefault("core_ready_func", _check_home_assistant_core_ready)
    return ha_vm.wait_for_home_assistant_first_boot(*args, **kwargs)
wait_for_home_assistant_first_boot._ha_vm_delegate = "wait_for_home_assistant_first_boot"

def _create_ha_vm_from_vdi(*args, **kwargs):
    return _call_ha_vm_with_overrides("_create_ha_vm_from_vdi", ["_run_vbox", "_run_vbox_progress", "_choose_bridged_adapter"], *args, **kwargs)
_create_ha_vm_from_vdi._ha_vm_delegate = "_create_ha_vm_from_vdi"

def install_home_assistant_vm_from_image(*args, **kwargs):
    return _call_ha_vm_with_overrides("install_home_assistant_vm_from_image", ["_run_vbox", "_run_vbox_progress", "_vbox_vm_exists", "_extract_haos_disk", "_resize_virtualbox_disk", "_create_ha_vm_from_vdi", "_import_ha_ova", "_choose_bridged_adapter"], *args, **kwargs)
install_home_assistant_vm_from_image._ha_vm_delegate = "install_home_assistant_vm_from_image"

def download_and_install_home_assistant_vm(*args, **kwargs):
    return _call_ha_vm_with_overrides("download_and_install_home_assistant_vm", ["_vbox_vm_exists", "get_latest_haos_virtualbox_asset", "download_file", "install_home_assistant_vm_from_image"], *args, **kwargs)
download_and_install_home_assistant_vm._ha_vm_delegate = "download_and_install_home_assistant_vm"

def start_home_assistant_vm(*args, **kwargs):
    return _call_ha_vm_with_overrides("start_home_assistant_vm", ["_vbox_vm_exists", "_run_vbox_progress"], *args, **kwargs)
start_home_assistant_vm._ha_vm_delegate = "start_home_assistant_vm"

def _get_flask_secret_key():
    configured = os.getenv("VIPER_SECRET_KEY")
    if configured:
        return configured
    key_path = cfg.DATA_DIR / "remote_session_secret.txt"
    try:
        if key_path.exists():
            value = key_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        key_path.write_text(value, encoding="utf-8")
        return value
    except Exception:
        logging.warning("Using temporary Flask session secret because persistent storage is unavailable.", exc_info=True)
        return secrets.token_hex(32)


app = Flask(__name__, template_folder=_resolve_template_dir())
app.secret_key = _get_flask_secret_key()
dash_app = None
viper_remote_api.register_control_routes(app, lambda: dash_app)
activity_logs = []

RUNNING_SENTINEL = cfg.DATA_DIR / "viper_running.sentinel"
CRASH_LOG_PATH = cfg.DATA_DIR / "viper_last_crash.txt"
HA_INSTALL_LOG_PATH = cfg.DATA_DIR / "viper_ha_install.log"
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


_single_instance_mutex = None


def focus_existing_viper_window():
    """Bring the already-running Viper window forward when a second launch occurs."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Viper Vision Control Panel")
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        logging.debug("Could not focus existing Viper window", exc_info=True)
        return False


def acquire_single_instance_lock():
    """Prevent two Viper GUI/listener instances from handling the same HA event."""
    global _single_instance_mutex
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "ViperVisionSingleInstance")
        if not mutex:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _single_instance_mutex = mutex
        return True
    except Exception:
        logging.debug("Single-instance lock failed open", exc_info=True)
        return True

CINDERELLA_STATUS_EVENT_MAP = cinderella.CINDERELLA_STATUS_EVENT_MAP

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


def start_plumbing_monitor():
    threading.Thread(target=monitor_plumbing, name="ViperPlumbingMonitor", daemon=True).start()

# --- WEB HELPERS ---


def ensure_cinderella_message_config(config_obj: dict) -> dict:
    default_messages = cfg.get_default_config().get("cinderella_messages", {})
    return cinderella.ensure_message_config(config_obj, default_messages, cfg._deep_merge)


def choose_cinderella_message(event: str, error: str = "", source: str = "vacuum") -> str:
    if dash_app is None:
        config_obj = cfg.load_config()
    else:
        config_obj = dash_app.config
    ensure_cinderella_message_config(config_obj)
    return cinderella.choose_message(config_obj, event, error=error, source=source)

def _json_or_redirect(message: str, ok: bool = True, status_code: int = 200):
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"
    if wants_json:
        return jsonify({"ok": ok, "message": message}), status_code
    flash(message)
    return redirect(url_for("remote_ui"))

_resolve_channel_settings = broadcast.resolve_channel_settings
_normalize_broadcast_mode = broadcast.normalize_broadcast_mode
_normalize_broadcast_message_text = broadcast.normalize_broadcast_message_text
_infer_fridge_channel_from_message = broadcast.infer_fridge_channel_from_message
_resolve_broadcast_channel = broadcast.resolve_broadcast_channel


def _notify_dashboard_async(*args, **kwargs):
    try:
        wx.CallAfter(dash_app.notify, *args, **kwargs)
    except AssertionError:
        dash_app.notify(*args, **kwargs)


def _dispatch_broadcast_message(raw_message: str, push: bool = False, channel: str = "") -> dict:
    return broadcast.dispatch_broadcast_message(
        raw_message,
        config=dash_app.config if dash_app is not None else {},
        notify=_notify_dashboard_async,
        submit=safe_submit,
        play_notification=audio.play_notification,
        play_broadcast_chime=audio.play_broadcast_chime,
        send_text_push=audio._send_text_pushover,
        system_ready=bool(dash_app is not None and not is_shutting_down.is_set()),
        push=push,
        channel=channel,
    )


def _broadcast_message(raw_message: str, push: bool = False, channel: str = ""):
    result = _dispatch_broadcast_message(raw_message, push=push, channel=channel)
    return _json_or_redirect(
        result.get("message", ""),
        ok=result.get("ok", True),
        status_code=result.get("status_code", 200),
    )

def _doorbell_rtsp_for_key(key: str):
    settings = cfg.get_resolved_doorbell_settings(include_env=True)
    if key == "back":
        return settings.get("rtsp_back") or ""
    return settings.get("rtsp_front") or ""


def _doorbell_video_settings(config_data=None):
    return vision.normalize_video_analysis_settings(config_data or (dash_app.config if dash_app else None))


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
        record_event("doorbell", f"{side.title()} doorbell event routed from Home Assistant.", location=location)
        status, code = _handle_doorbell(location, action.get("rtsp_url") or "", side)
        logging.info("[HA LISTENER] doorbell action side=%s code=%s status=%s", side, code, status)
    elif action_type == "cinderella":
        record_event("vacuum", f"Vacuum event routed: {action.get('event', '') or 'unknown'}.")
        _dispatch_cinderella_event(action.get("event", ""), action.get("error", ""), action.get("source", "vacuum"))
    elif action_type == "broadcast":
        message = (action.get("message") or "").strip()
        channel = (action.get("channel") or "default").strip()
        if message:
            record_event("broadcast", f"Home Assistant broadcast routed to {channel}.")
            logging.info("[HA LISTENER] broadcast channel=%s message=%r", channel, message)
            result = _dispatch_broadcast_message(message, channel=channel)
            logging.info(
                "[HA LISTENER] broadcast result ok=%s path=%s resolved_channel=%s message=%r",
                result.get("ok"),
                result.get("path", ""),
                result.get("resolved_channel", ""),
                result.get("message", ""),
            )


# ==========================================
# FLASK ROUTES & WEBHOOKS
# ==========================================

def _current_diagnostics(*, check_ha=False):
    return viper_remote_web._current_diagnostics(check_ha=check_ha)


def _current_ha_states(timeout=8):
    return viper_remote_web._current_ha_states(timeout=timeout)


def _save_current_ha_snapshot():
    return viper_remote_web._save_current_ha_snapshot()


def _build_web_vacuum_context():
    return viper_remote_web._build_web_vacuum_context()


def _cached_web_vacuum_controls(entity_id):
    return viper_remote_web._cached_web_vacuum_controls(entity_id)


viper_remote_web.register_remote_routes(
    app,
    lambda: dash_app,
    handle_doorbell=_handle_doorbell,
    doorbell_rtsp_for_key=_doorbell_rtsp_for_key,
    broadcast_message=_broadcast_message,
    json_or_redirect=_json_or_redirect,
    ensure_cinderella_message_config=ensure_cinderella_message_config,
    choose_cinderella_message=choose_cinderella_message,
    normalize_broadcast_mode=_normalize_broadcast_mode,
    doorbell_video_settings=_doorbell_video_settings,
    activity_logs=activity_logs,
    edge_voices=EDGE_VOICES,
    gemini_tts_voices=GEMINI_TTS_VOICES,
    dialects=DIALECTS,
)


def run_flask_server():
    from waitress import serve
    serve(app, host="0.0.0.0", port=cfg.FLASK_PORT, threads=8)

class ViperTaskBarIcon(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
        self.SetIcon(icon, "Viper Vision")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_restore)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self.on_restore)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_restore)
        self.Bind(wx.adv.EVT_TASKBAR_RIGHT_DCLICK, self.on_restore)

    def on_restore(self, event):
        wx.CallAfter(self._restore_frame)
        wx.CallLater(150, self._restore_frame)
        wx.CallLater(500, self._restore_frame)

    def _restore_frame(self):
        if hasattr(self.frame, "restore_from_tray_focus"):
            self.frame.restore_from_tray_focus()
            return
        self.frame.Show(True)
        if self.frame.IsIconized():
            self.frame.Iconize(False)
        if hasattr(self.frame, "Restore"):
            try:
                self.frame.Restore()
            except Exception:
                pass
        self.frame.SetFocus()
        self.frame.Raise()
        try:
            self.frame.RequestUserAttention(wx.USER_ATTENTION_INFO)
        except Exception:
            pass
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = self.frame.GetHandle()
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            logging.debug("Could not force Viper window to foreground from tray.", exc_info=True)


class ViperDashboard(DashboardTabMixin, DeviceToolsMixin, DoorbellTabMixin, PromptEditorMixin, SpeakerManagementMixin, SetupStatusMixin, TtsSettingsMixin, FridgeTabMixin, HvacTabMixin, VacuumTabMixin, DiagnosticsTabMixin, AppLifecycleMixin, wx.Frame):
    def __init__(self):
        mark_startup_phase("dashboard init started")
        super().__init__(None, title="Viper Vision Control Panel", size=(800, 750))
        global dash_app
        dash_app = self

        self.running = True
        self.first_run = not cfg.CONFIG_FILE.exists()
        self.clean_first_run_test = os.getenv("VIPER_CLEAN_FIRST_RUN_TEST", "").strip().lower() in {"1", "true", "yes", "on"}
        self.config = cfg.load_config()
        ensure_cinderella_message_config(self.config)
        self.is_armed = self.config.get("is_armed", True)
        self.setup_events = []
        self.last_setup_status = ""
        self.last_video_analysis = {}
        self.last_video_followup_decision = {}
        self._last_focus_snapshot_log = {}
        self._last_smartthings_reload_notice_at = 0.0
        self._startup_health_checked = False
        self.startup_api_status = {"checked": False, "running": False, "message": "Startup API checks have not run yet."}
        record_event("startup", "Viper dashboard is opening.")
        cfg.sync_globals_from_config()

        self.sr = None
        threading.Thread(target=self._init_screen_reader_bridge, name="ViperScreenReaderBridge", daemon=True).start()

        self.speech_queue = PriorityQueue()
        self.speech_lock = threading.Lock()
        self._msg_counter = 0
        self.ha_listener = ha_listener.HomeAssistantEventListener(
            self._ha_listener_config,
            _handle_ha_listener_action,
            self._on_ha_listener_status,
            is_shutting_down,
        )

        self.panel = wx.Panel(self)
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tb_icon = ViperTaskBarIcon(self)

        self.status_display = AccessibleStatusText(
            self.panel,
            value="Viper Vision Online",
            style=wx.ALIGN_CENTER,
        )
        self.status_display.SetBackgroundColour(self.panel.GetBackgroundColour())
        font = self.status_display.GetFont()
        font.SetPointSize(12)
        self.status_display.SetFont(font)
        self.main_sizer.Add(self.status_display, 0, wx.ALL | wx.EXPAND, 10)

        self.setup_notebook()
        mark_startup_phase("dashboard controls ready")

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
        self.Bind(wx.EVT_ACTIVATE, self.on_dashboard_activate)
        self.Center()
        self.Show()
        mark_startup_phase("main window shown")
        if self.should_auto_open_setup_wizard():
            wx.CallLater(650, self.show_initial_setup_wizard)
        if previous_run_unclean:
            wx.CallAfter(
                self.notify,
                "Viper may not have shut down cleanly last time. Use Run Diagnostics or Create Support Bundle if anything seems wrong.",
                priority=10,
            )

        threading.Thread(target=self.speech_worker, daemon=True).start()
        self.ha_listener.start()
        record_event("home assistant", "HA listener start requested.")
        mark_startup_phase("background workers started")
        wx.CallLater(3500, self.refresh_hvac_status)
        wx.CallLater(6000, self.run_startup_api_checks)
        wx.CallLater(20000, self.run_startup_health_self_test)
        self._ha_address_recovery_stop = threading.Event()
        threading.Thread(target=self._ha_address_recovery_worker, name="ViperHAAddressRecovery", daemon=True).start()

    def _init_screen_reader_bridge(self):
        try:
            from accessible_output2.outputs import auto
            bridge = auto.Auto()
            self.sr = bridge
            logging.info("Screen Reader Bridge established.")
        except Exception as e:
            self.sr = None
            logging.error(f"Screen Reader Bridge failed: {e}")

    def should_auto_open_setup_wizard(self):
        if self.first_run or self.clean_first_run_test:
            return True
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        api_settings = cfg.get_api_settings(self.config, include_env=True)
        return not ha_settings.get("ha_token") or not api_settings.get("gemini_api_key")

    def show_initial_setup_wizard(self):
        logging.info(
            "[SETUP] Auto-opening setup wizard. first_run=%s clean_first_run_test=%s",
            self.first_run,
            self.clean_first_run_test,
        )
        self.Show(True)
        self.restore_startup_focus()
        self.on_open_setup_wizard(None)

    def _setup_window_attrs(self):
        return ("_setup_wizard_dialog", "_ha_setup_dialog", "_ha_server_assistant_dialog")

    def _active_setup_window(self):
        for attr in self._setup_window_attrs():
            dlg = getattr(self, attr, None)
            if self._is_live_window(dlg):
                return dlg
        return None

    def _enter_setup_window_mode(self, setup_window=None):
        """Keep Alt+Tab focused on setup, not the main control panel."""
        try:
            window = setup_window if self._is_live_window(setup_window) else self._active_setup_window()
            if window is not None:
                window.Show(True)
                window.Raise()
            if self.IsShown():
                self.Show(False)
            self._log_setup_focus_snapshot("enter_setup_window_mode")
            if window is not None:
                wx.CallAfter(self._restore_setup_window_focus, window)
                wx.CallLater(150, self._restore_setup_window_focus, window)
        except Exception:
            logging.debug("Could not enter setup window mode.", exc_info=True)

    def _leave_setup_window_mode(self):
        try:
            if self._active_setup_window() is not None:
                return
            self.Show(True)
            self.restore_main_window_focus()
            self._log_setup_focus_snapshot("leave_setup_window_mode")
        except Exception:
            logging.debug("Could not leave setup window mode.", exc_info=True)

    def _show_control_panel_for_setup_action(self):
        """Show the control panel only when a wizard action intentionally opens a main app area."""
        try:
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
        except Exception:
            logging.debug("Could not show control panel for setup action.", exc_info=True)

    def on_dashboard_activate(self, event):
        try:
            if event.GetActive():
                setup_window = self._active_setup_window()
                if setup_window is not None:
                    logging.info("[FOCUS] Dashboard activated while setup is open; returning focus to setup window.")
                    wx.CallAfter(self._restore_setup_window_focus, setup_window)
                    wx.CallLater(120, self._restore_setup_window_focus, setup_window)
                    return
        except Exception:
            logging.debug("Could not redirect dashboard activation to setup window.", exc_info=True)
        event.Skip()

    def _restore_setup_window_focus(self, setup_window=None):
        window = setup_window if self._is_live_window(setup_window) else self._active_setup_window()
        if window is None:
            return False
        try:
            if window.IsIconized():
                window.Iconize(False)
            if hasattr(window, "Restore"):
                window.Restore()
            window.Show(True)
            window.Enable(True)
            window.Raise()
            try:
                if self.IsShown():
                    self.Show(False)
            except Exception:
                pass
            if hasattr(window, "force_initial_focus"):
                window._initial_focus_given = False
                window.force_initial_focus()
            else:
                window.SetFocus()
            self._log_setup_focus_snapshot("restore_setup_window_focus")
            return True
        except Exception:
            logging.debug("Could not restore setup window focus.", exc_info=True)
            return False

    def _close_setup_surfaces(self, keep=None):
        for attr in self._setup_window_attrs():
            if attr == keep:
                continue
            dlg = getattr(self, attr, None)
            if dlg is None:
                continue
            try:
                if hasattr(dlg, "_destroyed"):
                    dlg._destroyed = True
                dlg.Destroy()
            except RuntimeError:
                pass
            except Exception:
                logging.debug("Could not close setup surface %s.", attr, exc_info=True)
            setattr(self, attr, None)

    def _is_live_window(self, window):
        if window is None:
            return False
        try:
            return bool(window.GetHandle())
        except RuntimeError:
            return False
        except Exception:
            return False

    def _log_setup_focus_snapshot(self, context):
        try:
            now = time.monotonic()
            if not hasattr(self, "_last_focus_snapshot_log"):
                self._last_focus_snapshot_log = {}
            last = self._last_focus_snapshot_log.get(context, 0)
            if now - last < 10:
                return
            self._last_focus_snapshot_log[context] = now
            active = []
            disabled = []
            dashboard_state = f"dashboard:shown={self.IsShown()}:enabled={self.IsEnabled()}"
            for attr in self._setup_window_attrs():
                dlg = getattr(self, attr, None)
                if not self._is_live_window(dlg):
                    continue
                title = dlg.GetTitle() if hasattr(dlg, "GetTitle") else attr
                enabled = dlg.IsEnabled() if hasattr(dlg, "IsEnabled") else None
                shown = dlg.IsShown() if hasattr(dlg, "IsShown") else None
                active.append(f"{attr}:{title}:shown={shown}:enabled={enabled}")
                if enabled is False:
                    disabled.append(title)
            focus = wx.Window.FindFocus()
            focus_desc = "none"
            if focus is not None:
                focus_desc = f"{focus.__class__.__name__}:{focus.GetName() if hasattr(focus, 'GetName') else ''}"
            foreground = ""
            foreground_enabled = None
            if platform.system().lower() == "windows":
                try:
                    import ctypes

                    user32 = ctypes.windll.user32
                    hwnd = user32.GetForegroundWindow()
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, buf, 512)
                    foreground = buf.value
                    foreground_enabled = bool(user32.IsWindowEnabled(hwnd))
                except Exception:
                    foreground = "unavailable"
            logging.info(
                "[FOCUS SNAPSHOT] %s foreground=%r foreground_enabled=%s focus=%r setup_windows=%s disabled_setup_windows=%s",
                context,
                foreground,
                foreground_enabled,
                focus_desc,
                [dashboard_state] + active,
                disabled,
            )
        except Exception:
            logging.debug("Could not log setup focus snapshot.", exc_info=True)

    def _ha_listener_config(self):
        config = dict(getattr(self, "config", {}) or cfg.load_config())
        ha_settings = cfg.get_ha_settings(config, include_env=True)
        config["ha_ip"] = ha_settings.get("ha_ip") or config.get("ha_ip") or ""
        config["ha_port"] = ha_settings.get("ha_port") or config.get("ha_port") or "8123"
        config["ha_token"] = ha_settings.get("ha_token") or config.get("ha_token") or ""
        return config

    def _ha_address_recovery_worker(self):
        # Give the app and first-run setup time to settle before probing HA.
        stop_event = getattr(self, "_ha_address_recovery_stop", None)
        if stop_event is None:
            return
        if stop_event.wait(90):
            return
        while not stop_event.is_set() and not is_shutting_down.is_set():
            try:
                self.check_and_repair_home_assistant_address()
            except Exception:
                logging.debug("Home Assistant address recovery check failed.", exc_info=True)
            stop_event.wait(300)

    def check_and_repair_home_assistant_address(self, *, announce=False):
        settings = cfg.get_ha_settings(self.config, include_env=True)
        token = (settings.get("ha_token") or "").strip()
        host = (settings.get("ha_ip") or "").strip()
        port = (settings.get("ha_port") or "8123").strip()
        if not token:
            return {"ok": False, "changed": False, "message": "Home Assistant token is missing; address recovery skipped."}
        if host:
            health = discovery.check_ha_core_health(ha_ip=host, ha_port=port, token=token, timeout=3)
            if health.get("ok"):
                return {"ok": True, "changed": False, "message": f"Home Assistant is still reachable at {host}:{port}."}
            logging.info("[HA RECOVERY] Saved Home Assistant address failed health check: %s", health.get("message"))
        found = discovery.find_home_assistant(token=token, seed_host="", seed_port=port or "8123", timeout=2)
        if not found.get("ok") or found.get("auth_error") == "bad_token":
            message = found.get("message") or "Home Assistant address recovery did not find a reachable server."
            if announce:
                wx.CallAfter(self.notify, message, 10)
            return {"ok": False, "changed": False, "message": message, "result": found}
        new_host = (found.get("ha_ip") or "").strip()
        new_port = (found.get("ha_port") or port or "8123").strip()
        if not new_host:
            return {"ok": False, "changed": False, "message": "Home Assistant was found, but no IP was returned.", "result": found}
        changed = new_host != host or new_port != port
        if changed:
            old = f"{host}:{port}" if host else "not saved"
            self.config["ha_ip"] = new_host
            self.config["ha_port"] = new_port
            self.save_config()
            cfg.sync_globals_from_config()
            message = f"Home Assistant moved from {old} to {new_host}:{new_port}. Viper updated the saved address."
            logging.info("[HA RECOVERY] %s", message)
            if announce:
                wx.CallAfter(self.notify, message, 10)
            return {"ok": True, "changed": True, "message": message, "result": found}
        return {"ok": True, "changed": False, "message": f"Home Assistant found at {new_host}:{new_port}.", "result": found}

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            page = "index"
            if hasattr(self, "notebook"):
                current = self.notebook.GetPageText(self.notebook.GetSelection())
                page = {
                    "Dashboard": "index",
                    "Doorbell Vision": "ring-setup",
                    "Speakers & Audio": "speakers",
                    "Home Devices": "scenarios",
                    "Home Assistant": "setup",
                    "Diagnostics": "troubleshooting",
                    "Advanced": "ha-install",
                }.get(current, "index")
            if not open_help(page):
                self.notify("Help file not found.", priority=10)
            return
        event.Skip()

    def _on_ha_listener_status(self, status):
        self._maybe_notify_health_recovery(status)
        if not status.get("running"):
            label = "HA listener: stopped"
        elif status.get("connected"):
            label = f"HA listener: connected to {status.get('last_host') or 'Home Assistant'}"
        else:
            err = status.get("last_error") or "connecting"
            label = f"HA listener: not connected. {err}"
        if label != getattr(self, "_last_ha_listener_label", ""):
            self._last_ha_listener_label = label
            record_event("home assistant", label)
        if hasattr(self, "ha_listener_status_txt"):
            wx.CallAfter(self.ha_listener_status_txt.SetLabel, label)
        if hasattr(self, "ha_connection_status_txt"):
            wx.CallAfter(self.ha_connection_status_txt.SetValue, viper_system_health.short_ha_status(status))
        if hasattr(self, "system_health_txt"):
            wx.CallAfter(self.refresh_system_health_display)

    def _maybe_notify_health_recovery(self, status):
        try:
            reload_at = float(status.get("last_smartthings_reload_at") or 0)
        except (TypeError, ValueError):
            reload_at = 0
        if reload_at <= 0 or reload_at == getattr(self, "_last_smartthings_reload_notice_at", 0):
            return
        self._last_smartthings_reload_notice_at = reload_at
        repeats = int(status.get("repeated_smartthings_reloads_24h") or 0)
        result = status.get("last_smartthings_reload_result") or "reload attempted"
        message = f"Viper reloaded Refrigerator SmartThings because door events looked stale. Result: {result}"
        if repeats >= 3:
            message += f" Warning: this has happened {repeats} times in the last 24 hours."
        wx.CallAfter(self.notify, message, priority=1, interrupt=False, speak=False)
        record_event("health repair", message)
        logging.warning("[HEALTH] %s", message)

    def run_startup_health_self_test(self):
        if getattr(self, "_startup_health_checked", False) or is_shutting_down.is_set():
            return
        self._startup_health_checked = True
        try:
            diag = self._current_diagnostics(check_ha=False)
            critical = diag.get("critical_workflows") or {}
            overall = critical.get("overall") or "UNKNOWN"
            if overall != "OK":
                issues = [
                    f"{item.get('name')}: {item.get('message')}"
                    for item in critical.get("items", [])
                    if item.get("status") in {"BROKEN", "SUSPICIOUS"}
                ]
                first = issues[0] if issues else "Open Diagnostics for details."
                message = f"Startup health check needs attention: {overall}. {first}"
                self.notify(message, priority=10, speak=False)
                record_event("startup health", message)
            else:
                record_event("startup health", "Startup health check passed.")
            logging.info("[HEALTH] Startup self-test overall=%s items=%s", overall, critical.get("items", []))
            wx.CallAfter(self.refresh_system_health_display)
        except Exception:
            logging.debug("Startup health self-test failed.", exc_info=True)
            record_event("startup health", "Startup health check failed to complete.")

    def restore_startup_focus(self):
        try:
            if any(self._is_live_window(getattr(self, attr, None)) for attr in self._setup_window_attrs()):
                logging.info("[FOCUS] Startup focus skipped because setup dialog is open.")
                return
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
            self._nudge_windows_foreground()
        except Exception:
            logging.debug("Could not restore startup window.", exc_info=True)

    def _preferred_startup_focus(self):
        try:
            if hasattr(self, "notebook") and hasattr(self, "tab_dash"):
                for idx in range(self.notebook.GetPageCount()):
                    if self.notebook.GetPage(idx) is self.tab_dash:
                        self.notebook.SetSelection(idx)
                        break
            for name in ("notebook", "btn_arm", "broadcast_input"):
                ctrl = getattr(self, name, None)
                if self._control_accepts_keyboard_focus(ctrl):
                    return ctrl
        except Exception:
            logging.debug("Could not choose startup focus target.", exc_info=True)
        return getattr(self, "notebook", None) or self

    def _control_accepts_keyboard_focus(self, ctrl):
        if ctrl is None or not hasattr(ctrl, "SetFocus"):
            return False
        try:
            if hasattr(ctrl, "IsShownOnScreen") and not ctrl.IsShownOnScreen():
                return False
            if hasattr(ctrl, "IsEnabled") and not ctrl.IsEnabled():
                return False
            if hasattr(ctrl, "CanAcceptFocus") and not ctrl.CanAcceptFocus():
                return False
            if hasattr(ctrl, "CanAcceptFocusFromKeyboard") and not ctrl.CanAcceptFocusFromKeyboard():
                return False
        except RuntimeError:
            return False
        except Exception:
            logging.debug("Could not inspect focus target.", exc_info=True)
            return False
        return True

    def _focus_control_for_screen_reader(self, focus_target, context):
        if focus_target is None:
            focus_target = self
        if hasattr(focus_target, "SetFocusFromKbd"):
            try:
                focus_target.SetFocusFromKbd()
                logging.info("[FOCUS] %s focused with SetFocusFromKbd: %s", context, self._describe_focus_target(focus_target))
                return True
            except Exception:
                pass
        if hasattr(focus_target, "SetFocus"):
            focus_target.SetFocus()
            logging.info("[FOCUS] %s focused with SetFocus: %s", context, self._describe_focus_target(focus_target))
            return True
        return False

    def _describe_focus_target(self, ctrl):
        try:
            label = ctrl.GetLabel() if hasattr(ctrl, "GetLabel") else ""
            name = ctrl.GetName() if hasattr(ctrl, "GetName") else ""
            return f"{ctrl.__class__.__name__} name={name!r} label={label!r}"
        except Exception:
            return repr(ctrl)

    def restore_main_window_focus(self):
        self.record_setup_event("focus_restore_start", "Restoring main Viper window focus after setup.")
        self._log_setup_focus_snapshot("before_restore_main_window_focus")
        self._restore_main_window_focus_once()
        wx.CallLater(100, self._restore_main_window_focus_once)
        wx.CallLater(300, self._restore_main_window_focus_once)
        wx.CallLater(700, self._restore_main_window_focus_once)

    def restore_from_tray_focus(self):
        logging.info("[FOCUS] Restoring Viper from system tray.")
        self._log_setup_focus_snapshot("before_restore_from_tray")
        self._restore_from_tray_focus_once()
        wx.CallLater(100, self._restore_from_tray_focus_once)
        wx.CallLater(300, self._restore_from_tray_focus_once)
        wx.CallLater(700, self._restore_from_tray_focus_once)

    def _restore_from_tray_focus_once(self):
        try:
            if self._active_setup_window() is not None:
                self._restore_setup_window_focus()
                return
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
            try:
                self.RequestUserAttention(wx.USER_ATTENTION_INFO)
            except Exception:
                pass
            self._nudge_windows_foreground()
            target = self._preferred_focus_after_tray_restore()
            self._focus_control_for_screen_reader(target, "tray_restore")
        except Exception:
            logging.debug("Could not restore Viper focus from tray.", exc_info=True)

    def _preferred_focus_after_tray_restore(self):
        try:
            if hasattr(self, "notebook"):
                self.notebook.GetPage(self.notebook.GetSelection())
                if self._control_accepts_keyboard_focus(self.notebook):
                    return self.notebook
        except Exception:
            logging.debug("Could not choose preferred focus target after tray restore.", exc_info=True)
        return getattr(self, "notebook", None) or self

    def on_notebook_page_changed(self, event):
        try:
            notebook = event.GetEventObject() if hasattr(event, "GetEventObject") else None
            if notebook is not None:
                wx.CallAfter(self._ensure_selected_notebook_page, notebook)
        except Exception:
            logging.debug("Could not schedule lazy tab setup.", exc_info=True)
        if hasattr(event, "Skip"):
            event.Skip()

    def _setup_tab_once(self, key, setup_func, page=None):
        if key in getattr(self, "_lazy_setup_done", set()):
            return
        self._lazy_setup_done.add(key)
        started = time.perf_counter()
        setup_func()
        if page is not None:
            try:
                page.Layout()
                if hasattr(page, "FitInside"):
                    page.FitInside()
            except Exception:
                logging.debug("Could not layout lazy tab %s.", key, exc_info=True)
        logging.info("[STARTUP] Built lazy tab %s in %.3fs", key, time.perf_counter() - started)

    def _lazy_tab_prewarm_items(self):
        return [
            ("doorbell", self.setup_doorbell_tab, self.tab_doorbell),
            ("hvac", self.setup_hvac_tab, self.tab_hvac),
            ("fridge", self.setup_fridge_tab, self.tab_fridge),
            ("vacuum", self.setup_vacuum_tab, self.tab_vacuum),
            ("setup", self.setup_setup_tab, self.tab_setup),
            ("diagnostics", self.setup_diagnostics_tab, self.tab_diagnostics_overview),
            ("ha_status", self.setup_ha_status_tab, self.tab_ha_status),
            ("recent_events", self.setup_recent_events_tab, self.tab_recent_events),
            ("speed", self.setup_speed_tab, self.tab_speed),
            ("tts", self.setup_tts_config_tab, self.tab_tts),
            ("devices", self.setup_devices_tab, self.tab_dev),
            ("prompts", self.setup_prompt_editor_tab, self.tab_prompts),
            ("utils", self.setup_utils_tab, self.tab_util),
        ]

    def _prewarm_lazy_tabs_in_background(self):
        if getattr(self, "_lazy_prewarm_running", False):
            return
        self._lazy_prewarm_queue = list(self._lazy_tab_prewarm_items())
        self._lazy_prewarm_running = True
        wx.CallLater(75, self._prewarm_next_lazy_tab)

    def _prewarm_next_lazy_tab(self):
        try:
            queue = getattr(self, "_lazy_prewarm_queue", [])
            while queue:
                key, setup_func, page = queue.pop(0)
                self._lazy_prewarm_queue = queue
                if key in getattr(self, "_lazy_setup_done", set()):
                    continue
                self._setup_tab_once(key, setup_func, page)
                wx.CallLater(175, self._prewarm_next_lazy_tab)
                return
        except Exception:
            logging.debug("Could not prewarm lazy tabs.", exc_info=True)
        self._lazy_prewarm_running = False
        logging.info("[STARTUP] Lazy tab background prewarm complete.")

    def _ensure_selected_notebook_page(self, notebook=None):
        try:
            notebook = notebook or getattr(self, "notebook", None)
            if notebook is None:
                return
            selection = notebook.GetSelection()
            if selection == wx.NOT_FOUND:
                return
            page = notebook.GetPage(selection)
            self._ensure_tab_page(page)
        except Exception:
            logging.debug("Could not build selected tab lazily.", exc_info=True)

    def _ensure_tab_page(self, page):
        if page is getattr(self, "tab_dash", None):
            self._setup_tab_once("dash", self.setup_dash_tab, page)
        elif page is getattr(self, "tab_doorbell", None):
            self._setup_tab_once("doorbell", self.setup_doorbell_tab, page)
        elif page is getattr(self, "tab_prompts", None):
            self._setup_tab_once("prompts", self.setup_prompt_editor_tab, page)
        elif page is getattr(self, "tab_audio_shell", None):
            self._ensure_selected_notebook_page(self.audio_notebook)
        elif page is getattr(self, "tab_tts", None):
            self._setup_tab_once("tts", self.setup_tts_config_tab, page)
        elif page is getattr(self, "tab_dev", None):
            self._setup_tab_once("devices", self.setup_devices_tab, page)
        elif page is getattr(self, "tab_devices_shell", None):
            self._ensure_selected_notebook_page(self.devices_notebook)
        elif page is getattr(self, "tab_fridge", None):
            self._setup_tab_once("fridge", self.setup_fridge_tab, page)
        elif page is getattr(self, "tab_hvac", None):
            self._setup_tab_once("hvac", self.setup_hvac_tab, page)
        elif page is getattr(self, "tab_vacuum", None):
            self._setup_tab_once("vacuum", self.setup_vacuum_tab, page)
        elif page is getattr(self, "tab_setup", None):
            self._setup_tab_once("setup", self.setup_setup_tab, page)
        elif page is getattr(self, "tab_diagnostics_shell", None):
            self._ensure_selected_notebook_page(self.diagnostics_notebook)
        elif page is getattr(self, "tab_diagnostics_overview", None):
            self._setup_tab_once("diagnostics", self.setup_diagnostics_tab, page)
        elif page is getattr(self, "tab_recent_events", None):
            self._setup_tab_once("recent_events", self.setup_recent_events_tab, page)
        elif page is getattr(self, "tab_speed", None):
            self._setup_tab_once("speed", self.setup_speed_tab, page)
        elif page is getattr(self, "tab_ha_status", None):
            self._setup_tab_once("ha_status", self.setup_ha_status_tab, page)
        elif page is getattr(self, "tab_util", None):
            self._setup_tab_once("utils", self.setup_utils_tab, page)

    def _focus_notebook_tab_for_screen_reader(self, notebook):
        try:
            if notebook is not None and self._control_accepts_keyboard_focus(notebook):
                self._focus_control_for_screen_reader(notebook, "tab_change")
        except Exception:
            logging.debug("Could not focus newly selected notebook tab.", exc_info=True)

    def _restore_main_window_focus_once(self):
        try:
            if self._active_setup_window() is not None:
                self._restore_setup_window_focus()
                return
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
            self._nudge_windows_foreground()
            self._focus_preferred_child_after_setup()
            self.record_setup_event("focus_restore_attempt", "Focus restore attempt completed.")
            if hasattr(self, "tb_icon"):
                wx.CallAfter(self.tb_icon._restore_frame)
                wx.CallLater(150, self.tb_icon._restore_frame)
                wx.CallLater(225, self._focus_preferred_child_after_setup)
        except Exception:
            logging.debug("Could not restore main Viper window focus.", exc_info=True)

    def _focus_preferred_child_after_setup(self):
        focus_target = self._preferred_focus_after_setup()
        if focus_target is None:
            focus_target = getattr(self, "notebook", None) or self
        self._focus_control_for_screen_reader(focus_target, "setup_restore")

    def _preferred_focus_after_setup(self):
        try:
            if hasattr(self, "notebook") and hasattr(self, "tab_setup"):
                for idx in range(self.notebook.GetPageCount()):
                    if self.notebook.GetPage(idx) is self.tab_setup:
                        self.notebook.SetSelection(idx)
                        break
            for name in ("btn_setup_wizard", "btn_choose_setup_speakers", "btn_ha_setup", "btn_new_user_setup", "btn_diagnostics", "notebook"):
                ctrl = getattr(self, name, None)
                if self._control_accepts_keyboard_focus(ctrl):
                    return ctrl
        except Exception:
            logging.debug("Could not choose preferred focus target after setup.", exc_info=True)
        return None

    def _select_book_page(self, book, index):
        try:
            book.SetSelection(index)
        except Exception:
            logging.debug("Could not switch page.", exc_info=True)

    def _nudge_windows_foreground(self):
        if platform.system().lower() != "windows":
            return
        try:
            import ctypes

            hwnd = self.GetHandle()
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SW_RESTORE = 9
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            logging.debug("Could not nudge Viper window to Windows foreground.", exc_info=True)

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
        self._lazy_setup_done = set()
        self.notebook = wx.Notebook(self.panel)
        self.tab_dash = wx.Panel(self.notebook)
        self.tab_doorbell = wx.Panel(self.notebook)
        self.tab_prompts = wx.ScrolledWindow(self.notebook)
        self.tab_prompts.SetScrollRate(0, 20)
        self.tab_audio_shell = wx.Panel(self.notebook)
        self.audio_notebook = wx.Notebook(self.tab_audio_shell)
        self.tab_tts = wx.ScrolledWindow(self.audio_notebook)
        self.tab_tts.SetScrollRate(0, 20)
        self.tab_dev = wx.Panel(self.audio_notebook)
        self.tab_devices_shell = wx.Panel(self.notebook)
        self.devices_notebook = wx.Notebook(self.tab_devices_shell)
        self.tab_fridge = wx.ScrolledWindow(self.devices_notebook)
        self.tab_fridge.SetScrollRate(0, 20)
        self.tab_hvac = wx.ScrolledWindow(self.devices_notebook)
        self.tab_hvac.SetScrollRate(0, 20)
        self.tab_vacuum = wx.ScrolledWindow(self.devices_notebook)
        self.tab_vacuum.SetScrollRate(0, 20)
        self.tab_setup = wx.Panel(self.notebook)
        self.tab_diagnostics_shell = wx.Panel(self.notebook)
        self.diagnostics_notebook = wx.Notebook(self.tab_diagnostics_shell)
        self.tab_diagnostics_overview = wx.Panel(self.diagnostics_notebook)
        self.tab_recent_events = wx.ScrolledWindow(self.diagnostics_notebook)
        self.tab_recent_events.SetScrollRate(0, 20)
        self.tab_speed = wx.ScrolledWindow(self.diagnostics_notebook)
        self.tab_speed.SetScrollRate(0, 20)
        self.tab_ha_status = wx.ScrolledWindow(self.diagnostics_notebook)
        self.tab_ha_status.SetScrollRate(0, 20)
        self.tab_util = wx.Panel(self.notebook)

        self.notebook.AddPage(self.tab_dash, "Dashboard")
        self.notebook.AddPage(self.tab_doorbell, "Doorbell Vision")
        self.notebook.AddPage(self.tab_prompts, "AI Descriptions")
        self.notebook.AddPage(self.tab_audio_shell, "Speakers & Audio")
        self.notebook.AddPage(self.tab_devices_shell, "Home Devices")
        self.notebook.AddPage(self.tab_setup, "Home Assistant")
        self.notebook.AddPage(self.tab_diagnostics_shell, "Diagnostics")
        self.notebook.AddPage(self.tab_util, "Advanced")
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_notebook_page_changed)

        audio_sizer = wx.BoxSizer(wx.VERTICAL)
        self.audio_notebook.AddPage(self.tab_tts, "Voice Behavior")
        self.audio_notebook.AddPage(self.tab_dev, "Speakers & Chimes")
        self.audio_notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_notebook_page_changed)
        audio_sizer.Add(self.audio_notebook, 1, wx.EXPAND)
        self.tab_audio_shell.SetSizer(audio_sizer)

        devices_sizer = wx.BoxSizer(wx.VERTICAL)
        self.devices_notebook.AddPage(self.tab_fridge, "Refrigerator & Ice")
        self.devices_notebook.AddPage(self.tab_hvac, "HVAC")
        self.devices_notebook.AddPage(self.tab_vacuum, "Robot Vacuum")
        self.devices_notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_notebook_page_changed)
        devices_sizer.Add(self.devices_notebook, 1, wx.EXPAND)
        self.tab_devices_shell.SetSizer(devices_sizer)

        diagnostics_sizer = wx.BoxSizer(wx.VERTICAL)
        self.diagnostics_notebook.AddPage(self.tab_diagnostics_overview, "Tests & Support")
        self.diagnostics_notebook.AddPage(self.tab_recent_events, "Recent Events")
        self.diagnostics_notebook.AddPage(self.tab_speed, "Speed")
        self.diagnostics_notebook.AddPage(self.tab_ha_status, "Home Assistant Status")
        self.diagnostics_notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_notebook_page_changed)
        diagnostics_sizer.Add(self.diagnostics_notebook, 1, wx.EXPAND)
        self.tab_diagnostics_shell.SetSizer(diagnostics_sizer)

        self.setup_hidden_ai_voice_compat_controls()
        self._setup_tab_once("dash", self.setup_dash_tab, self.tab_dash)

        self.main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)
        wx.CallLater(1200, self._prewarm_lazy_tabs_in_background)

    def setup_hidden_ai_voice_compat_controls(self):
        def hide_disabled(control):
            control.Hide()
            control.Enable(False)

        self.voice_list = []
        self.engine_choice = wx.Choice(self.panel, choices=["Gemini (Cloud)", "Ollama (Local)", "Dual (Comparison)"])
        self.engine_choice.SetStringSelection(self.config.get("vision_engine", "Gemini (Cloud)"))
        hide_disabled(self.engine_choice)

        self.tts_engine_choice = wx.Choice(self.panel, choices=["Edge TTS (Natural)", "Gemini TTS", "Google Cloud", "Local PC SAPI"])
        self.tts_engine_choice.SetStringSelection(self.config.get("tts_engine", "Edge TTS (Natural)"))
        hide_disabled(self.tts_engine_choice)

        self.secondary_voice_label = wx.StaticText(self.panel, label="")
        hide_disabled(self.secondary_voice_label)
        self.secondary_voice_choice = wx.Choice(self.panel, choices=[])
        hide_disabled(self.secondary_voice_choice)
        self.btn_refresh_v = wx.Button(self.panel, label="Force Refresh Natural Voices")
        hide_disabled(self.btn_refresh_v)

        self.voice_choice = wx.Choice(self.panel, choices=self.voice_list)
        current_voice_idx = self.config.get("local_voice_index", 1)
        if self.voice_list and current_voice_idx < len(self.voice_list):
            self.voice_choice.SetSelection(current_voice_idx)
        elif self.voice_list:
            self.voice_choice.SetSelection(0)
        hide_disabled(self.voice_choice)

        prompt_names = list(self.config.get("prompts", {}).keys()) or ["Standard"]
        self.prompt_choice = wx.Choice(self.panel, choices=prompt_names)
        self.prompt_choice.SetStringSelection(self.config.get("active_prompt", prompt_names[0]))
        hide_disabled(self.prompt_choice)
        self.prompt_editor = wx.TextCtrl(self.panel, style=wx.TE_MULTILINE)
        self.prompt_editor.SetValue(self.config.get("prompts", {}).get(self.config.get("active_prompt", prompt_names[0]), ""))
        hide_disabled(self.prompt_editor)

    def _describe_control(self, control, description):
        ui_common.describe_control(
            control,
            description,
            focus_handler=self._on_control_focus_for_diagnostics,
            bind_focus=ui_common.should_log_focus(),
        )

    def _make_accessible_status_text(self, parent, **kwargs):
        return ui_common.make_accessible_status_text(parent, **kwargs)

    def _safe_submit(self, fn, *args, **kwargs):
        return ui_common.submit_ui_task(fn, *args, **kwargs)

    def _normalize_broadcast_mode(self, mode):
        return _normalize_broadcast_mode(mode)

    def _is_hidden_vacuum_setting_entity_id(self, entity_id):
        return _is_hidden_vacuum_setting_entity_id(entity_id)

    def _current_diagnostics(self, *, check_ha=False):
        return _current_diagnostics(check_ha=check_ha)

    def _dispatch_broadcast_message(self, message, *, channel="manual"):
        return _dispatch_broadcast_message(message, channel=channel)

    def _dispatch_cinderella_event(self, event, error="", source="vacuum"):
        return _dispatch_cinderella_event(event, error=error, source=source)

    def _run_doorbell_pipeline(self, location, rtsp_url, key):
        return _handle_doorbell(location, rtsp_url, key)

    def _mark_app_clean_shutdown(self):
        mark_app_clean_shutdown()

    def _open_url(self, url):
        return open_url(url)

    def _support_email(self):
        return SUPPORT_EMAIL

    def _on_control_focus_for_diagnostics(self, event):
        control = event.GetEventObject()
        try:
            label = control.GetLabel() if hasattr(control, "GetLabel") else ""
            name = control.GetName() if hasattr(control, "GetName") else ""
            logging.info(
                "[FOCUS] Dashboard focus class=%s name=%r label=%r shown=%s enabled=%s can_focus=%s",
                control.__class__.__name__,
                self._truncate_focus_log_text(name),
                self._truncate_focus_log_text(label),
                control.IsShownOnScreen() if hasattr(control, "IsShownOnScreen") else None,
                control.IsEnabled() if hasattr(control, "IsEnabled") else None,
                control.CanAcceptFocusFromKeyboard() if hasattr(control, "CanAcceptFocusFromKeyboard") else None,
            )
        except Exception:
            logging.debug("Could not log dashboard focus target.", exc_info=True)
        event.Skip()

    def _truncate_focus_log_text(self, value, limit=180):
        text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
        if len(text) <= limit:
            return text
        return text[:limit] + "...[truncated]"

    def _announce_focus_help(self, event, text):
        wx.CallAfter(self._safe_speak, text)
        event.Skip()

    def on_voice_change(self, event):
        self.config["local_voice_index"] = self.voice_choice.GetSelection()
        self.save_config()
        self.notify(f"PC Voice set to: {self.voice_choice.GetStringSelection()}")

    def _safe_speak(self, msg):
        if self.config.get("global_mute", False):
            return
        if self.sr:
            try: self.sr.output(msg)
            except Exception: pass

    def _global_mute_status_label(self):
        return (
            "Global mute is ON. Viper will log events but will not play chimes, speech, tests, or broadcasts."
            if self.config.get("global_mute", False)
            else "Global mute is OFF. Viper audio output is active."
        )

    def _sync_global_mute_controls(self):
        muted = bool(self.config.get("global_mute", False))
        if hasattr(self, "global_mute_chk"):
            self.global_mute_chk.SetValue(muted)
        if hasattr(self, "global_mute_status_txt"):
            self.global_mute_status_txt.SetLabel(self._global_mute_status_label())

    def set_global_mute(self, muted, source="dashboard"):
        muted = bool(muted)
        was_muted = bool(self.config.get("global_mute", False))
        if muted == was_muted:
            self._sync_global_mute_controls()
            return
        if muted and not was_muted:
            self._safe_speak("Global mute enabled.")
        self.config["global_mute"] = muted
        self.save_config()
        self._sync_global_mute_controls()
        message = f"Global mute {'enabled' if muted else 'disabled'} from {source}."
        self.notify(message, priority=1, interrupt=True, speak=not muted)

    def on_global_mute_change(self, event):
        self.set_global_mute(self.global_mute_chk.GetValue(), source="dashboard")

    def on_engine_change(self, event):
        self.config["vision_engine"] = self.engine_choice.GetStringSelection()
        self.save_config()
        self.notify(f"Vision Engine set to {self.config['vision_engine']}")

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

    def notify(self, text, priority=5, interrupt=False, speak=True):
        timestamp = datetime.now().strftime("%H:%M")
        activity_logs.insert(0, {"time": timestamp, "msg": text})
        if len(activity_logs) > 15: activity_logs.pop()
        if priority <= 3 or self.speech_queue.qsize() < 2:
            wx.CallAfter(self.status_display.SetValue, text)
        if not speak:
            return
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
        record_event("security", msg)
        self.refresh_system_health_display()
        self.notify(msg, priority=1, interrupt=True)
        safe_submit(audio.play_notification, "utilities", msg)

    def on_broadcast(self, event):
        msg = self.broadcast_input.GetValue().strip()
        if msg:
            self.broadcast_input.Clear()
            self.notify(f"Broadcasting: {msg}", priority=3, interrupt=True)
            record_event("broadcast", "Manual intercom broadcast sent.")
            safe_submit(audio.play_notification, "manual", msg)

    def on_home_assistant_setup(self, event):
        self.show_home_assistant_setup(initial_page=self.suggested_setup_page())

    def on_new_user_setup(self, event):
        self.show_new_user_setup_assistant()

    def show_new_user_setup_assistant(self):
        self._close_setup_surfaces(keep="_ha_server_assistant_dialog")
        existing = getattr(self, "_ha_server_assistant_dialog", None)
        if self._is_live_window(existing):
            try:
                existing.force_initial_focus()
                self._enter_setup_window_mode(existing)
                self._log_setup_focus_snapshot("reuse_ha_server_assistant")
                return
            except Exception:
                self._ha_server_assistant_dialog = None
        else:
            self._ha_server_assistant_dialog = None
        dlg = HomeAssistantFirstRunAssistantDialog(self)
        self._ha_server_assistant_dialog = dlg
        dlg.Show()
        wx.CallAfter(self._enter_setup_window_mode, dlg)
        wx.CallLater(75, dlg.force_initial_focus)
        wx.CallLater(300, dlg.force_initial_focus)
        wx.CallLater(450, self._log_setup_focus_snapshot, "show_ha_server_assistant")

    def show_home_assistant_setup(self, initial_page=None, auto_run=False, preserve_wizard=False):
        if preserve_wizard:
            for attr in ("_ha_setup_dialog", "_ha_server_assistant_dialog"):
                dlg = getattr(self, attr, None)
                if dlg is None:
                    continue
                try:
                    if hasattr(dlg, "_destroyed"):
                        dlg._destroyed = True
                    dlg.Destroy()
                except RuntimeError:
                    pass
                except Exception:
                    logging.debug("Could not close setup surface %s.", attr, exc_info=True)
                setattr(self, attr, None)
        else:
            self._close_setup_surfaces(keep="_ha_setup_dialog")
        existing = getattr(self, "_ha_setup_dialog", None)
        if self._is_live_window(existing):
            try:
                if initial_page:
                    existing.select_page(initial_page)
                existing.force_initial_focus()
                if auto_run:
                    wx.CallAfter(existing.on_beginner_auto_setup, None)
                self._log_setup_focus_snapshot("reuse_ha_setup")
                return
            except Exception:
                logging.debug("Could not reuse existing Home Assistant setup dialog.", exc_info=True)
                try:
                    existing.Destroy()
                except Exception:
                    pass
                self._ha_setup_dialog = None
        else:
            self._ha_setup_dialog = None
        use_env_prefill = not self.clean_first_run_test
        dlg = HomeAssistantSetupDialog(self, use_env_prefill=use_env_prefill)
        self._ha_setup_dialog = dlg
        if initial_page:
            dlg.select_page(initial_page)
        dlg.Show()
        wx.CallAfter(self._enter_setup_window_mode, dlg)
        if auto_run:
            wx.CallAfter(dlg.on_beginner_auto_setup, None)
        wx.CallLater(75, dlg.force_initial_focus)
        wx.CallLater(300, dlg.force_initial_focus)
        wx.CallLater(450, self._log_setup_focus_snapshot, "show_ha_setup")


if __name__ == "__main__":
    LOG_PATH = cfg.DATA_DIR / "viper_full_debug.log"
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[file_handler, logging.StreamHandler()], force=True)
    install_crash_hooks()
    if "--ha-recovery-diagnose" in sys.argv:
        diagnosis = ha_recovery.diagnose()
        if "--compact" in sys.argv:
            diagnosis = ha_recovery.compact_diagnosis(diagnosis)
        print(json.dumps(diagnosis, indent=2, sort_keys=True))
        sys.exit(0)
    if "--ha-recovery-test-push" in sys.argv:
        ok = ha_recovery.send_recovery_test_push()
        print(json.dumps({"ok": ok, "message": "HA recovery Pushover test sent." if ok else "HA recovery Pushover test failed."}, indent=2, sort_keys=True))
        sys.exit(0)
    if "--ha-recovery-pause" in sys.argv:
        minutes = ha_recovery.DEFAULT_PAUSE_MINUTES
        reason = "Home Assistant maintenance"
        for index, arg in enumerate(sys.argv):
            if arg == "--minutes" and index + 1 < len(sys.argv):
                try:
                    minutes = int(sys.argv[index + 1])
                except ValueError:
                    minutes = ha_recovery.DEFAULT_PAUSE_MINUTES
            elif arg == "--reason" and index + 1 < len(sys.argv):
                reason = sys.argv[index + 1]
        print(json.dumps(ha_recovery.pause_recovery(minutes, reason), indent=2, sort_keys=True))
        sys.exit(0)
    if "--ha-recovery-resume" in sys.argv:
        print(json.dumps(ha_recovery.resume_recovery(), indent=2, sort_keys=True))
        sys.exit(0)
    if "--ha-recovery-pause-status" in sys.argv:
        print(json.dumps(ha_recovery.maintenance_pause_status(), indent=2, sort_keys=True))
        sys.exit(0)
    if "--ha-recovery-once" in sys.argv:
        result = ha_recovery.repair_once(push="--no-push" not in sys.argv)
        if "--compact" in sys.argv:
            print(ha_recovery.compact_status_line(result))
            print(json.dumps(ha_recovery.compact_result(result), indent=2, sort_keys=True))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        sys.exit(0 if result.get("ok") else 1)
    if not acquire_single_instance_lock():
        focused = focus_existing_viper_window()
        logging.warning("Another Viper Vision instance is already running; exiting duplicate startup. focused_existing_window=%s", focused)
        if not focused:
            try:
                webbrowser.open("http://127.0.0.1:5050/remote")
            except Exception:
                pass
        sys.exit(0)
    mark_app_running()
    logging.info("===== VIPER VISION STARTING =====")
    gui_app = wx.App(False)
    cfg.ensure_default_assets()
    threading.Thread(target=audio.startup_cleanup, name="ViperStartupCleanup", daemon=True).start()
    threading.Thread(target=audio.start_local_server, daemon=True).start()
    threading.Thread(target=run_flask_server, daemon=True).start()
    # Flask routes all guard on 'dash_app is None', so no fixed sleep is needed.
    dash_app = ViperDashboard()
    start_plumbing_monitor()
    gui_app.MainLoop()
