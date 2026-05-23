import asyncio
import json
import logging
import threading
import time
from copy import deepcopy


DEFAULT_ACTIVE_STATES = ["on", "true", "detected", "motion", "ding", "pressed", "open"]
INACTIVE_STATES = {"", "off", "false", "idle", "closed", "clear", "none", "unknown", "unavailable"}
ERROR_CLEAR_STATES = {"", "none", "ok", "no_error", "no error", "unknown", "unavailable", "0"}

CINDERELLA_STATUS_EVENT_MAP = {
    "starting": "departure",
    "cleaning": "departure",
    "spot_cleaning": "departure",
    "zoned_cleaning": "departure",
    "zone_cleaning": "departure",
    "segment_cleaning": "departure",
    "mapping": "departure",
    "patrol": "departure",
    "robot_status_mopping": "departure",
    "clean_mop_cleaning": "departure",
    "clean_mop_mopping": "departure",
    "segment_mopping": "departure",
    "segment_clean_mop_cleaning": "departure",
    "segment_clean_mop_mopping": "departure",
    "zoned_mopping": "departure",
    "zoned_clean_mop_cleaning": "departure",
    "zoned_clean_mop_mopping": "departure",
    "washing_mop": "washing",
    "washing_the_mop": "washing",
    "washing_the_mop_2": "washing",
    "back_to_dock_washing_duster": "washing",
    "emptying": "emptying",
    "emptying_bin": "emptying",
    "emptying_dustbin": "emptying",
    "emptying_the_bin": "emptying",
    "returning": "returning",
    "returning_home": "returning",
    "docking": "returning",
    "going_to_target": "returning",
    "going_to_wash_the_mop": "returning",
    "attaching_the_mop": "returning",
    "detaching_the_mop": "returning",
    "air_drying_stopping": "returning",
    "charging": "victory",
    "docked": "victory",
    "charging_complete": "victory",
    "idle": "victory",
    "paused": "paused",
    "remote_control_active": "paused",
    "manual_mode": "paused",
    "in_call": "paused",
    "locked": "paused",
    "unknown": "status_update",
    "charger_disconnected": "status_update",
    "charging_problem": "status_update",
    "error": "status_update",
    "shutting_down": "status_update",
    "updating": "status_update",
    "device_offline": "status_update",
    "egg_attack": "status_update",
}


def state_text(state_obj):
    if not isinstance(state_obj, dict):
        return ""
    return str(state_obj.get("state", "")).strip()


def normalize_state(value):
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def default_doorbell_trigger(side, config):
    side = "back" if side == "back" else "front"
    return {
        "enabled": False,
        "source": "ha_state",
        "trigger_entity_id": "",
        "active_states": list(DEFAULT_ACTIVE_STATES),
        "rtsp_url": "",
        "camera_id": "",
        "mqtt_topic": "",
    }


def normalize_doorbell_trigger(raw, side, config):
    fallback = default_doorbell_trigger(side, config)
    current = raw if isinstance(raw, dict) else {}
    merged = {**fallback, **current}
    source = str(merged.get("source") or "ha_state").strip().lower()
    if source not in {"ha_state", "mqtt", "webhook"}:
        source = "ha_state"
    states = merged.get("active_states")
    if not isinstance(states, list):
        states = fallback["active_states"]
    active_states = [str(item).strip().lower() for item in states if str(item).strip()]
    return {
        "enabled": bool(merged.get("enabled")),
        "source": source,
        "trigger_entity_id": str(merged.get("trigger_entity_id") or "").strip(),
        "active_states": active_states or list(DEFAULT_ACTIVE_STATES),
        "rtsp_url": str(merged.get("rtsp_url") or "").strip(),
        "camera_id": str(merged.get("camera_id") or "").strip(),
        "mqtt_topic": str(merged.get("mqtt_topic") or "").strip(),
    }


def normalize_doorbell_triggers(config):
    config = config if isinstance(config, dict) else {}
    raw = config.get("doorbell_triggers") if isinstance(config.get("doorbell_triggers"), dict) else {}
    return {
        "front": normalize_doorbell_trigger(raw.get("front"), "front", config),
        "back": normalize_doorbell_trigger(raw.get("back"), "back", config),
    }


def doorbell_state_is_active(state, active_states=None):
    normalized = normalize_state(state)
    active = {normalize_state(item) for item in (active_states or DEFAULT_ACTIVE_STATES)}
    return normalized in active


def route_state_change(config, entity_id, old_state, new_state):
    """Convert a Home Assistant state_changed event into Viper actions."""
    entity_id = str(entity_id or "").strip()
    old_value = state_text(old_state)
    new_value = state_text(new_state)
    old_norm = normalize_state(old_value)
    new_norm = normalize_state(new_value)
    actions = []

    for side, trigger in normalize_doorbell_triggers(config).items():
        if not trigger.get("enabled") or trigger.get("source") != "ha_state":
            continue
        if entity_id != trigger.get("trigger_entity_id"):
            continue
        active_states = trigger.get("active_states")
        if doorbell_state_is_active(new_value, active_states) and not doorbell_state_is_active(old_value, active_states):
            actions.append(
                {
                    "type": "doorbell",
                    "side": side,
                    "location": "back door" if side == "back" else "front door",
                    "rtsp_url": trigger.get("rtsp_url") or "",
                }
            )

    if bool(config.get("cinderella_enabled", True)):
        if entity_id == "sensor.cinderella_status" and new_norm != old_norm:
            event = CINDERELLA_STATUS_EVENT_MAP.get(new_norm)
            if event:
                actions.append({"type": "cinderella", "event": event, "error": "", "source": "vacuum"})
        elif entity_id == "sensor.cinderella_vacuum_error" and new_norm not in ERROR_CLEAR_STATES and new_norm != old_norm:
            actions.append({"type": "cinderella", "event": "error", "error": new_norm, "source": "vacuum"})
        elif entity_id == "sensor.cinderella_dock_dock_error" and new_norm not in ERROR_CLEAR_STATES and new_norm != old_norm:
            actions.append({"type": "cinderella", "event": "error", "error": new_norm, "source": "dock"})
        elif entity_id == "binary_sensor.cinderella_dock_mop_drying" and new_norm == "on" and old_norm != "on":
            actions.append({"type": "cinderella", "event": "drying", "error": "", "source": "dock"})

    fridge_messages = {
        "binary_sensor.refrigerator_fridge_door": {
            "open": ("fridge_open", "The refrigerator door is open."),
            "on": ("fridge_open", "The refrigerator door is open."),
            "closed": ("fridge_closed", "The refrigerator door is closed."),
            "off": ("fridge_closed", "The refrigerator door is closed."),
        },
        "binary_sensor.refrigerator_freezer_door": {
            "open": ("freezer_open", "The freezer door is open."),
            "on": ("freezer_open", "The freezer door is open."),
            "closed": ("freezer_closed", "The freezer door is closed."),
            "off": ("freezer_closed", "The freezer door is closed."),
        },
    }
    if entity_id in fridge_messages and new_norm != old_norm:
        match = fridge_messages[entity_id].get(new_norm)
        if match:
            channel, message = match
            actions.append({"type": "broadcast", "channel": channel, "message": message})

    return actions


def websocket_url(ha_ip, ha_port):
    host = str(ha_ip or "").strip()
    port = str(ha_port or "8123").strip()
    return f"ws://{host}:{port}/api/websocket"


class HomeAssistantEventListener:
    def __init__(self, config_provider, action_handler, status_handler=None, stop_event=None):
        self.config_provider = config_provider
        self.action_handler = action_handler
        self.status_handler = status_handler
        self.stop_event = stop_event or threading.Event()
        self.thread = None
        self._status = {
            "running": False,
            "connected": False,
            "last_error": "",
            "last_event_at": 0.0,
            "last_action_at": 0.0,
            "last_host": "",
        }
        self._status_lock = threading.Lock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._thread_main, name="ViperHAListener", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def status(self):
        with self._status_lock:
            return deepcopy(self._status)

    def _set_status(self, **updates):
        with self._status_lock:
            self._status.update(updates)
            snapshot = deepcopy(self._status)
        if self.status_handler:
            try:
                self.status_handler(snapshot)
            except Exception:
                logging.debug("HA listener status handler failed", exc_info=True)

    def _thread_main(self):
        try:
            asyncio.run(self._run_forever())
        except Exception as e:
            logging.exception("[HA LISTENER] stopped unexpectedly: %s", e)
            self._set_status(running=False, connected=False, last_error=str(e))

    async def _run_forever(self):
        backoff = 2
        self._set_status(running=True, connected=False, last_error="")
        while not self.stop_event.is_set():
            config = self.config_provider() or {}
            if not config.get("ha_listener_enabled", True):
                self._set_status(running=True, connected=False, last_error="listener disabled")
                await asyncio.sleep(2)
                continue
            ha_ip = str(config.get("ha_ip") or "").strip()
            ha_port = str(config.get("ha_port") or "8123").strip()
            token = str(config.get("ha_token") or "").strip()
            if not ha_ip or not token:
                self._set_status(running=True, connected=False, last_error="missing Home Assistant host or token")
                await asyncio.sleep(5)
                continue
            try:
                await self._connect_and_listen(ha_ip, ha_port, token)
                backoff = 2
            except Exception as e:
                logging.warning("[HA LISTENER] connection failed: %s", e)
                self._set_status(connected=False, last_error=str(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self._set_status(running=False, connected=False)

    async def _recv_json(self, ws):
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=30))

    async def _connect_and_listen(self, ha_ip, ha_port, token):
        import websockets

        url = websocket_url(ha_ip, ha_port)
        self._set_status(last_host=f"{ha_ip}:{ha_port}", last_error="")
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
            auth_required = await self._recv_json(ws)
            if auth_required.get("type") != "auth_required":
                raise RuntimeError("Home Assistant did not request WebSocket authentication.")
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth_result = await self._recv_json(ws)
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(auth_result.get("message") or "Home Assistant WebSocket authentication failed.")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            subscribe_result = await self._recv_json(ws)
            if not subscribe_result.get("success"):
                raise RuntimeError(subscribe_result.get("error", {}).get("message") or "state_changed subscribe failed.")
            logging.info("[HA LISTENER] connected to %s", url)
            self._set_status(connected=True, last_error="")

            while not self.stop_event.is_set():
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                payload = json.loads(message)
                self._handle_ws_payload(payload)

    def _handle_ws_payload(self, payload):
        if payload.get("type") != "event":
            return
        event = payload.get("event") or {}
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data") or {}
        entity_id = data.get("entity_id") or ""
        old_state = data.get("old_state") or {}
        new_state = data.get("new_state") or {}
        config = self.config_provider() or {}
        self._set_status(last_event_at=time.time())
        for action in route_state_change(config, entity_id, old_state, new_state):
            logging.info("[HA LISTENER] routed entity=%s action=%s", entity_id, action)
            self._set_status(last_action_at=time.time())
            self.action_handler(action)
