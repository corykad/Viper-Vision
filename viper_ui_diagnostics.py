import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import wx

import viper_audio as audio
import viper_config as cfg
import viper_diagnostics as diagnostics
import viper_discovery as discovery
import viper_ha_recovery
import viper_health
import viper_matter
import viper_vision as vision
from viper_runtime import format_recent_events, recent_events, record_event, safe_submit


class DiagnosticsTabMixin:
    def setup_diagnostics_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        health_box = wx.StaticBox(self.tab_diagnostics_overview, label="Health Summary")
        health_sizer = wx.StaticBoxSizer(health_box, wx.VERTICAL)
        self.diagnostics_health_txt = wx.TextCtrl(
            self.tab_diagnostics_overview,
            value="Health summary is loading.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 210),
        )
        self._describe_control(self.diagnostics_health_txt, "Health Summary. Read-only active issues, resolved history, normal log noise, and latest log line.")
        health_sizer.Add(self.diagnostics_health_txt, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_refresh_health = wx.Button(self.tab_diagnostics_overview, label="Refresh Health Summary", size=(-1, 40))
        self.btn_refresh_health.Bind(wx.EVT_BUTTON, self.on_refresh_health_summary)
        self._describe_control(self.btn_refresh_health, "Refresh Health Summary button. Quickly refreshes the local Viper health summary without opening the full diagnostics report.")
        health_sizer.Add(self.btn_refresh_health, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(health_sizer, 1, wx.ALL | wx.EXPAND, 10)

        watchdog_box = wx.StaticBox(self.tab_diagnostics_overview, label="Home Assistant Watchdog")
        watchdog_sizer = wx.StaticBoxSizer(watchdog_box, wx.VERTICAL)
        self.ha_watchdog_txt = wx.TextCtrl(
            self.tab_diagnostics_overview,
            value="Watchdog status is loading.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 190),
        )
        self._describe_control(self.ha_watchdog_txt, "Home Assistant Watchdog status. Read-only scheduled task state, last run result, recovery state, and recent watchdog log lines.")
        watchdog_sizer.Add(self.ha_watchdog_txt, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_ha_watchdog = wx.Button(self.tab_diagnostics_overview, label="Refresh HA Watchdog", size=(-1, 40))
        self.btn_test_ha_watchdog_push = wx.Button(self.tab_diagnostics_overview, label="Test HA Recovery Push", size=(-1, 40))
        self.ha_watchdog_pause_choice = wx.Choice(
            self.tab_diagnostics_overview,
            choices=["30 minutes", "1 hour", "2 hours", "4 hours", "8 hours", "Until tomorrow"],
        )
        self.ha_watchdog_pause_choice.SetSelection(2)
        self.btn_pause_ha_watchdog = wx.Button(self.tab_diagnostics_overview, label="Pause HA Recovery", size=(-1, 40))
        self.btn_resume_ha_watchdog = wx.Button(self.tab_diagnostics_overview, label="Resume HA Recovery", size=(-1, 40))
        self.btn_refresh_ha_watchdog.Bind(wx.EVT_BUTTON, self.on_refresh_ha_watchdog)
        self.btn_test_ha_watchdog_push.Bind(wx.EVT_BUTTON, self.on_test_ha_watchdog_push)
        self.btn_pause_ha_watchdog.Bind(wx.EVT_BUTTON, self.on_pause_ha_watchdog)
        self.btn_resume_ha_watchdog.Bind(wx.EVT_BUTTON, self.on_resume_ha_watchdog)
        self._describe_control(self.btn_refresh_ha_watchdog, "Refresh HA Watchdog button. Checks the scheduled watchdog task, last run result, and latest recovery state.")
        self._describe_control(self.btn_test_ha_watchdog_push, "Test HA Recovery Push button. Sends a safe Pushover test using the same path as the Home Assistant recovery watchdog.")
        self._describe_control(self.ha_watchdog_pause_choice, "HA Recovery pause length. Choose how long Viper should leave Home Assistant alone during manual shutdowns or updates.")
        self._describe_control(self.btn_pause_ha_watchdog, "Pause HA Recovery button. Temporarily disables automatic Home Assistant repair while you manually shut down, update, or restart Home Assistant.")
        self._describe_control(self.btn_resume_ha_watchdog, "Resume HA Recovery button. Re-enables automatic Home Assistant recovery after maintenance.")
        watchdog_buttons.Add(self.btn_refresh_ha_watchdog, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_buttons.Add(self.btn_test_ha_watchdog_push, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_buttons.Add(self.ha_watchdog_pause_choice, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_buttons.Add(self.btn_pause_ha_watchdog, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_buttons.Add(self.btn_resume_ha_watchdog, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_sizer.Add(watchdog_buttons, 0, wx.EXPAND)
        sizer.Add(watchdog_sizer, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        smoke_box = wx.StaticBox(self.tab_diagnostics_overview, label="Safe Smoke Test")
        smoke_sizer = wx.StaticBoxSizer(smoke_box, wx.VERTICAL)
        self.smoke_test_txt = wx.TextCtrl(
            self.tab_diagnostics_overview,
            value="Press Run Safe Smoke Test to check Viper readiness without playing audio or triggering doorbell flows.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 230),
        )
        self._describe_control(self.smoke_test_txt, "Safe Smoke Test results. Read-only pass fail report with exact next steps for anything broken.")
        smoke_sizer.Add(self.smoke_test_txt, 1, wx.ALL | wx.EXPAND, 5)
        smoke_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        smoke_grid.AddGrowableCol(0, 1)
        smoke_grid.AddGrowableCol(1, 1)
        self.btn_run_safe_smoke = wx.Button(self.tab_diagnostics_overview, label="Run Safe Smoke Test", size=(-1, 40))
        self.btn_test_front_camera_diag = wx.Button(self.tab_diagnostics_overview, label="Test Front Camera Frame", size=(-1, 40))
        self.btn_test_back_camera_diag = wx.Button(self.tab_diagnostics_overview, label="Test Back Camera Frame", size=(-1, 40))
        self.btn_test_manual_broadcast_diag = wx.Button(self.tab_diagnostics_overview, label="Test Manual Broadcast", size=(-1, 40))
        self.btn_test_pushover_diag = wx.Button(self.tab_diagnostics_overview, label="Test Pushover", size=(-1, 40))
        self.btn_save_ha_snapshot_diag = wx.Button(self.tab_diagnostics_overview, label="Save HA Snapshot", size=(-1, 40))
        self.btn_check_matter_diag = wx.Button(self.tab_diagnostics_overview, label="Check Matter And Alexa", size=(-1, 40))
        self.btn_repair_matter_diag = wx.Button(self.tab_diagnostics_overview, label="Repair Matter And Alexa", size=(-1, 40))
        self.btn_run_safe_smoke.Bind(wx.EVT_BUTTON, self.on_run_safe_smoke_test)
        self.btn_test_front_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "front"))
        self.btn_test_back_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "back"))
        self.btn_test_manual_broadcast_diag.Bind(wx.EVT_BUTTON, self.on_test_diagnostics_manual_broadcast)
        self.btn_test_pushover_diag.Bind(wx.EVT_BUTTON, self.on_test_diagnostics_pushover)
        self.btn_save_ha_snapshot_diag.Bind(wx.EVT_BUTTON, self.on_save_diagnostics_ha_snapshot)
        self.btn_check_matter_diag.Bind(wx.EVT_BUTTON, self.on_check_diagnostics_matter)
        self.btn_repair_matter_diag.Bind(wx.EVT_BUTTON, self.on_repair_diagnostics_matter)
        for button, description in {
            self.btn_run_safe_smoke: "Run Safe Smoke Test button. Checks configuration, Home Assistant, listener, camera URLs, speaker routes, support bundle creation, and active health issues without playing audio.",
            self.btn_test_front_camera_diag: "Test Front Camera Frame button. Captures one frame from the configured front camera stream.",
            self.btn_test_back_camera_diag: "Test Back Camera Frame button. Captures one frame from the configured back camera stream.",
            self.btn_test_manual_broadcast_diag: "Test Manual Broadcast button. Speaks a short manual test announcement through configured speakers.",
            self.btn_test_pushover_diag: "Test Pushover button. Sends a short phone push notification through the configured Pushover account.",
            self.btn_save_ha_snapshot_diag: "Save HA Snapshot button. Saves current important Home Assistant entities and reports what changed since the previous snapshot.",
            self.btn_check_matter_diag: "Check Matter And Alexa button. Checks Viper Matter switches, Samba reachability, Matterbridge, exposed devices, and Alexa pairing fabric.",
            self.btn_repair_matter_diag: "Repair Matter And Alexa button. Repairs Viper-owned Home Assistant Matter duplicates, refreshes Matterbridge configuration, and restarts Matterbridge when needed.",
        }.items():
            self._describe_control(button, description)
            smoke_grid.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        smoke_sizer.Add(smoke_grid, 0, wx.EXPAND)
        sizer.Add(smoke_sizer, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        box = wx.StaticBox(self.tab_diagnostics_overview, label="Diagnostic Actions")
        dsizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        self.btn_about = wx.Button(self.tab_diagnostics_overview, label="About Viper Vision And Data Folders", size=(-1, 40))
        self.btn_about.Bind(wx.EVT_BUTTON, self.on_show_about)
        self.btn_diagnostics = wx.Button(self.tab_diagnostics_overview, label="Run Diagnostics", size=(-1, 40))
        self.btn_diagnostics.Bind(wx.EVT_BUTTON, self.on_run_diagnostics)
        self.btn_support_bundle = wx.Button(self.tab_diagnostics_overview, label="Create Support Report To Email Developer", size=(-1, 40))
        self.btn_support_bundle.Bind(wx.EVT_BUTTON, self.on_create_support_report)
        self.btn_api = wx.Button(self.tab_diagnostics_overview, label="Check API Cost", size=(-1, 40))
        self.btn_api.Bind(wx.EVT_BUTTON, self.on_api)
        self.btn_batt = wx.Button(self.tab_diagnostics_overview, label="Check Doorbell Batteries", size=(-1, 40))
        self.btn_batt.Bind(wx.EVT_BUTTON, self.on_batt)
        self.btn_filter = wx.Button(self.tab_diagnostics_overview, label="Check Refrigerator Filter", size=(-1, 40))
        self.btn_filter.Bind(wx.EVT_BUTTON, self.on_filter)
        for button, description in {
            self.btn_about: "About Viper Vision And Data Folders button. Shows version, app folder, data folder, config path, log path, remote URL, and where support bundles are saved.",
            self.btn_diagnostics: "Run Diagnostics button. Checks Viper configuration, Home Assistant listener status, Home Assistant health, FFmpeg, and recent errors.",
            self.btn_support_bundle: "Create Support Report To Email Developer button. Creates a redacted diagnostic bundle and opens an email draft.",
            self.btn_api: "Check API Cost button. Reads the local API usage log and reports estimated Gemini usage cost.",
            self.btn_batt: "Check Doorbell Batteries button. Checks Home Assistant battery entities for front and back door devices.",
            self.btn_filter: "Check Refrigerator Filter button. Checks refrigerator water filter status from Home Assistant.",
        }.items():
            self._describe_control(button, description)
            dsizer.Add(button, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(dsizer, 0, wx.ALL | wx.EXPAND, 10)
        self.tab_diagnostics_overview.SetSizer(sizer)
        self.refresh_health_summary()
        self.refresh_ha_watchdog_status()


    def on_show_about(self, event):
        diag = diagnostics.collect_diagnostics(
            self.config,
            ha_listener_status=self.ha_listener.status() if hasattr(self, "ha_listener") else {},
        )
        remote_url = f"http://localhost:{cfg.FLASK_PORT}/remote"
        text = "\n".join(
            [
                "Viper Vision",
                f"Version: {diagnostics.APP_VERSION}",
                "",
                "Build and runtime:",
                f"Frozen installer build: {'yes' if diag['app']['frozen'] else 'no'}",
                f"Python: {diag['app']['python']}",
                f"Platform: {diag['app']['platform']}",
                f"Executable: {diag['app']['executable']}",
                "",
                "Folders and files:",
                f"Application folder: {diag['paths']['app_dir']}",
                f"Data folder: {diag['paths']['data_dir']}",
                f"Config file: {diag['paths']['config_file']}",
                f"Main log file: {diag['paths']['log_file']}",
                f"Chimes folder: {cfg.CHIMES_DIR}",
                f"Support bundles save in: {cfg.DATA_DIR}",
                "",
                "Local remote:",
                remote_url,
                "",
                "Privacy:",
                "Diagnostics and support bundles stay on this computer unless you choose to share them.",
                "Support bundles redact Home Assistant tokens, Gemini keys, Pushover keys, MQTT passwords, RTSP passwords, and Ring identifiers.",
            ]
        )
        self._show_about_dialog(text, remote_url)

    def _show_about_dialog(self, text, remote_url):
        dlg = wx.Dialog(self, title="About Viper Vision", size=(760, 560))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.TextCtrl(panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self._describe_control(box, "About Viper Vision. Read-only version, folder, config, log, and privacy information.")
        sizer.Add(box, 1, wx.ALL | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy_data_btn = wx.Button(panel, label="Copy Data Folder")
        open_data_btn = wx.Button(panel, label="Open Data Folder")
        open_remote_btn = wx.Button(panel, label="Open Remote")
        close_btn = wx.Button(panel, label="Close")
        self._describe_control(copy_data_btn, "Copy Data Folder button. Copies Viper's writable data folder path.")
        self._describe_control(open_data_btn, "Open Data Folder button. Opens the folder containing Viper config, logs, chimes, and support bundles.")
        self._describe_control(open_remote_btn, "Open Remote button. Opens Viper's local web remote in your browser.")
        self._describe_control(close_btn, "Close About dialog button.")

        def copy_data_folder(_event):
            value = str(cfg.DATA_DIR)
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(value))
                finally:
                    wx.TheClipboard.Close()
            self.notify("Viper data folder path copied.", priority=10)

        copy_data_btn.Bind(wx.EVT_BUTTON, copy_data_folder)
        open_data_btn.Bind(wx.EVT_BUTTON, lambda _event: self._open_url(str(cfg.DATA_DIR)))
        open_remote_btn.Bind(wx.EVT_BUTTON, lambda _event: self._open_url(remote_url))
        close_btn.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        for button in (copy_data_btn, open_data_btn, open_remote_btn, close_btn):
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        panel.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def on_run_diagnostics(self, event):
        self.notify("Running diagnostics...", priority=10)
        self._safe_submit(self._run_diagnostics)

    def refresh_health_summary(self):
        try:
            diag = self._current_diagnostics(check_ha=False)
            text = diagnostics.health_summary_text(diag)
        except Exception as e:
            logging.exception("Health summary refresh failed")
            text = f"Health summary failed: {e}"
        if hasattr(self, "diagnostics_health_txt"):
            self.diagnostics_health_txt.SetValue(text)
        return text

    def on_refresh_health_summary(self, event):
        text = self.refresh_health_summary()
        first_line = text.splitlines()[0] if text else "Health summary refreshed."
        self.notify(first_line, priority=10)

    def refresh_ha_watchdog_status(self):
        try:
            text = diagnostics.ha_watchdog_status_text(diagnostics.ha_watchdog_status())
        except Exception as e:
            logging.exception("HA watchdog status refresh failed")
            text = f"HA watchdog status failed: {e}"
        if hasattr(self, "ha_watchdog_txt"):
            self.ha_watchdog_txt.SetValue(text)
        return text

    def on_refresh_ha_watchdog(self, event):
        text = self.refresh_ha_watchdog_status()
        first_line = text.splitlines()[0] if text else "HA watchdog refreshed."
        self.notify(first_line, priority=10)

    def on_test_ha_watchdog_push(self, event):
        if hasattr(self, "ha_watchdog_txt"):
            self.ha_watchdog_txt.SetValue("Sending safe HA recovery Pushover test.")
        self.notify("Sending HA recovery Pushover test.", priority=10)
        self._safe_submit(self._run_ha_watchdog_push_test)

    def on_pause_ha_watchdog(self, event):
        minutes = self._selected_ha_watchdog_pause_minutes()
        status = viper_ha_recovery.pause_recovery(minutes, "Manual Home Assistant shutdown or update from Viper")
        text = f"HA recovery paused for {self._ha_watchdog_pause_label(minutes)}. {status.get('message') or ''}"
        if hasattr(self, "ha_watchdog_txt"):
            current = self.refresh_ha_watchdog_status()
            self.ha_watchdog_txt.SetValue(f"{text}\n\n{current}")
        self.notify(text, priority=10)

    def on_resume_ha_watchdog(self, event):
        status = viper_ha_recovery.resume_recovery()
        text = f"HA recovery resumed. {status.get('message') or ''}"
        if hasattr(self, "ha_watchdog_txt"):
            current = self.refresh_ha_watchdog_status()
            self.ha_watchdog_txt.SetValue(f"{text}\n\n{current}")
        self.notify(text, priority=10)

    def _selected_ha_watchdog_pause_minutes(self):
        choice = getattr(self, "ha_watchdog_pause_choice", None)
        value = choice.GetStringSelection() if choice else "2 hours"
        return {
            "30 minutes": 30,
            "1 hour": 60,
            "2 hours": 120,
            "4 hours": 240,
            "8 hours": 480,
            "Until tomorrow": 12 * 60,
        }.get(value, 120)

    def _ha_watchdog_pause_label(self, minutes):
        if minutes < 60:
            return f"{minutes} minutes"
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"

    def _run_ha_watchdog_push_test(self):
        ok = viper_ha_recovery.send_recovery_test_push()
        text = (
            "HA recovery Pushover test sent. Check your phone."
            if ok
            else "HA recovery Pushover test failed. Check Pushover settings and the Viper log."
        )
        wx.CallAfter(self._finish_ha_watchdog_action, text)

    def _finish_ha_watchdog_action(self, text):
        if hasattr(self, "ha_watchdog_txt"):
            current = self.refresh_ha_watchdog_status()
            self.ha_watchdog_txt.SetValue(f"{text}\n\n{current}")
        self.notify(text, priority=10)

    def _smoke_result_line(self, label, ok, detail="", fix=""):
        state = "PASS" if ok else "FIX"
        parts = [f"{state}: {label}"]
        if detail:
            parts.append(str(detail))
        if not ok and fix:
            parts.append(f"Next: {fix}")
        return ". ".join(parts)

    def _smoke_support_bundle_probe(self):
        probe_dir = cfg.DATA_DIR / "smoke_test"
        result = diagnostics.create_support_bundle(
            self.config,
            ha_listener_status=self.ha_listener.status() if hasattr(self, "ha_listener") else {},
            output_dir=probe_dir,
        )
        path = Path(result.get("path") or "")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return bool(result.get("ok"))

    def _collect_safe_smoke_results(self):
        runtime = cfg.get_runtime_settings(self.config, include_env=True)
        ha = runtime["home_assistant"]
        api = runtime["api"]
        doorbell = runtime["doorbell"]
        speakers = runtime["speakers"]
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        diag = diagnostics.collect_diagnostics(
            self.config,
            ha_listener_status=listener,
        )
        health = diag.get("health", {})
        results = []

        results.append(("Config file", cfg.CONFIG_FILE.exists(), str(cfg.CONFIG_FILE), "Save settings from the app once."))
        results.append(("Home Assistant host", bool(ha.get("ha_ip")), ha.get("ha_ip") or "missing", "Open Setup Wizard and enter the Home Assistant address."))
        results.append(("Home Assistant token", bool(ha.get("ha_token")), "available" if ha.get("ha_token") else "missing", "Paste a long-lived token in setup or restore the saved secret."))
        results.append(("Gemini key", bool(api.get("gemini_api_key")), "available" if api.get("gemini_api_key") else "missing", "Add a Gemini key or choose non-Gemini speech/vision options."))
        results.append(("FFmpeg", bool(diag.get("ffmpeg", {}).get("available")), diag.get("ffmpeg", {}).get("resolved") or "not found", "Install FFmpeg or check the configured FFmpeg path."))
        results.append(("HA listener", bool(listener.get("connected")), listener.get("last_error") or listener.get("last_host") or "connected", "Check Home Assistant network/token, then restart or wait for reconnect."))

        if ha.get("ha_ip") and ha.get("ha_token"):
            ha_connection = discovery.test_ha_connection(
                token=ha.get("ha_token"),
                ha_ip=ha.get("ha_ip"),
                ha_port=ha.get("ha_port") or "8123",
                timeout=5,
            )
            results.append(("HA API", bool(ha_connection.get("ok")), ha_connection.get("message") or ha_connection.get("error") or "", "Check Home Assistant address, token, and whether HA Core is running."))
            states_result = discovery.get_ha_states(
                token=ha.get("ha_token"),
                ha_ip=ha.get("ha_ip"),
                ha_port=ha.get("ha_port") or "8123",
                timeout=5,
            )
            fridge_histories = {}
            for entity_id in (diagnostics.FRIDGE_DOOR_ENTITY, diagnostics.FREEZER_DOOR_ENTITY):
                history_result = discovery.get_entity_history(
                    entity_id,
                    token=ha.get("ha_token"),
                    ha_ip=ha.get("ha_ip"),
                    ha_port=ha.get("ha_port") or "8123",
                    timeout=5,
                )
                if history_result.get("ok"):
                    fridge_histories[entity_id] = history_result.get("history", [])
            fridge_health = diagnostics.refrigerator_door_sensor_diagnostics(
                states=states_result.get("states") if states_result.get("ok") else None,
                histories=fridge_histories,
            )
            results.append((
                "Refrigerator door sensors",
                bool(fridge_health.get("ok")),
                fridge_health.get("message") or fridge_health.get("status") or "not checked",
                "Open Home Assistant Developer Tools and verify the fridge door entity changes to on when opened.",
            ))
        else:
            results.append(("HA API", False, "host or token missing", "Complete Home Assistant setup first."))

        results.append(("Front door trigger", bool(doorbell.get("front_trigger_entity_id")), doorbell.get("front_trigger_entity_id") or "missing", "Choose the front Ring trigger entity in Doorbell Vision setup."))
        results.append(("Back door trigger", bool(doorbell.get("back_trigger_entity_id")), doorbell.get("back_trigger_entity_id") or "missing", "Choose the back Ring trigger entity in Doorbell Vision setup."))
        front_rtsp = doorbell.get("configured_rtsp_front") or doorbell.get("raw_rtsp_front") or ""
        back_rtsp = doorbell.get("configured_rtsp_back") or doorbell.get("raw_rtsp_back") or ""
        results.append(("Front camera RTSP URL", bool(front_rtsp), front_rtsp or "missing", "Find and save a front Ring-MQTT live stream."))
        results.append(("Back camera RTSP URL", bool(back_rtsp), back_rtsp or "missing", "Find and save a back Ring-MQTT live stream."))

        routes = speakers.get("routes", {})
        results.append(("Speaker routes", bool(speakers.get("enabled_count") and routes.get("doorbell") and routes.get("utilities") and routes.get("fridge")), f"{speakers.get('enabled_count', 0)} enabled; doorbell {len(routes.get('doorbell', []))}, utilities {len(routes.get('utilities', []))}, fridge {len(routes.get('fridge', []))}", "Use Choose Alert Speakers and enable doorbell, utilities, and fridge/freezer routes."))
        results.append(("Manual broadcast route", bool(speakers.get("enabled_count")), f"{speakers.get('enabled_count', 0)} enabled speaker(s)", "Add or enable at least one speaker."))
        results.append(("Support bundle", self._smoke_support_bundle_probe(), "temporary bundle created and removed", "Check write permission in the Viper data folder."))
        results.append(("Active health issues", not health.get("active_issues"), "; ".join(health.get("active_issues") or ["none"]), "Open the Health Summary and fix listed active issues."))
        return results

    def _camera_rtsp_url_for_side(self, side):
        triggers = self.config.get("doorbell_triggers", {})
        trigger = triggers.get(side, {}) if isinstance(triggers, dict) and isinstance(triggers.get(side), dict) else {}
        return str(trigger.get("rtsp_url") or self.config.get("rtsp_front" if side == "front" else "rtsp_back") or "").strip()

    def on_run_safe_smoke_test(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Running safe smoke test. This does not play audio or trigger doorbell flows.")
        self.notify("Running safe smoke test.", priority=10)
        self._safe_submit(self._run_safe_smoke_test)

    def _run_safe_smoke_test(self):
        try:
            lines = self._format_safe_smoke_report(self._collect_safe_smoke_results())
        except Exception as e:
            logging.exception("Safe smoke test failed")
            lines = f"Smoke Test: ERROR\n\nThe smoke test itself failed: {e}\nNext: Create a support report and send the diagnostics zip."
        wx.CallAfter(self._finish_safe_smoke_test, lines)

    def _format_safe_smoke_report(self, results):
        failed = [item for item in results if not item[1]]
        lines = [
            f"Smoke Test: {'PASS' if not failed else 'NEEDS ATTENTION'}",
            f"Passed {len(results) - len(failed)} of {len(results)} checks.",
            "",
        ]
        for label, ok, detail, fix in results:
            lines.append(self._smoke_result_line(label, ok, detail, fix))
        lines.append("")
        if failed:
            lines.append("Most important next step:")
            lines.append(failed[0][3] or f"Fix {failed[0][0]}.")
        else:
            lines.append("Optional next step: use the camera/audio buttons below for live hardware confirmation.")
        return "\n".join(lines)

    def _finish_safe_smoke_test(self, text):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue(text)
        if hasattr(self, "diagnostics_health_txt"):
            self.refresh_health_summary()
        first_line = str(text or "").splitlines()[0] if text else "Smoke test finished."
        self.notify(first_line, priority=10)

    def on_test_diagnostics_camera(self, event, side):
        url = self._camera_rtsp_url_for_side(side)
        if not url:
            message = f"{side.title()} camera is not configured. Save a Ring-MQTT RTSP stream first."
            if hasattr(self, "smoke_test_txt"):
                self.smoke_test_txt.SetValue(message)
            self.notify(message, priority=10)
            return
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue(f"Testing {side} camera frame capture.")
        self.notify(f"Testing {side} camera frame.", priority=10)
        self._safe_submit(self._run_diagnostics_camera_test, side, url)

    def _run_diagnostics_camera_test(self, side, url):
        try:
            frame = vision.grab_frame(url, cfg.DATA_DIR / "rtsp_test", f"diagnostics_{side}", min_bytes=14000, timeout=8)
            ok = bool(frame)
            message = f"{side.title()} camera frame test {'passed' if ok else 'failed'}."
            if frame:
                message += f" Captured: {Path(frame).name}"
            else:
                message += " No usable frame was captured."
        except Exception as e:
            logging.exception("Diagnostics camera test failed side=%s", side)
            message = f"{side.title()} camera frame test failed. {e}"
        wx.CallAfter(self._finish_diagnostics_action, message)

    def on_test_diagnostics_manual_broadcast(self, event):
        message = "Viper smoke test broadcast."
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Sending manual broadcast smoke test.")
        self.notify("Sending manual broadcast smoke test.", priority=10)
        self._safe_submit(self._run_diagnostics_manual_broadcast, message)

    def _run_diagnostics_manual_broadcast(self, message):
        result = self._dispatch_broadcast_message(message, channel="manual")
        ok = bool(result.get("ok"))
        text = f"Manual broadcast test {'sent' if ok else 'failed'}. {result.get('message') or ''}"
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_test_diagnostics_pushover(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Sending Pushover test notification.")
        self.notify("Sending Pushover test notification.", priority=10)
        self._safe_submit(self._run_diagnostics_pushover_test)

    def _run_diagnostics_pushover_test(self):
        settings = cfg.get_api_settings(self.config, include_env=True)
        if not settings.get("pushover_enabled") or not settings.get("pushover_user_key") or not settings.get("pushover_api_token"):
            text = "Pushover test failed. Pushover is not enabled or one of the Pushover keys is missing."
            wx.CallAfter(self._finish_diagnostics_action, text)
            return
        ok = audio._send_text_pushover(
            "Viper Vision Diagnostics",
            f"Test Pushover notification from Viper Vision at {datetime.now().strftime('%I:%M %p')}.",
        )
        text = "Pushover test sent. Check your phone." if ok else "Pushover test failed. Open Diagnostics or the Viper log for the Pushover error."
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_save_diagnostics_ha_snapshot(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Saving Home Assistant integration snapshot.")
        self.notify("Saving Home Assistant snapshot.", priority=10)
        self._safe_submit(self._run_diagnostics_ha_snapshot)

    def on_check_diagnostics_matter(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Checking Matter, Alexa, Samba, and Matterbridge.")
        self.notify("Checking Matter and Alexa.", priority=10)
        self._safe_submit(self._run_diagnostics_matter_check)

    def _run_diagnostics_matter_check(self):
        try:
            report = viper_matter.matter_health_report(self.config)
            text = viper_matter.format_matter_health_report(report)
        except Exception as e:
            logging.exception("Matter diagnostics check failed")
            text = f"Matter/Alexa check failed: {e}"
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_repair_diagnostics_matter(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Repairing Matter and Alexa setup.")
        self.notify("Repairing Matter and Alexa setup.", priority=10)
        self._safe_submit(self._run_diagnostics_matter_repair)

    def _run_diagnostics_matter_repair(self):
        try:
            result = viper_matter.repair_matter_stack(self.config, cleanup_registry=True)
            text = viper_matter.format_matter_repair_report(result)
        except Exception as e:
            logging.exception("Matter diagnostics repair failed")
            text = f"Matter/Alexa repair failed: {e}"
        wx.CallAfter(self._finish_diagnostics_action, text)

    def _run_diagnostics_ha_snapshot(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        states = discovery.get_ha_states(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=8,
        )
        if not states.get("ok"):
            text = f"HA snapshot failed. {states.get('message') or states.get('error') or 'Could not read Home Assistant states.'}"
            wx.CallAfter(self._finish_diagnostics_action, text)
            return
        result = diagnostics.save_ha_integration_snapshot(
            self.config,
            ha_states=states.get("states", []),
            ha_listener_status=self.ha_listener.status() if hasattr(self, "ha_listener") else {},
        )
        diff = result.get("diff", {})
        text = "\n".join(
            [
                "HA snapshot saved.",
                f"Path: {result.get('path')}",
                f"Entities: {result.get('snapshot', {}).get('entity_count', 0)}",
                f"Changes since previous snapshot: added {len(diff.get('added', []))}, removed {len(diff.get('removed', []))}, changed {len(diff.get('changed', []))}.",
            ]
        )
        wx.CallAfter(self._finish_diagnostics_action, text)

    def _finish_diagnostics_action(self, message):
        if hasattr(self, "smoke_test_txt"):
            current = self.smoke_test_txt.GetValue()
            prefix = current.strip()
            self.smoke_test_txt.SetValue((prefix + "\n\n" if prefix else "") + str(message))
        self.notify(str(message), priority=10)

    def _run_diagnostics(self):
        try:
            diag = self._current_diagnostics(check_ha=True)
            text = diagnostics.diagnostics_text(diag)
            wx.CallAfter(self._show_text_dialog, "Viper Vision Diagnostics", text)
        except Exception as e:
            logging.exception("Diagnostics failed")
            wx.CallAfter(self.notify, f"Diagnostics failed: {e}", priority=10)

    def on_create_support_bundle(self, event):
        self.on_create_support_report(event)

    def on_create_support_report(self, event):
        self.record_setup_event("support_report_start", "Creating support report.")
        self.notify("Creating support bundle...", priority=10)
        self._safe_submit(self._run_support_bundle)

    def _run_support_bundle(self):
        try:
            diag = self._current_diagnostics(check_ha=True)
            result = diagnostics.create_support_bundle(
                self.config,
                ha_listener_status=diag.get("ha_listener", {}),
                ha_connection=diag.get("ha_connection", {}),
                ha_health=diag.get("ha_health", {}),
                setup_summary=self.build_setup_checklist_summary(),
                setup_events=self.setup_events,
                last_setup_status=self.last_setup_status,
            )
            self.record_setup_event("support_report_created", "Support report bundle created.", path=result.get("path", ""))
            wx.CallAfter(self.notify, f"Support report created: {result['path']}", priority=10)
            wx.CallAfter(self._show_support_report_dialog, result)
        except Exception as e:
            logging.exception("Support bundle failed")
            self.record_setup_event("support_report_failed", str(e))
            wx.CallAfter(self.notify, f"Support bundle failed: {e}", priority=10)

    def _support_email_url(self, bundle_path):
        subject = "Viper Vision Support Report"
        body = "\n".join([
            "Hi,",
            "",
            "I created a Viper Vision support report. Please attach this zip file before sending:",
            str(bundle_path or ""),
            "",
            f"Viper version: {diagnostics.APP_VERSION}",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            "",
            "Notes about what went wrong:",
            "",
        ])
        return f"mailto:{self._support_email()}?subject={quote(subject)}&body={quote(body)}"

    def _open_support_email_draft(self, bundle_path):
        self.record_setup_event("support_email_draft_open", "Opening support email draft.")
        self._open_url(self._support_email_url(bundle_path))

    def _show_support_report_dialog(self, result):
        path = result.get("path", "") if isinstance(result, dict) else ""
        included = result.get("included", []) if isinstance(result, dict) else []
        dlg = wx.Dialog(self, title="Support Report Created", size=(760, 520))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        text = "\n".join([
            "Support report created.",
            "",
            path,
            "",
            "This zip includes redacted diagnostics, setup status, setup event history, recent logs, API usage, and crash information if present.",
            "Secrets are redacted, but review the zip before sharing.",
            "Press Open Email Draft to start an email to the Viper developer. Attach the zip file manually before sending.",
            "",
            f"Files included: {len(included)}",
        ])
        box = wx.TextCtrl(panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self._describe_control(box, "Support report details. Read only. Shows the support zip path, what was included, and how to email it.")
        sizer.Add(box, 1, wx.ALL | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy_btn = wx.Button(panel, label="Copy Zip Path")
        folder_btn = wx.Button(panel, label="Open Folder")
        email_btn = wx.Button(panel, label="Open Email Draft")
        close_btn = wx.Button(panel, label="Close")
        self._describe_control(copy_btn, "Copy Zip Path button. Copies the support zip path to the clipboard.")
        self._describe_control(folder_btn, "Open Folder button. Opens the folder containing the support zip.")
        self._describe_control(email_btn, "Open Email Draft button. Opens an email draft addressed to the Viper developer. Attach the zip manually.")
        self._describe_control(close_btn, "Close support report dialog button. Closes this support report dialog.")

        def copy_path(_event):
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(path))
                finally:
                    wx.TheClipboard.Close()
            self.notify("Support report path copied.", priority=10)

        def open_folder(_event):
            folder = str(Path(path).parent) if path else str(cfg.DATA_DIR)
            self._open_url(folder)

        copy_btn.Bind(wx.EVT_BUTTON, copy_path)
        folder_btn.Bind(wx.EVT_BUTTON, open_folder)
        email_btn.Bind(wx.EVT_BUTTON, lambda _event: self._open_support_email_draft(path))
        close_btn.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        for button in (copy_btn, folder_btn, email_btn, close_btn):
            buttons.Add(button, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 5)
        panel.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _show_text_dialog(self, title, text):
        dlg = wx.Dialog(self, title=title, size=(760, 560))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.TextCtrl(panel, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        box.SetName(f"{title}. Read only diagnostic text.")
        sizer.Add(box, 1, wx.ALL | wx.EXPAND, 10)
        close = wx.Button(panel, label="Close")
        close.Bind(wx.EVT_BUTTON, lambda _event: dlg.EndModal(wx.ID_OK))
        sizer.Add(close, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        panel.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def setup_recent_events_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.StaticBox(self.tab_recent_events, label="Recent Events")
        bsizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.btn_refresh_recent_events = wx.Button(self.tab_recent_events, label="Refresh Recent Events", size=(-1, 40))
        self.btn_refresh_recent_events.Bind(wx.EVT_BUTTON, self.on_refresh_recent_events)
        self._describe_control(
            self.btn_refresh_recent_events,
            "Refresh Recent Events button. Updates the recent Viper event and Home Assistant recovery journal.",
        )
        bsizer.Add(self.btn_refresh_recent_events, 0, wx.ALL | wx.EXPAND, 5)

        self.recent_events_txt = wx.TextCtrl(
            self.tab_recent_events,
            value=self._build_recent_events_text(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP,
            size=(-1, 520),
        )
        self._describe_control(
            self.recent_events_txt,
            "Recent Events. Read-only timeline of Viper actions, Home Assistant listener status, HVAC refreshes, broadcasts, and SmartThings recovery events.",
        )
        bsizer.Add(self.recent_events_txt, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(bsizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_recent_events.SetSizer(sizer)

    def _build_recent_events_text(self):
        lines = ["Health History", ""]
        lines.extend(self._build_health_history_lines())
        lines.extend(["", "Recent Events", ""])
        lines.extend(format_recent_events(limit=20))
        health_events = viper_health.recent_health_events(limit=12)
        lines.extend(["", "Recent Home Assistant recovery events:"])
        if not health_events:
            lines.append("No recent recovery events recorded.")
        else:
            for item in reversed(health_events):
                lines.append(f"{item.get('timestamp')}: {item.get('event_type')} {item.get('status')}: {item.get('message')}")
        return "\n".join(lines)

    def _format_history_timestamp(self, value):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            return "never"
        try:
            return datetime.fromtimestamp(number).strftime("%Y-%m-%d %I:%M:%S %p")
        except Exception:
            return "unknown"

    def _last_runtime_event(self, kinds):
        wanted = {str(item).lower() for item in (kinds if isinstance(kinds, (list, tuple, set)) else [kinds])}
        for event in recent_events(limit=40):
            if str(event.get("kind") or "").lower() in wanted:
                return event
        return {}

    def _last_health_recovery_event(self):
        events = viper_health.recent_health_events(limit=20)
        for item in reversed(events):
            if str(item.get("event_type") or "").startswith("smartthings_reload"):
                return item
        return {}

    def _build_health_history_lines(self):
        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        last_doorbell = self._last_runtime_event("doorbell")
        last_hvac = self._last_runtime_event("hvac")
        last_broadcast = self._last_runtime_event("broadcast")
        last_recovery = self._last_health_recovery_event()
        last_routed = listener.get("last_routed_action") or {}
        lines = [
            f"HA listener: {'connected' if listener.get('connected') else 'not connected'}.",
            f"Last connected: {self._format_history_timestamp(listener.get('last_connected_at'))}.",
            f"Last reconnect attempt: {self._format_history_timestamp(listener.get('last_reconnect_at'))}.",
            f"Reconnect count: {listener.get('reconnect_count', 0)}.",
            f"Last successful HA poll: {self._format_history_timestamp(listener.get('last_successful_poll_at'))}.",
            f"Last HA event: {listener.get('last_event_entity') or 'none'}; {listener.get('last_event_old_state') or ''} -> {listener.get('last_event_new_state') or ''}.",
            f"Last routed action: {last_routed.get('type') if isinstance(last_routed, dict) else last_routed or 'none'}.",
            f"Last doorbell action: {last_doorbell.get('time', 'none')}: {last_doorbell.get('message', 'none')}.",
            f"Last HVAC action: {last_hvac.get('time', 'none')}: {last_hvac.get('message', 'none')}.",
            f"Last broadcast: {last_broadcast.get('time', 'none')}: {last_broadcast.get('message', 'none')}.",
            f"Last SmartThings reload: {self._format_history_timestamp(listener.get('last_smartthings_reload_at'))}; result: {listener.get('last_smartthings_reload_result') or 'none'}.",
            f"SmartThings reloads in 24 hours: {listener.get('repeated_smartthings_reloads_24h', 0)}.",
        ]
        if last_recovery:
            lines.append(
                f"Last SmartThings recovery journal: {last_recovery.get('timestamp')}: "
                f"{last_recovery.get('event_type')} {last_recovery.get('status')}: {last_recovery.get('message')}"
            )
        else:
            lines.append("Last SmartThings recovery journal: none.")
        return lines

    def on_refresh_recent_events(self, event):
        if hasattr(self, "recent_events_txt"):
            self.recent_events_txt.SetValue(self._build_recent_events_text())
        record_event("diagnostics", "Recent Events refreshed.")
        self.notify("Recent Events refreshed.", priority=10, speak=False)

    def setup_speed_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.StaticBox(self.tab_speed, label="Speed Diagnostics")
        bsizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.btn_refresh_speed = wx.Button(self.tab_speed, label="Refresh speed diagnostics", size=(-1, 40))
        self.btn_refresh_speed.Bind(wx.EVT_BUTTON, self.on_refresh_speed)
        self._describe_control(
            self.btn_refresh_speed,
            "Refresh speed diagnostics button. Reads the latest Viper log and summarizes doorbell, TTS, speaker, and chime timing.",
        )
        bsizer.Add(self.btn_refresh_speed, 0, wx.ALL | wx.EXPAND, 5)

        self.speed_status_txt = wx.TextCtrl(
            self.tab_speed,
            value="Press Refresh speed diagnostics to read the latest timing log.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 420),
        )
        self._describe_control(
            self.speed_status_txt,
            "Speed diagnostics status. This read only box summarizes recent timing measurements from the Viper log.",
        )
        bsizer.Add(self.speed_status_txt, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(bsizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_speed.SetSizer(sizer)

    def setup_ha_status_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.StaticBox(self.tab_ha_status, label="Home Assistant Status")
        bsizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.btn_refresh_ha_status = wx.Button(self.tab_ha_status, label="Check Home Assistant status", size=(-1, 40))
        self.btn_refresh_ha_status.Bind(wx.EVT_BUTTON, self.on_refresh_ha_status)
        self._describe_control(
            self.btn_refresh_ha_status,
            "Check Home Assistant status button. Tests the Home Assistant connection and verifies configured speaker and automation entities.",
        )
        bsizer.Add(self.btn_refresh_ha_status, 0, wx.ALL | wx.EXPAND, 5)

        self.ha_listener_status_txt = wx.StaticText(self.tab_ha_status, label="HA listener: starting")
        self._describe_control(
            self.ha_listener_status_txt,
            "Home Assistant listener status. This tells whether Viper is directly listening for Home Assistant state changes.",
        )
        bsizer.Add(self.ha_listener_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        self.ha_status_txt = wx.TextCtrl(
            self.tab_ha_status,
            value="Press Check Home Assistant status to test Home Assistant and configured entities.",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 420),
        )
        self._describe_control(
            self.ha_status_txt,
            "Home Assistant status. This read only box lists connection status, entity checks, and useful counts.",
        )
        bsizer.Add(self.ha_status_txt, 1, wx.ALL | wx.EXPAND, 5)
        sizer.Add(bsizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_ha_status.SetSizer(sizer)

    def on_refresh_speed(self, event):
        self.speed_status_txt.SetValue("Reading speed log...")
        safe_submit(self._run_speed_diagnostics)

    def _run_speed_diagnostics(self):
        log_path = cfg.DATA_DIR / "viper_full_debug.log"
        if not log_path.exists():
            wx.CallAfter(self.speed_status_txt.SetValue, f"No speed log found at {log_path}.")
            return
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            summary = self._build_speed_summary(text)
        except Exception as e:
            summary = f"Could not read speed log: {e}"
        wx.CallAfter(self.speed_status_txt.SetValue, summary)

    def _latest_trace_block(self, lines, trace):
        if not trace:
            return []
        start = next((i for i, line in enumerate(lines) if trace in line and "webhook_received" in line), None)
        if start is None:
            start = next((i for i, line in enumerate(lines) if trace in line), None)
        if start is None:
            return []
        end = min(len(lines), start + 180)
        return lines[start:end]

    def _first_float(self, pattern, text):
        match = re.search(pattern, text)
        return float(match.group(1)) if match else None

    def _last_float(self, pattern, text):
        matches = re.findall(pattern, text)
        return float(matches[-1]) if matches else None

    def _format_seconds(self, value):
        return f"{value:.2f} seconds" if value is not None else "not found"

    def _median(self, values):
        if not values:
            return None
        values = sorted(values)
        mid = len(values) // 2
        if len(values) % 2:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2

    def _build_speed_summary(self, text):
        lines = text.splitlines()
        traces = []
        for trace in re.findall(r"trace=(doorbell-[a-z]+-\d+)", text):
            if trace not in traces:
                traces.append(trace)

        output = ["Speed Diagnostics", f"Log: {cfg.DATA_DIR / 'viper_full_debug.log'}", ""]
        latest = ""
        for trace in reversed(traces):
            if "fast_capture=" in "\n".join(self._latest_trace_block(lines, trace)):
                latest = trace
                break
        if not latest and traces:
            latest = traces[-1]
        if not latest:
            output.append("No doorbell traces found yet.")
        else:
            block_lines = self._latest_trace_block(lines, latest)
            block = "\n".join(block_lines)
            output.extend([
                f"Latest doorbell trace: {latest}",
                f"RTSP capture: {self._format_seconds(self._first_float(r'fast_capture=([0-9.]+)s', block))}",
                f"Total to vision verdict: {self._format_seconds(self._first_float(r'total_to_verdict=([0-9.]+)s', block))}",
                f"Audio submitted: {self._format_seconds(self._first_float(r'audio_notification_submitted=([0-9.]+)s', block))}",
                f"Doorbell TTS path: {self._format_seconds(self._last_float(r'TTS path for doorbell:unknown completed in ([0-9.]+)s', block))}",
                f"Home Assistant play request: {self._format_seconds(self._last_float(r'HA PLAY TIMING .* submitted in ([0-9.]+)s', block))}",
                f"Sonos play request: {self._format_seconds(self._last_float(r'SONOS DISPATCH TIMING - .* submitted in ([0-9.]+)s', block))}",
                f"Pushover sent: {'yes' if '[PUSHOVER]' in block else 'not found'}",
            ])
            engine_match = re.search(r"category=doorbell engine=([a-z]+)", block)
            if engine_match:
                output.append(f"Doorbell TTS engine: {engine_match.group(1)}")

        output.append("")
        output.append("Recent medians from the whole log:")
        recent_doorbell_blocks = []
        for trace in reversed(traces):
            block = "\n".join(self._latest_trace_block(lines, trace))
            if "fast_capture=" in block:
                recent_doorbell_blocks.append(block)
            if len(recent_doorbell_blocks) >= 8:
                break
        capture_values = [self._first_float(r"fast_capture=([0-9.]+)s", b) for b in recent_doorbell_blocks]
        verdict_values = [self._first_float(r"total_to_verdict=([0-9.]+)s", b) for b in recent_doorbell_blocks]
        capture_values = [v for v in capture_values if v is not None]
        verdict_values = [v for v in verdict_values if v is not None]
        gemini_tts_values = [float(v) for v in re.findall(r"Gemini TTS API response took: ([0-9.]+)s", text)][-20:]
        ha_play_values = [float(v) for v in re.findall(r"HA PLAY TIMING .* submitted in ([0-9.]+)s", text)][-20:]
        sonos_values = [float(v) for v in re.findall(r"SONOS .* TIMING - .* submitted in ([0-9.]+)s", text)][-20:]
        output.extend([
            f"Doorbell RTSP capture median: {self._format_seconds(self._median(capture_values))}",
            f"Doorbell verdict median: {self._format_seconds(self._median(verdict_values))}",
            f"Gemini TTS API median: {self._format_seconds(self._median(gemini_tts_values))}",
            f"HA play request median: {self._format_seconds(self._median(ha_play_values))}",
            f"Sonos play request median: {self._format_seconds(self._median(sonos_values))}",
        ])
        output.append("")
        output.append("Notes:")
        output.append("If doorbell TTS engine says google, the latest doorbell did not use Gemini voice.")
        output.append("If HA play is above 1 second but Sonos is fast, the delay is likely Home Assistant media service response time.")
        return "\n".join(output)

    def on_refresh_ha_status(self, event):
        self.ha_status_txt.SetValue("Checking Home Assistant...")
        record_event("diagnostics", "Home Assistant status check started.")
        safe_submit(self._run_ha_status_check)

    def _run_ha_status_check(self):
        try:
            summary = self._build_ha_status_summary()
        except Exception as e:
            summary = f"Home Assistant status check failed: {e}"
            record_event("diagnostics", summary)
        else:
            record_event("diagnostics", "Home Assistant status check finished.")
        wx.CallAfter(self.ha_status_txt.SetValue, summary)
        wx.CallAfter(self.refresh_system_health_display)

    def _format_ha_status_timestamp(self, value):
        try:
            value = float(value or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            return "never"
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")

    def _build_ha_status_summary(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        lines = [
            "Home Assistant Status",
            f"Host: {ha_settings.get('ha_ip') or 'not configured'}:{ha_settings.get('ha_port') or '8123'}",
            "",
        ]
        listener_status = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        lines.extend([
            f"Viper HA listener enabled: {'yes' if self.config.get('ha_listener_enabled', True) else 'no'}",
            f"Viper HA listener connected: {'yes' if listener_status.get('connected') else 'no'}",
            f"Viper HA listener last error: {listener_status.get('last_error') or 'none'}",
            f"Last HA event entity: {listener_status.get('last_event_entity') or 'none'}",
            f"Last HA event raw state: {listener_status.get('last_event_old_state') or ''} -> {listener_status.get('last_event_new_state') or ''}",
            f"Last HA event normalized: {listener_status.get('last_event_old_normalized') or ''} -> {listener_status.get('last_event_new_normalized') or ''}",
            f"Last HA event routed actions: {listener_status.get('last_event_action_count', 0)}",
            f"Last routed action: {listener_status.get('last_routed_action') or 'none'}",
            f"Last fridge/freezer poll: {self._format_ha_status_timestamp(listener_status.get('last_fridge_poll_at'))}",
            f"Last vacuum poll: {self._format_ha_status_timestamp(listener_status.get('last_cinderella_poll_at'))}",
            f"Last successful poll: {self._format_ha_status_timestamp(listener_status.get('last_successful_poll_at'))}",
            f"Reconnect count: {listener_status.get('reconnect_count', 0)}",
            f"Poll failure count: {listener_status.get('poll_failure_count', 0)}",
            f"Last poll error: {listener_status.get('last_poll_error') or 'none'}",
            "",
        ])

        connection = discovery.test_ha_connection(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        if not connection.get("ok"):
            lines.append(f"Connection: failed. {connection.get('message') or connection.get('error')}")
            return "\n".join(lines)
        lines.append(f"Connection: ok. Entities visible: {connection.get('entity_count', 'unknown')}")

        scan = discovery.discover_ha_entities(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=8,
        )
        if scan.get("ok"):
            categories = scan.get("categories", {})
            lines.extend([
                "",
                "Discovery counts:",
                f"Media players: {len(categories.get('media_players', []))}",
                f"Ring cameras: {len(categories.get('ring_cameras', []))}",
                f"Door sensors: {len(categories.get('door_sensors', []))}",
                f"Fridge sensors: {len(categories.get('fridge_sensors', []))}",
                f"Freezer sensors: {len(categories.get('freezer_sensors', []))}",
                f"Roborock entities: {len(categories.get('roborock_entities', []))}",
            ])
        else:
            lines.append(f"Discovery: failed. {scan.get('message') or scan.get('error')}")

        triggers = self.config.get("doorbell_triggers", {})
        lines.extend(["", "Doorbell RTSP triggers:"])
        for side in ("front", "back"):
            trigger = triggers.get(side, {}) if isinstance(triggers, dict) else {}
            label = "Front" if side == "front" else "Back"
            lines.append(
                f"{label}: enabled={bool(trigger.get('enabled'))}, source={trigger.get('source') or 'ha_state'}, "
                f"trigger entity={trigger.get('trigger_entity_id') or 'not selected'}, "
                f"RTSP={'set' if trigger.get('rtsp_url') else 'missing'}"
            )

        entity_ids = []
        for name, speaker in self.config.get("speakers", {}).items():
            if speaker.get("type") in {"ha", "alexa"} and speaker.get("id"):
                entity_ids.append((f"Speaker {name}", speaker["id"]))
        ice_entities = self._configured_ice_maker_entities()
        for label, entity_id in [
            ("Fridge door", "binary_sensor.refrigerator_fridge_door"),
            ("Freezer door", "binary_sensor.refrigerator_freezer_door"),
            ("Water filter", "sensor.refrigerator_water_filter_usage"),
            ("Ice maker switch", ice_entities["switch"]),
            ("Ice maker keep-on helper", ice_entities["keep_on"]),
            ("Ice usage counter", ice_entities["counter"]),
            ("Cinderella status", self.config.get("cinderella_status_entity") or "sensor.cinderella_status"),
            ("Cinderella vacuum error", self.config.get("cinderella_vacuum_error_entity") or "sensor.cinderella_vacuum_error"),
            ("Cinderella dock error", self.config.get("cinderella_dock_error_entity") or "sensor.cinderella_dock_dock_error"),
            ("Cinderella mop drying", self.config.get("cinderella_mop_drying_entity") or "binary_sensor.cinderella_dock_mop_drying"),
        ]:
            entity_ids.append((label, entity_id))

        lines.append("")
        lines.append("Entity checks:")
        seen = set()
        for label, entity_id in entity_ids:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            result = discovery.validate_entity_exists(
                entity_id,
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port"),
                timeout=5,
            )
            if result.get("ok") and result.get("exists"):
                state = result.get("entity", {}).get("state", "unknown")
                lines.append(f"{label}: found. {entity_id}. State: {state}")
            elif result.get("ok"):
                lines.append(f"{label}: missing. {entity_id}")
            else:
                lines.append(f"{label}: check failed. {entity_id}. {result.get('message') or result.get('error')}")

        lines.append("")
        lines.append("Configured speakers:")
        speakers = self.config.get("speakers", {})
        if not speakers:
            lines.append("No speakers configured in Viper.")
        else:
            for name, speaker in speakers.items():
                enabled = "enabled" if speaker.get("enabled", True) else "disabled"
                lines.append(f"{name}: {speaker.get('type')} {speaker.get('id')} {enabled}")
        return "\n".join(lines)

