"""Plain-English health summaries for the Viper dashboard."""

from __future__ import annotations

import viper_config as cfg
import viper_ha_listener as ha_listener
import viper_runtime


def _yes_no(value):
    return "yes" if value else "no"


def _count_enabled_speakers(config):
    speakers = cfg.get_speaker_settings(config, include_env=True).get("speakers") or {}
    return sum(1 for speaker in speakers.values() if isinstance(speaker, dict) and speaker.get("enabled", True))


def _doorbell_line(side, trigger):
    ids = trigger.get("trigger_entity_ids") or []
    if not ids and trigger.get("trigger_entity_id"):
        ids = [trigger.get("trigger_entity_id")]
    rtsp = "set" if trigger.get("rtsp_url") else "missing"
    enabled = "enabled" if trigger.get("enabled", True) else "disabled"
    return f"{side.title()} door: {enabled}. Triggers: {len(ids)}. Camera stream: {rtsp}."


def _hvac_line(hvac_last_states):
    states = list((hvac_last_states or {}).values())
    if not states:
        return "Heat pumps: status not refreshed yet."
    online = sum(1 for item in states if item.get("available"))
    return f"Heat pumps: {online} of {len(states)} online."


def build_system_health_summary(config, *, listener_status=None, hvac_last_states=None, startup_lines=None, recent_events=None):
    config = config or {}
    listener_status = listener_status or {}
    ha = cfg.get_ha_settings(config, include_env=True)
    triggers = ha_listener.normalize_doorbell_triggers(config)
    speakers_enabled = _count_enabled_speakers(config)
    lines = [
        "System Health",
        "",
        f"Home Assistant host: {ha.get('ha_ip') or 'not configured'}:{ha.get('ha_port') or '8123'}.",
        f"Home Assistant token available: {_yes_no(bool(ha.get('ha_token')))}.",
        f"HA listener enabled: {_yes_no(config.get('ha_listener_enabled', True))}.",
        f"HA listener connected: {_yes_no(listener_status.get('connected'))}.",
        f"HA listener last error: {listener_status.get('last_error') or 'none'}.",
        f"Last HA event: {listener_status.get('last_event_entity') or 'none'}.",
        f"Last routed action: {listener_status.get('last_routed_action') or 'none'}.",
        "",
        "Core workflows:",
        _doorbell_line("front", triggers.get("front", {})),
        _doorbell_line("back", triggers.get("back", {})),
        f"Speaker routes: {speakers_enabled} enabled speaker target(s).",
        _hvac_line(hvac_last_states),
    ]

    if startup_lines is None:
        startup_lines = viper_runtime.startup_summary_lines(limit=8)
    if recent_events is None:
        recent_events = viper_runtime.format_recent_events(limit=8)

    lines.extend(["", *startup_lines, "", *recent_events])
    return "\n".join(lines)


def short_ha_status(listener_status):
    listener_status = listener_status or {}
    if not listener_status.get("running"):
        return "Home Assistant listener stopped."
    if listener_status.get("connected"):
        host = listener_status.get("last_host") or "Home Assistant"
        return f"Home Assistant listener connected to {host}."
    error = listener_status.get("last_error") or "connecting"
    return f"Home Assistant listener not connected: {error}"
