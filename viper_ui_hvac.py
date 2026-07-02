import logging

import wx

import viper_hvac as hvac
import viper_runtime


class HvacTabMixin:
    def _make_hvac_status_text(self, parent, value, size):
        control = wx.TextCtrl(
            parent,
            value=value,
            size=size,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP,
        )
        return control

    def setup_hvac_tab(self):
        self.hvac_controls = {}
        self.hvac_last_states = getattr(self, "hvac_last_states", {})
        self.hvac_offline_alerted = getattr(self, "hvac_offline_alerted", set())

        outer = wx.BoxSizer(wx.VERTICAL)
        self.hvac_notebook = wx.Notebook(self.tab_hvac)

        self.tab_hvac_all = wx.ScrolledWindow(self.hvac_notebook)
        self.tab_hvac_all.SetScrollRate(0, 20)
        self._setup_hvac_all_tab()
        self.hvac_notebook.AddPage(self.tab_hvac_all, "All Heat Pumps")

        for unit in hvac.HEAT_PUMPS:
            page = wx.ScrolledWindow(self.hvac_notebook)
            page.SetScrollRate(0, 20)
            self._setup_hvac_unit_tab(page, unit)
            self.hvac_notebook.AddPage(page, unit["name"])

        outer.Add(self.hvac_notebook, 1, wx.EXPAND)
        self.tab_hvac.SetSizer(outer)

    def _setup_hvac_all_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        box = wx.StaticBox(self.tab_hvac_all, label="Current Status")
        top = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.hvac_all_status_txt = self._make_hvac_status_text(
            self.tab_hvac_all,
            value=self._cached_hvac_all_status_text(),
            size=(-1, 260),
        )
        self._describe_control(
            self.hvac_all_status_txt,
            "HVAC summary. Shows mode, target temperature, and online status for all heat pumps.",
        )
        top.Add(self.hvac_all_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        flow = wx.StaticText(
            self.tab_hvac_all,
            label=(
                "Use this when you want the whole house to do the same thing. "
                "Pick a temperature, then choose cool, heat, or off."
            ),
        )
        flow.Wrap(720)
        top.Add(flow, 0, wx.ALL | wx.EXPAND, 5)

        temp_row = wx.BoxSizer(wx.HORIZONTAL)
        temp_row.Add(wx.StaticText(self.tab_hvac_all, label="Whole-house temperature:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.hvac_all_temp = wx.SpinCtrlDouble(self.tab_hvac_all, min=50, max=86, inc=1)
        self.hvac_all_temp.SetDigits(0)
        self.hvac_all_temp.SetValue(70)
        self._describe_control(self.hvac_all_temp, "All heat pumps target temperature in degrees Fahrenheit.")
        temp_row.Add(self.hvac_all_temp, 0, wx.ALL, 5)
        top.Add(temp_row, 0, wx.EXPAND)

        primary_box = wx.StaticBox(self.tab_hvac_all, label="Main Actions")
        primary_sizer = wx.StaticBoxSizer(primary_box, wx.VERTICAL)
        button_grid = wx.GridSizer(rows=0, cols=2, vgap=6, hgap=6)
        for label, mode, with_temp in [
            ("Cool Whole House", "cool", True),
            ("Heat Whole House", "heat", True),
            ("Turn Whole House Off", "off", False),
        ]:
            btn = wx.Button(self.tab_hvac_all, label=label, size=(-1, 40))
            btn.Bind(wx.EVT_BUTTON, lambda event, m=mode, wt=with_temp: self.on_hvac_all_command(event, m, wt))
            self._describe_control(btn, f"{label}. Sends this command to every configured heat pump.")
            button_grid.Add(btn, 0, wx.EXPAND)
        primary_sizer.Add(button_grid, 0, wx.ALL | wx.EXPAND, 5)
        top.Add(primary_sizer, 0, wx.ALL | wx.EXPAND, 5)

        secondary_box = wx.StaticBox(self.tab_hvac_all, label="Fine Tuning")
        secondary_sizer = wx.StaticBoxSizer(secondary_box, wx.VERTICAL)
        secondary_grid = wx.GridSizer(rows=0, cols=2, vgap=6, hgap=6)
        for label, mode, with_temp in [
            ("Change Temperature Only", "", True),
            ("Switch To Cool, Keep Temps", "cool", False),
            ("Switch To Heat, Keep Temps", "heat", False),
        ]:
            btn = wx.Button(self.tab_hvac_all, label=label, size=(-1, 40))
            btn.Bind(wx.EVT_BUTTON, lambda event, m=mode, wt=with_temp: self.on_hvac_all_command(event, m, wt))
            self._describe_control(btn, f"{label}. Sends this fine-tuning command to every configured heat pump.")
            secondary_grid.Add(btn, 0, wx.EXPAND)
        secondary_sizer.Add(secondary_grid, 0, wx.ALL | wx.EXPAND, 5)
        top.Add(secondary_sizer, 0, wx.ALL | wx.EXPAND, 5)

        refresh = wx.Button(self.tab_hvac_all, label="Refresh Current Status", size=(-1, 40))
        refresh.Bind(wx.EVT_BUTTON, lambda event: self.refresh_hvac_status(announce=True))
        self._describe_control(refresh, "Refresh Current Status button. Reads current heat pump states from Home Assistant.")
        top.Add(refresh, 0, wx.ALL | wx.EXPAND, 5)

        sizer.Add(top, 0, wx.ALL | wx.EXPAND, 10)
        self.tab_hvac_all.SetSizer(sizer)

    def _setup_hvac_unit_tab(self, page, unit):
        sizer = wx.BoxSizer(wx.VERTICAL)
        controls = {"unit": unit, "page": page}

        status_box = wx.StaticBox(page, label=f"{unit['name']} Current Status")
        status_sizer = wx.StaticBoxSizer(status_box, wx.VERTICAL)
        status = self._make_hvac_status_text(
            page,
            value=self._cached_hvac_unit_status_text(unit),
            size=(-1, 165),
        )
        self._describe_control(status, f"{unit['name']} HVAC status and last command details.")
        controls["status"] = status
        status_sizer.Add(status, 0, wx.ALL | wx.EXPAND, 5)
        refresh = wx.Button(page, label=f"Refresh {unit['name']} Current Status", size=(-1, 38))
        refresh.Bind(wx.EVT_BUTTON, lambda event, u=unit: self.refresh_hvac_status(announce=True, focus_unit=u["key"]))
        self._describe_control(refresh, f"Refresh {unit['name']} Current Status button. Reads current state from Home Assistant.")
        status_sizer.Add(refresh, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(status_sizer, 0, wx.ALL | wx.EXPAND, 10)

        temp_box = wx.StaticBox(page, label="Comfort Setting")
        temp_sizer = wx.StaticBoxSizer(temp_box, wx.VERTICAL)
        quick_help = wx.StaticText(
            page,
            label="Pick the temperature you want, then choose cool, heat, or off.",
        )
        quick_help.Wrap(720)
        temp_sizer.Add(quick_help, 0, wx.ALL | wx.EXPAND, 5)
        temp_row = wx.BoxSizer(wx.HORIZONTAL)
        temp_row.Add(wx.StaticText(page, label="Temperature:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        temp = wx.SpinCtrlDouble(page, min=50, max=86, inc=1)
        temp.SetDigits(0)
        temp.SetValue(70)
        self._describe_control(temp, f"{unit['name']} target temperature in degrees Fahrenheit.")
        controls["temperature"] = temp
        temp_row.Add(temp, 0, wx.ALL, 5)
        temp_sizer.Add(temp_row, 0, wx.EXPAND)

        primary_grid = wx.GridSizer(rows=0, cols=3, vgap=6, hgap=6)
        for label, mode, with_temp in [
            ("Cool Room", "cool", True),
            ("Heat Room", "heat", True),
            ("Turn Room Off", "off", False),
        ]:
            btn = wx.Button(page, label=label, size=(-1, 40))
            btn.Bind(wx.EVT_BUTTON, lambda event, u=unit, m=mode, wt=with_temp: self.on_hvac_quick_command(event, u, m, wt))
            self._describe_control(btn, f"{label} for {unit['name']}.")
            primary_grid.Add(btn, 0, wx.EXPAND)
        temp_sizer.Add(primary_grid, 0, wx.ALL | wx.EXPAND, 5)

        set_temp = wx.Button(page, label="Change Temperature Without Changing Mode", size=(-1, 40))
        set_temp.Bind(wx.EVT_BUTTON, lambda event, u=unit: self.on_hvac_set_temperature(event, u))
        self._describe_control(set_temp, f"Change {unit['name']} target temperature without changing the current mode.")
        temp_sizer.Add(set_temp, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(temp_sizer, 0, wx.ALL | wx.EXPAND, 10)

        advanced_box = wx.StaticBox(page, label="Airflow")
        advanced_sizer = wx.StaticBoxSizer(advanced_box, wx.VERTICAL)
        fan_row = wx.BoxSizer(wx.HORIZONTAL)
        fan_row.Add(wx.StaticText(page, label="Fan mode:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        fan = wx.Choice(page, choices=[])
        controls["fan"] = fan
        self._describe_control(fan, f"{unit['name']} fan mode picker.")
        fan_row.Add(fan, 1, wx.ALL | wx.EXPAND, 5)
        set_fan = wx.Button(page, label="Set Fan", size=(-1, 40))
        set_fan.Bind(wx.EVT_BUTTON, lambda event, u=unit: self.on_hvac_set_fan(event, u))
        self._describe_control(set_fan, f"Set {unit['name']} fan mode.")
        fan_row.Add(set_fan, 0, wx.ALL, 5)
        advanced_sizer.Add(fan_row, 0, wx.EXPAND)

        swing_row = wx.BoxSizer(wx.HORIZONTAL)
        swing_row.Add(wx.StaticText(page, label="Swing mode:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        swing = wx.Choice(page, choices=[])
        controls["swing"] = swing
        self._describe_control(swing, f"{unit['name']} swing mode picker.")
        swing_row.Add(swing, 1, wx.ALL | wx.EXPAND, 5)
        set_swing = wx.Button(page, label="Set Swing", size=(-1, 40))
        set_swing.Bind(wx.EVT_BUTTON, lambda event, u=unit: self.on_hvac_set_swing(event, u))
        self._describe_control(set_swing, f"Set {unit['name']} swing mode.")
        swing_row.Add(set_swing, 0, wx.ALL, 5)
        advanced_sizer.Add(swing_row, 0, wx.EXPAND)
        sizer.Add(advanced_sizer, 0, wx.ALL | wx.EXPAND, 10)

        advanced_mode_box = wx.StaticBox(page, label="Advanced Modes")
        advanced_mode_sizer = wx.StaticBoxSizer(advanced_mode_box, wx.VERTICAL)
        advanced_help = wx.StaticText(
            page,
            label="Dry, fan-only, and auto use the raw IR entity. Alexa still only sees off, cool, and heat.",
        )
        advanced_help.Wrap(720)
        advanced_mode_sizer.Add(advanced_help, 0, wx.ALL | wx.EXPAND, 5)

        raw_mode_row = wx.BoxSizer(wx.HORIZONTAL)
        raw_mode_row.Add(wx.StaticText(page, label="Mode:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        raw_mode = wx.Choice(page, choices=["off", "cool", "heat", "dry", "fan_only", "heat_cool"])
        raw_mode.SetStringSelection("off")
        controls["raw_mode"] = raw_mode
        self._describe_control(raw_mode, f"{unit['name']} advanced Daikin mode picker. Includes Dry, Fan Only, and Auto.")
        raw_mode_row.Add(raw_mode, 1, wx.ALL | wx.EXPAND, 5)
        set_raw_mode = wx.Button(page, label="Apply Advanced Mode", size=(-1, 40))
        set_raw_mode.Bind(wx.EVT_BUTTON, lambda event, u=unit: self.on_hvac_set_raw_mode(event, u))
        self._describe_control(set_raw_mode, f"Apply selected advanced Daikin mode for {unit['name']}.")
        raw_mode_row.Add(set_raw_mode, 0, wx.ALL, 5)
        advanced_mode_sizer.Add(raw_mode_row, 0, wx.EXPAND)
        sizer.Add(advanced_mode_sizer, 0, wx.ALL | wx.EXPAND, 10)

        self.hvac_controls[unit["key"]] = controls
        page.SetSizer(sizer)
        cached = getattr(self, "hvac_last_states", {}).get(unit["key"])
        if cached:
            self._sync_hvac_controls_from_summary(unit["key"], cached)

    def refresh_hvac_status(self, announce=False, focus_unit=""):
        if hasattr(self, "hvac_all_status_txt"):
            self.hvac_all_status_txt.SetValue("Reading heat pump status from Home Assistant...")
        self._safe_submit(self._run_hvac_refresh, announce, focus_unit)

    def _cached_hvac_all_status_text(self):
        states = list(getattr(self, "hvac_last_states", {}).values())
        if states:
            return hvac.format_all_status(states)
        return "Current heat pump status will appear here. Viper also refreshes this once shortly after startup."

    def _cached_hvac_unit_status_text(self, unit):
        item = getattr(self, "hvac_last_states", {}).get(unit["key"])
        if item:
            return hvac.format_unit_status(item)
        return f"Current status for {unit['name']} will appear here. Press Refresh Current Status."

    def _run_hvac_refresh(self, announce=False, focus_unit=""):
        try:
            states = hvac.get_states(self.config, timeout=8)
            summaries = [hvac.summarize_unit(unit, states) for unit in hvac.HEAT_PUMPS]
            wx.CallAfter(self._finish_hvac_refresh, summaries, announce, focus_unit)
        except Exception as exc:
            logging.exception("HVAC refresh failed")
            wx.CallAfter(self._finish_hvac_error, f"HVAC refresh failed: {exc}", announce)

    def _finish_hvac_refresh(self, summaries, announce=False, focus_unit=""):
        previous_states = getattr(self, "hvac_last_states", {})
        self.hvac_last_states = {item["key"]: item for item in summaries}
        if hasattr(self, "_notify_hvac_offline_transitions"):
            self._notify_hvac_offline_transitions(previous_states, self.hvac_last_states)
        controls_by_unit = getattr(self, "hvac_controls", {})
        for item in summaries:
            if controls_by_unit.get(item["key"]):
                self._sync_hvac_controls_from_summary(item["key"], item)
        summary = hvac.format_all_status(summaries)
        if hasattr(self, "hvac_all_status_txt"):
            self.hvac_all_status_txt.SetValue(summary)
        viper_runtime.record_event("hvac", "Heat pump status refreshed.")
        if hasattr(self, "refresh_system_health_display"):
            self.refresh_system_health_display()
        if announce:
            if focus_unit and focus_unit in self.hvac_last_states:
                self.notify(hvac.format_unit_status(self.hvac_last_states[focus_unit]), priority=10)
            else:
                self.notify("HVAC status refreshed.", priority=10)

    def _notify_hvac_offline_transitions(self, previous_states, current_states):
        alerted = getattr(self, "hvac_offline_alerted", set())
        for key, current in (current_states or {}).items():
            was_available = bool((previous_states or {}).get(key, {}).get("available"))
            is_available = bool(current.get("available"))
            name = current.get("name") or key.replace("_", " ").title()
            if is_available:
                alerted.discard(key)
                continue
            if not was_available or key in alerted:
                continue
            alerted.add(key)
            message = f"{name} heat pump went offline. Wi-Fi was {current.get('wifi_quality_label') or 'unknown'}."
            viper_runtime.record_event("hvac", message)
            self._safe_submit(self._send_hvac_offline_pushover, name, message)
        self.hvac_offline_alerted = alerted

    def _send_hvac_offline_pushover(self, name, message):
        try:
            import viper_audio

            sent = viper_audio._send_text_pushover("Viper heat pump offline", message)
            if sent:
                logging.info("HVAC offline Pushover sent for %s", name)
            else:
                logging.info("HVAC offline Pushover skipped for %s", name)
        except Exception:
            logging.exception("HVAC offline Pushover failed for %s", name)

    def _finish_hvac_error(self, message, announce=False):
        if hasattr(self, "hvac_all_status_txt"):
            self.hvac_all_status_txt.SetValue(message)
        for controls in getattr(self, "hvac_controls", {}).values():
            if controls.get("status"):
                controls["status"].SetValue(message)
        if announce:
            self.notify(message, priority=10)
        viper_runtime.record_event("hvac", message)
        if hasattr(self, "refresh_system_health_display"):
            self.refresh_system_health_display()

    def _sync_hvac_controls_from_summary(self, unit_key, item):
        controls = getattr(self, "hvac_controls", {}).get(unit_key) or {}
        if controls.get("status"):
            controls["status"].SetValue(hvac.format_unit_status(item))
        if controls.get("temperature") and item.get("target_temperature") is not None:
            controls["temperature"].SetValue(float(item["target_temperature"]))
        self._sync_hvac_choice(controls.get("fan"), item.get("fan_modes"), item.get("fan_mode"))
        self._sync_hvac_choice(controls.get("swing"), item.get("swing_modes"), item.get("swing_mode"))
        raw_modes = item.get("source_hvac_modes") or hvac.RAW_ADVANCED_MODES
        self._sync_hvac_choice(controls.get("raw_mode"), raw_modes, item.get("source_state"))

    def _sync_hvac_choice(self, choice, options, current):
        if choice is None:
            return
        options = [str(item) for item in (options or []) if str(item)]
        current = str(current or "")
        if current and current not in options:
            options.insert(0, current)
        choice.Set(options)
        if current and current in options:
            choice.SetStringSelection(current)
        elif options:
            choice.SetSelection(0)

    def on_hvac_set_mode(self, event, unit, mode):
        self._run_hvac_command_async(
            lambda: hvac.set_mode(self.config, unit, mode),
            f"{unit['name']} set to {hvac.hvac_mode_label(mode)}.",
        )

    def on_hvac_quick_command(self, event, unit, mode, with_temperature):
        controls = self.hvac_controls.get(unit["key"]) or {}
        temperature = controls.get("temperature").GetValue() if controls.get("temperature") else 70
        if mode == "off":
            worker = lambda: hvac.set_mode(self.config, unit, "off")
            message = f"{unit['name']} turned off."
        elif with_temperature:
            worker = lambda: hvac.set_temperature_and_mode(self.config, unit, temperature, mode)
            message = f"{unit['name']} set to {hvac.hvac_mode_label(mode)} at {int(float(temperature))}."
        else:
            worker = lambda: hvac.set_mode(self.config, unit, mode)
            message = f"{unit['name']} set to {hvac.hvac_mode_label(mode)}."
        self._run_hvac_command_async(worker, message)

    def on_hvac_set_temperature(self, event, unit):
        controls = self.hvac_controls.get(unit["key"]) or {}
        temperature = controls.get("temperature").GetValue() if controls.get("temperature") else 70
        self._run_hvac_command_async(
            lambda: hvac.set_temperature(self.config, unit, temperature),
            f"{unit['name']} target temperature set to {int(float(temperature))}.",
        )

    def on_hvac_set_fan(self, event, unit):
        controls = self.hvac_controls.get(unit["key"]) or {}
        fan = controls.get("fan").GetStringSelection() if controls.get("fan") else ""
        if not fan:
            self.notify(f"No fan mode is selected for {unit['name']}.", priority=10)
            return
        self._run_hvac_command_async(
            lambda: hvac.set_fan_mode(self.config, unit, fan),
            f"{unit['name']} fan set to {fan}.",
        )

    def on_hvac_set_raw_mode(self, event, unit):
        controls = self.hvac_controls.get(unit["key"]) or {}
        mode = controls.get("raw_mode").GetStringSelection() if controls.get("raw_mode") else ""
        if not mode:
            self.notify(f"No raw mode is selected for {unit['name']}.", priority=10)
            return
        self._run_hvac_command_async(
            lambda: hvac.set_mode(self.config, unit, mode),
            f"{unit['name']} raw mode set to {hvac.hvac_mode_label(mode)}.",
        )

    def on_hvac_set_swing(self, event, unit):
        controls = self.hvac_controls.get(unit["key"]) or {}
        swing = controls.get("swing").GetStringSelection() if controls.get("swing") else ""
        if not swing:
            self.notify(f"No swing mode is selected for {unit['name']}.", priority=10)
            return
        self._run_hvac_command_async(
            lambda: hvac.set_swing_mode(self.config, unit, swing),
            f"{unit['name']} swing set to {swing}.",
        )

    def on_hvac_all_command(self, event, mode, with_temperature):
        temperature = self.hvac_all_temp.GetValue() if with_temperature else None
        if mode == "off":
            message = "All heat pumps turned off."
        elif not mode and with_temperature:
            message = f"All heat pump target temperatures set to {int(float(temperature))}."
        elif with_temperature:
            message = f"All heat pumps set to {hvac.hvac_mode_label(mode)} at {int(float(temperature))}."
        else:
            message = f"All heat pumps set to {hvac.hvac_mode_label(mode)}."
        self._run_hvac_command_async(
            lambda: hvac.apply_all(self.config, mode=mode, temperature=temperature),
            message,
        )

    def _run_hvac_command_async(self, worker, success_message):
        def run():
            try:
                result = worker()
                message = hvac.summarize_service_results(result, success_message) if isinstance(result, list) else success_message
                viper_runtime.record_event("hvac", message)
                wx.CallAfter(self.notify, message, 10)
                wx.CallAfter(lambda: wx.CallLater(1200, self.refresh_hvac_status))
            except Exception as exc:
                logging.exception("HVAC command failed")
                wx.CallAfter(self.notify, f"HVAC command failed: {exc}", 10)

        self._safe_submit(run)
