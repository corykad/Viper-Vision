import os

import wx

from viper_runtime import executor, is_shutting_down


class AppLifecycleMixin:
    def on_minimize(self, event):
        if isinstance(event, wx.CloseEvent) and event.CanVeto(): event.Veto()
        wx.CallLater(500, self.Hide)

    def on_quit(self, event):
        self.running = False
        is_shutting_down.set()
        if hasattr(self, "_ha_address_recovery_stop"):
            self._ha_address_recovery_stop.set()
        self._mark_app_clean_shutdown()
        if hasattr(self, "ha_listener"):
            self.ha_listener.stop()
        executor.shutdown(wait=False)
        self.tb_icon.RemoveIcon()
        self.tb_icon.Destroy()
        self.Destroy()
        os._exit(0)
