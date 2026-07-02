import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import wx

import viper_audio as audio
import viper_config as cfg
import viper_diagnostics as diagnostics
import viper_discovery as discovery
import viper_health
import viper_ha_recovery
import viper_ha_listener as ha_listener
import viper_matter
import viper_vision as vision


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
        self.btn_pause_ha_watchdog = wx.Button(self.tab_diagnostics_overview, label="Pause HA Recovery 2 Hours", size=(-1, 40))
        self.btn_resume_ha_watchdog = wx.Button(self.tab_diagnostics_overview, label="Resume HA Recovery", size=(-1, 40))
        self.btn_refresh_ha_watchdog.Bind(wx.EVT_BUTTON, self.on_refresh_ha_watchdog)
        self.btn_test_ha_watchdog_push.Bind(wx.EVT_BUTTON, self.on_test_ha_watchdog_push)
        self.btn_pause_ha_watchdog.Bind(wx.EVT_BUTTON, self.on_pause_ha_watchdog)
        self.btn_resume_ha_watchdog.Bind(wx.EVT_BUTTON, self.on_resume_ha_watchdog)
        self._describe_control(self.btn_refresh_ha_watchdog, "Refresh HA Watchdog button. Checks the scheduled watchdog task, last run result, and latest recovery state.")
        self._describe_control(self.btn_test_ha_watchdog_push, "Test HA Recovery Push button. Sends a safe Pushover test using the same path as the Home Assistant recovery watchdog.")
        self._describe_control(self.btn_pause_ha_watchdog, "Pause HA Recovery 2 Hours button. Temporarily disables automatic Home Assistant recovery while you run updates or maintenance.")
        self._describe_control(self.btn_resume_ha_watchdog, "Resume HA Recovery button. Re-enables automatic Home Assistant recovery after maintenance.")
        watchdog_buttons.Add(self.btn_refresh_ha_watchdog, 1, wx.ALL | wx.EXPAND, 5)
        watchdog_buttons.Add(self.btn_test_ha_watchdog_push, 1, wx.ALL | wx.EXPAND, 5)
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
        self.btn_test_fridge_chime_diag = wx.Button(self.tab_diagnostics_overview, label="Test Fridge Chime", size=(-1, 40))
        self.btn_test_freezer_chime_diag = wx.Button(self.tab_diagnostics_overview, label="Test Freezer Chime", size=(-1, 40))
        self.btn_sim_fridge_event_diag = wx.Button(self.tab_diagnostics_overview, label="Simulate Fridge Event", size=(-1, 40))
        self.btn_sim_vacuum_event_diag = wx.Button(self.tab_diagnostics_overview, label="Simulate Vacuum Event", size=(-1, 40))
        self.btn_reload_fridge_smartthings_diag = wx.Button(self.tab_diagnostics_overview, label="Reload Refrigerator SmartThings", size=(-1, 40))
        self.btn_save_ha_snapshot_diag = wx.Button(self.tab_diagnostics_overview, label="Save HA Snapshot", size=(-1, 40))
        self.btn_check_matter_diag = wx.Button(self.tab_diagnostics_overview, label="Check Matter And Alexa", size=(-1, 40))
        self.btn_repair_matter_diag = wx.Button(self.tab_diagnostics_overview, label="Repair Matter And Alexa", size=(-1, 40))
        self.btn_run_safe_smoke.Bind(wx.EVT_BUTTON, self.on_run_safe_smoke_test)
        self.btn_test_front_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "front"))
        self.btn_test_back_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "back"))
        self.btn_test_manual_broadcast_diag.Bind(wx.EVT_BUTTON, self.on_test_diagnostics_manual_broadcast)
        self.btn_test_pushover_diag.Bind(wx.EVT_BUTTON, self.on_test_diagnostics_pushover)
        self.btn_test_fridge_chime_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_chime(event, "fridge_open"))
        self.btn_test_freezer_chime_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_chime(event, "freezer_open"))
        self.btn_sim_fridge_event_diag.Bind(wx.EVT_BUTTON, self.on_simulate_diagnostics_fridge_event)
        self.btn_sim_vacuum_event_diag.Bind(wx.EVT_BUTTON, self.on_simulate_diagnostics_vacuum_event)
        self.btn_reload_fridge_smartthings_diag.Bind(wx.EVT_BUTTON, self.on_reload_diagnostics_fridge_smartthings)
        self.btn_save_ha_snapshot_diag.Bind(wx.EVT_BUTTON, self.on_save_diagnostics_ha_snapshot)
        self.btn_check_matter_diag.Bind(wx.EVT_BUTTON, self.on_check_diagnostics_matter)
        self.btn_repair_matter_diag.Bind(wx.EVT_BUTTON, self.on_repair_diagnostics_matter)
        for button, description in {
            self.btn_run_safe_smoke: "Run Safe Smoke Test button. Checks configuration, Home Assistant, listener, camera URLs, speaker routes, support bundle creation, and active health issues without playing audio.",
            self.btn_test_front_camera_diag: "Test Front Camera Frame button. Captures one frame from the configured front camera stream.",
            self.btn_test_back_camera_diag: "Test Back Camera Frame button. Captures one frame from the configured back camera stream.",
            self.btn_test_manual_broadcast_diag: "Test Manual Broadcast button. Speaks a short manual test announcement through configured speakers.",
            self.btn_test_pushover_diag: "Test Pushover button. Sends a short phone push notification through the configured Pushover account.",
            self.btn_test_fridge_chime_diag: "Test Fridge Chime button. Plays the configured fridge open chime through fridge route speakers.",
            self.btn_test_freezer_chime_diag: "Test Freezer Chime button. Plays the configured freezer open chime through fridge route speakers.",
            self.btn_sim_fridge_event_diag: "Simulate Fridge Event button. Verifies the fridge entity exists, routes a sample transition through Viper, and dispatches the configured fridge alert.",
            self.btn_sim_vacuum_event_diag: "Simulate Vacuum Event button. Verifies the vacuum status entity exists, routes a sample transition through Viper, and dispatches a Cinderella alert.",
            self.btn_reload_fridge_smartthings_diag: "Reload Refrigerator SmartThings button. Reloads the Home Assistant SmartThings entry that owns the refrigerator door sensors.",
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
        status = viper_ha_recovery.pause_recovery(120, "Home Assistant maintenance from Viper Diagnostics")
        text = f"HA recovery paused. {status.get('message') or ''}"
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

    def on_test_diagnostics_chime(self, event, channel):
        label = channel.replace("_", " ").title()
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue(f"Sending {label} chime test.")
        self.notify(f"Testing {label} chime.", priority=10)
        self._safe_submit(self._run_diagnostics_chime, channel)

    def _run_diagnostics_chime(self, channel):
        try:
            ch_settings = self.config.get("broadcast_channels", {}).get(channel, {})
            chime = ch_settings.get("chime", "")
            audio.play_broadcast_chime(chime, channel)
            text = f"{channel.replace('_', ' ').title()} chime test sent."
        except Exception as e:
            logging.exception("Diagnostics chime test failed channel=%s", channel)
            text = f"{channel.replace('_', ' ').title()} chime test failed. {e}"
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_simulate_diagnostics_fridge_event(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Simulating fridge-open event through Viper.")
        self.notify("Simulating fridge event through Viper.", priority=10)
        self._safe_submit(self._run_diagnostics_fridge_event_simulation)

    def _run_diagnostics_fridge_event_simulation(self):
        entity_id = diagnostics.FRIDGE_DOOR_ENTITY
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        entity_check = discovery.validate_entity_exists(
            entity_id,
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        entity_line = "entity not checked"
        if entity_check.get("ok"):
            entity_line = "entity found" if entity_check.get("exists") else "entity missing"
        elif entity_check.get("message") or entity_check.get("error"):
            entity_line = f"entity check failed: {entity_check.get('message') or entity_check.get('error')}"
        actions = ha_listener.route_state_change(self.config, entity_id, {"state": "off"}, {"state": "on"})
        route_ok = bool(actions and actions[0].get("type") == "broadcast" and actions[0].get("channel") == "fridge_open")
        if route_ok:
            action = actions[0]
            result = self._dispatch_broadcast_message(action.get("message", ""), channel=action.get("channel", "fridge_open"))
            dispatch_line = f"dispatch {'ok' if result.get('ok') else 'failed'}: {result.get('message') or ''}"
        else:
            dispatch_line = "dispatch skipped: route logic did not produce fridge_open"
        text = "\n".join(
            [
                "Fridge event simulation",
                f"HA entity: {entity_line}. {entity_id}",
                f"Route logic: {'ok' if route_ok else 'failed'}. Actions: {actions}",
                dispatch_line,
            ]
        )
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_simulate_diagnostics_vacuum_event(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Simulating vacuum status event through Viper.")
        self.notify("Simulating vacuum event through Viper.", priority=10)
        self._safe_submit(self._run_diagnostics_vacuum_event_simulation)

    def _run_diagnostics_vacuum_event_simulation(self):
        entity_id = self.config.get("cinderella_status_entity") or "sensor.cinderella_status"
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        entity_check = discovery.validate_entity_exists(
            entity_id,
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=5,
        )
        entity_line = "entity not checked"
        if entity_check.get("ok"):
            entity_line = "entity found" if entity_check.get("exists") else "entity missing"
        elif entity_check.get("message") or entity_check.get("error"):
            entity_line = f"entity check failed: {entity_check.get('message') or entity_check.get('error')}"
        actions = ha_listener.route_state_change(self.config, entity_id, {"state": "idle"}, {"state": "room_cleaning"})
        route_ok = bool(actions and actions[0].get("type") == "cinderella" and actions[0].get("event") == "departure")
        if route_ok and hasattr(self, "_dispatch_cinderella_event"):
            action = actions[0]
            ok = self._dispatch_cinderella_event(action.get("event", ""), action.get("error", ""), action.get("source", "vacuum"))
            dispatch_line = f"dispatch {'ok' if ok else 'failed'}"
        elif route_ok:
            dispatch_line = "dispatch skipped: dashboard Cinderella dispatcher is unavailable"
        else:
            dispatch_line = "dispatch skipped: route logic did not produce a departure action"
        text = "\n".join(
            [
                "Vacuum event simulation",
                f"HA entity: {entity_line}. {entity_id}",
                f"Route logic: {'ok' if route_ok else 'failed'}. Actions: {actions}",
                dispatch_line,
            ]
        )
        wx.CallAfter(self._finish_diagnostics_action, text)

    def on_reload_diagnostics_fridge_smartthings(self, event):
        if hasattr(self, "smoke_test_txt"):
            self.smoke_test_txt.SetValue("Reloading the Home Assistant SmartThings entry for the refrigerator.")
        self.notify("Reloading refrigerator SmartThings entry.", priority=10)
        self._safe_submit(self._run_diagnostics_fridge_smartthings_reload)

    def _run_diagnostics_fridge_smartthings_reload(self):
        import asyncio

        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        entry = asyncio.run(viper_health.find_config_entry_for_entity(
            ha_settings.get("ha_ip"),
            ha_settings.get("ha_port") or "8123",
            ha_settings.get("ha_token"),
            diagnostics.FRIDGE_DOOR_ENTITY,
        ))
        if not entry.get("ok"):
            text = f"Refrigerator SmartThings reload failed. Viper could not find the HA config entry: {entry.get('message')}"
            wx.CallAfter(self._finish_diagnostics_action, text)
            return
        result = viper_health.reload_config_entry(
            ha_settings.get("ha_ip"),
            ha_settings.get("ha_port") or "8123",
            ha_settings.get("ha_token"),
            entry.get("config_entry_id"),
        )
        viper_health.record_health_event(
            "manual_smartthings_reload",
            "ok" if result.get("ok") else "failed",
            f"Manual refrigerator SmartThings reload from Diagnostics: {result.get('message') or 'unknown result'}",
            details={"entry": entry, "result": result},
        )
        text = "\n".join([
            "Refrigerator SmartThings reload",
            f"Entry: {entry.get('config_entry_id')}",
            f"Platform: {entry.get('platform') or 'unknown'}",
            f"Result: {'ok' if result.get('ok') else 'failed'}. {result.get('message') or ''}",
            "Next: open and close the fridge once, then refresh Health Summary. Viper should show the fridge door event as the last HA event.",
        ])
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

