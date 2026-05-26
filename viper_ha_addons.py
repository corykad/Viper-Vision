import asyncio
import json
import logging
import re
from urllib.parse import urlparse

import requests
import websockets


RING_MQTT_ADDON_SLUG = "03cabcc9_ring_mqtt"
RING_MQTT_REPOSITORY_URL = "https://github.com/tsightler/ring-mqtt-ha-addon"


def addon_items_from_payload(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("addons"), list):
            return data.get("addons")
        if isinstance(payload.get("addons"), list):
            return payload.get("addons")
    return []


def payload_data(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload.get("data")
    return payload if isinstance(payload, dict) else {}


def hassio_request(settings, method, path, *, payload=None, timeout=30, ws_request_func=None):
    ha_ip = settings.get("ha_ip") or ""
    ha_port = settings.get("ha_port") or "8123"
    token = settings.get("ha_token") or ""
    url = f"http://{ha_ip}:{ha_port}/api/hassio{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.request(method, url, headers=headers, json=payload or {}, timeout=timeout)
    if response.status_code == 404:
        return ws_request_func(settings, method, path, payload=payload, timeout=timeout)
    if response.status_code in {401, 403}:
        logging.info(
            "[HA SETUP] REST Supervisor API returned HTTP %s for %s; trying WebSocket supervisor/api fallback.",
            response.status_code,
            path,
        )
        return ws_request_func(settings, method, path, payload=payload, timeout=timeout)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return {}


def hassio_ws_request(settings, method, path, *, payload=None, timeout=30, ws_command_func=None):
    return ws_command_func(
        settings,
        {
            "type": "supervisor/api",
            "endpoint": path,
            "method": str(method or "GET").lower(),
            "data": payload or {},
            "timeout": timeout,
        },
        timeout=timeout,
    )


def ha_ws_command(settings, command, *, timeout=30):
    ha_ip = settings.get("ha_ip") or ""
    ha_port = settings.get("ha_port") or "8123"
    token = settings.get("ha_token") or ""
    if not ha_ip or not token:
        raise RuntimeError("Home Assistant host or token is missing.")

    async def call_ws_command():
        host = ha_ip
        port = str(ha_port or "8123")
        scheme = "wss" if str(host).startswith("https://") else "ws"
        if "://" in str(host):
            parsed = urlparse(str(host))
            scheme = "wss" if parsed.scheme == "https" else "ws"
            host = parsed.hostname or host
            port = str(parsed.port or port or ("443" if scheme == "wss" else "8123"))
        ws_url = f"{scheme}://{host}:{port}/api/websocket"
        async with websockets.connect(ws_url, open_timeout=min(float(timeout), 10.0)) as ws:
            greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if greeting.get("type") != "auth_required":
                raise RuntimeError("Home Assistant WebSocket did not request authentication.")
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if auth.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant rejected the token for WebSocket access.")
            message = dict(command or {})
            message["id"] = 1
            await ws.send(json.dumps(message))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if not response.get("success"):
                error = response.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else str(error)
                code = error.get("code") if isinstance(error, dict) else ""
                raise RuntimeError(message or code or "Home Assistant rejected the WebSocket request.")
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    try:
        return asyncio.run(call_ws_command())
    except RuntimeError as e:
        raise RuntimeError(f"Home Assistant WebSocket request failed: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Home Assistant WebSocket request failed: {e}") from e


def check_supervisor_install_permission(settings, hassio_request_func):
    try:
        hassio_request_func(settings, "GET", "/supervisor/info", timeout=8)
        return {
            "ok": True,
            "reason": "ok",
            "message": "Installer permission: this Home Assistant token can access Supervisor add-on management.",
        }
    except Exception as e:
        message = str(e)
        lowered = message.lower()
        if "rejected this token" in lowered:
            return {
                "ok": False,
                "reason": "supervisor_token_rejected",
                "message": "Installer permission: blocked. Viper can use the normal Home Assistant API, but Supervisor add-on management rejected this external token. Use the Home Assistant VM console fallback.",
            }
        if "did not expose the supervisor" in lowered:
            return {
                "ok": False,
                "reason": "supervisor_unavailable",
                "message": "Installer permission: not available because this Home Assistant system does not expose Supervisor add-on management.",
            }
        return {
            "ok": False,
            "reason": "check_failed",
            "message": f"Installer permission: could not be checked. {message}",
        }


def get_installed_addons(settings, hassio_request_func):
    try:
        payload = hassio_request_func(settings, "GET", "/addons", timeout=30)
        addons = addon_items_from_payload(payload)
        if addons:
            return addons
    except Exception as e:
        logging.info("[HA SETUP] Installed add-on list unavailable from /addons: %s", e)
    try:
        payload = hassio_request_func(settings, "GET", "/supervisor/info", timeout=30)
        data = payload_data(payload)
        addons = data.get("addons", []) if isinstance(data, dict) else []
        return addons if isinstance(addons, list) else []
    except Exception as e:
        logging.info("[HA SETUP] Installed add-on list unavailable from supervisor info: %s", e)
        return []


def ensure_addon_started(settings, slug, get_addon_info_func, hassio_request_func):
    if not slug:
        return False
    try:
        info = get_addon_info_func(settings, slug)
        if str(info.get("state", "")).lower() in {"started", "running"}:
            return True
    except Exception:
        pass
    try:
        hassio_request_func(settings, "POST", f"/addons/{slug}/start", timeout=90)
        return True
    except Exception as e:
        message = str(e).lower()
        if "already" in message or "started" in message or "running" in message:
            return True
        raise


def restart_addon(settings, slug, hassio_request_func, ensure_addon_started_func):
    if not slug:
        return False
    try:
        hassio_request_func(settings, "POST", f"/addons/{slug}/restart", timeout=120)
        return True
    except Exception as e:
        logging.info("[HA SETUP] Add-on restart failed for %s; falling back to start. %s", slug, e)
        return ensure_addon_started_func(settings, slug)


def absolute_ha_url(settings, path_or_url):
    text = str(path_or_url or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    ha_ip = settings.get("ha_ip") or ""
    ha_port = settings.get("ha_port") or "8123"
    if not text.startswith("/"):
        text = "/" + text
    return f"http://{ha_ip}:{ha_port}{text}"


def normalize_addon_webui(settings, value):
    text = str(value or "").strip()
    if not text:
        return ""
    ha_ip = settings.get("ha_ip") or ""
    text = text.replace("[HOST]", ha_ip).replace("{host}", ha_ip).replace("0.0.0.0", ha_ip)
    text = re.sub(r"\[PORT:(\d+)\]", r"\1", text)
    text = re.sub(r"\[PROTO:[^\]]+\]", "http", text)
    return absolute_ha_url(settings, text)


def current_ha_user_id(settings, ws_command_func):
    try:
        user = ws_command_func(settings, {"type": "auth/current_user"}, timeout=15)
    except Exception as e:
        logging.info("[HA SETUP] Could not read current Home Assistant user for ingress session: %s", e)
        return ""
    return str(user.get("id") or user.get("user_id") or "").strip()


def create_ingress_session(settings, hassio_request_func, ws_command_func):
    user_id = current_ha_user_id(settings, ws_command_func)
    payload = {"user_id": user_id} if user_id else {}
    session_payload = hassio_request_func(settings, "POST", "/ingress/session", payload=payload, timeout=30)
    session_data = payload_data(session_payload)
    return session_data.get("session") or session_data.get("ingress_session") or session_data.get("token") or ""


def ingress_session_url(settings, session, addon_info):
    token = str(session or "").strip()
    if not token:
        return ""
    data = addon_info if isinstance(addon_info, dict) else {}

    def suffix_from_ingress_path(value):
        text = str(value or "").strip()
        marker = "/api/hassio_ingress/"
        if marker not in text:
            return text
        after = text.split(marker, 1)[1]
        parts = after.split("/", 1)
        if len(parts) == 2 and parts[1]:
            return "/" + parts[1].lstrip("/")
        return "/"

    entry = str(data.get("ingress_entry") or "").strip()
    if entry:
        entry = suffix_from_ingress_path(entry)
        if not entry.startswith("/"):
            entry = "/" + entry
        return absolute_ha_url(settings, f"/api/hassio_ingress/{token}{entry}")

    ingress_url = str(data.get("ingress_url") or "").strip()
    suffix = suffix_from_ingress_path(ingress_url) or "/"
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return absolute_ha_url(settings, f"/api/hassio_ingress/{token}{suffix}")


def is_ring_mqtt_slug(slug):
    slug_l = str(slug or "").strip().lower()
    return slug_l in {"ring_mqtt", RING_MQTT_ADDON_SLUG}


def ring_mqtt_app_page_url(settings, slug):
    if is_ring_mqtt_slug(slug):
        slug = RING_MQTT_ADDON_SLUG
    return absolute_ha_url(settings, f"/config/app/{slug}/info")


def resolve_addon_login_url(settings, slug, get_addon_info_func):
    if is_ring_mqtt_slug(slug):
        return absolute_ha_url(settings, f"/app/{RING_MQTT_ADDON_SLUG}")
    info = get_addon_info_func(settings, slug)
    data = payload_data(info)
    if data.get("ingress") or data.get("ingress_url") or data.get("ingress_entry"):
        return absolute_ha_url(settings, f"/app/{slug}")
    if data.get("webui"):
        return normalize_addon_webui(settings, data.get("webui"))
    return ring_mqtt_app_page_url(settings, slug)


def find_addon_slug(addons, *, exact_slugs=(), text_tokens=()):
    lowered_exact = {str(item).lower() for item in exact_slugs}
    if lowered_exact:
        for addon in addons:
            slug = str(addon.get("slug") or addon.get("addon") or "").strip()
            if slug.lower() in lowered_exact:
                return slug
    for addon in addons:
        haystack = " ".join(
            str(addon.get(key, ""))
            for key in ("slug", "addon", "name", "description", "repository", "url")
        ).lower()
        if all(token.lower() in haystack for token in text_tokens):
            return str(addon.get("slug") or addon.get("addon") or "").strip()
    return ""


def find_ring_mqtt_slug(addons):
    exact_slugs = {"ring_mqtt", RING_MQTT_ADDON_SLUG}
    preferred_repo = "03cabcc9"
    preferred_repo_url = "github.com/tsightler/ring-mqtt-ha-addon"
    exact_names = {"ring-mqtt with video streaming", "ring mqtt with video streaming"}
    candidates = []
    for addon in addons or []:
        slug = str(addon.get("slug") or addon.get("addon") or "").strip()
        name = str(addon.get("name") or "").strip()
        description = str(addon.get("description") or "")
        repository = str(addon.get("repository") or "")
        url = str(addon.get("url") or addon.get("repository_url") or addon.get("source") or "")
        slug_l = slug.lower()
        name_l = name.lower()
        description_l = description.lower()
        repository_l = repository.lower()
        url_l = url.lower()
        if not slug:
            continue
        if slug_l in exact_slugs:
            return RING_MQTT_ADDON_SLUG
        score = 0
        if name_l in exact_names:
            score += 100
        if "ring_mqtt" in slug_l or "ring-mqtt" in slug_l:
            score += 80
        if repository == preferred_repo:
            score += 60
        if preferred_repo_url in repository_l or preferred_repo_url in url_l:
            score += 80
        if "ring devices" in description_l and "mqtt" in description_l:
            score += 30
        if "video streaming" in name_l or "video streaming" in description_l:
            score += 10
        has_ring = "ring" in slug_l or "ring" in name_l or "ring" in description_l or preferred_repo_url in repository_l or preferred_repo_url in url_l
        has_mqtt = "mqtt" in slug_l or "mqtt" in name_l or "mqtt" in description_l or preferred_repo_url in repository_l or preferred_repo_url in url_l
        if score >= 80 and has_ring and has_mqtt:
            candidates.append((score, slug))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def addon_installed_in_store(addons, slug):
    wanted = str(slug or "").lower()
    for addon in addons or []:
        addon_slug = str(addon.get("slug") or addon.get("addon") or "").lower()
        if addon_slug == wanted and bool(addon.get("installed")):
            return True
    return False


def configure_ring_mqtt_rtsp_port(settings, hassio_request_func):
    payload = {"network": {"8554/tcp": 8554}}
    hassio_request_func(settings, "POST", f"/addons/{RING_MQTT_ADDON_SLUG}/options", payload=payload, timeout=60)
    return True


def configure_ring_mqtt_rtsp_port_and_restart(settings, configure_func, restart_func, ensure_started_func):
    configure_func(settings)
    restart_func(settings, RING_MQTT_ADDON_SLUG)
    ensure_started_func(settings, RING_MQTT_ADDON_SLUG)
    return True


def install_ring_mqtt_requirements(
    settings,
    *,
    progress,
    hassio_request_func,
    get_installed_addons_func,
    ensure_addon_started_func,
    configure_ring_mqtt_func,
):
    lines = ["Ring MQTT Requirements Installer"]

    def add_progress(message, *, announce=False):
        lines.append(str(message))
        progress(lines, str(message), announce=announce)

    try:
        add_progress("Checking whether Home Assistant Supervisor accepts add-on setup requests.", announce=True)
        hassio_request_func(settings, "GET", "/supervisor/info", timeout=12)
        add_progress("Supervisor API: available.")

        try:
            add_progress("Adding the Ring-MQTT add-on repository if it is not already present.")
            hassio_request_func(settings, "POST", "/store/repositories", payload={"repository": RING_MQTT_REPOSITORY_URL}, timeout=60)
            add_progress("Ring-MQTT repository: added.", announce=True)
        except Exception as e:
            message = str(e)
            if "already" in message.lower() or "exist" in message.lower():
                add_progress("Ring-MQTT repository: already present.")
            else:
                add_progress(f"Ring-MQTT repository: add reported {message}")

        try:
            add_progress("Reloading the Home Assistant app store so Ring-MQTT appears.")
            hassio_request_func(settings, "POST", "/store/reload", timeout=60)
            add_progress("App store: reloaded.")
        except Exception as e:
            add_progress(f"App store reload: {e}")

        add_progress("Reading Home Assistant app store add-ons.")
        store_payload = hassio_request_func(settings, "GET", "/store/addons", timeout=30)
        addons = addon_items_from_payload(store_payload)
        add_progress("Reading installed Home Assistant add-ons.")
        installed_addons = get_installed_addons_func(settings)
        mosquitto_slug = find_addon_slug(
            installed_addons,
            exact_slugs=("core_mosquitto",),
            text_tokens=("mosquitto",),
        ) or find_addon_slug(
            addons,
            exact_slugs=("core_mosquitto",),
            text_tokens=("mosquitto",),
        )
        ring_slug = find_addon_slug(
            installed_addons,
            exact_slugs=(RING_MQTT_ADDON_SLUG, "ring_mqtt"),
        ) or find_ring_mqtt_slug(installed_addons) or find_addon_slug(
            addons,
            exact_slugs=(RING_MQTT_ADDON_SLUG, "ring_mqtt"),
        ) or find_ring_mqtt_slug(addons)
        if ring_slug and not is_ring_mqtt_slug(ring_slug):
            logging.warning("[HA SETUP] Refusing non-Ring-MQTT add-on slug during detection: %s", ring_slug)
            add_progress(f"Ignoring non-Ring-MQTT add-on that matched by mistake: {ring_slug}.")
            ring_slug = ""

        if not mosquitto_slug:
            add_progress("Mosquitto Broker: not found in the app store.", announce=True)
        else:
            mosquitto_already_installed = bool(find_addon_slug(installed_addons, exact_slugs=(mosquitto_slug,))) or addon_installed_in_store(addons, mosquitto_slug)
            if mosquitto_already_installed:
                add_progress(f"Mosquitto Broker: already installed as {mosquitto_slug}.")
            else:
                try:
                    add_progress(f"Installing Mosquitto Broker as {mosquitto_slug}. This can take a minute.")
                    hassio_request_func(settings, "POST", f"/store/addons/{mosquitto_slug}/install", payload={"background": False}, timeout=180)
                    add_progress(f"Mosquitto Broker: installed as {mosquitto_slug}.", announce=True)
                except Exception as e:
                    message = str(e)
                    if "already" in message.lower() or "installed" in message.lower():
                        add_progress(f"Mosquitto Broker: already installed as {mosquitto_slug}.")
                    else:
                        add_progress(f"Mosquitto Broker install: {message}")
            try:
                add_progress("Starting Mosquitto Broker if it is not already running.")
                ensure_addon_started_func(settings, mosquitto_slug)
                add_progress("Mosquitto Broker: start requested.")
            except Exception as e:
                add_progress(f"Mosquitto Broker start: {e}")

        if not ring_slug:
            installed = False
            for candidate_slug in (RING_MQTT_ADDON_SLUG, "ring_mqtt"):
                try:
                    add_progress(f"Installing Ring-MQTT with Video Streaming as {candidate_slug}. This can take several minutes.")
                    hassio_request_func(settings, "POST", f"/store/addons/{candidate_slug}/install", payload={"background": False}, timeout=240)
                    ring_slug = candidate_slug
                    installed = True
                    add_progress(f"Ring-MQTT with Video Streaming: installed as {candidate_slug}.", announce=True)
                    break
                except Exception as e:
                    message = str(e)
                    if "already" in message.lower() or "installed" in message.lower():
                        ring_slug = candidate_slug
                        installed = True
                        add_progress(f"Ring-MQTT with Video Streaming: already installed as {candidate_slug}.")
                        break
            if not installed:
                add_progress("Ring-MQTT with Video Streaming: not found after adding the repository. Viper did not open another add-on.", announce=True)
            else:
                try:
                    add_progress("Starting Ring-MQTT.")
                    ensure_addon_started_func(settings, ring_slug)
                    add_progress("Ring-MQTT: start requested.")
                except Exception as e:
                    add_progress(f"Ring-MQTT start: {e}")
        else:
            ring_already_installed = bool(find_addon_slug(installed_addons, exact_slugs=(ring_slug,))) or addon_installed_in_store(addons, ring_slug)
            if ring_already_installed:
                add_progress("Ring-MQTT is already installed. Opening Ring login now when setup finishes.", announce=True)
            else:
                try:
                    add_progress(f"Installing Ring-MQTT with Video Streaming as {ring_slug}. This can take several minutes.")
                    hassio_request_func(settings, "POST", f"/store/addons/{ring_slug}/install", payload={"background": False}, timeout=240)
                    add_progress(f"Ring-MQTT with Video Streaming: installed as {ring_slug}.", announce=True)
                except Exception as e:
                    message = str(e)
                    if "already" in message.lower() or "installed" in message.lower():
                        add_progress(f"Ring-MQTT with Video Streaming: already installed as {ring_slug}.")
                    else:
                        add_progress(f"Ring-MQTT install: {message}")
            try:
                add_progress("Starting Ring-MQTT.")
                ensure_addon_started_func(settings, ring_slug)
                add_progress("Ring-MQTT: start requested.")
            except Exception as e:
                add_progress(f"Ring-MQTT start: {e}")

        if ring_slug:
            try:
                add_progress("Configuring Ring-MQTT RTSP port 8554 and restarting Ring-MQTT.")
                configure_ring_mqtt_func(settings)
                add_progress("Ring-MQTT RTSP port 8554: configured.")
                add_progress("Ring-MQTT: restarted so RTSP port 8554 is active.", announce=True)
            except Exception as e:
                add_progress(
                    "Ring-MQTT RTSP port 8554: could not be configured automatically. "
                    "Open the Ring-MQTT app configuration in Home Assistant, set network port 8554 for 8554/tcp, save, and restart Ring-MQTT."
                )
                add_progress(f"Ring-MQTT RTSP port error: {e}")

        add_progress("")
        add_progress("Next steps:")
        add_progress("1. Viper will open the Ring-MQTT app page in your normal browser.")
        add_progress("2. On that Home Assistant app page, tab to Open Web UI and activate it.")
        add_progress("3. Enter Ring credentials only inside Ring-MQTT or Home Assistant.")
        add_progress("4. After Ring-MQTT login is complete, return to Viper and press Find Ring MQTT Streams.", announce=True)
        return {"ok": True, "message": "\n".join(lines), "ring_slug": ring_slug}
    except Exception as e:
        lines.extend([
            f"Installer could not continue: {e}",
            "",
            "Accessible fallback using the Home Assistant VM console:",
            "1. Open the Home Assistant VirtualBox window or console.",
            "2. At the ha > prompt, run:",
            f"   addons repositories add {RING_MQTT_REPOSITORY_URL}",
            "   addons reload",
            "   addons list",
            "   addons install core_mosquitto",
            "3. Run addons list again, find the Ring-MQTT slug, then run:",
            "   addons install SLUG_HERE",
            "",
            "If you are using SSH instead of the ha > console prompt, prefix each command with ha, for example: ha addons list",
        ])
        return {"ok": False, "message": "\n".join(lines)}
