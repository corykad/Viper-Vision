import unittest
import tempfile
import zipfile
import re
from pathlib import Path
from html.parser import HTMLParser
from unittest import mock
from unittest.mock import patch
from PIL import Image

import viper_config as cfg
import viper_discovery as discovery
import viper_diagnostics as diagnostics
import viper_audio as audio
import viper_ha_listener as ha_listener
import viper_vacuum as vacuum
import viper_vision as vision
import viper_release_audit as release_audit
import accessibility_report
import main


class RemoteAccessibilityParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.buttons = []
        self.controls = []
        self.labels_for = set()
        self._button = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        parent_tags = [item[0] for item in self.stack]
        self.stack.append((tag, attrs_dict))
        if tag == "label" and attrs_dict.get("for"):
            self.labels_for.add(attrs_dict["for"])
        if tag == "button":
            self._button = {"attrs": attrs_dict, "text": "", "line": self.getpos()[0]}
        if tag in {"input", "select", "textarea"}:
            self.controls.append({
                "tag": tag,
                "attrs": attrs_dict,
                "line": self.getpos()[0],
                "inside_label": "label" in parent_tags,
            })

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None:
            self.buttons.append(self._button)
            self._button = None
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                del self.stack[idx:]
                break

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data


def _rendered_text(text):
    return " ".join(str(text or "").split())


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
                "options": ["off", "standard", "deep", "deep_plus"],
            },
        },
        {
            "entity_id": "select.cinderella_cleaning_mode",
            "state": "vacuum_and_mop",
            "attributes": {
                "friendly_name": "cinderella Cleaning mode",
                "options": ["vacuum_and_mop", "vacuum_only", "mop_only"],
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
            "entity_id": "select.cinderella_dock_empty_mode",
            "state": "smart",
            "attributes": {
                "friendly_name": "cinderella Dock Empty mode",
                "options": ["off", "smart", "light", "balanced", "max"],
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
        self.last_video_analysis = {}
        self.video_analysis_requests = []

    def save_config(self):
        self.saved = True
        self.config = cfg.validate_and_normalize_config(self.config)

    def _call_ha_service_data(self, domain_service, data, **kwargs):
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

    def _run_manual_doorbell_video_analysis(self, side, seconds=None, source="test"):
        self.video_analysis_requests.append((side, seconds, source))


class ViperReleaseTests(unittest.TestCase):
    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            if self.target:
                self.target(*self.args, **self.kwargs)

    def test_service_unavailable_fast_pass_is_weak_and_detected(self):
        text = "The AI service is currently unavailable."
        self.assertTrue(vision._description_is_weak(text))
        self.assertTrue(vision._description_is_service_unavailable(text))
        self.assertFalse(vision._description_is_service_unavailable("A delivery driver is at the front door."))

    def test_video_analysis_defaults_are_bounded(self):
        settings = vision.normalize_video_analysis_settings(
            {"doorbell_video_analysis": {"mode": "smart", "manual_clip_seconds": 99, "max_manual_clip_seconds": 12}}
        )
        self.assertEqual(settings["mode"], "smart")
        self.assertEqual(settings["model"], vision.GEMINI_VISION_MODEL)
        self.assertEqual(settings["manual_clip_seconds"], 12)
        self.assertEqual(settings["smart_clip_seconds"], 3)
        self.assertEqual(settings["fps"], 2)

    def test_legacy_gemini_vision_models_migrate_to_current_default(self):
        for old_model in vision.LEGACY_GEMINI_VISION_MODELS:
            with self.subTest(old_model=old_model):
                settings = vision.normalize_video_analysis_settings(
                    {"doorbell_video_analysis": {"model": old_model}}
                )
                self.assertEqual(settings["model"], vision.GEMINI_VISION_MODEL)

    def test_gemini_vision_default_matches_config_default(self):
        defaults = cfg.get_default_config()
        self.assertEqual(vision.GEMINI_VISION_MODEL, "gemini-3.5-flash")
        self.assertEqual(defaults["doorbell_video_analysis"]["model"], vision.GEMINI_VISION_MODEL)
        self.assertEqual(vision.VIDEO_ANALYSIS_MODEL, vision.GEMINI_VISION_MODEL)

    def test_background_refinement_captures_extra_stills_without_blocking_fast_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            frames = []
            for index in range(2):
                frame = tmp_path / f"frame_{index}.jpg"
                Image.new("RGB", (320, 180), color=(80 + index, 80, 80)).save(frame, format="JPEG")
                frames.append(str(frame))

            with patch.object(vision.time, "sleep") as sleep, patch.object(vision, "grab_frame", side_effect=frames) as grab:
                captured = vision.capture_background_refinement_frames(
                    "rtsp://camera",
                    tmp_path,
                    "refine_front_1",
                    min_bytes=14000,
                    count=2,
                )

            self.assertEqual(captured, frames)
            self.assertEqual(grab.call_count, 2)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(all(call.kwargs["fast_mode"] for call in grab.call_args_list))

    def test_weak_still_refinement_uses_multi_frame_answer_when_strong(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            initial = tmp_path / "initial.jpg"
            later = tmp_path / "later.jpg"
            Image.new("RGB", (320, 180), color=(80, 80, 80)).save(initial, format="JPEG")
            Image.new("RGB", (320, 180), color=(90, 90, 90)).save(later, format="JPEG")

            with patch.object(vision, "capture_background_refinement_frames", return_value=[str(later)]), patch.object(
                vision,
                "get_gemini_multi_image_description",
                return_value="A delivery driver is walking away from the front porch with no package visible.",
            ) as multi:
                refined, frames = vision.refine_weak_doorbell_still_description(
                    "rtsp://camera",
                    tmp_path,
                    "refine_front_1",
                    min_bytes=14000,
                    location="front door",
                    first_description="The image is unclear.",
                    base_prompt="Describe the front door.",
                    initial_frame=str(initial),
                )

            self.assertIn("delivery driver", refined)
            self.assertEqual(frames, [str(initial), str(later)])
            self.assertEqual(multi.call_count, 1)

    def test_rtsp_frame_sanity_rejects_green_artifact_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.jpg"
            img = Image.new("RGB", (640, 360), color=(60, 60, 60))
            for x in range(0, 180):
                for y in range(0, 180):
                    img.putpixel((x, y), (0, 255, 0))
            img.save(bad, format="JPEG")

            self.assertFalse(vision._frame_passes_sanity_check(bad))

    def test_rtsp_frame_sanity_rejects_smaller_green_bands(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "banded.jpg"
            img = Image.new("RGB", (640, 360), color=(80, 80, 80))
            for x in range(0, 640):
                for y in range(0, 16):
                    img.putpixel((x, y), (0, 255, 0))
            img.save(bad, format="JPEG")

            self.assertFalse(vision._frame_passes_sanity_check(bad))

    def test_rtsp_frame_sanity_allows_normal_backyard_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.jpg"
            img = Image.new("RGB", (640, 360), color=(120, 125, 120))
            for x in range(0, 640):
                for y in range(210, 360):
                    img.putpixel((x, y), (55, 120, 45))
            img.save(good, format="JPEG")

            self.assertTrue(vision._frame_passes_sanity_check(good))

    def test_green_artifact_ai_description_is_weak_for_refinement(self):
        text = "A person stands on the porch, but the feed is heavily distorted by large green and grey bands of digital corruption."
        self.assertTrue(vision._description_is_weak(text))

    def test_vacuum_actions_hide_pause_and_stop_when_not_running(self):
        docked = main.vacuum_basic_actions_for_state("docked")
        docked_services = {action["service"] for action in docked}
        self.assertIn("vacuum/start", docked_services)
        self.assertNotIn("vacuum/pause", docked_services)
        self.assertNotIn("vacuum/stop", docked_services)

        running = main.vacuum_basic_actions_for_state("cleaning")
        running_services = {action["service"] for action in running}
        self.assertIn("vacuum/pause", running_services)
        self.assertIn("vacuum/stop", running_services)
        self.assertNotIn("vacuum/start", running_services)

    def test_vacuum_cleaning_mode_maps_to_roborock_select(self):
        calls = main.vacuum_cleaning_mode_service_calls(
            "vacuum.cinderella",
            _sample_states(),
            "mop_only",
        )
        self.assertIn(
            ("select/select_option", {"entity_id": "select.cinderella_cleaning_mode", "option": "mop_only"}),
            calls,
        )

    def test_config_normalizes_vacuum_cleaning_mode(self):
        self.assertEqual(cfg.validate_and_normalize_config({"vacuum_cleaning_mode": "mop_only"})["vacuum_cleaning_mode"], "mop_only")
        self.assertEqual(cfg.validate_and_normalize_config({"vacuum_cleaning_mode": "bad"})["vacuum_cleaning_mode"], "vacuum_mop")

    def test_doorbell_photo_description_can_be_custom_per_door(self):
        config = cfg.validate_and_normalize_config(
            {
                "ai_description_styles": {
                    "front_photo": "custom",
                    "back_photo": "fast_security",
                },
                "ai_custom_descriptions": {"front_photo": "front custom prompt"},
            }
        )

        self.assertEqual(cfg.get_doorbell_photo_prompt(config, "front"), "front custom prompt")
        self.assertIn("fast security summary", cfg.get_doorbell_photo_prompt(config, "back").lower())

    def test_doorbell_video_description_supports_jobs_and_placeholders(self):
        config = cfg.validate_and_normalize_config(
            {
                "ai_description_styles": {
                    "manual_video": "custom",
                    "smart_video": "custom",
                    "detailed_video": "balanced",
                },
                "ai_custom_descriptions": {
                    "manual_video": "Manual {side} {location}.",
                    "smart_video": "Smart after {first_description} at {location}.",
                },
            }
        )

        self.assertEqual(cfg.get_doorbell_video_prompt(config, "manual", side="front", location="front door"), "Manual front front door.")
        self.assertEqual(
            cfg.get_doorbell_video_prompt(config, "smart", first_description="unclear", location="back door"),
            "Smart after unclear at back door.",
        )
        self.assertIn("doorbell video", cfg.get_doorbell_video_prompt(config, "detailed").lower())

    def test_legacy_prompt_profiles_migrate_to_custom_description_jobs(self):
        config = cfg.validate_and_normalize_config(
            {
                "active_prompt": "Standard",
                "prompts": {
                    "Standard": cfg.get_default_config()["prompts"]["Standard"],
                    "Porch Detail": "custom front legacy text",
                },
                "doorbell_prompt_profiles": {"front": "Porch Detail", "back": "Standard"},
            }
        )

        self.assertEqual(config["ai_description_styles"]["front_photo"], "custom")
        self.assertEqual(config["ai_custom_descriptions"]["front_photo"], "custom front legacy text")
        self.assertEqual(config["ai_description_styles"]["back_photo"], cfg.DEFAULT_AI_DESCRIPTION_STYLES["back_photo"])

    def test_cut_off_video_response_is_detected(self):
        self.assertTrue(vision._looks_like_cut_off_video_response("The video shows a"))
        self.assertTrue(vision._looks_like_cut_off_video_response("At night, your front"))
        self.assertFalse(vision._looks_like_cut_off_video_response("The front porch is quiet, with no person visible."))

    def test_low_detail_video_response_is_detected(self):
        self.assertTrue(vision._looks_like_low_detail_video_response("A person walks by."))
        self.assertTrue(vision._looks_like_low_detail_video_response("Motion detected."))
        self.assertFalse(vision._looks_like_low_detail_video_response("No extra detail from the video."))
        self.assertFalse(
            vision._looks_like_low_detail_video_response(
                "A person is standing near the front door on the right side of the porch. "
                "They move toward the walkway while carrying a small package, and no vehicle is visible."
            )
        )

    def test_video_analysis_retries_short_complete_answer(self):
        class FakeResponse:
            def __init__(self, text):
                self.text = text
                self.candidates = []
                self.usage_metadata = None

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake video bytes")
            video_path = f.name
        try:
            calls = [
                FakeResponse("A person walks by."),
                FakeResponse(
                    "A person appears near the front door and moves from the porch toward the walkway. "
                    "No package or vehicle is visible, and there is no obvious safety concern in the clip."
                ),
            ]
            with patch.object(vision, "_generate_video_description", side_effect=calls) as generate:
                result = vision.analyze_video_clip(video_path, "Describe the video.", model_name="test-model", fps=2)

            self.assertIn("front door", result)
            self.assertEqual(generate.call_count, 2)
            self.assertGreaterEqual(generate.call_args_list[-1].kwargs["max_output_tokens"], 640)
        finally:
            Path(video_path).unlink(missing_ok=True)

    def test_smart_video_followup_is_strict(self):
        self.assertFalse(
            vision.should_run_automatic_video_followup(
                "smart",
                "A delivery driver is standing at the porch with a package.",
                "front",
                {"doorbell_video_analysis": {"smart_cooldown_seconds": 15}},
            )
        )
        vision._video_followup_last["front"] = 0
        self.assertTrue(
            vision.should_run_automatic_video_followup(
                "smart",
                "The image is unclear and hard to tell.",
                "front",
                {"doorbell_video_analysis": {"smart_cooldown_seconds": 15}},
            )
        )

    def setUp(self):
        self.previous_dash_app = main.dash_app
        self.client = main.app.test_client()
        main.app.config.update(TESTING=True)

    def tearDown(self):
        main.dash_app = self.previous_dash_app

    def test_first_run_auto_opens_modern_setup_wizard(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.first_run = True
        fake.clean_first_run_test = False
        fake.config = {}
        self.assertTrue(main.ViperDashboard.should_auto_open_setup_wizard(fake))

    def test_existing_env_credentials_do_not_force_setup_wizard(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.first_run = False
        fake.clean_first_run_test = False
        fake.config = {}
        with patch.object(cfg, "get_ha_settings", return_value={"ha_token": "ha-token"}), patch.object(
            cfg, "get_api_settings", return_value={"gemini_api_key": "gemini-key"}
        ):
            self.assertFalse(main.ViperDashboard.should_auto_open_setup_wizard(fake))

    def test_missing_credentials_open_setup_wizard_after_first_run(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.first_run = False
        fake.clean_first_run_test = False
        fake.config = {}
        with patch.object(cfg, "get_ha_settings", return_value={"ha_token": ""}), patch.object(
            cfg, "get_api_settings", return_value={"gemini_api_key": "gemini-key"}
        ):
            self.assertTrue(main.ViperDashboard.should_auto_open_setup_wizard(fake))

    def test_home_assistant_setup_prefills_env_on_real_first_run(self):
        class FakeDialog:
            captured = {}

            def __init__(self, parent, *, use_env_prefill=True):
                FakeDialog.captured["use_env_prefill"] = use_env_prefill

            def Show(self):
                FakeDialog.captured["shown"] = True

            def force_initial_focus(self):
                FakeDialog.captured["focused"] = True

            def Destroy(self):
                return None

        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.first_run = True
        fake.clean_first_run_test = False
        fake.config = {}
        fake._ha_setup_dialog = None
        fake._setup_wizard_dialog = None
        fake._ha_server_assistant_dialog = None
        fake.restore_main_window_focus = lambda: None
        fake._close_setup_surfaces = lambda keep=None: None
        fake._is_live_window = lambda window: False
        fake._log_setup_focus_snapshot = lambda context: None
        with patch.object(main, "HomeAssistantSetupDialog", FakeDialog):
            with patch.object(main.wx, "CallAfter"), patch.object(main.wx, "CallLater"):
                main.ViperDashboard.show_home_assistant_setup(fake)
        self.assertTrue(FakeDialog.captured["use_env_prefill"])
        self.assertTrue(FakeDialog.captured["shown"])

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

    def test_ice_maker_status_uses_switch_helper_and_counter_entities(self):
        fake = FakeDashboard()
        fake.config["ice_maker_switch_entity"] = "switch.refrigerator_cubed_ice"
        fake.config["ice_maker_keep_on_entity"] = "input_boolean.keep_ice_maker_on"
        fake.config["ice_maker_counter_entity"] = "counter.ice_usage_counter"

        states = {
            "switch.refrigerator_cubed_ice": {"state": "on"},
            "input_boolean.keep_ice_maker_on": {"state": "on"},
            "counter.ice_usage_counter": {"state": "4"},
        }

        def fake_state(entity_id, timeout=5):
            return {"ok": True, "exists": True, "entity_id": entity_id, "entity": states[entity_id]}

        fake._get_ha_entity_state = fake_state
        fake._configured_ice_maker_entities = lambda: main.ViperDashboard._configured_ice_maker_entities(fake)
        fake._format_ice_maker_status = lambda summary, counter_text, entities: main.ViperDashboard._format_ice_maker_status(
            fake,
            summary,
            counter_text,
            entities,
        )
        status = main.ViperDashboard.get_ice_maker_status(fake)

        self.assertTrue(status["is_on"])
        self.assertEqual(status["button_label"], "Turn Ice Maker Off")
        self.assertEqual(status["counter_text"], "4")
        self.assertIn("Ice usage counter: 4", status["message"])

    def test_fridge_tab_and_remote_use_single_ice_maker_toggle(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        template = (root / "templates" / "remote.html").read_text(encoding="utf-8")

        self.assertIn("self.btn_ice_toggle = wx.Button", main_text)
        self.assertIn("self.ice_maker_status_txt = AccessibleStatusText", main_text)
        self.assertIn("def on_ice_maker_toggle", main_text)
        self.assertIn("ice_maker_counter_entity", main_text)
        self.assertNotIn("self.btn_ice_on =", main_text)
        self.assertNotIn("self.btn_ice_off =", main_text)
        self.assertNotIn("self.ice_maker_status_txt = wx.TextCtrl", main_text)
        self.assertIn("web_ice_maker_toggle", template)
        self.assertIn("ice_maker.get('counter_text'", template)
        self.assertNotIn("url_for('web_ice_maker_on')", template)
        self.assertNotIn("url_for('web_ice_maker_off')", template)

    def test_short_statuses_use_accessible_static_text_not_read_only_edit_boxes(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        main_dashboard = main_text.split("class ViperDashboard", 1)[1]

        self.assertIn("class AccessibleStatusText(wx.StaticText):", main_text)
        expected_statuses = [
            "self.status_display = AccessibleStatusText",
            "self.video_analysis_status_txt = AccessibleStatusText",
            "self.doorbell_summary_txt = AccessibleStatusText",
            "self.prompt_status_txt = AccessibleStatusText",
            "self.setup_next_action_txt = AccessibleStatusText",
            "self.vacuum_status_txt = AccessibleStatusText",
            "self.vacuum_room_status_txt = AccessibleStatusText",
            "self.ice_maker_status_txt = AccessibleStatusText",
        ]
        missing = [snippet for snippet in expected_statuses if snippet not in main_dashboard]
        self.assertEqual(missing, [], f"Short statuses should be static text: {missing}")

        forbidden_short_status_edit_boxes = [
            "self.status_display = wx.TextCtrl",
            "self.video_analysis_status_txt = wx.TextCtrl",
            "self.doorbell_summary_txt = wx.TextCtrl",
            "self.prompt_status_txt = wx.TextCtrl",
            "self.setup_next_action_txt = wx.TextCtrl",
            "self.vacuum_status_txt = wx.TextCtrl",
            "self.vacuum_room_status_txt = wx.TextCtrl",
            "self.ice_maker_status_txt = wx.TextCtrl",
        ]
        offenders = [snippet for snippet in forbidden_short_status_edit_boxes if snippet in main_dashboard]
        self.assertEqual(offenders, [], f"Short statuses should not be read-only edit boxes: {offenders}")

    def test_runtime_settings_groups_product_areas_without_losing_legacy_keys(self):
        settings = cfg.get_runtime_settings(
            {
                "ha_ip": "192.168.4.50",
                "ha_token": "plain-test-token",
                "rtsp_front": "rtsp://ha/front",
                "rtsp_back": "rtsp://ha/back",
                "doorbell_triggers": {
                    "front": {"trigger_entity_id": "event.front_door_ding"},
                    "back": {"trigger_entity_id": "event.back_door_ding"},
                },
                "speakers": {
                    "Kitchen": {"id": "media_player.kitchen", "type": "ha", "enabled": True, "doorbell": True, "utilities": False},
                    "Office": {"id": "media_player.office", "type": "ha", "enabled": False},
                },
                "broadcast_channels": {"fridge_open": {"mode": "sound only", "chime": "ding.mp3"}},
                "vacuum_rooms": {"vacuum.cinderella": [{"name": "Kitchen", "segment": "7"}]},
            },
            include_env=False,
        )

        self.assertEqual(settings["home_assistant"]["ha_ip"], "192.168.4.50")
        self.assertEqual(settings["doorbell"]["configured_rtsp_front"], "rtsp://ha/front")
        self.assertEqual(settings["doorbell"]["front_trigger_entity_id"], "event.front_door_ding")
        self.assertEqual(settings["speakers"]["speaker_count"], 2)
        self.assertEqual(settings["speakers"]["enabled_count"], 1)
        self.assertEqual(settings["speakers"]["routes"]["doorbell"], ["Kitchen"])
        self.assertEqual(settings["speakers"]["routes"]["utilities"], [])
        self.assertEqual(settings["fridge"]["fridge_open"]["mode"], "chime")
        self.assertEqual(settings["vacuum"]["rooms"]["vacuum.cinderella"][0]["segment"], 7)

    def test_setup_checklist_requires_real_enabled_speaker_routes(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config(
            {
                "ha_ip": "192.168.4.49",
                "ha_token": "token",
                "rtsp_front": "rtsp://front",
                "rtsp_back": "rtsp://back",
                "doorbell_triggers": {
                    "front": {"trigger_entity_id": "binary_sensor.front"},
                    "back": {"trigger_entity_id": "binary_sensor.back"},
                },
                "speakers": {
                    "Kitchen": {
                        "id": "media_player.kitchen",
                        "type": "ha",
                        "enabled": False,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    }
                },
            }
        )
        fake.ha_listener = type("Listener", (), {"status": lambda self: {"connected": True}})()

        summary = main.ViperDashboard.build_setup_checklist_summary(fake)

        self.assertIn("Speaker routes: Needs setup", summary)
        self.assertIn("Enabled routes: doorbell 0, utilities 0, fridge/freezer 0", summary)
        self.assertIn("Press Choose Alert Speakers", summary)

    def test_audio_settings_exposes_effective_category_tts_and_chimes(self):
        settings = cfg.get_audio_settings(
            {
                "front_chime": "front.wav",
                "back_chime": "back.wav",
                "tts_defaults": {"engine": "edge", "edge_voice": "en-US-GuyNeural", "speed": "normal"},
                "tts_alerts": {
                    "doorbell": {"use_defaults": True, "engine": "gemini", "speed": "fast"},
                    "utilities": {"use_defaults": False, "engine": "google", "speed": "relaxed"},
                },
            },
            include_env=False,
        )

        self.assertEqual(settings["front_chime"], "front.wav")
        self.assertEqual(settings["back_chime"], "back.wav")
        self.assertEqual(settings["effective_tts_alerts"]["doorbell"]["engine"], "edge")
        self.assertEqual(settings["effective_tts_alerts"]["doorbell"]["speed"], "fast")
        self.assertEqual(settings["effective_tts_alerts"]["utilities"]["engine"], "google")

    def test_audio_dispatch_skips_network_tts_when_no_routed_speakers(self):
        config = cfg.validate_and_normalize_config(
            {
                "tts_engine": "Gemini TTS",
                "speakers": {
                    "Kitchen": {
                        "id": "media_player.kitchen",
                        "type": "ha",
                        "enabled": False,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    }
                },
            }
        )

        with patch.object(audio.cfg, "load_config", return_value=config), patch.object(
            audio, "speak_hd_pc"
        ), patch.object(audio, "_generate_network_tts_file") as gen_tts:
            result = audio._execute_announce_all("Test speech", context={"channel": "utilities"})

        self.assertTrue(result["no_targets"])
        gen_tts.assert_not_called()

    def test_release_audit_fails_zero_speaker_routes(self):
        audit = release_audit.Audit(emit=False)
        config = cfg.validate_and_normalize_config(
            {
                "speakers": {
                    "Kitchen": {
                        "id": "media_player.kitchen",
                        "type": "ha",
                        "enabled": False,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    }
                }
            }
        )

        release_audit._check_speakers(audit, config)

        self.assertTrue(any("No network playback targets for doorbell" in item for item in audit.failures))
        self.assertTrue(any("all are disabled" in item for item in audit.failures))

    def test_release_audit_notes_disabled_speakers_without_warning_when_routes_work(self):
        audit = release_audit.Audit(emit=False)
        config = cfg.validate_and_normalize_config(
            {
                "speakers": {
                    "Kitchen": {
                        "id": "media_player.kitchen",
                        "type": "ha",
                        "enabled": True,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    },
                    "Office": {
                        "id": "media_player.office",
                        "type": "ha",
                        "enabled": False,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    },
                }
            }
        )

        release_audit._check_speakers(audit, config)

        self.assertFalse(audit.failures)
        self.assertFalse(audit.warnings)
        self.assertTrue(any("NOTE: Disabled saved speakers" in line for line in audit.lines))

    def test_release_audit_simulates_doorbell_routes(self):
        audit = release_audit.Audit(emit=False)
        config = cfg.validate_and_normalize_config(
            {
                "doorbell_triggers": {
                    "front": {
                        "enabled": True,
                        "source": "ha_state",
                        "trigger_entity_id": "binary_sensor.front_door_ding",
                        "active_states": ["on"],
                        "rtsp_url": "rtsp://camera/front",
                    }
                }
            }
        )

        release_audit._check_doorbells(audit, config, None)

        self.assertFalse(audit.failures)
        self.assertTrue(any("front synthetic state change routes" in line for line in audit.lines))

    def _doorbell_audio_test_config(self):
        return cfg.validate_and_normalize_config(
            {
                "enable_alexa": True,
                "speakers": {
                    "Door HA": {
                        "type": "ha",
                        "id": "media_player.door_ha",
                        "enabled": True,
                        "doorbell": True,
                        "utilities": False,
                        "fridge": False,
                    },
                    "Utility HA": {
                        "type": "ha",
                        "id": "media_player.utility_ha",
                        "enabled": True,
                        "doorbell": False,
                        "utilities": True,
                        "fridge": False,
                    },
                    "Door Sonos": {
                        "type": "sonos",
                        "id": "192.168.1.20",
                        "enabled": True,
                        "doorbell": True,
                        "utilities": False,
                        "fridge": False,
                    },
                    "Disabled Sonos": {
                        "type": "sonos",
                        "id": "192.168.1.21",
                        "enabled": False,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    },
                    "Door Alexa": {
                        "type": "alexa",
                        "id": "media_player.door_echo",
                        "enabled": True,
                        "doorbell": True,
                        "utilities": False,
                        "fridge": False,
                    },
                },
            }
        )

    def _global_mute_test_config(self):
        config = self._doorbell_audio_test_config()
        config["global_mute"] = True
        config["speakers"]["Manual HA"] = {
            "type": "ha",
            "id": "media_player.manual_ha",
            "enabled": True,
            "doorbell": True,
            "utilities": True,
            "fridge": True,
        }
        return config

    def test_global_mute_blocks_direct_audio_paths(self):
        config = self._global_mute_test_config()
        calls = []

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio.threading, "Thread", self.ImmediateThread), \
             patch.object(audio, "prep_sonos_speakers", side_effect=lambda *args, **kwargs: calls.append(("prep_sonos", args, kwargs))), \
             patch.object(audio.soco, "SoCo", side_effect=lambda ip: calls.append(("soco", ip)) or f"sonos:{ip}"), \
             patch.object(audio, "_safe_sonos_play", side_effect=lambda *args: calls.append(("sonos", args))), \
             patch.object(audio, "_safe_ha_play", side_effect=lambda *args: calls.append(("ha", args))), \
             patch.object(audio, "_safe_alexa_play", side_effect=lambda *args: calls.append(("alexa", args))), \
             patch.object(audio, "_generate_network_tts_file", side_effect=lambda *args, **kwargs: calls.append(("tts", args, kwargs)) or "test.mp3"), \
             patch.object(audio, "_dispatch_to_sonos", side_effect=lambda *args, **kwargs: calls.append(("dispatch_sonos", args, kwargs))):
            audio.play_broadcast_chime("", "fridge_open")
            audio.test_specific_chime("", "front")
            audio.sonos_instant_chime("front door")
            audio.announce_specific_speaker("ha", "media_player.manual_ha", "Muted speaker test")
            audio.sonos_speak_verdict("Muted verdict")

        self.assertEqual(calls, [])

    def test_global_mute_blocks_notification_queue_and_dispatch(self):
        config = self._global_mute_test_config()
        calls = []

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio, "announce_all", side_effect=lambda *args, **kwargs: calls.append(("announce_all", args, kwargs))), \
             patch.object(audio.threading, "Thread", self.ImmediateThread):
            audio.play_notification("manual", "Muted broadcast")

        self.assertEqual(calls, [])

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio.network_speech_queue, "put", side_effect=lambda *args, **kwargs: calls.append(("queue_put", args, kwargs))), \
             patch.object(audio, "_execute_announce_all", side_effect=lambda *args, **kwargs: calls.append(("execute", args, kwargs))), \
             patch.object(audio.threading, "Thread", self.ImmediateThread):
            audio.announce_all("Muted direct", urgent=True, context={"channel": "doorbell"})

        self.assertEqual(calls, [])

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio, "speak_hd_pc", side_effect=lambda *args: calls.append(("pc", args))), \
             patch.object(audio, "_generate_network_tts_file", side_effect=lambda *args, **kwargs: calls.append(("tts", args, kwargs))):
            result = audio._execute_announce_all("Muted dispatch", context={"channel": "utilities"})

        self.assertTrue(result["muted"])
        self.assertEqual(calls, [])

    def test_doorbell_test_chime_uses_same_speaker_routing_as_live_doorbell(self):
        calls = []
        config = self._doorbell_audio_test_config()

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio, "prep_sonos_speakers", side_effect=lambda target_ips=None, **kwargs: [f"sonos:{ip}" for ip in (target_ips or [])]), \
             patch.object(audio.threading, "Thread", self.ImmediateThread), \
             patch.object(audio, "_safe_sonos_play", side_effect=lambda speaker, url, tag: calls.append(("sonos", speaker, tag))), \
             patch.object(audio, "_safe_ha_play", side_effect=lambda entity, url, headers: calls.append(("ha", entity))), \
             patch.object(audio, "_safe_alexa_play", side_effect=lambda entity, url, headers: calls.append(("alexa", entity))):
            audio.test_specific_chime("", "front")

        self.assertIn(("ha", "media_player.door_ha"), calls)
        self.assertNotIn(("ha", "media_player.utility_ha"), calls)
        self.assertIn(("sonos", "sonos:192.168.1.20", "SONOS CHIME"), calls)
        self.assertFalse(any(call[0] == "sonos" and "192.168.1.21" in call[1] for call in calls))
        self.assertIn(("alexa", "media_player.door_echo"), calls)

    def test_live_doorbell_chime_reaches_home_assistant_speakers_too(self):
        calls = []
        config = self._doorbell_audio_test_config()

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio, "prep_sonos_speakers", side_effect=lambda target_ips=None, **kwargs: [f"sonos:{ip}" for ip in (target_ips or [])]), \
             patch.object(audio.threading, "Thread", self.ImmediateThread), \
             patch.object(audio, "_safe_sonos_play", side_effect=lambda speaker, url, tag: calls.append(("sonos", speaker, tag))), \
             patch.object(audio, "_safe_ha_play", side_effect=lambda entity, url, headers: calls.append(("ha", entity))), \
             patch.object(audio, "_safe_alexa_play", side_effect=lambda entity, url, headers: calls.append(("alexa", entity))):
            audio.sonos_instant_chime("front door")

        self.assertIn(("ha", "media_player.door_ha"), calls)
        self.assertNotIn(("ha", "media_player.utility_ha"), calls)
        self.assertIn(("sonos", "sonos:192.168.1.20", "SONOS INSTANT CHIME"), calls)
        self.assertFalse(any(call[0] == "sonos" and "192.168.1.21" in call[1] for call in calls))
        self.assertIn(("alexa", "media_player.door_echo"), calls)

    def test_fridge_broadcast_chime_uses_fridge_routing_for_all_speaker_types(self):
        calls = []
        config = cfg.validate_and_normalize_config(
            {
                "enable_alexa": True,
                "speakers": {
                    "Fridge HA": {
                        "type": "ha",
                        "id": "media_player.fridge_ha",
                        "enabled": True,
                        "doorbell": False,
                        "utilities": False,
                        "fridge": True,
                    },
                    "Doorbell HA": {
                        "type": "ha",
                        "id": "media_player.doorbell_ha",
                        "enabled": True,
                        "doorbell": True,
                        "utilities": False,
                        "fridge": False,
                    },
                    "Fridge Sonos": {
                        "type": "sonos",
                        "id": "192.168.1.30",
                        "enabled": True,
                        "doorbell": False,
                        "utilities": False,
                        "fridge": True,
                    },
                    "Fridge Alexa": {
                        "type": "alexa",
                        "id": "media_player.fridge_echo",
                        "enabled": True,
                        "doorbell": False,
                        "utilities": False,
                        "fridge": True,
                    },
                },
            }
        )

        with patch.object(audio.cfg, "load_config", return_value=config), \
             patch.object(audio.soco, "SoCo", side_effect=lambda ip: f"sonos:{ip}"), \
             patch.object(audio.threading, "Thread", self.ImmediateThread), \
             patch.object(audio, "_safe_sonos_play", side_effect=lambda speaker, url, tag: calls.append(("sonos", speaker, tag))), \
             patch.object(audio, "_safe_ha_play", side_effect=lambda entity, url, headers: calls.append(("ha", entity))), \
             patch.object(audio, "_safe_alexa_play", side_effect=lambda entity, url, headers: calls.append(("alexa", entity))):
            audio.play_broadcast_chime("", "fridge_open")

        self.assertIn(("ha", "media_player.fridge_ha"), calls)
        self.assertNotIn(("ha", "media_player.doorbell_ha"), calls)
        self.assertIn(("sonos", "sonos:192.168.1.30", "SONOS BROADCAST CHIME"), calls)
        self.assertIn(("alexa", "media_player.fridge_echo"), calls)

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
        self.assertIn("cinderella Mop intensity", body)
        self.assertNotIn("cinderella Dock Empty mode", body)
        self.assertIn("Turn on cinderella Dock Child lock", body)
        self.assertNotIn("Turn off cinderella Dock Child lock", body)
        self.assertIn("Kitchen", body)
        self.assertNotIn("cinderella Full Cleaning", body)

    def test_remote_page_renders_diagnostics_controls(self):
        main.dash_app = FakeDashboard()
        main.dash_app.build_setup_next_action_summary = lambda: "Core setup is ready."
        main.dash_app.build_setup_checklist_summary = lambda: "Setup Status\n\nOne next action: Run Test Everything."

        with patch.object(main.discovery, "get_ha_states", return_value={"ok": True, "states": _sample_states(), "entity_count": 8}):
            response = self.client.get("/remote")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Diagnostics", body)
        self.assertIn("Health status:", body)
        self.assertIn("Create Support Bundle", body)
        self.assertIn("Setup Status", body)
        self.assertIn("Core setup is ready.", body)
        self.assertIn("Run Safe Setup Smoke Test", body)
        self.assertIn("Restore Optional Items", body)

    def test_remote_setup_smoke_route_reports_pass_fix(self):
        main.dash_app = FakeDashboard()
        main.dash_app.build_setup_next_action_summary = lambda: "Core setup is ready."
        main.dash_app.build_setup_checklist_summary = lambda: "Setup Status"
        main.dash_app._collect_safe_smoke_results = lambda: [("Config file", True, "ok", "")]
        main.dash_app._format_safe_smoke_report = lambda results: "Smoke Test: PASS"

        response = self.client.post("/remote/setup/smoke", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.dash_app.last_remote_setup_smoke_report, "Smoke Test: PASS")
        self.assertIn("Smoke Test: PASS", response.get_data(as_text=True))

    def test_remote_restore_optional_setup_route_clears_skips(self):
        main.dash_app = FakeDashboard()
        main.dash_app.config["setup_skips"] = {"gemini": True, "pushover": False, "fridge": True, "vacuum": False}
        main.dash_app._setup_skip_state = lambda: main.ViperDashboard._setup_skip_state(main.dash_app)
        main.dash_app.record_setup_event = lambda *args, **kwargs: None
        main.dash_app.build_setup_next_action_summary = lambda: "Core setup is ready."
        main.dash_app.build_setup_checklist_summary = lambda: "Setup Status"

        with patch.object(cfg, "save_config") as save_config:
            response = self.client.post("/remote/setup/restore_optional", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        save_config.assert_called_once()
        self.assertFalse(any(main.dash_app.config["setup_skips"].values()))

    def test_desktop_diagnostics_tab_has_health_summary_controls(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        tab_start = main_text.index("def setup_diagnostics_tab")
        tab_end = main_text.index("def setup_utils_tab", tab_start)
        tab_text = main_text[tab_start:tab_end]

        self.assertIn('label="Health Summary"', tab_text)
        self.assertIn("self.diagnostics_health_txt", tab_text)
        self.assertIn('label="Refresh Health Summary"', tab_text)
        self.assertIn("self.refresh_health_summary()", tab_text)
        self.assertIn('label="Safe Smoke Test"', tab_text)
        self.assertIn('label="Run Safe Smoke Test"', tab_text)
        self.assertIn('label="Test Front Camera Frame"', tab_text)
        self.assertIn('label="Test Manual Broadcast"', tab_text)

    def test_safe_smoke_report_points_to_first_broken_item(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        results = [
            ("Config file", True, "ok", "save settings"),
            ("HA API", False, "timeout", "Check Home Assistant address, token, and whether HA Core is running."),
            ("Support bundle", True, "ok", "check folder"),
        ]

        text = main.ViperDashboard._format_safe_smoke_report(fake, results)

        self.assertIn("Smoke Test: NEEDS ATTENTION", text)
        self.assertIn("FIX: HA API. timeout. Next: Check Home Assistant address", text)
        self.assertIn("Most important next step:", text)

    def test_safe_smoke_report_pass_invites_optional_hardware_tests(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        results = [
            ("Config file", True, "ok", ""),
            ("HA API", True, "ok", ""),
        ]

        text = main.ViperDashboard._format_safe_smoke_report(fake, results)

        self.assertIn("Smoke Test: PASS", text)
        self.assertIn("Optional next step: use the camera/audio buttons below", text)

    def test_diagnostics_action_buttons_call_expected_routes(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")

        self.assertIn("def on_run_safe_smoke_test", main_text)
        self.assertIn("def on_test_diagnostics_camera", main_text)
        self.assertIn("def on_test_diagnostics_manual_broadcast", main_text)
        self.assertIn("def on_test_diagnostics_chime", main_text)
        self.assertIn('_dispatch_broadcast_message(message, channel="manual")', main_text)
        self.assertIn("audio.play_broadcast_chime(chime, channel)", main_text)

    def test_remote_page_renders_doorbell_video_controls(self):
        main.dash_app = FakeDashboard()
        main.dash_app.config["doorbell_video_analysis"]["mode"] = "smart"

        with patch.object(main.discovery, "get_ha_states", return_value={"ok": True, "states": _sample_states(), "entity_count": 8}):
            response = self.client.get("/remote")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Doorbell video analysis", body)
        self.assertIn("Smart mode: fast still image first", body)
        self.assertIn("Analyze Front Camera Video Now", body)
        self.assertIn("manual_clip_seconds", body)

    def test_main_window_has_ai_descriptions_tab_without_profile_language(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        prompt_tab_start = main_text.index("def setup_prompt_editor_tab")
        prompt_tab_end = main_text.index("def _video_prompt_name_for_mode", prompt_tab_start)
        prompt_tab_text = main_text[prompt_tab_start:prompt_tab_end]
        self.assertIn('self.notebook.AddPage(self.tab_prompts, "AI Descriptions")', main_text)
        self.assertIn("Front door alert", main_text)
        self.assertIn("Back door alert", main_text)
        self.assertIn("Manual outside video check", main_text)
        self.assertIn("Save AI Description Settings", prompt_tab_text)
        self.assertIn("Reset AI Descriptions To Recommended Settings", prompt_tab_text)
        self.assertNotIn("profile", prompt_tab_text.lower())
        self.assertNotIn("default prompt", prompt_tab_text.lower())
        self.assertNotIn("prompt profile to edit", prompt_tab_text.lower())
        self.assertNotIn("Video Prompt Assignment", main_text)
        self.assertNotIn("Video prompt profile to edit:", main_text)

    def test_remote_doorbell_video_settings_are_saved_and_clamped(self):
        main.dash_app = FakeDashboard()

        response = self.client.post(
            "/remote/doorbell/video_settings",
            data={"video_mode": "detailed", "manual_clip_seconds": "99"},
        )

        self.assertEqual(response.status_code, 302)
        settings = main.dash_app.config["doorbell_video_analysis"]
        self.assertTrue(main.dash_app.saved)
        self.assertEqual(settings["mode"], "detailed")
        self.assertEqual(settings["manual_clip_seconds"], settings["max_manual_clip_seconds"])

    def test_remote_manual_video_analysis_submits_background_request(self):
        main.dash_app = FakeDashboard()

        with patch.object(main, "safe_submit", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs) or object()):
            response = self.client.post(
                "/remote/doorbell/video_analyze/back",
                data={"manual_clip_seconds": "4"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(main.dash_app.video_analysis_requests, [("back", 4, "remote web interface")])

    def test_desktop_video_status_summary_is_screen_reader_clear(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config(
            {"doorbell_video_analysis": {"mode": "fast", "manual_clip_seconds": 4}}
        )
        fake.last_video_analysis = {
            "front": {
                "description": "The video shows a",
                "source": "desktop app",
                "elapsed": 10.0,
                "incomplete": True,
            }
        }

        text = main.ViperDashboard._video_analysis_summary_text(fake)

        self.assertIn("Mode: Fast.", text)
        self.assertIn("Still image only", text)
        self.assertIn("Smart rules are inactive right now.", text)
        self.assertIn("Manual Analyze Camera Video Now buttons upload 4 seconds.", text)
        self.assertIn("Gemini returned an incomplete answer: The video shows a", text)
        self.assertNotIn("Current mode: Fast mode", text)
        self.assertNotIn("Smart mode parameters", text)

    def test_remote_diagnostics_endpoint_returns_json(self):
        main.dash_app = FakeDashboard()

        response = self.client.get("/remote/diagnostics?format=json")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["app"]["version"], "1.2.3")
        self.assertIn("ffmpeg", payload)

    def test_ring_mqtt_installer_helpers_find_addon_slugs(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        addons = [
            {"slug": "core_mosquitto", "name": "Mosquitto broker"},
            {"slug": "abcd_ring_mqtt", "name": "Ring-MQTT with Video Streaming"},
        ]
        self.assertEqual(
            main.HomeAssistantSetupDialog._find_addon_slug(
                fake,
                addons,
                exact_slugs=("core_mosquitto",),
                text_tokens=("mosquitto",),
            ),
            "core_mosquitto",
        )
        self.assertEqual(
            main.HomeAssistantSetupDialog._find_addon_slug(fake, addons, text_tokens=("ring", "mqtt")),
            "abcd_ring_mqtt",
        )
        ring_addons = [
            {"slug": "a0d7b954_mqtt-io", "name": "MQTT IO", "description": "Expose GPIO modules via MQTT"},
            {"slug": "core_matter_server", "name": "Matter Server", "description": "Pairing devices for Matter support"},
            {
                "slug": "ring_mqtt",
                "name": "Ring-MQTT with Video Streaming",
                "description": "Integrate Ring Devices into Home Assistant via MQTT and RTSP",
                "repository": "03cabcc9",
            },
        ]
        self.assertEqual(
            main.HomeAssistantSetupDialog._find_ring_mqtt_slug(fake, ring_addons),
            "03cabcc9_ring_mqtt",
        )
        self.assertEqual(
            main.HomeAssistantSetupDialog._find_ring_mqtt_slug(
                fake,
                [
                    {"slug": "a0d7b954_mqtt-io", "name": "MQTT IO", "description": "Expose GPIO modules via MQTT"},
                    {"slug": "core_matter_server", "name": "Matter Server", "description": "Pairing devices for Matter support"},
                ],
            ),
            "",
        )
        self.assertTrue(main.HomeAssistantSetupDialog._is_ring_mqtt_slug(fake, "ring_mqtt"))
        self.assertTrue(main.HomeAssistantSetupDialog._is_ring_mqtt_slug(fake, "03cabcc9_ring_mqtt"))
        self.assertFalse(main.HomeAssistantSetupDialog._is_ring_mqtt_slug(fake, "core_matter_server"))

    def test_ring_mqtt_already_installed_routes_to_login_slug(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        captured = []
        store_addons = [
            {"slug": "core_mosquitto", "name": "Mosquitto broker", "installed": True},
            {"slug": "03cabcc9_ring_mqtt", "name": "Ring-MQTT with Video Streaming", "repository": "03cabcc9", "installed": True},
        ]
        fake._get_installed_addons = lambda settings: store_addons
        fake._ensure_addon_started = lambda settings, slug: True
        configured = []
        fake._configure_ring_mqtt_rtsp_port_and_restart = lambda settings: configured.append(settings) or True

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/store/addons":
                return {"data": {"addons": store_addons}}
            return {"data": {}}

        fake._hassio_request = fake_hassio
        fake._finish_install_ring_mqtt_requirements = lambda result: captured.append(result)
        with patch.object(main.wx, "CallAfter", side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)):
            main.HomeAssistantSetupDialog._run_install_ring_mqtt_requirements(
                fake,
                {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            )

        self.assertEqual(captured[0]["ring_slug"], "03cabcc9_ring_mqtt")
        self.assertIn("already installed. Opening Ring login now", captured[0]["message"])
        self.assertEqual(len(configured), 1)
        self.assertIn("Ring-MQTT RTSP port 8554: configured.", captured[0]["message"])

    def test_ring_mqtt_fresh_install_routes_to_login_slug(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        captured = []
        requests_seen = []
        store_addons = [
            {"slug": "core_mosquitto", "name": "Mosquitto broker", "installed": False},
            {"slug": "03cabcc9_ring_mqtt", "name": "Ring-MQTT with Video Streaming", "repository": "03cabcc9", "installed": False},
        ]
        fake._get_installed_addons = lambda settings: []
        fake._ensure_addon_started = lambda settings, slug: True
        configured = []
        fake._configure_ring_mqtt_rtsp_port_and_restart = lambda settings: configured.append(settings) or True

        def fake_hassio(settings, method, path, **kwargs):
            requests_seen.append((method, path))
            if path == "/store/addons":
                return {"data": {"addons": store_addons}}
            return {"data": {}}

        fake._hassio_request = fake_hassio
        fake._finish_install_ring_mqtt_requirements = lambda result: captured.append(result)
        with patch.object(main.wx, "CallAfter", side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)):
            main.HomeAssistantSetupDialog._run_install_ring_mqtt_requirements(
                fake,
                {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            )

        self.assertEqual(captured[0]["ring_slug"], "03cabcc9_ring_mqtt")
        self.assertIn(("POST", "/store/addons/03cabcc9_ring_mqtt/install"), requests_seen)
        self.assertEqual(len(configured), 1)

    def test_configure_ring_mqtt_rtsp_port_posts_network_mapping_and_restarts(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        calls = []

        def fake_hassio(settings, method, path, **kwargs):
            calls.append((method, path, kwargs.get("payload")))
            return {"data": {}}

        fake._hassio_request = fake_hassio
        fake._ensure_addon_started = lambda settings, slug: calls.append(("ENSURE", slug, None)) or True

        main.HomeAssistantSetupDialog._configure_ring_mqtt_rtsp_port_and_restart(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
        )

        self.assertIn(("POST", "/addons/03cabcc9_ring_mqtt/options", {"network": {"8554/tcp": 8554}}), calls)
        self.assertIn(("POST", "/addons/03cabcc9_ring_mqtt/restart", None), calls)
        self.assertIn(("ENSURE", "03cabcc9_ring_mqtt", None), calls)

    def test_ring_mqtt_port_configuration_failure_reports_manual_fallback(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        captured = []
        store_addons = [
            {"slug": "core_mosquitto", "name": "Mosquitto broker", "installed": True},
            {"slug": "03cabcc9_ring_mqtt", "name": "Ring-MQTT with Video Streaming", "repository": "03cabcc9", "installed": True},
        ]
        fake._get_installed_addons = lambda settings: store_addons
        fake._ensure_addon_started = lambda settings, slug: True
        fake._configure_ring_mqtt_rtsp_port_and_restart = lambda settings: (_ for _ in ()).throw(RuntimeError("not allowed"))

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/store/addons":
                return {"data": {"addons": store_addons}}
            return {"data": {}}

        fake._hassio_request = fake_hassio
        fake._finish_install_ring_mqtt_requirements = lambda result: captured.append(result)
        with patch.object(main.wx, "CallAfter", side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)):
            main.HomeAssistantSetupDialog._run_install_ring_mqtt_requirements(
                fake,
                {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            )

        self.assertIn("could not be configured automatically", captured[0]["message"])
        self.assertIn("set network port 8554 for 8554/tcp", captured[0]["message"])

    def test_resolve_addon_login_url_uses_frontend_app_route_for_ingress(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/addons/abcd_ring_mqtt/info":
                return {"data": {"ingress": True, "ingress_url": "/api/hassio_ingress/oldtoken"}}
            return {"data": {}}

        fake._hassio_request = fake_hassio

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            "abcd_ring_mqtt",
        )

        self.assertEqual(url, "http://192.168.4.49:8123/app/abcd_ring_mqtt")

    def test_resolve_addon_login_url_uses_frontend_app_route_for_basic_ingress(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/addons/abcd_ring_mqtt/info":
                return {"data": {"ingress": True}}
            return {"data": {}}

        fake._hassio_request = fake_hassio

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            "abcd_ring_mqtt",
        )

        self.assertEqual(url, "http://192.168.4.49:8123/app/abcd_ring_mqtt")

    def test_create_ingress_session_uses_current_user_id_when_available(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        captured = []
        fake._ha_ws_command = lambda settings, command, **kwargs: {"id": "user123"} if command.get("type") == "auth/current_user" else {}

        def fake_hassio(settings, method, path, **kwargs):
            captured.append(kwargs.get("payload"))
            return {"data": {"session": "session123"}}

        fake._hassio_request = fake_hassio

        session = main.HomeAssistantSetupDialog._create_ingress_session(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
        )

        self.assertEqual(session, "session123")
        self.assertEqual(captured[0], {"user_id": "user123"})

    def test_resolve_addon_login_url_uses_frontend_app_route_with_ingress_entry(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/addons/abcd_ring_mqtt/info":
                return {"data": {"ingress": True, "ingress_entry": "/auth"}}
            return {"data": {}}

        fake._hassio_request = fake_hassio

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            "abcd_ring_mqtt",
        )

        self.assertEqual(url, "http://192.168.4.49:8123/app/abcd_ring_mqtt")

    def test_resolve_addon_login_url_ignores_direct_api_ingress_paths(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/addons/abcd_ring_mqtt/info":
                return {"data": {"ingress": True, "ingress_entry": "/api/hassio_ingress/oldtoken/login"}}
            return {"data": {}}

        fake._hassio_request = fake_hassio

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            "abcd_ring_mqtt",
        )

        self.assertEqual(url, "http://192.168.4.49:8123/app/abcd_ring_mqtt")
        self.assertNotIn("/api/hassio_ingress", url)

    def test_resolve_addon_login_url_refuses_repository_url(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        def fake_hassio(settings, method, path, **kwargs):
            if path == "/addons/03cabcc9_ring_mqtt/info":
                return {"data": {"url": "https://github.com/tsightler/ring-mqtt-ha-addon"}}
            if path == "/ingress/session":
                return {"data": {}}
            return {"data": {}}

        fake._hassio_request = fake_hassio

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            "ring_mqtt",
        )

        self.assertEqual(url, "http://192.168.4.49:8123/app/03cabcc9_ring_mqtt")
        self.assertNotIn("github.com", url)

    def test_resolve_addon_login_url_uses_webui(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        fake._hassio_request = lambda settings, method, path, **kwargs: {"data": {"webui": "[PROTO:ssl]://[HOST]:[PORT:55123]"}}

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            "some_other_addon",
        )

        self.assertEqual(url, "http://192.168.4.49:55123")

    def test_resolve_ring_mqtt_login_url_is_confirmed_full_slug_route(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        fake._hassio_request = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not query HA for Ring-MQTT login URL"))

        url = main.HomeAssistantSetupDialog._resolve_addon_login_url(
            fake,
            {"ha_ip": "192.168.4.50", "ha_port": "8123", "ha_token": "token"},
            "ring_mqtt",
        )

        self.assertEqual(url, "http://192.168.4.50:8123/app/03cabcc9_ring_mqtt")

    def test_ring_mqtt_log_scan_extracts_rtsp_streams(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        log_text = (
            '2026 ring-attr [Front Door] ring/abc123/camera/343ea489ad6c/info/state '
            '{"stream_Source":"rtsp://03cabcc9-ring-mqtt:8554/343ea489ad6c_live"}\n'
            '2026 ring-attr [Back door] ring/abc123/camera/343ea4745067/info/state '
            '{"stream_Source":"rtsp://03cabcc9-ring-mqtt:8554/343ea4745067_live"}'
        )

        class Response:
            status_code = 200
            text = log_text

        with patch.object(main.requests, "get", return_value=Response()) as get:
            result = main.HomeAssistantSetupDialog._run_find_ring_mqtt_log_streams(
                fake,
                {"ha_ip": "192.168.4.50", "ha_port": "8123", "ha_token": "token"},
                "192.168.4.50",
            )

        get.assert_called_once()
        self.assertEqual(len(result["streams"]), 2)
        self.assertEqual(result["streams"][0]["friendly_name"], "Front Door")
        self.assertEqual(result["streams"][0]["rtsp_url"], "rtsp://192.168.4.50:8554/343ea489ad6c_live")
        self.assertEqual(result["streams"][1]["rtsp_url"], "rtsp://192.168.4.50:8554/343ea4745067_live")

    def test_ring_mqtt_login_dialog_defaults_to_accessible_browser_guide(self):
        class ParentFrame(main.wx.Frame):
            def on_find_live_rtsp_streams(self, event):
                return None

        app = main.wx.App.Get() or main.wx.App(False)
        parent = ParentFrame(None)
        dlg = main.RingMqttLoginDialog(
            parent,
            "http://192.168.4.49:8123/app/03cabcc9_ring_mqtt",
            ha_login_url="http://192.168.4.49:8123/config/app/03cabcc9_ring_mqtt/info",
        )
        try:
            self.assertIsNone(dlg.webview)
            self.assertEqual(dlg.btn_ha_login.GetLabel(), "Open Ring-MQTT App Page In Browser")
            self.assertEqual(dlg.btn_ring_login.GetLabel(), "Try Direct Ring-MQTT Web UI In Browser")
            self.assertEqual(dlg.btn_try_embedded.GetLabel(), "Try Embedded Browser")
        finally:
            dlg.Destroy()
            parent.Destroy()

    def test_ring_mqtt_login_dialog_browser_buttons_open_expected_urls(self):
        class ParentFrame(main.wx.Frame):
            def on_find_live_rtsp_streams(self, event):
                return None

        app = main.wx.App.Get() or main.wx.App(False)
        parent = ParentFrame(None)
        dlg = main.RingMqttLoginDialog(
            parent,
            "http://192.168.4.49:8123/app/03cabcc9_ring_mqtt",
            ha_login_url="http://192.168.4.49:8123/config/app/03cabcc9_ring_mqtt/info",
        )
        try:
            with patch.object(main, "open_url") as browser_open:
                dlg.on_ha_login(None)
                dlg.on_ring_login(None)
            self.assertEqual(browser_open.call_args_list[0].args[0], "http://192.168.4.49:8123/config/app/03cabcc9_ring_mqtt/info")
            self.assertEqual(browser_open.call_args_list[1].args[0], "http://192.168.4.49:8123/app/03cabcc9_ring_mqtt")
        finally:
            dlg.Destroy()
            parent.Destroy()

    def test_ring_mqtt_login_dialog_auto_opens_home_assistant_page(self):
        class ParentFrame(main.wx.Frame):
            def on_find_live_rtsp_streams(self, event):
                return None

        app = main.wx.App.Get() or main.wx.App(False)
        parent = ParentFrame(None)
        with patch.object(main, "open_url", return_value=True) as open_url:
            dlg = main.RingMqttLoginDialog(
                parent,
                "http://192.168.4.49:8123/app/03cabcc9_ring_mqtt",
                ha_login_url="http://192.168.4.49:8123/config/app/03cabcc9_ring_mqtt/info",
            )
            try:
                dlg.open_initial_home_assistant_page()
            finally:
                dlg.Destroy()
                parent.Destroy()
        open_url.assert_called_with("http://192.168.4.49:8123/config/app/03cabcc9_ring_mqtt/info")

    def test_open_url_uses_startfile_fallback_when_webbrowser_fails(self):
        with patch.object(main.webbrowser, "open", return_value=False):
            with patch.object(main.os, "name", "nt"):
                with patch.object(main.os, "startfile") as startfile:
                    self.assertTrue(main.open_url("http://192.168.4.49:8123/"))
        startfile.assert_called_once_with("http://192.168.4.49:8123/")

    def test_open_ring_mqtt_login_uses_apps_page_not_legacy_addon_route(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        calls = []
        fake._settings = lambda: {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"}
        fake._ensure_addon_started = lambda settings, slug: True
        fake._resolve_addon_login_url = lambda settings, slug: main.HomeAssistantSetupDialog._absolute_ha_url(fake, settings, "/app/03cabcc9_ring_mqtt")
        fake._set_setup_status = lambda *args, **kwargs: None
        fake._after_ring_mqtt_login = lambda: None

        class FakeDialog:
            def __init__(self, parent, url, ha_login_url=""):
                calls.append((url, ha_login_url))

            def ShowModal(self):
                return main.wx.ID_CANCEL

            def Destroy(self):
                return None

        with patch.object(main, "RingMqttLoginDialog", FakeDialog):
            main.HomeAssistantSetupDialog._open_ring_mqtt_login(fake, "03cabcc9_ring_mqtt")

        self.assertEqual(calls[0][0], "http://192.168.4.49:8123/app/03cabcc9_ring_mqtt")
        self.assertEqual(calls[0][1], "http://192.168.4.49:8123/config/app/03cabcc9_ring_mqtt/info")
        self.assertNotIn("/hassio/addon/", calls[0][1])

    def test_open_ring_mqtt_login_refuses_matter_slug(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        messages = []
        fake._settings = lambda: {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"}
        fake._set_setup_status = lambda message, **kwargs: messages.append(message)

        with patch.object(main, "RingMqttLoginDialog") as dialog:
            main.HomeAssistantSetupDialog._open_ring_mqtt_login(fake, "core_matter_server")

        dialog.assert_not_called()
        self.assertIn("refused to open", messages[0].lower())

    def test_normalize_addon_webui_replaces_home_assistant_placeholders(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        url = main.HomeAssistantSetupDialog._normalize_addon_webui(
            fake,
            {"ha_ip": "192.168.4.49", "ha_port": "8123"},
            "[PROTO:ssl]://[HOST]:[PORT:8080]",
        )

        self.assertEqual(url, "http://192.168.4.49:8080")

    def test_supervisor_permission_check_reports_rejected_external_token(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        class FakeResponse:
            status_code = 403

            def raise_for_status(self):
                raise AssertionError("raise_for_status should not run for rejected tokens")

        with patch.object(main.requests, "request", return_value=FakeResponse()):
            with patch.object(
                main.HomeAssistantSetupDialog,
                "_hassio_ws_request",
                side_effect=RuntimeError("Home Assistant rejected this token for Supervisor add-on installation."),
            ):
                result = main.HomeAssistantSetupDialog._check_supervisor_install_permission(
                    fake,
                    {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "supervisor_token_rejected")
        self.assertIn("Supervisor add-on management rejected", result["message"])

    def test_hassio_request_falls_back_to_websocket_when_rest_rejects_token(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        class FakeResponse:
            status_code = 401

            def raise_for_status(self):
                raise AssertionError("raise_for_status should not run for rejected tokens")

        with patch.object(main.requests, "request", return_value=FakeResponse()):
            with patch.object(
                main.HomeAssistantSetupDialog,
                "_hassio_ws_request",
                return_value={"healthy": True},
            ) as ws_request:
                result = main.HomeAssistantSetupDialog._hassio_request(
                    fake,
                    {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
                    "GET",
                    "/supervisor/info",
                )

        self.assertEqual(result, {"healthy": True})
        ws_request.assert_called_once()

    def test_supervisor_permission_check_accepts_admin_capable_token(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"healthy": True}}

        with patch.object(main.requests, "request", return_value=FakeResponse()):
            result = main.HomeAssistantSetupDialog._check_supervisor_install_permission(
                fake,
                {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "ok")

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
        service, payload = main.dash_app.service_calls[-1]
        self.assertEqual(service, "vacuum/send_command")
        self.assertEqual(payload["entity_id"], "vacuum.cinderella")
        self.assertEqual(payload["command"], "app_segment_clean")
        self.assertEqual(payload["params"], [{"segments": [7, 1], "repeat": 3}])

    def test_web_room_clean_applies_selected_cleaning_mode_first(self):
        main.dash_app = FakeDashboard()
        main.dash_app._last_web_vacuum_controls = {"vacuum.cinderella": _sample_states()}
        response = self.client.post(
            "/remote/vacuum/clean_rooms",
            data={
                "vacuum_entity": "vacuum.cinderella",
                "segments": ["7"],
                "repeat": "1",
                "cleaning_mode": "vacuum_only",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            ("select/select_option", {"entity_id": "select.cinderella_cleaning_mode", "option": "vacuum_only"}),
            main.dash_app.service_calls,
        )
        self.assertEqual(main.dash_app.service_calls[-1][0], "vacuum/send_command")
        self.assertEqual(main.dash_app.config["vacuum_cleaning_mode"], "vacuum_only")

    def test_web_vacuum_context_includes_state_specific_actions_and_modes(self):
        main.dash_app = FakeDashboard()
        with main.app.test_request_context("/remote?vacuum_entity=vacuum.cinderella"):
            with patch.object(main.discovery, "get_ha_states", return_value={"ok": True, "states": _sample_states()}):
                context = main._build_web_vacuum_context()

        action_services = {action["service"] for action in context["actions"]}
        self.assertIn("vacuum/start", action_services)
        self.assertNotIn("vacuum/pause", action_services)
        self.assertEqual(context["cleaning_mode"], "vacuum_mop")
        self.assertIn({"value": "mop_only", "label": "Mop only"}, context["cleaning_modes"])

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

    def test_vacuum_room_selection_uses_real_checkboxes_for_screen_readers(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        vacuum_tab = main_text.split("def setup_vacuum_tab", 1)[1].split("def on_refresh_vacuum", 1)[0]
        room_refresh = main_text.split("def _finish_vacuum_room_refresh", 1)[1].split("def _sanitize_vacuum_rooms", 1)[0]

        self.assertIn("self.vacuum_room_scroll = wx.ScrolledWindow", vacuum_tab)
        self.assertIn("wx.CheckBox(self.vacuum_room_scroll", room_refresh)
        self.assertIn("_room_checkbox_label", room_refresh)
        self.assertIn("room ID", main_text)
        self.assertNotIn("wx.CheckListBox(self.tab_vacuum", vacuum_tab)

    def test_vacuum_dynamic_setting_buttons_include_target_names(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        dynamic = main_text.split("def _rebuild_vacuum_dynamic_controls", 1)[1].split("def _show_vacuum_setting", 1)[0]

        self.assertIn('btn_label = f"Apply {label}"', dynamic)
        self.assertIn('btn_label = f"Turn {\'on\' if turn_on else \'off\'} {label}"', dynamic)
        self.assertNotIn('btn_on_label = f"Turn on {label}"', dynamic)
        self.assertNotIn('btn_off_label = f"Turn off {label}"', dynamic)
        self.assertIn('btn_label = f"Set {label}"', dynamic)
        self.assertIn('btn_label = f"Press {label}"', dynamic)

    def test_flaky_roborock_dock_empty_mode_is_hidden_from_easy_settings(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        vacuum_text = Path("viper_vacuum.py").read_text(encoding="utf-8")

        self.assertFalse(main._web_show_vacuum_setting({"entity_id": "select.cinderella_dock_empty_mode"}))
        self.assertTrue(main._web_show_vacuum_setting({"entity_id": "select.cinderella_mop_intensity"}))
        self.assertTrue(vacuum.is_hidden_vacuum_setting_entity_id("select.cinderella_dock_empty_mode"))
        self.assertIn("HIDDEN_VACUUM_SETTING_SUFFIXES", main_text)
        self.assertIn('"_dock_empty_mode"', vacuum_text)
        self.assertIn("Home Assistant reports it but rejects write attempts", main_text)

    def test_vacuum_settings_use_slow_service_timeout_and_clear_timeout_message(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        compact = re.sub(r"\s+", " ", main_text)

        self.assertIn('"select/select_option", {"entity_id": entity_id, "option": option}, f"Set {entity_id} to {option}.", timeout=30', compact)
        self.assertIn('"number/set_value", {"entity_id": entity_id, "value": value}, f"Set {entity_id} to {value}.", timeout=30', compact)
        self.assertIn('except requests.exceptions.ReadTimeout:', main_text)
        self.assertIn("press Refresh vacuum controls", main_text)

    def test_vacuum_setting_refresh_restores_focus_to_pressed_action_button(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        dynamic = main_text.split("def _rebuild_vacuum_dynamic_controls", 1)[1].split("def _show_vacuum_setting", 1)[0]
        service_runner = main_text.split("def _run_ha_service_async", 1)[1].split("def setup_speed_tab", 1)[0]

        self.assertIn("self.vacuum_action_buttons = {}", main_text)
        self.assertIn("self._pending_vacuum_focus_entity_id = \"\"", main_text)
        self.assertIn("self.vacuum_action_buttons[entity_id] = btn", dynamic)
        self.assertIn("def _restore_pending_vacuum_focus", main_text)
        self.assertIn("def _focus_vacuum_action_button", main_text)
        self.assertIn("restore_focus_entity_id=entity_id", main_text)
        self.assertIn("self._pending_vacuum_focus_entity_id = restore_focus_entity_id", service_runner)
        self.assertIn("wx.CallAfter(self._focus_vacuum_action_button, button)", main_text)

    def test_desktop_accessibility_contract_for_statuses_and_buttons(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        main_dashboard = main_text.split("class ViperDashboard", 1)[1]

        forbidden_button_labels = [
            'label="Apply setting"',
            'label="Set number"',
            'label="Press button"',
            'label="Turn on"',
            'label="Turn off"',
            'label="Test"',
            'label="Del"',
        ]
        for label in forbidden_button_labels:
            self.assertNotIn(label, main_dashboard, f"Desktop button label is too generic for JAWS: {label}")

        focusable_status_names = [
            "Global mute status.",
            "Setup Status.",
            "Setup checklist.",
            "Doorbell video analysis status.",
            "Doorbell Vision status.",
            "AI Description status.",
            "Vacuum status.",
            "Vacuum room status.",
            "Health Summary.",
            "Safe Smoke Test results.",
            "Speed diagnostics status.",
            "Home Assistant status.",
        ]
        missing = [name for name in focusable_status_names if name not in main_dashboard]
        self.assertEqual(missing, [], f"Readable desktop status names missing: {missing}")

        literal_button_labels = re.findall(r"wx\.Button\([^\\n]+label=\"([^\"]*)\"", main_dashboard)
        empty_or_tiny = [label for label in literal_button_labels if len(label.strip()) < 2]
        self.assertEqual(empty_or_tiny, [], "Desktop buttons need visible text that JAWS can announce.")

        checklist_uses = re.findall(r"self\.([A-Za-z0-9_]+)\s*=\s*wx\.CheckListBox", main_dashboard)
        self.assertEqual(
            checklist_uses,
            ["speaker_list"],
            "wx.CheckListBox should only be used for the speaker list, which has explicit speech handlers. Use real wx.CheckBox controls elsewhere.",
        )

    def test_remote_accessibility_contract_for_buttons_and_statuses(self):
        template = Path("templates/remote.html").read_text(encoding="utf-8")
        parser = RemoteAccessibilityParser()
        parser.feed(template)

        ambiguous = []
        generic_labels = {
            "apply setting",
            "turn on",
            "turn off",
            "set number",
            "press button",
            "test",
            "del",
            "delete",
            "save",
            "close",
        }
        for button in parser.buttons:
            attrs = button["attrs"]
            text = _rendered_text(button["text"])
            accessible = _rendered_text(attrs.get("aria-label") or text)
            normalized = accessible.lower()
            if not accessible:
                ambiguous.append(f"line {button['line']}: empty button")
            elif normalized in generic_labels and not attrs.get("aria-label"):
                ambiguous.append(f"line {button['line']}: generic button text {accessible!r}")
            elif normalized in {"turn on", "turn off", "apply", "set"}:
                ambiguous.append(f"line {button['line']}: missing target in button text {accessible!r}")
        self.assertEqual(ambiguous, [], "Remote buttons need target-specific accessible names for JAWS.")

        self.assertIn('id="global-mute-status-text"', template)
        self.assertIn('id="arm-status-text"', template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn('role="alert"', template)
        self.assertIn('aria-label="Selected vacuum status"', template)
        self.assertIn('aria-label="Latest doorbell video analysis results"', template)

    def test_remote_accessibility_contract_for_form_controls(self):
        template = Path("templates/remote.html").read_text(encoding="utf-8")
        parser = RemoteAccessibilityParser()
        parser.feed(template)

        unlabeled = []
        for control in parser.controls:
            attrs = control["attrs"]
            control_type = (attrs.get("type") or "").lower()
            if control_type in {"hidden", "submit"}:
                continue
            control_id = attrs.get("id")
            has_label = bool(control_id and control_id in parser.labels_for)
            has_aria = bool(attrs.get("aria-label") or attrs.get("aria-labelledby") or attrs.get("aria-describedby"))
            if not (has_label or has_aria or control["inside_label"]):
                name = attrs.get("name") or attrs.get("id") or control["tag"]
                unlabeled.append(f"line {control['line']}: {control['tag']} {name}")
        self.assertEqual(unlabeled, [], "Remote form controls need a label or aria text for screen readers.")

    def test_setup_wizard_accessibility_contract(self):
        main_text = Path("main.pyw").read_text(encoding="utf-8")
        wizard_text = main_text.split("class ViperSetupWizardDialog", 1)[1].split("class ViperDashboard", 1)[0]

        required_status_names = [
            "Setup wizard page title",
            "Current setup step status",
            "Setup wizard instructions",
            "Current setup checklist",
        ]
        missing = [name for name in required_status_names if name not in wizard_text]
        self.assertEqual(missing, [], f"Setup wizard readable status names missing: {missing}")

        required_buttons = [
            "Find Home Assistant",
            "Check This PC And Home Assistant",
            "Install Home Assistant",
            "Open Home Assistant Account Setup",
            "Open Home Assistant Token Page",
            "Save Selected Doorbell Triggers",
            "Save Selected Camera Streams",
            "Test Front Doorbell Camera",
            "Test Back Doorbell Camera",
            "Save Selected Speakers",
            "Test Checked Speakers",
            "Set Up Refrigerator Alerts",
            "Set Up Robot Vacuum",
            "Refresh Checklist",
        ]
        missing_buttons = [label for label in required_buttons if label not in wizard_text]
        self.assertEqual(missing_buttons, [], f"Setup wizard button labels missing: {missing_buttons}")

        forbidden = ['label="Test"', 'label="Save"', 'label="Apply setting"', 'label="Turn on"', 'label="Turn off"']
        present = [label for label in forbidden if label in wizard_text]
        self.assertEqual(present, [], "Setup wizard should not introduce generic button labels.")

    def test_accessibility_report_generator_writes_release_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "accessibility_report.txt"
            report = accessibility_report.build_report(Path.cwd())
            report_path.write_text(report, encoding="utf-8")
            text = report_path.read_text(encoding="utf-8")

        self.assertIn("Viper Vision Accessibility Control Inventory", text)
        self.assertIn("Desktop wx Controls", text)
        self.assertIn("Remote Web Controls", text)
        self.assertIn("button: self.btn_arm", text)
        self.assertIn("Turn On Global Mute", text)
        self.assertNotIn("NO LABEL FOUND", text)

    def test_cinderella_specific_dock_error_prefers_dock_bucket(self):
        main.dash_app = FakeDashboard()
        main.dash_app.config["cinderella_messages"]["specific_errors"]["dock_duct_blockage"] = [
            "Dock duct blockage test message."
        ]

        message = main.choose_cinderella_message("error", error="duct_blockage", source="dock")

        self.assertEqual(message, "Dock duct blockage test message.")

    def test_current_config_requires_structured_doorbell_triggers(self):
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
        self.assertEqual(normalized["doorbell_triggers"]["front"]["rtsp_url"], "")
        self.assertEqual(normalized["doorbell_triggers"]["front"]["camera_id"], "")
        self.assertEqual(normalized["doorbell_triggers"]["front"]["mqtt_topic"], "")
        self.assertFalse(normalized["doorbell_triggers"]["front"]["enabled"])
        self.assertEqual(normalized["doorbell_triggers"]["back"]["rtsp_url"], "")

    def test_config_discards_unknown_runtime_source_keys(self):
        normalized = cfg.validate_and_normalize_config(
            {
                "ha_ip": "192.168.1.10",
                "ha_token_source": "windows_credential_manager",
                "gemini_api_key_source": "environment",
                "old_unused_option": True,
            }
        )

        self.assertEqual(normalized["ha_ip"], "192.168.1.10")
        self.assertNotIn("ha_token_source", normalized)
        self.assertNotIn("gemini_api_key_source", normalized)
        self.assertNotIn("old_unused_option", normalized)

    def test_setup_dialog_uses_configured_rtsp_not_derived_guess(self):
        settings = cfg.get_doorbell_settings(
            {
                "ha_ip": "192.168.1.10",
                "front_camera_id": "front123",
                "back_camera_id": "back456",
                "rtsp_front": "",
                "rtsp_back": "",
            },
            include_env=False,
        )

        self.assertEqual(settings["configured_rtsp_front"], "")
        self.assertEqual(settings["configured_rtsp_back"], "")
        self.assertEqual(settings["rtsp_front"], "rtsp://192.168.1.10:8554/front123_live")
        self.assertEqual(settings["rtsp_back"], "rtsp://192.168.1.10:8554/back456_live")

    def test_setup_dialog_does_not_advertise_ring_rtsp_guesses(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "main.pyw").read_text(encoding="utf-8")

        self.assertIn('label="Discover Devices"', text)
        self.assertIn("configured_rtsp_front", text)
        self.assertNotIn('self.rtsp_front_txt.SetValue(found["rtsp_url"])', text)
        self.assertNotIn('self.rtsp_back_txt.SetValue(found["rtsp_url"])', text)
        self.assertNotIn('lines.append(f"  RTSP: {item[', text)

    def test_setup_rtsp_discovery_tests_all_candidates(self):
        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        fake._normalize_rtsp_host = lambda url, host: url
        captured = []
        streams = [
            {"name": "front_live", "rtsp_url": "rtsp://ha:8554/front_live"},
            {"name": "back_live", "rtsp_url": "rtsp://ha:8554/back_live"},
            {"name": "side_live", "rtsp_url": "rtsp://ha:8554/side_live"},
        ]

        def fake_grab_frame(url, *_args, **_kwargs):
            captured.append(url)
            return "frame.jpg" if "side" not in url else ""

        with patch.object(main.vision, "grab_frame", side_effect=fake_grab_frame):
            with patch.object(main.wx, "CallAfter", side_effect=lambda func, *args: func(*args)):
                fake._finish_all_discovered_rtsp_tests = lambda results, host, attempts: captured.append(
                    ("results", [item["ok"] for item in results])
                )
                main.HomeAssistantSetupDialog._run_all_discovered_rtsp_tests(fake, streams, "ha", [])

        self.assertEqual(
            captured[:3],
            ["rtsp://ha:8554/front_live", "rtsp://ha:8554/back_live", "rtsp://ha:8554/side_live"],
        )
        self.assertEqual(captured[3], ("results", [True, True, False]))

    def test_ring_mqtt_stream_discovery_does_not_start_from_ha_camera_entities(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "main.pyw").read_text(encoding="utf-8")

        self.assertNotIn('settings["_candidate_streams"] = self._camera_rtsp_candidates_from_discovery(host)', text)
        self.assertIn("_run_find_ha_ring_rtsp_streams", text)
        self.assertIn("_run_find_ring_mqtt_log_streams", text)

    def test_speaker_discovery_formats_ha_and_sonos_candidates_without_adding(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = {
            "speakers": {
                "Saved Sonos": {"id": "192.168.4.20", "type": "sonos"},
            }
        }
        result = {
            "ok": True,
            "categories": {
                "media_players": [
                    {
                        "entity_id": "media_player.living_room",
                        "friendly_name": "Living Room",
                        "platform": "cast",
                    },
                    {
                        "entity_id": "media_player.kitchen_sonos",
                        "friendly_name": "Kitchen Sonos",
                        "platform": "sonos",
                    },
                ]
            },
        }

        ha_candidates = main.ViperDashboard._ha_speaker_candidates_from_result(fake, result)

        self.assertEqual(len(ha_candidates), 2)
        self.assertEqual(ha_candidates[0]["name"], "Living Room")
        self.assertFalse(ha_candidates[0]["is_sonos"])
        self.assertTrue(ha_candidates[1]["is_sonos"])
        self.assertEqual(fake.config["speakers"], {"Saved Sonos": {"id": "192.168.4.20", "type": "sonos"}})

    def test_speaker_discovery_reports_network_sonos_not_clearly_in_ha(self):
        calls = []
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = {"speakers": {}}
        fake._show_text_dialog = lambda title, text: calls.append((title, text))
        fake.notify = lambda text, priority=5, interrupt=False: calls.append(("notify", text))
        fake._speaker_candidate_lines = lambda candidates, title: main.ViperDashboard._speaker_candidate_lines(fake, candidates, title)
        fake._configured_speaker_ids = lambda: set()
        ha_candidates = [
            {
                "name": "Kitchen Sonos",
                "id": "media_player.kitchen_sonos",
                "type": "ha",
                "source": "Home Assistant",
                "is_sonos": True,
            }
        ]
        sonos_candidates = [
            {
                "name": "Kitchen Sonos",
                "id": "192.168.4.20",
                "type": "sonos",
                "source": "Network Sonos",
                "is_sonos": True,
            }
        ]

        main.ViperDashboard._show_discovered_speakers(fake, ha_candidates, sonos_candidates)

        text = calls[-1][1]
        self.assertIn("Sonos speakers already visible in Home Assistant", text)
        self.assertIn("Network Sonos speakers not clearly visible in Home Assistant", text)
        self.assertIn("Kitchen Sonos | sonos | 192.168.4.20 | available", text)
        self.assertIn("Check the speakers Viper should add", text)

    def test_discovered_speaker_targets_can_be_added_after_discovery(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = {"speakers": {}}
        fake.save_config = lambda: None
        fake.refresh_speaker_list = lambda: None
        fake._sync_speaker_routing_controls = lambda: None
        fake.refresh_setup_checklist = lambda: None
        fake._configured_speaker_ids = lambda: {
            str(data.get("id") or "")
            for data in fake.config.get("speakers", {}).values()
            if isinstance(data, dict) and data.get("id")
        }
        fake._unique_speaker_name = lambda base_name, spk_type: main.ViperDashboard._unique_speaker_name(fake, base_name, spk_type)

        added = main.ViperDashboard._add_discovered_speaker_targets(
            fake,
            [
                {"name": "Living Room", "id": "media_player.living_room", "type": "ha"},
                {"name": "Kitchen", "id": "192.168.4.20", "type": "sonos"},
            ],
        )

        self.assertEqual(added, 2)
        self.assertIn("Living Room (HA)", fake.config["speakers"])
        self.assertIn("Kitchen (SONOS)", fake.config["speakers"])
        self.assertTrue(fake.config["speakers"]["Living Room (HA)"]["doorbell"])

    def test_setup_dialog_includes_speaker_discovery(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "main.pyw").read_text(encoding="utf-8")

        self.assertIn('label="Discover Available Speakers"', text)
        self.assertIn('label="Add Selected Speakers"', text)
        self.assertIn('label="Save Selected Speakers"', text)
        self.assertIn("def _start_wizard_speaker_discovery", text)
        self.assertIn("def _populate_wizard_speaker_checks", text)
        self.assertIn("def on_save_wizard_speakers", text)
        self.assertIn("new speakers start unchecked", text.lower())
        self.assertIn("def on_discover_setup_speakers", text)
        self.assertIn("_run_setup_speaker_discovery", text)
        self.assertIn("lets you choose which ones to add", text)

    def test_setup_dialog_includes_summary_and_test_everything(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "main.pyw").read_text(encoding="utf-8")

        self.assertIn('label="Show Setup Summary"', text)
        self.assertIn('label="Test Everything"', text)
        self.assertIn('label="Create Support Report To Email Developer"', text)
        self.assertIn("def on_show_setup_summary", text)
        self.assertIn("def on_setup_test_everything", text)
        self.assertIn("def on_create_support_report", text)

    def test_suggested_setup_page_routes_to_missing_step(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = {}
        self.assertEqual(main.ViperDashboard.suggested_setup_page(fake), "connect")

        fake.config = {
            "ha_ip": "192.168.4.56",
            "ha_token": "token",
            "doorbell_triggers": {},
        }
        self.assertEqual(main.ViperDashboard.suggested_setup_page(fake), "doorbells")

        fake.config = {
            "ha_ip": "192.168.4.56",
            "ha_token": "token",
            "rtsp_front": "rtsp://front",
            "rtsp_back": "rtsp://back",
            "doorbell_triggers": {
                "front": {"trigger_entity_id": "binary_sensor.front", "rtsp_url": "rtsp://front"},
                "back": {"trigger_entity_id": "binary_sensor.back", "rtsp_url": "rtsp://back"},
            },
        }
        self.assertEqual(main.ViperDashboard.suggested_setup_page(fake), "speakers")

        fake.config["gemini_api_key"] = "gemini"
        fake.config["speakers"] = {
            "Kitchen": {"enabled": True, "doorbell": True, "utilities": True, "fridge": True}
        }
        self.assertEqual(main.ViperDashboard.suggested_setup_page(fake), "test")

    def test_setup_rtsp_finish_excludes_failed_candidates_from_selection(self):
        calls = []

        class TextBox:
            def __init__(self):
                self.value = ""

            def SetValue(self, value):
                self.value = value

            def GetValue(self):
                return self.value

        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        fake._trusted_rtsp_urls = set()
        fake._verified_rtsp_urls = set()
        fake.rtsp_front_txt = TextBox()
        fake.rtsp_back_txt = TextBox()
        fake.parent = type("Parent", (), {"save_config": lambda _self: calls.append("saved")})()
        fake._set_busy = lambda busy: calls.append(("busy", busy))
        fake._set_setup_status = lambda message, announce=False: calls.append(("status", message))
        fake._settings = lambda: {"rtsp_front": fake.rtsp_front_txt.GetValue(), "rtsp_back": fake.rtsp_back_txt.GetValue()}
        fake._apply_settings_to_parent = lambda settings: calls.append(("apply", settings))

        def choose(side, passed, host):
            calls.append(("choose", side, [item["rtsp_url"] for item in passed]))
            return passed[0]["rtsp_url"] if side == "front" else passed[-1]["rtsp_url"]

        fake._choose_tested_ring_mqtt_stream = choose
        fake._auto_fill_tested_streams_if_clear = lambda passed, host: False
        results = [
            {"ok": True, "rtsp_url": "rtsp://ha:8554/front_live", "stream": {"name": "front_live"}},
            {"ok": False, "rtsp_url": "rtsp://ha:8554/bad_live", "stream": {"name": "bad_live"}, "message": "bad"},
            {"ok": True, "rtsp_url": "rtsp://ha:8554/back_live", "stream": {"name": "back_live"}},
            {"ok": True, "rtsp_url": "rtsp://ha:8554/side_live", "stream": {"name": "side_live"}},
        ]

        main.HomeAssistantSetupDialog._finish_all_discovered_rtsp_tests(fake, results, "ha", [])

        choose_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "choose"]
        self.assertEqual(len(choose_calls), 2)
        self.assertNotIn("rtsp://ha:8554/bad_live", choose_calls[0][2])
        self.assertIn("rtsp://ha:8554/front_live", fake._trusted_rtsp_urls)
        self.assertIn("rtsp://ha:8554/back_live", fake._verified_rtsp_urls)
        self.assertEqual(fake.rtsp_front_txt.GetValue(), "rtsp://ha:8554/front_live")
        self.assertEqual(fake.rtsp_back_txt.GetValue(), "rtsp://ha:8554/side_live")
        self.assertIn("saved", calls)

    def test_restore_main_window_focus_uses_repeated_strong_restore(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        calls = []
        fake._restore_main_window_focus_once = lambda: calls.append("restore")

        with patch.object(main.wx, "CallLater", side_effect=lambda _delay, func, *args: calls.append(("later", _delay))):
            main.ViperDashboard.restore_main_window_focus(fake)

        self.assertEqual(calls[0], "restore")
        self.assertEqual([item[1] for item in calls[1:]], [100, 300, 700])

    def test_dashboard_activation_returns_focus_to_open_setup_window(self):
        calls = []

        class Event:
            def GetActive(self):
                return True

            def Skip(self):
                calls.append("skip")

        class SetupWindow:
            _initial_focus_given = True

            def GetHandle(self):
                return 123

            def IsIconized(self):
                return False

            def Restore(self):
                calls.append("restore")

            def Show(self, value):
                calls.append(("show", value))

            def Enable(self, value):
                calls.append(("enable", value))

            def Raise(self):
                calls.append("raise")

            def force_initial_focus(self):
                calls.append("setup_focus")

        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake._setup_wizard_dialog = SetupWindow()
        fake._ha_setup_dialog = None
        fake._ha_server_assistant_dialog = None
        fake._log_setup_focus_snapshot = lambda context: calls.append(("snapshot", context))

        with patch.object(main.wx, "CallAfter", lambda func, *args, **kwargs: func(*args, **kwargs)), \
             patch.object(main.wx, "CallLater", lambda _ms, func, *args, **kwargs: func(*args, **kwargs)):
            main.ViperDashboard.on_dashboard_activate(fake, Event())

        self.assertIn("raise", calls)
        self.assertIn("setup_focus", calls)
        self.assertNotIn("skip", calls)

    def test_setup_wizard_is_top_level_and_hides_dashboard_during_setup(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        self.assertIn("dlg = ViperSetupWizardDialog(None, owner=self)", main_text)
        self.assertIn("def _enter_setup_window_mode", main_text)
        self.assertIn("self.Show(False)", main_text)
        self.assertIn("def _leave_setup_window_mode", main_text)

    def test_startup_restore_does_not_force_control_focus(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        calls = []

        class Ctrl:
            def __init__(self, label=""):
                self.label = label

            def SetFocusFromKbd(self):
                calls.append(("kbd", self.label))

            def SetFocus(self):
                calls.append(("focus", self.label))

            def IsShownOnScreen(self):
                return True

            def IsEnabled(self):
                return True

            def CanAcceptFocus(self):
                return True

            def CanAcceptFocusFromKeyboard(self):
                return True

            def GetLabel(self):
                return self.label

            def GetName(self):
                return self.label

        class Notebook(Ctrl):
            def __init__(self):
                super().__init__("notebook")
                self.selection = None

            def GetPageCount(self):
                return 1

            def GetPage(self, _idx):
                return fake.tab_dash

            def SetSelection(self, idx):
                self.selection = idx

        fake.tab_dash = object()
        fake.notebook = Notebook()
        fake.btn_arm = Ctrl("Disarm System")
        fake.broadcast_input = Ctrl("Broadcast")
        fake._setup_window_attrs = lambda: []
        fake._is_live_window = lambda _window: False
        fake.Show = lambda value: calls.append(("show", value))
        fake.IsIconized = lambda: False
        fake.Restore = lambda: calls.append("restore")
        fake.Raise = lambda: calls.append("raise")
        fake._nudge_windows_foreground = lambda: calls.append("nudge")

        main.ViperDashboard.restore_startup_focus(fake)

        self.assertIn(("show", True), calls)
        self.assertIn("raise", calls)
        self.assertNotIn(("kbd", "notebook"), calls)
        self.assertNotIn(("kbd", "Disarm System"), calls)

    def test_tray_restore_uses_dashboard_focus_restore_when_available(self):
        calls = []

        class Frame:
            def restore_from_tray_focus(self):
                calls.append("tray_restore")

        fake = main.ViperTaskBarIcon.__new__(main.ViperTaskBarIcon)
        fake.frame = Frame()
        main.ViperTaskBarIcon._restore_frame(fake)
        self.assertEqual(calls, ["tray_restore"])

    def test_tray_restore_fallback_does_not_crash_without_dashboard_helper(self):
        calls = []

        class Frame:
            def Show(self, value):
                calls.append(("show", value))

            def IsIconized(self):
                return False

            def Restore(self):
                calls.append("restore")

            def SetFocus(self):
                calls.append("focus")

            def Raise(self):
                calls.append("raise")

            def RequestUserAttention(self, _level):
                calls.append("attention")

            def GetHandle(self):
                return 0

        fake = main.ViperTaskBarIcon.__new__(main.ViperTaskBarIcon)
        fake.frame = Frame()
        main.ViperTaskBarIcon._restore_frame(fake)
        self.assertIn("focus", calls)
        self.assertIn("raise", calls)

    def test_tray_restore_prefers_tab_control(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)

        class Ctrl:
            def __init__(self, label):
                self.label = label

            def SetFocus(self):
                pass

            def IsShownOnScreen(self):
                return True

            def IsEnabled(self):
                return True

            def CanAcceptFocus(self):
                return True

            def CanAcceptFocusFromKeyboard(self):
                return True

        class Notebook(Ctrl):
            def __init__(self):
                super().__init__("notebook")

            def GetSelection(self):
                return 0

            def GetPage(self, _idx):
                return fake.tab_doorbell

        fake.tab_doorbell = object()
        fake.notebook = Notebook()
        fake.btn_doorbell_setup = Ctrl("doorbell setup")
        fake.btn_analyze_front_video = Ctrl("analyze front")

        target = main.ViperDashboard._preferred_focus_after_tray_restore(fake)

        self.assertIs(target, fake.notebook)

    def test_tab_change_does_not_force_focus(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        calls = []

        class Ctrl:
            def __init__(self, label):
                self.label = label

            def SetFocusFromKbd(self):
                calls.append(("kbd", self.label))

            def SetFocus(self):
                calls.append(("focus", self.label))

            def IsShownOnScreen(self):
                return True

            def IsEnabled(self):
                return True

            def CanAcceptFocus(self):
                return True

            def CanAcceptFocusFromKeyboard(self):
                return True

            def GetLabel(self):
                return self.label

            def GetName(self):
                return self.label

        class Notebook(Ctrl):
            def __init__(self, page):
                super().__init__("notebook")
                self.page = page

            def GetSelection(self):
                return 0

            def GetPage(self, _idx):
                return self.page

        class Event:
            def __init__(self, notebook):
                self.notebook = notebook
                self.skipped = False

            def GetEventObject(self):
                return self.notebook

            def Skip(self):
                self.skipped = True

        fake.tab_doorbell = object()
        fake.tab_setup = object()
        fake.btn_doorbell_setup = Ctrl("old doorbell button")
        fake.btn_setup_wizard = Ctrl("new setup button")
        fake.notebook = Notebook(fake.tab_setup)

        event = Event(fake.notebook)
        with patch.object(main.wx, "CallAfter", lambda func, *args, **kwargs: func(*args, **kwargs)), \
             patch.object(main.wx, "CallLater", lambda _ms, func, *args, **kwargs: func(*args, **kwargs)):
            main.ViperDashboard.on_notebook_page_changed(fake, event)

        self.assertTrue(event.skipped)
        self.assertNotIn(("kbd", "notebook"), calls)
        self.assertNotIn(("kbd", "new setup button"), calls)
        self.assertNotIn(("kbd", "old doorbell button"), calls)

    def test_desktop_keeps_real_tabs_without_startup_focus_spam(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        self.assertIn("self.notebook = wx.Notebook(self.panel)", main_text)
        self.assertIn("self.audio_notebook = wx.Notebook(self.tab_audio_shell)", main_text)
        self.assertIn("self.devices_notebook = wx.Notebook(self.tab_devices_shell)", main_text)
        self.assertIn("self.diagnostics_notebook = wx.Notebook(self.tab_diagnostics_shell)", main_text)
        self.assertNotIn("self.notebook = wx.Simplebook(self.panel)", main_text)
        self.assertNotIn("wx.CallAfter(self.restore_startup_focus)", main_text)
        self.assertNotIn("wx.CallLater(150, self.restore_startup_focus)", main_text)
        self.assertNotIn("class StartupStatusFrame", main_text)
        startup_block = main_text.split('if __name__ == "__main__":', 1)[1]
        self.assertNotIn("StartupStatusFrame()", startup_block)
        self.assertNotIn("startup_frame.Show", startup_block)
        self.assertNotIn("dash_app.Raise()", startup_block)
        self.assertIn("dash_app = ViperDashboard()", startup_block)

    def test_remote_flask_secret_does_not_use_hardcoded_default(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        self.assertIn("app.secret_key = _get_flask_secret_key()", main_text)
        self.assertIn('os.getenv("VIPER_SECRET_KEY")', main_text)
        self.assertIn("secrets.token_hex(32)", main_text)
        self.assertNotIn('app.secret_key = os.getenv("VIPER_SECRET_KEY", "viper_vision_secure_key")', main_text)

    def test_broadcast_mode_aliases_keep_chime_only_from_speaking(self):
        config = {
            "broadcast_channels": {
                "default": {"mode": "speak", "chime": ""},
                "fridge_open": {"mode": "Chime only", "chime": "ding.mp3"},
            }
        }
        settings = main._resolve_channel_settings("fridge_open", config)
        self.assertEqual(main._normalize_broadcast_mode(settings["mode"]), "chime")

    def _setup_broadcast_dashboard(self, channels=None):
        main.is_shutting_down.clear()
        fake = FakeDashboard()
        fake.config["broadcast_channels"] = channels or {
            "default": {"mode": "speak", "chime": ""},
            "fridge_open": {"mode": "chime", "chime": "fridge-open.mp3"},
            "fridge_closed": {"mode": "chime", "chime": "fridge-closed.mp3"},
            "freezer_open": {"mode": "chime", "chime": "freezer-open.mp3"},
            "freezer_closed": {"mode": "chime", "chime": "freezer-closed.mp3"},
        }
        fake.notifications = []
        fake.notify = lambda *args, **kwargs: fake.notifications.append((args, kwargs))
        main.dash_app = fake
        return fake

    def _immediate_future_submit(self, func, *args):
        func(*args)
        return object()

    def test_broadcast_fridge_chime_channel_never_calls_tts(self):
        self._setup_broadcast_dashboard()
        calls = []

        with main.app.test_request_context("/remote/broadcast", method="POST", headers={"Accept": "application/json"}):
            with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
                 patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
                 patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
                response, status = main._broadcast_message("The fridge door is open.", channel="fridge_open")

        self.assertEqual(status, 200)
        self.assertEqual(calls, [("chime", ("fridge-open.mp3", "fridge_open"))])

    def test_dispatch_broadcast_fridge_chime_channel_never_calls_tts(self):
        self._setup_broadcast_dashboard()
        calls = []

        with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
             patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
             patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
            result = main._dispatch_broadcast_message("The refrigerator door is open.", channel="fridge_open")

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "chime")
        self.assertEqual(result["resolved_channel"], "fridge_open")
        self.assertEqual(calls, [("chime", ("fridge-open.mp3", "fridge_open"))])

    def test_global_mute_broadcast_logs_without_audio(self):
        fake = self._setup_broadcast_dashboard()
        fake.config["global_mute"] = True
        calls = []

        with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
             patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
             patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
            result = main._dispatch_broadcast_message("Test mute", channel="manual")

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "muted")
        self.assertEqual(calls, [])
        self.assertIn("Global mute is on", fake.notifications[0][0][0])

    def test_global_mute_remote_button_is_present(self):
        template = Path("templates/remote.html").read_text(encoding="utf-8")
        self.assertIn("web_toggle_global_mute", template)
        self.assertIn("Turn On Global Mute", template)
        self.assertIn("Turn Off Global Mute", template)

    def test_ha_listener_broadcast_uses_channel_routing_not_utility_tts(self):
        self._setup_broadcast_dashboard()
        calls = []

        with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
             patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
             patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
            main._handle_ha_listener_action({
                "type": "broadcast",
                "channel": "fridge_open",
                "message": "The refrigerator door is open.",
            })

        self.assertEqual(calls, [("chime", ("fridge-open.mp3", "fridge_open"))])

    def test_broadcast_legacy_fridge_manual_message_infers_chime(self):
        self._setup_broadcast_dashboard()
        calls = []

        with main.app.test_request_context("/remote/broadcast", method="POST", headers={"Accept": "application/json"}):
            with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
                 patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
                 patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
                response, status = main._broadcast_message("The fridge door is open.", channel="manual")

        self.assertEqual(status, 200)
        self.assertEqual(calls, [("chime", ("fridge-open.mp3", "fridge_open"))])

    def test_broadcast_legacy_refrigerator_closed_infers_chime(self):
        self._setup_broadcast_dashboard()
        calls = []

        with main.app.test_request_context("/remote/broadcast", method="POST", headers={"Accept": "application/json"}):
            with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
                 patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
                 patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
                response, status = main._broadcast_message("The refrigerator door is closed.", channel="")

        self.assertEqual(status, 200)
        self.assertEqual(calls, [("chime", ("fridge-closed.mp3", "fridge_closed"))])

    def test_broadcast_legacy_freezer_messages_infer_chime(self):
        self._setup_broadcast_dashboard()
        calls = []

        with main.app.test_request_context("/remote/broadcast", method="POST", headers={"Accept": "application/json"}):
            with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
                 patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
                 patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
                main._broadcast_message("The freezer door is open.", channel="default")
                main._broadcast_message("The freezer door is closed.", channel="manual")

        self.assertEqual(
            calls,
            [
                ("chime", ("freezer-open.mp3", "freezer_open")),
                ("chime", ("freezer-closed.mp3", "freezer_closed")),
            ],
        )

    def test_broadcast_normal_manual_message_still_speaks(self):
        self._setup_broadcast_dashboard()
        calls = []

        with main.app.test_request_context("/remote/broadcast", method="POST", headers={"Accept": "application/json"}):
            with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
                 patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
                 patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
                response, status = main._broadcast_message("This is a normal whole-house announcement.", channel="manual")

        self.assertEqual(status, 200)
        self.assertEqual(calls, [("tts", ("manual", "This is a normal whole-house announcement.", False))])

    def test_broadcast_silent_mode_calls_neither_chime_nor_tts(self):
        self._setup_broadcast_dashboard(
            {
                "default": {"mode": "speak", "chime": ""},
                "fridge_open": {"mode": "silent", "chime": "fridge-open.mp3"},
            }
        )
        calls = []

        with main.app.test_request_context("/remote/broadcast", method="POST", headers={"Accept": "application/json"}):
            with patch.object(main.audio, "play_broadcast_chime", side_effect=lambda *args: calls.append(("chime", args))), \
                 patch.object(main.audio, "play_notification", side_effect=lambda *args: calls.append(("tts", args))), \
                 patch.object(main, "safe_submit", side_effect=self._immediate_future_submit):
                response, status = main._broadcast_message("The fridge door is open.", channel="")

        self.assertEqual(status, 200)
        self.assertEqual(calls, [])

    def test_config_normalizes_broadcast_mode_aliases(self):
        normalized = cfg.validate_and_normalize_config(
            {"broadcast_channels": {"fridge_open": {"mode": "sound only", "chime": "ding.mp3"}}}
        )
        self.assertEqual(normalized["broadcast_channels"]["fridge_open"]["mode"], "chime")

        normalized = cfg.validate_and_normalize_config(
            {"broadcast_channels": {"fridge_open": {"mode": "tone only", "chime": "ding.mp3"}}}
        )
        self.assertEqual(normalized["broadcast_channels"]["fridge_open"]["mode"], "chime")

    def test_ha_discovery_missing_host_is_clear(self):
        with patch.object(discovery.cfg, "get_ha_settings", return_value={"ha_ip": "", "ha_port": "8123", "ha_token": ""}):
            result = discovery.discover_ha_entities(ha_ip="", ha_port="8123", token="token")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_host")

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

    def test_ha_listener_does_not_derive_ring_rtsp_url_from_flat_fields(self):
        triggers = ha_listener.normalize_doorbell_triggers(
            {
                "ha_ip": "192.168.1.10",
                "front_camera_id": "abc123",
                "rtsp_front": "rtsp://192.168.1.10:8554/abc123_live",
            }
        )

        self.assertFalse(triggers["front"]["enabled"])
        self.assertEqual(triggers["front"]["rtsp_url"], "")
        self.assertEqual(triggers["front"]["camera_id"], "")

    def test_ha_host_normalization_accepts_url_and_plain_host(self):
        self.assertEqual(discovery.normalize_ha_host("http://homeassistant.local:8123"), ("homeassistant.local", "8123"))
        self.assertEqual(discovery.normalize_ha_host("192.168.1.10"), ("192.168.1.10", "8123"))

    def test_find_home_assistant_saves_resolved_ip_for_local_name(self):
        class FakeResponse:
            status_code = 200

        with patch.object(discovery, "candidate_ha_hosts", return_value=[{"ha_ip": "homeassistant.local", "ha_port": "8123", "reason": "test"}]):
            with patch.object(discovery.requests, "get", return_value=FakeResponse()):
                with patch.object(discovery.socket, "gethostbyname", return_value="192.168.4.50"):
                    result = discovery.find_home_assistant(token="good-token", timeout=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["ha_ip"], "192.168.4.50")
        self.assertEqual(result["ha_host"], "homeassistant.local")
        self.assertEqual(result["ha_url"], "http://192.168.4.50:8123")

    def test_first_run_check_pc_saves_found_home_assistant_ip(self):
        saved = []
        fake = main.HomeAssistantFirstRunAssistantDialog.__new__(main.HomeAssistantFirstRunAssistantDialog)
        fake.parent = type(
            "Parent",
            (),
            {
                "config": {},
                "save_config": lambda self: saved.append(dict(self.config)),
            },
        )()
        fake.status_txt = type("Status", (), {"SetValue": lambda self, value: saved.append({"status": value})})()
        fake._finish_check_pc(
            {"installed": True, "version": "test", "path": "VBoxManage.exe"},
            {"installed": True, "version": "winget-test", "path": "winget.exe"},
            True,
            {"ok": True, "ha_ip": "192.168.4.50", "ha_port": "8123", "auth_ok": False},
            {"supported": True, "architecture": "amd64", "message": "supported test"},
        )

        self.assertEqual(fake.parent.config["ha_ip"], "192.168.4.50")
        self.assertEqual(fake.parent.config["ha_port"], "8123")
        self.assertTrue(any(item.get("ha_ip") == "192.168.4.50" for item in saved))

    def test_wait_for_home_assistant_first_boot_reports_delayed_success(self):
        progress = []
        results = [
            {"ok": False, "attempts": [{"error": "connection refused"}]},
            {"ok": True, "ha_ip": "192.168.4.50", "ha_port": "8123", "auth_ok": False},
        ]
        core_results = [
            {"ready": True, "ha_ip": "192.168.4.50", "ha_port": "8123", "auth_ok": False, "message": "Core ready"},
        ]

        with patch.object(main.discovery, "find_home_assistant", side_effect=results):
            with patch.object(main, "_check_home_assistant_core_ready", side_effect=core_results):
                with patch.object(main.time, "sleep", return_value=None):
                    result = main.wait_for_home_assistant_first_boot(
                        progress=progress.append,
                        seed_host="192.168.4.50",
                        timeout_seconds=60,
                        interval_seconds=5,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["ha_ip"], "192.168.4.50")
        self.assertTrue(any("Waiting for Home Assistant first boot" in line for line in progress))

    def test_wait_for_home_assistant_first_boot_does_not_accept_preparing_core(self):
        progress = []
        found = {"ok": True, "ha_ip": "192.168.4.50", "ha_port": "8123", "auth_ok": False}
        core_results = [
            {
                "ready": False,
                "found": True,
                "ha_ip": "192.168.4.50",
                "ha_port": "8123",
                "message": "Home Assistant web interface is responding, but Core/auth is still preparing.",
            },
            {
                "ready": True,
                "ha_ip": "192.168.4.50",
                "ha_port": "8123",
                "auth_ok": False,
                "message": "Core ready after download",
            },
        ]

        with patch.object(main.discovery, "find_home_assistant", return_value=found):
            with patch.object(main, "_check_home_assistant_core_ready", side_effect=core_results):
                with patch.object(main.time, "sleep", return_value=None):
                    result = main.wait_for_home_assistant_first_boot(
                        progress=progress.append,
                        seed_host="192.168.4.50",
                        timeout_seconds=60,
                        interval_seconds=5,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "Core ready after download")
        self.assertTrue(any("Core/auth is still preparing" in line for line in progress))

    def test_home_assistant_core_ready_requires_auth_layer_without_token(self):
        class PreparingResponse:
            status_code = 200
            text = "Preparing Home Assistant. Downloading Home Assistant Core."

        class ReadyResponse:
            status_code = 401
            text = "Unauthorized"

        candidates = [{"ha_ip": "192.168.4.50", "ha_port": "8123", "reason": "test"}]
        with patch.object(main.discovery, "candidate_ha_hosts", return_value=candidates):
            with patch.object(main.requests, "get", return_value=PreparingResponse()):
                preparing = main._check_home_assistant_core_ready(seed_host="192.168.4.50")
            with patch.object(main.requests, "get", return_value=ReadyResponse()):
                ready = main._check_home_assistant_core_ready(seed_host="192.168.4.50")

        self.assertFalse(preparing["ready"])
        self.assertTrue(preparing["found"])
        self.assertIn("still preparing", preparing["message"])
        self.assertTrue(ready["ready"])
        self.assertFalse(ready["auth_ok"])

    def test_setup_progress_parser_uses_real_download_percent(self):
        state = main._classify_setup_progress_message(
            "Downloading Home Assistant OS: 250 MB of 1000 MB, 25 percent."
        )

        self.assertEqual(state["phase"], "haos_download")
        self.assertEqual(state["phase_label"], "Downloading Home Assistant OS")
        self.assertEqual(state["percent"], 25)
        self.assertTrue(state["active"])

    def test_setup_progress_parser_uses_real_winget_byte_percent(self):
        state = main._classify_setup_progress_message("winget: ███████████████ 85 MB / 170 MB")

        self.assertEqual(state["phase"], "virtualbox_install")
        self.assertEqual(state["percent"], 50)

    def test_process_progress_line_removes_winget_progress_bar_symbols(self):
        line = main._clean_process_progress_line("██████████▒▒▒▒▒▒ 85 MB / 170 MB", output_prefix="winget")

        self.assertEqual(line, "VirtualBox download: 50 percent, 85 MB of 170 MB.")
        self.assertNotIn("█", line)
        self.assertNotIn("▒", line)

    def test_setup_progress_parser_tracks_home_assistant_core_wait(self):
        state = main._classify_setup_progress_message(
            "Waiting for Home Assistant first boot. Elapsed about 10 minute(s) of up to 25. Last check: Home Assistant web interface is responding, but Core/auth is still preparing."
        )

        self.assertEqual(state["phase"], "ha_core_wait")
        self.assertEqual(state["percent"], 40)
        self.assertIn("Home Assistant Core", state["phase_label"])
        formatted = main._format_setup_progress_state(state, ["detail line"])
        self.assertIn("Progress: 40%", formatted)
        self.assertIn("Recent detailed progress", formatted)

    def test_config_preserves_setup_progress_state_for_resume(self):
        config = cfg.validate_and_normalize_config(
            {
                "setup_progress": {
                    "active": True,
                    "phase": "ha_core_wait",
                    "phase_label": "Waiting For Home Assistant Core",
                    "status": "Still preparing",
                    "percent": 67,
                    "next_action": "Keep waiting.",
                }
            }
        )

        self.assertTrue(config["setup_progress"]["active"])
        self.assertEqual(config["setup_progress"]["phase"], "ha_core_wait")
        self.assertEqual(config["setup_progress"]["percent"], 67)

    def test_find_home_assistant_reports_bad_token_on_reachable_host(self):
        class FakeResponse:
            status_code = 401

        with patch.object(discovery, "candidate_ha_hosts", return_value=[{"ha_ip": "192.168.4.49", "ha_port": "8123", "reason": "test"}]):
            with patch.object(discovery.requests, "get", return_value=FakeResponse()):
                result = discovery.find_home_assistant(token="bad-token", timeout=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["ha_ip"], "192.168.4.49")
        self.assertFalse(result["auth_ok"])
        self.assertEqual(result["auth_error"], "bad_token")

    def test_ha_core_health_detects_core_hung_with_observer_alive(self):
        def fake_probe(url, **kwargs):
            if ":4357/" in url:
                return {"ok": True, "status_code": 200, "elapsed_ms": 50, "message": "HTTP 200 in 50 ms."}
            return {"ok": False, "error": "timeout", "elapsed_ms": 5000, "message": "Timed out after 5000 ms."}

        with patch.object(discovery, "_probe_url", side_effect=fake_probe):
            result = discovery.check_ha_core_health(ha_ip="192.168.4.49", ha_port="8123", timeout=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "core_hung")
        self.assertIn("VM is alive", result["message"])

    def test_diagnostics_text_includes_ha_health_split(self):
        diag = diagnostics.collect_diagnostics(
            cfg.validate_and_normalize_config({}),
            ha_health={
                "checked": True,
                "state": "core_hung",
                "message": "Observer is responding, Core is not.",
                "core": {"ok": False, "message": "Timed out."},
                "observer": {"ok": True, "message": "HTTP 200."},
            },
        )
        text = diagnostics.diagnostics_text(diag)

        self.assertIn("HA health state: core_hung", text)
        self.assertIn("HA Core API: not responding", text)
        self.assertIn("HA Observer: responding", text)

    def test_diagnostics_health_summary_splits_active_and_historical_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "viper_last_crash.txt").write_text("active crash", encoding="utf-8")
            (data_dir / "viper_last_crash.resolved.20260517_131116.txt").write_text("resolved crash", encoding="utf-8")
            (data_dir / "viper_full_debug.log").write_text(
                "\n".join([
                    "2026-05-17 10:00:00,000 [INFO] [ALEXA PLAY SKIP - media_player.test]: Alexa uses announce, not direct media playback.",
                    "2026-05-17 10:01:00,000 [INFO] [DOORBELL TIMING] trace=x ignored_disarmed",
                    "2026-05-17 10:02:00,000 [WARNING] [HA LISTENER] connection failed: no close frame received or sent",
                ]),
                encoding="utf-8",
            )

            with patch.object(cfg, "DATA_DIR", data_dir), patch.object(diagnostics, "ffmpeg_status", return_value={"available": True, "configured": "ffmpeg", "resolved": "ffmpeg"}):
                diag = diagnostics.collect_diagnostics(
                    cfg.validate_and_normalize_config({"ha_ip": "192.168.4.49", "ha_token": "token", "speakers": {"Kitchen": {"id": "media_player.kitchen"}}}),
                    ha_listener_status={"connected": True, "last_error": ""},
                )

        self.assertEqual(diag["health"]["status"], "attention")
        self.assertTrue(any("Active crash report" in item for item in diag["health"]["active_issues"]))
        self.assertTrue(any("Resolved crash archive" in item for item in diag["health"]["stale_history"]))
        self.assertTrue(any("close-frame warning" in item for item in diag["health"]["stale_history"]))
        self.assertTrue(any("ALEXA PLAY SKIP" in item for item in diag["health"]["normal_noise"]))
        self.assertTrue(diag["health"]["log_rotation"]["enabled"])

    def test_diagnostics_text_leads_with_no_active_issues_when_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "viper_full_debug.log").write_text("2026-05-17 10:00:00,000 [INFO] all good", encoding="utf-8")
            with patch.object(cfg, "DATA_DIR", data_dir), patch.object(diagnostics, "ffmpeg_status", return_value={"available": True, "configured": "ffmpeg", "resolved": "ffmpeg"}):
                diag = diagnostics.collect_diagnostics(
                    cfg.validate_and_normalize_config({"ha_ip": "192.168.4.49", "ha_token": "token", "speakers": {"Kitchen": {"id": "media_player.kitchen"}}}),
                    ha_listener_status={"connected": True, "last_error": ""},
                    ha_connection={"checked": True, "ok": True, "message": "ok"},
                    ha_health={"checked": True, "ok": True, "state": "healthy", "message": "ok"},
                )
                text = diagnostics.diagnostics_text(diag)

        self.assertEqual(diag["health"]["status"], "ok")
        self.assertIn("Active issues:\nNone detected.", text)
        self.assertIn("Log rotation:", text)

    def test_first_run_assistant_helpers_are_safe_without_virtualbox(self):
        with patch.object(main.shutil, "which", return_value=None), patch.object(main.Path, "exists", return_value=False):
            self.assertEqual(main.find_vboxmanage(), "")
            status = main.get_virtualbox_status()
        self.assertFalse(status["installed"])
        self.assertIn("not found", status["message"].lower())

    def test_find_ha_with_token_chains_into_entity_discovery(self):
        calls = []

        class FakeSetupDialog:
            ha_token_txt = type("Token", (), {"GetValue": lambda _self: "token"})()
            ha_ip_txt = type("Text", (), {"SetValue": lambda _self, value: calls.append(("ha_ip", value))})()
            ha_port_txt = type("Text", (), {"SetValue": lambda _self, value: calls.append(("ha_port", value))})()
            status_txt = type("Status", (), {"SetValue": lambda _self, value: calls.append(("status", value))})()

            def _settings(self):
                return {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"}

            def _refresh_derived_doorbell_preview(self):
                calls.append(("refresh", True))

            def _set_busy(self, busy):
                calls.append(("busy", busy))

            def _run_discovery_test(self, settings):
                calls.append(("discover", settings["ha_ip"]))

        fake = FakeSetupDialog()
        with patch.object(main, "safe_submit", side_effect=lambda func, *args: func(*args)):
            main.HomeAssistantSetupDialog._finish_find_ha(
                fake,
                {"ok": True, "ha_ip": "192.168.4.49", "ha_port": "8123", "auth_ok": True},
            )

        self.assertIn(("discover", "192.168.4.49"), calls)
        self.assertNotIn(("busy", False), calls)

    def test_find_ha_bad_token_does_not_chain_into_discovery(self):
        calls = []

        class FakeSetupDialog:
            ha_token_txt = type("Token", (), {"GetValue": lambda _self: "bad-token"})()
            ha_ip_txt = type("Text", (), {"SetValue": lambda _self, value: calls.append(("ha_ip", value))})()
            ha_port_txt = type("Text", (), {"SetValue": lambda _self, value: calls.append(("ha_port", value))})()
            status_txt = type("Status", (), {"SetValue": lambda _self, value: calls.append(("status", value))})()

            def _settings(self):
                return {"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "bad-token"}

            def _refresh_derived_doorbell_preview(self):
                calls.append(("refresh", True))

            def _set_busy(self, busy):
                calls.append(("busy", busy))

            def _run_discovery_test(self, settings):
                calls.append(("discover", settings["ha_ip"]))

        fake = FakeSetupDialog()
        with patch.object(main, "safe_submit", side_effect=lambda func, *args: func(*args)):
            main.HomeAssistantSetupDialog._finish_find_ha(
                fake,
                {"ok": True, "ha_ip": "192.168.4.49", "ha_port": "8123", "auth_ok": False, "auth_error": "bad_token"},
            )

        self.assertNotIn(("discover", "192.168.4.49"), calls)
        self.assertIn(("busy", False), calls)
        self.assertTrue(any("rejected the token" in value for key, value in calls if key == "status"))

    def test_official_setup_links_include_home_assistant_install(self):
        self.assertIn("home-assistant.io/installation/windows", main.OFFICIAL_LINKS["ha_windows"])
        self.assertIn("virtualbox.org", main.OFFICIAL_LINKS["virtualbox"])

    def test_winget_status_missing_is_clear(self):
        with patch.object(main.shutil, "which", return_value=None):
            status = main.get_winget_status()
        self.assertFalse(status["installed"])
        self.assertIn("winget was not found", status["message"])

    def test_ha_vm_platform_rejects_arm_for_auto_install(self):
        with patch.object(main.platform, "machine", return_value="ARM64"):
            status = main.get_ha_vm_platform_status()
            result = main.install_virtualbox_with_winget()
        self.assertFalse(status["supported"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["unsupported_platform"])
        self.assertIn("not supported", result["message"])

    def test_ha_vm_ram_normalization_clamps_values(self):
        self.assertEqual(main.normalize_ha_vm_ram_mb("bad"), 4096)
        self.assertEqual(main.normalize_ha_vm_ram_mb(1024), 2048)
        self.assertEqual(main.normalize_ha_vm_ram_mb(32768), 16384)
        self.assertEqual(main.normalize_ha_vm_ram_mb(6144), 6144)

    def test_ha_vm_disk_normalization_clamps_values(self):
        self.assertEqual(main.normalize_ha_vm_disk_gb("bad"), 32)
        self.assertEqual(main.normalize_ha_vm_disk_gb(8), 16)
        self.assertEqual(main.normalize_ha_vm_disk_gb(512), 256)
        self.assertEqual(main.normalize_ha_vm_disk_gb(64), 64)

    def test_latest_haos_asset_prefers_haos_ova_zip(self):
        fake_response = type(
            "Response",
            (),
            {
                "raise_for_status": lambda _self: None,
                "json": lambda _self: {
                    "tag_name": "17.1",
                    "assets": [
                        {"name": "haos_generic-aarch64-17.1.img.xz", "browser_download_url": "bad", "size": 10},
                        {"name": "haos_ova-17.1.vdi.zip", "browser_download_url": "https://example.test/haos.zip", "size": 20},
                    ],
                },
            },
        )()
        with patch.object(main.requests, "get", return_value=fake_response):
            asset = main.get_latest_haos_virtualbox_asset()
        self.assertEqual(asset["name"], "haos_ova-17.1.vdi.zip")
        self.assertEqual(asset["url"], "https://example.test/haos.zip")

    def test_create_ha_vm_from_vdi_uses_expected_virtualbox_settings(self):
        calls = []

        def fake_run_vbox(args, timeout=120):
            calls.append(args)
            if args == ["list", "bridgedifs"]:
                return "Name: Wi-Fi\nStatus: Up\n"
            return ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "_run_vbox", side_effect=fake_run_vbox):
            main._create_ha_vm_from_vdi(Path(tmp) / "haos.vdi", ram_mb=6144)

        flattened = [" ".join(call) for call in calls]
        self.assertTrue(any("--memory 6144" in call for call in flattened))
        self.assertTrue(any("--cpus 2" in call for call in flattened))
        self.assertTrue(any("--firmware efi" in call for call in flattened))
        self.assertTrue(any("--nic1 bridged" in call for call in flattened))
        self.assertTrue(any("--hostiocache on" in call for call in flattened))

    def test_install_ha_vm_from_vdi_resizes_disk_before_attach(self):
        calls = []

        def fake_run_vbox(args, timeout=120, progress=None):
            calls.append(args)
            if args == ["list", "vms"]:
                return ""
            if args == ["list", "bridgedifs"]:
                return "Name: Wi-Fi\nStatus: Up\n"
            return ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "_run_vbox_progress", side_effect=fake_run_vbox), patch.object(main, "_run_vbox", side_effect=fake_run_vbox):
            vdi = Path(tmp) / "haos.vdi"
            vdi.write_text("fake", encoding="utf-8")
            result = main.install_home_assistant_vm_from_image(vdi, ram_mb=6144, disk_gb=64)

        flattened = [" ".join(str(part) for part in call) for call in calls]
        self.assertTrue(result["ok"])
        self.assertTrue(any("modifymedium disk" in call and "--resize 65536" in call for call in flattened))
        self.assertTrue(any("--memory 6144" in call for call in flattened))

    def test_ha_install_preflight_summary_is_plain_english(self):
        with patch.object(main, "get_ha_vm_platform_status", return_value={"supported": True, "message": "Automatic install is supported."}), \
             patch.object(main, "get_virtualbox_status", return_value={"installed": True, "version": "7.2.8", "message": "VirtualBox found."}), \
             patch.object(main, "get_windows_virtualization_status", return_value={"is_windows": True, "needs_attention": True, "message": "Windows hypervisor features appear to be enabled."}), \
             patch.object(main, "get_ha_vm_drive_space_status", return_value={"ok": True, "message": "Drive space: 100 GB free."}):
            summary = main.build_ha_install_preflight_summary({"ram_mb": 6144, "disk_gb": 64})

        self.assertTrue(summary["ok"])
        self.assertTrue(summary["drive_ok"])
        self.assertIn("RAM for Home Assistant: 6144 MB", summary["message"])
        self.assertIn("Disk space for Home Assistant: 64 GB", summary["message"])
        self.assertIn("first boot can take up to 25 minutes", summary["message"])
        self.assertIn("Continue with these settings", summary["message"])

    def test_support_bundle_redacts_secrets(self):
        config = cfg.validate_and_normalize_config(
            {
                "ha_token": "super-secret-ha-token",
                "gemini_api_key": "secret-gemini-key",
                "pushover_user_key": "secret-push-user",
                "pushover_api_token": "secret-push-token",
                "mqtt_password": "secret-mqtt-password",
                "rtsp_front": "rtsp://user:secret-rtsp-password@192.168.1.50:8554/front_live",
                "front_camera_id": "ring-front-camera-id",
                "ring_topic_root": "ring-location-id",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = diagnostics.create_support_bundle(
                config,
                setup_summary="Setup summary with token=super-secret-ha-token",
                setup_events=[{"event": "entity_discovery_finish", "message": "done", "ha_token": "super-secret-ha-token"}],
                last_setup_status="Last status with gemini_api_key=secret-gemini-key",
                output_dir=tmp,
            )
            with zipfile.ZipFile(result["path"], "r") as zf:
                names = zf.namelist()
                combined = "\n".join(zf.read(name).decode("utf-8", errors="ignore") for name in zf.namelist())

        self.assertNotIn("super-secret-ha-token", combined)
        self.assertNotIn("secret-gemini-key", combined)
        self.assertNotIn("secret-push-user", combined)
        self.assertNotIn("secret-push-token", combined)
        self.assertNotIn("secret-mqtt-password", combined)
        self.assertNotIn("secret-rtsp-password", combined)
        self.assertNotIn("ring-front-camera-id", combined)
        self.assertIn("setup/setup_summary.txt", names)
        self.assertIn("setup/setup_events.json", names)
        self.assertIn("setup/last_setup_status.txt", names)
        self.assertIn("[REDACTED]", combined)

    def test_support_email_draft_is_addressed_and_redacted(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        url = main.ViperDashboard._support_email_url(fake, r"C:\temp\viper_support_bundle.zip")

        self.assertIn("mailto:ckadlik%40gmail.com", url.replace("@", "%40"))
        self.assertIn("Viper%20Vision%20Support%20Report", url)
        self.assertIn("viper_support_bundle.zip", url)
        self.assertNotIn("token", url.lower())

    def test_setup_event_logging_redacts_secrets(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.setup_events = []
        fake.last_setup_status = ""
        main.ViperDashboard.record_setup_event(
            fake,
            "entity_discovery_finish",
            "token=secret-token-value",
            ha_token="secret-token-value",
            entity_count=12,
        )

        event = fake.setup_events[-1]
        self.assertEqual(event["event"], "entity_discovery_finish")
        self.assertEqual(event["entity_count"], 12)
        self.assertNotIn("secret-token-value", str(event))
        self.assertIn("[REDACTED]", str(event))

    def test_installer_metadata_is_v12_and_help_is_packaged(self):
        root = Path(__file__).resolve().parents[1]
        iss = (root / "ViperVision.iss").read_text(encoding="utf-8")
        spec = (root / "ViperVision.spec").read_text(encoding="utf-8")
        self.assertIn('#define MyAppVersion "1.2.3"', iss)
        self.assertIn('OutputBaseFilename=ViperVision-v{#MyAppVersion}-Setup', iss)
        self.assertIn('("help", "help")', spec)
        self.assertIn('("watch_ha_health.ps1", ".")', spec)

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

    def test_help_docs_match_current_product_navigation(self):
        root = Path(__file__).resolve().parents[1]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in (root / "help").glob("*.html"))
        self.assertIn("Home Assistant tab", combined)
        self.assertIn("Doorbell Vision", combined)
        self.assertIn("Speakers and Audio", combined)
        self.assertIn("Home Devices", combined)
        self.assertIn("Diagnostics", combined)
        self.assertIn("About Viper Vision And Data Folders", combined)
        self.assertNotIn("Open Utilities, then Home Assistant Setup", combined)
        self.assertNotIn("Viper does not install Home Assistant, VirtualBox, Mosquitto, or ring-mqtt automatically.", combined)

    def test_setup_wizard_is_documented_and_available(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        setup_help = (root / "help" / "setup.html").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("class ViperSetupWizardDialog", main_text)
        self.assertIn("Open Setup Wizard", main_text)
        self.assertIn("Test Everything", main_text)
        self.assertEqual(main_text.count("Test Front Camera Now"), 3)
        self.assertEqual(main_text.count("Test Back Camera Now"), 3)
        self.assertNotIn('label="Test Front Camera"', main_text)
        self.assertNotIn('label="Test Back Camera"', main_text)
        self.assertIn("Change Doorbell Triggers", main_text)
        self.assertIn("Change Camera Streams", main_text)
        self.assertIn("Front selected trigger", main_text)
        self.assertIn("Front live stream", main_text)
        self.assertNotIn("camera candidate:", main_text)
        self.assertNotIn("possible Home Assistant camera entity", main_text)
        self.assertIn("Continue To {next_title}", main_text)
        self.assertIn("self.btn_next.Show(next_available)", main_text)
        self.assertIn("def _page_completion_status", main_text)
        self.assertIn("def _configured_doorbell_trigger_count", main_text)
        self.assertIn('label="Save Selected Doorbell Triggers"', main_text)
        self.assertIn("self.wizard_front_trigger_choice = wx.ComboBox", main_text)
        self.assertIn("self.wizard_back_trigger_choice = wx.ComboBox", main_text)
        self.assertIn("def on_save_wizard_doorbell_triggers", main_text)
        self.assertIn('label="Save Selected Camera Streams"', main_text)
        self.assertIn("self.wizard_front_stream_choice = wx.ComboBox", main_text)
        self.assertIn("self.wizard_back_stream_choice = wx.ComboBox", main_text)
        self.assertIn("def on_save_wizard_camera_streams", main_text)
        self.assertIn("Do not use a camera stream for this door", main_text)
        self.assertIn('label="Test Front Doorbell Camera"', main_text)
        self.assertIn('label="Test Back Doorbell Camera"', main_text)
        self.assertIn('label="Test Checked Speakers"', main_text)
        self.assertIn("def on_test_wizard_camera", main_text)
        self.assertIn("def on_test_wizard_speakers", main_text)
        wizard_text = main_text.split("class ViperSetupWizardDialog", 1)[1].split("class ViperDashboard", 1)[0]
        self.assertNotIn("Advanced Manual Setup", wizard_text)
        self.assertNotIn("btn_advanced", wizard_text)
        self.assertNotIn("show_home_assistant_setup", wizard_text)
        self.assertIn('label="Check This PC And Home Assistant"', wizard_text)
        self.assertIn('label="Install VirtualBox"', wizard_text)
        self.assertIn('label="Install Home Assistant"', wizard_text)
        self.assertIn('label="Start Or Wait For Home Assistant"', wizard_text)
        self.assertIn('label="Open Home Assistant Account Setup"', wizard_text)
        self.assertIn('label="Open Home Assistant Token Page"', wizard_text)
        self.assertIn("Home Assistant IP or host", main_text)
        self.assertIn("Home Assistant long-lived access token", main_text)
        self.assertIn("def on_find_home_assistant", main_text)
        self.assertIn("Connect And Discover Devices", main_text)
        self.assertIn("Home Assistant install is part of this wizard now", main_text)
        self.assertNotIn("owner.show_new_user_setup_assistant()", wizard_text)
        self.assertIn("Recommended: Follow The Setup Wizard", setup_help)
        self.assertIn("The Continue button for the next page only appears after the current step is ready", setup_help)
        self.assertIn("The normal Ring integration in Home Assistant provides these trigger entities", setup_help)
        self.assertIn("The **Home Assistant** tab is the recommended beginner path", readme)

    def test_first_run_assistant_continue_returns_to_setup_wizard_not_advanced_dialog(self):
        class FakeOwner:
            def __init__(self):
                self._ha_server_assistant_dialog = None
                self.opened_wizard = 0
                self.opened_advanced = 0

            def show_initial_setup_assistant(self):
                self.opened_wizard += 1

            def show_home_assistant_setup(self, *args, **kwargs):
                self.opened_advanced += 1

        fake = main.HomeAssistantFirstRunAssistantDialog.__new__(main.HomeAssistantFirstRunAssistantDialog)
        owner = FakeOwner()
        owner._ha_server_assistant_dialog = fake
        fake.parent = owner
        fake._destroyed = False
        fake.destroyed = False
        fake.Destroy = lambda: setattr(fake, "destroyed", True)

        with patch.object(main.wx, "CallAfter", lambda func, *args, **kwargs: func(*args, **kwargs)):
            main.HomeAssistantFirstRunAssistantDialog.on_continue(fake, None)

        self.assertEqual(owner.opened_wizard, 1)
        self.assertEqual(owner.opened_advanced, 0)
        self.assertTrue(fake.destroyed)

    def test_setup_wizard_install_home_assistant_stays_in_wizard(self):
        class FakeButton:
            def __init__(self):
                self.enabled = True
                self.label = "Check This PC And Home Assistant"
                self.name = self.label
                self.focused = False

            def Enable(self, value):
                self.enabled = bool(value)

            def SetLabel(self, value):
                self.label = value

            def SetName(self, value):
                self.name = value

            def SetFocusFromKbd(self):
                self.focused = True

        class FakeOwner:
            def __init__(self):
                self._setup_wizard_dialog = None
                self.opened = 0

            def show_new_user_setup_assistant(self):
                self.opened += 1

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        owner = FakeOwner()
        owner._setup_wizard_dialog = fake
        fake.parent = owner
        fake.PAGES = main.ViperSetupWizardDialog.PAGES
        fake.page_index = 0
        fake.btn_wizard_check_pc = FakeButton()
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)
        fake._render = lambda: None

        main.ViperSetupWizardDialog.on_install_home_assistant(fake, None)

        self.assertEqual(owner.opened, 0)
        self.assertEqual(fake.PAGES[fake.page_index]["action"], "ha_connect")
        self.assertTrue(fake.btn_wizard_check_pc.focused)
        self.assertIn("Home Assistant install is part of this wizard now", fake.status[-1])

    def test_advanced_setup_install_home_assistant_button_opens_assistant(self):
        class FakeButton:
            def __init__(self):
                self.enabled = True
                self.label = "Install Home Assistant On This PC"
                self.name = self.label

            def Enable(self, value):
                self.enabled = bool(value)

            def SetLabel(self, value):
                self.label = value

            def SetName(self, value):
                self.name = value

        class FakeOwner:
            def __init__(self):
                self._ha_setup_dialog = None
                self.opened = 0

            def show_new_user_setup_assistant(self):
                self.opened += 1

        fake = main.HomeAssistantSetupDialog.__new__(main.HomeAssistantSetupDialog)
        owner = FakeOwner()
        owner._ha_setup_dialog = fake
        fake.parent = owner
        fake.btn_install_ha = FakeButton()
        fake.events = []
        fake.hidden = False
        fake.destroyed = False
        fake._record_setup_event = lambda event, message, **details: fake.events.append((event, message))
        fake._set_setup_status = lambda message, announce=False: fake.events.append(("status", message))
        fake.Hide = lambda: setattr(fake, "hidden", True)
        fake.Show = lambda value=True: setattr(fake, "hidden", not value)
        fake.Destroy = lambda: setattr(fake, "destroyed", True)

        with patch.object(main.wx, "CallLater", lambda _ms, func, *args, **kwargs: func(*args, **kwargs)):
            main.HomeAssistantSetupDialog.on_install_home_assistant_from_setup(fake, None)

        self.assertEqual(owner.opened, 1)
        self.assertIsNone(owner._ha_setup_dialog)
        self.assertTrue(fake.hidden)
        self.assertTrue(fake.destroyed)
        self.assertEqual(fake.btn_install_ha.label, "Opening Home Assistant Installer")
        self.assertFalse(fake.btn_install_ha.enabled)

    def test_setup_wizard_critical_buttons_are_bound_to_handlers(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        required_bindings = [
            "self.btn_find_ha_wizard.Bind(wx.EVT_BUTTON, self.on_find_home_assistant)",
            "self.btn_wizard_check_pc.Bind(wx.EVT_BUTTON, self.on_wizard_check_pc)",
            "self.btn_wizard_install_vbox.Bind(wx.EVT_BUTTON, self.on_wizard_install_virtualbox)",
            "self.btn_wizard_install_ha.Bind(wx.EVT_BUTTON, self.on_wizard_install_home_assistant_vm)",
            "self.btn_wizard_start_ha.Bind(wx.EVT_BUTTON, self.on_wizard_start_home_assistant_vm)",
            "self.btn_wizard_open_ha.Bind(wx.EVT_BUTTON, self.on_wizard_open_home_assistant)",
            "self.btn_wizard_open_token.Bind(wx.EVT_BUTTON, self.on_wizard_open_token_page)",
            "self.btn_save_wizard_triggers.Bind(wx.EVT_BUTTON, self.on_save_wizard_doorbell_triggers)",
            "self.btn_save_wizard_streams.Bind(wx.EVT_BUTTON, self.on_save_wizard_camera_streams)",
            'self.btn_test_wizard_front_camera.Bind(wx.EVT_BUTTON, lambda event: self.on_test_wizard_camera(event, "front"))',
            'self.btn_test_wizard_back_camera.Bind(wx.EVT_BUTTON, lambda event: self.on_test_wizard_camera(event, "back"))',
            "self.btn_test_wizard_speakers.Bind(wx.EVT_BUTTON, self.on_test_wizard_speakers)",
            "self.btn_action.Bind(wx.EVT_BUTTON, self.on_action)",
            "self.btn_next.Bind(wx.EVT_BUTTON, self.on_next)",
            "self.btn_save_wizard_speakers.Bind(wx.EVT_BUTTON, self.on_save_wizard_speakers)",
        ]
        for binding in required_bindings:
            self.assertIn(binding, main_text)
        self.assertIn("def _start_direct_home_assistant_setup", main_text)
        self.assertIn("def _start_wizard_ring_mqtt_setup", main_text)
        self.assertIn("def _start_wizard_live_stream_discovery", main_text)
        self.assertIn("def _start_wizard_speaker_discovery", main_text)

    def test_setup_wizard_saves_user_selected_doorbell_triggers(self):
        class Choice:
            def __init__(self, selection):
                self.selection = selection

            def GetSelection(self):
                return self.selection

        class Parent:
            def __init__(self):
                self.config = {
                    "rtsp_front": "rtsp://ha:8554/front",
                    "rtsp_back": "rtsp://ha:8554/back",
                    "doorbell_triggers": {
                        "front": {"rtsp_url": "rtsp://ha:8554/front"},
                        "back": {"rtsp_url": "rtsp://ha:8554/back"},
                    },
                }
                self.saved = 0
                self.refreshed = 0

            def save_config(self):
                self.saved += 1

            def refresh_setup_checklist(self):
                self.refreshed += 1

            def build_setup_checklist_summary(self):
                return "summary"

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._wizard_doorbell_trigger_choices = [
            {"entity_id": "binary_sensor.front_door_ding"},
            {"entity_id": "event.back_door_ding"},
        ]
        fake.wizard_front_trigger_choice = Choice(0)
        fake.wizard_back_trigger_choice = Choice(1)
        fake._session_completed_actions = set()
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)
        fake._render = lambda: None

        main.ViperSetupWizardDialog.on_save_wizard_doorbell_triggers(fake, None)

        triggers = fake.parent.config["doorbell_triggers"]
        self.assertEqual(triggers["front"]["trigger_entity_id"], "binary_sensor.front_door_ding")
        self.assertEqual(triggers["back"]["trigger_entity_id"], "event.back_door_ding")
        self.assertTrue(triggers["front"]["enabled"])
        self.assertTrue(triggers["back"]["enabled"])
        self.assertEqual(fake.parent.saved, 1)
        self.assertEqual(fake.parent.refreshed, 1)
        self.assertIn("doorbells", fake._session_completed_actions)

    def test_setup_wizard_rejects_duplicate_doorbell_trigger_selection(self):
        class Choice:
            def GetSelection(self):
                return 0

        class Parent:
            config = {"doorbell_triggers": {}}

            def build_setup_checklist_summary(self):
                return "summary"

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._wizard_doorbell_trigger_choices = [{"entity_id": "binary_sensor.same_ding"}]
        fake.wizard_front_trigger_choice = Choice()
        fake.wizard_back_trigger_choice = Choice()
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)

        main.ViperSetupWizardDialog.on_save_wizard_doorbell_triggers(fake, None)

        self.assertIn("cannot be the same entity", fake.status[-1])

    def test_setup_wizard_saves_user_selected_camera_streams(self):
        class Choice:
            def __init__(self, selection):
                self.selection = selection

            def GetSelection(self):
                return self.selection

        class Parent:
            def __init__(self):
                self.config = {
                    "doorbell_triggers": {
                        "front": {"trigger_entity_id": "binary_sensor.front_door_ding"},
                        "back": {"trigger_entity_id": "binary_sensor.back_door_ding"},
                    }
                }
                self.saved = 0
                self.refreshed = 0

            def save_config(self):
                self.saved += 1

            def refresh_setup_checklist(self):
                self.refreshed += 1

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._wizard_stream_choices = [
            None,
            {
                "name": "front_live",
                "rtsp_url": "rtsp://ha:8554/front_live",
                "stream": {"camera_id": "front123", "topic": "ring/location/camera/front123/info/state"},
            },
            {
                "name": "back_live",
                "rtsp_url": "rtsp://ha:8554/back_live",
                "stream": {"camera_id": "back456", "topic": "ring/location/camera/back456/info/state"},
            },
        ]
        fake.wizard_front_stream_choice = Choice(1)
        fake.wizard_back_stream_choice = Choice(2)
        fake._session_completed_actions = set()
        fake._wizard_saved_stream_urls = set()
        fake._wizard_camera_test_status = {}
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)
        fake._render = lambda: None

        main.ViperSetupWizardDialog.on_save_wizard_camera_streams(fake, None)

        self.assertEqual(fake.parent.config["rtsp_front"], "rtsp://ha:8554/front_live")
        self.assertEqual(fake.parent.config["rtsp_back"], "rtsp://ha:8554/back_live")
        self.assertEqual(fake.parent.config["doorbell_triggers"]["front"]["rtsp_url"], "rtsp://ha:8554/front_live")
        self.assertEqual(fake.parent.config["doorbell_triggers"]["back"]["rtsp_url"], "rtsp://ha:8554/back_live")
        self.assertEqual(fake.parent.config["doorbell_triggers"]["front"]["camera_id"], "front123")
        self.assertEqual(fake.parent.config["doorbell_triggers"]["back"]["camera_id"], "back456")
        self.assertEqual(fake.parent.saved, 1)
        self.assertEqual(fake.parent.refreshed, 1)
        self.assertIn("live_streams", fake._session_completed_actions)

    def test_setup_wizard_rejects_duplicate_camera_stream_selection(self):
        class Choice:
            def GetSelection(self):
                return 1

        class Parent:
            config = {"doorbell_triggers": {}}

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._wizard_stream_choices = [
            None,
            {"name": "same_live", "rtsp_url": "rtsp://ha:8554/same_live", "stream": {}},
        ]
        fake.wizard_front_stream_choice = Choice()
        fake.wizard_back_stream_choice = Choice()
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)

        main.ViperSetupWizardDialog.on_save_wizard_camera_streams(fake, None)

        self.assertIn("cannot be the same stream", fake.status[-1])

    def test_setup_wizard_camera_test_reports_passed_stream(self):
        class Parent:
            config = {
                "rtsp_front": "rtsp://ha:8554/front_live",
                "doorbell_triggers": {"front": {"rtsp_url": "rtsp://ha:8554/front_live"}},
            }

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._wizard_camera_test_status = {}
        fake._wizard_saved_stream_urls = set()
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)
        fake._render = lambda: None
        fake._set_busy = lambda busy: None
        fake._stream_name_from_rtsp_url = lambda url: "front_live"

        main.ViperSetupWizardDialog._finish_wizard_camera_test(
            fake,
            "front",
            {"ok": True, "rtsp_url": "rtsp://ha:8554/front_live", "frame": r"C:\frame.jpg", "elapsed": 1.2},
        )

        self.assertIn("Front doorbell camera test passed", fake.status[-1])
        self.assertIn("saved and tested successfully", fake.status[-1])
        self.assertIn("rtsp://ha:8554/front_live", fake._wizard_saved_stream_urls)

    def test_setup_wizard_tests_checked_speakers(self):
        class Check:
            def __init__(self, value=True):
                self._value = value
                self._target = {"name": "Kitchen", "id": "media_player.kitchen", "type": "ha"}

            def IsEnabled(self):
                return True

            def GetValue(self):
                return self._value

            @property
            def _viper_speaker_target(self):
                return self._target

        class Parent:
            config = {"speakers": {}}

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._wizard_speaker_checks = [Check()]
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)
        fake.finished = []
        fake._finish_wizard_speaker_tests = lambda results, source: fake.finished.append((results, source))

        with patch.object(main, "safe_submit", lambda func, *args, **kwargs: func(*args, **kwargs)), \
             patch.object(main.wx, "CallAfter", lambda func, *args, **kwargs: func(*args, **kwargs)), \
             patch.object(main.audio, "announce_specific_speaker") as announce:
            main.ViperSetupWizardDialog.on_test_wizard_speakers(fake, None)

        announce.assert_called_once_with("ha", "media_player.kitchen", "Viper speaker setup test.")
        self.assertEqual(fake.finished[0][1], "checked")

    def test_setup_wizard_speaker_test_finish_marks_complete_when_routes_saved(self):
        class Parent:
            config = {
                "speakers": {
                    "Kitchen": {
                        "enabled": True,
                        "doorbell": True,
                        "utilities": True,
                        "fridge": True,
                    }
                }
            }

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake._session_completed_actions = set()
        fake.status = []
        fake._set_step_status = lambda message, announce=False: fake.status.append(message)
        fake._render = lambda: None

        main.ViperSetupWizardDialog._finish_wizard_speaker_tests(
            fake,
            [{"name": "Kitchen", "ok": True, "message": "Test announcement sent."}],
            "saved",
        )

        self.assertIn("Speaker test finished", fake.status[-1])
        self.assertIn("speakers_voice", fake._session_completed_actions)

    def test_home_assistant_install_assistant_buttons_are_bound_to_handlers(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        required_buttons = [
            '("Check This PC", self.on_check_pc',
            '("Install VirtualBox With Winget", self.on_install_virtualbox_winget',
            '("Optimize Windows For VirtualBox", self.on_optimize_windows_virtualbox',
            '("Download And Install Home Assistant VM", self.on_download_install_ha_vm',
            '("Choose Downloaded HA OS Image", self.on_choose_haos_image',
            '("Start Home Assistant VM", self.on_start_ha_vm',
            '("Find Home Assistant", self.on_find_ha',
            '("Open Home Assistant", self.on_open_found_ha',
            '("Continue To Viper Setup", self.on_continue',
        ]
        for button in required_buttons:
            self.assertIn(button, main_text)

    def test_home_assistant_setup_includes_safe_virtualbox_optimization_controls(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        ha_vm_text = (root / "viper_ha_vm.py").read_text(encoding="utf-8")
        self.assertIn("def optimize_windows_for_virtualbox", ha_vm_text)
        self.assertIn("def optimize_windows_for_virtualbox", main_text)
        self.assertIn("def on_wizard_optimize_windows_virtualbox", main_text)
        self.assertIn("def on_optimize_windows_virtualbox", main_text)
        self.assertIn("WSL2, Docker Desktop, Windows Sandbox", main_text)
        self.assertIn("WSL2, Docker Desktop, Windows Sandbox", ha_vm_text)
        self.assertIn("wx.MessageDialog", main_text)
        self.assertIn("is_windows_admin()", ha_vm_text)

    def test_home_assistant_address_recovery_updates_saved_ip(self):
        class FakeParent:
            def __init__(self):
                self.config = {
                    "ha_ip": "192.168.4.49",
                    "ha_port": "8123",
                    "ha_token": "token",
                }
                self.saved = 0

            def save_config(self):
                self.saved += 1

        fake = FakeParent()
        with mock.patch.object(main.cfg, "get_ha_settings", return_value={"ha_ip": "192.168.4.49", "ha_port": "8123", "ha_token": "token"}), \
             mock.patch.object(main.discovery, "check_ha_core_health", return_value={"ok": False, "message": "timeout"}), \
             mock.patch.object(main.discovery, "find_home_assistant", return_value={"ok": True, "ha_ip": "192.168.4.50", "ha_port": "8123", "auth_ok": True}), \
             mock.patch.object(main.cfg, "sync_globals_from_config"):
            result = main.ViperDashboard.check_and_repair_home_assistant_address(fake)

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(fake.config["ha_ip"], "192.168.4.50")
        self.assertEqual(fake.saved, 1)

    def test_ring_mqtt_login_find_streams_uses_setup_wizard_handler(self):
        class FakeParent:
            def __init__(self):
                self.discovery_started = 0
                self.legacy_started = 0

            def _start_wizard_live_stream_discovery(self):
                self.discovery_started += 1

            def on_find_live_rtsp_streams(self, event):
                self.legacy_started += 1

        class FakeText:
            def __init__(self):
                self.value = ""

            def SetValue(self, value):
                self.value = value

        fake = main.RingMqttLoginDialog.__new__(main.RingMqttLoginDialog)
        fake.parent = FakeParent()
        fake.status_txt = FakeText()

        main.RingMqttLoginDialog.on_find_streams(fake, None)

        self.assertEqual(fake.parent.discovery_started, 1)
        self.assertEqual(fake.parent.legacy_started, 0)
        self.assertIn("finding and testing doorbell cameras", fake.status_txt.value)

    def test_ring_mqtt_login_find_streams_uses_advanced_setup_fallback(self):
        class FakeParent:
            def __init__(self):
                self.legacy_started = 0

            def on_find_live_rtsp_streams(self, event):
                self.legacy_started += 1

        class FakeText:
            def __init__(self):
                self.value = ""

            def SetValue(self, value):
                self.value = value

        fake = main.RingMqttLoginDialog.__new__(main.RingMqttLoginDialog)
        fake.parent = FakeParent()
        fake.status_txt = FakeText()

        main.RingMqttLoginDialog.on_find_streams(fake, None)

        self.assertEqual(fake.parent.legacy_started, 1)
        self.assertIn("Ring-MQTT streams", fake.status_txt.value)

    def test_setup_wizard_page_order_matches_product_areas(self):
        titles = [page["title"] for page in main.ViperSetupWizardDialog.PAGES]
        self.assertEqual(
            titles,
            [
                "Welcome",
                "Home Assistant Connection",
                "Ring In Home Assistant",
                "Ring-MQTT Live Video",
                "Test Doorbell Cameras",
                "Confirm Doorbell Triggers",
                "Speakers And Audio",
                "AI And Speech",
                "Final Test",
                "Finish And Optional Devices",
            ],
        )
        actions = [page["action"] for page in main.ViperSetupWizardDialog.PAGES]
        self.assertIn("ring_integration", actions)
        self.assertIn("live_streams", actions)
        self.assertIn("tts", actions)
        self.assertNotIn("refrigerator_ice", actions)
        self.assertNotIn("robot_vacuum", actions)
        self.assertNotIn("ha_server", actions)

    def test_setup_wizard_resumes_to_final_test_when_core_ready(self):
        class Parent:
            def __init__(self):
                self.config = cfg.validate_and_normalize_config({
                    "ha_ip": "192.168.1.10",
                    "ha_token": "token",
                    "gemini_api_key": "gemini",
                    "rtsp_front": "rtsp://front",
                    "rtsp_back": "rtsp://back",
                    "doorbell_triggers": {
                        "front": {"trigger_entity_id": "binary_sensor.front", "rtsp_url": "rtsp://front"},
                        "back": {"trigger_entity_id": "binary_sensor.back", "rtsp_url": "rtsp://back"},
                    },
                    "speakers": {
                        "Kitchen": {"enabled": True, "doorbell": True, "utilities": True, "fridge": True}
                    },
                })

            def suggested_setup_page(self):
                return main.ViperDashboard.suggested_setup_page(self)

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake.page_index = 0
        fake.PAGES = main.ViperSetupWizardDialog.PAGES

        main.ViperSetupWizardDialog._apply_initial_resume_position(fake, fake.parent.suggested_setup_page())

        self.assertEqual(fake.PAGES[fake.page_index]["action"], "test")

    def test_setup_wizard_optional_buttons_show_mini_wizard_steps(self):
        class Parent:
            config = {}

            def build_setup_checklist_summary(self):
                return "summary"

        fake = main.ViperSetupWizardDialog.__new__(main.ViperSetupWizardDialog)
        fake.parent = Parent()
        fake.messages = []
        fake._open_product_area = lambda top, nested=None: fake.messages.append(f"opened {top} {nested}")
        fake.checklist_txt = type("Text", (), {"SetValue": lambda _self, value: fake.messages.append(value)})()

        main.ViperSetupWizardDialog.on_optional_fridge(fake, None)
        main.ViperSetupWizardDialog.on_optional_vacuum(fake, None)

        self.assertTrue(any("Mini-wizard: Refrigerator" in item for item in fake.messages))
        self.assertTrue(any("Mini-wizard: Robot vacuum" in item for item in fake.messages))

    def test_setup_confidence_summary_reports_ready_state(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config({
            "ha_ip": "192.168.1.10",
            "ha_token": "token",
            "gemini_api_key": "gemini",
            "rtsp_front": "rtsp://front",
            "rtsp_back": "rtsp://back",
            "doorbell_triggers": {
                "front": {"trigger_entity_id": "binary_sensor.front", "rtsp_url": "rtsp://front"},
                "back": {"trigger_entity_id": "binary_sensor.back", "rtsp_url": "rtsp://back"},
            },
            "speakers": {
                "Kitchen": {"enabled": True, "doorbell": True, "utilities": True, "fridge": True}
            },
        })
        fake.ha_listener = type("Listener", (), {"status": lambda _self: {"connected": True}})()

        text = main.ViperDashboard.build_setup_confidence_summary(fake)

        self.assertIn("Doorbell system ready: yes", text)
        self.assertIn("Recommended next: run Test Everything", text)

    def test_setup_status_command_center_points_to_first_fix_and_optional_cards(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config({
            "setup_skips": {"fridge": True},
            "speakers": {
                "Kitchen": {
                    "id": "media_player.kitchen",
                    "type": "ha",
                    "enabled": True,
                    "doorbell": True,
                    "utilities": True,
                    "fridge": True,
                }
            },
        })
        fake.ha_listener = type("Listener", (), {"status": lambda _self: {"connected": False}})()
        fake.last_setup_status = "Speaker test passed."

        next_action = main.ViperDashboard.build_setup_next_action_summary(fake)
        checklist = main.ViperDashboard.build_setup_checklist_summary(fake)

        self.assertIn("Next recommended action: Fix Home Assistant", next_action)
        self.assertIn("Last successful step: Speaker test passed.", next_action)
        self.assertIn("Fridge/freezer alerts: skipped", checklist)
        self.assertIn("Troubleshooting Recipes", checklist)

    def test_setup_status_reports_core_ready_and_exact_fix_pages(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config({
            "ha_ip": "192.168.1.10",
            "ha_token": "token",
            "setup_skips": {"gemini": True, "pushover": True, "fridge": True, "vacuum": True},
            "doorbell_triggers": {
                "front": {"trigger_entity_id": "binary_sensor.front", "rtsp_url": "rtsp://front"},
                "back": {"trigger_entity_id": "binary_sensor.back", "rtsp_url": "rtsp://back"},
            },
            "speakers": {
                "Kitchen": {"id": "media_player.kitchen", "type": "ha", "enabled": True, "doorbell": True, "utilities": True, "fridge": True}
            },
        })
        fake.ha_listener = type("Listener", (), {"status": lambda _self: {"connected": True}})()

        summary = main.ViperDashboard.build_setup_next_action_summary(fake)

        self.assertIn("Core setup is ready.", summary)
        self.assertIn("Recommended next: run Test Everything", summary)
        self.assertEqual(main.ViperDashboard._setup_page_for_issue(fake, "home_assistant"), "connect")
        self.assertEqual(main.ViperDashboard._setup_page_for_issue(fake, "doorbell_triggers"), "doorbells")
        self.assertEqual(main.ViperDashboard._setup_page_for_issue(fake, "live_video"), "live_streams")
        self.assertEqual(main.ViperDashboard._setup_page_for_issue(fake, "speakers"), "speakers")

    def test_restore_optional_setup_items_clears_skips(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config({"setup_skips": {"gemini": True, "fridge": True}})
        fake.events = []
        fake.messages = []
        fake.record_setup_event = lambda event, message="", **details: fake.events.append((event, details))
        fake._refresh_setup_status_controls = lambda: fake.events.append(("refresh", {}))
        fake.notify = lambda message, priority=0, speak=False: fake.messages.append(message)

        with patch.object(cfg, "save_config") as save_config:
            main.ViperDashboard.on_restore_optional_setup_items(fake, None)

        save_config.assert_called_once()
        self.assertFalse(any(fake.config["setup_skips"].values()))
        self.assertTrue(any(event[0] == "optional_setup_restored" for event in fake.events))

    def test_setup_backup_omits_secrets(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config({
            "ha_ip": "192.168.4.49",
            "ha_token": "super-secret-token",
            "gemini_api_key": "gemini-secret",
            "speakers": {
                "Kitchen": {"id": "media_player.kitchen", "type": "ha", "enabled": True}
            },
        })

        backup = main.ViperDashboard._non_secret_setup_backup(fake)

        self.assertEqual(backup["config"]["ha_token"], "")
        self.assertEqual(backup["config"]["gemini_api_key"], "")
        self.assertEqual(backup["config"]["ha_ip"], "192.168.4.49")

    def test_test_everything_appends_safe_smoke_report(self):
        fake = main.ViperDashboard.__new__(main.ViperDashboard)
        fake.config = cfg.validate_and_normalize_config({})
        fake.build_setup_checklist_summary = lambda live_result=None: "Checklist"
        fake._collect_safe_smoke_results = lambda: [("Config file", True, "ok", "")]
        fake._format_safe_smoke_report = lambda results: "Smoke Test: PASS"
        fake.finished = []

        with patch.object(main.wx, "CallAfter", lambda func, *args, **kwargs: fake.finished.append(args[0])):
            main.ViperDashboard._run_test_everything(fake)

        self.assertIn("Checklist", fake.finished[0])
        self.assertIn("Smoke Test: PASS", fake.finished[0])

    def test_advanced_home_assistant_setup_uses_product_page_names(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")

        self.assertIn('title="Advanced Home Assistant Setup"', main_text)
        self.assertIn('"Home Assistant", "Doorbell Vision", "Ring-MQTT Advanced", "Final Checks"', main_text)
        self.assertIn('notebook.AddPage(connect_page, "Home Assistant")', main_text)
        self.assertIn('notebook.AddPage(doorbell_page, "Doorbell Vision")', main_text)
        self.assertIn('notebook.AddPage(ring_page, "Ring-MQTT Advanced")', main_text)
        self.assertIn('notebook.AddPage(finish_page, "Final Checks")', main_text)
        self.assertIn("Home Assistant camera snapshot entities are not live video streams", main_text)
        self.assertIn("Run Beginner Auto Setup", main_text)

    def test_release_checklist_exists(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        self.assertIn(".\\run_tests.ps1", text)
        self.assertIn(".\\build_installer.ps1", text)
        self.assertIn(".\\smoke_installer.ps1", text)
        self.assertIn("Create Support Bundle", text)
        self.assertIn("Fresh-PC Screen Reader Acceptance Script", text)
        self.assertIn("About Viper Vision And Data Folders", text)
        self.assertNotIn("Viper does not install Home Assistant, VirtualBox, Mosquitto, or ring-mqtt automatically.", text)

    def test_about_dialog_is_available_from_diagnostics(self):
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.pyw").read_text(encoding="utf-8")
        self.assertIn("About Viper Vision And Data Folders", main_text)
        self.assertIn("def on_show_about", main_text)
        self.assertIn("Copy Data Folder", main_text)
        self.assertIn("Open Remote", main_text)
        self.assertIn("Support bundles redact Home Assistant tokens", main_text)

    def test_smoke_installer_script_exists(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "smoke_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("ViperVision-v1.2.3-Setup.exe", text)
        self.assertIn("VIPER_CLEAN_FIRST_RUN_TEST", text)
        self.assertIn("ffmpeg.exe", text)

    def test_home_assistant_stability_scripts_exist(self):
        root = Path(__file__).resolve().parents[1]
        watch = (root / "watch_ha_health.ps1").read_text(encoding="utf-8")
        harden = (root / "harden_ha_virtualbox.ps1").read_text(encoding="utf-8")

        self.assertIn("core_hung_vm_alive", watch)
        self.assertIn("4357", watch)
        self.assertIn("Refusing to harden while the VM is", harden)
        self.assertIn("--hostiocache on", harden)
        self.assertIn("UseVirtioNet", harden)
        self.assertIn("Windows Hypervisor/Hyper-V", harden)


if __name__ == "__main__":
    unittest.main()
