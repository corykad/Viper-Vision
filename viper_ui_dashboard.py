import wx

import viper_config as cfg
import viper_discovery as discovery
import viper_system_health
from viper_runtime import format_recent_events, is_shutting_down, record_event, safe_submit, startup_summary_lines


class DashboardTabMixin:
    def setup_dash_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        health_box = wx.StaticBox(self.tab_dash, label="System Health")
        health_sizer = wx.StaticBoxSizer(health_box, wx.VERTICAL)
        self.ha_connection_status_txt = wx.TextCtrl(
            self.tab_dash,
            value="Home Assistant listener is starting.",
            style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_WORDWRAP,
            size=(-1, 55),
        )
        self._describe_control(
            self.ha_connection_status_txt,
            "Home Assistant connection status. This read-only box tells whether Viper is connected to Home Assistant events.",
        )
        health_sizer.Add(self.ha_connection_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        self.system_health_txt = wx.TextCtrl(
            self.tab_dash,
            value=self._build_system_health_text(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP,
            size=(-1, 250),
        )
        self._describe_control(
            self.system_health_txt,
            "System Health dashboard. Read-only summary of Home Assistant, doorbells, speakers, HVAC, startup timing, and recent Viper events.",
        )
        health_sizer.Add(self.system_health_txt, 0, wx.ALL | wx.EXPAND, 5)

        health_buttons = wx.GridSizer(rows=0, cols=2, vgap=6, hgap=6)
        self.btn_refresh_system_health = wx.Button(self.tab_dash, label="Refresh System Health", size=(-1, 40))
        self.btn_test_home_assistant_dash = wx.Button(self.tab_dash, label="Test Home Assistant", size=(-1, 40))
        self.btn_test_doorbell_system_dash = wx.Button(self.tab_dash, label="Test Doorbell System", size=(-1, 40))
        self.btn_test_speakers_dash = wx.Button(self.tab_dash, label="Test Speakers", size=(-1, 40))
        self.btn_refresh_system_health.Bind(wx.EVT_BUTTON, self.on_refresh_system_health)
        self.btn_test_home_assistant_dash.Bind(wx.EVT_BUTTON, self.on_refresh_ha_from_dashboard)
        self.btn_test_doorbell_system_dash.Bind(wx.EVT_BUTTON, self.on_test_doorbell_system_from_dashboard)
        self.btn_test_speakers_dash.Bind(wx.EVT_BUTTON, self.on_test_speakers_from_dashboard)
        for button, description in {
            self.btn_refresh_system_health: "Refresh System Health button. Updates the dashboard health summary without changing settings.",
            self.btn_test_home_assistant_dash: "Test Home Assistant button. Opens Diagnostics and checks the Home Assistant connection.",
            self.btn_test_doorbell_system_dash: "Test Doorbell System button. Opens Doorbell Vision so you can run the full front or back doorbell flow.",
            self.btn_test_speakers_dash: "Test Speakers button. Opens Speakers and Audio so you can test or adjust speaker targets.",
        }.items():
            self._describe_control(button, description)
            health_buttons.Add(button, 0, wx.EXPAND)
        health_sizer.Add(health_buttons, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(health_sizer, 0, wx.ALL | wx.EXPAND, 15)

        self.btn_arm = wx.Button(self.tab_dash, label="Disarm System" if self.is_armed else "Arm System", size=(-1, 60))
        font = self.btn_arm.GetFont()
        font.SetPointSize(14)
        self.btn_arm.SetFont(font)
        self.btn_arm.Bind(wx.EVT_BUTTON, self.on_toggle_arm)
        sizer.Add(self.btn_arm, 0, wx.ALL | wx.EXPAND, 15)

        mute_box = wx.StaticBox(self.tab_dash, label="Global Mute")
        mute_sizer = wx.StaticBoxSizer(mute_box, wx.VERTICAL)
        self.global_mute_chk = wx.CheckBox(self.tab_dash, label="Mute all Viper audio")
        self.global_mute_chk.SetValue(self.config.get("global_mute", False))
        self.global_mute_chk.Bind(wx.EVT_CHECKBOX, self.on_global_mute_change)
        self._describe_control(
            self.global_mute_chk,
            "Global mute checkbox. When checked, Viper logs events but suppresses all chimes, TTS, speaker tests, broadcasts, doorbell audio, and Viper status speech.",
        )
        self.global_mute_status_txt = wx.StaticText(self.tab_dash, label=self._global_mute_status_label())
        self._describe_control(
            self.global_mute_status_txt,
            "Global mute status. Tells whether Viper audio output is muted or active.",
        )
        mute_sizer.Add(self.global_mute_chk, 0, wx.ALL, 5)
        mute_sizer.Add(self.global_mute_status_txt, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(mute_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 15)

        cbox = wx.StaticBox(self.tab_dash, label="Manual Intercom Broadcast")
        csizer = wx.StaticBoxSizer(cbox, wx.HORIZONTAL)
        self.broadcast_input = wx.TextCtrl(self.tab_dash, style=wx.TE_PROCESS_ENTER, size=(-1, 40))
        self.broadcast_btn = wx.Button(self.tab_dash, label="Speak", size=(-1, 40))
        self.broadcast_input.Bind(wx.EVT_TEXT_ENTER, self.on_broadcast)
        self.broadcast_btn.Bind(wx.EVT_BUTTON, self.on_broadcast)
        csizer.Add(self.broadcast_input, 1, wx.EXPAND | wx.ALL, 5)
        csizer.Add(self.broadcast_btn, 0, wx.ALL, 5)
        sizer.Add(csizer, 0, wx.ALL | wx.EXPAND, 15)

        self.tab_dash.SetSizer(sizer)

    def _build_system_health_text(self):
        listener_status = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        return viper_system_health.build_system_health_summary(
            self.config,
            listener_status=listener_status,
            hvac_last_states=getattr(self, "hvac_last_states", {}),
            startup_api_status=getattr(self, "startup_api_status", {}),
            startup_lines=startup_summary_lines(limit=8),
            recent_events=format_recent_events(limit=8),
        )

    def refresh_system_health_display(self):
        if hasattr(self, "system_health_txt"):
            self.system_health_txt.SetValue(self._build_system_health_text())
        if hasattr(self, "ha_connection_status_txt"):
            status = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
            self.ha_connection_status_txt.SetValue(viper_system_health.short_ha_status(status))

    def on_refresh_system_health(self, event):
        record_event("diagnostics", "System Health refreshed from the dashboard.")
        self.refresh_system_health_display()
        self.notify("System Health refreshed.", priority=10, speak=False)

    def run_startup_api_checks(self):
        if is_shutting_down.is_set() or getattr(self, "_startup_api_checks_started", False):
            return
        self._startup_api_checks_started = True
        self.startup_api_status = {"checked": False, "running": True, "message": "Startup API checks are running in the background."}
        self.refresh_system_health_display()
        safe_submit(self._run_startup_api_checks_worker)

    def _run_startup_api_checks_worker(self):
        lines = []
        ok = True
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        if ha_settings.get("ha_ip") and ha_settings.get("ha_token"):
            result = discovery.test_ha_connection(
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port"),
                timeout=4,
            )
            if result.get("ok"):
                lines.append(f"HA REST API: ok. Entities visible: {result.get('entity_count', 'unknown')}.")
            else:
                ok = False
                lines.append(f"HA REST API: failed. {result.get('message') or result.get('error') or 'No detail.'}")
        else:
            ok = False
            lines.append("HA REST API: skipped because host or token is missing.")

        speaker_settings = cfg.get_speaker_settings(self.config, include_env=True)
        routes = speaker_settings.get("routes") or {}
        lines.append(
            "Speaker routes: "
            f"{speaker_settings.get('enabled_count', 0)} enabled; "
            f"doorbell {len(routes.get('doorbell') or [])}, "
            f"utilities {len(routes.get('utilities') or [])}, "
            f"fridge {len(routes.get('fridge') or [])}."
        )

        api = cfg.get_api_settings(self.config, include_env=True)
        if api.get("pushover_enabled"):
            if api.get("pushover_user_key") and api.get("pushover_api_token"):
                lines.append("Pushover: configured. No startup test push sent.")
            else:
                ok = False
                lines.append("Pushover: enabled but user key or API token is missing.")
        else:
            lines.append("Pushover: disabled. No startup test push sent.")
        lines.append("Gemini: skipped to avoid billable startup checks.")

        listener = self.ha_listener.status() if hasattr(self, "ha_listener") else {}
        critical = listener.get("critical_health_status") or "not checked yet"
        critical_msg = listener.get("critical_health_message") or "No SmartThings watchdog result yet."
        lines.append(f"SmartThings fridge stream: {critical}. {critical_msg}")

        status = {
            "checked": True,
            "running": False,
            "ok": ok,
            "lines": lines,
            "message": "Startup API checks finished." if ok else "Startup API checks found something to review.",
        }
        wx.CallAfter(self._finish_startup_api_checks, status)

    def _finish_startup_api_checks(self, status):
        self.startup_api_status = status
        state = "ok" if status.get("ok") else "needs review"
        record_event("startup api", f"Startup API checks finished: {state}.")
        self.refresh_system_health_display()

    def _select_main_tab(self, page):
        if not hasattr(self, "notebook"):
            return
        self._select_notebook_page(self.notebook, page)

    def _select_notebook_page(self, notebook, page):
        if notebook is None or page is None:
            return
        for idx in range(notebook.GetPageCount()):
            if notebook.GetPage(idx) is page:
                notebook.SetSelection(idx)
                self._ensure_tab_page(page)
                return

    def on_refresh_ha_from_dashboard(self, event):
        record_event("diagnostics", "Home Assistant status check opened from the dashboard.")
        self._select_main_tab(self.tab_diagnostics_shell)
        self._select_notebook_page(self.diagnostics_notebook, self.tab_ha_status)
        if hasattr(self, "ha_status_txt"):
            self.on_refresh_ha_status(event)

    def on_test_doorbell_system_from_dashboard(self, event):
        record_event("diagnostics", "Doorbell system test opened from the dashboard.")
        self._select_main_tab(self.tab_doorbell)
        self.notify("Doorbell Vision opened. Use the full-flow buttons to test front or back door.", priority=10, speak=False)

    def on_test_speakers_from_dashboard(self, event):
        record_event("diagnostics", "Speaker test area opened from the dashboard.")
        self._select_main_tab(self.tab_audio_shell)
        self._select_notebook_page(self.audio_notebook, self.tab_dev)
        self.notify("Speakers and Audio opened. Use speaker tests or discovery from here.", priority=10, speak=False)
