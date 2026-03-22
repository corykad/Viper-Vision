# 🐍 Viper Vision v1.0
### AI-Powered Local Security & Multi-Room Voice Assistant

Viper Vision is a high-performance, modular security engine that transforms standard RTSP camera feeds into an intelligent, talking home security system. Using **Gemini 2.5 Flash**, it analyzes "flipbook" motion captures and broadcasts natural language descriptions across **Sonos**, **Alexa**, and **Home Assistant** speakers.

---

## ⚠️ CRITICAL REALITY CHECK BEFORE INSTALLING ⚠️
**This is NOT a "plug-and-play" setup.** 

Version 1.0 provides the core AI executable, the accessible dashboard, and the local audio routing. However, it does **not** include an automated installer for the backend. **You must have advanced knowledge of Docker and Home Assistant to use this software.** 

You will be personally responsible for mapping your own Entity IDs, installing custom Home Assistant components, finding your camera's RTSP streams, and writing the automations to trigger the AI. 

If you are a power user ready to build the ultimate AI security system, follow the guide below.

---

## 🚀 Key Features
* **AI Flipbook Analysis:** Instead of a single blurry snapshot, Viper grabs 3 high-speed frames to understand movement, intent, and context (e.g., "A delivery driver is placing a package by the door" vs. "Daisy is watching Jamison play in the front yard").
* **Multi-Layered Audio Routing:** Local Sonos MP3 hosting, Alexa cloud announcements, and native Home Assistant TTS.
* **Unified Dashboard:** A fully accessible `wxPython` interface for real-time monitoring and manual intercom broadcasts.

---

## 🛠️ Phase 1: Prerequisites
1. **Windows 10/11** computer to run the `ViperVision.exe` dashboard.
2. **Docker Desktop** installed and running on your network (ensure WSL 2 is enabled in Docker settings).
3. **FFmpeg** installed and added to your Windows System PATH.

---

## 🐳 Phase 2: Spin Up the Backend Stack
Viper Vision requires Home Assistant, an MQTT Broker (Mosquitto), and the Ring-MQTT bridge.

1. Download the `docker-compose.yml` file from this repository.
2. Place it in a dedicated folder (e.g., `C:\viper`).
3. Open PowerShell, navigate to that folder (`cd C:\viper`), and start the stack:
   ```powershell
   docker compose up -d
Wait a few minutes for the containers to download and initialize.

🔐 Phase 3: Authenticate Ring Cameras
In PowerShell, run this command to enter the authentication tool:

PowerShell
docker exec -it ring-mqtt ring-mqtt-auth-cli
Enter your Ring account email, password, and the 2FA code sent to your phone.

📡 Phase 4: Find Your RTSP URLs
Open Home Assistant (http://localhost:8123).

Go to Settings -> Devices & Services -> MQTT.

Click on your Ring Camera device -> Diagnostic -> click the Info sensor.

Expand Attributes and copy the stream_Source URL.
(Note: Replace the internal Docker hostname like 03cabcc9-ring-mqtt with the actual local IP address of your Docker host).

⚙️ Phase 5: Home Assistant Configuration
Create an Access Token: Go to your HA Profile -> Security -> Long-Lived Access Tokens. Create one named "Viper" and save it.

Alexa Support (Optional): Manually install HACS and the alexa_media_player custom integration.

Configure Viper: Open viper_config.json. Paste in your RTSP URLs and HA speaker Entity IDs.

📱 Phase 6: Pushover Mobile Alerts (Required)
Viper Vision uses Pushover to send critical AI alerts and images to your phone, bypassing the native Ring app.

Create a free account at Pushover.net and download the mobile app.

Copy your User Key from the main dashboard.

Create an Application/API Token named "Viper Vision" and copy that token.

🤖 Phase 7: Automations (The Glue)
Create an automation in HA to trigger Viper Vision when motion is detected.

YAML
alias: "Trigger Viper Vision - Front Door"
description: "Fires webhook to Viper AI when Front Door detects motion"
trigger:
  - platform: state
    entity_id: binary_sensor.front_door_motion
    to: "on"
action:
  - service: rest_command.viper_front_door
    data: {}
(Define the rest_command in configuration.yaml pointing to http://YOUR_WINDOWS_IP:5000/webhook/front_door).

🔑 Phase 8: Environment Variables
Add the following to your Windows User variables:

GEMINI_KEY: Your Google AI Studio Key.

HA_TOKEN: The Long-Lived Access Token.

PUSHOVER_USER: Your Pushover user key.

PUSHOVER_TOKEN: Your Pushover app token.

🚀 Phase 9: Launch!
Double-click ViperVision.exe. The dashboard will launch, and your AI security system is online.

🤝 Call for Contributors
This logic is sound and the GUI is fully accessible, but it needs an automated deployment method. Fork this repo if you can help build an Inno Setup/Wix installer!


***