import asyncio
import json
import logging
import threading
import time
from copy import deepcopy
from urllib import request as urlrequest

import viper_health


DEFAULT_ACTIVE_STATES = ["on", "true", "detected", "motion", "ding", "pressed", "open"]
INACTIVE_STATES = {"", "off", "false", "idle", "closed", "clear", "none", "unknown", "unavailable"}
ERROR_CLEAR_STATES = {"", "none", "ok", "no_error", "no error", "unknown", "unavailable", "0"}
FRIDGE_POLL_INTERVAL_SECONDS = 30
POLL_RECONNECT_FAILURE_LIMIT = 3
CRITICAL_HEALTH_INTERVAL_SECONDS = 5 * 60

FRIDGE_DOOR_MESSAGES = {
    "binary_sensor.refrigerator_fridge_door": {
        "open": ("fridge_open", "The refrigerator door is open."),
        "opened": ("fridge_open", "The refrigerator door is open."),
        "on": ("fridge_open", "The refrigerator door is open."),
        "true": ("fridge_open", "The refrigerator door is open."),
        "detected": ("fridge_open", "The refrigerator door is open."),
        "closed": ("fridge_closed", "The refrigerator door is closed."),
        "off": ("fridge_closed", "The refrigerator door is closed."),
        "false": ("fridge_closed", "The refrigerator door is closed."),
        "clear": ("fridge_closed", "The refrigerator door is closed."),
    },
    "binary_sensor.refrigerator_freezer_door": {
        "open": ("freezer_open", "The freezer door is open."),
        "opened": ("freezer_open", "The freezer door is open."),
        "on": ("freezer_open", "The freezer door is open."),
        "true": ("freezer_open", "The freezer door is open."),
        "detected": ("freezer_open", "The freezer door is open."),
        "closed": ("freezer_closed", "The freezer door is closed."),
        "off": ("freezer_closed", "The freezer door is closed."),
        "false": ("freezer_closed", "The freezer door is closed."),
        "clear": ("freezer_closed", "The freezer door is closed."),
    },
}
FRIDGE_OPEN_STATES = {"open", "opened", "on", "true", "detected"}
FRIDGE_CLOSED_STATES = {"closed", "off", "false", "clear"}

CINDERELLA_DEFAULT_ENTITIES = {
    "status": "sensor.cinderella_status",
    "vacuum_error": "sensor.cinderella_vacuum_error",
    "dock_error": "sensor.cinderella_dock_dock_error",
    "mop_drying": "binary_sensor.cinderella_dock_mop_drying",
}
CINDERELLA_ENTITY_CONFIG_KEYS = {
    "status": "cinderella_status_entity",
    "vacuum_error": "cinderella_vacuum_error_entity",
    "dock_error": "cinderella_dock_error_entity",
    "mop_drying": "cinderella_mop_drying_entity",
}
CINDERELLA_DRYING_ACTIVE_STATES = {"on", "true", "detected", "open", "drying"}

CINDERELLA_STATUS_EVENT_MAP = {
    "starting": "departure",
    "cleaning": "departure",
    "vacuuming": "departure",
    "mopping": "departure",
    "spot_cleaning": "departure",
    "spot_clean": "departure",
    "zoned_cleaning": "departure",
    "zone_cleaning": "departure",
    "room_cleaning": "departure",
    "room_clean": "departure",
    "segment_clean": "departure",
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
    "mop_washing": "washing",
    "cleaning_mop": "washing",
    "back_to_dock_washing_duster": "washing",
    "emptying": "emptying",
    "emptying_bin": "emptying",
    "emptying_dustbin": "emptying",
    "emptying_the_bin": "emptying",
    "returning": "returning",
    "returning_home": "returning",
    "returning_to_dock": "returning",
    "returning_to_base": "returning",
    "docking": "returning",
    "going_to_dock": "returning",
    "going_to_base": "returning",
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


def fridge_door_state_kind(value):
    """Reduce HA's many door state spellings to the transitions that matter."""
    normalized = normalize_state(value)
    if normalized in FRIDGE_OPEN_STATES:
        return "open"
    if normalized in FRIDGE_CLOSED_STATES:
        return "closed"
    return "unknown"


def default_doorbell_trigger(side, config):
    side = "back" if side == "back" else "front"
    return {
        "enabled": False,
        "source": "ha_state",
        "trigger_entity_id": "",
        "trigger_entity_ids": [],
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
    primary_entity_id = str(merged.get("trigger_entity_id") or "").strip()
    raw_entity_ids = merged.get("trigger_entity_ids")
    if isinstance(raw_entity_ids, str):
        raw_entity_ids = [raw_entity_ids]
    elif not isinstance(raw_entity_ids, list):
        raw_entity_ids = []
    trigger_entity_ids = []
    for candidate in [primary_entity_id, *raw_entity_ids]:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in trigger_entity_ids:
            trigger_entity_ids.append(candidate)
    return {
        "enabled": bool(merged.get("enabled")),
        "source": source,
        "trigger_entity_id": primary_entity_id,
        "trigger_entity_ids": trigger_entity_ids,
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


def cinderella_entities(config):
    config = config if isinstance(config, dict) else {}
    entities = {}
    for role, default in CINDERELLA_DEFAULT_ENTITIES.items():
        value = str(config.get(CINDERELLA_ENTITY_CONFIG_KEYS[role]) or default).strip()
        if value:
            entities[role] = value
    return entities


def route_cinderella_state_change(config, entity_id, old_norm, new_norm):
    if not bool(config.get("cinderella_enabled", True)) or new_norm == old_norm:
        return []

    entities = cinderella_entities(config)
    actions = []
    if entity_id == entities.get("status"):
        event = CINDERELLA_STATUS_EVENT_MAP.get(new_norm)
        if event:
            actions.append({"type": "cinderella", "event": event, "error": "", "source": "vacuum"})
    elif entity_id == entities.get("vacuum_error") and new_norm not in ERROR_CLEAR_STATES:
        actions.append({"type": "cinderella", "event": "error", "error": new_norm, "source": "vacuum"})
    elif entity_id == entities.get("dock_error") and new_norm not in ERROR_CLEAR_STATES:
        actions.append({"type": "cinderella", "event": "error", "error": new_norm, "source": "dock"})
    elif entity_id == entities.get("mop_drying") and new_norm in CINDERELLA_DRYING_ACTIVE_STATES and old_norm not in CINDERELLA_DRYING_ACTIVE_STATES:
        actions.append({"type": "cinderella", "event": "drying", "error": "", "source": "dock"})
    return actions


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
        trigger_entity_ids = trigger.get("trigger_entity_ids") or [trigger.get("trigger_entity_id")]
        if entity_id not in trigger_entity_ids:
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

    actions.extend(route_cinderella_state_change(config, entity_id, old_norm, new_norm))

    if entity_id in FRIDGE_DOOR_MESSAGES:
        old_kind = fridge_door_state_kind(old_norm)
        new_kind = fridge_door_state_kind(new_norm)
        if old_kind == new_kind:
            return actions
        if new_kind == "closed" and old_kind != "open":
            return actions
        match = FRIDGE_DOOR_MESSAGES[entity_id].get(new_norm)
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
        self._fridge_states = {}
        self._cinderella_states = {}
        self._status = {
            "running": False,
            "connected": False,
            "last_error": "",
            "last_event_at": 0.0,
            "last_action_at": 0.0,
            "last_host": "",
            "last_fridge_poll_at": 0.0,
            "last_cinderella_poll_at": 0.0,
            "last_event_entity": "",
            "last_event_old_state": "",
            "last_event_new_state": "",
            "last_event_old_normalized": "",
            "last_event_new_normalized": "",
            "last_event_action_count": 0,
            "last_routed_action": {},
            "last_connected_at": 0.0,
            "last_reconnect_at": 0.0,
            "reconnect_count": 0,
            "poll_failure_count": 0,
            "last_successful_poll_at": 0.0,
            "last_poll_error": "",
            "last_critical_health_check_at": 0.0,
            "critical_health_status": "",
            "critical_health_message": "",
            "last_smartthings_reload_at": 0.0,
            "last_smartthings_reload_result": "",
            "smartthings_reload_count": 0,
            "last_health_journal_event": {},
            "repeated_smartthings_reloads_24h": 0,
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

    def _poll_interval_seconds(self):
        try:
            config = self.config_provider() or {}
            interval = int(config.get("ha_fridge_poll_interval_seconds", FRIDGE_POLL_INTERVAL_SECONDS))
            return max(10, min(interval, 300))
        except Exception:
            return FRIDGE_POLL_INTERVAL_SECONDS

    def _smartthings_stale_seconds(self):
        try:
            config = self.config_provider() or {}
            minutes = int(config.get("ha_smartthings_stale_minutes", 90))
            return max(15 * 60, min(minutes * 60, 24 * 60 * 60))
        except Exception:
            return viper_health.DEFAULT_SMARTTHINGS_STALE_SECONDS

    def _smartthings_reload_cooldown_seconds(self):
        try:
            config = self.config_provider() or {}
            minutes = int(config.get("ha_smartthings_reload_cooldown_minutes", 360))
            return max(60 * 60, min(minutes * 60, 24 * 60 * 60))
        except Exception:
            return viper_health.DEFAULT_SMARTTHINGS_RELOAD_COOLDOWN_SECONDS

    def _smartthings_max_reloads_per_day(self):
        try:
            config = self.config_provider() or {}
            return max(1, min(int(config.get("ha_smartthings_max_reloads_per_day", 3)), 12))
        except Exception:
            return 3

    def _smartthings_recovery_enabled(self):
        try:
            config = self.config_provider() or {}
            return bool(config.get("ha_smartthings_recovery_enabled", True))
        except Exception:
            return True

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
                self._set_status(
                    connected=False,
                    last_error=str(e),
                    last_reconnect_at=time.time(),
                    reconnect_count=int(self.status().get("reconnect_count", 0)) + 1,
                )
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
            self._set_status(connected=True, last_error="", last_connected_at=time.time(), poll_failure_count=0, last_poll_error="")
            fridge_poll = await self._refresh_fridge_states(ha_ip, ha_port, token, recover_open=True)
            cinderella_poll = await self._refresh_cinderella_states(ha_ip, ha_port, token, recover_active=True)
            self._record_poll_health(fridge_poll, cinderella_poll)
            await self._run_critical_health_watchdog(ha_ip, ha_port, token)
            next_status_poll = time.monotonic() + self._poll_interval_seconds()
            next_critical_health = time.monotonic() + CRITICAL_HEALTH_INTERVAL_SECONDS

            while not self.stop_event.is_set():
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    if time.monotonic() >= next_status_poll:
                        fridge_poll = await self._refresh_fridge_states(ha_ip, ha_port, token, recover_open=False)
                        cinderella_poll = await self._refresh_cinderella_states(ha_ip, ha_port, token, recover_active=False)
                        self._record_poll_health(fridge_poll, cinderella_poll)
                        self._raise_if_poll_watchdog_tripped()
                        next_status_poll = time.monotonic() + self._poll_interval_seconds()
                    if time.monotonic() >= next_critical_health:
                        await self._run_critical_health_watchdog(ha_ip, ha_port, token)
                        next_critical_health = time.monotonic() + CRITICAL_HEALTH_INTERVAL_SECONDS
                    continue
                payload = json.loads(message)
                self._handle_ws_payload(payload)
                if time.monotonic() >= next_status_poll:
                    fridge_poll = await self._refresh_fridge_states(ha_ip, ha_port, token, recover_open=False)
                    cinderella_poll = await self._refresh_cinderella_states(ha_ip, ha_port, token, recover_active=False)
                    self._record_poll_health(fridge_poll, cinderella_poll)
                    self._raise_if_poll_watchdog_tripped()
                    next_status_poll = time.monotonic() + self._poll_interval_seconds()
                if time.monotonic() >= next_critical_health:
                    await self._run_critical_health_watchdog(ha_ip, ha_port, token)
                    next_critical_health = time.monotonic() + CRITICAL_HEALTH_INTERVAL_SECONDS

    def _record_poll_health(self, *results):
        checked = [item for item in results if item and item.get("checked")]
        if not checked:
            return
        successes = sum(int(item.get("successes", 0)) for item in checked)
        errors = []
        for item in checked:
            errors.extend(item.get("errors", []) or [])
        if successes:
            self._set_status(poll_failure_count=0, last_successful_poll_at=time.time(), last_poll_error="")
            return
        failure_count = int(self.status().get("poll_failure_count", 0)) + 1
        error_text = "; ".join(errors[-4:]) or "all Home Assistant poll reads failed"
        self._set_status(poll_failure_count=failure_count, last_poll_error=error_text)

    def _raise_if_poll_watchdog_tripped(self):
        failures = int(self.status().get("poll_failure_count", 0) or 0)
        if failures >= POLL_RECONNECT_FAILURE_LIMIT:
            raise RuntimeError(
                f"Home Assistant polling failed {failures} consecutive times while websocket was open: "
                f"{self.status().get('last_poll_error') or 'no detail'}"
            )

    def _fetch_ha_state(self, ha_ip, ha_port, token, entity_id):
        url = f"http://{ha_ip}:{ha_port}/api/states/{entity_id}"
        req = urlrequest.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlrequest.urlopen(req, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"state request returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))

    async def _fetch_refrigerator_health_states(self, ha_ip, ha_port, token):
        entities = (
            tuple(FRIDGE_DOOR_MESSAGES.keys())
            + tuple(viper_health.REFRIGERATOR_SUPPORT_ENTITIES)
        )
        states = []
        errors = []
        for entity_id in entities:
            try:
                states.append(await asyncio.to_thread(self._fetch_ha_state, ha_ip, ha_port, token, entity_id))
            except Exception as e:
                errors.append(f"{entity_id}: {e}")
        return states, errors

    async def _run_critical_health_watchdog(self, ha_ip, ha_port, token):
        self._set_status(last_critical_health_check_at=time.time())
        if not self._smartthings_recovery_enabled():
            self._set_status(
                critical_health_status="disabled",
                critical_health_message="SmartThings recovery is turned off in Viper settings.",
            )
            return {"ok": True, "status": "disabled"}
        states, errors = await self._fetch_refrigerator_health_states(ha_ip, ha_port, token)
        if not states:
            message = "; ".join(errors[-4:]) or "Viper could not read refrigerator entities from Home Assistant."
            event = viper_health.record_health_event("critical_health_check", "failed", message, details={"errors": errors[-8:]})
            self._set_status(critical_health_status="ha_read_failed", critical_health_message=message)
            self._set_status(last_health_journal_event=event)
            return {"ok": False, "status": "ha_read_failed", "message": message}
        health = viper_health.refrigerator_event_stream_health(
            states,
            stale_seconds=self._smartthings_stale_seconds(),
        )
        self._set_status(
            critical_health_status=health.get("status") or "",
            critical_health_message=health.get("message") or "",
        )
        if health.get("ok"):
            return health
        if health.get("status") != "door_stream_stale":
            return health

        now = time.time()
        repeat_count = viper_health.count_recent_health_events("smartthings_reload")
        max_reloads = self._smartthings_max_reloads_per_day()
        if repeat_count >= max_reloads:
            message = f"{health.get('message')} Reload skipped because Viper already tried {repeat_count} automatic SmartThings reloads in the last 24 hours."
            event = viper_health.record_health_event(
                "smartthings_reload_skipped",
                "daily_limit",
                message,
                details={"max_reloads_per_day": max_reloads, "reloads_24h": repeat_count, "health": health},
            )
            self._set_status(
                critical_health_message=message,
                last_health_journal_event=event,
                repeated_smartthings_reloads_24h=repeat_count,
            )
            return {**health, "reloaded": False, "daily_limit": True}

        cooldown = self._smartthings_reload_cooldown_seconds()
        last_reload = float(self.status().get("last_smartthings_reload_at") or 0)
        if last_reload and now - last_reload < cooldown:
            remaining = int((cooldown - (now - last_reload)) / 60) + 1
            message = f"{health.get('message')} Reload skipped for cooldown; next automatic reload allowed in about {remaining} minutes."
            event = viper_health.record_health_event(
                "smartthings_reload_skipped",
                "cooldown",
                message,
                details={"remaining_minutes": remaining, "health": health},
            )
            self._set_status(critical_health_message=message, last_health_journal_event=event, repeated_smartthings_reloads_24h=repeat_count)
            return {**health, "reloaded": False, "cooldown": True}

        registry = await viper_health.find_config_entry_for_entity(
            ha_ip,
            ha_port,
            token,
            viper_health.FRIDGE_DOOR_ENTITY,
        )
        if not registry.get("ok"):
            message = f"{health.get('message')} Viper could not find the SmartThings config entry: {registry.get('message')}"
            event = viper_health.record_health_event(
                "smartthings_reload",
                "failed",
                message,
                details={"registry": registry, "health": health},
            )
            self._set_status(critical_health_message=message)
            self._set_status(last_health_journal_event=event)
            return {**health, "reloaded": False, "registry": registry}
        entry_id = registry.get("config_entry_id")
        reload_result = await asyncio.to_thread(
            viper_health.reload_config_entry,
            ha_ip,
            ha_port,
            token,
            entry_id,
        )
        count = int(self.status().get("smartthings_reload_count") or 0) + (1 if reload_result.get("ok") else 0)
        result_text = reload_result.get("message") or "unknown result"
        event = viper_health.record_health_event(
            "smartthings_reload",
            "ok" if reload_result.get("ok") else "failed",
            f"Refrigerator SmartThings automatic reload: {result_text}",
            details={"entry_id": entry_id, "reload_result": reload_result, "health": health},
        )
        repeat_count = viper_health.count_recent_health_events("smartthings_reload")
        self._set_status(
            last_smartthings_reload_at=now,
            last_smartthings_reload_result=result_text,
            smartthings_reload_count=count,
            repeated_smartthings_reloads_24h=repeat_count,
            last_health_journal_event=event,
            critical_health_message=f"{health.get('message')} Automatic reload result: {result_text}",
        )
        logging.warning(
            "[HA LISTENER] SmartThings refrigerator watchdog reloaded entry=%s ok=%s result=%s",
            entry_id,
            reload_result.get("ok"),
            result_text,
        )
        self._fridge_states.clear()
        return {**health, "reloaded": bool(reload_result.get("ok")), "reload_result": reload_result}

    async def _refresh_fridge_states(self, ha_ip, ha_port, token, recover_open=False):
        result = {"checked": True, "successes": 0, "failures": 0, "errors": []}
        for entity_id in FRIDGE_DOOR_MESSAGES:
            try:
                new_state = await asyncio.to_thread(self._fetch_ha_state, ha_ip, ha_port, token, entity_id)
            except Exception as e:
                logging.debug("[HA LISTENER] fridge poll failed entity=%s error=%s", entity_id, e)
                result["failures"] += 1
                result["errors"].append(f"{entity_id}: {e}")
                continue

            result["successes"] += 1
            new_norm = normalize_state(state_text(new_state))
            old_norm = self._fridge_states.get(entity_id)
            self._fridge_states[entity_id] = new_norm
            self._set_status(last_fridge_poll_at=time.time())

            if old_norm is None:
                if recover_open and new_norm in FRIDGE_OPEN_STATES:
                    logging.info("[HA LISTENER] recovered open fridge/freezer state entity=%s state=%s", entity_id, new_norm)
                    self._dispatch_state_change(entity_id, {"state": "off"}, new_state)
                continue

            if new_norm != old_norm:
                logging.info("[HA LISTENER] fridge/freezer poll noticed entity=%s old=%s new=%s", entity_id, old_norm, new_norm)
                self._dispatch_state_change(entity_id, {"state": old_norm}, new_state)
        return result

    async def _refresh_cinderella_states(self, ha_ip, ha_port, token, recover_active=False):
        config = self.config_provider() or {}
        if not bool(config.get("cinderella_enabled", True)):
            return {"checked": False, "successes": 0, "failures": 0, "errors": []}
        result = {"checked": True, "successes": 0, "failures": 0, "errors": []}
        for role, entity_id in cinderella_entities(config).items():
            try:
                new_state = await asyncio.to_thread(self._fetch_ha_state, ha_ip, ha_port, token, entity_id)
            except Exception as e:
                logging.debug("[HA LISTENER] Cinderella poll failed entity=%s error=%s", entity_id, e)
                result["failures"] += 1
                result["errors"].append(f"{entity_id}: {e}")
                continue

            result["successes"] += 1
            new_norm = normalize_state(state_text(new_state))
            old_norm = self._cinderella_states.get(entity_id)
            self._cinderella_states[entity_id] = new_norm
            self._set_status(last_cinderella_poll_at=time.time())

            if old_norm is None:
                if recover_active and role in {"vacuum_error", "dock_error", "mop_drying"}:
                    actions = route_cinderella_state_change(config, entity_id, "off", new_norm)
                    if actions:
                        logging.info("[HA LISTENER] recovered active Cinderella state entity=%s state=%s", entity_id, new_norm)
                        self._dispatch_state_change(entity_id, {"state": "off"}, new_state)
                continue

            if new_norm != old_norm:
                logging.info("[HA LISTENER] Cinderella poll noticed entity=%s old=%s new=%s", entity_id, old_norm, new_norm)
                self._dispatch_state_change(entity_id, {"state": old_norm}, new_state)
        return result

    def _dispatch_state_change(self, entity_id, old_state, new_state):
        config = self.config_provider() or {}
        old_raw = state_text(old_state)
        new_raw = state_text(new_state)
        old_norm = normalize_state(old_raw)
        new_norm = normalize_state(new_raw)
        actions = route_state_change(config, entity_id, old_state, new_state)
        self._set_status(
            last_event_at=time.time(),
            last_event_entity=entity_id,
            last_event_old_state=old_raw,
            last_event_new_state=new_raw,
            last_event_old_normalized=old_norm,
            last_event_new_normalized=new_norm,
            last_event_action_count=len(actions),
        )
        logging.info(
            "[HA LISTENER] event entity=%s raw_old=%s raw_new=%s norm_old=%s norm_new=%s actions=%s",
            entity_id,
            old_raw,
            new_raw,
            old_norm,
            new_norm,
            len(actions),
        )
        for action in actions:
            logging.info("[HA LISTENER] routed entity=%s normalized_state=%s action=%s", entity_id, new_norm, action)
            self._set_status(last_action_at=time.time())
            self._set_status(last_routed_action=deepcopy(action))
            self.action_handler(action)

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
        if entity_id in FRIDGE_DOOR_MESSAGES:
            self._fridge_states[entity_id] = normalize_state(state_text(new_state))
        if entity_id in cinderella_entities(self.config_provider() or {}).values():
            self._cinderella_states[entity_id] = normalize_state(state_text(new_state))
        self._dispatch_state_change(entity_id, old_state, new_state)
