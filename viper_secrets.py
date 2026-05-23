"""Safe secret storage for Viper Vision.

On Windows, the keyring package stores these values in Windows Credential
Manager for the current user. If keyring is unavailable, callers get a safe
failure instead of losing secrets.
"""

from __future__ import annotations

import logging
import os

SERVICE_NAME = "Viper Vision"

SECRET_LABELS = {
    "ha_token": "Home Assistant token",
    "gemini_api_key": "Gemini API key",
    "pushover_user_key": "Pushover user key",
    "pushover_api_token": "Pushover app token",
    "mqtt_password": "MQTT password",
}

_keyring = None
_keyring_loaded = False


def _load_keyring():
    global _keyring, _keyring_loaded
    if os.getenv("VIPER_DISABLE_KEYRING", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    if not _keyring_loaded:
        _keyring_loaded = True
        try:
            import keyring  # type: ignore

            _keyring = keyring
        except Exception:
            logging.info("Python keyring package is not available; secrets remain in existing fallback storage.")
            _keyring = None
    return _keyring


def _account(name: str) -> str:
    return f"viper_vision:{name}"


def is_available() -> bool:
    return _load_keyring() is not None


def get_secret(name: str) -> str:
    keyring = _load_keyring()
    if keyring is None:
        return ""
    try:
        return keyring.get_password(SERVICE_NAME, _account(name)) or ""
    except Exception:
        logging.debug("Could not read secret %s from keyring", name, exc_info=True)
        return ""


def set_secret(name: str, value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    keyring = _load_keyring()
    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE_NAME, _account(name), value)
        return True
    except Exception:
        logging.warning("Could not save %s to Windows Credential Manager.", SECRET_LABELS.get(name, name), exc_info=True)
        return False


def delete_secret(name: str) -> bool:
    keyring = _load_keyring()
    if keyring is None:
        return False
    try:
        keyring.delete_password(SERVICE_NAME, _account(name))
        return True
    except Exception:
        return False


def storage_status() -> dict:
    available = is_available()
    return {
        "available": available,
        "service": SERVICE_NAME,
        "stored": {name: bool(get_secret(name)) for name in SECRET_LABELS} if available else {},
    }
