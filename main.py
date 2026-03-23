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
from datetime import datetime

from flask import Flask, request, render_template, redirect, url_for, flash
from waitress import serve

from accessible_output2.outputs import auto
import viper_config as cfg
import viper_audio as audio
import viper_vision as vision

# ==========================================
# GLOBAL APP & STATE
# ==========================================
executor = ThreadPoolExecutor(max_workers=10)
app = Flask(__name__, template_folder=str(cfg.BASE_DIR / "templates"))
app.secret_key = "viper_vision_secure_key" 
dash_app = None  
activity_logs = []

# ==========================================
# FLASK WEB & REMOTE ROUTES
# ==========================================
@app.route('/')
def index():
    return redirect(url_for('remote_ui'))

@app.route('/doorbell-webhook', methods=['POST'])
def handle_front():
    executor.submit(vision.process_doorbell, "front door", cfg.RTSP_FRONT, "front", dash_app, executor)
    return "OK", 200

@app.route('/doorbell-webhook/back', methods=['POST'])
def handle_back():
    executor.submit(vision.process_doorbell, "back door", cfg.RTSP_BACK, "back", dash_app, executor)
    return "OK", 200

@app.route('/remote')
def remote_ui():
    return render_template('remote.html', config=dash_app.config, activity_logs=activity_logs)

@app.route('/remote/toggle', methods=['POST'])
def web_toggle_arm():
    dash_app.on_toggle_arm(None)
    status = "Armed" if dash_app.is_armed else "Disarmed"
    flash(f"System {status} successfully.")
    return redirect(url_for('remote_ui'))

# --- WEB SPEAKER MANAGEMENT ---

@app.route('/remote/speaker/toggle/<name>', methods=['POST'])
def web_speaker_toggle(name):
    if name in dash_app.config["speakers"]:
        current = dash_app.config["speakers"][name]["enabled"]
        new_state = not current
        dash_app.config["speakers"][name]["enabled"] = new_state
        dash_app.save_config()
        
        status_msg = f"{name} {'enabled' if new_state else 'disabled'}"
        wx.CallAfter(dash_app.notify, f"{status_msg} via web", priority=10)
        wx.CallAfter(dash_app.refresh_speaker_list)
        
        spk_type = dash_app.config["speakers"][name]["type"]
        spk_id = dash_app.config["speakers"][name]["id"]
        executor.submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)
        
        flash(f"Speaker {status_msg}")
    return redirect(url_for('remote_ui'))

@app.route('/remote/speaker/test/<name>', methods=['POST'])
def web_speaker_test(name):
    if name in dash_app.config["speakers"]:
        spk = dash_app.config["speakers"][name]
        status = f"Testing connection to {name}."
        wx.CallAfter(dash_app.notify, status, priority=10)
        executor.submit(audio.announce_specific_speaker, spk["type"], spk["id"], status)
        flash(f"Sent test chime to {name}")
    return redirect(url_for('remote_ui'))

@app.route('/remote/speaker/add', methods=['POST'])
def web_speaker_add():
    # .strip() removes accidental leading or trailing spaces
    name = request.form.get("name", "").strip()
    spk_type = request.form.get("type")
    spk_id = request.form.get("id", "").strip()
    
    if name and spk_id:
        # We also strip the name here just to be 100% safe
        dash_app.config["speakers"][name] = {"id": spk_id, "type": spk_type, "enabled": True}
        dash_app.save_config()
        
        # This tells the audio engine to recognize the new IP immediately
        cfg.sync_globals_from_config()
        
        wx.CallAfter(dash_app.notify, f"Added speaker {name}")
        wx.CallAfter(dash_app.refresh_speaker_list)
        flash(f"Speaker {name} added.")
        
    return redirect(url_for('remote_ui'))
@app.route('/remote/speaker/rename/<old_name>/<new_name>')
def web_speaker_rename(old_name, new_name):
    if old_name in dash_app.config["speakers"] and new_name:
        data = dash_app.config["speakers"].pop(old_name)
        dash_app.config["speakers"][new_name] = data
        dash_app.save_config()
        wx.CallAfter(dash_app.notify, f"Renamed {old_name} to {new_name}")
        wx.CallAfter(dash_app.refresh_speaker_list)
        flash(f"Renamed {old_name} to {new_name}")
    return redirect(url_for('remote_ui'))

@app.route('/remote/speaker/delete/<name>', methods=['POST'])
def web_speaker_delete(name):
    if name in dash_app.config["speakers"]:
        del dash_app.config["speakers"][name]
        dash_app.save_config()
        wx.CallAfter(dash_app.notify, f"Removed speaker {name}")
        wx.CallAfter(dash_app.refresh_speaker_list)
        flash(f"Speaker {name} deleted.")
    return redirect(url_for('remote_ui'))

# --- WEB PROMPT MANAGEMENT ---

@app.route('/remote/switch_prompt', methods=['POST'])
def web_switch_prompt():
    new_p = request.form.get("profile_name")
    dash_app.config["active_prompt"] = new_p
    wx.CallAfter(dash_app.prompt_choice.SetStringSelection, new_p)
    wx.CallAfter(dash_app.prompt_editor.SetValue, dash_app.config["prompts"][new_p])
    dash_app.save_config()
    flash(f"Switched to {new_p} profile.")
    return redirect(url_for('remote_ui'))

@app.route('/remote/save_prompt', methods=['POST'])
def web_save_prompt():
    new_text = request.form.get("prompt_text")
    active_p = dash_app.config["active_prompt"]
    dash_app.config["prompts"][active_p] = new_text
    wx.CallAfter(dash_app.prompt_editor.SetValue, new_text)
    dash_app.save_config()
    flash("AI instructions saved.")
    return redirect(url_for('remote_ui'))

# --- WEB UTILITIES ---

@app.route('/remote/utils/api', methods=['POST'])
def web_api_check():
    dash_app.on_api(None)
    flash("API Check requested. Listen for announcement.")
    return redirect(url_for('remote_ui'))

@app.route('/remote/utils/batt', methods=['POST'])
def web_batt_check():
    dash_app.on_batt(None)
    flash("Battery Check requested. Listen for announcement.")
    return redirect(url_for('remote_ui'))

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
        # FIX: Explicitly set focus to the main Arm button so JAWS starts reading immediately
        self.frame.btn_arm.SetFocus()

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

        # AI PROMPT EDITOR UI
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

        # SPEAKER LIST UI
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
        self.btn_ren_spk = wx.Button(self.panel, label="Rename Selected")
        self.btn_ren_spk.Bind(wx.EVT_BUTTON, self.on_rename_speaker)
        self.btn_rem_spk = wx.Button(self.panel, label="Remove Selected")
        self.btn_rem_spk.Bind(wx.EVT_BUTTON, self.on_remove_speaker)
        btn_sizer.Add(self.btn_add_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_ren_spk, 1, wx.ALL, 5)
        btn_sizer.Add(self.btn_rem_spk, 1, wx.ALL, 5)
        ssizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 0)
        self.sizer.Add(ssizer, 1, wx.ALL | wx.EXPAND, 10)
        self.refresh_speaker_list()

        # MANUAL BROADCAST UI
        cbox = wx.StaticBox(self.panel, label="Manual Intercom Broadcast")
        csizer = wx.StaticBoxSizer(cbox, wx.HORIZONTAL)
        self.broadcast_input = wx.TextCtrl(self.panel, style=wx.TE_PROCESS_ENTER)
        self.broadcast_btn = wx.Button(self.panel, label="Speak")
        self.broadcast_input.Bind(wx.EVT_TEXT_ENTER, self.on_broadcast)
        self.broadcast_btn.Bind(wx.EVT_BUTTON, self.on_broadcast)
        csizer.Add(self.broadcast_input, 1, wx.EXPAND | wx.ALL, 5)
        csizer.Add(self.broadcast_btn, 0, wx.ALL, 5)
        self.sizer.Add(csizer, 0, wx.ALL | wx.EXPAND, 10)

        # UTILITIES UI
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
            try: self.sr.output(msg)
            except: pass

    # --- PROMPT HANDLERS ---
    def on_prompt_change(self, event):
        new_prompt = self.prompt_choice.GetStringSelection()
        self.config["active_prompt"] = new_prompt
        self.save_config()
        self.prompt_editor.SetValue(self.config["prompts"][new_prompt])
        self.notify(f"Loaded {new_prompt} profile")

    def on_save_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        txt = self.prompt_editor.GetValue().strip()
        if txt:
            self.config["prompts"][name] = txt
            self.save_config()
            self.notify(f"Saved {name}")

    def on_new_prompt(self, event):
        name = wx.GetTextFromUser("New Prompt Name:", "New Profile")
        if name and name not in self.config["prompts"]:
            self.config["prompts"][name] = "Analyze frames for security."
            self.config["active_prompt"] = name
            self.save_config()
            self.prompt_choice.Append(name)
            self.prompt_choice.SetStringSelection(name)
            self.prompt_editor.SetValue(self.config["prompts"][name])
            self.notify(f"Created {name}")

    def on_del_prompt(self, event):
        name = self.prompt_choice.GetStringSelection()
        if len(self.config["prompts"]) > 1:
            del self.config["prompts"][name]
            new_a = list(self.config["prompts"].keys())[0]
            self.config["active_prompt"] = new_a
            self.save_config()
            self.prompt_choice.Clear()
            self.prompt_choice.AppendItems(list(self.config["prompts"].keys()))
            self.prompt_choice.SetStringSelection(new_a)
            self.prompt_editor.SetValue(self.config["prompts"][new_a])

    # --- SPEAKER HANDLERS ---
    def refresh_speaker_list(self):
        self.speaker_list.Clear()
        for name, data in self.config.get("speakers", {}).items():
            idx = self.speaker_list.Append(f"{name} ({data['type'].upper()})")
            self.speaker_list.Check(idx, data.get("enabled", True))
            self.speaker_list.SetClientData(idx, name)

    def on_speaker_select(self, event):
        idx = event.GetInt()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetString(idx)
            state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
            wx.CallAfter(self._safe_speak, f"{name}, {state}")

    def on_speaker_focus(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetString(idx)
            state = "Checked" if self.speaker_list.IsChecked(idx) else "Unchecked"
            wx.CallAfter(self._safe_speak, f"Speaker Targets. {name}, {state}")

    def on_speaker_toggle(self, event):
        idx = event.GetInt()
        name = self.speaker_list.GetClientData(idx)
        is_chk = self.speaker_list.IsChecked(idx)
        self.config["speakers"][name]["enabled"] = is_chk
        self.save_config()
        # FIX: Also trigger a global sync so audio.py sees the toggle
        cfg.sync_globals_from_config()
        
        status_msg = f"{name} {'enabled' if is_chk else 'disabled'}"
        self.notify(status_msg, priority=10)
        
        spk_type = self.config["speakers"][name]["type"]
        spk_id = self.config["speakers"][name]["id"]
        executor.submit(audio.announce_specific_speaker, spk_type, spk_id, status_msg)

    def on_add_speaker(self, event):
        name = wx.GetTextFromUser("Speaker Name:", "Add")
        if name:
            dlg = wx.SingleChoiceDialog(self, "Type:", "Add", ["sonos", "ha", "alexa"])
            if dlg.ShowModal() == wx.ID_OK:
                t = dlg.GetStringSelection()
                i = wx.GetTextFromUser("ID/IP:", "Add")
                if i:
                    self.config["speakers"][name] = {"id": i, "type": t, "enabled": True}
                    self.save_config()
                    cfg.sync_globals_from_config()
                    self.refresh_speaker_list()
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
                cfg.sync_globals_from_config()
                self.refresh_speaker_list()

    def on_remove_speaker(self, event):
        idx = self.speaker_list.GetSelection()
        if idx != wx.NOT_FOUND:
            name = self.speaker_list.GetClientData(idx)
            del self.config["speakers"][name]
            self.save_config()
            cfg.sync_globals_from_config()
            self.refresh_speaker_list()

    # --- CORE ---
    def notify(self, text, priority=5, interrupt=False):
        timestamp = datetime.now().strftime("%H:%M")
        activity_logs.insert(0, {"time": timestamp, "msg": text})
        if len(activity_logs) > 15: activity_logs.pop()
        
        wx.CallAfter(self._safe_speak, text)
        
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
                p, _, _, msg = self.speech_queue.get(timeout=0.02)
                with self.speech_lock: q = list(self.speech_queue.queue)
                if p > 5 and (any(x[0] <= 2 for x in q) or len(q) > 5): continue
                if msg: wx.CallAfter(self._safe_speak, msg)
            except Empty: continue

    def on_toggle_arm(self, event):
        self.is_armed = not self.is_armed
        self.config["is_armed"] = self.is_armed
        self.save_config()
        self.btn_arm.SetLabel("Disarm System" if self.is_armed else "Arm System")
        msg = f"Viper Vision {'Armed' if self.is_armed else 'Disarmed'}"
        self.notify(msg, priority=1, interrupt=True)
        executor.submit(audio.announce_all, msg)

    def on_broadcast(self, event):
        msg = self.broadcast_input.GetValue().strip()
        if msg:
            self.broadcast_input.Clear()
            self.notify(f"Broadcasting: {msg}", priority=3, interrupt=True)
            executor.submit(audio.announce_all, msg)

    def on_api(self, event):
        executor.submit(self._run_api)

    def _run_api(self):
        try:
            with open(cfg.API_LOG_PATH, "r") as f:
                data = json.load(f)
            
            reqs = data.get("total_requests", 0)
            cost = (data.get("prompt_tokens", 0) * cfg.COST_PER_INPUT_TOKEN) + \
                   (data.get("response_tokens", 0) * cfg.COST_PER_OUTPUT_TOKEN)
            
            day_of_month = datetime.now().day
            days_in_month = 30 
            projected = (cost / max(1, day_of_month)) * days_in_month
            
            msg = f"API: {reqs} requests. Spent: ${cost:.4f}. Projected Monthly: ${projected:.2f}"
            self.notify(msg, priority=10)
        except Exception as e:
            self.notify("API log unavailable.", priority=10)
            logging.error(f"API Error: {e}")

    def on_batt(self, event):
        executor.submit(self._run_batt)

    def _run_batt(self):
        try:
            r = requests.get(f"http://{cfg.HA_IP}:{cfg.HA_PORT}/api/states", headers={"Authorization": f"Bearer {cfg.HA_TOKEN}"}, timeout=5)
            r.raise_for_status()
            
            # Use dictionary to deduplicate entities with the same Friendly Name
            seen_batteries = {}
            for s in r.json():
                if s.get("entity_id") in cfg.TARGET_BATTERY_ENTITIES:
                    name = s['attributes'].get('friendly_name', s['entity_id'])
                    name = name.replace(" Battery", "").strip() # Clean up names like "Front Door Battery Battery"
                    
                    try: val = float(s.get('state', 0))
                    except ValueError: val = 0
                    
                    seen_batteries[name] = f"{name}: {val:.0f}%"
                    
            res = list(seen_batteries.values())
            self.notify(", ".join(res) if res else "No battery sensors found.", priority=10)
            
        except Exception as e:
            self.notify("Battery query failed.", priority=10)
            logging.error(f"Battery Error: {e}")

    def on_minimize(self, event):
        if isinstance(event, wx.CloseEvent) and event.CanVeto(): event.Veto()
        wx.CallLater(500, self.Hide)

    def on_quit(self, event):
        self.running = False
        executor.shutdown(wait=False)
        self.tb_icon.RemoveIcon()
        self.tb_icon.Destroy()
        self.Destroy()
        wx.Exit()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    audio.startup_cleanup()
    executor.submit(audio.start_local_server)
    executor.submit(run_flask_server)
    time.sleep(0.5)
    gui_app = wx.App(False)
    dash_app = ViperDashboard()
    gui_app.MainLoop()