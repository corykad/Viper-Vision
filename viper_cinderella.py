import random


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


def ensure_message_config(config_obj: dict, default_messages: dict, deep_merge) -> dict:
    current = config_obj.get("cinderella_messages", {}) if isinstance(config_obj, dict) else {}
    merged = deep_merge(default_messages, current)
    config_obj["cinderella_messages"] = merged
    return merged


def choose_message(config_obj: dict, event: str, error: str = "", source: str = "vacuum") -> str:
    messages = config_obj.get("cinderella_messages", {}) if isinstance(config_obj, dict) else {}
    error_key = (error or "").strip().lower().replace(" ", "_").replace("-", "_")
    cleaned_error = error_key.replace("_", " ") if error_key else ""

    event_key = (event or "").strip().lower().replace(" ", "_").replace("-", "_")
    if event_key != "error":
        event_key = CINDERELLA_STATUS_EVENT_MAP.get(event_key, event_key)

    if event_key == "error":
        specific = messages.get("specific_errors", {})
        specific_keys = [f"dock_{error_key}", error_key] if source == "dock" else [error_key]
        for specific_key in specific_keys:
            if specific_key and specific_key in specific and specific[specific_key]:
                return random.choice([m for m in specific[specific_key] if str(m).strip()])
        template_key = "dock_error_templates" if source == "dock" else "vacuum_error_templates"
        templates = [m for m in messages.get(template_key, []) if str(m).strip()]
        if not templates:
            templates = ["Cinderella has an issue: {error}."]
        return random.choice(templates).format(error=cleaned_error or "unknown issue")

    choices = [m for m in messages.get(event_key, []) if str(m).strip()]
    if choices:
        return random.choice(choices)
    return ""
