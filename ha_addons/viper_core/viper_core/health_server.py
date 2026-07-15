import json
import logging
import mimetypes
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .web_ui import render_page


LOGGER = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, host, port, state_provider, event_handler=None, control_handler=None):
        self._server = ThreadingHTTPServer(
            (host, port),
            self._make_handler(state_provider, event_handler, control_handler),
        )

    def serve_forever(self):
        self._server.serve_forever()

    def shutdown(self):
        self._server.shutdown()

    @staticmethod
    def _make_handler(state_provider, event_handler, control_handler):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                path = _viper_path(parsed.path)
                state = state_provider()
                if path == "/":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    self._send_html(render_page(state, (query.get("page") or ["dashboard"])[-1]))
                    return
                if path == "/health":
                    self._send_json(state)
                    return
                if path == "/ready":
                    code = 200 if state.get("home_assistant", {}).get("ok") else 503
                    self._send_json(state, code=code)
                    return
                if path.startswith("/chimes/") and control_handler:
                    chime_path = control_handler.chime_path(unquote(path.rsplit("/", 1)[-1]))
                    if chime_path:
                        self._send_file(chime_path)
                        return
                if control_handler:
                    result = control_handler.handle_get(path)
                    if result is not None:
                        self._send_json(result)
                        return
                self._send_json({"ok": False, "message": "Not found."}, code=404)

            def do_POST(self):
                parsed = urlparse(self.path)
                path = _viper_path(parsed.path)
                parts = [item for item in path.strip("/").split("/") if item]
                length = int(self.headers.get("Content-Length") or 0)
                raw_bytes = self.rfile.read(length) if length else b""
                if path.startswith("/ui/"):
                    self._handle_ui_post(path, raw_bytes)
                    return
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("application/x-www-form-urlencoded"):
                    payload = _form_payload(raw_bytes)
                else:
                    raw = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else "{}"
                    try:
                        payload = json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        self._send_json({"ok": False, "message": "Request body must be JSON."}, code=400)
                        return
                if path == "/api/test/pushover" and event_handler:
                    result = event_handler(
                        "pushover_test",
                        {
                            "title": payload.get("title") or "Viper Core Test",
                            "message": payload.get("message") or "Viper Core Pushover is working.",
                        },
                    )
                    self._send_json(result, code=200 if result.get("ok") else 400)
                    return
                if control_handler:
                    result = control_handler.handle_post(path, payload)
                    if result is not None:
                        self._send_json(result, code=200 if result.get("ok", True) else 404)
                        return
                if not event_handler:
                    self._send_json({"ok": False, "message": "Event handling is not configured."}, code=503)
                    return
                if len(parts) < 2 or parts[0] != "event":
                    legacy = _legacy_event(path, payload)
                    if not legacy:
                        LOGGER.warning("Unknown endpoint: raw path=%s normalized path=%s payload=%s", self.path, path, payload)
                        self._send_json({"ok": False, "message": "Unknown endpoint."}, code=404)
                        return
                    event_type, payload = legacy
                else:
                    event_type = parts[1]
                result = event_handler(event_type, payload)
                self._send_json(result, code=200 if result.get("ok") else 400)

            def log_message(self, format, *args):
                return

            def _handle_ui_post(self, path, raw_bytes):
                if not control_handler:
                    self._redirect("/")
                    return
                wants_json = str(self.headers.get("X-Viper-Async") or "").lower() == "true"
                if path == "/ui/chimes/upload":
                    filename, content = _multipart_file(raw_bytes, self.headers.get("Content-Type", ""))
                    control_handler.upload_chime(filename, content)
                    self._redirect(self._return_path())
                    return
                if path == "/ui/chimes/upload-folder":
                    for filename, content in _multipart_files(raw_bytes, self.headers.get("Content-Type", "")):
                        control_handler.upload_chime(filename, content)
                    self._redirect(self._return_path())
                    return
                payload = _form_payload(raw_bytes)
                result = _ui_action(path, payload, control_handler, event_handler)
                if result is None:
                    LOGGER.warning("Unknown UI action: raw path=%s normalized path=%s payload=%s", self.path, path, payload)
                    self._send_json({"ok": False, "message": "Unknown UI action."}, code=404)
                    return
                if wants_json:
                    self._send_json(result, code=200 if result.get("ok", True) else 400)
                    return
                self._redirect(self._return_path())

            def _send_json(self, payload, code=200):
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html, code=200):
                body = str(html or "").encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_file(self, path):
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location):
                self.send_response(303)
                self.send_header("Location", location)
                self.end_headers()

            def _return_path(self):
                referer = self.headers.get("Referer") or "/"
                parsed_referer = urlparse(referer)
                if parsed_referer.path == "/":
                    return "/" + (f"?{parsed_referer.query}" if parsed_referer.query else "")
                return "/"

        return Handler


def _legacy_event(path, payload):
    normalized = str(path or "").strip().lower().rstrip("/")
    if normalized in {"/remote/broadcast", "/remote/broadcast_push"}:
        data = dict(payload or {})
        data["push"] = normalized.endswith("broadcast_push")
        data["message"] = data.get("message") or data.get("broadcast_text") or ""
        return "broadcast", data
    if normalized == "/cinderella":
        return "vacuum", dict(payload or {})
    if normalized in {"/doorbell-webhook", "/doorbell-webhook/front"}:
        data = dict(payload or {})
        data.setdefault("door", "front")
        data.setdefault("action", "pressed")
        return "doorbell", data
    if normalized == "/doorbell-webhook/back":
        data = dict(payload or {})
        data.setdefault("door", "back")
        data.setdefault("action", "pressed")
        return "doorbell", data
    return None


def _viper_path(path):
    path = str(path or "/")
    if path in {"", "/"}:
        return "/"
    for marker in ("/ui/", "/event/", "/chimes/"):
        index = path.find(marker)
        if index >= 0:
            return path[index:]
    return path


def _form_payload(raw_bytes):
    parsed = parse_qs(raw_bytes.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _ui_action(path, payload, control_handler, event_handler):
    if path == "/ui/control/armed":
        return control_handler.handle_post("/api/control/armed", payload)
    if path == "/ui/control/global_mute":
        return control_handler.handle_post("/api/control/global_mute", payload)
    if path == "/ui/control/ice_maker":
        return control_handler.handle_post("/api/control/ice_maker/enabled", payload)
    if path == "/ui/speakers":
        return control_handler.handle_post("/api/control/speakers", payload)
    if path.startswith("/ui/speakers/") and path.endswith("/enabled"):
        name = path[len("/ui/speakers/") : -len("/enabled")]
        return control_handler.handle_post(f"/api/control/speakers/{name}/enabled", payload)
    if path.startswith("/ui/speakers/") and path.endswith("/delete"):
        name = path[len("/ui/speakers/") : -len("/delete")]
        return control_handler.handle_post(f"/api/control/speakers/{name}/delete", payload)
    if path.startswith("/ui/speakers/") and path.endswith("/route"):
        name = path[len("/ui/speakers/") : -len("/route")]
        route_state = str(payload.get("route_state") or "")
        route, _sep, state = route_state.partition(":")
        return control_handler.handle_post(f"/api/control/speakers/{name}/route", {"route": route, "state": state})
    if path == "/ui/chimes/assign":
        return control_handler.handle_post("/api/control/chimes", payload)
    if path == "/ui/settings":
        return control_handler.handle_post("/api/control/settings", payload)
    if path == "/ui/hvac":
        return control_handler.handle_post("/api/control/hvac", payload)
    if path == "/ui/vacuum":
        return control_handler.handle_post("/api/control/vacuum", payload)
    if path == "/ui/chimes/delete":
        return control_handler.handle_post("/api/chimes/delete", payload)
    if path.rstrip("/") == "/ui/chimes/test" and event_handler:
        event_name = str(payload.get("event") or "").strip().lower()
        category = "fridge" if event_name.startswith(("fridge", "freezer")) else "doorbell" if event_name.endswith("doorbell") else "utilities"
        return event_handler("chime", {"filename": payload.get("filename", ""), "category": category, "event": event_name})
    if path == "/ui/test/doorbell/front" and event_handler:
        return event_handler("doorbell", {"door": "front", "action": "pressed"})
    if path == "/ui/test/doorbell/back" and event_handler:
        return event_handler("doorbell", {"door": "back", "action": "pressed"})
    if path == "/ui/test/doorbell_video/front" and event_handler:
        return event_handler("doorbell_video", {"door": "front", "source": "manual_web"})
    if path == "/ui/test/doorbell_video/back" and event_handler:
        return event_handler("doorbell_video", {"door": "back", "source": "manual_web"})
    if path == "/ui/test/fridge/fridge" and event_handler:
        return event_handler("fridge", {"appliance": "fridge", "state": "open"})
    if path == "/ui/test/fridge/fridge_closed" and event_handler:
        return event_handler("fridge", {"appliance": "fridge", "state": "closed"})
    if path == "/ui/test/fridge/freezer" and event_handler:
        return event_handler("fridge", {"appliance": "freezer", "state": "open"})
    if path == "/ui/test/fridge/freezer_closed" and event_handler:
        return event_handler("fridge", {"appliance": "freezer", "state": "closed"})
    if path == "/ui/test/pushover" and event_handler:
        return event_handler("pushover_test", {"title": "Viper Core Test", "message": "Viper Core Pushover is working."})
    if path == "/ui/broadcast" and event_handler:
        return event_handler("broadcast", {"message": payload.get("message", ""), "channel": "manual"})
    return None


def _multipart_file(body, content_type):
    files = _multipart_files(body, content_type)
    return files[0] if files else ("", b"")


def _multipart_files(body, content_type):
    if "multipart/form-data" not in str(content_type or ""):
        return []
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    files = []
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if field_name not in {"file", "files"}:
            continue
        files.append((part.get_filename() or "", part.get_payload(decode=True) or b""))
    return files
