import logging
import time


LOGGER = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, config, ha_client):
        self.config = config
        self.ha = ha_client
        self.recent_events = []
        self.last_event_by_key = {}

    def handle(self, event_type, payload):
        event_type = _clean(event_type)
        payload = payload if isinstance(payload, dict) else {}
        if event_type in {"doorbell", "fridge", "vacuum", "ice_maker", "hvac"}:
            return getattr(self, f"_handle_{event_type}")(payload)
        return self._record(event_type, payload, False, f"Unknown Viper Core event type: {event_type}")

    def _handle_doorbell(self, payload):
        door = _clean(payload.get("door") or payload.get("source") or "front")
        action = _clean(payload.get("action") or payload.get("event") or "pressed")
        key = f"doorbell:{door}:{action}"
        if self._is_duplicate(key, seconds=20):
            return self._record("doorbell", payload, True, f"Ignored duplicate {door} doorbell {action}.", duplicate=True)
        message = f"{_title(door)} doorbell {action}."
        self._notify("Viper Doorbell", message, self.config.doorbell_speaker_service)
        return self._record("doorbell", payload, True, message)

    def _handle_fridge(self, payload):
        appliance = _clean(payload.get("appliance") or payload.get("source") or "fridge")
        state = _clean(payload.get("state") or payload.get("event") or "changed")
        message = f"The {appliance.replace('_', ' ')} is {state.replace('_', ' ')}."
        self._notify("Viper Refrigerator", message, self.config.fridge_speaker_service)
        return self._record("fridge", payload, True, message)

    def _handle_vacuum(self, payload):
        event = _clean(payload.get("event") or payload.get("state") or "status")
        error = str(payload.get("error") or "").strip()
        message = f"Cinderella {event.replace('_', ' ')}."
        if error:
            message = f"{message} {error}"
        self._notify("Viper Vacuum", message, self.config.vacuum_speaker_service)
        return self._record("vacuum", payload, True, message)

    def _handle_ice_maker(self, payload):
        action = _clean(payload.get("action") or payload.get("event") or "status")
        message = f"Ice maker {action.replace('_', ' ')}."
        self._notify("Viper Ice Maker", message, "")
        return self._record("ice_maker", payload, True, message)

    def _handle_hvac(self, payload):
        unit = str(payload.get("unit") or payload.get("entity_id") or "Heat pump").strip()
        state = str(payload.get("state") or payload.get("action") or "status changed").strip()
        message = f"{unit}: {state}."
        self._notify("Viper Heat Pump", message, "")
        return self._record("hvac", payload, True, message)

    def _notify(self, title, message, speaker_service):
        if not self.ha.available():
            LOGGER.warning("Skipping HA notification because HA is not configured: %s", message)
            return
        service = speaker_service or self.config.notification_service
        payload = _service_payload(service, title, message, self.config.speaker_targets)
        try:
            self.ha.call_service(service, payload)
        except Exception as exc:
            LOGGER.warning("Primary notification service failed: %s", exc)
            try:
                self.ha.create_notification(title, message)
            except Exception as fallback_exc:
                LOGGER.warning("Fallback persistent notification failed: %s", fallback_exc)
        if self.config.pushover_service:
            try:
                self.ha.call_service(self.config.pushover_service, {"title": title, "message": message})
            except Exception as exc:
                LOGGER.warning("Pushover service failed: %s", exc)

    def _record(self, event_type, payload, ok, message, duplicate=False):
        item = {
            "ok": bool(ok),
            "event_type": event_type,
            "message": message,
            "payload": payload,
            "duplicate": bool(duplicate),
            "timestamp": int(time.time()),
        }
        self.recent_events.append(item)
        self.recent_events = self.recent_events[-50:]
        LOGGER.info("%s", message)
        return item

    def _is_duplicate(self, key, seconds):
        now = time.monotonic()
        previous = self.last_event_by_key.get(key)
        self.last_event_by_key[key] = now
        return previous is not None and now - previous < seconds


def _service_payload(service, title, message, speaker_targets):
    normalized = str(service or "").replace("/", ".").lower()
    if normalized == "persistent_notification.create":
        return {"title": title, "message": message}
    if normalized.startswith("notify."):
        return {"title": title, "message": message}
    if normalized in {"tts.speak", "tts.cloud_say", "tts.google_translate_say"}:
        payload = {"message": message}
        if speaker_targets:
            payload["entity_id"] = speaker_targets
        return payload
    if normalized.startswith("media_player."):
        return {"entity_id": speaker_targets, "media_content_id": message, "media_content_type": "music"}
    return {"title": title, "message": message}


def _clean(value):
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _title(value):
    return str(value or "").replace("_", " ").title()
