import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import requests
import websockets

import viper_config as cfg
import viper_discovery as discovery
import viper_ha_addons as ha_addons
import viper_hvac as hvac


MATTER_PACKAGE_FILENAME = "viper_matter_controls.yaml"
SAMBA_ADDON_SLUG = "core_samba"
MATTERBRIDGE_ADDON_SLUG = "246dd49f_matterbridge"
MATTERBRIDGE_REPOSITORY_URL = "https://github.com/Luligu/matterbridge-home-assistant-addon"
MATTERBRIDGE_HASS_PLUGIN = "matterbridge-hass"
MATTERBRIDGE_PORT = 8283


def base_viper_switches():
    return [
        {
            "entity_id": "switch.viper_armed",
            "unique_id": "viper_armed",
            "friendly_name": "Viper Armed",
            "rest_command": "viper_set_armed",
            "state_template": "{{ state_attr('sensor.viper_control_state', 'armed') | bool(false) }}",
            "on_action": "rest_command.viper_set_armed",
            "off_action": "rest_command.viper_set_armed",
            "on_payload": {"state": True},
            "off_payload": {"state": False},
        },
        {
            "entity_id": "switch.viper_global_mute",
            "unique_id": "viper_global_mute",
            "friendly_name": "Viper Global Mute",
            "rest_command": "viper_set_global_mute",
            "state_template": "{{ state_attr('sensor.viper_control_state', 'global_mute') | bool(false) }}",
            "on_action": "rest_command.viper_set_global_mute",
            "off_action": "rest_command.viper_set_global_mute",
            "on_payload": {"state": True},
            "off_payload": {"state": False},
        },
        {
            "entity_id": "switch.viper_ice_maker",
            "unique_id": "viper_ice_maker",
            "friendly_name": "Viper Ice Maker",
            "rest_command": "viper_set_ice_maker",
            "state_template": "{{ (state_attr('sensor.viper_control_state', 'ice_maker') or {}).get('enabled', false) | bool(false) }}",
            "on_action": "rest_command.viper_set_ice_maker",
            "off_action": "rest_command.viper_set_ice_maker",
            "on_payload": {"state": True},
            "off_payload": {"state": False},
        },
    ]


def speaker_switches(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    speakers = data.get("speakers") or {}
    controls = []
    used_ids = {"viper_armed", "viper_global_mute", "viper_ice_maker"}
    for name in sorted(speakers.keys(), key=lambda value: str(value).lower()):
        slug = _unique_slug(f"viper_{_speaker_slug_base(name)}_speaker", used_ids)
        quoted_name = quote(str(name), safe="")
        friendly_name = _speaker_friendly_name(name)
        controls.append(
            {
                "speaker_name": str(name),
                "entity_id": f"switch.{_ha_entity_slug(friendly_name)}",
                "unique_id": f"{slug}_enabled",
                "friendly_name": friendly_name,
                "rest_command": f"{slug}_enabled",
                "state_template": "{{ ((state_attr('sensor.viper_control_state', 'speakers') or {}).get('"
                + _jinja_single_quote(name)
                + "', {}).get('enabled', false)) | bool(false) }}",
                "endpoint_path": f"/api/control/speakers/{quoted_name}/enabled",
                "on_payload": {"state": True},
                "off_payload": {"state": False},
            }
        )
    return controls


def matter_switches(config_data=None):
    return base_viper_switches() + speaker_switches(config_data)


def matter_fan_entities(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    seen = set()
    entities = []
    for entity_id in data.get("matter_fan_entities") or []:
        entity_id = str(entity_id or "").strip().lower()
        if entity_id.startswith("fan.") and entity_id not in seen:
            seen.add(entity_id)
            entities.append(entity_id)
    return entities


def matter_hvac_entities(config_data=None):
    return [unit["proxy"] for unit in hvac.HEAT_PUMPS if unit.get("proxy")]


def matter_entity_ids(config_data=None):
    return (
        [control["entity_id"] for control in matter_switches(config_data)]
        + matter_fan_entities(config_data)
        + matter_hvac_entities(config_data)
    )


def matter_entity_domains(config_data=None):
    domains = {
        str(entity_id).split(".", 1)[0]
        for entity_id in matter_entity_ids(config_data)
        if "." in str(entity_id)
    }
    return sorted(domains or {"switch"})


def generate_matter_controls_package(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    viper_host = str(data.get("viper_host") or cfg.PC_IP).strip()
    viper_port = int(data.get("flask_port") or cfg.FLASK_PORT)
    base_url = f"http://{viper_host}:{viper_port}"
    speaker_controls = speaker_switches(data)

    lines = [
        "# Viper Vision Matter controls",
        "# Generated by Viper Vision. These switches are intended for Matterbridge, Alexa, and Google Home.",
        "",
        "rest_command:",
        _rest_bool_commands("viper_set_armed", f"{base_url}/api/control/armed"),
        _rest_bool_commands("viper_set_global_mute", f"{base_url}/api/control/global_mute"),
        _rest_bool_commands("viper_set_ice_maker", f"{base_url}/api/control/ice_maker/enabled"),
    ]
    for control in speaker_controls:
        lines.append(_rest_bool_commands(control["rest_command"], f"{base_url}{control['endpoint_path']}"))

    lines.extend(
        [
            "",
            "sensor:",
            "  - platform: rest",
            "    name: Viper Control State",
            f"    resource: {_q(base_url + '/api/control/state')}",
            "    unique_id: viper_control_state",
            "    method: GET",
            "    scan_interval: 10",
            "    timeout: 5",
            "    value_template: \"{{ value_json.ready | default(false) }}\"",
            "    json_attributes:",
            "      - armed",
            "      - global_mute",
            "      - ice_maker",
            "      - ready",
            "      - speakers",
            "",
            "template:",
            "  - switch:",
        ]
    )
    for control in base_viper_switches():
        lines.extend(_template_switch_lines(control))
    for control in speaker_controls:
        lines.extend(_template_switch_lines(control))
    return "\n".join(lines).rstrip() + "\n"


def write_matter_package(config_data=None, output_path=None):
    path = Path(output_path) if output_path else cfg.DATA_DIR / "ha_packages" / MATTER_PACKAGE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_matter_controls_package(config_data), encoding="utf-8")
    return path


def install_matter_package_via_samba(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    host = str(ha_settings.get("ha_ip") or "").strip()
    if not host:
        return {"ok": False, "reason": "missing_host", "message": "Home Assistant host is missing."}
    if host.startswith("http://") or host.startswith("https://"):
        parsed_host, _port = discovery.normalize_ha_host(host)
        host = parsed_host or host
    config_root = Path(f"\\\\{host}\\config")
    packages_dir = config_root / "packages"
    config_yaml = config_root / "configuration.yaml"
    try:
        config_available = config_root.exists()
    except OSError as e:
        config_available = False
        samba_error = str(e)
    else:
        samba_error = ""
    if not config_available:
        detail = f" {samba_error}" if samba_error else ""
        return {
            "ok": False,
            "reason": "samba_unavailable",
            "message": f"Could not open {config_root}.{detail} Install Samba share or copy the generated package manually.",
            "generated_path": str(write_matter_package(data)),
        }

    packages_dir.mkdir(parents=True, exist_ok=True)
    target = packages_dir / MATTER_PACKAGE_FILENAME
    target.write_text(generate_matter_controls_package(data), encoding="utf-8")
    packages_enabled = _configuration_has_packages_include(config_yaml)
    return {
        "ok": True,
        "method": "samba",
        "target": str(target),
        "packages_enabled": packages_enabled,
        "message": (
            f"Installed {MATTER_PACKAGE_FILENAME} to {target}."
            if packages_enabled
            else f"Installed {MATTER_PACKAGE_FILENAME}, but Home Assistant packages may not be enabled in configuration.yaml."
        ),
    }


def install_matter_package_via_ssh(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    host = str(ha_settings.get("ha_ip") or "").strip()
    if host.startswith("http://") or host.startswith("https://"):
        host, _port = discovery.normalize_ha_host(host)
    if not host:
        return {"ok": False, "method": "ssh", "reason": "missing_host", "message": "Home Assistant host is missing."}
    if not shutil.which("ssh.exe") or not shutil.which("scp.exe"):
        return {"ok": False, "method": "ssh", "reason": "missing_tools", "message": "Windows OpenSSH ssh.exe or scp.exe is missing."}
    package_path = write_matter_package(data)
    remote_path = f"/config/packages/{MATTER_PACKAGE_FILENAME}"
    mkdir = _run_ssh(host, "mkdir -p /config/packages")
    if not mkdir.get("ok"):
        return {"ok": False, "method": "ssh", "reason": "ssh_unreachable", "message": f"Could not prepare /config/packages over SSH: {mkdir.get('message')}"}
    copy = _run_scp_to_ha(host, package_path, remote_path)
    if not copy.get("ok"):
        return {"ok": False, "method": "ssh", "reason": "copy_failed", "message": f"Could not copy {MATTER_PACKAGE_FILENAME} over SSH: {copy.get('message')}"}
    verify = _run_ssh(host, f"test -s '{remote_path}'")
    if not verify.get("ok"):
        return {"ok": False, "method": "ssh", "reason": "verify_failed", "message": f"Copied package was not found at {remote_path}: {verify.get('message')}"}
    package_enabled = _run_ssh(host, "grep -qi 'include_dir_named packages' /config/configuration.yaml")
    reload_result = _reload_ha_matter_entities(data)
    return {
        "ok": True,
        "method": "ssh",
        "target": remote_path,
        "packages_enabled": bool(package_enabled.get("ok")),
        "reload": reload_result,
        "message": (
            f"Installed {MATTER_PACKAGE_FILENAME} to {remote_path} over SSH."
            if package_enabled.get("ok")
            else f"Installed {MATTER_PACKAGE_FILENAME} over SSH, but Home Assistant packages may not be enabled in configuration.yaml."
        ),
    }


def check_viper_control_api(config_data=None, timeout=5):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    host = str(data.get("viper_host") or cfg.PC_IP).strip()
    port = int(data.get("flask_port") or cfg.FLASK_PORT)
    url = f"http://{host}:{port}/api/control/state"
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code != 200:
            return {"ok": False, "url": url, "message": f"Viper control API returned HTTP {response.status_code}."}
        payload = response.json()
    except Exception as e:
        return {"ok": False, "url": url, "message": f"Viper control API could not be reached at {url}: {e}"}
    return {"ok": bool(payload.get("ready")), "url": url, "state": payload, "message": "Viper control API is reachable."}


def check_ha_matter_entities(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    result = discovery.get_ha_states(
        token=ha_settings.get("ha_token"),
        ha_ip=ha_settings.get("ha_ip"),
        ha_port=ha_settings.get("ha_port"),
        timeout=8,
    )
    if not result.get("ok"):
        return {"ok": False, "message": result.get("message") or "Could not read Home Assistant states.", "missing": matter_entity_ids(data)}
    available = {state.get("entity_id") for state in result.get("states", [])}
    expected = matter_entity_ids(data)
    missing = [entity_id for entity_id in expected if entity_id not in available]
    return {
        "ok": not missing,
        "expected": expected,
        "missing": missing,
        "message": "All Viper Matter entities exist in Home Assistant." if not missing else "Some Viper Matter entities are missing in Home Assistant.",
    }


def check_samba_access(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    host = str(ha_settings.get("ha_ip") or "").strip()
    if host.startswith("http://") or host.startswith("https://"):
        host, _port = discovery.normalize_ha_host(host)
    if not host:
        return {"ok": False, "reason": "missing_host", "message": "Home Assistant host is missing."}
    config_root = Path(f"\\\\{host}\\config")
    try:
        available = config_root.exists()
    except OSError as e:
        text = str(e)
        reason = "credentials_rejected" if "1326" in text or "password" in text.lower() else "unavailable"
        return {
            "ok": False,
            "reason": reason,
            "path": str(config_root),
            "message": f"Windows cannot open {config_root}. {text}",
        }
    return {
        "ok": bool(available),
        "reason": "ok" if available else "unavailable",
        "path": str(config_root),
        "message": f"Samba config share is reachable at {config_root}." if available else f"Windows cannot open {config_root}.",
    }


def check_ssh_config_access(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    host = str(ha_settings.get("ha_ip") or "").strip()
    if host.startswith("http://") or host.startswith("https://"):
        host, _port = discovery.normalize_ha_host(host)
    if not host:
        return {"ok": False, "reason": "missing_host", "message": "Home Assistant host is missing."}
    if not shutil.which("ssh.exe") or not shutil.which("scp.exe"):
        return {"ok": False, "reason": "missing_tools", "message": "Windows OpenSSH ssh.exe or scp.exe is missing."}
    probe = _run_ssh(host, "test -d /config && test -w /config && test -d /config/packages || mkdir -p /config/packages")
    if not probe.get("ok"):
        return {"ok": False, "reason": "ssh_unreachable", "message": f"Viper cannot write HA config over SSH: {probe.get('message')}"}
    return {"ok": True, "reason": "ok", "message": "Viper can write Home Assistant config over SSH/SCP."}


def matter_health_report(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    expected = matter_entity_ids(data)
    api_result = check_viper_control_api(data)
    samba = check_samba_access(data)
    ssh = check_ssh_config_access(data)
    ha_entities = _ha_matter_entity_health(data, expected)
    matterbridge = _matterbridge_health(data)
    issues = []
    if not api_result.get("ok"):
        issues.append("Viper control API is not reachable.")
    if ha_entities.get("missing"):
        issues.append("Some Viper Matter entities are missing in Home Assistant.")
    if ha_entities.get("duplicates"):
        issues.append("Home Assistant has duplicate Viper Matter entities with _2 suffixes.")
    if ha_entities.get("stale"):
        issues.append("Home Assistant has stale unavailable Viper Matter entities.")
    if ha_entities.get("unavailable"):
        issues.append("Some Viper Matter entities are unavailable in Home Assistant.")
    if not matterbridge.get("reachable"):
        issues.append("Matterbridge is not reachable.")
    elif not matterbridge.get("plugin_loaded"):
        issues.append("matterbridge-hass is not loaded.")
    elif int(matterbridge.get("device_count") or 0) < len(expected):
        issues.append("Matterbridge is exposing fewer Viper devices than expected.")
    if matterbridge.get("restart_required"):
        issues.append("Matterbridge says a restart is required.")
    return {
        "ok": not issues,
        "issues": issues,
        "expected": expected,
        "api": api_result,
        "samba": samba,
        "ssh": ssh,
        "ha": ha_entities,
        "matterbridge": matterbridge,
        "matterbridge_url": _matterbridge_url(data),
        "ha_host": ha_settings.get("ha_ip") or "",
    }


def format_matter_health_report(report):
    mb = report.get("matterbridge", {})
    ha = report.get("ha", {})
    samba = report.get("samba", {})
    ssh = report.get("ssh", {})
    title = "PASS" if report.get("ok") else "NEEDS ATTENTION"
    if report.get("ok") and (not samba.get("ok") or not ssh.get("ok")):
        title = "PASS WITH CONFIG WARNING"
    lines = [
        f"Matter/Alexa Health: {title}",
        "",
        _line("Viper control API", report.get("api", {}).get("ok"), report.get("api", {}).get("message", "")),
        _line("SSH config access", ssh.get("ok"), ssh.get("message", "")),
        _line("Samba config share fallback", samba.get("ok"), samba.get("message", "")),
        _line("HA clean switch entities", not ha.get("missing"), f"missing {len(ha.get('missing') or [])}"),
        _line("HA duplicate entities", not ha.get("duplicates"), f"duplicates {len(ha.get('duplicates') or [])}"),
        _line("HA stale entities", not ha.get("stale"), f"stale {len(ha.get('stale') or [])}"),
        _line("HA unavailable entities", not ha.get("unavailable"), f"unavailable {len(ha.get('unavailable') or [])}"),
        _line("Matterbridge reachable", mb.get("reachable"), mb.get("message", "")),
        _line("Matterbridge plugin", mb.get("plugin_loaded"), mb.get("plugin_message", "")),
        _line("Matterbridge devices", int(mb.get("device_count") or 0) >= len(report.get("expected") or []), f"{mb.get('device_count', 0)} exposed; expected {len(report.get('expected') or [])}"),
        _line("Matterbridge restart", not mb.get("restart_required"), "restart required" if mb.get("restart_required") else "no restart required"),
        _line("Alexa fabric", bool(mb.get("alexa_fabric")), mb.get("fabric_message", "")),
        "",
        "Expected Matter entities:",
    ]
    lines.extend(f"- {entity_id}" for entity_id in report.get("expected") or [])
    if ha.get("duplicates"):
        lines.extend(["", "Duplicate HA entities to repair:"])
        lines.extend(f"- {item.get('entity_id')} duplicates {item.get('base_entity_id')}" for item in ha.get("duplicates") or [])
    if ha.get("unavailable"):
        lines.extend(["", "Unavailable HA entities:"])
        lines.extend(f"- {entity_id}" for entity_id in ha.get("unavailable") or [])
    if ha.get("stale"):
        lines.extend(["", "Stale HA entities to repair:"])
        lines.extend(f"- {entity_id}" for entity_id in ha.get("stale") or [])
    if mb.get("devices"):
        lines.extend(["", "Matterbridge exposed devices:"])
        lines.extend(f"- {item.get('name')} ({item.get('reachable') and 'reachable' or 'not reachable'})" for item in mb.get("devices") or [])
    if not samba.get("ok"):
        lines.extend(
            [
                "",
                "Samba reachability:",
                "Samba is optional. Viper now prefers SSH/SCP for Home Assistant package copies.",
                "To keep it reachable: keep the Samba share add-on installed, started, and Start on boot enabled.",
                "Windows should be able to open: " + (samba.get("path") or "\\\\HOME_ASSISTANT_IP\\config"),
                "If Windows asks for credentials, use the Samba add-on username/password, not your HA long-lived token.",
            ]
        )
    if not ssh.get("ok"):
        lines.extend(
            [
                "",
                "SSH config access:",
                "This is the preferred Viper-only setup path for Home Assistant config writes.",
                "Make sure the Home Assistant SSH add-on or HA OS SSH access is enabled for root key login.",
                "The existing HA backup task uses this same SSH/SCP path.",
            ]
        )
    if report.get("issues"):
        lines.extend(["", "Repair plan:"])
        lines.extend(f"- {issue}" for issue in report.get("issues") or [])
        lines.append("Press Repair Matter/Alexa to fix Viper-owned duplicates, refresh Matterbridge config, and restart Matterbridge when needed.")
    return "\n".join(lines)


def repair_matter_stack(config_data=None, *, cleanup_registry=True):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    before = matter_health_report(data)
    actions = []
    if cleanup_registry and (before.get("ha", {}).get("duplicates") or before.get("ha", {}).get("stale")):
        cleanup = cleanup_ha_matter_duplicates(data, before)
        actions.append(cleanup)
    config_result = configure_matterbridge_hass(data, install_plugin=True)
    actions.append({"action": "configure_matterbridge", **config_result})
    mb = _matterbridge_health(data)
    if mb.get("restart_required") or not mb.get("plugin_loaded") or int(mb.get("device_count") or 0) < len(matter_entity_ids(data)):
        restart = restart_matterbridge_addon(data)
        actions.append({"action": "restart_matterbridge", **restart})
        if restart.get("ok"):
            time.sleep(12)
    after = matter_health_report(data)
    return {"ok": after.get("ok"), "before": before, "after": after, "actions": actions}


def format_matter_repair_report(result):
    lines = ["Matter/Alexa Repair", ""]
    for action in result.get("actions") or []:
        label = action.get("action", "action").replace("_", " ").title()
        lines.append(_line(label, action.get("ok"), action.get("message") or action.get("reason") or "done"))
        for item in action.get("changes") or []:
            lines.append(f"  - {item}")
    lines.extend(["", format_matter_health_report(result.get("after") or {})])
    return "\n".join(lines)


def setup_status_report(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    package_path = write_matter_package(data)
    samba_install = ensure_samba_addon(data)
    matterbridge_install = ensure_matterbridge_addon(data)
    install_result = install_matter_package_via_ssh(data)
    if not install_result.get("ok"):
        samba_result = install_matter_package_via_samba(data)
        if samba_result.get("ok"):
            install_result = samba_result
        else:
            install_result = {
                **install_result,
                "samba_fallback": samba_result,
                "message": f"{install_result.get('message', '')} Samba fallback also failed: {samba_result.get('message', '')}".strip(),
            }
    api_result = check_viper_control_api(data)
    ha_result = check_ha_matter_entities(data)
    matterbridge_result = configure_matterbridge_hass(data, install_plugin=True)
    return {
        "package_path": str(package_path),
        "samba_install": samba_install,
        "matterbridge_install": matterbridge_install,
        "install": install_result,
        "api": api_result,
        "ha": ha_result,
        "matterbridge": matterbridge_result,
        "entity_ids": matter_entity_ids(data),
        "entity_domains": matter_entity_domains(data),
        "matterbridge_url": _matterbridge_url(data),
    }


def format_setup_report(report):
    lines = [
        "Alexa and Google control setup",
        "",
        _line("Viper control API", report.get("api", {}).get("ok"), report.get("api", {}).get("message", "")),
        _line("Samba share", report.get("samba_install", {}).get("ok"), report.get("samba_install", {}).get("message", "")),
        _line("HA package install", report.get("install", {}).get("ok"), report.get("install", {}).get("message", "")),
        _line("HA Matter entities", report.get("ha", {}).get("ok"), report.get("ha", {}).get("message", "")),
        _line("Matterbridge add-on", report.get("matterbridge_install", {}).get("ok"), report.get("matterbridge_install", {}).get("message", "")),
        _line("Matterbridge plugin", report.get("matterbridge", {}).get("ok"), report.get("matterbridge", {}).get("message", "")),
        "",
        "Entities to expose:",
    ]
    for entity_id in report.get("entity_ids") or []:
        lines.append(f"- {entity_id}")
    missing = report.get("ha", {}).get("missing") or []
    if missing:
        lines.extend(["", "Missing in Home Assistant right now:"])
        lines.extend(f"- {entity_id}" for entity_id in missing)
        lines.extend(["", "Restart Home Assistant, then run this setup again."])
    lines.extend(
        [
            "",
            "Matterbridge:",
            f"- Open: {report.get('matterbridge_url') or 'http://HOME_ASSISTANT_IP:8283'}",
            "- Plugin: matterbridge-hass",
            f"- Host: {_ha_ws_url_from_report_url(report.get('matterbridge_url')) or 'ws://HOME_ASSISTANT_IP:8123'}",
            "- Main whitelist: the entity IDs above",
            f"- Entity whitelist/domain: {', '.join(report.get('entity_domains') or ['switch'])}",
            "",
            "Pairing:",
            "- The Matter pairing code is unique to this Matterbridge install.",
            "- Open Matterbridge and use the manual code or QR code shown there.",
            "- In Alexa, choose Matter, then Generic Matter device if asked.",
            "- In Google Home, choose Add device, then Matter-enabled device.",
        ]
    )
    manual_steps = report.get("matterbridge_install", {}).get("manual_steps") or []
    if manual_steps:
        lines.extend(["", "Manual Matterbridge install steps:"])
        lines.extend(f"- {step}" for step in manual_steps)
    samba_steps = report.get("samba_install", {}).get("manual_steps") or []
    if samba_steps:
        lines.extend(["", "Manual Samba install steps:"])
        lines.extend(f"- {step}" for step in samba_steps)
    return "\n".join(lines)


def ensure_matterbridge_addon(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    settings = cfg.get_ha_settings(data, include_env=True)
    result = _ensure_supervisor_addon(
        settings,
        label="Matterbridge",
        default_slug=MATTERBRIDGE_ADDON_SLUG,
        repository_url=MATTERBRIDGE_REPOSITORY_URL,
        find_slug_func=_find_matterbridge_slug,
        install_timeout=240,
    )
    if not result.get("ok"):
        result["manual_steps"] = matterbridge_manual_install_steps(settings)
    return result


def ensure_samba_addon(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    settings = cfg.get_ha_settings(data, include_env=True)
    result = _ensure_supervisor_addon(
        settings,
        label="Samba share",
        default_slug=SAMBA_ADDON_SLUG,
        repository_url="",
        find_slug_func=_find_samba_slug,
        install_timeout=180,
    )
    if not result.get("ok"):
        result["manual_steps"] = samba_manual_install_steps(settings)
    return result


def samba_manual_install_steps(settings=None):
    host = ""
    if isinstance(settings, dict):
        host = str(settings.get("ha_ip") or "").strip()
    base = f"http://{host}:{settings.get('ha_port') or '8123'}" if host and isinstance(settings, dict) else "Home Assistant"
    return [
        f"Open {base}.",
        "Go to Settings, Add-ons, Add-on Store.",
        "Install the official Samba share add-on.",
        "Start Samba share and turn on Start on boot.",
        f"Confirm Windows can open \\\\{host or 'HOME_ASSISTANT_IP'}\\config.",
        "Return to Viper and press Set Up Alexa And Google Controls again.",
    ]


def matterbridge_manual_install_steps(settings=None):
    host = ""
    if isinstance(settings, dict):
        host = str(settings.get("ha_ip") or "").strip()
    base = f"http://{host}:{settings.get('ha_port') or '8123'}" if host and isinstance(settings, dict) else "Home Assistant"
    return [
        f"Open {base}.",
        "Install the Matterbridge add-on repository:",
        MATTERBRIDGE_REPOSITORY_URL,
        "Install and start the Matterbridge add-on.",
        f"Open Matterbridge at http://{host or 'HOME_ASSISTANT_IP'}:{MATTERBRIDGE_PORT}.",
        "Install the matterbridge-hass plugin if it is not already installed.",
        "Return to Viper and press Set Up Alexa And Google Controls again.",
    ]


def configure_matterbridge_hass(config_data=None, timeout=12, install_plugin=False):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    ha_settings = cfg.get_ha_settings(data, include_env=True)
    token = ha_settings.get("ha_token") or ""
    host = str(ha_settings.get("ha_ip") or "").strip()
    if not host:
        return {"ok": False, "reason": "missing_host", "message": "Home Assistant host is missing."}
    if not token:
        return {"ok": False, "reason": "missing_token", "message": "Home Assistant token is missing."}
    if host.startswith("http://") or host.startswith("https://"):
        host, _port = discovery.normalize_ha_host(host)
    matterbridge_ws = f"ws://{host}:{MATTERBRIDGE_PORT}"
    ha_ws = f"ws://{host}:{ha_settings.get('ha_port') or '8123'}"
    entities = matter_entity_ids(data)
    try:
        plugins = _matterbridge_call_with_retry(matterbridge_ws, "/api/plugins", {}, timeout=timeout, attempts=8)
    except Exception as e:
        return {"ok": False, "reason": "unreachable", "message": f"Matterbridge is not reachable at http://{host}:{MATTERBRIDGE_PORT}: {e}"}
    plugin = _find_matterbridge_hass_plugin(plugins)
    if not plugin:
        if not install_plugin:
            return {"ok": False, "reason": "missing_plugin", "message": "Matterbridge is running, but the matterbridge-hass plugin is not installed yet."}
        try:
            _matterbridge_call(
                matterbridge_ws,
                "/api/install",
                {"packageName": MATTERBRIDGE_HASS_PLUGIN, "restart": False},
                timeout=120,
            )
            plugins = _matterbridge_call_with_retry(matterbridge_ws, "/api/plugins", {}, timeout=timeout, attempts=8)
            plugin = _find_matterbridge_hass_plugin(plugins)
        except Exception as e:
            return {"ok": False, "reason": "plugin_install_failed", "message": f"Could not install matterbridge-hass automatically: {e}"}
    if not plugin:
        return {"ok": False, "reason": "missing_plugin", "message": "matterbridge-hass did not appear after install. Open Matterbridge, install it, then run this setup again."}

    config_json = plugin.get("configJson") if isinstance(plugin.get("configJson"), dict) else {}
    existing_whitelist = list(config_json.get("whiteList") or [])
    existing_domains = list(config_json.get("entityWhiteList") or [])
    merged_whitelist = _merge_unique(existing_whitelist + entities)
    merged_domains = _merge_unique(existing_domains + matter_entity_domains(data))
    form_data = dict(config_json)
    form_data.update(
        {
            "name": "matterbridge-hass",
            "type": "DynamicPlatform",
            "version": plugin.get("version") or form_data.get("version") or "1.3.1",
            "host": ha_ws,
            "certificatePath": form_data.get("certificatePath") or "",
            "rejectUnauthorized": bool(form_data.get("rejectUnauthorized", False)),
            "token": token,
            "reconnectTimeout": int(form_data.get("reconnectTimeout") or 60),
            "reconnectRetries": int(form_data.get("reconnectRetries") or 10),
            "filterByArea": "",
            "filterByLabel": "",
            "whiteList": merged_whitelist,
            "blackList": [],
            "entityWhiteList": merged_domains,
            "entityBlackList": [],
            "deviceEntityBlackList": {},
            "splitEntities": [],
            "splitByLabel": "",
            "splitNameStrategy": form_data.get("splitNameStrategy") or "Entity name",
            "controllerStrategy": form_data.get("controllerStrategy") or "Merge",
            "namePostfix": form_data.get("namePostfix") or "",
            "postfix": form_data.get("postfix") or "",
            "airQualityRegex": form_data.get("airQualityRegex") or "",
            "enableServerRvc": bool(form_data.get("enableServerRvc", True)),
            "discardHiddenEntities": bool(form_data.get("discardHiddenEntities", True)),
            "virtualControlLabel": form_data.get("virtualControlLabel") or "",
            "debug": bool(form_data.get("debug", False)),
            "unregisterOnShutdown": bool(form_data.get("unregisterOnShutdown", False)),
        }
    )
    try:
        _matterbridge_call(
            matterbridge_ws,
            "/api/savepluginconfig",
            {"pluginName": "matterbridge-hass", "formData": form_data},
            timeout=timeout,
        )
    except Exception as e:
        return {"ok": False, "reason": "save_failed", "message": f"Matterbridge plugin config could not be saved: {e}"}
    return {
        "ok": True,
        "message": "Matterbridge matterbridge-hass plugin is installed and configured for the Viper Matter entities. Restart Matterbridge if newly added entities do not appear.",
        "registered_devices": plugin.get("registeredDevices"),
    }


def _rest_bool_commands(name, url):
    return "\n".join(
        [
            _rest_static_bool_command(f"{name}_on", url, True),
            _rest_static_bool_command(f"{name}_off", url, False),
        ]
    )


def _rest_static_bool_command(name, url, state):
    payload = "true" if state else "false"
    return "\n".join(
        [
            f"  {name}:",
            f"    url: {_q(url)}",
            "    method: POST",
            '    content_type: "application/json"',
            f"    payload: '{{\"state\": {payload}}}'",
            "    timeout: 10",
        ]
    )


def _template_switch_lines(control):
    return [
        f"      - name: {_q(control['friendly_name'])}",
        f"        unique_id: {_q(control['unique_id'])}",
        f"        state: \"{control['state_template']}\"",
        "        turn_on:",
        f"          - action: rest_command.{control['rest_command']}_on",
        "        turn_off:",
        f"          - action: rest_command.{control['rest_command']}_off",
    ]


def _configuration_has_packages_include(config_yaml):
    try:
        text = config_yaml.read_text(encoding="utf-8")
    except OSError:
        return False
    lowered = text.lower()
    return "packages:" in lowered and "include_dir_named packages" in lowered


def _unique_slug(value, used):
    base = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_") or "viper_speaker"
    slug = base
    index = 2
    while slug in used:
        slug = f"{base}_{index}"
        index += 1
    used.add(slug)
    return slug


def _title_words(value):
    words = re.sub(r"[^A-Za-z0-9]+", " ", str(value or "")).strip()
    words = re.sub(r"\bentry\s+way\b", "entryway", words, flags=re.IGNORECASE)
    return " ".join(part.capitalize() for part in words.split()) or "Speaker"


def _speaker_friendly_name(value):
    title = _title_words(value)
    if re.search(r"\b(speaker|sonos)\b$", title, flags=re.IGNORECASE):
        return f"Viper {title}"
    return f"Viper {title} Speaker"


def _speaker_slug_base(value):
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    text = re.sub(r"\bspeaker$", "", text).strip()
    text = re.sub(r"\bentry\s+way\b", "entryway", text)
    return text or "speaker"


def _ha_entity_slug(friendly_name):
    return re.sub(r"[^a-z0-9]+", "_", str(friendly_name or "").lower()).strip("_") or "viper_speaker"


def _jinja_single_quote(value):
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _q(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _merge_unique(values):
    merged = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _line(label, ok, detail):
    mark = "OK" if ok else "Needs attention"
    return f"{label}: {mark}. {detail}".rstrip()


def _ha_matter_entity_health(config_data, expected):
    ha_settings = cfg.get_ha_settings(config_data, include_env=True)
    result = discovery.get_ha_states(
        token=ha_settings.get("ha_token"),
        ha_ip=ha_settings.get("ha_ip"),
        ha_port=ha_settings.get("ha_port"),
        timeout=8,
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "message": result.get("message") or "Could not read Home Assistant states.",
            "missing": list(expected),
            "duplicates": [],
            "unavailable": [],
            "stale": [],
        }
    states = {item.get("entity_id"): item for item in result.get("states", []) if item.get("entity_id")}
    expected_set = set(expected)
    expected_set.add("sensor.viper_control_state")
    missing = [entity_id for entity_id in sorted(expected_set) if entity_id not in states]
    unavailable = [
        entity_id
        for entity_id in sorted(expected_set)
        if entity_id in states and str(states[entity_id].get("state") or "").lower() == "unavailable"
    ]
    duplicates = []
    stale = []
    for entity_id, state in sorted(states.items()):
        if not entity_id.startswith(("switch.viper_", "sensor.viper_control_state")):
            continue
        base = re.sub(r"_\d+$", "", entity_id)
        if entity_id != base and base in expected_set:
            duplicates.append(
                {
                    "entity_id": entity_id,
                    "base_entity_id": base,
                    "state": state.get("state"),
                    "base_state": states.get(base, {}).get("state"),
                }
            )
        elif entity_id not in expected_set and str(state.get("state") or "").lower() == "unavailable":
            stale.append(entity_id)
    return {
        "ok": not missing and not duplicates and not unavailable and not stale,
        "message": "Home Assistant Viper Matter entities are clean." if not missing and not duplicates and not unavailable and not stale else "Home Assistant Viper Matter entities need cleanup.",
        "missing": missing,
        "duplicates": duplicates,
        "unavailable": unavailable,
        "stale": stale,
        "states": {entity_id: states.get(entity_id, {}).get("state") for entity_id in sorted(expected_set) if entity_id in states},
    }


def _matterbridge_health(config_data):
    data = cfg.validate_and_normalize_config(config_data)
    ha = cfg.get_ha_settings(data, include_env=True)
    host = str(ha.get("ha_ip") or "").strip()
    if host.startswith("http://") or host.startswith("https://"):
        host, _port = discovery.normalize_ha_host(host)
    if not host:
        return {"reachable": False, "ok": False, "message": "Home Assistant host is missing."}
    ws_url = f"ws://{host}:{MATTERBRIDGE_PORT}"
    try:
        settings = _matterbridge_call(ws_url, "/api/settings", {}, timeout=8)
        plugins = _matterbridge_call(ws_url, "/api/plugins", {}, timeout=8)
        devices = _matterbridge_call(ws_url, "/api/devices", {}, timeout=8)
    except Exception as e:
        return {"reachable": False, "ok": False, "message": f"Matterbridge is not reachable at http://{host}:{MATTERBRIDGE_PORT}: {e}"}
    info = settings.get("matterbridgeInformation") if isinstance(settings, dict) else {}
    plugin = _find_matterbridge_hass_plugin(plugins)
    matter = {}
    try:
        matter = _matterbridge_call(ws_url, "/api/matter", {"id": "Matterbridge", "server": True}, timeout=8)
    except Exception:
        matter = {}
    fabrics = matter.get("fabricInformations") if isinstance(matter, dict) else []
    alexa_fabric = next((item for item in fabrics or [] if "alexa" in str(item.get("rootVendorName") or item.get("label") or "").lower()), None)
    exposed = [
        {
            "name": item.get("name"),
            "reachable": bool(item.get("reachable")),
            "cluster": item.get("cluster") or "",
        }
        for item in devices if isinstance(devices, list)
    ]
    return {
        "reachable": True,
        "ok": True,
        "message": "Matterbridge is reachable.",
        "restart_required": bool(info.get("restartRequired")),
        "plugin_loaded": bool(plugin and plugin.get("loaded") and plugin.get("started")),
        "plugin_message": "matterbridge-hass is loaded." if plugin and plugin.get("loaded") and plugin.get("started") else "matterbridge-hass is not loaded.",
        "plugin_configured": bool(plugin and plugin.get("configJson")),
        "registered_devices": int((plugin or {}).get("registeredDevices") or 0),
        "device_count": len(exposed),
        "devices": exposed,
        "alexa_fabric": bool(alexa_fabric),
        "fabric_message": f"Alexa fabric present: {alexa_fabric.get('label') or alexa_fabric.get('rootVendorName')}" if alexa_fabric else "No Alexa fabric is paired.",
        "manual_pairing_code": matter.get("manualPairingCode") if isinstance(matter, dict) else "",
        "qr_pairing_code": matter.get("qrPairingCode") if isinstance(matter, dict) else "",
        "advertising": bool(matter.get("advertising")) if isinstance(matter, dict) else False,
    }


def cleanup_ha_matter_duplicates(config_data=None, report=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    health = (report or matter_health_report(data)).get("ha", {})
    duplicates = health.get("duplicates") or []
    stale = health.get("stale") or []
    if not duplicates and not stale:
        return {"ok": True, "action": "cleanup_ha_duplicates", "message": "No Viper Matter duplicate entities found.", "changes": []}
    settings = cfg.get_ha_settings(data, include_env=True)
    changes = []
    try:
        for item in duplicates:
            duplicate_id = item.get("entity_id")
            base_id = item.get("base_entity_id")
            if not duplicate_id or not base_id:
                continue
            base_state = str(item.get("base_state") or "").lower()
            if base_state == "unavailable":
                _ha_ws_command(settings, {"type": "config/entity_registry/remove", "entity_id": base_id}, timeout=12)
                changes.append(f"Removed stale {base_id}.")
            _ha_ws_command(settings, {"type": "config/entity_registry/update", "entity_id": duplicate_id, "new_entity_id": base_id}, timeout=12)
            changes.append(f"Renamed {duplicate_id} to {base_id}.")
        for entity_id in stale:
            _ha_ws_command(settings, {"type": "config/entity_registry/remove", "entity_id": entity_id}, timeout=12)
            changes.append(f"Removed stale {entity_id}.")
    except Exception as e:
        return {"ok": False, "action": "cleanup_ha_duplicates", "message": f"HA duplicate cleanup failed: {e}", "changes": changes}
    return {"ok": True, "action": "cleanup_ha_duplicates", "message": "Viper Matter duplicate HA entities were cleaned up.", "changes": changes}


def restart_matterbridge_addon(config_data=None):
    data = cfg.validate_and_normalize_config(config_data) if config_data is not None else cfg.load_config()
    settings = cfg.get_ha_settings(data, include_env=True)
    slug = MATTERBRIDGE_ADDON_SLUG
    try:
        installed = ha_addons.get_installed_addons(settings, _hassio_request)
        slug = _find_matterbridge_slug(installed) or slug
    except Exception:
        pass
    try:
        url = f"http://{settings.get('ha_ip')}:{settings.get('ha_port') or '8123'}/api/services/hassio/addon_restart"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.get('ha_token')}", "Content-Type": "application/json"},
            json={"addon": slug},
            timeout=12,
        )
        response.raise_for_status()
    except Exception as e:
        return {"ok": False, "reason": "restart_failed", "message": f"Could not restart Matterbridge add-on {slug}: {e}"}
    return {"ok": True, "slug": slug, "message": f"Matterbridge add-on restart requested for {slug}."}


def _ha_ws_command(settings, command, *, timeout=12):
    return ha_addons.ha_ws_command(settings, command, timeout=timeout)


def _ssh_base_args(host):
    return [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=NUL",
        "-o", "LogLevel=ERROR",
        f"root@{host}",
    ]


def _run_ssh(host, command, *, timeout=20):
    try:
        completed = subprocess.run(
            ["ssh.exe", *_ssh_base_args(host), command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as e:
        return {"ok": False, "message": str(e)}
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return {"ok": completed.returncode == 0, "exit_code": completed.returncode, "message": output or "ok"}


def _run_scp_to_ha(host, local_path, remote_path, *, timeout=30):
    try:
        completed = subprocess.run(
            [
                "scp.exe",
                "-q",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=NUL",
                "-o", "LogLevel=ERROR",
                str(local_path),
                f"root@{host}:{remote_path}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as e:
        return {"ok": False, "message": str(e)}
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return {"ok": completed.returncode == 0, "exit_code": completed.returncode, "message": output or "ok"}


def _reload_ha_matter_entities(config_data):
    ha = cfg.get_ha_settings(config_data, include_env=True)
    host = str(ha.get("ha_ip") or "").strip()
    port = str(ha.get("ha_port") or "8123")
    token = ha.get("ha_token") or ""
    if not host or not token:
        return {"ok": False, "message": "Home Assistant host or token is missing."}
    if host.startswith("http://") or host.startswith("https://"):
        host, parsed_port = discovery.normalize_ha_host(host)
        port = parsed_port or port
    base_url = f"http://{host}:{port}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    results = []
    for service in ("rest/reload", "template/reload"):
        try:
            response = requests.post(f"{base_url}/api/services/{service}", headers=headers, json={}, timeout=8)
            results.append({"service": service, "ok": 200 <= response.status_code < 300, "status_code": response.status_code})
        except Exception as e:
            results.append({"service": service, "ok": False, "message": str(e)})
    return {
        "ok": all(item.get("ok") for item in results),
        "results": results,
        "message": "Requested HA rest/template reload." if all(item.get("ok") for item in results) else "One or more HA reload services failed.",
    }


def _matterbridge_url(config_data):
    ha = cfg.get_ha_settings(config_data, include_env=True)
    host = str(ha.get("ha_ip") or "").strip()
    if not host:
        return ""
    if host.startswith("http://") or host.startswith("https://"):
        host, _port = discovery.normalize_ha_host(host)
    return f"http://{host}:{MATTERBRIDGE_PORT}"


def _hassio_request(settings, method, path, *, payload=None, timeout=30):
    return ha_addons.hassio_request(
        settings,
        method,
        path,
        payload=payload,
        timeout=timeout,
        ws_request_func=_hassio_ws_request,
    )


def _hassio_ws_request(settings, method, path, *, payload=None, timeout=30):
    return ha_addons.hassio_ws_request(
        settings,
        method,
        path,
        payload=payload,
        timeout=timeout,
        ws_command_func=ha_addons.ha_ws_command,
    )


def _get_addon_info(settings, slug):
    return _hassio_request(settings, "GET", f"/addons/{slug}/info", timeout=30)


def _ensure_supervisor_addon(settings, *, label, default_slug, repository_url="", find_slug_func=None, install_timeout=180):
    if not settings.get("ha_ip"):
        return {"ok": False, "reason": "missing_host", "message": "Home Assistant host is missing."}
    if not settings.get("ha_token"):
        return {"ok": False, "reason": "missing_token", "message": "Home Assistant token is missing."}

    try:
        _hassio_request(settings, "GET", "/supervisor/info", timeout=12)
    except Exception as e:
        return {
            "ok": False,
            "reason": "supervisor_unavailable",
            "message": f"Automatic {label} install needs Home Assistant OS or Supervised with Supervisor access. {e}",
        }

    finder = find_slug_func or (lambda addons: ha_addons.find_addon_slug(addons, exact_slugs=(default_slug,)))
    try:
        installed = ha_addons.get_installed_addons(settings, _hassio_request)
        installed_slug = finder(installed)
        if installed_slug:
            ha_addons.ensure_addon_started(settings, installed_slug, _get_addon_info, _hassio_request)
            return {"ok": True, "slug": installed_slug, "message": f"{label} is installed and started as {installed_slug}."}

        if repository_url:
            try:
                _hassio_request(settings, "POST", "/store/repositories", payload={"repository": repository_url}, timeout=60)
            except Exception as e:
                text = str(e).lower()
                if "already" not in text and "exist" not in text:
                    return {
                        "ok": False,
                        "reason": "repo_add_failed",
                        "message": f"Could not add the {label} add-on repository automatically: {e}",
                    }
        try:
            _hassio_request(settings, "POST", "/store/reload", timeout=60)
        except Exception:
            pass

        store_payload = _hassio_request(settings, "GET", "/store/addons", timeout=30)
        store_addons = ha_addons.addon_items_from_payload(store_payload)
        slug = finder(store_addons) or default_slug
        try:
            _hassio_request(settings, "POST", f"/store/addons/{slug}/install", payload={"background": False}, timeout=install_timeout)
        except Exception as e:
            text = str(e).lower()
            if "already" not in text and "installed" not in text:
                return {
                    "ok": False,
                    "reason": "install_failed",
                    "message": f"Could not install {label} automatically: {e}",
                }
        ha_addons.ensure_addon_started(settings, slug, _get_addon_info, _hassio_request)
        return {"ok": True, "slug": slug, "message": f"{label} was installed and started as {slug}."}
    except Exception as e:
        return {
            "ok": False,
            "reason": "install_failed",
            "message": f"{label} automatic install failed: {e}",
        }


def _find_samba_slug(addons):
    slug = ha_addons.find_addon_slug(addons, exact_slugs=(SAMBA_ADDON_SLUG,))
    if slug:
        return slug
    for addon in addons or []:
        item_slug = str(addon.get("slug") or addon.get("addon") or "").strip()
        name = str(addon.get("name") or "").lower()
        description = str(addon.get("description") or "").lower()
        haystack = " ".join([item_slug.lower(), name, description])
        if item_slug and "samba" in haystack:
            return item_slug
    return ""


def _find_matterbridge_slug(addons):
    slug = ha_addons.find_addon_slug(addons, exact_slugs=(MATTERBRIDGE_ADDON_SLUG,))
    if slug:
        return slug
    candidates = []
    for addon in addons or []:
        item_slug = str(addon.get("slug") or addon.get("addon") or "").strip()
        name = str(addon.get("name") or "").lower()
        description = str(addon.get("description") or "").lower()
        repository = str(addon.get("repository") or addon.get("url") or addon.get("repository_url") or "").lower()
        haystack = " ".join([item_slug.lower(), name, description, repository])
        if item_slug and "matterbridge" in haystack:
            score = 10
            if "luligu" in haystack:
                score += 10
            if "home-assistant-addon" in haystack or "home assistant application" in haystack:
                score += 5
            candidates.append((score, item_slug))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _find_matterbridge_hass_plugin(plugins):
    for item in plugins if isinstance(plugins, list) else []:
        if item.get("name") == MATTERBRIDGE_HASS_PLUGIN:
            return item
    return None


def _ha_ws_url_from_report_url(matterbridge_url):
    text = str(matterbridge_url or "").strip()
    if not text:
        return ""
    host = text.replace("http://", "").replace("https://", "").split("/", 1)[0].split(":", 1)[0]
    return f"ws://{host}:8123" if host else ""


def _matterbridge_call_with_retry(ws_url, method, params=None, timeout=12, attempts=3):
    last_error = None
    for _attempt in range(max(1, int(attempts or 1))):
        try:
            return _matterbridge_call(ws_url, method, params=params, timeout=timeout)
        except Exception as e:
            last_error = e
            if _attempt < attempts - 1:
                import time

                time.sleep(3)
    raise last_error


def _matterbridge_call(ws_url, method, params=None, timeout=12):
    async def call():
        request_id = int(asyncio.get_event_loop().time() * 1000)
        async with websockets.connect(ws_url, open_timeout=min(float(timeout), 10.0)) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "sender": "Viper",
                        "method": method,
                        "src": "Frontend",
                        "dst": "Matterbridge",
                        "params": params or {},
                    }
                )
            )
            deadline = asyncio.get_event_loop().time() + float(timeout)
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - asyncio.get_event_loop().time()))
                payload = json.loads(raw)
                if payload.get("id") != request_id:
                    continue
                if not payload.get("success", False):
                    raise RuntimeError(payload.get("message") or payload.get("error") or f"Matterbridge rejected {method}.")
                return payload.get("response")
            raise TimeoutError(f"Matterbridge did not answer {method}.")

    return asyncio.run(call())
