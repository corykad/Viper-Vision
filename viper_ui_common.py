import os

import wx

from viper_runtime import safe_submit


class AccessibleStatusText(wx.StaticText):
    """Static status text with TextCtrl-like methods used by Viper status panels."""

    def __init__(self, parent, value="", wrap_width=760, **kwargs):
        self._wrap_width = wrap_width
        self._value = str(value or "")
        super().__init__(parent, label=self._value, **kwargs)
        if self._wrap_width:
            self.Wrap(self._wrap_width)

    def SetLabel(self, label):
        self._value = str(label or "")
        super().SetLabel(self._value)
        if self._wrap_width:
            self.Wrap(self._wrap_width)
        parent = self.GetParent()
        if parent:
            try:
                parent.Layout()
            except Exception:
                pass

    def SetValue(self, value):
        self.SetLabel(value)

    def GetValue(self):
        return self._value

    def AppendText(self, text):
        self.SetLabel(self._value + str(text or ""))

    def Clear(self):
        self.SetLabel("")

    def SetInsertionPointEnd(self):
        pass

    def ShowPosition(self, pos):
        pass

    def GetLastPosition(self):
        return len(self._value)


def describe_control(control, name, description="", *, focus_handler=None, bind_focus=False):
    text = description or name
    control.SetName(name)
    control.SetToolTip(text)
    try:
        accessible = control.GetOrCreateAccessible()
        if accessible:
            accessible.SetName(name)
            accessible.SetDescription(text)
    except Exception:
        pass
    if focus_handler and bind_focus:
        try:
            control.Bind(wx.EVT_SET_FOCUS, focus_handler)
        except Exception:
            pass


def should_log_focus():
    return os.getenv("VIPER_FOCUS_LOG", "").strip().lower() in {"1", "true", "yes", "on"}


def make_accessible_status_text(parent, **kwargs):
    return AccessibleStatusText(parent, **kwargs)


def submit_ui_task(fn, *args, **kwargs):
    return safe_submit(fn, *args, **kwargs)
