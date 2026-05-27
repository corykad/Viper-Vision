import requests
import wx

import viper_audio as audio
import viper_config as cfg


FRIDGE_CHANNELS = [
    ("fridge_open", "Fridge Door Opens"),
    ("fridge_closed", "Fridge Door Closes"),
    ("freezer_open", "Freezer Door Opens"),
    ("freezer_closed", "Freezer Door Closes"),
]


class FridgeTabMixin:
    def setup_fridge_tab(self):
        """Fridge & Freezer door channel settings with independent chime file selection per state."""
        outer = wx.BoxSizer(wx.VERTICAL)

        chime_list = self._get_chime_list()
        channels_cfg = self.config.get("broadcast_channels", {})
        self._fridge_controls = {}

        for ch_key, ch_label in FRIDGE_CHANNELS:
            ch_data = channels_cfg.get(ch_key, {"mode": "chime", "chime": ""})

            sbox = wx.StaticBox(self.tab_fridge, label=ch_label)
            ssizer = wx.StaticBoxSizer(sbox, wx.VERTICAL)

            mode_row = wx.BoxSizer(wx.HORIZONTAL)
            mode_row.Add(
                wx.StaticText(self.tab_fridge, label="When this happens:"),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                5,
            )
            mode_choice = wx.Choice(self.tab_fridge, choices=["speak", "chime", "silent"])
            mode_choice.SetStringSelection(ch_data.get("mode", "chime"))
            mode_choice.Bind(wx.EVT_CHOICE, lambda e, k=ch_key: self._on_fridge_channel_change(k))
            mode_row.Add(mode_choice, 0, wx.ALL, 5)
            ssizer.Add(mode_row, 0, wx.EXPAND)

            chime_row = wx.BoxSizer(wx.HORIZONTAL)
            chime_row.Add(
                wx.StaticText(self.tab_fridge, label="Play chime:"),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                5,
            )
            chime_choice = wx.Choice(self.tab_fridge, choices=chime_list)
            current_chime = ch_data.get("chime", "")
            chime_choice.SetStringSelection(current_chime if current_chime in chime_list else "(Default)")
            chime_choice.Bind(wx.EVT_CHOICE, lambda e, k=ch_key: self._on_fridge_channel_change(k))
            chime_row.Add(chime_choice, 1, wx.ALL, 5)
            ssizer.Add(chime_row, 0, wx.EXPAND)

            btn_test = wx.Button(self.tab_fridge, label=f"Test {ch_label} Chime", size=(-1, 32))
            btn_test.Bind(wx.EVT_BUTTON, lambda e, k=ch_key: self._on_test_fridge_chime(k))
            ssizer.Add(btn_test, 0, wx.EXPAND | wx.ALL, 5)

            self._fridge_controls[ch_key] = {
                "mode": mode_choice,
                "chime": chime_choice,
                "test": btn_test,
            }
            outer.Add(ssizer, 0, wx.ALL | wx.EXPAND, 10)

        ice_box = wx.StaticBox(self.tab_fridge, label="Ice Maker")
        ice_sizer = wx.StaticBoxSizer(ice_box, wx.VERTICAL)
        self.ice_maker_status_txt = self._make_accessible_status_text(
            self.tab_fridge,
            value="Checking ice maker status...",
            size=(-1, 105),
        )
        self._describe_control(
            self.ice_maker_status_txt,
            "Ice maker status. Shows whether the ice maker switch is on or off, whether the keep-on helper is active, and the current Home Assistant ice usage counter.",
        )
        ice_sizer.Add(self.ice_maker_status_txt, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_ice_toggle = wx.Button(self.tab_fridge, label="Turn Ice Maker On", size=(-1, 40))
        self.btn_ice_toggle.Bind(wx.EVT_BUTTON, self.on_ice_maker_toggle)
        self._describe_control(
            self.btn_ice_toggle,
            "Ice maker toggle button. The label changes to Turn Ice Maker Off when Home Assistant reports the ice maker is on.",
        )
        ice_sizer.Add(self.btn_ice_toggle, 0, wx.ALL | wx.EXPAND, 5)
        outer.Add(ice_sizer, 0, wx.ALL | wx.EXPAND, 10)
        wx.CallAfter(self.refresh_ice_maker_status)

        controls_box = wx.StaticBox(self.tab_fridge, label="Refrigerator Controls")
        controls_sizer = wx.StaticBoxSizer(controls_box, wx.VERTICAL)
        self.refrigerator_status_txt = self._make_accessible_status_text(
            self.tab_fridge,
            value="Checking refrigerator controls...",
            size=(-1, 165),
        )
        self._describe_control(
            self.refrigerator_status_txt,
            "Refrigerator status. Shows Home Assistant door sensors, temperature setpoints, filter status, power, energy, and special mode states.",
        )
        controls_sizer.Add(self.refrigerator_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        self.refrigerator_control_widgets = {}
        self.refrigerator_action_buttons = {}

        for label, entity_id, minimum, maximum in [
            ("Fridge temperature", "number.refrigerator_fridge_temperature", 34, 44),
            ("Freezer temperature", "number.refrigerator_freezer_temperature", -8, 5),
        ]:
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self.tab_fridge, label=f"{label}:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            spin = wx.SpinCtrlDouble(self.tab_fridge, min=float(minimum), max=float(maximum), inc=1.0)
            spin.SetDigits(0)
            btn = wx.Button(self.tab_fridge, label=f"Set {label}")
            btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_refrigerator_set_number(event, eid))
            self._describe_control(spin, f"{label} setpoint. Adjust the value, then press Set {label}.")
            self._describe_control(btn, f"Set {label} button. Sends the selected setpoint to Home Assistant.")
            row.Add(spin, 1, wx.ALL | wx.EXPAND, 5)
            row.Add(btn, 0, wx.ALL, 5)
            controls_sizer.Add(row, 0, wx.EXPAND)
            self.refrigerator_control_widgets[entity_id] = spin
            self.refrigerator_action_buttons[entity_id] = btn

        for label, entity_id in [
            ("Power Cool", "switch.refrigerator_power_cool"),
            ("Power Freeze", "switch.refrigerator_power_freeze"),
            ("Sabbath Mode", "switch.refrigerator_sabbath_mode"),
        ]:
            btn = wx.Button(self.tab_fridge, label=f"Turn on {label}", size=(-1, 36))
            btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_refrigerator_switch(event, eid))
            self._describe_control(btn, f"{label} toggle button. The label changes after Viper reads the current Home Assistant switch state.")
            controls_sizer.Add(btn, 0, wx.ALL | wx.EXPAND, 5)
            self.refrigerator_action_buttons[entity_id] = btn

        reset_refresh_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refrigerator_reset_filter = wx.Button(self.tab_fridge, label="Reset Water Filter", size=(-1, 36))
        self.btn_refrigerator_reset_filter.Bind(
            wx.EVT_BUTTON,
            lambda event: self.on_refrigerator_press_button(event, "button.refrigerator_reset_water_filter"),
        )
        self._describe_control(
            self.btn_refrigerator_reset_filter,
            "Reset water filter button. Sends the Samsung refrigerator water filter reset button press through Home Assistant.",
        )
        reset_refresh_row.Add(self.btn_refrigerator_reset_filter, 1, wx.ALL | wx.EXPAND, 5)
        self.refrigerator_action_buttons["button.refrigerator_reset_water_filter"] = self.btn_refrigerator_reset_filter

        self.btn_refresh_refrigerator_controls = wx.Button(self.tab_fridge, label="Refresh Refrigerator Controls", size=(-1, 36))
        self.btn_refresh_refrigerator_controls.Bind(wx.EVT_BUTTON, lambda event: self.refresh_refrigerator_controls_status(announce=True))
        self._describe_control(
            self.btn_refresh_refrigerator_controls,
            "Refresh refrigerator controls button. Reads the latest refrigerator entities from Home Assistant.",
        )
        reset_refresh_row.Add(self.btn_refresh_refrigerator_controls, 1, wx.ALL | wx.EXPAND, 5)
        controls_sizer.Add(reset_refresh_row, 0, wx.EXPAND)
        outer.Add(controls_sizer, 0, wx.ALL | wx.EXPAND, 10)
        wx.CallAfter(self.refresh_refrigerator_controls_status)

        btn_save = wx.Button(self.tab_fridge, label="Save All Fridge Settings", size=(-1, 40))
        btn_save.Bind(wx.EVT_BUTTON, self.on_save_fridge_settings)
        outer.Add(btn_save, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_fridge.SetSizer(outer)

    def _get_chime_list(self):
        files = ["(Default)"]
        if cfg.CHIMES_DIR.exists():
            files += [
                f.name
                for f in cfg.CHIMES_DIR.iterdir()
                if f.suffix.lower() in (".mp3", ".wav")
            ]
        return files

    def _on_fridge_channel_change(self, ch_key):
        ctrl = self._fridge_controls.get(ch_key, {})
        if not ctrl:
            return
        chime = ctrl["chime"].GetStringSelection()
        channels = self.config.setdefault("broadcast_channels", {})
        channels[ch_key] = {
            "mode": self._normalize_broadcast_mode(ctrl["mode"].GetStringSelection()),
            "chime": "" if chime == "(Default)" else chime,
        }
        self.save_config()

    def on_save_fridge_settings(self, event):
        channels = self.config.setdefault("broadcast_channels", {})
        for ch_key, ctrl in self._fridge_controls.items():
            chime = ctrl["chime"].GetStringSelection()
            channels[ch_key] = {
                "mode": self._normalize_broadcast_mode(ctrl["mode"].GetStringSelection()),
                "chime": "" if chime == "(Default)" else chime,
            }
        self.config["broadcast_channels"] = channels
        self.save_config()
        self.notify("Fridge & Freezer settings saved.", priority=10)

    def _sync_fridge_controls(self):
        channels_cfg = self.config.get("broadcast_channels", {})
        chime_list = self._get_chime_list()
        for ch_key, ctrl in self._fridge_controls.items():
            ch_data = channels_cfg.get(ch_key, {"mode": "chime", "chime": ""})
            ctrl["mode"].SetStringSelection(ch_data.get("mode", "chime"))
            current_chime = ch_data.get("chime", "")
            ctrl["chime"].Set(chime_list)
            ctrl["chime"].SetStringSelection(current_chime if current_chime in chime_list else "(Default)")

    def _on_test_fridge_chime(self, ch_key: str):
        ctrl = self._fridge_controls.get(ch_key, {})
        chime = ctrl["chime"].GetStringSelection() if ctrl else ""
        chime_file = "" if chime == "(Default)" else chime
        self._safe_submit(audio.play_broadcast_chime, chime_file, ch_key)
        label = ch_key.replace("_", " ").title()
        self.notify(f"Testing {label} chime.", priority=10)

    def _call_ha_service(self, domain_service: str, entity_id: str):
        return self._call_ha_service_data(domain_service, {"entity_id": entity_id})

    def _refrigerator_control_entities(self):
        return {
            "fridge_door": "binary_sensor.refrigerator_fridge_door",
            "freezer_door": "binary_sensor.refrigerator_freezer_door",
            "filter_status": "binary_sensor.refrigerator_filter_status",
            "fridge_number": "number.refrigerator_fridge_temperature",
            "freezer_number": "number.refrigerator_freezer_temperature",
            "water_filter_usage": "sensor.refrigerator_water_filter_usage",
            "fridge_sensor": "sensor.refrigerator_fridge_temperature",
            "freezer_sensor": "sensor.refrigerator_freezer_temperature",
            "power": "sensor.refrigerator_power",
            "energy": "sensor.refrigerator_energy",
            "power_cool": "switch.refrigerator_power_cool",
            "power_freeze": "switch.refrigerator_power_freeze",
            "sabbath_mode": "switch.refrigerator_sabbath_mode",
            "cubed_ice": "switch.refrigerator_cubed_ice",
            "reset_filter": "button.refrigerator_reset_water_filter",
        }

    def _ha_state_value(self, state_result):
        if not state_result.get("exists"):
            return "missing" if state_result.get("ok") else f"unknown: {state_result.get('message')}"
        entity = state_result.get("entity") or {}
        state = str(entity.get("state", "unknown")).strip() or "unknown"
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        unit = str(attrs.get("unit_of_measurement") or "").strip()
        if unit and state not in {"unknown", "unavailable"}:
            return f"{state} {unit}"
        return state

    def get_refrigerator_control_status(self, *, timeout=5):
        entities = self._refrigerator_control_entities()
        states = {
            key: self._get_ha_entity_state(entity_id, timeout=timeout)
            for key, entity_id in entities.items()
        }
        return {
            "ok": all(state.get("ok") for state in states.values()),
            "entities": entities,
            "states": states,
            "message": self._format_refrigerator_control_status(states, entities),
        }

    def _format_refrigerator_control_status(self, states, entities):
        value = self._ha_state_value
        return "\n".join(
            [
                f"Fridge door: {value(states['fridge_door'])}. Freezer door: {value(states['freezer_door'])}.",
                f"Fridge setpoint: {value(states['fridge_number'])}. Freezer setpoint: {value(states['freezer_number'])}.",
                f"Fridge sensor: {value(states['fridge_sensor'])}. Freezer sensor: {value(states['freezer_sensor'])}.",
                f"Filter: {value(states['filter_status'])}. Water filter usage: {value(states['water_filter_usage'])}.",
                f"Power: {value(states['power'])}. Energy: {value(states['energy'])}.",
                f"Power Cool: {value(states['power_cool'])}. Power Freeze: {value(states['power_freeze'])}. Sabbath Mode: {value(states['sabbath_mode'])}.",
                f"Cubed ice: {value(states['cubed_ice'])}. Filter reset: {entities['reset_filter']}.",
            ]
        )

    def refresh_refrigerator_controls_status(self, announce=False):
        if hasattr(self, "refrigerator_status_txt"):
            self.refrigerator_status_txt.SetValue("Checking refrigerator controls...")
        self._safe_submit(self._run_refrigerator_controls_status_check, announce)

    def _run_refrigerator_controls_status_check(self, announce=False):
        status = self.get_refrigerator_control_status(timeout=5)
        wx.CallAfter(self._finish_refrigerator_controls_status, status, announce)

    def _finish_refrigerator_controls_status(self, status, announce=False):
        states = status.get("states") or {}
        if hasattr(self, "refrigerator_status_txt"):
            self.refrigerator_status_txt.SetValue(status.get("message") or "Refrigerator controls unavailable.")
        for entity_id, widget in getattr(self, "refrigerator_control_widgets", {}).items():
            state = next((result for result in states.values() if result.get("entity_id") == entity_id), {})
            if state.get("exists"):
                try:
                    widget.SetValue(float((state.get("entity") or {}).get("state")))
                except (TypeError, ValueError):
                    pass
        for label, entity_id in [
            ("Power Cool", "switch.refrigerator_power_cool"),
            ("Power Freeze", "switch.refrigerator_power_freeze"),
            ("Sabbath Mode", "switch.refrigerator_sabbath_mode"),
        ]:
            button = getattr(self, "refrigerator_action_buttons", {}).get(entity_id)
            state = next((result for result in states.values() if result.get("entity_id") == entity_id), {})
            current = str(((state.get("entity") or {}).get("state") or "unknown")).lower() if state.get("exists") else "unknown"
            next_label = f"Turn {'off' if current == 'on' else 'on'} {label}"
            if button:
                button.SetLabel(next_label)
                button.SetName(next_label)
                button.SetToolTip(f"{next_label}. Current state is {current}.")
        if announce:
            self.notify("Refrigerator controls refreshed.", priority=10)

    def on_refrigerator_set_number(self, event, entity_id):
        spin = getattr(self, "refrigerator_control_widgets", {}).get(entity_id)
        value = spin.GetValue() if spin else None
        if value is None:
            self.notify("Choose a refrigerator temperature first.", priority=10)
            return
        self._run_refrigerator_service_async(
            "number/set_value",
            {"entity_id": entity_id, "value": value},
            f"Set {entity_id} to {value}.",
            timeout=30,
        )

    def on_refrigerator_switch(self, event, entity_id):
        button = getattr(self, "refrigerator_action_buttons", {}).get(entity_id)
        turn_off = button and str(button.GetLabel()).lower().startswith("turn off")
        service = "switch/turn_off" if turn_off else "switch/turn_on"
        self._run_refrigerator_service_async(service, {"entity_id": entity_id}, f"Sent {service.replace('/', '.')} to {entity_id}.")

    def on_refrigerator_press_button(self, event, entity_id):
        self._run_refrigerator_service_async("button/press", {"entity_id": entity_id}, f"Pressed {entity_id}.")

    def _run_refrigerator_service_async(self, service, payload, success_message, *, timeout=10):
        def worker():
            ok = self._call_ha_service_data(service, payload, timeout=timeout)
            if ok:
                wx.CallAfter(lambda: self.notify(success_message, priority=10))
                wx.CallAfter(lambda: wx.CallLater(1200, self.refresh_refrigerator_controls_status))

        self._safe_submit(worker)

    def _configured_ice_maker_entities(self):
        return {
            "switch": self.config.get("ice_maker_switch_entity") or cfg.ICE_MAKER_SWITCH_ENTITY,
            "keep_on": self.config.get("ice_maker_keep_on_entity") or cfg.ICE_MAKER_KEEP_ON_ENTITY,
            "counter": self.config.get("ice_maker_counter_entity") or cfg.ICE_MAKER_COUNTER_ENTITY,
        }

    def _get_ha_entity_state(self, entity_id: str, *, timeout=5):
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            return {"ok": False, "exists": False, "message": "Entity id is blank."}
        try:
            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                raise RuntimeError("Home Assistant host or token is missing.")
            response = requests.get(
                f"http://{ha_ip}:{ha_port}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
            if response.status_code == 404:
                return {"ok": True, "exists": False, "entity_id": entity_id, "message": "Entity was not found."}
            response.raise_for_status()
            return {"ok": True, "exists": True, "entity_id": entity_id, "entity": response.json()}
        except Exception as e:
            return {"ok": False, "exists": False, "entity_id": entity_id, "message": str(e)}

    def get_ice_maker_status(self, *, timeout=5):
        entities = self._configured_ice_maker_entities()
        switch = self._get_ha_entity_state(entities["switch"], timeout=timeout)
        keep_on = self._get_ha_entity_state(entities["keep_on"], timeout=timeout)
        counter = self._get_ha_entity_state(entities["counter"], timeout=timeout)

        switch_state = str((switch.get("entity") or {}).get("state") or "").strip().lower() if switch.get("exists") else ""
        keep_on_state = str((keep_on.get("entity") or {}).get("state") or "").strip().lower() if keep_on.get("exists") else ""
        counter_state = str((counter.get("entity") or {}).get("state") or "").strip() if counter.get("exists") else ""
        is_on = switch_state == "on"
        is_off = switch_state == "off"
        counter_text = counter_state if counter_state else ("missing" if counter.get("ok") else f"unknown: {counter.get('message')}")
        if is_on:
            summary = f"on. Keep-on helper is {keep_on_state or 'unknown'}."
            button_label = "Turn Ice Maker Off"
        elif is_off:
            summary = f"off. Keep-on helper is {keep_on_state or 'unknown'}."
            button_label = "Turn Ice Maker On"
        elif switch.get("ok"):
            summary = f"state is {switch_state or 'missing'}."
            button_label = "Turn Ice Maker On"
        else:
            summary = f"status unknown: {switch.get('message') or 'could not reach Home Assistant'}."
            button_label = "Turn Ice Maker On"
        return {
            "ok": bool(switch.get("ok") and counter.get("ok")),
            "switch_entity": entities["switch"],
            "keep_on_entity": entities["keep_on"],
            "counter_entity": entities["counter"],
            "switch_state": switch_state or "unknown",
            "keep_on_state": keep_on_state or "unknown",
            "counter_state": counter_state,
            "counter_text": counter_text,
            "is_on": is_on,
            "button_label": button_label,
            "summary": summary,
            "message": self._format_ice_maker_status(summary, counter_text, entities),
        }

    def _format_ice_maker_status(self, summary, counter_text, entities):
        return "\n".join(
            [
                f"Ice maker is {summary}",
                f"Ice usage counter: {counter_text}.",
                f"Switch entity: {entities['switch']}",
                f"Keep-on helper: {entities['keep_on']}",
                f"Counter entity: {entities['counter']}",
            ]
        )

    def refresh_ice_maker_status(self, announce=False):
        if hasattr(self, "ice_maker_status_txt"):
            self.ice_maker_status_txt.SetValue("Checking ice maker status...")
        self._safe_submit(self._run_ice_maker_status_check, announce)

    def _run_ice_maker_status_check(self, announce=False):
        status = self.get_ice_maker_status(timeout=5)
        wx.CallAfter(self._finish_ice_maker_status, status, announce)

    def _finish_ice_maker_status(self, status, announce=False):
        self._ice_maker_switch_state = status.get("switch_state", "unknown")
        if hasattr(self, "ice_maker_status_txt"):
            self.ice_maker_status_txt.SetValue(status.get("message") or "Ice maker status unavailable.")
        if hasattr(self, "btn_ice_toggle"):
            label = status.get("button_label") or "Turn Ice Maker On"
            self.btn_ice_toggle.SetLabel(label)
            self.btn_ice_toggle.SetName(label)
            self.btn_ice_toggle.SetToolTip(f"{label}. Current ice maker status: {status.get('summary', 'unknown')}")
        if announce:
            self.notify(status.get("message") or "Ice maker status refreshed.", priority=10)

    def _call_ha_service_data(self, domain_service: str, data: dict, *, timeout=10):
        entity_id = (data or {}).get("entity_id", "Home Assistant")
        try:
            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                raise RuntimeError("Home Assistant host or token is missing.")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            response = requests.post(
                f"http://{ha_ip}:{ha_port}/api/services/{domain_service}",
                headers=headers,
                json=data or {},
                timeout=timeout,
            )
            response.raise_for_status()
            return True
        except requests.exceptions.ReadTimeout:
            self.notify(
                f"Home Assistant did not answer within {timeout} seconds for {entity_id}. "
                "The Roborock integration can be slow; press Refresh vacuum controls to check whether the setting changed.",
                priority=10,
            )
            return False
        except requests.exceptions.HTTPError as e:
            if self._is_hidden_vacuum_setting_entity_id(entity_id):
                self.notify(
                    "Home Assistant reports that Roborock dock empty mode exists, but its integration rejects write attempts. "
                    "Viper hides this control from the vacuum tab; change it in Home Assistant until the integration exposes a reliable service.",
                    priority=10,
                )
            else:
                self.notify(f"HA service failed for {entity_id}: {e}", priority=10)
            return False
        except Exception as e:
            self.notify(f"HA service failed for {entity_id}: {e}", priority=10)
            return False

    def _call_ha_service_response(self, domain_service: str, data: dict):
        entity_id = (data or {}).get("entity_id", "Home Assistant")
        try:
            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                raise RuntimeError("Home Assistant host or token is missing.")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            response = requests.post(
                f"http://{ha_ip}:{ha_port}/api/services/{domain_service}?return_response",
                headers=headers,
                json=data or {},
                timeout=15,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            return {"ok": True, "data": payload}
        except Exception as e:
            return {"ok": False, "message": f"HA service failed for {entity_id}: {e}"}

    def on_ice_maker_on(self, event):
        entities = self._configured_ice_maker_entities()
        ok_helper = self._call_ha_service("input_boolean/turn_on", entities["keep_on"])
        ok_switch = self._call_ha_service("switch/turn_on", entities["switch"])
        if ok_helper and ok_switch:
            msg = "Ice maker turned on with refill override enabled."
            self.notify(msg, priority=10)
            self._safe_submit(audio.play_notification, "utilities", msg)
            wx.CallLater(750, self.refresh_ice_maker_status)
            return msg
        return "Ice maker on request failed. Check Home Assistant status."

    def on_ice_maker_off(self, event):
        entities = self._configured_ice_maker_entities()
        ok_switch = self._call_ha_service("switch/turn_off", entities["switch"])
        ok_helper = self._call_ha_service("input_boolean/turn_off", entities["keep_on"])
        if ok_switch and ok_helper:
            msg = "Ice maker turned off and refill override cleared."
            self.notify(msg, priority=10)
            self._safe_submit(audio.play_notification, "utilities", msg)
            wx.CallLater(750, self.refresh_ice_maker_status)
            return msg
        return "Ice maker off request failed. Check Home Assistant status."

    def on_ice_maker_toggle(self, event):
        if getattr(self, "_ice_maker_switch_state", "unknown") == "on":
            return self.on_ice_maker_off(event)
        return self.on_ice_maker_on(event)
