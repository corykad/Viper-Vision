import logging
import time
from datetime import datetime

import wx
from flask import flash, jsonify, redirect, render_template, request, url_for

import viper_audio as audio
import viper_config as cfg
import viper_diagnostics as diagnostics
import viper_discovery as discovery
import viper_health
import viper_vision as vision
from viper_runtime import is_shutting_down, safe_submit
import viper_vacuum as vacuum

HIDDEN_VACUUM_SETTING_SUFFIXES = vacuum.HIDDEN_VACUUM_SETTING_SUFFIXES
VACUUM_CLEANING_MODES = vacuum.VACUUM_CLEANING_MODES
VACUUM_CLEANING_MODE_ORDER = vacuum.VACUUM_CLEANING_MODE_ORDER
vacuum_basic_actions_for_state = vacuum.vacuum_basic_actions_for_state
vacuum_cleaning_mode_service_calls = vacuum.vacuum_cleaning_mode_service_calls
_ha_domain_from_entity_id = vacuum.ha_domain_from_entity_id
_web_entity_name = vacuum.web_entity_name
_web_short_entity_label = vacuum.web_short_entity_label
_web_vacuum_tokens = vacuum.web_vacuum_tokens
_web_looks_like_roborock = vacuum.web_looks_like_roborock
_web_show_vacuum_setting = vacuum.web_show_vacuum_setting
_is_hidden_vacuum_setting_entity_id = vacuum.is_hidden_vacuum_setting_entity_id
_normalize_vacuum_cleaning_mode = vacuum.normalize_vacuum_cleaning_mode

_routes = []
_get_dashboard = lambda: None
_deps = {}


def route(rule, **options):
    def decorator(func):
        _routes.append((rule, options, func))
        return func

    return decorator


def _dashboard():
    return _get_dashboard()


def _dep(name):
    return _deps[name]


def _handle_doorbell(*args, **kwargs):
    return _dep("handle_doorbell")(*args, **kwargs)


def _doorbell_rtsp_for_key(*args, **kwargs):
    return _dep("doorbell_rtsp_for_key")(*args, **kwargs)


def _broadcast_message(*args, **kwargs):
    return _dep("broadcast_message")(*args, **kwargs)


def _json_or_redirect(*args, **kwargs):
    return _dep("json_or_redirect")(*args, **kwargs)


def ensure_cinderella_message_config(*args, **kwargs):
    return _dep("ensure_cinderella_message_config")(*args, **kwargs)


def choose_cinderella_message(*args, **kwargs):
    return _dep("choose_cinderella_message")(*args, **kwargs)


def _normalize_broadcast_mode(*args, **kwargs):
    return _dep("normalize_broadcast_mode")(*args, **kwargs)


def _doorbell_video_settings(*args, **kwargs):
    return _dep("doorbell_video_settings")(*args, **kwargs)


def register_remote_routes(app, get_dashboard, **deps):
    global _get_dashboard, _deps
    _get_dashboard = get_dashboard
    _deps = dict(deps)
    for rule, options, func in _routes:
        app.add_url_rule(rule, endpoint=func.__name__, view_func=func, **options)


# ==========================================
# FLASK ROUTES & WEBHOOKS
# ==========================================
@route("/")
def index():
    return redirect(url_for("remote_ui"))

@route("/doorbell-webhook", methods=["POST"])
def handle_front():
    return _handle_doorbell("front door", _doorbell_rtsp_for_key("front"), "front")

@route("/doorbell-webhook/back", methods=["POST"])
def handle_back():
    return _handle_doorbell("back door", _doorbell_rtsp_for_key("back"), "back")

@route("/remote", methods=["GET", "POST"])
@route("/remote/", methods=["GET", "POST"])
def remote_ui():
    if request.method == "POST":
        return _broadcast_message(request.form.get("broadcast_text", ""))
    if _dashboard() is None:
        return "System initializing, please refresh...", 503
    ensure_cinderella_message_config(_dashboard().config)
    vacuum = _build_web_vacuum_context()
    chime_files = ["(Default)"]
    if cfg.CHIMES_DIR.exists():
        for f in cfg.CHIMES_DIR.iterdir():
            if f.suffix.lower() in [".mp3", ".wav"]:
                chime_files.append(f.name)
    return render_template(
        "remote.html",
        config=_dashboard().config,
        activity_logs=_dep("activity_logs"),
        chimes=chime_files,
        edge_voices=_dep("edge_voices"),
        gemini_tts_voices=_dep("gemini_tts_voices"),
        dialects=_dep("dialects"),
        vacuum=vacuum,
        doorbell_video_settings=_doorbell_video_settings(_dashboard().config),
        doorbell_video_modes=vision.VIDEO_ANALYSIS_LABELS,
        last_video_analysis=getattr(_dashboard(), "last_video_analysis", {}),
        last_video_followup_decision=getattr(_dashboard(), "last_video_followup_decision", {}),
        setup_status_summary=_dashboard().build_setup_next_action_summary() if hasattr(_dashboard(), "build_setup_next_action_summary") else "",
        setup_checklist_summary=_dashboard().build_setup_checklist_summary() if hasattr(_dashboard(), "build_setup_checklist_summary") else "",
        setup_smoke_report=getattr(_dashboard(), "last_remote_setup_smoke_report", ""),
        diagnostics_summary=diagnostics.health_summary_text(_current_diagnostics(check_ha=False)),
        ice_maker=_dashboard().get_ice_maker_status(timeout=2) if hasattr(_dashboard(), "get_ice_maker_status") else {},
    )


def _current_diagnostics(*, check_ha=False):
    if _dashboard() is None:
        return diagnostics.collect_diagnostics({})
    ha_connection = {"checked": False}
    ha_health = {"checked": False}
    ha_states = None
    fridge_histories = None
    if check_ha:
        ha_settings = cfg.get_ha_settings(_dashboard().config, include_env=True)
        ha_health = discovery.check_ha_core_health(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        result = discovery.test_ha_connection(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        ha_connection = {
            "checked": True,
            "ok": bool(result.get("ok")),
            "message": result.get("message") or result.get("error") or "",
            "entity_count": result.get("entity_count"),
        }
        states_result = discovery.get_ha_states(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        if states_result.get("ok"):
            ha_states = states_result.get("states")
        fridge_histories = {}
        for entity_id in (diagnostics.FRIDGE_DOOR_ENTITY, diagnostics.FREEZER_DOOR_ENTITY):
            history_result = discovery.get_entity_history(
                entity_id,
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port"),
                timeout=5,
            )
            if history_result.get("ok"):
                fridge_histories[entity_id] = history_result.get("history", [])
    listener_status = _dashboard().ha_listener.status() if hasattr(_dashboard(), "ha_listener") else {}
    return diagnostics.collect_diagnostics(
        _dashboard().config,
        ha_listener_status=listener_status,
        ha_connection=ha_connection,
        ha_health=ha_health,
        ha_states=ha_states,
        fridge_histories=fridge_histories,
    )


def _current_ha_states(timeout=8):
    if _dashboard() is None:
        return {"ok": False, "message": "System not ready.", "states": []}
    ha_settings = cfg.get_ha_settings(_dashboard().config, include_env=True)
    return discovery.get_ha_states(
        token=ha_settings.get("ha_token"),
        ha_ip=ha_settings.get("ha_ip"),
        ha_port=ha_settings.get("ha_port"),
        timeout=timeout,
    )


def _save_current_ha_snapshot():
    if _dashboard() is None:
        return {"ok": False, "message": "System not ready."}
    states_result = _current_ha_states(timeout=8)
    if not states_result.get("ok"):
        return {
            "ok": False,
            "message": states_result.get("message") or states_result.get("error") or "Could not read Home Assistant states.",
        }
    listener_status = _dashboard().ha_listener.status() if hasattr(_dashboard(), "ha_listener") else {}
    return diagnostics.save_ha_integration_snapshot(
        _dashboard().config,
        ha_states=states_result.get("states", []),
        ha_listener_status=listener_status,
    )


@route("/remote/diagnostics", methods=["GET"])
def web_diagnostics():
    if _dashboard() is None:
        return "System initializing, please refresh...", 503
    diag = _current_diagnostics(check_ha=request.args.get("check_ha") == "1")
    wants_json = request.accept_mimetypes.best == "application/json" or request.args.get("format") == "json"
    if wants_json:
        return jsonify(diag)
    return "<pre>" + diagnostics.diagnostics_text(diag).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>"


@route("/remote/diagnostics/ha_snapshot", methods=["POST"])
def web_ha_snapshot():
    if _dashboard() is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        result = _save_current_ha_snapshot()
        if result.get("ok"):
            diff = result.get("diff", {})
            flash(
                "HA snapshot saved: "
                f"{result.get('path')}. "
                f"Added {len(diff.get('added', []))}, removed {len(diff.get('removed', []))}, changed {len(diff.get('changed', []))}."
            )
        else:
            flash(f"HA snapshot failed: {result.get('message') or 'unknown error'}")
    except Exception as e:
        logging.exception("HA snapshot creation failed")
        flash(f"HA snapshot failed: {e}")
    return redirect(url_for("remote_ui"))


@route("/remote/diagnostics/reload_fridge_smartthings", methods=["POST"])
def web_reload_fridge_smartthings():
    if _dashboard() is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        import asyncio

        ha_settings = cfg.get_ha_settings(_dashboard().config, include_env=True)
        entry = asyncio.run(viper_health.find_config_entry_for_entity(
            ha_settings.get("ha_ip"),
            ha_settings.get("ha_port") or "8123",
            ha_settings.get("ha_token"),
            diagnostics.FRIDGE_DOOR_ENTITY,
        ))
        if not entry.get("ok"):
            flash(f"Refrigerator SmartThings reload failed: {entry.get('message') or 'config entry not found'}")
            return redirect(url_for("remote_ui") + "#diagnostics-heading")
        result = viper_health.reload_config_entry(
            ha_settings.get("ha_ip"),
            ha_settings.get("ha_port") or "8123",
            ha_settings.get("ha_token"),
            entry.get("config_entry_id"),
        )
        viper_health.record_health_event(
            "manual_smartthings_reload",
            "ok" if result.get("ok") else "failed",
            f"Manual refrigerator SmartThings reload from web remote: {result.get('message') or 'unknown result'}",
            details={"entry": entry, "result": result},
        )
        if result.get("ok"):
            flash("Refrigerator SmartThings entry reloaded. Open and close the fridge once, then refresh diagnostics.")
        else:
            flash(f"Refrigerator SmartThings reload failed: {result.get('message') or 'unknown error'}")
    except Exception as e:
        logging.exception("Refrigerator SmartThings reload failed")
        flash(f"Refrigerator SmartThings reload failed: {e}")
    return redirect(url_for("remote_ui") + "#diagnostics-heading")


@route("/remote/diagnostics/support_bundle", methods=["POST"])
def web_support_bundle():
    if _dashboard() is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        diag = _current_diagnostics(check_ha=True)
        result = diagnostics.create_support_bundle(
            _dashboard().config,
            ha_listener_status=diag.get("ha_listener", {}),
            ha_connection=diag.get("ha_connection", {}),
            ha_health=diag.get("ha_health", {}),
            setup_summary=_dashboard().build_setup_checklist_summary() if hasattr(_dashboard(), "build_setup_checklist_summary") else "",
            setup_events=getattr(_dashboard(), "setup_events", []),
            last_setup_status=getattr(_dashboard(), "last_setup_status", ""),
        )
        flash(f"Support bundle created: {result['path']}")
    except Exception as e:
        logging.exception("Support bundle creation failed")
        flash(f"Support bundle failed: {e}")
    return redirect(url_for("remote_ui"))


@route("/remote/setup/smoke", methods=["POST"])
def web_setup_smoke():
    if _dashboard() is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        report = _dashboard()._format_safe_smoke_report(_dashboard()._collect_safe_smoke_results())
        _dashboard().last_remote_setup_smoke_report = report
        flash("Safe setup smoke test finished. Review Setup Status for PASS/FIX details.")
    except Exception as e:
        logging.exception("Remote setup smoke test failed")
        _dashboard().last_remote_setup_smoke_report = f"Smoke Test: ERROR\n\nThe smoke test failed: {e}"
        flash(f"Safe setup smoke test failed: {e}")
    return redirect(url_for("remote_ui") + "#setup-status-heading")


@route("/remote/setup/restore_optional", methods=["POST"])
def web_restore_optional_setup():
    if _dashboard() is None:
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    try:
        skips = _dashboard()._setup_skip_state()
        if not any(skips.values()):
            flash("No optional setup items are currently skipped.")
        else:
            restored = [key for key, value in skips.items() if value]
            _dashboard().config["setup_skips"] = {key: False for key in skips}
            cfg.save_config(_dashboard().config)
            _dashboard().record_setup_event("optional_setup_restored_remote", "Restored skipped optional setup items from the remote web UI.", restored=", ".join(restored))
            flash("Restored optional setup items. They will show as available again.")
    except Exception as e:
        logging.exception("Remote optional setup restore failed")
        flash(f"Could not restore optional setup items: {e}")
    return redirect(url_for("remote_ui") + "#setup-status-heading")

def _web_vacuum_switch_next_action(state):
    state = str(state or "").strip().lower()
    turn_on = state != "on"
    return {
        "turn_on": turn_on,
        "button_label": "Turn on" if turn_on else "Turn off",
        "next_state": "on" if turn_on else "off",
    }

def _build_web_vacuum_context():
    empty = {
        "ok": False,
        "message": "Home Assistant is not ready.",
        "vacuums": [],
        "selected": "",
        "selected_entity": None,
        "state": "unknown",
        "actions": [],
        "status_lines": [],
        "cleaning_modes": [{"value": key, "label": VACUUM_CLEANING_MODES[key]} for key in VACUUM_CLEANING_MODE_ORDER],
        "cleaning_mode": "vacuum_mop",
        "cleaning_mode_label": VACUUM_CLEANING_MODES["vacuum_mop"],
        "room_repeat_count": 1,
        "fan_speeds": [],
        "settings": [],
        "related_controls": [],
        "rooms": [],
    }
    if not _dashboard():
        return empty
    ha_settings = cfg.get_ha_settings(_dashboard().config, include_env=True)
    result = discovery.get_ha_states(
        token=ha_settings.get("ha_token"),
        ha_ip=ha_settings.get("ha_ip"),
        ha_port=ha_settings.get("ha_port"),
        timeout=8,
    )
    if not result.get("ok"):
        empty["message"] = result.get("message") or result.get("error") or "Home Assistant scan failed."
        return empty
    states = result.get("states", [])
    vacuums = [entity for entity in states if _ha_domain_from_entity_id(entity.get("entity_id", "")) == "vacuum"]
    roborock_vacuums = [entity for entity in vacuums if _web_looks_like_roborock(entity)]
    vacuums = roborock_vacuums or vacuums
    selected = request.values.get("vacuum_entity", "")
    if selected and not any(entity.get("entity_id") == selected for entity in vacuums):
        selected = ""
    selected = selected or (vacuums[0].get("entity_id") if vacuums else "")
    selected_entity = next((entity for entity in vacuums if entity.get("entity_id") == selected), None)
    tokens = _web_vacuum_tokens(selected)
    related = []
    for entity in states:
        entity_id = entity.get("entity_id", "")
        domain = _ha_domain_from_entity_id(entity_id)
        if domain not in {"vacuum", "select", "number", "switch", "button", "sensor", "binary_sensor"}:
            continue
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        text = " ".join(str(part).lower() for part in [entity_id, attrs.get("friendly_name"), attrs.get("manufacturer"), attrs.get("model")])
        if entity_id == selected or any(token and token in text for token in tokens):
            related.append(entity)
    related = sorted(related, key=lambda e: (_ha_domain_from_entity_id(e.get("entity_id", "")), _web_entity_name(e).lower()))
    try:
        _dashboard()._last_web_vacuum_controls = getattr(_dashboard(), "_last_web_vacuum_controls", {})
        _dashboard()._last_web_vacuum_controls[selected] = related
    except Exception:
        pass
    attrs = selected_entity.get("attributes") if selected_entity and isinstance(selected_entity.get("attributes"), dict) else {}
    status_lines = []
    if selected_entity:
        battery = attrs.get("battery_level")
        if battery is None:
            battery_entity = next((entity for entity in related if entity.get("entity_id", "").endswith("_battery")), None)
            battery = battery_entity.get("state") if battery_entity else "unknown"
        status_lines.extend([
            f"Selected: {_web_short_entity_label(selected_entity)}",
            f"State: {selected_entity.get('state', 'unknown')}",
            f"Battery: {battery}",
            f"Current suction speed: {attrs.get('fan_speed', 'unknown')}",
        ])
        for entity in related:
            domain = _ha_domain_from_entity_id(entity.get("entity_id", ""))
            if domain in {"sensor", "binary_sensor"}:
                status_lines.append(f"{_web_short_entity_label(entity)}: {entity.get('state', 'unknown')}")
    settings = []
    for entity in related:
        if not _web_show_vacuum_setting(entity):
            continue
        entity_attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        next_action = _web_vacuum_switch_next_action(entity.get("state")) if _ha_domain_from_entity_id(entity.get("entity_id", "")) == "switch" else {}
        settings.append({
            "entity_id": entity.get("entity_id", ""),
            "domain": _ha_domain_from_entity_id(entity.get("entity_id", "")),
            "label": _web_short_entity_label(entity),
            "state": entity.get("state", ""),
            "button_label": next_action.get("button_label", ""),
            "turn_on": "1" if next_action.get("turn_on") else "0",
            "next_state": next_action.get("next_state", ""),
            "options": [str(option) for option in entity_attrs.get("options", [])] if isinstance(entity_attrs.get("options"), list) else [],
            "min": entity_attrs.get("min", 0),
            "max": entity_attrs.get("max", 100),
            "step": entity_attrs.get("step", 1),
        })
    rooms = _dashboard().config.get("vacuum_rooms", {}).get(selected, [])
    current_mode = _normalize_vacuum_cleaning_mode(_dashboard().config.get("vacuum_cleaning_mode", "vacuum_mop"))
    room_repeat_count = max(1, min(3, int(_dashboard().config.get("vacuum_room_repeat_count", 1) or 1)))
    fan_speeds = [str(speed) for speed in attrs.get("fan_speed_list", [])] if isinstance(attrs.get("fan_speed_list"), list) else []
    return {
        "ok": bool(vacuums),
        "message": "Vacuum controls loaded." if vacuums else "No vacuum entities found in Home Assistant.",
        "vacuums": [{"entity_id": entity.get("entity_id", ""), "label": _web_short_entity_label(entity), "state": entity.get("state", "unknown")} for entity in vacuums],
        "selected": selected,
        "selected_entity": selected_entity,
        "state": selected_entity.get("state", "unknown") if selected_entity else "unknown",
        "actions": vacuum_basic_actions_for_state(selected_entity.get("state", "unknown") if selected_entity else "unknown"),
        "status_lines": status_lines,
        "cleaning_modes": [{"value": key, "label": VACUUM_CLEANING_MODES[key]} for key in VACUUM_CLEANING_MODE_ORDER],
        "cleaning_mode": current_mode,
        "cleaning_mode_label": VACUUM_CLEANING_MODES[current_mode],
        "room_repeat_count": room_repeat_count,
        "fan_speeds": fan_speeds,
        "current_fan_speed": str(attrs.get("fan_speed", "")),
        "settings": settings,
        "related_controls": related,
        "rooms": rooms,
    }

def _cached_web_vacuum_controls(entity_id):
    if not _dashboard():
        return []
    cache = getattr(_dashboard(), "_last_web_vacuum_controls", {})
    return cache.get(entity_id, []) if isinstance(cache, dict) else []

@route("/remote/tts_engine", methods=["POST"])
def web_set_tts_engine():
    if _dashboard():
        new_engine = request.form.get("tts_engine")
        new_edge = request.form.get("edge_tts_voice")
        new_gemini = request.form.get("gemini_tts_voice")
        new_tld = request.form.get("google_tts_tld")

        _dashboard().config["tts_engine"] = new_engine
        if new_edge: _dashboard().config["edge_tts_voice"] = new_edge
        if new_gemini: _dashboard().config["gemini_tts_voice"] = new_gemini
        if new_tld: _dashboard().config["google_tts_tld"] = new_tld

        _dashboard().save_config()
        wx.CallAfter(_dashboard().tts_engine_choice.SetStringSelection, new_engine)
        wx.CallAfter(_dashboard()._update_secondary_voice_ui)
        flash(f"Voice Settings Saved. TTS set to {new_engine}")
    return redirect(url_for("remote_ui"))

@route("/remote/chimes/test/<door>", methods=["POST"])
def web_test_chime(door):
    if _dashboard():
        chime_file = request.form.get(f"{door}_chime")
        safe_submit(audio.test_specific_chime, chime_file, door)
        flash(f"Sent test chime to {door} door.")
    return redirect(url_for("remote_ui"))

@route("/remote/chimes/save", methods=["POST"])
def web_save_chimes():
    if _dashboard():
        f_val = request.form.get("front_chime")
        b_val = request.form.get("back_chime")
        _dashboard().config["front_chime"] = "" if f_val == "(Default)" else f_val
        _dashboard().config["back_chime"] = "" if b_val == "(Default)" else b_val
        _dashboard().save_config()
        wx.CallAfter(_dashboard()._populate_chimes)
        flash("Custom chimes saved successfully.")
    return redirect(url_for("remote_ui"))


@route("/remote/doorbell/video_settings", methods=["POST"])
def web_save_doorbell_video_settings():
    if _dashboard() is None:
        return _json_or_redirect("System not ready.", ok=False, status_code=503)
    current = _doorbell_video_settings(_dashboard().config)
    mode = (request.form.get("video_mode") or current["mode"]).strip().lower()
    if mode not in vision.VIDEO_ANALYSIS_MODES:
        mode = current["mode"]
    manual_seconds = vision.clamp_manual_video_seconds(request.form.get("manual_clip_seconds"), _dashboard().config)
    settings = dict(current)
    settings["mode"] = mode
    settings["manual_clip_seconds"] = manual_seconds
    _dashboard().config["doorbell_video_analysis"] = settings
    _dashboard().save_config()
    if hasattr(_dashboard(), "_refresh_video_analysis_controls"):
        wx.CallAfter(_dashboard()._refresh_video_analysis_controls)
    return _json_or_redirect(
        f"Doorbell video mode saved: {vision.VIDEO_ANALYSIS_LABELS.get(mode, mode)}. Manual clips are {manual_seconds} seconds."
    )


@route("/remote/doorbell/video_analyze/<side>", methods=["POST"])
def web_analyze_doorbell_video(side):
    if _dashboard() is None:
        return _json_or_redirect("System not ready.", ok=False, status_code=503)
    side = "back" if side == "back" else "front"
    seconds = vision.clamp_manual_video_seconds(request.form.get("manual_clip_seconds"), _dashboard().config)
    future = safe_submit(_dashboard()._run_manual_doorbell_video_analysis, side, seconds, "remote web interface")
    if future is None:
        return _json_or_redirect("Video analysis rejected because Viper is shutting down.", ok=False, status_code=503)
    return _json_or_redirect(f"Started {side} camera video analysis for {seconds} seconds. Viper will speak the result.")


@route("/remote/broadcast", methods=["POST"])
def web_broadcast():
    payload = request.get_json(silent=True) or {}
    msg     = payload.get("broadcast_text") or request.form.get("broadcast_text", "")
    channel = payload.get("channel")        or request.form.get("channel", "manual")
    return _broadcast_message(msg, push=False, channel=channel)

@route("/remote/broadcast_push", methods=["POST"])
def web_broadcast_push():
    payload = request.get_json(silent=True) or {}
    msg     = payload.get("broadcast_text") or request.form.get("broadcast_text", "")
    channel = payload.get("channel")        or request.form.get("channel", "manual")
    return _broadcast_message(msg, push=True, channel=channel)

@route("/remote/utils/engine", methods=["POST"])
def web_set_engine():
    if _dashboard():
        new_engine = request.form.get("engine_name")
        _dashboard().config["vision_engine"] = new_engine
        _dashboard().save_config()
        wx.CallAfter(_dashboard().engine_choice.SetStringSelection, new_engine)
        flash(f"Vision Engine switched to {new_engine}")
    return redirect(url_for("remote_ui"))

@route("/remote/toggle", methods=["POST"])
def web_toggle_arm():
    if _dashboard():
        _dashboard().on_toggle_arm(None)
        status = "Armed" if _dashboard().is_armed else "Disarmed"
        flash(f"System {status} successfully.")
    return redirect(url_for("remote_ui"))

@route("/remote/global_mute", methods=["POST"])
def web_toggle_global_mute():
    if _dashboard():
        muted = request.form.get("global_mute") == "1"
        _dashboard().set_global_mute(muted, source="remote")
        flash(f"Global mute {'enabled' if muted else 'disabled'}.")
    return redirect(url_for("remote_ui") + "#dashboard-heading")

@route("/remote/speaker/toggle/<name>", methods=["POST"])
def web_speaker_toggle(name):
    if _dashboard() and name in _dashboard().config["speakers"]:
        current = _dashboard().config["speakers"][name]["enabled"]
        new_state = not current
        _dashboard().config["speakers"][name]["enabled"] = new_state
        _dashboard().save_config()
        status_msg = f"{name} {'enabled' if new_state else 'disabled'}"
        wx.CallAfter(_dashboard().notify, f"{status_msg} via web", priority=10)
        wx.CallAfter(_dashboard().refresh_speaker_list)
        spk_type = _dashboard().config["speakers"][name]["type"]
        spk_id = _dashboard().config["speakers"][name]["id"]
        safe_submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)
        flash(f"Speaker {status_msg}")
    return redirect(url_for("remote_ui"))

@route("/remote/speaker/test/<name>", methods=["POST"])
def web_speaker_test(name):
    if _dashboard() and name in _dashboard().config["speakers"]:
        spk = _dashboard().config["speakers"][name]
        status = f"Testing connection to {name}."
        wx.CallAfter(_dashboard().notify, status, priority=10)
        safe_submit(audio.announce_specific_speaker, spk["type"], spk["id"], status)
        flash(f"Sent test chime to {name}")
    return redirect(url_for("remote_ui"))


@route("/remote/speaker/settings/<name>", methods=["POST"])
def web_speaker_settings(name):
    if _dashboard() and name in _dashboard().config["speakers"]:
        spk = _dashboard().config["speakers"][name]
        spk["doorbell"] = "doorbell" in request.form
        spk["utilities"] = "utilities" in request.form
        spk["fridge"] = "fridge" in request.form
        spk["quiet_hours_exempt"] = "quiet_hours_exempt" in request.form
        _dashboard().save_config()
        wx.CallAfter(_dashboard().refresh_speaker_list)
        wx.CallAfter(_dashboard()._sync_speaker_routing_controls)
        flash(f"Saved routing for {name}.")
    return redirect(url_for("remote_ui"))

@route("/remote/settings/quiet_hours", methods=["POST"])
def web_save_quiet_hours():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    _dashboard().config["quiet_hours_enabled"] = "quiet_hours_enabled" in request.form
    _dashboard().config["quiet_hours_start"] = request.form.get("quiet_hours_start", "22:00").strip() or "22:00"
    _dashboard().config["quiet_hours_end"] = request.form.get("quiet_hours_end", "07:00").strip() or "07:00"
    _dashboard().save_config()
    wx.CallAfter(_dashboard()._sync_quiet_hours_controls)
    flash("Quiet hours settings saved.")
    return redirect(url_for("remote_ui"))

@route("/remote/vacuum/action", methods=["POST"])
def web_vacuum_action():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    service = request.form.get("service", "").strip()
    allowed = {
        "vacuum/start",
        "vacuum/pause",
        "vacuum/stop",
        "vacuum/return_to_base",
        "vacuum/locate",
        "vacuum/clean_spot",
    }
    if not entity_id or service not in allowed:
        flash("Vacuum action was missing a vacuum or valid action.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    mode = _normalize_vacuum_cleaning_mode(request.form.get("cleaning_mode") or _dashboard().config.get("vacuum_cleaning_mode"))
    _dashboard().config["vacuum_cleaning_mode"] = mode
    if service == "vacuum/start":
        for mode_service, mode_payload in vacuum_cleaning_mode_service_calls(entity_id, _cached_web_vacuum_controls(entity_id), mode):
            _dashboard()._call_ha_service_data(mode_service, mode_payload, timeout=30)
    ok = _dashboard()._call_ha_service_data(service, {"entity_id": entity_id})
    _dashboard().save_config()
    action_name = service.replace("/", ".")
    if service == "vacuum/start":
        action_name = f"{VACUUM_CLEANING_MODES[mode]} start"
    flash(f"Sent {action_name} to {entity_id}." if ok else "Vacuum action failed. Check the Viper log.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@route("/remote/vacuum/cleaning_mode", methods=["POST"])
def web_vacuum_cleaning_mode():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    mode = _normalize_vacuum_cleaning_mode(request.form.get("cleaning_mode"))
    _dashboard().config["vacuum_cleaning_mode"] = mode
    _dashboard().save_config()
    flash(f"Vacuum cleaning mode saved: {VACUUM_CLEANING_MODES[mode]}.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))


@route("/remote/vacuum/room_repeat", methods=["POST"])
def web_vacuum_room_repeat():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    try:
        repeat = int(request.form.get("repeat", "1"))
    except ValueError:
        repeat = 1
    repeat = max(1, min(3, repeat))
    _dashboard().config["vacuum_room_repeat_count"] = repeat
    _dashboard().save_config()
    flash(f"Room cleaning repeat count saved: {repeat}.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))


@route("/remote/vacuum/fan_speed", methods=["POST"])
def web_vacuum_fan_speed():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    fan_speed = request.form.get("fan_speed", "").strip()
    if not entity_id or not fan_speed:
        flash("Choose a vacuum and suction speed first.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = _dashboard()._call_ha_service_data("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": fan_speed})
    flash(f"Set suction speed to {fan_speed}." if ok else "Could not set suction speed.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@route("/remote/vacuum/setting", methods=["POST"])
def web_vacuum_setting():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    vacuum_entity = request.form.get("vacuum_entity", "").strip()
    entity_id = request.form.get("entity_id", "").strip()
    domain = request.form.get("domain", "").strip()
    if not entity_id:
        flash("Vacuum setting was missing an entity.")
        return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
    if _is_hidden_vacuum_setting_entity_id(entity_id):
        flash("That Roborock dock setting is hidden because Home Assistant reports it but rejects write attempts. Change it in Home Assistant until the Roborock integration exposes a reliable control.")
        return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
    if domain == "select":
        option = request.form.get("option", "").strip()
        ok = _dashboard()._call_ha_service_data("select/select_option", {"entity_id": entity_id, "option": option}, timeout=30)
        flash(f"Set {entity_id} to {option}." if ok else f"Could not set {entity_id}.")
    elif domain == "number":
        raw_value = request.form.get("value", "").strip()
        try:
            value = float(raw_value)
        except ValueError:
            flash("Number setting must be a valid number.")
            return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))
        ok = _dashboard()._call_ha_service_data("number/set_value", {"entity_id": entity_id, "value": value}, timeout=30)
        flash(f"Set {entity_id} to {value}." if ok else f"Could not set {entity_id}.")
    elif domain == "switch":
        turn_on = request.form.get("turn_on") == "1"
        service = "switch/turn_on" if turn_on else "switch/turn_off"
        ok = _dashboard()._call_ha_service_data(service, {"entity_id": entity_id})
        flash(f"Turned {'on' if turn_on else 'off'} {entity_id}." if ok else f"Could not change {entity_id}.")
    else:
        flash("Unsupported vacuum setting type.")
    return redirect(url_for("remote_ui", vacuum_entity=vacuum_entity))

@route("/remote/vacuum/rooms", methods=["POST"])
def web_vacuum_rooms():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    if not entity_id:
        flash("Choose a vacuum first.")
        return redirect(url_for("remote_ui"))
    result = _dashboard()._call_ha_service_response("roborock/get_maps", {"entity_id": entity_id})
    if not result.get("ok"):
        flash(result.get("message") or "Could not load Roborock rooms.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    rooms = _dashboard()._parse_roborock_rooms(result.get("data"), entity_id)
    _dashboard()._save_vacuum_rooms(entity_id, rooms)
    flash(f"Loaded and saved {len(rooms)} room{'s' if len(rooms) != 1 else ''} for {entity_id}.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@route("/remote/vacuum/clean_rooms", methods=["POST"])
def web_vacuum_clean_rooms():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    raw_segments = request.form.getlist("segments")
    try:
        segments = [int(segment) for segment in raw_segments]
        repeat = int(request.form.get("repeat", "1"))
    except ValueError:
        flash("Room clean request had an invalid segment or repeat count.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    repeat = max(1, min(3, repeat))
    if not entity_id or not segments:
        flash("Choose a vacuum and at least one room first.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    mode = _normalize_vacuum_cleaning_mode(request.form.get("cleaning_mode") or _dashboard().config.get("vacuum_cleaning_mode"))
    _dashboard().config["vacuum_cleaning_mode"] = mode
    _dashboard().config["vacuum_room_repeat_count"] = repeat
    for mode_service, mode_payload in vacuum_cleaning_mode_service_calls(entity_id, _cached_web_vacuum_controls(entity_id), mode):
        _dashboard()._call_ha_service_data(mode_service, mode_payload, timeout=30)
    payload = {"entity_id": entity_id, "command": "app_segment_clean", "params": [{"segments": segments, "repeat": repeat}]}
    ok = _dashboard()._call_ha_service_data("vacuum/send_command", payload)
    _dashboard().save_config()
    flash(f"Sent {VACUUM_CLEANING_MODES[mode].lower()} room clean request for {len(segments)} room{'s' if len(segments) != 1 else ''}." if ok else "Could not send room clean request.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@route("/remote/vacuum/advanced", methods=["POST"])
def web_vacuum_advanced():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    command = request.form.get("command", "").strip()
    params_text = request.form.get("params", "").strip()
    if not entity_id or not command:
        flash("Choose a vacuum and enter a command first.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    payload = {"entity_id": entity_id, "command": command}
    if params_text:
        try:
            payload["params"] = json.loads(params_text)
        except json.JSONDecodeError as exc:
            flash(f"Advanced command parameters are not valid JSON: {exc}")
            return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = _dashboard()._call_ha_service_data("vacuum/send_command", payload)
    flash(f"Sent advanced command {command}." if ok else "Could not send advanced command.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@route("/remote/vacuum/goto", methods=["POST"])
def web_vacuum_goto():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    entity_id = request.form.get("vacuum_entity", "").strip()
    try:
        x = int(request.form.get("x", "").strip())
        y = int(request.form.get("y", "").strip())
    except ValueError:
        flash("Roborock coordinates must be whole numbers.")
        return redirect(url_for("remote_ui", vacuum_entity=entity_id))
    ok = _dashboard()._call_ha_service_data("roborock/set_vacuum_goto_position", {"entity_id": entity_id, "x": x, "y": y})
    flash(f"Sent vacuum to coordinates {x}, {y}." if ok else "Could not send go-to-position request.")
    return redirect(url_for("remote_ui", vacuum_entity=entity_id))

@route("/remote/ice/on", methods=["POST"])
def web_ice_maker_on():
    if _dashboard():
        _dashboard().on_ice_maker_on(None)
        flash("Ice maker forced on. Auto-off override enabled.")
    return redirect(url_for("remote_ui"))

@route("/remote/ice/off", methods=["POST"])
def web_ice_maker_off():
    if _dashboard():
        _dashboard().on_ice_maker_off(None)
        flash("Ice maker turned off. Auto-off override cleared.")
    return redirect(url_for("remote_ui"))

@route("/remote/ice/toggle", methods=["POST"])
def web_ice_maker_toggle():
    if _dashboard():
        result = _dashboard().on_ice_maker_toggle(None)
        flash(result or "Ice maker toggle requested.")
    return redirect(url_for("remote_ui"))


@route("/remote/speaker/add", methods=["POST"])
def web_speaker_add():
    if _dashboard():
        name = request.form.get("name")
        spk_type = request.form.get("type")
        spk_id = request.form.get("id")
        if name and spk_id:
            _dashboard().config["speakers"][name] = {"id": spk_id, "type": spk_type, "enabled": True, "doorbell": True, "utilities": True, "fridge": True, "quiet_hours_exempt": False}
            _dashboard().save_config()
            wx.CallAfter(_dashboard().notify, f"Added speaker {name}")
            wx.CallAfter(_dashboard().refresh_speaker_list)
            flash(f"Speaker {name} added.")
    return redirect(url_for("remote_ui"))

@route("/remote/speaker/rename", methods=["POST"])
def web_speaker_rename():
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if not _dashboard():
        return _json_or_redirect("System not ready.", ok=False, status_code=503)
    if not old_name or old_name not in _dashboard().config["speakers"]:
        return _json_or_redirect("Original speaker was not found.", ok=False, status_code=404)
    if not new_name:
        return _json_or_redirect("New speaker name cannot be blank.", ok=False, status_code=400)
    if new_name != old_name and new_name in _dashboard().config["speakers"]:
        return _json_or_redirect(f"A speaker named {new_name} already exists.", ok=False, status_code=409)

    data = _dashboard().config["speakers"].pop(old_name)
    _dashboard().config["speakers"][new_name] = data
    _dashboard().save_config()
    wx.CallAfter(_dashboard().notify, f"Renamed {old_name} to {new_name}")
    wx.CallAfter(_dashboard().refresh_speaker_list)
    return _json_or_redirect(f"Renamed {old_name} to {new_name}")

@route("/remote/speaker/delete/<name>", methods=["POST"])
def web_speaker_delete(name):
    if _dashboard() and name in _dashboard().config["speakers"]:
        del _dashboard().config["speakers"][name]
        _dashboard().save_config()
        wx.CallAfter(_dashboard().notify, f"Removed speaker {name}")
        wx.CallAfter(_dashboard().refresh_speaker_list)
        flash(f"Speaker {name} deleted.")
    return redirect(url_for("remote_ui"))

@route("/remote/switch_prompt", methods=["POST"])
def web_switch_prompt():
    if _dashboard():
        new_p = request.form.get("profile_name")
        _dashboard().config["active_prompt"] = new_p
        wx.CallAfter(_dashboard().prompt_choice.SetStringSelection, new_p)
        wx.CallAfter(_dashboard().prompt_editor.SetValue, _dashboard().config["prompts"][new_p])
        _dashboard().save_config()
        flash(f"Switched to {new_p} profile.")
    return redirect(url_for("remote_ui"))

@route("/remote/save_prompt", methods=["POST"])
def web_save_prompt():
    if _dashboard():
        new_text = request.form.get("prompt_text")
        active_p = _dashboard().config["active_prompt"]
        _dashboard().config["prompts"][active_p] = new_text
        wx.CallAfter(_dashboard().prompt_editor.SetValue, new_text)
        _dashboard().save_config()
        flash("AI instructions saved.")
    return redirect(url_for("remote_ui"))

@route("/remote/utils/api", methods=["POST"])
def web_api_check():
    if _dashboard():
        _dashboard().on_api(None)
        flash("API Check requested. Listen for announcement.")
    return redirect(url_for("remote_ui"))

@route("/remote/utils/batt", methods=["POST"])
def web_batt_check():
    if _dashboard():
        _dashboard().on_batt(None)
        flash("Battery Check requested. Listen for announcement.")
    return redirect(url_for("remote_ui"))

@route("/remote/utils/filter", methods=["POST"])
def web_filter_check():
    if _dashboard():
        _dashboard().on_filter(None)
        flash("Filter Check requested. Listen for announcement.")
    return redirect(url_for("remote_ui"))

@route("/remote/utils/scan_sonos", methods=["POST"])
def web_scan_sonos():
    if _dashboard():
        _dashboard().on_scan_sonos(None)
        flash("Sonos scan started. Listen for results and check your phone.")
    return redirect(url_for("remote_ui"))

@route("/remote/utils/scan_ha", methods=["POST"])
def web_scan_ha():
    if _dashboard():
        _dashboard().on_scan_ha(None)
        flash("Home Assistant scan started. Check your PC screen.")
    return redirect(url_for("remote_ui"))



@route("/remote/cinderella/save/<bucket>", methods=["POST"])
def web_save_cinderella_bucket(bucket):
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(_dashboard().config)
    raw_text = request.form.get("messages", "")
    values = [line.strip() for line in raw_text.splitlines() if line.strip()]
    valid_buckets = {"departure", "washing", "emptying", "drying", "returning", "victory", "paused", "status_update", "vacuum_error_templates", "dock_error_templates"}
    if bucket not in valid_buckets:
        flash("Unknown Cinderella message bucket.")
        return redirect(url_for("remote_ui"))
    messages[bucket] = values
    _dashboard().save_config()
    flash(f"Saved Cinderella messages for {bucket.replace('_', ' ')}.")
    return redirect(url_for("remote_ui"))


@route("/remote/cinderella/error/add", methods=["POST"])
def web_add_cinderella_error_bucket():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(_dashboard().config)
    error_name = (request.form.get("error_name", "") or "").strip().lower().replace(" ", "_")
    if not error_name:
        flash("Error bucket name cannot be blank.")
        return redirect(url_for("remote_ui"))
    specific = messages.setdefault("specific_errors", {})
    if error_name not in specific:
        specific[error_name] = [f"Cinderella has a very specific complaint: {error_name.replace('_', ' ')}."]
        _dashboard().save_config()
        flash(f"Added Cinderella error bucket: {error_name}.")
    else:
        flash(f"Cinderella error bucket already exists: {error_name}.")
    return redirect(url_for("remote_ui"))


@route("/remote/cinderella/error/save/<error_name>", methods=["POST"])
def web_save_cinderella_error_bucket(error_name):
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(_dashboard().config)
    specific = messages.setdefault("specific_errors", {})
    raw_text = request.form.get("messages", "")
    values = [line.strip() for line in raw_text.splitlines() if line.strip()]
    specific[error_name] = values
    _dashboard().save_config()
    flash(f"Saved Cinderella messages for specific error {error_name}.")
    return redirect(url_for("remote_ui"))


@route("/remote/cinderella/error/delete/<error_name>", methods=["POST"])
def web_delete_cinderella_error_bucket(error_name):
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    messages = ensure_cinderella_message_config(_dashboard().config)
    specific = messages.setdefault("specific_errors", {})
    if error_name in specific:
        del specific[error_name]
        _dashboard().save_config()
        flash(f"Deleted Cinderella error bucket {error_name}.")
    else:
        flash(f"Cinderella error bucket {error_name} was not found.")
    return redirect(url_for("remote_ui"))


@route("/cinderella", methods=["POST"])
def cinderella_message_endpoint():
    if _dashboard() is None or is_shutting_down.is_set():
        return jsonify({"ok": False, "error": "System not ready or shutting down."}), 503

    request_started = time.time()
    payload = request.get_json(silent=True) or {}
    if not payload and request.form:
        payload = request.form.to_dict(flat=True)

    event = (payload.get("event") or "").strip().lower()
    error = (payload.get("error") or "").strip().lower()
    source = (payload.get("source") or "vacuum").strip().lower()

    if not event:
        return jsonify({"ok": False, "error": "Missing event."}), 400

    logging.info(
        "[CINDERELLA EVENT] received event=%s source=%s error=%s",
        event,
        source,
        error or "none",
    )

    message = choose_cinderella_message(event, error=error, source=source)
    if not message:
        logging.warning(
            "[CINDERELLA EVENT] no message configured for event=%s source=%s error=%s",
            event,
            source,
            error or "none",
        )
        return jsonify({"ok": False, "error": f"No message configured for event {event}."}), 404

    context = {
        "channel": "cinderella",
        "event": event,
        "source": source,
        "error": error,
        "received_ts": request_started,
        "received_iso": datetime.now().isoformat(timespec="seconds"),
    }
    logging.info(
        "[CINDERELLA EVENT] selected message for event=%s source=%s: %s",
        event,
        source,
        message,
    )

    future = safe_submit(audio.announce_all, message, False, context)
    if future is None:
        logging.warning(
            "[CINDERELLA EVENT] announcement rejected during shutdown for event=%s source=%s",
            event,
            source,
        )
        return jsonify({"ok": False, "error": "Announcement rejected during shutdown."}), 503

    logging.info(
        "[CINDERELLA EVENT] queued event=%s source=%s total_request_time=%.3fs",
        event,
        source,
        time.time() - request_started,
    )
    return jsonify({"ok": True, "message": message})


@route("/remote/settings/broadcast_channels", methods=["POST"])
def web_save_broadcast_channels():
    if not _dashboard():
        flash("System not ready.")
        return redirect(url_for("remote_ui"))
    channels = _dashboard().config.setdefault("broadcast_channels", {})
    for key in request.form:
        if key.startswith("channel_") and key.endswith("_mode"):
            ch_name = key[len("channel_"):-len("_mode")]
            chime_val = request.form.get(f"channel_{ch_name}_chime", "")
            channels[ch_name] = {
                "mode":  _normalize_broadcast_mode(request.form.get(f"channel_{ch_name}_mode", "speak")),
                "chime": "" if chime_val == "(Default)" else chime_val,
            }
    _dashboard().config["broadcast_channels"] = channels
    _dashboard().save_config()
    wx.CallAfter(_dashboard()._sync_fridge_controls)
    flash("Fridge & Freezer channel settings saved.")
    return redirect(url_for("remote_ui"))



@route("/remote/fridge/test/<channel>", methods=["POST"])
def web_test_fridge_chime(channel):
    """Play the current or saved chime for a fridge/freezer channel on all speakers."""
    if _dashboard():
        posted_chime = request.form.get(f"channel_{channel}_chime", "")
        chime = "" if posted_chime in ("", "(Default)") else posted_chime

        if not chime:
            channels = _dashboard().config.get("broadcast_channels", {})
            ch_data  = channels.get(channel, {})
            chime    = ch_data.get("chime", "")

        label = channel.replace("_", " ").title()
        safe_submit(audio.play_broadcast_chime, chime, channel)
        flash(f"Testing chime for: {label}")
    return redirect(url_for("remote_ui"))
