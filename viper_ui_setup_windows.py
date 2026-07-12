import logging
import platform
import time

import wx

import viper_config as cfg


class SetupWindowMixin:
    def should_auto_open_setup_wizard(self):
        if self.first_run or self.clean_first_run_test:
            return True
        ha_settings = cfg.get_ha_settings(self.config, include_env=True)
        api_settings = cfg.get_api_settings(self.config, include_env=True)
        return not ha_settings.get("ha_token") or not api_settings.get("gemini_api_key")

    def show_initial_setup_wizard(self):
        logging.info(
            "[SETUP] Auto-opening setup wizard. first_run=%s clean_first_run_test=%s",
            self.first_run,
            self.clean_first_run_test,
        )
        self.Show(True)
        self.restore_startup_focus()
        self.on_open_setup_wizard(None)

    def _setup_window_attrs(self):
        return ("_setup_wizard_dialog", "_ha_setup_dialog", "_ha_server_assistant_dialog")

    def _active_setup_window(self):
        for attr in self._setup_window_attrs():
            dlg = getattr(self, attr, None)
            if self._is_live_window(dlg):
                return dlg
        return None

    def _enter_setup_window_mode(self, setup_window=None):
        """Keep Alt+Tab focused on setup, not the main control panel."""
        try:
            window = setup_window if self._is_live_window(setup_window) else self._active_setup_window()
            if window is not None:
                window.Show(True)
                window.Raise()
            if self.IsShown():
                self.Show(False)
            self._log_setup_focus_snapshot("enter_setup_window_mode")
            if window is not None:
                wx.CallAfter(self._restore_setup_window_focus, window)
                wx.CallLater(150, self._restore_setup_window_focus, window)
        except Exception:
            logging.debug("Could not enter setup window mode.", exc_info=True)

    def _leave_setup_window_mode(self):
        try:
            if self._active_setup_window() is not None:
                return
            self.Show(True)
            self.restore_main_window_focus()
            self._log_setup_focus_snapshot("leave_setup_window_mode")
        except Exception:
            logging.debug("Could not leave setup window mode.", exc_info=True)

    def _show_control_panel_for_setup_action(self):
        """Show the control panel only when a wizard action intentionally opens a main app area."""
        try:
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
        except Exception:
            logging.debug("Could not show control panel for setup action.", exc_info=True)

    def on_dashboard_activate(self, event):
        try:
            if event.GetActive():
                setup_window = self._active_setup_window()
                if setup_window is not None:
                    logging.info("[FOCUS] Dashboard activated while setup is open; returning focus to setup window.")
                    wx.CallAfter(self._restore_setup_window_focus, setup_window)
                    wx.CallLater(120, self._restore_setup_window_focus, setup_window)
                    return
        except Exception:
            logging.debug("Could not redirect dashboard activation to setup window.", exc_info=True)
        event.Skip()

    def _restore_setup_window_focus(self, setup_window=None):
        window = setup_window if self._is_live_window(setup_window) else self._active_setup_window()
        if window is None:
            return False
        try:
            if window.IsIconized():
                window.Iconize(False)
            if hasattr(window, "Restore"):
                window.Restore()
            window.Show(True)
            window.Enable(True)
            window.Raise()
            try:
                if self.IsShown():
                    self.Show(False)
            except Exception:
                pass
            if hasattr(window, "force_initial_focus"):
                window._initial_focus_given = False
                window.force_initial_focus()
            else:
                window.SetFocus()
            self._log_setup_focus_snapshot("restore_setup_window_focus")
            return True
        except Exception:
            logging.debug("Could not restore setup window focus.", exc_info=True)
            return False

    def _close_setup_surfaces(self, keep=None):
        for attr in self._setup_window_attrs():
            if attr == keep:
                continue
            dlg = getattr(self, attr, None)
            if dlg is None:
                continue
            try:
                if hasattr(dlg, "_destroyed"):
                    dlg._destroyed = True
                dlg.Destroy()
            except RuntimeError:
                pass
            except Exception:
                logging.debug("Could not close setup surface %s.", attr, exc_info=True)
            setattr(self, attr, None)

    def _is_live_window(self, window):
        if window is None:
            return False
        try:
            return bool(window.GetHandle())
        except RuntimeError:
            return False
        except Exception:
            return False

    def _log_setup_focus_snapshot(self, context):
        try:
            now = time.monotonic()
            if not hasattr(self, "_last_focus_snapshot_log"):
                self._last_focus_snapshot_log = {}
            last = self._last_focus_snapshot_log.get(context, 0)
            if now - last < 10:
                return
            self._last_focus_snapshot_log[context] = now
            active = []
            disabled = []
            dashboard_state = f"dashboard:shown={self.IsShown()}:enabled={self.IsEnabled()}"
            for attr in self._setup_window_attrs():
                dlg = getattr(self, attr, None)
                if not self._is_live_window(dlg):
                    continue
                title = dlg.GetTitle() if hasattr(dlg, "GetTitle") else attr
                enabled = dlg.IsEnabled() if hasattr(dlg, "IsEnabled") else None
                shown = dlg.IsShown() if hasattr(dlg, "IsShown") else None
                active.append(f"{attr}:{title}:shown={shown}:enabled={enabled}")
                if enabled is False:
                    disabled.append(title)
            focus = wx.Window.FindFocus()
            focus_desc = "none"
            if focus is not None:
                focus_desc = f"{focus.__class__.__name__}:{focus.GetName() if hasattr(focus, 'GetName') else ''}"
            foreground = ""
            foreground_enabled = None
            if platform.system().lower() == "windows":
                try:
                    import ctypes

                    user32 = ctypes.windll.user32
                    hwnd = user32.GetForegroundWindow()
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, buf, 512)
                    foreground = buf.value
                    foreground_enabled = bool(user32.IsWindowEnabled(hwnd))
                except Exception:
                    foreground = "unavailable"
            logging.info(
                "[FOCUS SNAPSHOT] %s foreground=%r foreground_enabled=%s focus=%r setup_windows=%s disabled_setup_windows=%s",
                context,
                foreground,
                foreground_enabled,
                focus_desc,
                [dashboard_state] + active,
                disabled,
            )
        except Exception:
            logging.debug("Could not log setup focus snapshot.", exc_info=True)

    def restore_startup_focus(self):
        try:
            if any(self._is_live_window(getattr(self, attr, None)) for attr in self._setup_window_attrs()):
                logging.info("[FOCUS] Startup focus skipped because setup dialog is open.")
                return
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
            self._nudge_windows_foreground()
        except Exception:
            logging.debug("Could not restore startup window.", exc_info=True)

    def _preferred_startup_focus(self):
        try:
            if hasattr(self, "notebook") and hasattr(self, "tab_dash"):
                for idx in range(self.notebook.GetPageCount()):
                    if self.notebook.GetPage(idx) is self.tab_dash:
                        self.notebook.SetSelection(idx)
                        break
            for name in ("notebook", "btn_arm", "broadcast_input"):
                ctrl = getattr(self, name, None)
                if self._control_accepts_keyboard_focus(ctrl):
                    return ctrl
        except Exception:
            logging.debug("Could not choose startup focus target.", exc_info=True)
        return getattr(self, "notebook", None) or self

    def _control_accepts_keyboard_focus(self, ctrl):
        if ctrl is None or not hasattr(ctrl, "SetFocus"):
            return False
        try:
            if hasattr(ctrl, "IsShownOnScreen") and not ctrl.IsShownOnScreen():
                return False
            if hasattr(ctrl, "IsEnabled") and not ctrl.IsEnabled():
                return False
            if hasattr(ctrl, "CanAcceptFocus") and not ctrl.CanAcceptFocus():
                return False
            if hasattr(ctrl, "CanAcceptFocusFromKeyboard") and not ctrl.CanAcceptFocusFromKeyboard():
                return False
        except RuntimeError:
            return False
        except Exception:
            logging.debug("Could not inspect focus target.", exc_info=True)
            return False
        return True

    def _focus_control_for_screen_reader(self, focus_target, context):
        if focus_target is None:
            focus_target = self
        if hasattr(focus_target, "SetFocusFromKbd"):
            try:
                focus_target.SetFocusFromKbd()
                logging.info("[FOCUS] %s focused with SetFocusFromKbd: %s", context, self._describe_focus_target(focus_target))
                return True
            except Exception:
                pass
        if hasattr(focus_target, "SetFocus"):
            focus_target.SetFocus()
            logging.info("[FOCUS] %s focused with SetFocus: %s", context, self._describe_focus_target(focus_target))
            return True
        return False

    def _describe_focus_target(self, ctrl):
        try:
            label = ctrl.GetLabel() if hasattr(ctrl, "GetLabel") else ""
            name = ctrl.GetName() if hasattr(ctrl, "GetName") else ""
            return f"{ctrl.__class__.__name__} name={name!r} label={label!r}"
        except Exception:
            return repr(ctrl)

    def restore_main_window_focus(self):
        self.record_setup_event("focus_restore_start", "Restoring main Viper window focus after setup.")
        self._log_setup_focus_snapshot("before_restore_main_window_focus")
        self._restore_main_window_focus_once()
        wx.CallLater(100, self._restore_main_window_focus_once)
        wx.CallLater(300, self._restore_main_window_focus_once)
        wx.CallLater(700, self._restore_main_window_focus_once)

    def restore_from_tray_focus(self):
        logging.info("[FOCUS] Restoring Viper from system tray.")
        self._log_setup_focus_snapshot("before_restore_from_tray")
        self._restore_from_tray_focus_once()
        wx.CallLater(100, self._restore_from_tray_focus_once)
        wx.CallLater(300, self._restore_from_tray_focus_once)
        wx.CallLater(700, self._restore_from_tray_focus_once)

    def _restore_from_tray_focus_once(self):
        try:
            if self._active_setup_window() is not None:
                self._restore_setup_window_focus()
                return
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
            try:
                self.RequestUserAttention(wx.USER_ATTENTION_INFO)
            except Exception:
                pass
            self._nudge_windows_foreground()
            target = self._preferred_focus_after_tray_restore()
            self._focus_control_for_screen_reader(target, "tray_restore")
        except Exception:
            logging.debug("Could not restore Viper focus from tray.", exc_info=True)

    def _preferred_focus_after_tray_restore(self):
        try:
            if hasattr(self, "notebook"):
                self.notebook.GetPage(self.notebook.GetSelection())
                if self._control_accepts_keyboard_focus(self.notebook):
                    return self.notebook
        except Exception:
            logging.debug("Could not choose preferred focus target after tray restore.", exc_info=True)
        return getattr(self, "notebook", None) or self

    def _restore_main_window_focus_once(self):
        try:
            if self._active_setup_window() is not None:
                self._restore_setup_window_focus()
                return
            self.Show(True)
            if self.IsIconized():
                self.Iconize(False)
            if hasattr(self, "Restore"):
                self.Restore()
            self.Raise()
            self._nudge_windows_foreground()
            self._focus_preferred_child_after_setup()
            self.record_setup_event("focus_restore_attempt", "Focus restore attempt completed.")
            if hasattr(self, "tb_icon"):
                wx.CallAfter(self.tb_icon._restore_frame)
                wx.CallLater(150, self.tb_icon._restore_frame)
                wx.CallLater(225, self._focus_preferred_child_after_setup)
        except Exception:
            logging.debug("Could not restore main Viper window focus.", exc_info=True)

    def _focus_preferred_child_after_setup(self):
        focus_target = self._preferred_focus_after_setup()
        if focus_target is None:
            focus_target = getattr(self, "notebook", None) or self
        self._focus_control_for_screen_reader(focus_target, "setup_restore")

    def _preferred_focus_after_setup(self):
        try:
            if hasattr(self, "notebook") and hasattr(self, "tab_setup"):
                for idx in range(self.notebook.GetPageCount()):
                    if self.notebook.GetPage(idx) is self.tab_setup:
                        self.notebook.SetSelection(idx)
                        break
            for name in ("btn_setup_wizard", "btn_choose_setup_speakers", "btn_ha_setup", "btn_new_user_setup", "btn_diagnostics", "notebook"):
                ctrl = getattr(self, name, None)
                if self._control_accepts_keyboard_focus(ctrl):
                    return ctrl
        except Exception:
            logging.debug("Could not choose preferred focus target after setup.", exc_info=True)
        return None

    def _select_book_page(self, book, index):
        try:
            book.SetSelection(index)
        except Exception:
            logging.debug("Could not switch page.", exc_info=True)

    def _nudge_windows_foreground(self):
        if platform.system().lower() != "windows":
            return
        try:
            import ctypes

            hwnd = self.GetHandle()
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SW_RESTORE = 9
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            logging.debug("Could not nudge Viper window to Windows foreground.", exc_info=True)

    def on_home_assistant_setup(self, event):
        self.show_home_assistant_setup(initial_page=self.suggested_setup_page())

    def on_new_user_setup(self, event):
        self.show_new_user_setup_assistant()

    def show_new_user_setup_assistant(self):
        self._close_setup_surfaces(keep="_ha_server_assistant_dialog")
        existing = getattr(self, "_ha_server_assistant_dialog", None)
        if self._is_live_window(existing):
            try:
                existing.force_initial_focus()
                self._enter_setup_window_mode(existing)
                self._log_setup_focus_snapshot("reuse_ha_server_assistant")
                return
            except Exception:
                self._ha_server_assistant_dialog = None
        else:
            self._ha_server_assistant_dialog = None
        dlg = self._create_home_assistant_first_run_assistant_dialog()
        self._ha_server_assistant_dialog = dlg
        dlg.Show()
        wx.CallAfter(self._enter_setup_window_mode, dlg)
        wx.CallLater(75, dlg.force_initial_focus)
        wx.CallLater(300, dlg.force_initial_focus)
        wx.CallLater(450, self._log_setup_focus_snapshot, "show_ha_server_assistant")

    def show_home_assistant_setup(self, initial_page=None, auto_run=False, preserve_wizard=False):
        if preserve_wizard:
            for attr in ("_ha_setup_dialog", "_ha_server_assistant_dialog"):
                dlg = getattr(self, attr, None)
                if dlg is None:
                    continue
                try:
                    if hasattr(dlg, "_destroyed"):
                        dlg._destroyed = True
                    dlg.Destroy()
                except RuntimeError:
                    pass
                except Exception:
                    logging.debug("Could not close setup surface %s.", attr, exc_info=True)
                setattr(self, attr, None)
        else:
            self._close_setup_surfaces(keep="_ha_setup_dialog")
        existing = getattr(self, "_ha_setup_dialog", None)
        if self._is_live_window(existing):
            try:
                if initial_page:
                    existing.select_page(initial_page)
                existing.force_initial_focus()
                if auto_run:
                    wx.CallAfter(existing.on_beginner_auto_setup, None)
                self._log_setup_focus_snapshot("reuse_ha_setup")
                return
            except Exception:
                logging.debug("Could not reuse existing Home Assistant setup dialog.", exc_info=True)
                try:
                    existing.Destroy()
                except Exception:
                    pass
                self._ha_setup_dialog = None
        else:
            self._ha_setup_dialog = None
        use_env_prefill = not self.clean_first_run_test
        dlg = self._create_home_assistant_setup_dialog(use_env_prefill=use_env_prefill)
        self._ha_setup_dialog = dlg
        if initial_page:
            dlg.select_page(initial_page)
        dlg.Show()
        wx.CallAfter(self._enter_setup_window_mode, dlg)
        if auto_run:
            wx.CallAfter(dlg.on_beginner_auto_setup, None)
        wx.CallLater(75, dlg.force_initial_focus)
        wx.CallLater(300, dlg.force_initial_focus)
        wx.CallLater(450, self._log_setup_focus_snapshot, "show_ha_setup")
