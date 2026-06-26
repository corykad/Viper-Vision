import ctypes
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import wx
import wx.adv
try:
    import wx.html2 as wxhtml2
except Exception:
    wxhtml2 = None

import viper_audio as audio
import viper_config as cfg
import viper_diagnostics as diagnostics
import viper_discovery as discovery
import viper_ha_addons as ha_addons
import viper_ha_listener as ha_listener
import viper_ha_package as ha_package
import viper_ha_vm as ha_vm
import viper_speakers as speakers
import viper_vision as vision
from viper_runtime import safe_submit


def _ring_discovery():
    import viper_ring_discovery as ring_discovery
    return ring_discovery


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
HA_INSTALL_LOG_PATH = cfg.DATA_DIR / "viper_ha_install.log"

class AccessibleStatusText(wx.StaticText):
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

    def SetValue(self, value):
        self.SetLabel(value)

    def GetValue(self):
        return self._value

    def AppendText(self, text):
        self.SetLabel(self._value + str(text or ""))

    def Clear(self):
        self.SetLabel("")

    def GetLastPosition(self):
        return len(self._value)


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


def open_official_link(*args, **kwargs):
    return ha_vm.open_official_link(*args, **kwargs)


def open_url(*args, **kwargs):
    return ha_vm.open_url(*args, **kwargs)


def find_vboxmanage(*args, **kwargs):
    return ha_vm.find_vboxmanage(*args, **kwargs)


def find_winget(*args, **kwargs):
    return ha_vm.find_winget(*args, **kwargs)


def get_machine_architecture(*args, **kwargs):
    return ha_vm.get_machine_architecture(*args, **kwargs)


def get_ha_vm_platform_status(*args, **kwargs):
    return ha_vm.get_ha_vm_platform_status(*args, **kwargs)


def normalize_ha_vm_ram_mb(*args, **kwargs):
    return ha_vm.normalize_ha_vm_ram_mb(*args, **kwargs)


def normalize_ha_vm_disk_gb(*args, **kwargs):
    return ha_vm.normalize_ha_vm_disk_gb(*args, **kwargs)


def get_ha_vm_drive_space_status(*args, **kwargs):
    return ha_vm.get_ha_vm_drive_space_status(*args, **kwargs)


def get_winget_status(*args, **kwargs):
    return ha_vm.get_winget_status(*args, **kwargs)


def get_virtualbox_status(*args, **kwargs):
    return ha_vm.get_virtualbox_status(*args, **kwargs)


def is_windows_admin(*args, **kwargs):
    return ha_vm.is_windows_admin(*args, **kwargs)


def _run_powershell_command(*args, **kwargs):
    return ha_vm._run_powershell_command(*args, **kwargs)


def _windows_optional_feature_state(*args, **kwargs):
    return ha_vm._windows_optional_feature_state(*args, **kwargs)


def get_windows_virtualization_status(*args, **kwargs):
    return ha_vm.get_windows_virtualization_status(*args, **kwargs)


def optimize_windows_for_virtualbox(*args, **kwargs):
    return ha_vm.optimize_windows_for_virtualbox(*args, **kwargs)


def install_virtualbox_with_winget(*args, **kwargs):
    return ha_vm.install_virtualbox_with_winget(*args, **kwargs)


def _hidden_subprocess_kwargs(*args, **kwargs):
    return ha_vm._hidden_subprocess_kwargs(*args, **kwargs)


def _clean_process_progress_line(*args, **kwargs):
    return ha_vm._clean_process_progress_line(*args, **kwargs)


def _run_process_with_progress(*args, **kwargs):
    return ha_vm._run_process_with_progress(*args, **kwargs)


def _run_vbox(*args, **kwargs):
    return ha_vm._run_vbox(*args, **kwargs)


def _run_vbox_progress(*args, **kwargs):
    return ha_vm._run_vbox_progress(*args, **kwargs)


def _vbox_vm_exists(*args, **kwargs):
    return ha_vm._vbox_vm_exists(*args, **kwargs)


def _choose_bridged_adapter(*args, **kwargs):
    return ha_vm._choose_bridged_adapter(*args, **kwargs)


def get_latest_haos_virtualbox_asset(*args, **kwargs):
    return ha_vm.get_latest_haos_virtualbox_asset(*args, **kwargs)


def download_file(*args, **kwargs):
    return ha_vm.download_file(*args, **kwargs)


def _extract_haos_disk(*args, **kwargs):
    return ha_vm._extract_haos_disk(*args, **kwargs)


def _import_ha_ova(*args, **kwargs):
    return ha_vm._import_ha_ova(*args, **kwargs)


def _resize_virtualbox_disk(*args, **kwargs):
    return ha_vm._resize_virtualbox_disk(*args, **kwargs)


def _setup_progress_default_state(*args, **kwargs):
    return ha_vm._setup_progress_default_state(*args, **kwargs)


def _coerce_setup_progress_state(*args, **kwargs):
    return ha_vm._coerce_setup_progress_state(*args, **kwargs)


def _bytes_progress_percent(*args, **kwargs):
    return ha_vm._bytes_progress_percent(*args, **kwargs)


def _classify_setup_progress_message(*args, **kwargs):
    return ha_vm._classify_setup_progress_message(*args, **kwargs)


def _format_setup_progress_state(*args, **kwargs):
    return ha_vm._format_setup_progress_state(*args, **kwargs)


def _check_home_assistant_core_ready(*args, **kwargs):
    return ha_vm._check_home_assistant_core_ready(*args, **kwargs)


def build_ha_install_preflight_summary(*args, **kwargs):
    return ha_vm.build_ha_install_preflight_summary(*args, **kwargs)


def wait_for_home_assistant_first_boot(*args, **kwargs):
    kwargs.setdefault("core_ready_func", _check_home_assistant_core_ready)
    return ha_vm.wait_for_home_assistant_first_boot(*args, **kwargs)


def _create_ha_vm_from_vdi(*args, **kwargs):
    return ha_vm._create_ha_vm_from_vdi(*args, **kwargs)


def install_home_assistant_vm_from_image(*args, **kwargs):
    return ha_vm.install_home_assistant_vm_from_image(*args, **kwargs)


def download_and_install_home_assistant_vm(*args, **kwargs):
    return ha_vm.download_and_install_home_assistant_vm(*args, **kwargs)


def start_home_assistant_vm(*args, **kwargs):
    return ha_vm.start_home_assistant_vm(*args, **kwargs)


def _current_diagnostics(*, check_ha=False):
    return diagnostics.collect_diagnostics(cfg.load_config())


def _dispatch_broadcast_message(message, *, channel="manual"):
    return {"ok": False, "message": "Broadcast is available from the main dashboard."}


def _normalize_broadcast_mode(mode):
    return str(mode or "default").strip().lower() or "default"


def _is_hidden_vacuum_setting_entity_id(entity_id):
    return False


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

    def _make_accessible_status_text(self, parent, **kwargs):
        return AccessibleStatusText(parent, **kwargs)

    def _safe_submit(self, fn, *args, **kwargs):
        return safe_submit(fn, *args, **kwargs)

    def _normalize_broadcast_mode(self, mode):
        return _normalize_broadcast_mode(mode)

    def _is_hidden_vacuum_setting_entity_id(self, entity_id):
        return _is_hidden_vacuum_setting_entity_id(entity_id)

    def _current_diagnostics(self, *, check_ha=False):
        return _current_diagnostics(check_ha=check_ha)

    def _dispatch_broadcast_message(self, message, *, channel="manual"):
        return _dispatch_broadcast_message(message, channel=channel)

    def _open_url(self, url):
        return open_url(url)

    def _support_email(self):
        return SUPPORT_EMAIL

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
            mqtt_result = _ring_discovery().listen_for_ring_topics(
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
            mqtt_result = _ring_discovery().listen_for_ring_topics(
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
        return ha_addons.check_supervisor_install_permission(settings, self._hassio_request)

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
        return ha_addons.hassio_request(
            settings,
            method,
            path,
            payload=payload,
            timeout=timeout,
            ws_request_func=self._hassio_ws_request,
        )

    def _hassio_ws_request(self, settings, method, path, *, payload=None, timeout=30):
        return ha_addons.hassio_ws_request(
            settings,
            method,
            path,
            payload=payload,
            timeout=timeout,
            ws_command_func=self._ha_ws_command,
        )

    def _ha_ws_command(self, settings, command, *, timeout=30):
        return ha_addons.ha_ws_command(settings, command, timeout=timeout)

    def _addon_items_from_payload(self, payload):
        return ha_addons.addon_items_from_payload(payload)

    def _payload_data(self, payload):
        return ha_addons.payload_data(payload)

    def _get_installed_addons(self, settings):
        return ha_addons.get_installed_addons(settings, self._hassio_request)

    def _get_addon_info(self, settings, slug):
        payload = self._hassio_request(settings, "GET", f"/addons/{slug}/info", timeout=30)
        return self._payload_data(payload)

    def _ensure_addon_started(self, settings, slug):
        return ha_addons.ensure_addon_started(settings, slug, self._get_addon_info, self._hassio_request)

    def _restart_addon(self, settings, slug):
        return ha_addons.restart_addon(settings, slug, self._hassio_request, self._ensure_addon_started)

    def _configure_ring_mqtt_rtsp_port(self, settings):
        return ha_addons.configure_ring_mqtt_rtsp_port(settings, self._hassio_request)

    def _configure_ring_mqtt_rtsp_port_and_restart(self, settings):
        return ha_addons.configure_ring_mqtt_rtsp_port_and_restart(
            settings,
            self._configure_ring_mqtt_rtsp_port,
            self._restart_addon,
            self._ensure_addon_started,
        )

    def _absolute_ha_url(self, settings, path_or_url):
        return ha_addons.absolute_ha_url(settings, path_or_url)

    def _normalize_addon_webui(self, settings, value):
        return ha_addons.normalize_addon_webui(settings, value)

    def _get_current_ha_user_id(self, settings):
        return ha_addons.current_ha_user_id(settings, self._ha_ws_command)

    def _create_ingress_session(self, settings):
        return ha_addons.create_ingress_session(settings, self._hassio_request, self._ha_ws_command)

    def _ingress_session_url(self, settings, session, addon_info):
        return ha_addons.ingress_session_url(settings, session, addon_info)

    def _resolve_addon_login_url(self, settings, slug):
        return ha_addons.resolve_addon_login_url(settings, slug, self._get_addon_info)

    def _ring_mqtt_app_page_url(self, settings, slug):
        return ha_addons.ring_mqtt_app_page_url(settings, slug)

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
        return ha_addons.find_addon_slug(addons, exact_slugs=exact_slugs, text_tokens=text_tokens)

    def _find_ring_mqtt_slug(self, addons):
        return ha_addons.find_ring_mqtt_slug(addons)

    def _is_ring_mqtt_slug(self, slug):
        return ha_addons.is_ring_mqtt_slug(slug)

    def _addon_installed_in_store(self, addons, slug):
        return ha_addons.addon_installed_in_store(addons, slug)

    def _run_install_ring_mqtt_requirements(self, settings):
        def progress(lines, message, *, announce=False):
            self._append_setup_progress(lines, message, announce=announce)

        result = ha_addons.install_ring_mqtt_requirements(
            settings,
            progress=progress,
            hassio_request_func=self._hassio_request,
            get_installed_addons_func=self._get_installed_addons,
            ensure_addon_started_func=self._ensure_addon_started,
            configure_ring_mqtt_func=self._configure_ring_mqtt_rtsp_port_and_restart,
        )
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
            import soco
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
        result = _ring_discovery().test_mqtt_connection(
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
        result = _ring_discovery().listen_for_ring_topics(
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
                mqtt_result = _ring_discovery().listen_for_ring_topics(
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
            import soco
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
