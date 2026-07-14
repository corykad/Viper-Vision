import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path


LOGGER = logging.getLogger(__name__)
OPTIONS_PATH = Path("/data/options.json")


@dataclass(frozen=True)
class CoreConfig:
    log_level: str = "info"
    health_check_seconds: int = 60
    enabled_features: dict = field(default_factory=dict)
    notification_service: str = "persistent_notification.create"
    doorbell_speaker_service: str = ""
    fridge_speaker_service: str = ""
    vacuum_speaker_service: str = ""
    pushover_service: str = ""
    speaker_targets: list = field(default_factory=list)
    fallback_ha_url: str = ""
    fallback_ha_token: str = ""
    supervisor_token: str = ""

    @property
    def ha_url(self):
        if self.supervisor_token:
            return "http://supervisor/core/api"
        return self.fallback_ha_url.rstrip("/")

    @property
    def ha_token(self):
        return self.supervisor_token or self.fallback_ha_token

    def public_dict(self):
        return {
            "log_level": self.log_level,
            "health_check_seconds": self.health_check_seconds,
            "enabled_features": self.enabled_features,
            "notification_service": self.notification_service,
            "doorbell_speaker_service_configured": bool(self.doorbell_speaker_service),
            "fridge_speaker_service_configured": bool(self.fridge_speaker_service),
            "vacuum_speaker_service_configured": bool(self.vacuum_speaker_service),
            "pushover_service_configured": bool(self.pushover_service),
            "speaker_target_count": len(self.speaker_targets),
            "ha_mode": "supervisor" if self.supervisor_token else "fallback",
            "ha_url_configured": bool(self.ha_url),
            "ha_token_configured": bool(self.ha_token),
        }


def load_config(options_path=OPTIONS_PATH):
    data = {}
    if options_path.exists():
        try:
            data = json.loads(options_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not read add-on options: %s", exc)

    return CoreConfig(
        log_level=str(data.get("log_level") or "info").lower(),
        health_check_seconds=max(15, int(data.get("health_check_seconds") or 60)),
        enabled_features=dict(data.get("enabled_features") or {}),
        notification_service=str(data.get("notification_service") or "persistent_notification.create").strip(),
        doorbell_speaker_service=str(data.get("doorbell_speaker_service") or "").strip(),
        fridge_speaker_service=str(data.get("fridge_speaker_service") or "").strip(),
        vacuum_speaker_service=str(data.get("vacuum_speaker_service") or "").strip(),
        pushover_service=str(data.get("pushover_service") or "").strip(),
        speaker_targets=[str(item).strip() for item in (data.get("speaker_targets") or []) if str(item).strip()],
        fallback_ha_url=str(data.get("fallback_ha_url") or "").rstrip("/"),
        fallback_ha_token=str(data.get("fallback_ha_token") or ""),
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN", ""),
    )


def configure_logging(level_name):
    levels = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    logging.basicConfig(
        level=levels.get(str(level_name).lower(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
