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


APP_VERSION = "1.2"

SECRET_KEYWORDS = (
    "token",
    "api_key",
    "apikey",
    "password",
    "secret",
    "gemini",
    "pushover",
    "mqtt_password",
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
        r"((?:ha_token|gemini_api_key|pushover_user_key|pushover_api_token|mqtt_password)\s*[=:]\s*)[^\s,;]+",
        r"((?:token|api[_-]?key|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"(rtsp://[^:\s]+:)[^@\s]+(@)",
    ]
    for pattern in patterns:
        redacted = re.sub(pattern, r"\1[REDACTED]\2" if pattern.endswith("(@)") else r"\1[REDACTED]", redacted, flags=re.IGNORECASE)
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


def config_shape(config_data):
    data = config_data if isinstance(config_data, dict) else {}
    return {
        "has_ha_host": bool(data.get("ha_ip")),
        "has_ha_token": bool(data.get("ha_token")),
        "has_gemini_key": bool(data.get("gemini_api_key")),
        "ha_listener_enabled": bool(data.get("ha_listener_enabled", True)),
        "speaker_count": len(data.get("speakers", {}) if isinstance(data.get("speakers"), dict) else {}),
        "front_rtsp_configured": bool(data.get("rtsp_front") or data.get("doorbell_triggers", {}).get("front", {}).get("rtsp_url")),
        "back_rtsp_configured": bool(data.get("rtsp_back") or data.get("doorbell_triggers", {}).get("back", {}).get("rtsp_url")),
        "cinderella_enabled": bool(data.get("cinderella_enabled", True)),
    }


def collect_diagnostics(config_data=None, *, ha_listener_status=None, ha_connection=None):
    config_data = cfg.validate_and_normalize_config(config_data if config_data is not None else cfg.load_config())
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
        "recent_errors": recent_log_lines(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def diagnostics_text(diag):
    lines = [
        "Viper Vision Diagnostics",
        f"Version: {diag['app']['version']}",
        f"Frozen build: {'yes' if diag['app']['frozen'] else 'no'}",
        f"Python: {diag['app']['python']}",
        f"Platform: {diag['app']['platform']}",
        f"Data folder: {diag['paths']['data_dir']}",
        f"Config file: {diag['paths']['config_file']}",
        f"FFmpeg available: {'yes' if diag['ffmpeg']['available'] else 'no'}",
        f"FFmpeg resolved path: {diag['ffmpeg']['resolved']}",
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
    errors = diag.get("recent_errors", [])
    lines.append("")
    lines.append("Recent warnings/errors:")
    lines.extend(errors[-20:] if errors else ["None found in the current log."])
    return "\n".join(lines)


def _write_zip_text(zip_file, name, value):
    zip_file.writestr(name, redact_text(value))


def create_support_bundle(config_data=None, *, ha_listener_status=None, ha_connection=None, output_dir=None):
    config_data = cfg.validate_and_normalize_config(config_data if config_data is not None else cfg.load_config())
    output = Path(output_dir or cfg.DATA_DIR)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_path = output / f"viper_support_bundle_{stamp}.zip"
    diag = collect_diagnostics(config_data, ha_listener_status=ha_listener_status, ha_connection=ha_connection)

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_zip_text(zf, "diagnostics.txt", diagnostics_text(diag))
        zf.writestr("diagnostics.json", json.dumps(diag, indent=2))
        zf.writestr("config_redacted.json", json.dumps(redact_config(config_data), indent=2))
        zf.writestr("config_shape.json", json.dumps(config_shape(config_data), indent=2))

        for path, arcname in [
            (cfg.DATA_DIR / "viper_full_debug.log", "logs/viper_full_debug.log"),
            (cfg.API_LOG_PATH, "logs/api_usage.json"),
        ]:
            if Path(path).exists():
                try:
                    _write_zip_text(zf, arcname, Path(path).read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass

        for path, arcname in [
            (cfg.APP_DIR / "ViperVision.iss", "build/ViperVision.iss"),
            (cfg.APP_DIR / "requirements.txt", "build/requirements.txt"),
        ]:
            if Path(path).exists():
                try:
                    _write_zip_text(zf, arcname, Path(path).read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass

    return {"ok": True, "path": str(bundle_path), "diagnostics": diag}
