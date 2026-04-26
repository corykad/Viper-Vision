import re
import time
from collections import OrderedDict

import paho.mqtt.client as mqtt

import viper_config as cfg


RING_TOPIC_ROOT = "ring/#"
RING_EVENT_HINTS = ("/motion/state", "/ding/state", "/doorbell/state")
CAMERA_ID_RE = re.compile(r"/camera/([^/]+)/")
RING_ROOT_RE = re.compile(r"^ring/([^/]+)/camera/", re.IGNORECASE)


def test_mqtt_connection(
    *,
    mqtt_host=None,
    mqtt_port=1883,
    mqtt_username="",
    mqtt_password="",
    topic=RING_TOPIC_ROOT,
    timeout=8,
):
    """Connect to MQTT and subscribe to the Ring root topic."""
    host = mqtt_host or cfg.get_ha_settings().get("ha_ip")
    if not host:
        return _error("missing_host", "Enter the Home Assistant or MQTT host first.")

    status = {"connected": False, "rc": None}
    client = mqtt.Client()
    if mqtt_username:
        client.username_pw_set(mqtt_username, mqtt_password or None)

    def on_connect(_client, _userdata, _flags, rc):
        status["rc"] = rc
        if rc == 0:
            status["connected"] = True
            _client.subscribe(topic)

    client.on_connect = on_connect
    try:
        client.connect(host, int(mqtt_port), keepalive=30)
    except OSError as e:
        return _error("unreachable", f"Could not connect to MQTT at {host}:{mqtt_port}.", exception=str(e))
    except Exception as e:
        return _error("mqtt_connect_failed", "MQTT connection failed.", exception=str(e))

    client.loop_start()
    try:
        end = time.monotonic() + max(2, int(timeout))
        while time.monotonic() < end and status["rc"] is None:
            time.sleep(0.1)
    finally:
        client.loop_stop()
        client.disconnect()

    if not status["connected"]:
        rc = status["rc"]
        if rc == 4:
            return _error("bad_mqtt_credentials", "MQTT rejected the username or password.", rc=rc)
        if rc == 5:
            return _error("not_authorized", "MQTT connection was not authorized.", rc=rc)
        return _error("mqtt_not_connected", "MQTT did not accept the connection.", rc=rc)

    return {
        "ok": True,
        "error": None,
        "message": f"Connected to MQTT at {host}:{mqtt_port}.",
        "topic_root": topic,
    }


def listen_for_ring_topics(
    *,
    mqtt_host=None,
    mqtt_port=1883,
    mqtt_username="",
    mqtt_password="",
    topic=RING_TOPIC_ROOT,
    duration=None,
    rtsp_host=None,
    stop_event=None,
    stop_on_first=True,
):
    """Listen for Ring MQTT activity and suggest Viper doorbell settings.

    Intended for a setup wizard worker thread. Ask the user to walk to the
    door or press the doorbell while this runs.
    """
    host = mqtt_host or cfg.get_ha_settings().get("ha_ip")
    if not host:
        return _error("missing_host", "Enter the Home Assistant host before listening for Ring topics.")

    events = OrderedDict()
    found = False
    client = mqtt.Client()
    if mqtt_username:
        client.username_pw_set(mqtt_username, mqtt_password or None)

    def on_connect(_client, _userdata, _flags, rc):
        if rc == 0:
            _client.subscribe(topic)

    def on_message(_client, _userdata, message):
        nonlocal found
        payload = _decode_payload(message.payload)
        parsed = parse_ring_topic(message.topic, payload, rtsp_host=rtsp_host or host)
        if parsed["is_candidate"]:
            events[message.topic] = parsed
            found = True
            if stop_on_first and stop_event:
                stop_event.set()

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, int(mqtt_port), keepalive=30)
    except OSError as e:
        return _error("unreachable", f"Could not connect to MQTT at {host}:{mqtt_port}.", exception=str(e))
    except Exception as e:
        return _error("mqtt_connect_failed", "MQTT connection failed.", exception=str(e))

    client.loop_start()
    try:
        end = time.monotonic() + max(5, int(duration)) if duration else None
        while True:
            if stop_event and stop_event.is_set():
                break
            if stop_on_first and found:
                break
            if end and time.monotonic() >= end:
                break
            time.sleep(0.2)
    finally:
        client.loop_stop()
        client.disconnect()

    suggestions = list(events.values())
    return {
        "ok": True,
        "error": None,
        "topic_root": topic,
        "duration": duration,
        "cancelled": bool(stop_event and stop_event.is_set() and not found),
        "suggestions": suggestions,
        "count": len(suggestions),
    }


def parse_ring_topic(topic, payload="", *, rtsp_host=None):
    text_topic = str(topic or "")
    lowered = text_topic.lower()
    camera_id = _camera_id_from_topic(text_topic)
    ring_topic_root = ring_root_from_topic(text_topic)
    is_event_topic = any(hint in lowered for hint in RING_EVENT_HINTS)
    payload_text = str(payload or "").strip()
    active_payload = payload_text.upper() in {"ON", "TRUE", "1", "MOTION", "DING"}
    is_candidate = lowered.startswith("ring/") and is_event_topic and (active_payload or camera_id)
    rtsp_url = f"rtsp://{rtsp_host}:8554/{camera_id}_live" if rtsp_host and camera_id else ""
    return {
        "topic": text_topic,
        "payload": payload_text,
        "camera_id": camera_id,
        "ring_topic_root": ring_topic_root,
        "rtsp_url": rtsp_url,
        "event_type": _event_type(lowered),
        "is_candidate": is_candidate,
    }


def suggest_front_back(suggestions):
    """Return first two unique suggestions as front/back defaults."""
    unique = []
    seen = set()
    for item in suggestions or []:
        topic = item.get("topic")
        if topic and topic not in seen:
            seen.add(topic)
            unique.append(item)
    return {
        "front": unique[0] if len(unique) >= 1 else None,
        "back": unique[1] if len(unique) >= 2 else None,
    }


def _camera_id_from_topic(topic):
    match = CAMERA_ID_RE.search(str(topic or ""))
    return match.group(1) if match else ""


def ring_root_from_topic(topic):
    match = RING_ROOT_RE.search(str(topic or ""))
    return match.group(1) if match else ""


def _event_type(topic):
    for event in ("motion", "ding", "doorbell"):
        if f"/{event}/state" in topic:
            return event
    return ""


def _decode_payload(payload):
    try:
        return payload.decode("utf-8", errors="replace")
    except Exception:
        return str(payload)


def _error(error, message, **extra):
    result = {"ok": False, "error": error, "message": message}
    result.update(extra)
    return result
