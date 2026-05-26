def configured_speaker_ids(config: dict) -> set:
    return {
        str(data.get("id") or "")
        for data in (config or {}).get("speakers", {}).values()
        if isinstance(data, dict) and data.get("id")
    }


def speaker_candidate_lines(candidates, title, configured_ids=None):
    lines = [title]
    if not candidates:
        lines.append("  None found.")
        return lines
    configured_ids = configured_ids or set()
    for item in candidates:
        configured = "already configured" if item.get("id") in configured_ids else "available"
        lines.append(f"  {item.get('name')} | {item.get('type')} | {item.get('id')} | {configured}")
    return lines


def split_speaker_candidates(ha_candidates, sonos_candidates):
    ha_sonos = [item for item in ha_candidates or [] if item.get("is_sonos")]
    ha_other = [item for item in ha_candidates or [] if not item.get("is_sonos")]
    ha_sonos_ids = {item.get("id") for item in ha_sonos}
    network_sonos = [
        item for item in sonos_candidates or []
        if item.get("id") not in ha_sonos_ids
    ]
    return ha_other, ha_sonos, network_sonos


def flatten_discovered_speaker_targets(ha_candidates, sonos_candidates, configured_ids=None):
    ha_other, ha_sonos, network_sonos = split_speaker_candidates(ha_candidates, sonos_candidates)
    configured_ids = configured_ids or set()
    targets = []
    for item in ha_other + ha_sonos + network_sonos:
        target = dict(item)
        target["configured"] = target.get("id") in configured_ids
        targets.append(target)
    return targets


def unique_speaker_name(config: dict, base_name, spk_type):
    speakers = (config or {}).setdefault("speakers", {})
    base = f"{base_name} ({str(spk_type or 'ha').upper()})"
    name = base
    suffix = 2
    while name in speakers:
        name = f"{base} {suffix}"
        suffix += 1
    return name


def add_discovered_speaker_targets(config: dict, targets, routes=None):
    speakers = (config or {}).setdefault("speakers", {})
    existing_ids = configured_speaker_ids(config)
    routes = routes or {}
    added = 0
    for item in targets or []:
        spk_id = item.get("id") or ""
        if not spk_id or spk_id in existing_ids:
            continue
        spk_type = item.get("type") or "ha"
        name = unique_speaker_name(config, item.get("name") or spk_id, spk_type)
        speakers[name] = {
            "id": spk_id,
            "type": spk_type,
            "enabled": True,
            "doorbell": bool(routes.get("doorbell", True)),
            "utilities": bool(routes.get("utilities", True)),
            "fridge": bool(routes.get("fridge", True)),
            "quiet_hours_exempt": bool(routes.get("quiet_hours_exempt", False)),
        }
        existing_ids.add(spk_id)
        added += 1
    return added


def discovered_speaker_summary_text(
    ha_candidates,
    sonos_candidates,
    *,
    configured_ids=None,
    ha_error="",
    sonos_error="",
):
    ha_other, ha_sonos, network_sonos = split_speaker_candidates(ha_candidates, sonos_candidates)
    configured_ids = configured_ids or set()
    lines = [
        "Available Speakers",
        "",
        "Check the speakers Viper should add, then press Add Selected Speakers.",
        "",
    ]
    if ha_error:
        lines.append(f"Home Assistant discovery: {ha_error}")
        lines.append("")
    if sonos_error:
        lines.append(f"Network Sonos discovery: {sonos_error}")
        lines.append("")
    lines.extend(speaker_candidate_lines(ha_other, "Home Assistant media players:", configured_ids))
    lines.append("")
    lines.extend(speaker_candidate_lines(ha_sonos, "Sonos speakers already visible in Home Assistant:", configured_ids))
    lines.append("")
    lines.extend(speaker_candidate_lines(network_sonos, "Network Sonos speakers not clearly visible in Home Assistant:", configured_ids))
    return "\n".join(lines)


def ha_speaker_candidates_from_result(result):
    categories = result.get("categories", {}) if isinstance(result, dict) else {}
    candidates = []
    for entity in categories.get("media_players", []):
        entity_id = entity.get("entity_id") or ""
        if not entity_id:
            continue
        name = entity.get("friendly_name") or entity_id.replace("media_player.", "")
        platform = (entity.get("platform") or entity.get("integration") or "").lower()
        search = " ".join(str(entity.get(key, "")) for key in ("entity_id", "friendly_name", "platform", "integration")).lower()
        spk_type = "alexa" if "alexa" in search or "echo" in search else "ha"
        if platform == "sonos" or "sonos" in search:
            spk_type = "ha"
        candidates.append({
            "name": name,
            "id": entity_id,
            "type": spk_type,
            "source": "Home Assistant",
            "is_sonos": platform == "sonos" or "sonos" in search,
        })
    return candidates


def sonos_speaker_candidates_from_soco(speakers):
    candidates = []
    for speaker in speakers or []:
        ip = getattr(speaker, "ip_address", "") or ""
        name = getattr(speaker, "player_name", "") or ip or "Unnamed Sonos"
        if not ip:
            continue
        candidates.append({
            "name": name,
            "id": ip,
            "type": "sonos",
            "source": "Network Sonos",
            "is_sonos": True,
        })
    return candidates
