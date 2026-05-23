import logging
import socket
import time
from copy import deepcopy
from urllib.parse import urlparse

import requests

import viper_config as cfg


DEFAULT_TIMEOUT = 8
OBSERVER_PORT = "4357"

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
    "stream_Source",
    "stream_source",
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
    settings = cfg.get_ha_settings()
    resolved_host = ha_ip or settings["ha_ip"]
    resolved_port = ha_port or settings["ha_port"] or "8123"
    if not resolved_host:
        return _error(
            "missing_host",
            "Home Assistant host is not configured. Use Find Home Assistant or enter the IP address.",
        )
    url = f"http://{resolved_host}:{resolved_port}{path}"
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


def normalize_ha_host(value):
    text = str(value or "").strip()
    if not text:
        return "", "8123"
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or ""
        port = str(parsed.port or 8123)
        return host, port
    if ":" in text and not text.startswith("["):
        host, port = text.rsplit(":", 1)
        return host.strip(), port.strip() or "8123"
    return text, "8123"


def resolve_host_to_ip(host):
    """Return the numeric IP for a host name when it can be resolved."""
    text = str(host or "").strip()
    if not text:
        return ""
    parsed_host, _ = normalize_ha_host(text)
    host = parsed_host or text
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    try:
        resolved = socket.gethostbyname(host)
    except OSError:
        return ""
    if resolved and not resolved.startswith("127."):
        return resolved
    return ""


def candidate_ha_hosts(seed_host="", seed_port="8123"):
    candidates = []
    seen = set()

    def add(host, port="8123", reason="candidate"):
        host, parsed_port = normalize_ha_host(host)
        port = str(port or parsed_port or "8123")
        key = (host.lower(), port)
        if host and key not in seen:
            seen.add(key)
            candidates.append({"ha_ip": host, "ha_port": port, "reason": reason})

    if seed_host:
        add(seed_host, seed_port, "saved setting")
    add("homeassistant.local", "8123", "Home Assistant local name")
    add("homeassistant", "8123", "Home Assistant host name")
    settings = cfg.get_ha_settings(include_env=True)
    if settings.get("ha_ip"):
        add(settings["ha_ip"], settings.get("ha_port") or "8123", "configured default")

    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        parts = local_ip.split(".")
        if len(parts) == 4 and not local_ip.startswith("127."):
            for last in ("2", "10", "49", "50", "100", "101", "200"):
                add(".".join(parts[:3] + [last]), "8123", "local subnet guess")
    except Exception:
        pass
    return candidates


def find_home_assistant(*, token=None, seed_host="", seed_port="8123", timeout=2):
    """Try common HA host names and a few local subnet guesses."""
    attempts = []
    for candidate in candidate_ha_hosts(seed_host, seed_port):
        host = candidate["ha_ip"]
        port = candidate["ha_port"]
        try:
            url = f"http://{host}:{port}/api/"
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = requests.get(url, headers=headers, timeout=timeout)
            ok_without_token = response.status_code in {200, 401, 403}
            ok_with_token = bool(token) and response.status_code == 200
            attempts.append({**candidate, "url": url, "status_code": response.status_code})
            if ok_with_token or (not token and ok_without_token):
                resolved_host = resolve_host_to_ip(host) or host
                return _ok(
                    {
                        "ha_ip": resolved_host,
                        "ha_host": host,
                        "ha_url": f"http://{resolved_host}:{port}",
                        "ha_port": port,
                        "attempts": attempts,
                        "auth_ok": ok_with_token,
                    }
                )
            if token and response.status_code in {401, 403}:
                resolved_host = resolve_host_to_ip(host) or host
                return _ok(
                    {
                        "ha_ip": resolved_host,
                        "ha_host": host,
                        "ha_url": f"http://{resolved_host}:{port}",
                        "ha_port": port,
                        "attempts": attempts,
                        "auth_ok": False,
                        "auth_error": "bad_token",
                        "message": "Home Assistant was found, but it rejected the access token.",
                    }
                )
        except requests.exceptions.RequestException as e:
            attempts.append({**candidate, "error": str(e)})
    return _error("not_found", "Home Assistant was not found automatically.", attempts=attempts)


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


def _probe_url(url, *, timeout=5, headers=None, expected_statuses=None):
    started = time.perf_counter()
    try:
        response = requests.get(url, headers=headers or {}, timeout=timeout)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ok = response.status_code in (expected_statuses or {200})
        return {
            "ok": ok,
            "url": url,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "error": None,
            "message": f"HTTP {response.status_code} in {elapsed_ms} ms.",
        }
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": url,
            "status_code": None,
            "elapsed_ms": elapsed_ms,
            "error": "timeout",
            "message": f"Timed out after {elapsed_ms} ms.",
        }
    except requests.exceptions.ConnectionError as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": url,
            "status_code": None,
            "elapsed_ms": elapsed_ms,
            "error": "unreachable",
            "message": str(e),
        }
    except requests.exceptions.RequestException as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": url,
            "status_code": None,
            "elapsed_ms": elapsed_ms,
            "error": "request_failed",
            "message": str(e),
        }


def check_ha_core_health(*, ha_ip=None, ha_port=None, token=None, timeout=5):
    """Compare Home Assistant Core and Observer responsiveness.

    Observer on port 4357 can stay healthy even when Core on port 8123 is hung.
    A 401/403 from /api/ is considered Core-responsive because HA answered.
    """
    settings = cfg.get_ha_settings()
    host = ha_ip or settings["ha_ip"]
    port = str(ha_port or settings["ha_port"] or "8123")
    if not host:
        return {
            "checked": True,
            "ok": False,
            "state": "missing_host",
            "message": "Home Assistant host is not configured.",
            "core": {},
            "observer": {},
        }

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    core = _probe_url(
        f"http://{host}:{port}/api/",
        timeout=timeout,
        headers=headers,
        expected_statuses={200, 401, 403},
    )
    observer = _probe_url(
        f"http://{host}:{OBSERVER_PORT}/",
        timeout=timeout,
        expected_statuses={200},
    )

    if core["ok"] and observer["ok"]:
        state = "healthy"
        message = "Home Assistant Core and Observer are responding."
        ok = True
    elif not core["ok"] and observer["ok"]:
        state = "core_hung"
        message = "Home Assistant Observer is responding, but Core is not. The VM is alive; HA Core is likely hung or overloaded."
        ok = False
    elif core["ok"] and not observer["ok"]:
        state = "core_only"
        message = "Home Assistant Core is responding, but Observer is not."
        ok = True
    else:
        state = "unreachable"
        message = "Home Assistant Core and Observer are both unreachable from this PC."
        ok = False

    return {
        "checked": True,
        "ok": ok,
        "state": state,
        "message": message,
        "host": host,
        "core": core,
        "observer": observer,
    }


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
