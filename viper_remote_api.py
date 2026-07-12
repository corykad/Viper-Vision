from __future__ import annotations

import wx
from flask import jsonify, request


def _api_bool_value():
    payload = request.get_json(silent=True) or {}
    value = payload.get("state", payload.get("enabled", payload.get("muted", request.form.get("state"))))
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "on", "yes", "enabled", "armed", "mute", "muted"}:
        return True
    if text in {"0", "false", "off", "no", "disabled", "disarmed", "unmute", "unmuted"}:
        return False
    return None


def viper_control_state(dashboard):
    if dashboard is None:
        return {"ready": False}
    ice_maker_state = {}
    if hasattr(dashboard, "get_ice_maker_status"):
        try:
            status = dashboard.get_ice_maker_status(timeout=2)
            ice_maker_state = {
                "enabled": bool(status.get("is_on")),
                "switch_state": status.get("switch_state") or "unknown",
                "switch_entity": status.get("switch_entity") or "",
                "keep_on_state": status.get("keep_on_state") or "unknown",
                "counter_text": status.get("counter_text") or "",
            }
        except Exception as e:
            ice_maker_state = {"enabled": False, "switch_state": "unknown", "error": str(e)}
    speakers_state = {}
    for name, speaker in (dashboard.config.get("speakers") or {}).items():
        speakers_state[name] = {
            "enabled": bool(speaker.get("enabled", True)),
            "type": speaker.get("type", ""),
            "id": speaker.get("id", ""),
        }
    return {
        "ready": True,
        "armed": bool(getattr(dashboard, "is_armed", dashboard.config.get("is_armed", True))),
        "global_mute": bool(dashboard.config.get("global_mute", False)),
        "ice_maker": ice_maker_state,
        "speakers": speakers_state,
    }


def register_control_routes(app, get_dashboard):
    @app.route("/api/control/state", methods=["GET"])
    def api_control_state():
        dashboard = get_dashboard()
        status = 200 if dashboard is not None else 503
        return jsonify(viper_control_state(dashboard)), status

    @app.route("/api/control/armed", methods=["POST"])
    def api_control_armed():
        dashboard = get_dashboard()
        if dashboard is None:
            return jsonify({"ok": False, "message": "System initializing."}), 503
        enabled = _api_bool_value()
        if enabled is None:
            return jsonify({"ok": False, "message": "Send JSON {'state': true} or {'state': false}."}), 400
        dashboard.is_armed = bool(enabled)
        dashboard.config["is_armed"] = dashboard.is_armed
        dashboard.save_config()
        if hasattr(dashboard, "btn_arm"):
            wx.CallAfter(dashboard.btn_arm.SetLabel, "Disarm System" if dashboard.is_armed else "Arm System")
        message = f"Viper Vision {'Armed' if dashboard.is_armed else 'Disarmed'} from API."
        wx.CallAfter(dashboard.notify, message, 1, True, True)
        return jsonify({"ok": True, "armed": dashboard.is_armed, "state": viper_control_state(dashboard)})

    @app.route("/api/control/global_mute", methods=["POST"])
    def api_control_global_mute():
        dashboard = get_dashboard()
        if dashboard is None:
            return jsonify({"ok": False, "message": "System initializing."}), 503
        muted = _api_bool_value()
        if muted is None:
            return jsonify({"ok": False, "message": "Send JSON {'state': true} or {'state': false}."}), 400
        dashboard.set_global_mute(muted, source="api")
        return jsonify({"ok": True, "global_mute": bool(dashboard.config.get("global_mute", False)), "state": viper_control_state(dashboard)})

    @app.route("/api/control/speakers/<path:name>/enabled", methods=["POST"])
    def api_control_speaker_enabled(name):
        dashboard = get_dashboard()
        if dashboard is None:
            return jsonify({"ok": False, "message": "System initializing."}), 503
        speakers_cfg = dashboard.config.get("speakers") or {}
        if name not in speakers_cfg:
            return jsonify({"ok": False, "message": f"Unknown speaker: {name}", "speakers": sorted(speakers_cfg.keys())}), 404
        enabled = _api_bool_value()
        if enabled is None:
            return jsonify({"ok": False, "message": "Send JSON {'state': true} or {'state': false}."}), 400
        speakers_cfg[name]["enabled"] = bool(enabled)
        dashboard.save_config()
        if hasattr(dashboard, "refresh_speaker_list"):
            wx.CallAfter(dashboard.refresh_speaker_list)
        status_msg = f"{name} {'enabled' if enabled else 'disabled'} from API"
        wx.CallAfter(dashboard.notify, status_msg, 10, False, False)
        return jsonify({"ok": True, "speaker": name, "enabled": bool(enabled), "state": viper_control_state(dashboard)})

    @app.route("/api/control/ice_maker/enabled", methods=["POST"])
    def api_control_ice_maker_enabled():
        dashboard = get_dashboard()
        if dashboard is None:
            return jsonify({"ok": False, "message": "System initializing."}), 503
        enabled = _api_bool_value()
        if enabled is None:
            return jsonify({"ok": False, "message": "Send JSON {'state': true} or {'state': false}."}), 400
        required = [
            "_configured_ice_maker_entities",
            "_call_ha_service",
            "_set_ice_maker_switch_with_confirmation",
            "_reset_ice_maker_counter",
        ]
        if not all(hasattr(dashboard, name) for name in required):
            return jsonify({"ok": False, "message": "Ice maker controls are not available."}), 503
        entities = dashboard._configured_ice_maker_entities()
        if enabled:
            dashboard._call_ha_service("input_boolean/turn_off", entities["auto_refill"])
            ok_helper = dashboard._call_ha_service("input_boolean/turn_on", entities["keep_on"])
            switch_ok = dashboard._set_ice_maker_switch_with_confirmation(entities, "on")
            counter_ok = dashboard._reset_ice_maker_counter(entities)
            ok = bool(ok_helper and switch_ok)
            message = "Ice maker turned on with refill override enabled."
        else:
            switch_ok = dashboard._set_ice_maker_switch_with_confirmation(entities, "off")
            ok_helper = dashboard._call_ha_service("input_boolean/turn_off", entities["keep_on"])
            dashboard._call_ha_service("input_boolean/turn_off", entities["auto_refill"])
            counter_ok = dashboard._reset_ice_maker_counter(entities)
            ok = bool(switch_ok and ok_helper)
            message = "Ice maker turned off and refill override cleared."
        if counter_ok:
            message += " Counter reset."
        if not ok:
            message = f"Ice maker {'on' if enabled else 'off'} request failed. Home Assistant did not confirm the requested state."
        if hasattr(dashboard, "refresh_ice_maker_status"):
            wx.CallAfter(dashboard.refresh_ice_maker_status)
        if hasattr(dashboard, "notify"):
            wx.CallAfter(dashboard.notify, f"{message} Source: API.", 10, False, False)
        status = 200 if ok else 502
        return jsonify({"ok": ok, "ice_maker": bool(enabled), "message": message, "state": viper_control_state(dashboard)}), status
