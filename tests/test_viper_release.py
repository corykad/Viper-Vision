import unittest
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import viper_config as cfg
import viper_discovery as discovery
import viper_diagnostics as diagnostics
import viper_ha_listener as ha_listener
import main


def _sample_states():
    return [
        {
            "entity_id": "vacuum.cinderella",
            "state": "docked",
            "attributes": {
                "friendly_name": "cinderella",
                "fan_speed": "max",
                "fan_speed_list": ["quiet", "balanced", "turbo", "max"],
            },
        },
        {
            "entity_id": "select.cinderella_mop_mode",
            "state": "standard",
            "attributes": {
                "friendly_name": "cinderella Mop mode",
                "options": ["standard", "deep", "deep_plus"],
            },
        },
        {
            "entity_id": "select.cinderella_mop_intensity",
            "state": "extreme",
            "attributes": {
                "friendly_name": "cinderella Mop intensity",
                "options": ["mild", "moderate", "intense", "extreme"],
            },
        },
        {
            "entity_id": "number.cinderella_volume",
            "state": "90.0",
            "attributes": {
                "friendly_name": "cinderella Volume",
                "min": 0,
                "max": 100,
                "step": 1,
            },
        },
        {
            "entity_id": "switch.cinderella_dock_child_lock",
            "state": "off",
            "attributes": {"friendly_name": "cinderella Dock Child lock"},
        },
        {
            "entity_id": "button.cinderella_full_cleaning",
            "state": "unknown",
            "attributes": {"friendly_name": "cinderella Full Cleaning"},
        },
        {
            "entity_id": "sensor.cinderella_battery",
            "state": "100",
            "attributes": {"friendly_name": "cinderella Battery"},
        },
        {
            "entity_id": "binary_sensor.cinderella_charging",
            "state": "on",
            "attributes": {"friendly_name": "cinderella Charging"},
        },
    ]


def _sample_maps_response():
    return {
        "changed_states": [],
        "service_response": {
            "vacuum.cinderella": {
                "maps": [
                    {
                        "flag": 0,
                        "name": "",
                        "rooms": {
                            "1": "Living room",
                            "2": "Bathroom",
                            "7": "Kitchen",
                        },
                    }
                ]
            }
        },
    }


class FakeDashboard:
    def __init__(self):
        self.config = cfg.validate_and_normalize_config(
            {
                "ha_ip": "192.168.1.10",
                "ha_port": "8123",
                "ha_token": "test-token",
                "gemini_api_key": "test-key",
                "prompts": {"Standard": "Analyze this frame."},
                "active_prompt": "Standard",
                "speakers": {},
                "vacuum_rooms": {},
            }
        )
        self.saved = False
        self.service_calls = []

    def save_config(self):
        self.saved = True
        self.config = cfg.validate_and_normalize_config(self.config)

    def _call_ha_service_data(self, domain_service, data):
        self.service_calls.append((domain_service, data))
        return True

    def _call_ha_service_response(self, domain_service, data):
        self.service_calls.append((domain_service, data))
        return {"ok": True, "data": _sample_maps_response()}

    def _parse_roborock_rooms(self, data, entity_id):
        return main.ViperDashboard._parse_roborock_rooms(self, data, entity_id)

    def _sanitize_vacuum_rooms(self, rooms):
        return main.ViperDashboard._sanitize_vacuum_rooms(self, rooms)

    def _save_vacuum_rooms(self, entity_id, rooms):
        return main.ViperDashboard._save_vacuum_rooms(self, entity_id, rooms)


class ViperReleaseTests(unittest.TestCase):
    def setUp(self):
        self.previous_dash_app = main.dash_app
        self.client = main.app.test_client()
        main.app.config.update(TESTING=True)

    def tearDown(self):
        main.dash_app = self.previous_dash_app

    def test_config_normalizes_saved_vacuum_rooms(self):
        normalized = cfg.validate_and_normalize_config(
            {
                "vacuum_rooms": {
                    "vacuum.cinderella": [
                        {"name": "Kitchen", "map": "Current map", "segment": "7"},
                        {"name": "Bad room", "segment": "not-a-number"},
                        "not a room",
                    ]
                }
            }
        )

        rooms = normalized["vacuum_rooms"]["vacuum.cinderella"]
        self.assertEqual(rooms, [{"label": "Kitchen (7)", "name": "Kitchen", "map": "Current map", "segment": 7}])

    def test_roborock_map_response_parses_rooms(self):
        fake = FakeDashboard()

        rooms = fake._parse_roborock_rooms(_sample_maps_response(), "vacuum.cinderella")

        self.assertEqual([room["name"] for room in rooms], ["Bathroom", "Kitchen", "Living room"])
        self.assertEqual([room["segment"] for room in rooms], [2, 7, 1])
        self.assertEqual(rooms[1]["label"], "Kitchen (7)")

    def test_remote_page_renders_vacuum_controls_from_mocked_home_assistant(self):
        main.dash_app = FakeDashboard()
        main.dash_app.config["vacuum_rooms"]["vacuum.cinderella"] = [
            {"label": "Kitchen (7)", "name": "Kitchen", "map": "Current map", "segment": 7}
        ]

        with patch.object(main.discovery, "get_ha_states", return_value={"ok": True, "states": _sample_states(), "entity_count": 8}):
            response = self.client.get("/remote?vacuum_entity=vacuum.cinderella")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Vacuum Controls", body)
        self.assertIn("Set suction speed", body)
        self.assertIn("cinderella Mop mode", body)
        self.assertIn("Kitchen", body)
        self.assertNotIn("cinderella Full Cleaning", body)

    def test_remote_page_renders_diagnostics_controls(self):
        main.dash_app = FakeDashboard()

        with patch.object(main.discovery, "get_ha_states", return_value={"ok": True, "states": _sample_states(), "entity_count": 8}):
            response = self.client.get("/remote")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Diagnostics", body)
        self.assertIn("Create Support Bundle", body)

    def test_remote_diagnostics_endpoint_returns_json(self):
        main.dash_app = FakeDashboard()

        response = self.client.get("/remote/diagnostics?format=json")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["app"]["version"], "1.2")
        self.assertIn("ffmpeg", payload)

    def test_web_room_refresh_saves_rooms_to_config(self):
        main.dash_app = FakeDashboard()

        response = self.client.post("/remote/vacuum/rooms", data={"vacuum_entity": "vacuum.cinderella"})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(main.dash_app.saved)
        rooms = main.dash_app.config["vacuum_rooms"]["vacuum.cinderella"]
        self.assertEqual(len(rooms), 3)
        self.assertEqual(rooms[1]["name"], "Kitchen")

    def test_web_room_clean_sends_segment_clean_command(self):
        main.dash_app = FakeDashboard()

        response = self.client.post(
            "/remote/vacuum/clean_rooms",
            data={
                "vacuum_entity": "vacuum.cinderella",
                "segments": ["7", "1"],
                "repeat": "3",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(main.dash_app.service_calls), 1)
        service, payload = main.dash_app.service_calls[0]
        self.assertEqual(service, "vacuum/send_command")
        self.assertEqual(payload["entity_id"], "vacuum.cinderella")
        self.assertEqual(payload["command"], "app_segment_clean")
        self.assertEqual(payload["params"], [{"segments": [7, 1], "repeat": 3}])

    def test_web_vacuum_setting_routes_select_number_and_child_lock(self):
        main.dash_app = FakeDashboard()

        self.client.post(
            "/remote/vacuum/setting",
            data={
                "vacuum_entity": "vacuum.cinderella",
                "entity_id": "select.cinderella_mop_mode",
                "domain": "select",
                "option": "deep",
            },
        )
        self.client.post(
            "/remote/vacuum/setting",
            data={
                "vacuum_entity": "vacuum.cinderella",
                "entity_id": "number.cinderella_volume",
                "domain": "number",
                "value": "55",
            },
        )
        self.client.post(
            "/remote/vacuum/setting",
            data={
                "vacuum_entity": "vacuum.cinderella",
                "entity_id": "switch.cinderella_dock_child_lock",
                "domain": "switch",
                "turn_on": "1",
            },
        )

        self.assertEqual(
            main.dash_app.service_calls,
            [
                ("select/select_option", {"entity_id": "select.cinderella_mop_mode", "option": "deep"}),
                ("number/set_value", {"entity_id": "number.cinderella_volume", "value": 55.0}),
                ("switch/turn_on", {"entity_id": "switch.cinderella_dock_child_lock"}),
            ],
        )

    def test_cinderella_specific_dock_error_prefers_dock_bucket(self):
        main.dash_app = FakeDashboard()
        main.dash_app.config["cinderella_messages"]["specific_errors"]["dock_duct_blockage"] = [
            "Dock duct blockage test message."
        ]

        message = main.choose_cinderella_message("error", error="duct_blockage", source="dock")

        self.assertEqual(message, "Dock duct blockage test message.")

    def test_v12_config_migrates_flat_doorbell_fields_to_triggers(self):
        normalized = cfg.validate_and_normalize_config(
            {
                "ha_ip": "192.168.1.10",
                "front_camera_id": "front123",
                "back_camera_id": "back456",
                "rtsp_front": "rtsp://example/front",
                "front_doorbell_mqtt_topic": "ring/root/camera/front123/motion/state",
            }
        )

        self.assertTrue(normalized["ha_listener_enabled"])
        self.assertEqual(normalized["doorbell_triggers"]["front"]["rtsp_url"], "rtsp://example/front")
        self.assertEqual(normalized["doorbell_triggers"]["front"]["camera_id"], "front123")
        self.assertEqual(
            normalized["doorbell_triggers"]["front"]["mqtt_topic"],
            "ring/root/camera/front123/motion/state",
        )
        self.assertEqual(
            normalized["doorbell_triggers"]["back"]["rtsp_url"],
            "rtsp://192.168.1.10:8554/back456_live",
        )

    def test_ha_listener_routes_doorbell_state_to_rtsp_action(self):
        config = cfg.validate_and_normalize_config(
            {
                "doorbell_triggers": {
                    "front": {
                        "enabled": True,
                        "source": "ha_state",
                        "trigger_entity_id": "binary_sensor.front_doorbell_motion",
                        "rtsp_url": "rtsp://camera/front",
                    }
                }
            }
        )

        actions = ha_listener.route_state_change(
            config,
            "binary_sensor.front_doorbell_motion",
            {"state": "off"},
            {"state": "on"},
        )

        self.assertEqual(actions[0]["type"], "doorbell")
        self.assertEqual(actions[0]["side"], "front")
        self.assertEqual(actions[0]["rtsp_url"], "rtsp://camera/front")

    def test_ha_listener_ignores_repeated_active_doorbell_state(self):
        config = cfg.validate_and_normalize_config(
            {
                "doorbell_triggers": {
                    "back": {
                        "enabled": True,
                        "source": "ha_state",
                        "trigger_entity_id": "binary_sensor.back_doorbell_motion",
                        "rtsp_url": "rtsp://camera/back",
                    }
                }
            }
        )

        actions = ha_listener.route_state_change(
            config,
            "binary_sensor.back_doorbell_motion",
            {"state": "on"},
            {"state": "on"},
        )

        self.assertEqual(actions, [])

    def test_ha_listener_routes_roborock_status_and_errors(self):
        config = cfg.validate_and_normalize_config({"cinderella_enabled": True})

        status_actions = ha_listener.route_state_change(
            config,
            "sensor.cinderella_status",
            {"state": "cleaning"},
            {"state": "washing_the_mop"},
        )
        error_actions = ha_listener.route_state_change(
            config,
            "sensor.cinderella_dock_dock_error",
            {"state": "ok"},
            {"state": "duct_blockage"},
        )

        self.assertEqual(status_actions, [{"type": "cinderella", "event": "washing", "error": "", "source": "vacuum"}])
        self.assertEqual(error_actions, [{"type": "cinderella", "event": "error", "error": "duct_blockage", "source": "dock"}])

    def test_ha_listener_derives_ring_rtsp_url(self):
        self.assertEqual(
            ha_listener.derive_rtsp_url("192.168.1.10", "abc123"),
            "rtsp://192.168.1.10:8554/abc123_live",
        )

    def test_ha_host_normalization_accepts_url_and_plain_host(self):
        self.assertEqual(discovery.normalize_ha_host("http://homeassistant.local:8123"), ("homeassistant.local", "8123"))
        self.assertEqual(discovery.normalize_ha_host("192.168.1.10"), ("192.168.1.10", "8123"))

    def test_first_run_assistant_helpers_are_safe_without_virtualbox(self):
        with patch.object(main.shutil, "which", return_value=None), patch.object(main.Path, "exists", return_value=False):
            self.assertEqual(main.find_vboxmanage(), "")
            status = main.get_virtualbox_status()
        self.assertFalse(status["installed"])
        self.assertIn("not found", status["message"].lower())

    def test_official_setup_links_include_home_assistant_install(self):
        self.assertIn("home-assistant.io/installation/windows", main.OFFICIAL_LINKS["ha_windows"])
        self.assertIn("virtualbox.org", main.OFFICIAL_LINKS["virtualbox"])

    def test_support_bundle_redacts_secrets(self):
        config = cfg.validate_and_normalize_config(
            {
                "ha_token": "super-secret-ha-token",
                "gemini_api_key": "secret-gemini-key",
                "pushover_user_key": "secret-push-user",
                "pushover_api_token": "secret-push-token",
                "mqtt_password": "secret-mqtt-password",
                "front_camera_id": "ring-front-camera-id",
                "ring_topic_root": "ring-location-id",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = diagnostics.create_support_bundle(config, output_dir=tmp)
            with zipfile.ZipFile(result["path"], "r") as zf:
                combined = "\n".join(zf.read(name).decode("utf-8", errors="ignore") for name in zf.namelist())

        self.assertNotIn("super-secret-ha-token", combined)
        self.assertNotIn("secret-gemini-key", combined)
        self.assertNotIn("secret-push-user", combined)
        self.assertNotIn("secret-push-token", combined)
        self.assertNotIn("secret-mqtt-password", combined)
        self.assertNotIn("ring-front-camera-id", combined)
        self.assertIn("[REDACTED]", combined)

    def test_installer_metadata_is_v12_and_help_is_packaged(self):
        root = Path(__file__).resolve().parents[1]
        iss = (root / "ViperVision.iss").read_text(encoding="utf-8")
        spec = (root / "ViperVision.spec").read_text(encoding="utf-8")
        self.assertIn('#define MyAppVersion "1.2"', iss)
        self.assertIn('OutputBaseFilename=ViperVision-v{#MyAppVersion}-Setup', iss)
        self.assertIn('("help", "help")', spec)

    def test_crash_report_formatter_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            crash_path = Path(tmp) / "crash.txt"
            with patch.object(main, "CRASH_LOG_PATH", crash_path), patch.object(main.logging, "critical"):
                try:
                    raise RuntimeError("diagnostic crash test")
                except RuntimeError:
                    exc_type, exc_value, exc_tb = main.sys.exc_info()
                    text = main._write_crash_report(exc_type, exc_value, exc_tb, source="test")
            saved = crash_path.read_text(encoding="utf-8")
        self.assertIn("diagnostic crash test", text)
        self.assertIn("diagnostic crash test", saved)

    def test_help_files_exist_for_f1_topics(self):
        root = Path(__file__).resolve().parents[1]
        for name in [
            "index.html",
            "ha-install.html",
            "setup.html",
            "ring-setup.html",
            "scenarios.html",
            "tts.html",
            "speakers.html",
            "vacuum.html",
            "troubleshooting.html",
            "style.css",
        ]:
            self.assertTrue((root / "help" / name).exists(), name)

    def test_release_checklist_exists(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        self.assertIn(".\\run_tests.ps1", text)
        self.assertIn(".\\build_installer.ps1", text)
        self.assertIn(".\\smoke_installer.ps1", text)
        self.assertIn("Create Support Bundle", text)

    def test_smoke_installer_script_exists(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "smoke_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("ViperVision-v1.2-Setup.exe", text)
        self.assertIn("VIPER_CLEAN_FIRST_RUN_TEST", text)
        self.assertIn("ffmpeg.exe", text)


if __name__ == "__main__":
    unittest.main()
