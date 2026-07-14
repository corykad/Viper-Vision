import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthServer:
    def __init__(self, host, port, state_provider, event_handler=None):
        self._server = ThreadingHTTPServer((host, port), self._make_handler(state_provider, event_handler))

    def serve_forever(self):
        self._server.serve_forever()

    def shutdown(self):
        self._server.shutdown()

    @staticmethod
    def _make_handler(state_provider, event_handler):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state = state_provider()
                if self.path in {"/", "/health"}:
                    self._send_json(state)
                    return
                if self.path == "/ready":
                    code = 200 if state.get("home_assistant", {}).get("ok") else 503
                    self._send_json(state, code=code)
                    return
                self._send_json({"ok": False, "message": "Not found."}, code=404)

            def do_POST(self):
                if not event_handler:
                    self._send_json({"ok": False, "message": "Event handling is not configured."}, code=503)
                    return
                parts = [item for item in self.path.strip("/").split("/") if item]
                if len(parts) < 2 or parts[0] != "event":
                    self._send_json({"ok": False, "message": "Unknown endpoint."}, code=404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
                try:
                    payload = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    self._send_json({"ok": False, "message": "Request body must be JSON."}, code=400)
                    return
                result = event_handler(parts[1], payload)
                self._send_json(result, code=200 if result.get("ok") else 400)

            def log_message(self, format, *args):
                return

            def _send_json(self, payload, code=200):
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
