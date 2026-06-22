import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from pathlib import Path

import viper_config as cfg
import viper_health


APP_VERSION = "1.2.3"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2
FRIDGE_DOOR_ENTITY = "binary_sensor.refrigerator_fridge_door"
FREEZER_DOOR_ENTITY = "binary_sensor.refrigerator_freezer_door"
HA_SNAPSHOT_LATEST = "ha_integration_snapshot_latest.json"

SECRET_KEYWORDS = (
    "token",
    "api_key",
    "apikey",
    "password",
    "secret",
    "gemini",
    "pushover",
    "mqtt_password",
    "rtsp_password",
)

RING_ID_KEYS = {
    "front_camera_id",
    "back_camera_id",
    "ring_topic_root",
    "camera_id",
    "mqtt_topic",
    "front_doorbell_mqtt_topic",
    "back_doorbell_mqtt_topic",
}


def _entity_domain(entity_id):
    return str(entity_id or "").split(".", 1)[0] if "." in str(entity_id or "") else ""


def _entity_name_text(entity):
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    return " ".join(
        str(value or "").lower()
        for value in (
            entity.get("entity_id", ""),
            attrs.get("friendly_name", ""),
            attrs.get("device_class", ""),
        )
    )


def _snapshot_entity_relevant(entity, config_data):
    entity_id = str(entity.get("entity_id") or "")
    if not entity_id:
        return False
    text = _entity_name_text(entity)
    configured = set()
    config_data = config_data if isinstance(config_data, dict) else {}
    for key in (
        "cinderella_status_entity",
        "cinderella_vacuum_error_entity",
        "cinderella_dock_error_entity",
        "cinderella_mop_drying_entity",
        "ice_maker_switch_entity",
        "ice_maker_keep_on_entity",
        "ice_maker_counter_entity",
    ):
        value = str(config_data.get(key) or "").strip()
        if value:
            configured.add(value)
    triggers = config_data.get("doorbell_triggers") if isinstance(config_data.get("doorbell_triggers"), dict) else {}
    for trigger in triggers.values():
        if isinstance(trigger, dict) and trigger.get("trigger_entity_id"):
            configured.add(str(trigger.get("trigger_entity_id")))
    for speaker in (config_data.get("speakers") or {}).values() if isinstance(config_data.get("speakers"), dict) else []:
        if isinstance(speaker, dict) and speaker.get("id"):
            configured.add(str(speaker.get("id")))
    if entity_id in configured:
        return True
    tokens = (
        "refrigerator",
        "fridge",
        "freezer",
        "water_filter",
        "filter",
        "cinderella",
        "roborock",
        "saros",
        "ring",
        "doorbell",
        "viper_",
    )
    return any(token in text for token in tokens)


def build_ha_integration_snapshot(config_data=None, *, ha_states=None, ha_listener_status=None, generated_at=None):
    config_data = cfg.validate_and_normalize_config(config_data if config_data is not None else cfg.load_config())
    ha_states = ha_states if isinstance(ha_states, list) else []
    relevant = [entity for entity in ha_states if isinstance(entity, dict) and _snapshot_entity_relevant(entity, config_data)]
    entities = {}
    categories = {
        "doorbell": 0,
        "fridge": 0,
        "freezer": 0,
        "vacuum": 0,
        "speakers": 0,
        "other": 0,
    }
    for entity in sorted(relevant, key=lambda item: str(item.get("entity_id") or "")):
        entity_id = str(entity.get("entity_id") or "")
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        options = attrs.get("options") if isinstance(attrs.get("options"), list) else None
        entry = {
            "domain": _entity_domain(entity_id),
            "state": str(entity.get("state", "")),
            "friendly_name": str(attrs.get("friendly_name") or ""),
            "device_class": str(attrs.get("device_class") or ""),
            "last_changed": str(entity.get("last_changed") or ""),
            "last_updated": str(entity.get("last_updated") or ""),
        }
        if options is not None:
            entry["options"] = [str(item) for item in options]
        if isinstance(attrs.get("fan_speed_list"), list):
            entry["fan_speed_list"] = [str(item) for item in attrs.get("fan_speed_list")]
        entities[entity_id] = entry
        text = _entity_name_text(entity)
        if "freezer" in text:
            categories["freezer"] += 1
        elif "refrigerator" in text or "fridge" in text or "filter" in text:
            categories["fridge"] += 1
        elif any(token in text for token in ("cinderella", "roborock", "saros")):
            categories["vacuum"] += 1
        elif any(token in text for token in ("ring", "doorbell", "viper_")):
            categories["doorbell"] += 1
        elif _entity_domain(entity_id) == "media_player":
            categories["speakers"] += 1
        else:
            categories["other"] += 1
    return {
        "generated_at": (generated_at or datetime.now()).isoformat(timespec="seconds"),
        "entity_count": len(entities),
        "categories": categories,
        "listener": deepcopy(ha_listener_status or {}),
        "entities": entities,
    }


def diff_ha_integration_snapshots(previous, current):
    previous_entities = (previous or {}).get("entities") if isinstance(previous, dict) else {}
    current_entities = (current or {}).get("entities") if isinstance(current, dict) else {}
    previous_entities = previous_entities if isinstance(previous_entities, dict) else {}
    current_entities = current_entities if isinstance(current_entities, dict) else {}
    previous_ids = set(previous_entities)
    current_ids = set(current_entities)
    changed = []
    for entity_id in sorted(previous_ids & current_ids):
        old = previous_entities[entity_id]
        new = current_entities[entity_id]
        fields = {}
        for key in ("state", "friendly_name", "device_class", "options", "fan_speed_list"):
            if old.get(key) != new.get(key):
                fields[key] = {"old": old.get(key), "new": new.get(key)}
        if fields:
            changed.append({"entity_id": entity_id, "fields": fields})
    return {
        "added": sorted(current_ids - previous_ids),
        "removed": sorted(previous_ids - current_ids),
        "changed": changed,
    }


def save_ha_integration_snapshot(config_data=None, *, ha_states=None, ha_listener_status=None, output_dir=None):
    output = Path(output_dir or cfg.DATA_DIR)
    output.mkdir(parents=True, exist_ok=True)
    latest = output / HA_SNAPSHOT_LATEST
    previous = {}
    if latest.exists():
        try:
            previous = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    snapshot = build_ha_integration_snapshot(
        config_data,
        ha_states=ha_states,
        ha_listener_status=ha_listener_status,
    )
    diff = diff_ha_integration_snapshots(previous, snapshot) if previous else {"added": [], "removed": [], "changed": []}
    stamped = output / f"ha_integration_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    text = json.dumps(snapshot, indent=2, sort_keys=True)
    stamped.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return {
        "ok": True,
        "path": str(stamped),
        "latest_path": str(latest),
        "snapshot": snapshot,
        "diff": diff,
    }


def _mask(value):
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 6:
        return "[REDACTED]"
    return f"{text[:2]}...[REDACTED]...{text[-2:]}"


def should_redact_key(key):
    text = str(key or "").lower()
    return any(word in text for word in SECRET_KEYWORDS) or text in RING_ID_KEYS


def redact_config(value, key=""):
    if should_redact_key(key):
        return _mask(value)
    if isinstance(value, dict):
        return {str(k): redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(item, key) for item in value]
    return deepcopy(value)


def redact_text(text):
    redacted = str(text or "")
    patterns = [
        r"(Authorization:\s*Bearer\s+)[^\s]+",
        r"((?:ha_token|gemini_api_key|pushover_user_key|pushover_api_token|mqtt_password|rtsp_password)\s*[=:]\s*)[^\s,;]+",
        r"([\"'](?:ha_token|gemini_api_key|pushover_user_key|pushover_api_token|mqtt_password|rtsp_password|token|api[_-]?key|password|secret)[\"']\s*:\s*[\"'])[^\"']+([\"'])",
        r"((?:token|api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"(rtsp://[^:\s]+:)[^@\s]+(@)",
    ]
    for pattern in patterns:
        if pattern.endswith("(@)"):
            repl = r"\1[REDACTED]\2"
        elif "([\"'])" in pattern:
            repl = r"\1[REDACTED]\3"
        else:
            repl = r"\1[REDACTED]"
        redacted = re.sub(pattern, repl, redacted, flags=re.IGNORECASE)
    return redacted


def ffmpeg_status():
    configured = cfg.FFMPEG_BIN
    path = shutil.which(configured) or configured
    exists = bool(path and (Path(path).exists() or shutil.which(path)))
    return {"configured": configured, "resolved": str(path or ""), "available": exists}


def recent_log_lines(log_path=None, limit=80):
    path = Path(log_path or (cfg.DATA_DIR / "viper_full_debug.log"))
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    interesting = [line for line in lines if any(word in line for word in ("ERROR", "WARNING", "Traceback", "CRITICAL"))]
    return [redact_text(line) for line in interesting[-limit:]]


def _hidden_subprocess_kwargs():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def _run_powershell_json(command, *, timeout=10):
    if os.name != "nt":
        return {"ok": False, "message": "Windows Task Scheduler checks only run on Windows."}
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        return {"ok": False, "message": "PowerShell was not found."}
    try:
        result = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            **_hidden_subprocess_kwargs(),
        )
    except Exception as e:
        return {"ok": False, "message": str(e)}
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        return {"ok": False, "message": (result.stderr or output or f"PowerShell exited {result.returncode}").strip()}
    if not output:
        return {"ok": False, "message": "PowerShell returned no data."}
    try:
        return {"ok": True, "data": json.loads(output)}
    except json.JSONDecodeError:
        return {"ok": False, "message": output}


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tail_file(path, limit=12):
    path = Path(path)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").replace("\x00", "")
        return [line for line in text.splitlines() if line.strip()][-limit:]
    except OSError:
        return []


def ha_watchdog_status():
    task_name = "Viper Home Assistant Watchdog"
    log_path = cfg.DATA_DIR / "ha_vm_watchdog.log"
    state_path = cfg.DATA_DIR / "ha_recovery_state.json"
    status = {
        "checked": True,
        "task_name": task_name,
        "installed": False,
        "state": "unknown",
        "last_run_time": "",
        "last_task_result": "",
        "next_run_time": "",
        "action_execute": "",
        "action_arguments": "",
        "silent": False,
        "last_recovery_state": _read_json_file(state_path),
        "log_path": str(log_path),
        "recent_log_lines": [redact_text(line) for line in _tail_file(log_path, limit=12)],
        "message": "Watchdog task has not been checked.",
    }
    command = rf"""
$task = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue
if ($null -eq $task) {{
  [pscustomobject]@{{ installed=$false; message='Scheduled task is not installed.' }} | ConvertTo-Json -Compress
}} else {{
  $info = Get-ScheduledTaskInfo -TaskName '{task_name}'
  $action = $task.Actions | Select-Object -First 1
  [pscustomobject]@{{
    installed=$true
    state=[string]$task.State
    execute=[string]$action.Execute
    arguments=[string]$action.Arguments
    lastRunTime=[string]$info.LastRunTime
    lastTaskResult=[string]$info.LastTaskResult
    nextRunTime=[string]$info.NextRunTime
    missedRuns=[int]$info.NumberOfMissedRuns
    runLevel=[string]$task.Principal.RunLevel
  }} | ConvertTo-Json -Compress
}}
"""
    result = _run_powershell_json(command)
    if result.get("ok"):
        data = result.get("data") or {}
        status.update({
            "installed": bool(data.get("installed")),
            "state": str(data.get("state") or "missing"),
            "last_run_time": str(data.get("lastRunTime") or ""),
            "last_task_result": str(data.get("lastTaskResult") or ""),
            "next_run_time": str(data.get("nextRunTime") or ""),
            "action_execute": str(data.get("execute") or ""),
            "action_arguments": str(data.get("arguments") or ""),
            "missed_runs": data.get("missedRuns"),
            "run_level": str(data.get("runLevel") or ""),
        })
    else:
        status["message"] = result.get("message") or "Could not query scheduled task."
        return status
    status["silent"] = "wscript.exe" in status["action_execute"].lower()
    if not status["installed"]:
        status["message"] = "Scheduled task is not installed."
    elif status["last_task_result"] in {"0", ""}:
        status["message"] = "Watchdog task is installed and last run was clean."
    else:
        status["message"] = f"Watchdog task is installed, but last result was {status['last_task_result']}."
    return status


def ha_watchdog_status_text(status):
    status = status if isinstance(status, dict) else {}
    recovery = status.get("last_recovery_state") if isinstance(status.get("last_recovery_state"), dict) else {}
    lines = [
        "HA watchdog status:",
        f"Installed: {'yes' if status.get('installed') else 'no'}",
        f"Task state: {status.get('state') or 'unknown'}",
        f"Silent runner: {'yes' if status.get('silent') else 'no'}",
        f"Run level: {status.get('run_level') or 'unknown'}",
        f"Last run: {status.get('last_run_time') or 'never'}",
        f"Last result: {status.get('last_task_result') or 'unknown'}",
        f"Next run: {status.get('next_run_time') or 'unknown'}",
        f"Action: {status.get('action_execute') or 'unknown'} {status.get('action_arguments') or ''}".strip(),
        f"Recovery failures: {recovery.get('failures', 0)}",
        f"Last problem: {recovery.get('last_problem_state') or 'none'}",
        f"Message: {status.get('message') or ''}",
    ]
    logs = status.get("recent_log_lines") or []
    if logs:
        lines.extend(["", "Recent watchdog log:"])
        lines.extend(logs[-8:])
    return "\n".join(lines)


def _recent_log_noise(log_path=None, limit=20):
    path = Path(log_path or (cfg.DATA_DIR / "viper_full_debug.log"))
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    patterns = (
        "ALEXA PLAY SKIP",
        "ignored_disarmed",
        "[CLEANUP] Purged",
    )
    noise = [line for line in lines if any(pattern in line for pattern in patterns)]
    return [redact_text(line) for line in noise[-limit:]]


def _resolved_crash_files(limit=5):
    try:
        files = sorted(
            cfg.DATA_DIR.glob("viper_last_crash.resolved.*.txt"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    resolved = []
    for path in files[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        resolved.append({
            "path": str(path),
            "name": path.name,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "bytes": stat.st_size,
        })
    return resolved


def _last_current_log_line():
    path = cfg.DATA_DIR / "viper_full_debug.log"
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _parse_ha_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _history_transition_counts(history):
    rows = history if isinstance(history, list) else []
    states = [str(item.get("state") or "").strip().lower() for item in rows if isinstance(item, dict)]
    return {
        "rows": len(states),
        "opens": sum(1 for state in states if state in {"on", "open"}),
        "closes": sum(1 for state in states if state in {"off", "closed"}),
        "unavailable": sum(1 for state in states if state in {"unavailable", "unknown"}),
        "latest_state": states[-1] if states else "",
        "latest_changed": next(
            (
                str(item.get("last_changed") or "")
                for item in reversed(rows)
                if isinstance(item, dict) and item.get("last_changed")
            ),
            "",
        ),
    }


def refrigerator_door_sensor_diagnostics(*, states=None, histories=None, now=None):
    states = states if isinstance(states, list) else []
    histories = histories if isinstance(histories, dict) else {}
    by_id = {item.get("entity_id"): item for item in states if isinstance(item, dict)}
    fridge = by_id.get(FRIDGE_DOOR_ENTITY)
    freezer = by_id.get(FREEZER_DOOR_ENTITY)
    result = {
        "checked": bool(fridge or freezer or histories),
        "ok": True,
        "status": "unknown",
        "message": "Refrigerator door sensors were not checked.",
        "fridge": {
            "entity_id": FRIDGE_DOOR_ENTITY,
            "present": bool(fridge),
            "state": fridge.get("state") if fridge else "",
            "last_changed": fridge.get("last_changed") if fridge else "",
            "history": _history_transition_counts(histories.get(FRIDGE_DOOR_ENTITY, [])),
        },
        "freezer": {
            "entity_id": FREEZER_DOOR_ENTITY,
            "present": bool(freezer),
            "state": freezer.get("state") if freezer else "",
            "last_changed": freezer.get("last_changed") if freezer else "",
            "history": _history_transition_counts(histories.get(FREEZER_DOOR_ENTITY, [])),
        },
    }
    if not result["checked"]:
        return result
    if not fridge or not freezer:
        missing = "fridge" if not fridge else "freezer"
        result.update({
            "ok": False,
            "status": "missing_entity",
            "message": f"Home Assistant is missing the refrigerator {missing} door entity.",
        })
        return result

    fridge_hist = result["fridge"]["history"]
    freezer_hist = result["freezer"]["history"]
    freezer_active = freezer_hist["opens"] > 0
    fridge_inactive = fridge_hist["opens"] == 0
    if freezer_active and fridge_inactive:
        result.update({
            "ok": False,
            "status": "fridge_sensor_stale",
            "message": (
                "Freezer door events are reaching Home Assistant, but the fridge door has no open events "
                "in the same recent diagnostic window. Reload or re-auth SmartThings, then test the fridge door entity."
            ),
        })
        return result

    now = now or datetime.now(timezone.utc)
    fridge_changed = _parse_ha_time(fridge.get("last_changed"))
    freezer_changed = _parse_ha_time(freezer.get("last_changed"))
    if fridge_changed and freezer_changed and freezer_changed > fridge_changed:
        delta = (freezer_changed - fridge_changed).total_seconds()
        if delta >= 2 * 60 * 60:
            result.update({
                "ok": False,
                "status": "fridge_sensor_older_than_freezer",
                "message": (
                    "The freezer door sensor has changed recently, but the fridge door sensor has been quiet for over two hours. "
                    "If the fridge was opened during that time, SmartThings is not reporting the fridge compartment contact."
                ),
            })
            return result

    result.update({
        "status": "ok",
        "message": "Fridge and freezer door sensors both look available in Home Assistant.",
    })
    return result


def build_health_summary(config_data, *, ha_listener_status=None, ha_connection=None, ha_health=None, fridge_sensor_health=None, recent_errors=None):
    shape = config_shape(config_data)
    ha_listener_status = ha_listener_status or {}
    ha_connection = ha_connection or {"checked": False}
    ha_health = ha_health or {"checked": False}
    active = []
    history = []

    crash_path = cfg.DATA_DIR / "viper_last_crash.txt"
    if crash_path.exists():
        active.append(f"Active crash report is present: {crash_path.name}.")
    for item in _resolved_crash_files():
        history.append(f"Resolved crash archive: {item['name']} ({item['modified']}).")

    if not shape["has_ha_host"]:
        active.append("Home Assistant host is not configured.")
    listener_connected = bool(ha_listener_status.get("connected"))
    connection_ok = bool(ha_connection.get("checked") and ha_connection.get("ok"))
    if not shape["has_ha_token"] and not listener_connected and not connection_ok:
        active.append("Home Assistant token is not configured.")
    if not shape["speaker_count"]:
        active.append("No alert speakers are configured.")

    if ha_listener_status:
        listener_enabled = shape["ha_listener_enabled"]
        if listener_enabled and not listener_connected:
            active.append(f"Home Assistant listener is not connected: {ha_listener_status.get('last_error') or 'no error detail'}.")
        elif ha_listener_status.get("last_error"):
            history.append(f"Home Assistant listener last reported: {ha_listener_status.get('last_error')}.")
        critical_status = ha_listener_status.get("critical_health_status")
        if critical_status and critical_status not in {"ok", "disabled"}:
            active.append(
                f"Critical HA watchdog says {critical_status}: "
                f"{ha_listener_status.get('critical_health_message') or 'no detail'}"
            )

    if ha_connection.get("checked") and not ha_connection.get("ok"):
        active.append(f"Home Assistant connection check failed: {ha_connection.get('message') or ha_connection.get('error') or 'no detail'}.")

    if ha_health.get("checked") and not ha_health.get("ok", False):
        state = ha_health.get("state") or "unknown"
        active.append(f"Home Assistant health check is {state}: {ha_health.get('message') or 'no detail'}.")

    fridge_sensor_health = fridge_sensor_health or {"checked": False}
    if fridge_sensor_health.get("checked") and not fridge_sensor_health.get("ok", True):
        active.append(fridge_sensor_health.get("message") or "Refrigerator door sensor diagnostics found an issue.")

    if not ffmpeg_status()["available"]:
        active.append("FFmpeg is not available; camera frame capture may fail.")

    recent_errors = list(recent_errors or [])
    if recent_errors:
        last_error = recent_errors[-1]
        if "no close frame received or sent" in last_error.lower():
            history.append("Recent HA websocket close-frame warning appears transient unless the listener is currently disconnected.")
        elif "503 unavailable" in last_error.lower():
            history.append("Recent AI service 503 indicates provider demand; Viper should retry or use fallback behavior.")

    return {
        "status": "attention" if active else "ok",
        "active_issues": [redact_text(item) for item in active],
        "stale_history": [redact_text(item) for item in history],
        "normal_noise": _recent_log_noise(),
        "last_log_line": redact_text(_last_current_log_line()),
        "log_rotation": {
            "enabled": True,
            "max_bytes": LOG_MAX_BYTES,
            "backup_count": LOG_BACKUP_COUNT,
            "current_log": str(cfg.DATA_DIR / "viper_full_debug.log"),
        },
    }


def config_shape(config_data):
    data = config_data if isinstance(config_data, dict) else {}
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    api_settings = cfg.get_api_settings(data, include_env=True)
    return {
        "has_ha_host": bool(ha_settings.get("ha_ip")),
        "has_ha_token": bool(ha_settings.get("ha_token")),
        "has_gemini_key": bool(api_settings.get("gemini_api_key")),
        "ha_listener_enabled": bool(data.get("ha_listener_enabled", True)),
        "speaker_count": len(data.get("speakers", {}) if isinstance(data.get("speakers"), dict) else {}),
        "front_rtsp_configured": bool(data.get("rtsp_front") or data.get("doorbell_triggers", {}).get("front", {}).get("rtsp_url")),
        "back_rtsp_configured": bool(data.get("rtsp_back") or data.get("doorbell_triggers", {}).get("back", {}).get("rtsp_url")),
        "cinderella_enabled": bool(data.get("cinderella_enabled", True)),
    }


def collect_diagnostics(config_data=None, *, ha_listener_status=None, ha_connection=None, ha_health=None, ha_states=None, fridge_histories=None):
    config_data = cfg.validate_and_normalize_config(config_data if config_data is not None else cfg.load_config())
    recent_errors = recent_log_lines()
    fridge_sensor_health = refrigerator_door_sensor_diagnostics(states=ha_states, histories=fridge_histories)
    health = build_health_summary(
        config_data,
        ha_listener_status=ha_listener_status,
        ha_connection=ha_connection,
        ha_health=ha_health,
        fridge_sensor_health=fridge_sensor_health,
        recent_errors=recent_errors,
    )
    diag_base = {
        "health": health,
        "ha_listener": ha_listener_status or {},
        "fridge_sensor_health": fridge_sensor_health,
        "ffmpeg": ffmpeg_status(),
        "ha_connection": ha_connection or {"checked": False},
    }
    critical_workflows = viper_health.critical_workflow_status(config_data, diag=diag_base)
    return {
        "app": {
            "name": "Viper Vision",
            "version": APP_VERSION,
            "frozen": bool(getattr(sys, "frozen", False)),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": sys.executable,
        },
        "paths": {
            "app_dir": str(cfg.APP_DIR),
            "data_dir": str(cfg.DATA_DIR),
            "config_file": str(cfg.CONFIG_FILE),
            "log_file": str(cfg.DATA_DIR / "viper_full_debug.log"),
        },
        "ffmpeg": diag_base["ffmpeg"],
        "config_shape": config_shape(config_data),
        "ha_listener": ha_listener_status or {},
        "ha_connection": ha_connection or {"checked": False},
        "ha_health": ha_health or {"checked": False},
        "ha_watchdog": ha_watchdog_status(),
        "fridge_sensor_health": fridge_sensor_health,
        "critical_workflows": critical_workflows,
        "recent_health_events": viper_health.recent_health_events(limit=8),
        "beginner_health": viper_health.beginner_health_lines(diag_base),
        "health": health,
        "recent_errors": recent_errors,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def health_summary_text(diag):
    health = diag.get("health", {}) if isinstance(diag, dict) else {}
    listener = diag.get("ha_listener", {}) if isinstance(diag, dict) else {}
    status = health.get("status") or "unknown"
    lines = [
        f"Health status: {status.upper()}",
        "",
        "Plain-English status:",
    ]
    lines.extend(diag.get("beginner_health") or ["No plain-English health details were generated."])
    lines.extend(["", "Critical workflow canaries:"])
    lines.extend(viper_health.critical_workflow_lines(diag.get("critical_workflows") or {}))
    lines.extend([
        "",
        "Active issues:",
    ])
    lines.extend(health.get("active_issues") or ["None detected."])
    lines.extend(["", "Resolved or historical notes:"])
    lines.extend((health.get("stale_history") or ["None."])[:8])
    lines.extend(["", "Normal log noise recently seen:"])
    lines.extend((health.get("normal_noise") or ["None."])[-6:])
    rotation = health.get("log_rotation") or {}
    if rotation:
        lines.extend([
            "",
            f"Log rotation: {rotation.get('max_bytes', LOG_MAX_BYTES)} bytes, {rotation.get('backup_count', LOG_BACKUP_COUNT)} backups.",
        ])
    lines.extend(
        [
            "",
            ha_watchdog_status_text(diag.get("ha_watchdog") or {}),
            "",
            "HA event health:",
            f"Listener connected: {'yes' if listener.get('connected') else 'no'}",
            f"Last event entity: {listener.get('last_event_entity') or 'none'}",
            f"Last event raw: {listener.get('last_event_old_state') or ''} -> {listener.get('last_event_new_state') or ''}",
            f"Last event normalized: {listener.get('last_event_old_normalized') or ''} -> {listener.get('last_event_new_normalized') or ''}",
            f"Last routed action count: {listener.get('last_event_action_count', 0)}",
            f"Reconnect count: {listener.get('reconnect_count', 0)}",
            f"Poll failures: {listener.get('poll_failure_count', 0)}",
            f"Last poll error: {listener.get('last_poll_error') or 'none'}",
        ]
    )
    if health.get("last_log_line"):
        lines.extend(["", f"Latest log line: {health['last_log_line']}"])
    events = diag.get("recent_health_events") or []
    if events:
        lines.extend(["", "Recent health recovery journal:"])
        for item in events[-6:]:
            lines.append(f"{item.get('timestamp')}: {item.get('event_type')} {item.get('status')}: {item.get('message')}")
    return "\n".join(lines)


def diagnostics_text(diag):
    lines = [
        "Viper Vision Diagnostics",
        f"Version: {diag['app']['version']}",
        f"Frozen build: {'yes' if diag['app']['frozen'] else 'no'}",
        f"Python: {diag['app']['python']}",
        f"Platform: {diag['app']['platform']}",
        f"Data folder: {diag['paths']['data_dir']}",
        f"Config file: {diag['paths']['config_file']}",
        f"Health status: {diag.get('health', {}).get('status', 'unknown')}",
        f"FFmpeg available: {'yes' if diag['ffmpeg']['available'] else 'no'}",
        f"FFmpeg resolved path: {diag['ffmpeg']['resolved']}",
        f"Log rotation: {diag.get('health', {}).get('log_rotation', {}).get('max_bytes', LOG_MAX_BYTES)} bytes, {diag.get('health', {}).get('log_rotation', {}).get('backup_count', LOG_BACKUP_COUNT)} backups",
        f"HA listener connected: {'yes' if diag.get('ha_listener', {}).get('connected') else 'no'}",
        f"HA listener last error: {diag.get('ha_listener', {}).get('last_error') or 'none'}",
        f"HA listener last event entity: {diag.get('ha_listener', {}).get('last_event_entity') or 'none'}",
        f"HA listener last event raw: {diag.get('ha_listener', {}).get('last_event_old_state') or ''} -> {diag.get('ha_listener', {}).get('last_event_new_state') or ''}",
        f"HA listener last event normalized: {diag.get('ha_listener', {}).get('last_event_old_normalized') or ''} -> {diag.get('ha_listener', {}).get('last_event_new_normalized') or ''}",
        f"HA listener last event action count: {diag.get('ha_listener', {}).get('last_event_action_count', 0)}",
        f"HA listener reconnect count: {diag.get('ha_listener', {}).get('reconnect_count', 0)}",
        f"HA listener poll failures: {diag.get('ha_listener', {}).get('poll_failure_count', 0)}",
        f"HA listener last poll error: {diag.get('ha_listener', {}).get('last_poll_error') or 'none'}",
        f"HA watchdog installed: {'yes' if diag.get('ha_watchdog', {}).get('installed') else 'no'}",
        f"HA watchdog last result: {diag.get('ha_watchdog', {}).get('last_task_result') or 'unknown'}",
        f"HA watchdog silent: {'yes' if diag.get('ha_watchdog', {}).get('silent') else 'no'}",
        f"Critical workflows: {diag.get('critical_workflows', {}).get('overall', 'unknown')}",
        f"SmartThings reloads in 24h: {diag.get('ha_listener', {}).get('repeated_smartthings_reloads_24h', 0)}",
        f"Fridge sensor health: {diag.get('fridge_sensor_health', {}).get('status', 'unknown')}",
        f"Fridge sensor message: {diag.get('fridge_sensor_health', {}).get('message', 'not checked')}",
        f"HA host configured: {'yes' if diag['config_shape']['has_ha_host'] else 'no'}",
        f"HA token configured: {'yes' if diag['config_shape']['has_ha_token'] else 'no'}",
        f"Gemini key configured: {'yes' if diag['config_shape']['has_gemini_key'] else 'no'}",
        f"Speakers configured: {diag['config_shape']['speaker_count']}",
        f"Front RTSP configured: {'yes' if diag['config_shape']['front_rtsp_configured'] else 'no'}",
        f"Back RTSP configured: {'yes' if diag['config_shape']['back_rtsp_configured'] else 'no'}",
    ]
    ha_conn = diag.get("ha_connection", {})
    if ha_conn.get("checked"):
        lines.append(f"HA connection: {'ok' if ha_conn.get('ok') else 'failed'}")
        if ha_conn.get("message"):
            lines.append(f"HA connection message: {ha_conn['message']}")
    ha_health = diag.get("ha_health", {})
    if ha_health.get("checked"):
        lines.append(f"HA health state: {ha_health.get('state') or 'unknown'}")
        lines.append(f"HA health message: {ha_health.get('message') or 'none'}")
        core = ha_health.get("core", {})
        observer = ha_health.get("observer", {})
        if core:
            lines.append(
                f"HA Core API: {'responding' if core.get('ok') else 'not responding'}"
                f" ({core.get('message') or 'no result'})"
            )
        if observer:
            lines.append(
                f"HA Observer: {'responding' if observer.get('ok') else 'not responding'}"
                f" ({observer.get('message') or 'no result'})"
            )
    errors = diag.get("recent_errors", [])
    lines.extend(["", health_summary_text(diag)])
    lines.append("")
    lines.append("Recent warnings/errors:")
    lines.extend(errors[-20:] if errors else ["None found in the current log."])
    return "\n".join(lines)


def _write_zip_text(zip_file, name, value):
    zip_file.writestr(name, redact_text(value))


def _redacted_setup_events(events):
    cleaned = []
    for item in events or []:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            str(key): redact_config(value, str(key)) if should_redact_key(key) else (redact_text(value) if isinstance(value, str) else redact_config(value, str(key)))
            for key, value in item.items()
        })
    return cleaned


def create_support_bundle(
    config_data=None,
    *,
    ha_listener_status=None,
    ha_connection=None,
    ha_health=None,
    setup_summary="",
    setup_events=None,
    last_setup_status="",
    output_dir=None,
):
    config_data = cfg.validate_and_normalize_config(config_data if config_data is not None else cfg.load_config())
    output = Path(output_dir or cfg.DATA_DIR)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_path = output / f"viper_support_bundle_{stamp}.zip"
    diag = collect_diagnostics(
        config_data,
        ha_listener_status=ha_listener_status,
        ha_connection=ha_connection,
        ha_health=ha_health,
    )
    redacted_setup_events = _redacted_setup_events(setup_events or [])
    diag["setup"] = {
        "summary": redact_text(setup_summary or ""),
        "last_status": redact_text(last_setup_status or ""),
        "event_count": len(redacted_setup_events),
    }
    included = [
        "diagnostics.txt",
        "diagnostics.json",
        "config_redacted.json",
        "config_shape.json",
        "setup/setup_summary.txt",
        "setup/setup_events.json",
        "setup/last_setup_status.txt",
    ]

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_zip_text(zf, "diagnostics.txt", diagnostics_text(diag))
        _write_zip_text(zf, "diagnostics.json", json.dumps(diag, indent=2))
        _write_zip_text(zf, "config_redacted.json", json.dumps(redact_config(config_data), indent=2))
        _write_zip_text(zf, "config_shape.json", json.dumps(config_shape(config_data), indent=2))
        _write_zip_text(zf, "setup/setup_summary.txt", setup_summary or "")
        zf.writestr("setup/setup_events.json", json.dumps(redacted_setup_events, indent=2))
        _write_zip_text(zf, "setup/last_setup_status.txt", last_setup_status or "")

        for path, arcname in [
            (cfg.DATA_DIR / "viper_full_debug.log", "logs/viper_full_debug.log"),
            (cfg.DATA_DIR / "viper_full_debug.log.1", "logs/viper_full_debug.log.1"),
            (cfg.DATA_DIR / "viper_full_debug.log.2", "logs/viper_full_debug.log.2"),
            (cfg.DATA_DIR / "viper_ha_install.log", "logs/viper_ha_install.log"),
            (cfg.DATA_DIR / HA_SNAPSHOT_LATEST, f"home_assistant/{HA_SNAPSHOT_LATEST}"),
            (cfg.API_LOG_PATH, "logs/api_usage.json"),
            (cfg.DATA_DIR / "viper_last_crash.txt", "logs/viper_last_crash.txt"),
        ]:
            if Path(path).exists():
                try:
                    _write_zip_text(zf, arcname, Path(path).read_text(encoding="utf-8", errors="ignore"))
                    included.append(arcname)
                except OSError:
                    pass

        for path, arcname in [
            (cfg.APP_DIR / "ViperVision.iss", "build/ViperVision.iss"),
            (cfg.APP_DIR / "requirements.txt", "build/requirements.txt"),
        ]:
            if Path(path).exists():
                try:
                    _write_zip_text(zf, arcname, Path(path).read_text(encoding="utf-8", errors="ignore"))
                    included.append(arcname)
                except OSError:
                    pass
        zf.writestr(
            "support_bundle_manifest.txt",
            "\n".join([
                "Viper Vision Support Bundle",
                f"Generated: {diag['generated_at']}",
                "Secrets are redacted, but review before sharing.",
                "",
                "Included files:",
                *[f"- {name}" for name in included],
            ]),
        )

    return {"ok": True, "path": str(bundle_path), "diagnostics": diag, "included": included}
