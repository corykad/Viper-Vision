import logging
import signal
import shutil
import threading
import time

from .config import configure_logging, load_config
from .control import ControlApi, ControlState
from .events import EventProcessor
from .ha import HomeAssistantClient
from .health_server import HealthServer


LOGGER = logging.getLogger("viper_core")
STOP_EVENT = threading.Event()
REQUIRED_ENTITY_IDS = [
    "switch.refrigerator_cubed_ice",
    "binary_sensor.refrigerator_fridge_door",
    "binary_sensor.refrigerator_freezer_door",
    "input_boolean.keep_ice_maker_on",
    "input_boolean.ice_maker_auto_refill_running",
    "counter.ice_usage_counter",
    "climate.office_heat_pump_alexa",
    "climate.living_room_heat_pump_alexa",
    "climate.kitchen_heat_pump_alexa",
    "climate.jamie_s_room_heat_pump_alexa",
    "climate.master_bedroom_heat_pump_alexa",
]


def main():
    config = load_config()
    configure_logging(config.log_level)
    client = HomeAssistantClient(config.ha_url, config.ha_token)
    control_state = ControlState()
    control_api = ControlApi(control_state, client)
    events = EventProcessor(config, client, control_state)
    state = {
        "ok": False,
        "service": "Viper Core",
        "version": "0.1.21",
        "config": config.public_dict(),
        "home_assistant": {"ok": False, "message": "No health check has run yet."},
        "dependencies": {"ok": False, "message": "No dependency check has run yet."},
        "devices": {"ok": False, "message": "No device status refresh has run yet.", "heat_pumps": [], "airflow": [], "vacuum": {}, "refrigerator": {}},
        "recent_events": events.recent_events,
    }

    def current_state():
        state["recent_events"] = list(events.recent_events)
        state["control"] = control_state.public_state()
        state["runtime"] = {"ffmpeg": shutil.which("ffmpeg") or ""}
        return dict(state)

    server = HealthServer("0.0.0.0", 8099, current_state, events.handle, control_api)
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
            dependency_status = client.dependency_status(REQUIRED_ENTITY_IDS) if ha_status.get("ok") else {
                "ok": False,
                "message": "Skipped because Home Assistant API is not ready.",
                "entities": {},
            }
            state["home_assistant"] = ha_status
            state["dependencies"] = dependency_status
            state["devices"] = control_api.device_status() if ha_status.get("ok") else {
                "ok": False,
                "message": "Skipped because Home Assistant API is not ready.",
                "heat_pumps": [],
                "airflow": [],
                "vacuum": {},
                "refrigerator": {},
            }
            state["ok"] = bool(ha_status.get("ok") and dependency_status.get("ok"))
            if ha_status.get("ok"):
                LOGGER.info("Home Assistant API reachable in %sms.", ha_status.get("latency_ms"))
                if not dependency_status.get("ok"):
                    LOGGER.warning("Viper Core dependency check failed: %s", dependency_status.get("message"))
            else:
                LOGGER.warning("Home Assistant API check failed: %s", ha_status.get("message"))
            STOP_EVENT.wait(config.health_check_seconds)
    finally:
        server.shutdown()
        LOGGER.info("Viper Core stopped.")


if __name__ == "__main__":
    main()
