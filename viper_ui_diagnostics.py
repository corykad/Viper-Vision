import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import wx

import viper_audio as audio
import viper_config as cfg
import viper_diagnostics as diagnostics
import viper_discovery as discovery
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
        self.btn_test_fridge_chime_diag = wx.Button(self.tab_diagnostics_overview, label="Test Fridge Chime", size=(-1, 40))
        self.btn_test_freezer_chime_diag = wx.Button(self.tab_diagnostics_overview, label="Test Freezer Chime", size=(-1, 40))
        self.btn_run_safe_smoke.Bind(wx.EVT_BUTTON, self.on_run_safe_smoke_test)
        self.btn_test_front_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "front"))
        self.btn_test_back_camera_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_camera(event, "back"))
        self.btn_test_manual_broadcast_diag.Bind(wx.EVT_BUTTON, self.on_test_diagnostics_manual_broadcast)
        self.btn_test_fridge_chime_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_chime(event, "fridge_open"))
        self.btn_test_freezer_chime_diag.Bind(wx.EVT_BUTTON, lambda event: self.on_test_diagnostics_chime(event, "freezer_open"))
        for button, description in {
            self.btn_run_safe_smoke: "Run Safe Smoke Test button. Checks configuration, Home Assistant, listener, camera URLs, speaker routes, support bundle creation, and active health issues without playing audio.",
            self.btn_test_front_camera_diag: "Test Front Camera Frame button. Captures one frame from the configured front camera stream.",
            self.btn_test_back_camera_diag: "Test Back Camera Frame button. Captures one frame from the configured back camera stream.",
            self.btn_test_manual_broadcast_diag: "Test Manual Broadcast button. Speaks a short manual test announcement through configured speakers.",
            self.btn_test_fridge_chime_diag: "Test Fridge Chime button. Plays the configured fridge open chime through fridge route speakers.",
            self.btn_test_freezer_chime_diag: "Test Freezer Chime button. Plays the configured freezer open chime through fridge route speakers.",
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

