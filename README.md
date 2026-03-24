# 🐍👁️ Viper Vision

**Viper Vision** is an intelligent, highly accessible, self-hosted smart home security dashboard. It bridges the gap between Ring cameras, Home Assistant, Sonos, and Gemini AI to create a fully automated, talking security system.

When motion or a doorbell ring is detected, Viper Vision captures frames via an RTSP stream, analyzes them using Google's Gemini 2.5 Flash AI, and broadcasts a natural-language description of what it sees (e.g., *"There is a delivery driver holding a package at the front door"*) across your house via Sonos, Alexa, and Google Nest speakers.

---

## ✨ Key Features
* **AI Frame Analysis:** Uses Gemini Vision to analyze RTSP flipbooks and generate concise, accurate security descriptions.
* **Universal Audio Broadcasting:** Automatically discovers and broadcasts to Sonos, Alexa, and Home Assistant Cast devices.
* **Accessible Desktop UI:** Built with `wxPython` and `accessible_output2` for full screen-reader compatibility.
* **Remote Web Dashboard:** Control the system, manage speaker targets, and check batteries via a mobile-friendly Flask interface.
* **Automated Port Defender:** Includes a custom Windows batch script that prevents WinNAT from stealing Docker networking ports.
* **Background Health Monitor:** Automatically alerts you via audio if your MQTT broker or Camera Bridge goes offline.

---

## 📂 Project Structure
* `/setup` - Contains the Master Setup script for initializing directories and fixing Windows networking.
* `/src` - The core Python application, including the main dashboard, AI vision handlers, and audio routing.
* `docker-compose.yml` - The Docker stack containing Mosquitto, Home Assistant, and the Ring-MQTT bridge.

---

## 🚀 Quick Start Guide

### 1. Prerequisites
* **Windows 10/11** (Required for the WinNAT port-fixer and wxPython UI)
* **Docker Desktop** installed and running
* **Python 3.10+**

### 2. The Master Setup
To avoid port collisions and firewall blocks, **do not run `docker-compose up` manually**. Instead, use the provided installer:
1. Navigate to the `/setup` folder.
2. Right-click `Viper_Master_Setup.bat` and select **Run as Administrator**.
3. **Wait about 30 seconds** for the Docker containers to fully download, extract, and start up in the background.

### 3. Connect Your Ring Account
Once Docker is running, link your cameras to the local bridge:
1. Open your browser and go to `http://localhost:55123`
2. Log in with your Ring credentials to generate the local RTSP streams.

### 4. Install Dependencies & Set Variables
Open your terminal in the root project directory and install the required Python libraries:
```
pip install -r requirements.txt

You must set the following System Environment Variables on your Windows machine for the app to function:

HA_TOKEN: Your Long-Lived Access Token from Home Assistant.

GEMINI_KEY: Your Google Gemini API Key.

PUSHOVER_USER: Your Pushover User Key (for mobile push notifications).

PUSHOVER_TOKEN: Your Pushover App Token.

5. Launch Viper Vision
Navigate to the source directory and run the main app:

cd src
python main.py