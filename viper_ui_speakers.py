import logging

import wx

import viper_audio as audio
import viper_discovery as discovery
import viper_speakers as speakers
from viper_runtime import safe_submit
from viper_ui_setup_wizard import DiscoveredSpeakersDialog


class SpeakerManagementMixin:
    def refresh_speaker_list(self):
        if not hasattr(self, "speaker_list"):
            self._pending_speaker_list_refresh = True
            return
        self.speaker_list.Clear()
        for name, data in self.config.get("speakers", {}).items():
            idx = self.speaker_list.Append(f"{name} ({data['type'].upper()})")
            self.speaker_list.Check(idx, data.get("enabled", True))
            self.speaker_list.SetClientData(idx, name)
        self._refresh_tts_target_choices()
        self._pending_speaker_list_refresh = False

    def on_speaker_select(self, event):
        idx = event.GetInt()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetString(idx)
            state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
            self._sync_speaker_routing_controls()
            wx.CallAfter(self._safe_speak, f"{name}, {state}")

    def on_speaker_focus(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetString(idx)
            state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
            self._sync_speaker_routing_controls()
            wx.CallAfter(self._safe_speak, f"Speaker Targets. {name}, {state}")

    def on_speaker_toggle(self, event):
        idx = event.GetInt()
        name = self.speaker_list.GetClientData(idx)
        is_chk = self.speaker_list.IsChecked(idx)
        self.config["speakers"][name]["enabled"] = is_chk
        self.save_config()
        self._sync_speaker_routing_controls()
        status_msg = f"{name} {'enabled' if is_chk else 'disabled'}"
        self.notify(status_msg, priority=10)
        spk_type = self.config["speakers"][name]["type"]
        spk_id = self.config["speakers"][name]["id"]
        safe_submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)

    def _sync_speaker_routing_controls(self):
        required = [
            "speaker_list",
            "chk_route_doorbell",
            "chk_route_utilities",
            "chk_route_fridge",
            "chk_route_qhexempt",
        ]
        if not all(hasattr(self, name) for name in required):
            self._pending_speaker_route_sync = True
            return
        idx = self.speaker_list.GetSelection()
        enabled = idx != wx.NOT_FOUND
        for chk in [self.chk_route_doorbell, self.chk_route_utilities, self.chk_route_fridge, self.chk_route_qhexempt]:
            chk.Enable(enabled)
        if not enabled:
            for chk in [self.chk_route_doorbell, self.chk_route_utilities, self.chk_route_fridge, self.chk_route_qhexempt]:
                chk.SetValue(False)
            self._pending_speaker_route_sync = False
            return
        name = self.speaker_list.GetClientData(idx)
        spk = self.config["speakers"].get(name, {})
        self.chk_route_doorbell.SetValue(spk.get("doorbell", True))
        self.chk_route_utilities.SetValue(spk.get("utilities", True))
        self.chk_route_fridge.SetValue(spk.get("fridge", True))
        self.chk_route_qhexempt.SetValue(spk.get("quiet_hours_exempt", False))
        self._pending_speaker_route_sync = False

    def on_speaker_route_change(self, event):
        idx = self.speaker_list.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        name = self.speaker_list.GetClientData(idx)
        spk = self.config["speakers"].setdefault(name, {})
        spk["doorbell"] = self.chk_route_doorbell.GetValue()
        spk["utilities"] = self.chk_route_utilities.GetValue()
        spk["fridge"] = self.chk_route_fridge.GetValue()
        spk["quiet_hours_exempt"] = self.chk_route_qhexempt.GetValue()
        self.save_config()
        self.notify(f"Saved routing for {name}", priority=10)

    def on_add_speaker(self, event):
        name = wx.GetTextFromUser("Speaker Name:", "Add")
        if name:
            dlg = wx.SingleChoiceDialog(self, "Type:", "Add", ["sonos", "ha", "alexa"])
            if dlg.ShowModal() == wx.ID_OK:
                spk_type = dlg.GetStringSelection()
                spk_id = wx.GetTextFromUser("ID/IP:", "Add")
                if spk_id:
                    self.config["speakers"][name] = {"id": spk_id, "type": spk_type, "enabled": True, "doorbell": True, "utilities": True, "fridge": True, "quiet_hours_exempt": False}
                    self.save_config()
                    self.refresh_speaker_list()
                    self._sync_speaker_routing_controls()
            dlg.Destroy()

    def on_rename_speaker(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            old = self.speaker_list.GetClientData(idx)
            new = wx.GetTextFromUser(f"Rename {old}:", "Rename", old)
            if new and new != old:
                d = self.config["speakers"].pop(old)
                self.config["speakers"][new] = d
                self.save_config()
                self.refresh_speaker_list()
                self._sync_speaker_routing_controls()

    def on_remove_speaker(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetClientData(idx)
            del self.config["speakers"][name]
            self.save_config()
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()

    def _configured_speaker_ids(self):
        return speakers.configured_speaker_ids(self.config)

    def _speaker_candidate_lines(self, candidates, title):
        return speakers.speaker_candidate_lines(candidates, title, self._configured_speaker_ids())

    def _flatten_discovered_speaker_targets(self, ha_candidates, sonos_candidates):
        return speakers.flatten_discovered_speaker_targets(ha_candidates, sonos_candidates, self._configured_speaker_ids())

    def _unique_speaker_name(self, base_name, spk_type):
        return speakers.unique_speaker_name(self.config, base_name, spk_type)

    def _add_discovered_speaker_targets(self, targets, routes=None):
        added = speakers.add_discovered_speaker_targets(self.config, targets, routes)
        if added:
            self.save_config()
            self.refresh_speaker_list()
            self._sync_speaker_routing_controls()
            self.refresh_setup_checklist()
        return added

    def _discovered_speaker_summary_text(self, ha_candidates, sonos_candidates, ha_error="", sonos_error=""):
        return speakers.discovered_speaker_summary_text(
            ha_candidates,
            sonos_candidates,
            configured_ids=self._configured_speaker_ids(),
            ha_error=ha_error,
            sonos_error=sonos_error,
        )

    def _ha_speaker_candidates_from_result(self, result):
        return speakers.ha_speaker_candidates_from_result(result)

    def _sonos_speaker_candidates_from_soco(self, discovered_speakers):
        return speakers.sonos_speaker_candidates_from_soco(discovered_speakers)

    def on_discover_speakers(self, event):
        self.notify("Discovering available speakers. Viper will let you choose which speakers to add.", priority=10)
        safe_submit(self._run_discover_speakers)

    def _run_discover_speakers(self):
        ha_result = discovery.discover_ha_entities(timeout=5)
        ha_candidates = []
        ha_error = ""
        if ha_result.get("ok"):
            ha_candidates = self._ha_speaker_candidates_from_result(ha_result)
        else:
            ha_error = ha_result.get("message") or "Home Assistant speaker discovery failed."

        sonos_candidates = []
        sonos_error = ""
        try:
            import soco
            sonos_candidates = self._sonos_speaker_candidates_from_soco(soco.discover())
        except Exception as e:
            sonos_error = f"Network Sonos discovery failed: {e}"

        wx.CallAfter(self._show_discovered_speakers, ha_candidates, sonos_candidates, ha_error, sonos_error)

    def _show_discovered_speakers(self, ha_candidates, sonos_candidates, ha_error="", sonos_error="", parent_window=None):
        summary_text = self._discovered_speaker_summary_text(ha_candidates, sonos_candidates, ha_error, sonos_error)
        self.notify(
            f"Speaker discovery complete. Found {len(ha_candidates)} Home Assistant speaker target(s) and {len(sonos_candidates)} network Sonos speaker(s).",
            priority=10,
        )
        targets = self._flatten_discovered_speaker_targets(ha_candidates, sonos_candidates)
        logging.info(
            "[SPEAKER DISCOVERY] ha_candidates=%d sonos_candidates=%d addable=%d ha_error=%r sonos_error=%r",
            len(ha_candidates or []),
            len(sonos_candidates or []),
            len([item for item in targets if not item.get("configured")]),
            ha_error,
            sonos_error,
        )
        try:
            window_ready = isinstance(self, wx.Window) and bool(self.GetHandle())
        except Exception:
            window_ready = False
        if not window_ready:
            self._show_text_dialog("Available Speakers", summary_text)
            return
        parent = parent_window if isinstance(parent_window, wx.Window) else self
        dlg = DiscoveredSpeakersDialog(parent, targets, summary_text)
        try:
            try:
                dlg.Raise()
                dlg.SetFocus()
            except Exception:
                pass
            if dlg.ShowModal() == wx.ID_OK:
                added = self._add_discovered_speaker_targets(dlg.selected_targets, getattr(dlg, "selected_routes", None))
                if added:
                    self.notify(f"Added {added} speaker target(s). They are enabled for doorbell, utility, and fridge/freezer alerts.", priority=10)
                else:
                    self.notify("No new speakers were selected or added.", priority=10)
        finally:
            dlg.Destroy()
