import logging
from copy import deepcopy

import requests

import viper_config as cfg


DEFAULT_TIMEOUT = 8

DISCOVERY_CATEGORIES = [
    "media_players",
    "alexa_media_players",
    "sonos_media_players",
    "door_sensors",
    "fridge_sensors",
    "freezer_sensors",
    "cameras",
    "ring_cameras",
    "switches",
    "ice_maker_candidates",
    "filter_sensors",
    "vacuum_entities",
    "roborock_entities",
]

SUMMARY_ATTRIBUTE_KEYS = [
    "app_id",
    "app_name",
    "attribution",
    "brand",
    "device_class",
    "entity_picture",
    "friendly_name",
    "icon",
    "integration",
    "manufacturer",
    "media_content_type",
    "media_title",
    "model",
    "platform",
    "source",
    "source_list",
    "supported_features",
    "unit_of_measurement",
]

DOOR_DEVICE_CLASSES = {"door", "garage_door", "opening", "window"}
FRIDGE_KEYWORDS = {"fridge", "refrigerator", "cooler"}
FREEZER_KEYWORDS = {"freezer"}
ICE_MAKER_KEYWORDS = {"ice maker", "icemaker", "cubed ice", "ice machine", "ice_maker"}
FILTER_KEYWORDS = {"filter", "water filter", "air filter", "purifier filter"}
ALEXA_KEYWORDS = {"alexa", "echo"}
SONOS_KEYWORDS = {"sonos"}
RING_KEYWORDS = {"ring", "doorbell"}
ROBOROCK_KEYWORDS = {"roborock", "robo rock", "xiaowa", "s7", "s8", "q revo"}


def _ha_base_url(ha_ip=None, ha_port=None):
    settings = cfg.get_ha_settings()
    ip = ha_ip or settings["ha_ip"]
    port = ha_port or settings["ha_port"]
    return f"http://{ip}:{port}"


def _ha_headers(token=None):
    settings = cfg.get_ha_settings()
    token = token if token is not None else settings["ha_token"]
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _ok(data=None, **extra):
    result = {"ok": True, "error": None}
    if data is not None:
        result.update(data)
    result.update(extra)
    return result


def _error(error, message, *, status_code=None, exception=None, **extra):
    if exception:
        logging.warning("Home Assistant discovery error: %s", exception)
    result = {
        "ok": False,
        "error": error,
        "message": message,
        "status_code": status_code,
    }
    result.update(extra)
    return result


def _request_json(path, *, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    url = f"{_ha_base_url(ha_ip, ha_port)}{path}"
    settings = cfg.get_ha_settings()
    resolved_token = token if token is not None else settings["ha_token"]
    if not resolved_token:
        return _error("missing_token", "Home Assistant access token is not configured.", url=url)
    try:
        response = requests.get(url, headers=_ha_headers(resolved_token), timeout=timeout)
    except requests.exceptions.Timeout as e:
        return _error("timeout", "Home Assistant request timed out.", exception=e, url=url)
    except requests.exceptions.ConnectionError as e:
        return _error("unreachable", "Home Assistant is unreachable.", exception=e, url=url)
    except requests.exceptions.RequestException as e:
        return _error("request_failed", "Home Assistant request failed.", exception=e, url=url)

    if response.status_code in {401, 403}:
        return _error(
            "bad_token",
            "Home Assistant rejected the access token.",
            status_code=response.status_code,
            url=url,
        )
    if response.status_code == 404:
        return _error("not_found", "Home Assistant entity or endpoint was not found.", status_code=404, url=url)
    if response.status_code >= 400:
        return _error(
            "http_error",
            f"Home Assistant returned HTTP {response.status_code}.",
            status_code=response.status_code,
            url=url,
        )

    try:
        data = response.json()
    except ValueError as e:
        return _error("invalid_json", "Home Assistant returned invalid JSON.", exception=e, url=url)
    return _ok({"data": data}, status_code=response.status_code, url=url)


def test_ha_connection(*, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    """Check that Home Assistant is reachable and the token can read states."""
    result = _request_json("/api/states", token=token, ha_ip=ha_ip, ha_port=ha_port, timeout=timeout)
    if not result["ok"]:
        return result
    states = result.get("data")
    if not isinstance(states, list):
        return _error("invalid_json", "Home Assistant states response was not a list.", url=result.get("url"))
    if not states:
        return _error("empty_result", "Home Assistant returned no entities.", url=result.get("url"))
    return _ok({"entity_count": len(states)}, status_code=result.get("status_code"), url=result.get("url"))


def get_ha_states(*, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    """Return raw Home Assistant states from GET /api/states."""
    result = _request_json("/api/states", token=token, ha_ip=ha_ip, ha_port=ha_port, timeout=timeout)
    if not result["ok"]:
        return result
    states = result.get("data")
    if not isinstance(states, list):
        return _error("invalid_json", "Home Assistant states response was not a list.", url=result.get("url"))
    if not states:
        return _error("empty_result", "Home Assistant returned no entities.", url=result.get("url"))
    return _ok({"states": states, "entity_count": len(states)}, status_code=result.get("status_code"), url=result.get("url"))


def get_entity(entity_id, *, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    """Return one normalized entity from Home Assistant."""
    if not entity_id or not isinstance(entity_id, str):
        return _error("invalid_entity_id", "Entity ID must be a non-empty string.")
    result = _request_json(
        f"/api/states/{entity_id.strip()}",
        token=token,
        ha_ip=ha_ip,
        ha_port=ha_port,
        timeout=timeout,
    )
    if not result["ok"]:
        return result
    entity = result.get("data")
    if not isinstance(entity, dict) or not entity.get("entity_id"):
        return _error("invalid_json", "Home Assistant entity response was invalid.", url=result.get("url"))
    return _ok({"entity": _normalize_entity(entity)}, status_code=result.get("status_code"), url=result.get("url"))


def validate_entity_exists(entity_id, *, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    """Return ok=True if an entity exists in Home Assistant."""
    result = get_entity(entity_id, token=token, ha_ip=ha_ip, ha_port=ha_port, timeout=timeout)
    if result["ok"]:
        return _ok({"exists": True, "entity": result["entity"]}, status_code=result.get("status_code"), url=result.get("url"))
    if result.get("error") == "not_found":
        return _ok({"exists": False, "entity": None}, status_code=result.get("status_code"), url=result.get("url"))
    return result


def discover_ha_entities(*, states=None, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    """Discover and categorize useful Home Assistant entities for setup.

    This function performs no UI work and is safe to call from a worker thread.
    Pass a pre-fetched states list to categorize cached data without network I/O.
    """
    if states is None:
        result = get_ha_states(token=token, ha_ip=ha_ip, ha_port=ha_port, timeout=timeout)
        if not result["ok"]:
            return result
        states = result["states"]
        source = {"url": result.get("url"), "status_code": result.get("status_code")}
    else:
        source = {"url": None, "status_code": None}

    if not isinstance(states, list):
        return _error("invalid_states", "States must be a list of Home Assistant entity dictionaries.")
    if not states:
        return _error("empty_result", "No Home Assistant entities were provided.")

    categories = {name: [] for name in DISCOVERY_CATEGORIES}
    all_entities = []
    for raw_entity in states:
        if not isinstance(raw_entity, dict) or not raw_entity.get("entity_id"):
            continue
        entity = _normalize_entity(raw_entity)
        all_entities.append(entity)
        _categorize_entity(entity, categories)

    if not all_entities:
        return _error("empty_result", "No valid Home Assistant entities were found.")

    counts = {name: len(items) for name, items in categories.items()}
    return _ok(
        {
            "categories": categories,
            "counts": counts,
            "all_entities": all_entities,
            "entity_count": len(all_entities),
        },
        **source,
    )


def search_entities(query, *, states=None, discovery=None, token=None, ha_ip=None, ha_port=None, timeout=DEFAULT_TIMEOUT):
    """Search discovered entities by ID, name, domain, state, platform, or summary text."""
    text = (query or "").strip().lower()
    if discovery is None:
        discovery = discover_ha_entities(states=states, token=token, ha_ip=ha_ip, ha_port=ha_port, timeout=timeout)
    if not discovery.get("ok"):
        return discovery

    entities = discovery.get("all_entities", [])
    if not text:
        return _ok({"query": query, "matches": entities, "match_count": len(entities)})

    matches = []
    for entity in entities:
        haystack = " ".join(
            str(part).lower()
            for part in [
                entity.get("entity_id"),
                entity.get("friendly_name"),
                entity.get("domain"),
                entity.get("state"),
                entity.get("device_class"),
                entity.get("platform"),
                entity.get("attributes_summary"),
            ]
        )
        if text in haystack:
            matches.append(entity)
    return _ok({"query": query, "matches": matches, "match_count": len(matches)})


def _normalize_entity(raw_entity):
    entity_id = raw_entity.get("entity_id", "")
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    attributes = raw_entity.get("attributes") if isinstance(raw_entity.get("attributes"), dict) else {}
    friendly_name = attributes.get("friendly_name") or entity_id
    platform = _detect_platform(entity_id, friendly_name, attributes)
    summary = _summarize_attributes(attributes)
    return {
        "entity_id": entity_id,
        "friendly_name": str(friendly_name),
        "domain": domain,
        "state": raw_entity.get("state"),
        "device_class": attributes.get("device_class"),
        "platform": platform,
        "integration": platform,
        "attributes_summary": summary,
    }


def _detect_platform(entity_id, friendly_name, attributes):
    explicit = attributes.get("platform") or attributes.get("integration")
    if explicit:
        return str(explicit).lower()

    text = _entity_text(entity_id, friendly_name, attributes)
    if _has_keyword(text, ALEXA_KEYWORDS):
        return "alexa_media"
    if _has_keyword(text, SONOS_KEYWORDS):
        return "sonos"
    if _has_keyword(text, RING_KEYWORDS):
        return "ring"
    if _has_keyword(text, ROBOROCK_KEYWORDS):
        return "roborock"
    return None


def _summarize_attributes(attributes):
    summary = {}
    for key in SUMMARY_ATTRIBUTE_KEYS:
        if key not in attributes:
            continue
        value = attributes[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = value[:8]
        elif isinstance(value, dict):
            summary[key] = {k: value[k] for k in list(value)[:8]}
    return summary


def _categorize_entity(entity, categories):
    domain = entity["domain"]
    device_class = (entity.get("device_class") or "").lower()
    platform = (entity.get("platform") or "").lower()
    text = _entity_text(entity["entity_id"], entity["friendly_name"], entity.get("attributes_summary", {}))

    if domain == "media_player":
        categories["media_players"].append(entity)
        if platform == "alexa_media" or _has_keyword(text, ALEXA_KEYWORDS):
            categories["alexa_media_players"].append(entity)
        if platform == "sonos" or _has_keyword(text, SONOS_KEYWORDS):
            categories["sonos_media_players"].append(entity)

    if domain == "binary_sensor" and (device_class in DOOR_DEVICE_CLASSES or _has_keyword(text, {"door", "contact"})):
        categories["door_sensors"].append(entity)

    if domain in {"binary_sensor", "sensor"} and _has_keyword(text, FRIDGE_KEYWORDS):
        categories["fridge_sensors"].append(entity)

    if domain in {"binary_sensor", "sensor"} and _has_keyword(text, FREEZER_KEYWORDS):
        categories["freezer_sensors"].append(entity)

    if domain == "camera":
        categories["cameras"].append(entity)
        if platform == "ring" or _has_keyword(text, RING_KEYWORDS):
            categories["ring_cameras"].append(entity)

    if domain in {"switch", "input_boolean"}:
        categories["switches"].append(entity)

    if domain in {"switch", "input_boolean", "sensor", "binary_sensor"} and _has_keyword(text, ICE_MAKER_KEYWORDS):
        categories["ice_maker_candidates"].append(entity)

    if domain == "sensor" and _has_keyword(text, FILTER_KEYWORDS):
        categories["filter_sensors"].append(entity)

    if domain == "vacuum":
        categories["vacuum_entities"].append(entity)
        if platform == "roborock" or _has_keyword(text, ROBOROCK_KEYWORDS):
            categories["roborock_entities"].append(entity)
    elif _has_keyword(text, ROBOROCK_KEYWORDS):
        categories["roborock_entities"].append(entity)


def _entity_text(entity_id, friendly_name, attributes):
    parts = [entity_id or "", friendly_name or ""]
    if isinstance(attributes, dict):
        attrs = attributes
    else:
        attrs = {}
    for key in [
        "app_name",
        "attribution",
        "brand",
        "device_class",
        "friendly_name",
        "icon",
        "integration",
        "manufacturer",
        "model",
        "platform",
        "source",
    ]:
        value = attrs.get(key)
        if value is not None:
            parts.append(str(value))
    return " ".join(parts).replace("_", " ").replace("-", " ").lower()


def _has_keyword(text, keywords):
    return any(keyword in text for keyword in keywords)


def clone_discovery_result(discovery):
    """Return a defensive copy for callers that cache discovery data."""
    return deepcopy(discovery)
