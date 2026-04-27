import unittest
from unittest.mock import patch

import viper_config as cfg
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


if __name__ == "__main__":
    unittest.main()
