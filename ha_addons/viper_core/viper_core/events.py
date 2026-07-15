import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape

from . import vision


LOGGER = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, config, ha_client, control_state=None):
        self.config = config
        self.ha = ha_client
        self.control_state = control_state
        self.recent_events = []
        self.last_event_by_key = {}

    def handle(self, event_type, payload):
        event_type = _clean(event_type)
        payload = payload if isinstance(payload, dict) else {}
        if event_type in {"doorbell", "doorbell_video", "fridge", "vacuum", "ice_maker", "hvac", "broadcast", "chime", "pushover_test"}:
            return getattr(self, f"_handle_{event_type}")(payload)
        return self._record(event_type, payload, False, f"Unknown Viper Core event type: {event_type}")

    def _handle_doorbell(self, payload):
        door = _clean(payload.get("door") or payload.get("source") or "front")
        action = _clean(payload.get("action") or payload.get("event") or "pressed")
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        key = f"doorbell:{door}"
        if self._is_duplicate(key, seconds=getattr(effective_config, "doorbell_dedupe_seconds", 30)):
            return self._record("doorbell", payload, True, f"Ignored duplicate {_title(door)} door event from {action}.", duplicate=True)
        if self.control_state and not self.control_state.public_state().get("armed", True):
            return self._record("doorbell", payload, True, f"Ignored {door} doorbell {action}; Viper is disarmed.")
        message = vision.describe_doorbell(effective_config, self.ha, door)
        if not message:
            message = f"{_title(door)} doorbell {action.replace('_', ' ')}."
        self._notify("Viper Doorbell", message, self.config.doorbell_speaker_service, "doorbell", "doorbell", payload)
        self._maybe_start_video_followup(door, message, effective_config, payload)
        return self._record("doorbell", payload, True, message)

    def _handle_doorbell_video(self, payload):
        door = _clean(payload.get("door") or payload.get("source") or "front")
        seconds = payload.get("seconds")
        mode = _clean(payload.get("mode") or "manual")
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        message = vision.describe_live_doorbell(effective_config, self.ha, door, seconds=seconds, mode=mode)
        if not message:
            message = f"{_title(door)} live video did not return a description."
        spoken = f"{_title(door)} live video: {message}"
        self._notify("Viper Doorbell Video", spoken, self.config.doorbell_speaker_service, "doorbell", "doorbell_video", payload)
        return self._record("doorbell_video", payload, True, spoken)

    def _handle_fridge(self, payload):
        appliance = _clean(payload.get("appliance") or payload.get("source") or "fridge")
        state = _clean(payload.get("state") or payload.get("event") or "changed")
        stale = self._fridge_stale_status()
        if state == "stale_check":
            if stale.get("stale"):
                message = f"Refrigerator stale check needs attention: last door update was {stale.get('age_minutes')} minutes ago."
            else:
                message = "Refrigerator stale check passed."
            return self._record("fridge", {**payload, "stale_status": stale}, True, message)
        message = f"The {appliance.replace('_', ' ')} is {state.replace('_', ' ')}."
        self._notify("Viper Refrigerator", message, self.config.fridge_speaker_service, "fridge", "fridge", payload)
        return self._record("fridge", {**payload, "stale_status": stale}, True, message)

    def _maybe_start_video_followup(self, door, first_message, effective_config, payload):
        mode = str(getattr(effective_config, "doorbell_video_mode", "fast") or "fast").lower()
        if mode in {"fast", "manual"}:
            return
        if mode == "smart" and not vision.description_needs_live_followup(first_message):
            self._record("doorbell_video", {**payload, "door": door, "mode": mode}, True, "Smart live video follow-up skipped; still image was clear.")
            return
        thread = threading.Thread(
            target=self._background_doorbell_video,
            args=(door, mode, getattr(effective_config, "doorbell_live_video_seconds", 4)),
            daemon=True,
        )
        thread.start()

    def _background_doorbell_video(self, door, mode, seconds):
        try:
            self._handle_doorbell_video({"door": door, "seconds": seconds, "mode": mode, "source": f"automatic_{mode}"})
        except Exception:
            LOGGER.exception("Doorbell live video follow-up failed.")

    def _handle_vacuum(self, payload):
        event = _clean(payload.get("event") or payload.get("state") or "status")
        error = str(payload.get("error") or "").strip()
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        announce_events = set(getattr(effective_config, "vacuum_announce_events", []) or [])
        if announce_events and event not in announce_events and event != "error":
            return self._record("vacuum", payload, True, f"Logged Cinderella {event.replace('_', ' ')} without announcement.")
        dedupe_key = f"vacuum:{event}:{error.lower()}"
        quiet_seconds = int(getattr(effective_config, "vacuum_repeat_quiet_minutes", 20) or 20) * 60
        if self._is_duplicate(dedupe_key, seconds=quiet_seconds):
            return self._record("vacuum", payload, True, f"Ignored repeated Cinderella {event.replace('_', ' ')} update.", duplicate=True)
        message = _vacuum_message(event, error)
        if error:
            message = f"{message} {error}"
        self._notify("Viper Vacuum", message, self.config.vacuum_speaker_service, "utilities", "vacuum", payload)
        return self._record("vacuum", payload, True, message)

    def _handle_ice_maker(self, payload):
        action = _clean(payload.get("action") or payload.get("event") or "status")
        message = f"Ice maker {action.replace('_', ' ')}."
        self._notify("Viper Ice Maker", message, "", "utilities", "ice_maker", payload)
        return self._record("ice_maker", payload, True, message)

    def _handle_hvac(self, payload):
        unit = str(payload.get("unit") or payload.get("entity_id") or "Heat pump").strip()
        state = str(payload.get("state") or payload.get("action") or "status changed").strip()
        message = f"{unit}: {state}."
        self._notify("Viper Heat Pump", message, "", "utilities", "hvac", payload)
        return self._record("hvac", payload, True, message)

    def _handle_broadcast(self, payload):
        message = str(payload.get("message") or payload.get("broadcast_text") or "").strip()
        if not message:
            return self._record("broadcast", payload, False, "Broadcast ignored: no message provided.")
        channel = _clean(payload.get("channel") or "utilities")
        category = "fridge" if channel.startswith(("fridge", "freezer")) else "utilities"
        title = "Viper Broadcast"
        if payload.get("push"):
            title = "Viper Broadcast Push"
        self._notify(title, message, "", category, "broadcast", payload)
        return self._record("broadcast", payload, True, message)

    def _handle_chime(self, payload):
        filename = str(payload.get("filename") or "").strip()
        category = _clean(payload.get("category") or "utilities")
        if not filename:
            return self._record("chime", payload, False, "Chime test ignored: no file selected.")
        if self.control_state and filename not in self.control_state.available_chimes():
            return self._record("chime", payload, False, f"Chime {filename} is not uploaded.")
        if not self._play_chime(filename, category):
            return self._record("chime", payload, False, f"Chime {filename} could not be played by any enabled speaker.")
        return self._record("chime", payload, True, f"Tested chime {filename}.")

    def _handle_pushover_test(self, payload):
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        title = str(payload.get("title") or "Viper Core Test").strip()
        message = str(payload.get("message") or "Viper Core Pushover is working.").strip()
        if not getattr(effective_config, "pushover_user_key", "") or not getattr(effective_config, "pushover_api_token", ""):
            return self._record("pushover_test", payload, False, "Pushover test failed: Pushover keys are not configured.")
        try:
            _send_pushover(effective_config.pushover_api_token, effective_config.pushover_user_key, title, message)
        except Exception as exc:
            return self._record("pushover_test", payload, False, f"Pushover test failed: {exc}")
        return self._record("pushover_test", payload, True, "Pushover test sent.")

    def _notify(self, title, message, speaker_service, category, event_type="", payload=None):
        event_payload = payload if isinstance(payload, dict) else {}
        if not self.ha.available():
            LOGGER.warning("Skipping HA notification because HA is not configured: %s", message)
            return
        if self.control_state and self.control_state.public_state().get("global_mute"):
            LOGGER.info("Global mute is on. Logged without audio: %s", message)
            return
        service = speaker_service or self.config.notification_service
        service_payload = _service_payload(service, title, message, self.config.speaker_targets)
        try:
            self.ha.call_service(service, service_payload)
        except Exception as exc:
            LOGGER.warning("Primary notification service failed: %s", exc)
            try:
                self.ha.create_notification(title, message)
            except Exception as fallback_exc:
                LOGGER.warning("Fallback persistent notification failed: %s", fallback_exc)
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        if self.config.pushover_service and _push_allowed(event_type):
            try:
                self.ha.call_service(self.config.pushover_service, {"title": title, "message": message})
            except Exception as exc:
                LOGGER.warning("Pushover service failed: %s", exc)
        if _push_allowed(event_type) and getattr(effective_config, "pushover_user_key", "") and getattr(effective_config, "pushover_api_token", ""):
            try:
                _send_pushover(effective_config.pushover_api_token, effective_config.pushover_user_key, title, message)
            except Exception as exc:
                LOGGER.warning("Direct Pushover failed: %s", exc)
        chime_played = self._play_event_chime(event_type, event_payload, category)
        if event_type == "fridge" and chime_played:
            LOGGER.info("Skipping refrigerator speech because chime handled %s.", message)
            return
        self._speak(title, message, category)

    def _play_event_chime(self, event_type, payload, category):
        if not self.control_state:
            return False
        chime = self.control_state.chime_for_event(event_type, payload)
        if not chime:
            LOGGER.info("No chime assigned for %s event payload %s.", event_type, payload)
            return False
        return self._play_chime(chime, category)

    def _play_chime(self, chime, category):
        route_targets = self._route_targets(category)
        url = self._chime_url(chime)
        media_source = _chime_media_source(chime)
        if route_targets["sonos"] and not url:
            LOGGER.warning("Cannot play chime %s on direct Sonos because external_base_url is not configured.", chime)
        sent = False
        for target in route_targets["sonos"] if url else []:
            try:
                _play_sonos_url(target, url)
                sent = True
            except Exception as exc:
                LOGGER.warning("Direct Sonos chime failed: %s", exc)
        for target in route_targets["ha"]:
            try:
                self.ha.call_service(
                    "media_player/play_media",
                    {
                        "entity_id": target,
                        "media_content_id": media_source,
                        "media_content_type": _chime_media_type(chime),
                    },
                )
                sent = True
            except Exception as exc:
                LOGGER.warning("HA chime playback failed: %s", exc)
        if sent:
            LOGGER.info(
                "Sent chime %s to %s Home Assistant speaker(s) and %s Sonos speaker(s).",
                chime,
                len(route_targets["ha"]),
                len(route_targets["sonos"]),
            )
        else:
            LOGGER.warning("Chime %s was assigned but no compatible speakers were enabled for %s.", chime, category)
        return sent

    def _chime_url(self, chime):
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        base = effective_config.external_base_url
        if not base:
            return ""
        return f"{base.rstrip('/')}/chimes/{urllib.parse.quote(str(chime), safe='')}"

    def _speak(self, title, message, category):
        if not self.ha.available():
            return
        route_targets = self._route_targets(category)
        tts_targets = route_targets["ha"]
        sonos_targets = route_targets["sonos"]
        alexa_targets = route_targets["alexa"]
        LOGGER.info(
            "Speaking %s through %s Home Assistant, %s Sonos, and %s Alexa target(s).",
            category,
            len(tts_targets),
            len(sonos_targets),
            len(alexa_targets),
        )
        if self.config.tts_service and tts_targets:
            try:
                self.ha.call_service(
                    self.config.tts_service,
                    {"entity_id": tts_targets, "message": message},
                )
            except Exception as exc:
                LOGGER.warning("TTS service failed: %s", exc)
        if sonos_targets:
            try:
                payload = self.ha.tts_get_url(message)
                media_url = payload.get("url") if isinstance(payload, dict) else ""
                if not media_url:
                    raise RuntimeError("Home Assistant did not return a TTS media URL.")
                for target in sonos_targets:
                    _play_sonos_url(target, media_url)
            except Exception as exc:
                LOGGER.warning("Direct Sonos playback failed: %s", exc)
        if self.config.alexa_notify_service and alexa_targets:
            try:
                self.ha.call_service(
                    self.config.alexa_notify_service,
                    {
                        "message": message,
                        "title": title,
                        "target": alexa_targets,
                        "data": {"type": "announce"},
                    },
                )
            except Exception as exc:
                LOGGER.warning("Alexa announce failed: %s", exc)

    def _route_targets(self, category):
        if self.control_state:
            return self.control_state.speaker_targets(category)
        return {
            "ha": list(self.config.tts_targets or []),
            "sonos": list(self.config.direct_sonos_targets or []),
            "alexa": list(self.config.alexa_targets or []),
        }

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

    def _fridge_stale_status(self):
        if not self.ha.available():
            return {"stale": True, "message": "Home Assistant is not available."}
        threshold = 45
        if self.control_state:
            threshold = int(getattr(self.control_state.effective_config(self.config), "fridge_stale_minutes", 45) or 45)
        newest = None
        newest_entity = ""
        for entity_id in ("binary_sensor.refrigerator_fridge_door", "binary_sensor.refrigerator_freezer_door"):
            try:
                state = self.ha.get_state(entity_id) or {}
            except Exception as exc:
                return {"stale": True, "message": f"Could not read {entity_id}: {exc}"}
            changed = _parse_ha_time(state.get("last_changed") or state.get("last_updated"))
            if changed and (newest is None or changed > newest):
                newest = changed
                newest_entity = entity_id
        if not newest:
            return {"stale": True, "message": "Door sensors did not include update times."}
        age_minutes = int((datetime.now(timezone.utc) - newest).total_seconds() / 60)
        return {
            "stale": age_minutes > threshold,
            "age_minutes": age_minutes,
            "threshold_minutes": threshold,
            "entity_id": newest_entity,
        }


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


def _chime_media_type(filename):
    suffix = str(filename or "").rsplit(".", 1)[-1].lower()
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }.get(suffix, "audio/mpeg")


def _chime_media_source(filename):
    return f"media-source://media_source/local/viper_core_chimes/{urllib.parse.quote(str(filename or ''), safe='')}"


def _play_sonos_url(host, media_url):
    host = str(host or "").strip()
    media_url = str(media_url or "").strip()
    if not host or not media_url:
        return
    endpoint = f"http://{host}:1400/MediaRenderer/AVTransport/Control"
    escaped_url = escape(media_url, quote=True)
    _sonos_soap(
        endpoint,
        "SetAVTransportURI",
        (
            '<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            "<InstanceID>0</InstanceID>"
            f"<CurrentURI>{escaped_url}</CurrentURI>"
            "<CurrentURIMetaData></CurrentURIMetaData>"
            "</u:SetAVTransportURI>"
        ),
    )
    _sonos_soap(
        endpoint,
        "Play",
        (
            '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            "<InstanceID>0</InstanceID><Speed>1</Speed>"
            "</u:Play>"
        ),
    )


def _sonos_soap(endpoint, action, body):
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=envelope,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        response.read()


def _send_pushover(token, user, title, message):
    payload = urllib.parse.urlencode(
        {
            "token": str(token or ""),
            "user": str(user or ""),
            "title": str(title or "Viper Core"),
            "message": str(message or ""),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def _clean(value):
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _title(value):
    return str(value or "").replace("_", " ").title()


def _push_allowed(event_type):
    return _clean(event_type) not in {"fridge"}


def _parse_ha_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _vacuum_message(event, error=""):
    labels = {
        "departure": "Cinderella started cleaning.",
        "washing": "Cinderella is washing the mop.",
        "emptying": "Cinderella is emptying the bin.",
        "returning": "Cinderella is heading back to the dock.",
        "victory": "Cinderella is back at the dock.",
        "paused": "Cinderella is paused.",
        "drying": "Cinderella is drying the mop.",
        "status_update": "Cinderella status changed.",
        "error": "Cinderella needs attention.",
    }
    return labels.get(event, f"Cinderella {event.replace('_', ' ')}.")
