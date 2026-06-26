import asyncio
import json
import logging
import time
from copy import deepcopy
from datetime import datetime, timezone

import requests
import websockets

import viper_config as cfg


DEFAULT_SMARTTHINGS_STALE_SECONDS = 20 * 60
DEFAULT_SMARTTHINGS_RELOAD_COOLDOWN_SECONDS = 6 * 60 * 60
HEALTH_JOURNAL_FILE = "viper_health_events.jsonl"
FRIDGE_DOOR_ENTITY = "binary_sensor.refrigerator_fridge_door"
FREEZER_DOOR_ENTITY = "binary_sensor.refrigerator_freezer_door"
REFRIGERATOR_SUPPORT_ENTITIES = (
    "sensor.refrigerator_power",
    "sensor.refrigerator_power_energy",
    "sensor.refrigerator_energy",
    "sensor.refrigerator_water_filter_usage",
    "sensor.refrigerator_fridge_temperature",
    "sensor.refrigerator_freezer_temperature",
    "switch.refrigerator_cubed_ice",
    "switch.refrigerator_power_cool",
    "switch.refrigerator_power_freeze",
)


def parse_ha_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def newest_ha_timestamp(entity):
    if not isinstance(entity, dict):
        return None
    for key in ("last_reported", "last_updated", "last_changed"):
        parsed = parse_ha_datetime(entity.get(key))
        if parsed:
            return parsed
    return None


def _plain_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes"
    hours = minutes // 60
    rem = minutes % 60
    if rem:
        return f"{hours} hours {rem} minutes"
    return f"{hours} hours"


def refrigerator_event_stream_health(states, *, now=None, stale_seconds=DEFAULT_SMARTTHINGS_STALE_SECONDS):
    """Explain whether the fridge door event stream looks healthy.

    Door sensors can stay closed for hours, so an old door timestamp alone is not
    enough. We flag trouble when the door entities are much older than other
    refrigerator SmartThings entities. That means HA/SmartThings is alive, but
    the exact door event stream Viper needs may be stale.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    by_id = {
        str(item.get("entity_id") or ""): item
        for item in (states or [])
        if isinstance(item, dict) and item.get("entity_id")
    }
    door_entities = [by_id.get(FRIDGE_DOOR_ENTITY), by_id.get(FREEZER_DOOR_ENTITY)]
    present_doors = [item for item in door_entities if item]
    if len(present_doors) < 2:
        return {
            "checked": True,
            "ok": False,
            "status": "missing_door_entity",
            "message": "Home Assistant is missing one of the refrigerator door entities.",
            "details": {},
        }

    support_entities = [by_id.get(entity_id) for entity_id in REFRIGERATOR_SUPPORT_ENTITIES if by_id.get(entity_id)]
    door_times = [newest_ha_timestamp(item) for item in present_doors]
    support_times = [newest_ha_timestamp(item) for item in support_entities]
    door_times = [item for item in door_times if item]
    support_times = [item for item in support_times if item]
    newest_door = max(door_times) if door_times else None
    newest_support = max(support_times) if support_times else None
    door_age = (now - newest_door).total_seconds() if newest_door else None
    support_age = (now - newest_support).total_seconds() if newest_support else None
    support_ahead = (newest_support - newest_door).total_seconds() if newest_support and newest_door else None

    details = {
        "newest_door_at": newest_door.isoformat() if newest_door else "",
        "newest_support_at": newest_support.isoformat() if newest_support else "",
        "door_age_seconds": door_age,
        "support_age_seconds": support_age,
        "support_ahead_seconds": support_ahead,
        "stale_seconds": stale_seconds,
        "support_entities_seen": [item.get("entity_id") for item in support_entities],
    }
    if newest_support and newest_door and support_ahead is not None and support_ahead >= stale_seconds:
        return {
            "checked": True,
            "ok": False,
            "status": "door_stream_stale",
            "message": (
                "Home Assistant is still receiving refrigerator updates, but the fridge/freezer door event stream "
                f"has been quiet for {_plain_age(support_ahead)} longer than the rest of the refrigerator. "
                "Viper should reload the SmartThings entry."
            ),
            "details": details,
        }
    return {
        "checked": True,
        "ok": True,
        "status": "ok",
        "message": (
            "Refrigerator door entities are present. Last door update: "
            f"{_plain_age(door_age)} ago."
        ),
        "details": details,
    }


async def _ha_ws_call(ws, message_id, message_type, **payload):
    await ws.send(json.dumps({"id": message_id, "type": message_type, **payload}))
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if message.get("id") == message_id:
            return message


async def find_config_entry_for_entity(ha_ip, ha_port, token, entity_id):
    if not ha_ip or not token or not entity_id:
        return {"ok": False, "message": "Home Assistant host, token, or entity is missing."}
    url = f"ws://{ha_ip}:{ha_port or '8123'}/api/websocket"
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
            auth_required = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if auth_required.get("type") != "auth_required":
                return {"ok": False, "message": "Home Assistant did not request WebSocket authentication."}
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth_result = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if auth_result.get("type") != "auth_ok":
                return {"ok": False, "message": auth_result.get("message") or "Home Assistant WebSocket auth failed."}
            result = await _ha_ws_call(ws, 1, "config/entity_registry/list")
    except Exception as e:
        logging.warning("[HEALTH] Could not read HA entity registry: %s", e)
        return {"ok": False, "message": str(e)}

    if not result.get("success"):
        error = result.get("error") or {}
        return {"ok": False, "message": error.get("message") or "HA entity registry command failed."}
    for item in result.get("result") or []:
        if item.get("entity_id") == entity_id:
            entry_id = item.get("config_entry_id") or ""
            return {
                "ok": bool(entry_id),
                "entity_id": entity_id,
                "config_entry_id": entry_id,
                "platform": item.get("platform") or "",
                "device_id": item.get("device_id") or "",
                "message": "Config entry found." if entry_id else "Entity has no config entry id.",
            }
    return {"ok": False, "entity_id": entity_id, "message": "Entity was not found in HA entity registry."}


def reload_config_entry(ha_ip, ha_port, token, entry_id, *, timeout=20):
    if not ha_ip or not token or not entry_id:
        return {"ok": False, "message": "Home Assistant host, token, or config entry id is missing."}
    url = f"http://{ha_ip}:{ha_port or '8123'}/api/services/homeassistant/reload_config_entry"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"entry_id": entry_id},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        logging.warning("[HEALTH] SmartThings reload request failed: %s", e)
        return {"ok": False, "message": str(e), "url": url}
    ok = 200 <= response.status_code < 300
    return {
        "ok": ok,
        "status_code": response.status_code,
        "message": "Home Assistant reloaded the SmartThings entry." if ok else f"Home Assistant returned HTTP {response.status_code}.",
        "body": response.text[:500],
        "url": url,
    }


def _journal_path(path=None):
    return path or (cfg.DATA_DIR / HEALTH_JOURNAL_FILE)


def record_health_event(event_type, status, message, *, details=None, path=None, now=None):
    entry = {
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "event_type": str(event_type or "unknown"),
        "status": str(status or "unknown"),
        "message": str(message or ""),
        "details": details if isinstance(details, dict) else {},
    }
    output = _journal_path(path)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        logging.debug("[HEALTH] Could not write health journal event.", exc_info=True)
    return entry


def recent_health_events(*, limit=20, path=None):
    journal = _journal_path(path)
    if not journal.exists():
        return []
    try:
        lines = journal.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-max(limit * 3, limit):]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events[-limit:]


def count_recent_health_events(event_type, *, within_seconds=24 * 60 * 60, path=None, now=None):
    now = now or datetime.now(timezone.utc)
    count = 0
    for item in recent_health_events(limit=500, path=path):
        if item.get("event_type") != event_type:
            continue
        parsed = parse_ha_datetime(item.get("timestamp"))
        if parsed and (now - parsed).total_seconds() <= within_seconds:
            count += 1
    return count


def _speaker_route_count(config_data, route):
    speakers = config_data.get("speakers") if isinstance(config_data, dict) else {}
    if not isinstance(speakers, dict):
        return 0
    count = 0
    for data in speakers.values():
        if isinstance(data, dict) and data.get("enabled") and data.get(route):
            count += 1
    return count


def _enabled_speaker_count(config_data):
    speakers = config_data.get("speakers") if isinstance(config_data, dict) else {}
    if not isinstance(speakers, dict):
        return 0
    return sum(1 for data in speakers.values() if isinstance(data, dict) and data.get("enabled"))


def critical_workflow_status(config_data=None, *, diag=None):
    config_data = config_data if isinstance(config_data, dict) else {}
    diag = diag if isinstance(diag, dict) else {}
    listener = diag.get("ha_listener") if isinstance(diag.get("ha_listener"), dict) else {}
    ha_conn = diag.get("ha_connection") if isinstance(diag.get("ha_connection"), dict) else {}
    fridge = diag.get("fridge_sensor_health") if isinstance(diag.get("fridge_sensor_health"), dict) else {}
    ffmpeg = diag.get("ffmpeg") if isinstance(diag.get("ffmpeg"), dict) else {}
    routes = {
        "doorbell": _speaker_route_count(config_data, "doorbell"),
        "utilities": _speaker_route_count(config_data, "utilities"),
        "fridge": _speaker_route_count(config_data, "fridge"),
    }
    enabled_speakers = _enabled_speaker_count(config_data)
    items = []

    def add(name, status, message):
        items.append({"name": name, "status": status, "message": message})

    if listener.get("connected"):
        add("HA listener", "OK", f"Connected to {listener.get('last_host') or 'Home Assistant'}.")
    else:
        add("HA listener", "BROKEN", listener.get("last_error") or "Listener is not connected.")

    critical = listener.get("critical_health_status") or "unknown"
    if critical == "ok":
        add("SmartThings fridge stream", "OK", listener.get("critical_health_message") or "Door event stream looks healthy.")
    elif critical in {"door_stream_stale", "ha_read_failed", "missing_door_entity"}:
        add("SmartThings fridge stream", "SUSPICIOUS", listener.get("critical_health_message") or critical)
    elif critical == "disabled":
        add("SmartThings fridge stream", "SUSPICIOUS", "Automatic SmartThings recovery is disabled.")
    else:
        add("SmartThings fridge stream", "SUSPICIOUS", "Waiting for the first watchdog check.")

    if fridge.get("checked") and not fridge.get("ok", True):
        add("Fridge door sensors", "SUSPICIOUS", fridge.get("message") or "Door sensor diagnostics found an issue.")
    elif fridge.get("checked"):
        add("Fridge door sensors", "OK", fridge.get("message") or "Door entities are present.")
    else:
        add("Fridge door sensors", "SUSPICIOUS", "Not checked yet.")

    if routes["fridge"]:
        add("Fridge chime route", "OK", f"{routes['fridge']} enabled speaker route(s).")
    elif enabled_speakers == 0:
        add("Fridge chime route", "SUSPICIOUS", "All speakers are disabled, so fridge/freezer chimes are intentionally quiet until a speaker is enabled.")
    else:
        add("Fridge chime route", "BROKEN", "No enabled speaker is routed for fridge/freezer alerts.")

    if ha_conn.get("checked"):
        add("HA API", "OK" if ha_conn.get("ok") else "BROKEN", ha_conn.get("message") or ha_conn.get("error") or "Checked.")
    else:
        add("HA API", "SUSPICIOUS", "Not checked in this quick summary.")

    if ffmpeg.get("available"):
        add("Doorbell camera capture", "OK", "FFmpeg is available.")
    else:
        add("Doorbell camera capture", "BROKEN", "FFmpeg is missing, so camera frame capture may fail.")

    api = cfg.get_api_settings(config_data, include_env=True)
    add(
        "Pushover",
        "OK" if api.get("pushover_enabled") and api.get("pushover_user_key") and api.get("pushover_api_token") else "SUSPICIOUS",
        "Configured." if api.get("pushover_enabled") and api.get("pushover_user_key") and api.get("pushover_api_token") else "Not fully configured or disabled.",
    )
    add("Gemini", "OK" if api.get("gemini_api_key") else "SUSPICIOUS", "API key configured." if api.get("gemini_api_key") else "API key missing.")

    priority = {"BROKEN": 2, "SUSPICIOUS": 1, "OK": 0}
    worst = max((priority.get(item["status"], 1) for item in items), default=1)
    overall = "BROKEN" if worst >= 2 else ("SUSPICIOUS" if worst == 1 else "OK")
    return {"overall": overall, "items": items, "routes": routes, "enabled_speakers": enabled_speakers}


def critical_workflow_lines(summary):
    summary = summary if isinstance(summary, dict) else {}
    lines = [f"Critical workflows: {summary.get('overall') or 'UNKNOWN'}"]
    for item in summary.get("items") or []:
        lines.append(f"{item.get('status', 'UNKNOWN')}: {item.get('name')}: {item.get('message') or ''}")
    return lines


def beginner_health_lines(diag):
    diag = diag if isinstance(diag, dict) else {}
    health = diag.get("health") if isinstance(diag.get("health"), dict) else {}
    listener = diag.get("ha_listener") if isinstance(diag.get("ha_listener"), dict) else {}
    fridge = diag.get("fridge_sensor_health") if isinstance(diag.get("fridge_sensor_health"), dict) else {}
    lines = [
        f"Overall: {(health.get('status') or 'unknown').upper()}",
        f"Home Assistant listener: {'connected' if listener.get('connected') else 'not connected'}",
        f"Last HA event Viper saw: {listener.get('last_event_entity') or 'none yet'}",
        f"Refrigerator door health: {fridge.get('status') or 'not checked'}",
    ]
    critical_status = listener.get("critical_health_status")
    if critical_status:
        lines.append(f"Critical watchdog: {critical_status}")
        lines.append(f"What it means: {listener.get('critical_health_message') or 'No detail.'}")
    reload_at = listener.get("last_smartthings_reload_at")
    if reload_at:
        when = datetime.fromtimestamp(float(reload_at)).strftime("%Y-%m-%d %I:%M:%S %p")
        lines.append(f"Last automatic SmartThings reload: {when}")
        lines.append(f"Reload result: {listener.get('last_smartthings_reload_result') or 'unknown'}")
    repeat_count = count_recent_health_events("smartthings_reload")
    if repeat_count >= 3:
        lines.append(f"Repeated recovery warning: SmartThings was reloaded {repeat_count} times in the last 24 hours.")
    active = health.get("active_issues") or []
    if active:
        lines.append("Needs attention:")
        lines.extend(str(item) for item in active[:5])
    else:
        lines.append("Needs attention: none detected.")
    return lines


def clone(value):
    return deepcopy(value)
