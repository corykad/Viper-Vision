import asyncio
import json
import logging
import os
import platform
import random
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from queue import Empty, PriorityQueue, Queue
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import soco
import websockets
import wx
import wx.adv
try:
    import wx.html2 as wxhtml2
except Exception:
    wxhtml2 = None
from accessible_output2.outputs import auto
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from waitress import serve

import viper_audio as audio
import viper_config as cfg
import viper_discovery as discovery
import viper_diagnostics as diagnostics
import viper_ha_listener as ha_listener
import viper_ha_package as ha_package
import viper_ha_vm as ha_vm
import viper_ring_discovery as ring_discovery
import viper_vision as vision

AI_DESCRIPTION_STYLE_LABELS = {
    "balanced": "Balanced",
    "fast_security": "Fast security summary",
    "people_movement": "People and movement",
    "packages_deliveries": "Packages and deliveries",
    "detailed_blind": "Detailed for blind user",
    "custom": "Custom",
}
AI_DESCRIPTION_STYLE_KEYS_BY_LABEL = {label: key for key, label in AI_DESCRIPTION_STYLE_LABELS.items()}
AI_DESCRIPTION_JOBS = [
    (
        "front_photo",
        "Front door alert",
        "Front door alert description style. Choose what Gemini should pay attention to for front door still-image alerts.",
        "Front door custom AI instructions. Only used when Front door alert is set to Custom.",
    ),
    (
        "back_photo",
        "Back door alert",
        "Back door alert description style. Choose what Gemini should pay attention to for back door still-image alerts.",
        "Back door custom AI instructions. Only used when Back door alert is set to Custom.",
    ),
    (
        "manual_video",
        "Manual outside video check",
        "Manual outside video check description style. Choose what Gemini should pay attention to when you press Analyze Camera Video Now.",
        "Manual outside video custom AI instructions. You may use placeholders: {location}, {side}, and {first_description}.",
    ),
    (
        "smart_video",
        "Smart video follow-up",
        "Smart video follow-up description style. Choose what Gemini should pay attention to when Smart mode asks for more detail.",
        "Smart video follow-up custom AI instructions. You may use placeholders: {location}, {side}, and {first_description}.",
    ),
    (
        "detailed_video",
        "Detailed video follow-up",
        "Detailed video follow-up description style. Choose what Gemini should pay attention to when Detailed mode sends video after an alert.",
        "Detailed video follow-up custom AI instructions. You may use placeholders: {location}, {side}, and {first_description}.",
    ),
]

HIDDEN_VACUUM_SETTING_SUFFIXES = {
    "_dock_empty_mode",
}

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


class AccessibleStatusText(wx.StaticText):
    """Static status text with TextCtrl-like setters for existing update paths."""

    def __init__(self, parent, value="", wrap_width=760, **kwargs):
        self._wrap_width = wrap_width
        self._value = str(value or "")
        super().__init__(parent, label=self._value, **kwargs)
        if self._wrap_width:
            self.Wrap(self._wrap_width)

    def SetLabel(self, label):
        self._value = str(label or "")
        super().SetLabel(self._value)
        if self._wrap_width:
            self.Wrap(self._wrap_width)
        parent = self.GetParent()
        if parent:
            try:
                parent.Layout()
            except Exception:
                pass

    def SetValue(self, value):
        self.SetLabel(value)

    def GetValue(self):
        return self._value

    def SetInsertionPointEnd(self):
        pass

    def ShowPosition(self, pos):
        pass

    def GetLastPosition(self):
        return len(self._value)

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

def open_official_link(*args, **kwargs):
    return ha_vm.open_official_link(*args, **kwargs)
open_official_link._ha_vm_delegate = "open_official_link"

def open_url(*args, **kwargs):
    return ha_vm.open_url(*args, **kwargs)
open_url._ha_vm_delegate = "open_url"

def find_vboxmanage(*args, **kwargs):
    return ha_vm.find_vboxmanage(*args, **kwargs)
find_vboxmanage._ha_vm_delegate = "find_vboxmanage"

def find_winget(*args, **kwargs):
    return ha_vm.find_winget(*args, **kwargs)
find_winget._ha_vm_delegate = "find_winget"

def get_machine_architecture(*args, **kwargs):
    return ha_vm.get_machine_architecture(*args, **kwargs)
get_machine_architecture._ha_vm_delegate = "get_machine_architecture"

def get_ha_vm_platform_status(*args, **kwargs):
    return ha_vm.get_ha_vm_platform_status(*args, **kwargs)
get_ha_vm_platform_status._ha_vm_delegate = "get_ha_vm_platform_status"

def normalize_ha_vm_ram_mb(*args, **kwargs):
    return ha_vm.normalize_ha_vm_ram_mb(*args, **kwargs)
normalize_ha_vm_ram_mb._ha_vm_delegate = "normalize_ha_vm_ram_mb"

def normalize_ha_vm_disk_gb(*args, **kwargs):
    return ha_vm.normalize_ha_vm_disk_gb(*args, **kwargs)
normalize_ha_vm_disk_gb._ha_vm_delegate = "normalize_ha_vm_disk_gb"

def get_ha_vm_drive_space_status(*args, **kwargs):
    return ha_vm.get_ha_vm_drive_space_status(*args, **kwargs)
get_ha_vm_drive_space_status._ha_vm_delegate = "get_ha_vm_drive_space_status"

def get_winget_status(*args, **kwargs):
    return ha_vm.get_winget_status(*args, **kwargs)
get_winget_status._ha_vm_delegate = "get_winget_status"

def get_virtualbox_status(*args, **kwargs):
    return ha_vm.get_virtualbox_status(*args, **kwargs)
get_virtualbox_status._ha_vm_delegate = "get_virtualbox_status"

def is_windows_admin(*args, **kwargs):
    return ha_vm.is_windows_admin(*args, **kwargs)
is_windows_admin._ha_vm_delegate = "is_windows_admin"

def _run_powershell_command(*args, **kwargs):
    return ha_vm._run_powershell_command(*args, **kwargs)
_run_powershell_command._ha_vm_delegate = "_run_powershell_command"

def _windows_optional_feature_state(*args, **kwargs):
    return ha_vm._windows_optional_feature_state(*args, **kwargs)
_windows_optional_feature_state._ha_vm_delegate = "_windows_optional_feature_state"

def get_windows_virtualization_status(*args, **kwargs):
    return ha_vm.get_windows_virtualization_status(*args, **kwargs)
get_windows_virtualization_status._ha_vm_delegate = "get_windows_virtualization_status"

def optimize_windows_for_virtualbox(*args, **kwargs):
    return ha_vm.optimize_windows_for_virtualbox(*args, **kwargs)
optimize_windows_for_virtualbox._ha_vm_delegate = "optimize_windows_for_virtualbox"

def install_virtualbox_with_winget(*args, **kwargs):
    return ha_vm.install_virtualbox_with_winget(*args, **kwargs)
install_virtualbox_with_winget._ha_vm_delegate = "install_virtualbox_with_winget"

def _hidden_subprocess_kwargs(*args, **kwargs):
    return ha_vm._hidden_subprocess_kwargs(*args, **kwargs)
_hidden_subprocess_kwargs._ha_vm_delegate = "_hidden_subprocess_kwargs"

def _clean_process_progress_line(*args, **kwargs):
    return ha_vm._clean_process_progress_line(*args, **kwargs)
_clean_process_progress_line._ha_vm_delegate = "_clean_process_progress_line"

def _run_process_with_progress(*args, **kwargs):
    return ha_vm._run_process_with_progress(*args, **kwargs)
_run_process_with_progress._ha_vm_delegate = "_run_process_with_progress"

def _run_vbox(*args, **kwargs):
    return ha_vm._run_vbox(*args, **kwargs)
_run_vbox._ha_vm_delegate = "_run_vbox"

def _run_vbox_progress(*args, **kwargs):
    return ha_vm._run_vbox_progress(*args, **kwargs)
_run_vbox_progress._ha_vm_delegate = "_run_vbox_progress"

def _vbox_vm_exists(*args, **kwargs):
    return ha_vm._vbox_vm_exists(*args, **kwargs)
_vbox_vm_exists._ha_vm_delegate = "_vbox_vm_exists"

def _choose_bridged_adapter(*args, **kwargs):
    return ha_vm._choose_bridged_adapter(*args, **kwargs)
_choose_bridged_adapter._ha_vm_delegate = "_choose_bridged_adapter"

def get_latest_haos_virtualbox_asset(*args, **kwargs):
    return ha_vm.get_latest_haos_virtualbox_asset(*args, **kwargs)
get_latest_haos_virtualbox_asset._ha_vm_delegate = "get_latest_haos_virtualbox_asset"

def download_file(*args, **kwargs):
    return ha_vm.download_file(*args, **kwargs)
download_file._ha_vm_delegate = "download_file"

def _extract_haos_disk(*args, **kwargs):
    return ha_vm._extract_haos_disk(*args, **kwargs)
_extract_haos_disk._ha_vm_delegate = "_extract_haos_disk"

def _import_ha_ova(*args, **kwargs):
    return ha_vm._import_ha_ova(*args, **kwargs)
_import_ha_ova._ha_vm_delegate = "_import_ha_ova"

def _resize_virtualbox_disk(*args, **kwargs):
    return ha_vm._resize_virtualbox_disk(*args, **kwargs)
_resize_virtualbox_disk._ha_vm_delegate = "_resize_virtualbox_disk"

def _setup_progress_default_state(*args, **kwargs):
    return ha_vm._setup_progress_default_state(*args, **kwargs)
_setup_progress_default_state._ha_vm_delegate = "_setup_progress_default_state"

def _coerce_setup_progress_state(*args, **kwargs):
    return ha_vm._coerce_setup_progress_state(*args, **kwargs)
_coerce_setup_progress_state._ha_vm_delegate = "_coerce_setup_progress_state"

def _bytes_progress_percent(*args, **kwargs):
    return ha_vm._bytes_progress_percent(*args, **kwargs)
_bytes_progress_percent._ha_vm_delegate = "_bytes_progress_percent"

def _classify_setup_progress_message(*args, **kwargs):
    return ha_vm._classify_setup_progress_message(*args, **kwargs)
_classify_setup_progress_message._ha_vm_delegate = "_classify_setup_progress_message"

def _format_setup_progress_state(*args, **kwargs):
    return ha_vm._format_setup_progress_state(*args, **kwargs)
_format_setup_progress_state._ha_vm_delegate = "_format_setup_progress_state"

def _check_home_assistant_core_ready(*args, **kwargs):
    return ha_vm._check_home_assistant_core_ready(*args, **kwargs)
_check_home_assistant_core_ready._ha_vm_delegate = "_check_home_assistant_core_ready"

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


def _normalize_broadcast_mode(mode) -> str:
    """Normalize saved/UI mode labels into the internal mode names."""
    text = str(mode or "speak").strip().lower()
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
    return aliases.get(text, "speak")


def _normalize_broadcast_message_text(message: str) -> str:
    return " ".join(str(message or "").strip().lower().rstrip(".!").split())


def _infer_fridge_channel_from_message(message: str) -> str:
    normalized = _normalize_broadcast_message_text(message)
    return {
        "the fridge door is open": "fridge_open",
        "the fridge door is closed": "fridge_closed",
        "the refrigerator door is open": "fridge_open",
        "the refrigerator door is closed": "fridge_closed",
        "the freezer door is open": "freezer_open",
        "the freezer door is closed": "freezer_closed",
    }.get(normalized, "")


def _resolve_broadcast_channel(channel: str, message: str) -> str:
    requested = str(channel or "").strip().lower()
    if requested in {"", "default", "manual"}:
        inferred = _infer_fridge_channel_from_message(message)
        if inferred:
            return inferred
    return requested


def _notify_dashboard_async(*args, **kwargs):
    try:
        wx.CallAfter(dash_app.notify, *args, **kwargs)
    except AssertionError:
        dash_app.notify(*args, **kwargs)


def _dispatch_broadcast_message(raw_message: str, push: bool = False, channel: str = "") -> dict:
    """Dispatch a broadcast according to its channel's configured behaviour.

    channel=""/"default" → global default
    channel="fridge_open" etc → per-state fridge/freezer control
    channel="manual"          → always speak (GUI / manual web UI)

    This helper is deliberately independent of Flask request/response state so
    the web UI, legacy Home Assistant automations, and the direct HA listener
    all use the same chime/speak/silent routing.
    """
    if dash_app is None or is_shutting_down.is_set():
        return {"ok": False, "message": "System not ready or shutting down.", "status_code": 503}

    msg = (raw_message or "").strip()
    if not msg:
        return {"ok": False, "message": "No message provided.", "status_code": 400}

    try:
        config = dash_app.config
        if config.get("global_mute", False):
            _notify_dashboard_async(
                f"Global mute is on. Broadcast logged with no audio: {msg}",
                priority=3, interrupt=True, speak=False,
            )
            logging.info("[GLOBAL MUTE] Broadcast logged only channel=%r message=%r", channel or "default", msg)
            return {
                "ok": True,
                "message": f"Global mute is on. Broadcast logged with no audio: {msg}",
                "status_code": 200,
                "path": "muted",
                "resolved_channel": _resolve_broadcast_channel(channel, msg),
            }
        requested_channel = str(channel or "").strip().lower()
        resolved_channel = _resolve_broadcast_channel(channel, msg)

        # User-entered manual broadcasts always speak. Legacy HA fridge/freezer
        # messages can arrive as manual/default, so infer those before this branch.
        if requested_channel == "manual" and resolved_channel == "manual":
            ch_settings = {"mode": "speak", "chime": ""}
        else:
            ch_settings = _resolve_channel_settings(resolved_channel, config)

        mode  = _normalize_broadcast_mode(ch_settings["mode"])
        chime = ch_settings["chime"]
        logging.info(
            "[BROADCAST ROUTE] requested_channel=%r resolved_channel=%r mode=%s chime=%r path=%s message=%r",
            requested_channel or "default",
            resolved_channel or "default",
            mode,
            chime,
            mode,
            msg,
        )

        _notify_dashboard_async(
            f"Broadcast [{resolved_channel or 'default'}] [{mode}]: {msg}",
            priority=3, interrupt=True, speak=(mode == "speak"),
        )

        if mode == "silent":
            logging.info("[BROADCAST] Silent channel=%r logged only: %r", resolved_channel, msg)
            return {
                "ok": True,
                "message": f"Broadcast logged (silent): {msg}",
                "status_code": 200,
                "path": "silent",
                "resolved_channel": resolved_channel,
            }

        if mode == "chime":
            future = safe_submit(audio.play_broadcast_chime, chime, resolved_channel)
            if future is None:
                return {"ok": False, "message": "System shutting down.", "status_code": 503}
            logging.info("[BROADCAST] Chime channel=%r chime=%r for: %r", resolved_channel, chime, msg)
            return {
                "ok": True,
                "message": f"Chime played for: {msg}",
                "status_code": 200,
                "path": "chime",
                "resolved_channel": resolved_channel,
                "chime": chime,
            }

        # speak
        broadcast_context = {
            "channel": "broadcast",
            "push": push,
            "received_ts": time.time(),
            "received_iso": datetime.now().isoformat(timespec="seconds"),
        }
        future = safe_submit(audio.play_notification, "manual", msg, push)
        if future is None:
            return {"ok": False, "message": "Broadcast rejected — system shutting down.", "status_code": 503}
        return {
            "ok": True,
            "message": f"Broadcast sent: {msg}",
            "status_code": 200,
            "path": "speak",
            "resolved_channel": resolved_channel,
        }

    except Exception as e:
        logging.exception("Broadcast route failed")
        return {"ok": False, "message": f"Broadcast failed: {e}", "status_code": 500}


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
        status, code = _handle_doorbell(location, action.get("rtsp_url") or "", side)
        logging.info("[HA LISTENER] doorbell action side=%s code=%s status=%s", side, code, status)
    elif action_type == "cinderella":
        _dispatch_cinderella_event(action.get("event", ""), action.get("error", ""), action.get("source", "vacuum"))
    elif action_type == "broadcast":
        message = (action.get("message") or "").strip()
        channel = (action.get("channel") or "default").strip()
        if message:
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
        doorbell_video_settings=_doorbell_video_settings(dash_app.config),
        doorbell_video_modes=vision.VIDEO_ANALYSIS_LABELS,
        last_video_analysis=getattr(dash_app, "last_video_analysis", {}),
        setup_status_summary=dash_app.build_setup_next_action_summary() if hasattr(dash_app, "build_setup_next_action_summary") else "",
        setup_checklist_summary=dash_app.build_setup_checklist_summary() if hasattr(dash_app, "build_setup_checklist_summary") else "",
        setup_smoke_report=getattr(dash_app, "last_remote_setup_smoke_report", ""),
        diagnostics_summary=diagnostics.health_summary_text(_current_diagnostics(check_ha=False)),
        ice_maker=dash_app.get_ice_maker_status(timeout=2) if hasattr(dash_app, "get_ice_maker_status") else {},
    )


def _current_diagnostics(*, check_ha=False):
    if dash_app is None:
        return diagnostics.collect_diagnostics({})
    ha_connection = {"checked": False}
    ha_health = {"checked": False}
    if check_ha:
        ha_settings = cfg.get_ha_settings(dash_app.config, include_env=True)
        ha_health = discovery.check_ha_core_health(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
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
        ha_health=ha_health,
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
            ha_health=diag.get("ha_health", {}),
            setup_summary=dash_app.build_setup_checklist_summary() if hasattr(dash_app, "build_setup_checklist_summary") else "",
            setup_events=getattr(dash_app, "setup_events", []),
            last_setup_status=getattr(dash_app, "last_setup_status", ""),
        )
        flash(f"Support bundle created: {result['path']}")
    except Exception as e:
        logging.exception("Support bundle creation failed")
        flash(f"Support bundle failed: {e}")
    return redirect(url_for("remote_ui"))


@app.route("/remote/setup/smoke", methods=["POST"])
def web_setup_smoke():
    if dash_app is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        report = dash_app._format_safe_smoke_report(dash_app._collect_safe_smoke_results())
        dash_app.last_remote_setup_smoke_report = report
        flash("Safe setup smoke test finished. Review Setup Status for PASS/FIX details.")
    except Exception as e:
        logging.exception("Remote setup smoke test failed")
        dash_app.last_remote_setup_smoke_report = f"Smoke Test: ERROR\n\nThe smoke test failed: {e}"
        flash(f"Safe setup smoke test failed: {e}")
    return redirect(url_for("remote_ui") + "#setup-status-heading")


@app.route("/remote/setup/restore_optional", methods=["POST"])
def web_restore_optional_setup():
    if dash_app is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        skips = dash_app._setup_skip_state()
        if not any(skips.values()):
            flash("No optional setup items are currently skipped.")
        else:
            restored = [key for key, value in skips.items() if value]
            dash_app.config["setup_skips"] = {key: False for key in skips}
            cfg.save_config(dash_app.config)
            dash_app.record_setup_event("optional_setup_restored_remote", "Restored skipped optional setup items from the remote web UI.", restored=", ".join(restored))
            flash("Restored optional setup items. They will show as available again.")
    except Exception as e:
        logging.exception("Remote optional setup restore failed")
        flash(f"Could not restore optional setup items: {e}")
    return redirect(url_for("remote_ui") + "#setup-status-heading")

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
    if _is_hidden_vacuum_setting_entity_id(entity_id):
        return False
    if domain in {"select", "number"}:
        return True
    if domain == "switch" and "child_lock" in entity_id:
        return True
    return False

def _is_hidden_vacuum_setting_entity_id(entity_id):
    entity_id = str(entity_id or "").lower()
    return any(entity_id.endswith(suffix) for suffix in HIDDEN_VACUUM_SETTING_SUFFIXES)

def _web_vacuum_switch_next_action(state):
    state = str(state or "").strip().lower()
    turn_on = state != "on"
    return {
        "turn_on": turn_on,
        "button_label": "Turn on" if turn_on else "Turn off",
        "next_state": "on" if turn_on else "off",
    }

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
        next_action = _web_vacuum_switch_next_action(entity.get("state")) if _ha_domain_from_entity_id(entity.get("entity_id", "")) == "switch" else {}
        settings.append({
            "entity_id": entity.get("entity_id", ""),
            "domain": _ha_domain_from_entity_id(entity.get("entity_id", "")),
            "label": _web_short_entity_label(entity),
            "state": entity.get("state", ""),
            "button_label": next_action.get("button_label", ""),
            "turn_on": "1" if next_action.get("turn_on") else "0",
            "next_state": next_action.get("next_state", ""),
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


@app.route("/remote/doorbell/video_settings", methods=["POST"])
def web_save_doorbell_video_settings():
    if dash_app is None:
        return _json_or_redirect("System not ready.", ok=False, status_code=503)
    current = _doorbell_video_settings(dash_app.config)
    mode = (request.form.get("video_mode") or current["mode"]).strip().lower()
    if mode not in vision.VIDEO_ANALYSIS_MODES:
        mode = current["mode"]
    manual_seconds = vision.clamp_manual_video_seconds(request.form.get("manual_clip_seconds"), dash_app.config)
    settings = dict(current)
    settings["mode"] = mode
    settings["manual_clip_seconds"] = manual_seconds
    dash_app.config["doorbell_video_analysis"] = settings
    dash_app.save_config()
    if hasattr(dash_app, "_refresh_video_analysis_controls"):
        wx.CallAfter(dash_app._refresh_video_analysis_controls)
    return _json_or_redirect(
        f"Doorbell video mode saved: {vision.VIDEO_ANALYSIS_LABELS.get(mode, mode)}. Manual clips are {manual_seconds} seconds."
    )


@app.route("/remote/doorbell/video_analyze/<side>", methods=["POST"])
def web_analyze_doorbell_video(side):
    if dash_app is None:
        return _json_or_redirect("System not ready.", ok=False, status_code=503)
    side = "back" if side == "back" else "front"
    seconds = vision.clamp_manual_video_seconds(request.form.get("manual_clip_seconds"), dash_app.config)
    future = safe_submit(dash_app._run_manual_doorbell_video_analysis, side, seconds, "remote web interface")
    if future is None:
        return _json_or_redirect("Video analysis rejected because Viper is shutting down.", ok=False, status_code=503)
    return _json_or_redirect(f"Started {side} camera video analysis for {seconds} seconds. Viper will speak the result.")


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

@app.route("/remote/global_mute", methods=["POST"])
def web_toggle_global_mute():
    if dash_app:
        muted = request.form.get("global_mute") == "1"
        dash_app.set_global_mute(muted, source="remote")
        flash(f"Global mute {'enabled' if muted else 'disabled'}.")
    return redirect(url_for("remote_ui") + "#dashboard-heading")

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
    if _is_hidden_vacuum_setting_entity_id(entity_id):
        flash("That Roborock dock setting is hidden because Home Assistant reports it but rejects write attempts. Change it in Home Assistant until the Roborock integration exposes a reliable control.")
        return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
    if domain == "select":
        option = request.form.get("option", "").strip()
        ok = dash_app._call_ha_service_data("select/select_option", {"entity_id": entity_id, "option": option}, timeout=30)
        flash(f"Set {entity_id} to {option}." if ok else f"Could not set {entity_id}.")
    elif domain == "number":
        raw_value = request.form.get("value", "").strip()
        try:
            value = float(raw_value)
        except ValueError:
            flash("Number setting must be a valid number.")
            return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
        ok = dash_app._call_ha_service_data("number/set_value", {"entity_id": entity_id, "value": value}, timeout=30)
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

@app.route("/remote/ice/toggle", methods=["POST"])
def web_ice_maker_toggle():
    if dash_app:
        result = dash_app.on_ice_maker_toggle(None)
        flash(result or "Ice maker toggle requested.")
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
                "mode":  _normalize_broadcast_mode(request.form.get(f"channel_{ch_name}_mode", "speak")),
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


class HomeAssistantVmResourcesDialog(wx.Dialog):
    def __init__(self, parent, initial_ram_mb=DEFAULT_HA_VM_RAM_MB, initial_disk_gb=DEFAULT_HA_VM_DISK_GB):
        super().__init__(parent, title="Home Assistant VM Resources", size=(620, 520))
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        instructions = wx.TextCtrl(
            panel,
            value=(
                "Choose how much memory and disk space Home Assistant should use.\n\n"
                "Recommended memory: 4096 MB, which is 4 GB. Use 6144 MB or more only if this PC has enough memory and Home Assistant has many integrations.\n\n"
                "Recommended disk: 32 GB. Use 64 GB or more if the user plans to keep lots of history, add-ons, logs, or camera-related tools."
            ),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 170),
        )
        instructions.SetName("Home Assistant VM resource instructions")
        instructions.SetToolTip("Read-only guidance for choosing Home Assistant VM RAM and disk space.")
        sizer.Add(instructions, 0, wx.ALL | wx.EXPAND, 10)

        row = wx.BoxSizer(wx.HORIZONTAL)
        label = wx.StaticText(panel, label="Home Assistant RAM in megabytes")
        label.SetName("Home Assistant RAM in megabytes")
        row.Add(label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        self.ram_ctrl = wx.SpinCtrl(panel, min=MIN_HA_VM_RAM_MB, max=MAX_HA_VM_RAM_MB, initial=normalize_ha_vm_ram_mb(initial_ram_mb), size=(160, -1))
        self.ram_ctrl.SetName("Home Assistant RAM in megabytes")
        self.ram_ctrl.SetToolTip("Amount of RAM for the Home Assistant virtual machine. Recommended value is 4096.")
        row.Add(self.ram_ctrl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        quick = wx.BoxSizer(wx.HORIZONTAL)
        for label_text, value in (("Use 2 GB", 2048), ("Use 4 GB Recommended", 4096), ("Use 6 GB", 6144), ("Use 8 GB", 8192)):
            btn = wx.Button(panel, label=label_text)
            btn.SetName(label_text)
            btn.SetToolTip(f"Set Home Assistant RAM to {value} megabytes.")
            btn.Bind(wx.EVT_BUTTON, lambda _event, ram=value: self.ram_ctrl.SetValue(ram))
            quick.Add(btn, 1, wx.ALL | wx.EXPAND, 4)
        sizer.Add(quick, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        disk_row = wx.BoxSizer(wx.HORIZONTAL)
        disk_label = wx.StaticText(panel, label="Home Assistant disk space in gigabytes")
        disk_label.SetName("Home Assistant disk space in gigabytes")
        disk_row.Add(disk_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        self.disk_ctrl = wx.SpinCtrl(panel, min=MIN_HA_VM_DISK_GB, max=MAX_HA_VM_DISK_GB, initial=normalize_ha_vm_disk_gb(initial_disk_gb), size=(160, -1))
        self.disk_ctrl.SetName("Home Assistant disk space in gigabytes")
        self.disk_ctrl.SetToolTip("Target disk size for the Home Assistant virtual machine. Recommended value is 32 GB.")
        disk_row.Add(self.disk_ctrl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        sizer.Add(disk_row, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        disk_quick = wx.BoxSizer(wx.HORIZONTAL)
        for label_text, value in (("Use 16 GB Minimum", 16), ("Use 32 GB Recommended", 32), ("Use 64 GB", 64), ("Use 128 GB", 128)):
            btn = wx.Button(panel, label=label_text)
            btn.SetName(label_text)
            btn.SetToolTip(f"Set Home Assistant disk space to {value} gigabytes.")
            btn.Bind(wx.EVT_BUTTON, lambda _event, disk=value: self.disk_ctrl.SetValue(disk))
            disk_quick.Add(btn, 1, wx.ALL | wx.EXPAND, 4)
        sizer.Add(disk_quick, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        buttons = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, "OK")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        ok_btn.SetDefault()
        buttons.AddButton(ok_btn)
        buttons.AddButton(cancel_btn)
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)

        panel.SetSizer(sizer)
        wx.CallAfter(self.ram_ctrl.SetFocus)

    def ram_mb(self):
        return normalize_ha_vm_ram_mb(self.ram_ctrl.GetValue())

    def disk_gb(self):
        return normalize_ha_vm_disk_gb(self.disk_ctrl.GetValue())


class HomeAssistantFirstRunAssistantDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(None, title="Home Assistant Setup Assistant", size=(780, 720))
        self.parent = parent
        self._destroyed = False
        self._initial_focus_given = False
        self.progress_dlg = None
        self.progress_txt = None
        self._last_progress_spoken = 0
        self._progress_log_lines = []
        self._setup_progress_state = _coerce_setup_progress_state(
            getattr(parent, "config", {}).get("setup_progress", {}) if parent is not None else {}
        )
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=(
                "This assistant helps brand-new users get from nothing to a working Viper setup. "
                "It can install VirtualBox with winget, download the official Home Assistant OS VirtualBox image, create the VM, start it, find the Home Assistant address, and then continue to Viper setup."
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
            ("Install VirtualBox With Winget", self.on_install_virtualbox_winget, "Installs Oracle VirtualBox using winget. Windows may ask for administrator permission."),
            ("Optimize Windows For VirtualBox", self.on_optimize_windows_virtualbox, "Turns off Windows hypervisor features that can make VirtualBox Home Assistant unstable. Requires administrator permission and a reboot."),
            ("Download And Install Home Assistant VM", self.on_download_install_ha_vm, "Downloads the official Home Assistant OS VirtualBox image and creates the Home Assistant virtual machine."),
            ("Choose Downloaded HA OS Image", self.on_choose_haos_image, "Choose a Home Assistant OS VirtualBox zip, VDI, or OVA file you already downloaded."),
            ("Start Home Assistant VM", self.on_start_ha_vm, "Starts the Home Assistant virtual machine in headless mode."),
            ("Find Home Assistant", self.on_find_ha, "Searches common Home Assistant addresses on your network."),
            ("Open Home Assistant", self.on_open_found_ha, "Opens the saved or detected Home Assistant address in your browser."),
            ("Open HA Windows Guide", lambda _e: open_official_link("ha_windows"), "Opens the official Home Assistant Windows installation guide."),
            ("Open VirtualBox Download", lambda _e: open_official_link("virtualbox"), "Opens the official VirtualBox download page."),
            ("Open HA OS Download", lambda _e: open_official_link("ha_os_releases"), "Opens the official Home Assistant OS release downloads page."),
            ("Open Token Help", lambda _e: open_official_link("ha_tokens"), "Opens Home Assistant developer documentation for long lived access tokens."),
            ("Open Viper Help", lambda _e: open_help("ha-install"), "Opens Viper's local Home Assistant installation help page."),
            ("Continue To Viper Setup", self.on_continue, "Opens Viper's Home Assistant setup dialog."),
        ]
        self.btn_check_pc = None
        for label, handler, help_text in buttons:
            btn = wx.Button(panel, label=label, size=(-1, 44))
            btn.Bind(wx.EVT_BUTTON, handler)
            self._describe_control(btn, help_text)
            if label == "Check This PC":
                self.btn_check_pc = btn
            grid.Add(btn, 0, wx.EXPAND)
        sizer.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        close = wx.Button(panel, label="Close", size=(-1, 44))
        close.Bind(wx.EVT_BUTTON, self.on_close)
        self._describe_control(close, "Close setup assistant button.")
        sizer.Add(close, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)
        wx.CallAfter(self.force_initial_focus)
        wx.CallLater(150, self.force_initial_focus)
        wx.CallLater(500, self.force_initial_focus)
        wx.CallAfter(self.on_check_pc, None)

    def _describe_control(self, control, description):
        control.SetName(description)
        control.SetToolTip(description)
        try:
            control.Bind(wx.EVT_SET_FOCUS, self._on_control_focus_for_diagnostics)
        except Exception:
            pass

    def _on_control_focus_for_diagnostics(self, event):
        control = event.GetEventObject()
        try:
            label = control.GetLabel() if hasattr(control, "GetLabel") else ""
            logging.info(
                "[FOCUS] First-run assistant focus class=%s name=%r label=%r shown=%s enabled=%s can_focus=%s",
                control.__class__.__name__,
                control.GetName() if hasattr(control, "GetName") else "",
                label,
                control.IsShownOnScreen() if hasattr(control, "IsShownOnScreen") else None,
                control.IsEnabled() if hasattr(control, "IsEnabled") else None,
                control.CanAcceptFocusFromKeyboard() if hasattr(control, "CanAcceptFocusFromKeyboard") else None,
            )
        except Exception:
            logging.debug("Could not log first-run assistant focus target.", exc_info=True)
        event.Skip()

    def on_close(self, event):
        self._destroyed = True
        owner = getattr(self, "parent", None)
        try:
            if self.progress_dlg is not None:
                try:
                    self.progress_dlg.Destroy()
                except Exception:
                    pass
            if owner is not None:
                if getattr(owner, "_ha_server_assistant_dialog", None) is self:
                    owner._ha_server_assistant_dialog = None
                wx.CallAfter(owner._leave_setup_window_mode)
        except Exception:
            logging.debug("Could not restore focus after closing Home Assistant server assistant.", exc_info=True)
        self.Destroy()

    def force_initial_focus(self):
        try:
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
            self._nudge_dialog_foreground()
            if self._initial_focus_given:
                return
            self._initial_focus_given = True
            focus_target = self.btn_check_pc or self.status_txt
            if hasattr(focus_target, "SetFocusFromKbd"):
                try:
                    focus_target.SetFocusFromKbd()
                    return
                except Exception:
                    pass
            focus_target.SetFocus()
        except Exception:
            logging.debug("Could not force first-run assistant focus.", exc_info=True)

    def on_activate(self, event):
        try:
            if event.GetActive():
                self._render()
                self._initial_focus_given = False
                wx.CallAfter(self.force_initial_focus)
                wx.CallLater(150, self.force_initial_focus)
        except Exception:
            logging.debug("Could not restore first-run assistant focus on activation.", exc_info=True)
        event.Skip()

    def _nudge_dialog_foreground(self):
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
            logging.debug("Could not nudge first-run assistant to Windows foreground.", exc_info=True)

    def on_toggle_advanced_doorbell(self, event):
        self._show_advanced_doorbell = self.advanced_doorbell_chk.GetValue()
        self._apply_advanced_doorbell_visibility()
        parent = self.advanced_doorbell_chk.GetParent()
        if parent:
            parent.Layout()
        self.Layout()

    def _apply_advanced_doorbell_visibility(self):
        show = bool(getattr(self, "_show_advanced_doorbell", False))
        for widget in getattr(self, "_advanced_doorbell_widgets", []):
            widget.Show(show)

    def _initial_status(self):
        base = "\n".join(
            [
                "Home Assistant Setup Assistant",
                "",
                "Recommended path:",
                "1. If you already have Home Assistant, press Find Home Assistant.",
                "2. If you need a new Home Assistant server on this PC, install VirtualBox, then press Download And Install Home Assistant VM.",
                "3. Press Start Home Assistant VM.",
                "4. Press Find Home Assistant, then Open Home Assistant.",
                "5. Finish Home Assistant onboarding in your browser.",
                "6. Create a long-lived access token.",
                "7. Return here and continue to Viper setup.",
                "",
                "The easiest always-on hardware path is Home Assistant Green or a dedicated mini PC. VirtualBox is useful for trying Home Assistant on this Windows computer.",
            ]
        )
        if self._setup_progress_state.get("phase") or self._setup_progress_state.get("status"):
            return _format_setup_progress_state(self._setup_progress_state) + "\n\n" + base
        return base

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("ha-install")
            return
        event.Skip()

    def on_check_pc(self, event):
        self.status_txt.SetValue("Checking this PC and looking for Home Assistant...")
        safe_submit(self._run_check_pc)

    def _run_check_pc(self):
        platform_status = get_ha_vm_platform_status()
        virtualization = get_windows_virtualization_status()
        vbox = get_virtualbox_status()
        winget = get_winget_status()
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        found = discovery.find_home_assistant(
            token=ha_settings.get("ha_token") or None,
            seed_host=ha_settings.get("ha_ip") or "",
            seed_port=ha_settings.get("ha_port") or "8123",
            timeout=2,
        )
        vm_exists = _vbox_vm_exists(HA_VM_NAME) if vbox.get("installed") else False
        wx.CallAfter(self._finish_check_pc, vbox, winget, vm_exists, found, platform_status, virtualization)

    def _finish_check_pc(self, vbox, winget, vm_exists, found, platform_status=None, virtualization=None):
        if getattr(self, "_destroyed", False):
            return
        lines = ["Home Assistant Setup Assistant", ""]
        platform_status = platform_status or get_ha_vm_platform_status()
        virtualization = virtualization or get_windows_virtualization_status()
        lines.append(f"Computer architecture: {platform_status.get('architecture', 'unknown')}.")
        lines.append(platform_status.get("message", ""))
        if virtualization.get("is_windows"):
            lines.append(virtualization.get("message", ""))
            if virtualization.get("needs_attention"):
                lines.append("Optional stability step: press Optimize Windows For VirtualBox, then reboot Windows.")
        lines.append("")
        if winget.get("installed"):
            lines.append(f"winget: found. {winget.get('version') or winget.get('path')}")
        else:
            lines.append("winget: not found. Viper can still open the official VirtualBox download page.")

        if vbox.get("installed"):
            lines.append(f"VirtualBox: found. {vbox.get('version') or vbox.get('path')}")
            lines.append(f'Home Assistant VM: {"found" if vm_exists else "not found yet"}.')
        else:
            lines.append("VirtualBox: not found. Press Install VirtualBox With Winget, or use Open VirtualBox Download.")

        if found.get("ok"):
            self.parent.config["ha_ip"] = found.get("ha_ip", "")
            self.parent.config["ha_port"] = found.get("ha_port", "8123")
            self.parent.save_config()
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
            if not platform_status.get("supported"):
                lines.append("Automatic VirtualBox/HAOS VM install is unavailable on this machine. Use the official Home Assistant install guide or connect to an existing Home Assistant server.")
            elif not vbox.get("installed"):
                lines.append("If Home Assistant is not installed, install VirtualBox first.")
            elif not vm_exists:
                lines.append("Next beginner step: press Download And Install Home Assistant VM.")
            else:
                lines.append("Next beginner step: press Start Home Assistant VM, wait several minutes, then Find Home Assistant again.")
        lines.append("")
        lines.append("Viper uses official sources only: winget for VirtualBox and Home Assistant's official GitHub release for the HAOS VirtualBox image.")
        self.status_txt.SetValue("\n".join(lines))

    def _confirm_windows_virtualbox_optimization(self):
        message = (
            "This will turn off Windows hypervisor features so VirtualBox can run Home Assistant with direct hardware virtualization.\n\n"
            "This can affect WSL2, Docker Desktop, Windows Sandbox, and Hyper-V virtual machines until you re-enable those Windows features.\n\n"
            "Windows must be rebooted after the change. Continue?"
        )
        with wx.MessageDialog(self, message, "Optimize Windows For VirtualBox", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) as dlg:
            return dlg.ShowModal() == wx.ID_YES

    def on_optimize_windows_virtualbox(self, event):
        if not self._confirm_windows_virtualbox_optimization():
            self.status_txt.SetValue("Windows optimization was cancelled. No Windows settings were changed.")
            return
        self.status_txt.SetValue(
            "Optimizing Windows for VirtualBox.\n\n"
            "Viper is turning off Hyper-V and related Windows hypervisor features. This requires administrator permission and a reboot."
        )
        self.status_txt.SetFocus()
        self._thread_status("Starting Windows VirtualBox optimization. No terminal window should appear.")
        safe_submit(self._run_optimize_windows_virtualbox)

    def _run_optimize_windows_virtualbox(self):
        result = optimize_windows_for_virtualbox(progress=self._thread_status)
        wx.CallAfter(self._finish_optimize_windows_virtualbox, result)

    def _finish_optimize_windows_virtualbox(self, result):
        if getattr(self, "_destroyed", False):
            return
        lines = ["Windows VirtualBox optimization result", "", result.get("message", "No result message.")]
        output = (result.get("output") or "").strip()
        if output:
            lines.extend(["", "Command output:", output[-2500:]])
        if result.get("reboot_required"):
            lines.extend(["", "Next step: reboot Windows, then open Viper and continue setup."])
        elif result.get("needs_admin"):
            lines.extend(["", "Next step: close Viper, right-click Viper Vision, choose Run as administrator, then press this button again."])
        self.status_txt.SetValue("\n".join(lines))
        self._thread_status(result.get("message", "Windows VirtualBox optimization finished."))

    def _append_status(self, line):
        if getattr(self, "_destroyed", False):
            return
        try:
            if not self.IsShown():
                self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
        except Exception:
            logging.debug("Could not keep Home Assistant setup assistant visible during progress update.", exc_info=True)
        try:
            HA_INSTALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with HA_INSTALL_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
        except Exception:
            logging.debug("Could not write Home Assistant install progress log.", exc_info=True)
        self._setup_progress_state = _classify_setup_progress_message(line, self._setup_progress_state)
        self._progress_log_lines.append(str(line))
        self._progress_log_lines = self._progress_log_lines[-40:]
        try:
            self.parent.config["setup_progress"] = dict(self._setup_progress_state)
            self.parent.save_config()
        except Exception:
            logging.debug("Could not persist setup progress state.", exc_info=True)
        self.status_txt.SetValue(_format_setup_progress_state(self._setup_progress_state, self._progress_log_lines))
        self.status_txt.ShowPosition(self.status_txt.GetLastPosition())
        if self.progress_txt is not None:
            try:
                self.progress_txt.SetValue(_format_setup_progress_state(self._setup_progress_state, self._progress_log_lines))
                self.progress_txt.ShowPosition(self.progress_txt.GetLastPosition())
            except RuntimeError:
                self.progress_txt = None
                self.progress_dlg = None
        try:
            if self.progress_dlg is not None:
                self.progress_dlg.Raise()
        except Exception:
            pass

    def _thread_status(self, line):
        logging.info("[HA FIRST RUN] %s", line)
        wx.CallAfter(self._append_status, line)
        now = time.monotonic()
        if now - self._last_progress_spoken >= 8:
            self._last_progress_spoken = now
            try:
                speaker = getattr(self.parent, "_safe_speak", None)
                if callable(speaker):
                    wx.CallAfter(speaker, str(line))
            except Exception:
                pass

    def _show_progress_window(self, title, initial_message):
        self._last_progress_spoken = 0
        if self.progress_dlg is not None:
            try:
                self.progress_dlg.Destroy()
            except Exception:
                pass
        dlg = wx.Dialog(self, title=title, size=(720, 420), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.progress_txt = wx.TextCtrl(
            panel,
            value=initial_message,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 300),
        )
        self.progress_txt.SetName(f"{title} progress")
        self.progress_txt.SetToolTip("Read-only progress. Viper is working in the background. No terminal window is required.")
        sizer.Add(self.progress_txt, 1, wx.ALL | wx.EXPAND, 12)
        note = wx.StaticText(panel, label="Leave this window open while Viper works. Progress is also written to the main setup assistant.")
        note.Wrap(650)
        sizer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        panel.SetSizer(sizer)
        dlg.Show()
        self.progress_dlg = dlg
        wx.CallAfter(self.progress_txt.SetFocus)
        wx.CallAfter(self._thread_status, initial_message)

    def _finish_progress_window(self, final_message):
        if self.progress_txt is not None:
            try:
                self.progress_txt.SetValue((self.progress_txt.GetValue().rstrip() + "\n\n" + final_message).strip())
                self.progress_txt.ShowPosition(self.progress_txt.GetLastPosition())
            except RuntimeError:
                pass
        try:
            speaker = getattr(self.parent, "_safe_speak", None)
            if callable(speaker):
                wx.CallAfter(speaker, final_message)
        except Exception:
            pass

    def _ask_vm_resources(self):
        current_ram = self.parent.config.get("ha_vm_ram_mb", DEFAULT_HA_VM_RAM_MB)
        current_disk = self.parent.config.get("ha_vm_disk_gb", DEFAULT_HA_VM_DISK_GB)
        dlg = HomeAssistantVmResourcesDialog(self, current_ram, current_disk)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            ram_mb = dlg.ram_mb()
            disk_gb = dlg.disk_gb()
        finally:
            dlg.Destroy()
        self.parent.config["ha_vm_ram_mb"] = ram_mb
        self.parent.config["ha_vm_disk_gb"] = disk_gb
        self.parent.save_config()
        return {"ram_mb": ram_mb, "disk_gb": disk_gb}

    def _ask_vm_ram_mb(self):
        resources = self._ask_vm_resources()
        return resources.get("ram_mb") if resources else None

    def _confirm_ha_install_preflight(self, resources):
        summary = build_ha_install_preflight_summary(resources)
        style = wx.YES_NO | wx.ICON_WARNING
        if not summary.get("drive_ok"):
            style |= wx.NO_DEFAULT
        with wx.MessageDialog(self, summary["message"], "Review Home Assistant VM Install", style) as dlg:
            return dlg.ShowModal() == wx.ID_YES

    def _begin_ha_install_preflight(self, resources, image_path=None):
        ram_mb = resources["ram_mb"]
        disk_gb = resources["disk_gb"]
        self.status_txt.SetValue(
            "Checking this PC before installing Home Assistant.\n\n"
            f"Selected settings: {ram_mb} MB RAM and {disk_gb} GB disk space.\n\n"
            "Viper is checking VirtualBox, disk space, and Windows virtualization status in the background. "
            "This window should remain responsive."
        )
        self.status_txt.SetFocus()
        wx.CallLater(100, self.force_initial_focus)
        self._thread_status("Checking Home Assistant install readiness in the background.")
        safe_submit(self._run_ha_install_preflight, dict(resources), image_path)

    def _run_ha_install_preflight(self, resources, image_path=None):
        summary = build_ha_install_preflight_summary(resources)
        wx.CallAfter(self._finish_ha_install_preflight, resources, image_path, summary)

    def _finish_ha_install_preflight(self, resources, image_path, summary):
        if getattr(self, "_destroyed", False):
            return
        style = wx.YES_NO | wx.ICON_WARNING
        if not summary.get("drive_ok"):
            style |= wx.NO_DEFAULT
        self.status_txt.SetValue(
            "Home Assistant install review is ready.\n\n"
            "A confirmation dialog is open. Choose Yes to start the install, or No to cancel."
        )
        with wx.MessageDialog(self, summary["message"], "Review Home Assistant VM Install", style) as dlg:
            proceed = dlg.ShowModal() == wx.ID_YES
        if not proceed:
            self.status_txt.SetValue("Home Assistant VM install cancelled at the review step. No VM was created.")
            self._thread_status("Home Assistant VM install cancelled at the review step.")
            return
        if image_path:
            self._start_install_ha_vm_from_image(image_path, resources)
        else:
            self._start_download_install_ha_vm(resources)

    def _start_download_install_ha_vm(self, resources):
        ram_mb = resources["ram_mb"]
        disk_gb = resources["disk_gb"]
        self.status_txt.SetValue(
            "Downloading and installing Home Assistant OS.\n\n"
            f"Viper will download the latest official Home Assistant OS VirtualBox image, create a VM named Home Assistant, configure {ram_mb} MB RAM, {disk_gb} GB disk space, 2 CPUs, and bridged networking when available. This can take several minutes.\n\n"
            "Progress stays in this Viper setup assistant. No terminal window should appear."
        )
        self.status_txt.SetFocus()
        wx.CallLater(100, self.force_initial_focus)
        self._thread_status(f"Using {ram_mb} MB RAM and {disk_gb} GB disk space. You can adjust VM resources later in VirtualBox if needed.")
        safe_submit(self._run_download_install_ha_vm, ram_mb, disk_gb)

    def _start_install_ha_vm_from_image(self, image_path, resources):
        ram_mb = resources["ram_mb"]
        disk_gb = resources["disk_gb"]
        self.status_txt.SetValue(
            f"Installing Home Assistant VM from selected image with {ram_mb} MB RAM and {disk_gb} GB disk space:\n{image_path}\n\n"
            "Progress stays in this Viper setup assistant. No terminal window should appear."
        )
        self.status_txt.SetFocus()
        wx.CallLater(100, self.force_initial_focus)
        self._thread_status("Progress stays in this Viper setup assistant. No terminal window should appear.")
        safe_submit(self._run_install_ha_vm_from_image, image_path, ram_mb, disk_gb)

    def on_install_virtualbox_winget(self, event):
        platform_status = get_ha_vm_platform_status()
        if not platform_status.get("supported"):
            self.status_txt.SetValue(
                platform_status["message"]
                + "\n\nViper opened the official Home Assistant install page. Choose a supported Home Assistant install path for this machine."
            )
            open_official_link("ha_install")
            return
        self.status_txt.SetValue(
            "Installing VirtualBox with winget.\n\n"
            "Windows may ask for administrator permission. If winget is missing or the install fails, Viper will guide you to the official VirtualBox download page."
        )
        self.status_txt.SetFocus()
        self._thread_status("Progress stays in this Viper setup assistant. No terminal window should appear.")
        safe_submit(self._run_install_virtualbox_winget)

    def _run_install_virtualbox_winget(self):
        result = install_virtualbox_with_winget(progress=self._thread_status)
        wx.CallAfter(self._finish_install_virtualbox_winget, result)

    def _finish_install_virtualbox_winget(self, result):
        if getattr(self, "_destroyed", False):
            return
        lines = ["VirtualBox winget install result", "", result.get("message", "No result message.")]
        output = (result.get("output") or "").strip()
        if output:
            lines.extend(["", "winget output:", output[-2000:]])
        if result.get("open_download"):
            lines.append("")
            lines.append("Viper opened the official VirtualBox download page.")
            open_official_link("virtualbox")
        lines.append("")
        lines.append("Next step: press Check This PC. If VirtualBox is found, press Download And Install Home Assistant VM.")
        self.status_txt.SetValue("\n".join(lines))
        self._thread_status(result.get("message", "VirtualBox install finished."))

    def on_download_install_ha_vm(self, event):
        platform_status = get_ha_vm_platform_status()
        if not platform_status.get("supported"):
            self.status_txt.SetValue(
                platform_status["message"]
                + "\n\nThe automatic VirtualBox install path is for Windows x64 only. Viper opened the official Home Assistant install page."
            )
            open_official_link("ha_install")
            return
        if not get_virtualbox_status().get("installed"):
            self.status_txt.SetValue(
                "VirtualBox is not installed yet.\n\n"
                "Press Install VirtualBox With Winget, or press Open VirtualBox Download and install it manually."
            )
            return
        resources = self._ask_vm_resources()
        if not resources:
            self.status_txt.SetValue("Home Assistant VM install cancelled. No VM settings were changed.")
            return
        self._begin_ha_install_preflight(resources)

    def _run_download_install_ha_vm(self, ram_mb, disk_gb):
        result = download_and_install_home_assistant_vm(progress=self._thread_status, ram_mb=ram_mb, disk_gb=disk_gb)
        if result.get("ok"):
            self._thread_status("Home Assistant VM is installed. Starting the VM now.")
            result["start_result"] = self._start_and_wait_for_ha()
        wx.CallAfter(self._finish_download_install_ha_vm, result)

    def _finish_download_install_ha_vm(self, result):
        if getattr(self, "_destroyed", False):
            return
        lines = ["Home Assistant VM install result", "", result.get("message", "No result message.")]
        if result.get("ok"):
            start_result = result.get("start_result") or {}
            lines.extend(["", "Home Assistant VM start and first boot result:", start_result.get("message", "No start result message.")])
            first_boot = start_result.get("first_boot") or {}
            if first_boot.get("ok"):
                lines.extend(["", "Home Assistant is ready. Press Open Home Assistant to complete onboarding, then Continue To Viper Setup."])
            elif first_boot:
                lines.extend(["", first_boot.get("message", "Home Assistant is still booting. Press Find Home Assistant later.")])
            else:
                lines.extend(["", "Next step: press Start Home Assistant VM. First boot can take several minutes."])
        else:
            lines.append("")
            lines.append("Fallback: press Open HA OS Download, download the VirtualBox image manually, then press Choose Downloaded HA OS Image.")
        self._progress_log_lines.extend(lines)
        self._progress_log_lines = self._progress_log_lines[-40:]
        self.status_txt.SetValue(_format_setup_progress_state(self._setup_progress_state, self._progress_log_lines))
        self._thread_status(result.get("message", "Home Assistant VM install finished."))

    def on_choose_haos_image(self, event):
        platform_status = get_ha_vm_platform_status()
        if not platform_status.get("supported"):
            self.status_txt.SetValue(
                platform_status["message"]
                + "\n\nViper will not import a VirtualBox HAOS image automatically on this machine. Use the official Home Assistant install guide instead."
            )
            open_official_link("ha_install")
            return
        with wx.FileDialog(
            self,
            "Choose Home Assistant OS VirtualBox image",
            wildcard="Home Assistant OS image (*.zip;*.vdi;*.ova)|*.zip;*.vdi;*.ova|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        resources = self._ask_vm_resources()
        if not resources:
            self.status_txt.SetValue("Home Assistant VM install cancelled. No VM settings were changed.")
            return
        self._begin_ha_install_preflight(resources, image_path=path)

    def _run_install_ha_vm_from_image(self, path, ram_mb, disk_gb):
        result = install_home_assistant_vm_from_image(path, progress=self._thread_status, ram_mb=ram_mb, disk_gb=disk_gb)
        if result.get("ok"):
            self._thread_status("Home Assistant VM is installed. Starting the VM now.")
            result["start_result"] = self._start_and_wait_for_ha()
        wx.CallAfter(self._finish_download_install_ha_vm, result)

    def on_start_ha_vm(self, event):
        self.status_txt.SetValue("Starting Home Assistant VM. Viper will keep checking for first boot readiness for up to 25 minutes.")
        self.status_txt.SetFocus()
        wx.CallLater(100, self.force_initial_focus)
        self._thread_status("Progress stays in this Viper setup assistant. No terminal window should appear.")
        safe_submit(self._run_start_ha_vm)

    def _run_start_ha_vm(self):
        result = self._start_and_wait_for_ha()
        wx.CallAfter(self._finish_start_ha_vm, result)

    def _start_and_wait_for_ha(self):
        result = start_home_assistant_vm(progress=self._thread_status)
        if not result.get("ok"):
            return result
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        self._thread_status("Home Assistant VM started. Waiting for the Home Assistant web interface to finish first boot.")
        first_boot = wait_for_home_assistant_first_boot(
            progress=self._thread_status,
            token=ha_settings.get("ha_token") or None,
            seed_host=ha_settings.get("ha_ip") or "",
            seed_port=ha_settings.get("ha_port") or "8123",
            timeout_seconds=1500,
            interval_seconds=15,
        )
        result["first_boot"] = first_boot
        if first_boot.get("ok"):
            result["message"] = first_boot.get("message") or result.get("message")
        return result

    def _finish_start_ha_vm(self, result):
        if getattr(self, "_destroyed", False):
            return
        lines = ["Home Assistant VM start result", "", result.get("message", "No result message.")]
        lines.append("")
        if result.get("ok"):
            first_boot = result.get("first_boot") or {}
            if first_boot.get("ok"):
                self.parent.config["ha_ip"] = first_boot.get("ha_ip", "")
                self.parent.config["ha_port"] = first_boot.get("ha_port", "8123")
                self.parent.save_config()
                lines.append("Home Assistant is ready. Press Open Home Assistant to complete onboarding, then Continue To Viper Setup.")
                self._thread_status("Home Assistant is ready. You can open it now.")
            elif first_boot:
                lines.append(first_boot.get("message", "Home Assistant is still booting. Press Find Home Assistant later."))
            else:
                lines.append("Home Assistant VM started. Viper did not receive a first boot status. Press Find Home Assistant after a few minutes.")
        else:
            lines.append("If this mentions virtualization or VT-x, make sure VirtualBox is installed correctly and Hyper-V is not blocking VirtualBox.")
        self._progress_log_lines.extend(lines)
        self._progress_log_lines = self._progress_log_lines[-40:]
        self.status_txt.SetValue(_format_setup_progress_state(self._setup_progress_state, self._progress_log_lines))
        self._thread_status(result.get("message", "Home Assistant VM start finished."))

    def on_open_found_ha(self, event):
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        host = (ha_settings.get("ha_ip") or "homeassistant.local").strip()
        port = (ha_settings.get("ha_port") or "8123").strip()
        if not re.match(r"^https?://", host, re.IGNORECASE):
            url = f"http://{host}:{port}"
        else:
            url = host
        if open_url(url):
            self.status_txt.SetValue(
                f"Opened Home Assistant in your browser:\n{url}\n\n"
                "Complete onboarding, create your owner account, then create a long-lived access token. Return to Viper and press Continue To Viper Setup."
            )
        else:
            self.status_txt.SetValue(f"Viper could not open the browser. Manually open this address:\n{url}")

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
        if getattr(self, "_destroyed", False):
            return
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
        owner = getattr(self, "parent", None)
        try:
            if owner is not None:
                if getattr(owner, "_ha_server_assistant_dialog", None) is self:
                    owner._ha_server_assistant_dialog = None
                wx.CallAfter(owner.show_initial_setup_assistant)
        finally:
            self._destroyed = True
            self.Destroy()


def _dialog_status(dialog, message, *, announce=False):
    setter = getattr(dialog, "_set_setup_status", None)
    if callable(setter):
        setter(message, announce=announce)
        return
    status = getattr(dialog, "status_txt", None)
    if status is not None and hasattr(status, "SetValue"):
        status.SetValue(message)


class RingMqttLoginDialog(wx.Dialog):
    def __init__(self, parent, url, ha_login_url=""):
        super().__init__(parent, title="Ring-MQTT Login Guide", size=(880, 620))
        self.parent = parent
        self.login_url = url or ""
        self.ha_login_url = ha_login_url or ""
        self.current_url = self.ha_login_url or self.login_url
        self.webview = None

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        status = (
            "Ring-MQTT Login Guide\n\n"
            "Viper installed or checked Mosquitto Broker and Ring-MQTT with Video Streaming. "
            "For screen reader accessibility, Viper opens Home Assistant in your normal web browser. "
            "Viper will open the Ring-MQTT app page automatically. If Home Assistant asks you to sign in, sign in there. "
            "The Ring-MQTT login page usually does not open directly. On the Ring-MQTT app page, tab to the Open Web UI button and activate it. "
            "That Home Assistant button opens the actual Ring-MQTT Ring login and setup page. "
            "If Home Assistant lands somewhere else after login, return to this dialog and press Open Ring-MQTT App Page In Browser again. "
            "Viper does not collect or store your Ring email, password, two factor code, or refresh token. "
            "When Ring-MQTT says Ring login is complete, return to this dialog and press I Finished Ring Login."
        )
        self.status_txt = wx.TextCtrl(panel, value=status, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status_txt.SetName("Ring-MQTT login guide instructions")
        self.status_txt.SetToolTip("Accessible instructions for opening Home Assistant and Ring-MQTT in your normal browser.")
        sizer.Add(self.status_txt, 1, wx.ALL | wx.EXPAND, 10)

        url_text = (
            f"Ring-MQTT app page:\n{self.ha_login_url or 'not available'}\n\n"
            "Use the Open Web UI button on that Home Assistant app page to reach the Ring login.\n\n"
            f"Direct Ring-MQTT web UI attempt, optional:\n{self.login_url or 'not available'}"
        )
        self.url_txt = wx.TextCtrl(panel, value=url_text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.url_txt.SetName("Ring-MQTT browser links")
        self.url_txt.SetToolTip("Read-only Home Assistant and Ring-MQTT links.")
        sizer.Add(self.url_txt, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        buttons = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        buttons.AddGrowableCol(0, 1)
        buttons.AddGrowableCol(1, 1)
        self.btn_ha_login = wx.Button(panel, label="Open Ring-MQTT App Page In Browser")
        self.btn_ring_login = wx.Button(panel, label="Try Direct Ring-MQTT Web UI In Browser")
        self.btn_copy = wx.Button(panel, label="Copy Ring-MQTT Page Link")
        self.btn_finished = wx.Button(panel, label="I Finished Ring Login")
        self.btn_find_streams = wx.Button(panel, label="Find And Test Doorbell Cameras")
        self.btn_try_embedded = wx.Button(panel, label="Try Embedded Browser")
        self.btn_help = wx.Button(panel, label="Help")
        self.btn_close = wx.Button(panel, label="Close")
        for btn in (
            self.btn_ha_login,
            self.btn_ring_login,
            self.btn_copy,
            self.btn_finished,
            self.btn_find_streams,
            self.btn_try_embedded,
            self.btn_help,
            self.btn_close,
        ):
            btn.SetName(btn.GetLabel())
            buttons.Add(btn, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_ha_login.Bind(wx.EVT_BUTTON, self.on_ha_login)
        self.btn_ring_login.Bind(wx.EVT_BUTTON, self.on_ring_login)
        self.btn_copy.Bind(wx.EVT_BUTTON, self.on_copy_ring_link)
        self.btn_finished.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_OK))
        self.btn_find_streams.Bind(wx.EVT_BUTTON, self.on_find_streams)
        self.btn_try_embedded.Bind(wx.EVT_BUTTON, self.on_try_embedded)
        self.btn_help.Bind(wx.EVT_BUTTON, lambda _event: open_help("ring-mqtt-setup"))
        self.btn_close.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CANCEL))
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        wx.CallAfter(self.status_txt.SetFocus)
        wx.CallAfter(self.open_initial_home_assistant_page)

    def _announce_browser_result(self, label, ok):
        if ok:
            self.url_txt.SetValue(
                f"{label} was sent to your default browser.\n\n"
                f"Ring-MQTT app page:\n{self.ha_login_url or 'not available'}\n\n"
                "Use the Open Web UI button on that Home Assistant app page to reach the Ring login.\n\n"
                f"Direct Ring-MQTT web UI attempt, optional:\n{self.login_url or 'not available'}"
            )
        else:
            self.url_txt.SetValue(
                f"Viper could not open {label} automatically. Copy the link below and open it in your browser.\n\n"
                f"Ring-MQTT app page:\n{self.ha_login_url or 'not available'}\n\n"
                "Use the Open Web UI button on that Home Assistant app page to reach the Ring login.\n\n"
                f"Direct Ring-MQTT web UI attempt, optional:\n{self.login_url or 'not available'}"
            )

    def open_initial_home_assistant_page(self):
        target = self.ha_login_url or self.login_url
        if not target:
            return
        self.current_url = target
        self._announce_browser_result("Ring-MQTT app page", open_url(target))

    def on_ha_login(self, event):
        target = self.ha_login_url or self.login_url
        self.current_url = target
        if target:
            self._announce_browser_result("Ring-MQTT app page", open_url(target))

    def on_ring_login(self, event):
        target = self.login_url
        self.current_url = target
        if target:
            self._announce_browser_result("direct Ring-MQTT web UI attempt", open_url(target))

    def on_copy_ring_link(self, event):
        if not self.login_url:
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self.login_url))
            finally:
                wx.TheClipboard.Close()

    def on_find_streams(self, event):
        wizard_finder = getattr(self.parent, "_start_wizard_live_stream_discovery", None)
        if callable(wizard_finder):
            try:
                wizard_finder()
                self.status_txt.SetValue(
                    "Viper started finding and testing doorbell cameras in the setup wizard.\n\n"
                    "You can close this Ring-MQTT guide and return to the wizard for the results."
                )
            except Exception as e:
                logging.exception("Ring-MQTT guide could not start wizard stream discovery.")
                self.status_txt.SetValue(
                    "Viper could not start doorbell camera discovery from the setup wizard.\n\n"
                    f"Error: {e}\n\n"
                    "Close this guide and use the Test Doorbell Cameras step in the setup wizard."
                )
            return

        finder = getattr(self.parent, "on_find_live_rtsp_streams", None)
        if callable(finder):
            finder(event)
            self.status_txt.SetValue(
                "Viper started finding Ring-MQTT streams.\n\n"
                "Results will appear in the Home Assistant setup window."
            )
            return

        self.status_txt.SetValue(
            "Viper could not start stream discovery from this window.\n\n"
            "Close this guide and use the Test Doorbell Cameras step in the setup wizard."
        )

    def on_try_embedded(self, event):
        if wxhtml2 is None:
            wx.MessageBox(
                "The embedded browser is not available on this PC. Use the normal browser buttons instead.",
                "Embedded Browser Unavailable",
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        url = self.current_url or self.ha_login_url or self.login_url
        if not url:
            return
        dlg = wx.Dialog(self, title="Embedded Browser Preview", size=(980, 760))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        warning = wx.TextCtrl(
            panel,
            value="This embedded browser may not work well with JAWS or NVDA. Use the normal browser path if it is not accessible.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        warning.SetName("Embedded browser accessibility warning")
        sizer.Add(warning, 0, wx.ALL | wx.EXPAND, 10)
        browser = wxhtml2.WebView.New(panel)
        browser.LoadURL(url)
        sizer.Add(browser, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        close = wx.Button(panel, label="Close Embedded Browser")
        close.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        sizer.Add(close, 0, wx.ALL | wx.EXPAND, 10)
        panel.SetSizer(sizer)
        dlg.ShowModal()
        dlg.Destroy()

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("ring-mqtt-setup")
            return
        event.Skip()


class DiscoveredSpeakersDialog(wx.Dialog):
    def __init__(self, parent, speaker_targets, summary_text):
        super().__init__(parent, title="Choose Speakers To Add", size=(760, 640))
        self.selected_targets = []
        self._checks = []
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        instructions = wx.TextCtrl(
            panel,
            value=(
                "Choose speakers to add to Viper.\n\n"
                "Tab through each speaker. Press Space to check or uncheck it. "
                "Already configured speakers are shown but disabled. Nothing is saved until you press Add Selected Speakers."
            ),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 105),
        )
        instructions.SetName("Choose speakers instructions")
        sizer.Add(instructions, 0, wx.ALL | wx.EXPAND, 10)

        scroll = wx.ScrolledWindow(panel, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
        scroll.SetScrollRate(0, 20)
        scroll_sizer = wx.BoxSizer(wx.VERTICAL)
        for item in speaker_targets:
            name = item.get("name") or "Unnamed speaker"
            spk_type = item.get("type") or "ha"
            spk_id = item.get("id") or ""
            source = item.get("source") or "discovery"
            configured = bool(item.get("configured"))
            label = f"{name}, {spk_type}, {spk_id}, {source}"
            if configured:
                label += ", already configured"
            check = wx.CheckBox(scroll, label=label)
            check.SetName(label)
            check.SetToolTip(label)
            check.SetValue(False)
            check.Enable(not configured)
            check._viper_speaker_target = item
            self._checks.append(check)
            scroll_sizer.Add(check, 0, wx.ALL | wx.EXPAND, 5)
        if not speaker_targets:
            none = wx.StaticText(scroll, label="No speakers were found.")
            none.SetName("No speakers were found")
            scroll_sizer.Add(none, 0, wx.ALL | wx.EXPAND, 8)
        scroll.SetSizer(scroll_sizer)
        sizer.Add(scroll, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        routing_box = wx.StaticBox(panel, label="Routes For Newly Added Speakers")
        routing_sizer = wx.StaticBoxSizer(routing_box, wx.VERTICAL)
        self.route_doorbell_chk = wx.CheckBox(panel, label="Use selected speakers for doorbell alerts")
        self.route_utilities_chk = wx.CheckBox(panel, label="Use selected speakers for utility announcements")
        self.route_fridge_chk = wx.CheckBox(panel, label="Use selected speakers for fridge and freezer alerts")
        self.route_quiet_exempt_chk = wx.CheckBox(panel, label="Allow selected speakers during quiet hours")
        for check in (self.route_doorbell_chk, self.route_utilities_chk, self.route_fridge_chk):
            check.SetValue(True)
        self.route_quiet_exempt_chk.SetValue(False)
        for check in (self.route_doorbell_chk, self.route_utilities_chk, self.route_fridge_chk, self.route_quiet_exempt_chk):
            check.SetName(check.GetLabel())
            check.SetToolTip(check.GetLabel())
            routing_sizer.Add(check, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(routing_sizer, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)

        summary = wx.TextCtrl(panel, value=summary_text, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 120))
        summary.SetName("Speaker discovery details")
        sizer.Add(summary, 0, wx.ALL | wx.EXPAND, 10)

        buttons = wx.FlexGridSizer(rows=0, cols=3, vgap=6, hgap=6)
        buttons.AddGrowableCol(0, 1)
        buttons.AddGrowableCol(1, 1)
        buttons.AddGrowableCol(2, 1)
        add_btn = wx.Button(panel, label="Add Selected Speakers")
        close_btn = wx.Button(panel, label="Close Without Adding")
        help_btn = wx.Button(panel, label="Help")
        add_btn.SetName("Add Selected Speakers")
        close_btn.SetName("Close Without Adding")
        help_btn.SetName("Help")
        add_btn.Bind(wx.EVT_BUTTON, self.on_add)
        close_btn.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CANCEL))
        help_btn.Bind(wx.EVT_BUTTON, lambda _event: open_help("speakers"))
        for button in (add_btn, close_btn, help_btn):
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        wx.CallAfter(instructions.SetFocus)
        wx.CallLater(75, self._force_initial_focus)
        wx.CallLater(250, self._force_initial_focus)

    def _force_initial_focus(self):
        try:
            self.Raise()
            focus_target = next((check for check in self._checks if check.IsEnabled()), None)
            if focus_target is None and self._checks:
                focus_target = self._checks[0]
            if focus_target is not None:
                focus_target.SetFocus()
            else:
                self.SetFocus()
        except Exception:
            logging.debug("Could not focus discovered speakers dialog.", exc_info=True)

    def _on_activate(self, event):
        if event.GetActive():
            wx.CallAfter(self._force_initial_focus)
        event.Skip()

    def on_add(self, event):
        self.selected_targets = [
            check._viper_speaker_target
            for check in self._checks
            if check.IsEnabled() and check.GetValue()
        ]
        self.selected_routes = {
            "doorbell": self.route_doorbell_chk.GetValue(),
            "utilities": self.route_utilities_chk.GetValue(),
            "fridge": self.route_fridge_chk.GetValue(),
            "quiet_hours_exempt": self.route_quiet_exempt_chk.GetValue(),
        }
        self.EndModal(wx.ID_OK)

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("speakers")
            return
        event.Skip()


class HomeAssistantSetupDialog(wx.Dialog):
    def __init__(self, parent, *, use_env_prefill=True):
        super().__init__(None, title="Advanced Home Assistant Setup", size=(860, 860))
        self.parent = parent
        self._destroyed = False
        self._initial_focus_given = False
        self.discovery_result = None
        self.ring_listen_cancel = None
        self._doorbell_preview_updating = False
        settings = cfg.get_ha_settings(parent.config, include_env=use_env_prefill)
        api_settings = cfg.get_api_settings(parent.config, include_env=use_env_prefill)
        doorbell_settings = cfg.get_doorbell_settings(parent.config, include_env=use_env_prefill)
        has_advanced_doorbell_values = bool(
            doorbell_settings.get("front_camera_id")
            or doorbell_settings.get("back_camera_id")
            or doorbell_settings.get("ring_topic_root")
            or doorbell_settings.get("front_doorbell_mqtt_topic")
            or doorbell_settings.get("back_doorbell_mqtt_topic")
            or doorbell_settings.get("mqtt_username")
            or doorbell_settings.get("mqtt_password")
        )
        self._show_advanced_doorbell = bool(
            parent.config.get("show_advanced_ring_mqtt", has_advanced_doorbell_values)
        )
        self._advanced_doorbell_widgets = []
        self._last_derived_values = {}
        self._front_trigger_initial = ""
        self._back_trigger_initial = ""
        self._verified_rtsp_urls = set()
        self._trusted_rtsp_urls = set()
        self._auto_ha_find_done = bool(settings.get("ha_ip"))
        self._ha_find_failed = False
        self._devices_discovered = False
        self._show_discover_devices = False
        self._auto_speaker_discovery_done = False
        self._record_setup_event("dialog_open", "Home Assistant setup dialog opened.")

        triggers = parent.config.get("doorbell_triggers", {})
        front_trigger = triggers.get("front", {}) if isinstance(triggers, dict) else {}
        back_trigger = triggers.get("back", {}) if isinstance(triggers, dict) else {}
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._setup_page_names = ["Home Assistant", "Doorbell Vision", "Ring-MQTT Advanced", "Final Checks"]
        self._setup_page_indexes = {
            "connect": 0,
            "home assistant": 0,
            "ha": 0,
            "doorbells": 1,
            "doorbell vision": 1,
            "ring": 2,
            "ring-mqtt": 2,
            "ring-mqtt advanced": 2,
            "finish": 3,
            "final checks": 3,
        }
        header = wx.BoxSizer(wx.HORIZONTAL)
        self.setup_page_title = wx.StaticText(panel, label="Home Assistant (1 of 4)")
        title_font = self.setup_page_title.GetFont()
        title_font.SetPointSize(12)
        title_font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.setup_page_title.SetFont(title_font)
        self.setup_page_title.SetName("Setup page title")
        header.Add(self.setup_page_title, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        self.btn_setup_page_back = wx.Button(panel, label="Back")
        self.btn_setup_page_next = wx.Button(panel, label="Next")
        self.btn_setup_page_back.SetName("Back setup page")
        self.btn_setup_page_next.SetName("Next setup page")
        self.btn_setup_page_back.Bind(wx.EVT_BUTTON, self.on_setup_page_back)
        self.btn_setup_page_next.Bind(wx.EVT_BUTTON, self.on_setup_page_next)
        header.Add(self.btn_setup_page_back, 0, wx.ALL, 5)
        header.Add(self.btn_setup_page_next, 0, wx.ALL, 5)
        sizer.Add(header, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 8)

        notebook = wx.Simplebook(panel)
        self.notebook = notebook
        notebook.SetName("Home Assistant setup wizard pages")
        connect_page = wx.Panel(notebook)
        doorbell_page = wx.Panel(notebook)
        ring_page = wx.Panel(notebook)
        finish_page = wx.Panel(notebook)
        connect_sizer = wx.BoxSizer(wx.VERTICAL)
        doorbell_sizer = wx.BoxSizer(wx.VERTICAL)
        ring_sizer = wx.BoxSizer(wx.VERTICAL)
        finish_sizer = wx.BoxSizer(wx.VERTICAL)

        def add_labeled_control(container, parent_window, label, factory, *, description=""):
            label_ctrl = wx.StaticText(parent_window, label=label)
            label_ctrl.SetName(label)
            container.Add(label_ctrl, 0, wx.TOP | wx.LEFT | wx.RIGHT | wx.EXPAND, 6)
            control = factory(parent_window)
            control._viper_label_ctrl = label_ctrl
            control.SetName(label)
            control.SetToolTip(description or label)
            container.Add(control, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)
            return control

        def add_text_row(container, parent_window, label, value="", *, description=""):
            return add_labeled_control(
                container,
                parent_window,
                label,
                lambda owner: wx.TextCtrl(owner, value=value),
                description=description,
            )

        def add_password_row(container, parent_window, label, value="", *, description=""):
            return add_labeled_control(
                container,
                parent_window,
                label,
                lambda owner: wx.TextCtrl(owner, value=value, style=wx.TE_PASSWORD),
                description=description,
            )

        def add_choice_row(container, parent_window, label, *, description=""):
            return add_labeled_control(
                container,
                parent_window,
                label,
                lambda owner: wx.Choice(owner, choices=["No Home Assistant entities discovered yet"]),
                description=description,
            )

        def add_checkbox_row(container, parent_window, label, *, description=""):
            control = wx.CheckBox(parent_window, label=label)
            control.SetName(label)
            control.SetToolTip(description or label)
            container.Add(control, 0, wx.ALL | wx.EXPAND, 6)
            return control

        def add_page_intro(container, parent_window, name, text):
            intro = wx.TextCtrl(
                parent_window,
                value=text,
                style=wx.TE_MULTILINE | wx.TE_READONLY,
                size=(-1, 92),
            )
            self._describe_control(intro, name, text)
            container.Add(intro, 0, wx.ALL | wx.EXPAND, 8)
            return intro

        add_page_intro(
            connect_sizer,
            connect_page,
            "Home Assistant advanced setup instructions",
            "Use this page only when the beginner wizard needs manual help. Enter or confirm the Home Assistant address, token, Gemini key, and optional Pushover settings. Blank secret boxes can still be valid when values come from environment variables or Windows Credential Manager.",
        )
        add_page_intro(
            doorbell_sizer,
            doorbell_page,
            "Doorbell Vision advanced setup instructions",
            "Choose Home Assistant trigger entities and live RTSP URLs for each door. Use Ring-MQTT discovery, a verified manual URL, or an existing saved URL. Home Assistant camera snapshot entities are not live video streams.",
        )
        add_page_intro(
            ring_sizer,
            ring_page,
            "Ring-MQTT advanced setup instructions",
            "Install or open Ring-MQTT here. Advanced MQTT topics, camera IDs, and MQTT credentials stay hidden unless you check Show advanced Ring and MQTT fields.",
        )
        add_page_intro(
            finish_sizer,
            finish_page,
            "Final checks instructions",
            "Run setup checks, create a support report, then save. Save and Close live only on this final page so the setup flow has one clear finish point.",
        )

        self.ha_ip_txt = add_text_row(
            connect_sizer,
            connect_page,
            "Home Assistant IP / host",
            settings.get("ha_ip") or "",
            description="Home Assistant address. Enter the IP address or host name for Home Assistant, for example 192.168.1.50 or homeassistant.local.",
        )

        self.ha_port_txt = add_text_row(
            connect_sizer,
            connect_page,
            "Port",
            settings.get("ha_port") or "8123",
            description="Home Assistant port. Usually 8123.",
        )

        self.ha_token_txt = add_password_row(
            connect_sizer,
            connect_page,
            "Long-lived access token",
            settings.get("ha_token") or "",
            description="Home Assistant long lived access token. This lets Viper discover entities and listen for state changes. Create it in your Home Assistant user profile.",
        )

        self.ha_listener_chk = add_checkbox_row(
            connect_sizer,
            connect_page,
            "Enable direct Home Assistant listener",
            description="Direct Home Assistant listener checkbox. Keep this checked for the beginner setup. It lets Viper react to Home Assistant state changes without YAML automations.",
        )
        self.ha_listener_chk.SetValue(bool(parent.config.get("ha_listener_enabled", True)))

        self.gemini_key_txt = add_password_row(
            connect_sizer,
            connect_page,
            "Gemini API key",
            api_settings.get("gemini_api_key") or "",
            description="Optional Gemini API key. Used for live doorbell image analysis and Gemini speech. This is not required for Home Assistant entity discovery or the direct Home Assistant listener.",
        )

        self.pushover_enabled_chk = add_checkbox_row(
            connect_sizer,
            connect_page,
            "Use Pushover notifications",
            description="Optional Pushover alerts checkbox. Turn this on only if you want Viper to send phone push notifications through Pushover.",
        )
        self.pushover_enabled_chk.SetValue(bool(api_settings.get("pushover_enabled")))
        self.pushover_enabled_chk.Bind(wx.EVT_CHECKBOX, self.on_pushover_toggle)

        self.pushover_user_txt = add_password_row(
            connect_sizer,
            connect_page,
            "Pushover user key",
            api_settings.get("pushover_user_key") or "",
            description="Pushover user key. Optional. This comes from your Pushover account, not from Home Assistant.",
        )

        self.pushover_token_txt = add_password_row(
            connect_sizer,
            connect_page,
            "Pushover app token",
            api_settings.get("pushover_api_token") or "",
            description="Pushover app token. Optional. This comes from your Pushover application settings.",
        )

        self.advanced_doorbell_chk = add_checkbox_row(
            ring_sizer,
            ring_page,
            "Show advanced Ring and MQTT fields",
            description="Advanced doorbell setup checkbox. Leave this off until you need to enter Ring MQTT topics, MQTT credentials, camera IDs, or manual RTSP stream details.",
        )
        self.advanced_doorbell_chk.SetValue(self._show_advanced_doorbell)
        self.advanced_doorbell_chk.Bind(wx.EVT_CHECKBOX, self.on_toggle_advanced_doorbell)

        self.rtsp_front_txt = add_text_row(
            doorbell_sizer,
            doorbell_page,
            "Front door RTSP URL",
            doorbell_settings.get("configured_rtsp_front") or "",
            description="Front door live RTSP URL. This must be current video, not a Home Assistant snapshot.",
        )

        self.rtsp_back_txt = add_text_row(
            doorbell_sizer,
            doorbell_page,
            "Back door RTSP URL",
            doorbell_settings.get("configured_rtsp_back") or "",
            description="Back door live RTSP URL. This must be current video, not a Home Assistant snapshot.",
        )

        self.front_trigger_choice = add_choice_row(
            doorbell_sizer,
            doorbell_page,
            "Front door HA trigger entity",
            description="Front door Home Assistant trigger entity. Choose the binary sensor or sensor that changes when the front doorbell or motion event fires.",
        )
        self._front_trigger_initial = front_trigger.get("trigger_entity_id") or ""

        self.back_trigger_choice = add_choice_row(
            doorbell_sizer,
            doorbell_page,
            "Back door HA trigger entity",
            description="Back door Home Assistant trigger entity. Choose the binary sensor or sensor that changes when the back doorbell or motion event fires.",
        )
        self._back_trigger_initial = back_trigger.get("trigger_entity_id") or ""

        self.advanced_doorbell_panel = wx.Panel(ring_page)
        advanced_sizer = wx.BoxSizer(wx.VERTICAL)
        self.front_camera_id_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "Front Ring camera ID",
            doorbell_settings.get("front_camera_id") or "",
            description="Advanced front Ring camera ID. Usually leave this blank and let Viper discover or infer it.",
        )
        self.back_camera_id_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "Back Ring camera ID",
            doorbell_settings.get("back_camera_id") or "",
            description="Advanced back Ring camera ID. Usually leave this blank and let Viper discover or infer it.",
        )
        self.ring_topic_root_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "Ring topic root / location ID",
            doorbell_settings.get("ring_topic_root") or "",
            description="Advanced Ring MQTT location ID or topic root. Only needed if using ring-mqtt topics directly.",
        )
        self.front_mqtt_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "Front Ring MQTT topic",
            doorbell_settings.get("front_doorbell_mqtt_topic") or "",
            description="Advanced front Ring MQTT topic. Only needed if using ring-mqtt directly instead of Home Assistant state triggers.",
        )
        self.back_mqtt_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "Back Ring MQTT topic",
            doorbell_settings.get("back_doorbell_mqtt_topic") or "",
            description="Advanced back Ring MQTT topic. Only needed if using ring-mqtt directly instead of Home Assistant state triggers.",
        )
        self.mqtt_host_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "MQTT host",
            doorbell_settings.get("mqtt_host") or settings.get("ha_ip") or "",
            description="Advanced MQTT broker address. Usually this is your Home Assistant IP if using the Mosquitto add-on.",
        )
        self.mqtt_port_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "MQTT port",
            doorbell_settings.get("mqtt_port") or "1883",
            description="Advanced MQTT broker port. Usually 1883.",
        )
        self.mqtt_user_txt = add_text_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "MQTT username",
            doorbell_settings.get("mqtt_username") or "",
            description="Advanced MQTT username. This is the MQTT broker username, not your Home Assistant token.",
        )
        self.mqtt_password_txt = add_password_row(
            advanced_sizer,
            self.advanced_doorbell_panel,
            "MQTT password",
            doorbell_settings.get("mqtt_password") or "",
            description="Advanced MQTT password. This is the MQTT broker password, not your Home Assistant token.",
        )
        advanced_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_mqtt = wx.Button(self.advanced_doorbell_panel, label="Test MQTT")
        self.btn_ring = wx.Button(self.advanced_doorbell_panel, label="Find Ring Topics")
        advanced_buttons.Add(self.btn_mqtt, 1, wx.ALL | wx.EXPAND, 5)
        advanced_buttons.Add(self.btn_ring, 1, wx.ALL | wx.EXPAND, 5)
        advanced_sizer.Add(advanced_buttons, 0, wx.EXPAND)
        self.advanced_doorbell_panel.SetSizer(advanced_sizer)
        ring_sizer.Add(self.advanced_doorbell_panel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        self.btn_find_ha = wx.Button(connect_page, label="Find Home Assistant")
        self.btn_test = wx.Button(connect_page, label="Discover Devices")
        self.btn_install_ha = wx.Button(connect_page, label="Install Home Assistant On This PC")
        self.btn_beginner_setup = wx.Button(connect_page, label="Run Beginner Auto Setup")
        self.btn_change_doorbell_triggers_now = wx.Button(panel, label="Change Doorbell Triggers")
        self.btn_find_ring_mqtt_streams_now = wx.Button(panel, label="Find Ring MQTT Streams Now")
        self.btn_change_camera_streams_now = wx.Button(panel, label="Change Camera Streams")
        self.btn_test_front_rtsp_now = wx.Button(panel, label="Test Front Camera Now")
        self.btn_test_back_rtsp_now = wx.Button(panel, label="Test Back Camera Now")
        self.btn_install_ring_mqtt = wx.Button(ring_page, label="Install Ring MQTT Requirements")
        self.btn_ring_help = wx.Button(ring_page, label="Ring Setup Assistant")
        self.btn_discover_setup_speakers = wx.Button(finish_page, label="Discover Available Speakers")
        self.btn_setup_summary = wx.Button(finish_page, label="Show Setup Summary")
        self.btn_setup_test_everything = wx.Button(finish_page, label="Test Everything")
        self.btn_setup_support_report = wx.Button(finish_page, label="Create Support Report To Email Developer")
        self.btn_help = wx.Button(finish_page, label="Help")
        self.btn_save = wx.Button(finish_page, label="Save")
        self.btn_close = wx.Button(finish_page, label="Close")
        self.btn_find_ha.Bind(wx.EVT_BUTTON, self.on_find_ha)
        self.btn_install_ha.Bind(wx.EVT_BUTTON, self.on_install_home_assistant_from_setup)
        self.btn_beginner_setup.Bind(wx.EVT_BUTTON, self.on_beginner_auto_setup)
        self.btn_test.Bind(wx.EVT_BUTTON, self.on_test)
        self.btn_change_doorbell_triggers_now.Bind(wx.EVT_BUTTON, self.on_change_doorbell_triggers_now)
        self.btn_find_ring_mqtt_streams_now.Bind(wx.EVT_BUTTON, self.on_find_live_rtsp_streams)
        self.btn_change_camera_streams_now.Bind(wx.EVT_BUTTON, self.on_change_camera_streams_now)
        self.btn_test_front_rtsp_now.Bind(wx.EVT_BUTTON, lambda event: self.on_test_rtsp(event, "front"))
        self.btn_test_back_rtsp_now.Bind(wx.EVT_BUTTON, lambda event: self.on_test_rtsp(event, "back"))
        self.btn_install_ring_mqtt.Bind(wx.EVT_BUTTON, self.on_install_ring_mqtt_requirements)
        self.btn_mqtt.Bind(wx.EVT_BUTTON, self.on_test_mqtt)
        self.btn_ring.Bind(wx.EVT_BUTTON, self.on_find_ring_topics)
        self.btn_ring_help.Bind(wx.EVT_BUTTON, self.on_ring_setup_assistant)
        self.btn_discover_setup_speakers.Bind(wx.EVT_BUTTON, self.on_discover_setup_speakers)
        self.btn_setup_summary.Bind(wx.EVT_BUTTON, self.on_show_setup_summary)
        self.btn_setup_test_everything.Bind(wx.EVT_BUTTON, self.on_setup_test_everything)
        self.btn_setup_support_report.Bind(wx.EVT_BUTTON, self.parent.on_create_support_report)
        self.btn_help.Bind(wx.EVT_BUTTON, lambda _event: open_help("index"))
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save)
        self.btn_close.Bind(wx.EVT_BUTTON, self.on_close_setup)
        button_descriptions = {
            self.btn_find_ha: "Find Home Assistant button. This appears only if Viper did not find Home Assistant automatically, or if Home Assistant still needs to be installed.",
            self.btn_install_ha: "Install Home Assistant On This PC button. Opens the Home Assistant server assistant with VirtualBox install, Home Assistant OS download, VM creation, and server start options.",
            self.btn_beginner_setup: "Run Beginner Auto Setup button. Leaves this advanced dialog and starts Viper's recommended automatic setup path.",
            self.btn_test: "Discover devices again button. Re-reads Home Assistant entities using the saved address and token.",
            self.btn_change_doorbell_triggers_now: "Change Doorbell Triggers button. Opens the Doorbell Vision page and puts focus on the front door trigger selector.",
            self.btn_find_ring_mqtt_streams_now: "Find Ring MQTT Streams Now button. Checks Ring-MQTT for live stream names without making you switch pages.",
            self.btn_change_camera_streams_now: "Change Camera Streams button. Opens the Doorbell Vision page and puts focus on the front door RTSP URL box.",
            self.btn_test_front_rtsp_now: "Test Front Camera Now button. Tests the configured front door live camera URL without making you switch pages.",
            self.btn_test_back_rtsp_now: "Test Back Camera Now button. Tests the configured back door live camera URL without making you switch pages.",
            self.btn_install_ring_mqtt: "Install Ring MQTT requirements button. Uses the Home Assistant Supervisor API to install Mosquitto Broker and Ring-MQTT with Video Streaming without using the inaccessible Apps screen.",
            self.btn_mqtt: "Test MQTT button. Advanced only. Checks whether Viper can connect to the MQTT broker.",
            self.btn_ring: "Find Ring topics button. Advanced only. Listens for Ring MQTT motion or doorbell topics.",
            self.btn_ring_help: "Ring setup assistant button. Explains how to use Mosquitto and ring-mqtt for Ring triggers and live RTSP streams.",
            self.btn_discover_setup_speakers: "Discover Available Speakers button. Shows Home Assistant media players and network Sonos speakers, then lets you choose which ones to add.",
            self.btn_setup_summary: "Show Setup Summary button. Shows Home Assistant, doorbell camera, speaker, and Gemini readiness without changing settings.",
            self.btn_setup_test_everything: "Test Everything button. Runs safe Home Assistant and camera checks from the setup dialog without changing settings.",
            self.btn_setup_support_report: "Create Support Report To Email Developer button. Creates a redacted zip with setup details and opens an email draft addressed to the Viper developer.",
            self.btn_help: "Help button. Opens Viper local help.",
            self.btn_save: "Save Home Assistant setup button. Saves the address, token, doorbell triggers, and camera URLs.",
            self.btn_close: "Close setup button. Closes without saving new changes.",
        }
        for button, description in button_descriptions.items():
            self._describe_control(button, button.GetLabel(), description)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        self.Bind(wx.EVT_CLOSE, self.on_close_setup)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)

        def add_button_grid(container, buttons):
            grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
            grid.AddGrowableCol(0, 1)
            grid.AddGrowableCol(1, 1)
            for button in buttons:
                grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
            container.Add(grid, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        add_button_grid(connect_sizer, [self.btn_find_ha, self.btn_install_ha, self.btn_test, self.btn_beginner_setup])
        add_button_grid(ring_sizer, [self.btn_install_ring_mqtt, self.btn_ring_help])
        add_button_grid(finish_sizer, [self.btn_discover_setup_speakers, self.btn_setup_summary, self.btn_setup_test_everything, self.btn_setup_support_report, self.btn_save, self.btn_help, self.btn_close])

        finish_text = wx.TextCtrl(
            finish_page,
            value=(
                "Advanced setup order:\n"
                "1. Home Assistant page: enter address and token, then discover devices.\n"
                "2. Doorbell Vision page: pick trigger entities, find Ring-MQTT streams, and test cameras.\n"
                "3. Ring-MQTT Advanced page: install Ring-MQTT or reveal advanced MQTT fields only when needed.\n"
                "4. Final Checks page: discover speakers, run tests, create a support report, then save."
            ),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 150),
        )
        self._describe_control(finish_text, "Home Assistant setup recommended order", "Read-only summary of the recommended beginner setup order.")
        finish_sizer.Insert(0, finish_text, 1, wx.ALL | wx.EXPAND, 8)

        connect_page.SetSizer(connect_sizer)
        doorbell_page.SetSizer(doorbell_sizer)
        ring_page.SetSizer(ring_sizer)
        finish_page.SetSizer(finish_sizer)
        notebook.AddPage(connect_page, "Home Assistant")
        notebook.AddPage(doorbell_page, "Doorbell Vision")
        notebook.AddPage(ring_page, "Ring-MQTT Advanced")
        notebook.AddPage(finish_page, "Final Checks")
        sizer.Add(notebook, 1, wx.ALL | wx.EXPAND, 8)

        self.status_txt = wx.TextCtrl(
            panel,
            value=(
                "Advanced Home Assistant setup is for troubleshooting and manual edits. For a new installation, use the main Setup Wizard first. "
                "This box reports connection results, Ring-MQTT stream tests, speaker discovery, and save status. "
                "Doorbell live video URLs must come from Ring-MQTT discovery, manual entry, or existing saved config. Home Assistant camera snapshot entities are not live video streams."
            ),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 110),
        )
        self._describe_control(self.status_txt, "Home Assistant setup status", "This read-only box explains what Viper found or what needs to be fixed.")
        sizer.Add(self.status_txt, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)

        camera_action_label = wx.StaticText(panel, label="Doorbell setup actions")
        camera_action_label.SetName("Doorbell setup actions")
        sizer.Add(camera_action_label, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 12)
        camera_action_sizer = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        camera_action_sizer.AddGrowableCol(0, 1)
        camera_action_sizer.AddGrowableCol(1, 1)
        for button in (
            self.btn_change_doorbell_triggers_now,
            self.btn_find_ring_mqtt_streams_now,
            self.btn_change_camera_streams_now,
            self.btn_test_front_rtsp_now,
            self.btn_test_back_rtsp_now,
        ):
            camera_action_sizer.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(camera_action_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        panel.SetSizer(sizer)
        self.on_pushover_toggle(None)
        self._apply_advanced_doorbell_visibility()
        self._update_connect_actions()
        self.ha_ip_txt.Bind(wx.EVT_TEXT, lambda _event: self._update_connect_actions())
        self.ha_token_txt.Bind(wx.EVT_TEXT, lambda _event: self._update_setup_action_gates())
        self.rtsp_front_txt.Bind(wx.EVT_TEXT, lambda _event: self._update_setup_action_gates())
        self.rtsp_back_txt.Bind(wx.EVT_TEXT, lambda _event: self._update_setup_action_gates())
        for ctrl in (self.ha_ip_txt, self.front_camera_id_txt, self.back_camera_id_txt, self.ring_topic_root_txt):
            ctrl.Bind(wx.EVT_TEXT, self.on_doorbell_derivation_change)
        self._refresh_derived_doorbell_preview()
        self._populate_trigger_choices_from_config(front_trigger.get("trigger_entity_id", ""), back_trigger.get("trigger_entity_id", ""))
        self._update_setup_action_gates()
        self._update_setup_page_nav()
        wx.CallAfter(self.force_initial_focus)
        wx.CallLater(150, self.force_initial_focus)
        wx.CallLater(500, self.force_initial_focus)
        if not self.ha_ip_txt.GetValue().strip():
            wx.CallAfter(self._auto_find_ha_if_needed)

    def force_initial_focus(self):
        if getattr(self, "_destroyed", False):
            return
        try:
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
            self._nudge_dialog_foreground()
            focus_target = None
            for candidate in (getattr(self, "btn_test", None), getattr(self, "btn_find_ha", None), getattr(self, "btn_install_ha", None), getattr(self, "status_txt", None)):
                if candidate is None:
                    continue
                try:
                    if hasattr(candidate, "IsShownOnScreen") and not candidate.IsShownOnScreen():
                        continue
                    if hasattr(candidate, "IsEnabled") and not candidate.IsEnabled():
                        continue
                    if hasattr(candidate, "CanAcceptFocusFromKeyboard") and not candidate.CanAcceptFocusFromKeyboard():
                        continue
                    focus_target = candidate
                    break
                except RuntimeError:
                    continue
            if focus_target is None:
                focus_target = getattr(self, "status_txt", None)
            if focus_target is None:
                return
            if self._initial_focus_given and wx.Window.FindFocus() is focus_target:
                return
            self._initial_focus_given = True
            if hasattr(focus_target, "SetFocusFromKbd"):
                try:
                    focus_target.SetFocusFromKbd()
                    return
                except Exception:
                    pass
            focus_target.SetFocus()
        except Exception:
            logging.debug("Could not force Home Assistant setup focus.", exc_info=True)

    def on_activate(self, event):
        try:
            if event.GetActive() and not getattr(self, "_destroyed", False):
                self._initial_focus_given = False
                wx.CallAfter(self.force_initial_focus)
                wx.CallLater(150, self.force_initial_focus)
        except Exception:
            logging.debug("Could not restore Home Assistant setup focus on activation.", exc_info=True)
        event.Skip()

    def _nudge_dialog_foreground(self):
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
            logging.debug("Could not nudge Home Assistant setup to Windows foreground.", exc_info=True)

    def select_page(self, page_name):
        page_index = self._setup_page_indexes.get(str(page_name or "").lower())
        if page_index is not None:
            self.notebook.SetSelection(page_index)
            self._update_setup_page_nav()

    def _update_setup_page_nav(self):
        if not hasattr(self, "notebook"):
            return
        idx = self.notebook.GetSelection()
        count = len(getattr(self, "_setup_page_names", [])) or self.notebook.GetPageCount()
        title = self._setup_page_names[idx] if 0 <= idx < len(self._setup_page_names) else f"Page {idx + 1}"
        if hasattr(self, "setup_page_title"):
            self.setup_page_title.SetLabel(f"{title} ({idx + 1} of {count})")
        if hasattr(self, "btn_setup_page_back"):
            self.btn_setup_page_back.Enable(idx > 0)
        if hasattr(self, "btn_setup_page_next"):
            can_go_next = idx < count - 1
            if idx == 0 and not getattr(self, "_devices_discovered", False):
                can_go_next = False
            show_next = not (idx == 0 and not getattr(self, "_devices_discovered", False))
            self.btn_setup_page_next.Show(show_next)
            self.btn_setup_page_next.Enable(can_go_next)
            if idx == 0 and not getattr(self, "_devices_discovered", False):
                self.btn_setup_page_next.SetToolTip("Next becomes available after Home Assistant devices have been discovered.")
            else:
                self.btn_setup_page_next.SetToolTip("Next setup page")
            parent = self.btn_setup_page_next.GetParent()
            if parent:
                parent.Layout()

    def _update_connect_actions(self):
        if not hasattr(self, "btn_find_ha") or not hasattr(self, "btn_test"):
            return
        has_host = bool(self.ha_ip_txt.GetValue().strip())
        show_find = (not has_host) or bool(getattr(self, "_ha_find_failed", False))
        self.btn_find_ha.Show(show_find)
        if hasattr(self, "btn_install_ha"):
            self.btn_install_ha.Show((not has_host) or bool(getattr(self, "_ha_find_failed", False)))
        show_discover = has_host and bool(getattr(self, "_show_discover_devices", False))
        self.btn_test.Show(show_discover)
        if show_discover:
            self.btn_test.SetDefault()
        if show_find:
            self.btn_find_ha.SetToolTip("Find Home Assistant button. Use this only if Viper did not find Home Assistant automatically, or if Home Assistant still needs to be installed.")
        self.btn_test.SetToolTip("Discover Devices button. Use this after Home Assistant is found and your long-lived access token is entered.")
        env_token_available = bool(cfg.get_ha_settings(self.parent.config, include_env=True).get("ha_token"))
        if env_token_available and not self.ha_token_txt.GetValue().strip():
            self.ha_token_txt.SetToolTip("Home Assistant token is available from environment variables. You can leave this box blank.")
        env_api = cfg.get_api_settings(self.parent.config, include_env=True)
        if env_api.get("gemini_api_key") and not self.gemini_key_txt.GetValue().strip():
            self.gemini_key_txt.SetToolTip("Gemini API key is available from environment variables. You can leave this box blank.")
        if env_api.get("pushover_user_key") and not self.pushover_user_txt.GetValue().strip():
            self.pushover_user_txt.SetToolTip("Pushover user key is available from environment variables. You can leave this box blank.")
        if env_api.get("pushover_api_token") and not self.pushover_token_txt.GetValue().strip():
            self.pushover_token_txt.SetToolTip("Pushover app token is available from environment variables. You can leave this box blank.")
        parent = self.btn_test.GetParent()
        if parent:
            parent.Layout()
        self.Layout()
        self._update_setup_action_gates()
        self._update_setup_page_nav()

    def _effective_ha_host_and_token_present(self):
        host = bool(getattr(self, "ha_ip_txt", None) and self.ha_ip_txt.GetValue().strip())
        parent_config = getattr(getattr(self, "parent", None), "config", {}) or {}
        token = bool(cfg.get_ha_settings(parent_config, include_env=True).get("ha_token"))
        if getattr(self, "ha_token_txt", None) and self.ha_token_txt.GetValue().strip():
            token = True
        return host, token

    def _set_button_gate(self, button, enabled, enabled_tip, disabled_tip):
        if button is None:
            return
        try:
            button.Enable(bool(enabled))
            button.SetToolTip(enabled_tip if enabled else disabled_tip)
            if not enabled:
                try:
                    button.SetName(f"{button.GetLabel()}. Unavailable. {disabled_tip}")
                except Exception:
                    pass
            else:
                try:
                    button.SetName(button.GetLabel())
                except Exception:
                    pass
        except RuntimeError:
            pass

    def _update_setup_action_gates(self):
        if getattr(self, "_destroyed", False):
            return
        host_present, token_present = self._effective_ha_host_and_token_present()
        front_rtsp = bool(getattr(self, "rtsp_front_txt", None) and self.rtsp_front_txt.GetValue().strip())
        back_rtsp = bool(getattr(self, "rtsp_back_txt", None) and self.rtsp_back_txt.GetValue().strip())
        ha_ready = host_present and token_present
        self._set_button_gate(
            getattr(self, "btn_find_ring_mqtt_streams_now", None),
            ha_ready,
            "Finds Ring-MQTT live streams using Home Assistant.",
            "Home Assistant host and token are required before Viper can read Ring-MQTT streams.",
        )
        self._set_button_gate(
            getattr(self, "btn_test_front_rtsp_now", None),
            front_rtsp,
            "Tests the saved front door live stream.",
            "A front door RTSP URL is required before this test can run.",
        )
        self._set_button_gate(
            getattr(self, "btn_test_back_rtsp_now", None),
            back_rtsp,
            "Tests the saved back door live stream.",
            "A back door RTSP URL is required before this test can run.",
        )
        self._set_button_gate(
            getattr(self, "btn_setup_test_everything", None),
            ha_ready,
            "Runs safe setup checks.",
            "Home Assistant host and token are required before Test Everything can run.",
        )
        self._set_button_gate(
            getattr(self, "btn_save", None),
            ha_ready,
            "Saves Home Assistant and doorbell setup.",
            "Home Assistant host and token are required before setup can be saved.",
        )

    def on_install_home_assistant_from_setup(self, event):
        self._record_setup_event("install_ha_assistant_open", "Opening Home Assistant server assistant from setup.")
        owner = getattr(self, "parent", None)
        if owner is None:
            self._set_setup_status("Viper could not open the Home Assistant installer because the main app window was not available.", announce=True)
            return
        try:
            self.btn_install_ha.Enable(False)
            self.btn_install_ha.SetLabel("Opening Home Assistant Installer")
            self.btn_install_ha.SetName("Opening Home Assistant Installer")
        except Exception:
            pass
        try:
            self._destroyed = True
            if getattr(owner, "_ha_setup_dialog", None) is self:
                owner._ha_setup_dialog = None
            self.Hide()
            owner.show_new_user_setup_assistant()
            wx.CallLater(150, self.Destroy)
        except Exception:
            logging.exception("[HA SETUP] Failed to open Home Assistant install assistant")
            self._destroyed = False
            self.Show(True)
            try:
                self.btn_install_ha.Enable(True)
                self.btn_install_ha.SetLabel("Install Home Assistant On This PC")
                self.btn_install_ha.SetName("Install Home Assistant On This PC")
            except Exception:
                pass
            self._set_setup_status("Viper could not open the Home Assistant install assistant. Check viper_full_debug.log for details.", announce=True)

    def _auto_find_ha_if_needed(self):
        if self.ha_ip_txt.GetValue().strip() or getattr(self, "_auto_ha_find_done", False):
            self._update_connect_actions()
            return
        self._auto_ha_find_done = True
        self._ha_find_failed = False
        self._set_busy(True)
        self._set_setup_status(
            "Viper is automatically looking for Home Assistant. If it cannot find it, the Find Home Assistant button will become available.",
            announce=True,
        )
        safe_submit(self._run_auto_find_ha)

    def _run_auto_find_ha(self):
        try:
            env_ha = cfg.get_ha_settings(self.parent.config, include_env=True)
            result = discovery.find_home_assistant(
                token=self.ha_token_txt.GetValue().strip() or env_ha.get("ha_token") or None,
                seed_host="",
                seed_port=self.ha_port_txt.GetValue().strip() or "8123",
                timeout=2,
            )
        except Exception as e:
            logging.exception("[HA SETUP] Automatic Home Assistant find failed unexpectedly")
            result = {"ok": False, "error": "unexpected_error", "message": str(e), "attempts": []}
        wx.CallAfter(self._finish_auto_find_ha, result)

    def _finish_auto_find_ha(self, result):
        self._set_busy(False)
        if result.get("ok"):
            self._ha_find_failed = False
            self._show_discover_devices = True
            self.ha_ip_txt.SetValue(result.get("ha_ip", ""))
            self.ha_port_txt.SetValue(result.get("ha_port", "8123"))
            token_note = (
                "Your Home Assistant token is available from environment variables. Press Discover Devices."
                if cfg.get_ha_settings(self.parent.config, include_env=True).get("ha_token") and not self.ha_token_txt.GetValue().strip()
                else "Enter your long-lived access token, then press Discover Devices."
            )
            self._set_setup_status(
                f"Home Assistant was found automatically at {result.get('ha_ip')}:{result.get('ha_port')}. {token_note}",
                announce=True,
            )
            self._refresh_derived_doorbell_preview()
        else:
            self._ha_find_failed = True
            self._set_setup_status(
                "Viper could not find Home Assistant automatically. If Home Assistant is already installed, press Find Home Assistant. If it is not installed yet, press Install Home Assistant On This PC.",
                announce=True,
            )
        self._update_connect_actions()

    def on_setup_page_back(self, event):
        idx = self.notebook.GetSelection()
        if idx > 0:
            self.notebook.SetSelection(idx - 1)
            self._update_setup_page_nav()

    def on_setup_page_next(self, event):
        idx = self.notebook.GetSelection()
        if idx < self.notebook.GetPageCount() - 1:
            self.notebook.SetSelection(idx + 1)
            self._update_setup_page_nav()

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("ring-setup")
            return
        event.Skip()

    def _describe_control(self, control, name, description=""):
        control.SetName(name)
        control.SetToolTip(description or name)
        try:
            accessible = control.GetOrCreateAccessible()
            if accessible:
                accessible.SetName(name)
                accessible.SetDescription(description or name)
        except Exception:
            pass
        try:
            control.Bind(wx.EVT_SET_FOCUS, self._on_control_focus_for_diagnostics)
        except Exception:
            pass

    def _on_control_focus_for_diagnostics(self, event):
        control = event.GetEventObject()
        try:
            label = control.GetLabel() if hasattr(control, "GetLabel") else ""
            logging.info(
                "[FOCUS] HA setup focus class=%s name=%r label=%r shown=%s enabled=%s can_focus=%s",
                control.__class__.__name__,
                control.GetName() if hasattr(control, "GetName") else "",
                label,
                control.IsShownOnScreen() if hasattr(control, "IsShownOnScreen") else None,
                control.IsEnabled() if hasattr(control, "IsEnabled") else None,
                control.CanAcceptFocusFromKeyboard() if hasattr(control, "CanAcceptFocusFromKeyboard") else None,
            )
        except Exception:
            logging.debug("Could not log Home Assistant setup focus target.", exc_info=True)
        event.Skip()

    def _record_setup_event(self, event, message="", **details):
        recorder = getattr(getattr(self, "parent", None), "record_setup_event", None)
        if callable(recorder):
            recorder(event, message, **details)

    def _set_setup_status(self, message, *, announce=False):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring status update after setup dialog was destroyed: %s", message)
            return
        self._last_setup_status = str(message or "")
        self._record_setup_event("status", self._last_setup_status, announced=bool(announce))
        try:
            self.status_txt.SetValue(message)
        except RuntimeError:
            logging.info("[HA SETUP] Ignoring status update for deleted setup status box.")
            return
        if announce:
            try:
                logging.info("[HA SETUP] %s", message.replace("\n", " | "))
            except Exception:
                pass
            speaker = getattr(self.parent, "_safe_speak", None)
            if callable(speaker):
                wx.CallAfter(speaker, message)

    def _replace_setup_progress(self, lines, *, announce=False):
        snapshot = [str(line) for line in lines]
        if not hasattr(self, "status_txt"):
            return
        def _call():
            return self._set_setup_status("\n".join(snapshot), announce=announce)
        wx.CallAfter(_call)

    def _append_setup_progress(self, lines, message, *, announce=False):
        lines.append(str(message))
        self._record_setup_event("progress", str(message))
        try:
            logging.info("[HA SETUP PROGRESS] %s", str(message).replace("\n", " | "))
        except Exception:
            pass
        if hasattr(self, "status_txt"):
            self._replace_setup_progress(lines, announce=announce)

    def _status(self, message, *, announce=False):
        setter = getattr(self, "_set_setup_status", None)
        if callable(setter):
            setter(message, announce=announce)
        else:
            self.status_txt.SetValue(message)

    def on_toggle_advanced_doorbell(self, event):
        self._show_advanced_doorbell = self.advanced_doorbell_chk.GetValue()
        self._apply_advanced_doorbell_visibility()
        parent = self.advanced_doorbell_chk.GetParent()
        if parent:
            parent.Layout()
        self.Layout()

    def _set_children_enabled(self, window, enabled):
        for child in window.GetChildren():
            child.Enable(enabled)
            if child.GetChildren():
                self._set_children_enabled(child, enabled)

    def _apply_advanced_doorbell_visibility(self):
        show = bool(getattr(self, "_show_advanced_doorbell", False))
        panel = getattr(self, "advanced_doorbell_panel", None)
        if panel:
            panel.Show(show)
            self._set_children_enabled(panel, show)
            panel.Layout()
            parent = panel.GetParent()
            if parent:
                parent.Layout()
        for widget in getattr(self, "_advanced_doorbell_widgets", []):
            widget.Show(show)
            widget.Enable(show)

    def _settings(self):
        front_trigger_entity = self._choice_entity_id(self.front_trigger_choice)
        back_trigger_entity = self._choice_entity_id(self.back_trigger_choice)
        env_ha = cfg.get_ha_settings(self.parent.config, include_env=True)
        env_api = cfg.get_api_settings(self.parent.config, include_env=True)
        env_doorbell = cfg.get_doorbell_settings(self.parent.config, include_env=True)
        pushover_user = self.pushover_user_txt.GetValue().strip() or env_api.get("pushover_user_key") or ""
        pushover_token = self.pushover_token_txt.GetValue().strip() or env_api.get("pushover_api_token") or ""
        return {
            "ha_ip": self.ha_ip_txt.GetValue().strip() or env_ha.get("ha_ip") or "",
            "ha_port": self.ha_port_txt.GetValue().strip() or "8123",
            "ha_token": self.ha_token_txt.GetValue().strip() or env_ha.get("ha_token") or "",
            "gemini_api_key": self.gemini_key_txt.GetValue().strip() or env_api.get("gemini_api_key") or "",
            "ha_listener_enabled": self.ha_listener_chk.GetValue(),
            "pushover_enabled": bool(self.pushover_enabled_chk.GetValue()),
            "pushover_user_key": pushover_user,
            "pushover_api_token": pushover_token,
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
            "mqtt_password": self.mqtt_password_txt.GetValue().strip() or env_doorbell.get("mqtt_password") or "",
            "show_advanced_ring_mqtt": self.advanced_doorbell_chk.GetValue(),
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
        try:
            return choice.GetClientData(idx) or ""
        except Exception:
            return ""

    def _populate_trigger_choices_from_config(self, front_entity="", back_entity=""):
        choices = []
        if self.discovery_result and self.discovery_result.get("ok"):
            candidates = self._doorbell_trigger_candidates()
            for entity in candidates:
                choices.append((self._entity_choice_label(entity), entity.get("entity_id")))
        for entity_id in [front_entity, back_entity]:
            if entity_id and entity_id not in [item[1] for item in choices]:
                choices.append((entity_id, entity_id))
        labels = [item[0] for item in choices]
        for choice, current in [(self.front_trigger_choice, front_entity), (self.back_trigger_choice, back_entity)]:
            if labels:
                choice.Set(labels)
            else:
                choice.Set(["No Home Assistant entities discovered yet"])
                choice.SetSelection(0)
                continue
            for idx, (_label, entity_id) in enumerate(choices):
                choice.SetClientData(idx, entity_id)
            if current:
                match = next((idx for idx, item in enumerate(choices) if item[1] == current), wx.NOT_FOUND)
                if match != wx.NOT_FOUND:
                    choice.SetSelection(match)
                    continue
            if labels:
                choice.SetSelection(0)

    def _entity_search_text(self, entity):
        return " ".join(
            str(part).lower()
            for part in [
                entity.get("entity_id"),
                entity.get("friendly_name"),
                entity.get("domain"),
                entity.get("device_class"),
                entity.get("platform"),
                entity.get("integration"),
                entity.get("attributes_summary"),
            ]
        ).replace("_", " ")

    def _doorbell_trigger_candidates(self):
        if not self.discovery_result or not self.discovery_result.get("ok"):
            return []
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
            text = self._entity_search_text(entity)
            if entity.get("domain") in {"binary_sensor", "sensor", "event", "button"} and any(
                token in text for token in ["ring", "doorbell", "motion", "ding", "front door", "back door", "visitor"]
            ):
                if entity_id and entity_id not in seen:
                    seen.add(entity_id)
                    candidates.append(entity)
        return candidates

    def _doorbell_camera_candidates(self):
        if not self.discovery_result or not self.discovery_result.get("ok"):
            return []
        categories = self.discovery_result.get("categories", {})
        seen = set()
        cameras = []
        for entity in categories.get("ring_cameras", []) + categories.get("cameras", []):
            entity_id = entity.get("entity_id")
            text = self._entity_search_text(entity)
            if entity_id and entity_id not in seen and any(token in text for token in ["ring", "doorbell", "front", "back", "porch"]):
                seen.add(entity_id)
                cameras.append(entity)
        return cameras

    def _camera_rtsp_candidates_from_discovery(self, host):
        if not host:
            return []
        candidates = []
        seen_urls = set()
        for entity in self._doorbell_camera_candidates():
            entity_id = entity.get("entity_id") or ""
            slug = self._rtsp_stream_slug(self._object_id_from_entity(entity))
            if not slug:
                continue
            text = self._entity_search_text(entity)
            names = [slug]
            if not slug.endswith("_live"):
                names.append(f"{slug}_live")
            for name in names:
                rtsp_url = f"rtsp://{host}:8554/{name}"
                if rtsp_url in seen_urls:
                    continue
                seen_urls.add(rtsp_url)
                candidates.append({
                    "name": name,
                    "rtsp_url": rtsp_url,
                    "source": "Home Assistant camera entity",
                    "entity_id": entity_id,
                    "friendly_name": entity.get("friendly_name") or entity_id,
                    "camera_id": slug,
                    "candidate_only": True,
                    "score_text": text,
                })
        return candidates

    def _score_doorbell_entity(self, entity, side):
        text = self._entity_search_text(entity)
        score = 0
        for token, points in [
            ("ring", 6),
            ("doorbell", 6),
            ("ding", 5),
            ("motion", 4),
            ("visitor", 3),
            ("camera", 2),
            ("front", 8 if side == "front" else -3),
            ("porch", 4 if side == "front" else 0),
            ("back", 8 if side == "back" else -3),
            ("rear", 6 if side == "back" else -2),
        ]:
            if token in text:
                score += points
        return score

    def _select_choice_entity(self, choice, entity_id):
        if not entity_id:
            return False
        for idx in range(choice.GetCount()):
            if choice.GetClientData(idx) == entity_id:
                choice.SetSelection(idx)
                return True
        return False

    def _object_id_from_entity(self, entity):
        entity_id = entity.get("entity_id", "")
        if "." not in entity_id:
            return ""
        return entity_id.split(".", 1)[1]

    def _rtsp_stream_slug(self, camera_slug):
        camera_slug = (camera_slug or "").strip()
        if camera_slug.endswith("_snapshot"):
            camera_slug = camera_slug[: -len("_snapshot")]
        return camera_slug

    def _derive_rtsp_from_camera_entity(self, entity, side):
        host = self.ha_ip_txt.GetValue().strip()
        camera_slug = self._rtsp_stream_slug(self._object_id_from_entity(entity))
        if not host or not camera_slug:
            return ""
        text = self._entity_search_text(entity)
        if any(token in text for token in ["snapshot", "live view"]):
            return ""
        candidates = [
            f"rtsp://{host}:8554/{camera_slug}",
            f"rtsp://{host}:8554/{camera_slug}_live",
        ]
        if "ring" in text:
            candidates.reverse()
        return candidates[0]

    def _auto_configure_doorbells_from_discovery(self):
        if not self.discovery_result or not self.discovery_result.get("ok"):
            return {"ok": False, "message": "Press Discover Devices first so Viper can read Home Assistant entities."}

        triggers = self._doorbell_trigger_candidates()
        selected = {}

        def pick_best(items, side, used_ids):
            available = [item for item in items if item.get("entity_id") not in used_ids]
            best = max(available, key=lambda entity: self._score_doorbell_entity(entity, side), default=None)
            if best and self._score_doorbell_entity(best, side) > 0:
                used_ids.add(best.get("entity_id"))
                return best
            return None

        used_trigger_ids = set()
        selected["front_trigger"] = pick_best(triggers, "front", used_trigger_ids)
        selected["back_trigger"] = pick_best(triggers, "back", used_trigger_ids)

        if selected.get("front_trigger"):
            self._select_choice_entity(self.front_trigger_choice, selected["front_trigger"].get("entity_id"))
        if selected.get("back_trigger"):
            self._select_choice_entity(self.back_trigger_choice, selected["back_trigger"].get("entity_id"))

        self._refresh_derived_doorbell_preview()
        lines = ["Doorbell trigger setup:"]
        for side in ("front", "back"):
            trigger = selected.get(f"{side}_trigger")
            lines.append(f"{side.title()} selected trigger: {trigger.get('entity_id') if trigger else 'not selected'}")
        lines.append("")
        lines.append("If these are wrong, press Change Doorbell Triggers. Live video uses the Ring-MQTT stream fields, not Home Assistant camera entities.")
        return {"ok": True, "message": "\n".join(lines)}

    def on_auto_configure_doorbells(self, event):
        result = self._auto_configure_doorbells_from_discovery()
        self.status_txt.SetValue(result.get("message") or "Auto configuration finished.")
        wx.CallAfter(self._focus_camera_test_actions)

    def on_change_doorbell_triggers_now(self, event):
        self.select_page("doorbell vision")
        self._set_setup_status(
            "Change Doorbell Triggers. Choose the Home Assistant entity that changes when someone presses the doorbell. Usually this is a ding or button-press binary sensor.",
            announce=True,
        )
        wx.CallAfter(self._focus_control, getattr(self, "front_trigger_choice", None), "HA setup")

    def on_change_camera_streams_now(self, event):
        self.select_page("doorbell vision")
        self._set_setup_status(
            "Change Camera Streams. Use Find Ring MQTT Streams Now for automatic setup, or type a tested live RTSP URL in the front or back RTSP box.",
            announce=True,
        )
        wx.CallAfter(self._focus_control, getattr(self, "rtsp_front_txt", None), "HA setup")

    def _focus_control(self, control, context="HA setup"):
        if getattr(self, "_destroyed", False) or control is None:
            return
        try:
            if hasattr(control, "IsShownOnScreen") and not control.IsShownOnScreen():
                return
            if hasattr(control, "IsEnabled") and not control.IsEnabled():
                return
            if hasattr(control, "SetFocusFromKbd"):
                control.SetFocusFromKbd()
            else:
                control.SetFocus()
            self._log_focus(context)
        except Exception:
            logging.exception("[HA SETUP] Could not focus requested control.")

    def _log_focus(self, context="HA setup"):
        try:
            control = wx.Window.FindFocus()
            if control is None:
                logging.info("[FOCUS] %s focus target: none", context)
                return
            label = control.GetLabel() if hasattr(control, "GetLabel") else ""
            logging.info(
                "[FOCUS] %s focus class=%s name=%r label=%r shown=%s enabled=%s",
                context,
                control.__class__.__name__,
                control.GetName() if hasattr(control, "GetName") else "",
                label,
                control.IsShownOnScreen() if hasattr(control, "IsShownOnScreen") else None,
                control.IsEnabled() if hasattr(control, "IsEnabled") else None,
            )
        except Exception:
            logging.debug("Could not log focus target for %s.", context, exc_info=True)

    def _focus_camera_test_actions(self):
        if getattr(self, "_destroyed", False):
            return
        target = getattr(self, "btn_test_front_rtsp_now", None)
        if target is None:
            return
        self._focus_control(target, "HA setup")

    def on_doorbell_derivation_change(self, event):
        if not self._doorbell_preview_updating:
            self._refresh_derived_doorbell_preview()
        if event:
            event.Skip()

    def _derived_doorbell_values(self):
        settings = self._settings()
        ha_ip = settings["ha_ip"]
        front_camera_id = self._rtsp_stream_slug(settings["front_camera_id"])
        back_camera_id = self._rtsp_stream_slug(settings["back_camera_id"])
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
            self._set_text_if_blank_or_previous_preview(self.front_mqtt_txt, "front_doorbell_mqtt_topic", derived["front_doorbell_mqtt_topic"])
            self._set_text_if_blank_or_previous_preview(self.back_mqtt_txt, "back_doorbell_mqtt_topic", derived["back_doorbell_mqtt_topic"])
        finally:
            self._last_derived_values = derived
            self._doorbell_preview_updating = False

    def _rtsp_host_from_ha_host(self, host):
        host = (host or "").strip()
        if not host:
            return ""
        if "://" in host:
            try:
                parsed = requests.utils.urlparse(host)
                host = parsed.hostname or host
            except Exception:
                pass
        if ":" in host and not host.startswith("["):
            host = host.split(":", 1)[0]
        return host.strip("/")

    def _ring_mqtt_stream_score(self, stream_name, side):
        text = (stream_name or "").lower().replace("_", " ").replace("-", " ")
        score = 0
        for token, points in [
            ("ring", 4),
            ("door", 3),
            ("doorbell", 6),
            ("camera", 1),
            ("live", 2),
            ("snapshot", -20),
            ("front", 12 if side == "front" else -5),
            ("porch", 5 if side == "front" else 0),
            ("back", 12 if side == "back" else -5),
            ("rear", 8 if side == "back" else -4),
        ]:
            if token in text:
                score += points
        return score

    def _live_stream_score(self, stream, side):
        text = " ".join(
            str(stream.get(key, ""))
            for key in ("name", "friendly_name", "entity_id", "topic", "source", "score_text")
        )
        return self._ring_mqtt_stream_score(text, side)

    def _normalize_rtsp_host(self, rtsp_url, host):
        rtsp_url = (rtsp_url or "").strip()
        host = self._rtsp_host_from_ha_host(host)
        if not rtsp_url or not host:
            return rtsp_url
        try:
            parsed = urlparse(rtsp_url)
            if parsed.scheme.lower() != "rtsp" or not parsed.path:
                return rtsp_url
            port = f":{parsed.port}" if parsed.port else ""
            auth = ""
            if parsed.username:
                auth = parsed.username
                if parsed.password:
                    auth += f":{parsed.password}"
                auth += "@"
            return f"rtsp://{auth}{host}{port}{parsed.path}"
        except Exception:
            return rtsp_url

    def _stream_name_from_rtsp_url(self, rtsp_url):
        try:
            parsed = urlparse(rtsp_url or "")
            return parsed.path.strip("/").split("/")[-1]
        except Exception:
            return ""

    def _run_find_ha_ring_rtsp_streams(self, settings, host):
        token = settings.get("ha_token") or ""
        ha_ip = settings.get("ha_ip") or host
        ha_port = settings.get("ha_port") or "8123"
        if not token or not ha_ip:
            return {"streams": [], "attempt": "Home Assistant stream scan skipped because the host or token is missing."}
        result = discovery.get_ha_states(token=token, ha_ip=ha_ip, ha_port=ha_port, timeout=8)
        if not result.get("ok"):
            return {"streams": [], "attempt": f"Home Assistant stream scan failed: {result.get('message') or result.get('error')}"}
        streams = []
        for state in result.get("states", []):
            attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
            stream_source = ""
            for key, value in attrs.items():
                if str(key).lower() == "stream_source" and str(value).lower().startswith("rtsp://"):
                    stream_source = str(value).strip()
                    break
            if not stream_source:
                continue
            entity_id = state.get("entity_id", "")
            friendly_name = attrs.get("friendly_name") or entity_id
            rtsp_url = self._normalize_rtsp_host(stream_source, host)
            name = self._stream_name_from_rtsp_url(rtsp_url) or entity_id
            streams.append({
                "name": name,
                "rtsp_url": rtsp_url,
                "source": "Home Assistant ring-mqtt",
                "entity_id": entity_id,
                "friendly_name": str(friendly_name),
            })
        return {
            "streams": streams,
            "attempt": f"Home Assistant stream_Source scan -> {len(streams)} RTSP stream(s)",
        }

    def _run_find_ring_mqtt_log_streams(self, settings, host):
        token = settings.get("ha_token") or ""
        ha_ip = settings.get("ha_ip") or host
        ha_port = settings.get("ha_port") or "8123"
        if not token or not ha_ip:
            return {"streams": [], "attempt": "Ring-MQTT log scan skipped because the host or token is missing."}
        url = f"http://{ha_ip}:{ha_port}/api/hassio/addons/{RING_MQTT_ADDON_SLUG}/logs"
        try:
            response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=12)
            if response.status_code != 200:
                return {"streams": [], "attempt": f"Ring-MQTT log scan -> HTTP {response.status_code}"}
        except Exception as e:
            return {"streams": [], "attempt": f"Ring-MQTT log scan failed: {e}"}

        streams = []
        clean = re.sub(r"\x1b\[[0-9;]*m", "", response.text or "")
        pattern = re.compile(
            r"\[(?P<name>[^\]]+)\].*?ring/(?P<root>[^/\s]+)/camera/(?P<camera_id>[^/\s]+)/info/state\s+"
            r".*?\"stream_Source\"\s*:\s*\"(?P<rtsp>rtsp://[^\"]+)\"",
            re.IGNORECASE,
        )
        for match in pattern.finditer(clean):
            friendly_name = match.group("name").strip()
            camera_id = match.group("camera_id").strip()
            rtsp_url = self._normalize_rtsp_host(match.group("rtsp").strip(), host)
            streams.append({
                "name": self._stream_name_from_rtsp_url(rtsp_url) or f"{camera_id}_live",
                "rtsp_url": rtsp_url,
                "source": "Ring-MQTT add-on log",
                "entity_id": "",
                "friendly_name": friendly_name,
                "camera_id": camera_id,
                "ring_topic_root": match.group("root").strip(),
            })
        return {
            "streams": streams,
            "attempt": f"Ring-MQTT add-on log scan -> {len(streams)} RTSP stream(s)",
        }

    def on_find_live_rtsp_streams(self, event):
        host = self._rtsp_host_from_ha_host(self.ha_ip_txt.GetValue())
        if not host:
            self._set_setup_status("Enter the Home Assistant IP or RTSP host first.", announce=True)
            return
        self._record_setup_event("rtsp_discovery_start", "Finding and testing Ring-MQTT RTSP streams.", host=host)
        settings = self._settings()
        self._set_busy(True)
        self._set_setup_status(
            "Looking for Ring-MQTT live streams. Viper checks Ring-MQTT camera attributes, add-on logs, and Ring MQTT topics, then tests each possible RTSP stream before filling the camera boxes.",
            announce=True,
        )
        safe_submit(self._run_find_live_rtsp_streams, host, settings)

    def _run_find_live_rtsp_streams(self, host, settings):
        attempts = []
        streams = []
        self._replace_setup_progress(
            [
                "Finding Ring-MQTT live streams",
                "",
                f"RTSP host: {host}",
                "Checking Home Assistant Ring-MQTT camera attributes.",
            ],
            announce=False,
        )
        ha_streams = self._run_find_ha_ring_rtsp_streams(settings, host)
        streams.extend(ha_streams.get("streams", []))
        attempts.append(ha_streams.get("attempt", "Home Assistant stream scan completed."))
        self._replace_setup_progress(
            [
                "Finding Ring-MQTT live streams",
                "",
                f"RTSP host: {host}",
                attempts[-1],
                "Checking Ring-MQTT add-on logs.",
            ],
            announce=False,
        )
        log_streams = self._run_find_ring_mqtt_log_streams(settings, host)
        streams.extend(log_streams.get("streams", []))
        attempts.append(log_streams.get("attempt", "Ring-MQTT log scan completed."))
        self._replace_setup_progress(
            [
                "Finding Ring-MQTT live streams",
                "",
                f"RTSP host: {host}",
                *attempts,
                "Listening briefly for Ring MQTT topics.",
            ],
            announce=False,
        )
        mqtt_result = None
        mqtt_host = settings.get("mqtt_host") or settings.get("ha_ip") or host
        if mqtt_host:
            mqtt_result = ring_discovery.listen_for_ring_topics(
                mqtt_host=mqtt_host,
                mqtt_port=settings.get("mqtt_port") or 1883,
                mqtt_username=settings.get("mqtt_username") or "",
                mqtt_password=settings.get("mqtt_password") or "",
                topic="ring/#",
                duration=8,
                rtsp_host=host,
                stop_on_first=False,
            )
            if mqtt_result.get("ok"):
                for item in mqtt_result.get("suggestions", []):
                    rtsp_url = item.get("rtsp_url") or ""
                    camera_id = item.get("camera_id") or ""
                    if rtsp_url and camera_id:
                        streams.append({
                            "name": f"{camera_id}_live",
                            "rtsp_url": rtsp_url,
                            "source": "ring-mqtt",
                            "topic": item.get("topic", ""),
                        })
                attempts.append(f"MQTT ring/# at {mqtt_host}:{settings.get('mqtt_port') or 1883} -> {mqtt_result.get('count', 0)} possible Ring stream topic(s)")
            else:
                attempts.append(
                    f"MQTT ring/# at {mqtt_host}:{settings.get('mqtt_port') or 1883} -> "
                    f"{mqtt_result.get('message') or mqtt_result.get('error') or 'failed'}"
                )
        self._replace_setup_progress(
            [
                "Finding Ring-MQTT live streams",
                "",
                f"RTSP host: {host}",
                *attempts,
                f"Total possible stream entries found before cleanup: {len(streams)}.",
            ],
            announce=False,
        )
        seen = set()
        unique = []
        for stream in streams:
            name = stream.get("name", "").strip()
            key = stream.get("rtsp_url") or name
            if name and key not in seen:
                seen.add(key)
                unique.append(stream)
        result = {"ok": bool(unique), "host": host, "streams": unique, "attempts": attempts, "mqtt_result": mqtt_result}
        wx.CallAfter(self._finish_find_live_rtsp_streams, result)

    def _choose_ring_mqtt_stream(self, side, streams, host):
        scored = sorted(
            streams,
            key=lambda stream: self._live_stream_score(stream, side),
            reverse=True,
        )
        labels = [f"Skip {side} door"]
        for stream in scored:
            name = stream.get("name", "")
            url = stream.get("rtsp_url") or f"rtsp://{host}:8554/{name}"
            source = stream.get("source") or "ring-mqtt"
            friendly = stream.get("friendly_name") or stream.get("entity_id") or name
            labels.append(f"{friendly}, {name} from {source}  -  {url}")
        dlg = wx.SingleChoiceDialog(
            self,
            f"Choose the Ring MQTT stream for the {side} door camera.",
            f"{side.title()} Door Ring MQTT Stream",
            labels,
        )
        try:
            if labels:
                best_score = self._live_stream_score(scored[0], side) if scored else 0
                dlg.SetSelection(1 if best_score > 0 else 0)
            if dlg.ShowModal() != wx.ID_OK:
                return ""
            idx = dlg.GetSelection()
            if idx <= 0:
                return ""
            name = scored[idx - 1].get("name", "")
            return scored[idx - 1].get("rtsp_url") or (f"rtsp://{host}:8554/{name}" if name else "")
        finally:
            dlg.Destroy()

    def _finish_find_live_rtsp_streams(self, result):
        self._set_busy(False)
        host = result.get("host") or self._rtsp_host_from_ha_host(self.ha_ip_txt.GetValue())
        streams = result.get("streams") or []
        attempts = result.get("attempts") or []
        if not streams:
            message = (
                "No Ring MQTT live streams were found. Viper checked Home Assistant ring-mqtt camera attributes and Ring MQTT topics.\n"
                "Install and start Mosquitto Broker and Ring-MQTT with Video Streaming, then open advanced Ring and MQTT fields and enter the MQTT username and password.\n"
                + "\n".join(attempts)
            )
            self._set_setup_status(message, announce=True)
            open_help("ring-mqtt-setup")
            return

        names = ", ".join(stream.get("name", "") for stream in streams[:8])
        more = f" and {len(streams) - 8} more" if len(streams) > 8 else ""
        self._set_setup_status(
            f"Found {len(streams)} Ring-MQTT live stream{'s' if len(streams) != 1 else ''}. "
            "Viper will test every possible stream for a live frame before filling the RTSP boxes.\n"
            f"Streams found: {names}{more}",
            announce=True,
        )
        self._set_busy(True)
        safe_submit(self._run_all_discovered_rtsp_tests, streams, host, attempts)

    def _stream_rtsp_url(self, stream, host):
        rtsp_url = (stream.get("rtsp_url") or "").strip()
        if rtsp_url:
            return self._normalize_rtsp_host(rtsp_url, host)
        name = (stream.get("name") or "").strip()
        return f"rtsp://{host}:8554/{name}" if host and name else ""

    def _run_all_discovered_rtsp_tests(self, streams, host, attempts=None):
        results = []
        attempts = attempts or []
        self._record_setup_event("rtsp_candidate_test_start", "Testing discovered RTSP streams.", candidate_count=len(streams or []))
        progress_lines = [
            "Testing Ring-MQTT live streams",
            "",
            f"Found {len(streams or [])} possible stream(s).",
            "Viper will test each stream for a real video frame before saving it.",
            "",
        ]
        self._replace_setup_progress(progress_lines, announce=True)
        for index, stream in enumerate(streams or [], 1):
            rtsp_url = self._stream_rtsp_url(stream, host)
            label = stream.get("friendly_name") or stream.get("name") or rtsp_url or f"stream {index}"
            self._append_setup_progress(
                progress_lines,
                f"Testing stream {index} of {len(streams or [])}: {label}",
                announce=False,
            )
            started = time.perf_counter()
            result = {
                "index": index,
                "stream": stream,
                "name": stream.get("name", ""),
                "friendly_name": stream.get("friendly_name", ""),
                "source": stream.get("source", ""),
                "rtsp_url": rtsp_url,
                "ok": False,
                "elapsed": 0,
                "message": "No RTSP URL was available for this stream.",
            }
            if rtsp_url:
                try:
                    test_dir = cfg.DATA_DIR / "rtsp_test"
                    test_dir.mkdir(parents=True, exist_ok=True)
                    min_bytes = min(cfg.FRONT_MIN_FRAME_BYTES, cfg.BACK_MIN_FRAME_BYTES)
                    frame = vision.grab_frame(rtsp_url, test_dir, f"setup_candidate_{index}", min_bytes=min_bytes, timeout=8)
                    result.update({
                        "ok": bool(frame),
                        "frame": frame,
                        "message": "Frame captured." if frame else "No live frame was captured before the timeout.",
                    })
                except Exception as e:
                    result["message"] = str(e)
            result["elapsed"] = time.perf_counter() - started
            results.append(result)
            status = "passed" if result.get("ok") else "failed"
            self._append_setup_progress(
                progress_lines,
                f"Stream {index} {status} in {result['elapsed']:.1f} seconds: {result.get('message')}",
                announce=False,
            )
            self._record_setup_event(
                "rtsp_candidate_test_result",
                result.get("message") or "",
                candidate_index=index,
                ok=bool(result.get("ok")),
                elapsed=round(result.get("elapsed") or 0, 3),
                source=result.get("source", ""),
                name=result.get("name", ""),
            )
        wx.CallAfter(self._finish_all_discovered_rtsp_tests, results, host, attempts)

    def _best_tested_stream(self, side, tested_streams, used_urls=None):
        used_urls = used_urls or set()
        candidates = [
            item for item in tested_streams
            if item.get("ok") and item.get("rtsp_url") and item.get("rtsp_url") not in used_urls
        ]
        if not candidates:
            return None, 0
        best = max(candidates, key=lambda item: self._live_stream_score(item.get("stream") or {}, side))
        return best, self._live_stream_score(best.get("stream") or {}, side)

    def _auto_fill_tested_streams_if_clear(self, passed, host):
        if not passed:
            return False
        if len(passed) == 1:
            self.rtsp_front_txt.SetValue(passed[0]["rtsp_url"])
            self._trusted_rtsp_urls.add(passed[0]["rtsp_url"])
            self._verified_rtsp_urls.add(passed[0]["rtsp_url"])
            return True
        if len(passed) == 2:
            used = set()
            front, front_score = self._best_tested_stream("front", passed, used)
            if front:
                used.add(front["rtsp_url"])
            back, back_score = self._best_tested_stream("back", passed, used)
            if front and back and front_score > 0 and back_score > 0:
                self.rtsp_front_txt.SetValue(front["rtsp_url"])
                self.rtsp_back_txt.SetValue(back["rtsp_url"])
                for item in (front, back):
                    self._trusted_rtsp_urls.add(item["rtsp_url"])
                    self._verified_rtsp_urls.add(item["rtsp_url"])
                return True
        return False

    def _choose_tested_ring_mqtt_stream(self, side, passed, host):
        streams = []
        for item in passed:
            if not item.get("ok") or not item.get("rtsp_url"):
                continue
            stream = dict(item.get("stream") or {})
            stream["rtsp_url"] = item.get("rtsp_url")
            streams.append(stream)
        return self._choose_ring_mqtt_stream(side, streams, host)

    def _finish_all_discovered_rtsp_tests(self, results, host, attempts):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring RTSP test results after setup dialog was destroyed.")
            return
        self._set_busy(False)
        passed = [item for item in results if item.get("ok") and item.get("rtsp_url")]
        failed = [item for item in results if not item.get("ok")]
        self._record_setup_event("rtsp_candidate_test_finish", "RTSP stream testing finished.", passed=len(passed), failed=len(failed))
        for item in passed:
            self._trusted_rtsp_urls.add(item["rtsp_url"])
            self._verified_rtsp_urls.add(item["rtsp_url"])

        lines = [
            f"RTSP stream testing finished. {len(passed)} passed, {len(failed)} failed.",
        ]
        for item in results:
            label = item.get("friendly_name") or item.get("name") or item.get("rtsp_url") or f"stream {item.get('index')}"
            status = "passed" if item.get("ok") else "failed"
            elapsed = item.get("elapsed")
            elapsed_text = f" in {elapsed:.1f} seconds" if isinstance(elapsed, (int, float)) else ""
            lines.append(f"- {label}: {status}{elapsed_text}.")

        if not passed:
            lines.extend([
                "",
                "No live RTSP streams passed. Viper left the RTSP fields editable. Fix Ring-MQTT video streaming or enter a live RTSP URL manually, then use the camera test buttons.",
            ])
            if attempts:
                lines.extend(["", "Discovery attempts:", *attempts])
            self._set_setup_status("\n".join(lines), announce=True)
            self._update_setup_action_gates()
            return

        if self._auto_fill_tested_streams_if_clear(passed, host):
            settings = self._settings()
            self._apply_settings_to_parent(settings)
            self.parent.save_config()
            lines.extend([
                "",
                "Viper filled the RTSP URL field(s) from tested live streams and saved the setup.",
            ])
            self._set_setup_status("\n".join(lines), announce=True)
            self._update_setup_action_gates()
            return

        front_url = self._choose_tested_ring_mqtt_stream("front", passed, host)
        if front_url:
            self.rtsp_front_txt.SetValue(front_url)
            self._trusted_rtsp_urls.add(front_url)
            self._verified_rtsp_urls.add(front_url)
        remaining = [item for item in passed if item.get("rtsp_url") != front_url]
        back_url = self._choose_tested_ring_mqtt_stream("back", remaining or passed, host)
        if back_url:
            self.rtsp_back_txt.SetValue(back_url)
            self._trusted_rtsp_urls.add(back_url)
            self._verified_rtsp_urls.add(back_url)
        if front_url or back_url:
            settings = self._settings()
            self._apply_settings_to_parent(settings)
            self.parent.save_config()
            lines.extend([
                "",
                "Viper filled the selected tested-live RTSP URL fields and saved the setup.",
            ])
        else:
            lines.extend([
                "",
                "No RTSP boxes were changed. The tested-live streams remain available; run Find Ring MQTT Streams again to choose them.",
            ])
        self._set_setup_status("\n".join(lines), announce=True)
        self._update_setup_action_gates()

    def _run_selected_rtsp_tests(self, tests):
        results = []
        for side, rtsp_url in tests:
            started = time.perf_counter()
            try:
                test_dir = cfg.DATA_DIR / "rtsp_test"
                test_dir.mkdir(parents=True, exist_ok=True)
                min_bytes = cfg.BACK_MIN_FRAME_BYTES if side == "back" else cfg.FRONT_MIN_FRAME_BYTES
                frame = vision.grab_frame(rtsp_url, test_dir, f"setup_{side}", min_bytes=min_bytes, timeout=8)
                results.append({
                    "side": side,
                    "ok": bool(frame),
                    "frame": frame,
                    "rtsp_url": rtsp_url,
                    "elapsed": time.perf_counter() - started,
                    "message": "Frame captured." if frame else "No live frame was captured before the timeout.",
                })
            except Exception as e:
                results.append({
                    "side": side,
                    "ok": False,
                    "rtsp_url": rtsp_url,
                    "elapsed": time.perf_counter() - started,
                    "message": str(e),
                })
        wx.CallAfter(self._finish_selected_rtsp_tests, results)

    def _finish_selected_rtsp_tests(self, results):
        self._set_busy(False)
        lines = ["Selected Ring-MQTT stream tests finished."]
        for result in results:
            side = result.get("side", "camera").title()
            elapsed = result.get("elapsed")
            elapsed_text = f" in {elapsed:.1f} seconds" if isinstance(elapsed, (int, float)) else ""
            if result.get("ok"):
                url = result.get("rtsp_url") or ""
                if url:
                    self._verified_rtsp_urls.add(url)
                lines.append(f"{side}: passed{elapsed_text}.")
            else:
                lines.append(f"{side}: failed{elapsed_text}. {result.get('message') or 'No live frame captured.'}")
                lines.append(f"URL tested: {result.get('rtsp_url') or ''}")
        if all(result.get("ok") for result in results):
            settings = self._settings()
            self._apply_settings_to_parent(settings)
            self.parent.save_config()
            lines.append("Both selected stream URLs passed and Viper saved them.")
        else:
            lines.append("One or more streams failed. Choose a different stream URL or check Ring-MQTT video streaming.")
        self._set_setup_status("\n".join(lines), announce=True)

    def on_pushover_toggle(self, event):
        enabled = self.pushover_enabled_chk.GetValue()
        for ctrl in (self.pushover_user_txt, self.pushover_token_txt):
            label = getattr(ctrl, "_viper_label_ctrl", None)
            if label:
                label.Show(enabled)
                label.Enable(enabled)
            ctrl.Show(enabled)
            ctrl.Enable(enabled)
        parent = self.pushover_enabled_chk.GetParent()
        if parent:
            parent.Layout()
        self.Layout()
        if event:
            event.Skip()

    def _set_busy(self, busy):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring busy state change after setup dialog was destroyed.")
            return
        def enable_control(name, enabled):
            control = getattr(self, name, None)
            if control is None:
                return
            try:
                control.Enable(enabled)
            except RuntimeError:
                logging.info("[HA SETUP] Ignoring busy state change for deleted control: %s", name)
        def enable_obj(control, enabled):
            if control is None:
                return
            try:
                control.Enable(enabled)
            except RuntimeError:
                logging.info("[HA SETUP] Ignoring busy state change for deleted advanced panel.")
        advanced_visible = bool(getattr(self, "_show_advanced_doorbell", False))
        enable_control("btn_find_ha", not busy)
        enable_control("btn_beginner_setup", not busy)
        enable_control("btn_test", not busy)
        enable_control("btn_change_doorbell_triggers_now", not busy)
        enable_control("btn_find_ring_mqtt_streams_now", not busy)
        enable_control("btn_change_camera_streams_now", not busy)
        enable_control("btn_test_front_rtsp_now", not busy)
        enable_control("btn_test_back_rtsp_now", not busy)
        enable_control("btn_install_ring_mqtt", not busy)
        enable_control("btn_mqtt", (not busy) and advanced_visible)
        enable_control("btn_ring", (not busy) and advanced_visible)
        enable_control("btn_ring_help", not busy)
        enable_control("btn_discover_setup_speakers", not busy)
        enable_control("btn_setup_summary", not busy)
        enable_control("btn_setup_test_everything", not busy)
        enable_control("btn_help", not busy)
        enable_control("btn_save", not busy)
        if getattr(self, "advanced_doorbell_panel", None):
            enable_obj(self.advanced_doorbell_panel, (not busy) and advanced_visible)
            try:
                self._set_children_enabled(self.advanced_doorbell_panel, (not busy) and advanced_visible)
            except RuntimeError:
                logging.info("[HA SETUP] Ignoring child enable after advanced panel was destroyed.")
        if not busy:
            try:
                self._update_connect_actions()
            except RuntimeError:
                logging.info("[HA SETUP] Ignoring action update after setup dialog was destroyed.")

    def on_beginner_auto_setup(self, event):
        settings = self._settings()
        if not settings["ha_token"]:
            self._set_setup_status(
                "Viper needs a Home Assistant long-lived access token before it can discover or save entities. Paste it here, or set it in the HA_TOKEN environment variable.",
                announce=True,
            )
            return
        if settings["pushover_enabled"] and (not settings["pushover_user_key"] or not settings["pushover_api_token"]):
            self._set_setup_status("Pushover is optional. Either enter both Pushover values, set PUSHOVER_USER and PUSHOVER_TOKEN in environment variables, or turn Pushover off.", announce=True)
            return
        self._set_busy(True)
        self._record_setup_event("beginner_setup_start", "Beginner automatic setup started.")
        self._set_setup_status(
            "Starting beginner setup. Viper will find Home Assistant, discover devices, pick likely doorbell triggers, find Ring MQTT live streams, and save Viper settings. Speakers are discovered separately so you can choose them.",
            announce=True,
        )
        safe_submit(self._run_beginner_auto_setup, settings)

    def _collect_live_rtsp_streams(self, settings, host):
        attempts = []
        streams = []
        ha_streams = self._run_find_ha_ring_rtsp_streams(settings, host)
        streams.extend(ha_streams.get("streams", []))
        attempts.append(ha_streams.get("attempt", "Home Assistant stream scan completed."))
        log_streams = self._run_find_ring_mqtt_log_streams(settings, host)
        streams.extend(log_streams.get("streams", []))
        attempts.append(log_streams.get("attempt", "Ring-MQTT log scan completed."))
        mqtt_host = settings.get("mqtt_host") or settings.get("ha_ip") or host
        if mqtt_host:
            mqtt_result = ring_discovery.listen_for_ring_topics(
                mqtt_host=mqtt_host,
                mqtt_port=settings.get("mqtt_port") or 1883,
                mqtt_username=settings.get("mqtt_username") or "",
                mqtt_password=settings.get("mqtt_password") or "",
                topic="ring/#",
                duration=8,
                rtsp_host=host,
                stop_on_first=False,
            )
            if mqtt_result.get("ok"):
                for item in mqtt_result.get("suggestions", []):
                    rtsp_url = item.get("rtsp_url") or ""
                    camera_id = item.get("camera_id") or ""
                    if rtsp_url and camera_id:
                        streams.append({
                            "name": f"{camera_id}_live",
                            "rtsp_url": rtsp_url,
                            "source": "ring-mqtt",
                            "topic": item.get("topic", ""),
                        })
                attempts.append(f"MQTT ring/# at {mqtt_host}:{settings.get('mqtt_port') or 1883} -> {mqtt_result.get('count', 0)} possible Ring topic(s)")
            else:
                attempts.append(
                    f"MQTT ring/# at {mqtt_host}:{settings.get('mqtt_port') or 1883} -> "
                    f"{mqtt_result.get('message') or mqtt_result.get('error') or 'failed'}"
                )
        else:
            attempts.append("MQTT scan skipped because no MQTT or Home Assistant host is set.")
        seen = set()
        unique = []
        for stream in streams:
            name = stream.get("name", "").strip()
            key = stream.get("rtsp_url") or name
            if name and key not in seen:
                seen.add(key)
                unique.append(stream)
        return {"streams": unique, "attempts": attempts}

    def _best_live_stream_url(self, side, streams, host):
        scored = sorted(
            streams or [],
            key=lambda stream: self._live_stream_score(stream, side),
            reverse=True,
        )
        if not scored:
            return "", 0, ""
        best = scored[0]
        score = self._live_stream_score(best, side)
        if score <= 0:
            return "", score, best.get("name", "")
        name = best.get("name", "")
        url = best.get("rtsp_url") or (f"rtsp://{host}:8554/{name}" if name else "")
        return url, score, name

    def _run_beginner_auto_setup(self, settings):
        result = {"ok": False, "message": "Beginner setup did not complete."}
        started = time.perf_counter()
        def finish(result):
            result["elapsed"] = time.perf_counter() - started
            self._record_setup_event(
                "beginner_setup_finish",
                result.get("message") or "",
                ok=bool(result.get("ok")),
                elapsed=round(result.get("elapsed") or 0, 3),
            )
            wx.CallAfter(self._finish_beginner_auto_setup, result)

        try:
            host_result = discovery.find_home_assistant(
                token=settings.get("ha_token") or None,
                seed_host=settings.get("ha_ip") or "",
                seed_port=settings.get("ha_port") or "8123",
                timeout=2,
            )
            if not host_result.get("ok"):
                result = {
                    "ok": False,
                    "message": host_result.get("message") or "Home Assistant was not found.",
                    "host_result": host_result,
                }
                finish(result)
                return
            settings["ha_ip"] = host_result.get("ha_ip") or settings.get("ha_ip") or ""
            settings["ha_port"] = host_result.get("ha_port") or settings.get("ha_port") or "8123"
            if host_result.get("auth_error") == "bad_token":
                result = {
                    "ok": False,
                    "message": "Home Assistant was found, but it rejected the long-lived access token.",
                    "host_result": host_result,
                }
                finish(result)
                return

            entity_result = discovery.discover_ha_entities(
                ha_ip=settings["ha_ip"],
                ha_port=settings["ha_port"],
                token=settings["ha_token"],
                timeout=8,
            )
            if not entity_result.get("ok"):
                result = {
                    "ok": False,
                    "message": entity_result.get("message") or "Home Assistant entity discovery failed.",
                    "host_result": host_result,
                    "discovery": entity_result,
                }
                finish(result)
                return

            rtsp_host = self._rtsp_host_from_ha_host(settings["ha_ip"])
            stream_result = self._collect_live_rtsp_streams(settings, rtsp_host) if rtsp_host else {"streams": [], "attempts": []}
            result = {
                "ok": True,
                "settings": settings,
                "host_result": host_result,
                "discovery": entity_result,
                "streams": stream_result.get("streams", []),
                "stream_attempts": stream_result.get("attempts", []),
            }
        except Exception as e:
            logging.exception("[HA SETUP] Beginner auto setup failed unexpectedly")
            result = {"ok": False, "message": str(e)}
        finish(result)

    def _auto_add_ha_speakers_from_discovery(self):
        if not self.discovery_result or not self.discovery_result.get("ok"):
            return 0
        speakers = self.parent.config.setdefault("speakers", {})
        existing_ids = {data.get("id") for data in speakers.values() if isinstance(data, dict)}
        added = 0
        for entity in self.discovery_result.get("categories", {}).get("media_players", []):
            entity_id = entity.get("entity_id")
            if not entity_id or entity_id in existing_ids:
                continue
            name = entity.get("friendly_name") or entity_id.replace("media_player.", "")
            spk_type = "alexa" if "echo" in name.lower() or "alexa" in entity_id.lower() else "ha"
            speakers[f"{name} ({spk_type.upper()})"] = {
                "id": entity_id,
                "type": spk_type,
                "enabled": True,
                "doorbell": True,
                "utilities": True,
                "fridge": True,
                "quiet_hours_exempt": False,
            }
            existing_ids.add(entity_id)
            added += 1
        return added

    def _finish_beginner_auto_setup(self, result):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring beginner setup result after setup dialog was destroyed.")
            return
        self._set_busy(False)
        self._show_discover_devices = True
        self._update_connect_actions()
        if not result.get("ok"):
            self._set_setup_status(result.get("message") or "Beginner setup failed.", announce=True)
            return

        settings = result.get("settings", {})
        self.ha_ip_txt.SetValue(settings.get("ha_ip") or "")
        self.ha_port_txt.SetValue(settings.get("ha_port") or "8123")
        self.discovery_result = result.get("discovery")
        self._devices_discovered = bool(self.discovery_result and self.discovery_result.get("ok"))
        self._ha_find_failed = False
        self._update_connect_actions()
        self._populate_trigger_choices_from_config(
            self._choice_entity_id(self.front_trigger_choice),
            self._choice_entity_id(self.back_trigger_choice),
        )
        doorbell_result = self._auto_configure_doorbells_from_discovery()

        host = self._rtsp_host_from_ha_host(settings.get("ha_ip") or "")
        streams = result.get("streams") or []
        front_url, front_score, front_name = self._best_live_stream_url("front", streams, host)
        back_url, back_score, back_name = self._best_live_stream_url("back", streams, host)
        if front_url:
            self.rtsp_front_txt.SetValue(front_url)
            self._trusted_rtsp_urls.add(front_url)
        if back_url and back_url != front_url:
            self.rtsp_back_txt.SetValue(back_url)
            self._trusted_rtsp_urls.add(back_url)
        speaker_count = 0

        save_settings = self._settings()
        self._apply_settings_to_parent(save_settings)
        self.parent.save_config()
        cfg.sync_globals_from_config()

        counts = self.discovery_result.get("counts", {}) if self.discovery_result else {}
        lines = [
            "Beginner setup complete. Viper saved its config file.",
            f"Home Assistant: {save_settings['ha_ip']}:{save_settings['ha_port']}",
            f"Entities discovered: {self.discovery_result.get('entity_count', 0) if self.discovery_result else 0}",
            "Media players added automatically: 0. Speakers are left for you to choose.",
            f"Front selected trigger: {save_settings.get('front_trigger_entity_id') or 'not selected'}",
            f"Back selected trigger: {save_settings.get('back_trigger_entity_id') or 'not selected'}",
            f"Front live stream: {front_name or ('saved value' if save_settings.get('rtsp_front') else 'not found')}",
            f"Back live stream: {back_name or ('saved value' if save_settings.get('rtsp_back') else 'not found')}",
            f"Vacuums found: {counts.get('vacuum_entities', 0)}",
            "",
            doorbell_result.get("message") or "",
        ]
        if not save_settings.get("rtsp_front") or not save_settings.get("rtsp_back"):
            lines.append("Camera setup still needs attention. Use Find Ring MQTT Streams or open Ring Setup Assistant.")
        else:
            lines.append("Next step: press Test Front Camera Now and Test Back Camera Now below this status box.")
            if not self._auto_speaker_discovery_done:
                lines.append("Viper will now discover available speakers so you can choose what to add.")
        self._set_setup_status("\n".join(lines), announce=True)
        if not self._auto_speaker_discovery_done:
            self._auto_speaker_discovery_done = True
            wx.CallAfter(self.on_discover_setup_speakers, None)
        else:
            wx.CallAfter(self._focus_camera_test_actions)

    def on_find_ha(self, event):
        settings = self._settings()
        self._record_setup_event("find_ha_start", "Finding Home Assistant.", seed_host=settings.get("ha_ip", ""))
        self._set_busy(True)
        self._set_setup_status(
            "Looking for Home Assistant. Viper will try the address you entered, homeassistant.local, and common local network addresses.",
            announce=True,
        )
        safe_submit(self._run_find_ha, settings)

    def _run_find_ha(self, settings):
        started = time.perf_counter()
        try:
            result = discovery.find_home_assistant(
                token=settings.get("ha_token") or None,
                seed_host=settings.get("ha_ip") or "",
                seed_port=settings.get("ha_port") or "8123",
                timeout=2,
            )
        except Exception as e:
            logging.exception("[HA SETUP] Find Home Assistant failed unexpectedly")
            result = {"ok": False, "error": "unexpected_error", "message": str(e), "attempts": []}
        self._record_setup_event(
            "find_ha_finish",
            result.get("message") or "",
            ok=bool(result.get("ok")),
            auth_ok=bool(result.get("auth_ok")),
            elapsed=round(time.perf_counter() - started, 3),
        )
        wx.CallAfter(self._finish_find_ha, result)

    def _finish_find_ha(self, result):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring Home Assistant find result after setup dialog was destroyed.")
            return
        if result.get("ok"):
            self._ha_find_failed = False
            self._show_discover_devices = True
            self.ha_ip_txt.SetValue(result.get("ha_ip", ""))
            self.ha_port_txt.SetValue(result.get("ha_port", "8123"))
            auth_note = "Token accepted." if result.get("auth_ok") else "Host found. Token still needs to be tested."
            if result.get("auth_error") == "bad_token":
                auth_note = "Host found, but Home Assistant rejected the token."
            _dialog_status(self, f"Found Home Assistant at {result.get('ha_ip')}:{result.get('ha_port')}. {auth_note}", announce=True)
            self._refresh_derived_doorbell_preview()
            if result.get("auth_error") == "bad_token":
                self._set_busy(False)
                return
            if self._settings().get("ha_token"):
                settings = self._settings()
                _dialog_status(
                    self,
                    f"Found Home Assistant at {result.get('ha_ip')}:{result.get('ha_port')}. "
                    "Now discovering sensors, cameras, speakers, vacuums, and doorbell triggers...",
                    announce=True,
                )
                safe_submit(self._run_discovery_test, settings)
                return
            _dialog_status(
                self,
                "Home Assistant was found. Paste a long-lived access token, then press Discover Devices. If your token is set in environment variables, you can leave the token box blank.",
                announce=True,
            )
            self._set_busy(False)
            self._update_connect_actions()
            return
        self._set_busy(False)
        self._ha_find_failed = True
        self._update_connect_actions()
        attempts = result.get("attempts", [])
        detail_lines = [
            "Home Assistant was not found automatically. Enter the host manually, usually homeassistant.local or the HA IP address.\n"
            f"Attempts made: {len(attempts)}"
        ]
        for attempt in attempts[:6]:
            if attempt.get("url"):
                detail_lines.append(f"Tried {attempt.get('url')}: HTTP {attempt.get('status_code')}.")
            elif attempt.get("ha_ip"):
                detail_lines.append(f"Tried {attempt.get('ha_ip')}:{attempt.get('ha_port')}: {attempt.get('error', 'no response')}.")
        if result.get("message"):
            detail_lines.append(result.get("message"))
        _dialog_status(self, "\n".join(detail_lines), announce=True)

    def on_test_rtsp(self, event, side):
        settings = self._settings()
        rtsp_url = settings["rtsp_back"] if side == "back" else settings["rtsp_front"]
        if not rtsp_url:
            self._set_setup_status(f"Enter the {side} door RTSP URL before testing it.", announce=True)
            return
        self._record_setup_event("manual_rtsp_test_start", f"Testing {side} RTSP URL.", side=side)
        self._set_busy(True)
        self._set_setup_status(f"Testing {side} door RTSP. This checks for a live video frame.", announce=True)
        safe_submit(self._run_test_rtsp, side, rtsp_url)

    def _run_test_rtsp(self, side, rtsp_url):
        started = time.perf_counter()
        try:
            test_dir = cfg.DATA_DIR / "rtsp_test"
            test_dir.mkdir(parents=True, exist_ok=True)
            min_bytes = cfg.BACK_MIN_FRAME_BYTES if side == "back" else cfg.FRONT_MIN_FRAME_BYTES
            frame = vision.grab_frame(rtsp_url, test_dir, f"setup_{side}", min_bytes=min_bytes, timeout=8)
            result = {"ok": bool(frame), "frame": frame, "rtsp_url": rtsp_url}
        except Exception as e:
            result = {"ok": False, "message": str(e), "rtsp_url": rtsp_url}
        result["elapsed"] = time.perf_counter() - started
        wx.CallAfter(self._finish_test_rtsp, side, result)

    def _finish_test_rtsp(self, side, result):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring manual RTSP test result after setup dialog was destroyed.")
            return
        self._set_busy(False)
        elapsed = result.get("elapsed")
        elapsed_text = f" in {elapsed:.1f} seconds" if isinstance(elapsed, (int, float)) else ""
        url = result.get("rtsp_url") or ""
        if result.get("ok"):
            frame = result.get("frame") or ""
            message = f"{side.title()} door camera test passed. Viper captured a live RTSP frame{elapsed_text}."
            if frame:
                message += f"\nFrame saved at {frame}."
            if url:
                self._verified_rtsp_urls.add(url)
            logging.info("[SETUP RTSP TEST] side=%s ok=True elapsed=%.3f url=%s frame=%s", side, elapsed or 0, url, frame)
            self._record_setup_event("manual_rtsp_test_finish", message, side=side, ok=True, elapsed=round(elapsed or 0, 3))
            self._set_setup_status(message, announce=True)
        else:
            detail = result.get("message") or "No live frame was captured before the timeout."
            message = (
                f"{side.title()} door camera test failed{elapsed_text}. "
                f"Check Ring-MQTT video streaming, the stream name, and the RTSP URL.\nURL tested: {url}\n{detail}"
            )
            logging.warning("[SETUP RTSP TEST] side=%s ok=False elapsed=%.3f url=%s message=%s", side, elapsed or 0, url, detail)
            self._record_setup_event("manual_rtsp_test_finish", message, side=side, ok=False, elapsed=round(elapsed or 0, 3))
            self._set_setup_status(message, announce=True)

    def _check_supervisor_install_permission(self, settings):
        try:
            self._hassio_request(settings, "GET", "/supervisor/info", timeout=8)
            return {
                "ok": True,
                "reason": "ok",
                "message": "Installer permission: this Home Assistant token can access Supervisor add-on management.",
            }
        except Exception as e:
            message = str(e)
            lowered = message.lower()
            if "rejected this token" in lowered:
                return {
                    "ok": False,
                    "reason": "supervisor_token_rejected",
                    "message": "Installer permission: blocked. Viper can use the normal Home Assistant API, but Supervisor add-on management rejected this external token. Use the Home Assistant VM console fallback.",
                }
            if "did not expose the supervisor" in lowered:
                return {
                    "ok": False,
                    "reason": "supervisor_unavailable",
                    "message": "Installer permission: not available because this Home Assistant system does not expose Supervisor add-on management.",
                }
            return {
                "ok": False,
                "reason": "check_failed",
                "message": f"Installer permission: could not be checked. {message}",
            }

    def on_install_ring_mqtt_requirements(self, event):
        settings = self._settings()
        if not settings["ha_ip"] or not settings["ha_token"]:
            self._set_setup_status(
                "Enter the Home Assistant host and long-lived access token before installing Ring MQTT requirements.",
                announce=True,
            )
            return
        self._set_busy(True)
        self._set_setup_status(
            "Installing Ring MQTT requirements. Viper will add the Ring-MQTT repository, install Mosquitto Broker, install Ring-MQTT with Video Streaming, and start Mosquitto if possible.",
            announce=True,
        )
        safe_submit(self._run_install_ring_mqtt_requirements, settings)

    def _hassio_request(self, settings, method, path, *, payload=None, timeout=30):
        ha_ip = settings.get("ha_ip") or ""
        ha_port = settings.get("ha_port") or "8123"
        token = settings.get("ha_token") or ""
        url = f"http://{ha_ip}:{ha_port}/api/hassio{path}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = requests.request(method, url, headers=headers, json=payload or {}, timeout=timeout)
        if response.status_code == 404:
            return self._hassio_ws_request(settings, method, path, payload=payload, timeout=timeout)
        if response.status_code in {401, 403}:
            logging.info("[HA SETUP] REST Supervisor API returned HTTP %s for %s; trying WebSocket supervisor/api fallback.", response.status_code, path)
            return self._hassio_ws_request(settings, method, path, payload=payload, timeout=timeout)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {}

    def _hassio_ws_request(self, settings, method, path, *, payload=None, timeout=30):
        return self._ha_ws_command(
            settings,
            {
                "type": "supervisor/api",
                "endpoint": path,
                "method": str(method or "GET").lower(),
                "data": payload or {},
                "timeout": timeout,
            },
            timeout=timeout,
        )

    def _ha_ws_command(self, settings, command, *, timeout=30):
        ha_ip = settings.get("ha_ip") or ""
        ha_port = settings.get("ha_port") or "8123"
        token = settings.get("ha_token") or ""
        if not ha_ip or not token:
            raise RuntimeError("Home Assistant host or token is missing.")

        async def call_ws_command():
            host = ha_ip
            port = str(ha_port or "8123")
            scheme = "wss" if str(host).startswith("https://") else "ws"
            if "://" in str(host):
                parsed = urlparse(str(host))
                scheme = "wss" if parsed.scheme == "https" else "ws"
                host = parsed.hostname or host
                port = str(parsed.port or port or ("443" if scheme == "wss" else "8123"))
            ws_url = f"{scheme}://{host}:{port}/api/websocket"
            async with websockets.connect(ws_url, open_timeout=min(float(timeout), 10.0)) as ws:
                greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if greeting.get("type") != "auth_required":
                    raise RuntimeError("Home Assistant WebSocket did not request authentication.")
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if auth.get("type") != "auth_ok":
                    raise RuntimeError("Home Assistant rejected the token for WebSocket access.")
                message = dict(command or {})
                message["id"] = 1
                await ws.send(json.dumps(message))
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if not response.get("success"):
                    error = response.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    code = error.get("code") if isinstance(error, dict) else ""
                    raise RuntimeError(message or code or "Home Assistant rejected the WebSocket request.")
                result = response.get("result")
                return result if isinstance(result, dict) else {}

        try:
            return asyncio.run(call_ws_command())
        except RuntimeError as e:
            raise RuntimeError(f"Home Assistant WebSocket request failed: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Home Assistant WebSocket request failed: {e}") from e

    def _addon_items_from_payload(self, payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("addons"), list):
                return data.get("addons")
            if isinstance(payload.get("addons"), list):
                return payload.get("addons")
        return []

    def _payload_data(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload.get("data")
        return payload if isinstance(payload, dict) else {}

    def _get_installed_addons(self, settings):
        try:
            payload = self._hassio_request(settings, "GET", "/addons", timeout=30)
            addons = self._addon_items_from_payload(payload)
            if addons:
                return addons
        except Exception as e:
            logging.info("[HA SETUP] Installed add-on list unavailable from /addons: %s", e)
        try:
            payload = self._hassio_request(settings, "GET", "/supervisor/info", timeout=30)
            data = self._payload_data(payload)
            addons = data.get("addons", []) if isinstance(data, dict) else []
            return addons if isinstance(addons, list) else []
        except Exception as e:
            logging.info("[HA SETUP] Installed add-on list unavailable from supervisor info: %s", e)
            return []

    def _get_addon_info(self, settings, slug):
        payload = self._hassio_request(settings, "GET", f"/addons/{slug}/info", timeout=30)
        return self._payload_data(payload)

    def _ensure_addon_started(self, settings, slug):
        if not slug:
            return False
        try:
            info = self._get_addon_info(settings, slug)
            if str(info.get("state", "")).lower() in {"started", "running"}:
                return True
        except Exception:
            pass
        try:
            self._hassio_request(settings, "POST", f"/addons/{slug}/start", timeout=90)
            return True
        except Exception as e:
            message = str(e).lower()
            if "already" in message or "started" in message or "running" in message:
                return True
            raise

    def _restart_addon(self, settings, slug):
        if not slug:
            return False
        try:
            self._hassio_request(settings, "POST", f"/addons/{slug}/restart", timeout=120)
            return True
        except Exception as e:
            logging.info("[HA SETUP] Add-on restart failed for %s; falling back to start. %s", slug, e)
            return self._ensure_addon_started(settings, slug)

    def _configure_ring_mqtt_rtsp_port(self, settings):
        payload = {"network": {"8554/tcp": 8554}}
        self._hassio_request(settings, "POST", f"/addons/{RING_MQTT_ADDON_SLUG}/options", payload=payload, timeout=60)
        return True

    def _configure_ring_mqtt_rtsp_port_and_restart(self, settings):
        self._configure_ring_mqtt_rtsp_port(settings)
        self._restart_addon(settings, RING_MQTT_ADDON_SLUG)
        self._ensure_addon_started(settings, RING_MQTT_ADDON_SLUG)
        return True

    def _absolute_ha_url(self, settings, path_or_url):
        text = str(path_or_url or "").strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        ha_ip = settings.get("ha_ip") or ""
        ha_port = settings.get("ha_port") or "8123"
        if not text.startswith("/"):
            text = "/" + text
        return f"http://{ha_ip}:{ha_port}{text}"

    def _normalize_addon_webui(self, settings, value):
        text = str(value or "").strip()
        if not text:
            return ""
        ha_ip = settings.get("ha_ip") or ""
        text = text.replace("[HOST]", ha_ip).replace("{host}", ha_ip).replace("0.0.0.0", ha_ip)
        text = re.sub(r"\[PORT:(\d+)\]", r"\1", text)
        text = re.sub(r"\[PROTO:[^\]]+\]", "http", text)
        return self._absolute_ha_url(settings, text)

    def _get_current_ha_user_id(self, settings):
        try:
            user = self._ha_ws_command(settings, {"type": "auth/current_user"}, timeout=15)
        except Exception as e:
            logging.info("[HA SETUP] Could not read current Home Assistant user for ingress session: %s", e)
            return ""
        return str(user.get("id") or user.get("user_id") or "").strip()

    def _create_ingress_session(self, settings):
        user_id = self._get_current_ha_user_id(settings)
        payload = {"user_id": user_id} if user_id else {}
        session_payload = self._hassio_request(settings, "POST", "/ingress/session", payload=payload, timeout=30)
        session_data = self._payload_data(session_payload)
        return session_data.get("session") or session_data.get("ingress_session") or session_data.get("token") or ""

    def _ingress_session_url(self, settings, session, addon_info):
        token = str(session or "").strip()
        if not token:
            return ""
        data = addon_info if isinstance(addon_info, dict) else {}

        def suffix_from_ingress_path(value):
            text = str(value or "").strip()
            marker = "/api/hassio_ingress/"
            if marker not in text:
                return text
            after = text.split(marker, 1)[1]
            parts = after.split("/", 1)
            if len(parts) == 2 and parts[1]:
                return "/" + parts[1].lstrip("/")
            return "/"

        entry = str(data.get("ingress_entry") or "").strip()
        if entry:
            entry = suffix_from_ingress_path(entry)
            if not entry.startswith("/"):
                entry = "/" + entry
            return self._absolute_ha_url(settings, f"/api/hassio_ingress/{token}{entry}")

        ingress_url = str(data.get("ingress_url") or "").strip()
        suffix = suffix_from_ingress_path(ingress_url) or "/"
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return self._absolute_ha_url(settings, f"/api/hassio_ingress/{token}{suffix}")

    def _resolve_addon_login_url(self, settings, slug):
        if self._is_ring_mqtt_slug(slug):
            return self._absolute_ha_url(settings, f"/app/{RING_MQTT_ADDON_SLUG}")
        info = self._get_addon_info(settings, slug)
        data = self._payload_data(info)
        if data.get("ingress") or data.get("ingress_url") or data.get("ingress_entry"):
            return self._absolute_ha_url(settings, f"/app/{slug}")
        for key in ("webui",):
            if data.get(key):
                return self._normalize_addon_webui(settings, data.get(key))
        return self._ring_mqtt_app_page_url(settings, slug)

    def _ring_mqtt_app_page_url(self, settings, slug):
        if self._is_ring_mqtt_slug(slug):
            slug = RING_MQTT_ADDON_SLUG
        return self._absolute_ha_url(settings, f"/config/app/{slug}/info")

    def _open_ring_mqtt_login(self, slug):
        settings = self._settings()
        if not self._is_ring_mqtt_slug(slug):
            message = (
                f"Viper refused to open add-on slug '{slug}' because it is not Ring-MQTT. "
                "This prevents Home Assistant from opening the wrong app, such as Matter Server. "
                "Install Ring-MQTT with Video Streaming, then run the installer again."
            )
            logging.warning("[HA SETUP] %s", message)
            self._set_setup_status(message, announce=True)
            return
        try:
            self._ensure_addon_started(settings, slug)
            url = self._resolve_addon_login_url(settings, slug)
        except Exception as e:
            app_url = self._ring_mqtt_app_page_url(settings, slug)
            self._set_setup_status(
                f"Ring-MQTT is installed, but Viper could not open the Ring login page automatically: {e}\n"
                f"Open this Ring-MQTT app page in your browser instead: {app_url}",
                announce=True,
            )
            open_help("ring-mqtt-setup")
            return
        if not url:
            app_url = self._ring_mqtt_app_page_url(settings, slug)
            self._set_setup_status(f"Ring-MQTT login URL was not found. Open this Ring-MQTT app page in your browser instead: {app_url}", announce=True)
            return
        ha_login_url = self._ring_mqtt_app_page_url(settings, slug)
        logging.info("[HA SETUP] Opening Ring-MQTT slug=%s app_url=%s login_url=%s", slug, ha_login_url, url)
        dlg = RingMqttLoginDialog(self, url, ha_login_url=ha_login_url)
        try:
            completed = dlg.ShowModal() == wx.ID_OK
        finally:
            dlg.Destroy()
        if completed:
            self._after_ring_mqtt_login()

    def _after_ring_mqtt_login(self):
        settings = self._settings()
        mqtt_host = settings.get("mqtt_host") or settings.get("ha_ip")
        if mqtt_host and (settings.get("mqtt_username") or settings.get("mqtt_password")):
            self._set_busy(True)
            self._set_setup_status("Checking Ring-MQTT streams now that Ring login is complete.", announce=True)
            host = self._rtsp_host_from_ha_host(settings.get("ha_ip") or mqtt_host)
            safe_submit(self._run_find_live_rtsp_streams, host, settings)
        else:
            self._set_setup_status(
                "Ring login window closed. Next, enter MQTT credentials if needed, then press Find Ring MQTT Streams.",
                announce=True,
            )

    def _find_addon_slug(self, addons, *, exact_slugs=(), text_tokens=()):
        lowered_exact = {str(item).lower() for item in exact_slugs}
        if lowered_exact:
            for addon in addons:
                slug = str(addon.get("slug") or addon.get("addon") or "").strip()
                if slug.lower() in lowered_exact:
                    return slug
        for addon in addons:
            haystack = " ".join(
                str(addon.get(key, ""))
                for key in ("slug", "addon", "name", "description", "repository", "url")
            ).lower()
            if all(token.lower() in haystack for token in text_tokens):
                return str(addon.get("slug") or addon.get("addon") or "").strip()
        return ""

    def _find_ring_mqtt_slug(self, addons):
        exact_slugs = {"ring_mqtt", RING_MQTT_ADDON_SLUG}
        preferred_repo = "03cabcc9"
        preferred_repo_url = "github.com/tsightler/ring-mqtt-ha-addon"
        exact_names = {"ring-mqtt with video streaming", "ring mqtt with video streaming"}
        candidates = []
        for addon in addons or []:
            slug = str(addon.get("slug") or addon.get("addon") or "").strip()
            name = str(addon.get("name") or "").strip()
            description = str(addon.get("description") or "")
            repository = str(addon.get("repository") or "")
            url = str(addon.get("url") or addon.get("repository_url") or addon.get("source") or "")
            slug_l = slug.lower()
            name_l = name.lower()
            description_l = description.lower()
            repository_l = repository.lower()
            url_l = url.lower()
            if not slug:
                continue
            if slug_l in exact_slugs:
                return RING_MQTT_ADDON_SLUG
            score = 0
            if name_l in exact_names:
                score += 100
            if "ring_mqtt" in slug_l or "ring-mqtt" in slug_l:
                score += 80
            if repository == preferred_repo:
                score += 60
            if preferred_repo_url in repository_l or preferred_repo_url in url_l:
                score += 80
            if "ring devices" in description_l and "mqtt" in description_l:
                score += 30
            if "video streaming" in name_l or "video streaming" in description_l:
                score += 10
            has_ring = "ring" in slug_l or "ring" in name_l or "ring" in description_l or preferred_repo_url in repository_l or preferred_repo_url in url_l
            has_mqtt = "mqtt" in slug_l or "mqtt" in name_l or "mqtt" in description_l or preferred_repo_url in repository_l or preferred_repo_url in url_l
            if score >= 80 and has_ring and has_mqtt:
                candidates.append((score, slug))
        if not candidates:
            return ""
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _is_ring_mqtt_slug(self, slug):
        slug_l = str(slug or "").strip().lower()
        return slug_l in {"ring_mqtt", RING_MQTT_ADDON_SLUG}

    def _addon_installed_in_store(self, addons, slug):
        wanted = str(slug or "").lower()
        for addon in addons or []:
            addon_slug = str(addon.get("slug") or addon.get("addon") or "").lower()
            if addon_slug == wanted and bool(addon.get("installed")):
                return True
        return False

    def _run_install_ring_mqtt_requirements(self, settings):
        lines = ["Ring MQTT Requirements Installer"]
        def progress(message, *, announce=False):
            self._append_setup_progress(lines, message, announce=announce)

        try:
            progress("Checking whether Home Assistant Supervisor accepts add-on setup requests.", announce=True)
            self._hassio_request(settings, "GET", "/supervisor/info", timeout=12)
            progress("Supervisor API: available.")

            repo_url = "https://github.com/tsightler/ring-mqtt-ha-addon"
            try:
                progress("Adding the Ring-MQTT add-on repository if it is not already present.")
                self._hassio_request(settings, "POST", "/store/repositories", payload={"repository": repo_url}, timeout=60)
                progress("Ring-MQTT repository: added.", announce=True)
            except Exception as e:
                message = str(e)
                if "already" in message.lower() or "exist" in message.lower():
                    progress("Ring-MQTT repository: already present.")
                else:
                    progress(f"Ring-MQTT repository: add reported {message}")

            try:
                progress("Reloading the Home Assistant app store so Ring-MQTT appears.")
                self._hassio_request(settings, "POST", "/store/reload", timeout=60)
                progress("App store: reloaded.")
            except Exception as e:
                progress(f"App store reload: {e}")

            progress("Reading Home Assistant app store add-ons.")
            store_payload = self._hassio_request(settings, "GET", "/store/addons", timeout=30)
            addons = self._addon_items_from_payload(store_payload)
            progress("Reading installed Home Assistant add-ons.")
            installed_addons = self._get_installed_addons(settings)
            mosquitto_slug = self._find_addon_slug(
                installed_addons,
                exact_slugs=("core_mosquitto",),
                text_tokens=("mosquitto",),
            ) or self._find_addon_slug(
                addons,
                exact_slugs=("core_mosquitto",),
                text_tokens=("mosquitto",),
            )
            ring_slug = self._find_addon_slug(
                installed_addons,
                exact_slugs=(RING_MQTT_ADDON_SLUG, "ring_mqtt"),
            ) or self._find_ring_mqtt_slug(installed_addons) or self._find_addon_slug(
                addons,
                exact_slugs=(RING_MQTT_ADDON_SLUG, "ring_mqtt"),
            ) or self._find_ring_mqtt_slug(addons)
            if ring_slug and not self._is_ring_mqtt_slug(ring_slug):
                logging.warning("[HA SETUP] Refusing non-Ring-MQTT add-on slug during detection: %s", ring_slug)
                progress(f"Ignoring non-Ring-MQTT add-on that matched by mistake: {ring_slug}.")
                ring_slug = ""

            if not mosquitto_slug:
                progress("Mosquitto Broker: not found in the app store.", announce=True)
            else:
                mosquitto_already_installed = bool(self._find_addon_slug(installed_addons, exact_slugs=(mosquitto_slug,))) or self._addon_installed_in_store(addons, mosquitto_slug)
                if mosquitto_already_installed:
                    progress(f"Mosquitto Broker: already installed as {mosquitto_slug}.")
                else:
                    try:
                        progress(f"Installing Mosquitto Broker as {mosquitto_slug}. This can take a minute.")
                        self._hassio_request(settings, "POST", f"/store/addons/{mosquitto_slug}/install", payload={"background": False}, timeout=180)
                        progress(f"Mosquitto Broker: installed as {mosquitto_slug}.", announce=True)
                    except Exception as e:
                        message = str(e)
                        if "already" in message.lower() or "installed" in message.lower():
                            progress(f"Mosquitto Broker: already installed as {mosquitto_slug}.")
                        else:
                            progress(f"Mosquitto Broker install: {message}")
                try:
                    progress("Starting Mosquitto Broker if it is not already running.")
                    self._ensure_addon_started(settings, mosquitto_slug)
                    progress("Mosquitto Broker: start requested.")
                except Exception as e:
                    progress(f"Mosquitto Broker start: {e}")

            if not ring_slug:
                installed = False
                for candidate_slug in (RING_MQTT_ADDON_SLUG, "ring_mqtt"):
                    try:
                        progress(f"Installing Ring-MQTT with Video Streaming as {candidate_slug}. This can take several minutes.")
                        self._hassio_request(settings, "POST", f"/store/addons/{candidate_slug}/install", payload={"background": False}, timeout=240)
                        ring_slug = candidate_slug
                        installed = True
                        progress(f"Ring-MQTT with Video Streaming: installed as {candidate_slug}.", announce=True)
                        break
                    except Exception as e:
                        message = str(e)
                        if "already" in message.lower() or "installed" in message.lower():
                            ring_slug = candidate_slug
                            installed = True
                            progress(f"Ring-MQTT with Video Streaming: already installed as {candidate_slug}.")
                            break
                if not installed:
                    progress("Ring-MQTT with Video Streaming: not found after adding the repository. Viper did not open another add-on.", announce=True)
                else:
                    try:
                        progress("Starting Ring-MQTT.")
                        self._ensure_addon_started(settings, ring_slug)
                        progress("Ring-MQTT: start requested.")
                    except Exception as e:
                        progress(f"Ring-MQTT start: {e}")
            else:
                ring_already_installed = bool(self._find_addon_slug(installed_addons, exact_slugs=(ring_slug,))) or self._addon_installed_in_store(addons, ring_slug)
                if ring_already_installed:
                    progress("Ring-MQTT is already installed. Opening Ring login now when setup finishes.", announce=True)
                else:
                    try:
                        progress(f"Installing Ring-MQTT with Video Streaming as {ring_slug}. This can take several minutes.")
                        self._hassio_request(settings, "POST", f"/store/addons/{ring_slug}/install", payload={"background": False}, timeout=240)
                        progress(f"Ring-MQTT with Video Streaming: installed as {ring_slug}.", announce=True)
                    except Exception as e:
                        message = str(e)
                        if "already" in message.lower() or "installed" in message.lower():
                            progress(f"Ring-MQTT with Video Streaming: already installed as {ring_slug}.")
                        else:
                            progress(f"Ring-MQTT install: {message}")
                try:
                    progress("Starting Ring-MQTT.")
                    self._ensure_addon_started(settings, ring_slug)
                    progress("Ring-MQTT: start requested.")
                except Exception as e:
                    progress(f"Ring-MQTT start: {e}")

            if ring_slug:
                try:
                    progress("Configuring Ring-MQTT RTSP port 8554 and restarting Ring-MQTT.")
                    self._configure_ring_mqtt_rtsp_port_and_restart(settings)
                    progress("Ring-MQTT RTSP port 8554: configured.")
                    progress("Ring-MQTT: restarted so RTSP port 8554 is active.", announce=True)
                except Exception as e:
                    progress(
                        "Ring-MQTT RTSP port 8554: could not be configured automatically. "
                        "Open the Ring-MQTT app configuration in Home Assistant, set network port 8554 for 8554/tcp, save, and restart Ring-MQTT."
                    )
                    progress(f"Ring-MQTT RTSP port error: {e}")

            progress("")
            progress("Next steps:")
            progress("1. Viper will open the Ring-MQTT app page in your normal browser.")
            progress("2. On that Home Assistant app page, tab to Open Web UI and activate it.")
            progress("3. Enter Ring credentials only inside Ring-MQTT or Home Assistant.")
            progress("4. After Ring-MQTT login is complete, return to Viper and press Find Ring MQTT Streams.", announce=True)
            result = {"ok": True, "message": "\n".join(lines), "ring_slug": ring_slug}
        except Exception as e:
            lines.extend([
                f"Installer could not continue: {e}",
                "",
                "Accessible fallback using the Home Assistant VM console:",
                "1. Open the Home Assistant VirtualBox window or console.",
                "2. At the ha > prompt, run:",
                "   addons repositories add https://github.com/tsightler/ring-mqtt-ha-addon",
                "   addons reload",
                "   addons list",
                "   addons install core_mosquitto",
                "3. Run addons list again, find the Ring-MQTT slug, then run:",
                "   addons install SLUG_HERE",
                "",
                "If you are using SSH instead of the ha > console prompt, prefix each command with ha, for example: ha addons list",
            ])
            result = {"ok": False, "message": "\n".join(lines)}
        wx.CallAfter(self._finish_install_ring_mqtt_requirements, result)

    def _finish_install_ring_mqtt_requirements(self, result):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring Ring-MQTT installer result after setup dialog was destroyed.")
            return
        self._set_busy(False)
        self._set_setup_status(result.get("message") or "Ring MQTT installer finished.", announce=True)
        if not result.get("ok"):
            open_help("ring-mqtt-setup")
            return
        ring_slug = result.get("ring_slug") or ""
        if ring_slug:
            wx.CallAfter(self._open_ring_mqtt_login, ring_slug)
        else:
            open_help("ring-mqtt-setup")

    def on_ring_setup_assistant(self, event):
        settings = self._settings()
        lines = [
            "Ring Setup Assistant",
            "",
            "For Ring doorbell vision, Viper now expects Mosquitto plus Ring-MQTT with Video Streaming for live RTSP streams.",
            "The normal Home Assistant Ring integration can still provide trigger entities, but Ring-MQTT is the supported path for Ring camera video.",
            "",
        ]
        if not self.discovery_result:
            lines.append("Next step: press Discover Devices so Viper can look for Ring, doorbell, motion, camera, and speaker entities.")
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
            lines.append("Likely status: Viper can use this setup. Test each Ring-MQTT RTSP URL, save, then trigger the doorbell.")
        elif ring_cameras or cameras or door_sensors:
            lines.append("Likely status: Home Assistant has some useful entities, but the doorbell setup is incomplete.")
            if not (front_trigger or back_trigger):
                lines.append("Choose the Home Assistant entity that changes when Ring motion or a doorbell press happens.")
            if not (front_rtsp or back_rtsp):
                lines.append("Add a live RTSP URL from Ring-MQTT with Video Streaming. Home Assistant snapshots are too stale for doorbell AI.")
        else:
            lines.append("Likely status: Home Assistant does not expose Ring trigger entities yet.")
            lines.append("Install Mosquitto Broker and Ring-MQTT with Video Streaming. You may also install the normal Ring integration for Home Assistant trigger entities.")

        lines.extend([
            "",
            "Recommended path: install Mosquitto Broker, create an MQTT user, add the ring-mqtt repository, sign in to Ring inside ring-mqtt, enable video streaming, then return here and press Find Ring MQTT Streams.",
            "The full step-by-step guide has been opened.",
        ])
        self.status_txt.SetValue("\n".join(lines))
        open_help("ring-setup")

    def on_discover_setup_speakers(self, event):
        settings = self._settings()
        self._record_setup_event("speaker_discovery_start", "Discovering available speakers.")
        self._set_busy(True)
        self._set_setup_status(
            "Discovering available speakers. Viper will show Home Assistant media players and network Sonos speakers, then let you choose which ones to add.",
            announce=True,
        )
        safe_submit(self._run_setup_speaker_discovery, settings)

    def _run_setup_speaker_discovery(self, settings):
        ha_result = discovery.discover_ha_entities(
            ha_ip=settings.get("ha_ip") or None,
            ha_port=settings.get("ha_port") or None,
            token=settings.get("ha_token") or None,
            timeout=5,
        )
        ha_candidates = []
        ha_error = ""
        if ha_result.get("ok"):
            ha_candidates = self.parent._ha_speaker_candidates_from_result(ha_result)
        else:
            ha_error = ha_result.get("message") or "Home Assistant speaker discovery failed."

        sonos_candidates = []
        sonos_error = ""
        try:
            sonos_candidates = self.parent._sonos_speaker_candidates_from_soco(soco.discover())
        except Exception as e:
            sonos_error = f"Network Sonos discovery failed: {e}"

        wx.CallAfter(self._finish_setup_speaker_discovery, ha_candidates, sonos_candidates, ha_error, sonos_error)

    def _finish_setup_speaker_discovery(self, ha_candidates, sonos_candidates, ha_error="", sonos_error=""):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring speaker discovery result after setup dialog was destroyed.")
            return
        self._set_busy(False)
        summary = self.parent._discovered_speaker_summary_text(ha_candidates, sonos_candidates, ha_error, sonos_error)
        self.parent._show_discovered_speakers(ha_candidates, sonos_candidates, ha_error, sonos_error, parent_window=self)
        self._record_setup_event(
            "speaker_discovery_finish",
            "Speaker discovery complete.",
            ha_candidates=len(ha_candidates),
            sonos_candidates=len(sonos_candidates),
            ha_error=ha_error,
            sonos_error=sonos_error,
        )
        self._set_setup_status(summary + "\n\nSpeaker discovery complete. Choose speakers in the dialog to add them, or review the list above.", announce=True)

    def _setup_summary_text(self):
        settings = self._settings()
        speakers = self.parent.config.get("speakers", {}) if isinstance(self.parent.config.get("speakers"), dict) else {}
        return "\n".join([
            "Setup Summary",
            "",
            f"Home Assistant host: {settings.get('ha_ip') or 'missing'}:{settings.get('ha_port') or '8123'}",
            f"Home Assistant token: {'present' if settings.get('ha_token') else 'missing'}",
            f"Direct HA listener: {'enabled' if settings.get('ha_listener_enabled') else 'disabled'}",
            "",
            f"Front trigger: {settings.get('front_trigger_entity_id') or 'not selected'}",
            f"Back trigger: {settings.get('back_trigger_entity_id') or 'not selected'}",
            f"Front RTSP: {'set' if settings.get('rtsp_front') else 'missing'}",
            f"Back RTSP: {'set' if settings.get('rtsp_back') else 'missing'}",
            "",
            f"Saved speakers: {len(speakers)}",
            f"Gemini API key: {'present' if settings.get('gemini_api_key') else 'missing'}",
            "",
            "Use Discover Available Speakers to see speaker targets without adding them. Use Test Everything to verify Home Assistant and camera frames.",
        ])

    def on_show_setup_summary(self, event):
        text = self._setup_summary_text()
        self._set_setup_status(text, announce=True)
        self.parent._show_text_dialog("Setup Summary", text)

    def on_setup_test_everything(self, event):
        self._set_setup_status("Test Everything started. Results will appear on the main Setup tab.", announce=True)
        self.parent.on_test_everything(event)

    def on_test(self, event):
        settings = self._settings()
        if not settings["ha_ip"] or not settings["ha_token"]:
            self._set_setup_status("Enter the Home Assistant host and access token first. If HA_TOKEN is set in environment variables, the token box can stay blank.", announce=True)
            return
        if settings["pushover_enabled"] and (not settings["pushover_user_key"] or not settings["pushover_api_token"]):
            self._set_setup_status("Pushover is optional. Either enter both Pushover values, set PUSHOVER_USER and PUSHOVER_TOKEN in environment variables, or turn Pushover off.", announce=True)
            return

        self._set_busy(True)
        self._record_setup_event("entity_discovery_start", "Testing Home Assistant connection and discovering entities.")
        self._set_setup_status("Testing Home Assistant connection and discovering entities.", announce=True)
        safe_submit(self._run_discovery_test, settings)

    def _run_discovery_test(self, settings):
        started = time.perf_counter()
        try:
            result = discovery.discover_ha_entities(
                ha_ip=settings["ha_ip"],
                ha_port=settings["ha_port"],
                token=settings["ha_token"],
                timeout=8,
            )
            if result.get("ok"):
                result["supervisor_install_permission"] = self._check_supervisor_install_permission(settings)
        except Exception as e:
            logging.exception("[HA SETUP] Home Assistant discovery failed unexpectedly")
            result = {"ok": False, "error": "unexpected_error", "message": str(e)}
        self._record_setup_event(
            "entity_discovery_finish",
            result.get("message") or "",
            ok=bool(result.get("ok")),
            entity_count=result.get("entity_count", 0),
            elapsed=round(time.perf_counter() - started, 3),
        )
        wx.CallAfter(self._finish_discovery_test, result)

    def _finish_discovery_test(self, result):
        if getattr(self, "_destroyed", False):
            logging.info("[HA SETUP] Ignoring discovery test result after setup dialog was destroyed.")
            return
        self._set_busy(False)
        self.discovery_result = result if result.get("ok") else None
        self._devices_discovered = bool(result.get("ok"))
        self._update_setup_page_nav()
        if not result.get("ok"):
            details = [result.get("message") or "Home Assistant discovery failed."]
            if result.get("error"):
                details.append(f"Reason: {result.get('error')}.")
            if result.get("status_code"):
                details.append(f"HTTP status: {result.get('status_code')}.")
            if result.get("url"):
                details.append(f"URL: {result.get('url')}.")
            if result.get("error") == "missing_token":
                details.append("Paste a Home Assistant long-lived access token, then press Discover Devices again.")
            elif result.get("error") == "bad_token":
                details.append("Create a new long-lived access token in Home Assistant and paste the whole token.")
            self._set_setup_status("\n".join(details), announce=True)
            return
        self._populate_trigger_choices_from_config(
            self._choice_entity_id(self.front_trigger_choice),
            self._choice_entity_id(self.back_trigger_choice),
        )
        auto_result = self._auto_configure_doorbells_from_discovery()
        supervisor_permission = result.get("supervisor_install_permission") or {}
        supervisor_line = supervisor_permission.get("message") or "Ring-MQTT installer permission was not checked."

        counts = result.get("counts", {})
        lines = [
            f"Connected. Found {result.get('entity_count', 0)} entities.",
            supervisor_line,
            f"Media players: {counts.get('media_players', 0)}",
            f"Door sensors: {counts.get('door_sensors', 0)}",
            f"Cameras: {counts.get('cameras', 0)}",
            f"Fridge sensors: {counts.get('fridge_sensors', 0)}",
            f"Freezer sensors: {counts.get('freezer_sensors', 0)}",
            f"Ice maker entities: {counts.get('ice_maker_candidates', 0)}",
            f"Filter sensors: {counts.get('filter_sensors', 0)}",
            f"Vacuums: {counts.get('vacuum_entities', 0)}",
            "",
            auto_result.get("message") or "",
            "",
            "Viper saved the safe non-speaker setup it could infer. Speakers are not auto-added; use Choose Alert Speakers on the main Setup screen.",
            "Next, Viper will look for Ring-MQTT live streams and test every found RTSP URL before asking you to change anything.",
        ]
        self._set_setup_status("\n".join(lines), announce=True)
        save_settings = self._settings()
        self._apply_settings_to_parent(save_settings)
        self.parent.save_config()
        host = self._rtsp_host_from_ha_host(save_settings.get("ha_ip") or "")
        if host:
            self._set_busy(True)
            self._set_setup_status(
                "Now looking for Ring-MQTT live streams. Viper will test every found RTSP URL before filling anything.",
                announce=True,
            )
            safe_submit(self._run_find_live_rtsp_streams, host, save_settings)

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
        elif not self.back_mqtt_txt.GetValue().strip():
            assigned = "Back"
            self.back_mqtt_txt.SetValue(found["topic"])
            if found.get("camera_id"):
                self.back_camera_id_txt.SetValue(found["camera_id"])
        else:
            assigned = "Neither field was empty"
        if found.get("ring_topic_root") and not self.ring_topic_root_txt.GetValue().strip():
            self.ring_topic_root_txt.SetValue(found["ring_topic_root"])
        self._refresh_derived_doorbell_preview()
        lines = [f"Detected {len(suggestions)} Ring topic(s):"]
        for item in suggestions[:8]:
            lines.append(f"- {item['topic']} payload={item.get('payload', '')}")
        lines.append("")
        lines.append(f"Assigned to: {assigned}.")
        lines.append("Enter or confirm each RTSP URL on the Doorbells page, then use the camera test buttons.")
        self.status_txt.SetValue("\n".join(lines))

    def on_save(self, event):
        settings = self._settings()
        if not settings["ha_ip"] or not settings["ha_token"]:
            self._record_setup_event("setup_save_blocked", "Save blocked because Home Assistant host or token is missing.")
            self.status_txt.SetValue("Enter the Home Assistant host and access token before saving.")
            return
        if settings["pushover_enabled"] and (not settings["pushover_user_key"] or not settings["pushover_api_token"]):
            self._record_setup_event("setup_save_blocked", "Save blocked because Pushover is enabled but incomplete.")
            self.status_txt.SetValue("Pushover is optional. Either enter both Pushover values, set PUSHOVER_USER and PUSHOVER_TOKEN in environment variables, or turn Pushover off.")
            return
        bad_guesses = self._untrusted_rtsp_guesses(settings)
        if bad_guesses:
            self._record_setup_event("setup_save_blocked", "Save blocked because one or more RTSP URLs were untrusted.", bad_guess_count=len(bad_guesses))
            self._set_setup_status(
                "Viper did not save because one or more RTSP URLs look like untested Home Assistant camera URLs:\n"
                + "\n".join(f"- {side}: {url}" for side, url in bad_guesses)
                + "\n\nPress Find Ring MQTT Streams to get real Ring-MQTT stream names, or test each camera URL successfully before saving.",
                announce=True,
            )
            return

        self._apply_settings_to_parent(settings)
        self.parent.save_config()
        cfg.sync_globals_from_config()
        self._record_setup_event("setup_save_success", "Home Assistant setup saved.")
        if settings["gemini_api_key"]:
            self.parent.notify("Home Assistant settings saved.", priority=10)
        else:
            self.parent.notify("Home Assistant settings saved. Add Gemini later for doorbell vision and Gemini speech.", priority=10)
        self.parent.config = cfg.load_config()
        self.parent.refresh_setup_checklist()
        if getattr(self.parent, "_ha_setup_dialog", None) is self:
            self.parent._ha_setup_dialog = None
        self._destroyed = True
        wx.CallAfter(self.parent._leave_setup_window_mode)
        self.Destroy()

    def on_close_setup(self, event):
        self._destroyed = True
        self._record_setup_event("setup_close", "Home Assistant setup dialog closed without saving.")
        if getattr(self.parent, "_ha_setup_dialog", None) is self:
            self.parent._ha_setup_dialog = None
        wx.CallAfter(self.parent._leave_setup_window_mode)
        self.Destroy()

    def _untrusted_rtsp_guesses(self, settings):
        derived = self._derived_doorbell_values()
        bad = []
        for side, key, camera_key in (
            ("front", "rtsp_front", "front_camera_id"),
            ("back", "rtsp_back", "back_camera_id"),
        ):
            url = (settings.get(key) or "").strip()
            if not url:
                continue
            if url in self._verified_rtsp_urls or url in self._trusted_rtsp_urls:
                continue
            camera_id = (settings.get(camera_key) or "").lower()
            looks_like_ha_camera_guess = bool(camera_id and url == derived.get(key) and ("live_view" in camera_id or "snapshot" in camera_id))
            if looks_like_ha_camera_guess:
                bad.append((side, url))
        return bad

    def _apply_settings_to_parent(self, settings):
        self.parent.config["ha_ip"] = settings["ha_ip"]
        self.parent.config["ha_port"] = settings["ha_port"]
        typed_ha_token = self.ha_token_txt.GetValue().strip()
        typed_gemini_key = self.gemini_key_txt.GetValue().strip()
        typed_pushover_user = self.pushover_user_txt.GetValue().strip()
        typed_pushover_token = self.pushover_token_txt.GetValue().strip()
        typed_mqtt_password = self.mqtt_password_txt.GetValue().strip()
        if typed_ha_token:
            self.parent.config["ha_token"] = typed_ha_token
        elif not cfg.get_ha_settings(self.parent.config, include_env=True).get("ha_token"):
            self.parent.config["ha_token"] = ""
        if typed_gemini_key:
            self.parent.config["gemini_api_key"] = typed_gemini_key
        elif not cfg.get_api_settings(self.parent.config, include_env=True).get("gemini_api_key"):
            self.parent.config["gemini_api_key"] = ""
        self.parent.config["pushover_enabled"] = settings["pushover_enabled"]
        self.parent.config["pushover_user_key"] = typed_pushover_user if settings["pushover_enabled"] and typed_pushover_user else ""
        self.parent.config["pushover_api_token"] = typed_pushover_token if settings["pushover_enabled"] and typed_pushover_token else ""
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
        self.parent.config["mqtt_password"] = typed_mqtt_password
        self.parent.config["show_advanced_ring_mqtt"] = settings.get("show_advanced_ring_mqtt", False)
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


class ViperSetupWizardDialog(wx.Dialog):
    PAGES = [
        {
            "title": "Welcome",
            "body": (
                "This wizard sets up the core Viper doorbell system first. "
                "The goal is simple: connect Home Assistant, expose Ring doorbells, get live Ring-MQTT video, choose speakers, then run one final test.\n\n"
                "Refrigerator alerts and robot vacuum controls are optional follow-up setup areas after the doorbell system works."
            ),
            "primary": "Start Setup",
            "action": "start",
        },
        {
            "title": "Home Assistant Connection",
            "body": (
                "Step 1: make sure Home Assistant exists and Viper can talk to it.\n\n"
                "This page handles the whole Home Assistant path: find an existing server, install VirtualBox if needed, install Home Assistant OS if needed, wait for Home Assistant Core, open Home Assistant account setup in your browser, then test your long-lived access token."
            ),
            "primary": "Set Up Or Verify Home Assistant",
            "action": "ha_connect",
        },
        {
            "title": "Ring In Home Assistant",
            "body": (
                "Step 2: make sure Ring devices are visible inside Home Assistant.\n\n"
                "This gives Viper doorbell trigger entities, such as a ding or motion sensor. "
                "Viper will use those triggers to know when to start the doorbell alert."
            ),
            "primary": "Open Ring Integration In Browser",
            "action": "ring_integration",
        },
        {
            "title": "Ring-MQTT Live Video",
            "body": (
                "Step 3: install or check Mosquitto and Ring-MQTT with Video Streaming.\n\n"
                "Home Assistant's normal Ring camera snapshots are not live enough for fast doorbell AI. "
                "Ring-MQTT gives Viper live RTSP streams. Viper can install the apps, expose RTSP port 8554, and open the accessible Ring-MQTT login guide."
            ),
            "primary": "Install Or Open Ring-MQTT",
            "action": "ring_mqtt",
        },
        {
            "title": "Test Doorbell Cameras",
            "body": (
                "Step 4: make sure Viper can see your doorbell cameras.\n\n"
                "Viper should not guess stream URLs from Home Assistant camera names. "
                "This step reads Ring-MQTT logs and topics, tests each real live video stream, lets you choose which stream is front or back, saves that choice, and lets you re-test either door on this same page."
            ),
            "primary": "Find And Test Doorbell Cameras",
            "action": "live_streams",
        },
        {
            "title": "Confirm Doorbell Triggers",
            "body": (
                "Step 5: choose which Home Assistant entities mean front doorbell and back doorbell.\n\n"
                "Most users want the Ring ding entities. If Viper already picked the right triggers, just continue. If it picked the wrong trigger, change it here."
            ),
            "primary": "Check Doorbell Triggers",
            "action": "doorbells",
        },
        {
            "title": "Speakers And Audio",
            "body": (
                "Step 6: choose where Viper should speak or play chimes.\n\n"
                "Viper can discover Home Assistant media players and Sonos speakers. "
                "New speakers are not checked automatically. Tab through the speaker checkboxes, press Space to choose the speakers you want, save them, then use Test Checked Speakers on this same page."
            ),
            "primary": "Discover Available Speakers",
            "action": "speakers_voice",
        },
        {
            "title": "AI And Speech",
            "body": (
                "Step 7: set the voice and AI defaults.\n\n"
                "Gemini is used for doorbell image descriptions and can also be used for speech. "
                "Keep this simple at first; detailed per-category voice settings live in Speakers and Audio."
            ),
            "primary": "Open AI And Speech Settings",
            "action": "tts",
        },
        {
            "title": "Final Test",
            "body": (
                "Step 8: run a safe system test.\n\n"
                "Viper checks Home Assistant, listener status, live camera frames, speaker routing, Gemini setup, and diagnostics readiness."
            ),
            "primary": "Test Everything",
            "action": "test",
        },
        {
            "title": "Finish And Optional Devices",
            "body": (
                "Core setup is complete when Home Assistant, doorbell triggers, live camera streams, speakers, and AI/speech are working.\n\n"
                "Refrigerator alerts and robot vacuum controls are optional. You can set them up now or later from Home Devices."
            ),
            "primary": "Open Main Viper Dashboard",
            "action": "finish",
        },
    ]

    def __init__(self, parent=None, owner=None):
        super().__init__(None, title="Viper Vision Setup Wizard", size=(820, 620))
        self.parent = owner or parent
        self.page_index = 0
        self._initial_focus_given = False
        self._session_completed_actions = set()
        self._ring_integration_opened = False
        self._ring_mqtt_opened = False
        self._trusted_rtsp_urls = set()
        self._verified_rtsp_urls = set()
        self._wizard_speaker_checks = []
        self._wizard_speaker_targets = []
        self._wizard_doorbell_trigger_candidates = []
        self._wizard_doorbell_trigger_choices = []
        self._wizard_stream_test_results = []
        self._wizard_stream_choices = []
        self._wizard_saved_stream_urls = set()
        self._wizard_camera_test_status = {}
        self._wizard_progress_lines = []
        self._wizard_progress_state = _coerce_setup_progress_state(self.parent.config.get("setup_progress", {}))
        self._last_focus_control_log = {}
        initial_action = getattr(self.parent, "_requested_setup_page", "") or getattr(self.parent, "suggested_setup_page", lambda: "start")()
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.title_txt = wx.StaticText(panel, label="")
        title_font = self.title_txt.GetFont()
        title_font.SetPointSize(13)
        title_font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.title_txt.SetFont(title_font)
        self.title_txt.SetName("Setup wizard page title")
        sizer.Add(self.title_txt, 0, wx.ALL | wx.EXPAND, 10)

        self.step_status_txt = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
        self.step_status_txt.SetName("Current setup step status")
        self.step_status_txt.SetToolTip("Read-only status for the current setup step.")
        sizer.Add(self.step_status_txt, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.instructions_txt = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 190))
        self.instructions_txt.SetName("Setup wizard instructions")
        self.instructions_txt.SetToolTip("Read-only instructions for the current setup step.")
        sizer.Add(self.instructions_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.checklist_txt = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 210))
        self.checklist_txt.SetName("Current setup checklist")
        self.checklist_txt.SetToolTip("Read-only setup checklist status.")
        sizer.Add(self.checklist_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.ha_panel = wx.Panel(panel)
        ha_sizer = wx.BoxSizer(wx.VERTICAL)

        def add_ha_text_row(label, control):
            row = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(self.ha_panel, label=label)
            lbl.SetName(label)
            row.Add(lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            control.SetName(label)
            control.SetToolTip(label)
            row.Add(control, 1, wx.ALL | wx.EXPAND, 5)
            ha_sizer.Add(row, 0, wx.EXPAND)

        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        self.wizard_ha_host_txt = wx.TextCtrl(self.ha_panel, value=str(ha_settings.get("ha_ip") or ""))
        self.wizard_ha_port_txt = wx.TextCtrl(self.ha_panel, value=str(ha_settings.get("ha_port") or "8123"))
        self.wizard_ha_token_txt = wx.TextCtrl(self.ha_panel, style=wx.TE_PASSWORD)
        if not self.parent.config.get("ha_token") and ha_settings.get("ha_token"):
            self.wizard_ha_token_txt.SetToolTip("Home Assistant token is already available from environment variables or Windows Credential Manager. You can leave this box blank.")
        elif self.parent.config.get("ha_token"):
            self.wizard_ha_token_txt.SetToolTip("Home Assistant token is already saved. You can leave this box blank unless you want to replace it.")
        add_ha_text_row("Home Assistant IP or host", self.wizard_ha_host_txt)
        add_ha_text_row("Home Assistant port", self.wizard_ha_port_txt)
        add_ha_text_row("Home Assistant long-lived access token", self.wizard_ha_token_txt)
        self.btn_find_ha_wizard = wx.Button(self.ha_panel, label="Find Home Assistant")
        self.btn_find_ha_wizard.SetName("Find Home Assistant")
        self.btn_find_ha_wizard.SetToolTip("Search common local network addresses for Home Assistant and fill the address field.")
        self.btn_find_ha_wizard.Bind(wx.EVT_BUTTON, self.on_find_home_assistant)
        ha_sizer.Add(self.btn_find_ha_wizard, 0, wx.ALL | wx.EXPAND, 5)

        ha_buttons = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        ha_buttons.AddGrowableCol(0, 1)
        ha_buttons.AddGrowableCol(1, 1)
        self.btn_wizard_check_pc = wx.Button(self.ha_panel, label="Check This PC And Home Assistant")
        self.btn_wizard_install_vbox = wx.Button(self.ha_panel, label="Install VirtualBox")
        self.btn_wizard_optimize_windows = wx.Button(self.ha_panel, label="Optimize Windows For VirtualBox")
        self.btn_wizard_install_ha = wx.Button(self.ha_panel, label="Install Home Assistant")
        self.btn_wizard_start_ha = wx.Button(self.ha_panel, label="Start Or Wait For Home Assistant")
        self.btn_wizard_open_ha = wx.Button(self.ha_panel, label="Open Home Assistant Account Setup")
        self.btn_wizard_open_token = wx.Button(self.ha_panel, label="Open Home Assistant Token Page")
        for button in (
            self.btn_wizard_check_pc,
            self.btn_wizard_install_vbox,
            self.btn_wizard_optimize_windows,
            self.btn_wizard_install_ha,
            self.btn_wizard_start_ha,
            self.btn_wizard_open_ha,
            self.btn_wizard_open_token,
        ):
            button.SetName(button.GetLabel())
            button.SetToolTip(button.GetLabel())
            ha_buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_wizard_check_pc.Bind(wx.EVT_BUTTON, self.on_wizard_check_pc)
        self.btn_wizard_install_vbox.Bind(wx.EVT_BUTTON, self.on_wizard_install_virtualbox)
        self.btn_wizard_optimize_windows.Bind(wx.EVT_BUTTON, self.on_wizard_optimize_windows_virtualbox)
        self.btn_wizard_install_ha.Bind(wx.EVT_BUTTON, self.on_wizard_install_home_assistant_vm)
        self.btn_wizard_start_ha.Bind(wx.EVT_BUTTON, self.on_wizard_start_home_assistant_vm)
        self.btn_wizard_open_ha.Bind(wx.EVT_BUTTON, self.on_wizard_open_home_assistant)
        self.btn_wizard_open_token.Bind(wx.EVT_BUTTON, self.on_wizard_open_token_page)
        ha_sizer.Add(ha_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)

        self.ha_panel.SetSizer(ha_sizer)
        sizer.Add(self.ha_panel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.doorbell_trigger_panel = wx.Panel(panel)
        doorbell_sizer = wx.BoxSizer(wx.VERTICAL)

        def add_doorbell_choice_row(label, control):
            row = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(self.doorbell_trigger_panel, label=label)
            lbl.SetName(label)
            row.Add(lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            control.SetName(label)
            control.SetToolTip(label)
            row.Add(control, 1, wx.ALL | wx.EXPAND, 5)
            doorbell_sizer.Add(row, 0, wx.EXPAND)

        self.wizard_front_trigger_choice = wx.ComboBox(self.doorbell_trigger_panel, style=wx.CB_READONLY)
        self.wizard_back_trigger_choice = wx.ComboBox(self.doorbell_trigger_panel, style=wx.CB_READONLY)
        add_doorbell_choice_row("Front door trigger entity", self.wizard_front_trigger_choice)
        add_doorbell_choice_row("Back door trigger entity", self.wizard_back_trigger_choice)
        self.btn_save_wizard_triggers = wx.Button(self.doorbell_trigger_panel, label="Save Selected Doorbell Triggers")
        self.btn_save_wizard_triggers.SetName("Save Selected Doorbell Triggers")
        self.btn_save_wizard_triggers.SetToolTip("Save the selected Home Assistant trigger entities for the front and back doorbells.")
        self.btn_save_wizard_triggers.Bind(wx.EVT_BUTTON, self.on_save_wizard_doorbell_triggers)
        doorbell_sizer.Add(self.btn_save_wizard_triggers, 0, wx.ALL | wx.EXPAND, 5)
        self.doorbell_trigger_panel.SetSizer(doorbell_sizer)
        sizer.Add(self.doorbell_trigger_panel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.camera_stream_panel = wx.Panel(panel)
        camera_stream_sizer = wx.BoxSizer(wx.VERTICAL)

        def add_camera_stream_choice_row(label, control):
            row = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(self.camera_stream_panel, label=label)
            lbl.SetName(label)
            row.Add(lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            control.SetName(label)
            control.SetToolTip(label)
            row.Add(control, 1, wx.ALL | wx.EXPAND, 5)
            camera_stream_sizer.Add(row, 0, wx.EXPAND)

        self.wizard_front_stream_choice = wx.ComboBox(self.camera_stream_panel, style=wx.CB_READONLY)
        self.wizard_back_stream_choice = wx.ComboBox(self.camera_stream_panel, style=wx.CB_READONLY)
        add_camera_stream_choice_row("Front door camera stream", self.wizard_front_stream_choice)
        add_camera_stream_choice_row("Back door camera stream", self.wizard_back_stream_choice)
        self.btn_save_wizard_streams = wx.Button(self.camera_stream_panel, label="Save Selected Camera Streams")
        self.btn_save_wizard_streams.SetName("Save Selected Camera Streams")
        self.btn_save_wizard_streams.SetToolTip("Save the selected tested Ring-MQTT live streams for the front and back doorbells.")
        self.btn_save_wizard_streams.Bind(wx.EVT_BUTTON, self.on_save_wizard_camera_streams)
        camera_stream_sizer.Add(self.btn_save_wizard_streams, 0, wx.ALL | wx.EXPAND, 5)
        camera_test_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        camera_test_grid.AddGrowableCol(0, 1)
        camera_test_grid.AddGrowableCol(1, 1)
        self.btn_test_wizard_front_camera = wx.Button(self.camera_stream_panel, label="Test Front Doorbell Camera")
        self.btn_test_wizard_back_camera = wx.Button(self.camera_stream_panel, label="Test Back Doorbell Camera")
        for button in (self.btn_test_wizard_front_camera, self.btn_test_wizard_back_camera):
            button.SetName(button.GetLabel())
            button.SetToolTip(button.GetLabel())
            camera_test_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_test_wizard_front_camera.Bind(wx.EVT_BUTTON, lambda event: self.on_test_wizard_camera(event, "front"))
        self.btn_test_wizard_back_camera.Bind(wx.EVT_BUTTON, lambda event: self.on_test_wizard_camera(event, "back"))
        camera_stream_sizer.Add(camera_test_grid, 0, wx.EXPAND)
        self.camera_stream_panel.SetSizer(camera_stream_sizer)
        sizer.Add(self.camera_stream_panel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.speaker_panel = wx.Panel(panel)
        speaker_sizer = wx.BoxSizer(wx.VERTICAL)
        self.speaker_scroll = wx.ScrolledWindow(self.speaker_panel, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
        self.speaker_scroll.SetScrollRate(0, 20)
        self.speaker_scroll_sizer = wx.BoxSizer(wx.VERTICAL)
        self.speaker_scroll.SetSizer(self.speaker_scroll_sizer)
        speaker_sizer.Add(self.speaker_scroll, 1, wx.ALL | wx.EXPAND, 5)
        route_box = wx.StaticBox(self.speaker_panel, label="Routes For Selected Speakers")
        route_sizer = wx.StaticBoxSizer(route_box, wx.VERTICAL)
        self.wizard_route_doorbell_chk = wx.CheckBox(self.speaker_panel, label="Use selected speakers for doorbell alerts")
        self.wizard_route_utilities_chk = wx.CheckBox(self.speaker_panel, label="Use selected speakers for utility announcements")
        self.wizard_route_fridge_chk = wx.CheckBox(self.speaker_panel, label="Use selected speakers for fridge and freezer alerts")
        self.wizard_route_quiet_exempt_chk = wx.CheckBox(self.speaker_panel, label="Allow selected speakers during quiet hours")
        for chk in (self.wizard_route_doorbell_chk, self.wizard_route_utilities_chk, self.wizard_route_fridge_chk):
            chk.SetValue(True)
        for chk in (self.wizard_route_doorbell_chk, self.wizard_route_utilities_chk, self.wizard_route_fridge_chk, self.wizard_route_quiet_exempt_chk):
            chk.SetName(chk.GetLabel())
            chk.SetToolTip(chk.GetLabel())
            route_sizer.Add(chk, 0, wx.ALL | wx.EXPAND, 4)
        speaker_sizer.Add(route_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        self.btn_save_wizard_speakers = wx.Button(self.speaker_panel, label="Save Selected Speakers")
        self.btn_save_wizard_speakers.SetName("Save Selected Speakers")
        self.btn_save_wizard_speakers.SetToolTip("Save the checked speaker targets and selected routes.")
        self.btn_save_wizard_speakers.Bind(wx.EVT_BUTTON, self.on_save_wizard_speakers)
        speaker_sizer.Add(self.btn_save_wizard_speakers, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_test_wizard_speakers = wx.Button(self.speaker_panel, label="Test Checked Speakers")
        self.btn_test_wizard_speakers.SetName("Test Checked Speakers")
        self.btn_test_wizard_speakers.SetToolTip("Play a short test announcement on the checked speaker targets, or on saved speakers if none are checked.")
        self.btn_test_wizard_speakers.Bind(wx.EVT_BUTTON, self.on_test_wizard_speakers)
        speaker_sizer.Add(self.btn_test_wizard_speakers, 0, wx.ALL | wx.EXPAND, 5)
        self.speaker_panel.SetSizer(speaker_sizer)
        sizer.Add(self.speaker_panel, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        buttons = wx.FlexGridSizer(rows=0, cols=3, vgap=6, hgap=6)
        for col in range(3):
            buttons.AddGrowableCol(col, 1)
        self.btn_back = wx.Button(panel, label="Back")
        self.btn_action = wx.Button(panel, label="Start Setup")
        self.btn_next = wx.Button(panel, label="Next")
        self.btn_refresh = wx.Button(panel, label="Refresh Checklist")
        self.btn_install_ha_wizard = wx.Button(panel, label="Home Assistant Install Is In This Wizard")
        self.btn_optional_fridge = wx.Button(panel, label="Set Up Refrigerator Alerts")
        self.btn_optional_vacuum = wx.Button(panel, label="Set Up Robot Vacuum")
        self.btn_close = wx.Button(panel, label="Close")
        for btn in (self.btn_back, self.btn_action, self.btn_next, self.btn_refresh, self.btn_install_ha_wizard, self.btn_optional_fridge, self.btn_optional_vacuum, self.btn_close):
            btn.SetName(btn.GetLabel())
            btn.SetToolTip(btn.GetLabel())
            try:
                btn.Bind(wx.EVT_SET_FOCUS, self._on_control_focus_for_diagnostics)
            except Exception:
                pass
            buttons.Add(btn, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_back.Bind(wx.EVT_BUTTON, self.on_back)
        self.btn_action.Bind(wx.EVT_BUTTON, self.on_action)
        self.btn_next.Bind(wx.EVT_BUTTON, self.on_next)
        self.btn_refresh.Bind(wx.EVT_BUTTON, self.on_refresh)
        self.btn_install_ha_wizard.Bind(wx.EVT_BUTTON, self.on_install_home_assistant)
        self.btn_optional_fridge.Bind(wx.EVT_BUTTON, self.on_optional_fridge)
        self.btn_optional_vacuum.Bind(wx.EVT_BUTTON, self.on_optional_vacuum)
        self.btn_close.Bind(wx.EVT_BUTTON, self.on_close)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_help_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)
        self._apply_initial_resume_position(initial_action)
        self._render()
        wx.CallAfter(self.force_initial_focus)
        wx.CallLater(150, self.force_initial_focus)
        wx.CallLater(500, self.force_initial_focus)

    def _apply_initial_resume_position(self, action):
        target_action = {
            "connect": "ha_connect",
            "doorbells": "doorbells",
            "live_streams": "live_streams",
            "speakers": "speakers_voice",
            "test": "test",
            "finish": "test",
        }.get(action, action or "start")
        for index, page in enumerate(self.PAGES):
            if page.get("action") == target_action:
                self.page_index = index
                return

    def go_to_setup_action(self, action):
        self._apply_initial_resume_position(action)
        self._render()
        self._initial_focus_given = False
        wx.CallAfter(self.force_initial_focus)

    def force_initial_focus(self):
        try:
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
            self._nudge_dialog_foreground()
            if self._initial_focus_given:
                return
            self._initial_focus_given = True
            focus_target = self._current_page_focus_target() or self.btn_action
            if hasattr(focus_target, "SetFocusFromKbd"):
                try:
                    focus_target.SetFocusFromKbd()
                    return
                except Exception:
                    pass
            focus_target.SetFocus()
        except Exception:
            logging.debug("Could not force setup wizard focus.", exc_info=True)

    def _current_page_focus_target(self):
        action = self.PAGES[self.page_index].get("action")
        return {
            "ha_connect": getattr(self, "wizard_ha_token_txt", None),
            "doorbells": getattr(self, "wizard_front_trigger_choice", None),
            "live_streams": getattr(self, "wizard_front_stream_choice", None),
            "speakers_voice": getattr(self, "speaker_scroll", None),
            "test": getattr(self, "btn_action", None),
        }.get(action, getattr(self, "btn_action", None))

    def _on_control_focus_for_diagnostics(self, event):
        control = event.GetEventObject()
        try:
            label = control.GetLabel() if hasattr(control, "GetLabel") else ""
            key = f"{control.__class__.__name__}:{control.GetName() if hasattr(control, 'GetName') else ''}:{label}"
            now = time.monotonic()
            last = self._last_focus_control_log.get(key, 0)
            if now - last < 10:
                event.Skip()
                return
            self._last_focus_control_log[key] = now
            logging.info(
                "[FOCUS] Setup wizard focus class=%s name=%r label=%r shown=%s enabled=%s can_focus=%s",
                control.__class__.__name__,
                control.GetName() if hasattr(control, "GetName") else "",
                label,
                control.IsShownOnScreen() if hasattr(control, "IsShownOnScreen") else None,
                control.IsEnabled() if hasattr(control, "IsEnabled") else None,
                control.CanAcceptFocusFromKeyboard() if hasattr(control, "CanAcceptFocusFromKeyboard") else None,
            )
        except Exception:
            logging.debug("Could not log setup wizard focus target.", exc_info=True)
        event.Skip()

    def _nudge_dialog_foreground(self):
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
            logging.debug("Could not nudge setup wizard to Windows foreground.", exc_info=True)

    def on_activate(self, event):
        try:
            if event.GetActive():
                self._initial_focus_given = False
                wx.CallAfter(self.force_initial_focus)
                wx.CallLater(150, self.force_initial_focus)
        except Exception:
            logging.debug("Could not restore setup wizard focus on activation.", exc_info=True)
        if not event.GetActive():
            event.Skip()

    def on_help_key(self, event):
        if event.GetKeyCode() == wx.WXK_F1:
            open_help("setup")
            return
        event.Skip()

    def _render(self):
        page = self.PAGES[self.page_index]
        self.title_txt.SetLabel(f"Step {self.page_index + 1} of {len(self.PAGES)}: {page['title']}")
        complete, status = self._page_completion_status(page)
        self.step_status_txt.SetValue(status)
        self.instructions_txt.SetValue(page["body"])
        primary = self._primary_label_for_page(page, complete)
        self.btn_action.SetLabel(primary)
        self.btn_action.SetName(primary)
        self.btn_action.SetToolTip(primary)
        try:
            accessible = self.btn_action.GetOrCreateAccessible()
            if accessible:
                accessible.SetName(primary)
                accessible.SetDescription(primary)
        except Exception:
            pass
        self.btn_back.Enable(self.page_index > 0)
        next_available = complete and self.page_index < len(self.PAGES) - 1 and page["action"] != "start"
        if next_available:
            next_title = self.PAGES[self.page_index + 1]["title"]
            next_label = f"Continue To {next_title}"
            self.btn_next.SetLabel(next_label)
            self.btn_next.SetName(next_label)
            self.btn_next.SetToolTip(next_label)
        self.btn_next.Show(next_available)
        self.btn_next.Enable(next_available)
        self.btn_install_ha_wizard.Show(False)
        self.ha_panel.Show(page.get("action") == "ha_connect")
        self.doorbell_trigger_panel.Show(page.get("action") == "doorbells")
        if page.get("action") == "doorbells":
            self._refresh_wizard_doorbell_trigger_controls()
        self.camera_stream_panel.Show(page.get("action") == "live_streams")
        if page.get("action") == "live_streams":
            self._refresh_wizard_camera_stream_controls()
        show_optional = page["action"] == "finish"
        self.btn_optional_fridge.Show(show_optional)
        self.btn_optional_vacuum.Show(show_optional)
        self.speaker_panel.Show(page.get("action") == "speakers_voice")
        has_speaker_choices = bool(getattr(self, "_wizard_speaker_checks", []))
        self.btn_save_wizard_speakers.Show(page.get("action") == "speakers_voice" and has_speaker_choices)
        self.checklist_txt.SetValue(self._page_status_summary(page, complete, status))
        try:
            self.Layout()
            self.FitInside() if hasattr(self, "FitInside") else None
        except Exception:
            pass

    def _page_status_summary(self, page, complete, status):
        title = page.get("title", "Setup")
        ready = "Passed" if complete else "Needs setup"
        if page.get("action") == "start":
            ready = "Ready"
        extra = ""
        if page.get("action") == "test" and self._core_setup_ready():
            extra = "\n\nResume from here: core setup already looks complete. Run Test Everything for a fresh PASS/FIX report, then continue to optional devices."
        elif page.get("action") == "finish":
            extra = "\n\nSetup confidence:\n" + self.parent.build_setup_confidence_summary()
        return (
            f"{title}: {ready}.\n"
            f"{status}\n\n"
            "Overall setup checklist:\n"
            f"{self.parent.build_setup_checklist_summary()}"
            f"{extra}"
        )

    def _primary_label_for_page(self, page, complete):
        action = page.get("action")
        if action == "ring_integration":
            if self._ring_integration_opened and not complete:
                return "Check For Ring Doorbell Triggers"
            if complete:
                return "Open Ring Integration Again"
        if action == "ha_connect":
            return "Connect And Discover Devices"
        if action == "ring_mqtt":
            if complete:
                return "Open Ring-MQTT Again"
        if action == "live_streams" and complete:
            return "Find Or Re-Test Doorbell Cameras"
        if action == "doorbells" and complete:
            return "Check Doorbell Triggers Again"
        if action == "speakers_voice" and complete:
            return "Choose Or Add More Speakers"
        if action == "tts" and complete:
            return "Review AI And Speech Settings"
        if action == "test" and complete:
            return "Run Test Everything Again"
        return page["primary"]

    def _page_completion_status(self, page):
        action = page.get("action")
        if action == "start":
            return True, "Press Start Setup to begin. Viper will show only the next useful step after each step is ready."
        if action == "ha_connect":
            if self._home_assistant_ready():
                return True, "Home Assistant host and token are available. Continue to Ring Integration Login."
            return False, "Home Assistant is not ready yet. Enter or find the Home Assistant address, paste your long-lived token if it is not already saved, then press Connect And Discover Devices. If Viper cannot find Home Assistant, use the Home Assistant buttons on this page: Check This PC, Install VirtualBox, Install Home Assistant, then Start Or Wait For Home Assistant."
        if action == "ring_integration":
            trigger_count = self._configured_doorbell_trigger_count()
            if trigger_count:
                return True, f"Ring trigger setup looks ready. Viper has {trigger_count} doorbell trigger entity or entities saved."
            if self._ring_integration_opened:
                return False, "After logging into Ring in Home Assistant, press Check For Ring Doorbell Triggers. Viper needs at least one ding, button, or motion trigger entity before continuing."
            return False, "Ring integration is not verified yet. This step opens Home Assistant so you can log into the normal Ring integration for doorbell triggers."
        if action == "ring_mqtt":
            if self._has_any_live_rtsp_url():
                return True, "At least one live RTSP URL is already saved. Continue to Test Doorbell Cameras so Viper can verify it on this page."
            if "ring_mqtt" in self._session_completed_actions:
                return True, "Ring-MQTT setup was opened in this session. Continue to Test Doorbell Cameras to find and test live video."
            return False, "Ring-MQTT live video is not verified yet. Press Install Or Open Ring-MQTT, finish Ring-MQTT login if needed, then continue."
        if action == "live_streams":
            if self._has_any_live_rtsp_url():
                return True, self._saved_camera_stream_status()
            return False, "No working doorbell camera is saved yet. Press Find And Test Doorbell Cameras. Viper reads Ring-MQTT logs and topics, tests real video frames, then lets you choose front and back streams on this same page."
        if action == "doorbells":
            trigger_count = self._configured_doorbell_trigger_count()
            if trigger_count:
                return True, f"Doorbell triggers look ready. Viper has {trigger_count} saved trigger entity or entities."
            return False, "No doorbell trigger is saved yet. Choose the Home Assistant ding or motion entity for each door you use."
        if action == "speakers_voice":
            if self._has_required_speaker_routes():
                return True, "Speaker routes are saved for doorbell, utility, and fridge or freezer alerts. Continue to AI And Speech."
            return False, "Speaker routes are not ready yet. Discover speakers, choose at least one speaker, and keep doorbell, utility, and fridge or freezer routing enabled."
        if action == "tts":
            if self._gemini_key_ready():
                return True, "Gemini key is available. Continue to Final Test."
            return False, "Gemini API key is missing. Add it before testing doorbell AI descriptions."
        if action == "test":
            if "test" in self._session_completed_actions:
                return True, "Test Everything was started in this setup session. Continue to Finish And Optional Devices when the results look good."
            if self._core_setup_ready():
                return False, "Core setup already looks complete. Run Test Everything to get a fresh PASS/FIX readiness report before finishing."
            return False, "Run Test Everything before finishing setup."
        if action == "finish":
            return True, "Core setup is ready to finish. Refrigerator and robot vacuum setup are optional."
        return False, "Complete this step before continuing."

    def _core_setup_ready(self):
        return (
            self._home_assistant_ready()
            and self._configured_doorbell_trigger_count() > 0
            and self._has_any_live_rtsp_url()
            and self._has_required_speaker_routes()
            and self._gemini_key_ready()
        )

    def _home_assistant_ready(self):
        if "ha_connect" in self._session_completed_actions:
            return True
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        return bool(ha_settings.get("ha_ip") and ha_settings.get("ha_token"))

    def _configured_doorbell_trigger_count(self):
        triggers = self.parent.config.get("doorbell_triggers", {})
        if not isinstance(triggers, dict):
            return 0
        count = 0
        for key in ("front", "back"):
            item = triggers.get(key, {})
            if isinstance(item, dict) and item.get("trigger_entity_id"):
                count += 1
        return count

    def _has_any_live_rtsp_url(self):
        triggers = self.parent.config.get("doorbell_triggers", {})
        trigger_urls = []
        if isinstance(triggers, dict):
            for key in ("front", "back"):
                item = triggers.get(key, {})
                if isinstance(item, dict):
                    trigger_urls.append(item.get("rtsp_url"))
        return bool(self.parent.config.get("rtsp_front") or self.parent.config.get("rtsp_back") or any(trigger_urls))

    def _configured_stream_url(self, side):
        triggers = self.parent.config.get("doorbell_triggers", {})
        trigger = triggers.get(side, {}) if isinstance(triggers, dict) and isinstance(triggers.get(side), dict) else {}
        return str(
            trigger.get("rtsp_url")
            or self.parent.config.get("rtsp_front" if side == "front" else "rtsp_back")
            or ""
        ).strip()

    def _saved_camera_stream_status(self):
        lines = ["Doorbell camera stream setup is saved."]
        saved_count = 0
        for side in ("front", "back"):
            url = self._configured_stream_url(side)
            label = side.title()
            if not url:
                lines.append(f"{label}: not configured.")
                continue
            saved_count += 1
            stream_name = self._stream_name_from_rtsp_url(url) or url
            test_status = self._wizard_camera_test_status.get(side, {})
            if test_status.get("ok"):
                lines.append(f"{label}: saved and tested successfully. Stream: {stream_name}.")
            elif url in self._wizard_saved_stream_urls:
                lines.append(f"{label}: saved from a stream that already passed testing. Stream: {stream_name}.")
            elif test_status:
                lines.append(f"{label}: saved but the most recent test failed. Stream: {stream_name}.")
            else:
                lines.append(f"{label}: saved. Press Test {label} Doorbell Camera on this page if you want to verify it again.")
        if saved_count == 1:
            lines.append("One camera is enough for a one-door setup. Continue to Confirm Doorbell Triggers, or add another stream if needed.")
        else:
            lines.append("Continue to Confirm Doorbell Triggers, or re-test cameras here if needed.")
        return " ".join(lines)

    def _has_enabled_speaker(self):
        speakers = self.parent.config.get("speakers", {})
        return any(isinstance(data, dict) and data.get("enabled", True) for data in speakers.values())

    def _has_required_speaker_routes(self):
        speakers = self.parent.config.get("speakers", {})
        enabled = [
            data for data in speakers.values()
            if isinstance(data, dict) and data.get("enabled", True)
        ]
        if not enabled:
            return False
        return (
            any(data.get("doorbell", False) for data in enabled)
            and any(data.get("utilities", False) for data in enabled)
            and any(data.get("fridge", False) for data in enabled)
        )

    def _gemini_key_ready(self):
        api_settings = cfg.get_api_settings(self.parent.config, include_env=True)
        return bool(api_settings.get("gemini_api_key"))

    def on_back(self, event):
        self.page_index = max(0, self.page_index - 1)
        self._render()

    def on_next(self, event):
        complete, status = self._page_completion_status(self.PAGES[self.page_index])
        if not complete:
            self.checklist_txt.SetValue(status)
            return
        self.page_index = min(len(self.PAGES) - 1, self.page_index + 1)
        self._render()

    def on_refresh(self, event):
        self._render()

    def on_action(self, event):
        action = self.PAGES[self.page_index]["action"]
        if action == "start":
            self._session_completed_actions.add("start")
            self.on_next(event)
            return
        elif action == "ha_connect":
            self._start_direct_home_assistant_setup()
            return
        elif action == "ring_integration":
            if not self._require_home_assistant_ready("Ring setup needs Home Assistant host and token first. Complete the Home Assistant Connection step, then return here."):
                return
            if self._ring_integration_opened and not self._configured_doorbell_trigger_count():
                self._start_ring_trigger_check()
                return
            self._ring_integration_opened = True
            if self._open_home_assistant_path("/config/integrations/integration/ring"):
                if self._configured_doorbell_trigger_count():
                    self._session_completed_actions.add("ring_integration")
                    self.checklist_txt.SetValue("Ring integration page opened. Viper already has Ring doorbell trigger entities saved, so Continue To Ring-MQTT Live Video is now available.")
                else:
                    self.checklist_txt.SetValue(
                        "Opened the Ring integration page in your browser.\n\n"
                        "Sign into Home Assistant if asked. Add or log into the normal Ring integration. "
                        "When Ring doorbell entities appear in Home Assistant, return here and press Check For Ring Doorbell Triggers."
                    )
            else:
                self.checklist_txt.SetValue("Viper could not open the Ring integration page because the Home Assistant address is missing.")
            self._render()
            return
        elif action == "ring_mqtt":
            if not self._require_home_assistant_ready("Ring-MQTT setup needs Home Assistant host and token first. Go to the Home Assistant page, enter those values, then return here."):
                return
            self._ring_mqtt_opened = True
            self._start_wizard_ring_mqtt_setup()
            return
        elif action == "live_streams":
            if not self._require_home_assistant_ready("Live stream discovery needs Home Assistant host and token first. Complete the Home Assistant Connection step, then return here."):
                return
            self._start_wizard_live_stream_discovery()
            return
        elif action == "doorbells":
            if not self._require_home_assistant_ready("Doorbell setup needs Home Assistant host and token first. Go to the Home Assistant page, enter those values, then return here."):
                return
            self._start_ring_trigger_check()
            return
        elif action == "speakers_voice":
            if not self._require_home_assistant_ready("Speaker discovery needs Home Assistant host and token first. Go to the Home Assistant page, enter those values, then return here."):
                return
            self._start_wizard_speaker_discovery()
        elif action == "tts":
            self._open_product_area("Speakers & Audio", "Voice Behavior")
            self.checklist_txt.SetValue("Opened AI and speech settings. Set Gemini and default TTS options there, then return to this wizard for the final test.")
        elif action == "finish":
            self._open_product_area("Dashboard")
            self.checklist_txt.SetValue("Opened the main Viper dashboard. Optional device setup buttons are also available on this page.")
        elif action == "test":
            if not self._require_home_assistant_ready("Test Everything needs Home Assistant host and token first. Go to the Home Assistant page, enter those values, then return here."):
                return
            self._open_product_area("Diagnostics", "Tests & Support")
            self.parent.on_test_everything(event)
            self._session_completed_actions.add("test")
            self.checklist_txt.SetValue("Test Everything started. Results appear in the main Setup tab and diagnostics dialogs.")
        wx.CallAfter(self.on_refresh, None)

    def _set_step_status(self, message, announce=False):
        text = str(message or "")
        try:
            self.step_status_txt.SetValue(text)
            self.checklist_txt.SetValue(text + "\n\nOverall setup checklist:\n" + self.parent.build_setup_checklist_summary())
        except Exception:
            pass
        if announce:
            speaker = getattr(self.parent, "_safe_speak", None)
            if callable(speaker):
                wx.CallAfter(speaker, text)

    def _set_setup_status(self, message, announce=False):
        self._set_step_status(message, announce=announce)

    def _wizard_progress(self, message, *, announce=False):
        text = str(message or "").strip()
        if not text:
            return
        logging.info("[SETUP WIZARD HA] %s", text)
        self._wizard_progress_state = _classify_setup_progress_message(text, self._wizard_progress_state)
        self._wizard_progress_lines.append(text)
        self._wizard_progress_lines = self._wizard_progress_lines[-40:]
        try:
            self.parent.config["setup_progress"] = dict(self._wizard_progress_state)
            self.parent.save_config()
        except Exception:
            logging.debug("Could not save wizard setup progress.", exc_info=True)
        self._set_step_status(
            _format_setup_progress_state(self._wizard_progress_state, self._wizard_progress_lines),
            announce=announce,
        )

    def _thread_wizard_progress(self, message):
        wx.CallAfter(self._wizard_progress, message)

    def _replace_setup_progress(self, lines, announce=False):
        self._set_step_status("\n".join(str(line) for line in lines), announce=announce)

    def _append_setup_progress(self, lines, message, announce=False):
        lines.append(str(message))
        self._replace_setup_progress(lines, announce=announce)

    def _set_busy(self, busy):
        controls = [
            self.btn_action,
            self.btn_back,
            self.btn_refresh,
            self.btn_install_ha_wizard,
            self.btn_close,
        ]
        for name in (
            "btn_wizard_check_pc",
            "btn_wizard_install_vbox",
            "btn_wizard_optimize_windows",
            "btn_wizard_install_ha",
            "btn_wizard_start_ha",
            "btn_wizard_open_ha",
            "btn_wizard_open_token",
            "btn_find_ha_wizard",
            "btn_save_wizard_triggers",
            "btn_save_wizard_streams",
            "btn_test_wizard_front_camera",
            "btn_test_wizard_back_camera",
            "btn_test_wizard_speakers",
        ):
            control = getattr(self, name, None)
            if control is not None:
                controls.append(control)
        for control in controls:
            try:
                control.Enable(not busy)
            except Exception:
                pass
        self.btn_next.Enable(False if busy else self.btn_next.IsShown())

    def _record_setup_event(self, event_type, message, **details):
        recorder = getattr(self.parent, "_record_setup_event", None)
        if callable(recorder):
            recorder(event_type, message, **details)
        else:
            logging.info("[SETUP EVENT] %s message=%r details=%s", event_type, message, details)

    def _wizard_settings(self):
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        api_settings = cfg.get_api_settings(self.parent.config, include_env=True)
        doorbell = cfg.get_doorbell_settings(self.parent.config, include_env=True)
        typed_host = ""
        typed_port = ""
        typed_token = ""
        if hasattr(self, "wizard_ha_host_txt"):
            typed_host = self.wizard_ha_host_txt.GetValue().strip()
            typed_port = self.wizard_ha_port_txt.GetValue().strip()
            typed_token = self.wizard_ha_token_txt.GetValue().strip()
        return {
            "ha_ip": typed_host or ha_settings.get("ha_ip") or "",
            "ha_port": typed_port or ha_settings.get("ha_port") or "8123",
            "ha_token": typed_token or ha_settings.get("ha_token") or "",
            "gemini_api_key": api_settings.get("gemini_api_key") or "",
            "mqtt_host": doorbell.get("mqtt_host") or ha_settings.get("ha_ip") or "",
            "mqtt_port": doorbell.get("mqtt_port") or "1883",
            "mqtt_username": doorbell.get("mqtt_username") or "",
            "mqtt_password": doorbell.get("mqtt_password") or "",
        }

    def _settings(self):
        return self._wizard_settings()

    def on_find_home_assistant(self, event):
        settings = self._wizard_settings()
        self._set_step_status("Searching for Home Assistant on your network. This stays in the wizard.", announce=True)
        self.btn_find_ha_wizard.Enable(False)
        safe_submit(self._run_find_home_assistant_for_wizard, settings)

    def _run_find_home_assistant_for_wizard(self, settings):
        try:
            result = discovery.find_home_assistant(
                token=settings.get("ha_token") or None,
                seed_host=settings.get("ha_ip") or "",
                seed_port=settings.get("ha_port") or "8123",
                timeout=2,
            )
        except Exception as e:
            logging.exception("[SETUP WIZARD] Home Assistant find failed")
            result = {"ok": False, "message": str(e)}
        wx.CallAfter(self._finish_find_home_assistant_for_wizard, result)

    def _finish_find_home_assistant_for_wizard(self, result):
        try:
            self.btn_find_ha_wizard.Enable(True)
        except Exception:
            pass
        if result.get("ok"):
            host = result.get("ha_ip") or ""
            port = result.get("ha_port") or "8123"
            self.wizard_ha_host_txt.SetValue(host)
            self.wizard_ha_port_txt.SetValue(port)
            self._set_step_status(
                f"Home Assistant found at {host}:{port}. Paste your long-lived token if it is not already saved, then press Connect And Discover Devices.",
                announce=True,
            )
        else:
            self._set_step_status(
                (result.get("message") or "Viper could not find Home Assistant.")
                + "\n\nIf Home Assistant is not installed yet, use the buttons on this Home Assistant page: Check This PC, Install VirtualBox, Install Home Assistant, then Start Or Wait For Home Assistant.",
                announce=True,
            )
        self._render()

    def on_wizard_check_pc(self, event):
        self._set_busy(True)
        self._wizard_progress("Checking this PC, VirtualBox, existing Home Assistant VM, and Home Assistant network reachability.", announce=True)
        safe_submit(self._run_wizard_check_pc)

    def _run_wizard_check_pc(self):
        platform_status = get_ha_vm_platform_status()
        virtualization = get_windows_virtualization_status()
        vbox = get_virtualbox_status()
        winget = get_winget_status()
        ha_settings = self._wizard_settings()
        found = discovery.find_home_assistant(
            token=ha_settings.get("ha_token") or None,
            seed_host=ha_settings.get("ha_ip") or "",
            seed_port=ha_settings.get("ha_port") or "8123",
            timeout=2,
        )
        vm_exists = _vbox_vm_exists(HA_VM_NAME) if vbox.get("installed") else False
        wx.CallAfter(self._finish_wizard_check_pc, platform_status, virtualization, vbox, winget, vm_exists, found)

    def _finish_wizard_check_pc(self, platform_status, virtualization, vbox, winget, vm_exists, found):
        self._set_busy(False)
        lines = [
            "Home Assistant check results",
            "",
            f"Computer architecture: {platform_status.get('architecture', 'unknown')}.",
            platform_status.get("message", ""),
            virtualization.get("message", ""),
            f"winget: {'found' if winget.get('installed') else 'not found'}. {winget.get('version') or winget.get('message') or ''}",
            f"VirtualBox: {'found' if vbox.get('installed') else 'not found'}. {vbox.get('version') or vbox.get('message') or ''}",
            f"Home Assistant VM: {'found' if vm_exists else 'not found yet'}.",
        ]
        if virtualization.get("needs_attention"):
            lines.append("Optional stability step: press Optimize Windows For VirtualBox, reboot Windows, then continue.")
        if found.get("ok"):
            host = found.get("ha_ip") or ""
            port = found.get("ha_port") or "8123"
            self.wizard_ha_host_txt.SetValue(host)
            self.wizard_ha_port_txt.SetValue(port)
            self.parent.config["ha_ip"] = host
            self.parent.config["ha_port"] = port
            self.parent.save_config()
            lines.append(f"Home Assistant: found at {host}:{port}.")
            if found.get("auth_ok"):
                lines.append("Token: accepted. Press Connect And Discover Devices.")
            elif found.get("auth_error") == "bad_token":
                lines.append("Token: rejected. Create a new long-lived access token and paste it above.")
            else:
                lines.append("Token: missing or not tested. Create/paste a long-lived access token above.")
        else:
            lines.append("Home Assistant: not found automatically.")
            if not vbox.get("installed"):
                lines.append("Next action: press Install VirtualBox.")
            elif not vm_exists:
                lines.append("Next action: press Install Home Assistant.")
            else:
                lines.append("Next action: press Start Or Wait For Home Assistant.")
        self._set_step_status("\n".join(lines), announce=True)
        self._render()

    def _confirm_wizard_windows_optimization(self):
        message = (
            "This will turn off Windows hypervisor features so VirtualBox can run Home Assistant more reliably.\n\n"
            "This can affect WSL2, Docker Desktop, Windows Sandbox, and Hyper-V virtual machines until you re-enable those Windows features.\n\n"
            "Windows must be rebooted after the change. Continue?"
        )
        with wx.MessageDialog(self, message, "Optimize Windows For VirtualBox", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) as dlg:
            return dlg.ShowModal() == wx.ID_YES

    def on_wizard_optimize_windows_virtualbox(self, event):
        if not self._confirm_wizard_windows_optimization():
            self._set_step_status("Windows optimization was cancelled. No Windows settings were changed.", announce=True)
            return
        self._set_busy(True)
        self._wizard_progress("Starting Windows VirtualBox optimization. This requires administrator permission and a reboot.", announce=True)
        safe_submit(self._run_wizard_optimize_windows_virtualbox)

    def _run_wizard_optimize_windows_virtualbox(self):
        result = optimize_windows_for_virtualbox(progress=self._thread_wizard_progress)
        wx.CallAfter(self._finish_wizard_optimize_windows_virtualbox, result)

    def _finish_wizard_optimize_windows_virtualbox(self, result):
        self._set_busy(False)
        lines = ["Windows VirtualBox optimization result", "", result.get("message", "No result message.")]
        if result.get("reboot_required"):
            lines.extend(["", "Next step: reboot Windows before starting the Home Assistant VM."])
        elif result.get("needs_admin"):
            lines.extend(["", "Next step: close Viper, run it as administrator, then press Optimize Windows For VirtualBox again."])
        output = (result.get("output") or "").strip()
        if output:
            lines.extend(["", "Most recent command output:", output[-2000:]])
        self._set_step_status("\n".join(lines), announce=True)
        self._render()

    def on_wizard_install_virtualbox(self, event):
        platform_status = get_ha_vm_platform_status()
        if not platform_status.get("supported"):
            self._set_step_status(platform_status["message"] + "\n\nViper opened the official Home Assistant install page.", announce=True)
            open_official_link("ha_install")
            return
        self._set_busy(True)
        self._wizard_progress("Starting VirtualBox install with winget. Windows may ask for administrator permission.", announce=True)
        safe_submit(self._run_wizard_install_virtualbox)

    def _run_wizard_install_virtualbox(self):
        result = install_virtualbox_with_winget(progress=self._thread_wizard_progress)
        wx.CallAfter(self._finish_wizard_install_virtualbox, result)

    def _finish_wizard_install_virtualbox(self, result):
        self._set_busy(False)
        self._wizard_progress(result.get("message", "VirtualBox install finished."), announce=True)
        if result.get("open_download"):
            open_official_link("virtualbox")
        self._render()

    def on_wizard_install_home_assistant_vm(self, event):
        platform_status = get_ha_vm_platform_status()
        if not platform_status.get("supported"):
            self._set_step_status(platform_status["message"] + "\n\nViper opened the official Home Assistant install page.", announce=True)
            open_official_link("ha_install")
            return
        if not get_virtualbox_status().get("installed"):
            self._set_step_status("VirtualBox is not installed yet. Press Install VirtualBox first.", announce=True)
            return
        resources = self._ask_wizard_vm_resources()
        if not resources:
            self._set_step_status("Home Assistant install cancelled. No VM settings were changed.", announce=True)
            return
        if not self._confirm_wizard_ha_install_preflight(resources):
            self._set_step_status("Home Assistant install cancelled at the review step. No VM was created.", announce=True)
            return
        ram_mb = resources["ram_mb"]
        disk_gb = resources["disk_gb"]
        self.parent.config["ha_vm_ram_mb"] = ram_mb
        self.parent.config["ha_vm_disk_gb"] = disk_gb
        self.parent.save_config()
        self._set_busy(True)
        self._wizard_progress(f"Using {ram_mb} MB RAM and {disk_gb} GB disk space. Downloading and installing Home Assistant OS.", announce=True)
        safe_submit(self._run_wizard_install_home_assistant_vm, ram_mb, disk_gb)

    def _ask_wizard_vm_resources(self):
        current_ram = self.parent.config.get("ha_vm_ram_mb", DEFAULT_HA_VM_RAM_MB)
        current_disk = self.parent.config.get("ha_vm_disk_gb", DEFAULT_HA_VM_DISK_GB)
        dlg = HomeAssistantVmResourcesDialog(self, current_ram, current_disk)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return {"ram_mb": dlg.ram_mb(), "disk_gb": dlg.disk_gb()}
        finally:
            dlg.Destroy()

    def _confirm_wizard_ha_install_preflight(self, resources):
        summary = build_ha_install_preflight_summary(resources)
        style = wx.YES_NO | wx.ICON_WARNING
        if not summary.get("drive_ok"):
            style |= wx.NO_DEFAULT
        with wx.MessageDialog(self, summary["message"], "Review Home Assistant VM Install", style) as dlg:
            return dlg.ShowModal() == wx.ID_YES

    def _run_wizard_install_home_assistant_vm(self, ram_mb, disk_gb):
        result = download_and_install_home_assistant_vm(progress=self._thread_wizard_progress, ram_mb=ram_mb, disk_gb=disk_gb)
        if result.get("ok"):
            self._thread_wizard_progress("Home Assistant VM is installed. Starting the VM now.")
            result["start_result"] = self._wizard_start_and_wait_for_ha()
        wx.CallAfter(self._finish_wizard_install_home_assistant_vm, result)

    def _finish_wizard_install_home_assistant_vm(self, result):
        self._set_busy(False)
        if result.get("ok"):
            start_result = result.get("start_result") or {}
            first_boot = start_result.get("first_boot") or {}
            if first_boot.get("ok"):
                self.wizard_ha_host_txt.SetValue(first_boot.get("ha_ip", ""))
                self.wizard_ha_port_txt.SetValue(first_boot.get("ha_port", "8123"))
                self.parent.config["ha_ip"] = first_boot.get("ha_ip", "")
                self.parent.config["ha_port"] = first_boot.get("ha_port", "8123")
                self.parent.save_config()
            self._wizard_progress(start_result.get("message") or result.get("message") or "Home Assistant install finished.", announce=True)
        else:
            self._wizard_progress(result.get("message") or "Home Assistant install failed.", announce=True)
        self._render()

    def on_wizard_start_home_assistant_vm(self, event):
        self._set_busy(True)
        self._wizard_progress("Starting Home Assistant VM. Viper will keep checking for Core readiness for up to 25 minutes.", announce=True)
        safe_submit(self._run_wizard_start_home_assistant_vm)

    def _run_wizard_start_home_assistant_vm(self):
        result = self._wizard_start_and_wait_for_ha()
        wx.CallAfter(self._finish_wizard_start_home_assistant_vm, result)

    def _wizard_start_and_wait_for_ha(self):
        result = start_home_assistant_vm(progress=self._thread_wizard_progress)
        if not result.get("ok"):
            return result
        settings = self._wizard_settings()
        self._thread_wizard_progress("Home Assistant VM started. Waiting for Home Assistant Core to finish first boot.")
        first_boot = wait_for_home_assistant_first_boot(
            progress=self._thread_wizard_progress,
            token=settings.get("ha_token") or None,
            seed_host=settings.get("ha_ip") or "",
            seed_port=settings.get("ha_port") or "8123",
            timeout_seconds=1500,
            interval_seconds=15,
        )
        result["first_boot"] = first_boot
        if first_boot.get("ok"):
            result["message"] = first_boot.get("message") or result.get("message")
        return result

    def _finish_wizard_start_home_assistant_vm(self, result):
        self._set_busy(False)
        first_boot = result.get("first_boot") or {}
        if first_boot.get("ok"):
            self.wizard_ha_host_txt.SetValue(first_boot.get("ha_ip", ""))
            self.wizard_ha_port_txt.SetValue(first_boot.get("ha_port", "8123"))
            self.parent.config["ha_ip"] = first_boot.get("ha_ip", "")
            self.parent.config["ha_port"] = first_boot.get("ha_port", "8123")
            self.parent.save_config()
        self._wizard_progress(result.get("message") or "Home Assistant VM start finished.", announce=True)
        self._render()

    def on_wizard_open_home_assistant(self, event):
        settings = self._wizard_settings()
        host = (settings.get("ha_ip") or "homeassistant.local").strip()
        port = (settings.get("ha_port") or "8123").strip()
        url = host if re.match(r"^https?://", host, re.IGNORECASE) else f"http://{host}:{port}"
        if open_url(url):
            self._set_step_status(
                f"Opened Home Assistant account setup in your browser:\n{url}\n\nCreate the Home Assistant owner account there. After that, create a long-lived access token and paste it in this wizard.",
                announce=True,
            )
        else:
            self._set_step_status(f"Viper could not open the browser. Open this address manually:\n{url}", announce=True)

    def on_wizard_open_token_page(self, event):
        if self._open_home_assistant_path("/profile"):
            self._set_step_status(
                "Opened the Home Assistant profile page. In Home Assistant, go to Security or Long-Lived Access Tokens, create a token for Viper, copy it, paste it in this wizard, then press Connect And Discover Devices.",
                announce=True,
            )
        else:
            self._set_step_status("Home Assistant address is missing. Find or enter Home Assistant before opening the token page.", announce=True)

    def _hassio_request(self, *args, **kwargs):
        return HomeAssistantSetupDialog._hassio_request(self, *args, **kwargs)

    def _hassio_ws_request(self, *args, **kwargs):
        return HomeAssistantSetupDialog._hassio_ws_request(self, *args, **kwargs)

    def _ha_ws_command(self, *args, **kwargs):
        return HomeAssistantSetupDialog._ha_ws_command(self, *args, **kwargs)

    def _addon_items_from_payload(self, *args, **kwargs):
        return HomeAssistantSetupDialog._addon_items_from_payload(self, *args, **kwargs)

    def _payload_data(self, *args, **kwargs):
        return HomeAssistantSetupDialog._payload_data(self, *args, **kwargs)

    def _get_installed_addons(self, *args, **kwargs):
        return HomeAssistantSetupDialog._get_installed_addons(self, *args, **kwargs)

    def _get_addon_info(self, *args, **kwargs):
        return HomeAssistantSetupDialog._get_addon_info(self, *args, **kwargs)

    def _ensure_addon_started(self, *args, **kwargs):
        return HomeAssistantSetupDialog._ensure_addon_started(self, *args, **kwargs)

    def _restart_addon(self, *args, **kwargs):
        return HomeAssistantSetupDialog._restart_addon(self, *args, **kwargs)

    def _configure_ring_mqtt_rtsp_port(self, *args, **kwargs):
        return HomeAssistantSetupDialog._configure_ring_mqtt_rtsp_port(self, *args, **kwargs)

    def _configure_ring_mqtt_rtsp_port_and_restart(self, *args, **kwargs):
        return HomeAssistantSetupDialog._configure_ring_mqtt_rtsp_port_and_restart(self, *args, **kwargs)

    def _absolute_ha_url(self, *args, **kwargs):
        return HomeAssistantSetupDialog._absolute_ha_url(self, *args, **kwargs)

    def _normalize_addon_webui(self, *args, **kwargs):
        return HomeAssistantSetupDialog._normalize_addon_webui(self, *args, **kwargs)

    def _get_current_ha_user_id(self, *args, **kwargs):
        return HomeAssistantSetupDialog._get_current_ha_user_id(self, *args, **kwargs)

    def _create_ingress_session(self, *args, **kwargs):
        return HomeAssistantSetupDialog._create_ingress_session(self, *args, **kwargs)

    def _ingress_session_url(self, *args, **kwargs):
        return HomeAssistantSetupDialog._ingress_session_url(self, *args, **kwargs)

    def _resolve_addon_login_url(self, *args, **kwargs):
        return HomeAssistantSetupDialog._resolve_addon_login_url(self, *args, **kwargs)

    def _ring_mqtt_app_page_url(self, *args, **kwargs):
        return HomeAssistantSetupDialog._ring_mqtt_app_page_url(self, *args, **kwargs)

    def _find_addon_slug(self, *args, **kwargs):
        return HomeAssistantSetupDialog._find_addon_slug(self, *args, **kwargs)

    def _find_ring_mqtt_slug(self, *args, **kwargs):
        return HomeAssistantSetupDialog._find_ring_mqtt_slug(self, *args, **kwargs)

    def _is_ring_mqtt_slug(self, *args, **kwargs):
        return HomeAssistantSetupDialog._is_ring_mqtt_slug(self, *args, **kwargs)

    def _addon_installed_in_store(self, *args, **kwargs):
        return HomeAssistantSetupDialog._addon_installed_in_store(self, *args, **kwargs)

    def _rtsp_host_from_ha_host(self, *args, **kwargs):
        return HomeAssistantSetupDialog._rtsp_host_from_ha_host(self, *args, **kwargs)

    def _normalize_rtsp_host(self, *args, **kwargs):
        return HomeAssistantSetupDialog._normalize_rtsp_host(self, *args, **kwargs)

    def _stream_name_from_rtsp_url(self, *args, **kwargs):
        return HomeAssistantSetupDialog._stream_name_from_rtsp_url(self, *args, **kwargs)

    def _run_find_ha_ring_rtsp_streams(self, *args, **kwargs):
        return HomeAssistantSetupDialog._run_find_ha_ring_rtsp_streams(self, *args, **kwargs)

    def _run_find_ring_mqtt_log_streams(self, *args, **kwargs):
        return HomeAssistantSetupDialog._run_find_ring_mqtt_log_streams(self, *args, **kwargs)

    def _stream_rtsp_url(self, *args, **kwargs):
        return HomeAssistantSetupDialog._stream_rtsp_url(self, *args, **kwargs)

    def _ring_mqtt_stream_score(self, *args, **kwargs):
        return HomeAssistantSetupDialog._ring_mqtt_stream_score(self, *args, **kwargs)

    def _live_stream_score(self, *args, **kwargs):
        return HomeAssistantSetupDialog._live_stream_score(self, *args, **kwargs)

    def _start_direct_home_assistant_setup(self):
        settings = self._wizard_settings()
        if not settings.get("ha_token"):
            self._set_step_status(
                "Home Assistant token is missing. Paste a long-lived access token in the token box on this wizard page, or set HA_TOKEN in environment variables. If you do not have Home Assistant yet, use Check This PC, Install VirtualBox, Install Home Assistant, then Start Or Wait For Home Assistant.",
                announce=True,
            )
            return
        self._set_step_status("Connecting to Home Assistant and discovering devices. This stays in the wizard.", announce=True)
        self.btn_action.Enable(False)
        safe_submit(self._run_direct_home_assistant_setup, settings)

    def _run_direct_home_assistant_setup(self, settings):
        result = {"ok": False, "message": "Home Assistant setup did not complete."}
        try:
            host_result = discovery.find_home_assistant(
                token=settings.get("ha_token"),
                seed_host=settings.get("ha_ip") or "",
                seed_port=settings.get("ha_port") or "8123",
                timeout=2,
            )
            if not host_result.get("ok") or host_result.get("auth_error") == "bad_token":
                result = {
                    "ok": False,
                    "message": host_result.get("message") or "Home Assistant was not found or rejected the token.",
                    "host_result": host_result,
                }
                wx.CallAfter(self._finish_direct_home_assistant_setup, result)
                return
            settings["ha_ip"] = host_result.get("ha_ip") or settings.get("ha_ip") or ""
            settings["ha_port"] = host_result.get("ha_port") or settings.get("ha_port") or "8123"
            entity_result = discovery.discover_ha_entities(
                ha_ip=settings["ha_ip"],
                ha_port=settings["ha_port"],
                token=settings["ha_token"],
                timeout=8,
            )
            if not entity_result.get("ok"):
                result = {
                    "ok": False,
                    "message": entity_result.get("message") or "Home Assistant entity discovery failed.",
                    "host_result": host_result,
                    "discovery": entity_result,
                }
                wx.CallAfter(self._finish_direct_home_assistant_setup, result)
                return
            result = {
                "ok": True,
                "message": "Home Assistant connected and devices discovered.",
                "settings": settings,
                "host_result": host_result,
                "discovery": entity_result,
            }
        except Exception as e:
            logging.exception("[SETUP WIZARD] Direct Home Assistant setup failed")
            result = {"ok": False, "message": str(e)}
        wx.CallAfter(self._finish_direct_home_assistant_setup, result)

    def _finish_direct_home_assistant_setup(self, result):
        self.btn_action.Enable(True)
        if not result.get("ok"):
            message = result.get("message") or "Home Assistant setup failed."
            host_result = result.get("host_result") or {}
            if host_result and host_result.get("auth_error") != "bad_token":
                message += "\n\nViper could not reach a Home Assistant server. If Home Assistant is not installed yet, use the Home Assistant install buttons on this same wizard page."
            self._set_step_status(message, announce=True)
            self._render()
            return
        settings = result.get("settings") or {}
        self.parent.config["ha_ip"] = settings.get("ha_ip") or self.parent.config.get("ha_ip") or ""
        self.parent.config["ha_port"] = settings.get("ha_port") or self.parent.config.get("ha_port") or "8123"
        if settings.get("ha_token") and not self.parent.config.get("ha_token"):
            self.parent.config["ha_token"] = settings.get("ha_token")
        if settings.get("gemini_api_key") and not self.parent.config.get("gemini_api_key"):
            self.parent.config["gemini_api_key"] = settings.get("gemini_api_key")
        self._apply_best_doorbell_triggers_from_discovery(result.get("discovery") or {})
        self.parent.save_config()
        self.parent.refresh_setup_checklist()
        counts = (result.get("discovery") or {}).get("counts", {})
        self._session_completed_actions.add("ha_connect")
        try:
            self.wizard_ha_host_txt.SetValue(self.parent.config.get("ha_ip") or "")
            self.wizard_ha_port_txt.SetValue(str(self.parent.config.get("ha_port") or "8123"))
        except Exception:
            pass
        self._set_step_status(
            "Home Assistant passed. "
            f"Found {(result.get('discovery') or {}).get('entity_count', 0)} entities, "
            f"{counts.get('media_players', 0)} media players, "
            f"{counts.get('ring_cameras', 0)} Ring camera entities, and "
            f"{counts.get('vacuum_entities', 0)} vacuums. Continue To Ring In Home Assistant is now available.",
            announce=True,
        )
        self._render()

    def _entity_score_for_doorbell(self, entity, side):
        text = " ".join(
            str(entity.get(key, ""))
            for key in ("entity_id", "friendly_name", "domain", "platform", "attributes_summary")
        ).lower().replace("_", " ")
        score = 0
        for token, points in (
            ("ring", 8),
            ("doorbell", 8),
            ("ding", 9),
            ("button", 5),
            ("motion", 4),
            ("visitor", 4),
            ("front", 10 if side == "front" else -4),
            ("porch", 4 if side == "front" else 0),
            ("back", 10 if side == "back" else -4),
            ("rear", 8 if side == "back" else -4),
        ):
            if token in text:
                score += points
        if entity.get("domain") in {"binary_sensor", "sensor", "event"}:
            score += 2
        return score

    def _apply_best_doorbell_triggers_from_discovery(self, result):
        if not result.get("ok"):
            return 0
        entities = result.get("all_entities") or []
        candidates = [
            entity for entity in entities
            if entity.get("domain") in {"binary_sensor", "sensor", "event"}
            and any(token in (" ".join(str(entity.get(key, "")) for key in ("entity_id", "friendly_name", "platform", "attributes_summary")).lower()) for token in ("ring", "doorbell", "ding", "motion", "visitor"))
        ]
        triggers = self.parent.config.setdefault("doorbell_triggers", {})
        used = set()
        changed = 0
        for side in ("front", "back"):
            current = triggers.get(side, {}) if isinstance(triggers.get(side), dict) else {}
            if current.get("trigger_entity_id"):
                used.add(current.get("trigger_entity_id"))
                continue
            available = [item for item in candidates if item.get("entity_id") not in used]
            best = max(available, key=lambda item: self._entity_score_for_doorbell(item, side), default=None)
            if best and self._entity_score_for_doorbell(best, side) > 0:
                used.add(best.get("entity_id"))
                existing = dict(current)
                existing.update({
                    "enabled": bool(existing.get("rtsp_url")),
                    "source": "ha_state",
                    "trigger_entity_id": best.get("entity_id"),
                    "active_states": list(ha_listener.DEFAULT_ACTIVE_STATES),
                })
                triggers[side] = existing
                changed += 1
        return changed

    def _doorbell_trigger_label(self, entity):
        entity_id = str(entity.get("entity_id") or "").strip()
        friendly = str(entity.get("friendly_name") or "").strip()
        domain = str(entity.get("domain") or "").strip()
        if friendly and friendly.lower() != entity_id.lower():
            return f"{friendly}, {entity_id}"
        if domain:
            return f"{entity_id}, {domain}"
        return entity_id

    def _collect_doorbell_trigger_candidates(self, result=None):
        entities = []
        if isinstance(result, dict) and result.get("ok"):
            entities.extend(result.get("all_entities") or [])
        entities.extend(getattr(self, "_wizard_doorbell_trigger_candidates", []) or [])

        triggers = self.parent.config.get("doorbell_triggers", {})
        if isinstance(triggers, dict):
            for side in ("front", "back"):
                current = triggers.get(side, {}) if isinstance(triggers.get(side), dict) else {}
                entity_id = str(current.get("trigger_entity_id") or "").strip()
                if entity_id:
                    entities.append({
                        "entity_id": entity_id,
                        "friendly_name": entity_id,
                        "domain": entity_id.split(".", 1)[0] if "." in entity_id else "",
                        "platform": "saved",
                    })

        filtered = []
        seen = set()
        for entity in entities:
            entity_id = str(entity.get("entity_id") or "").strip()
            if not entity_id or entity_id in seen:
                continue
            text = " ".join(
                str(entity.get(key, ""))
                for key in ("entity_id", "friendly_name", "domain", "platform", "attributes_summary")
            ).lower()
            if (
                entity.get("domain") in {"binary_sensor", "sensor", "event"}
                and any(token in text for token in ("ring", "doorbell", "ding", "motion", "visitor", "button", "front", "back"))
            ):
                seen.add(entity_id)
                filtered.append(entity)
        filtered.sort(key=lambda item: (-(self._entity_score_for_doorbell(item, "front") + self._entity_score_for_doorbell(item, "back")), self._doorbell_trigger_label(item).lower()))
        self._wizard_doorbell_trigger_candidates = filtered
        return filtered

    def _refresh_wizard_doorbell_trigger_controls(self):
        if not hasattr(self, "wizard_front_trigger_choice"):
            return
        candidates = self._collect_doorbell_trigger_candidates()
        self._wizard_doorbell_trigger_choices = candidates
        choices = [self._doorbell_trigger_label(item) for item in candidates]
        triggers = self.parent.config.get("doorbell_triggers", {})
        front_id = ""
        back_id = ""
        if isinstance(triggers, dict):
            front = triggers.get("front", {}) if isinstance(triggers.get("front"), dict) else {}
            back = triggers.get("back", {}) if isinstance(triggers.get("back"), dict) else {}
            front_id = str(front.get("trigger_entity_id") or "").strip()
            back_id = str(back.get("trigger_entity_id") or "").strip()

        for control, selected_id in (
            (self.wizard_front_trigger_choice, front_id),
            (self.wizard_back_trigger_choice, back_id),
        ):
            current = selected_id or control.GetValue()
            control.SetItems(choices)
            index = next((idx for idx, item in enumerate(candidates) if item.get("entity_id") == current), wx.NOT_FOUND)
            if index != wx.NOT_FOUND:
                control.SetSelection(index)
            elif choices:
                control.SetSelection(0)

    def on_save_wizard_doorbell_triggers(self, event):
        candidates = list(getattr(self, "_wizard_doorbell_trigger_choices", []) or self._collect_doorbell_trigger_candidates())
        if not candidates:
            self._set_step_status("No doorbell trigger choices are available yet. Press Check Doorbell Triggers first, after the Ring integration is logged in through Home Assistant.", announce=True)
            return

        def selected_entity(control):
            idx = control.GetSelection()
            if idx == wx.NOT_FOUND or idx >= len(candidates):
                return ""
            return str(candidates[idx].get("entity_id") or "").strip()

        front_entity = selected_entity(self.wizard_front_trigger_choice)
        back_entity = selected_entity(self.wizard_back_trigger_choice)
        if front_entity and back_entity and front_entity == back_entity:
            self._set_step_status("Front and back doorbell triggers cannot be the same entity. Choose a different trigger for one door, or leave one door unconfigured.", announce=True)
            return
        if not front_entity and not back_entity:
            self._set_step_status("Choose at least one doorbell trigger before saving.", announce=True)
            return

        triggers = self.parent.config.setdefault("doorbell_triggers", {})
        for side, entity_id in (("front", front_entity), ("back", back_entity)):
            if not entity_id:
                continue
            current = triggers.get(side, {}) if isinstance(triggers.get(side), dict) else {}
            current.update({
                "enabled": bool(entity_id and (current.get("rtsp_url") or self.parent.config.get(f"rtsp_{side}"))),
                "source": "ha_state",
                "trigger_entity_id": entity_id,
                "active_states": list(ha_listener.DEFAULT_ACTIVE_STATES),
            })
            triggers[side] = current
        self.parent.save_config()
        self.parent.refresh_setup_checklist()
        self._session_completed_actions.add("doorbells")
        self._set_step_status(
            "Doorbell triggers saved.\n"
            f"Front trigger: {front_entity or 'not changed'}\n"
            f"Back trigger: {back_entity or 'not changed'}\n\n"
            "Continue To Speakers And Audio is now available.",
            announce=True,
        )
        self._render()

    def _start_ring_trigger_check(self):
        settings = self._wizard_settings()
        self._set_step_status("Checking Home Assistant for Ring doorbell trigger entities.", announce=True)
        self.btn_action.Enable(False)
        safe_submit(self._run_ring_trigger_check, settings)

    def _run_ring_trigger_check(self, settings):
        try:
            result = discovery.discover_ha_entities(
                ha_ip=settings["ha_ip"],
                ha_port=settings["ha_port"],
                token=settings["ha_token"],
                timeout=8,
            )
        except Exception as e:
            result = {"ok": False, "message": str(e)}
        wx.CallAfter(self._finish_ring_trigger_check, result)

    def _finish_ring_trigger_check(self, result):
        self.btn_action.Enable(True)
        if not result.get("ok"):
            self._set_step_status(result.get("message") or "Could not check Ring trigger entities.", announce=True)
            self._render()
            return
        self._collect_doorbell_trigger_candidates(result)
        changed = self._apply_best_doorbell_triggers_from_discovery(result)
        self.parent.save_config()
        self.parent.refresh_setup_checklist()
        self._refresh_wizard_doorbell_trigger_controls()
        trigger_count = self._configured_doorbell_trigger_count()
        if trigger_count:
            self._session_completed_actions.add("ring_integration")
            self._set_step_status(
                f"Ring trigger check passed. Viper has {trigger_count} trigger entity or entities saved. "
                f"New trigger entities selected now: {changed}. Review the front and back trigger combo boxes, then press Save Selected Doorbell Triggers if you need to change them.",
                announce=True,
            )
        else:
            self._set_step_status(
                "Ring trigger check did not find a usable ding, button, motion, or visitor entity. "
                "Finish logging into the normal Ring integration in Home Assistant, then run this check again.",
                announce=True,
            )
        self._render()

    def _start_wizard_ring_mqtt_setup(self):
        settings = self._wizard_settings()
        self._set_busy(True)
        self._set_step_status(
            "Installing or checking Ring-MQTT from the wizard. Viper will check Mosquitto, Ring-MQTT, RTSP port 8554, and then open the Ring-MQTT login guide.",
            announce=True,
        )
        safe_submit(self._run_install_ring_mqtt_requirements, settings)

    def _run_install_ring_mqtt_requirements(self, settings):
        return HomeAssistantSetupDialog._run_install_ring_mqtt_requirements(self, settings)

    def _finish_install_ring_mqtt_requirements(self, result):
        self._set_busy(False)
        self._set_step_status(result.get("message") or "Ring-MQTT setup finished.", announce=True)
        if not result.get("ok"):
            open_help("ring-mqtt-setup")
            self._render()
            return
        self._session_completed_actions.add("ring_mqtt")
        ring_slug = result.get("ring_slug") or RING_MQTT_ADDON_SLUG
        if ring_slug:
            wx.CallAfter(self._open_ring_mqtt_login, ring_slug)
        self._render()

    def _open_ring_mqtt_login(self, slug):
        return HomeAssistantSetupDialog._open_ring_mqtt_login(self, slug)

    def _after_ring_mqtt_login(self):
        self._session_completed_actions.add("ring_mqtt")
        self._set_step_status(
            "Ring-MQTT login guide closed. If Ring login is complete, continue to Test Doorbell Cameras and press Find And Test Doorbell Cameras.",
            announce=True,
        )
        self._render()

    def _start_wizard_live_stream_discovery(self):
        settings = self._wizard_settings()
        host = self._rtsp_host_from_ha_host(settings.get("ha_ip") or settings.get("mqtt_host"))
        if not host:
            self._set_step_status("Home Assistant host is missing, so Viper cannot find Ring-MQTT RTSP streams yet.", announce=True)
            return
        self._set_busy(True)
        self._set_step_status(
            "Finding and testing doorbell cameras inside the wizard. Viper checks Ring-MQTT camera attributes, add-on logs, and MQTT topics.",
            announce=True,
        )
        safe_submit(self._run_wizard_live_stream_discovery, settings, host)

    def _run_wizard_live_stream_discovery(self, settings, host):
        attempts = []
        streams = []
        try:
            self._replace_setup_progress(["Finding Ring-MQTT live streams", "", "Checking Home Assistant Ring-MQTT camera attributes."], announce=False)
            ha_streams = self._run_find_ha_ring_rtsp_streams(settings, host)
            streams.extend(ha_streams.get("streams", []))
            attempts.append(ha_streams.get("attempt", "Home Assistant stream scan completed."))

            self._replace_setup_progress(["Finding Ring-MQTT live streams", "", *attempts, "Checking Ring-MQTT add-on logs."], announce=False)
            log_streams = self._run_find_ring_mqtt_log_streams(settings, host)
            streams.extend(log_streams.get("streams", []))
            attempts.append(log_streams.get("attempt", "Ring-MQTT log scan completed."))

            mqtt_host = settings.get("mqtt_host") or settings.get("ha_ip") or host
            if mqtt_host:
                self._replace_setup_progress(["Finding Ring-MQTT live streams", "", *attempts, "Listening briefly for Ring MQTT topics."], announce=False)
                mqtt_result = ring_discovery.listen_for_ring_topics(
                    mqtt_host=mqtt_host,
                    mqtt_port=settings.get("mqtt_port") or 1883,
                    mqtt_username=settings.get("mqtt_username") or "",
                    mqtt_password=settings.get("mqtt_password") or "",
                    topic="ring/#",
                    duration=8,
                    rtsp_host=host,
                    stop_on_first=False,
                )
                if mqtt_result.get("ok"):
                    for item in mqtt_result.get("suggestions", []):
                        rtsp_url = item.get("rtsp_url") or ""
                        camera_id = item.get("camera_id") or ""
                        if rtsp_url and camera_id:
                            streams.append({
                                "name": f"{camera_id}_live",
                                "rtsp_url": rtsp_url,
                                "source": "ring-mqtt",
                                "topic": item.get("topic", ""),
                            })
                    attempts.append(f"MQTT ring/# at {mqtt_host}:{settings.get('mqtt_port') or 1883} -> {mqtt_result.get('count', 0)} possible Ring stream topic(s)")
                else:
                    attempts.append(
                        f"MQTT ring/# at {mqtt_host}:{settings.get('mqtt_port') or 1883} -> "
                        f"{mqtt_result.get('message') or mqtt_result.get('error') or 'failed'}"
                    )

            seen = set()
            unique = []
            for stream in streams:
                name = stream.get("name", "").strip()
                key = stream.get("rtsp_url") or name
                if name and key not in seen:
                    seen.add(key)
                    unique.append(stream)

            results = []
            progress_lines = [
                "Testing Ring-MQTT live streams",
                "",
                f"Found {len(unique)} possible stream(s).",
                "Viper will test each stream for a real video frame before saving it.",
                "",
            ]
            for index, stream in enumerate(unique, 1):
                rtsp_url = self._stream_rtsp_url(stream, host)
                label = stream.get("friendly_name") or stream.get("name") or rtsp_url or f"stream {index}"
                self._append_setup_progress(progress_lines, f"Testing stream {index} of {len(unique)}: {label}", announce=False)
                started = time.perf_counter()
                result = {
                    "index": index,
                    "stream": stream,
                    "name": stream.get("name", ""),
                    "friendly_name": stream.get("friendly_name", ""),
                    "source": stream.get("source", ""),
                    "rtsp_url": rtsp_url,
                    "ok": False,
                    "elapsed": 0,
                    "message": "No RTSP URL was available for this stream.",
                }
                if rtsp_url:
                    try:
                        test_dir = cfg.DATA_DIR / "rtsp_test"
                        test_dir.mkdir(parents=True, exist_ok=True)
                        frame = vision.grab_frame(rtsp_url, test_dir, f"wizard_stream_{index}", min_bytes=min(cfg.FRONT_MIN_FRAME_BYTES, cfg.BACK_MIN_FRAME_BYTES), timeout=8)
                        result.update({
                            "ok": bool(frame),
                            "frame": frame,
                            "message": "Frame captured." if frame else "No live frame was captured before the timeout.",
                        })
                    except Exception as e:
                        result["message"] = str(e)
                result["elapsed"] = time.perf_counter() - started
                results.append(result)
                status = "passed" if result.get("ok") else "failed"
                self._append_setup_progress(progress_lines, f"Stream {index} {status} in {result['elapsed']:.1f} seconds: {result.get('message')}", announce=False)
            wx.CallAfter(self._finish_wizard_live_stream_discovery, {"ok": True, "results": results, "attempts": attempts, "host": host})
        except Exception as e:
            logging.exception("[SETUP WIZARD] Live stream discovery failed")
            wx.CallAfter(self._finish_wizard_live_stream_discovery, {"ok": False, "message": str(e), "attempts": attempts, "host": host})

    def _finish_wizard_live_stream_discovery(self, result):
        self._set_busy(False)
        if not result.get("ok"):
            self._set_step_status(result.get("message") or "Live stream discovery failed.", announce=True)
            self._render()
            return
        results = result.get("results") or []
        self._wizard_stream_test_results = list(results)
        passed = [item for item in results if item.get("ok") and item.get("rtsp_url")]
        failed = [item for item in results if not item.get("ok")]
        lines = [f"RTSP stream testing finished. {len(passed)} passed, {len(failed)} failed."]
        for item in results:
            label = item.get("friendly_name") or item.get("name") or item.get("rtsp_url") or f"stream {item.get('index')}"
            status = "passed" if item.get("ok") else "failed"
            elapsed = item.get("elapsed")
            elapsed_text = f" in {elapsed:.1f} seconds" if isinstance(elapsed, (int, float)) else ""
            lines.append(f"- {label}: {status}{elapsed_text}.")
        if not passed:
            lines.extend(["", "No live RTSP streams passed. Finish Ring-MQTT login, confirm port 8554 is exposed, then run this step again."])
            self._set_step_status("\n".join(lines), announce=True)
            self._render()
            return

        self._refresh_wizard_camera_stream_controls()
        lines.extend([
            "",
            "Review the front and back camera stream boxes on this page.",
            "Choose Do not use a camera stream for any door you do not use.",
            "Then press Save Selected Camera Streams.",
        ])
        self._set_step_status("\n".join(lines), announce=True)
        self._render()

    def _best_tested_stream_for_wizard(self, side, passed, used_urls):
        candidates = [item for item in passed if item.get("rtsp_url") not in used_urls]
        if not candidates:
            return None
        return max(candidates, key=lambda item: self._live_stream_score(item.get("stream") or {}, side))

    def _camera_stream_label(self, item):
        if not item:
            return "Do not use a camera stream for this door"
        stream = item.get("stream") or {}
        friendly = item.get("friendly_name") or stream.get("friendly_name") or stream.get("entity_id") or ""
        name = item.get("name") or stream.get("name") or self._stream_name_from_rtsp_url(item.get("rtsp_url") or "")
        source = item.get("source") or stream.get("source") or "Ring-MQTT"
        rtsp_url = item.get("rtsp_url") or stream.get("rtsp_url") or ""
        label_parts = [part for part in (friendly, name, source) if part]
        label = ", ".join(label_parts) if label_parts else rtsp_url
        return f"{label}. URL: {rtsp_url}" if rtsp_url else label

    def _saved_stream_item(self, side):
        triggers = self.parent.config.get("doorbell_triggers", {})
        trigger = triggers.get(side, {}) if isinstance(triggers, dict) and isinstance(triggers.get(side), dict) else {}
        rtsp_url = (
            trigger.get("rtsp_url")
            or self.parent.config.get("rtsp_front" if side == "front" else "rtsp_back")
            or ""
        )
        rtsp_url = str(rtsp_url).strip()
        if not rtsp_url:
            return None
        name = self._stream_name_from_rtsp_url(rtsp_url) or f"{side}_door_live"
        stream = {
            "name": name,
            "rtsp_url": rtsp_url,
            "source": "saved config",
            "camera_id": trigger.get("camera_id") or self.parent.config.get("front_camera_id" if side == "front" else "back_camera_id", ""),
            "topic": trigger.get("mqtt_topic") or self.parent.config.get("mqtt_front_topic" if side == "front" else "mqtt_back_topic", ""),
        }
        return {
            "name": name,
            "friendly_name": f"Saved {side} door stream",
            "source": "saved config",
            "rtsp_url": rtsp_url,
            "ok": True,
            "stream": stream,
        }

    def _passed_wizard_streams(self):
        results = [
            dict(item)
            for item in getattr(self, "_wizard_stream_test_results", []) or []
            if item.get("ok") and item.get("rtsp_url")
        ]
        for side in ("front", "back"):
            saved = self._saved_stream_item(side)
            if saved:
                results.append(saved)
        seen = set()
        unique = []
        for item in results:
            rtsp_url = (item.get("rtsp_url") or "").strip()
            if not rtsp_url or rtsp_url in seen:
                continue
            seen.add(rtsp_url)
            unique.append(item)
        return unique

    def _refresh_wizard_camera_stream_controls(self):
        candidates = self._passed_wizard_streams()
        choices = [None] + candidates
        labels = [self._camera_stream_label(item) for item in choices]
        self._wizard_stream_choices = choices
        for control in (self.wizard_front_stream_choice, self.wizard_back_stream_choice):
            control.SetItems(labels)
            if labels:
                control.SetSelection(0)

        def select_url(control, url):
            url = (url or "").strip()
            if not url:
                return False
            for index, item in enumerate(choices):
                if item and item.get("rtsp_url") == url:
                    control.SetSelection(index)
                    return True
            return False

        triggers = self.parent.config.get("doorbell_triggers", {})
        front_saved = self.parent.config.get("rtsp_front", "")
        back_saved = self.parent.config.get("rtsp_back", "")
        if isinstance(triggers, dict):
            front = triggers.get("front", {}) if isinstance(triggers.get("front"), dict) else {}
            back = triggers.get("back", {}) if isinstance(triggers.get("back"), dict) else {}
            front_saved = front.get("rtsp_url") or front_saved
            back_saved = back.get("rtsp_url") or back_saved
        front_selected = select_url(self.wizard_front_stream_choice, front_saved)
        back_selected = select_url(self.wizard_back_stream_choice, back_saved)
        passed = [item for item in candidates if item.get("rtsp_url")]
        if passed and not front_selected:
            front = self._best_tested_stream_for_wizard("front", passed, set()) or passed[0]
            select_url(self.wizard_front_stream_choice, front.get("rtsp_url"))
            used = {front.get("rtsp_url")}
        else:
            used = {front_saved} if front_saved else set()
        if passed and not back_selected:
            back = self._best_tested_stream_for_wizard("back", passed, used)
            if back:
                select_url(self.wizard_back_stream_choice, back.get("rtsp_url"))

    def _selected_wizard_stream(self, control):
        selection = control.GetSelection()
        if selection < 0 or selection >= len(self._wizard_stream_choices):
            return None
        return self._wizard_stream_choices[selection]

    def _clear_stream_for_trigger(self, side):
        self.parent.config["rtsp_front" if side == "front" else "rtsp_back"] = ""
        self.parent.config["front_camera_id" if side == "front" else "back_camera_id"] = ""
        triggers = self.parent.config.setdefault("doorbell_triggers", {})
        current = triggers.get(side, {}) if isinstance(triggers.get(side), dict) else {}
        current.update({
            "rtsp_url": "",
            "camera_id": "",
            "mqtt_topic": "",
            "enabled": False,
        })
        triggers[side] = current

    def on_save_wizard_camera_streams(self, event):
        if not getattr(self, "_wizard_stream_choices", None):
            self._refresh_wizard_camera_stream_controls()
        front = self._selected_wizard_stream(self.wizard_front_stream_choice)
        back = self._selected_wizard_stream(self.wizard_back_stream_choice)
        if not front and not back:
            self._set_step_status(
                "No camera streams are selected. Press Find And Test Doorbell Cameras, then choose at least one tested stream before saving.",
                announce=True,
            )
            return
        if front and back and front.get("rtsp_url") == back.get("rtsp_url"):
            self._set_step_status(
                "Front and back door camera streams cannot be the same stream. Choose a different stream for one door, or choose Do not use a camera stream for a door you do not use.",
                announce=True,
            )
            return
        if front:
            self.parent.config["rtsp_front"] = front["rtsp_url"]
            self._save_stream_to_trigger("front", front)
            self._wizard_saved_stream_urls.add(front["rtsp_url"])
            self._wizard_camera_test_status["front"] = {"ok": True, "rtsp_url": front["rtsp_url"], "message": "Passed during Ring-MQTT stream discovery."}
        else:
            self._clear_stream_for_trigger("front")
            self._wizard_camera_test_status.pop("front", None)
        if back:
            self.parent.config["rtsp_back"] = back["rtsp_url"]
            self._save_stream_to_trigger("back", back)
            self._wizard_saved_stream_urls.add(back["rtsp_url"])
            self._wizard_camera_test_status["back"] = {"ok": True, "rtsp_url": back["rtsp_url"], "message": "Passed during Ring-MQTT stream discovery."}
        else:
            self._clear_stream_for_trigger("back")
            self._wizard_camera_test_status.pop("back", None)
        if hasattr(self.parent, "save_config"):
            self.parent.save_config()
        if hasattr(self.parent, "refresh_setup_checklist"):
            self.parent.refresh_setup_checklist()
        self._session_completed_actions.add("live_streams")
        saved = []
        if front:
            saved.append(f"front: {front.get('name') or front.get('rtsp_url')}")
        if back:
            saved.append(f"back: {back.get('name') or back.get('rtsp_url')}")
        self._set_step_status(
            "Camera stream selection saved. "
            + "; ".join(saved)
            + ". Continue To Confirm Doorbell Triggers is now available.",
            announce=True,
        )
        self._render()

    def on_test_wizard_camera(self, event, side):
        side = "back" if side == "back" else "front"
        selected = self._selected_wizard_stream(self.wizard_back_stream_choice if side == "back" else self.wizard_front_stream_choice)
        rtsp_url = (selected or {}).get("rtsp_url") or self._configured_stream_url(side)
        if not rtsp_url:
            self._set_step_status(
                f"{side.title()} door camera is not selected or saved yet. Choose a tested Ring-MQTT stream on this page, then press Save Selected Camera Streams.",
                announce=True,
            )
            return
        self._set_busy(True)
        self._set_step_status(f"Testing {side} doorbell camera from the setup wizard. Viper is checking for a live video frame.", announce=True)
        safe_submit(self._run_wizard_camera_test, side, rtsp_url)

    def _run_wizard_camera_test(self, side, rtsp_url):
        started = time.perf_counter()
        try:
            test_dir = cfg.DATA_DIR / "rtsp_test"
            test_dir.mkdir(parents=True, exist_ok=True)
            min_bytes = cfg.BACK_MIN_FRAME_BYTES if side == "back" else cfg.FRONT_MIN_FRAME_BYTES
            frame = vision.grab_frame(rtsp_url, test_dir, f"wizard_{side}", min_bytes=min_bytes, timeout=8)
            result = {
                "ok": bool(frame),
                "frame": frame,
                "rtsp_url": rtsp_url,
                "message": "Frame captured." if frame else "No live frame was captured before the timeout.",
                "elapsed": time.perf_counter() - started,
            }
        except Exception as e:
            result = {
                "ok": False,
                "rtsp_url": rtsp_url,
                "message": str(e),
                "elapsed": time.perf_counter() - started,
            }
        wx.CallAfter(self._finish_wizard_camera_test, side, result)

    def _finish_wizard_camera_test(self, side, result):
        self._set_busy(False)
        self._wizard_camera_test_status[side] = dict(result or {})
        elapsed = result.get("elapsed")
        elapsed_text = f" in {elapsed:.1f} seconds" if isinstance(elapsed, (int, float)) else ""
        label = side.title()
        if result.get("ok"):
            url = result.get("rtsp_url") or ""
            if url:
                self._wizard_saved_stream_urls.add(url)
            frame = result.get("frame") or ""
            message = f"{label} doorbell camera test passed{elapsed_text}. Viper captured a live frame."
            if frame:
                message += f"\nFrame saved at {frame}."
        else:
            message = (
                f"{label} doorbell camera test failed{elapsed_text}. "
                f"{result.get('message') or 'No live frame was captured.'}\n"
                f"URL tested: {result.get('rtsp_url') or ''}"
            )
        self._set_step_status(message + "\n\n" + self._saved_camera_stream_status(), announce=True)
        self._render()

    def _save_stream_to_trigger(self, side, item):
        triggers = self.parent.config.setdefault("doorbell_triggers", {})
        current = triggers.get(side, {}) if isinstance(triggers.get(side), dict) else {}
        stream = item.get("stream") or {}
        current.update({
            "enabled": bool(current.get("trigger_entity_id") and item.get("rtsp_url")),
            "source": "ha_state",
            "active_states": list(ha_listener.DEFAULT_ACTIVE_STATES),
            "rtsp_url": item.get("rtsp_url") or "",
            "camera_id": stream.get("camera_id") or item.get("camera_id") or current.get("camera_id", ""),
            "mqtt_topic": stream.get("topic") or item.get("topic") or current.get("mqtt_topic", ""),
        })
        triggers[side] = current

    def _start_wizard_speaker_discovery(self):
        settings = self._wizard_settings()
        self._set_busy(True)
        self._set_step_status(
            "Discovering available speakers inside the wizard. Viper will show real checkboxes here; new speakers start unchecked.",
            announce=True,
        )
        safe_submit(self._run_wizard_speaker_discovery, settings)

    def _run_wizard_speaker_discovery(self, settings):
        ha_result = discovery.discover_ha_entities(
            ha_ip=settings.get("ha_ip") or None,
            ha_port=settings.get("ha_port") or None,
            token=settings.get("ha_token") or None,
            timeout=5,
        )
        ha_candidates = []
        ha_error = ""
        if ha_result.get("ok"):
            ha_candidates = self.parent._ha_speaker_candidates_from_result(ha_result)
        else:
            ha_error = ha_result.get("message") or "Home Assistant speaker discovery failed."

        sonos_candidates = []
        sonos_error = ""
        try:
            sonos_candidates = self.parent._sonos_speaker_candidates_from_soco(soco.discover())
        except Exception as e:
            sonos_error = f"Network Sonos discovery failed: {e}"
        wx.CallAfter(self._finish_wizard_speaker_discovery, ha_candidates, sonos_candidates, ha_error, sonos_error)

    def _finish_wizard_speaker_discovery(self, ha_candidates, sonos_candidates, ha_error="", sonos_error=""):
        self._set_busy(False)
        targets = self.parent._flatten_discovered_speaker_targets(ha_candidates, sonos_candidates)
        self._populate_wizard_speaker_checks(targets)
        summary = self.parent._discovered_speaker_summary_text(ha_candidates, sonos_candidates, ha_error, sonos_error)
        self._set_step_status(
            summary
            + "\n\nSpeaker discovery complete. Tab through each speaker checkbox. Press Space to check speakers to add, then press Save Selected Speakers.",
            announce=True,
        )
        self._render()

    def _populate_wizard_speaker_checks(self, targets):
        self._wizard_speaker_checks = []
        self._wizard_speaker_targets = list(targets or [])
        try:
            self.speaker_scroll_sizer.Clear(True)
        except Exception:
            pass
        if not targets:
            none = wx.StaticText(self.speaker_scroll, label="No speakers were found yet. Press Discover Available Speakers.")
            none.SetName("No speakers were found")
            self.speaker_scroll_sizer.Add(none, 0, wx.ALL | wx.EXPAND, 5)
        for item in targets or []:
            name = item.get("name") or "Unnamed speaker"
            spk_type = item.get("type") or "ha"
            spk_id = item.get("id") or ""
            source = item.get("source") or "discovery"
            configured = bool(item.get("configured"))
            label = f"{name}, {spk_type}, {spk_id}, {source}"
            if configured:
                label += ", already configured"
            check = wx.CheckBox(self.speaker_scroll, label=label)
            check.SetName(label)
            check.SetToolTip(label)
            check.SetValue(False)
            check.Enable(not configured)
            check._viper_speaker_target = item
            self._wizard_speaker_checks.append(check)
            self.speaker_scroll_sizer.Add(check, 0, wx.ALL | wx.EXPAND, 4)
        self.speaker_scroll.Layout()
        self.speaker_panel.Layout()
        self.Layout()

    def on_save_wizard_speakers(self, event):
        selected = [
            check._viper_speaker_target
            for check in self._wizard_speaker_checks
            if check.IsEnabled() and check.GetValue()
        ]
        if not selected:
            if self._has_required_speaker_routes():
                self._set_step_status("No new speakers were selected. Existing speaker routes are already saved, so Continue To AI And Speech is available.", announce=True)
                self._render()
                return
            self._set_step_status("No speakers are checked yet, or existing speakers do not have the needed alert routes. Tab through the speaker checkboxes and press Space on each speaker Viper should use.", announce=True)
            return
        routes = {
            "doorbell": self.wizard_route_doorbell_chk.GetValue(),
            "utilities": self.wizard_route_utilities_chk.GetValue(),
            "fridge": self.wizard_route_fridge_chk.GetValue(),
            "quiet_hours_exempt": self.wizard_route_quiet_exempt_chk.GetValue(),
        }
        added = self.parent._add_discovered_speaker_targets(selected, routes)
        self.parent.refresh_setup_checklist()
        if added:
            if self._has_required_speaker_routes():
                self._session_completed_actions.add("speakers_voice")
                self._set_step_status(f"Added {added} speaker target(s). Continue To AI And Speech is now available.", announce=True)
            else:
                self._set_step_status(f"Added {added} speaker target(s), but the needed alert routes are not all enabled yet. Keep doorbell, utility, and fridge or freezer routing checked, then save again.", announce=True)
        else:
            self._set_step_status("No new speakers were added. They may already be configured.", announce=True)
        self._render()

    def _checked_wizard_speaker_targets(self):
        return [
            check._viper_speaker_target
            for check in getattr(self, "_wizard_speaker_checks", []) or []
            if check.IsEnabled() and check.GetValue()
        ]

    def _saved_wizard_speaker_targets(self):
        targets = []
        speakers = self.parent.config.get("speakers", {})
        if not isinstance(speakers, dict):
            return targets
        for name, data in speakers.items():
            if not isinstance(data, dict) or not data.get("enabled", True):
                continue
            targets.append({
                "name": name,
                "id": data.get("id", ""),
                "type": data.get("type", "ha"),
                "source": "Saved speakers",
                "configured": True,
            })
        return targets

    def on_test_wizard_speakers(self, event):
        targets = self._checked_wizard_speaker_targets()
        source = "checked"
        if not targets:
            targets = self._saved_wizard_speaker_targets()
            source = "saved"
        if not targets:
            self._set_step_status(
                "No speakers are checked or saved yet. Press Discover Available Speakers, check at least one speaker, and press Save Selected Speakers.",
                announce=True,
            )
            return
        self._set_step_status(f"Sending a setup test announcement to {len(targets)} {source} speaker target(s).", announce=True)
        safe_submit(self._run_wizard_speaker_tests, targets, source)

    def _run_wizard_speaker_tests(self, targets, source):
        message = "Viper speaker setup test."
        results = []
        for target in targets or []:
            name = target.get("name") or target.get("id") or "speaker"
            spk_type = target.get("type") or "ha"
            spk_id = target.get("id") or ""
            if not spk_id:
                results.append({"name": name, "ok": False, "message": "Speaker target has no ID."})
                continue
            try:
                audio.announce_specific_speaker(spk_type, spk_id, message)
                results.append({"name": name, "ok": True, "message": "Test announcement sent."})
            except Exception as e:
                logging.exception("[SETUP WIZARD] Speaker test failed name=%s type=%s id=%s", name, spk_type, spk_id)
                results.append({"name": name, "ok": False, "message": str(e)})
        wx.CallAfter(self._finish_wizard_speaker_tests, results, source)

    def _finish_wizard_speaker_tests(self, results, source):
        passed = [item for item in results if item.get("ok")]
        failed = [item for item in results if not item.get("ok")]
        lines = [f"Speaker test finished. {len(passed)} sent, {len(failed)} failed. Source: {source} speakers."]
        for item in results:
            status = "sent" if item.get("ok") else "failed"
            lines.append(f"- {item.get('name')}: {status}. {item.get('message') or ''}")
        if passed and self._has_required_speaker_routes():
            self._session_completed_actions.add("speakers_voice")
            lines.append("Speaker routes are saved. Continue To AI And Speech is available.")
        elif passed:
            lines.append("The test was sent. Press Save Selected Speakers if these are new speakers, and make sure doorbell, utility, and fridge/freezer routes are checked.")
        self._set_step_status("\n".join(lines), announce=True)
        self._render()

    def _open_home_assistant_path(self, path):
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        host = str(ha_settings.get("ha_ip") or "").strip()
        port = str(ha_settings.get("ha_port") or "8123").strip()
        if not host:
            return False
        if host.startswith("http://") or host.startswith("https://"):
            base = host.rstrip("/")
        else:
            base = f"http://{host}:{port}".rstrip("/")
        return open_url(base + "/" + str(path or "").lstrip("/"))

    def _tell_live_stream_next_step(self):
        self.checklist_txt.SetValue(
            "Opened Doorbell Vision setup.\n\n"
            "Press Find Ring MQTT Streams Now there. Viper will scan Ring-MQTT logs and topics, test the streams it finds, and let you assign working streams to the front and back doors."
        )

    def on_optional_fridge(self, event):
        self._open_product_area("Home Devices", "Refrigerator & Ice")
        self.checklist_txt.SetValue(
            "Mini-wizard: Refrigerator and freezer alerts\n\n"
            "1. Choose fridge/freezer open and closed behavior: chime-only is safest at first.\n"
            "2. Pick each chime from the channel controls.\n"
            "3. Press the fridge/freezer chime test buttons.\n"
            "4. Configure water filter and ice maker options if you want those spoken checks.\n"
            "5. Return to Diagnostics and run the Safe Smoke Test if anything feels off."
        )

    def on_optional_vacuum(self, event):
        self._open_product_area("Home Devices", "Robot Vacuum")
        self.checklist_txt.SetValue(
            "Mini-wizard: Robot vacuum controls\n\n"
            "1. Press Refresh vacuum controls so Viper reads current Home Assistant entities.\n"
            "2. Choose the vacuum entity if more than one is available.\n"
            "3. Load rooms before saving room shortcuts.\n"
            "4. Pick status message behavior for cleaning, returning, washing, emptying, and drying events.\n"
            "5. Test only safe commands first, such as refresh/status or dock-related controls."
        )

    def _require_home_assistant_ready(self, message):
        ha_settings = cfg.get_ha_settings(self.parent.config, include_env=True)
        if ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
            return True
        self.checklist_txt.SetValue(message)
        try:
            self.instructions_txt.SetValue(message)
        except Exception:
            pass
        return False

    def on_install_home_assistant(self, event):
        self.page_index = next(
            (index for index, page in enumerate(self.PAGES) if page.get("action") == "ha_connect"),
            self.page_index,
        )
        self._set_step_status(
            "Home Assistant install is part of this wizard now. Use Check This PC, Install VirtualBox, Install Home Assistant, and Start Or Wait For Home Assistant on this page.",
            announce=True,
        )
        self._render()
        try:
            self.btn_wizard_check_pc.SetFocusFromKbd()
        except Exception:
            try:
                self.btn_wizard_check_pc.SetFocus()
            except Exception:
                pass

    def _open_product_area(self, top_page, nested_page=None):
        owner = getattr(self, "parent", None)
        if owner is None or not hasattr(owner, "notebook"):
            return
        try:
            if hasattr(owner, "_show_control_panel_for_setup_action"):
                owner._show_control_panel_for_setup_action()
            for index in range(owner.notebook.GetPageCount()):
                if owner.notebook.GetPageText(index) == top_page:
                    selector = getattr(owner, "_select_book_page", None)
                    if callable(selector):
                        selector(owner.notebook, index)
                    else:
                        owner.notebook.SetSelection(index)
                    break
            nested = None
            if top_page == "Speakers & Audio":
                nested = getattr(owner, "audio_notebook", None)
            elif top_page == "Home Devices":
                nested = getattr(owner, "devices_notebook", None)
            elif top_page == "Diagnostics":
                nested = getattr(owner, "diagnostics_notebook", None)
            if nested is not None and nested_page:
                for index in range(nested.GetPageCount()):
                    if nested.GetPageText(index) == nested_page:
                        selector = getattr(owner, "_select_book_page", None)
                        if callable(selector):
                            selector(nested, index)
                        else:
                            nested.SetSelection(index)
                        break
            owner.Show(True)
            owner.Raise()
        except Exception:
            logging.debug("Could not open product area %s / %s from setup wizard.", top_page, nested_page, exc_info=True)

    def on_close(self, event):
        owner = getattr(self, "parent", None)
        try:
            if owner is not None:
                if getattr(owner, "_setup_wizard_dialog", None) is self:
                    owner._setup_wizard_dialog = None
                wx.CallAfter(owner.refresh_setup_checklist)
                wx.CallAfter(owner._leave_setup_window_mode)
        except Exception:
            logging.debug("Could not refresh setup after closing setup wizard.", exc_info=True)
        self.Destroy()


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
        self.setup_events = []
        self.last_setup_status = ""
        self.last_video_analysis = {}
        self._last_focus_snapshot_log = {}
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
        self._ha_address_recovery_stop = threading.Event()
        threading.Thread(target=self._ha_address_recovery_worker, name="ViperHAAddressRecovery", daemon=True).start()

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
        config = cfg.load_config()
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
        if hasattr(event, "Skip"):
            event.Skip()

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
        self.tab_vacuum = wx.ScrolledWindow(self.devices_notebook)
        self.tab_vacuum.SetScrollRate(0, 20)
        self.tab_setup = wx.Panel(self.notebook)
        self.tab_diagnostics_shell = wx.Panel(self.notebook)
        self.diagnostics_notebook = wx.Notebook(self.tab_diagnostics_shell)
        self.tab_diagnostics_overview = wx.Panel(self.diagnostics_notebook)
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

        audio_sizer = wx.BoxSizer(wx.VERTICAL)
        self.audio_notebook.AddPage(self.tab_tts, "Voice Behavior")
        self.audio_notebook.AddPage(self.tab_dev, "Speakers & Chimes")
        audio_sizer.Add(self.audio_notebook, 1, wx.EXPAND)
        self.tab_audio_shell.SetSizer(audio_sizer)

        devices_sizer = wx.BoxSizer(wx.VERTICAL)
        self.devices_notebook.AddPage(self.tab_fridge, "Refrigerator & Ice")
        self.devices_notebook.AddPage(self.tab_vacuum, "Robot Vacuum")
        devices_sizer.Add(self.devices_notebook, 1, wx.EXPAND)
        self.tab_devices_shell.SetSizer(devices_sizer)

        diagnostics_sizer = wx.BoxSizer(wx.VERTICAL)
        self.diagnostics_notebook.AddPage(self.tab_diagnostics_overview, "Tests & Support")
        self.diagnostics_notebook.AddPage(self.tab_speed, "Speed")
        self.diagnostics_notebook.AddPage(self.tab_ha_status, "Home Assistant Status")
        diagnostics_sizer.Add(self.diagnostics_notebook, 1, wx.EXPAND)
        self.tab_diagnostics_shell.SetSizer(diagnostics_sizer)

        self.setup_hidden_ai_voice_compat_controls()
        self.setup_dash_tab()
        self.setup_doorbell_tab()
        self.setup_prompt_editor_tab()
        self.setup_setup_tab()
        self.setup_tts_config_tab()
        self.setup_devices_tab()
        self.setup_diagnostics_tab()
        self.setup_utils_tab()
        self.setup_fridge_tab()
        self.setup_vacuum_tab()
        self.setup_speed_tab()
        self.setup_ha_status_tab()

        self.main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)

    def setup_hidden_ai_voice_compat_controls(self):
        def hide_disabled(control):
            control.Hide()
            control.Enable(False)

        self.voice_list = audio.get_available_windows_voices()
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

    def setup_dash_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.btn_arm = wx.Button(self.tab_dash, label="Disarm System" if self.is_armed else "Arm System", size=(-1, 60))
        font = self.btn_arm.GetFont()
        font.SetPointSize(14)
        self.btn_arm.SetFont(font)
        self.btn_arm.Bind(wx.EVT_BUTTON, self.on_toggle_arm)
        sizer.Add(self.btn_arm, 0, wx.ALL | wx.EXPAND, 15)

        mute_box = wx.StaticBox(self.tab_dash, label="Global Mute")
        mute_sizer = wx.StaticBoxSizer(mute_box, wx.VERTICAL)
        self.global_mute_chk = wx.CheckBox(self.tab_dash, label="Mute all Viper audio")
        self.global_mute_chk.SetValue(self.config.get("global_mute", False))
        self.global_mute_chk.Bind(wx.EVT_CHECKBOX, self.on_global_mute_change)
        self._describe_control(
            self.global_mute_chk,
            "Global mute checkbox. When checked, Viper logs events but suppresses all chimes, TTS, speaker tests, broadcasts, doorbell audio, and Viper status speech.",
        )
        self.global_mute_status_txt = wx.StaticText(self.tab_dash, label=self._global_mute_status_label())
        self._describe_control(
            self.global_mute_status_txt,
            "Global mute status. Tells whether Viper audio output is muted or active.",
        )
        mute_sizer.Add(self.global_mute_chk, 0, wx.ALL, 5)
        mute_sizer.Add(self.global_mute_status_txt, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(mute_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 15)

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

    def setup_doorbell_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = AccessibleStatusText(
            self.tab_doorbell,
            value=(
                "Doorbell Vision is where front and back door monitoring comes together.\n\n"
                "Use the setup button to choose Home Assistant trigger entities, Ring-MQTT RTSP streams, and camera tests. "
                "Use the full-flow test buttons to verify chime, live video capture, AI vision, and speech."
            ),
            size=(-1, 120),
        )
        self._describe_control(
            intro,
            "Doorbell Vision introduction. Overview of front and back door monitoring setup and tests.",
        )
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        doorbell_box = wx.StaticBox(self.tab_doorbell, label="Doorbell Setup And Tests")
        doorbell_sizer = wx.StaticBoxSizer(doorbell_box, wx.VERTICAL)
        self.btn_doorbell_setup = wx.Button(self.tab_doorbell, label="Set Up Doorbell Triggers And Cameras", size=(-1, 44))
        self.btn_doorbell_test_front_flow = wx.Button(self.tab_doorbell, label="Test Front Doorbell Full Flow", size=(-1, 44))
        self.btn_doorbell_test_back_flow = wx.Button(self.tab_doorbell, label="Test Back Doorbell Full Flow", size=(-1, 44))
        self.btn_doorbell_setup.Bind(wx.EVT_BUTTON, self.on_open_setup_wizard)
        self.btn_doorbell_test_front_flow.Bind(wx.EVT_BUTTON, lambda event: self.on_test_doorbell_full_flow(event, "front"))
        self.btn_doorbell_test_back_flow.Bind(wx.EVT_BUTTON, lambda event: self.on_test_doorbell_full_flow(event, "back"))
        descriptions = {
            self.btn_doorbell_setup: "Set Up Doorbell Triggers And Cameras button. Opens the guided setup wizard for trigger entities and live RTSP streams.",
            self.btn_doorbell_test_front_flow: "Test Front Doorbell Full Flow button. Runs the complete front doorbell path through Home Assistant, RTSP capture, AI vision, and speech.",
            self.btn_doorbell_test_back_flow: "Test Back Doorbell Full Flow button. Runs the complete back doorbell path through Home Assistant, RTSP capture, AI vision, and speech.",
        }
        for button, description in descriptions.items():
            self._describe_control(button, description)
            doorbell_sizer.Add(button, 0, wx.ALL | wx.EXPAND, 6)
        sizer.Add(doorbell_sizer, 0, wx.ALL | wx.EXPAND, 10)

        video_box = wx.StaticBox(self.tab_doorbell, label="Doorbell Video Analysis")
        video_sizer = wx.StaticBoxSizer(video_box, wx.VERTICAL)
        video_intro = AccessibleStatusText(
            self.tab_doorbell,
            value=(
                "Choose how much video Viper sends to Gemini after a doorbell event.\n"
                "Fast mode is still image only. Smart mode speaks the fast still image first, then sends a short video only when the first answer is unclear or missing useful detail. "
                "Detailed mode sends a short video after every alert. Manual mode only sends video when you press an analyze button."
            ),
            size=(-1, 105),
        )
        self._describe_control(
            video_intro,
            "Doorbell video analysis explanation. Description of Fast, Smart, Detailed, and Manual modes.",
        )
        video_sizer.Add(video_intro, 0, wx.ALL | wx.EXPAND, 5)

        settings = vision.normalize_video_analysis_settings(self.config)
        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        mode_lbl = wx.StaticText(self.tab_doorbell, label="Video analysis mode:")
        self.video_analysis_mode_choice = wx.Choice(
            self.tab_doorbell,
            choices=[vision.VIDEO_ANALYSIS_LABELS[key] for key in vision.VIDEO_ANALYSIS_MODES],
        )
        self.video_analysis_mode_choice.SetSelection(list(vision.VIDEO_ANALYSIS_MODES).index(settings["mode"]))
        self.video_analysis_mode_choice.Bind(wx.EVT_CHOICE, self.on_save_video_analysis_settings)
        mode_row.Add(mode_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        mode_row.Add(self.video_analysis_mode_choice, 1, wx.ALL | wx.EXPAND, 5)
        video_sizer.Add(mode_row, 0, wx.EXPAND)

        seconds_row = wx.BoxSizer(wx.HORIZONTAL)
        seconds_lbl = wx.StaticText(self.tab_doorbell, label="Manual video length in seconds:")
        self.manual_video_seconds_spin = wx.SpinCtrl(
            self.tab_doorbell,
            min=2,
            max=settings["max_manual_clip_seconds"],
            initial=settings["manual_clip_seconds"],
        )
        self.manual_video_seconds_spin.Bind(wx.EVT_SPINCTRL, self.on_save_video_analysis_settings)
        self.manual_video_seconds_spin.Bind(wx.EVT_TEXT, self.on_save_video_analysis_settings)
        seconds_row.Add(seconds_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        seconds_row.Add(self.manual_video_seconds_spin, 0, wx.ALL, 5)
        video_sizer.Add(seconds_row, 0, wx.EXPAND)

        self.video_analysis_status_txt = AccessibleStatusText(
            self.tab_doorbell,
            value=self._video_analysis_summary_text(),
            size=(-1, 100),
        )
        video_sizer.Add(self.video_analysis_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        video_buttons = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        video_buttons.AddGrowableCol(0, 1)
        video_buttons.AddGrowableCol(1, 1)
        self.btn_analyze_front_video = wx.Button(self.tab_doorbell, label="Analyze Front Camera Video Now", size=(-1, 44))
        self.btn_analyze_back_video = wx.Button(self.tab_doorbell, label="Analyze Back Camera Video Now", size=(-1, 44))
        self.btn_analyze_front_video.Bind(wx.EVT_BUTTON, lambda event: self.on_analyze_doorbell_video(event, "front"))
        self.btn_analyze_back_video.Bind(wx.EVT_BUTTON, lambda event: self.on_analyze_doorbell_video(event, "back"))
        for control, description in {
            self.video_analysis_mode_choice: "Video analysis mode picker. Fast is still image only. Smart sends bounded video only when the first answer is unclear. Detailed sends video every alert. Manual sends video only when you press an analyze button.",
            self.manual_video_seconds_spin: "Manual video length in seconds. Controls how much live camera video Viper uploads when you press Analyze Camera Video Now.",
            self.video_analysis_status_txt: "Doorbell video analysis status. Latest mode and latest video result.",
            self.btn_analyze_front_video: "Analyze Front Camera Video Now button. Captures the front camera for the manual video length, sends it to Gemini, and speaks what is happening outside.",
            self.btn_analyze_back_video: "Analyze Back Camera Video Now button. Captures the back camera for the manual video length, sends it to Gemini, and speaks what is happening outside.",
        }.items():
            self._describe_control(control, description)
        video_buttons.Add(self.btn_analyze_front_video, 1, wx.ALL | wx.EXPAND, 5)
        video_buttons.Add(self.btn_analyze_back_video, 1, wx.ALL | wx.EXPAND, 5)
        video_sizer.Add(video_buttons, 0, wx.EXPAND)
        sizer.Add(video_sizer, 0, wx.ALL | wx.EXPAND, 10)

        chime_box = wx.StaticBox(self.tab_doorbell, label="Instant Doorbell Chimes")
        chime_sizer = wx.StaticBoxSizer(chime_box, wx.VERTICAL)
        front_sizer = wx.BoxSizer(wx.HORIZONTAL)
        front_lbl = wx.StaticText(self.tab_doorbell, label="Front door chime:")
        self.front_chime_choice = wx.Choice(self.tab_doorbell)
        self.btn_test_front = wx.Button(self.tab_doorbell, label="Test Front Door Chime")
        self.btn_test_front.Bind(wx.EVT_BUTTON, self.on_test_front)
        front_sizer.Add(front_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        front_sizer.Add(self.front_chime_choice, 1, wx.ALL, 5)
        front_sizer.Add(self.btn_test_front, 0, wx.ALL, 5)
        back_sizer = wx.BoxSizer(wx.HORIZONTAL)
        back_lbl = wx.StaticText(self.tab_doorbell, label="Back door chime:")
        self.back_chime_choice = wx.Choice(self.tab_doorbell)
        self.btn_test_back = wx.Button(self.tab_doorbell, label="Test Back Door Chime")
        self.btn_test_back.Bind(wx.EVT_BUTTON, self.on_test_back)
        back_sizer.Add(back_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        back_sizer.Add(self.back_chime_choice, 1, wx.ALL, 5)
        back_sizer.Add(self.btn_test_back, 0, wx.ALL, 5)
        chime_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_chimes = wx.Button(self.tab_doorbell, label="Refresh Chime Folder")
        self.btn_save_chimes = wx.Button(self.tab_doorbell, label="Save Doorbell Chimes")
        self.btn_refresh_chimes.Bind(wx.EVT_BUTTON, self.on_refresh_chimes)
        self.btn_save_chimes.Bind(wx.EVT_BUTTON, self.on_save_chimes)
        chime_btn_sizer.Add(self.btn_refresh_chimes, 1, wx.ALL, 5)
        chime_btn_sizer.Add(self.btn_save_chimes, 1, wx.ALL, 5)
        for control, description in {
            self.front_chime_choice: "Front door chime picker. Choose the instant chime Viper plays for the front door.",
            self.back_chime_choice: "Back door chime picker. Choose the instant chime Viper plays for the back door.",
            self.btn_test_front: "Test Front Door Chime button. Plays the selected front door chime.",
            self.btn_test_back: "Test Back Door Chime button. Plays the selected back door chime.",
            self.btn_refresh_chimes: "Refresh Chime Folder button. Reloads available chime files from the chimes folder.",
            self.btn_save_chimes: "Save Doorbell Chimes button. Saves front and back door chime choices.",
        }.items():
            self._describe_control(control, description)
        chime_sizer.Add(front_sizer, 0, wx.EXPAND)
        chime_sizer.Add(back_sizer, 0, wx.EXPAND)
        chime_sizer.Add(chime_btn_sizer, 0, wx.EXPAND)
        sizer.Add(chime_sizer, 0, wx.ALL | wx.EXPAND, 10)
        self._populate_chimes()

        self.doorbell_summary_txt = AccessibleStatusText(
            self.tab_doorbell,
            value=self._doorbell_summary_text(),
            size=(-1, 220),
        )
        self._describe_control(
            self.doorbell_summary_txt,
            "Doorbell Vision status. Status of front and back trigger entities and RTSP URLs.",
        )
        sizer.Add(self.doorbell_summary_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        self.tab_doorbell.SetSizer(sizer)

    def _doorbell_summary_text(self):
        settings = cfg.get_doorbell_settings(self.config, include_env=True)
        front_rtsp = settings.get("configured_rtsp_front") or settings.get("raw_rtsp_front") or ""
        back_rtsp = settings.get("configured_rtsp_back") or settings.get("raw_rtsp_back") or ""
        return "\n".join(
            [
                "Doorbell Vision Status",
                "",
                f"Front trigger entity: {settings.get('front_trigger_entity_id') or 'not set'}",
                f"Front RTSP URL: {front_rtsp or 'not set'}",
                f"Back trigger entity: {settings.get('back_trigger_entity_id') or 'not set'}",
                f"Back RTSP URL: {back_rtsp or 'not set'}",
                "",
                "Next best action:",
                "Use Set Up Doorbell Triggers And Cameras if anything above says not set. Then run both full-flow tests.",
            ]
        )

    def setup_prompt_editor_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = AccessibleStatusText(
            self.tab_prompts,
            value=(
                "Choose what Viper should pay attention to.\n\n"
                "You do not need to edit AI instructions unless you choose Custom."
            ),
            size=(-1, 80),
        )
        self._describe_control(
            intro,
            "AI Descriptions introduction. Choose what Viper should pay attention to. You do not need to edit AI instructions unless you choose Custom.",
        )
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        desc_box = wx.StaticBox(self.tab_prompts, label="AI Description Settings")
        desc_sizer = wx.StaticBoxSizer(desc_box, wx.VERTICAL)
        self.ai_description_controls = {}
        styles = self.config.get("ai_description_styles", {})
        custom = self.config.get("ai_custom_descriptions", {})
        style_labels = list(AI_DESCRIPTION_STYLE_LABELS.values())
        for job, label, style_description, custom_description in AI_DESCRIPTION_JOBS:
            job_box = wx.StaticBox(self.tab_prompts, label=label)
            job_sizer = wx.StaticBoxSizer(job_box, wx.VERTICAL)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self.tab_prompts, label=f"{label} style:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            choice = wx.Choice(self.tab_prompts, choices=style_labels)
            style_key = styles.get(job, cfg.DEFAULT_AI_DESCRIPTION_STYLES.get(job, "balanced"))
            choice.SetStringSelection(AI_DESCRIPTION_STYLE_LABELS.get(style_key, "Balanced"))
            choice.Bind(wx.EVT_CHOICE, self.on_ai_description_style_change)
            self._describe_control(choice, style_description)
            row.Add(choice, 1, wx.ALL | wx.EXPAND, 5)
            job_sizer.Add(row, 0, wx.EXPAND)

            custom_label = wx.StaticText(self.tab_prompts, label=f"{label} custom AI instructions:")
            custom_editor = wx.TextCtrl(self.tab_prompts, style=wx.TE_MULTILINE, size=(-1, 95))
            custom_editor.SetValue(custom.get(job, ""))
            self._describe_control(custom_editor, custom_description)
            job_sizer.Add(custom_label, 0, wx.ALL, 5)
            job_sizer.Add(custom_editor, 0, wx.ALL | wx.EXPAND, 5)
            self.ai_description_controls[job] = {
                "choice": choice,
                "custom_label": custom_label,
                "custom_editor": custom_editor,
            }
            desc_sizer.Add(job_sizer, 0, wx.ALL | wx.EXPAND, 6)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_save_ai_descriptions = wx.Button(self.tab_prompts, label="Save AI Description Settings", size=(-1, 44))
        self.btn_reset_ai_descriptions = wx.Button(self.tab_prompts, label="Reset AI Descriptions To Recommended Settings", size=(-1, 44))
        self.btn_save_ai_descriptions.Bind(wx.EVT_BUTTON, self.on_save_ai_descriptions)
        self.btn_reset_ai_descriptions.Bind(wx.EVT_BUTTON, self.on_reset_ai_descriptions)
        self._describe_control(self.btn_save_ai_descriptions, "Save AI Description Settings button. Saves what Viper should pay attention to for each doorbell image and video situation.")
        self._describe_control(self.btn_reset_ai_descriptions, "Reset AI Descriptions To Recommended Settings button. Restores recommended description styles and clears custom AI instructions.")
        buttons.Add(self.btn_save_ai_descriptions, 1, wx.ALL | wx.EXPAND, 5)
        buttons.Add(self.btn_reset_ai_descriptions, 1, wx.ALL | wx.EXPAND, 5)
        desc_sizer.Add(buttons, 0, wx.EXPAND)
        sizer.Add(desc_sizer, 1, wx.ALL | wx.EXPAND, 10)

        self.prompt_status_txt = AccessibleStatusText(self.tab_prompts, value="AI description settings ready.", size=(-1, 70))
        self._describe_control(self.prompt_status_txt, "AI Description status. Reports saved AI description settings.")
        sizer.Add(self.prompt_status_txt, 0, wx.ALL | wx.EXPAND, 10)
        self.tab_prompts.SetSizer(sizer)
        self._sync_ai_description_custom_visibility()

    def _ai_description_style_key(self, choice):
        return AI_DESCRIPTION_STYLE_KEYS_BY_LABEL.get(choice.GetStringSelection(), "balanced")

    def _sync_ai_description_custom_visibility(self):
        controls = getattr(self, "ai_description_controls", {})
        for group in controls.values():
            show = self._ai_description_style_key(group["choice"]) == "custom"
            group["custom_label"].Show(show)
            group["custom_editor"].Show(show)
        try:
            self.tab_prompts.Layout()
            self.tab_prompts.FitInside()
        except Exception:
            logging.debug("Could not update AI description custom editor visibility.", exc_info=True)

    def on_ai_description_style_change(self, event):
        self._sync_ai_description_custom_visibility()
        if event:
            event.Skip()

    def on_save_ai_descriptions(self, event):
        styles = {}
        custom = {}
        for job, group in getattr(self, "ai_description_controls", {}).items():
            styles[job] = self._ai_description_style_key(group["choice"])
            custom[job] = group["custom_editor"].GetValue().strip()
        self.config["ai_description_styles"] = styles
        self.config["ai_custom_descriptions"] = custom
        self.save_config()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue("Saved AI description settings.")
        self.notify("AI description settings saved.", priority=10)

    def on_reset_ai_descriptions(self, event):
        self.config["ai_description_styles"] = dict(cfg.DEFAULT_AI_DESCRIPTION_STYLES)
        self.config["ai_custom_descriptions"] = {job: "" for job in cfg.AI_DESCRIPTION_JOBS}
        for job, group in getattr(self, "ai_description_controls", {}).items():
            style_key = self.config["ai_description_styles"].get(job, "balanced")
            group["choice"].SetStringSelection(AI_DESCRIPTION_STYLE_LABELS.get(style_key, "Balanced"))
            group["custom_editor"].SetValue("")
        self.save_config()
        self._sync_ai_description_custom_visibility()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue("Reset AI descriptions to recommended settings.")
        self.notify("AI descriptions reset to recommended settings.", priority=10)

    def _video_prompt_name_for_mode(self, mode):
        defaults = cfg.get_default_config().get("doorbell_video_prompt_profiles", {})
        prompts = self.config.setdefault("video_prompts", {})
        profiles = self.config.setdefault("doorbell_video_prompt_profiles", {})
        selected = profiles.get(mode) or defaults.get(mode) or self.config.get("active_video_prompt") or next(iter(prompts), "")
        if selected not in prompts:
            selected = next(iter(prompts), "")
            profiles[mode] = selected
        return selected

    def _video_prompt_text_for_mode(self, mode):
        name = self._video_prompt_name_for_mode(mode)
        return self.config.get("video_prompts", {}).get(name, "")

    def _video_analysis_summary_text(self):
        settings = vision.normalize_video_analysis_settings(self.config)
        mode_names = {
            "fast": "Fast",
            "smart": "Smart",
            "detailed": "Detailed",
            "manual": "Manual",
        }
        mode_descriptions = {
            "fast": "Still image only. Viper does not automatically upload video.",
            "smart": "Viper speaks the still image first, then uploads a 3 second video only if the answer is unclear or missing useful detail.",
            "detailed": "Viper speaks the still image first, then uploads a 5 second video after every doorbell alert.",
            "manual": "Viper uploads video only when you press an Analyze Camera Video Now button.",
        }
        mode = settings["mode"]
        last_lines = []
        for side in ("front", "back"):
            entry = self.last_video_analysis.get(side, {}) if hasattr(self, "last_video_analysis") else {}
            if entry:
                result_text = entry.get("description", "")
                if entry.get("incomplete"):
                    result_text = f"Gemini returned an incomplete answer: {result_text}"
                last_lines.append(
                    f"Last {side} video from {entry.get('source', 'unknown')}: {result_text} "
                    f"It took {entry.get('elapsed', 0):.1f} seconds."
                )
        if not last_lines:
            last_lines.append("No video analysis has run yet.")
        smart_line = "Smart rules are inactive right now."
        if mode == "smart":
            smart_line = "Smart rules active: 3 second clip, 2 frames per second, at most one video follow-up per camera per minute."
        return "\n".join(
            [
                f"Mode: {mode_names.get(mode, mode.title())}.",
                f"What this mode does: {mode_descriptions.get(mode, '')}",
                smart_line,
                f"Manual Analyze Camera Video Now buttons upload {settings['manual_clip_seconds']} seconds.",
                "",
                *last_lines,
            ]
        )

    def _refresh_video_analysis_controls(self):
        if not hasattr(self, "video_analysis_mode_choice"):
            return
        settings = vision.normalize_video_analysis_settings(self.config)
        try:
            self.video_analysis_mode_choice.SetSelection(list(vision.VIDEO_ANALYSIS_MODES).index(settings["mode"]))
            self.manual_video_seconds_spin.SetRange(2, settings["max_manual_clip_seconds"])
            self.manual_video_seconds_spin.SetValue(settings["manual_clip_seconds"])
            self.video_analysis_status_txt.SetValue(self._video_analysis_summary_text())
        except Exception:
            logging.debug("Could not refresh video analysis controls.", exc_info=True)

    def on_save_video_analysis_settings(self, event):
        if not hasattr(self, "video_analysis_mode_choice"):
            if event:
                event.Skip()
            return
        current = vision.normalize_video_analysis_settings(self.config)
        selection = self.video_analysis_mode_choice.GetSelection()
        mode = vision.VIDEO_ANALYSIS_MODES[selection] if 0 <= selection < len(vision.VIDEO_ANALYSIS_MODES) else current["mode"]
        manual_seconds = vision.clamp_manual_video_seconds(self.manual_video_seconds_spin.GetValue(), self.config)
        settings = dict(current)
        settings["mode"] = mode
        settings["manual_clip_seconds"] = manual_seconds
        self.config["doorbell_video_analysis"] = settings
        self.save_config()
        self.video_analysis_status_txt.SetValue(self._video_analysis_summary_text())
        self.notify(
            f"Doorbell video mode saved. {vision.VIDEO_ANALYSIS_LABELS.get(mode, mode)}. Manual video is {manual_seconds} seconds.",
            priority=10,
        )
        if event:
            event.Skip()

    def on_analyze_doorbell_video(self, event, side):
        settings = vision.normalize_video_analysis_settings(self.config)
        seconds = vision.clamp_manual_video_seconds(
            self.manual_video_seconds_spin.GetValue() if hasattr(self, "manual_video_seconds_spin") else settings["manual_clip_seconds"],
            self.config,
        )
        label = "back" if side == "back" else "front"
        self.notify(f"Analyzing {label} camera video for {seconds} seconds.", priority=10)
        safe_submit(self._run_manual_doorbell_video_analysis, label, seconds, "desktop app")

    def _run_manual_doorbell_video_analysis(self, side, seconds=None, source="desktop app"):
        side = "back" if side == "back" else "front"
        rtsp_url = _doorbell_rtsp_for_key(side)
        if not rtsp_url:
            message = f"No RTSP URL is configured for the {side} camera."
            wx.CallAfter(self.notify, message, 10)
            self.record_video_analysis_result(side, message, {"ok": False, "elapsed": 0.0}, source=source)
            return
        seconds = vision.clamp_manual_video_seconds(seconds, self.config)
        prompt = cfg.get_doorbell_video_prompt(
            self.config,
            "manual",
            location=f"{side} door",
            side=side,
        )
        logging.info("[VIDEO ANALYSIS] manual_start side=%s seconds=%s source=%s", side, seconds, source)
        result = vision.analyze_rtsp_video(
            rtsp_url,
            side=side,
            seconds=seconds,
            prompt=prompt,
            config_data=self.config,
            trace_id=f"manual-{side}-{int(time.time() * 1000)}",
        )
        description = result.get("description") or "Video analysis did not return a description."
        self.record_video_analysis_result(side, description, result, source=source)
        wx.CallAfter(self.notify, f"{side.title()} camera video: {description}", 1, True)
        audio.play_notification("doorbell", f"{side.title()} camera video: {description}")

    def record_video_analysis_result(self, side, description, result=None, source="unknown"):
        if not hasattr(self, "last_video_analysis"):
            self.last_video_analysis = {}
        result = result or {}
        entry = {
            "side": "back" if side == "back" else "front",
            "description": description,
            "source": source,
            "elapsed": float(result.get("elapsed") or 0.0),
            "ok": bool(result.get("ok", True)),
            "incomplete": vision._looks_like_cut_off_video_response(description),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        self.last_video_analysis[entry["side"]] = entry
        logging.info("[VIDEO ANALYSIS] result side=%s source=%s ok=%s elapsed=%.2fs text=%r", entry["side"], source, entry["ok"], entry["elapsed"], description)
        if hasattr(self, "video_analysis_status_txt"):
            wx.CallAfter(self.video_analysis_status_txt.SetValue, self._video_analysis_summary_text())

    def setup_setup_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = AccessibleStatusText(
            self.tab_setup,
            value=(
                "Setup is the guided place for getting Viper working.\n\n"
                "Use Open Setup Wizard first. It walks through Home Assistant, Ring, live video, speakers, AI speech, and final testing in order. Refrigerator and robot vacuum setup come after the core doorbell path works."
            ),
            size=(-1, 90),
        )
        self._describe_control(intro, "Setup introduction. Overview of the guided setup area.")
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        command_box = wx.StaticBox(self.tab_setup, label="Setup Status")
        command_sizer = wx.StaticBoxSizer(command_box, wx.VERTICAL)
        self.setup_next_action_txt = AccessibleStatusText(
            self.tab_setup,
            value=self.build_setup_next_action_summary(),
            size=(-1, 90),
        )
        self._describe_control(self.setup_next_action_txt, "Setup Status. Current readiness, last successful step, skipped optional features, and the next recommended action.")
        command_sizer.Add(self.setup_next_action_txt, 0, wx.ALL | wx.EXPAND, 5)

        command_grid = wx.FlexGridSizer(rows=0, cols=3, vgap=6, hgap=6)
        for col in range(3):
            command_grid.AddGrowableCol(col, 1)
        self.btn_continue_setup = wx.Button(self.tab_setup, label="Continue Setup", size=(-1, 40))
        self.btn_fix_current_setup = wx.Button(self.tab_setup, label="Fix Current Item", size=(-1, 40))
        self.btn_test_current_setup = wx.Button(self.tab_setup, label="Test Current Item", size=(-1, 40))
        self.btn_skip_optional_setup = wx.Button(self.tab_setup, label="Skip Optional Item", size=(-1, 40))
        self.btn_unskip_optional_setup = wx.Button(self.tab_setup, label="Restore Optional Items", size=(-1, 40))
        self.btn_backup_setup = wx.Button(self.tab_setup, label="Backup Setup", size=(-1, 40))
        self.btn_restore_setup = wx.Button(self.tab_setup, label="Restore Setup", size=(-1, 40))
        self.btn_continue_setup.Bind(wx.EVT_BUTTON, self.on_continue_setup)
        self.btn_fix_current_setup.Bind(wx.EVT_BUTTON, self.on_fix_current_setup_item)
        self.btn_test_current_setup.Bind(wx.EVT_BUTTON, self.on_test_current_setup_item)
        self.btn_skip_optional_setup.Bind(wx.EVT_BUTTON, self.on_skip_optional_setup_item)
        self.btn_unskip_optional_setup.Bind(wx.EVT_BUTTON, self.on_restore_optional_setup_items)
        self.btn_backup_setup.Bind(wx.EVT_BUTTON, self.on_backup_setup)
        self.btn_restore_setup.Bind(wx.EVT_BUTTON, self.on_restore_setup)
        for button, description in {
            self.btn_continue_setup: "Continue Setup button. Opens the guided setup wizard at the next recommended step.",
            self.btn_fix_current_setup: "Fix Current Item button. Opens the exact setup area for the first item that needs attention.",
            self.btn_test_current_setup: "Test Current Item button. Runs the safest relevant test for the current setup item.",
            self.btn_skip_optional_setup: "Skip Optional Item button. Marks the current optional feature as skipped for now.",
            self.btn_unskip_optional_setup: "Restore Optional Items button. Makes previously skipped optional setup items show as available again.",
            self.btn_backup_setup: "Backup Setup button. Exports non-secret setup settings to the Viper data folder.",
            self.btn_restore_setup: "Restore Setup button. Imports a setup backup and keeps secrets to be re-entered separately.",
        }.items():
            self._describe_control(button, description)
            command_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        command_sizer.Add(command_grid, 0, wx.EXPAND)

        recipe_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        recipe_grid.AddGrowableCol(0, 1)
        recipe_grid.AddGrowableCol(1, 1)
        self.btn_recipe_ha_token = wx.Button(self.tab_setup, label="Fix HA Token", size=(-1, 36))
        self.btn_recipe_ring_streams = wx.Button(self.tab_setup, label="Fix Ring-MQTT Streams", size=(-1, 36))
        self.btn_recipe_speakers = wx.Button(self.tab_setup, label="Fix Speaker Audio", size=(-1, 36))
        self.btn_recipe_doorbell = wx.Button(self.tab_setup, label="Fix Doorbell Events", size=(-1, 36))
        self.btn_recipe_camera = wx.Button(self.tab_setup, label="Fix Camera Frames", size=(-1, 36))
        self.btn_recipe_gemini = wx.Button(self.tab_setup, label="Fix Gemini Replies", size=(-1, 36))
        for key, button in (
            ("ha_token", self.btn_recipe_ha_token),
            ("ring_streams", self.btn_recipe_ring_streams),
            ("speakers", self.btn_recipe_speakers),
            ("doorbell", self.btn_recipe_doorbell),
            ("camera", self.btn_recipe_camera),
            ("gemini", self.btn_recipe_gemini),
        ):
            button.Bind(wx.EVT_BUTTON, lambda event, recipe=key: self.on_show_troubleshooting_recipe(event, recipe))
            self._describe_control(button, f"Troubleshooting recipe button for {key.replace('_', ' ')}.")
            recipe_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        command_sizer.Add(recipe_grid, 0, wx.EXPAND)
        sizer.Add(command_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.setup_checklist_txt = wx.TextCtrl(
            self.tab_setup,
            value=self.build_setup_checklist_summary(),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 340),
        )
        self._describe_control(
            self.setup_checklist_txt,
            "Setup checklist. Read-only status of Home Assistant, Ring-MQTT, RTSP, speakers, TTS, and diagnostics readiness.",
        )
        sizer.Add(self.setup_checklist_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        buttons = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        buttons.AddGrowableCol(0, 1)
        buttons.AddGrowableCol(1, 1)
        self.btn_setup_wizard = wx.Button(self.tab_setup, label="Open Setup Wizard", size=(-1, 44))
        self.btn_choose_setup_speakers = wx.Button(self.tab_setup, label="Choose Alert Speakers", size=(-1, 44))
        self.btn_refresh_setup_checklist = wx.Button(self.tab_setup, label="Refresh Setup Status", size=(-1, 44))
        self.btn_test_everything = wx.Button(self.tab_setup, label="Test Everything", size=(-1, 44))
        self.btn_setup_wizard.Bind(wx.EVT_BUTTON, self.on_open_setup_wizard)
        self.btn_choose_setup_speakers.Bind(wx.EVT_BUTTON, self.on_choose_setup_speakers)
        self.btn_refresh_setup_checklist.Bind(wx.EVT_BUTTON, lambda event: self.refresh_setup_checklist())
        self.btn_test_everything.Bind(wx.EVT_BUTTON, self.on_test_everything)
        for button, description in {
            self.btn_setup_wizard: "Open Setup Wizard button. Opens the beginner setup wizard for Home Assistant, Ring, live video, speakers, AI speech, and final testing.",
            self.btn_choose_setup_speakers: "Choose Alert Speakers button. Opens speaker discovery or the speaker list so you can choose which speakers Viper uses.",
            self.btn_refresh_setup_checklist: "Refresh Setup Checklist button. Updates the read-only checklist above.",
            self.btn_test_everything: "Test Everything button. Runs safe setup checks and diagnostics without changing settings.",
        }.items():
            self._describe_control(button, description)
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        self.tab_setup.SetSizer(sizer)
        self._update_main_setup_actions()

    def on_choose_setup_speakers(self, event):
        if not self.config.get("speakers"):
            self.on_discover_speakers(event)
            return
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(idx) is self.tab_audio_shell:
                self._select_book_page(self.notebook, idx)
                break
        if hasattr(self, "audio_notebook"):
            for idx in range(self.audio_notebook.GetPageCount()):
                if self.audio_notebook.GetPage(idx) is self.tab_dev:
                    self._select_book_page(self.audio_notebook, idx)
                    break
        wx.CallAfter(self.speaker_list.SetFocus)
        self.notify("Choose alert speakers. Use Spacebar to toggle each speaker, then choose routing for the selected speaker.", priority=10)

    def on_fix_tts_setup(self, event):
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(idx) is self.tab_audio_shell:
                self._select_book_page(self.notebook, idx)
                break
        if hasattr(self, "audio_notebook"):
            for idx in range(self.audio_notebook.GetPageCount()):
                if self.audio_notebook.GetPage(idx) is self.tab_tts:
                    self._select_book_page(self.audio_notebook, idx)
                    break
        wx.CallAfter(self.tts_engine_choice.SetFocus)
        self.notify("Opened Gemini and TTS setup. Choose a default engine and enter the Gemini API key if you want Gemini vision or Gemini TTS.", priority=10)

    def _select_top_page(self, page):
        if not hasattr(self, "notebook") or page is None:
            return
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(idx) is page:
                self._select_book_page(self.notebook, idx)
                return

    def _select_nested_page(self, notebook, page):
        if notebook is None or page is None:
            return
        for idx in range(notebook.GetPageCount()):
            if notebook.GetPage(idx) is page:
                self._select_book_page(notebook, idx)
                return

    def _open_devices_page(self, page_name):
        self._select_top_page(getattr(self, "tab_devices_shell", None))
        if page_name == "vacuum":
            self._select_nested_page(getattr(self, "devices_notebook", None), getattr(self, "tab_vacuum", None))
            wx.CallAfter(getattr(self, "btn_refresh_vacuum", self).SetFocus)
        else:
            self._select_nested_page(getattr(self, "devices_notebook", None), getattr(self, "tab_fridge", None))

    def _setup_skip_state(self):
        skips = self.config.get("setup_skips", {})
        if not isinstance(skips, dict):
            skips = {}
        return {
            "gemini": bool(skips.get("gemini", False)),
            "pushover": bool(skips.get("pushover", False)),
            "fridge": bool(skips.get("fridge", False)),
            "vacuum": bool(skips.get("vacuum", False)),
        }

    def _setup_readiness_items(self, live_result=None):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha = runtime["home_assistant"]
        api = runtime["api"]
        doorbell = runtime["doorbell"]
        speakers = runtime["speakers"]
        routes = speakers.get("routes", {})
        skips = self._setup_skip_state()
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        progress = _coerce_setup_progress_state(self.config.get("setup_progress", {}))
        front_rtsp = doorbell.get("configured_rtsp_front") or doorbell.get("raw_rtsp_front") or ""
        back_rtsp = doorbell.get("configured_rtsp_back") or doorbell.get("raw_rtsp_back") or ""
        ha_ready = bool(ha.get("ha_ip") and ha.get("ha_token"))
        triggers_ready = bool(doorbell.get("front_trigger_entity_id") and doorbell.get("back_trigger_entity_id"))
        streams_ready = bool(front_rtsp and back_rtsp)
        speaker_routes_ready = bool(speakers.get("enabled_count") and routes.get("doorbell") and routes.get("utilities") and routes.get("fridge"))
        gemini_ready = bool(api.get("gemini_api_key"))
        fridge_configured = any(
            bool((self.config.get("broadcast_channels", {}) or {}).get(key, {}).get("chime"))
            or bool((self.config.get("broadcast_channels", {}) or {}).get(key, {}).get("entity_id"))
            for key in ("fridge_open", "freezer_open")
        )
        vacuum_configured = bool(self.config.get("vacuum_entity") or self.config.get("vacuum_rooms") or self.config.get("cinderella_status_entity"))
        items = [
            {
                "key": "home_assistant",
                "label": "Home Assistant",
                "ok": ha_ready,
                "optional": False,
                "skipped": False,
                "detail": f"{ha.get('ha_ip')}:{ha.get('ha_port') or '8123'}" if ha_ready else "Needs host and long-lived token.",
                "fix": "Open Setup Wizard at Home Assistant connection.",
                "test": "Run Home Assistant connection test.",
            },
            {
                "key": "ring_mqtt",
                "label": "Ring-MQTT",
                "ok": streams_ready,
                "optional": False,
                "skipped": False,
                "detail": "RTSP streams are saved." if streams_ready else "Needed for Ring live video streams.",
                "fix": "Open Ring-MQTT stream discovery in the setup wizard.",
                "test": "Run camera frame tests after streams are saved.",
            },
            {
                "key": "doorbell_triggers",
                "label": "Doorbell triggers",
                "ok": triggers_ready,
                "optional": False,
                "skipped": False,
                "detail": f"front={doorbell.get('front_trigger_entity_id') or 'missing'}, back={doorbell.get('back_trigger_entity_id') or 'missing'}",
                "fix": "Choose front and back Home Assistant trigger entities.",
                "test": "Test Everything simulates event routing.",
            },
            {
                "key": "live_video",
                "label": "Live video",
                "ok": streams_ready,
                "optional": False,
                "skipped": False,
                "detail": f"front={'saved' if front_rtsp else 'missing'}, back={'saved' if back_rtsp else 'missing'}",
                "fix": "Find and save front and back RTSP streams.",
                "test": "Capture one frame from each stream.",
            },
            {
                "key": "speakers",
                "label": "Speakers",
                "ok": speaker_routes_ready,
                "optional": False,
                "skipped": False,
                "detail": f"{speakers.get('enabled_count', 0)} enabled; routes doorbell {len(routes.get('doorbell', []))}, utilities {len(routes.get('utilities', []))}, fridge/freezer {len(routes.get('fridge', []))}",
                "fix": "Open Choose Alert Speakers.",
                "test": "Send a manual speaker test.",
            },
            {
                "key": "gemini",
                "label": "Gemini and TTS",
                "ok": gemini_ready or skips["gemini"],
                "optional": True,
                "skipped": skips["gemini"],
                "detail": "Ready." if gemini_ready else ("Skipped for now." if skips["gemini"] else "Needed for Gemini vision and Gemini TTS."),
                "fix": "Open Gemini and voice behavior setup.",
                "test": "Run diagnostics and Test Everything.",
            },
            {
                "key": "pushover",
                "label": "Pushover",
                "ok": bool(api.get("pushover_enabled")) or skips["pushover"],
                "optional": True,
                "skipped": skips["pushover"],
                "detail": "Enabled." if api.get("pushover_enabled") else ("Skipped for now." if skips["pushover"] else "Optional mobile push notifications are not enabled."),
                "fix": "Open Gemini and voice behavior setup.",
                "test": "Run diagnostics after entering Pushover keys.",
            },
            {
                "key": "fridge",
                "label": "Fridge/freezer alerts",
                "ok": fridge_configured or skips["fridge"],
                "optional": True,
                "skipped": skips["fridge"],
                "detail": "Configured." if fridge_configured else ("Skipped for now." if skips["fridge"] else "Optional feature not configured."),
                "fix": "Open Refrigerator & Ice setup.",
                "test": "Play fridge/freezer chime tests.",
            },
            {
                "key": "vacuum",
                "label": "Robot vacuum",
                "ok": vacuum_configured or skips["vacuum"],
                "optional": True,
                "skipped": skips["vacuum"],
                "detail": "Configured." if vacuum_configured else ("Skipped for now." if skips["vacuum"] else "Optional feature not configured."),
                "fix": "Open Robot Vacuum setup.",
                "test": "Refresh vacuum controls.",
            },
        ]
        if live_result and live_result.get("ha_connection"):
            conn = live_result["ha_connection"]
            items[0]["ok"] = bool(conn.get("ok"))
            items[0]["detail"] = conn.get("message") or conn.get("error") or items[0]["detail"]
        core_ready = all(item["ok"] for item in items if not item["optional"])
        optional_ready = all(item["ok"] for item in items if item["optional"])
        last_step = progress.get("phase_label") or progress.get("status") or getattr(self, "last_setup_status", "") or "No setup step has reported progress yet."
        return {
            "items": items,
            "core_ready": core_ready,
            "optional_ready": optional_ready,
            "last_step": last_step,
            "progress": progress,
            "listener": listener,
        }

    def _current_setup_issue(self, include_optional=True):
        readiness = self._setup_readiness_items()
        for item in readiness["items"]:
            if not item["ok"] and not item["optional"]:
                return item
        if include_optional:
            for item in readiness["items"]:
                if item["optional"] and not item["ok"]:
                    return item
        return None

    def build_setup_next_action_summary(self):
        readiness = self._setup_readiness_items()
        issue = self._current_setup_issue(include_optional=True)
        skipped = [item["label"] for item in readiness["items"] if item.get("skipped")]
        if readiness["core_ready"]:
            lines = ["Core setup is ready."]
        else:
            lines = ["Core setup needs attention."]
        lines.extend([
            f"Optional setup: {'ready or skipped' if readiness['optional_ready'] else 'available'}",
            f"Last successful step: {readiness['last_step']}",
            f"Skipped optional: {', '.join(skipped) if skipped else 'none'}",
        ])
        if issue:
            action = "Fix" if not issue["optional"] else "Fix or Skip"
            lines.append(f"Next recommended action: {action} {issue['label']}. {issue['fix']}")
        else:
            lines.append("Recommended next: run Test Everything, then try a real doorbell press while armed.")
        return "\n".join(lines)

    def _format_setup_status_items(self, live_result=None):
        readiness = self._setup_readiness_items(live_result=live_result)
        lines = ["Setup Status", ""]
        for item in readiness["items"]:
            state = "Ready" if item["ok"] and not item.get("skipped") else ("Skipped" if item.get("skipped") else "Needs attention")
            optional = " optional" if item["optional"] else ""
            lines.append(f"{item['label']}: {state}{optional}. {item['detail']}")
            if not item["ok"]:
                lines.append(f"  Fix: {item['fix']}")
                lines.append(f"  Test: {item['test']}")
        lines.append("")
        issue = self._current_setup_issue(include_optional=True)
        if issue:
            lines.append(f"One next action: {issue['fix']}")
        else:
            lines.append("Core setup is ready.")
            lines.append("One next action: Run Test Everything, then test a real doorbell press while armed.")
        return "\n".join(lines)

    def _refresh_setup_status_controls(self):
        if hasattr(self, "setup_next_action_txt"):
            self.setup_next_action_txt.SetValue(self.build_setup_next_action_summary())
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue(self.build_setup_checklist_summary())
        self._update_main_setup_actions()

    def on_continue_setup(self, event):
        self.on_open_setup_wizard(event)

    def _setup_page_for_issue(self, key):
        return {
            "home_assistant": "connect",
            "ring_mqtt": "live_streams",
            "doorbell_triggers": "doorbells",
            "live_video": "live_streams",
            "speakers": "speakers",
        }.get(key, "test")

    def on_fix_current_setup_item(self, event):
        issue = self._current_setup_issue(include_optional=True)
        if not issue:
            self.on_test_everything(event)
            return
        key = issue["key"]
        if key in {"home_assistant", "ring_mqtt", "doorbell_triggers", "live_video"}:
            self.open_setup_wizard_at(self._setup_page_for_issue(key))
        elif key == "speakers":
            self.open_setup_wizard_at("speakers")
        elif key == "gemini":
            self.on_fix_tts_setup(event)
        elif key == "pushover":
            self.on_fix_tts_setup(event)
        elif key == "fridge":
            self._open_devices_page("fridge")
            self.notify("Opened Refrigerator & Ice setup. Choose chime behavior, sensors, then test the fridge and freezer chimes.", priority=10)
        elif key == "vacuum":
            self._open_devices_page("vacuum")
            self.notify("Opened Robot Vacuum setup. Refresh controls, choose the vacuum entity, and test status announcements.", priority=10)

    def on_test_current_setup_item(self, event):
        issue = self._current_setup_issue(include_optional=True)
        key = issue["key"] if issue else "all"
        if key == "home_assistant":
            self.on_test_everything(event)
        elif key in {"ring_mqtt", "live_video"}:
            self.on_test_diagnostics_camera(event, "front")
            self.on_test_diagnostics_camera(event, "back")
        elif key == "speakers":
            self.on_test_diagnostics_manual_broadcast(event)
        elif key == "fridge":
            self.on_test_diagnostics_chime(event, "fridge_open")
        elif key == "vacuum":
            self._open_devices_page("vacuum")
            self.on_refresh_vacuum(event)
        else:
            self.on_test_everything(event)

    def on_skip_optional_setup_item(self, event):
        issue = self._current_setup_issue(include_optional=True)
        if not issue or not issue["optional"]:
            self.notify("There is no optional setup item ready to skip. Fix the required item first.", priority=10)
            return
        skips = self._setup_skip_state()
        skips[issue["key"]] = True
        self.config["setup_skips"] = skips
        cfg.save_config(self.config)
        self.record_setup_event("optional_setup_skipped", f"Skipped optional setup item: {issue['label']}", key=issue["key"])
        self._refresh_setup_status_controls()
        self.notify(f"Skipped optional setup item for now: {issue['label']}.", priority=10)

    def on_restore_optional_setup_items(self, event):
        skips = self._setup_skip_state()
        if not any(skips.values()):
            self.notify("No optional setup items are currently skipped.", priority=10)
            return
        restored = [key for key, value in skips.items() if value]
        self.config["setup_skips"] = {key: False for key in skips}
        cfg.save_config(self.config)
        self.record_setup_event("optional_setup_restored", "Restored skipped optional setup items.", restored=", ".join(restored))
        self._refresh_setup_status_controls()
        self.notify("Restored optional setup items. They will show as available again.", priority=10)

    def _non_secret_setup_backup(self):
        data = cfg.validate_and_normalize_config(self.config)
        redacted = diagnostics.redact_config(data)
        for key in ("ha_token", "gemini_api_key", "pushover_user_key", "pushover_api_token", "mqtt_password"):
            redacted[key] = ""
        return {
            "viper_setup_backup_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "notes": "Secrets are intentionally omitted. Re-enter tokens after restore if needed.",
            "config": redacted,
        }

    def on_backup_setup(self, event):
        try:
            backup = self._non_secret_setup_backup()
            path = cfg.DATA_DIR / f"viper_setup_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
            self.record_setup_event("setup_backup_created", "Setup backup created.", path=str(path))
            self.notify(f"Setup backup created: {path}", priority=10)
            wx.MessageBox(f"Setup backup created:\n{path}\n\nSecrets were not included.", "Backup Setup", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            logging.exception("Setup backup failed")
            self.notify(f"Setup backup failed: {e}", priority=10)

    def on_restore_setup(self, event):
        with wx.FileDialog(
            self,
            "Choose Viper setup backup",
            wildcard="Viper setup backup (*.json)|*.json",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = Path(dlg.GetPath())
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            restored = payload.get("config") if isinstance(payload, dict) else None
            if not isinstance(restored, dict):
                raise ValueError("Backup file does not contain a config object.")
            current = cfg.validate_and_normalize_config(self.config)
            for key in ("ha_token", "gemini_api_key", "pushover_user_key", "pushover_api_token", "mqtt_password"):
                restored[key] = current.get(key, "")
            self.config = cfg.write_config(restored)
            self.record_setup_event("setup_backup_restored", "Setup backup restored.", path=str(path))
            self._refresh_setup_status_controls()
            self.notify("Setup backup restored. Re-enter any missing secrets, then run Test Everything.", priority=10)
            wx.MessageBox("Setup backup restored.\n\nSecrets are not stored in backups, so re-enter any missing tokens before testing.", "Restore Setup", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            logging.exception("Setup restore failed")
            self.notify(f"Setup restore failed: {e}", priority=10)

    def on_show_troubleshooting_recipe(self, event, recipe):
        recipes = {
            "ha_token": (
                "Home Assistant token works in browser but not Viper",
                [
                    "Open the Home Assistant profile page and create a new long-lived token.",
                    "Paste the token into the Setup Wizard Home Assistant step.",
                    "Press Connect And Discover Devices, then Test Everything.",
                    "If it still fails, create a support report from Diagnostics.",
                ],
            ),
            "ring_streams": (
                "Ring-MQTT installed but no RTSP streams found",
                [
                    "Open the Setup Wizard and use the Ring-MQTT step.",
                    "Confirm the add-on is logged in and video streaming is enabled.",
                    "Run live stream discovery and save two different passed streams.",
                    "Use Fix Camera Frames if either stream captures no frame.",
                ],
            ),
            "speakers": (
                "Speaker test says sent but nothing played",
                [
                    "Open Choose Alert Speakers and confirm the intended speaker is enabled.",
                    "Check doorbell, utilities, and fridge/freezer route boxes.",
                    "If it is a Home Assistant speaker, verify the media_player entity can play from HA.",
                    "Try Manual Broadcast from Diagnostics.",
                ],
            ),
            "doorbell": (
                "Doorbell rings but Viper does not announce",
                [
                    "Open the Setup Wizard doorbell step.",
                    "Choose front and back trigger entities that change state when the bell is pressed.",
                    "Confirm Viper is armed and the Home Assistant listener is enabled.",
                    "Run Test Everything, then try one real doorbell press.",
                ],
            ),
            "camera": (
                "Camera works once then fails",
                [
                    "Open Ring-MQTT stream discovery and retest streams.",
                    "Save only streams that pass frame capture.",
                    "Check that front and back use different RTSP URLs when possible.",
                    "Run the individual front and back camera frame tests.",
                ],
            ),
            "gemini": (
                "Gemini replies too short or is unavailable",
                [
                    "Open AI Descriptions and keep prompts specific about people, packages, movement, and safety.",
                    "Open Voice Behavior and confirm Gemini key is saved if using Gemini.",
                    "Use Smart or Detailed video mode when still images are too weak.",
                    "Run Test Everything and check Diagnostics if Gemini service is unavailable.",
                ],
            ),
        }
        title, steps = recipes.get(recipe, recipes["ha_token"])
        text = title + "\n\n" + "\n".join(f"{idx}. {step}" for idx, step in enumerate(steps, 1))
        wx.MessageBox(text, "Troubleshooting Recipe", wx.OK | wx.ICON_INFORMATION)

    def record_setup_event(self, event, message="", **details):
        if not hasattr(self, "setup_events"):
            self.setup_events = []
        if not hasattr(self, "last_setup_status"):
            self.last_setup_status = ""
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": str(event or "setup_event"),
            "message": diagnostics.redact_text(str(message or "")),
        }
        for key, value in (details or {}).items():
            if diagnostics.should_redact_key(key):
                entry[str(key)] = diagnostics.redact_config(value, str(key))
            elif isinstance(value, str):
                entry[str(key)] = diagnostics.redact_text(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                entry[str(key)] = value
            else:
                entry[str(key)] = diagnostics.redact_config(value, str(key))
        self.setup_events.append(entry)
        if len(self.setup_events) > 250:
            self.setup_events = self.setup_events[-250:]
        if entry["event"] == "status":
            self.last_setup_status = entry["message"]
        logging.info(
            "[SETUP EVENT] %s message=%r details=%s",
            entry["event"],
            entry["message"],
            {k: v for k, v in entry.items() if k not in {"time", "event", "message"}},
        )

    def _check_line(self, label, ok, detail=""):
        state = "Passed" if ok else "Needs setup"
        return f"{label}: {state}{'. ' + detail if detail else ''}"

    def build_setup_checklist_summary(self, live_result=None):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha_settings = runtime["home_assistant"]
        api_settings = runtime["api"]
        doorbell_settings = runtime["doorbell"]
        speaker_settings = runtime["speakers"]
        speakers = speaker_settings["speakers"]
        listener_status = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        live_result = live_result or {}

        lines = [self._format_setup_status_items(live_result=live_result), "", "Detailed Checklist", ""]
        lines.append(self._check_line("Home Assistant address", bool(ha_settings.get("ha_ip")), ha_settings.get("ha_ip") or "No host saved."))
        lines.append(self._check_line("Home Assistant token", bool(ha_settings.get("ha_token")), "Long-lived token is saved." if ha_settings.get("ha_token") else "Paste a long-lived token."))
        if "ha_connection" in live_result:
            conn = live_result["ha_connection"]
            lines.append(self._check_line("Home Assistant live connection", bool(conn.get("ok")), conn.get("message") or conn.get("error") or "Connection tested."))
        else:
            if listener_status.get("connected"):
                live_detail = "Listener is connected. Press Test Everything when you want Viper to also verify the Home Assistant REST API."
            elif ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
                live_detail = "Not checked yet in this checklist. Press Test Everything to confirm Home Assistant accepts the token and Viper can read entities."
            else:
                live_detail = "Needs Home Assistant host and token before Viper can run a live check."
            lines.append(self._check_line("Home Assistant live connection", bool(listener_status.get("connected")), live_detail))
        lines.append(self._check_line("Direct Home Assistant listener", bool(self.config.get("ha_listener_enabled", True)), "Enabled." if self.config.get("ha_listener_enabled", True) else "Disabled; advanced HA automations/webhooks must be used."))
        if listener_status.get("connected"):
            listener_detail = f"Connected to {listener_status.get('last_host') or ha_settings.get('ha_ip') or 'Home Assistant'}."
        elif not self.config.get("ha_listener_enabled", True):
            listener_detail = "Disabled by choice. Viper will rely on advanced Home Assistant automations or webhooks."
        elif ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
            raw_error = listener_status.get("last_error") or "waiting to connect"
            if "missing Home Assistant host or token" in raw_error:
                listener_detail = "Credentials are available from config, environment variables, or Windows Credential Manager. The listener should reconnect shortly; press Test Everything to verify Home Assistant directly."
            else:
                listener_detail = raw_error
        else:
            listener_detail = "Missing Home Assistant host or token."
        lines.append(self._check_line("Listener currently connected", bool(listener_status.get("connected")), listener_detail))
        lines.append("")
        front_trigger = doorbell_settings.get("front_trigger_entity_id", "")
        back_trigger = doorbell_settings.get("back_trigger_entity_id", "")
        front_rtsp = doorbell_settings.get("configured_rtsp_front") or doorbell_settings.get("raw_rtsp_front") or ""
        back_rtsp = doorbell_settings.get("configured_rtsp_back") or doorbell_settings.get("raw_rtsp_back") or ""
        lines.append(self._check_line("Front door trigger", bool(front_trigger), front_trigger or "Choose a front trigger entity."))
        lines.append(self._check_line("Back door trigger", bool(back_trigger), back_trigger or "Choose a back trigger entity."))
        lines.append(self._check_line("Front live RTSP URL", bool(front_rtsp), front_rtsp or "Find Ring MQTT streams."))
        lines.append(self._check_line("Back live RTSP URL", bool(back_rtsp), back_rtsp or "Find Ring MQTT streams."))
        if "rtsp_front" in live_result:
            lines.append(self._check_line("Front RTSP frame test", bool(live_result["rtsp_front"].get("ok")), live_result["rtsp_front"].get("message") or "Frame captured."))
        if "rtsp_back" in live_result:
            lines.append(self._check_line("Back RTSP frame test", bool(live_result["rtsp_back"].get("ok")), live_result["rtsp_back"].get("message") or "Frame captured."))
        lines.append("")
        lines.append(self._check_line("Ring-MQTT RTSP stream setup", bool(front_rtsp and back_rtsp), "Both RTSP URLs are saved." if front_rtsp and back_rtsp else "Use Ring-MQTT setup if using Ring cameras."))
        enabled_speakers = speaker_settings["enabled_count"]
        speaker_routes = speaker_settings.get("routes", {})
        doorbell_route_count = len(speaker_routes.get("doorbell", []))
        utilities_route_count = len(speaker_routes.get("utilities", []))
        fridge_route_count = len(speaker_routes.get("fridge", []))
        required_routes_ok = bool(enabled_speakers and doorbell_route_count and utilities_route_count and fridge_route_count)
        if speakers:
            speaker_detail = (
                f"{speaker_settings['speaker_count']} saved, {enabled_speakers} enabled. "
                f"Enabled routes: doorbell {doorbell_route_count}, utilities {utilities_route_count}, "
                f"fridge/freezer {fridge_route_count}. "
                "Use Choose Alert Speakers if any route count is zero or if the wrong speaker is enabled."
            )
        else:
            speaker_detail = "Add or scan Home Assistant/Sonos speakers, then choose which ones receive alerts."
        lines.append(self._check_line("Speaker routes", required_routes_ok, speaker_detail))
        lines.append(self._check_line("Gemini API key", bool(api_settings.get("gemini_api_key")), "Saved." if api_settings.get("gemini_api_key") else "Needed for Gemini vision and Gemini TTS."))
        lines.append(self._check_line("Pushover", bool(api_settings.get("pushover_enabled")), "Enabled." if api_settings.get("pushover_enabled") else "Optional."))
        lines.append("")
        lines.append("Optional Feature Cards")
        for item in self._setup_readiness_items()["items"]:
            if item["optional"]:
                state = "ready" if item["ok"] and not item.get("skipped") else ("skipped" if item.get("skipped") else "not configured")
                lines.append(f"{item['label']}: {state}. {item['detail']}")
        lines.append("")
        lines.append("Troubleshooting Recipes")
        lines.append("Use the recipe buttons above for Home Assistant tokens, Ring-MQTT streams, speaker audio, doorbell events, camera frames, and Gemini replies.")
        lines.append("")
        lines.append("Next best action:")
        if not ha_settings.get("ha_ip") or not ha_settings.get("ha_token"):
            lines.append("Press Open Setup Wizard and complete the Home Assistant Connection step.")
        elif not (front_trigger and back_trigger):
            lines.append("Open Doorbell Vision, then press Set Up Doorbell Triggers And Cameras.")
        elif not (front_rtsp and back_rtsp):
            lines.append("Open Doorbell Vision, then press Set Up Doorbell Triggers And Cameras and find Ring-MQTT streams.")
        elif not required_routes_ok:
            lines.append("Press Choose Alert Speakers. Enable at least one speaker for doorbell, utilities, and fridge/freezer alerts.")
        elif not api_settings.get("gemini_api_key"):
            lines.append("Open Speakers & Audio, then configure Gemini or choose a non-Gemini TTS engine.")
        else:
            lines.append("Press Test Everything to verify Home Assistant, RTSP camera frames, and diagnostics. Then try a full doorbell flow test.")
        return "\n".join(lines)

    def build_setup_confidence_summary(self):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha = runtime["home_assistant"]
        api = runtime["api"]
        doorbell = runtime["doorbell"]
        speakers = runtime["speakers"]
        routes = speakers.get("routes", {})
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        front_rtsp = doorbell.get("configured_rtsp_front") or doorbell.get("raw_rtsp_front") or ""
        back_rtsp = doorbell.get("configured_rtsp_back") or doorbell.get("raw_rtsp_back") or ""
        ready = bool(
            ha.get("ha_ip")
            and ha.get("ha_token")
            and doorbell.get("front_trigger_entity_id")
            and doorbell.get("back_trigger_entity_id")
            and front_rtsp
            and back_rtsp
            and speakers.get("enabled_count")
            and routes.get("doorbell")
            and routes.get("utilities")
            and routes.get("fridge")
            and api.get("gemini_api_key")
        )
        lines = [
            f"Doorbell system ready: {'yes' if ready else 'needs attention'}",
            f"Home Assistant listener: {'connected' if listener.get('connected') else 'not connected'}",
            f"Doorbell triggers: front {'yes' if doorbell.get('front_trigger_entity_id') else 'no'}, back {'yes' if doorbell.get('back_trigger_entity_id') else 'no'}",
            f"Camera streams: front {'yes' if front_rtsp else 'no'}, back {'yes' if back_rtsp else 'no'}",
            f"Audio routes: doorbell {len(routes.get('doorbell', []))}, utilities {len(routes.get('utilities', []))}, fridge/freezer {len(routes.get('fridge', []))}",
            f"Gemini key: {'yes' if api.get('gemini_api_key') else 'no'}",
            "Recommended next: run Test Everything, then test the real doorbell while armed.",
        ]
        return "\n".join(lines)

    def refresh_setup_checklist(self):
        if hasattr(self, "setup_next_action_txt"):
            self.setup_next_action_txt.SetValue(self.build_setup_next_action_summary())
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue(self.build_setup_checklist_summary())
        if hasattr(self, "doorbell_summary_txt"):
            self.doorbell_summary_txt.SetValue(self._doorbell_summary_text())
        self._update_main_setup_actions()
        self.notify("Setup checklist refreshed.", priority=10)

    def _set_main_button_gate(self, button, enabled, enabled_tip, disabled_tip):
        if button is None:
            return
        try:
            button.Enable(bool(enabled))
            button.SetToolTip(enabled_tip if enabled else disabled_tip)
            button.SetName(button.GetLabel() if enabled else f"{button.GetLabel()}. Unavailable. {disabled_tip}")
        except Exception:
            pass

    def _update_main_setup_actions(self):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha_settings = runtime["home_assistant"]
        doorbell_settings = runtime["doorbell"]
        speaker_settings = runtime["speakers"]
        speakers = speaker_settings["speakers"]
        speaker_routes = speaker_settings.get("routes", {})
        required_routes_ok = bool(
            speaker_settings.get("enabled_count")
            and speaker_routes.get("doorbell")
            and speaker_routes.get("utilities")
            and speaker_routes.get("fridge")
        )
        has_ha = bool(ha_settings.get("ha_ip") and ha_settings.get("ha_token"))
        has_doorbell_setup = bool(
            doorbell_settings.get("front_trigger_entity_id")
            and doorbell_settings.get("back_trigger_entity_id")
            and (doorbell_settings.get("configured_rtsp_front") or doorbell_settings.get("raw_rtsp_front"))
            and (doorbell_settings.get("configured_rtsp_back") or doorbell_settings.get("raw_rtsp_back"))
        )
        self._set_main_button_gate(
            getattr(self, "btn_fix_doorbells", None),
            has_ha,
            "Opens doorbell setup.",
            "Home Assistant host and token are needed before doorbell entities and Ring-MQTT streams can be discovered.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_choose_setup_speakers", None),
            has_ha,
            "Finds or edits alert speakers.",
            "Home Assistant host and token are needed before Home Assistant speaker discovery can run.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_test_everything", None),
            has_ha and has_doorbell_setup and required_routes_ok,
            "Runs final setup tests.",
            "Home Assistant, doorbell triggers, live streams, and at least one enabled speaker route for doorbell, utilities, and fridge/freezer should be configured first.",
        )
        readiness = self._setup_readiness_items()
        required_issue = self._current_setup_issue(include_optional=False)
        issue = self._current_setup_issue(include_optional=True)
        optional_issue = issue and issue.get("optional")
        skipped = [item for item in readiness["items"] if item.get("skipped")]
        fix_button = getattr(self, "btn_fix_current_setup", None)
        if fix_button is not None:
            if required_issue:
                fix_button.SetLabel("Fix Current Item")
                fix_button.SetName("Fix Current Item")
            elif optional_issue:
                fix_button.SetLabel("Set Up Optional Item")
                fix_button.SetName("Set Up Optional Item")
            else:
                fix_button.SetLabel("Setup Is Ready")
                fix_button.SetName("Setup Is Ready")
        self._set_main_button_gate(
            fix_button,
            bool(issue),
            "Opens the exact setup area for the current item.",
            "Core setup is ready. Run Test Everything when you want a fresh verification.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_test_current_setup", None),
            bool(issue) or readiness["core_ready"],
            "Runs the safest relevant test for the current setup item.",
            "Add Home Assistant host and token before tests can run.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_skip_optional_setup", None),
            optional_issue,
            "Marks the current optional feature as skipped for now.",
            "Required setup items cannot be skipped. Finish the required item first.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_unskip_optional_setup", None),
            bool(skipped),
            "Restores skipped optional items so they appear in setup again.",
            "No optional setup items are currently skipped.",
        )

    def suggested_setup_page(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        if not ha_settings.get("ha_ip") or not ha_settings.get("ha_token"):
            return "connect"
        triggers = self.config.get("doorbell_triggers", {}) if isinstance(self.config.get("doorbell_triggers"), dict) else {}
        front = triggers.get("front", {}) if isinstance(triggers, dict) else {}
        back = triggers.get("back", {}) if isinstance(triggers, dict) else {}
        if not (front.get("trigger_entity_id") or back.get("trigger_entity_id")):
            return "doorbells"
        if not (front.get("rtsp_url") or self.config.get("rtsp_front")) or not (back.get("rtsp_url") or self.config.get("rtsp_back")):
            return "live_streams"
        speaker_settings = cfg.get_speaker_settings(self.config, include_env=True)
        routes = speaker_settings.get("routes", {})
        if not (speaker_settings.get("enabled_count") and routes.get("doorbell") and routes.get("utilities") and routes.get("fridge")):
            return "speakers"
        return "test"

    def on_open_setup_wizard(self, event):
        self._close_setup_surfaces(keep="_setup_wizard_dialog")
        existing = getattr(self, "_setup_wizard_dialog", None)
        if self._is_live_window(existing):
            try:
                requested = getattr(self, "_requested_setup_page", "")
                if requested and hasattr(existing, "go_to_setup_action"):
                    existing.go_to_setup_action(requested)
                    self._requested_setup_page = ""
                existing.force_initial_focus()
                self._enter_setup_window_mode(existing)
                self._log_setup_focus_snapshot("reuse_setup_wizard")
                return
            except Exception:
                self._setup_wizard_dialog = None
        else:
            self._setup_wizard_dialog = None
        dlg = ViperSetupWizardDialog(None, owner=self)
        if hasattr(self, "_requested_setup_page"):
            self._requested_setup_page = ""
        self._setup_wizard_dialog = dlg
        dlg.Show()
        wx.CallAfter(self._enter_setup_window_mode, dlg)
        wx.CallLater(75, dlg.force_initial_focus)
        wx.CallLater(300, dlg.force_initial_focus)
        wx.CallLater(450, self._log_setup_focus_snapshot, "show_setup_wizard")

    def open_setup_wizard_at(self, action):
        self._requested_setup_page = action
        self.on_open_setup_wizard(None)

    def on_setup_everything_automatically(self, event):
        self.on_open_setup_wizard(event)

    def on_test_everything(self, event):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha_settings = runtime["home_assistant"]
        doorbell_settings = runtime["doorbell"]
        if not (ha_settings.get("ha_ip") and ha_settings.get("ha_token")):
            message = "Test Everything cannot run yet. Press Open Setup Wizard and complete the Home Assistant Connection step first."
            if hasattr(self, "setup_checklist_txt"):
                self.setup_checklist_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        if not (
            doorbell_settings.get("front_trigger_entity_id")
            and doorbell_settings.get("back_trigger_entity_id")
            and (doorbell_settings.get("configured_rtsp_front") or doorbell_settings.get("raw_rtsp_front"))
            and (doorbell_settings.get("configured_rtsp_back") or doorbell_settings.get("raw_rtsp_back"))
        ):
            message = "Test Everything cannot run yet. Open Doorbell Vision and set up triggers and live camera streams first."
            if hasattr(self, "setup_checklist_txt"):
                self.setup_checklist_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        speaker_settings = runtime["speakers"]
        speaker_routes = speaker_settings.get("routes", {})
        if not (
            speaker_settings.get("enabled_count")
            and speaker_routes.get("doorbell")
            and speaker_routes.get("utilities")
            and speaker_routes.get("fridge")
        ):
            message = "Test Everything cannot run yet. Press Choose Alert Speakers and enable at least one speaker route for doorbell, utilities, and fridge/freezer first."
            if hasattr(self, "setup_checklist_txt"):
                self.setup_checklist_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue("Running setup checks. This can take a few seconds.")
        self.notify("Running setup checks.", priority=10)
        safe_submit(self._run_test_everything)

    def _run_test_everything(self):
        live_result = {}
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        if ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
            live_result["ha_connection"] = discovery.test_ha_connection(
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port") or "8123",
                timeout=5,
            )
        for side, key in (("front", "rtsp_front"), ("back", "rtsp_back")):
            url = self.config.get(key) or (self.config.get("doorbell_triggers", {}).get(side, {}) or {}).get("rtsp_url")
            if not url:
                continue
            try:
                frame = vision.grab_frame(url, cfg.DATA_DIR / "rtsp_test", f"setup_check_{side}", min_bytes=14000, timeout=8)
                live_result[key] = {"ok": bool(frame), "message": f"Frame captured: {Path(frame).name}" if frame else "No frame captured."}
            except Exception as e:
                live_result[key] = {"ok": False, "message": str(e)}
        summary = self.build_setup_checklist_summary(live_result=live_result)
        try:
            summary += "\n\n" + self._format_safe_smoke_report(self._collect_safe_smoke_results())
        except Exception as e:
            logging.exception("Could not append safe smoke report to Test Everything.")
            summary += f"\n\nSmoke Test: ERROR\n\nThe smoke test report failed: {e}"
        wx.CallAfter(self._finish_test_everything, summary)

    def _finish_test_everything(self, summary):
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue(summary)
        self._update_main_setup_actions()
        self.notify("Setup checks finished.", priority=10)

    def setup_ai_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        def hide_disabled(control):
            control.Hide()
            control.Enable(False)

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
        hide_disabled(self.tts_engine_choice)

        self.secondary_voice_label = wx.StaticText(self.tab_ai, label="Network Speaker Voice:")
        self.secondary_voice_choice = wx.Choice(self.tab_ai, choices=[])
        self.secondary_voice_choice.Bind(wx.EVT_CHOICE, self.on_secondary_voice_change)
        hide_disabled(self.secondary_voice_label)
        hide_disabled(self.secondary_voice_choice)

        self.btn_refresh_v = wx.Button(self.tab_ai, label="Force Refresh Natural Voices")
        self.btn_refresh_v.Bind(wx.EVT_BUTTON, self.on_refresh_edge_voices)
        hide_disabled(self.btn_refresh_v)

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
        hide_disabled(self.tts_engine_choice)
        hide_disabled(self.secondary_voice_label)
        hide_disabled(self.secondary_voice_choice)
        hide_disabled(self.btn_refresh_v)

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
        try:
            control.Bind(wx.EVT_SET_FOCUS, self._on_control_focus_for_diagnostics)
        except Exception:
            pass

    def _on_control_focus_for_diagnostics(self, event):
        control = event.GetEventObject()
        try:
            label = control.GetLabel() if hasattr(control, "GetLabel") else ""
            logging.info(
                "[FOCUS] Dashboard focus class=%s name=%r label=%r shown=%s enabled=%s can_focus=%s",
                control.__class__.__name__,
                control.GetName() if hasattr(control, "GetName") else "",
                label,
                control.IsShownOnScreen() if hasattr(control, "IsShownOnScreen") else None,
                control.IsEnabled() if hasattr(control, "IsEnabled") else None,
                control.CanAcceptFocusFromKeyboard() if hasattr(control, "CanAcceptFocusFromKeyboard") else None,
            )
        except Exception:
            logging.debug("Could not log dashboard focus target.", exc_info=True)
        event.Skip()

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
            self.btn_refresh_v.Enable(False)

        for control in (self.tts_engine_choice, self.secondary_voice_label, self.secondary_voice_choice, self.btn_refresh_v):
            control.Hide()
            control.Enable(False)
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
        self.btn_discover_spk = wx.Button(self.tab_dev, label="Discover Available Speakers")
        self.btn_discover_spk.Bind(wx.EVT_BUTTON, self.on_discover_speakers)
        self.btn_ren_spk = wx.Button(self.tab_dev, label="Rename Selected")
        self.btn_ren_spk.Bind(wx.EVT_BUTTON, self.on_rename_speaker)
        self.btn_rem_spk = wx.Button(self.tab_dev, label="Remove Selected")
        self.btn_rem_spk.Bind(wx.EVT_BUTTON, self.on_remove_speaker)
        btn_sizer.Add(self.btn_add_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_discover_spk, 1, wx.ALL, 5)
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

        qbox = wx.StaticBox(self.tab_dev, label="Quiet Hours")
        qsizer = wx.StaticBoxSizer(qbox, wx.VERTICAL)
        self.quiet_hours_enable_chk = wx.CheckBox(self.tab_dev, label="Enable quiet hours (suppresses utility announcements)")
        self.quiet_hours_enable_chk.SetValue(self.config.get("quiet_hours_enabled", False))
        self.quiet_hours_enable_chk.Bind(wx.EVT_CHECKBOX, self.on_quiet_hours_change)
        qsizer.Add(self.quiet_hours_enable_chk, 0, wx.ALL, 5)

        qrow = wx.BoxSizer(wx.HORIZONTAL)
        qrow.Add(wx.StaticText(self.tab_dev, label="Quiet hours start time, HH:MM:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.quiet_hours_start_txt = wx.TextCtrl(self.tab_dev, value=self.config.get("quiet_hours_start", "22:00"))
        qrow.Add(self.quiet_hours_start_txt, 1, wx.ALL, 5)
        qrow.Add(wx.StaticText(self.tab_dev, label="Quiet hours end time, HH:MM:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.quiet_hours_end_txt = wx.TextCtrl(self.tab_dev, value=self.config.get("quiet_hours_end", "07:00"))
        qrow.Add(self.quiet_hours_end_txt, 1, wx.ALL, 5)
        qsizer.Add(qrow, 0, wx.EXPAND)

        self.btn_save_quiet_hours = wx.Button(self.tab_dev, label="Save Quiet Hours", size=(-1, 40))
        self.btn_save_quiet_hours.Bind(wx.EVT_BUTTON, self.on_quiet_hours_change)
        qsizer.Add(self.btn_save_quiet_hours, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(qsizer, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_dev.SetSizer(sizer)

    def setup_diagnostics_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        health_box = wx.StaticBox(self.tab_diagnostics_overview, label="Health Summary")
        health_sizer = wx.StaticBoxSizer(health_box, wx.VERTICAL)
        self.diagnostics_health_txt = wx.TextCtrl(
            self.tab_diagnostics_overview,
            value="Health summary is loading.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 210),
        )
        self._describe_control(self.diagnostics_health_txt, "Health Summary. Read-only active issues, resolved history, normal log noise, and latest log line.")
        health_sizer.Add(self.diagnostics_health_txt, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_refresh_health = wx.Button(self.tab_diagnostics_overview, label="Refresh Health Summary", size=(-1, 40))
        self.btn_refresh_health.Bind(wx.EVT_BUTTON, self.on_refresh_health_summary)
        self._describe_control(self.btn_refresh_health, "Refresh Health Summary button. Quickly refreshes the local Viper health summary without opening the full diagnostics report.")
        health_sizer.Add(self.btn_refresh_health, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(health_sizer, 1, wx.ALL | wx.EXPAND, 10)

        smoke_box = wx.StaticBox(self.tab_diagnostics_overview, label="Safe Smoke Test")
        smoke_sizer = wx.StaticBoxSizer(smoke_box, wx.VERTICAL)
        self.smoke_test_txt = wx.TextCtrl(
            self.tab_diagnostics_overview,
            value="Press Run Safe Smoke Test to check Viper readiness without playing audio or triggering doorbell flows.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 230),
        )
        self._describe_control(self.smoke_test_txt, "Safe Smoke Test results. Read-only pass fail report with exact next steps for anything broken.")
        smoke_sizer.Add(self.smoke_test_txt, 1, wx.ALL | wx.EXPAND, 5)
        smoke_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        smoke_grid.AddGrowableCol(0, 1)
        smoke_grid.AddGrowableCol(1, 1)
        self.btn_run_safe_smoke = wx.Button(self.tab_diagnostics_overview, label="Run Safe Smoke Test", size=(-1, 40))
        self.btn_test_front_camera_diag = wx.Button(self.tab_diagnostics_overview, label="Test Front Camera Frame", size=(-1, 40))
        self.btn_test_back_camera_diag = wx.Button(self.tab_diagnostics_overview, label="Test Back Camera Frame", size=(-1, 40))
        self.btn_test_manual_broadcast_diag = wx.Button(self.tab_diagnostics_overview, label="Test Manual Broadcast", size=(-1, 40))
        self.btn_test_fridge_chime_diag = wx.Button(self.tab_diagnostics_overview, label="Test Fridge Chime", size=(-1, 40))
        self.btn_test_freezer_chime_diag = wx.Button(self.tab_diagnostics_overview, label="Test Freezer Chime", size=(-1, 40))
        self.btn_run_safe_smoke.Bind(wx.EVT_BUTTON, self.on_run_safe_smoke_test)
        self.btn_test_front_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "front"))
        self.btn_test_back_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "back"))
        self.btn_test_manual_broadcast_diag.Bind(wx.EVT_BUTTON, self.on_test_diagnostics_manual_broadcast)
        self.btn_test_fridge_chime_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_chime(event, "fridge_open"))
        self.btn_test_freezer_chime_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_chime(event, "freezer_open"))
        for button, description in {
            self.btn_run_safe_smoke: "Run Safe Smoke Test button. Checks configuration, Home Assistant, listener, camera URLs, speaker routes, support bundle creation, and active health issues without playing audio.",
            self.btn_test_front_camera_diag: "Test Front Camera Frame button. Captures one frame from the configured front camera stream.",
            self.btn_test_back_camera_diag: "Test Back Camera Frame button. Captures one frame from the configured back camera stream.",
            self.btn_test_manual_broadcast_diag: "Test Manual Broadcast button. Speaks a short manual test announcement through configured speakers.",
            self.btn_test_fridge_chime_diag: "Test Fridge Chime button. Plays the configured fridge open chime through fridge route speakers.",
            self.btn_test_freezer_chime_diag: "Test Freezer Chime button. Plays the configured freezer open chime through fridge route speakers.",
        }.items():
            self._describe_control(button, description)
            smoke_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        smoke_sizer.Add(smoke_grid, 0, wx.EXPAND)
        sizer.Add(smoke_sizer, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        box = wx.StaticBox(self.tab_diagnostics_overview, label="Diagnostic Actions")
        dsizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        self.btn_about = wx.Button(self.tab_diagnostics_overview, label="About Viper Vision And Data Folders", size=(-1, 40))
        self.btn_about.Bind(wx.EVT_BUTTON, self.on_show_about)
        self.btn_diagnostics = wx.Button(self.tab_diagnostics_overview, label="Run Diagnostics", size=(-1, 40))
        self.btn_diagnostics.Bind(wx.EVT_BUTTON, self.on_run_diagnostics)
        self.btn_support_bundle = wx.Button(self.tab_diagnostics_overview, label="Create Support Report To Email Developer", size=(-1, 40))
        self.btn_support_bundle.Bind(wx.EVT_BUTTON, self.on_create_support_report)
        self.btn_api = wx.Button(self.tab_diagnostics_overview, label="Check API Cost", size=(-1, 40))
        self.btn_api.Bind(wx.EVT_BUTTON, self.on_api)
        self.btn_batt = wx.Button(self.tab_diagnostics_overview, label="Check Doorbell Batteries", size=(-1, 40))
        self.btn_batt.Bind(wx.EVT_BUTTON, self.on_batt)
        self.btn_filter = wx.Button(self.tab_diagnostics_overview, label="Check Refrigerator Filter", size=(-1, 40))
        self.btn_filter.Bind(wx.EVT_BUTTON, self.on_filter)
        for button, description in {
            self.btn_about: "About Viper Vision And Data Folders button. Shows version, app folder, data folder, config path, log path, remote URL, and where support bundles are saved.",
            self.btn_diagnostics: "Run Diagnostics button. Checks Viper configuration, Home Assistant listener status, Home Assistant health, FFmpeg, and recent errors.",
            self.btn_support_bundle: "Create Support Report To Email Developer button. Creates a redacted diagnostic bundle and opens an email draft.",
            self.btn_api: "Check API Cost button. Reads the local API usage log and reports estimated Gemini usage cost.",
            self.btn_batt: "Check Doorbell Batteries button. Checks Home Assistant battery entities for front and back door devices.",
            self.btn_filter: "Check Refrigerator Filter button. Checks refrigerator water filter status from Home Assistant.",
        }.items():
            self._describe_control(button, description)
            dsizer.Add(button, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(dsizer, 0, wx.ALL | wx.EXPAND, 10)
        self.tab_diagnostics_overview.SetSizer(sizer)
        self.refresh_health_summary()

    def setup_utils_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        intro = AccessibleStatusText(
            self.tab_util,
            value=(
                "Advanced contains setup tools that most people use rarely after Viper is working.\n\n"
                "Daily controls now live in Dashboard, Doorbell Vision, Speakers & Audio, and Home Devices. "
                "Health checks and logs live in Diagnostics."
            ),
            size=(-1, 110),
        )
        self._describe_control(intro, "Advanced introduction. Explanation of rarely used setup and export tools.")
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        ubox = wx.StaticBox(self.tab_util, label="Advanced Setup And Export Tools")
        usizer = wx.StaticBoxSizer(ubox, wx.VERTICAL)

        self.btn_new_user_setup = wx.Button(self.tab_util, label="Advanced: Home Assistant Server Assistant", size=(-1, 40))
        self.btn_new_user_setup.Bind(wx.EVT_BUTTON, self.on_new_user_setup)
        self.btn_ha_setup = wx.Button(self.tab_util, label="Advanced Home Assistant Setup", size=(-1, 40))
        self.btn_ha_setup.Bind(wx.EVT_BUTTON, self.on_home_assistant_setup)
        self.btn_ha_package = wx.Button(self.tab_util, label="Advanced: Export HA YAML Package", size=(-1, 40))
        self.btn_ha_package.Bind(wx.EVT_BUTTON, self.on_generate_ha_package)
        self.btn_scan = wx.Button(self.tab_util, label="Advanced: Scan Network for Sonos", size=(-1, 40))
        self.btn_scan.Bind(wx.EVT_BUTTON, self.on_scan_sonos)
        self.btn_scan_ha = wx.Button(self.tab_util, label="Advanced: Scan HA for Speakers", size=(-1, 40))
        self.btn_scan_ha.Bind(wx.EVT_BUTTON, self.on_scan_ha)

        usizer.Add(self.btn_new_user_setup, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_ha_setup, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_ha_package, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_scan, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_scan_ha, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(usizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_util.SetSizer(sizer)

    def setup_vacuum_tab(self):
        self.vacuum_state_entities = []
        self.vacuum_control_entities = []
        self.vacuum_control_widgets = {}
        self.vacuum_action_buttons = {}
        self._pending_vacuum_focus_entity_id = ""
        self.vacuum_rooms = []
        self.vacuum_room_checks = []

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

        self.vacuum_status_txt = AccessibleStatusText(
            self.tab_vacuum,
            value="Press Refresh vacuum controls to scan Home Assistant for Roborock controls.",
            size=(-1, 150),
        )
        self._describe_control(
            self.vacuum_status_txt,
            "Vacuum status. Summarizes the selected vacuum state and nearby Roborock status sensors.",
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

        self.vacuum_room_scroll = wx.ScrolledWindow(self.tab_vacuum, style=wx.VSCROLL | wx.TAB_TRAVERSAL, size=(-1, 150))
        self.vacuum_room_scroll.SetScrollRate(0, 20)
        self.vacuum_room_sizer = wx.BoxSizer(wx.VERTICAL)
        self.vacuum_room_scroll.SetSizer(self.vacuum_room_sizer)
        self._describe_control(
            self.vacuum_room_scroll,
            "Roborock room checkbox list. Tab through each room checkbox. Press Space to check or uncheck rooms, then press Clean selected rooms.",
        )
        room_outer.Add(self.vacuum_room_scroll, 0, wx.ALL | wx.EXPAND, 5)

        repeat_row = wx.BoxSizer(wx.HORIZONTAL)
        repeat_row.Add(wx.StaticText(self.tab_vacuum, label="Room clean repeat count:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_room_repeat = wx.SpinCtrl(self.tab_vacuum, min=1, max=3, initial=1)
        self._describe_control(
            self.vacuum_room_repeat,
            "Room clean repeat count. Choose 1, 2, or 3 passes for selected rooms.",
        )
        repeat_row.Add(self.vacuum_room_repeat, 0, wx.ALL, 5)
        room_outer.Add(repeat_row, 0, wx.EXPAND)

        self.vacuum_room_status_txt = AccessibleStatusText(
            self.tab_vacuum,
            value="Press Refresh room list to load Roborock rooms from Home Assistant.",
            size=(-1, 80),
        )
        self._describe_control(
            self.vacuum_room_status_txt,
            "Vacuum room status. Reports map and room discovery results.",
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
        self._restore_pending_vacuum_focus()

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

    def _room_checkbox_label(self, room, checked=False):
        label = room.get("label") or f"{room.get('name', 'Room')} ({room.get('segment', 'unknown')})"
        state = "checked" if checked else "not checked"
        return f"{label}, room ID {room.get('segment', 'unknown')}, {state}"

    def _on_vacuum_room_checkbox(self, event):
        check = event.GetEventObject()
        room = getattr(check, "_viper_room", {})
        checked = bool(check.GetValue())
        label = self._room_checkbox_label(room, checked)
        check.SetName(label)
        check.SetToolTip(label)
        self.vacuum_room_status_txt.SetValue(f"{label}. Press Clean selected rooms when your room choices are correct.")
        wx.CallAfter(self._safe_speak, label)
        event.Skip()

    def _rebuild_vacuum_dynamic_controls(self):
        self._clear_sizer(self.vacuum_controls_sizer)
        self.vacuum_control_widgets = {}
        self.vacuum_action_buttons = {}
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
            label = self._short_entity_label(entity)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{label}:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            if domain == "select":
                options = [str(item) for item in attrs.get("options", [])] if isinstance(attrs.get("options"), list) else []
                choice = wx.Choice(self.vacuum_controls_panel, choices=options)
                if str(entity.get("state", "")) in options:
                    choice.SetStringSelection(str(entity.get("state")))
                elif options:
                    choice.SetSelection(0)
                btn_label = f"Apply {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_set_select(event, eid))
                self._describe_control(choice, f"{label} combo box. Choose a Roborock setting value, then press {btn_label}. Current value is {entity.get('state', 'unknown')}.")
                self._describe_control(btn, f"{btn_label} button. Sends the selected {label} value to Home Assistant.")
                row.Add(choice, 1, wx.ALL | wx.EXPAND, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_control_widgets[entity_id] = choice
                self.vacuum_action_buttons[entity_id] = btn
            elif domain == "number":
                minimum = attrs.get("min", 0)
                maximum = attrs.get("max", 100)
                step = attrs.get("step", 1)
                spin = wx.SpinCtrlDouble(self.vacuum_controls_panel, min=float(minimum), max=float(maximum), inc=float(step))
                try:
                    spin.SetValue(float(entity.get("state", minimum)))
                except (TypeError, ValueError):
                    spin.SetValue(float(minimum))
                btn_label = f"Set {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_set_number(event, eid))
                self._describe_control(spin, f"{label} numeric value. Adjust the value, then press {btn_label}. Current value is {entity.get('state', 'unknown')}.")
                self._describe_control(btn, f"{btn_label} button. Sends the numeric value to Home Assistant.")
                row.Add(spin, 1, wx.ALL | wx.EXPAND, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_control_widgets[entity_id] = spin
                self.vacuum_action_buttons[entity_id] = btn
            elif domain == "switch":
                state = str(entity.get("state", "")).lower()
                turn_on = state != "on"
                btn_label = f"Turn {'on' if turn_on else 'off'} {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id, next_on=turn_on: self.on_vacuum_switch(event, eid, next_on))
                self._describe_control(btn, f"{btn_label} button. Current state is {state or 'unknown'}.")
                row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{label} current state {state or 'unknown'}"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_action_buttons[entity_id] = btn
            elif domain == "button":
                btn_label = f"Press {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_press_button(event, eid))
                self._describe_control(btn, f"{btn_label} button. Sends a Home Assistant button press for this Roborock control.")
                row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{label} last state {entity.get('state', 'unknown')}"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_action_buttons[entity_id] = btn
            self.vacuum_controls_sizer.Add(row, 0, wx.EXPAND)
        self.vacuum_controls_panel.Layout()

    def _restore_pending_vacuum_focus(self):
        entity_id = getattr(self, "_pending_vacuum_focus_entity_id", "")
        if not entity_id:
            return
        self._pending_vacuum_focus_entity_id = ""
        button = getattr(self, "vacuum_action_buttons", {}).get(entity_id)
        if not button:
            return
        wx.CallAfter(self._focus_vacuum_action_button, button)

    def _focus_vacuum_action_button(self, button):
        try:
            if hasattr(button, "SetFocusFromKbd"):
                button.SetFocusFromKbd()
            else:
                button.SetFocus()
        except Exception:
            logging.debug("Could not restore focus to vacuum action button.", exc_info=True)

    def _show_vacuum_setting(self, entity):
        entity_id = entity.get("entity_id", "")
        domain = self._ha_domain(entity)
        if _is_hidden_vacuum_setting_entity_id(entity_id):
            return False
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
        self._run_ha_service_async(
            "select/select_option",
            {"entity_id": entity_id, "option": option},
            f"Set {entity_id} to {option}.",
            timeout=30,
            restore_focus_entity_id=entity_id,
        )

    def on_vacuum_set_number(self, event, entity_id):
        spin = self.vacuum_control_widgets.get(entity_id)
        value = spin.GetValue() if spin else None
        if value is None:
            self.notify("Enter a number first.", priority=10)
            return
        self._run_ha_service_async(
            "number/set_value",
            {"entity_id": entity_id, "value": value},
            f"Set {entity_id} to {value}.",
            timeout=30,
            restore_focus_entity_id=entity_id,
        )

    def on_vacuum_switch(self, event, entity_id, turn_on):
        service = "switch/turn_on" if turn_on else "switch/turn_off"
        label = "on" if turn_on else "off"
        self._run_ha_service_async(service, {"entity_id": entity_id}, f"Turned {label} {entity_id}.", restore_focus_entity_id=entity_id)

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
        if hasattr(self, "vacuum_room_sizer"):
            self._clear_sizer(self.vacuum_room_sizer)
        self.vacuum_room_checks = []
        for room in rooms:
            label = self._room_checkbox_label(room, False)
            check = wx.CheckBox(self.vacuum_room_scroll, label=label)
            check._viper_room = room
            check.SetName(label)
            check.SetToolTip(label)
            check.Bind(wx.EVT_CHECKBOX, self._on_vacuum_room_checkbox)
            self.vacuum_room_sizer.Add(check, 0, wx.ALL | wx.EXPAND, 4)
            self.vacuum_room_checks.append(check)
        if not rooms and hasattr(self, "vacuum_room_sizer"):
            self.vacuum_room_sizer.Add(
                wx.StaticText(self.vacuum_room_scroll, label="No rooms loaded yet. Press Refresh room list."),
                0,
                wx.ALL | wx.EXPAND,
                4,
            )
        status_lines = [message]
        if rooms:
            status_lines.append("Room checkboxes loaded. Tab through the room checkboxes; JAWS should read each room name, room ID, and checked state.")
        self.vacuum_room_status_txt.SetValue("\n".join(status_lines))
        if save:
            self._save_vacuum_rooms(self._selected_vacuum_entity_id(), rooms)
        if hasattr(self, "vacuum_room_scroll"):
            self.vacuum_room_scroll.Layout()
            self.vacuum_room_scroll.FitInside()
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

    def on_vacuum_clean_selected_rooms(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        selected_rooms = [
            getattr(check, "_viper_room", {})
            for check in getattr(self, "vacuum_room_checks", [])
            if check.GetValue()
        ]
        if not selected_rooms:
            self.notify("Check one or more rooms first.", priority=10)
            return
        segments = [room["segment"] for room in selected_rooms if "segment" in room]
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

    def _run_ha_service_async(self, service, payload, success_message, *, timeout=10, restore_focus_entity_id=""):
        def worker():
            ok = self._call_ha_service_data(service, payload, timeout=timeout)
            if ok:
                if restore_focus_entity_id:
                    self._pending_vacuum_focus_entity_id = restore_focus_entity_id
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
            "Speed diagnostics status. This read only box summarizes recent timing measurements from the Viper log.",
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
            "Home Assistant status. This read only box lists connection status, entity checks, and useful counts.",
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
                f"Roborock entities: {len(categories.get('roborock_entities', []))}",
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
        ice_entities = self._configured_ice_maker_entities()
        for label, entity_id in [
            ("Fridge door", "binary_sensor.refrigerator_fridge_door"),
            ("Freezer door", "binary_sensor.refrigerator_freezer_door"),
            ("Water filter", "sensor.refrigerator_water_filter_usage"),
            ("Ice maker switch", ice_entities["switch"]),
            ("Ice maker keep-on helper", ice_entities["keep_on"]),
            ("Ice usage counter", ice_entities["counter"]),
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

    def _refresh_prompt_choices(self):
        prompt_names = list(self.config.get("prompts", {}).keys()) or ["Standard"]
        for choice in (
            getattr(self, "prompt_choice", None),
            getattr(self, "prompt_default_choice", None),
            getattr(self, "prompt_front_choice", None),
            getattr(self, "prompt_back_choice", None),
        ):
            if not choice:
                continue
            current = choice.GetStringSelection()
            choice.Set(prompt_names)
            choice.SetStringSelection(current if current in prompt_names else self.config.get("active_prompt", prompt_names[0]))

    def _refresh_video_prompt_choices(self):
        prompt_names = list(self.config.get("video_prompts", {}).keys()) or ["Manual Outside Check"]
        for choice in (
            getattr(self, "video_prompt_choice", None),
            getattr(self, "video_prompt_manual_choice", None),
            getattr(self, "video_prompt_smart_choice", None),
            getattr(self, "video_prompt_detailed_choice", None),
        ):
            if not choice:
                continue
            current = choice.GetStringSelection()
            choice.Set(prompt_names)
            choice.SetStringSelection(current if current in prompt_names else self.config.get("active_video_prompt", prompt_names[0]))

    def on_prompt_assignment_change(self, event):
        default_prompt = self.prompt_default_choice.GetStringSelection()
        front_prompt = self.prompt_front_choice.GetStringSelection()
        back_prompt = self.prompt_back_choice.GetStringSelection()
        if default_prompt:
            self.config["active_prompt"] = default_prompt
        self.config["doorbell_prompt_profiles"] = {
            "front": front_prompt or default_prompt,
            "back": back_prompt or default_prompt,
        }
        self.save_config()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue(
                f"Saved still photo prompt assignment. Default: {default_prompt}. Front: {front_prompt}. Back: {back_prompt}."
            )
        self.notify("Still photo prompt assignment saved.", priority=10)

    def on_prompt_change(self, event):
        new_prompt = self.prompt_choice.GetStringSelection()
        self.prompt_editor.SetValue(self.config.get("prompts", {}).get(new_prompt, ""))
        self.notify(f"Loaded {new_prompt} profile")

    def on_save_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        txt = self.prompt_editor.GetValue().strip()
        if txt:
            self.config["prompts"][name] = txt
            self.save_config()
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Saved still photo prompt profile {name}.")
            self.notify(f"Saved {name}")

    def on_new_prompt(self, event):
        name = wx.GetTextFromUser("New Still Photo Prompt Name:", "New Still Photo Prompt")
        if name and name not in self.config["prompts"]:
            self.config["prompts"][name] = "Analyze frames for security."
            self.config["active_prompt"] = name
            self.save_config()
            self._refresh_prompt_choices()
            self.prompt_choice.SetStringSelection(name)
            if hasattr(self, "prompt_default_choice"):
                self.prompt_default_choice.SetStringSelection(name)
            self.prompt_editor.SetValue(self.config["prompts"][name])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Created still photo prompt profile {name}.")
            self.notify(f"Created {name}")

    def on_del_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        if len(self.config["prompts"]) > 1:
            del self.config["prompts"][name]
            new_a = list(self.config["prompts"].keys())[0]
            self.config["active_prompt"] = new_a
            profiles = self.config.setdefault("doorbell_prompt_profiles", {})
            if profiles.get("front") == name:
                profiles["front"] = new_a
            if profiles.get("back") == name:
                profiles["back"] = new_a
            self.save_config()
            self._refresh_prompt_choices()
            self.prompt_choice.SetStringSelection(new_a)
            self.prompt_editor.SetValue(self.config["prompts"][new_a])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Deleted still photo prompt profile {name}.")

    def on_video_prompt_assignment_change(self, event):
        manual_prompt = self.video_prompt_manual_choice.GetStringSelection()
        smart_prompt = self.video_prompt_smart_choice.GetStringSelection()
        detailed_prompt = self.video_prompt_detailed_choice.GetStringSelection()
        if manual_prompt:
            self.config["active_video_prompt"] = manual_prompt
        self.config["doorbell_video_prompt_profiles"] = {
            "manual": manual_prompt,
            "smart": smart_prompt,
            "detailed": detailed_prompt,
        }
        self.save_config()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue(
                f"Saved video prompt assignment. Manual: {manual_prompt}. Smart: {smart_prompt}. Detailed: {detailed_prompt}."
            )
        self.notify("Video prompt assignment saved.", priority=10)

    def on_save_video_prompts(self, event):
        assignments = {
            "manual": ("Manual Outside Check", getattr(self, "video_prompt_manual_editor", None)),
            "smart": ("Smart Follow Up", getattr(self, "video_prompt_smart_editor", None)),
            "detailed": ("Detailed Doorbell Video", getattr(self, "video_prompt_detailed_editor", None)),
        }
        prompts = self.config.setdefault("video_prompts", {})
        profiles = self.config.setdefault("doorbell_video_prompt_profiles", {})
        saved = []
        for mode, (fallback_name, editor) in assignments.items():
            if editor is None:
                continue
            name = profiles.get(mode) or fallback_name
            text = editor.GetValue().strip()
            if not text:
                continue
            prompts[name] = text
            profiles[mode] = name
            saved.append(mode)
        if saved:
            self.config["active_video_prompt"] = profiles.get("manual") or self.config.get("active_video_prompt", "")
            self.save_config()
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue("Saved video prompts for: " + ", ".join(saved) + ".")
            self.notify("Video prompts saved.", priority=10)
        else:
            self.notify("No video prompt text was saved. Each prompt needs text first.", priority=10)

    def on_video_prompt_change(self, event):
        name = self.video_prompt_choice.GetStringSelection()
        self.video_prompt_editor.SetValue(self.config.get("video_prompts", {}).get(name, ""))
        self.notify(f"Loaded video prompt {name}", priority=10)

    def on_save_video_prompt(self, event):
        name = self.video_prompt_choice.GetStringSelection()
        txt = self.video_prompt_editor.GetValue().strip()
        if txt:
            self.config.setdefault("video_prompts", {})[name] = txt
            self.save_config()
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Saved video prompt profile {name}.")
            self.notify(f"Saved video prompt {name}", priority=10)

    def on_new_video_prompt(self, event):
        name = wx.GetTextFromUser("New Video Prompt Name:", "New Video Prompt")
        if name and name not in self.config.setdefault("video_prompts", {}):
            self.config["video_prompts"][name] = (
                "Describe this doorbell video for a blind homeowner. Mention people, vehicles, packages, motion, and anything that needs attention. "
                "Use one or two complete sentences."
            )
            self.config["active_video_prompt"] = name
            self.save_config()
            self._refresh_video_prompt_choices()
            self.video_prompt_choice.SetStringSelection(name)
            if hasattr(self, "video_prompt_manual_choice"):
                self.video_prompt_manual_choice.SetStringSelection(name)
            self.video_prompt_editor.SetValue(self.config["video_prompts"][name])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Created video prompt profile {name}.")
            self.notify(f"Created video prompt {name}", priority=10)

    def on_del_video_prompt(self, event):
        name = self.video_prompt_choice.GetStringSelection()
        prompts = self.config.setdefault("video_prompts", {})
        if len(prompts) > 1:
            del prompts[name]
            new_a = list(prompts.keys())[0]
            self.config["active_video_prompt"] = new_a
            profiles = self.config.setdefault("doorbell_video_prompt_profiles", {})
            for key in ("manual", "smart", "detailed"):
                if profiles.get(key) == name:
                    profiles[key] = new_a
            self.save_config()
            self._refresh_video_prompt_choices()
            self.video_prompt_choice.SetStringSelection(new_a)
            self.video_prompt_editor.SetValue(prompts[new_a])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Deleted video prompt profile {name}.")

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

    def _configured_speaker_ids(self):
        return {
            str(data.get("id") or "")
            for data in self.config.get("speakers", {}).values()
            if isinstance(data, dict) and data.get("id")
        }

    def _speaker_candidate_lines(self, candidates, title):
        lines = [title]
        if not candidates:
            lines.append("  None found.")
            return lines
        configured_ids = self._configured_speaker_ids()
        for item in candidates:
            configured = "already configured" if item.get("id") in configured_ids else "available"
            lines.append(f"  {item.get('name')} | {item.get('type')} | {item.get('id')} | {configured}")
        return lines

    def _flatten_discovered_speaker_targets(self, ha_candidates, sonos_candidates):
        ha_sonos = [item for item in ha_candidates if item.get("is_sonos")]
        ha_other = [item for item in ha_candidates if not item.get("is_sonos")]
        ha_sonos_ids = {item.get("id") for item in ha_sonos}
        network_sonos = [
            item for item in sonos_candidates
            if item.get("id") not in ha_sonos_ids
        ]
        configured_ids = self._configured_speaker_ids()
        targets = []
        for item in ha_other + ha_sonos + network_sonos:
            target = dict(item)
            target["configured"] = target.get("id") in configured_ids
            targets.append(target)
        return targets

    def _unique_speaker_name(self, base_name, spk_type):
        speakers = self.config.setdefault("speakers", {})
        base = f"{base_name} ({str(spk_type or 'ha').upper()})"
        name = base
        suffix = 2
        while name in speakers:
            name = f"{base} {suffix}"
            suffix += 1
        return name

    def _add_discovered_speaker_targets(self, targets, routes=None):
        speakers = self.config.setdefault("speakers", {})
        existing_ids = self._configured_speaker_ids()
        routes = routes or {}
        added = 0
        for item in targets or []:
            spk_id = item.get("id") or ""
            if not spk_id or spk_id in existing_ids:
                continue
            spk_type = item.get("type") or "ha"
            name = self._unique_speaker_name(item.get("name") or spk_id, spk_type)
            speakers[name] = {
                "id": spk_id,
                "type": spk_type,
                "enabled": True,
                "doorbell": bool(routes.get("doorbell", True)),
                "utilities": bool(routes.get("utilities", True)),
                "fridge": bool(routes.get("fridge", True)),
                "quiet_hours_exempt": bool(routes.get("quiet_hours_exempt", False)),
            }
            existing_ids.add(spk_id)
            added += 1
        if added:
            self.save_config()
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()
            self.refresh_setup_checklist()
        return added

    def _discovered_speaker_summary_text(self, ha_candidates, sonos_candidates, ha_error="", sonos_error=""):
        ha_sonos = [item for item in ha_candidates if item.get("is_sonos")]
        ha_other = [item for item in ha_candidates if not item.get("is_sonos")]
        ha_sonos_ids = {item.get("id") for item in ha_sonos}
        network_sonos = [
            item for item in sonos_candidates
            if item.get("id") not in ha_sonos_ids
        ]
        lines = [
            "Available Speakers",
            "",
            "Check the speakers Viper should add, then press Add Selected Speakers.",
            "",
        ]
        if ha_error:
            lines.append(f"Home Assistant discovery: {ha_error}")
            lines.append("")
        if sonos_error:
            lines.append(f"Network Sonos discovery: {sonos_error}")
            lines.append("")
        lines.extend(self._speaker_candidate_lines(ha_other, "Home Assistant media players:"))
        lines.append("")
        lines.extend(self._speaker_candidate_lines(ha_sonos, "Sonos speakers already visible in Home Assistant:"))
        lines.append("")
        lines.extend(self._speaker_candidate_lines(network_sonos, "Network Sonos speakers not clearly visible in Home Assistant:"))
        return "\n".join(lines)

    def _ha_speaker_candidates_from_result(self, result):
        categories = result.get("categories", {}) if isinstance(result, dict) else {}
        candidates = []
        for entity in categories.get("media_players", []):
            entity_id = entity.get("entity_id") or ""
            if not entity_id:
                continue
            name = entity.get("friendly_name") or entity_id.replace("media_player.", "")
            platform = (entity.get("platform") or entity.get("integration") or "").lower()
            search = " ".join(str(entity.get(key, "")) for key in ("entity_id", "friendly_name", "platform", "integration")).lower()
            spk_type = "alexa" if "alexa" in search or "echo" in search else "ha"
            if platform == "sonos" or "sonos" in search:
                spk_type = "ha"
            candidates.append({
                "name": name,
                "id": entity_id,
                "type": spk_type,
                "source": "Home Assistant",
                "is_sonos": platform == "sonos" or "sonos" in search,
            })
        return candidates

    def _sonos_speaker_candidates_from_soco(self, speakers):
        candidates = []
        for speaker in speakers or []:
            ip = getattr(speaker, "ip_address", "") or ""
            name = getattr(speaker, "player_name", "") or ip or "Unnamed Sonos"
            if not ip:
                continue
            candidates.append({
                "name": name,
                "id": ip,
                "type": "sonos",
                "source": "Network Sonos",
                "is_sonos": True,
            })
        return candidates

    def on_discover_speakers(self, event):
        self.notify("Discovering available speakers. Viper will let you choose which speakers to add.", priority=10)
        safe_submit(self._run_discover_speakers)

    def _run_discover_speakers(self):
        ha_result = discovery.discover_ha_entities(timeout=5)
        ha_candidates = []
        ha_error = ""
        if ha_result.get("ok"):
            ha_candidates = self._ha_speaker_candidates_from_result(ha_result)
        else:
            ha_error = ha_result.get("message") or "Home Assistant speaker discovery failed."

        sonos_candidates = []
        sonos_error = ""
        try:
            sonos_candidates = self._sonos_speaker_candidates_from_soco(soco.discover())
        except Exception as e:
            sonos_error = f"Network Sonos discovery failed: {e}"

        wx.CallAfter(self._show_discovered_speakers, ha_candidates, sonos_candidates, ha_error, sonos_error)

    def _show_discovered_speakers(self, ha_candidates, sonos_candidates, ha_error="", sonos_error="", parent_window=None):
        summary_text = self._discovered_speaker_summary_text(ha_candidates, sonos_candidates, ha_error, sonos_error)
        self.notify(
            f"Speaker discovery complete. Found {len(ha_candidates)} Home Assistant speaker target(s) and {len(sonos_candidates)} network Sonos speaker(s).",
            priority=10,
        )
        targets = self._flatten_discovered_speaker_targets(ha_candidates, sonos_candidates)
        logging.info(
            "[SPEAKER DISCOVERY] ha_candidates=%d sonos_candidates=%d addable=%d ha_error=%r sonos_error=%r",
            len(ha_candidates or []),
            len(sonos_candidates or []),
            len([item for item in targets if not item.get("configured")]),
            ha_error,
            sonos_error,
        )
        try:
            window_ready = isinstance(self, wx.Window) and bool(self.GetHandle())
        except Exception:
            window_ready = False
        if not window_ready:
            self._show_text_dialog("Available Speakers", summary_text)
            return
        parent = parent_window if isinstance(parent_window, wx.Window) else self
        dlg = DiscoveredSpeakersDialog(parent, targets, summary_text)
        try:
            try:
                dlg.Raise()
                dlg.SetFocus()
            except Exception:
                pass
            if dlg.ShowModal() == wx.ID_OK:
                added = self._add_discovered_speaker_targets(dlg.selected_targets, getattr(dlg, "selected_routes", None))
                if added:
                    self.notify(f"Added {added} speaker target(s). They are enabled for doorbell, utility, and fridge/freezer alerts.", priority=10)
                else:
                    self.notify("No new speakers were selected or added.", priority=10)
        finally:
            dlg.Destroy()

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

    def on_show_about(self, event):
        diag = diagnostics.collect_diagnostics(
            self.config,
            ha_listener_status=self.ha_listener.status() if hasattr(self, "ha_listener") else {},
        )
        remote_url = f"http://localhost:{cfg.FLASK_PORT}/remote"
        text = "\n".join(
            [
                "Viper Vision",
                f"Version: {diagnostics.APP_VERSION}",
                "",
                "Build and runtime:",
                f"Frozen installer build: {'yes' if diag['app']['frozen'] else 'no'}",
                f"Python: {diag['app']['python']}",
                f"Platform: {diag['app']['platform']}",
                f"Executable: {diag['app']['executable']}",
                "",
                "Folders and files:",
                f"Application folder: {diag['paths']['app_dir']}",
                f"Data folder: {diag['paths']['data_dir']}",
                f"Config file: {diag['paths']['config_file']}",
                f"Main log file: {diag['paths']['log_file']}",
                f"Chimes folder: {cfg.CHIMES_DIR}",
                f"Support bundles save in: {cfg.DATA_DIR}",
                "",
                "Local remote:",
                remote_url,
                "",
                "Privacy:",
                "Diagnostics and support bundles stay on this computer unless you choose to share them.",
                "Support bundles redact Home Assistant tokens, Gemini keys, Pushover keys, MQTT passwords, RTSP passwords, and Ring identifiers.",
            ]
        )
        self._show_about_dialog(text, remote_url)

    def _show_about_dialog(self, text, remote_url):
        dlg = wx.Dialog(self, title="About Viper Vision", size=(760, 560))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.TextCtrl(panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self._describe_control(box, "About Viper Vision. Read-only version, folder, config, log, and privacy information.")
        sizer.Add(box, 1, wx.ALL | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy_data_btn = wx.Button(panel, label="Copy Data Folder")
        open_data_btn = wx.Button(panel, label="Open Data Folder")
        open_remote_btn = wx.Button(panel, label="Open Remote")
        close_btn = wx.Button(panel, label="Close")
        self._describe_control(copy_data_btn, "Copy Data Folder button. Copies Viper's writable data folder path.")
        self._describe_control(open_data_btn, "Open Data Folder button. Opens the folder containing Viper config, logs, chimes, and support bundles.")
        self._describe_control(open_remote_btn, "Open Remote button. Opens Viper's local web remote in your browser.")
        self._describe_control(close_btn, "Close About dialog button.")

        def copy_data_folder(_event):
            value = str(cfg.DATA_DIR)
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(value))
                finally:
                    wx.TheClipboard.Close()
            self.notify("Viper data folder path copied.", priority=10)

        copy_data_btn.Bind(wx.EVT_BUTTON, copy_data_folder)
        open_data_btn.Bind(wx.EVT_BUTTON, lambda _event: open_url(str(cfg.DATA_DIR)))
        open_remote_btn.Bind(wx.EVT_BUTTON, lambda _event: open_url(remote_url))
        close_btn.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        for button in (copy_data_btn, open_data_btn, open_remote_btn, close_btn):
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        panel.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def on_run_diagnostics(self, event):
        self.notify("Running diagnostics...", priority=10)
        safe_submit(self._run_diagnostics)

    def refresh_health_summary(self):
        try:
            diag = _current_diagnostics(check_ha=False)
            text = diagnostics.health_summary_text(diag)
        except Exception as e:
            logging.exception("Health summary refresh failed")
            text = f"Health summary failed: {e}"
        if hasattr(self, "diagnostics_health_txt"):
            self.diagnostics_health_txt.SetValue(text)
        return text

    def on_refresh_health_summary(self, event):
        text = self.refresh_health_summary()
        first_line = text.splitlines()[0] if text else "Health summary refreshed."
        self.notify(first_line, priority=10)

    def _smoke_result_line(self, label, ok, detail="", fix=""):
        state = "PASS" if ok else "FIX"
        parts = [f"{state}: {label}"]
        if detail:
            parts.append(str(detail))
        if not ok and fix:
            parts.append(f"Next: {fix}")
        return ". ".join(parts)

    def _smoke_support_bundle_probe(self):
        probe_dir = cfg.DATA_DIR / "smoke_test"
        result = diagnostics.create_support_bundle(
            self.config,
            ha_listener_status=self.ha_listener.status() if hasattr(self, "ha_listener") else {},
            output_dir=probe_dir,
        )
        path = Path(result.get("path") or "")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return bool(result.get("ok"))

    def _collect_safe_smoke_results(self):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha = runtime["home_assistant"]
        api = runtime["api"]
        doorbell = runtime["doorbell"]
        speakers = runtime["speakers"]
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        diag = diagnostics.collect_diagnostics(
            self.config,
            ha_listener_status=listener,
        )
        health = diag.get("health", {})
        results = []

        results.append(("Config file", cfg.CONFIG_FILE.exists(), str(cfg.CONFIG_FILE), "Save settings from the app once."))
        results.append(("Home Assistant host", bool(ha.get("ha_ip")), ha.get("ha_ip") or "missing", "Open Setup Wizard and enter the Home Assistant address."))
        results.append(("Home Assistant token", bool(ha.get("ha_token")), "available" if ha.get("ha_token") else "missing", "Paste a long-lived token in setup or restore the saved secret."))
        results.append(("Gemini key", bool(api.get("gemini_api_key")), "available" if api.get("gemini_api_key") else "missing", "Add a Gemini key or choose non-Gemini speech/vision options."))
        results.append(("FFmpeg", bool(diag.get("ffmpeg", {}).get("available")), diag.get("ffmpeg", {}).get("resolved") or "not found", "Install FFmpeg or check the configured FFmpeg path."))
        results.append(("HA listener", bool(listener.get("connected")), listener.get("last_error") or listener.get("last_host") or "connected", "Check Home Assistant network/token, then restart or wait for reconnect."))

        if ha.get("ha_ip") and ha.get("ha_token"):
            ha_connection = discovery.test_ha_connection(
                token=ha.get("ha_token"),
                ha_ip=ha.get("ha_ip"),
                ha_port=ha.get("ha_port") or "8123",
                timeout=5,
            )
            results.append(("HA API", bool(ha_connection.get("ok")), ha_connection.get("message") or ha_connection.get("error") or "", "Check Home Assistant address, token, and whether HA Core is running."))
        else:
            results.append(("HA API", False, "host or token missing", "Complete Home Assistant setup first."))

        results.append(("Front door trigger", bool(doorbell.get("front_trigger_entity_id")), doorbell.get("front_trigger_entity_id") or "missing", "Choose the front Ring trigger entity in Doorbell Vision setup."))
        results.append(("Back door trigger", bool(doorbell.get("back_trigger_entity_id")), doorbell.get("back_trigger_entity_id") or "missing", "Choose the back Ring trigger entity in Doorbell Vision setup."))
        front_rtsp = doorbell.get("configured_rtsp_front") or doorbell.get("raw_rtsp_front") or ""
        back_rtsp = doorbell.get("configured_rtsp_back") or doorbell.get("raw_rtsp_back") or ""
        results.append(("Front camera RTSP URL", bool(front_rtsp), front_rtsp or "missing", "Find and save a front Ring-MQTT live stream."))
        results.append(("Back camera RTSP URL", bool(back_rtsp), back_rtsp or "missing", "Find and save a back Ring-MQTT live stream."))

        routes = speakers.get("routes", {})
        results.append(("Speaker routes", bool(speakers.get("enabled_count") and routes.get("doorbell") and routes.get("utilities") and routes.get("fridge")), f"{speakers.get('enabled_count', 0)} enabled; doorbell {len(routes.get('doorbell', []))}, utilities {len(routes.get('utilities', []))}, fridge {len(routes.get('fridge', []))}", "Use Choose Alert Speakers and enable doorbell, utilities, and fridge/freezer routes."))
        results.append(("Manual broadcast route", bool(speakers.get("enabled_count")), f"{speakers.get('enabled_count', 0)} enabled speaker(s)", "Add or enable at least one speaker."))
        results.append(("Support bundle", self._smoke_support_bundle_probe(), "temporary bundle created and removed", "Check write permission in the Viper data folder."))
        results.append(("Active health issues", not health.get("active_issues"), "; ".join(health.get("active_issues") or ["none"]), "Open the Health Summary and fix listed active issues."))
        return results

    def _camera_rtsp_url_for_side(self, side):
        triggers = self.config.get("doorbell_triggers", {})
        trigger = triggers.get(side, {}) if isinstance(triggers, dict) and isinstance(triggers.get(side), dict) else {}
        return str(trigger.get("rtsp_url") or self.config.get("rtsp_front" if side == "front" else "rtsp_back") or "").strip()

    def on_run_safe_smoke_test(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Running safe smoke test. This does not play audio or trigger doorbell flows.")
        self.notify("Running safe smoke test.", priority=10)
        safe_submit(self._run_safe_smoke_test)

    def _run_safe_smoke_test(self):
        try:
            lines = self._format_safe_smoke_report(self._collect_safe_smoke_results())
        except Exception as e:
            logging.exception("Safe smoke test failed")
            lines = f"Smoke Test: ERROR\n\nThe smoke test itself failed: {e}\nNext: Create a support report and send the diagnostics zip."
        wx.CallAfter(self._finish_safe_smoke_test, lines)

    def _format_safe_smoke_report(self, results):
        failed = [item for item in results if not item[1]]
        lines = [
            f"Smoke Test: {'PASS' if not failed else 'NEEDS ATTENTION'}",
            f"Passed {len(results) - len(failed)} of {len(results)} checks.",
            "",
        ]
        for label, ok, detail, fix in results:
            lines.append(self._smoke_result_line(label, ok, detail, fix))
        lines.append("")
        if failed:
            lines.append("Most important next step:")
            lines.append(failed[0][3] or f"Fix {failed[0][0]}.")
        else:
            lines.append("Optional next step: use the camera/audio buttons below for live hardware confirmation.")
        return "\n".join(lines)

    def _finish_safe_smoke_test(self, text):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue(text)
        if hasattr(self, "diagnostics_health_txt"):
            self.refresh_health_summary()
        first_line = str(text or "").splitlines()[0] if text else "Smoke test finished."
        self.notify(first_line, priority=10)

    def on_test_diagnostics_camera(self, event, side):
        url = self._camera_rtsp_url_for_side(side)
        if not url:
            message = f"{side.title()} camera is not configured. Save a Ring-MQTT RTSP stream first."
            if hasattr(self, "smoke_test_txt"):
                self.smoke_test_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue(f"Testing {side} camera frame capture.")
        self.notify(f"Testing {side} camera frame.", priority=10)
        safe_submit(self._run_diagnostics_camera_test, side, url)

    def _run_diagnostics_camera_test(self, side, url):
        try:
            frame = vision.grab_frame(url, cfg.DATA_DIR / "rtsp_test", f"diagnostics_{side}", min_bytes=14000, timeout=8)
            ok = bool(frame)
            message = f"{side.title()} camera frame test {'passed' if ok else 'failed'}."
            if frame:
                message += f" Captured: {Path(frame).name}"
            else:
                message += " No usable frame was captured."
        except Exception as e:
            logging.exception("Diagnostics camera test failed side=%s", side)
            message = f"{side.title()} camera frame test failed. {e}"
        wx.CallAfter(self._finish_diagnostics_action, message)

    def on_test_diagnostics_manual_broadcast(self, event):
        message = "Viper smoke test broadcast."
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Sending manual broadcast smoke test.")
        self.notify("Sending manual broadcast smoke test.", priority=10)
        safe_submit(self._run_diagnostics_manual_broadcast, message)

    def _run_diagnostics_manual_broadcast(self, message):
        result = _dispatch_broadcast_message(message, channel="manual")
        ok = bool(result.get("ok"))
        text = f"Manual broadcast test {'sent' if ok else 'failed'}. {result.get('message') or ''}"
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_test_diagnostics_chime(self, event, channel):
        label = channel.replace("_", " ").title()
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue(f"Sending {label} chime test.")
        self.notify(f"Testing {label} chime.", priority=10)
        safe_submit(self._run_diagnostics_chime, channel)

    def _run_diagnostics_chime(self, channel):
        try:
            ch_settings = self.config.get("broadcast_channels", {}).get(channel, {})
            chime = ch_settings.get("chime", "")
            audio.play_broadcast_chime(chime, channel)
            text = f"{channel.replace('_', ' ').title()} chime test sent."
        except Exception as e:
            logging.exception("Diagnostics chime test failed channel=%s", channel)
            text = f"{channel.replace('_', ' ').title()} chime test failed. {e}"
        wx.CallAfter(self._finish_diagnostics_action, text)

    def _finish_diagnostics_action(self, message):
        if hasattr(self, "smoke_test_txt"):
            current = self.smoke_test_txt.GetValue()
            prefix = current.strip()
            self.smoke_test_txt.SetValue((prefix + "\n\n" if prefix else "") + str(message))
        self.notify(str(message), priority=10)

    def _run_diagnostics(self):
        try:
            diag = _current_diagnostics(check_ha=True)
            text = diagnostics.diagnostics_text(diag)
            wx.CallAfter(self._show_text_dialog, "Viper Vision Diagnostics", text)
        except Exception as e:
            logging.exception("Diagnostics failed")
            wx.CallAfter(self.notify, f"Diagnostics failed: {e}", priority=10)

    def on_create_support_bundle(self, event):
        self.on_create_support_report(event)

    def on_create_support_report(self, event):
        self.record_setup_event("support_report_start", "Creating support report.")
        self.notify("Creating support bundle...", priority=10)
        safe_submit(self._run_support_bundle)

    def _run_support_bundle(self):
        try:
            diag = _current_diagnostics(check_ha=True)
            result = diagnostics.create_support_bundle(
                self.config,
                ha_listener_status=diag.get("ha_listener", {}),
                ha_connection=diag.get("ha_connection", {}),
                ha_health=diag.get("ha_health", {}),
                setup_summary=self.build_setup_checklist_summary(),
                setup_events=self.setup_events,
                last_setup_status=self.last_setup_status,
            )
            self.record_setup_event("support_report_created", "Support report bundle created.", path=result.get("path", ""))
            wx.CallAfter(self.notify, f"Support report created: {result['path']}", priority=10)
            wx.CallAfter(self._show_support_report_dialog, result)
        except Exception as e:
            logging.exception("Support bundle failed")
            self.record_setup_event("support_report_failed", str(e))
            wx.CallAfter(self.notify, f"Support bundle failed: {e}", priority=10)

    def _support_email_url(self, bundle_path):
        subject = "Viper Vision Support Report"
        body = "\n".join([
            "Hi,",
            "",
            "I created a Viper Vision support report. Please attach this zip file before sending:",
            str(bundle_path or ""),
            "",
            f"Viper version: {diagnostics.APP_VERSION}",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            "",
            "Notes about what went wrong:",
            "",
        ])
        return f"mailto:{SUPPORT_EMAIL}?subject={quote(subject)}&body={quote(body)}"

    def _open_support_email_draft(self, bundle_path):
        self.record_setup_event("support_email_draft_open", "Opening support email draft.")
        open_url(self._support_email_url(bundle_path))

    def _show_support_report_dialog(self, result):
        path = result.get("path", "") if isinstance(result, dict) else ""
        included = result.get("included", []) if isinstance(result, dict) else []
        dlg = wx.Dialog(self, title="Support Report Created", size=(760, 520))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        text = "\n".join([
            "Support report created.",
            "",
            path,
            "",
            "This zip includes redacted diagnostics, setup status, setup event history, recent logs, API usage, and crash information if present.",
            "Secrets are redacted, but review the zip before sharing.",
            "Press Open Email Draft to start an email to the Viper developer. Attach the zip file manually before sending.",
            "",
            f"Files included: {len(included)}",
        ])
        box = wx.TextCtrl(panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self._describe_control(box, "Support report details. Read only. Shows the support zip path, what was included, and how to email it.")
        sizer.Add(box, 1, wx.ALL | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy_btn = wx.Button(panel, label="Copy Zip Path")
        folder_btn = wx.Button(panel, label="Open Folder")
        email_btn = wx.Button(panel, label="Open Email Draft")
        close_btn = wx.Button(panel, label="Close")
        self._describe_control(copy_btn, "Copy Zip Path button. Copies the support zip path to the clipboard.")
        self._describe_control(folder_btn, "Open Folder button. Opens the folder containing the support zip.")
        self._describe_control(email_btn, "Open Email Draft button. Opens an email draft addressed to the Viper developer. Attach the zip manually.")
        self._describe_control(close_btn, "Close support report dialog button. Closes this support report dialog.")

        def copy_path(_event):
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(path))
                finally:
                    wx.TheClipboard.Close()
            self.notify("Support report path copied.", priority=10)

        def open_folder(_event):
            folder = str(Path(path).parent) if path else str(cfg.DATA_DIR)
            open_url(folder)

        copy_btn.Bind(wx.EVT_BUTTON, copy_path)
        folder_btn.Bind(wx.EVT_BUTTON, open_folder)
        email_btn.Bind(wx.EVT_BUTTON, lambda _event: self._open_support_email_draft(path))
        close_btn.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        for button in (copy_btn, folder_btn, email_btn, close_btn):
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        panel.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

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
        self.notify("Scanning network for Sonos. This will only show what is available.", priority=10)
        safe_submit(self._run_scan_sonos)

    def _run_scan_sonos(self):
        try:
            speakers = soco.discover()
            if not speakers:
                self.notify("No Sonos found.", priority=10)
                return
            candidates = self._sonos_speaker_candidates_from_soco(speakers)
            wx.CallAfter(self._show_discovered_speakers, [], candidates, "", "")
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
        self.notify("Scanning HA for speakers. This will only show what is available.", priority=10)
        safe_submit(self._run_scan_ha)

    def _run_scan_ha(self):
        result = discovery.discover_ha_entities(timeout=5)
        if not result.get("ok"):
            msg = result.get("message") or "HA scan failed."
            wx.CallAfter(self.notify, msg, priority=10)
            return
        wx.CallAfter(self._show_discovered_speakers, self._ha_speaker_candidates_from_result(result), [], "", "")

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


        ice_box = wx.StaticBox(self.tab_fridge, label="Ice Maker")
        ice_sizer = wx.StaticBoxSizer(ice_box, wx.VERTICAL)
        self.ice_maker_status_txt = AccessibleStatusText(
            self.tab_fridge,
            value="Checking ice maker status...",
            size=(-1, 105),
        )
        self._describe_control(
            self.ice_maker_status_txt,
            "Ice maker status. Shows whether the ice maker switch is on or off, whether the keep-on helper is active, and the current Home Assistant ice usage counter.",
        )
        ice_sizer.Add(self.ice_maker_status_txt, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_ice_toggle = wx.Button(self.tab_fridge, label="Turn Ice Maker On", size=(-1, 40))
        self.btn_ice_toggle.Bind(wx.EVT_BUTTON, self.on_ice_maker_toggle)
        self._describe_control(
            self.btn_ice_toggle,
            "Ice maker toggle button. The label changes to Turn Ice Maker Off when Home Assistant reports the ice maker is on.",
        )
        ice_sizer.Add(self.btn_ice_toggle, 0, wx.ALL | wx.EXPAND, 5)
        outer.Add(ice_sizer, 0, wx.ALL | wx.EXPAND, 10)
        wx.CallAfter(self.refresh_ice_maker_status)

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
            "mode":  _normalize_broadcast_mode(ctrl["mode"].GetStringSelection()),
            "chime": "" if chime == "(Default)" else chime,
        }
        self.save_config()

    def on_save_fridge_settings(self, event):
        channels = self.config.setdefault("broadcast_channels", {})
        for ch_key, ctrl in self._fridge_controls.items():
            chime = ctrl["chime"].GetStringSelection()
            channels[ch_key] = {
                "mode":  _normalize_broadcast_mode(ctrl["mode"].GetStringSelection()),
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
        safe_submit(audio.play_broadcast_chime, chime_file, ch_key)
        label = ch_key.replace("_", " ").title()
        self.notify(f"Testing {label} chime.", priority=10)

    def _call_ha_service(self, domain_service: str, entity_id: str):
        """Call a Home Assistant service for a single entity."""
        return self._call_ha_service_data(domain_service, {"entity_id": entity_id})

    def _configured_ice_maker_entities(self):
        return {
            "switch": self.config.get("ice_maker_switch_entity") or cfg.ICE_MAKER_SWITCH_ENTITY,
            "keep_on": self.config.get("ice_maker_keep_on_entity") or cfg.ICE_MAKER_KEEP_ON_ENTITY,
            "counter": self.config.get("ice_maker_counter_entity") or cfg.ICE_MAKER_COUNTER_ENTITY,
        }

    def _get_ha_entity_state(self, entity_id: str, *, timeout=5):
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            return {"ok": False, "exists": False, "message": "Entity id is blank."}
        try:
            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                raise RuntimeError("Home Assistant host or token is missing.")
            response = requests.get(
                f"http://{ha_ip}:{ha_port}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
            if response.status_code == 404:
                return {"ok": True, "exists": False, "entity_id": entity_id, "message": "Entity was not found."}
            response.raise_for_status()
            return {"ok": True, "exists": True, "entity_id": entity_id, "entity": response.json()}
        except Exception as e:
            return {"ok": False, "exists": False, "entity_id": entity_id, "message": str(e)}

    def get_ice_maker_status(self, *, timeout=5):
        entities = self._configured_ice_maker_entities()
        switch = self._get_ha_entity_state(entities["switch"], timeout=timeout)
        keep_on = self._get_ha_entity_state(entities["keep_on"], timeout=timeout)
        counter = self._get_ha_entity_state(entities["counter"], timeout=timeout)

        switch_state = str((switch.get("entity") or {}).get("state") or "").strip().lower() if switch.get("exists") else ""
        keep_on_state = str((keep_on.get("entity") or {}).get("state") or "").strip().lower() if keep_on.get("exists") else ""
        counter_state = str((counter.get("entity") or {}).get("state") or "").strip() if counter.get("exists") else ""
        is_on = switch_state == "on"
        is_off = switch_state == "off"
        counter_text = counter_state if counter_state else ("missing" if counter.get("ok") else f"unknown: {counter.get('message')}")
        if is_on:
            summary = f"on. Keep-on helper is {keep_on_state or 'unknown'}."
            button_label = "Turn Ice Maker Off"
        elif is_off:
            summary = f"off. Keep-on helper is {keep_on_state or 'unknown'}."
            button_label = "Turn Ice Maker On"
        elif switch.get("ok"):
            summary = f"state is {switch_state or 'missing'}."
            button_label = "Turn Ice Maker On"
        else:
            summary = f"status unknown: {switch.get('message') or 'could not reach Home Assistant'}."
            button_label = "Turn Ice Maker On"
        return {
            "ok": bool(switch.get("ok") and counter.get("ok")),
            "switch_entity": entities["switch"],
            "keep_on_entity": entities["keep_on"],
            "counter_entity": entities["counter"],
            "switch_state": switch_state or "unknown",
            "keep_on_state": keep_on_state or "unknown",
            "counter_state": counter_state,
            "counter_text": counter_text,
            "is_on": is_on,
            "button_label": button_label,
            "summary": summary,
            "message": self._format_ice_maker_status(summary, counter_text, entities),
        }

    def _format_ice_maker_status(self, summary, counter_text, entities):
        return "\n".join(
            [
                f"Ice maker is {summary}",
                f"Ice usage counter: {counter_text}.",
                f"Switch entity: {entities['switch']}",
                f"Keep-on helper: {entities['keep_on']}",
                f"Counter entity: {entities['counter']}",
            ]
        )

    def refresh_ice_maker_status(self, announce=False):
        if hasattr(self, "ice_maker_status_txt"):
            self.ice_maker_status_txt.SetValue("Checking ice maker status...")
        safe_submit(self._run_ice_maker_status_check, announce)

    def _run_ice_maker_status_check(self, announce=False):
        status = self.get_ice_maker_status(timeout=5)
        wx.CallAfter(self._finish_ice_maker_status, status, announce)

    def _finish_ice_maker_status(self, status, announce=False):
        self._ice_maker_switch_state = status.get("switch_state", "unknown")
        if hasattr(self, "ice_maker_status_txt"):
            self.ice_maker_status_txt.SetValue(status.get("message") or "Ice maker status unavailable.")
        if hasattr(self, "btn_ice_toggle"):
            label = status.get("button_label") or "Turn Ice Maker On"
            self.btn_ice_toggle.SetLabel(label)
            self.btn_ice_toggle.SetName(label)
            self.btn_ice_toggle.SetToolTip(f"{label}. Current ice maker status: {status.get('summary', 'unknown')}")
        if announce:
            self.notify(status.get("message") or "Ice maker status refreshed.", priority=10)

    def _call_ha_service_data(self, domain_service: str, data: dict, *, timeout=10):
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
            response = requests.post(
                f"http://{ha_ip}:{ha_port}/api/services/{domain_service}",
                headers=headers,
                json=data or {},
                timeout=timeout,
            )
            response.raise_for_status()
            return True
        except requests.exceptions.ReadTimeout:
            self.notify(
                f"Home Assistant did not answer within {timeout} seconds for {entity_id}. "
                "The Roborock integration can be slow; press Refresh vacuum controls to check whether the setting changed.",
                priority=10,
            )
            return False
        except requests.exceptions.HTTPError as e:
            if _is_hidden_vacuum_setting_entity_id(entity_id):
                self.notify(
                    "Home Assistant reports that Roborock dock empty mode exists, but its integration rejects write attempts. "
                    "Viper hides this control from the vacuum tab; change it in Home Assistant until the integration exposes a reliable service.",
                    priority=10,
                )
            else:
                self.notify(f"HA service failed for {entity_id}: {e}", priority=10)
            return False
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

    def on_test_doorbell_full_flow(self, event, side: str):
        side = "back" if side == "back" else "front"
        label = "back" if side == "back" else "front"
        self.notify(f"Starting {label} doorbell full flow test through Home Assistant.", priority=10)
        safe_submit(self._run_doorbell_full_flow_test, side)

    def _run_doorbell_full_flow_test(self, side: str):
        side = "back" if side == "back" else "front"
        label = "Back" if side == "back" else "Front"
        try:
            triggers = ha_listener.normalize_doorbell_triggers(self.config)
            trigger = triggers.get(side, {})
            other_side = "front" if side == "back" else "back"
            other_trigger = triggers.get(other_side, {})
            entity_id = trigger.get("trigger_entity_id") or ""
            rtsp_url = trigger.get("rtsp_url") or _doorbell_rtsp_for_key(side)
            if not self.config.get("ha_listener_enabled", True):
                wx.CallAfter(self.notify, "Home Assistant listener is disabled. Turn it on before running the doorbell full flow test.", 10)
                return
            if hasattr(self, "ha_listener"):
                status = self.ha_listener.status()
                if not status.get("connected"):
                    error = status.get("last_error") or "not connected"
                    wx.CallAfter(self.notify, f"Home Assistant listener is not connected: {error}. The full flow test was not sent.", 10)
                    return
            if not trigger.get("enabled"):
                wx.CallAfter(self.notify, f"{label} doorbell trigger is not enabled. Save a trigger entity and RTSP URL in Home Assistant Setup first.", 10)
                return
            if not entity_id:
                wx.CallAfter(self.notify, f"{label} doorbell trigger entity is missing. Choose it in Home Assistant Setup first.", 10)
                return
            other_entity_id = other_trigger.get("trigger_entity_id") or ""
            if other_trigger.get("enabled") and other_entity_id and other_entity_id == entity_id:
                wx.CallAfter(
                    self.notify,
                    f"{label} doorbell full flow test was not sent because front and back use the same Home Assistant trigger entity. Open Home Assistant Setup and choose separate front and back trigger entities.",
                    10,
                )
                return
            if not rtsp_url:
                wx.CallAfter(self.notify, f"{label} doorbell RTSP URL is missing. Add and test the camera URL first.", 10)
                return

            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                wx.CallAfter(self.notify, "Home Assistant host or token is missing. Open Home Assistant Setup first.", 10)
                return

            active_states = trigger.get("active_states") or ha_listener.DEFAULT_ACTIVE_STATES
            active_state = str(active_states[0] if active_states else "on")
            now = datetime.now().isoformat(timespec="seconds")
            payload = {
                "entity_id": entity_id,
                "old_state": {
                    "entity_id": entity_id,
                    "state": "off",
                    "attributes": {"friendly_name": f"Viper {label} Doorbell Test"},
                    "last_changed": now,
                    "last_updated": now,
                },
                "new_state": {
                    "entity_id": entity_id,
                    "state": active_state,
                    "attributes": {
                        "friendly_name": f"Viper {label} Doorbell Test",
                        "viper_test": True,
                    },
                    "last_changed": now,
                    "last_updated": now,
                },
            }
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            response = requests.post(
                f"http://{ha_ip}:{ha_port}/api/events/state_changed",
                headers=headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logging.info(
                "[HA SETUP] Fired synthetic %s doorbell state_changed event entity=%s active_state=%s rtsp_configured=%s",
                side,
                entity_id,
                active_state,
                bool(rtsp_url),
            )
            wx.CallAfter(
                self.notify,
                f"{label} doorbell test event sent through Home Assistant. Watch for chime, camera capture, AI verdict, and speech.",
                10,
            )
        except Exception as e:
            logging.exception("[HA SETUP] Doorbell full flow test failed side=%s", side)
            wx.CallAfter(self.notify, f"{label} doorbell full flow test failed: {e}", 10)

    def on_ice_maker_on(self, event):
        """Force the ice maker on and enable the helper so the 5-second auto-off
        automation does not shut it back off."""
        entities = self._configured_ice_maker_entities()
        ok_helper = self._call_ha_service("input_boolean/turn_on", entities["keep_on"])
        ok_switch = self._call_ha_service("switch/turn_on", entities["switch"])
        if ok_helper and ok_switch:
            msg = "Ice maker turned on with refill override enabled."
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)
            wx.CallLater(750, self.refresh_ice_maker_status)
            return msg
        return "Ice maker on request failed. Check Home Assistant status."

    def on_ice_maker_off(self, event):
        """Turn the ice maker off and clear the helper override."""
        entities = self._configured_ice_maker_entities()
        ok_switch = self._call_ha_service("switch/turn_off", entities["switch"])
        ok_helper = self._call_ha_service("input_boolean/turn_off", entities["keep_on"])
        if ok_switch and ok_helper:
            msg = "Ice maker turned off and refill override cleared."
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)
            wx.CallLater(750, self.refresh_ice_maker_status)
            return msg
        return "Ice maker off request failed. Check Home Assistant status."

    def on_ice_maker_toggle(self, event):
        if getattr(self, "_ice_maker_switch_state", "unknown") == "on":
            return self.on_ice_maker_off(event)
        return self.on_ice_maker_on(event)


    def on_minimize(self, event):
        if isinstance(event, wx.CloseEvent) and event.CanVeto(): event.Veto()
        wx.CallLater(500, self.Hide)

    def on_quit(self, event):
        self.running = False
        is_shutting_down.set()
        if hasattr(self, "_ha_address_recovery_stop"):
            self._ha_address_recovery_stop.set()
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
    audio.startup_cleanup()
    threading.Thread(target=audio.start_local_server, daemon=True).start()
    threading.Thread(target=run_flask_server, daemon=True).start()
    # Flask routes all guard on 'dash_app is None', so no fixed sleep is needed.
    dash_app = ViperDashboard()
    gui_app.MainLoop()
