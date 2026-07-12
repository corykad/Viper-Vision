"""Automated pre-release audit for Viper Vision real event routing.

This script is intentionally conservative by default. It does not play audio,
move vacuums, call Gemini, or fire Home Assistant events unless an explicit
option is added later. The goal is to catch the release blockers that unit
tests miss: bad saved routes, missing trigger entities, disabled speakers,
missing chimes, and event paths that would never reach a network speaker.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import viper_audio as audio
import viper_config as cfg
import viper_discovery as discovery
import viper_ha_listener as ha_listener


REPORT_DIR = Path("release_check_reports")
REQUIRED_PACKAGE_FILES = [
    "main.pyw",
    "viper_ha_client.py",
    "viper_ha_vm_delegates.py",
    "viper_hvac.py",
    "viper_remote_api.py",
    "viper_remote_web.py",
    "viper_runtime.py",
    "viper_system_health.py",
    "viper_ui_common.py",
    "viper_ui_dashboard.py",
    "viper_ui_device_tools.py",
    "viper_ui_doorbell.py",
    "viper_ui_hvac.py",
    "viper_ui_lifecycle.py",
    "viper_ui_prompts.py",
    "viper_ui_speakers.py",
    "viper_ui_setup_status.py",
    "viper_ui_setup_windows.py",
    "viper_ui_tts.py",
    "templates/remote.html",
    "help/index.html",
]


class Audit:
    def __init__(self, *, live_ha: bool = False, rtsp: bool = False, emit: bool = True):
        self.live_ha = live_ha
        self.rtsp = rtsp
        self.emit = emit
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.lines: list[str] = []
        self.data: dict = {
            "ok": False,
            "failures": self.failures,
            "warnings": self.warnings,
            "checks": {},
        }

    def line(self, text: str = ""):
        if self.emit:
            print(text)
        self.lines.append(text)

    def pass_(self, text: str):
        self.line(f"PASS: {text}")

    def warn(self, text: str):
        self.warnings.append(text)
        self.line(f"WARN: {text}")

    def note(self, text: str):
        self.line(f"NOTE: {text}")

    def fail(self, text: str):
        self.failures.append(text)
        self.line(f"FAIL: {text}")


def _route_targets(config: dict, channel: str, *, manual: bool = False):
    ha, sonos, alexa, category, quiet = audio._collect_targets_for_context(
        config,
        {"channel": channel, "manual": manual},
    )
    return {
        "ha": ha,
        "sonos": sonos,
        "alexa": alexa,
        "category": category,
        "quiet_hours_active": quiet,
        "count": len(ha) + len(sonos) + len(alexa),
    }


def _short_targets(targets: dict) -> str:
    return (
        f"ha={len(targets['ha'])}, sonos={len(targets['sonos'])}, "
        f"alexa={len(targets['alexa'])}"
    )


def _entity_ids_from_states(states: list[dict]) -> set[str]:
    return {str(item.get("entity_id") or "") for item in states if isinstance(item, dict)}


def _check_speakers(audit: Audit, config: dict):
    settings = cfg.get_speaker_settings(config, include_env=True)
    routes = {}
    for channel, manual in (
        ("doorbell", False),
        ("utilities", False),
        ("fridge_open", False),
        ("freezer_open", False),
        ("broadcast", True),
    ):
        routes[channel] = _route_targets(config, channel, manual=manual)

    audit.data["checks"]["speaker_routes"] = {
        "speaker_count": settings["speaker_count"],
        "enabled_count": settings["enabled_count"],
        "routes": routes,
        "enable_alexa": bool(config.get("enable_alexa", False)),
    }

    audit.line("")
    audit.line("Speaker And Audio Routes")
    audit.line("------------------------")
    audit.line(f"Saved speakers: {settings['speaker_count']}")
    audit.line(f"Enabled speakers: {settings['enabled_count']}")
    audit.line(f"Alexa globally enabled: {bool(config.get('enable_alexa', False))}")

    if settings["speaker_count"] == 0:
        audit.fail("No speakers are saved. Events cannot play on network speakers.")
    elif settings["enabled_count"] == 0:
        audit.fail("Speakers are saved, but all are disabled.")

    for channel, targets in routes.items():
        audit.line(f"{channel}: {_short_targets(targets)}")
        if channel in {"doorbell", "utilities", "fridge_open", "freezer_open", "broadcast"} and targets["count"] == 0:
            audit.fail(f"No network playback targets for {channel}.")

    disabled = [
        name
        for name, speaker in settings["speakers"].items()
        if isinstance(speaker, dict) and not speaker.get("enabled", True)
    ]
    if disabled:
        audit.note(f"Disabled saved speakers will not receive events: {', '.join(disabled)}")


def _check_tts(audit: Audit, config: dict):
    audio_settings = cfg.get_audio_settings(config, include_env=True)
    effective = audio_settings.get("effective_tts_alerts", {})
    api = cfg.get_api_settings(config, include_env=True)
    audit.data["checks"]["tts"] = {
        "effective_tts_alerts": effective,
        "gemini_api_key_configured": bool(api.get("gemini_api_key")),
    }

    audit.line("")
    audit.line("TTS Profiles")
    audit.line("------------")
    for category in ("doorbell", "utilities", "manual"):
        profile = effective.get(category, {})
        engine = profile.get("engine") or audio_settings.get("tts_engine") or "unknown"
        voice = profile.get("gemini_voice") or profile.get("edge_voice") or ""
        speed = profile.get("speed") or "normal"
        audit.line(f"{category}: engine={engine}, voice={voice}, speed={speed}")
        if str(engine).lower() == "gemini" and not api.get("gemini_api_key"):
            audit.fail(f"{category} uses Gemini TTS but no Gemini API key is available.")


def _check_doorbells(audit: Audit, config: dict, live_entities: set[str] | None):
    triggers = ha_listener.normalize_doorbell_triggers(config)
    audit.data["checks"]["doorbells"] = {}
    audit.line("")
    audit.line("Doorbell Event Routing")
    audit.line("----------------------")
    for side, trigger in triggers.items():
        if not trigger.get("enabled", True):
            audit.warn(f"{side} doorbell is disabled.")
            continue
        entity_id = trigger.get("trigger_entity_id") or ""
        rtsp_url = trigger.get("rtsp_url") or ""
        audit.data["checks"]["doorbells"][side] = dict(trigger)
        audit.line(f"{side}: trigger={entity_id or 'missing'}, rtsp={'set' if rtsp_url else 'missing'}")
        if not entity_id:
            audit.fail(f"{side} doorbell has no Home Assistant trigger entity.")
        if not rtsp_url:
            audit.fail(f"{side} doorbell has no RTSP URL.")
        if live_entities is not None and entity_id and entity_id not in live_entities:
            audit.fail(f"{side} trigger entity does not exist in Home Assistant: {entity_id}")
        if entity_id:
            actions = ha_listener.route_state_change(
                config,
                entity_id,
                {"state": "off"},
                {"state": (trigger.get("active_states") or ["on"])[0]},
            )
            if any(action.get("type") == "doorbell" and action.get("side") == side for action in actions):
                audit.pass_(f"{side} synthetic state change routes to doorbell action.")
            else:
                audit.fail(f"{side} synthetic state change did not route to a doorbell action.")


def _check_fridge(audit: Audit, config: dict, live_entities: set[str] | None):
    audit.line("")
    audit.line("Fridge And Freezer Events")
    audit.line("-------------------------")
    expected = {
        "binary_sensor.refrigerator_fridge_door": ("fridge_open", "The refrigerator door is open."),
        "binary_sensor.refrigerator_freezer_door": ("freezer_open", "The freezer door is open."),
    }
    broadcast = cfg.get_fridge_settings(config, include_env=True)
    audit.data["checks"]["fridge"] = {"broadcast_channels": broadcast}
    for entity_id, (channel, _message) in expected.items():
        if live_entities is not None and entity_id not in live_entities:
            audit.warn(f"Default fridge/freezer entity not found in Home Assistant: {entity_id}")
        actions = ha_listener.route_state_change(config, entity_id, {"state": "off"}, {"state": "on"})
        if any(action.get("type") == "broadcast" and action.get("channel") == channel for action in actions):
            audit.pass_(f"{entity_id} routes to {channel}.")
        else:
            audit.fail(f"{entity_id} did not route to {channel}.")

    for channel in ("fridge_open", "fridge_closed", "freezer_open", "freezer_closed"):
        settings = (broadcast or {}).get(channel, {})
        mode = settings.get("mode", "speak")
        chime = settings.get("chime") or ""
        audit.line(f"{channel}: mode={mode}, chime={chime or 'default'}")
        if mode == "chime" and chime:
            path = cfg.CHIMES_DIR / chime
            if not path.exists():
                audit.fail(f"{channel} chime file is missing: {path}")


def _check_cinderella(audit: Audit, config: dict, live_entities: set[str] | None):
    audit.line("")
    audit.line("Roborock / Cinderella Events")
    audit.line("----------------------------")
    entities = [
        "sensor.cinderella_status",
        "sensor.cinderella_vacuum_error",
        "sensor.cinderella_dock_dock_error",
        "binary_sensor.cinderella_dock_mop_drying",
    ]
    for entity_id in entities:
        if live_entities is not None and entity_id not in live_entities:
            audit.warn(f"Default Cinderella entity not found in Home Assistant: {entity_id}")
    samples = [
        ("sensor.cinderella_status", "idle", "cleaning", "departure"),
        ("sensor.cinderella_status", "cleaning", "returning_home", "returning"),
        ("sensor.cinderella_vacuum_error", "none", "water_carriage_drop", "error"),
        ("sensor.cinderella_dock_dock_error", "none", "duct_blockage", "error"),
        ("binary_sensor.cinderella_dock_mop_drying", "off", "on", "drying"),
    ]
    for entity_id, old_state, new_state, expected_event in samples:
        actions = ha_listener.route_state_change(config, entity_id, {"state": old_state}, {"state": new_state})
        if any(action.get("type") == "cinderella" and action.get("event") == expected_event for action in actions):
            audit.pass_(f"{entity_id} {old_state}->{new_state} routes to Cinderella {expected_event}.")
        else:
            audit.fail(f"{entity_id} {old_state}->{new_state} did not route to Cinderella {expected_event}.")


def _check_packaging(audit: Audit):
    audit.line("")
    audit.line("Package Readiness")
    audit.line("-----------------")
    root = Path(__file__).resolve().parent
    missing = []
    for name in REQUIRED_PACKAGE_FILES:
        path = root / name
        if path.exists():
            audit.pass_(f"Required package file exists: {name}")
        else:
            missing.append(name)
            audit.fail(f"Required package file is missing: {name}")
    audit.data["checks"]["package_files"] = {"required": REQUIRED_PACKAGE_FILES, "missing": missing}


def _check_live_ha(audit: Audit, config: dict) -> set[str] | None:
    if not audit.live_ha:
        audit.warn("Live Home Assistant checks skipped. Add --live-ha to validate entities against Home Assistant.")
        return None
    ha = cfg.get_ha_settings(config, include_env=True)
    host = ha.get("ha_ip")
    port = ha.get("ha_port") or "8123"
    token = ha.get("ha_token")
    if not host or not token:
        audit.fail("Live HA check requested, but Home Assistant host or token is missing.")
        return None
    audit.line("")
    audit.line("Live Home Assistant")
    audit.line("-------------------")
    connection = discovery.test_ha_connection(token=token, ha_ip=host, ha_port=port, timeout=8)
    audit.data["checks"]["ha_connection"] = {k: v for k, v in connection.items() if k not in {"ha_token", "token"}}
    if connection.get("ok"):
        audit.pass_(f"Home Assistant token can read states. Entities: {connection.get('entity_count')}")
    else:
        audit.fail(f"Home Assistant connection failed: {connection.get('message') or connection.get('error')}")
        return None
    states_result = discovery.get_ha_states(token=token, ha_ip=host, ha_port=port, timeout=10)
    if not states_result.get("ok"):
        audit.fail(f"Could not fetch Home Assistant states: {states_result.get('message') or states_result.get('error')}")
        return None
    states = states_result.get("states") or []
    audit.data["checks"]["ha_entities"] = {"entity_count": len(states)}
    return _entity_ids_from_states(states)


def run_audit(*, live_ha: bool = False, rtsp: bool = False) -> int:
    audit = Audit(live_ha=live_ha, rtsp=rtsp)
    config = cfg.load_config()
    audit.line("Viper Vision Automated Event Audit")
    audit.line("==================================")
    audit.line(f"Data folder: {cfg.DATA_DIR}")
    audit.line(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    live_entities = _check_live_ha(audit, config)
    _check_speakers(audit, config)
    _check_tts(audit, config)
    _check_doorbells(audit, config, live_entities)
    _check_fridge(audit, config, live_entities)
    _check_cinderella(audit, config, live_entities)
    _check_packaging(audit)

    if rtsp:
        audit.warn("RTSP frame checks are not implemented in this safe audit yet. Use Viper's Test Camera buttons or Test Everything for frame capture.")

    audit.line("")
    audit.line("Summary")
    audit.line("-------")
    audit.line(f"Failures: {len(audit.failures)}")
    audit.line(f"Warnings: {len(audit.warnings)}")
    audit.data["ok"] = not audit.failures

    REPORT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    text_path = REPORT_DIR / f"viper_event_audit_{stamp}.txt"
    json_path = REPORT_DIR / f"viper_event_audit_{stamp}.json"
    text_path.write_text("\n".join(audit.lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(audit.data, indent=2), encoding="utf-8")
    audit.line(f"Report saved: {text_path}")
    audit.line(f"JSON saved: {json_path}")
    return 0 if audit.data["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Viper Vision's safe automated event routing audit.")
    parser.add_argument("--live-ha", action="store_true", help="Validate configured entities against live Home Assistant.")
    parser.add_argument("--rtsp", action="store_true", help="Reserved for future RTSP frame checks.")
    args = parser.parse_args()
    return run_audit(live_ha=args.live_ha, rtsp=args.rtsp)


if __name__ == "__main__":
    raise SystemExit(main())
