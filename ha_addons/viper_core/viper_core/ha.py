import json
import time
import urllib.error
import urllib.request


class HomeAssistantClient:
    def __init__(self, base_url, token, timeout=8):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def available(self):
        return bool(self.base_url and self.token)

    def api_status(self):
        started = time.monotonic()
        if not self.available():
            return {
                "ok": False,
                "reason": "missing_config",
                "message": "Home Assistant URL or token is not configured.",
                "latency_ms": 0,
            }

        request = urllib.request.Request(
            f"{self.base_url}/",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body) if body else {}
            message = payload.get("message") if isinstance(payload, dict) else ""
            return {
                "ok": True,
                "reason": "ok",
                "message": message or "Home Assistant API is reachable.",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except urllib.error.HTTPError as exc:
            return {
                "ok": False,
                "reason": f"http_{exc.code}",
                "message": f"Home Assistant API returned HTTP {exc.code}.",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "reason": "connection_failed",
                "message": str(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_state(self, entity_id):
        return self._request("GET", f"/states/{entity_id}")

    def call_service(self, domain_service, data=None):
        domain_service = str(domain_service or "").strip().replace(".", "/")
        return self._request("POST", f"/services/{domain_service}", data or {})

    def create_notification(self, title, message):
        return self.call_service(
            "persistent_notification/create",
            {"title": str(title or "Viper Core"), "message": str(message or "")},
        )

    def _request(self, method, path, data=None):
        if not self.available():
            raise RuntimeError("Home Assistant URL or token is not configured.")
        url = f"{self.base_url}{'/' + str(path).lstrip('/')}"
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=str(method or "GET").upper())
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None
