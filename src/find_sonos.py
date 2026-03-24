import soco
from soco.discovery import discover
import requests

# Your Pushover credentials (copy these from your viper_config.json)
USER_KEY = "YOUR_USER_KEY"
API_TOKEN = "YOUR_API_TOKEN"

def send_to_phone(message):
    url = "https://api.pushover.net/1/messages.json"
    data = {
        "token": API_TOKEN,
        "user": USER_KEY,
        "message": message,
        "title": "📡 Sonos Network Scan"
    }
    requests.post(url, data=data)

def scan_network():
    print("Scanning for Sonos speakers... this takes about 10 seconds.")
    speakers = discover()
    
    if not speakers:
        send_to_phone("Scan Complete: No Sonos speakers found on the network. Check if the PC is on the same WiFi as the speakers.")
        return

    report = "Found the following speakers:\n\n"
    for speaker in speakers:
        report += f"🔊 {speaker.player_name}\n📍 IP: {speaker.ip_address}\n\n"
    
    print(report)
    send_to_phone(report)

if __name__ == "__main__":
    scan_network()