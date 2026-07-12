import json
from datetime import datetime

import wx

import viper_audio as audio
import viper_config as cfg
import viper_discovery as discovery
import viper_ha_package as ha_package
import viper_ui_common as ui_common
from viper_runtime import safe_submit


AccessibleStatusText = ui_common.AccessibleStatusText


class DeviceToolsMixin:
    def setup_devices_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        sbox = wx.StaticBox(self.tab_dev, label="Speaker Targets (Spacebar to Toggle)")
        ssizer = wx.StaticBoxSizer(sbox, wx.VERTICAL)
        self.speaker_list = wx.CheckListBox(self.tab_dev, choices=[], size=(-1, 150))
        self.speaker_list.Bind(wx.EVT_CHECKLISTBOX, self.on_speaker_toggle)
        self.speaker_list.Bind(wx.EVT_LISTBOX, self.on_speaker_select)
        self.speaker_list.Bind(wx.EVT_SET_FOCUS, self.on_speaker_focus)
        ssizer.Add(self.speaker_list, 1, wx.EXPAND | wx.ALL, 5)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add_spk = wx.Button(self.tab_dev, label="Add Speaker")
        self.btn_add_spk.Bind(wx.EVT_BUTTON, self.on_add_speaker)
        self.btn_discover_spk = wx.Button(self.tab_dev, label="Discover Available Speakers")
        self.btn_discover_spk.Bind(wx.EVT_BUTTON, self.on_discover_speakers)
        self.btn_ren_spk = wx.Button(self.tab_dev, label="Rename Selected")
        self.btn_ren_spk.Bind(wx.EVT_BUTTON, self.on_rename_speaker)
        self.btn_rem_spk = wx.Button(self.tab_dev, label="Remove Selected")
        self.btn_rem_spk.Bind(wx.EVT_BUTTON, self.on_remove_speaker)
        btn_sizer.Add(self.btn_add_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_discover_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_ren_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_rem_spk, 1, wx.ALL, 5)
        ssizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 0)
        sizer.Add(ssizer, 1, wx.ALL | wx.EXPAND, 10)

        rbox = wx.StaticBox(self.tab_dev, label="Selected Speaker Routing")
        rsizer = wx.StaticBoxSizer(rbox, wx.VERTICAL)
        self.chk_route_doorbell = wx.CheckBox(self.tab_dev, label="Doorbell Alerts")
        self.chk_route_utilities = wx.CheckBox(self.tab_dev, label="Utilities Spoken")
        self.chk_route_fridge = wx.CheckBox(self.tab_dev, label="Fridge / Freezer")
        self.chk_route_qhexempt = wx.CheckBox(self.tab_dev, label="Ignore Quiet Hours")
        for _chk in [self.chk_route_doorbell, self.chk_route_utilities, self.chk_route_fridge, self.chk_route_qhexempt]:
            _chk.Bind(wx.EVT_CHECKBOX, self.on_speaker_route_change)
            rsizer.Add(_chk, 0, wx.ALL, 5)
        sizer.Add(rsizer, 0, wx.ALL | wx.EXPAND, 10)

        self.refresh_speaker_list()
        self._sync_speaker_routing_controls()

        qbox = wx.StaticBox(self.tab_dev, label="Quiet Hours")
        qsizer = wx.StaticBoxSizer(qbox, wx.VERTICAL)
        self.quiet_hours_enable_chk = wx.CheckBox(self.tab_dev, label="Enable quiet hours (suppresses utility announcements)")
        self.quiet_hours_enable_chk.SetValue(self.config.get("quiet_hours_enabled", False))
        self.quiet_hours_enable_chk.Bind(wx.EVT_CHECKBOX, self.on_quiet_hours_change)
        qsizer.Add(self.quiet_hours_enable_chk, 0, wx.ALL, 5)

        qrow = wx.BoxSizer(wx.HORIZONTAL)
        qrow.Add(wx.StaticText(self.tab_dev, label="Quiet hours start time, HH:MM:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.quiet_hours_start_txt = wx.TextCtrl(self.tab_dev, value=self.config.get("quiet_hours_start", "22:00"))
        qrow.Add(self.quiet_hours_start_txt, 1, wx.ALL, 5)
        qrow.Add(wx.StaticText(self.tab_dev, label="Quiet hours end time, HH:MM:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.quiet_hours_end_txt = wx.TextCtrl(self.tab_dev, value=self.config.get("quiet_hours_end", "07:00"))
        qrow.Add(self.quiet_hours_end_txt, 1, wx.ALL, 5)
        qsizer.Add(qrow, 0, wx.EXPAND)

        self.btn_save_quiet_hours = wx.Button(self.tab_dev, label="Save Quiet Hours", size=(-1, 40))
        self.btn_save_quiet_hours.Bind(wx.EVT_BUTTON, self.on_quiet_hours_change)
        qsizer.Add(self.btn_save_quiet_hours, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(qsizer, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_dev.SetSizer(sizer)

    def setup_utils_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        intro = AccessibleStatusText(
            self.tab_util,
            value=(
                "Advanced contains setup tools that most people use rarely after Viper is working.\n\n"
                "Daily controls now live in Dashboard, Doorbell Vision, Speakers & Audio, and Home Devices. "
                "Health checks and logs live in Diagnostics."
            ),
            size=(-1, 110),
        )
        self._describe_control(intro, "Advanced introduction. Explanation of rarely used setup and export tools.")
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        ubox = wx.StaticBox(self.tab_util, label="Advanced Setup And Export Tools")
        usizer = wx.StaticBoxSizer(ubox, wx.VERTICAL)

        self.btn_new_user_setup = wx.Button(self.tab_util, label="Advanced: Home Assistant Server Assistant", size=(-1, 40))
        self.btn_new_user_setup.Bind(wx.EVT_BUTTON, self.on_new_user_setup)
        self.btn_ha_setup = wx.Button(self.tab_util, label="Advanced Home Assistant Setup", size=(-1, 40))
        self.btn_ha_setup.Bind(wx.EVT_BUTTON, self.on_home_assistant_setup)
        self.btn_ha_package = wx.Button(self.tab_util, label="Advanced: Export HA YAML Package", size=(-1, 40))
        self.btn_ha_package.Bind(wx.EVT_BUTTON, self.on_generate_ha_package)
        self.btn_scan = wx.Button(self.tab_util, label="Advanced: Scan Network for Sonos", size=(-1, 40))
        self.btn_scan.Bind(wx.EVT_BUTTON, self.on_scan_sonos)
        self.btn_scan_ha = wx.Button(self.tab_util, label="Advanced: Scan HA for Speakers", size=(-1, 40))
        self.btn_scan_ha.Bind(wx.EVT_BUTTON, self.on_scan_ha)

        usizer.Add(self.btn_new_user_setup, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_ha_setup, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_ha_package, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_scan, 0, wx.ALL | wx.EXPAND, 5)
        usizer.Add(self.btn_scan_ha, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(usizer, 1, wx.ALL | wx.EXPAND, 10)
        self.tab_util.SetSizer(sizer)

    def on_api(self, event):
        safe_submit(self._run_api)

    def _run_api(self):
        try:
            if not cfg.API_LOG_PATH.exists():
                self.notify("API log is currently empty.", priority=10)
                return
            with open(cfg.API_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            reqs = data.get("total_requests", 0)
            cost = (data.get("prompt_tokens", 0) * cfg.COST_PER_INPUT_TOKEN) + (data.get("response_tokens", 0) * cfg.COST_PER_OUTPUT_TOKEN)
            projected = (cost / max(1, datetime.now().day)) * 30
            msg = f"API: {reqs} requests. Spent: ${cost:.4f}. Projected: ${projected:.2f}"
            self.notify(msg, priority=10)
        except Exception:
            self.notify("API log unavailable.", priority=10)

    def on_generate_ha_package(self, event):
        options = ha_package.package_options_from_config(self.config)
        bundle = ha_package.write_package_bundle(options)
        self.notify(
            f"HA package generated: {bundle['package'].name}. See ha_packages folder.",
            priority=10,
        )

    def on_batt(self, event):
        self.notify("Checking battery levels...", priority=10)
        safe_submit(self._run_batt)

    def _run_batt(self):
        try:
            ha_settings = self._ha_settings_for_utility_query()
            result = discovery.get_ha_states(
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port"),
                timeout=10,
            )
            if not result.get("ok") and result.get("error") in {"unreachable", "timeout"}:
                self.check_and_repair_home_assistant_address()
                ha_settings = self._ha_settings_for_utility_query()
                result = discovery.get_ha_states(
                    token=ha_settings.get("ha_token"),
                    ha_ip=ha_settings.get("ha_ip"),
                    ha_port=ha_settings.get("ha_port"),
                    timeout=10,
                )
            if not result.get("ok"):
                raise RuntimeError(self._format_ha_utility_error(result))
            stats = []
            for s in result.get("states", []):
                eid = s.get("entity_id", "").lower()
                if any(k in eid for k in cfg.BATTERY_KEYWORDS) and ("front" in eid or "back" in eid):
                    friendly = s["attributes"].get("friendly_name", eid)
                    try:
                        val = float(s.get("state", 0))
                        stats.append(f"{friendly}: {val:.0f}%")
                    except Exception:
                        pass
            msg = "Battery Levels: " + (", ".join(stats) if stats else "No sensors found.")
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)
        except Exception as e:
            self.notify(f"Battery query failed: {e}", priority=10)

    def on_filter(self, event):
        self.notify("Checking refrigerator filter...", priority=10)
        safe_submit(self._run_filter)

    def _run_filter(self):
        try:
            entity_id = "sensor.refrigerator_water_filter_usage"
            ha_settings = self._ha_settings_for_utility_query()
            result = discovery.get_entity(
                entity_id,
                token=ha_settings.get("ha_token"),
                ha_ip=ha_settings.get("ha_ip"),
                ha_port=ha_settings.get("ha_port"),
                timeout=10,
            )
            if not result.get("ok") and result.get("error") in {"unreachable", "timeout"}:
                self.check_and_repair_home_assistant_address()
                ha_settings = self._ha_settings_for_utility_query()
                result = discovery.get_entity(
                    entity_id,
                    token=ha_settings.get("ha_token"),
                    ha_ip=ha_settings.get("ha_ip"),
                    ha_port=ha_settings.get("ha_port"),
                    timeout=10,
                )
            if not result.get("ok"):
                raise RuntimeError(self._format_ha_utility_error(result, entity_id=entity_id))
            s = result.get("entity") or {}
            friendly = s.get("attributes", {}).get("friendly_name", "Refrigerator Water filter usage")
            raw_state = str(s.get("state", "")).strip()
            msg = f"{friendly}: {raw_state} percent."
            self.notify(msg, priority=10)
            safe_submit(audio.play_notification, "utilities", msg)
        except Exception as e:
            self.notify(f"Filter query failed: {e}", priority=10)

    def _ha_settings_for_utility_query(self):
        return cfg.get_ha_settings(self.config, include_env=True)

    def _format_ha_utility_error(self, result, *, entity_id=""):
        message = result.get("message") or result.get("error") or "Home Assistant request failed."
        url = result.get("url") or ""
        if result.get("error") == "unreachable":
            host = (cfg.get_ha_settings(self.config, include_env=True).get("ha_ip") or "").strip()
            if host and not discovery.resolve_host_to_ip(host):
                message += f" The saved Home Assistant host '{host}' does not resolve from Windows right now."
        if entity_id and result.get("error") == "not_found":
            message += f" Missing entity: {entity_id}."
        if url:
            message += f" URL: {url}"
        return message

    def on_scan_sonos(self, event):
        self.notify("Scanning network for Sonos. This will only show what is available.", priority=10)
        safe_submit(self._run_scan_sonos)

    def _run_scan_sonos(self):
        try:
            import soco
            speakers = soco.discover()
            if not speakers:
                self.notify("No Sonos found.", priority=10)
                return
            candidates = self._sonos_speaker_candidates_from_soco(speakers)
            wx.CallAfter(self._show_discovered_speakers, [], candidates, "", "")
        except Exception:
            self.notify("Sonos scan failed.", priority=10)

    def _prompt_add_sonos_speakers(self, new_speakers):
        choices = [f"{spk.player_name} ({spk.ip_address})" for spk in new_speakers]
        dlg = wx.MultiChoiceDialog(self, "Select speakers to add:", "New Sonos Found", choices)
        if dlg.ShowModal() == wx.ID_OK:
            added = 0
            for idx in dlg.GetSelections():
                spk = new_speakers[idx]
                name = spk.player_name + " Sonos"
                self.config.setdefault("speakers", {})[name] = {
                    "id": spk.ip_address,
                    "type": "sonos",
                    "enabled": True,
                    "doorbell": True,
                    "utilities": True,
                    "fridge": True,
                    "quiet_hours_exempt": False,
                }
                added += 1
            self.config = cfg.write_config(self.config)
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()
            self.notify(f"Added {added} Sonos speaker{'s' if added != 1 else ''}.", priority=10)
        dlg.Destroy()

    def on_scan_ha(self, event):
        self.notify("Scanning HA for speakers. This will only show what is available.", priority=10)
        safe_submit(self._run_scan_ha)

    def _run_scan_ha(self):
        result = discovery.discover_ha_entities(timeout=5)
        if not result.get("ok"):
            msg = result.get("message") or "HA scan failed."
            wx.CallAfter(self.notify, msg, priority=10)
            return
        wx.CallAfter(self._show_discovered_speakers, self._ha_speaker_candidates_from_result(result), [], "", "")

    def _prompt_add_ha_speakers(self, new_speakers):
        choices = [f"{s.get('attributes', {}).get('friendly_name', s['entity_id'])} ({s['entity_id']})" for s in new_speakers]
        dlg = wx.MultiChoiceDialog(self, "Select HA speakers to add:", "New HA Found", choices)
        if dlg.ShowModal() == wx.ID_OK:
            added = 0
            for idx in dlg.GetSelections():
                spk = new_speakers[idx]
                raw_name = spk.get("attributes", {}).get("friendly_name", spk["entity_id"].replace("media_player.", ""))
                spk_type = "alexa" if "echo" in raw_name.lower() or "alexa" in spk["entity_id"].lower() else "ha"
                self.config.setdefault("speakers", {})[f"{raw_name} ({spk_type.upper()})"] = {
                    "id": spk["entity_id"],
                    "type": spk_type,
                    "enabled": True,
                    "doorbell": True,
                    "utilities": True,
                    "fridge": True,
                    "quiet_hours_exempt": False,
                }
                added += 1
            self.config = cfg.write_config(self.config)
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()
            self.notify(f"Added {added} Home Assistant speaker{'s' if added != 1 else ''}.", priority=10)
        dlg.Destroy()
