import logging


def _chime_target_summary(config: dict, channel: str) -> dict:
    """Return the configured audible chime targets for a fridge/freezer channel."""
    category = "fridge" if str(channel or "").lower().startswith(("fridge", "freezer")) else "manual"
    ha_targets, sonos_targets, alexa_targets = [], [], []
    for _name, spk in (config.get("speakers") or {}).items():
        if not spk.get("enabled", True):
            continue
        if category == "fridge" and not spk.get("fridge", True):
            continue
        spk_type = spk.get("type")
        spk_id = spk.get("id")
        if not spk_id:
            continue
        if spk_type == "ha":
            ha_targets.append(spk_id)
        elif spk_type == "sonos":
            sonos_targets.append(spk_id)
        elif spk_type == "alexa" and config.get("enable_alexa", False):
            alexa_targets.append(spk_id)
    total = len(ha_targets) + len(sonos_targets) + len(alexa_targets)
    return {
        "ha_targets": ha_targets,
        "sonos_targets": sonos_targets,
        "alexa_targets": alexa_targets,
        "target_count": total,
        "has_targets": total > 0,
    }


def resolve_channel_settings(channel: str, config: dict) -> dict:
    """Return mode+chime for a channel with fridge/freezer fallback chains."""
    channels = config.get("broadcast_channels", {})
    ch_key = (channel or "").lower()

    candidates = [ch_key]
    if ch_key in ("fridge_open", "fridge_closed"):
        candidates.append("fridge")
    elif ch_key in ("freezer_open", "freezer_closed"):
        candidates.append("freezer")
    candidates.append("default")

    for key in candidates:
        if key in channels:
            entry = channels[key]
            return {"mode": entry.get("mode", "speak"), "chime": entry.get("chime", "")}

    return {"mode": "speak", "chime": ""}


def normalize_broadcast_mode(mode) -> str:
    """Normalize saved/UI mode labels into internal mode names."""
    text = str(mode or "speak").strip().lower()
    text = text.replace("-", " ").replace("_", " ")
    aliases = {
        "speak": "speak",
        "voice": "speak",
        "spoken": "speak",
        "speak message": "speak",
        "chime": "chime",
        "chime only": "chime",
        "sound": "chime",
        "sound only": "chime",
        "tone": "chime",
        "tone only": "chime",
        "silent": "silent",
        "mute": "silent",
        "muted": "silent",
        "off": "silent",
        "log only": "silent",
    }
    return aliases.get(text, "speak")


def normalize_broadcast_message_text(message: str) -> str:
    return " ".join(str(message or "").strip().lower().rstrip(".!").split())


def infer_fridge_channel_from_message(message: str) -> str:
    normalized = normalize_broadcast_message_text(message)
    return {
        "the fridge door is open": "fridge_open",
        "the fridge door is closed": "fridge_closed",
        "the refrigerator door is open": "fridge_open",
        "the refrigerator door is closed": "fridge_closed",
        "the freezer door is open": "freezer_open",
        "the freezer door is closed": "freezer_closed",
    }.get(normalized, "")


def resolve_broadcast_channel(channel: str, message: str) -> str:
    requested = str(channel or "").strip().lower()
    if requested in {"", "default", "manual"}:
        inferred = infer_fridge_channel_from_message(message)
        if inferred:
            return inferred
    return requested


def dispatch_broadcast_message(
    raw_message: str,
    *,
    config: dict,
    notify,
    submit,
    play_notification,
    play_broadcast_chime,
    send_text_push=None,
    system_ready: bool = True,
    push: bool = False,
    channel: str = "",
) -> dict:
    """Dispatch a broadcast according to channel configuration.

    The caller supplies side-effect callbacks so this module can stay independent
    of wx, Flask, and app globals.
    """
    if not system_ready:
        return {"ok": False, "message": "System not ready or shutting down.", "status_code": 503}

    msg = (raw_message or "").strip()
    if not msg:
        return {"ok": False, "message": "No message provided.", "status_code": 400}

    try:
        def submit_push_if_requested():
            if not push or send_text_push is None:
                return
            submit(send_text_push, "Home Alert", msg)

        if config.get("global_mute", False):
            notify(
                f"Global mute is on. Broadcast logged with no audio: {msg}",
                priority=3,
                interrupt=True,
                speak=False,
            )
            submit_push_if_requested()
            logging.info("[GLOBAL MUTE] Broadcast logged only channel=%r message=%r", channel or "default", msg)
            return {
                "ok": True,
                "message": f"Global mute is on. Broadcast logged with no audio: {msg}",
                "status_code": 200,
                "path": "muted",
                "resolved_channel": resolve_broadcast_channel(channel, msg),
            }

        requested_channel = str(channel or "").strip().lower()
        resolved_channel = resolve_broadcast_channel(channel, msg)

        # User-entered manual broadcasts always speak. Legacy HA fridge/freezer
        # messages can arrive as manual/default, so infer those before this branch.
        if requested_channel == "manual" and resolved_channel == "manual":
            ch_settings = {"mode": "speak", "chime": ""}
        else:
            ch_settings = resolve_channel_settings(resolved_channel, config)

        mode = normalize_broadcast_mode(ch_settings["mode"])
        chime = ch_settings["chime"]
        logging.info(
            "[BROADCAST ROUTE] requested_channel=%r resolved_channel=%r mode=%s chime=%r path=%s message=%r",
            requested_channel or "default",
            resolved_channel or "default",
            mode,
            chime,
            mode,
            msg,
        )

        notify(
            f"Broadcast [{resolved_channel or 'default'}] [{mode}]: {msg}",
            priority=3,
            interrupt=True,
            speak=(mode == "speak"),
        )

        if mode == "silent":
            submit_push_if_requested()
            logging.info("[BROADCAST] Silent channel=%r logged only: %r", resolved_channel, msg)
            return {
                "ok": True,
                "message": f"Broadcast logged (silent): {msg}",
                "status_code": 200,
                "path": "silent",
                "resolved_channel": resolved_channel,
            }

        if mode == "chime":
            target_summary = _chime_target_summary(config, resolved_channel)
            if not target_summary["has_targets"]:
                submit_push_if_requested()
                logging.warning(
                    "[BROADCAST] Chime channel=%r has no enabled audible targets. message=%r",
                    resolved_channel, msg,
                )
                return {
                    "ok": False,
                    "message": (
                        f"Chime not played for {resolved_channel or 'default'}: "
                        "no enabled speaker has fridge/freezer routing."
                    ),
                    "status_code": 409,
                    "path": "no_chime_targets",
                    "resolved_channel": resolved_channel,
                    "chime": chime,
                    "target_count": 0,
                }
            future = submit(play_broadcast_chime, chime, resolved_channel)
            if future is None:
                return {"ok": False, "message": "System shutting down.", "status_code": 503}
            submit_push_if_requested()
            logging.info("[BROADCAST] Chime channel=%r chime=%r for: %r", resolved_channel, chime, msg)
            return {
                "ok": True,
                "message": f"Chime played for: {msg}",
                "status_code": 200,
                "path": "chime",
                "resolved_channel": resolved_channel,
                "chime": chime,
                "target_count": target_summary["target_count"],
            }

        future = submit(play_notification, "manual", msg, push)
        if future is None:
            return {"ok": False, "message": "Broadcast rejected — system shutting down.", "status_code": 503}
        return {
            "ok": True,
            "message": f"Broadcast sent: {msg}",
            "status_code": 200,
            "path": "speak",
            "resolved_channel": resolved_channel,
        }

    except Exception as e:
        logging.exception("Broadcast route failed")
        return {"ok": False, "message": f"Broadcast failed: {e}", "status_code": 500}
