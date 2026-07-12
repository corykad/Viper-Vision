import logging

import wx

import viper_config as cfg
import viper_ui_common as ui_common


AccessibleStatusText = ui_common.AccessibleStatusText

AI_DESCRIPTION_STYLE_LABELS = {
    "balanced": "Balanced",
    "fast_security": "Fast security summary",
    "people_movement": "People and movement",
    "packages_deliveries": "Packages and deliveries",
    "detailed_blind": "Detailed for blind user",
    "custom": "Custom",
}
AI_DESCRIPTION_STYLE_KEYS_BY_LABEL = {label: key for key, label in AI_DESCRIPTION_STYLE_LABELS.items()}
AI_DESCRIPTION_JOBS = [
    (
        "front_photo",
        "Front door alert",
        "Front door alert description style. Choose what Gemini should pay attention to for front door still-image alerts.",
        "Front door custom AI instructions. Only used when Front door alert is set to Custom.",
    ),
    (
        "back_photo",
        "Back door alert",
        "Back door alert description style. Choose what Gemini should pay attention to for back door still-image alerts.",
        "Back door custom AI instructions. Only used when Back door alert is set to Custom.",
    ),
    (
        "manual_video",
        "Manual outside video check",
        "Manual outside video check description style. Choose what Gemini should pay attention to when you press Analyze Camera Video Now.",
        "Manual outside video custom AI instructions. You may use placeholders: {location}, {side}, and {first_description}.",
    ),
    (
        "smart_video",
        "Smart video follow-up",
        "Smart video follow-up description style. Choose what Gemini should pay attention to when Smart mode asks for more detail.",
        "Smart video follow-up custom AI instructions. You may use placeholders: {location}, {side}, and {first_description}.",
    ),
    (
        "detailed_video",
        "Detailed video follow-up",
        "Detailed video follow-up description style. Choose what Gemini should pay attention to when Detailed mode sends video after an alert.",
        "Detailed video follow-up custom AI instructions. You may use placeholders: {location}, {side}, and {first_description}.",
    ),
]


class PromptEditorMixin:
    def setup_prompt_editor_tab(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = AccessibleStatusText(
            self.tab_prompts,
            value=(
                "Choose what Viper should pay attention to.\n\n"
                "You do not need to edit AI instructions unless you choose Custom."
            ),
            size=(-1, 80),
        )
        self._describe_control(
            intro,
            "AI Descriptions introduction. Choose what Viper should pay attention to. You do not need to edit AI instructions unless you choose Custom.",
        )
        sizer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        desc_box = wx.StaticBox(self.tab_prompts, label="AI Description Settings")
        desc_sizer = wx.StaticBoxSizer(desc_box, wx.VERTICAL)
        self.ai_description_controls = {}
        styles = self.config.get("ai_description_styles", {})
        custom = self.config.get("ai_custom_descriptions", {})
        style_labels = list(AI_DESCRIPTION_STYLE_LABELS.values())
        for job, label, style_description, custom_description in AI_DESCRIPTION_JOBS:
            job_box = wx.StaticBox(self.tab_prompts, label=label)
            job_sizer = wx.StaticBoxSizer(job_box, wx.VERTICAL)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self.tab_prompts, label=f"{label} style:"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            choice = wx.Choice(self.tab_prompts, choices=style_labels)
            style_key = styles.get(job, cfg.DEFAULT_AI_DESCRIPTION_STYLES.get(job, "balanced"))
            choice.SetStringSelection(AI_DESCRIPTION_STYLE_LABELS.get(style_key, "Balanced"))
            choice.Bind(wx.EVT_CHOICE, self.on_ai_description_style_change)
            self._describe_control(choice, style_description)
            row.Add(choice, 1, wx.ALL | wx.EXPAND, 5)
            job_sizer.Add(row, 0, wx.EXPAND)

            custom_label = wx.StaticText(self.tab_prompts, label=f"{label} custom AI instructions:")
            custom_editor = wx.TextCtrl(self.tab_prompts, style=wx.TE_MULTILINE, size=(-1, 95))
            custom_editor.SetValue(custom.get(job, ""))
            self._describe_control(custom_editor, custom_description)
            job_sizer.Add(custom_label, 0, wx.ALL, 5)
            job_sizer.Add(custom_editor, 0, wx.ALL | wx.EXPAND, 5)
            self.ai_description_controls[job] = {
                "choice": choice,
                "custom_label": custom_label,
                "custom_editor": custom_editor,
            }
            desc_sizer.Add(job_sizer, 0, wx.ALL | wx.EXPAND, 6)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_save_ai_descriptions = wx.Button(self.tab_prompts, label="Save AI Description Settings", size=(-1, 44))
        self.btn_reset_ai_descriptions = wx.Button(self.tab_prompts, label="Reset AI Descriptions To Recommended Settings", size=(-1, 44))
        self.btn_save_ai_descriptions.Bind(wx.EVT_BUTTON, self.on_save_ai_descriptions)
        self.btn_reset_ai_descriptions.Bind(wx.EVT_BUTTON, self.on_reset_ai_descriptions)
        self._describe_control(self.btn_save_ai_descriptions, "Save AI Description Settings button. Saves what Viper should pay attention to for each doorbell image and video situation.")
        self._describe_control(self.btn_reset_ai_descriptions, "Reset AI Descriptions To Recommended Settings button. Restores recommended description styles and clears custom AI instructions.")
        buttons.Add(self.btn_save_ai_descriptions, 1, wx.ALL | wx.EXPAND, 5)
        buttons.Add(self.btn_reset_ai_descriptions, 1, wx.ALL | wx.EXPAND, 5)
        desc_sizer.Add(buttons, 0, wx.EXPAND)
        sizer.Add(desc_sizer, 1, wx.ALL | wx.EXPAND, 10)

        self.prompt_status_txt = AccessibleStatusText(self.tab_prompts, value="AI description settings ready.", size=(-1, 70))
        self._describe_control(self.prompt_status_txt, "AI Description status. Reports saved AI description settings.")
        sizer.Add(self.prompt_status_txt, 0, wx.ALL | wx.EXPAND, 10)
        self.tab_prompts.SetSizer(sizer)
        self._sync_ai_description_custom_visibility()

    def _ai_description_style_key(self, choice):
        return AI_DESCRIPTION_STYLE_KEYS_BY_LABEL.get(choice.GetStringSelection(), "balanced")

    def _sync_ai_description_custom_visibility(self):
        controls = getattr(self, "ai_description_controls", {})
        for group in controls.values():
            show = self._ai_description_style_key(group["choice"]) == "custom"
            group["custom_label"].Show(show)
            group["custom_editor"].Show(show)
        try:
            self.tab_prompts.Layout()
            self.tab_prompts.FitInside()
        except Exception:
            logging.debug("Could not update AI description custom editor visibility.", exc_info=True)

    def on_ai_description_style_change(self, event):
        self._sync_ai_description_custom_visibility()
        if event:
            event.Skip()

    def on_save_ai_descriptions(self, event):
        styles = {}
        custom = {}
        for job, group in getattr(self, "ai_description_controls", {}).items():
            styles[job] = self._ai_description_style_key(group["choice"])
            custom[job] = group["custom_editor"].GetValue().strip()
        self.config["ai_description_styles"] = styles
        self.config["ai_custom_descriptions"] = custom
        self.save_config()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue("Saved AI description settings.")
        self.notify("AI description settings saved.", priority=10)

    def on_reset_ai_descriptions(self, event):
        self.config["ai_description_styles"] = dict(cfg.DEFAULT_AI_DESCRIPTION_STYLES)
        self.config["ai_custom_descriptions"] = {job: "" for job in cfg.AI_DESCRIPTION_JOBS}
        for job, group in getattr(self, "ai_description_controls", {}).items():
            style_key = self.config["ai_description_styles"].get(job, "balanced")
            group["choice"].SetStringSelection(AI_DESCRIPTION_STYLE_LABELS.get(style_key, "Balanced"))
            group["custom_editor"].SetValue("")
        self.save_config()
        self._sync_ai_description_custom_visibility()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue("Reset AI descriptions to recommended settings.")
        self.notify("AI descriptions reset to recommended settings.", priority=10)

    def _refresh_prompt_choices(self):
        prompt_names = list(self.config.get("prompts", {}).keys()) or ["Standard"]
        for choice in (
            getattr(self, "prompt_choice", None),
            getattr(self, "prompt_default_choice", None),
            getattr(self, "prompt_front_choice", None),
            getattr(self, "prompt_back_choice", None),
        ):
            if not choice:
                continue
            current = choice.GetStringSelection()
            choice.Set(prompt_names)
            choice.SetStringSelection(current if current in prompt_names else self.config.get("active_prompt", prompt_names[0]))

    def _refresh_video_prompt_choices(self):
        prompt_names = list(self.config.get("video_prompts", {}).keys()) or ["Manual Outside Check"]
        for choice in (
            getattr(self, "video_prompt_choice", None),
            getattr(self, "video_prompt_manual_choice", None),
            getattr(self, "video_prompt_smart_choice", None),
            getattr(self, "video_prompt_detailed_choice", None),
        ):
            if not choice:
                continue
            current = choice.GetStringSelection()
            choice.Set(prompt_names)
            choice.SetStringSelection(current if current in prompt_names else self.config.get("active_video_prompt", prompt_names[0]))

    def on_prompt_assignment_change(self, event):
        default_prompt = self.prompt_default_choice.GetStringSelection()
        front_prompt = self.prompt_front_choice.GetStringSelection()
        back_prompt = self.prompt_back_choice.GetStringSelection()
        if default_prompt:
            self.config["active_prompt"] = default_prompt
        self.config["doorbell_prompt_profiles"] = {
            "front": front_prompt or default_prompt,
            "back": back_prompt or default_prompt,
        }
        self.save_config()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue(
                f"Saved still photo prompt assignment. Default: {default_prompt}. Front: {front_prompt}. Back: {back_prompt}."
            )
        self.notify("Still photo prompt assignment saved.", priority=10)

    def on_prompt_change(self, event):
        new_prompt = self.prompt_choice.GetStringSelection()
        self.prompt_editor.SetValue(self.config.get("prompts", {}).get(new_prompt, ""))
        self.notify(f"Loaded {new_prompt} profile")

    def on_save_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        txt = self.prompt_editor.GetValue().strip()
        if txt:
            self.config["prompts"][name] = txt
            self.save_config()
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Saved still photo prompt profile {name}.")
            self.notify(f"Saved {name}")

    def on_new_prompt(self, event):
        name = wx.GetTextFromUser("New Still Photo Prompt Name:", "New Still Photo Prompt")
        if name and name not in self.config["prompts"]:
            self.config["prompts"][name] = "Analyze frames for security."
            self.config["active_prompt"] = name
            self.save_config()
            self._refresh_prompt_choices()
            self.prompt_choice.SetStringSelection(name)
            if hasattr(self, "prompt_default_choice"):
                self.prompt_default_choice.SetStringSelection(name)
            self.prompt_editor.SetValue(self.config["prompts"][name])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Created still photo prompt profile {name}.")
            self.notify(f"Created {name}")

    def on_del_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        if len(self.config["prompts"]) > 1:
            del self.config["prompts"][name]
            new_a = list(self.config["prompts"].keys())[0]
            self.config["active_prompt"] = new_a
            profiles = self.config.setdefault("doorbell_prompt_profiles", {})
            if profiles.get("front") == name:
                profiles["front"] = new_a
            if profiles.get("back") == name:
                profiles["back"] = new_a
            self.save_config()
            self._refresh_prompt_choices()
            self.prompt_choice.SetStringSelection(new_a)
            self.prompt_editor.SetValue(self.config["prompts"][new_a])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Deleted still photo prompt profile {name}.")

    def on_video_prompt_assignment_change(self, event):
        manual_prompt = self.video_prompt_manual_choice.GetStringSelection()
        smart_prompt = self.video_prompt_smart_choice.GetStringSelection()
        detailed_prompt = self.video_prompt_detailed_choice.GetStringSelection()
        if manual_prompt:
            self.config["active_video_prompt"] = manual_prompt
        self.config["doorbell_video_prompt_profiles"] = {
            "manual": manual_prompt,
            "smart": smart_prompt,
            "detailed": detailed_prompt,
        }
        self.save_config()
        if hasattr(self, "prompt_status_txt"):
            self.prompt_status_txt.SetValue(
                f"Saved video prompt assignment. Manual: {manual_prompt}. Smart: {smart_prompt}. Detailed: {detailed_prompt}."
            )
        self.notify("Video prompt assignment saved.", priority=10)

    def on_save_video_prompts(self, event):
        assignments = {
            "manual": ("Manual Outside Check", getattr(self, "video_prompt_manual_editor", None)),
            "smart": ("Smart Follow Up", getattr(self, "video_prompt_smart_editor", None)),
            "detailed": ("Detailed Doorbell Video", getattr(self, "video_prompt_detailed_editor", None)),
        }
        prompts = self.config.setdefault("video_prompts", {})
        profiles = self.config.setdefault("doorbell_video_prompt_profiles", {})
        saved = []
        for mode, (fallback_name, editor) in assignments.items():
            if editor is None:
                continue
            name = profiles.get(mode) or fallback_name
            text = editor.GetValue().strip()
            if not text:
                continue
            prompts[name] = text
            profiles[mode] = name
            saved.append(mode)
        if saved:
            self.config["active_video_prompt"] = profiles.get("manual") or self.config.get("active_video_prompt", "")
            self.save_config()
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue("Saved video prompts for: " + ", ".join(saved) + ".")
            self.notify("Video prompts saved.", priority=10)
        else:
            self.notify("No video prompt text was saved. Each prompt needs text first.", priority=10)

    def on_video_prompt_change(self, event):
        name = self.video_prompt_choice.GetStringSelection()
        self.video_prompt_editor.SetValue(self.config.get("video_prompts", {}).get(name, ""))
        self.notify(f"Loaded video prompt {name}", priority=10)

    def on_save_video_prompt(self, event):
        name = self.video_prompt_choice.GetStringSelection()
        txt = self.video_prompt_editor.GetValue().strip()
        if txt:
            self.config.setdefault("video_prompts", {})[name] = txt
            self.save_config()
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Saved video prompt profile {name}.")
            self.notify(f"Saved video prompt {name}", priority=10)

    def on_new_video_prompt(self, event):
        name = wx.GetTextFromUser("New Video Prompt Name:", "New Video Prompt")
        if name and name not in self.config.setdefault("video_prompts", {}):
            self.config["video_prompts"][name] = (
                "Describe this doorbell video for a blind homeowner. Mention people, vehicles, packages, motion, and anything that needs attention. "
                "Use one or two complete sentences."
            )
            self.config["active_video_prompt"] = name
            self.save_config()
            self._refresh_video_prompt_choices()
            self.video_prompt_choice.SetStringSelection(name)
            if hasattr(self, "video_prompt_manual_choice"):
                self.video_prompt_manual_choice.SetStringSelection(name)
            self.video_prompt_editor.SetValue(self.config["video_prompts"][name])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Created video prompt profile {name}.")
            self.notify(f"Created video prompt {name}", priority=10)

    def on_del_video_prompt(self, event):
        name = self.video_prompt_choice.GetStringSelection()
        prompts = self.config.setdefault("video_prompts", {})
        if len(prompts) > 1:
            del prompts[name]
            new_a = list(prompts.keys())[0]
            self.config["active_video_prompt"] = new_a
            profiles = self.config.setdefault("doorbell_video_prompt_profiles", {})
            for key in ("manual", "smart", "detailed"):
                if profiles.get(key) == name:
                    profiles[key] = new_a
            self.save_config()
            self._refresh_video_prompt_choices()
            self.video_prompt_choice.SetStringSelection(new_a)
            self.video_prompt_editor.SetValue(prompts[new_a])
            if hasattr(self, "prompt_status_txt"):
                self.prompt_status_txt.SetValue(f"Deleted video prompt profile {name}.")
