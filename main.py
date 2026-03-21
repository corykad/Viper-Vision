import wx
import wx.adv
import os
import sys
import json
import threading
import requests
import time
import logging
from queue import PriorityQueue, Empty
from concurrent.futures import ThreadPoolExecutor

from accessible_output2.outputs import auto
from flask import Flask, request
from waitress import serve

import viper_config as cfg
import viper_audio as audio
import viper_vision as vision

# ==========================================
# GLOBAL APP & THREAD POOL
# ==========================================
executor = ThreadPoolExecutor(max_workers=10)
app = Flask(__name__)
dash_app = None  

# ==========================================
# FLASK WEBHOOK ROUTES
# ==========================================
@app.route('/doorbell-webhook', methods=['POST'])
def handle_front():
    executor.submit(vision.process_doorbell, "front door", cfg.RTSP_FRONT, "front", dash_app, executor)
    return "OK", 200

@app.route('/doorbell-webhook/back', methods=['POST'])
def handle_back():
    executor.submit(vision.process_doorbell, "back door", cfg.RTSP_BACK, "back", dash_app, executor)
    return "OK", 200

def run_flask_server():
    serve(app, host='0.0.0.0', port=cfg.FLASK_PORT, threads=4)

# ==========================================
# SYSTEM TRAY TETHER
# ==========================================
class ViperTaskBarIcon(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
        self.SetIcon(icon, "Viper Vision")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_restore)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_restore)

    def on_restore(self, event):
        self.frame.Show(True)
        if self.frame.IsIconized():
            self.frame.Iconize(False)
        self.frame.Raise()
        self.frame.SetFocus()

# ==========================================
# MAIN DASHBOARD CLASS
# ==========================================
class ViperDashboard(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Viper Vision Control Panel", size=(550, 950))
        
        self.running = True
        self.config = cfg.load_config()
        self.is_armed = self.config.get("is_armed", True)
        
        cfg.sync_globals_from_config()

        try:
            self.sr = auto.Auto()
            logging.info("Screen Reader Bridge established.")
        except Exception as e:
            self.sr = None
            logging.error(f"Screen Reader Bridge failed: {e}")

        self.speech_queue = PriorityQueue()
        self.speech_lock = threading.Lock()
        self._msg_counter = 0  

        self.panel = wx.Panel(self)
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.tb_icon = ViperTaskBarIcon(self)

        self.setup_ui()

        self.status_display = wx.TextCtrl(
            self.panel, value="Viper Vision Online", 
            style=wx.TE_READONLY | wx.TE_CENTRE | wx.NO_BORDER
        )
        self.status_display.SetBackgroundColour(self.panel.GetBackgroundColour())
        self.sizer.Add(self.status_display, 0, wx.ALL | wx.EXPAND, 15)

        self.panel.SetSizer(self.sizer)
        self.Bind(wx.EVT_CLOSE, self.on_minimize)
        self.Center()
        self.Show()

        threading.Thread(target=self.speech_worker, daemon=True).start()

    def save_config(self):
        cfg.save_config(self.config)

    def setup_ui(self):
        self.btn_arm = wx.Button(self.panel, label="Disarm System" if self.is_armed else "Arm System", size=(-1, 45))
        self.btn_arm.Bind(wx.EVT_BUTTON, self.on_toggle_arm)
        self.sizer.Add(self.btn_arm, 0, wx.ALL | wx.EXPAND, 10)

        # --- AI PROMPT EDITOR UI ---
        pbox = wx.StaticBox(self.panel, label="AI Prompt Editor")
        psizer = wx.StaticBoxSizer(pbox, wx.VERTICAL)
        
        self.prompt_choice = wx.Choice(self.panel, choices=list(self.config["prompts"].keys()))
        active_p = self.config.get("active_prompt", "Standard")
        self.prompt_choice.SetStringSelection(active_p)
        self.prompt_choice.Bind(wx.EVT_CHOICE, self.on_prompt_change)
        psizer.Add(self.prompt_choice, 0, wx.EXPAND | wx.ALL, 5)

        self.prompt_editor = wx.TextCtrl(self.panel, style=wx.TE_MULTILINE, size=(-1, 80))
        self.prompt_editor.SetValue(self.config["prompts"].get(active_p, ""))
        psizer.Add(self.prompt_editor, 0, wx.EXPAND | wx.ALL, 5)

        pbtn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_save_prompt = wx.Button(self.panel, label="Save Current")
        self.btn_save_prompt.Bind(wx.EVT_BUTTON, self.on_save_prompt)
        self.btn_new_prompt = wx.Button(self.panel, label="New Profile")
        self.btn_new_prompt.Bind(wx.EVT_BUTTON, self.on_new_prompt)
        self.btn_del_prompt = wx.Button(self.panel, label="Delete Profile")
        self.btn_del_prompt.Bind(wx.EVT_BUTTON, self.on_del_prompt)
        
        pbtn_sizer.Add(self.btn_save_prompt, 1, wx.ALL, 2)
        pbtn_sizer.Add(self.btn_new_prompt, 1, wx.ALL, 2)
        pbtn_sizer.Add(self.btn_del_prompt, 1, wx.ALL, 2)
        psizer.Add(pbtn_sizer, 0, wx.EXPAND | wx.ALL, 0)

        self.sizer.Add(psizer, 0, wx.ALL | wx.EXPAND, 10)

        # --- SPEAKER LIST UI ---
        sbox = wx.StaticBox(self.panel, label="Speaker Targets (Spacebar to Toggle)")
        ssizer = wx.StaticBoxSizer(sbox, wx.VERTICAL)
        
        self.speaker_list = wx.CheckListBox(self.panel, choices=[])
        self.speaker_list.Bind(wx.EVT_CHECKLISTBOX, self.on_speaker_toggle)
        self.speaker_list.Bind(wx.EVT_LISTBOX, self.on_speaker_select) 
        self.speaker_list.Bind(wx.EVT_SET_FOCUS, self.on_speaker_focus)
        ssizer.Add(self.speaker_list, 1, wx.EXPAND | wx.ALL, 5)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add_spk = wx.Button(self.panel, label="Add Speaker")
        self.btn_add_spk.Bind(wx.EVT_BUTTON, self.on_add_speaker)
        self.btn_rem_spk = wx.Button(self.panel, label="Remove Selected")
        self.btn_rem_spk.Bind(wx.EVT_BUTTON, self.on_remove_speaker)
        btn_sizer.Add(self.btn_add_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_rem_spk, 1, wx.ALL, 5)
        
        ssizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 0)
        self.sizer.Add(ssizer, 1, wx.ALL | wx.EXPAND, 10)
        
        self.refresh_speaker_list()

        # --- MANUAL BROADCAST UI ---
        cbox = wx.StaticBox(self.panel, label="Manual Intercom Broadcast")
        csizer = wx.StaticBoxSizer(cbox, wx.HORIZONTAL)
        self.broadcast_input = wx.TextCtrl(self.panel, style=wx.TE_PROCESS_ENTER)
        self.broadcast_btn = wx.Button(self.panel, label="Speak")
        
        self.broadcast_input.Bind(wx.EVT_TEXT_ENTER, self.on_broadcast)
        self.broadcast_btn.Bind(wx.EVT_BUTTON, self.on_broadcast)
        
        csizer.Add(self.broadcast_input, 1, wx.EXPAND | wx.ALL, 5)
        csizer.Add(self.broadcast_btn, 0, wx.ALL, 5)
        self.sizer.Add(csizer, 0, wx.ALL | wx.EXPAND, 10)

        # --- SYSTEM UTILITIES UI ---
        ubox = wx.StaticBox(self.panel, label="System Utilities")
        usizer = wx.StaticBoxSizer(ubox, wx.HORIZONTAL)
        self.btn_api = wx.Button(self.panel, label="Check API")
        self.btn_api.Bind(wx.EVT_BUTTON, self.on_api)
        self.btn_batt = wx.Button(self.panel, label="Check Batteries")
        self.btn_batt.Bind(wx.EVT_BUTTON, self.on_batt)
        usizer.Add(self.btn_api, 1, wx.ALL, 5)
        usizer.Add(self.btn_batt, 1, wx.ALL, 5)
        self.sizer.Add(usizer, 0, wx.ALL | wx.EXPAND, 10)

        self.btn_min = wx.Button(self.panel, label="Minimize to Tray")
        self.btn_min.Bind(wx.EVT_BUTTON, self.on_minimize)
        self.sizer.Add(self.btn_min, 0, wx.ALL | wx.EXPAND, 10)

        self.btn_exit = wx.Button(self.panel, label="Exit Application", size=(-1, 40))
        self.btn_exit.Bind(wx.EVT_BUTTON, self.on_quit)
        self.sizer.Add(self.btn_exit, 0, wx.ALL | wx.EXPAND, 10)

    def _safe_speak(self, msg):
        if self.sr:
            try:
                self.sr.output(msg)
            except Exception as e:
                logging.error(f"[TTS COM ERROR] {e}")

    # --- PROMPT EDITOR EVENT HANDLERS ---
    def on_prompt_change(self, event):
        new_prompt = self.prompt_choice.GetStringSelection()
        self.config["active_prompt"] = new_prompt
        self.save_config()
        self.prompt_editor.SetValue(self.config["prompts"][new_prompt])
        self.notify(f"Loaded {new_prompt} profile", priority=5)

    def on_save_prompt(self, event):
        current_name = self.prompt_choice.GetStringSelection()
        new_text = self.prompt_editor.GetValue().strip()
        if not new_text:
            self.notify("Prompt cannot be empty.", priority=5)
            return
        
        self.config["prompts"][current_name] = new_text
        self.save_config()
        self.notify(f"Saved changes to {current_name}", priority=5)

    def on_new_prompt(self, event):
        name = wx.GetTextFromUser("Enter a name for the new prompt profile:", "New Prompt Profile")
        if not name: return
        
        if name in self.config["prompts"]:
            self.notify("A profile with that name already exists.", priority=5)
            return

        default_text = "Analyze frames for security. Describe people and actions."
        self.config["prompts"][name] = default_text
        self.config["active_prompt"] = name
        self.save_config()

        self.prompt_choice.Append(name)
        self.prompt_choice.SetStringSelection(name)
        self.prompt_editor.SetValue(default_text)
        self.prompt_editor.SetFocus()
        self.notify(f"Created new profile: {name}", priority=5)

    def on_del_prompt(self, event):
        current_name = self.prompt_choice.GetStringSelection()
        
        if len(self.config["prompts"]) <= 1:
            self.notify("Cannot delete the last remaining prompt profile.", priority=5)
            return

        dlg = wx.MessageDialog(self, f"Are you sure you want to delete the '{current_name}' profile?", "Confirm Delete", wx.YES_NO | wx.ICON_WARNING)
        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_YES:
            del self.config["prompts"][current_name]
            
            new_active = list(self.config["prompts"].keys())[0]
            self.config["active_prompt"] = new_active
            self.save_config()

            self.prompt_choice.Clear()
            self.prompt_choice.AppendItems(list(self.config["prompts"].keys()))
            self.prompt_choice.SetStringSelection(new_active)
            self.prompt_editor.SetValue(self.config["prompts"][new_active])
            
            self.notify(f"Deleted {current_name}. Active profile is now {new_active}.", priority=5)

    # --- SPEAKER LIST EVENT HANDLERS ---
    def refresh_speaker_list(self):
        self.speaker_list.Clear()
        speakers = self.config.get("speakers", {})
        for name, data in speakers.items():
            idx = self.speaker_list.Append(f"{name} ({data['type'].upper()})")
            self.speaker_list.Check(idx, data.get("enabled", True))
            self.speaker_list.SetClientData(idx, name)

    def on_speaker_select(self, event):
        try:
            idx = event.GetInt()
            if idx != wx.NOT_FOUND and idx < self.speaker_list.GetCount():
                name = self.speaker_list.GetString(idx)
                state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
                wx.CallAfter(self._safe_speak, f"{name}, {state}")
        except Exception as e:
            logging.error(f"[UI ERROR] {e}")
        finally:
            event.Skip()

    def on_speaker_focus(self, event):
        try:
            idx = self.speaker_list.GetSelection()
            if idx != wx.NOT_FOUND and idx < self.speaker_list.GetCount():
                name = self.speaker_list.GetString(idx)
                state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
                wx.CallAfter(self._safe_speak, f"Speaker Targets. {name}, {state}")
        except Exception as e:
            logging.error(f"[UI ERROR] {e}")
        finally:
            event.Skip()

    def on_speaker_toggle(self, event):
        idx = event.GetInt()
        name = self.speaker_list.GetClientData(idx)
        is_checked = self.speaker_list.IsChecked(idx)
        self.config["speakers"][name]["enabled"] = is_checked
        self.save_config()
        
        status_msg = f"{name} {'enabled' if is_checked else 'disabled'}"
        
        self.notify(status_msg, priority=10)
        spk_type = self.config["speakers"][name]["type"]
        spk_id = self.config["speakers"][name]["id"]
        executor.submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)

    def on_add_speaker(self, event):
        name = wx.GetTextFromUser("Enter a friendly name for the speaker:", "Add Speaker")
        if not name: return
        
        dlg = wx.SingleChoiceDialog(self, "Select speaker type:", "Speaker Type", ["sonos", "ha", "alexa"])
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy(); return
        spk_type = dlg.GetStringSelection()
        dlg.Destroy()

        spk_id = wx.GetTextFromUser("Enter the IP address (Sonos) or Entity ID (HA/Alexa):", "Add Speaker")
        if not spk_id: return

        self.config["speakers"][name] = {"id": spk_id, "type": spk_type, "enabled": True}
        self.save_config()
        self.refresh_speaker_list()
        self.notify(f"Added {name}", priority=10)

    def on_remove_speaker(self, event):
        idx = self.speaker_list.GetSelection()
        if idx == wx.NOT_FOUND:
            self.notify("Select a speaker to remove.", priority=10)
            return
        name = self.speaker_list.GetClientData(idx)
        del self.config["speakers"][name]
        self.save_config()
        self.refresh_speaker_list()
        self.notify(f"Removed {name}", priority=10)

    # --- CORE UI LOGIC ---
    def notify(self, text, priority=5, interrupt=False):
        logging.info(f"[NOTIFY] P{priority}: {text}")
        if priority <= 3 or self.speech_queue.qsize() < 2:
            wx.CallAfter(self.status_display.SetValue, text)

        with self.speech_lock:
            if interrupt:
                while not self.speech_queue.empty():
                    try: self.speech_queue.get_nowait()
                    except Empty: break
            self._msg_counter += 1
            self.speech_queue.put((priority, time.time(), self._msg_counter, text))

    def speech_worker(self):
        while self.running:
            try:
                priority, _, _, msg = self.speech_queue.get(timeout=0.02)
                with self.speech_lock:
                    queue_snapshot = list(self.speech_queue.queue)
                
                q_size = len(queue_snapshot)
                has_urgent = any(p <= 2 for p, _, _, _ in queue_snapshot)

                if priority > 5 and (has_urgent or q_size > 5):
                    continue

                if msg:
                    wx.CallAfter(self._safe_speak, msg)
            except Empty: continue

    def on_toggle_arm(self, event):
        self.is_armed = not self.is_armed
        self.config["is_armed"] = self.is_armed
        self.save_config()
        self.btn_arm.SetLabel("Disarm System" if self.is_armed else "Arm System")
        
        status_msg = f"Viper Vision System {'Armed' if self.is_armed else 'Disarmed'}"
        
        self.notify(status_msg, priority=1, interrupt=True)
        executor.submit(audio.announce_all, status_msg)
        executor.submit(audio.sonos_speak_verdict, status_msg)

    def on_broadcast(self, event):
        message = self.broadcast_input.GetValue().strip()
        if not message: return
        self.broadcast_input.Clear()
        
        self.notify(f"Broadcasting: {message}", priority=3, interrupt=True)
        
        executor.submit(audio.announce_all, message)
        executor.submit(audio.sonos_speak_verdict, message)

    def on_api(self, event):
        self.notify("Checking API usage...", priority=10)
        executor.submit(self._run_api)

    def _run_api(self):
        try:
            with open(cfg.API_LOG_PATH, "r") as f:
                data = json.load(f)

            reqs = data.get("total_requests", 0)
            cost = (data.get("prompt_tokens", 0) * cfg.COST_PER_INPUT_TOKEN) + \
                   (data.get("response_tokens", 0) * cfg.COST_PER_OUTPUT_TOKEN)

            self.notify(f"API usage: {reqs} requests. Cost: ${cost:.4f}", priority=10)
        except Exception as e:
            self.notify("API log unavailable.", priority=10)
            logging.error(f"API query failed: {e}")

    def on_batt(self, event):
        self.notify("Checking battery levels...", priority=10)
        executor.submit(self._run_batt)

    def _run_batt(self):
        try:
            r = requests.get(f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/states", headers={"Authorization": f"Bearer {cfg.HA_TOKEN}"}, timeout=5)
            r.raise_for_status()
            results = [f"{s['attributes'].get('friendly_name', s['entity_id'])}: {float(s.get('state', 0)):.0f}%" 
                       for s in r.json() if s.get("entity_id", "").lower() in cfg.TARGET_BATTERY_ENTITIES]
            self.notify(", ".join(results) if results else "No battery sensors found.", priority=10)
        except Exception as e: 
            self.notify("Battery query failed.", priority=10)
            logging.error(f"Battery request failed: {e}")

    def on_minimize(self, event):
        if isinstance(event, wx.CloseEvent) and event.CanVeto(): event.Veto()
        self.notify("Viper Vision minimized to tray.", priority=10)
        wx.CallLater(500, self.Hide)

    def on_quit(self, event):
        self.running = False
        executor.shutdown(wait=False)
        self.tb_icon.RemoveIcon()
        self.tb_icon.Destroy()
        self.Destroy()
        wx.Exit()

# ==========================================
# RUN SEQUENCE
# ==========================================
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=[
            logging.FileHandler(cfg.LOG_FILE, mode='a'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    audio.startup_cleanup()
    
    executor.submit(audio.start_local_server)
    executor.submit(run_flask_server)
    time.sleep(0.5)
    
    logging.info("=== VIPER VISION: UNIFIED GLOBAL SWITCH ONLINE ===")
    
    gui_app = wx.App(False)
    dash_app = ViperDashboard()
    gui_app.MainLoop()