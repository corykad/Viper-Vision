import json
import logging
from datetime import datetime
from pathlib import Path

import wx

import viper_config as cfg
import viper_diagnostics as diagnostics
import viper_discovery as discovery
import viper_matter
import viper_ui_common as ui_common
import viper_vision as vision
from viper_ha_vm import _coerce_setup_progress_state
from viper_runtime import safe_submit
from viper_ui_setup_wizard import ViperSetupWizardDialog

AccessibleStatusText = ui_common.AccessibleStatusText


class SetupStatusMixin:
    def setup_setup_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = AccessibleStatusText(
            self.tab_setup,
            value=(
                "Setup is the guided place for getting Viper working.\n\n"
                "Use Open Setup Wizard first. It walks through Home Assistant, Ring, live video, speakers, AI speech, and final testing in order. Refrigerator and robot vacuum setup come after the core doorbell path works."
            ),
            size=(-1, 90),
        )
        self._describe_control(intro, "Setup introduction. Overview of the guided setup area.")
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        command_box = wx.StaticBox(self.tab_setup, label="Setup Status")
        command_sizer = wx.StaticBoxSizer(command_box, wx.VERTICAL)
        self.setup_next_action_txt = AccessibleStatusText(
            self.tab_setup,
            value=self.build_setup_next_action_summary(),
            size=(-1, 90),
        )
        self._describe_control(self.setup_next_action_txt, "Setup Status. Current readiness, last successful step, skipped optional features, and the next recommended action.")
        command_sizer.Add(self.setup_next_action_txt, 0, wx.ALL | wx.EXPAND, 5)

        command_grid = wx.FlexGridSizer(rows=0, cols=3, vgap=6, hgap=6)
        for col in range(3):
            command_grid.AddGrowableCol(col, 1)
        self.btn_continue_setup = wx.Button(self.tab_setup, label="Continue Setup", size=(-1, 40))
        self.btn_fix_current_setup = wx.Button(self.tab_setup, label="Fix Current Item", size=(-1, 40))
        self.btn_test_current_setup = wx.Button(self.tab_setup, label="Test Current Item", size=(-1, 40))
        self.btn_skip_optional_setup = wx.Button(self.tab_setup, label="Skip Optional Item", size=(-1, 40))
        self.btn_unskip_optional_setup = wx.Button(self.tab_setup, label="Restore Optional Items", size=(-1, 40))
        self.btn_backup_setup = wx.Button(self.tab_setup, label="Backup Setup", size=(-1, 40))
        self.btn_restore_setup = wx.Button(self.tab_setup, label="Restore Setup", size=(-1, 40))
        self.btn_continue_setup.Bind(wx.EVT_BUTTON, self.on_continue_setup)
        self.btn_fix_current_setup.Bind(wx.EVT_BUTTON, self.on_fix_current_setup_item)
        self.btn_test_current_setup.Bind(wx.EVT_BUTTON, self.on_test_current_setup_item)
        self.btn_skip_optional_setup.Bind(wx.EVT_BUTTON, self.on_skip_optional_setup_item)
        self.btn_unskip_optional_setup.Bind(wx.EVT_BUTTON, self.on_restore_optional_setup_items)
        self.btn_backup_setup.Bind(wx.EVT_BUTTON, self.on_backup_setup)
        self.btn_restore_setup.Bind(wx.EVT_BUTTON, self.on_restore_setup)
        for button, description in {
            self.btn_continue_setup: "Continue Setup button. Opens the guided setup wizard at the next recommended step.",
            self.btn_fix_current_setup: "Fix Current Item button. Opens the exact setup area for the first item that needs attention.",
            self.btn_test_current_setup: "Test Current Item button. Runs the safest relevant test for the current setup item.",
            self.btn_skip_optional_setup: "Skip Optional Item button. Marks the current optional feature as skipped for now.",
            self.btn_unskip_optional_setup: "Restore Optional Items button. Makes previously skipped optional setup items show as available again.",
            self.btn_backup_setup: "Backup Setup button. Exports non-secret setup settings to the Viper data folder.",
            self.btn_restore_setup: "Restore Setup button. Imports a setup backup and keeps secrets to be re-entered separately.",
        }.items():
            self._describe_control(button, description)
            command_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        command_sizer.Add(command_grid, 0, wx.EXPAND)

        recipe_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        recipe_grid.AddGrowableCol(0, 1)
        recipe_grid.AddGrowableCol(1, 1)
        self.btn_recipe_ha_token = wx.Button(self.tab_setup, label="Fix HA Token", size=(-1, 36))
        self.btn_recipe_ring_streams = wx.Button(self.tab_setup, label="Fix Ring-MQTT Streams", size=(-1, 36))
        self.btn_recipe_speakers = wx.Button(self.tab_setup, label="Fix Speaker Audio", size=(-1, 36))
        self.btn_recipe_doorbell = wx.Button(self.tab_setup, label="Fix Doorbell Events", size=(-1, 36))
        self.btn_recipe_camera = wx.Button(self.tab_setup, label="Fix Camera Frames", size=(-1, 36))
        self.btn_recipe_gemini = wx.Button(self.tab_setup, label="Fix Gemini Replies", size=(-1, 36))
        for key, button in (
            ("ha_token", self.btn_recipe_ha_token),
            ("ring_streams", self.btn_recipe_ring_streams),
            ("speakers", self.btn_recipe_speakers),
            ("doorbell", self.btn_recipe_doorbell),
            ("camera", self.btn_recipe_camera),
            ("gemini", self.btn_recipe_gemini),
        ):
            button.Bind(wx.EVT_BUTTON, lambda event, recipe=key: self.on_show_troubleshooting_recipe(event, recipe))
            self._describe_control(button, f"Troubleshooting recipe button for {key.replace('_', ' ')}.")
            recipe_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        command_sizer.Add(recipe_grid, 0, wx.EXPAND)
        sizer.Add(command_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.setup_checklist_txt = wx.TextCtrl(
            self.tab_setup,
            value=self.build_setup_checklist_summary(),
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 340),
        )
        self._describe_control(
            self.setup_checklist_txt,
            "Setup checklist. Read-only status of Home Assistant, Ring-MQTT, RTSP, speakers, TTS, and diagnostics readiness.",
        )
        sizer.Add(self.setup_checklist_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        buttons = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        buttons.AddGrowableCol(0, 1)
        buttons.AddGrowableCol(1, 1)
        self.btn_setup_wizard = wx.Button(self.tab_setup, label="Open Setup Wizard", size=(-1, 44))
        self.btn_choose_setup_speakers = wx.Button(self.tab_setup, label="Choose Alert Speakers", size=(-1, 44))
        self.btn_setup_matter = wx.Button(self.tab_setup, label="Set Up Alexa And Google Controls", size=(-1, 44))
        self.btn_add_matter_fan = wx.Button(self.tab_setup, label="Add Alexa Ceiling Fan", size=(-1, 44))
        self.btn_refresh_setup_checklist = wx.Button(self.tab_setup, label="Refresh Setup Status", size=(-1, 44))
        self.btn_test_everything = wx.Button(self.tab_setup, label="Test Everything", size=(-1, 44))
        self.btn_setup_wizard.Bind(wx.EVT_BUTTON, self.on_open_setup_wizard)
        self.btn_choose_setup_speakers.Bind(wx.EVT_BUTTON, self.on_choose_setup_speakers)
        self.btn_setup_matter.Bind(wx.EVT_BUTTON, self.on_setup_matter_switches)
        self.btn_add_matter_fan.Bind(wx.EVT_BUTTON, self.on_add_matter_fan)
        self.btn_refresh_setup_checklist.Bind(wx.EVT_BUTTON, lambda event: self.refresh_setup_checklist())
        self.btn_test_everything.Bind(wx.EVT_BUTTON, self.on_test_everything)
        for button, description in {
            self.btn_setup_wizard: "Open Setup Wizard button. Opens the beginner setup wizard for Home Assistant, Ring, live video, speakers, AI speech, and final testing.",
            self.btn_choose_setup_speakers: "Choose Alert Speakers button. Opens speaker discovery or the speaker list so you can choose which speakers Viper uses.",
            self.btn_setup_matter: "Set Up Alexa And Google Controls button. Creates or checks Home Assistant controls for Viper arm, mute, speaker controls, and configured fan entities so Matterbridge can expose them to voice assistants.",
            self.btn_add_matter_fan: "Add Alexa Ceiling Fan button. Adds a Home Assistant fan entity ID to the Matterbridge allow list, then reruns Alexa and Google setup.",
            self.btn_refresh_setup_checklist: "Refresh Setup Checklist button. Updates the read-only checklist above.",
            self.btn_test_everything: "Test Everything button. Runs safe setup checks and diagnostics without changing settings.",
        }.items():
            self._describe_control(button, description)
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        self.tab_setup.SetSizer(sizer)
        self._update_main_setup_actions()

    def on_choose_setup_speakers(self, event):
        if not self.config.get("speakers"):
            self.on_discover_speakers(event)
            return
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(idx) is self.tab_audio_shell:
                self._select_book_page(self.notebook, idx)
                break
        if hasattr(self, "audio_notebook"):
            for idx in range(self.audio_notebook.GetPageCount()):
                if self.audio_notebook.GetPage(idx) is self.tab_dev:
                    self._select_book_page(self.audio_notebook, idx)
                    break
        wx.CallAfter(self.speaker_list.SetFocus)
        self.notify("Choose alert speakers. Use Spacebar to toggle each speaker, then choose routing for the selected speaker.", priority=10)

    def on_setup_matter_switches(self, event):
        self.notify("Setting up Alexa and Google controls...", priority=10)
        safe_submit(self._run_setup_matter_switches)

    def on_add_matter_fan(self, event):
        current = ", ".join(self.config.get("matter_fan_entities") or [])
        prompt = "Enter the Home Assistant fan entity ID, like fan.living_room_ceiling_fan."
        if current:
            prompt += f"\n\nAlready added: {current}"
        entity_id = wx.GetTextFromUser(prompt, "Add Alexa Ceiling Fan").strip().lower()
        if not entity_id:
            return
        if not entity_id.startswith("fan.") or "." not in entity_id:
            self.notify("That does not look like a Home Assistant fan entity. It should start with fan.", priority=10)
            return
        fan_entities = list(self.config.get("matter_fan_entities") or [])
        if entity_id not in fan_entities:
            fan_entities.append(entity_id)
            self.config["matter_fan_entities"] = fan_entities
            self.save_config()
        self.notify(f"Added {entity_id} for Alexa and Google. Updating Matterbridge now.", priority=10)
        safe_submit(self._run_setup_matter_switches)

    def _run_setup_matter_switches(self):
        try:
            report = viper_matter.setup_status_report(self.config)
            text = viper_matter.format_setup_report(report)
            wx.CallAfter(self._show_text_dialog, "Alexa And Google Switch Setup", text)
            install_ok = bool(report.get("install", {}).get("ok"))
            ha_ok = bool(report.get("ha", {}).get("ok"))
            if install_ok and ha_ok:
                wx.CallAfter(self.notify, "Alexa and Google controls are ready in Home Assistant. Pair Matterbridge or refresh Alexa and Google.", priority=10)
            elif install_ok:
                wx.CallAfter(self.notify, "Matter control package installed. Restart Home Assistant, then run this setup again.", priority=10)
            else:
                wx.CallAfter(self.notify, "Matter control setup needs manual package install. See the setup report.", priority=10)
        except Exception as e:
            logging.exception("Matter control setup failed")
            wx.CallAfter(self.notify, f"Alexa and Google control setup failed: {e}", priority=10)

    def on_fix_tts_setup(self, event):
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(idx) is self.tab_audio_shell:
                self._select_book_page(self.notebook, idx)
                break
        if hasattr(self, "audio_notebook"):
            for idx in range(self.audio_notebook.GetPageCount()):
                if self.audio_notebook.GetPage(idx) is self.tab_tts:
                    self._select_book_page(self.audio_notebook, idx)
                    break
        wx.CallAfter(self.tts_engine_choice.SetFocus)
        self.notify("Opened Gemini and TTS setup. Choose a default engine and enter the Gemini API key if you want Gemini vision or Gemini TTS.", priority=10)

    def _select_top_page(self, page):
        if not hasattr(self, "notebook") or page is None:
            return
        for idx in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(idx) is page:
                self._select_book_page(self.notebook, idx)
                return

    def _select_nested_page(self, notebook, page):
        if notebook is None or page is None:
            return
        for idx in range(notebook.GetPageCount()):
            if notebook.GetPage(idx) is page:
                self._select_book_page(notebook, idx)
                return

    def _open_devices_page(self, page_name):
        self._select_top_page(getattr(self, "tab_devices_shell", None))
        if page_name == "vacuum":
            self._select_nested_page(getattr(self, "devices_notebook", None), getattr(self, "tab_vacuum", None))
            wx.CallAfter(getattr(self, "btn_refresh_vacuum", self).SetFocus)
        else:
            self._select_nested_page(getattr(self, "devices_notebook", None), getattr(self, "tab_fridge", None))

    def _setup_skip_state(self):
        skips = self.config.get("setup_skips", {})
        if not isinstance(skips, dict):
            skips = {}
        return {
            "gemini": bool(skips.get("gemini", False)),
            "pushover": bool(skips.get("pushover", False)),
            "fridge": bool(skips.get("fridge", False)),
            "vacuum": bool(skips.get("vacuum", False)),
        }

    def _setup_readiness_items(self, live_result=None):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha = runtime["home_assistant"]
        api = runtime["api"]
        doorbell = runtime["doorbell"]
        speakers = runtime["speakers"]
        routes = speakers.get("routes", {})
        skips = self._setup_skip_state()
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        progress = _coerce_setup_progress_state(self.config.get("setup_progress", {}))
        front_rtsp = doorbell.get("configured_rtsp_front") or doorbell.get("raw_rtsp_front") or ""
        back_rtsp = doorbell.get("configured_rtsp_back") or doorbell.get("raw_rtsp_back") or ""
        ha_ready = bool(ha.get("ha_ip") and ha.get("ha_token"))
        triggers_ready = bool(doorbell.get("front_trigger_entity_id") and doorbell.get("back_trigger_entity_id"))
        streams_ready = bool(front_rtsp and back_rtsp)
        speaker_routes_ready = bool(speakers.get("enabled_count") and routes.get("doorbell") and routes.get("utilities") and routes.get("fridge"))
        gemini_ready = bool(api.get("gemini_api_key"))
        fridge_configured = any(
            bool((self.config.get("broadcast_channels", {}) or {}).get(key, {}).get("chime"))
            or bool((self.config.get("broadcast_channels", {}) or {}).get(key, {}).get("entity_id"))
            for key in ("fridge_open", "freezer_open")
        )
        vacuum_configured = bool(self.config.get("vacuum_entity") or self.config.get("vacuum_rooms") or self.config.get("cinderella_status_entity"))
        items = [
            {
                "key": "home_assistant",
                "label": "Home Assistant",
                "ok": ha_ready,
                "optional": False,
                "skipped": False,
                "detail": f"{ha.get('ha_ip')}:{ha.get('ha_port') or '8123'}" if ha_ready else "Needs host and long-lived token.",
                "fix": "Open Setup Wizard at Home Assistant connection.",
                "test": "Run Home Assistant connection test.",
            },
            {
                "key": "ring_mqtt",
                "label": "Ring-MQTT",
                "ok": streams_ready,
                "optional": False,
                "skipped": False,
                "detail": "RTSP streams are saved." if streams_ready else "Needed for Ring live video streams.",
                "fix": "Open Ring-MQTT stream discovery in the setup wizard.",
                "test": "Run camera frame tests after streams are saved.",
            },
            {
                "key": "doorbell_triggers",
                "label": "Doorbell triggers",
                "ok": triggers_ready,
                "optional": False,
                "skipped": False,
                "detail": f"front={doorbell.get('front_trigger_entity_id') or 'missing'}, back={doorbell.get('back_trigger_entity_id') or 'missing'}",
                "fix": "Choose front and back Home Assistant trigger entities.",
                "test": "Test Everything simulates event routing.",
            },
            {
                "key": "live_video",
                "label": "Live video",
                "ok": streams_ready,
                "optional": False,
                "skipped": False,
                "detail": f"front={'saved' if front_rtsp else 'missing'}, back={'saved' if back_rtsp else 'missing'}",
                "fix": "Find and save front and back RTSP streams.",
                "test": "Capture one frame from each stream.",
            },
            {
                "key": "speakers",
                "label": "Speakers",
                "ok": speaker_routes_ready,
                "optional": False,
                "skipped": False,
                "detail": f"{speakers.get('enabled_count', 0)} enabled; routes doorbell {len(routes.get('doorbell', []))}, utilities {len(routes.get('utilities', []))}, fridge/freezer {len(routes.get('fridge', []))}",
                "fix": "Open Choose Alert Speakers.",
                "test": "Send a manual speaker test.",
            },
            {
                "key": "gemini",
                "label": "Gemini and TTS",
                "ok": gemini_ready or skips["gemini"],
                "optional": True,
                "skipped": skips["gemini"],
                "detail": "Ready." if gemini_ready else ("Skipped for now." if skips["gemini"] else "Needed for Gemini vision and Gemini TTS."),
                "fix": "Open Gemini and voice behavior setup.",
                "test": "Run diagnostics and Test Everything.",
            },
            {
                "key": "pushover",
                "label": "Pushover",
                "ok": bool(api.get("pushover_enabled")) or skips["pushover"],
                "optional": True,
                "skipped": skips["pushover"],
                "detail": "Enabled." if api.get("pushover_enabled") else ("Skipped for now." if skips["pushover"] else "Optional mobile push notifications are not enabled."),
                "fix": "Open Gemini and voice behavior setup.",
                "test": "Run diagnostics after entering Pushover keys.",
            },
            {
                "key": "fridge",
                "label": "Fridge/freezer alerts",
                "ok": fridge_configured or skips["fridge"],
                "optional": True,
                "skipped": skips["fridge"],
                "detail": "Configured." if fridge_configured else ("Skipped for now." if skips["fridge"] else "Optional feature not configured."),
                "fix": "Open Refrigerator & Ice setup.",
                "test": "Play fridge/freezer chime tests.",
            },
            {
                "key": "vacuum",
                "label": "Robot vacuum",
                "ok": vacuum_configured or skips["vacuum"],
                "optional": True,
                "skipped": skips["vacuum"],
                "detail": "Configured." if vacuum_configured else ("Skipped for now." if skips["vacuum"] else "Optional feature not configured."),
                "fix": "Open Robot Vacuum setup.",
                "test": "Refresh vacuum controls.",
            },
        ]
        if live_result and live_result.get("ha_connection"):
            conn = live_result["ha_connection"]
            items[0]["ok"] = bool(conn.get("ok"))
            items[0]["detail"] = conn.get("message") or conn.get("error") or items[0]["detail"]
        core_ready = all(item["ok"] for item in items if not item["optional"])
        optional_ready = all(item["ok"] for item in items if item["optional"])
        last_step = progress.get("phase_label") or progress.get("status") or getattr(self, "last_setup_status", "") or "No setup step has reported progress yet."
        return {
            "items": items,
            "core_ready": core_ready,
            "optional_ready": optional_ready,
            "last_step": last_step,
            "progress": progress,
            "listener": listener,
        }

    def _current_setup_issue(self, include_optional=True):
        readiness = self._setup_readiness_items()
        for item in readiness["items"]:
            if not item["ok"] and not item["optional"]:
                return item
        if include_optional:
            for item in readiness["items"]:
                if item["optional"] and not item["ok"]:
                    return item
        return None

    def build_setup_next_action_summary(self):
        readiness = self._setup_readiness_items()
        issue = self._current_setup_issue(include_optional=True)
        skipped = [item["label"] for item in readiness["items"] if item.get("skipped")]
        if readiness["core_ready"]:
            lines = ["Core setup is ready."]
        else:
            lines = ["Core setup needs attention."]
        lines.extend([
            f"Optional setup: {'ready or skipped' if readiness['optional_ready'] else 'available'}",
            f"Last successful step: {readiness['last_step']}",
            f"Skipped optional: {', '.join(skipped) if skipped else 'none'}",
        ])
        if issue:
            action = "Fix" if not issue["optional"] else "Fix or Skip"
            lines.append(f"Next recommended action: {action} {issue['label']}. {issue['fix']}")
        else:
            lines.append("Recommended next: run Test Everything, then try a real doorbell press while armed.")
        return "\n".join(lines)

    def _format_setup_status_items(self, live_result=None):
        readiness = self._setup_readiness_items(live_result=live_result)
        lines = ["Setup Status", ""]
        for item in readiness["items"]:
            state = "Ready" if item["ok"] and not item.get("skipped") else ("Skipped" if item.get("skipped") else "Needs attention")
            optional = " optional" if item["optional"] else ""
            lines.append(f"{item['label']}: {state}{optional}. {item['detail']}")
            if not item["ok"]:
                lines.append(f"  Fix: {item['fix']}")
                lines.append(f"  Test: {item['test']}")
        lines.append("")
        issue = self._current_setup_issue(include_optional=True)
        if issue:
            lines.append(f"One next action: {issue['fix']}")
        else:
            lines.append("Core setup is ready.")
            lines.append("One next action: Run Test Everything, then test a real doorbell press while armed.")
        return "\n".join(lines)

    def _refresh_setup_status_controls(self):
        if hasattr(self, "setup_next_action_txt"):
            self.setup_next_action_txt.SetValue(self.build_setup_next_action_summary())
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue(self.build_setup_checklist_summary())
        self._update_main_setup_actions()

    def on_continue_setup(self, event):
        self.on_open_setup_wizard(event)

    def _setup_page_for_issue(self, key):
        return {
            "home_assistant": "connect",
            "ring_mqtt": "live_streams",
            "doorbell_triggers": "doorbells",
            "live_video": "live_streams",
            "speakers": "speakers",
        }.get(key, "test")

    def on_fix_current_setup_item(self, event):
        issue = self._current_setup_issue(include_optional=True)
        if not issue:
            self.on_test_everything(event)
            return
        key = issue["key"]
        if key in {"home_assistant", "ring_mqtt", "doorbell_triggers", "live_video"}:
            self.open_setup_wizard_at(self._setup_page_for_issue(key))
        elif key == "speakers":
            self.open_setup_wizard_at("speakers")
        elif key == "gemini":
            self.on_fix_tts_setup(event)
        elif key == "pushover":
            self.on_fix_tts_setup(event)
        elif key == "fridge":
            self._open_devices_page("fridge")
            self.notify("Opened Refrigerator & Ice setup. Choose chime behavior, sensors, then test the fridge and freezer chimes.", priority=10)
        elif key == "vacuum":
            self._open_devices_page("vacuum")
            self.notify("Opened Robot Vacuum setup. Refresh controls, choose the vacuum entity, and test status announcements.", priority=10)

    def on_test_current_setup_item(self, event):
        issue = self._current_setup_issue(include_optional=True)
        key = issue["key"] if issue else "all"
        if key == "home_assistant":
            self.on_test_everything(event)
        elif key in {"ring_mqtt", "live_video"}:
            self.on_test_diagnostics_camera(event, "front")
            self.on_test_diagnostics_camera(event, "back")
        elif key == "speakers":
            self.on_test_diagnostics_manual_broadcast(event)
        elif key == "fridge":
            self._open_devices_page("fridge")
            self.notify("Opened Refrigerator & Ice. Use the fridge/freezer chime test buttons there.", priority=10)
        elif key == "vacuum":
            self._open_devices_page("vacuum")
            self.on_refresh_vacuum(event)
        else:
            self.on_test_everything(event)

    def on_skip_optional_setup_item(self, event):
        issue = self._current_setup_issue(include_optional=True)
        if not issue or not issue["optional"]:
            self.notify("There is no optional setup item ready to skip. Fix the required item first.", priority=10)
            return
        skips = self._setup_skip_state()
        skips[issue["key"]] = True
        self.config["setup_skips"] = skips
        cfg.save_config(self.config)
        self.record_setup_event("optional_setup_skipped", f"Skipped optional setup item: {issue['label']}", key=issue["key"])
        self._refresh_setup_status_controls()
        self.notify(f"Skipped optional setup item for now: {issue['label']}.", priority=10)

    def on_restore_optional_setup_items(self, event):
        skips = self._setup_skip_state()
        if not any(skips.values()):
            self.notify("No optional setup items are currently skipped.", priority=10)
            return
        restored = [key for key, value in skips.items() if value]
        self.config["setup_skips"] = {key: False for key in skips}
        cfg.save_config(self.config)
        self.record_setup_event("optional_setup_restored", "Restored skipped optional setup items.", restored=", ".join(restored))
        self._refresh_setup_status_controls()
        self.notify("Restored optional setup items. They will show as available again.", priority=10)

    def _non_secret_setup_backup(self):
        data = cfg.validate_and_normalize_config(self.config)
        redacted = diagnostics.redact_config(data)
        for key in ("ha_token", "gemini_api_key", "pushover_user_key", "pushover_api_token", "mqtt_password"):
            redacted[key] = ""
        return {
            "viper_setup_backup_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "notes": "Secrets are intentionally omitted. Re-enter tokens after restore if needed.",
            "config": redacted,
        }

    def on_backup_setup(self, event):
        try:
            backup = self._non_secret_setup_backup()
            path = cfg.DATA_DIR / f"viper_setup_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
            self.record_setup_event("setup_backup_created", "Setup backup created.", path=str(path))
            self.notify(f"Setup backup created: {path}", priority=10)
            wx.MessageBox(f"Setup backup created:\n{path}\n\nSecrets were not included.", "Backup Setup", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            logging.exception("Setup backup failed")
            self.notify(f"Setup backup failed: {e}", priority=10)

    def on_restore_setup(self, event):
        with wx.FileDialog(
            self,
            "Choose Viper setup backup",
            wildcard="Viper setup backup (*.json)|*.json",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = Path(dlg.GetPath())
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            restored = payload.get("config") if isinstance(payload, dict) else None
            if not isinstance(restored, dict):
                raise ValueError("Backup file does not contain a config object.")
            current = cfg.validate_and_normalize_config(self.config)
            for key in ("ha_token", "gemini_api_key", "pushover_user_key", "pushover_api_token", "mqtt_password"):
                restored[key] = current.get(key, "")
            self.config = cfg.write_config(restored)
            self.record_setup_event("setup_backup_restored", "Setup backup restored.", path=str(path))
            self._refresh_setup_status_controls()
            self.notify("Setup backup restored. Re-enter any missing secrets, then run Test Everything.", priority=10)
            wx.MessageBox("Setup backup restored.\n\nSecrets are not stored in backups, so re-enter any missing tokens before testing.", "Restore Setup", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            logging.exception("Setup restore failed")
            self.notify(f"Setup restore failed: {e}", priority=10)

    def on_show_troubleshooting_recipe(self, event, recipe):
        recipes = {
            "ha_token": (
                "Home Assistant token works in browser but not Viper",
                [
                    "Open the Home Assistant profile page and create a new long-lived token.",
                    "Paste the token into the Setup Wizard Home Assistant step.",
                    "Press Connect And Discover Devices, then Test Everything.",
                    "If it still fails, create a support report from Diagnostics.",
                ],
            ),
            "ring_streams": (
                "Ring-MQTT installed but no RTSP streams found",
                [
                    "Open the Setup Wizard and use the Ring-MQTT step.",
                    "Confirm the add-on is logged in and video streaming is enabled.",
                    "Run live stream discovery and save two different passed streams.",
                    "Use Fix Camera Frames if either stream captures no frame.",
                ],
            ),
            "speakers": (
                "Speaker test says sent but nothing played",
                [
                    "Open Choose Alert Speakers and confirm the intended speaker is enabled.",
                    "Check doorbell, utilities, and fridge/freezer route boxes.",
                    "If it is a Home Assistant speaker, verify the media_player entity can play from HA.",
                    "Try Manual Broadcast from Diagnostics.",
                ],
            ),
            "doorbell": (
                "Doorbell rings but Viper does not announce",
                [
                    "Open the Setup Wizard doorbell step.",
                    "Choose front and back trigger entities that change state when the bell is pressed.",
                    "Confirm Viper is armed and the Home Assistant listener is enabled.",
                    "Run Test Everything, then try one real doorbell press.",
                ],
            ),
            "camera": (
                "Camera works once then fails",
                [
                    "Open Ring-MQTT stream discovery and retest streams.",
                    "Save only streams that pass frame capture.",
                    "Check that front and back use different RTSP URLs when possible.",
                    "Run the individual front and back camera frame tests.",
                ],
            ),
            "gemini": (
                "Gemini replies too short or is unavailable",
                [
                    "Open AI Descriptions and keep prompts specific about people, packages, movement, and safety.",
                    "Open Voice Behavior and confirm Gemini key is saved if using Gemini.",
                    "Use Smart or Detailed video mode when still images are too weak.",
                    "Run Test Everything and check Diagnostics if Gemini service is unavailable.",
                ],
            ),
        }
        title, steps = recipes.get(recipe, recipes["ha_token"])
        text = title + "\n\n" + "\n".join(f"{idx}. {step}" for idx, step in enumerate(steps, 1))
        wx.MessageBox(text, "Troubleshooting Recipe", wx.OK | wx.ICON_INFORMATION)

    def record_setup_event(self, event, message="", **details):
        if not hasattr(self, "setup_events"):
            self.setup_events = []
        if not hasattr(self, "last_setup_status"):
            self.last_setup_status = ""
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": str(event or "setup_event"),
            "message": diagnostics.redact_text(str(message or "")),
        }
        for key, value in (details or {}).items():
            if diagnostics.should_redact_key(key):
                entry[str(key)] = diagnostics.redact_config(value, str(key))
            elif isinstance(value, str):
                entry[str(key)] = diagnostics.redact_text(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                entry[str(key)] = value
            else:
                entry[str(key)] = diagnostics.redact_config(value, str(key))
        self.setup_events.append(entry)
        if len(self.setup_events) > 250:
            self.setup_events = self.setup_events[-250:]
        if entry["event"] == "status":
            self.last_setup_status = entry["message"]
        logging.info(
            "[SETUP EVENT] %s message=%r details=%s",
            entry["event"],
            entry["message"],
            {k: v for k, v in entry.items() if k not in {"time", "event", "message"}},
        )

    def _check_line(self, label, ok, detail=""):
        state = "Passed" if ok else "Needs setup"
        return f"{label}: {state}{'. ' + detail if detail else ''}"

    def build_setup_checklist_summary(self, live_result=None):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha_settings = runtime["home_assistant"]
        api_settings = runtime["api"]
        doorbell_settings = runtime["doorbell"]
        speaker_settings = runtime["speakers"]
        speakers = speaker_settings["speakers"]
        listener_status = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        live_result = live_result or {}

        lines = [self._format_setup_status_items(live_result=live_result), "", "Detailed Checklist", ""]
        lines.append(self._check_line("Home Assistant address", bool(ha_settings.get("ha_ip")), ha_settings.get("ha_ip") or "No host saved."))
        lines.append(self._check_line("Home Assistant token", bool(ha_settings.get("ha_token")), "Long-lived token is saved." if ha_settings.get("ha_token") else "Paste a long-lived token."))
        if "ha_connection" in live_result:
            conn = live_result["ha_connection"]
            lines.append(self._check_line("Home Assistant live connection", bool(conn.get("ok")), conn.get("message") or conn.get("error") or "Connection tested."))
        else:
            if listener_status.get("connected"):
                live_detail = "Listener is connected. Press Test Everything when you want Viper to also verify the Home Assistant REST API."
            elif ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
                live_detail = "Not checked yet in this checklist. Press Test Everything to confirm Home Assistant accepts the token and Viper can read entities."
            else:
                live_detail = "Needs Home Assistant host and token before Viper can run a live check."
            lines.append(self._check_line("Home Assistant live connection", bool(listener_status.get("connected")), live_detail))
        lines.append(self._check_line("Direct Home Assistant listener", bool(self.config.get("ha_listener_enabled", True)), "Enabled." if self.config.get("ha_listener_enabled", True) else "Disabled; advanced HA automations/webhooks must be used."))
        if listener_status.get("connected"):
            listener_detail = f"Connected to {listener_status.get('last_host') or ha_settings.get('ha_ip') or 'Home Assistant'}."
        elif not self.config.get("ha_listener_enabled", True):
            listener_detail = "Disabled by choice. Viper will rely on advanced Home Assistant automations or webhooks."
        elif ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
            raw_error = listener_status.get("last_error") or "waiting to connect"
            if "missing Home Assistant host or token" in raw_error:
                listener_detail = "Credentials are available from config, environment variables, or Windows Credential Manager. The listener should reconnect shortly; press Test Everything to verify Home Assistant directly."
            else:
                listener_detail = raw_error
        else:
            listener_detail = "Missing Home Assistant host or token."
        lines.append(self._check_line("Listener currently connected", bool(listener_status.get("connected")), listener_detail))
        lines.append("")
        front_trigger = doorbell_settings.get("front_trigger_entity_id", "")
        back_trigger = doorbell_settings.get("back_trigger_entity_id", "")
        front_rtsp = doorbell_settings.get("configured_rtsp_front") or doorbell_settings.get("raw_rtsp_front") or ""
        back_rtsp = doorbell_settings.get("configured_rtsp_back") or doorbell_settings.get("raw_rtsp_back") or ""
        lines.append(self._check_line("Front door trigger", bool(front_trigger), front_trigger or "Choose a front trigger entity."))
        lines.append(self._check_line("Back door trigger", bool(back_trigger), back_trigger or "Choose a back trigger entity."))
        lines.append(self._check_line("Front live RTSP URL", bool(front_rtsp), front_rtsp or "Find Ring MQTT streams."))
        lines.append(self._check_line("Back live RTSP URL", bool(back_rtsp), back_rtsp or "Find Ring MQTT streams."))
        if "rtsp_front" in live_result:
            lines.append(self._check_line("Front RTSP frame test", bool(live_result["rtsp_front"].get("ok")), live_result["rtsp_front"].get("message") or "Frame captured."))
        if "rtsp_back" in live_result:
            lines.append(self._check_line("Back RTSP frame test", bool(live_result["rtsp_back"].get("ok")), live_result["rtsp_back"].get("message") or "Frame captured."))
        lines.append("")
        lines.append(self._check_line("Ring-MQTT RTSP stream setup", bool(front_rtsp and back_rtsp), "Both RTSP URLs are saved." if front_rtsp and back_rtsp else "Use Ring-MQTT setup if using Ring cameras."))
        enabled_speakers = speaker_settings["enabled_count"]
        speaker_routes = speaker_settings.get("routes", {})
        doorbell_route_count = len(speaker_routes.get("doorbell", []))
        utilities_route_count = len(speaker_routes.get("utilities", []))
        fridge_route_count = len(speaker_routes.get("fridge", []))
        required_routes_ok = bool(enabled_speakers and doorbell_route_count and utilities_route_count and fridge_route_count)
        if speakers:
            speaker_detail = (
                f"{speaker_settings['speaker_count']} saved, {enabled_speakers} enabled. "
                f"Enabled routes: doorbell {doorbell_route_count}, utilities {utilities_route_count}, "
                f"fridge/freezer {fridge_route_count}. "
                "Use Choose Alert Speakers if any route count is zero or if the wrong speaker is enabled."
            )
        else:
            speaker_detail = "Add or scan Home Assistant/Sonos speakers, then choose which ones receive alerts."
        lines.append(self._check_line("Speaker routes", required_routes_ok, speaker_detail))
        lines.append(self._check_line("Gemini API key", bool(api_settings.get("gemini_api_key")), "Saved." if api_settings.get("gemini_api_key") else "Needed for Gemini vision and Gemini TTS."))
        lines.append(self._check_line("Pushover", bool(api_settings.get("pushover_enabled")), "Enabled." if api_settings.get("pushover_enabled") else "Optional."))
        lines.append("")
        lines.append("Optional Feature Cards")
        for item in self._setup_readiness_items()["items"]:
            if item["optional"]:
                state = "ready" if item["ok"] and not item.get("skipped") else ("skipped" if item.get("skipped") else "not configured")
                lines.append(f"{item['label']}: {state}. {item['detail']}")
        lines.append("")
        lines.append("Troubleshooting Recipes")
        lines.append("Use the recipe buttons above for Home Assistant tokens, Ring-MQTT streams, speaker audio, doorbell events, camera frames, and Gemini replies.")
        lines.append("")
        lines.append("Next best action:")
        if not ha_settings.get("ha_ip") or not ha_settings.get("ha_token"):
            lines.append("Press Open Setup Wizard and complete the Home Assistant Connection step.")
        elif not (front_trigger and back_trigger):
            lines.append("Open Doorbell Vision, then press Set Up Doorbell Triggers And Cameras.")
        elif not (front_rtsp and back_rtsp):
            lines.append("Open Doorbell Vision, then press Set Up Doorbell Triggers And Cameras and find Ring-MQTT streams.")
        elif not required_routes_ok:
            lines.append("Press Choose Alert Speakers. Enable at least one speaker for doorbell, utilities, and fridge/freezer alerts.")
        elif not api_settings.get("gemini_api_key"):
            lines.append("Open Speakers & Audio, then configure Gemini or choose a non-Gemini TTS engine.")
        else:
            lines.append("Press Test Everything to verify Home Assistant, RTSP camera frames, and diagnostics. Then try a full doorbell flow test.")
        return "\n".join(lines)

    def build_setup_confidence_summary(self):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha = runtime["home_assistant"]
        api = runtime["api"]
        doorbell = runtime["doorbell"]
        speakers = runtime["speakers"]
        routes = speakers.get("routes", {})
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        front_rtsp = doorbell.get("configured_rtsp_front") or doorbell.get("raw_rtsp_front") or ""
        back_rtsp = doorbell.get("configured_rtsp_back") or doorbell.get("raw_rtsp_back") or ""
        ready = bool(
            ha.get("ha_ip")
            and ha.get("ha_token")
            and doorbell.get("front_trigger_entity_id")
            and doorbell.get("back_trigger_entity_id")
            and front_rtsp
            and back_rtsp
            and speakers.get("enabled_count")
            and routes.get("doorbell")
            and routes.get("utilities")
            and routes.get("fridge")
            and api.get("gemini_api_key")
        )
        lines = [
            f"Doorbell system ready: {'yes' if ready else 'needs attention'}",
            f"Home Assistant listener: {'connected' if listener.get('connected') else 'not connected'}",
            f"Doorbell triggers: front {'yes' if doorbell.get('front_trigger_entity_id') else 'no'}, back {'yes' if doorbell.get('back_trigger_entity_id') else 'no'}",
            f"Camera streams: front {'yes' if front_rtsp else 'no'}, back {'yes' if back_rtsp else 'no'}",
            f"Audio routes: doorbell {len(routes.get('doorbell', []))}, utilities {len(routes.get('utilities', []))}, fridge/freezer {len(routes.get('fridge', []))}",
            f"Gemini key: {'yes' if api.get('gemini_api_key') else 'no'}",
            "Recommended next: run Test Everything, then test the real doorbell while armed.",
        ]
        return "\n".join(lines)

    def refresh_setup_checklist(self):
        if hasattr(self, "setup_next_action_txt"):
            self.setup_next_action_txt.SetValue(self.build_setup_next_action_summary())
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue(self.build_setup_checklist_summary())
        if hasattr(self, "doorbell_summary_txt"):
            self.doorbell_summary_txt.SetValue(self._doorbell_summary_text())
        self._update_main_setup_actions()
        self.notify("Setup checklist refreshed.", priority=10)

    def _set_main_button_gate(self, button, enabled, enabled_tip, disabled_tip):
        if button is None:
            return
        try:
            button.Enable(bool(enabled))
            button.SetToolTip(enabled_tip if enabled else disabled_tip)
            button.SetName(button.GetLabel() if enabled else f"{button.GetLabel()}. Unavailable. {disabled_tip}")
        except Exception:
            pass

    def _update_main_setup_actions(self):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha_settings = runtime["home_assistant"]
        doorbell_settings = runtime["doorbell"]
        speaker_settings = runtime["speakers"]
        speakers = speaker_settings["speakers"]
        speaker_routes = speaker_settings.get("routes", {})
        required_routes_ok = bool(
            speaker_settings.get("enabled_count")
            and speaker_routes.get("doorbell")
            and speaker_routes.get("utilities")
            and speaker_routes.get("fridge")
        )
        has_ha = bool(ha_settings.get("ha_ip") and ha_settings.get("ha_token"))
        has_doorbell_setup = bool(
            doorbell_settings.get("front_trigger_entity_id")
            and doorbell_settings.get("back_trigger_entity_id")
            and (doorbell_settings.get("configured_rtsp_front") or doorbell_settings.get("raw_rtsp_front"))
            and (doorbell_settings.get("configured_rtsp_back") or doorbell_settings.get("raw_rtsp_back"))
        )
        self._set_main_button_gate(
            getattr(self, "btn_fix_doorbells", None),
            has_ha,
            "Opens doorbell setup.",
            "Home Assistant host and token are needed before doorbell entities and Ring-MQTT streams can be discovered.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_choose_setup_speakers", None),
            has_ha,
            "Finds or edits alert speakers.",
            "Home Assistant host and token are needed before Home Assistant speaker discovery can run.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_test_everything", None),
            has_ha and has_doorbell_setup and required_routes_ok,
            "Runs final setup tests.",
            "Home Assistant, doorbell triggers, live streams, and at least one enabled speaker route for doorbell, utilities, and fridge/freezer should be configured first.",
        )
        readiness = self._setup_readiness_items()
        required_issue = self._current_setup_issue(include_optional=False)
        issue = self._current_setup_issue(include_optional=True)
        optional_issue = issue and issue.get("optional")
        skipped = [item for item in readiness["items"] if item.get("skipped")]
        fix_button = getattr(self, "btn_fix_current_setup", None)
        if fix_button is not None:
            if required_issue:
                fix_button.SetLabel("Fix Current Item")
                fix_button.SetName("Fix Current Item")
            elif optional_issue:
                fix_button.SetLabel("Set Up Optional Item")
                fix_button.SetName("Set Up Optional Item")
            else:
                fix_button.SetLabel("Setup Is Ready")
                fix_button.SetName("Setup Is Ready")
        self._set_main_button_gate(
            fix_button,
            bool(issue),
            "Opens the exact setup area for the current item.",
            "Core setup is ready. Run Test Everything when you want a fresh verification.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_test_current_setup", None),
            bool(issue) or readiness["core_ready"],
            "Runs the safest relevant test for the current setup item.",
            "Add Home Assistant host and token before tests can run.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_skip_optional_setup", None),
            optional_issue,
            "Marks the current optional feature as skipped for now.",
            "Required setup items cannot be skipped. Finish the required item first.",
        )
        self._set_main_button_gate(
            getattr(self, "btn_unskip_optional_setup", None),
            bool(skipped),
            "Restores skipped optional items so they appear in setup again.",
            "No optional setup items are currently skipped.",
        )

    def suggested_setup_page(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        if not ha_settings.get("ha_ip") or not ha_settings.get("ha_token"):
            return "connect"
        triggers = self.config.get("doorbell_triggers", {}) if isinstance(self.config.get("doorbell_triggers"), dict) else {}
        front = triggers.get("front", {}) if isinstance(triggers, dict) else {}
        back = triggers.get("back", {}) if isinstance(triggers, dict) else {}
        if not (front.get("trigger_entity_id") or back.get("trigger_entity_id")):
            return "doorbells"
        if not (front.get("rtsp_url") or self.config.get("rtsp_front")) or not (back.get("rtsp_url") or self.config.get("rtsp_back")):
            return "live_streams"
        speaker_settings = cfg.get_speaker_settings(self.config, include_env=True)
        routes = speaker_settings.get("routes", {})
        if not (speaker_settings.get("enabled_count") and routes.get("doorbell") and routes.get("utilities") and routes.get("fridge")):
            return "speakers"
        return "test"

    def on_open_setup_wizard(self, event):
        self._close_setup_surfaces(keep="_setup_wizard_dialog")
        existing = getattr(self, "_setup_wizard_dialog", None)
        if self._is_live_window(existing):
            try:
                requested = getattr(self, "_requested_setup_page", "")
                if requested and hasattr(existing, "go_to_setup_action"):
                    existing.go_to_setup_action(requested)
                    self._requested_setup_page = ""
                existing.force_initial_focus()
                self._enter_setup_window_mode(existing)
                self._log_setup_focus_snapshot("reuse_setup_wizard")
                return
            except Exception:
                self._setup_wizard_dialog = None
        else:
            self._setup_wizard_dialog = None
        dlg = ViperSetupWizardDialog(None, owner=self)
        if hasattr(self, "_requested_setup_page"):
            self._requested_setup_page = ""
        self._setup_wizard_dialog = dlg
        dlg.Show()
        wx.CallAfter(self._enter_setup_window_mode, dlg)
        wx.CallLater(75, dlg.force_initial_focus)
        wx.CallLater(300, dlg.force_initial_focus)
        wx.CallLater(450, self._log_setup_focus_snapshot, "show_setup_wizard")

    def open_setup_wizard_at(self, action):
        self._requested_setup_page = action
        self.on_open_setup_wizard(None)

    def on_setup_everything_automatically(self, event):
        self.on_open_setup_wizard(event)

    def on_test_everything(self, event):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha_settings = runtime["home_assistant"]
        doorbell_settings = runtime["doorbell"]
        if not (ha_settings.get("ha_ip") and ha_settings.get("ha_token")):
            message = "Test Everything cannot run yet. Press Open Setup Wizard and complete the Home Assistant Connection step first."
            if hasattr(self, "setup_checklist_txt"):
                self.setup_checklist_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        if not (
            doorbell_settings.get("front_trigger_entity_id")
            and doorbell_settings.get("back_trigger_entity_id")
            and (doorbell_settings.get("configured_rtsp_front") or doorbell_settings.get("raw_rtsp_front"))
            and (doorbell_settings.get("configured_rtsp_back") or doorbell_settings.get("raw_rtsp_back"))
        ):
            message = "Test Everything cannot run yet. Open Doorbell Vision and set up triggers and live camera streams first."
            if hasattr(self, "setup_checklist_txt"):
                self.setup_checklist_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        speaker_settings = runtime["speakers"]
        speaker_routes = speaker_settings.get("routes", {})
        if not (
            speaker_settings.get("enabled_count")
            and speaker_routes.get("doorbell")
            and speaker_routes.get("utilities")
            and speaker_routes.get("fridge")
        ):
            message = "Test Everything cannot run yet. Press Choose Alert Speakers and enable at least one speaker route for doorbell, utilities, and fridge/freezer first."
            if hasattr(self, "setup_checklist_txt"):
                self.setup_checklist_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue("Running setup checks. This can take a few seconds.")
        self.notify("Running setup checks.", priority=10)
        safe_submit(self._run_test_everything)

    def _run_test_everything(self):
        live_result = {}
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        if ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
            live_result["ha_connection"] = discovery.test_ha_connection(
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port") or "8123",
                timeout=5,
            )
        for side, key in (("front", "rtsp_front"), ("back", "rtsp_back")):
            url = self.config.get(key) or (self.config.get("doorbell_triggers", {}).get(side, {}) or {}).get("rtsp_url")
            if not url:
                continue
            try:
                frame = vision.grab_frame(url, cfg.DATA_DIR / "rtsp_test", f"setup_check_{side}", min_bytes=14000, timeout=8)
                live_result[key] = {"ok": bool(frame), "message": f"Frame captured: {Path(frame).name}" if frame else "No frame captured."}
            except Exception as e:
                live_result[key] = {"ok": False, "message": str(e)}
        summary = self.build_setup_checklist_summary(live_result=live_result)
        try:
            summary += "\n\n" + self._format_safe_smoke_report(self._collect_safe_smoke_results())
        except Exception as e:
            logging.exception("Could not append safe smoke report to Test Everything.")
            summary += f"\n\nSmoke Test: ERROR\n\nThe smoke test report failed: {e}"
        wx.CallAfter(self._finish_test_everything, summary)

    def _finish_test_everything(self, summary):
        if hasattr(self, "setup_checklist_txt"):
            self.setup_checklist_txt.SetValue(summary)
        self._update_main_setup_actions()
        self.notify("Setup checks finished.", priority=10)
