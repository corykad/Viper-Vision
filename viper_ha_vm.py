import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import zipfile
import webbrowser
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

import requests

import viper_config as cfg
import viper_discovery as discovery

OFFICIAL_LINKS = {
    "ha_windows": "https://www.home-assistant.io/installation/windows/",
    "ha_install": "https://www.home-assistant.io/installation/",
    "ha_tokens": "https://developers.home-assistant.io/docs/auth_api/",
    "virtualbox": "https://www.virtualbox.org/wiki/Downloads",
    "ha_os_releases": "https://github.com/home-assistant/operating-system/releases/latest",
    "mosquitto_docs": "https://github.com/home-assistant/addons/blob/master/mosquitto/DOCS.md",
    "ring_mqtt_addon": "https://github.com/tsightler/ring-mqtt-ha-addon",
}

RING_MQTT_ADDON_SLUG = "03cabcc9_ring_mqtt"
HA_VM_NAME = "Home Assistant"
HA_VM_BASE_DIR = Path(r"C:\VMs")
HA_VM_DIR = HA_VM_BASE_DIR / HA_VM_NAME
HAOS_RELEASE_API = "https://api.github.com/repos/home-assistant/operating-system/releases/latest"
SUPPORTED_HA_VM_ARCHITECTURES = {"amd64", "x86_64", "x64", "intel64"}
DEFAULT_HA_VM_RAM_MB = 4096
MIN_HA_VM_RAM_MB = 2048
MAX_HA_VM_RAM_MB = 16384
DEFAULT_HA_VM_DISK_GB = 32
MIN_HA_VM_DISK_GB = 16
MAX_HA_VM_DISK_GB = 256
SUPPORT_EMAIL = "ckadlik@gmail.com"


def open_official_link(key):
    url = OFFICIAL_LINKS.get(key)
    if not url:
        return False
    open_url(url)
    return True


def open_url(url):
    target = str(url or "").strip()
    if not target:
        return False
    try:
        if webbrowser.open(target, new=2):
            return True
    except Exception:
        logging.debug("webbrowser.open failed for %s", target, exc_info=True)
    if os.name == "nt":
        try:
            os.startfile(target)
            return True
        except Exception:
            logging.debug("os.startfile failed for %s", target, exc_info=True)
    return False


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


def find_winget():
    candidate = shutil.which("winget.exe") or shutil.which("winget")
    return str(Path(candidate)) if candidate else ""


def get_machine_architecture():
    return (platform.machine() or os.environ.get("PROCESSOR_ARCHITECTURE") or "").strip().lower()


def get_ha_vm_platform_status():
    arch = get_machine_architecture()
    is_windows = os.name == "nt"
    supported = is_windows and arch in SUPPORTED_HA_VM_ARCHITECTURES
    if supported:
        message = f"Automatic Home Assistant VM install is supported on this Windows x64 PC. Architecture: {arch or 'unknown'}."
    elif not is_windows:
        message = "Automatic Home Assistant VM install is only supported by Viper on Windows x64 right now."
    else:
        message = (
            f"Automatic Home Assistant VM install is not supported on this machine architecture: {arch or 'unknown'}. "
            "Use an existing Home Assistant server, Home Assistant Green, Raspberry Pi, or the official Home Assistant install guide."
        )
    return {"supported": supported, "architecture": arch or "unknown", "is_windows": is_windows, "message": message}


def normalize_ha_vm_ram_mb(value):
    try:
        ram = int(value)
    except (TypeError, ValueError):
        ram = DEFAULT_HA_VM_RAM_MB
    return max(MIN_HA_VM_RAM_MB, min(MAX_HA_VM_RAM_MB, ram))


def normalize_ha_vm_disk_gb(value):
    try:
        disk = int(value)
    except (TypeError, ValueError):
        disk = DEFAULT_HA_VM_DISK_GB
    return max(MIN_HA_VM_DISK_GB, min(MAX_HA_VM_DISK_GB, disk))


def get_ha_vm_drive_space_status(disk_gb=DEFAULT_HA_VM_DISK_GB):
    disk_gb = normalize_ha_vm_disk_gb(disk_gb)
    try:
        target = HA_VM_BASE_DIR
        while not target.exists() and target.parent != target:
            target = target.parent
        usage = shutil.disk_usage(target)
        free_gb = usage.free // (1024 ** 3)
        recommended_free_gb = disk_gb + 8
        ok = free_gb >= recommended_free_gb
        if ok:
            message = f"Drive space: {free_gb} GB free. Enough for a {disk_gb} GB Home Assistant disk."
        else:
            message = f"Drive space warning: {free_gb} GB free. Viper recommends at least {recommended_free_gb} GB free for a {disk_gb} GB Home Assistant disk."
        return {"ok": ok, "free_gb": free_gb, "recommended_free_gb": recommended_free_gb, "message": message}
    except Exception as e:
        return {"ok": False, "free_gb": None, "recommended_free_gb": disk_gb + 8, "message": f"Drive space could not be checked: {e}"}


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
    lines.extend(
        [
            "",
            "Viper will download the official Home Assistant OS image, create a VirtualBox VM named Home Assistant, start it, and wait for first boot.",
            "The first boot can take up to 25 minutes while Home Assistant downloads and prepares Core.",
            "",
            "Continue with these settings?",
        ]
    )
    return {
        "ok": bool(platform_status.get("supported") and vbox.get("installed")),
        "drive_ok": drive.get("ok"),
        "message": "\n".join(str(line) for line in lines if line is not None),
        "resources": {"ram_mb": ram_mb, "disk_gb": disk_gb},
    }


def get_winget_status():
    exe = find_winget()
    if not exe:
        return {"installed": False, "path": "", "version": "", "message": "winget was not found on this PC."}
    version = ""
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8, **_hidden_subprocess_kwargs())
        version = (result.stdout or result.stderr or "").strip()
    except Exception as e:
        version = f"version check failed: {e}"
    return {"installed": True, "path": exe, "version": version, "message": f"winget found at {exe}."}


def get_virtualbox_status():
    exe = find_vboxmanage()
    if not exe:
        return {"installed": False, "path": "", "version": "", "message": "VirtualBox was not found on this PC."}
    version = ""
    try:
        result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5, **_hidden_subprocess_kwargs())
        version = (result.stdout or result.stderr or "").strip()
    except Exception as e:
        version = f"version check failed: {e}"
    return {"installed": True, "path": exe, "version": version, "message": f"VirtualBox found at {exe}."}


def is_windows_admin():
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_powershell_command(command, *, timeout=300, progress=None, label="PowerShell"):
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        raise RuntimeError("Windows PowerShell was not found.")
    return _run_process_with_progress(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        timeout=timeout,
        progress=progress,
        idle_message=f"{label} is still running.",
        idle_seconds=20,
        output_prefix=label,
    )


def _windows_optional_feature_state(feature_name):
    if os.name != "nt":
        return "not_applicable"
    command = (
        f"$feature = Get-WindowsOptionalFeature -Online -FeatureName '{feature_name}' -ErrorAction SilentlyContinue; "
        "if ($null -eq $feature) { 'missing' } else { [string]$feature.State }"
    )
    try:
        output, returncode = _run_powershell_command(command, timeout=10, label="Windows")
        if returncode != 0:
            return "unknown"
        return (output.strip().splitlines()[-1] if output.strip() else "unknown").strip()
    except Exception:
        return "unknown"


def get_windows_virtualization_status():
    if os.name != "nt":
        return {
            "is_windows": False,
            "admin": False,
            "hypervisor_present": False,
            "features": {},
            "message": "Windows VirtualBox optimization is only relevant on Windows.",
            "needs_attention": False,
        }
    hypervisor_present = None
    try:
        output, returncode = _run_powershell_command(
            "Get-CimInstance Win32_ComputerSystem | Select-Object -ExpandProperty HypervisorPresent",
            timeout=10,
            label="Windows",
        )
        if returncode == 0:
            text = output.strip().splitlines()[-1].strip().lower() if output.strip() else ""
            hypervisor_present = text in {"true", "1"}
    except Exception:
        hypervisor_present = None
    feature_names = [
        "Microsoft-Hyper-V-All",
        "Windows-Hypervisor-Platform",
        "VirtualMachinePlatform",
        "Containers-DisposableClientVM",
        "Microsoft-Windows-Subsystem-Linux",
    ]
    features = {name: _windows_optional_feature_state(name) for name in feature_names}
    enabled_features = [name for name, state in features.items() if str(state).lower() == "enabled"]
    needs_attention = bool(hypervisor_present or enabled_features)
    if needs_attention:
        message = (
            "Windows hypervisor features appear to be enabled. VirtualBox can still run, but Home Assistant VMs are often more stable "
            "after Hyper-V, Windows Hypervisor Platform, Virtual Machine Platform, Windows Sandbox, and WSL2 support are turned off and Windows is rebooted."
        )
    elif hypervisor_present is False:
        message = "Windows hypervisor is not active. VirtualBox has the best chance of using direct hardware virtualization."
    else:
        message = "Viper could not fully determine Windows hypervisor status."
    return {
        "is_windows": True,
        "admin": is_windows_admin(),
        "hypervisor_present": hypervisor_present,
        "features": features,
        "enabled_features": enabled_features,
        "needs_attention": needs_attention,
        "message": message,
    }


def optimize_windows_for_virtualbox(progress=None):
    if os.name != "nt":
        return {"ok": False, "message": "This optimization is only available on Windows."}
    if not is_windows_admin():
        return {
            "ok": False,
            "needs_admin": True,
            "message": (
                "Windows rejected the optimization because Viper is not running as administrator. "
                "Run Viper as administrator, then press Optimize Windows For VirtualBox again."
            ),
        }
    commands = [
        ("Turning off automatic Windows hypervisor launch.", "bcdedit /set hypervisorlaunchtype off"),
        ("Disabling full Hyper-V.", "Disable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -NoRestart"),
        ("Disabling Windows Hypervisor Platform.", "Disable-WindowsOptionalFeature -Online -FeatureName Windows-Hypervisor-Platform -NoRestart"),
        ("Disabling Virtual Machine Platform. This can affect WSL2 and Docker Desktop.", "Disable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -NoRestart"),
        ("Disabling Windows Sandbox.", "Disable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -NoRestart"),
    ]
    outputs = []
    try:
        for message, command in commands:
            if progress:
                progress(message)
            output, returncode = _run_powershell_command(command, timeout=600, progress=progress, label="Windows")
            outputs.append(f"{message}\n{output}".strip())
            if returncode != 0:
                return {
                    "ok": False,
                    "message": f"Windows optimization stopped while running: {message} Exit code {returncode}.",
                    "output": "\n\n".join(outputs),
                }
        return {
            "ok": True,
            "reboot_required": True,
            "message": (
                "Windows virtualization optimization is complete. Reboot Windows before starting the Home Assistant VM. "
                "WSL2, Docker Desktop, Windows Sandbox, and Hyper-V-backed tools may stop working until these features are re-enabled."
            ),
            "output": "\n\n".join(outputs),
        }
    except Exception as e:
        return {"ok": False, "message": f"Windows optimization failed: {e}", "output": "\n\n".join(outputs)}


def install_virtualbox_with_winget(progress=None):
    platform_status = get_ha_vm_platform_status()
    if not platform_status.get("supported"):
        return {"ok": False, "message": platform_status["message"], "open_download": True, "unsupported_platform": True}
    winget = find_winget()
    if not winget:
        return {
            "ok": False,
            "message": "winget is not installed. Open the VirtualBox download page and install VirtualBox manually.",
            "open_download": True,
        }
    if progress:
        progress("Starting VirtualBox install with winget. Windows may ask for administrator permission.")
    cmd = [
        winget,
        "install",
        "--id",
        "Oracle.VirtualBox",
        "-e",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    try:
        output, returncode = _run_process_with_progress(
            cmd,
            timeout=1800,
            progress=progress,
            idle_message="VirtualBox install is still running. Waiting for winget to finish.",
            idle_seconds=20,
            output_prefix="winget",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "winget VirtualBox install timed out. Open the VirtualBox download page and install it manually."}
    except Exception as e:
        return {"ok": False, "message": f"winget VirtualBox install could not start: {e}"}
    if returncode == 0:
        status = get_virtualbox_status()
        if status.get("installed"):
            return {"ok": True, "message": f"VirtualBox installed or already present. {status.get('version') or status.get('path')}", "output": output}
        return {"ok": True, "message": "winget finished. If VirtualBox is still not detected, restart Viper or reboot Windows.", "output": output}
    already_installed = "already installed" in output.lower() or "no available upgrade" in output.lower()
    if already_installed:
        return {"ok": True, "message": "VirtualBox appears to already be installed.", "output": output}
    return {"ok": False, "message": f"winget VirtualBox install failed with exit code {returncode}.", "output": output}


def _hidden_subprocess_kwargs():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _clean_process_progress_line(line, output_prefix="command"):
    """Turn console progress bars into screen-reader friendly progress text."""
    text = str(line or "").strip()
    if not text:
        return ""
    if text in {"-", "\\", "|", "/"}:
        return ""
    progress_chars = "█▓▒░■□▌▐▏▎▍▊▉"
    percent = _bytes_progress_percent(text)
    if percent is not None:
        match = re.search(
            r"(?P<done>\d+(?:\.\d+)?)\s*(?P<done_unit>KB|MB|GB)\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>KB|MB|GB)",
            text,
            re.IGNORECASE,
        )
        done = f"{match.group('done')} {match.group('done_unit').upper()}" if match else ""
        total = f"{match.group('total')} {match.group('total_unit').upper()}" if match else ""
        label = "VirtualBox download" if str(output_prefix).lower() == "winget" else f"{output_prefix} progress"
        if done and total:
            return f"{label}: {percent} percent, {done} of {total}."
        return f"{label}: {percent} percent."
    if any(ch in text for ch in progress_chars):
        cleaned = re.sub(r"[█▓▒░■□▌▐▏▎▍▊▉]+", "", text)
        cleaned = re.sub(r"[-\\|/]+\s*", "", cleaned).strip()
        return cleaned
    return re.sub(r"\s+", " ", text)


def _run_process_with_progress(cmd, *, timeout=1800, progress=None, idle_message="", idle_seconds=20, output_prefix="command"):
    started = time.monotonic()
    output_lines = []
    line_queue = Queue()
    if progress:
        progress("Started command: " + " ".join(str(part) for part in cmd))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_hidden_subprocess_kwargs(),
    )

    def reader():
        try:
            for line in process.stdout:
                line_queue.put(line.rstrip())
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
            line_queue.put(None)

    threading.Thread(target=reader, daemon=True).start()
    last_report = time.monotonic()
    reader_done = False
    while True:
        if time.monotonic() - started > timeout:
            process.kill()
            raise subprocess.TimeoutExpired(cmd, timeout)
        try:
            line = line_queue.get(timeout=0.5)
            if line is None:
                reader_done = True
            elif line.strip():
                stripped = line.strip()
                clean_line = _clean_process_progress_line(stripped, output_prefix=output_prefix)
                if not clean_line:
                    continue
                output_lines.append(clean_line)
                last_report = time.monotonic()
                if progress:
                    if clean_line.lower().startswith(f"{str(output_prefix).lower()}:"):
                        progress(clean_line)
                    else:
                        progress(f"{output_prefix}: {clean_line}")
        except Empty:
            pass
        if progress and idle_message and time.monotonic() - last_report >= idle_seconds:
            elapsed = int(time.monotonic() - started)
            progress(f"{idle_message} Elapsed time: {elapsed} seconds.")
            last_report = time.monotonic()
        if reader_done and process.poll() is not None:
            break
        if process.poll() is not None and line_queue.empty():
            break
    return "\n".join(output_lines), int(process.returncode or 0)


def _run_vbox(args, *, timeout=120, progress=None):
    exe = find_vboxmanage()
    if not exe:
        raise RuntimeError("VirtualBox was not found. Install VirtualBox first.")
    if progress:
        output, returncode = _run_process_with_progress(
            [exe, *args],
            timeout=timeout,
            progress=progress,
            idle_message="VirtualBox is still working.",
            idle_seconds=10,
            output_prefix="VirtualBox",
        )
        if returncode != 0:
            raise RuntimeError(f"VBoxManage {' '.join(args)} failed with exit code {returncode}: {output}")
        return output
    result = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, **_hidden_subprocess_kwargs())
    output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part and part.strip())
    if result.returncode != 0:
        raise RuntimeError(f"VBoxManage {' '.join(args)} failed with exit code {result.returncode}: {output}")
    return output


def _run_vbox_progress(args, *, timeout=120, progress=None):
    if progress:
        return _run_vbox(args, timeout=timeout, progress=progress)
    return _run_vbox(args, timeout=timeout)


def _vbox_vm_exists(vm_name=HA_VM_NAME):
    try:
        output = _run_vbox(["list", "vms"], timeout=20)
    except Exception:
        return False
    needle = f'"{vm_name}"'
    return any(line.strip().startswith(needle) for line in output.splitlines())


def _choose_bridged_adapter():
    try:
        output = _run_vbox(["list", "bridgedifs"], timeout=20)
    except Exception:
        return ""
    blocks = re.split(r"\r?\n\r?\n+", output.strip())
    fallback = ""
    for block in blocks:
        name_match = re.search(r"^Name:\s*(.+)$", block, re.MULTILINE)
        status_match = re.search(r"^Status:\s*(.+)$", block, re.MULTILINE)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        if not fallback:
            fallback = name
        status = (status_match.group(1).strip().lower() if status_match else "")
        if status == "up":
            return name
    return fallback


def get_latest_haos_virtualbox_asset():
    response = requests.get(HAOS_RELEASE_API, timeout=20, headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    release = response.json()
    assets = release.get("assets") or []
    candidates = []
    for asset in assets:
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        lowered = name.lower()
        if "haos_ova" in lowered and (lowered.endswith(".zip") or lowered.endswith(".ova") or lowered.endswith(".vdi")):
            candidates.append(asset)
    if not candidates:
        for asset in assets:
            name = asset.get("name") or ""
            lowered = name.lower()
            if ("virtualbox" in lowered or "ova" in lowered) and (lowered.endswith(".zip") or lowered.endswith(".ova") or lowered.endswith(".vdi")):
                candidates.append(asset)
    if not candidates:
        raise RuntimeError("Could not find a Home Assistant OS VirtualBox download in the latest official release.")
    candidates.sort(key=lambda item: (0 if "haos_ova" in (item.get("name") or "").lower() else 1, item.get("name") or ""))
    asset = candidates[0]
    return {
        "name": asset.get("name") or "haos_virtualbox_image",
        "url": asset.get("browser_download_url") or "",
        "size": asset.get("size") or 0,
        "release": release.get("tag_name") or release.get("name") or "latest",
    }


def download_file(url, destination, progress=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"Starting download: {destination.name}.")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        downloaded = 0
        last_report_bytes = 0
        last_report_time = time.monotonic()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                enough_bytes = downloaded - last_report_bytes >= 25 * 1024 * 1024
                enough_time = now - last_report_time >= 5
                if progress and (enough_bytes or enough_time):
                    last_report_bytes = downloaded
                    last_report_time = now
                    if total:
                        percent = int((downloaded / total) * 100)
                        progress(f"Downloading Home Assistant OS: {downloaded // (1024 * 1024)} MB of {total // (1024 * 1024)} MB, {percent} percent.")
                    else:
                        progress(f"Downloading Home Assistant OS: {downloaded // (1024 * 1024)} MB downloaded.")
    if progress:
        progress(f"Download complete: {destination.name}.")
    return destination


def _extract_haos_disk(archive_path, progress=None):
    archive_path = Path(archive_path)
    lowered = archive_path.name.lower()
    if lowered.endswith(".ova") or lowered.endswith(".vdi"):
        return archive_path
    if not lowered.endswith(".zip"):
        raise RuntimeError(f"Unsupported Home Assistant OS download type: {archive_path.name}")
    extract_dir = archive_path.parent / archive_path.stem
    extract_dir.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("Extracting the Home Assistant OS VirtualBox disk.")
    with zipfile.ZipFile(archive_path, "r") as zf:
        members = [name for name in zf.namelist() if name.lower().endswith((".vdi", ".ova"))]
        if not members:
            raise RuntimeError("The Home Assistant OS zip did not contain a VDI or OVA file.")
        zf.extract(members[0], extract_dir)
        return extract_dir / members[0]


def _import_ha_ova(ova_path, progress=None):
    if progress:
        progress("Importing the Home Assistant OVA into VirtualBox.")
    _run_vbox_progress(["import", str(ova_path), "--vsys", "0", "--vmname", HA_VM_NAME], timeout=900, progress=progress)


def _create_ha_vm_from_vdi(vdi_path, progress=None, ram_mb=DEFAULT_HA_VM_RAM_MB):
    ram_mb = normalize_ha_vm_ram_mb(ram_mb)
    HA_VM_BASE_DIR.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("Creating the Home Assistant virtual machine in VirtualBox.")
    _run_vbox_progress(["createvm", "--name", HA_VM_NAME, "--ostype", "Linux_64", "--basefolder", str(HA_VM_BASE_DIR), "--register"], timeout=120, progress=progress)
    if progress:
        progress("Configuring Home Assistant VM memory, CPU, firmware, and boot disk.")
    _run_vbox_progress(["modifyvm", HA_VM_NAME, "--memory", str(ram_mb), "--cpus", "2", "--firmware", "efi", "--boot1", "disk"], timeout=60, progress=progress)
    adapter = _choose_bridged_adapter()
    if adapter:
        _run_vbox_progress(["modifyvm", HA_VM_NAME, "--nic1", "bridged", "--bridgeadapter1", adapter, "--nictype1", "82540EM"], timeout=60, progress=progress)
    else:
        _run_vbox_progress(["modifyvm", HA_VM_NAME, "--nic1", "nat"], timeout=60, progress=progress)
    if progress:
        progress("Attaching the Home Assistant disk to the VirtualBox VM.")
    _run_vbox_progress(["storagectl", HA_VM_NAME, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci", "--hostiocache", "on"], timeout=60, progress=progress)
    _run_vbox_progress(["storageattach", HA_VM_NAME, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", str(vdi_path)], timeout=120, progress=progress)
    if adapter and progress:
        progress(f"Home Assistant VM network set to bridged adapter: {adapter}.")
    elif progress:
        progress("No bridged adapter was detected. Viper used NAT; Home Assistant may not be reachable until networking is changed to bridged.")


def _resize_virtualbox_disk(vdi_path, disk_gb=DEFAULT_HA_VM_DISK_GB, progress=None):
    disk_gb = normalize_ha_vm_disk_gb(disk_gb)
    size_mb = disk_gb * 1024
    try:
        if progress:
            progress(f"Setting Home Assistant virtual disk target size to {disk_gb} GB.")
        _run_vbox_progress(["modifymedium", "disk", str(vdi_path), "--resize", str(size_mb)], timeout=300, progress=progress)
        return {"ok": True, "message": f"Home Assistant virtual disk set to {disk_gb} GB."}
    except Exception as e:
        logging.warning("[HA INSTALL] Could not resize Home Assistant disk %s to %s GB: %s", vdi_path, disk_gb, e)
        return {
            "ok": False,
            "message": (
                f"Viper could not resize the Home Assistant disk to {disk_gb} GB before attaching it. "
                "Continuing with the image's original disk size."
            ),
            "error": str(e),
        }


def install_home_assistant_vm_from_image(image_path, progress=None, ram_mb=DEFAULT_HA_VM_RAM_MB, disk_gb=DEFAULT_HA_VM_DISK_GB):
    ram_mb = normalize_ha_vm_ram_mb(ram_mb)
    disk_gb = normalize_ha_vm_disk_gb(disk_gb)
    image_path = Path(image_path)
    if not image_path.exists():
        return {"ok": False, "message": f"Home Assistant OS image was not found: {image_path}"}
    try:
        if _vbox_vm_exists(HA_VM_NAME):
            return {"ok": True, "message": f'A VirtualBox VM named "{HA_VM_NAME}" already exists. Viper will use the existing VM.'}
        disk_or_ova = _extract_haos_disk(image_path, progress=progress)
        if str(disk_or_ova).lower().endswith(".ova"):
            _import_ha_ova(disk_or_ova, progress=progress)
            _run_vbox_progress(["modifyvm", HA_VM_NAME, "--memory", str(ram_mb), "--cpus", "2"], timeout=60, progress=progress)
            adapter = _choose_bridged_adapter()
            if adapter:
                _run_vbox_progress(["modifyvm", HA_VM_NAME, "--nic1", "bridged", "--bridgeadapter1", adapter, "--nictype1", "82540EM"], timeout=60, progress=progress)
            if progress:
                progress("The selected OVA controls its own disk image. Viper configured RAM; disk size may need to be adjusted later in VirtualBox if you need more space.")
        else:
            _resize_virtualbox_disk(disk_or_ova, disk_gb=disk_gb, progress=progress)
            _create_ha_vm_from_vdi(disk_or_ova, progress=progress, ram_mb=ram_mb)
        return {"ok": True, "message": f'Home Assistant VM "{HA_VM_NAME}" is installed in VirtualBox with {ram_mb} MB RAM and a target disk size of {disk_gb} GB.'}
    except Exception as e:
        return {"ok": False, "message": f"Home Assistant VM install failed: {e}"}


def download_and_install_home_assistant_vm(progress=None, ram_mb=DEFAULT_HA_VM_RAM_MB, disk_gb=DEFAULT_HA_VM_DISK_GB):
    ram_mb = normalize_ha_vm_ram_mb(ram_mb)
    disk_gb = normalize_ha_vm_disk_gb(disk_gb)
    platform_status = get_ha_vm_platform_status()
    if not platform_status.get("supported"):
        return {"ok": False, "message": platform_status["message"], "unsupported_platform": True}
    try:
        if _vbox_vm_exists(HA_VM_NAME):
            return {"ok": True, "message": f'A VirtualBox VM named "{HA_VM_NAME}" already exists. Start it, then let Viper find Home Assistant.'}
        if progress:
            progress("Finding the latest official Home Assistant OS VirtualBox image.")
        asset = get_latest_haos_virtualbox_asset()
        if not asset.get("url"):
            raise RuntimeError("The Home Assistant OS release did not include a download URL.")
        downloads_dir = HA_VM_DIR / "downloads"
        destination = downloads_dir / asset["name"]
        if not destination.exists() or (asset.get("size") and destination.stat().st_size != asset.get("size")):
            if progress:
                progress(f"Downloading {asset['name']} from Home Assistant OS {asset.get('release', 'latest')}.")
            download_file(asset["url"], destination, progress=progress)
        else:
            if progress:
                progress(f"Using already downloaded Home Assistant OS image: {destination}.")
        return install_home_assistant_vm_from_image(destination, progress=progress, ram_mb=ram_mb, disk_gb=disk_gb)
    except Exception as e:
        return {"ok": False, "message": f"Home Assistant OS download/install failed: {e}"}


def start_home_assistant_vm(progress=None):
    try:
        if not _vbox_vm_exists(HA_VM_NAME):
            return {"ok": False, "message": f'No VirtualBox VM named "{HA_VM_NAME}" exists yet.'}
        if progress:
            progress("Starting Home Assistant VM headless.")
        output = _run_vbox_progress(["startvm", HA_VM_NAME, "--type", "headless"], timeout=120, progress=progress)
        return {"ok": True, "message": f"Home Assistant VM start requested. {output}".strip()}
    except RuntimeError as e:
        text = str(e)
        if "already locked" in text.lower() or "already running" in text.lower():
            return {"ok": True, "message": "Home Assistant VM is already running."}
        return {"ok": False, "message": f"Home Assistant VM could not be started: {e}"}


SETUP_PROGRESS_PHASES = {
    "virtualbox_install": "Installing VirtualBox",
    "haos_download": "Downloading Home Assistant OS",
    "haos_extract": "Extracting Home Assistant OS",
    "virtualbox_import": "Importing Home Assistant VM",
    "vm_create": "Creating Home Assistant VM",
    "vm_configure": "Configuring Home Assistant VM",
    "vm_start": "Starting Home Assistant VM",
    "ha_core_wait": "Waiting For Home Assistant Core",
    "ha_ready": "Home Assistant Ready",
    "failed": "Setup Needs Attention",
}


def _setup_progress_default_state():
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "active": False,
        "phase": "",
        "phase_label": "",
        "status": "",
        "detail": "",
        "percent": None,
        "started_at": now,
        "updated_at": now,
        "last_error": "",
        "next_action": "",
    }


def _coerce_setup_progress_state(value):
    state = _setup_progress_default_state()
    if isinstance(value, dict):
        state.update({key: value.get(key, state[key]) for key in state})
    return state


def _bytes_progress_percent(text):
    match = re.search(
        r"(?P<done>\d+(?:\.\d+)?)\s*(?P<done_unit>KB|MB|GB)\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>KB|MB|GB)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    multipliers = {"kb": 1, "mb": 1024, "gb": 1024 * 1024}
    done = float(match.group("done")) * multipliers[match.group("done_unit").lower()]
    total = float(match.group("total")) * multipliers[match.group("total_unit").lower()]
    if total <= 0:
        return None
    return max(0, min(100, int((done / total) * 100)))


def _classify_setup_progress_message(message, previous=None):
    state = _coerce_setup_progress_state(previous)
    text = str(message or "").strip()
    lowered = text.lower()
    now = datetime.now().isoformat(timespec="seconds")
    if not state.get("started_at"):
        state["started_at"] = now
    state["active"] = True
    state["status"] = text
    state["detail"] = text
    state["updated_at"] = now

    def set_phase(phase, percent=None, next_action=""):
        state["phase"] = phase
        state["phase_label"] = SETUP_PROGRESS_PHASES.get(phase, phase.replace("_", " ").title())
        if percent is not None:
            state["percent"] = max(0, min(100, int(percent)))
        if next_action:
            state["next_action"] = next_action

    winget_percent = _bytes_progress_percent(text)
    if "virtualbox install" in lowered or lowered.startswith("winget:") or winget_percent is not None:
        set_phase("virtualbox_install", winget_percent, "Wait for VirtualBox install to finish, then install Home Assistant OS.")
    if "successfully installed" in lowered and state.get("phase") == "virtualbox_install":
        set_phase("virtualbox_install", 100, "Press Check This PC, then Download And Install Home Assistant VM.")
        state["active"] = False
    elif "finding the latest official home assistant os" in lowered:
        set_phase("haos_download", 2, "Wait while Viper finds the official Home Assistant OS image.")
    elif lowered.startswith("downloading ") and "home assistant os" in lowered and "percent" not in lowered:
        set_phase("haos_download", 5, "Wait while Viper downloads the official Home Assistant OS image.")
    elif "downloading home assistant os:" in lowered:
        percent_match = re.search(r",\s*(\d+)\s*percent", lowered)
        percent = int(percent_match.group(1)) if percent_match else None
        set_phase("haos_download", percent, "Wait for the Home Assistant OS download to finish.")
    elif "download complete" in lowered:
        set_phase("haos_download", 100, "Viper will extract or import the Home Assistant OS image next.")
    elif "extracting the home assistant os" in lowered:
        set_phase("haos_extract", 0, "Wait while Viper extracts the disk image.")
    elif "importing the home assistant ova" in lowered:
        set_phase("virtualbox_import", None, "Wait while VirtualBox imports the Home Assistant appliance.")
    elif "creating the home assistant virtual machine" in lowered:
        set_phase("vm_create", 25, "Wait while Viper creates the VirtualBox machine.")
    elif "configuring home assistant vm" in lowered:
        set_phase("vm_configure", 50, "Wait while Viper sets memory, CPU, firmware, and networking.")
    elif "attaching the home assistant disk" in lowered:
        set_phase("vm_configure", 75, "Wait while Viper attaches the Home Assistant disk.")
    elif "home assistant vm network set" in lowered or "no bridged adapter" in lowered:
        set_phase("vm_configure", 95, "Viper will start the Home Assistant VM next.")
    elif "home assistant vm is installed" in lowered:
        set_phase("vm_start", 5, "Viper will start Home Assistant and wait for Core to finish first boot.")
    elif "starting home assistant vm" in lowered or "home assistant vm start requested" in lowered or "home assistant vm is already running" in lowered:
        set_phase("vm_start", 35, "Wait while the virtual machine starts.")
    elif "home assistant vm started" in lowered or "waiting for the home assistant web interface" in lowered:
        set_phase("ha_core_wait", 1, "Keep this window open while Home Assistant downloads/installs Core.")
    elif "waiting for home assistant first boot" in lowered:
        minute_match = re.search(r"elapsed about\s+(\d+)\s+minute", lowered)
        percent = None
        if minute_match:
            percent = min(99, max(1, int((int(minute_match.group(1)) / 25) * 100)))
        set_phase("ha_core_wait", percent, "Keep waiting. Home Assistant Core can take up to 25 minutes, especially near 97 percent.")
    elif "core/auth is still preparing" in lowered or "core is still preparing" in lowered or "downloading home assistant core" in lowered:
        set_phase("ha_core_wait", state.get("percent") or 50, "Keep waiting. The web page is up, but Home Assistant Core is not ready yet.")
    elif "home assistant is ready" in lowered or "home assistant core is ready" in lowered:
        set_phase("ha_ready", 100, "Open Home Assistant, finish onboarding, then continue Viper setup.")
        state["active"] = False
    elif "failed" in lowered or "timed out" in lowered or "could not" in lowered:
        set_phase("failed", state.get("percent"), "Read the message, then try the suggested fallback or create a support report.")
        state["last_error"] = text
        state["active"] = False
    return state


def _format_setup_progress_state(state, recent_lines=None):
    state = _coerce_setup_progress_state(state)
    recent_lines = [str(line) for line in (recent_lines or []) if str(line).strip()]
    percent = state.get("percent")
    percent_text = "unknown" if percent is None or percent == "" else f"{percent}%"
    lines = [
        "Current Home Assistant Setup Progress",
        "",
        f"Step: {state.get('phase_label') or 'Not started'}",
        f"Progress: {percent_text}",
        f"Status: {state.get('status') or 'No setup task is running.'}",
        f"Next action: {state.get('next_action') or 'Follow the button that Viper enables next.'}",
    ]
    if state.get("last_error"):
        lines.append(f"Last error: {state.get('last_error')}")
    if state.get("updated_at"):
        lines.append(f"Last update: {state.get('updated_at')}")
    if recent_lines:
        lines.extend(["", "Recent detailed progress:"])
        lines.extend(recent_lines[-20:])
    return "\n".join(lines)


def _check_home_assistant_core_ready(*, token=None, seed_host="", seed_port="8123", timeout=3):
    """Return ready only when HA Core/API auth layer is actually available."""
    attempts = []
    for candidate in discovery.candidate_ha_hosts(seed_host, seed_port):
        host = candidate["ha_ip"]
        port = candidate["ha_port"]
        url = f"http://{host}:{port}/api/"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            body = (response.text or "").strip()
            body_excerpt = re.sub(r"\s+", " ", body)[:160]
            attempts.append({**candidate, "url": url, "status_code": response.status_code, "body": body_excerpt})

            if token and response.status_code == 200:
                resolved_host = discovery.resolve_host_to_ip(host) or host
                return {
                    "ready": True,
                    "ha_ip": resolved_host,
                    "ha_port": port,
                    "auth_ok": True,
                    "message": f"Home Assistant Core is ready and accepted the token at {resolved_host}:{port}.",
                    "attempts": attempts,
                }
            if token and response.status_code in {401, 403}:
                resolved_host = discovery.resolve_host_to_ip(host) or host
                return {
                    "ready": True,
                    "ha_ip": resolved_host,
                    "ha_port": port,
                    "auth_ok": False,
                    "auth_error": "bad_token",
                    "message": f"Home Assistant Core is ready at {resolved_host}:{port}, but the saved token was rejected. Create or paste a new long-lived token after onboarding.",
                    "attempts": attempts,
                }
            if not token and response.status_code in {401, 403}:
                resolved_host = discovery.resolve_host_to_ip(host) or host
                return {
                    "ready": True,
                    "ha_ip": resolved_host,
                    "ha_port": port,
                    "auth_ok": False,
                    "message": f"Home Assistant Core is ready at {resolved_host}:{port}; login/onboarding can continue in the browser.",
                    "attempts": attempts,
                }
            if response.status_code == 200:
                return {
                    "ready": False,
                    "found": True,
                    "ha_ip": discovery.resolve_host_to_ip(host) or host,
                    "ha_port": port,
                    "message": "Home Assistant web interface is responding, but Core/auth is still preparing. It may still be downloading Home Assistant Core.",
                    "attempts": attempts,
                }
            if response.status_code in {502, 503, 504}:
                return {
                    "ready": False,
                    "found": True,
                    "ha_ip": discovery.resolve_host_to_ip(host) or host,
                    "ha_port": port,
                    "message": f"Home Assistant is booting and returned HTTP {response.status_code}.",
                    "attempts": attempts,
                }
        except requests.exceptions.RequestException as e:
            attempts.append({**candidate, "url": url, "error": str(e)})
    return {
        "ready": False,
        "found": False,
        "message": "Home Assistant has not responded yet.",
        "attempts": attempts,
    }


def wait_for_home_assistant_first_boot(progress=None, *, token=None, seed_host="", seed_port="8123", timeout_seconds=1500, interval_seconds=15, core_ready_func=None):
    """Wait for HAOS first boot without blocking the GUI thread."""
    core_ready_func = core_ready_func or _check_home_assistant_core_ready
    started = time.monotonic()
    deadline = started + max(30, int(timeout_seconds))
    last_message = "Home Assistant has not responded yet."
    while time.monotonic() < deadline:
        elapsed = int(time.monotonic() - started)
        try:
            found = discovery.find_home_assistant(
                token=token or None,
                seed_host=seed_host or "",
                seed_port=seed_port or "8123",
                timeout=3,
            )
            if found.get("ok"):
                core = core_ready_func(
                    token=token or None,
                    seed_host=found.get("ha_ip") or seed_host or "",
                    seed_port=found.get("ha_port") or seed_port or "8123",
                    timeout=3,
                )
                if core.get("ready"):
                    return {
                        "ok": True,
                        "message": core.get("message") or f"Home Assistant Core is ready at {core.get('ha_ip')}:{core.get('ha_port')}.",
                        "ha_ip": core.get("ha_ip", found.get("ha_ip", "")),
                        "ha_port": core.get("ha_port", found.get("ha_port", "8123")),
                        "auth_ok": core.get("auth_ok"),
                        "elapsed_seconds": elapsed,
                    }
                last_message = core.get("message") or "Home Assistant is responding, but Core is still preparing."
                if core.get("found"):
                    last_message += " Keep this window open; Viper will keep checking."
            elif found.get("auth_error") == "bad_token":
                last_message = "Home Assistant is reachable, but the saved token was rejected. Core appears to be running; create or paste a new long-lived token later."
            attempts = found.get("attempts") or []
            if attempts and not found.get("ok"):
                last = attempts[-1]
                last_message = last.get("error") or f"HTTP {last.get('status_code')} from {last.get('url')}"
            elif not found.get("ok"):
                last_message = found.get("message") or "Home Assistant has not responded yet."
        except Exception as e:
            last_message = str(e)
        if progress:
            elapsed_min = max(1, int((time.monotonic() - started) // 60) + 1)
            progress(
                f"Waiting for Home Assistant first boot. Elapsed about {elapsed_min} minute(s) of up to 25. Last check: {last_message}"
            )
        time.sleep(max(5, int(interval_seconds)))
    return {
        "ok": False,
        "message": (
            "Timed out waiting for Home Assistant first boot after 25 minutes. "
            "The VM is still running; wait longer or press Find Home Assistant."
        ),
        "error": last_message,
        "elapsed_seconds": int(time.monotonic() - started),
    }
