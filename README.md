# Viper Vision

Viper Vision is a Windows-first smart home notification system that connects Home Assistant, Ring doorbell events, speaker playback, Gemini vision, and configurable text-to-speech announcements.

Version `1.0` focuses on fast doorbell awareness, accessible controls, flexible TTS routing, and playful Roborock/Cinderella notifications.

## Features

- Doorbell AI analysis from RTSP snapshots using Gemini vision.
- Configurable TTS engines:
  - Gemini cloud TTS
  - Microsoft Edge TTS
  - Google Translate speech
  - Windows SAPI
- Per-alert voice behavior for doorbell, utilities, and manual broadcasts.
- Home Assistant REST endpoints for broadcasts, doorbell webhooks, fridge/freezer alerts, and Roborock events.
- Roborock/Cinderella message routing for vacuum status, dock status, and error events.
- Sonos, Home Assistant media player, and optional Alexa announcement support.
- Screen-reader-friendly wxPython UI with focus descriptions for key controls.
- Local timing logs for RTSP capture, Gemini vision, TTS generation, and speaker playback.

## Requirements

- Windows 10 or newer.
- Python 3.11+ recommended.
- Home Assistant for webhooks and media player routing.
- FFmpeg available on `PATH` or placed beside the app as `ffmpeg.exe`.
- A Gemini API key for Gemini vision and Gemini TTS features.

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

## Configuration

Viper stores local runtime settings in `viper_config.json`. That file is intentionally ignored by Git because it may contain local IPs, tokens, MQTT credentials, API keys, and personal smart home entity IDs.

Start from:

```text
viper_config.example.json
.env.example
```

Common values to configure:

- `GEMINI_API_KEY`
- Home Assistant host and long-lived access token
- Ring MQTT topics
- RTSP camera URLs or camera IDs
- Target speaker entities and Sonos IPs
- TTS defaults and per-alert overrides

## Running From Source

```powershell
python main.pyw
```

The local web UI and webhook server default to:

```text
http://<your-pc-ip>:5050
```

## Building The Windows App

The repository includes a PyInstaller spec and helper script:

```powershell
.\build_exe.ps1
```

Build output is generated under `dist/` and is not committed.

## Home Assistant

Viper can generate Home Assistant package YAML, but many users may prefer keeping existing `configuration.yaml` and `automations.yaml` files.

The important REST commands are:

- `rest_command.ring_vision_front`
- `rest_command.ring_vision_back`
- `rest_command.viper_broadcast`
- `rest_command.viper_broadcast_push`
- `rest_command.cinderella_event`

A sanitized package example is included under `ha_packages/`. Replace placeholder IPs, MQTT topics, and entity IDs before installing it.

## Roborock / Cinderella

The Cinderella event router maps Roborock status and error values into spoken messages. Known buckets include:

- departure
- washing
- emptying
- drying
- returning
- victory
- paused
- status updates
- vacuum errors
- dock errors

The app includes default messages and lets you edit message buckets from the remote UI.

## TTS Performance Notes

Gemini voices sound natural, but cloud audio generation adds latency. Edge or Google speech is faster. Viper logs timing markers such as:

- `[DOORBELL TIMING]`
- `[RTSP CANDIDATE]`
- `[AI TIMING]`
- `[TTS TIMING]`
- `[HA PLAY TIMING]`
- `[SONOS ... TIMING]`

These logs help tune RTSP capture, AI model timing, TTS latency, and speaker playback.

Gemini warmup calls are real API calls and may be billable. Keep Gemini TTS heartbeat/warmup disabled unless the latency benefit is worth the additional usage.

## Accessibility

The desktop UI is designed with screen-reader use in mind. Controls in the voice configuration area include spoken focus descriptions, and the app uses `accessible-output2` to announce status changes.

## Security

Do not publish:

- `viper_config.json`
- `.env`
- Home Assistant tokens
- Gemini API keys
- Pushover tokens
- MQTT passwords
- personal Ring topic IDs
- debug logs

The `.gitignore` is configured to keep those out of source control.

## License

MIT License. See `LICENSE`.
