import json
import os
import platform
import re
import shutil
import sys
import time
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import viper_config as cfg


APP_VERSION = "1.2.3"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2

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


def build_health_summary(config_data, *, ha_listener_status=None, ha_connection=None, ha_health=None, recent_errors=None):
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

    if ha_connection.get("checked") and not ha_connection.get("ok"):
        active.append(f"Home Assistant connection check failed: {ha_connection.get('message') or ha_connection.get('error') or 'no detail'}.")

    if ha_health.get("checked") and not ha_health.get("ok", False):
        state = ha_health.get("state") or "unknown"
        active.append(f"Home Assistant health check is {state}: {ha_health.get('message') or 'no detail'}.")

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


def collect_diagnostics(config_data=None, *, ha_listener_status=None, ha_connection=None, ha_health=None):
    config_data = cfg.validate_and_normalize_config(config_data if config_data is not None else cfg.load_config())
    recent_errors = recent_log_lines()
    health = build_health_summary(
        config_data,
        ha_listener_status=ha_listener_status,
        ha_connection=ha_connection,
        ha_health=ha_health,
        recent_errors=recent_errors,
    )
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
        "ffmpeg": ffmpeg_status(),
        "config_shape": config_shape(config_data),
        "ha_listener": ha_listener_status or {},
        "ha_connection": ha_connection or {"checked": False},
        "ha_health": ha_health or {"checked": False},
        "health": health,
        "recent_errors": recent_errors,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def health_summary_text(diag):
    health = diag.get("health", {}) if isinstance(diag, dict) else {}
    status = health.get("status") or "unknown"
    lines = [
        f"Health status: {status.upper()}",
        "",
        "Active issues:",
    ]
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
    if health.get("last_log_line"):
        lines.extend(["", f"Latest log line: {health['last_log_line']}"])
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
