import pytest
import json
import os
import time
import concurrent.futures
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import the Viper modules
import main
import viper_config as cfg
import viper_audio as audio
import viper_vision as vision

# ==========================================
# 1. CONFIGURATION TESTS
# ==========================================
def test_config_load_save(tmp_path):
    test_file = tmp_path / "test_config.json"
    with patch("viper_config.CONFIG_FILE", test_file):
        initial_data = cfg.get_default_config()
        cfg.save_config(initial_data)
        assert test_file.exists()
        loaded_data = cfg.load_config()
        assert loaded_data["active_prompt"] == "Standard"
        assert "Office Sonos" in loaded_data["speakers"]

def test_sync_globals():
    mock_data = {
        "speakers": {
            "Test Sonos": {"id": "1.2.3.4", "type": "sonos", "enabled": True},
            "Test HA": {"id": "media_player.test", "type": "ha", "enabled": True}
        }
    }
    with patch("json.load", return_value=mock_data), patch("builtins.open", MagicMock()):
        cfg.sync_globals_from_config()
        assert "1.2.3.4" in cfg.SONOS_IPS
        assert "media_player.test" in cfg.TARGET_SPEAKERS

# ==========================================
# 2. WEB INTERFACE (FLASK) TESTS - CORE
# ==========================================
@pytest.fixture
def client():
    main.app.config['TESTING'] = True
    main.dash_app = MagicMock()
    main.dash_app.config = cfg.get_default_config()
    main.dash_app.is_armed = True
    with main.app.test_client() as client:
        yield client

def test_remote_ui_loading(client):
    rv = client.get('/remote')
    assert rv.status_code == 200
    assert b"Viper Vision Remote" in rv.data

@patch("wx.CallAfter")
def test_web_speaker_add(mock_wx, client):
    rv = client.post('/remote/speaker/add', data={"name": " Garage  ", "type": "sonos", "id": " 192.168.4.28 "}, follow_redirects=True)
    assert rv.status_code == 200
    assert "Garage" in main.dash_app.config["speakers"]

# ==========================================
# 3. AUDIO & HARDWARE LOGIC TESTS
# ==========================================
@patch("requests.post")
def test_ha_tts_logic(mock_post):
    audio.announce_specific_speaker("ha", "media_player.test", "Hello World")
    args, kwargs = mock_post.call_args
    assert "api/services/tts/speak" in args[0]
    assert kwargs['json']['media_player_entity_id'] == "media_player.test"

@patch("soco.SoCo")
def test_sonos_playback_logic(mock_soco):
    instance = mock_soco.return_value
    audio.announce_specific_speaker("sonos", "1.2.3.4", "Test message")
    instance.unjoin.assert_called()
    assert instance.volume == 45
    instance.play_uri.assert_called()

# ==========================================
# 4. VISION & AI PIPELINE TESTS
# ==========================================
@patch("subprocess.run")
def test_ffmpeg_capture(mock_run):
    vision.grab_flipbook_frames("rtsp://test", "test_path")
    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd
    assert "tcp" in cmd

@patch("requests.post")
@patch("viper_vision.log_api_usage") 
@patch("viper_vision.Image.open", return_value=MagicMock())
@patch("viper_vision.Path.unlink")
@patch("viper_vision.client.models.generate_content")
def test_ai_verdict_logic(mock_gemini, mock_unlink, mock_img_open, mock_log, mock_post):
    mock_response = MagicMock()
    mock_response.text = "A person in a red shirt is at the door."
    mock_gemini.return_value = mock_response
    mock_dash = MagicMock()
    
    with patch("viper_vision.grab_flipbook_frames", return_value=["f1.jpg"]):
        vision.process_doorbell("front", "rtsp://url", "front", mock_dash, MagicMock())
        
    mock_dash.notify.assert_any_call("A person in a red shirt is at the door.", priority=1, interrupt=True)

# ==========================================
# 5. STRESS & THREAD SAFETY TESTS
# ==========================================
@patch("requests.post") 
@patch("viper_vision.log_api_usage") 
@patch("viper_audio.sonos_speak_verdict") 
@patch("viper_audio.sonos_instant_chime") 
@patch("viper_vision.Image.open", return_value=MagicMock())
@patch("viper_vision.Path.unlink")
@patch("viper_vision.client.models.generate_content")
@patch("viper_vision.grab_flipbook_frames", return_value=["dummy.jpg"])
@patch("viper_audio.announce_all")
@patch("builtins.open", new_callable=MagicMock)
def test_doorbell_spam_protection(mock_open, mock_announce, mock_grab, mock_ai, mock_unlink, mock_img_open, mock_chime, mock_sonos_speak, mock_log, mock_post):
    vision.last_trigger["front"] = 0
    def mash_button(): return main.handle_front()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as thread_pool:
        futures = [thread_pool.submit(mash_button) for _ in range(10)]
        for f in futures: assert f.result() == ("OK", 200)

    time.sleep(1)
    assert mock_ai.call_count == 1
    assert mock_announce.call_count == 2

# ==========================================
# 6. SECONDARY WEB ROUTES & PROMPTS
# ==========================================
@patch("wx.CallAfter")
def test_web_speaker_rename_and_delete(mock_wx, client):
    # Test Rename
    rv = client.get('/remote/speaker/rename/Office%20Sonos/Man%20Cave')
    assert rv.status_code == 302
    assert "Man Cave" in main.dash_app.config["speakers"]
    assert "Office Sonos" not in main.dash_app.config["speakers"]

    # Test Delete
    rv2 = client.post('/remote/speaker/delete/Man Cave')
    assert rv2.status_code == 302
    assert "Man Cave" not in main.dash_app.config["speakers"]

@patch("wx.CallAfter")
def test_web_prompt_management(mock_wx, client):
    # Test Switch Prompt
    client.post('/remote/switch_prompt', data={"profile_name": "Detailed"})
    assert main.dash_app.config["active_prompt"] == "Detailed"

    # Test Save Prompt
    client.post('/remote/save_prompt', data={"prompt_text": "New AI rules here."})
    assert main.dash_app.config["prompts"]["Detailed"] == "New AI rules here."

# ==========================================
# 7. UTILITIES: API MATH & BATTERY PARSER
# ==========================================
@patch("builtins.open")
@patch("json.load")
@patch("main.datetime")
def test_api_cost_math(mock_datetime, mock_json_load, mock_open):
    """Verifies the token cost multiplier and monthly projection math."""
    mock_json_load.return_value = {
        "total_requests": 150,
        "prompt_tokens": 1_000_000, # At $0.10 per 1M = $0.10
        "response_tokens": 500_000  # At $0.40 per 1M = $0.20
        # Total calculated cost should be $0.30
    }
    # Pretend today is the 10th of the month
    mock_datetime.now.return_value.day = 10 
    
    # We create a dummy class to catch the output of the math function
    class DummyDash:
        def notify(self, msg, priority):
            self.last_msg = msg
    
    dummy = DummyDash()
    main.ViperDashboard._run_api(dummy) # Run the math logic directly
    
    # Verify the results: Spent $0.30, Projected: ($0.30 / 10 days) * 30 days = $0.90
    assert "Spent: $0.30" in dummy.last_msg
    assert "Projected Monthly: $0.90" in dummy.last_msg

@patch("requests.get")
def test_battery_parser(mock_get):
    """Ensures Home Assistant battery data is parsed and cleaned correctly."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"entity_id": "sensor.front_door_battery", "state": "85", "attributes": {"friendly_name": "Front Door Battery"}},
        {"entity_id": "sensor.back_door_battery", "state": "42", "attributes": {"friendly_name": "Back Door Battery"}}
    ]
    mock_get.return_value = mock_response

    class DummyDash:
        def notify(self, msg, priority):
            self.last_msg = msg
            
    dummy = DummyDash()
    main.ViperDashboard._run_batt(dummy)
    
    # Ensure it cleaned up the " Battery" text and formatted the percentages
    assert "Front Door: 85%" in dummy.last_msg
    assert "Back Door: 42%" in dummy.last_msg

# ==========================================
# 8. GARBAGE COLLECTOR
# ==========================================
def test_audio_cleanup_sweeper(tmp_path):
    """Verifies the startup cleaner successfully deletes old mp3 files."""
    # Create a fake mp3 file in our temporary test directory
    fake_mp3 = tmp_path / "stale_alert.mp3"
    fake_mp3.touch()
    
    # Tell the audio system that our temp directory is the real Sonos directory
    with patch("viper_config.SONOS_AUDIO_DIR", tmp_path):
        audio.startup_cleanup()
        
        # The file should be completely gone
        assert not fake_mp3.exists()