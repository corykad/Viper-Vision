import logging
import signal
import threading
import time

from .config import configure_logging, load_config
from .events import EventProcessor
from .ha import HomeAssistantClient
from .health_server import HealthServer


LOGGER = logging.getLogger("viper_core")
STOP_EVENT = threading.Event()


def main():
    config = load_config()
    configure_logging(config.log_level)
    client = HomeAssistantClient(config.ha_url, config.ha_token)
    events = EventProcessor(config, client)
    state = {
        "ok": False,
        "service": "Viper Core",
        "version": "0.1.0",
        "config": config.public_dict(),
        "home_assistant": {"ok": False, "message": "No health check has run yet."},
        "recent_events": events.recent_events,
    }

    def current_state():
        state["recent_events"] = list(events.recent_events)
        return dict(state)

    server = HealthServer("0.0.0.0", 8099, current_state, events.handle)
    server_thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    server_thread.start()

    def stop(_signum=None, _frame=None):
        STOP_EVENT.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    LOGGER.info("Viper Core started. Health page is listening on port 8099.")
    try:
        while not STOP_EVENT.is_set():
            ha_status = client.api_status()
            state["home_assistant"] = ha_status
            state["ok"] = bool(ha_status.get("ok"))
            if ha_status.get("ok"):
                LOGGER.info("Home Assistant API reachable in %sms.", ha_status.get("latency_ms"))
            else:
                LOGGER.warning("Home Assistant API check failed: %s", ha_status.get("message"))
            STOP_EVENT.wait(config.health_check_seconds)
    finally:
        server.shutdown()
        LOGGER.info("Viper Core stopped.")


if __name__ == "__main__":
    main()
