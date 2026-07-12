import logging
import time
from datetime import datetime

import requests
import wx

import viper_audio as audio
import viper_config as cfg
import viper_ha_listener as ha_listener
import viper_ui_common as ui_common
import viper_vision as vision
from viper_runtime import safe_submit


AccessibleStatusText = ui_common.AccessibleStatusText


class DoorbellTabMixin:
    def setup_doorbell_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = AccessibleStatusText(
            self.tab_doorbell,
            value=(
                "Doorbell Vision is where front and back door monitoring comes together.\n\n"
                "Use the setup button to choose Home Assistant trigger entities, Ring-MQTT RTSP streams, and camera tests. "
                "Use the full-flow test buttons to verify chime, live video capture, AI vision, and speech."
            ),
            size=(-1, 120),
        )
        self._describe_control(
            intro,
            "Doorbell Vision introduction. Overview of front and back door monitoring setup and tests.",
        )
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        doorbell_box = wx.StaticBox(self.tab_doorbell, label="Doorbell Setup And Tests")
        doorbell_sizer = wx.StaticBoxSizer(doorbell_box, wx.VERTICAL)
        self.btn_doorbell_setup = wx.Button(self.tab_doorbell, label="Set Up Doorbell Triggers And Cameras", size=(-1, 44))
        self.btn_doorbell_test_front_flow = wx.Button(self.tab_doorbell, label="Test Front Doorbell Full Flow", size=(-1, 44))
        self.btn_doorbell_test_back_flow = wx.Button(self.tab_doorbell, label="Test Back Doorbell Full Flow", size=(-1, 44))
        self.btn_doorbell_setup.Bind(wx.EVT_BUTTON, self.on_open_setup_wizard)
        self.btn_doorbell_test_front_flow.Bind(wx.EVT_BUTTON, lambda event: self.on_test_doorbell_full_flow(event, "front"))
        self.btn_doorbell_test_back_flow.Bind(wx.EVT_BUTTON, lambda event: self.on_test_doorbell_full_flow(event, "back"))
        descriptions = {
            self.btn_doorbell_setup: "Set Up Doorbell Triggers And Cameras button. Opens the guided setup wizard for trigger entities and live RTSP streams.",
            self.btn_doorbell_test_front_flow: "Test Front Doorbell Full Flow button. Runs the complete front doorbell path through Home Assistant, RTSP capture, AI vision, and speech.",
            self.btn_doorbell_test_back_flow: "Test Back Doorbell Full Flow button. Runs the complete back doorbell path through Home Assistant, RTSP capture, AI vision, and speech.",
        }
        for button, description in descriptions.items():
            self._describe_control(button, description)
            doorbell_sizer.Add(button, 0, wx.ALL | wx.EXPAND, 6)
        sizer.Add(doorbell_sizer, 0, wx.ALL | wx.EXPAND, 10)

        video_box = wx.StaticBox(self.tab_doorbell, label="Doorbell Video Analysis")
        video_sizer = wx.StaticBoxSizer(video_box, wx.VERTICAL)
        video_intro = AccessibleStatusText(
            self.tab_doorbell,
            value=(
                "Choose how much video Viper sends to Gemini after a doorbell event.\n"
                "Fast mode is still image only. Smart mode speaks the fast still image first, then sends a short video only when the first answer is unclear or missing useful detail. "
                "Detailed mode sends a short video after every alert. Manual mode only sends video when you press an analyze button."
            ),
            size=(-1, 105),
        )
        self._describe_control(
            video_intro,
            "Doorbell video analysis explanation. Description of Fast, Smart, Detailed, and Manual modes.",
        )
        video_sizer.Add(video_intro, 0, wx.ALL | wx.EXPAND, 5)

        settings = vision.normalize_video_analysis_settings(self.config)
        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        mode_lbl = wx.StaticText(self.tab_doorbell, label="Video analysis mode:")
        self.video_analysis_mode_choice = wx.Choice(
            self.tab_doorbell,
            choices=[vision.VIDEO_ANALYSIS_LABELS[key] for key in vision.VIDEO_ANALYSIS_MODES],
        )
        self.video_analysis_mode_choice.SetSelection(list(vision.VIDEO_ANALYSIS_MODES).index(settings["mode"]))
        self.video_analysis_mode_choice.Bind(wx.EVT_CHOICE, self.on_save_video_analysis_settings)
        mode_row.Add(mode_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        mode_row.Add(self.video_analysis_mode_choice, 1, wx.ALL | wx.EXPAND, 5)
        video_sizer.Add(mode_row, 0, wx.EXPAND)

        seconds_row = wx.BoxSizer(wx.HORIZONTAL)
        seconds_lbl = wx.StaticText(self.tab_doorbell, label="Manual video length in seconds:")
        self.manual_video_seconds_spin = wx.SpinCtrl(
            self.tab_doorbell,
            min=2,
            max=settings["max_manual_clip_seconds"],
            initial=settings["manual_clip_seconds"],
        )
        self.manual_video_seconds_spin.Bind(wx.EVT_SPINCTRL, self.on_save_video_analysis_settings)
        self.manual_video_seconds_spin.Bind(wx.EVT_TEXT, self.on_save_video_analysis_settings)
        seconds_row.Add(seconds_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        seconds_row.Add(self.manual_video_seconds_spin, 0, wx.ALL, 5)
        video_sizer.Add(seconds_row, 0, wx.EXPAND)

        self.video_analysis_status_txt = AccessibleStatusText(
            self.tab_doorbell,
            value=self._video_analysis_summary_text(),
            size=(-1, 100),
        )
        video_sizer.Add(self.video_analysis_status_txt, 0, wx.ALL | wx.EXPAND, 5)

        video_buttons = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=6)
        video_buttons.AddGrowableCol(0, 1)
        video_buttons.AddGrowableCol(1, 1)
        self.btn_analyze_front_video = wx.Button(self.tab_doorbell, label="Analyze Front Camera Video Now", size=(-1, 44))
        self.btn_analyze_back_video = wx.Button(self.tab_doorbell, label="Analyze Back Camera Video Now", size=(-1, 44))
        self.btn_analyze_front_video.Bind(wx.EVT_BUTTON, lambda event: self.on_analyze_doorbell_video(event, "front"))
        self.btn_analyze_back_video.Bind(wx.EVT_BUTTON, lambda event: self.on_analyze_doorbell_video(event, "back"))
        for control, description in {
            self.video_analysis_mode_choice: "Video analysis mode picker. Fast is still image only. Smart sends bounded video only when the first answer is unclear. Detailed sends video every alert. Manual sends video only when you press an analyze button.",
            self.manual_video_seconds_spin: "Manual video length in seconds. Controls how much live camera video Viper uploads when you press Analyze Camera Video Now.",
            self.video_analysis_status_txt: "Doorbell video analysis status. Latest mode and latest video result.",
            self.btn_analyze_front_video: "Analyze Front Camera Video Now button. Captures the front camera for the manual video length, sends it to Gemini, and speaks what is happening outside.",
            self.btn_analyze_back_video: "Analyze Back Camera Video Now button. Captures the back camera for the manual video length, sends it to Gemini, and speaks what is happening outside.",
        }.items():
            self._describe_control(control, description)
        video_buttons.Add(self.btn_analyze_front_video, 1, wx.ALL | wx.EXPAND, 5)
        video_buttons.Add(self.btn_analyze_back_video, 1, wx.ALL | wx.EXPAND, 5)
        video_sizer.Add(video_buttons, 0, wx.EXPAND)
        sizer.Add(video_sizer, 0, wx.ALL | wx.EXPAND, 10)

        chime_box = wx.StaticBox(self.tab_doorbell, label="Instant Doorbell Chimes")
        chime_sizer = wx.StaticBoxSizer(chime_box, wx.VERTICAL)
        front_sizer = wx.BoxSizer(wx.HORIZONTAL)
        front_lbl = wx.StaticText(self.tab_doorbell, label="Front door chime:")
        self.front_chime_choice = wx.Choice(self.tab_doorbell)
        self.btn_test_front = wx.Button(self.tab_doorbell, label="Test Front Door Chime")
        self.btn_test_front.Bind(wx.EVT_BUTTON, self.on_test_front)
        front_sizer.Add(front_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        front_sizer.Add(self.front_chime_choice, 1, wx.ALL, 5)
        front_sizer.Add(self.btn_test_front, 0, wx.ALL, 5)
        back_sizer = wx.BoxSizer(wx.HORIZONTAL)
        back_lbl = wx.StaticText(self.tab_doorbell, label="Back door chime:")
        self.back_chime_choice = wx.Choice(self.tab_doorbell)
        self.btn_test_back = wx.Button(self.tab_doorbell, label="Test Back Door Chime")
        self.btn_test_back.Bind(wx.EVT_BUTTON, self.on_test_back)
        back_sizer.Add(back_lbl, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        back_sizer.Add(self.back_chime_choice, 1, wx.ALL, 5)
        back_sizer.Add(self.btn_test_back, 0, wx.ALL, 5)
        chime_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_chimes = wx.Button(self.tab_doorbell, label="Refresh Chime Folder")
        self.btn_save_chimes = wx.Button(self.tab_doorbell, label="Save Doorbell Chimes")
        self.btn_refresh_chimes.Bind(wx.EVT_BUTTON, self.on_refresh_chimes)
        self.btn_save_chimes.Bind(wx.EVT_BUTTON, self.on_save_chimes)
        chime_btn_sizer.Add(self.btn_refresh_chimes, 1, wx.ALL, 5)
        chime_btn_sizer.Add(self.btn_save_chimes, 1, wx.ALL, 5)
        for control, description in {
            self.front_chime_choice: "Front door chime picker. Choose the instant chime Viper plays for the front door.",
            self.back_chime_choice: "Back door chime picker. Choose the instant chime Viper plays for the back door.",
            self.btn_test_front: "Test Front Door Chime button. Plays the selected front door chime.",
            self.btn_test_back: "Test Back Door Chime button. Plays the selected back door chime.",
            self.btn_refresh_chimes: "Refresh Chime Folder button. Reloads available chime files from the chimes folder.",
            self.btn_save_chimes: "Save Doorbell Chimes button. Saves front and back door chime choices.",
        }.items():
            self._describe_control(control, description)
        chime_sizer.Add(front_sizer, 0, wx.EXPAND)
        chime_sizer.Add(back_sizer, 0, wx.EXPAND)
        chime_sizer.Add(chime_btn_sizer, 0, wx.EXPAND)
        sizer.Add(chime_sizer, 0, wx.ALL | wx.EXPAND, 10)
        self._populate_chimes()

        self.doorbell_summary_txt = AccessibleStatusText(
            self.tab_doorbell,
            value=self._doorbell_summary_text(),
            size=(-1, 220),
        )
        self._describe_control(
            self.doorbell_summary_txt,
            "Doorbell Vision status. Status of front and back trigger entities and RTSP URLs.",
        )
        sizer.Add(self.doorbell_summary_txt, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        self.tab_doorbell.SetSizer(sizer)

    def _doorbell_summary_text(self):
        settings = cfg.get_doorbell_settings(self.config, include_env=True)
        front_rtsp = settings.get("configured_rtsp_front") or settings.get("raw_rtsp_front") or ""
        back_rtsp = settings.get("configured_rtsp_back") or settings.get("raw_rtsp_back") or ""
        return "\n".join(
            [
                "Doorbell Vision Status",
                "",
                f"Front trigger entity: {settings.get('front_trigger_entity_id') or 'not set'}",
                f"Front RTSP URL: {front_rtsp or 'not set'}",
                f"Back trigger entity: {settings.get('back_trigger_entity_id') or 'not set'}",
                f"Back RTSP URL: {back_rtsp or 'not set'}",
                "",
                "Next best action:",
                "Use Set Up Doorbell Triggers And Cameras if anything above says not set. Then run both full-flow tests.",
            ]
        )

    def _doorbell_rtsp_for_key(self, key):
        settings = cfg.get_doorbell_settings(self.config, include_env=True)
        if key == "back":
            return settings.get("rtsp_back") or ""
        return settings.get("rtsp_front") or ""

    def _video_prompt_name_for_mode(self, mode):
        defaults = cfg.get_default_config().get("doorbell_video_prompt_profiles", {})
        prompts = self.config.setdefault("video_prompts", {})
        profiles = self.config.setdefault("doorbell_video_prompt_profiles", {})
        selected = profiles.get(mode) or defaults.get(mode) or self.config.get("active_video_prompt") or next(iter(prompts), "")
        if selected not in prompts:
            selected = next(iter(prompts), "")
            profiles[mode] = selected
        return selected

    def _video_prompt_text_for_mode(self, mode):
        name = self._video_prompt_name_for_mode(mode)
        return self.config.get("video_prompts", {}).get(name, "")

    def _video_analysis_summary_text(self):
        settings = vision.normalize_video_analysis_settings(self.config)
        mode_names = {
            "fast": "Fast",
            "smart": "Smart",
            "detailed": "Detailed",
            "manual": "Manual",
        }
        mode_descriptions = {
            "fast": "Still image only. Viper does not automatically upload video.",
            "smart": "Viper speaks the still image first, then uploads a 3 second video only if the answer is unclear or missing useful detail.",
            "detailed": "Viper speaks the still image first, then uploads a 5 second video after every doorbell alert.",
            "manual": "Viper uploads video only when you press an Analyze Camera Video Now button.",
        }
        mode = settings["mode"]
        last_lines = []
        for side in ("front", "back"):
            entry = self.last_video_analysis.get(side, {}) if hasattr(self, "last_video_analysis") else {}
            if entry:
                result_text = entry.get("description", "")
                if entry.get("incomplete"):
                    result_text = f"Gemini returned an incomplete answer: {result_text}"
                last_lines.append(
                    f"Last {side} video from {entry.get('source', 'unknown')}: {result_text} "
                    f"It took {entry.get('elapsed', 0):.1f} seconds."
                )
        if not last_lines:
            last_lines.append("No video analysis has run yet.")
        smart_line = "Smart rules are inactive right now."
        if mode == "smart":
            smart_line = "Smart rules active: 3 second clip, 2 frames per second, at most one video follow-up per camera per minute."
        decision_lines = []
        decisions = self.last_video_followup_decision if hasattr(self, "last_video_followup_decision") else {}
        reason_labels = {
            "strong_still_description": "still image was clear",
            "weak_description": "still image was still too weak",
            "service_unavailable": "AI service was unavailable",
            "motion_uncertain": "motion was unclear",
            "security_relevant_uncertain": "person, package, vehicle, or animal detail was uncertain",
            "visibility_issue": "visibility was poor",
            "detailed_mode": "Detailed mode always follows up",
            "fast_mode": "Fast mode skips automatic video",
            "manual_mode": "Manual mode only runs from the analyze button",
            "cooldown": "camera was inside the Smart cooldown window",
            "unknown_mode": "unknown video mode",
        }
        for side in ("front", "back"):
            decision = decisions.get(side, {})
            if decision:
                status = "video follow-up" if decision.get("run") else "skipped"
                reason = reason_labels.get(decision.get("reason"), decision.get("reason", "unknown"))
                markers = decision.get("markers") or []
                marker_text = f" Markers: {', '.join(markers)}." if markers else ""
                decision_lines.append(f"Last {side} Smart decision: {status}, {reason}.{marker_text}")
        if not decision_lines:
            decision_lines.append("No Smart video decision has run yet.")
        return "\n".join(
            [
                f"Mode: {mode_names.get(mode, mode.title())}.",
                f"What this mode does: {mode_descriptions.get(mode, '')}",
                smart_line,
                *decision_lines,
                f"Manual Analyze Camera Video Now buttons upload {settings['manual_clip_seconds']} seconds.",
                "",
                *last_lines,
            ]
        )

    def _refresh_video_analysis_controls(self):
        if not hasattr(self, "video_analysis_mode_choice"):
            return
        settings = vision.normalize_video_analysis_settings(self.config)
        try:
            self.video_analysis_mode_choice.SetSelection(list(vision.VIDEO_ANALYSIS_MODES).index(settings["mode"]))
            self.manual_video_seconds_spin.SetRange(2, settings["max_manual_clip_seconds"])
            self.manual_video_seconds_spin.SetValue(settings["manual_clip_seconds"])
            self.video_analysis_status_txt.SetValue(self._video_analysis_summary_text())
        except Exception:
            logging.debug("Could not refresh video analysis controls.", exc_info=True)

    def on_save_video_analysis_settings(self, event):
        if not hasattr(self, "video_analysis_mode_choice"):
            if event:
                event.Skip()
            return
        current = vision.normalize_video_analysis_settings(self.config)
        selection = self.video_analysis_mode_choice.GetSelection()
        mode = vision.VIDEO_ANALYSIS_MODES[selection] if 0 <= selection < len(vision.VIDEO_ANALYSIS_MODES) else current["mode"]
        manual_seconds = vision.clamp_manual_video_seconds(self.manual_video_seconds_spin.GetValue(), self.config)
        settings = dict(current)
        settings["mode"] = mode
        settings["manual_clip_seconds"] = manual_seconds
        self.config["doorbell_video_analysis"] = settings
        self.save_config()
        self.video_analysis_status_txt.SetValue(self._video_analysis_summary_text())
        self.notify(
            f"Doorbell video mode saved. {vision.VIDEO_ANALYSIS_LABELS.get(mode, mode)}. Manual video is {manual_seconds} seconds.",
            priority=10,
        )
        if event:
            event.Skip()

    def on_analyze_doorbell_video(self, event, side):
        settings = vision.normalize_video_analysis_settings(self.config)
        seconds = vision.clamp_manual_video_seconds(
            self.manual_video_seconds_spin.GetValue() if hasattr(self, "manual_video_seconds_spin") else settings["manual_clip_seconds"],
            self.config,
        )
        label = "back" if side == "back" else "front"
        self.notify(f"Analyzing {label} camera video for {seconds} seconds.", priority=10)
        safe_submit(self._run_manual_doorbell_video_analysis, label, seconds, "desktop app")

    def _run_manual_doorbell_video_analysis(self, side, seconds=None, source="desktop app"):
        side = "back" if side == "back" else "front"
        rtsp_url = self._doorbell_rtsp_for_key(side)
        if not rtsp_url:
            message = f"No RTSP URL is configured for the {side} camera."
            wx.CallAfter(self.notify, message, 10)
            self.record_video_analysis_result(side, message, {"ok": False, "elapsed": 0.0}, source=source)
            return
        seconds = vision.clamp_manual_video_seconds(seconds, self.config)
        prompt = cfg.get_doorbell_video_prompt(
            self.config,
            "manual",
            location=f"{side} door",
            side=side,
        )
        logging.info("[VIDEO ANALYSIS] manual_start side=%s seconds=%s source=%s", side, seconds, source)
        result = vision.analyze_rtsp_video(
            rtsp_url,
            side=side,
            seconds=seconds,
            prompt=prompt,
            config_data=self.config,
            trace_id=f"manual-{side}-{int(time.time() * 1000)}",
        )
        description = result.get("description") or "Video analysis did not return a description."
        self.record_video_analysis_result(side, description, result, source=source)
        wx.CallAfter(self.notify, f"{side.title()} camera video: {description}", 1, True)
        audio.play_notification("doorbell", f"{side.title()} camera video: {description}")

    def record_video_followup_decision(self, side, decision, mode="smart"):
        if not hasattr(self, "last_video_followup_decision"):
            self.last_video_followup_decision = {}
        label = "back" if side == "back" else "front"
        entry = {
            "side": label,
            "mode": mode,
            "run": bool(getattr(decision, "run", False)),
            "reason": getattr(decision, "reason", "unknown"),
            "markers": list(getattr(decision, "markers", ()) or ()),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        self.last_video_followup_decision[label] = entry
        logging.info(
            "[VIDEO ANALYSIS] decision side=%s mode=%s run=%s reason=%s markers=%s",
            label, mode, entry["run"], entry["reason"], ",".join(entry["markers"]) or "none",
        )
        if hasattr(self, "video_analysis_status_txt"):
            wx.CallAfter(self.video_analysis_status_txt.SetValue, self._video_analysis_summary_text())

    def record_video_analysis_result(self, side, description, result=None, source="unknown"):
        if not hasattr(self, "last_video_analysis"):
            self.last_video_analysis = {}
        result = result or {}
        entry = {
            "side": "back" if side == "back" else "front",
            "description": description,
            "source": source,
            "elapsed": float(result.get("elapsed") or 0.0),
            "ok": bool(result.get("ok", True)),
            "incomplete": vision._looks_like_cut_off_video_response(description),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        self.last_video_analysis[entry["side"]] = entry
        logging.info("[VIDEO ANALYSIS] result side=%s source=%s ok=%s elapsed=%.2fs text=%r", entry["side"], source, entry["ok"], entry["elapsed"], description)
        if hasattr(self, "video_analysis_status_txt"):
            wx.CallAfter(self.video_analysis_status_txt.SetValue, self._video_analysis_summary_text())

    def _populate_chimes(self):
        chime_files = ["(Default)"]
        if cfg.CHIMES_DIR.exists():
            for f in cfg.CHIMES_DIR.iterdir():
                if f.suffix.lower() in [".mp3", ".wav"]:
                    chime_files.append(f.name)
        current_front = self.config.get("front_chime", "")
        current_back = self.config.get("back_chime", "")
        self.front_chime_choice.Set(chime_files)
        self.back_chime_choice.Set(chime_files)
        if current_front in chime_files:
            self.front_chime_choice.SetStringSelection(current_front)
        else:
            self.front_chime_choice.SetStringSelection("(Default)")
        if current_back in chime_files:
            self.back_chime_choice.SetStringSelection(current_back)
        else:
            self.back_chime_choice.SetStringSelection("(Default)")

    def on_test_front(self, event):
        f_val = self.front_chime_choice.GetStringSelection()
        safe_submit(audio.test_specific_chime, f_val, "front")
        self.notify("Testing front chime.", priority=10)

    def on_test_back(self, event):
        b_val = self.back_chime_choice.GetStringSelection()
        safe_submit(audio.test_specific_chime, b_val, "back")
        self.notify("Testing back chime.", priority=10)

    def on_refresh_chimes(self, event):
        self._populate_chimes()
        self.notify("Chimes folder refreshed.", priority=10)

    def on_save_chimes(self, event):
        f_val = self.front_chime_choice.GetStringSelection()
        b_val = self.back_chime_choice.GetStringSelection()
        self.config["front_chime"] = "" if f_val == "(Default)" else f_val
        self.config["back_chime"] = "" if b_val == "(Default)" else b_val
        self.save_config()
        self.notify("Custom chimes saved.", priority=10)

    def on_test_doorbell_full_flow(self, event, side: str):
        side = "back" if side == "back" else "front"
        label = "back" if side == "back" else "front"
        self.notify(f"Starting {label} doorbell full flow test through Home Assistant.", priority=10)
        safe_submit(self._run_doorbell_full_flow_test, side)

    def _run_doorbell_full_flow_test(self, side: str):
        side = "back" if side == "back" else "front"
        label = "Back" if side == "back" else "Front"
        try:
            triggers = ha_listener.normalize_doorbell_triggers(self.config)
            trigger = triggers.get(side, {})
            other_side = "front" if side == "back" else "back"
            other_trigger = triggers.get(other_side, {})
            entity_id = trigger.get("trigger_entity_id") or ""
            rtsp_url = trigger.get("rtsp_url") or self._doorbell_rtsp_for_key(side)
            listener_warning = ""
            if not self.config.get("ha_listener_enabled", True):
                listener_warning = (
                    "Home Assistant listener is disabled. Sending the test event anyway, then running the doorbell flow directly."
                )
            if hasattr(self, "ha_listener"):
                status = self.ha_listener.status()
                if not status.get("connected"):
                    error = status.get("last_error") or "not connected"
                    listener_warning = (
                        f"Home Assistant listener is not connected: {error}. Sending the test event anyway, then running the doorbell flow directly."
                    )
            if listener_warning:
                wx.CallAfter(self.notify, listener_warning, 10)
            if not trigger.get("enabled"):
                wx.CallAfter(self.notify, f"{label} doorbell trigger is not enabled. Save a trigger entity and RTSP URL in Home Assistant Setup first.", 10)
                return
            if not entity_id:
                wx.CallAfter(self.notify, f"{label} doorbell trigger entity is missing. Choose it in Home Assistant Setup first.", 10)
                return
            other_entity_id = other_trigger.get("trigger_entity_id") or ""
            if other_trigger.get("enabled") and other_entity_id and other_entity_id == entity_id:
                wx.CallAfter(
                    self.notify,
                    f"{label} doorbell full flow test was not sent because front and back use the same Home Assistant trigger entity. Open Home Assistant Setup and choose separate front and back trigger entities.",
                    10,
                )
                return
            if not rtsp_url:
                wx.CallAfter(self.notify, f"{label} doorbell RTSP URL is missing. Add and test the camera URL first.", 10)
                return

            ha_settings = cfg.get_ha_settings(self.config, include_env=True)
            token = ha_settings.get("ha_token")
            ha_ip = ha_settings.get("ha_ip")
            ha_port = ha_settings.get("ha_port") or "8123"
            if not ha_ip or not token:
                wx.CallAfter(self.notify, "Home Assistant host or token is missing. Open Home Assistant Setup first.", 10)
                return

            active_states = trigger.get("active_states") or ha_listener.DEFAULT_ACTIVE_STATES
            active_state = str(active_states[0] if active_states else "on")
            now = datetime.now().isoformat(timespec="seconds")
            payload = {
                "entity_id": entity_id,
                "old_state": {
                    "entity_id": entity_id,
                    "state": "off",
                    "attributes": {"friendly_name": f"Viper {label} Doorbell Test"},
                    "last_changed": now,
                    "last_updated": now,
                },
                "new_state": {
                    "entity_id": entity_id,
                    "state": active_state,
                    "attributes": {
                        "friendly_name": f"Viper {label} Doorbell Test",
                        "viper_test": True,
                    },
                    "last_changed": now,
                    "last_updated": now,
                },
            }
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            response = requests.post(
                f"http://{ha_ip}:{ha_port}/api/events/state_changed",
                headers=headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logging.info(
                "[HA SETUP] Fired synthetic %s doorbell state_changed event entity=%s active_state=%s rtsp_configured=%s",
                side,
                entity_id,
                active_state,
                bool(rtsp_url),
            )
            wx.CallAfter(
                self.notify,
                f"{label} doorbell test event accepted by Home Assistant. Running the full doorbell flow now.",
                10,
            )
            status_text, status_code = self._run_doorbell_pipeline(f"{side} door", rtsp_url, side)
            logging.info(
                "[HA SETUP] Direct %s doorbell full flow completed code=%s status=%s",
                side,
                status_code,
                status_text,
            )
        except Exception as e:
            logging.exception("[HA SETUP] Doorbell full flow test failed side=%s", side)
            wx.CallAfter(self.notify, f"{label} doorbell full flow test failed: {e}", 10)
