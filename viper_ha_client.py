"""Shared Home Assistant REST client helpers for Viper Vision."""

from __future__ import annotations

import requests

import viper_config as cfg


class HomeAssistantClientError(RuntimeError):
    pass


def settings_from_config(config=None):
    settings = cfg.get_ha_settings(config or {}, include_env=True)
    host = str(settings.get("ha_ip") or "").strip()
    port = str(settings.get("ha_port") or "8123").strip()
    token = str(settings.get("ha_token") or "").strip()
    if not host:
        raise HomeAssistantClientError("Home Assistant host is missing.")
    if not token:
        raise HomeAssistantClientError("Home Assistant token is missing.")
    return {"host": host, "port": port, "token": token, "base_url": f"http://{host}:{port}"}


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def request(config, method, path, *, json_data=None, timeout=8):
    settings = settings_from_config(config)
    clean_path = "/" + str(path or "").lstrip("/")
    url = settings["base_url"] + clean_path
    response = requests.request(
        str(method or "GET").upper(),
        url,
        headers=headers(settings["token"]),
        json=json_data,
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.content:
        return None
    return response.json()


def safe_request(config, method, path, *, json_data=None, timeout=8):
    try:
        data = request(config, method, path, json_data=json_data, timeout=timeout)
        return {"ok": True, "data": data, "message": "Home Assistant answered."}
    except Exception as exc:
        return {
            "ok": False,
            "data": None,
            "error": type(exc).__name__,
            "message": str(exc),
        }


def get_states(config, *, timeout=8):
    states = request(config, "GET", "/api/states", timeout=timeout)
    return {item.get("entity_id"): item for item in (states or []) if isinstance(item, dict) and item.get("entity_id")}


def get_state(config, entity_id, *, timeout=5):
    return request(config, "GET", f"/api/states/{entity_id}", timeout=timeout)


def call_service(config, domain_service, data=None, *, timeout=10):
    return request(
        config,
        "POST",
        f"/api/services/{str(domain_service or '').strip('/')}",
        json_data=data or {},
        timeout=timeout,
    )
