import json
import logging
import re

import wx

import viper_config as cfg
import viper_discovery as discovery
import viper_vacuum as vacuum


VACUUM_CLEANING_MODES = vacuum.VACUUM_CLEANING_MODES
VACUUM_CLEANING_MODE_ORDER = vacuum.VACUUM_CLEANING_MODE_ORDER
vacuum_basic_actions_for_state = vacuum.vacuum_basic_actions_for_state
vacuum_cleaning_mode_service_calls = vacuum.vacuum_cleaning_mode_service_calls
_is_hidden_vacuum_setting_entity_id = vacuum.is_hidden_vacuum_setting_entity_id
_normalize_vacuum_cleaning_mode = vacuum.normalize_vacuum_cleaning_mode


class VacuumTabMixin:
    def setup_vacuum_tab(self):
        self.vacuum_state_entities = []
        self.vacuum_control_entities = []
        self.vacuum_control_widgets = {}
        self.vacuum_action_buttons = {}
        self._pending_vacuum_focus_entity_id = ""
        self.vacuum_rooms = []
        self.vacuum_room_checks = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        top_box = wx.StaticBox(self.tab_vacuum, label="Roborock Vacuum Controls")
        top = wx.StaticBoxSizer(top_box, wx.VERTICAL)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self.tab_vacuum, label="Vacuum entity:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_choice = wx.Choice(self.tab_vacuum, choices=[])
        self.vacuum_choice.Bind(wx.EVT_CHOICE, self.on_vacuum_choice_change)
        self._describe_control(
            self.vacuum_choice,
            "Vacuum entity picker. Choose which Roborock vacuum Viper should control.",
        )
        row.Add(self.vacuum_choice, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_refresh_vacuum = wx.Button(self.tab_vacuum, label="Refresh vacuum controls", size=(-1, 40))
        self.btn_refresh_vacuum.Bind(wx.EVT_BUTTON, self.on_refresh_vacuum)
        self._describe_control(
            self.btn_refresh_vacuum,
            "Refresh vacuum controls button. Scans Home Assistant for Roborock vacuum controls, modes, switches, buttons, and status sensors.",
        )
        row.Add(self.btn_refresh_vacuum, 0, wx.ALL, 5)
        top.Add(row, 0, wx.EXPAND)

        self.vacuum_status_txt = self._make_accessible_status_text(
            self.tab_vacuum,
            value="Press Refresh vacuum controls to scan Home Assistant for Roborock controls.",
            size=(-1, 150),
        )
        self._describe_control(
            self.vacuum_status_txt,
            "Vacuum status. Summarizes the selected vacuum state and nearby Roborock status sensors.",
        )
        top.Add(self.vacuum_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        mode_row.Add(wx.StaticText(self.tab_vacuum, label="Cleaning job mode:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_cleaning_mode_choice = wx.Choice(
            self.tab_vacuum,
            choices=[VACUUM_CLEANING_MODES[key] for key in VACUUM_CLEANING_MODE_ORDER],
        )
        self.vacuum_cleaning_mode_choice.SetSelection(0)
        self._describe_control(
            self.vacuum_cleaning_mode_choice,
            "Vacuum cleaning job mode picker. Choose vacuum and mop, vacuum only, or mop only before starting a whole-home or selected-room clean.",
        )
        mode_row.Add(self.vacuum_cleaning_mode_choice, 1, wx.ALL | wx.EXPAND, 5)
        top.Add(mode_row, 0, wx.EXPAND)

        self.vacuum_actions_panel = wx.Panel(self.tab_vacuum)
        self.vacuum_actions_sizer = wx.GridSizer(rows=0, cols=3, vgap=6, hgap=6)
        self.vacuum_actions_panel.SetSizer(self.vacuum_actions_sizer)
        top.Add(self.vacuum_actions_panel, 0, wx.ALL | wx.EXPAND, 5)

        fan_row = wx.BoxSizer(wx.HORIZONTAL)
        fan_row.Add(wx.StaticText(self.tab_vacuum, label="Vacuum suction speed:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_fan_choice = wx.Choice(self.tab_vacuum, choices=[])
        self._describe_control(
            self.vacuum_fan_choice,
            "Vacuum suction speed picker. Choose a fan speed from the selected vacuum, then press Set suction speed.",
        )
        fan_row.Add(self.vacuum_fan_choice, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_set_vacuum_fan = wx.Button(self.tab_vacuum, label="Set suction speed", size=(-1, 40))
        self.btn_set_vacuum_fan.Bind(wx.EVT_BUTTON, self.on_vacuum_set_fan_speed)
        self._describe_control(
            self.btn_set_vacuum_fan,
            "Set suction speed button. Sends the chosen suction or fan speed to the selected Roborock vacuum.",
        )
        fan_row.Add(self.btn_set_vacuum_fan, 0, wx.ALL, 5)
        top.Add(fan_row, 0, wx.EXPAND)
        sizer.Add(top, 0, wx.ALL | wx.EXPAND, 10)

        room_box = wx.StaticBox(self.tab_vacuum, label="Room Cleaning")
        room_outer = wx.StaticBoxSizer(room_box, wx.VERTICAL)
        room_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_vacuum_rooms = wx.Button(self.tab_vacuum, label="Refresh room list", size=(-1, 40))
        self.btn_refresh_vacuum_rooms.Bind(wx.EVT_BUTTON, self.on_refresh_vacuum_rooms)
        self._describe_control(
            self.btn_refresh_vacuum_rooms,
            "Refresh room list button. Asks Home Assistant for Roborock map rooms and fills the room checklist.",
        )
        room_buttons.Add(self.btn_refresh_vacuum_rooms, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_clean_vacuum_rooms = wx.Button(self.tab_vacuum, label="Clean selected rooms", size=(-1, 40))
        self.btn_clean_vacuum_rooms.Bind(wx.EVT_BUTTON, self.on_vacuum_clean_selected_rooms)
        self._describe_control(
            self.btn_clean_vacuum_rooms,
            "Clean selected rooms button. Sends the checked Roborock rooms to the selected vacuum.",
        )
        room_buttons.Add(self.btn_clean_vacuum_rooms, 1, wx.ALL | wx.EXPAND, 5)
        room_outer.Add(room_buttons, 0, wx.EXPAND)

        self.vacuum_room_scroll = wx.ScrolledWindow(self.tab_vacuum, style=wx.VSCROLL | wx.TAB_TRAVERSAL, size=(-1, 150))
        self.vacuum_room_scroll.SetScrollRate(0, 20)
        self.vacuum_room_sizer = wx.BoxSizer(wx.VERTICAL)
        self.vacuum_room_scroll.SetSizer(self.vacuum_room_sizer)
        self._describe_control(
            self.vacuum_room_scroll,
            "Roborock room checkbox list. Tab through each room checkbox. Press Space to check or uncheck rooms, then press Clean selected rooms.",
        )
        room_outer.Add(self.vacuum_room_scroll, 0, wx.ALL | wx.EXPAND, 5)

        repeat_row = wx.BoxSizer(wx.HORIZONTAL)
        repeat_row.Add(wx.StaticText(self.tab_vacuum, label="Room clean repeat count:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_room_repeat = wx.SpinCtrl(self.tab_vacuum, min=1, max=3, initial=1)
        self._describe_control(
            self.vacuum_room_repeat,
            "Room clean repeat count. Choose 1, 2, or 3 passes for selected rooms.",
        )
        repeat_row.Add(self.vacuum_room_repeat, 0, wx.ALL, 5)
        room_outer.Add(repeat_row, 0, wx.EXPAND)

        self.vacuum_room_status_txt = self._make_accessible_status_text(
            self.tab_vacuum,
            value="Press Refresh room list to load Roborock rooms from Home Assistant.",
            size=(-1, 80),
        )
        self._describe_control(
            self.vacuum_room_status_txt,
            "Vacuum room status. Reports map and room discovery results.",
        )
        room_outer.Add(self.vacuum_room_status_txt, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(room_outer, 0, wx.ALL | wx.EXPAND, 10)

        dynamic_box = wx.StaticBox(self.tab_vacuum, label="Discovered Roborock Settings")
        dynamic_outer = wx.StaticBoxSizer(dynamic_box, wx.VERTICAL)
        self.vacuum_controls_panel = wx.Panel(self.tab_vacuum)
        self.vacuum_controls_sizer = wx.BoxSizer(wx.VERTICAL)
        self.vacuum_controls_panel.SetSizer(self.vacuum_controls_sizer)
        dynamic_outer.Add(self.vacuum_controls_panel, 0, wx.ALL | wx.EXPAND, 5)
        sizer.Add(dynamic_outer, 0, wx.ALL | wx.EXPAND, 10)

        command_box = wx.StaticBox(self.tab_vacuum, label="Advanced Roborock Command")
        command = wx.StaticBoxSizer(command_box, wx.VERTICAL)
        command.Add(wx.StaticText(self.tab_vacuum, label="Command name:"), 0, wx.ALL, 5)
        self.vacuum_command_txt = wx.TextCtrl(self.tab_vacuum, value="")
        self._describe_control(
            self.vacuum_command_txt,
            "Advanced command name. Example: app_segment_clean. Leave blank unless you know the Roborock command to send.",
        )
        command.Add(self.vacuum_command_txt, 0, wx.ALL | wx.EXPAND, 5)
        command.Add(wx.StaticText(self.tab_vacuum, label="Parameters JSON, optional:"), 0, wx.ALL, 5)
        self.vacuum_params_txt = wx.TextCtrl(self.tab_vacuum, value="", style=wx.TE_MULTILINE, size=(-1, 90))
        self._describe_control(
            self.vacuum_params_txt,
            "Advanced command parameters JSON. Optional. Example: a JSON object or list for Home Assistant vacuum send command parameters.",
        )
        command.Add(self.vacuum_params_txt, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_send_vacuum_command = wx.Button(self.tab_vacuum, label="Send advanced vacuum command", size=(-1, 40))
        self.btn_send_vacuum_command.Bind(wx.EVT_BUTTON, self.on_vacuum_send_command)
        self._describe_control(
            self.btn_send_vacuum_command,
            "Send advanced vacuum command button. Calls Home Assistant vacuum send command for the selected Roborock vacuum.",
        )
        command.Add(self.btn_send_vacuum_command, 0, wx.ALL | wx.EXPAND, 5)

        command.Add(wx.StaticText(self.tab_vacuum, label="Home Assistant area IDs, comma separated, optional:"), 0, wx.ALL, 5)
        self.vacuum_area_ids_txt = wx.TextCtrl(self.tab_vacuum, value="")
        self._describe_control(
            self.vacuum_area_ids_txt,
            "Home Assistant area IDs for vacuum clean area. Enter comma separated area IDs only if your vacuum segments are mapped to Home Assistant areas.",
        )
        command.Add(self.vacuum_area_ids_txt, 0, wx.ALL | wx.EXPAND, 5)
        self.btn_clean_vacuum_areas = wx.Button(self.tab_vacuum, label="Clean Home Assistant areas", size=(-1, 40))
        self.btn_clean_vacuum_areas.Bind(wx.EVT_BUTTON, self.on_vacuum_clean_areas)
        self._describe_control(
            self.btn_clean_vacuum_areas,
            "Clean Home Assistant areas button. Calls vacuum clean area using the comma separated area IDs.",
        )
        command.Add(self.btn_clean_vacuum_areas, 0, wx.ALL | wx.EXPAND, 5)

        goto_row = wx.BoxSizer(wx.HORIZONTAL)
        goto_row.Add(wx.StaticText(self.tab_vacuum, label="Go to X:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_goto_x_txt = wx.TextCtrl(self.tab_vacuum, value="25500")
        self._describe_control(
            self.vacuum_goto_x_txt,
            "Roborock go to X coordinate. Enter an integer coordinate. The dock is often near 25500.",
        )
        goto_row.Add(self.vacuum_goto_x_txt, 1, wx.ALL | wx.EXPAND, 5)
        goto_row.Add(wx.StaticText(self.tab_vacuum, label="Go to Y:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.vacuum_goto_y_txt = wx.TextCtrl(self.tab_vacuum, value="25500")
        self._describe_control(
            self.vacuum_goto_y_txt,
            "Roborock go to Y coordinate. Enter an integer coordinate. The dock is often near 25500.",
        )
        goto_row.Add(self.vacuum_goto_y_txt, 1, wx.ALL | wx.EXPAND, 5)
        self.btn_vacuum_goto = wx.Button(self.tab_vacuum, label="Send vacuum to coordinates", size=(-1, 40))
        self.btn_vacuum_goto.Bind(wx.EVT_BUTTON, self.on_vacuum_goto_position)
        self._describe_control(
            self.btn_vacuum_goto,
            "Send vacuum to coordinates button. Calls the Roborock go to position service for the selected vacuum.",
        )
        goto_row.Add(self.btn_vacuum_goto, 0, wx.ALL, 5)
        command.Add(goto_row, 0, wx.EXPAND)
        sizer.Add(command, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_vacuum.SetSizer(sizer)
        wx.CallAfter(self.on_refresh_vacuum, None)

    def on_refresh_vacuum(self, event):
        self.vacuum_status_txt.SetValue("Scanning Home Assistant for Roborock vacuum controls...")
        self._safe_submit(self._run_vacuum_refresh)

    def _run_vacuum_refresh(self):
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        result = discovery.get_ha_states(
            token=ha_settings.get("ha_token"),
            ha_ip=ha_settings.get("ha_ip"),
            ha_port=ha_settings.get("ha_port"),
            timeout=8,
        )
        if not result.get("ok"):
            message = result.get("message") or result.get("error") or "Home Assistant scan failed."
            wx.CallAfter(self._finish_vacuum_refresh, [], [], f"Vacuum scan failed: {message}")
            return
        states = result.get("states", [])
        vacuums = [entity for entity in states if self._ha_domain(entity) == "vacuum"]
        roborock_vacuums = [entity for entity in vacuums if self._looks_like_roborock(entity)]
        selected_vacuums = roborock_vacuums or vacuums
        current = self._selected_vacuum_entity_id()
        if current and not any(e.get("entity_id") == current for e in selected_vacuums):
            current = ""
        selected = current or (selected_vacuums[0].get("entity_id") if selected_vacuums else "")
        controls = self._find_vacuum_related_controls(states, selected)
        summary = self._build_vacuum_summary(selected_vacuums, controls, selected)
        wx.CallAfter(self._finish_vacuum_refresh, selected_vacuums, controls, summary)

    def _finish_vacuum_refresh(self, vacuums, controls, summary):
        self.vacuum_state_entities = vacuums
        self.vacuum_control_entities = controls
        current = self._selected_vacuum_entity_id()
        vacuum_choices = [self._entity_choice_label(entity) for entity in vacuums]
        self.vacuum_choice.Set(vacuum_choices)
        if vacuum_choices:
            selected_label = next(
                (label for label, entity in zip(vacuum_choices, vacuums) if entity.get("entity_id") == current),
                vacuum_choices[0],
            )
            self.vacuum_choice.SetStringSelection(selected_label)
        self.vacuum_status_txt.SetValue(summary)
        self._populate_vacuum_fan_speed()
        self._sync_vacuum_cleaning_mode_choice()
        self._rebuild_vacuum_basic_actions()
        self._finish_vacuum_room_refresh(
            self._get_saved_vacuum_rooms(self._selected_vacuum_entity_id()),
            "Saved room list loaded. Press Refresh room list to update it from Home Assistant.",
            save=False,
        )
        self._rebuild_vacuum_dynamic_controls()
        self.tab_vacuum.Layout()
        self.tab_vacuum.FitInside()
        self._restore_pending_vacuum_focus()

    def _selected_vacuum_entity_id(self):
        if not hasattr(self, "vacuum_choice"):
            return ""
        selection = self.vacuum_choice.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(getattr(self, "vacuum_state_entities", [])):
            return ""
        return self.vacuum_state_entities[selection].get("entity_id", "")

    def on_vacuum_choice_change(self, event):
        selected = self._selected_vacuum_entity_id()
        controls = self._find_vacuum_related_controls(
            getattr(self, "_last_vacuum_states", []) or getattr(self, "vacuum_control_entities", []),
            selected,
        )
        if not controls:
            controls = getattr(self, "vacuum_control_entities", [])
        self.vacuum_control_entities = controls
        self.vacuum_status_txt.SetValue(self._build_vacuum_summary(self.vacuum_state_entities, controls, selected))
        self._populate_vacuum_fan_speed()
        self._sync_vacuum_cleaning_mode_choice()
        self._rebuild_vacuum_basic_actions()
        self._finish_vacuum_room_refresh(
            self._get_saved_vacuum_rooms(selected),
            "Saved room list loaded. Press Refresh room list to update it from Home Assistant.",
            save=False,
        )
        self._rebuild_vacuum_dynamic_controls()

    def _ha_domain(self, entity):
        entity_id = entity.get("entity_id", "")
        return entity_id.split(".", 1)[0] if "." in entity_id else ""

    def _ha_name(self, entity):
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        return str(attrs.get("friendly_name") or entity.get("entity_id") or "")

    def _looks_like_roborock(self, entity):
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        text = " ".join(
            str(part).lower()
            for part in [
                entity.get("entity_id"),
                attrs.get("friendly_name"),
                attrs.get("manufacturer"),
                attrs.get("model"),
                attrs.get("device_class"),
                attrs.get("platform"),
                attrs.get("integration"),
            ]
        )
        return any(token in text for token in ["roborock", "cinderella", "saros", "qrevo", "q revo", "s7", "s8"])

    def _vacuum_match_tokens(self, selected_entity_id):
        tokens = {"roborock", "cinderella", "saros", "qrevo", "q revo"}
        if selected_entity_id and "." in selected_entity_id:
            base = selected_entity_id.split(".", 1)[1]
            tokens.add(base.lower())
            tokens.update(part for part in re.split(r"[_\s-]+", base.lower()) if len(part) >= 4)
        return tokens

    def _find_vacuum_related_controls(self, states, selected_entity_id):
        if not states:
            return []
        self._last_vacuum_states = states
        control_domains = {"vacuum", "select", "number", "switch", "button", "sensor", "binary_sensor"}
        tokens = self._vacuum_match_tokens(selected_entity_id)
        related = []
        for entity in states:
            domain = self._ha_domain(entity)
            if domain not in control_domains:
                continue
            attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            text = " ".join(str(part).lower() for part in [entity.get("entity_id"), attrs.get("friendly_name"), attrs.get("manufacturer"), attrs.get("model")])
            if entity.get("entity_id") == selected_entity_id or any(token and token in text for token in tokens):
                related.append(entity)
        return sorted(related, key=lambda e: (self._ha_domain(e), self._ha_name(e).lower(), e.get("entity_id", "")))

    def _entity_choice_label(self, entity):
        name = self._ha_name(entity)
        entity_id = entity.get("entity_id", "")
        state = entity.get("state", "unknown")
        return f"{name} ({entity_id}, state {state})"

    def _short_entity_label(self, entity):
        name = self._ha_name(entity)
        entity_id = entity.get("entity_id", "")
        if name and name != entity_id:
            return f"{name} ({entity_id})"
        return entity_id

    def _build_vacuum_summary(self, vacuums, controls, selected):
        lines = ["Vacuum Controls", ""]
        if not vacuums:
            lines.append("No vacuum entities found in Home Assistant. Check the HA Status tab and your Home Assistant token.")
            return "\n".join(lines)
        selected_entity = next((entity for entity in vacuums if entity.get("entity_id") == selected), vacuums[0])
        attrs = selected_entity.get("attributes") if isinstance(selected_entity.get("attributes"), dict) else {}
        battery = attrs.get("battery_level", "unknown")
        if battery == "unknown" or battery is None:
            battery_entity = next((entity for entity in controls if entity.get("entity_id", "").endswith("_battery")), None)
            if battery_entity:
                battery = battery_entity.get("state", "unknown")
        lines.extend([
            f"Selected: {self._short_entity_label(selected_entity)}",
            f"State: {selected_entity.get('state', 'unknown')}",
            f"Battery: {battery}",
            f"Current suction speed: {attrs.get('fan_speed', 'unknown')}",
            f"Discovered related entities: {len(controls)}",
            "",
            "Interactive controls found:",
        ])
        counts = {}
        for entity in controls:
            domain = self._ha_domain(entity)
            counts[domain] = counts.get(domain, 0) + 1
        for domain in ["select", "number", "switch", "button", "sensor", "binary_sensor"]:
            if counts.get(domain):
                lines.append(f"{domain}: {counts[domain]}")
        if not any(self._ha_domain(entity) in {"select", "number", "switch", "button"} for entity in controls):
            lines.append("No extra Roborock select, number, switch, or button entities were found. Basic vacuum actions are still available.")
        sensor_entities = [entity for entity in controls if self._ha_domain(entity) in {"sensor", "binary_sensor"}]
        if sensor_entities:
            lines.extend(["", "Status snapshot:"])
            for entity in sensor_entities:
                lines.append(f"{self._short_entity_label(entity)}: {entity.get('state', 'unknown')}")
        return "\n".join(lines)

    def _populate_vacuum_fan_speed(self):
        selected = self._selected_vacuum_entity_id()
        entity = next((item for item in self.vacuum_state_entities if item.get("entity_id") == selected), None)
        attrs = entity.get("attributes") if entity and isinstance(entity.get("attributes"), dict) else {}
        speeds = attrs.get("fan_speed_list") if isinstance(attrs.get("fan_speed_list"), list) else []
        current = attrs.get("fan_speed")
        self.vacuum_fan_choice.Set([str(item) for item in speeds])
        if current and str(current) in [str(item) for item in speeds]:
            self.vacuum_fan_choice.SetStringSelection(str(current))
        elif speeds:
            self.vacuum_fan_choice.SetSelection(0)
        self.vacuum_fan_choice.Enable(bool(speeds))
        self.btn_set_vacuum_fan.Enable(bool(speeds))

    def _current_vacuum_cleaning_mode(self):
        if not hasattr(self, "vacuum_cleaning_mode_choice"):
            return _normalize_vacuum_cleaning_mode(self.config.get("vacuum_cleaning_mode", "vacuum_mop"))
        label = self.vacuum_cleaning_mode_choice.GetStringSelection()
        reverse = {value: key for key, value in VACUUM_CLEANING_MODES.items()}
        return _normalize_vacuum_cleaning_mode(reverse.get(label, self.config.get("vacuum_cleaning_mode", "vacuum_mop")))

    def _sync_vacuum_cleaning_mode_choice(self):
        if not hasattr(self, "vacuum_cleaning_mode_choice"):
            return
        mode = _normalize_vacuum_cleaning_mode(self.config.get("vacuum_cleaning_mode", "vacuum_mop"))
        label = VACUUM_CLEANING_MODES[mode]
        if label in [self.vacuum_cleaning_mode_choice.GetString(index) for index in range(self.vacuum_cleaning_mode_choice.GetCount())]:
            self.vacuum_cleaning_mode_choice.SetStringSelection(label)

    def _vacuum_cleaning_mode_calls(self, entity_id, mode):
        mode = _normalize_vacuum_cleaning_mode(mode)
        self.config["vacuum_cleaning_mode"] = mode
        self.save_config()
        current_fan = self.vacuum_fan_choice.GetStringSelection() if hasattr(self, "vacuum_fan_choice") else ""
        return vacuum_cleaning_mode_service_calls(entity_id, getattr(self, "vacuum_control_entities", []), mode, current_fan)

    def _rebuild_vacuum_basic_actions(self):
        if not hasattr(self, "vacuum_actions_sizer"):
            return
        self._clear_sizer(self.vacuum_actions_sizer)
        selected = self._selected_vacuum_entity_id()
        entity = next((item for item in getattr(self, "vacuum_state_entities", []) if item.get("entity_id") == selected), None)
        state = entity.get("state", "unknown") if entity else "unknown"
        for action in vacuum_basic_actions_for_state(state):
            btn = wx.Button(self.vacuum_actions_panel, label=action["label"], size=(-1, 40))
            btn.Bind(wx.EVT_BUTTON, lambda event, svc=action["service"]: self.on_vacuum_basic_action(event, svc))
            self._describe_control(
                btn,
                f"{action['label']} button. Useful when the selected vacuum state is {state}.",
            )
            self.vacuum_actions_sizer.Add(btn, 0, wx.EXPAND)
        self.vacuum_actions_panel.Layout()

    def _clear_sizer(self, sizer):
        while sizer.GetItemCount():
            item = sizer.GetItem(0)
            window = item.GetWindow()
            child_sizer = item.GetSizer()
            sizer.Detach(0)
            if window:
                window.Destroy()
            elif child_sizer:
                self._clear_sizer(child_sizer)

    def _room_checkbox_label(self, room, checked=False):
        label = room.get("label") or f"{room.get('name', 'Room')} ({room.get('segment', 'unknown')})"
        state = "checked" if checked else "not checked"
        return f"{label}, room ID {room.get('segment', 'unknown')}, {state}"

    def _on_vacuum_room_checkbox(self, event):
        check = event.GetEventObject()
        room = getattr(check, "_viper_room", {})
        checked = bool(check.GetValue())
        label = self._room_checkbox_label(room, checked)
        check.SetName(label)
        check.SetToolTip(label)
        self.vacuum_room_status_txt.SetValue(f"{label}. Press Clean selected rooms when your room choices are correct.")
        wx.CallAfter(self._safe_speak, label)
        event.Skip()

    def _rebuild_vacuum_dynamic_controls(self):
        self._clear_sizer(self.vacuum_controls_sizer)
        self.vacuum_control_widgets = {}
        self.vacuum_action_buttons = {}
        interactive = [entity for entity in self.vacuum_control_entities if self._show_vacuum_setting(entity)]
        if not interactive:
            self.vacuum_controls_sizer.Add(
                wx.StaticText(self.vacuum_controls_panel, label="No discovered setting entities yet. Press Refresh vacuum controls after Home Assistant is connected."),
                0,
                wx.ALL | wx.EXPAND,
                5,
            )
            self.vacuum_controls_panel.Layout()
            return
        for entity in interactive:
            domain = self._ha_domain(entity)
            entity_id = entity.get("entity_id", "")
            attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            label = self._short_entity_label(entity)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{label}:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            if domain == "select":
                options = [str(item) for item in attrs.get("options", [])] if isinstance(attrs.get("options"), list) else []
                choice = wx.Choice(self.vacuum_controls_panel, choices=options)
                if str(entity.get("state", "")) in options:
                    choice.SetStringSelection(str(entity.get("state")))
                elif options:
                    choice.SetSelection(0)
                btn_label = f"Apply {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_set_select(event, eid))
                self._describe_control(choice, f"{label} combo box. Choose a Roborock setting value, then press {btn_label}. Current value is {entity.get('state', 'unknown')}.")
                self._describe_control(btn, f"{btn_label} button. Sends the selected {label} value to Home Assistant.")
                row.Add(choice, 1, wx.ALL | wx.EXPAND, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_control_widgets[entity_id] = choice
                self.vacuum_action_buttons[entity_id] = btn
            elif domain == "number":
                minimum = attrs.get("min", 0)
                maximum = attrs.get("max", 100)
                step = attrs.get("step", 1)
                spin = wx.SpinCtrlDouble(self.vacuum_controls_panel, min=float(minimum), max=float(maximum), inc=float(step))
                try:
                    spin.SetValue(float(entity.get("state", minimum)))
                except (TypeError, ValueError):
                    spin.SetValue(float(minimum))
                btn_label = f"Set {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_set_number(event, eid))
                self._describe_control(spin, f"{label} numeric value. Adjust the value, then press {btn_label}. Current value is {entity.get('state', 'unknown')}.")
                self._describe_control(btn, f"{btn_label} button. Sends the numeric value to Home Assistant.")
                row.Add(spin, 1, wx.ALL | wx.EXPAND, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_control_widgets[entity_id] = spin
                self.vacuum_action_buttons[entity_id] = btn
            elif domain == "switch":
                state = str(entity.get("state", "")).lower()
                turn_on = state != "on"
                btn_label = f"Turn {'on' if turn_on else 'off'} {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id, next_on=turn_on: self.on_vacuum_switch(event, eid, next_on))
                self._describe_control(btn, f"{btn_label} button. Current state is {state or 'unknown'}.")
                row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{label} current state {state or 'unknown'}"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_action_buttons[entity_id] = btn
            elif domain == "button":
                btn_label = f"Press {label}"
                btn = wx.Button(self.vacuum_controls_panel, label=btn_label)
                btn.Bind(wx.EVT_BUTTON, lambda event, eid=entity_id: self.on_vacuum_press_button(event, eid))
                self._describe_control(btn, f"{btn_label} button. Sends a Home Assistant button press for this Roborock control.")
                row.Add(wx.StaticText(self.vacuum_controls_panel, label=f"{label} last state {entity.get('state', 'unknown')}"), 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
                row.Add(btn, 0, wx.ALL, 5)
                self.vacuum_action_buttons[entity_id] = btn
            self.vacuum_controls_sizer.Add(row, 0, wx.EXPAND)
        self.vacuum_controls_panel.Layout()

    def _restore_pending_vacuum_focus(self):
        entity_id = getattr(self, "_pending_vacuum_focus_entity_id", "")
        if not entity_id:
            return
        self._pending_vacuum_focus_entity_id = ""
        button = getattr(self, "vacuum_action_buttons", {}).get(entity_id)
        if not button:
            return
        wx.CallAfter(self._focus_vacuum_action_button, button)

    def _focus_vacuum_action_button(self, button):
        try:
            if hasattr(button, "SetFocusFromKbd"):
                button.SetFocusFromKbd()
            else:
                button.SetFocus()
        except Exception:
            logging.debug("Could not restore focus to vacuum action button.", exc_info=True)

    def _show_vacuum_setting(self, entity):
        entity_id = entity.get("entity_id", "")
        domain = self._ha_domain(entity)
        if _is_hidden_vacuum_setting_entity_id(entity_id):
            return False
        if domain in {"select", "number"}:
            return True
        if domain == "switch" and "child_lock" in entity_id:
            return True
        return False

    def on_vacuum_basic_action(self, event, service):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        if service == "vacuum/start":
            mode = self._current_vacuum_cleaning_mode()
            mode_calls = self._vacuum_cleaning_mode_calls(entity_id, mode)
            message = f"Sent {VACUUM_CLEANING_MODES[mode].lower()} start to {entity_id}."
        else:
            mode_calls = []
            message = f"Sent {service.replace('/', '.')} to {entity_id}."
        self._run_ha_service_async(service, {"entity_id": entity_id}, message, pre_calls=mode_calls)

    def on_vacuum_set_fan_speed(self, event):
        entity_id = self._selected_vacuum_entity_id()
        speed = self.vacuum_fan_choice.GetStringSelection()
        if not entity_id or not speed:
            self.notify("Choose a vacuum and suction speed first.", priority=10)
            return
        self._run_ha_service_async("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": speed}, f"Set suction speed to {speed}.")

    def on_vacuum_set_select(self, event, entity_id):
        choice = self.vacuum_control_widgets.get(entity_id)
        option = choice.GetStringSelection() if choice else ""
        if not option:
            self.notify("Choose a setting value first.", priority=10)
            return
        self._run_ha_service_async(
            "select/select_option",
            {"entity_id": entity_id, "option": option},
            f"Set {entity_id} to {option}.",
            timeout=30,
            restore_focus_entity_id=entity_id,
        )

    def on_vacuum_set_number(self, event, entity_id):
        spin = self.vacuum_control_widgets.get(entity_id)
        value = spin.GetValue() if spin else None
        if value is None:
            self.notify("Enter a number first.", priority=10)
            return
        self._run_ha_service_async(
            "number/set_value",
            {"entity_id": entity_id, "value": value},
            f"Set {entity_id} to {value}.",
            timeout=30,
            restore_focus_entity_id=entity_id,
        )

    def on_vacuum_switch(self, event, entity_id, turn_on):
        service = "switch/turn_on" if turn_on else "switch/turn_off"
        label = "on" if turn_on else "off"
        self._run_ha_service_async(service, {"entity_id": entity_id}, f"Turned {label} {entity_id}.", restore_focus_entity_id=entity_id)

    def on_vacuum_press_button(self, event, entity_id):
        self._run_ha_service_async("button/press", {"entity_id": entity_id}, f"Pressed {entity_id}.")

    def on_refresh_vacuum_rooms(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        self.vacuum_room_status_txt.SetValue("Loading Roborock rooms from Home Assistant...")
        self._safe_submit(self._run_vacuum_room_refresh, entity_id)

    def _run_vacuum_room_refresh(self, entity_id):
        result = self._call_ha_service_response("roborock/get_maps", {"entity_id": entity_id})
        if not result.get("ok"):
            message = result.get("message") or result.get("error") or "Room discovery failed."
            wx.CallAfter(self._finish_vacuum_room_refresh, self._get_saved_vacuum_rooms(entity_id), f"Room discovery failed: {message}", False)
            return
        rooms = self._parse_roborock_rooms(result.get("data"), entity_id)
        if not rooms:
            wx.CallAfter(
                self._finish_vacuum_room_refresh,
                self._get_saved_vacuum_rooms(entity_id),
                "No rooms came back from Roborock maps. Open Home Assistant Developer Tools and confirm roborock.get_maps returns rooms for this vacuum.",
                False,
            )
            return
        wx.CallAfter(self._finish_vacuum_room_refresh, rooms, f"Loaded and saved {len(rooms)} Roborock room{'s' if len(rooms) != 1 else ''}.", True)

    def _parse_roborock_rooms(self, data, entity_id):
        service_response = data.get("service_response") if isinstance(data, dict) else None
        if not isinstance(service_response, dict):
            return []
        vacuum_payload = service_response.get(entity_id) or next(iter(service_response.values()), {})
        maps = vacuum_payload.get("maps") if isinstance(vacuum_payload, dict) else []
        rooms = []
        for map_info in maps if isinstance(maps, list) else []:
            map_name = str(map_info.get("name") or "Current map")
            room_map = map_info.get("rooms") if isinstance(map_info.get("rooms"), dict) else {}
            for room_id, room_name in room_map.items():
                label = f"{room_name} ({room_id})" if map_name == "Current map" else f"{room_name} on {map_name} ({room_id})"
                try:
                    segment_id = int(room_id)
                except (TypeError, ValueError):
                    continue
                rooms.append({"label": label, "name": str(room_name), "map": map_name, "segment": segment_id})
        return sorted(rooms, key=lambda room: room["label"].lower())

    def _finish_vacuum_room_refresh(self, rooms, message, save=False):
        self.vacuum_rooms = rooms
        if hasattr(self, "vacuum_room_sizer"):
            self._clear_sizer(self.vacuum_room_sizer)
        self.vacuum_room_checks = []
        for room in rooms:
            label = self._room_checkbox_label(room, False)
            check = wx.CheckBox(self.vacuum_room_scroll, label=label)
            check._viper_room = room
            check.SetName(label)
            check.SetToolTip(label)
            check.Bind(wx.EVT_CHECKBOX, self._on_vacuum_room_checkbox)
            self.vacuum_room_sizer.Add(check, 0, wx.ALL | wx.EXPAND, 4)
            self.vacuum_room_checks.append(check)
        if not rooms and hasattr(self, "vacuum_room_sizer"):
            self.vacuum_room_sizer.Add(
                wx.StaticText(self.vacuum_room_scroll, label="No rooms loaded yet. Press Refresh room list."),
                0,
                wx.ALL | wx.EXPAND,
                4,
            )
        status_lines = [message]
        if rooms:
            status_lines.append("Room checkboxes loaded. Tab through the room checkboxes; JAWS should read each room name, room ID, and checked state.")
        self.vacuum_room_status_txt.SetValue("\n".join(status_lines))
        if save:
            self._save_vacuum_rooms(self._selected_vacuum_entity_id(), rooms)
        if hasattr(self, "vacuum_room_scroll"):
            self.vacuum_room_scroll.Layout()
            self.vacuum_room_scroll.FitInside()
        self.tab_vacuum.Layout()
        self.tab_vacuum.FitInside()

    def _sanitize_vacuum_rooms(self, rooms):
        cleaned = []
        for room in rooms if isinstance(rooms, list) else []:
            if not isinstance(room, dict):
                continue
            try:
                segment = int(room.get("segment"))
            except (TypeError, ValueError):
                continue
            name = str(room.get("name") or f"Room {segment}")
            map_name = str(room.get("map") or "Current map")
            label = str(room.get("label") or (f"{name} ({segment})" if map_name == "Current map" else f"{name} on {map_name} ({segment})"))
            cleaned.append({"label": label, "name": name, "map": map_name, "segment": segment})
        return sorted(cleaned, key=lambda room: room["label"].lower())

    def _get_saved_vacuum_rooms(self, entity_id):
        if not entity_id:
            return []
        return self._sanitize_vacuum_rooms(self.config.get("vacuum_rooms", {}).get(entity_id, []))

    def _save_vacuum_rooms(self, entity_id, rooms):
        if not entity_id:
            return
        sanitized = self._sanitize_vacuum_rooms(rooms)
        self.config.setdefault("vacuum_rooms", {})[entity_id] = sanitized
        self.save_config()

    def on_vacuum_clean_selected_rooms(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        selected_rooms = [
            getattr(check, "_viper_room", {})
            for check in getattr(self, "vacuum_room_checks", [])
            if check.GetValue()
        ]
        if not selected_rooms:
            self.notify("Check one or more rooms first.", priority=10)
            return
        segments = [room["segment"] for room in selected_rooms if "segment" in room]
        repeat = self.vacuum_room_repeat.GetValue()
        mode = self._current_vacuum_cleaning_mode()
        mode_calls = self._vacuum_cleaning_mode_calls(entity_id, mode)
        payload = {
            "entity_id": entity_id,
            "command": "app_segment_clean",
            "params": [{"segments": segments, "repeat": repeat}],
        }
        self._run_ha_service_async(
            "vacuum/send_command",
            payload,
            f"Sent {VACUUM_CLEANING_MODES[mode].lower()} room clean request for {len(segments)} room{'s' if len(segments) != 1 else ''}.",
            pre_calls=mode_calls,
        )

    def on_vacuum_send_command(self, event):
        entity_id = self._selected_vacuum_entity_id()
        command = self.vacuum_command_txt.GetValue().strip()
        params_text = self.vacuum_params_txt.GetValue().strip()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        if not command:
            self.notify("Enter a command name first.", priority=10)
            return
        payload = {"entity_id": entity_id, "command": command}
        if params_text:
            try:
                payload["params"] = json.loads(params_text)
            except json.JSONDecodeError as e:
                self.notify(f"Vacuum command parameters are not valid JSON: {e}", priority=10)
                return
        self._run_ha_service_async("vacuum/send_command", payload, f"Sent vacuum command {command}.")

    def on_vacuum_clean_areas(self, event):
        entity_id = self._selected_vacuum_entity_id()
        area_ids = [item.strip() for item in self.vacuum_area_ids_txt.GetValue().split(",") if item.strip()]
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        if not area_ids:
            self.notify("Enter one or more Home Assistant area IDs first.", priority=10)
            return
        self._run_ha_service_async(
            "vacuum/clean_area",
            {"entity_id": entity_id, "cleaning_area_id": area_ids},
            f"Sent clean area request for {len(area_ids)} area{'s' if len(area_ids) != 1 else ''}.",
        )

    def on_vacuum_goto_position(self, event):
        entity_id = self._selected_vacuum_entity_id()
        if not entity_id:
            self.notify("Choose a vacuum first.", priority=10)
            return
        try:
            x = int(self.vacuum_goto_x_txt.GetValue().strip())
            y = int(self.vacuum_goto_y_txt.GetValue().strip())
        except ValueError:
            self.notify("Roborock go to coordinates must be whole numbers.", priority=10)
            return
        self._run_ha_service_async(
            "roborock/set_vacuum_goto_position",
            {"entity_id": entity_id, "x": x, "y": y},
            f"Sent Roborock go to position {x}, {y}.",
        )

    def _run_ha_service_async(self, service, payload, success_message, *, timeout=10, restore_focus_entity_id="", pre_calls=None):
        def worker():
            for pre_service, pre_payload in pre_calls or []:
                self._call_ha_service_data(pre_service, pre_payload, timeout=30)
            ok = self._call_ha_service_data(service, payload, timeout=timeout)
            if ok:
                if restore_focus_entity_id:
                    self._pending_vacuum_focus_entity_id = restore_focus_entity_id
                wx.CallAfter(lambda: self.notify(success_message, priority=10))
                wx.CallAfter(lambda: wx.CallLater(1200, self.on_refresh_vacuum, None))
        self._safe_submit(worker)

