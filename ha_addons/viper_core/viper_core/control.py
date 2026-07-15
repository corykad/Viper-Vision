import json
import logging
import re
import shutil
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote


LOGGER = logging.getLogger(__name__)
CONTROL_STATE_PATH = Path("/data/control_state.json")
CHIMES_DIR = Path("/data/chimes")
MEDIA_CHIMES_DIR = Path("/media/viper_core_chimes")
ALLOWED_CHIME_SUFFIXES = {".mp3", ".wav", ".ogg", ".m4a"}
HEAT_PUMP_UNITS = [
    ("climate.office_heat_pump_alexa", "Office"),
    ("climate.living_room_heat_pump_alexa", "Living Room"),
    ("climate.kitchen_heat_pump_alexa", "Kitchen"),
    ("climate.jamie_s_room_heat_pump_alexa", "Jamie's Room"),
    ("climate.master_bedroom_heat_pump_alexa", "Master Bedroom"),
]
HEAT_PUMP_CLIMATES = [entity_id for entity_id, _name in HEAT_PUMP_UNITS]
HEAT_PUMP_AIRFLOW = [
    ("fan.office_airflow", "Airflow Office"),
    ("fan.living_room_airflow", "Airflow Living Room"),
    ("fan.kitchen_airflow", "Airflow Kitchen"),
    ("fan.jamie_s_room_airflow", "Airflow Jamie's Room"),
    ("fan.master_bedroom_airflow", "Airflow Master Bedroom"),
]
VACUUM_ENTITY = "vacuum.cinderella"
VACUUM_STATUS_ENTITY = "sensor.cinderella_status"


DEFAULT_SPEAKERS = {
    "entry way speaker": {
        "id": "media_player.entryway_speaker",
        "type": "ha",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "office sonos": {
        "id": "192.168.4.34",
        "type": "sonos",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "cory's sonos beam": {
        "id": "media_player.cory_s_sonos_beam",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "bathroom": {
        "id": "media_player.bathroom",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "master bedroom": {
        "id": "media_player.daisy_cory_master_bedroom",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "daisies loft echo": {
        "id": "media_player.daisies_loft_echo",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "office": {
        "id": "media_player.kitchen_3",
        "type": "alexa",
        "enabled": False,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
}


class ControlState:
    def __init__(self, path=CONTROL_STATE_PATH):
        self.path = Path(path)
        self.state = self._load()

    def public_state(self):
        state = deepcopy(self.state)
        state["ready"] = True
        state["chimes"]["available"] = self.available_chimes()
        settings = state.setdefault("settings", {})
        settings["gemini_configured"] = bool(settings.get("gemini_api_key"))
        settings["pushover_configured"] = bool(settings.get("pushover_user_key") and settings.get("pushover_api_token"))
        settings.pop("gemini_api_key", None)
        settings.pop("pushover_user_key", None)
        settings.pop("pushover_api_token", None)
        return state

    def effective_config(self, base_config):
        settings = self.state.get("settings") or {}
        base = getattr(base_config, "__dict__", {})
        return SimpleNamespace(
            **{
                **base,
                "external_base_url": settings.get("external_base_url") or getattr(base_config, "external_base_url", ""),
                "gemini_api_key": settings.get("gemini_api_key") or getattr(base_config, "gemini_api_key", ""),
                "pushover_user_key": settings.get("pushover_user_key") or getattr(base_config, "pushover_user_key", ""),
                "pushover_api_token": settings.get("pushover_api_token") or getattr(base_config, "pushover_api_token", ""),
                "gemini_vision_model": settings.get("gemini_vision_model") or getattr(base_config, "gemini_vision_model", "gemini-3.5-flash"),
                "front_door_camera_entity": settings.get("front_door_camera_entity") or getattr(base_config, "front_door_camera_entity", "camera.front_door_snapshot"),
                "back_door_camera_entity": settings.get("back_door_camera_entity") or getattr(base_config, "back_door_camera_entity", "camera.back_door_snapshot"),
                "front_door_stream_url": settings.get("front_door_stream_url") or "",
                "back_door_stream_url": settings.get("back_door_stream_url") or "",
                "front_door_live_stream_switch": settings.get("front_door_live_stream_switch") or "switch.front_door_live_stream",
                "back_door_live_stream_switch": settings.get("back_door_live_stream_switch") or "switch.back_door_live_stream",
                "front_door_photo_prompt": settings.get("front_door_photo_prompt") or "",
                "back_door_photo_prompt": settings.get("back_door_photo_prompt") or "",
                "doorbell_video_prompt": settings.get("doorbell_video_prompt") or "",
                "ai_description_styles": dict(settings.get("ai_description_styles") or {}),
                "ai_custom_descriptions": dict(settings.get("ai_custom_descriptions") or {}),
                "doorbell_video_mode": settings.get("doorbell_video_mode") or "fast",
                "doorbell_live_video_seconds": int(settings.get("doorbell_live_video_seconds") or 4),
                "doorbell_live_video_frames": int(settings.get("doorbell_live_video_frames") or 4),
                "doorbell_dedupe_seconds": int(settings.get("doorbell_dedupe_seconds") or 30),
                "fridge_stale_minutes": int(settings.get("fridge_stale_minutes") or 45),
                "vacuum_repeat_quiet_minutes": int(settings.get("vacuum_repeat_quiet_minutes") or 20),
                "vacuum_announce_events": list(settings.get("vacuum_announce_events") or []),
            }
        )

    def speaker_targets(self, category):
        category = str(category or "utilities").strip().lower()
        if self.state.get("global_mute"):
            return {"ha": [], "sonos": [], "alexa": []}
        targets = {"ha": [], "sonos": [], "alexa": []}
        for _name, speaker in (self.state.get("speakers") or {}).items():
            if not speaker.get("enabled", True):
                continue
            if category == "doorbell" and not speaker.get("doorbell", True):
                continue
            if category == "fridge" and not speaker.get("fridge", True):
                continue
            if category not in {"doorbell", "fridge"} and not speaker.get("utilities", True):
                continue
            speaker_id = str(speaker.get("id") or "").strip()
            speaker_type = str(speaker.get("type") or "").strip().lower()
            if speaker_id and speaker_type in targets:
                targets[speaker_type].append(speaker_id)
        return targets

    def set_armed(self, armed):
        self.state["armed"] = bool(armed)
        self._save()
        return self.public_state()

    def set_global_mute(self, muted):
        self.state["global_mute"] = bool(muted)
        self._save()
        return self.public_state()

    def set_speaker_enabled(self, speaker_name, enabled):
        name = unquote(str(speaker_name or "")).strip().lower()
        speakers = self.state.setdefault("speakers", {})
        if name not in speakers:
            return None
        speakers[name]["enabled"] = bool(enabled)
        self._save()
        return self.public_state()

    def set_speaker_route(self, speaker_name, route, enabled):
        route = str(route or "").strip().lower()
        if route not in {"doorbell", "fridge", "utilities"}:
            return None
        name = unquote(str(speaker_name or "")).strip().lower()
        speakers = self.state.setdefault("speakers", {})
        if name not in speakers:
            return None
        speakers[name][route] = bool(enabled)
        self._save()
        return self.public_state()

    def upsert_speaker(self, name, speaker_id, speaker_type, enabled=True, routes=None):
        name = str(name or "").strip().lower()
        speaker_id = str(speaker_id or "").strip()
        speaker_type = str(speaker_type or "").strip().lower()
        if not name or not speaker_id or speaker_type not in {"ha", "sonos", "alexa"}:
            return None
        routes = routes if isinstance(routes, dict) else {}
        self.state.setdefault("speakers", {})[name] = {
            "id": speaker_id,
            "type": speaker_type,
            "enabled": bool(enabled),
            "doorbell": bool(routes.get("doorbell", True)),
            "fridge": bool(routes.get("fridge", True)),
            "utilities": bool(routes.get("utilities", True)),
        }
        self._save()
        return self.public_state()

    def delete_speaker(self, speaker_name):
        name = unquote(str(speaker_name or "")).strip().lower()
        speakers = self.state.setdefault("speakers", {})
        if name not in speakers:
            return None
        speakers.pop(name)
        self._save()
        return self.public_state()

    def set_ice_maker_enabled(self, enabled):
        self.state.setdefault("ice_maker", {})["enabled"] = bool(enabled)
        self._save()
        return self.public_state()

    def set_chime(self, event_name, filename):
        event_name = str(event_name or "").strip().lower()
        if event_name not in {
            "front_doorbell",
            "back_doorbell",
            "fridge_open",
            "fridge_closed",
            "freezer_open",
            "freezer_closed",
        }:
            return None
        filename = _safe_filename(filename)
        if filename and filename not in self.available_chimes():
            return None
        self.state.setdefault("chimes", {}).setdefault("events", {})[event_name] = filename
        self._save()
        return self.public_state()

    def set_settings(self, payload):
        settings = self.state.setdefault("settings", {})
        for key in [
            "external_base_url",
            "gemini_vision_model",
            "front_door_camera_entity",
            "back_door_camera_entity",
            "front_door_stream_url",
            "back_door_stream_url",
            "front_door_live_stream_switch",
            "back_door_live_stream_switch",
            "front_door_photo_prompt",
            "back_door_photo_prompt",
            "doorbell_video_prompt",
        ]:
            if key in payload:
                settings[key] = str(payload.get(key) or "").strip().rstrip("/")
        if "doorbell_video_mode" in payload:
            mode = str(payload.get("doorbell_video_mode") or "fast").strip().lower()
            settings["doorbell_video_mode"] = mode if mode in {"fast", "smart", "detailed", "manual"} else "fast"
        styles = dict(settings.get("ai_description_styles") or {})
        custom = dict(settings.get("ai_custom_descriptions") or {})
        for job in ("front_photo", "back_photo", "manual_video", "smart_video", "detailed_video"):
            style_key = f"ai_style_{job}"
            custom_key = f"ai_custom_{job}"
            if style_key in payload:
                style = str(payload.get(style_key) or "balanced").strip().lower()
                styles[job] = style if style in {"balanced", "fast_security", "people_movement", "packages_deliveries", "detailed_blind", "custom"} else "balanced"
            if custom_key in payload:
                custom[job] = str(payload.get(custom_key) or "").strip()
        if styles:
            settings["ai_description_styles"] = styles
        if custom:
            settings["ai_custom_descriptions"] = custom
        for key, minimum, maximum in [
            ("doorbell_dedupe_seconds", 5, 180),
            ("doorbell_live_video_seconds", 2, 10),
            ("doorbell_live_video_frames", 2, 6),
            ("fridge_stale_minutes", 5, 240),
            ("vacuum_repeat_quiet_minutes", 1, 240),
        ]:
            if key in payload:
                settings[key] = _clamped_int(payload.get(key), settings.get(key), minimum, maximum)
        if "vacuum_announce_events" in payload:
            settings["vacuum_announce_events"] = _csv_list(payload.get("vacuum_announce_events"))
        if str(payload.get("gemini_api_key") or "").strip():
            settings["gemini_api_key"] = str(payload.get("gemini_api_key") or "").strip()
        if _payload_bool(payload.get("clear_gemini_api_key", False)):
            settings["gemini_api_key"] = ""
        if str(payload.get("pushover_user_key") or "").strip():
            settings["pushover_user_key"] = str(payload.get("pushover_user_key") or "").strip()
        if str(payload.get("pushover_api_token") or "").strip():
            settings["pushover_api_token"] = str(payload.get("pushover_api_token") or "").strip()
        if _payload_bool(payload.get("clear_pushover", False)):
            settings["pushover_user_key"] = ""
            settings["pushover_api_token"] = ""
        self._save()
        return self.public_state()

    def chime_for_event(self, event_type, payload):
        payload = payload if isinstance(payload, dict) else {}
        event_name = ""
        if event_type == "doorbell":
            door = str(payload.get("door") or "front").strip().lower()
            event_name = "back_doorbell" if door.startswith("back") else "front_doorbell"
        elif event_type == "fridge":
            appliance = str(payload.get("appliance") or "fridge").strip().lower()
            state = str(payload.get("state") or "").strip().lower()
            if state in {"open", "opened", "on"}:
                event_name = "freezer_open" if "freezer" in appliance else "fridge_open"
            elif state in {"closed", "close", "off"}:
                event_name = "freezer_closed" if "freezer" in appliance else "fridge_closed"
        if not event_name:
            return ""
        filename = self.state.get("chimes", {}).get("events", {}).get(event_name, "")
        return filename if filename in self.available_chimes() else ""

    def available_chimes(self):
        try:
            CHIMES_DIR.mkdir(parents=True, exist_ok=True)
            _sync_media_chimes()
            return sorted(
                item.name
                for item in CHIMES_DIR.iterdir()
                if item.is_file() and item.suffix.lower() in ALLOWED_CHIME_SUFFIXES
            )
        except OSError as exc:
            LOGGER.warning("Could not list chimes: %s", exc)
            return []

    def save_chime_file(self, filename, content):
        filename = _safe_filename(filename)
        if not filename:
            return {"ok": False, "message": "Choose an MP3, WAV, OGG, or M4A chime file."}
        try:
            CHIMES_DIR.mkdir(parents=True, exist_ok=True)
            path = CHIMES_DIR / filename
            path.write_bytes(content or b"")
            _sync_media_chimes()
        except OSError as exc:
            return {"ok": False, "message": f"Could not save chime: {exc}"}
        return {"ok": True, "filename": filename, "state": self.public_state()}

    def delete_chime_file(self, filename):
        filename = _safe_filename(filename)
        if not filename:
            return {"ok": False, "message": "Missing chime filename."}
        try:
            path = CHIMES_DIR / filename
            if path.exists():
                path.unlink()
            media_path = MEDIA_CHIMES_DIR / filename
            if media_path.exists():
                media_path.unlink()
            for event, selected in list(self.state.setdefault("chimes", {}).setdefault("events", {}).items()):
                if selected == filename:
                    self.state["chimes"]["events"][event] = ""
            self._save()
        except OSError as exc:
            return {"ok": False, "message": f"Could not delete chime: {exc}"}
        return {"ok": True, "state": self.public_state()}

    def _load(self):
        state = {
            "armed": True,
            "global_mute": False,
            "ice_maker": {"enabled": False},
            "settings": {
                "external_base_url": "",
                "gemini_api_key": "",
                "pushover_user_key": "",
                "pushover_api_token": "",
                "gemini_vision_model": "gemini-3.5-flash",
                "front_door_camera_entity": "camera.front_door_snapshot",
                "back_door_camera_entity": "camera.back_door_snapshot",
                "front_door_stream_url": "",
                "back_door_stream_url": "",
                "front_door_live_stream_switch": "switch.front_door_live_stream",
                "back_door_live_stream_switch": "switch.back_door_live_stream",
                "front_door_photo_prompt": "",
                "back_door_photo_prompt": "",
                "doorbell_video_prompt": "",
                "ai_description_styles": {
                    "front_photo": "balanced",
                    "back_photo": "balanced",
                    "manual_video": "detailed_blind",
                    "smart_video": "fast_security",
                    "detailed_video": "detailed_blind",
                },
                "ai_custom_descriptions": {
                    "front_photo": "",
                    "back_photo": "",
                    "manual_video": "",
                    "smart_video": "",
                    "detailed_video": "",
                },
                "doorbell_video_mode": "fast",
                "doorbell_live_video_seconds": 4,
                "doorbell_live_video_frames": 4,
                "doorbell_dedupe_seconds": 30,
                "fridge_stale_minutes": 45,
                "vacuum_repeat_quiet_minutes": 20,
                "vacuum_announce_events": [
                    "departure",
                    "washing",
                    "emptying",
                    "returning",
                    "victory",
                    "paused",
                    "drying",
                    "error",
                ],
            },
            "chimes": {
                "events": {
                    "front_doorbell": "",
                    "back_doorbell": "",
                    "fridge_open": "",
                    "fridge_closed": "",
                    "freezer_open": "",
                    "freezer_closed": "",
                },
                "available": [],
            },
            "speakers": deepcopy(DEFAULT_SPEAKERS),
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update({k: v for k, v in loaded.items() if k not in {"speakers", "settings", "chimes"}})
                    speakers = deepcopy(DEFAULT_SPEAKERS)
                    for name, speaker in (loaded.get("speakers") or {}).items():
                        normalized = str(name or "").strip().lower()
                        if normalized:
                            speakers.setdefault(normalized, {}).update(speaker if isinstance(speaker, dict) else {})
                    state["speakers"] = speakers
                    chimes = deepcopy(state["chimes"])
                    if isinstance(loaded.get("chimes"), dict):
                        chimes["events"].update(loaded["chimes"].get("events") or {})
                    state["chimes"] = chimes
                    settings = deepcopy(state["settings"])
                    if isinstance(loaded.get("settings"), dict):
                        settings.update(loaded["settings"])
                    state["settings"] = settings
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("Could not read Viper Core control state: %s", exc)
        return state

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("Could not save Viper Core control state: %s", exc)


class ControlApi:
    def __init__(self, control_state, ha_client):
        self.control_state = control_state
        self.ha = ha_client

    def handle_get(self, path):
        if path == "/api/control/state":
            return self.control_state.public_state()
        if path == "/api/chimes":
            return {"ok": True, "chimes": self.control_state.available_chimes()}
        return None

    def chime_path(self, filename):
        filename = _safe_filename(filename)
        if not filename:
            return None
        path = CHIMES_DIR / filename
        try:
            if path.exists() and path.is_file():
                return path
        except OSError:
            return None
        return None

    def upload_chime(self, filename, content):
        return self.control_state.save_chime_file(filename, content)

    def handle_post(self, path, payload):
        state = _payload_bool(payload)
        if path == "/api/control/armed":
            return {"ok": True, "state": self.control_state.set_armed(state)}
        if path == "/api/control/global_mute":
            return {"ok": True, "state": self.control_state.set_global_mute(state)}
        if path == "/api/control/ice_maker/enabled":
            self._set_ice_maker(state)
            return {"ok": True, "state": self.control_state.set_ice_maker_enabled(state)}
        if path == "/api/control/speakers":
            updated = self.control_state.upsert_speaker(
                payload.get("name"),
                payload.get("id"),
                payload.get("type"),
                _payload_bool(payload.get("enabled", True)),
                payload.get("routes") or payload,
            )
            if updated is None:
                return {"ok": False, "message": "Speaker needs a name, id, and type of ha, sonos, or alexa."}
            return {"ok": True, "state": updated}
        if path == "/api/control/chimes":
            updated = self.control_state.set_chime(payload.get("event"), payload.get("filename", ""))
            if updated is None:
                return {"ok": False, "message": "Unknown chime event or chime file."}
            return {"ok": True, "state": updated}
        if path == "/api/control/settings":
            return {"ok": True, "state": self.control_state.set_settings(payload)}
        if path == "/api/control/hvac":
            return self._set_hvac(payload)
        if path == "/api/control/vacuum":
            return self._vacuum_action(payload)
        if path == "/api/chimes/delete":
            return self.control_state.delete_chime_file(payload.get("filename"))
        prefix = "/api/control/speakers/"
        suffix = "/enabled"
        if path.startswith(prefix) and path.endswith(suffix):
            speaker_name = path[len(prefix) : -len(suffix)]
            updated = self.control_state.set_speaker_enabled(speaker_name, state)
            if updated is None:
                return {"ok": False, "message": f"Unknown speaker: {unquote(speaker_name)}"}
            return {"ok": True, "state": updated}
        route_suffix = "/route"
        if path.startswith(prefix) and path.endswith(route_suffix):
            speaker_name = path[len(prefix) : -len(route_suffix)]
            updated = self.control_state.set_speaker_route(speaker_name, payload.get("route"), state)
            if updated is None:
                return {"ok": False, "message": f"Unknown speaker or route: {unquote(speaker_name)}"}
            return {"ok": True, "state": updated}
        delete_suffix = "/delete"
        if path.startswith(prefix) and path.endswith(delete_suffix):
            speaker_name = path[len(prefix) : -len(delete_suffix)]
            updated = self.control_state.delete_speaker(speaker_name)
            if updated is None:
                return {"ok": False, "message": f"Unknown speaker: {unquote(speaker_name)}"}
            return {"ok": True, "state": updated}
        return None

    def device_status(self):
        if not self.ha.available():
            return {
                "ok": False,
                "message": "Home Assistant API is not configured.",
                "heat_pumps": [],
                "vacuum": {},
                "refrigerator": {},
            }
        heat_pumps = []
        ok = True
        for entity_id, name in HEAT_PUMP_UNITS:
            item = _read_entity(self.ha, entity_id)
            if not item.get("ok"):
                ok = False
            heat_pumps.append({
                "name": name,
                "entity_id": entity_id,
                **item,
            })
        vacuum = _read_entity(self.ha, VACUUM_ENTITY)
        refrigerator = {
            "ice_maker": _read_entity(self.ha, "switch.refrigerator_cubed_ice"),
            "fridge_door": _read_entity(self.ha, "binary_sensor.refrigerator_fridge_door"),
            "freezer_door": _read_entity(self.ha, "binary_sensor.refrigerator_freezer_door"),
            "filter_usage": _read_entity(self.ha, "sensor.refrigerator_water_filter_usage"),
            "filter_status": _read_entity(self.ha, "sensor.refrigerator_filter_status"),
        }
        airflow = []
        for entity_id, name in HEAT_PUMP_AIRFLOW:
            airflow.append({"name": name, "entity_id": entity_id, **_read_entity(self.ha, entity_id)})
        ok = ok and vacuum.get("ok", False)
        return {
            "ok": ok,
            "message": "Device status refreshed." if ok else "One or more devices need attention.",
            "heat_pumps": heat_pumps,
            "airflow": airflow,
            "vacuum": vacuum,
            "vacuum_status": _read_entity(self.ha, VACUUM_STATUS_ENTITY),
            "refrigerator": refrigerator,
            "timestamp": int(time.time()),
        }

    def _set_ice_maker(self, enabled):
        if not self.ha.available():
            return
        switch_entity = "switch.refrigerator_cubed_ice"
        keep_on_entity = "input_boolean.keep_ice_maker_on"
        refill_entity = "input_boolean.ice_maker_auto_refill_running"
        counter_entity = "counter.ice_usage_counter"
        try:
            if enabled:
                self.ha.call_service("input_boolean/turn_on", {"entity_id": keep_on_entity})
                self.ha.call_service("input_boolean/turn_off", {"entity_id": refill_entity})
                self.ha.call_service("counter/reset", {"entity_id": counter_entity})
                self.ha.call_service("switch/turn_on", {"entity_id": switch_entity})
            else:
                self.ha.call_service("input_boolean/turn_off", {"entity_id": keep_on_entity})
                self.ha.call_service("input_boolean/turn_off", {"entity_id": refill_entity})
                self.ha.call_service("switch/turn_off", {"entity_id": switch_entity})
        except Exception as exc:
            LOGGER.warning("Ice maker control failed: %s", exc)

    def _set_hvac(self, payload):
        mode = str(payload.get("mode") or "").strip().lower()
        temp = payload.get("temperature")
        target = str(payload.get("entity_id") or "all").strip()
        entities = HEAT_PUMP_CLIMATES if target in {"", "all"} else [target]
        calls = []
        try:
            for entity_id in entities:
                if mode == "off":
                    self.ha.call_service("climate/turn_off", {"entity_id": entity_id})
                    calls.append(entity_id)
                    continue
                data = {"entity_id": entity_id}
                if mode in {"cool", "heat"}:
                    data["hvac_mode"] = mode
                if temp not in {None, ""}:
                    data["temperature"] = float(temp)
                self.ha.call_service("climate/set_temperature", data)
                calls.append(entity_id)
        except Exception as exc:
            return {"ok": False, "message": f"Heat pump command failed: {exc}", "entities": calls}
        return {"ok": True, "message": f"Heat pump command sent to {len(calls)} unit(s).", "entities": calls}

    def _vacuum_action(self, payload):
        action = str(payload.get("action") or "").strip().lower()
        service = {
            "start": "vacuum/start",
            "pause": "vacuum/pause",
            "stop": "vacuum/stop",
            "dock": "vacuum/return_to_base",
        }.get(action)
        if not service:
            return {"ok": False, "message": "Unknown vacuum action."}
        entity_id = str(payload.get("entity_id") or VACUUM_ENTITY).strip()
        try:
            self.ha.call_service(service, {"entity_id": entity_id})
        except Exception as exc:
            return {"ok": False, "message": f"Vacuum command failed: {exc}"}
        return {"ok": True, "message": f"Vacuum {action} command sent.", "entity_id": entity_id}


def _payload_bool(payload):
    if isinstance(payload, dict) and "state" in payload:
        value = payload.get("state")
    else:
        value = payload
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "enabled"}
    return bool(value)


def _clamped_int(value, fallback, minimum, maximum):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        try:
            parsed = int(float(fallback))
        except (TypeError, ValueError):
            parsed = minimum
    return max(minimum, min(maximum, parsed))


def _csv_list(value):
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[\s,]+", str(value or ""))
    return [str(item).strip().lower() for item in items if str(item).strip()]


def _safe_filename(filename):
    filename = Path(str(filename or "").replace("\\", "/")).name.strip()
    if not filename:
        return ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_CHIME_SUFFIXES:
        return ""
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", filename)


def _sync_media_chimes():
    if not _media_chimes_available():
        return
    MEDIA_CHIMES_DIR.mkdir(parents=True, exist_ok=True)
    for item in CHIMES_DIR.iterdir():
        if not item.is_file() or item.suffix.lower() not in ALLOWED_CHIME_SUFFIXES:
            continue
        target = MEDIA_CHIMES_DIR / item.name
        if not target.exists() or item.stat().st_mtime > target.stat().st_mtime or item.stat().st_size != target.stat().st_size:
            shutil.copy2(item, target)


def _media_chimes_available():
    try:
        return MEDIA_CHIMES_DIR.exists() or MEDIA_CHIMES_DIR.parent.exists()
    except OSError:
        return False


def _read_entity(ha, entity_id):
    try:
        payload = ha.get_state(entity_id) or {}
    except Exception as exc:
        return {"ok": False, "state": "missing", "message": str(exc), "attributes": {}}
    state = str(payload.get("state") or "unknown")
    attributes = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    return {
        "ok": state.lower() not in {"unknown", "unavailable", "missing"},
        "state": state,
        "friendly_name": attributes.get("friendly_name", ""),
        "attributes": _public_attributes(attributes),
        "last_changed": payload.get("last_changed", ""),
        "last_updated": payload.get("last_updated", ""),
    }


def _public_attributes(attributes):
    keys = [
        "current_temperature",
        "temperature",
        "target_temp_high",
        "target_temp_low",
        "hvac_modes",
        "fan_mode",
        "fan_modes",
        "swing_mode",
        "swing_modes",
        "battery_level",
        "status",
        "percentage",
        "preset_mode",
        "preset_modes",
    ]
    return {key: attributes.get(key) for key in keys if key in attributes}
