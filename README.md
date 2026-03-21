# 🐍 Viper Vision
### AI-Powered Local Security & Multi-Room Voice Assistant

Viper Vision is a high-performance, modular security engine that transforms standard RTSP camera feeds into an intelligent, talking home security system. Using **Gemini 2.5 Flash**, it analyzes "flipbook" motion captures and broadcasts natural language descriptions across **Sonos**, **Alexa**, and **Home Assistant** speakers.

---

## 🚀 Key Features

* **AI Flipbook Analysis:** Instead of a single blurry snapshot, Viper grabs 3 high-speed frames to understand movement, intent, and context (e.g., "A delivery driver is placing a package by the door" vs. "A neighbor is walking their dog").
* **Multi-Layered Audio Routing:** * **Sonos:** Local MP3 hosting for low-latency chimes and custom TTS.
    * **Alexa:** Direct cloud-based announcements via Home Assistant.
    * **Home Assistant:** Native TTS routing to any connected media player.
* **Unified Dashboard:** A `wxPython` interface for real-time monitoring, arming/disarming, and manual intercom broadcasts.
* **Smart Prompting:** Switch between "Standard" and "Detailed" AI personalities on the fly.
* **Pushover Integration:** Get high-priority mobile alerts with the best frame from the capture attached.

---

## 🛠️ The Tech Stack

* **Brain:** Google Gemini 2.5 Flash API
* **Logic:** Python 3.11+ (Modular Architecture)
* **Video:** FFmpeg (RTSP Stream Processing)
* **Audio:** SoCo (Sonos Control), gTTS (Google Text-to-Speech), Flask (Local Audio Server)
* **Automation:** Home Assistant API & Ring-MQTT

---

## 📦 Installation (Quick Start)

### 1. Prerequisites
* **Windows 10/11**
* **FFmpeg** installed and added to your System PATH.
* **Docker Desktop** (For running the Ring/MQTT/HA stack).

### 2. Environment Variables
Viper Vision requires the following variables set in your Windows environment:
* `GEMINI_KEY`: Your Google AI Studio API Key.
* `HA_TOKEN`: A Long-Lived Access Token from Home Assistant.
* `PUSHOVER_USER`: Your Pushover user key.
* `PUSHOVER_TOKEN`: Your Pushover application token.

### 3. Setup
```powershell
# Clone the repository
git clone [https://github.com/YOUR_USERNAME/Viper-Vision.git](https://github.com/YOUR_USERNAME/Viper-Vision.git)
cd Viper-Vision

# Install dependencies
pip install -r requirements.txt

# Run the dashboard
python main.py