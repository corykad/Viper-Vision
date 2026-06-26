import time
from datetime import datetime

import viper_ha_client as ha_client


HEAT_PUMPS = [
    {
        "key": "office",
        "name": "Office",
        "proxy": "climate.office_heat_pump_alexa",
        "source": "climate.office_heat_pump_office_heat_pump",
    },
    {
        "key": "living_room",
        "name": "Living Room",
        "proxy": "climate.living_room_heat_pump_alexa",
        "source": "climate.living_room_heat_pump",
    },
    {
        "key": "kitchen",
        "name": "Kitchen",
        "proxy": "climate.kitchen_heat_pump_alexa",
        "source": "climate.kitchen_heat_pump",
    },
    {
        "key": "jamies_room",
        "name": "Jamie's Room",
        "proxy": "climate.jamie_s_room_heat_pump_alexa",
        "source": "climate.jamie_s_room_heat_pump",
    },
    {
        "key": "master_bedroom",
        "name": "Master Bedroom",
        "proxy": "climate.master_bedroom_heat_pump_alexa",
        "source": "climate.master_bedroom_heat_pump",
    },
]

SAFE_PROXY_MODES = {"off", "cool", "heat"}
RAW_ADVANCED_MODES = ["off", "cool", "heat", "dry", "fan_only", "heat_cool"]


def ha_request_settings(config):
    settings = ha_client.settings_from_config(config)
    return settings["host"], settings["port"], settings["token"]


def ha_headers(token):
    return ha_client.headers(token)


def get_states(config, *, timeout=8):
    return ha_client.get_states(config, timeout=timeout)


def call_service(config, domain_service, data, *, timeout=10):
    return ha_client.call_service(config, domain_service, data, timeout=timeout)


def entity_attr(entity, name, default=None):
    return ((entity or {}).get("attributes") or {}).get(name, default)


def hvac_mode_label(mode):
    labels = {
        "off": "Off",
        "cool": "Cool",
        "heat": "Heat",
        "heat_cool": "Auto",
        "auto": "Auto",
        "fan_only": "Fan Only",
        "dry": "Dry",
        "unavailable": "Unavailable",
        "unknown": "Unknown",
    }
    return labels.get(str(mode or "").lower(), str(mode or "Unknown"))


def _status_value(value, empty="unknown"):
    if value is None:
        return empty
    text = str(value).strip()
    return text if text else empty


def _temperature_text(value):
    if value is None or value == "":
        return "unknown"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _command_label(command):
    labels = {
        "set_temperature": "set temperature",
        "set_hvac_mode": "changed mode",
        "turn_off": "turned off",
        "turn_on": "turned on",
        "set_fan_mode": "set fan",
        "set_swing_mode": "set swing",
    }
    return labels.get(str(command or "").strip().lower(), str(command or "command").replace("_", " "))


def _time_text(value):
    text = str(value or "").strip()
    if not text:
        return "time unknown"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return text
    local = dt.astimezone()
    try:
        return local.strftime("%b %-d at %-I:%M %p")
    except ValueError:
        try:
            return local.strftime("%b %#d at %#I:%M %p")
        except Exception:
            return text


def _last_command_sentence(summary, *, include_name=True):
    command = _command_label(summary.get("last_command"))
    mode = summary.get("last_requested_mode")
    temp = summary.get("last_requested_temperature")
    parts = [command]
    if mode:
        parts.append(f"to {hvac_mode_label(mode)}")
    if temp is not None:
        parts.append(f"at {_temperature_text(temp)} degrees")
    prefix = f"{summary['name']}: " if include_name else ""
    return f"{prefix}{' '.join(parts)}. {_time_text(summary.get('last_command_time'))}."


def summarize_unit(unit, states):
    proxy = states.get(unit["proxy"]) or {}
    source = states.get(unit["source"]) or {}
    proxy_attrs = proxy.get("attributes") or {}
    source_attrs = source.get("attributes") or {}
    return {
        "key": unit["key"],
        "name": unit["name"],
        "proxy_entity": unit["proxy"],
        "source_entity": unit["source"],
        "state": proxy.get("state") or "missing",
        "source_state": source.get("state") or "missing",
        "available": bool(proxy) and proxy.get("state") not in {"unavailable", "unknown"},
        "target_temperature": proxy_attrs.get("temperature"),
        "current_temperature": proxy_attrs.get("current_temperature"),
        "hvac_modes": proxy_attrs.get("hvac_modes") or [],
        "source_hvac_modes": source_attrs.get("hvac_modes") or [],
        "fan_mode": source_attrs.get("fan_mode"),
        "fan_modes": source_attrs.get("fan_modes") or [],
        "swing_mode": source_attrs.get("swing_mode"),
        "swing_modes": source_attrs.get("swing_modes") or [],
        "last_command": proxy_attrs.get("last_command") or "",
        "last_command_time": proxy_attrs.get("last_command_time") or "",
        "last_requested_mode": proxy_attrs.get("last_requested_mode") or "",
        "last_requested_temperature": proxy_attrs.get("last_requested_temperature"),
    }


def format_unit_summary_line(summary):
    online = "online" if summary.get("available") else "offline"
    target = _temperature_text(summary.get("target_temperature"))
    fan = _status_value(summary.get("fan_mode"))
    swing = _status_value(summary.get("swing_mode"))
    raw = hvac_mode_label(summary.get("source_state"))
    return (
        f"{summary['name']}: {hvac_mode_label(summary.get('state'))}, "
        f"target {target}, fan {fan}, swing {swing}, raw {raw}, {online}."
    )


def format_all_status(summaries):
    summaries = list(summaries or [])
    if not summaries:
        return "No heat pumps found."
    online_count = sum(1 for item in summaries if item.get("available"))
    off_count = sum(1 for item in summaries if str(item.get("state") or "").lower() == "off")
    lines = [
        f"Heat pump status: {online_count} of {len(summaries)} online. {off_count} off.",
        "",
        "Current units:",
    ]
    lines.extend(format_unit_summary_line(item) for item in summaries)
    recent = [
        item for item in summaries
        if item.get("last_command") or item.get("last_requested_mode") or item.get("last_requested_temperature") is not None
    ]
    if recent:
        lines.extend(["", "Recent HVAC commands:"])
        for item in recent:
            lines.append(_last_command_sentence(item))
    return "\n".join(lines)


def format_unit_status(summary):
    online = "online" if summary.get("available") else "offline or unavailable"
    lines = [
        f"{summary['name']} is {online}.",
        f"Mode: {hvac_mode_label(summary.get('state'))}. Target: {_temperature_text(summary.get('target_temperature'))} degrees.",
        f"Fan: {_status_value(summary.get('fan_mode'))}. Swing: {_status_value(summary.get('swing_mode'))}.",
        f"Raw source mode: {hvac_mode_label(summary.get('source_state'))}.",
    ]
    if summary.get("last_command"):
        lines.append(f"Last command: {_last_command_sentence(summary, include_name=False)}")
    lines.extend(
        [
            f"Alexa proxy: {summary['proxy_entity']}",
            f"IR source: {summary['source_entity']}",
        ]
    )
    return "\n".join(lines)


def set_mode(config, unit, mode):
    mode = str(mode or "").lower()
    if mode == "off":
        return call_service(config, "climate/turn_off", {"entity_id": unit["proxy"]})
    if mode not in SAFE_PROXY_MODES:
        return call_service(config, "climate/set_hvac_mode", {"entity_id": unit["source"], "hvac_mode": mode})
    return call_service(config, "climate/set_hvac_mode", {"entity_id": unit["proxy"], "hvac_mode": mode})


def set_temperature(config, unit, temperature):
    return call_service(
        config,
        "climate/set_temperature",
        {"entity_id": unit["proxy"], "temperature": float(temperature)},
    )


def set_temperature_and_mode(config, unit, temperature, mode):
    mode = str(mode or "cool").lower()
    entity_id = unit["proxy"] if mode in SAFE_PROXY_MODES else unit["source"]
    return call_service(
        config,
        "climate/set_temperature",
        {"entity_id": entity_id, "temperature": float(temperature), "hvac_mode": mode},
    )


def set_fan_mode(config, unit, fan_mode):
    return call_service(
        config,
        "climate/set_fan_mode",
        {"entity_id": unit["source"], "fan_mode": fan_mode},
    )


def set_swing_mode(config, unit, swing_mode):
    return call_service(
        config,
        "climate/set_swing_mode",
        {"entity_id": unit["source"], "swing_mode": swing_mode},
    )


def apply_all(config, mode=None, temperature=None):
    results = []
    for unit in HEAT_PUMPS:
        try:
            if temperature is not None and mode and mode != "off":
                set_temperature_and_mode(config, unit, temperature, mode)
            elif temperature is not None:
                set_temperature(config, unit, temperature)
            elif mode:
                set_mode(config, unit, mode)
            results.append({"name": unit["name"], "ok": True})
        except Exception as exc:
            results.append({"name": unit["name"], "ok": False, "message": str(exc)})
        time.sleep(0.2)
    return results
