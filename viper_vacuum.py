import re


HIDDEN_VACUUM_SETTING_SUFFIXES = {
    "_dock_empty_mode",
}

VACUUM_RUNNING_STATES = {
    "cleaning",
    "mopping",
    "segment_cleaning",
    "segment_mopping",
    "zone_cleaning",
    "zoned_cleaning",
    "spot_cleaning",
    "going_to_target",
    "returning",
    "returning_home",
    "docking",
    "washing_mop",
    "emptying_the_bin",
    "drying_the_mop",
}
VACUUM_PAUSED_STATES = {"paused"}
VACUUM_DOCKED_STATES = {"docked", "charging", "idle", "sleeping"}
VACUUM_CLEANING_MODES = {
    "vacuum_mop": "Vacuum and mop",
    "vacuum_only": "Vacuum only",
    "mop_only": "Mop only",
}
VACUUM_CLEANING_MODE_ORDER = ("vacuum_mop", "vacuum_only", "mop_only")


def ha_domain_from_entity_id(entity_id):
    return entity_id.split(".", 1)[0] if isinstance(entity_id, str) and "." in entity_id else ""


def web_entity_name(entity):
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    return str(attrs.get("friendly_name") or entity.get("entity_id") or "")


def web_short_entity_label(entity):
    name = web_entity_name(entity)
    entity_id = entity.get("entity_id", "")
    return f"{name} ({entity_id})" if name and name != entity_id else entity_id


def web_vacuum_tokens(entity_id):
    tokens = {"roborock", "cinderella", "saros", "qrevo", "q revo"}
    if entity_id and "." in entity_id:
        base = entity_id.split(".", 1)[1]
        tokens.add(base.lower())
        tokens.update(part for part in re.split(r"[_\s-]+", base.lower()) if len(part) >= 4)
    return tokens


def web_looks_like_roborock(entity):
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    text = " ".join(
        str(part).lower()
        for part in [
            entity.get("entity_id"),
            attrs.get("friendly_name"),
            attrs.get("manufacturer"),
            attrs.get("model"),
            attrs.get("platform"),
            attrs.get("integration"),
        ]
    )
    return any(token in text for token in ["roborock", "cinderella", "saros", "qrevo", "q revo", "s7", "s8"])


def is_hidden_vacuum_setting_entity_id(entity_id):
    entity_id = str(entity_id or "").lower()
    return any(entity_id.endswith(suffix) for suffix in HIDDEN_VACUUM_SETTING_SUFFIXES)


def web_show_vacuum_setting(entity):
    entity_id = entity.get("entity_id", "")
    domain = ha_domain_from_entity_id(entity_id)
    if is_hidden_vacuum_setting_entity_id(entity_id):
        return False
    if domain in {"select", "number"}:
        return True
    if domain == "switch" and "child_lock" in entity_id:
        return True
    return False


def normalize_vacuum_state(state):
    return str(state or "").strip().lower().replace(" ", "_").replace("-", "_")


def vacuum_basic_actions_for_state(state):
    normalized = normalize_vacuum_state(state)
    if normalized in VACUUM_RUNNING_STATES:
        return [
            {"service": "vacuum/pause", "label": "Pause cleaning", "style": "secondary"},
            {"service": "vacuum/stop", "label": "Stop cleaning", "style": "danger"},
            {"service": "vacuum/return_to_base", "label": "Return to dock", "style": "secondary"},
            {"service": "vacuum/locate", "label": "Locate vacuum", "style": "secondary"},
        ]
    if normalized in VACUUM_PAUSED_STATES:
        return [
            {"service": "vacuum/start", "label": "Resume cleaning", "style": "primary"},
            {"service": "vacuum/stop", "label": "Stop cleaning", "style": "danger"},
            {"service": "vacuum/return_to_base", "label": "Return to dock", "style": "secondary"},
            {"service": "vacuum/locate", "label": "Locate vacuum", "style": "secondary"},
        ]
    actions = [
        {"service": "vacuum/start", "label": "Start selected cleaning mode", "style": "primary"},
        {"service": "vacuum/locate", "label": "Locate vacuum", "style": "secondary"},
        {"service": "vacuum/clean_spot", "label": "Spot clean", "style": "secondary"},
    ]
    if normalized not in VACUUM_DOCKED_STATES:
        actions.insert(1, {"service": "vacuum/return_to_base", "label": "Return to dock", "style": "secondary"})
    return actions


def normalize_vacuum_cleaning_mode(mode):
    mode = str(mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "vacuum_and_mop": "vacuum_mop",
        "vac_and_mop": "vacuum_mop",
        "clean_and_mop": "vacuum_mop",
        "vacuum": "vacuum_only",
        "vac": "vacuum_only",
        "mop": "mop_only",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in VACUUM_CLEANING_MODES else "vacuum_mop"


def cleaning_mode_option_score(option, mode):
    text = str(option or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return 0
    if mode == "vacuum_mop":
        preferred = ("vacuum_and_mop", "vac_and_mop", "clean_and_mop", "standard", "balanced")
        blocked = ("mop_only", "vacuum_only")
    elif mode == "vacuum_only":
        preferred = ("vacuum_only", "vacuum", "vac", "sweep", "sweeping", "off", "none", "close")
        blocked = ("mop_only",)
    else:
        preferred = ("mop_only", "mop", "mopping", "deep", "deep_plus", "standard")
        blocked = ("vacuum_only",)
    if any(item == text for item in preferred):
        return 100
    if any(item in text for item in preferred):
        return 80
    if any(item in text for item in blocked):
        return -100
    return 0


def select_best_cleaning_mode_option(options, mode):
    scored = sorted(
        ((option, cleaning_mode_option_score(option, mode)) for option in options),
        key=lambda item: item[1],
        reverse=True,
    )
    return scored[0][0] if scored and scored[0][1] > 0 else ""


def vacuum_cleaning_mode_service_calls(entity_id, controls, mode, fan_speed=""):
    mode = normalize_vacuum_cleaning_mode(mode)
    calls = []
    for control in controls or []:
        control_id = control.get("entity_id", "")
        domain = ha_domain_from_entity_id(control_id)
        attrs = control.get("attributes") if isinstance(control.get("attributes"), dict) else {}
        name_text = " ".join(str(part).lower() for part in [control_id, attrs.get("friendly_name")])
        if domain == "select" and any(token in name_text for token in ("cleaning_mode", "clean mode", "mop_mode", "mop mode", "water_box_mode", "water box")):
            options = [str(option) for option in attrs.get("options", [])] if isinstance(attrs.get("options"), list) else []
            option = select_best_cleaning_mode_option(options, mode)
            if option:
                calls.append(("select/select_option", {"entity_id": control_id, "option": option}))
        elif domain == "number" and mode == "vacuum_only" and any(token in name_text for token in ("mop_intensity", "water", "flow")):
            minimum = attrs.get("min", 0)
            try:
                value = float(minimum)
            except (TypeError, ValueError):
                value = 0
            calls.append(("number/set_value", {"entity_id": control_id, "value": value}))
    if mode == "mop_only":
        fan_options = []
        selected_vacuum = next((control for control in controls or [] if control.get("entity_id") == entity_id), None)
        attrs = selected_vacuum.get("attributes") if selected_vacuum and isinstance(selected_vacuum.get("attributes"), dict) else {}
        if isinstance(attrs.get("fan_speed_list"), list):
            fan_options = [str(item) for item in attrs.get("fan_speed_list")]
        quiet = select_best_cleaning_mode_option(fan_options, "vacuum_only")
        if quiet and quiet != str(fan_speed):
            calls.append(("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": quiet}))
    return calls
