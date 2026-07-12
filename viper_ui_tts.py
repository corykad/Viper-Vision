import logging
import os
from datetime import datetime

import wx

import viper_audio as audio
from viper_runtime import safe_submit


EDGE_VOICES = {
    "Andrew (Natural Male)": "en-US-AndrewNeural",
    "Ava (Natural Female)": "en-US-AvaNeural",
    "Aria (Female)": "en-US-AriaNeural",
    "Guy (Male)": "en-US-GuyNeural",
    "Jenny (Female)": "en-US-JennyNeural",
    "Emma (Natural Female)": "en-US-EmmaNeural",
    "Brian (Natural Male)": "en-US-BrianNeural",
    "Sonia (UK Female)": "en-GB-SoniaNeural",
}

GEMINI_TTS_VOICES = {
    "Zephyr (Bright)": "Zephyr",
    "Puck (Upbeat)": "Puck",
    "Charon (Informative)": "Charon",
    "Kore (Firm)": "Kore",
    "Fenrir (Excitable)": "Fenrir",
    "Leda (Youthful)": "Leda",
    "Orus (Firm)": "Orus",
    "Aoede (Breezy)": "Aoede",
    "Callirrhoe (Easy-going)": "Callirrhoe",
    "Autonoe (Bright)": "Autonoe",
    "Enceladus (Breathy)": "Enceladus",
    "Iapetus (Clear)": "Iapetus",
    "Umbriel (Easy-going)": "Umbriel",
    "Algieba (Smooth)": "Algieba",
    "Despina (Smooth)": "Despina",
    "Erinome (Clear)": "Erinome",
    "Algenib (Gravelly)": "Algenib",
    "Rasalgethi (Informative)": "Rasalgethi",
    "Laomedeia (Upbeat)": "Laomedeia",
    "Achernar (Soft)": "Achernar",
    "Alnilam (Firm)": "Alnilam",
    "Schedar (Even)": "Schedar",
    "Gacrux (Mature)": "Gacrux",
    "Pulcherrima (Forward)": "Pulcherrima",
    "Achird (Friendly)": "Achird",
    "Zubenelgenubi (Casual)": "Zubenelgenubi",
    "Vindemiatrix (Gentle)": "Vindemiatrix",
    "Sadachbia (Lively)": "Sadachbia",
    "Sadaltager (Knowledgeable)": "Sadaltager",
    "Sulafat (Warm)": "Sulafat",
}

GEMINI_TTS_MODELS = [
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
]

VOICE_BEHAVIOR_MODES = {
    "Fast reliable voice, uses Microsoft Edge TTS": "fast_reliable",
    "Natural emotional voice, uses Gemini cloud TTS": "natural_gemini",
    "Regular Google TTS voice, uses Google Translate speech": "google_regular",
    "Offline fallback voice, uses Windows local speech": "offline_fallback",
}

VOICE_PERSONALITIES = {
    "Warm friendly voice": {"key": "warm", "voice": "Sulafat", "style": "[warm, clear, friendly]"},
    "Clear crisp voice": {"key": "clear", "voice": "Iapetus", "style": "[clear, crisp, decently fast]"},
    "Firm authoritative voice": {"key": "firm", "voice": "Kore", "style": "[firm, authoritative, clear]"},
    "Upbeat bright voice": {"key": "upbeat", "voice": "Puck", "style": "[upbeat, bright, clear]"},
}

VOICE_SPEEDS = {
    "Relaxed, slower than normal": "relaxed",
    "Normal conversational speed": "normal",
    "Brisk, slightly faster": "brisk",
    "Fast alert speed": "fast",
    "Very fast but still clear": "very_fast",
}

TTS_PROFILE_LABELS = {
    "doorbell": "Doorbell Alerts",
    "utilities": "Utilities",
    "manual": "Manual Broadcasts",
}

DIALECTS = {
    "American": "com",
    "British": "co.uk",
    "Australian": "com.au",
    "Indian": "co.in",
}


class TtsSettingsMixin:
    def setup_tts_config_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._sapi_voice_choices = []

        self.default_tts_controls = self._add_tts_settings_box(
            sizer,
            "Default TTS settings for all alerts",
            self.config.get("tts_defaults", {}),
            "default",
            include_use_default=False,
        )
        self.alert_tts_controls = {}
        labels = {
            "doorbell": "Doorbell alerts",
            "utilities": "Utility alerts",
            "manual": "Manual broadcasts",
        }
        for category, title in labels.items():
            controls = self._add_tts_settings_box(
                sizer,
                title,
                self.config.get("tts_alerts", {}).get(category, {}),
                category,
                include_use_default=True,
            )
            self.alert_tts_controls[category] = controls

        self.gemini_warm_status = wx.StaticText(self.tab_tts, label=self._format_gemini_warm_status())
        sizer.Add(self.gemini_warm_status, 0, wx.ALL | wx.EXPAND, 10)

        self.btn_save_voice_behavior = wx.Button(self.tab_tts, label="Save TTS settings")
        self.btn_save_voice_behavior.Bind(wx.EVT_BUTTON, self.on_save_voice_behavior)
        self._describe_control(
            self.btn_save_voice_behavior,
            "Save TTS settings button. Saves default TTS settings and any per-alert overrides.",
        )
        sizer.Add(self.btn_save_voice_behavior, 0, wx.ALL | wx.EXPAND, 10)

        self.tab_tts.SetSizer(sizer)

    def _add_tts_settings_box(self, parent_sizer, title, settings, category, include_use_default=False):
        box = wx.StaticBox(self.tab_tts, label=title)
        box_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        controls = {}

        if include_use_default:
            use_default = wx.CheckBox(self.tab_tts, label=f"{title}: use default TTS settings")
            use_default.SetValue(bool(settings.get("use_defaults", True)))
            self._describe_control(
                use_default,
                f"{title} use default TTS settings checkbox. When checked, {title.lower()} use the default engine, voice, speed, and mood settings. Uncheck to customize this alert type.",
            )
            box_sizer.Add(use_default, 0, wx.ALL, 5)
            controls["use_defaults"] = use_default

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=8)
        grid.AddGrowableCol(1, 1)

        engine_choice = wx.Choice(self.tab_tts, choices=list(VOICE_BEHAVIOR_MODES.keys()))
        engine_value = settings.get("engine", "gemini")
        engine_key = {"gemini": "natural_gemini", "edge": "fast_reliable", "google": "google_regular", "sapi": "offline_fallback"}.get(engine_value, "natural_gemini")
        engine_label = next((label for label, key in VOICE_BEHAVIOR_MODES.items() if key == engine_key), "Natural emotional voice, uses Gemini cloud TTS")
        engine_choice.SetStringSelection(engine_label)
        self._describe_control(engine_choice, f"{title} TTS engine. This chooses which speech engine is used for {title.lower()}.")

        gemini_voice = wx.Choice(self.tab_tts, choices=list(GEMINI_TTS_VOICES.keys()))
        self._set_voice_choice_from_value(gemini_voice, settings.get("gemini_voice", "Sulafat"))
        self._describe_control(gemini_voice, f"{title} Gemini voice. Used when this alert type uses Gemini cloud TTS.")

        edge_voice = wx.Choice(self.tab_tts, choices=list(EDGE_VOICES.keys()))
        edge_label = next((label for label, voice in EDGE_VOICES.items() if voice == settings.get("edge_voice", "en-US-AriaNeural")), "Aria (Female)")
        edge_voice.SetStringSelection(edge_label)
        self._describe_control(edge_voice, f"{title} Microsoft Edge TTS voice. Used when this alert type uses Edge TTS or when Gemini falls back to Edge.")

        google_tld = wx.Choice(self.tab_tts, choices=list(DIALECTS.keys()))
        google_label = next((label for label, tld in DIALECTS.items() if tld == settings.get("google_tld", "com")), "American")
        google_tld.SetStringSelection(google_label)
        self._describe_control(google_tld, f"{title} regular Google TTS accent. Used when this alert type uses regular Google TTS.")

        sapi_voice = wx.Choice(self.tab_tts, choices=self.voice_list)
        self._sapi_voice_choices.append(sapi_voice)
        sapi_idx = int(settings.get("sapi_voice_index", self.config.get("local_voice_index", 1)))
        if self.voice_list:
            sapi_voice.SetSelection(sapi_idx if sapi_idx < len(self.voice_list) else 0)
        self._describe_control(sapi_voice, f"{title} Windows offline voice. Used when this alert type uses Windows offline speech.")

        speed_choice = self._make_speed_choice(settings.get("speed", "normal"))
        self._describe_control(speed_choice, f"{title} speech speed. Controls how fast this alert type is spoken.")

        mood_chk = wx.CheckBox(self.tab_tts, label=f"{title}: use dynamic mood")
        mood_chk.SetValue(bool(settings.get("dynamic_mood", True)))
        self._describe_control(mood_chk, f"{title} dynamic mood checkbox. When checked, Viper detects urgent, excited, or warning wording and adjusts Gemini delivery.")

        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} TTS engine"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(engine_choice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} Gemini voice"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(gemini_voice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} Edge voice"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(edge_voice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} regular Google TTS accent"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(google_tld, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} Windows offline voice"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(sapi_voice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} speech speed"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(speed_choice, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self.tab_tts, label=f"{title} dynamic mood"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(mood_chk, 1, wx.EXPAND)

        controls.update({
            "engine": engine_choice,
            "gemini_voice": gemini_voice,
            "edge_voice": edge_voice,
            "google_tld": google_tld,
            "sapi_voice": sapi_voice,
            "speed": speed_choice,
            "dynamic_mood": mood_chk,
        })

        if category == "default":
            keep_warm = wx.CheckBox(self.tab_tts, label="Default: reduce Gemini first-alert delay with warmup")
            keep_warm.SetValue(bool(settings.get("keep_warm", False)))
            self._describe_control(keep_warm, "Default Gemini warmup checkbox. When checked, Viper sends a small Gemini request every four minutes. These requests may be billed.")
            min_interval = wx.SpinCtrl(self.tab_tts, min=0, max=10, initial=int(settings.get("gemini_min_interval_seconds", 0)))
            self._describe_control(min_interval, "Default Gemini minimum seconds between requests. Zero is fastest. Increase only if Gemini returns quota errors.")
            grid.Add(wx.StaticText(self.tab_tts, label="Default Gemini warmup"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(keep_warm, 1, wx.EXPAND)
            grid.Add(wx.StaticText(self.tab_tts, label="Default Gemini request spacing seconds"), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(min_interval, 0, wx.EXPAND)
            controls["keep_warm"] = keep_warm
            controls["gemini_min_interval"] = min_interval

        box_sizer.Add(grid, 0, wx.ALL | wx.EXPAND, 8)

        if category != "default":
            test_btn = wx.Button(self.tab_tts, label=f"Test {title}")
            test_btn.Bind(wx.EVT_BUTTON, lambda evt, c=category: self.on_test_voice_behavior(evt, c))
            self._describe_control(test_btn, f"Test {title} button. Saves current TTS settings and plays a sample for {title.lower()}.")
            box_sizer.Add(test_btn, 0, wx.ALL | wx.EXPAND, 5)
            controls["test"] = test_btn
            controls["use_defaults"].Bind(wx.EVT_CHECKBOX, lambda evt, c=controls: self._sync_tts_override_controls(c))
            engine_choice.Bind(wx.EVT_CHOICE, lambda evt, c=controls: self._sync_tts_voice_controls(c))
            self._sync_tts_override_controls(controls)
        else:
            engine_choice.Bind(wx.EVT_CHOICE, lambda evt, c=controls: self._sync_tts_voice_controls(c))
            self._sync_tts_voice_controls(controls)

        parent_sizer.Add(box_sizer, 0, wx.ALL | wx.EXPAND, 10)
        return controls

    def _sync_tts_override_controls(self, controls):
        enabled = not controls["use_defaults"].GetValue()
        controls["engine"].Enable(enabled)
        controls["speed"].Enable(enabled)
        controls["dynamic_mood"].Enable(enabled)
        self._sync_tts_voice_controls(controls, parent_enabled=enabled)

    def _sync_tts_voice_controls(self, controls, parent_enabled=True):
        engine = self._engine_value_from_choice(controls["engine"])
        if "use_defaults" in controls and controls["use_defaults"].GetValue():
            parent_enabled = False
        if parent_enabled and engine == "sapi":
            self._ensure_windows_voice_list()
        controls["gemini_voice"].Enable(parent_enabled and engine == "gemini")
        controls["edge_voice"].Enable(parent_enabled and engine == "edge")
        controls["google_tld"].Enable(parent_enabled and engine == "google")
        controls["sapi_voice"].Enable(parent_enabled and engine == "sapi")

    def _ensure_windows_voice_list(self):
        if self.voice_list:
            return self.voice_list
        self.voice_list = audio.get_available_windows_voices()
        for choice in getattr(self, "_sapi_voice_choices", []):
            try:
                current = choice.GetSelection()
                choice.Set(self.voice_list)
                if self.voice_list:
                    choice.SetSelection(current if 0 <= current < len(self.voice_list) else 0)
            except Exception:
                logging.debug("Could not populate Windows offline voices.", exc_info=True)
        return self.voice_list

    def _make_speed_choice(self, speed_key):
        choice = wx.Choice(self.tab_tts, choices=list(VOICE_SPEEDS.keys()))
        label = next((label for label, key in VOICE_SPEEDS.items() if key == speed_key), "Normal conversational speed")
        choice.SetStringSelection(label)
        return choice

    def _set_voice_choice_from_value(self, choice, voice_value):
        label = "Sulafat (Warm)"
        for item_label, item_value in GEMINI_TTS_VOICES.items():
            if item_value == voice_value:
                label = item_label
                break
        choice.SetStringSelection(label)

    def _tts_target_choices(self):
        choices = ["configured", "all"]
        choices.extend(self.config.get("speakers", {}).keys())
        return choices

    def _refresh_tts_target_choices(self):
        if not hasattr(self, "tts_profile_controls"):
            return
        choices = self._tts_target_choices()
        for controls in self.tts_profile_controls.values():
            target_choice = controls["target"]
            current = target_choice.GetStringSelection() or "configured"
            new_choices = list(choices)
            if current not in new_choices:
                new_choices.append(current)
            target_choice.Set(new_choices)
            target_choice.SetStringSelection(current)

    def _format_gemini_warm_status(self):
        status = audio.gemini_tts_connection.status()
        if status.get("warm"):
            stamp = datetime.fromtimestamp(status.get("last_heartbeat_at", 0)).strftime("%H:%M:%S")
            return f"Warm: yes, last heartbeat {stamp}"
        err = status.get("last_error")
        if err:
            return f"Warm: no, last error: {err[:90]}"
        return "Warm: not yet"

    def _update_secondary_voice_ui(self):
        engine = self.tts_engine_choice.GetStringSelection()

        if engine == "Edge TTS (Natural)":
            self.secondary_voice_label.SetLabel("Microsoft TTS Voice:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(list(EDGE_VOICES.keys()))

            current_edge = self.config.get("edge_tts_voice", "en-US-AriaNeural")
            label_e = "Aria (Female)"
            for k, v in EDGE_VOICES.items():
                if v == current_edge:
                    label_e = k
            self.secondary_voice_choice.SetStringSelection(label_e)
            self.secondary_voice_choice.Enable(True)
            self.btn_refresh_v.Show()

        elif engine == "Gemini TTS":
            self.secondary_voice_label.SetLabel("Gemini TTS Voice:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(list(GEMINI_TTS_VOICES.keys()))

            current_voice = self.config.get("gemini_tts_voice", "Sulafat")
            label_g = "Sulafat (Warm)"
            for k, v in GEMINI_TTS_VOICES.items():
                if v == current_voice:
                    label_g = k
            self.secondary_voice_choice.SetStringSelection(label_g)
            self.secondary_voice_choice.Enable(True)
            self.btn_refresh_v.Hide()

        elif engine == "Google Cloud":
            self.secondary_voice_label.SetLabel("Google Assistant Accent:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(list(DIALECTS.keys()))

            current_tld = self.config.get("google_tts_tld", "com")
            label_d = "American"
            for k, v in DIALECTS.items():
                if v == current_tld:
                    label_d = k
            self.secondary_voice_choice.SetStringSelection(label_d)
            self.secondary_voice_choice.Enable(True)
            self.btn_refresh_v.Hide()

        else:
            self.secondary_voice_label.SetLabel("Network Speaker Voice:")
            self.secondary_voice_choice.Clear()
            self.secondary_voice_choice.AppendItems(["Uses Offline PC Voice setting below"])
            self.secondary_voice_choice.SetSelection(0)
            self.secondary_voice_choice.Enable(False)
            self.btn_refresh_v.Hide()
            self.btn_refresh_v.Enable(False)

        for control in (self.tts_engine_choice, self.secondary_voice_label, self.secondary_voice_choice, self.btn_refresh_v):
            control.Hide()
            control.Enable(False)

    def on_refresh_edge_voices(self, event):
        self.notify("Upgrading TTS definitions...", priority=10)
        os.system("pip install --upgrade edge-tts")
        self.notify("Definitions updated. Restart app if voices don't appear.")

    def on_tts_engine_change(self, event):
        selected = self.tts_engine_choice.GetStringSelection()
        self.config["tts_engine"] = selected
        self.save_config()
        self._update_secondary_voice_ui()
        self.notify(f"Home Speakers TTS set to: {selected}")

    def on_secondary_voice_change(self, event):
        engine = self.tts_engine_choice.GetStringSelection()
        label = self.secondary_voice_choice.GetStringSelection()

        if engine == "Edge TTS (Natural)":
            self.config["edge_tts_voice"] = EDGE_VOICES[label]
            self.save_config()
            self.notify(f"Microsoft TTS Voice set to: {label}")

        elif engine == "Gemini TTS":
            self.config["gemini_tts_voice"] = GEMINI_TTS_VOICES[label]
            self.save_config()
            self.notify(f"Gemini TTS Voice set to: {label}")

        elif engine == "Google Cloud":
            self.config["google_tts_tld"] = DIALECTS[label]
            self.save_config()
            self.notify(f"Google Assistant Accent set to: {label}")

    def on_tts_keep_warm_change(self, event):
        if not hasattr(self, "default_tts_controls"):
            return
        enabled = self.default_tts_controls["keep_warm"].GetValue()
        self.config["gemini_tts_keep_warm"] = enabled
        self.config["gemini_tts_heartbeat_seconds"] = 240
        self.save_config()
        if enabled:
            audio.gemini_tts_connection.start()
            self.notify("Gemini TTS keep-warm enabled. Heartbeats may count as billable API requests.", priority=10)
        else:
            self.notify("Gemini TTS keep-warm disabled.", priority=10)
        self.gemini_warm_status.SetLabel(self._format_gemini_warm_status())

    def on_tts_warm_now(self, event):
        self.notify("Warming Gemini TTS now.", priority=10)

        def _warm():
            ok = audio.gemini_tts_connection.warm_once()
            wx.CallAfter(self.gemini_warm_status.SetLabel, self._format_gemini_warm_status())
            wx.CallAfter(self.notify, "Gemini TTS warmup complete." if ok else "Gemini TTS warmup failed.", 10)

        safe_submit(_warm)

    def _engine_value_from_choice(self, choice):
        mode = VOICE_BEHAVIOR_MODES.get(choice.GetStringSelection(), "natural_gemini")
        return {"natural_gemini": "gemini", "fast_reliable": "edge", "google_regular": "google", "offline_fallback": "sapi"}.get(mode, "gemini")

    def _read_tts_settings_controls(self, controls, include_use_defaults=False):
        settings = {
            "engine": self._engine_value_from_choice(controls["engine"]),
            "gemini_voice": GEMINI_TTS_VOICES.get(controls["gemini_voice"].GetStringSelection(), "Sulafat"),
            "edge_voice": EDGE_VOICES.get(controls["edge_voice"].GetStringSelection(), "en-US-AriaNeural"),
            "google_tld": DIALECTS.get(controls["google_tld"].GetStringSelection(), "com"),
            "sapi_voice_index": controls["sapi_voice"].GetSelection() if self.voice_list else 0,
            "speed": VOICE_SPEEDS.get(controls["speed"].GetStringSelection(), "normal"),
            "dynamic_mood": controls["dynamic_mood"].GetValue(),
        }
        if include_use_defaults:
            settings["use_defaults"] = controls["use_defaults"].GetValue()
        if "keep_warm" in controls:
            settings["keep_warm"] = controls["keep_warm"].GetValue()
        if "gemini_min_interval" in controls:
            settings["gemini_min_interval_seconds"] = controls["gemini_min_interval"].GetValue()
        return settings

    def _apply_tts_settings(self):
        defaults = self._read_tts_settings_controls(self.default_tts_controls)
        alerts = {
            category: self._read_tts_settings_controls(controls, include_use_defaults=True)
            for category, controls in self.alert_tts_controls.items()
        }
        self.config["tts_defaults"] = defaults
        self.config["tts_alerts"] = alerts
        self.config["tts_engine"] = audio._engine_to_tts_engine(defaults["engine"])
        self.config["gemini_tts_voice"] = defaults["gemini_voice"]
        self.config["edge_tts_voice"] = defaults["edge_voice"]
        self.config["google_tts_tld"] = defaults["google_tld"]
        self.config["local_voice_index"] = defaults["sapi_voice_index"]
        self.config["gemini_tts_keep_warm"] = defaults.get("keep_warm", False)
        self.config["gemini_tts_min_interval_seconds"] = defaults.get("gemini_min_interval_seconds", 0)
        self.config["gemini_tts_heartbeat_seconds"] = 240

    def on_save_voice_behavior(self, event):
        self._apply_tts_settings()
        self.save_config()
        if self.config["tts_defaults"].get("keep_warm"):
            audio.gemini_tts_connection.start()
        self.gemini_warm_status.SetLabel(self._format_gemini_warm_status())
        self._update_secondary_voice_ui()
        self.notify("TTS settings saved.", priority=10)

    def on_test_voice_behavior(self, event, category):
        self.on_save_voice_behavior(event)
        sample = {
            "doorbell": "Someone is at the front door.",
            "utilities": "The refrigerator door has been open for two minutes.",
            "manual": "This is a whole house test broadcast.",
        }.get(category, "This is a Viper Vision test.")
        self.notify(f"Testing {TTS_PROFILE_LABELS[category]}.", priority=3)
        safe_submit(audio.play_notification, category, sample)
