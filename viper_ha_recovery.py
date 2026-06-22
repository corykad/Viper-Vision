import argparse
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import viper_config as cfg
import viper_discovery as discovery
import viper_ha_vm as ha_vm
import viper_health


STATE_FILE = cfg.DATA_DIR / "ha_recovery_state.json"
EVENT_TYPE = "ha_vbox_recovery"
VM_NAME = ha_vm.HA_VM_NAME


def _hidden_subprocess_kwargs():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def _run(args, *, timeout=30):
    result = subprocess.run(
        [str(part) for part in args],
        capture_output=True,
        text=True,
        timeout=timeout,
        **_hidden_subprocess_kwargs(),
    )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output}


def _vbox(args, *, timeout=60):
    exe = ha_vm.find_vboxmanage()
    if not exe:
        return {"ok": False, "returncode": None, "output": "VirtualBox was not found."}
    return _run([exe, *args], timeout=timeout)


def _service_state(name):
    if os.name != "nt":
        return "not_applicable"
    result = _run(["sc.exe", "query", name], timeout=8)
    if not result["ok"] and "does not exist" in result.get("output", "").lower():
        return "missing"
    match = re.search(r"STATE\s+:\s+\d+\s+(\w+)", result.get("output", ""))
    return match.group(1).lower() if match else "unknown"


def is_admin():
    return ha_vm.is_windows_admin()


def get_vm_state(vm_name=VM_NAME):
    result = _vbox(["showvminfo", vm_name, "--machinereadable"], timeout=20)
    if not result["ok"]:
        return {"ok": False, "state": "unknown", "message": result["output"], "raw": result}
    match = re.search(r'^VMState="([^"]+)"', result["output"], re.MULTILINE)
    state = match.group(1) if match else "unknown"
    return {"ok": True, "state": state, "message": f"VirtualBox VM is {state}.", "raw": result}


def classify_vbox_start_error(output):
    text = str(output or "")
    lowered = text.lower()
    if "vboxdrvstub" in lowered or "vboxsup" in lowered or "status_object_name_not_found" in lowered:
        return {
            "state": "vbox_driver_broken",
            "message": "VirtualBox core driver is missing or stuck. Run the VirtualBox installer Repair, or reboot and start vboxsup.",
        }
    if "nonexistent host networking interface" in lowered or "bridgeadapter" in lowered:
        return {
            "state": "vbox_bridge_broken",
            "message": "VirtualBox bridged networking is broken or points at a missing adapter.",
        }
    return {"state": "vbox_start_failed", "message": text[:500] or "VirtualBox could not start the Home Assistant VM."}


def diagnose(config_data=None):
    settings = cfg.get_ha_settings(config_data, include_env=True)
    token = settings.get("ha_token") or ""
    host = settings.get("ha_ip") or ""
    port = settings.get("ha_port") or "8123"
    ha_health = discovery.check_ha_core_health(ha_ip=host, ha_port=port, token=token, timeout=3)
    vbox_status = ha_vm.get_virtualbox_status()
    vm = get_vm_state()
    services = {
        "VBoxSDS": _service_state("VBoxSDS"),
        "vboxsup": _service_state("vboxsup"),
    }

    if ha_health.get("ok"):
        state = "healthy"
        message = ha_health.get("message") or "Home Assistant is healthy."
        severity = "ok"
    elif not vbox_status.get("installed"):
        state = "virtualbox_missing"
        message = vbox_status.get("message") or "VirtualBox is not installed."
        severity = "broken"
    elif vm.get("state") in {"poweroff", "saved", "aborted"}:
        state = "vm_stopped"
        message = f'Home Assistant VM is {vm.get("state")}.'
        severity = "broken"
    elif vm.get("state") == "running" and ha_health.get("state") == "core_hung":
        state = "ha_core_hung"
        message = ha_health.get("message") or "Home Assistant Core is hung."
        severity = "broken"
    elif vm.get("state") == "running":
        state = "ha_unreachable"
        message = ha_health.get("message") or "Home Assistant is unreachable even though the VM is running."
        severity = "broken"
    else:
        state = "unknown"
        message = vm.get("message") or ha_health.get("message") or "Home Assistant state is unknown."
        severity = "broken"

    return {
        "ok": severity == "ok",
        "state": state,
        "severity": severity,
        "message": message,
        "ha_health": ha_health,
        "virtualbox": vbox_status,
        "vm": vm,
        "services": services,
        "host": host,
        "port": port,
        "admin": is_admin(),
    }


def send_pushover(title, message):
    settings = cfg.get_api_settings(include_env=True)
    api_token = settings.get("pushover_api_token")
    user_key = settings.get("pushover_user_key")
    if not settings.get("pushover_enabled") or not api_token or not user_key:
        logging.info("[HA RECOVERY] Pushover skipped: not configured.")
        return False
    response = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"token": api_token, "user": user_key, "title": title, "message": message},
        timeout=10,
    )
    response.raise_for_status()
    return True


def _load_state(path=STATE_FILE):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state, path=STATE_FILE):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _notify(title, message, *, push=True, notifier=send_pushover):
    logging.info("[HA RECOVERY] %s: %s", title, message)
    if push:
        try:
            return bool(notifier(title, message))
        except Exception:
            logging.warning("[HA RECOVERY] Pushover failed.", exc_info=True)
    return False


def _record(status, message, details=None):
    return viper_health.record_health_event(EVENT_TYPE, status, message, details=details or {})


def compact_diagnosis(diagnosis):
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    ha_health = diagnosis.get("ha_health") if isinstance(diagnosis.get("ha_health"), dict) else {}
    core = ha_health.get("core") if isinstance(ha_health.get("core"), dict) else {}
    observer = ha_health.get("observer") if isinstance(ha_health.get("observer"), dict) else {}
    vm = diagnosis.get("vm") if isinstance(diagnosis.get("vm"), dict) else {}
    virtualbox = diagnosis.get("virtualbox") if isinstance(diagnosis.get("virtualbox"), dict) else {}
    return {
        "ok": bool(diagnosis.get("ok")),
        "state": diagnosis.get("state") or "unknown",
        "message": diagnosis.get("message") or "",
        "host": diagnosis.get("host") or "",
        "port": diagnosis.get("port") or "",
        "admin": bool(diagnosis.get("admin")),
        "core": {
            "ok": bool(core.get("ok")),
            "status_code": core.get("status_code"),
            "elapsed_ms": core.get("elapsed_ms"),
            "message": core.get("message") or "",
        },
        "observer": {
            "ok": bool(observer.get("ok")),
            "status_code": observer.get("status_code"),
            "elapsed_ms": observer.get("elapsed_ms"),
            "message": observer.get("message") or "",
        },
        "vm": {
            "ok": bool(vm.get("ok")),
            "state": vm.get("state") or "unknown",
            "message": vm.get("message") or "",
        },
        "services": diagnosis.get("services") if isinstance(diagnosis.get("services"), dict) else {},
        "virtualbox": {
            "installed": bool(virtualbox.get("installed")),
            "version": virtualbox.get("version") or "",
            "path": virtualbox.get("path") or "",
        },
    }


def compact_result(result):
    result = result if isinstance(result, dict) else {}
    return {
        "ok": bool(result.get("ok")),
        "action": result.get("action") or "",
        "message": result.get("message") or "",
        "before": compact_diagnosis(result.get("before") or {}),
        "after": compact_diagnosis(result.get("after") or {}),
    }


def compact_status_line(result):
    compact = compact_result(result)
    after = compact.get("after") or {}
    core = after.get("core") or {}
    observer = after.get("observer") or {}
    vm = after.get("vm") or {}
    services = after.get("services") or {}
    return (
        f"ok={compact.get('ok')} action={compact.get('action') or 'none'} "
        f"state={after.get('state')} core={core.get('status_code') or core.get('ok')} "
        f"observer={observer.get('status_code') or observer.get('ok')} "
        f"vm={vm.get('state')} VBoxSDS={services.get('VBoxSDS', 'unknown')} "
        f"vboxsup={services.get('vboxsup', 'unknown')} message={compact.get('message')}"
    )


def send_recovery_test_push(notifier=send_pushover):
    return _notify(
        "Viper HA recovery test",
        "This is a safe test. The HA recovery watchdog can send Pushover notifications.",
        push=True,
        notifier=notifier,
    )


def repair_once(*, push=True, notifier=send_pushover, state_path=STATE_FILE, reset_after_failures=3):
    before = diagnose()
    state = _load_state(state_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last_state = state.get("last_problem_state")
    if before["ok"]:
        if last_state and state.get("notified_problem"):
            _notify("Viper HA recovery fixed", "Home Assistant is responding again.", push=push, notifier=notifier)
            _record("fixed", "Home Assistant is responding again.", {"before": before})
        state.update({"last_problem_state": "", "notified_problem": False, "failures": 0, "last_checked": now})
        _save_state(state, state_path)
        return {"ok": True, "action": "none", "before": before, "after": before, "message": before["message"]}

    failures = int(state.get("failures") or 0) + 1
    state.update({"last_problem_state": before["state"], "notified_problem": True, "failures": failures, "last_checked": now})
    _save_state(state, state_path)

    if before["state"] != last_state:
        _notify("Viper HA problem detected", before["message"], push=push, notifier=notifier)
    _record("detected", before["message"], {"diagnosis": before, "failures": failures})

    action = "manual"
    repair_message = "Manual repair required."
    if before["state"] == "vm_stopped":
        action = "start_vm"
        repair_message = "Starting the Home Assistant VirtualBox VM."
        _notify("Viper HA recovery started", repair_message, push=push, notifier=notifier)
        result = _vbox(["startvm", VM_NAME, "--type", "headless"], timeout=60)
        if not result["ok"]:
            classified = classify_vbox_start_error(result["output"])
            after = diagnose()
            message = f"{classified['message']} Start output: {result['output'][:300]}"
            _notify("Viper HA recovery failed", message, push=push, notifier=notifier)
            _record("failed", message, {"before": before, "after": after, "start": result, "classified": classified})
            return {"ok": False, "action": action, "before": before, "after": after, "message": message, "repair": result}
    elif before["state"] in {"ha_core_hung", "ha_unreachable"} and failures >= reset_after_failures:
        action = "reset_vm"
        repair_message = f"Home Assistant failed {failures} checks. Resetting the VM."
        _notify("Viper HA recovery started", repair_message, push=push, notifier=notifier)
        result = _vbox(["controlvm", VM_NAME, "reset"], timeout=60)
        if not result["ok"]:
            after = diagnose()
            message = f"VirtualBox could not reset the Home Assistant VM: {result['output'][:400]}"
            _notify("Viper HA recovery failed", message, push=push, notifier=notifier)
            _record("failed", message, {"before": before, "after": after, "reset": result})
            return {"ok": False, "action": action, "before": before, "after": after, "message": message, "repair": result}
    else:
        message = before["message"]
        if before["state"] in {"ha_core_hung", "ha_unreachable"}:
            message = f"{before['message']} Waiting for {reset_after_failures} failed checks before resetting the VM."
        _record("waiting", message, {"before": before, "failures": failures})
        return {"ok": False, "action": "wait", "before": before, "after": before, "message": message}

    time.sleep(15)
    after = diagnose()
    if after["ok"]:
        state.update({"last_problem_state": "", "notified_problem": False, "failures": 0, "last_repair": now})
        _save_state(state, state_path)
        message = f"Recovery succeeded after action: {action}."
        _notify("Viper HA recovery fixed", message, push=push, notifier=notifier)
        _record("fixed", message, {"before": before, "after": after, "action": action})
        return {"ok": True, "action": action, "before": before, "after": after, "message": message}

    message = f"Recovery action {action} finished, but Home Assistant is still not healthy: {after['message']}"
    _notify("Viper HA recovery failed", message, push=push, notifier=notifier)
    _record("failed", message, {"before": before, "after": after, "action": action})
    return {"ok": False, "action": action, "before": before, "after": after, "message": message}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Diagnose and repair Viper's Home Assistant VirtualBox VM.")
    parser.add_argument("--diagnose", action="store_true", help="Only print diagnosis.")
    parser.add_argument("--no-push", action="store_true", help="Do not send Pushover notifications.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON and a one-line summary for logs.")
    parser.add_argument("--test-push", action="store_true", help="Send a safe HA recovery Pushover test without repairing anything.")
    args = parser.parse_args(argv)
    if args.diagnose:
        data = compact_diagnosis(diagnose()) if args.compact else diagnose()
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if args.test_push:
        ok = send_recovery_test_push()
        print(json.dumps({"ok": ok, "message": "HA recovery Pushover test sent." if ok else "HA recovery Pushover test failed."}, indent=2, sort_keys=True))
        return 0 if ok else 1
    result = repair_once(push=not args.no_push)
    if args.compact:
        print(compact_status_line(result))
        print(json.dumps(compact_result(result), indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
