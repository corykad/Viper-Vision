# Viper Vision

Viper Vision is a Windows-first smart home notification and control system. It connects Home Assistant, Ring doorbell events, RTSP camera snapshots, Gemini vision, configurable text-to-speech, speaker playback, refrigerator alerts, and Roborock vacuum controls into one accessible desktop app and web remote.

Version `1.1` adds a richer web remote, Roborock vacuum controls, room cleaning, saved vacuum room maps, better Home Assistant diagnostics, speed diagnostics, and stronger screen-reader labeling.

## What Viper Vision Does

Viper can:

- Watch doorbell events from Home Assistant or MQTT.
- Grab a fast RTSP camera frame when a doorbell event fires.
- Send that image to Gemini vision.
- Speak a short natural-language alert through your speakers.
- Play custom chimes for front and back door events.
- Broadcast manual messages through the house.
- Route different alert types to different speakers.
- Use Gemini TTS, Microsoft Edge TTS, Google speech, or Windows SAPI.
- Use different voices and speeds for doorbells, utilities, and manual broadcasts.
- Announce refrigerator and freezer events.
- Announce Roborock status, dock status, and errors using editable Cinderella messages.
- Control a Roborock vacuum from the desktop app and the web remote.
- Clean selected Roborock rooms by name.
- Save Roborock room IDs to `viper_config.json`.
- Show timing diagnostics for RTSP, Gemini vision, TTS, Home Assistant playback, and Sonos playback.
- Provide accessible desktop and web controls for screen-reader users.

## Big Picture

Viper Vision runs on a Windows PC. Home Assistant sends events to Viper over HTTP. Viper then decides what to do: analyze a camera frame, speak an alert, play a chime, broadcast a message, or trigger a Roborock announcement.

Typical layout:

```text
Ring / Roborock / Fridge / Sensors
        |
        v
Home Assistant
        |
        v
Viper Vision on Windows
        |
        v
Gemini, speakers, Sonos, Home Assistant media players, web remote
```

You do not have to be a Home Assistant expert to use this, but Home Assistant is the glue. Viper uses it for entity IDs, automations, REST commands, speakers, Roborock controls, and sensor events.

## Important Terms

If you are new to Home Assistant, these words matter:

- **Home Assistant**: A local smart home server. It connects smart devices and lets you automate them.
- **Entity**: A device, sensor, switch, media player, vacuum, or setting inside Home Assistant. Entity IDs look like `media_player.living_room`, `sensor.cinderella_status`, or `vacuum.cinderella`.
- **Integration**: A Home Assistant add-on for a device brand or service, such as Roborock, Ring, Sonos, or MQTT.
- **Automation**: A Home Assistant rule. For example: "When the Ring motion topic turns on, call Viper's doorbell webhook."
- **Action / Service**: A command Home Assistant can run. Examples: `rest_command.ring_vision_front`, `vacuum.start`, `select.select_option`.
- **Long-lived access token**: A password-like token Viper uses to talk to Home Assistant. Keep it private.
- **RTSP**: A camera video stream URL. Viper uses RTSP to grab a still frame quickly.
- **TTS**: Text-to-speech.
- **Webhook**: An HTTP URL Home Assistant calls to notify Viper.

## Requirements

Minimum:

- Windows 10 or Windows 11.
- Home Assistant running on your network.
- A Home Assistant long-lived access token.
- A Gemini API key if you want AI vision or Gemini TTS.
- At least one speaker target:
  - Sonos speaker by IP address,
  - Home Assistant `media_player` entity,
  - optional Alexa media player entity,
  - or local Windows SAPI fallback.
- FFmpeg for RTSP frame capture.

Recommended:

- Python 3.11 or newer if running from source.
- A static IP address for the Windows PC running Viper.
- A static IP address for Home Assistant.
- Home Assistant Roborock integration if using vacuum controls.
- Home Assistant Ring or MQTT setup if using doorbell triggers.
- A screen reader such as JAWS, NVDA, or Windows Narrator if you rely on speech feedback.

## Install Options

You can run Viper two ways.

### Option 1: Windows Release Zip

This is easiest for most users.

1. Download `ViperVision-v1.1-Setup.exe` from the GitHub release.
2. Run the installer.
3. Choose whether to create a desktop shortcut.
4. Launch Viper Vision from the Start menu or desktop shortcut.

If you use the portable zip instead, download `ViperVision-v1.1-windows.zip` and extract it to a folder such as:

   ```text
   C:\ViperVision
   ```

Then run:

   ```text
   ViperVision.exe
   ```

Windows may show a SmartScreen warning because this is a small unsigned personal project. Choose "More info" and "Run anyway" only if you trust the source.

Runtime data is stored under:

```text
%APPDATA%\viper_vision_1.0
```

That folder contains your real `viper_config.json`, logs, generated audio, and copied chimes.

### Option 2: Run From Source

Use this if you want to modify the app.

1. Install Python 3.11 or newer.
2. Open PowerShell in the project folder.
3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Start Viper:

   ```powershell
   python main.pyw
   ```

When running from source, runtime files are stored in the project folder.

## Files And Folders

Important public files:

- `main.pyw`: Main desktop app, Flask web server, and route handlers.
- `viper_audio.py`: TTS, speaker playback, chimes, and audio cache logic.
- `viper_vision.py`: Camera frame capture and Gemini vision logic.
- `viper_config.py`: Defaults, config validation, paths, and constants.
- `viper_discovery.py`: Home Assistant discovery helpers.
- `viper_ha_package.py`: Home Assistant package generator.
- `viper_ring_discovery.py`: Ring/MQTT discovery helpers.
- `templates/remote.html`: Accessible web remote.
- `ha_packages/`: Home Assistant package examples.
- `chimes/`: Bundled chime audio assets.
- `requirements.txt`: Python dependencies.
- `ViperVision.spec`: PyInstaller build spec.
- `build_exe.ps1`: Windows portable app build helper.
- `build_installer.ps1`: Windows installer build helper.
- `ViperVision.iss`: Inno Setup installer definition.

Private runtime files that should not be committed:

- `viper_config.json`
- `.env`
- `api_usage.json`
- `viper_full_debug.log`
- generated `static_phrase_*.mp3`
- `build/`
- `dist/`
- `exe_smoke_appdata/`
- `exe_smoke_appdata_final/`

## First Launch

1. Start Viper.
2. If the app is missing required settings, the setup dialog opens.
3. Enter Home Assistant and API details.
4. Save.
5. Use the **HA Status** tab to confirm Viper can reach Home Assistant.
6. Use the **Devices & Chimes** tab to add speakers.
7. Use the **Voice Behavior** tab to configure TTS.
8. Use the **Vacuum** tab to load Roborock controls and rooms.

The local web remote is available at:

```text
http://YOUR_VIPER_PC_IP:5050/remote
```

Example:

```text
http://192.168.4.56:5050/remote
```

## Windows Firewall

Home Assistant must be able to call Viper over your network.

Viper uses these ports by default:

- `5050`: Viper web remote and webhook server.
- `8090`: Local audio file server used for speaker playback.

If Home Assistant cannot reach Viper:

1. Open Windows Security.
2. Go to Firewall & network protection.
3. Allow ViperVision.exe or Python through the firewall.
4. Make sure private network access is allowed.
5. Confirm your PC and Home Assistant are on the same network.

You can test from another computer or phone:

```text
http://YOUR_VIPER_PC_IP:5050/remote
```

If the page loads, the web server is reachable.

## Home Assistant Setup From Zero

If you have never used Home Assistant, this is the beginner path.

### 1. Install Home Assistant

Home Assistant can run on a Home Assistant Green, Raspberry Pi, mini PC, NAS, virtual machine, or Docker. The official installation guide is here:

```text
https://www.home-assistant.io/installation/
```

After installation, open Home Assistant in a browser. The address usually looks like:

```text
http://homeassistant.local:8123
```

or:

```text
http://YOUR_HOME_ASSISTANT_IP:8123
```

### 2. Find Your Home Assistant IP

In Home Assistant:

1. Go to **Settings**.
2. Go to **System**.
3. Go to **Network**.
4. Find the local IP address.

You will enter this in Viper as `ha_ip`.

Example:

```text
192.168.4.49
```

### 3. Create A Long-Lived Access Token

Viper uses this token to read Home Assistant entities and call Home Assistant services.

1. In Home Assistant, select your user profile.
2. Scroll to **Long-lived access tokens**.
3. Select **Create Token**.
4. Name it something like:

   ```text
   Viper Vision
   ```

5. Copy the token.
6. Paste it into Viper's Home Assistant setup dialog.

Important: Home Assistant only shows this token once. If you lose it, create a new one.

### 4. Add Integrations

In Home Assistant, go to:

```text
Settings > Devices & services
```

Add the integrations you need.

Useful integrations:

- **Roborock**: Required for Roborock vacuum controls and Cinderella status.
- **Sonos**: If you want Home Assistant to see Sonos speakers as media players.
- **Ring**: Useful for Ring devices, although many Ring event setups also use MQTT.
- **MQTT**: Useful if you use ring-mqtt or another MQTT event bridge.
- **Alexa Media Player**: Optional and usually installed through HACS, not core Home Assistant.

### 5. Learn Entity IDs

Viper needs exact entity IDs.

In Home Assistant:

1. Go to **Settings**.
2. Go to **Devices & services**.
3. Choose an integration or device.
4. Look at the entities.

Examples:

```text
media_player.living_room
vacuum.cinderella
sensor.cinderella_status
sensor.cinderella_vacuum_error
sensor.cinderella_dock_dock_error
binary_sensor.cinderella_dock_mop_drying
```

You can also use:

```text
Developer Tools > States
```

Search for `cinderella`, `roborock`, `ring`, `fridge`, or `media_player`.

## Home Assistant YAML Setup

Viper needs Home Assistant to call its URLs. The cleanest setup is a Home Assistant package.

### Package Setup

1. In Home Assistant, open your `configuration.yaml`.
2. Make sure packages are enabled:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

3. Create this folder if it does not exist:

   ```text
   /config/packages
   ```

4. Copy:

   ```text
   ha_packages/viper_vision_package.example.yaml
   ```

   to:

   ```text
   /config/packages/viper_vision_package.yaml
   ```

5. Edit `viper_vision_package.yaml`.
6. Replace:

   ```text
   YOUR_VIPER_PC_IP
   YOUR_LOCATION_ID
   YOUR_FRONT_CAMERA_ID
   YOUR_BACK_CAMERA_ID
   ```

7. In Home Assistant, go to:

   ```text
   Developer Tools > YAML > Check Configuration
   ```

8. If the check passes, restart Home Assistant.

### Non-Package Setup

If you already have `configuration.yaml` and `automations.yaml`, you can keep using them.

Put the `rest_command:` section in `configuration.yaml`.

Put the `automation:` entries in `automations.yaml`.

Then run:

```text
Developer Tools > YAML > Check Configuration
```

Restart Home Assistant after adding new REST commands.

## Required Home Assistant REST Commands

Viper expects these Home Assistant actions to exist if you want the matching features:

```text
rest_command.ring_vision_front
rest_command.ring_vision_back
rest_command.viper_broadcast
rest_command.viper_broadcast_push
rest_command.cinderella_event
```

The package example defines them like this:

```yaml
rest_command:
  ring_vision_front:
    url: "http://YOUR_VIPER_PC_IP:5050/doorbell-webhook"
    method: POST
    timeout: 10

  ring_vision_back:
    url: "http://YOUR_VIPER_PC_IP:5050/doorbell-webhook/back"
    method: POST
    timeout: 10

  viper_broadcast:
    url: "http://YOUR_VIPER_PC_IP:5050/remote/broadcast"
    method: POST
    content_type: "application/json"
    payload: >
      {"broadcast_text": {{ message | tojson }},
       "channel": {{ channel | default('') | tojson }}}
    timeout: 15

  viper_broadcast_push:
    url: "http://YOUR_VIPER_PC_IP:5050/remote/broadcast_push"
    method: POST
    content_type: "application/json"
    payload: >
      {"broadcast_text": {{ message | tojson }},
       "channel": {{ channel | default('') | tojson }}}
    timeout: 15

  cinderella_event:
    url: "http://YOUR_VIPER_PC_IP:5050/cinderella"
    method: POST
    content_type: "application/json"
    payload: >
      {"event": {{ event | tojson }},
       "error": {{ error | default('') | tojson }},
       "source": {{ source | default('vacuum') | tojson }}}
    timeout: 15
```

## Testing Home Assistant Actions

In Home Assistant:

1. Go to **Developer Tools**.
2. Go to **Actions**.
3. Search for an action.
4. Fill in the data.
5. Select **Perform action**.

Test a broadcast:

```yaml
action: rest_command.viper_broadcast
data:
  message: "Testing Viper Vision."
  channel: manual
```

Test a Cinderella event:

```yaml
action: rest_command.cinderella_event
data:
  event: washing
```

Test a vacuum error:

```yaml
action: rest_command.cinderella_event
data:
  event: error
  error: water_carriage_drop
  source: vacuum
```

Test a dock error:

```yaml
action: rest_command.cinderella_event
data:
  event: error
  error: duct_blockage
  source: dock
```

## Doorbell Setup

Doorbell alerts need two things:

1. A trigger event from Home Assistant.
2. An RTSP camera stream Viper can capture.

### Doorbell Event Flow

```text
Doorbell event
  -> Home Assistant automation
  -> rest_command.ring_vision_front or rest_command.ring_vision_back
  -> Viper captures RTSP frame
  -> Gemini analyzes image
  -> Viper speaks alert
```

### Front Door URL

```text
http://YOUR_VIPER_PC_IP:5050/doorbell-webhook
```

### Back Door URL

```text
http://YOUR_VIPER_PC_IP:5050/doorbell-webhook/back
```

### RTSP URLs

Viper needs RTSP URLs for front and back cameras.

Examples:

```text
rtsp://192.168.4.49:8554/front_camera_live
rtsp://192.168.4.49:8554/back_camera_live
```

Your exact URL depends on your camera bridge. Some users expose Ring cameras through Home Assistant add-ons such as go2rtc or ring-mqtt. Viper only needs a working RTSP URL.

Test RTSP before blaming Viper. You can test with VLC:

1. Open VLC.
2. Choose **Media > Open Network Stream**.
3. Paste the RTSP URL.
4. Confirm video appears.

### Ring MQTT Topics

If your Ring events come through MQTT, topics often look like:

```text
ring/LOCATION_ID/camera/CAMERA_ID/motion/state
```

Payloads often use:

```text
ON
OFF
```

Viper has setup tools to help find Ring MQTT topics. In the desktop app, open:

```text
Utilities > Home Assistant Setup > Find Ring Topics
```

If your Ring setup uses different topics, edit the Home Assistant automation manually.

## Speaker Setup

Open the desktop app and go to:

```text
Devices & Chimes
```

Add each speaker you want Viper to use.

### Sonos Speaker

Use:

```text
Type: Sonos
ID: speaker IP address
```

Example:

```text
192.168.4.82
```

Viper can also scan for Sonos speakers from the Utilities tab.

### Home Assistant Media Player

Use:

```text
Type: Home Assistant
ID: media_player.your_speaker
```

Example:

```text
media_player.kitchen_speaker
```

### Alexa Speaker

Use:

```text
Type: Alexa
ID: media_player.your_echo
```

Alexa support depends on your Home Assistant Alexa media player setup.

### Speaker Routing

Each speaker has routing checkboxes:

- **Doorbell Alerts**: Speaker receives doorbell announcements.
- **Utilities Spoken**: Speaker receives utility announcements.
- **Fridge / Freezer**: Speaker receives fridge and freezer alerts.
- **Ignore Quiet Hours**: Speaker can still receive utility audio during quiet hours.

## Chime Setup

Chimes live in:

```text
chimes/
```

or, for the Windows app:

```text
%APPDATA%\viper_vision_1.0\chimes
```

Supported formats:

- `.mp3`
- `.wav`

In the desktop app:

1. Open **Devices & Chimes**.
2. Choose front and back chimes.
3. Use **Test** to verify.
4. Save.

## Voice Behavior And TTS Setup

Open:

```text
Voice Behavior
```

Viper supports these TTS engines:

- **Gemini TTS**: Natural cloud voices. Best quality, slower, may cost money.
- **Edge TTS**: Microsoft neural voices. Fast and reliable.
- **Google Cloud / Google speech**: Simple regular speech path.
- **Local PC SAPI**: Offline Windows voice fallback.

### Default Voice

Set the default engine, voice, and speed. Alerts can either use this default or override it.

### Per-Alert Voice Settings

Viper separates:

- **Doorbell alerts**: Usually fast and urgent.
- **Utilities**: Fridge, freezer, filter, Roborock, status alerts.
- **Manual broadcasts**: Text you type and send to the house.

Each category can use:

- default settings, or
- its own engine,
- its own voice,
- its own speed,
- its own dynamic mood setting.

### Dynamic Mood

Dynamic mood lets Viper adjust delivery based on alert text.

Examples:

- Security or motion alert: faster, more urgent.
- Celebration text: brighter and more excited.
- Warning text: slower and more authoritative.

### Gemini TTS Warmup Warning

Gemini warmup / heartbeat requests are real API requests. They may be billable. Keep warmup disabled unless you decide the lower latency is worth the extra usage.

## Gemini API Setup

Viper uses Gemini for:

- camera image analysis,
- optional Gemini TTS.

You need a Gemini API key.

1. Create or open a Google AI Studio account.
2. Create an API key.
3. Paste it into Viper's setup dialog.

Keep the key private. Do not commit it.

Viper logs API usage locally in:

```text
api_usage.json
```

The Utilities tab has **Check API Cost**.

## Pushover Setup

Pushover is optional. It can send phone push notifications in addition to audio.

You need:

- Pushover user key.
- Pushover app API token.

Enter these in the Home Assistant setup dialog if you use Pushover.

## Fridge And Freezer Setup

Viper supports fridge and freezer event routing.

In the desktop app:

```text
Fridge
```

In the web remote:

```text
Fridge & Freezer
```

Each event can be:

- **Speak**: Speak a message.
- **Chime only**: Play a chime with no speech.
- **Silent**: Do not play audio.

Supported default channels:

- `fridge_open`
- `fridge_closed`
- `freezer_open`
- `freezer_closed`

You need Home Assistant automations that call:

```text
rest_command.viper_broadcast
```

with the correct `channel`.

Example:

```yaml
action: rest_command.viper_broadcast
data:
  message: "The fridge door is open."
  channel: fridge_open
```

## Ice Maker Controls

Viper has buttons for the ice maker if your Home Assistant entity IDs match the configured defaults.

Defaults:

```text
switch.refrigerator_cubed_ice
input_boolean.keep_ice_maker_on
```

These can be overridden by environment variables:

```text
ICE_MAKER_SWITCH_ENTITY
ICE_MAKER_KEEP_ON_ENTITY
```

## Roborock / Cinderella Setup

Viper's Roborock feature has two parts:

1. **Cinderella notifications**: funny spoken messages when the vacuum changes status or hits an error.
2. **Vacuum controls**: start, pause, dock, suction speed, mop mode, room cleaning, and advanced commands.

### Required Home Assistant Integration

Install the official Roborock integration in Home Assistant:

```text
Settings > Devices & services > Add Integration > Roborock
```

Sign in with your Roborock account.

After setup, Home Assistant should expose entities like:

```text
vacuum.cinderella
sensor.cinderella_status
sensor.cinderella_vacuum_error
sensor.cinderella_dock_dock_error
binary_sensor.cinderella_dock_mop_drying
select.cinderella_mop_mode
select.cinderella_mop_intensity
number.cinderella_volume
switch.cinderella_dock_child_lock
```

Your entity IDs may differ. Use Developer Tools > States to confirm.

### Cinderella Event Router

The included Home Assistant package has an automation named:

```text
Viper Vision: Cinderella Event Router
```

It watches:

```text
sensor.cinderella_status
sensor.cinderella_vacuum_error
sensor.cinderella_dock_dock_error
binary_sensor.cinderella_dock_mop_drying
```

It sends events to:

```text
rest_command.cinderella_event
```

Viper then chooses a random message from the matching bucket.

### Roborock Status Buckets

Viper supports these message buckets:

- `departure`
- `washing`
- `emptying`
- `drying`
- `returning`
- `victory`
- `paused`
- `status_update`
- `vacuum_error_templates`
- `dock_error_templates`
- `specific_errors`

You can edit these from:

```text
http://YOUR_VIPER_PC_IP:5050/remote
```

Look for:

```text
Cinderella Message Studio
```

### Vacuum Controls

Open the desktop app:

```text
Vacuum
```

Available controls depend on what Home Assistant exposes.

Common controls:

- Start cleaning.
- Pause cleaning.
- Stop cleaning.
- Return to dock.
- Locate vacuum.
- Spot clean.
- Set suction speed.
- Mop mode.
- Mop intensity.
- Empty mode.
- Selected map.
- Volume.
- Child lock.
- Room cleaning.
- Advanced command.
- Go to coordinates.

### Room Cleaning

Room cleaning works like this:

1. Press **Refresh room list**.
2. Viper calls Home Assistant action:

   ```text
   roborock.get_maps
   ```

3. Home Assistant returns Roborock room names and room IDs.
4. Viper saves the rooms to `viper_config.json`.
5. You check rooms by name.
6. Viper sends:

   ```text
   vacuum.send_command
   command: app_segment_clean
   ```

The saved config looks like:

```json
"vacuum_rooms": {
  "vacuum.cinderella": [
    {
      "label": "Kitchen (7)",
      "name": "Kitchen",
      "map": "Current map",
      "segment": 7
    }
  ]
}
```

In the desktop app, the room checklist supports keyboard use:

- Arrow keys move through rooms.
- Space checks or unchecks the focused room.
- Viper speaks the checked state.

In the web remote, room checkboxes are normal HTML checkboxes and should work with screen readers.

## Desktop Tabs

### Dashboard

Shows the main arm/disarm state.

### Voice Behavior

Controls default TTS and per-alert voice settings.

### Devices & Chimes

Add speakers, edit speaker routing, test speakers, and choose doorbell chimes.

### Utilities

Tools for:

- API cost check.
- Doorbell battery check.
- Refrigerator filter check.
- Home Assistant setup.
- HA package generation.
- Sonos scan.
- Home Assistant speaker scan.
- Quiet hours.

### Fridge

Controls fridge/freezer channel behavior and chimes.

### Vacuum

Controls Roborock actions, settings, room cleaning, and advanced commands.

### Speed

Reads the latest Viper log and summarizes timing:

- RTSP capture.
- Gemini vision.
- Doorbell TTS path.
- Home Assistant play request.
- Sonos play request.
- Gemini TTS median.

### HA Status

Tests Home Assistant connection and checks important entities:

- speakers,
- fridge/freezer defaults,
- ice maker,
- Cinderella status,
- Cinderella errors,
- Roborock candidates.

## Web Remote

The web remote runs at:

```text
http://YOUR_VIPER_PC_IP:5050/remote
```

It includes:

- Section navigation.
- System arm/disarm.
- Vacuum controls.
- Room cleaning.
- Speaker targets.
- Speaker routing.
- Add, rename, delete, and test speakers.
- AI profile manager.
- Utility buttons.
- Quiet hours.
- Fridge and freezer channel settings.
- Manual broadcast.
- Discovery tools.
- Cinderella Message Studio.
- Recent activity.

The web remote is designed to be screen-reader friendly:

- Uses headings and landmarks.
- Includes skip navigation.
- Uses normal forms and buttons.
- Uses labels and legends.
- Uses live regions for feedback.
- Avoids requiring drag-and-drop or mouse-only controls.

## Accessibility Notes

Viper is built for screen-reader use.

Desktop accessibility:

- Key controls have focus descriptions.
- Viper uses `accessible-output2` for spoken status.
- The room checklist supports Space to toggle checked rooms.
- Buttons and fields use descriptive labels.

Web accessibility:

- The remote uses semantic headings and sections.
- Forms use labels.
- Checkboxes and buttons are keyboard reachable.
- Flash messages use alert behavior.
- Delete confirmation uses an accessible in-page dialog instead of a browser confirm prompt.

If a control is unclear with your screen reader, that is a bug worth fixing.

## Configuration Files

### `viper_config.json`

This is the main runtime config. It is private and ignored by Git.

It may contain:

- Home Assistant host.
- Home Assistant token.
- Gemini API key.
- Pushover tokens.
- speaker IDs.
- camera URLs.
- MQTT topics.
- TTS preferences.
- saved Roborock room IDs.
- Cinderella messages.

### `.env`

Optional. You can use environment variables instead of storing some values in JSON.

Start from:

```text
.env.example
```

Useful variables:

```text
FLASK_PORT=5050
SONOS_PORT=8090
HA_IP=192.168.4.49
HA_PORT=8123
PC_IP=192.168.4.56
GEMINI_API_KEY=
HA_TOKEN=
PUSHOVER_USER_KEY=
PUSHOVER_API_TOKEN=
RTSP_FRONT=
RTSP_BACK=
MQTT_HOST=
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=
```

Do not commit `.env`.

## Performance Tuning

The most common delay sources are:

- RTSP camera wake-up.
- Gemini vision request.
- Gemini TTS generation.
- Home Assistant media playback.
- Speaker wake-up.

Use the **Speed** tab first.

Important log markers:

```text
[DOORBELL TIMING]
[RTSP CANDIDATE]
[AI TIMING]
[TTS TIMING]
[HA PLAY TIMING]
[SONOS ... TIMING]
```

### RTSP Tuning

Viper has frame-size thresholds so it does not analyze a tiny blurry wake-up frame.

Environment variables:

```text
FRONT_MIN_FRAME_BYTES=30000
BACK_MIN_FRAME_BYTES=14000
RTSP_CONNECT_TIMEOUT_SECONDS=18
```

If Viper waits too long, lower the threshold.

If Viper analyzes blurry frames, raise the threshold.

### Gemini TTS Tuning

Gemini voices sound better but are slower than local or Edge TTS.

Options:

- Use Gemini for manual and utility messages.
- Use Edge TTS for doorbells if speed matters most.
- Keep Gemini for doorbells if voice quality matters most.
- Disable Gemini warmup unless you accept possible extra API charges.

### Speaker Tuning

If Home Assistant playback is slow but Sonos direct playback is fast, use Sonos direct targets where possible.

If the first speaker announcement is slow after idle time, the speaker itself may be waking up.

## Troubleshooting

### Home Assistant Cannot Reach Viper

Check:

- Viper is running.
- Windows firewall allows the app.
- PC IP address is correct.
- Port `5050` is correct.
- Home Assistant and PC are on the same network.

Open this from another device:

```text
http://YOUR_VIPER_PC_IP:5050/remote
```

### Viper Cannot Reach Home Assistant

Check:

- Home Assistant IP.
- Port `8123`.
- Long-lived token.
- HA Status tab.
- Home Assistant is not using HTTPS only.

### Doorbell Trigger Does Nothing

Check:

- Home Assistant automation is enabled.
- `rest_command.ring_vision_front` exists.
- `rest_command.ring_vision_back` exists.
- Home Assistant can reach `http://YOUR_VIPER_PC_IP:5050/remote`.
- Viper is armed.
- The trigger topic or entity is correct.

### Doorbell Speaks Too Slowly

Open the **Speed** tab.

Look at:

- RTSP capture time.
- Gemini vision time.
- TTS time.
- HA play time.
- Sonos play time.

Then tune the slow part.

### Gemini TTS Is Slow

That is normal compared with local or Edge TTS.

Options:

- Use a faster voice engine for doorbells.
- Keep Gemini only for manual broadcasts or utilities.
- Use shorter alert text.
- Avoid warmup unless you accept possible billable calls.

### No Audio Plays

Check:

- Speaker is enabled.
- Speaker routing includes that alert type.
- Quiet hours are not suppressing utilities.
- Speaker entity or IP is correct.
- Home Assistant media player can play media.
- Viper local audio server port `8090` is reachable.

### Roborock Rooms Do Not Appear

Check:

- Home Assistant Roborock integration is installed.
- `vacuum.cinderella` or your vacuum entity exists.
- In Viper, press **Refresh room list**.
- In Home Assistant Developer Tools, test `roborock.get_maps`.
- Confirm the Roborock app has a saved map with rooms.

### Spacebar Does Not Check Rooms

Make sure focus is on the room list item. Use Tab to reach the room list, arrow keys to move, then Space to check or uncheck.

### YAML Fails Validation

In Home Assistant:

```text
Developer Tools > YAML > Check Configuration
```

Common issues:

- Bad indentation.
- Missing quotes around strings.
- Wrong entity IDs.
- Package folder not enabled.
- Duplicate `homeassistant:` blocks.

### Token Or API Key Leaked

Immediately rotate it:

- Delete and recreate the Home Assistant long-lived token.
- Delete and recreate the Gemini API key.
- Delete and recreate Pushover token if needed.

## Logs

Important logs:

```text
viper_full_debug.log
api_usage.json
```

In the Windows release, these are under:

```text
%APPDATA%\viper_vision_1.0
```

In source mode, they are in the project folder.

Do not post logs publicly without checking for private URLs, IP addresses, entity IDs, and tokens.

## Building A Windows Release

Build the portable app folder:

```powershell
.\build_exe.ps1
```

Output:

```text
dist\ViperVision
```

Create a zip from the `dist\ViperVision` folder if you want a portable release asset.

Build the installer:

```powershell
.\build_installer.ps1
```

This requires Inno Setup 6. If it is missing, install it with:

```powershell
winget install --id JRSoftware.InnoSetup -e
```

Installer output:

```text
installer\ViperVision-v1.1-Setup.exe
```

Do not commit:

- `dist/`
- `installer/`
- `build/`
- generated zip files unless they are release assets only.

## Running Tests

Viper includes a small release test suite that uses Python's built-in `unittest` module. It does not call your real Home Assistant, Gemini account, speakers, or Roborock vacuum. Home Assistant responses are mocked so the tests can run safely before a commit.

Run everything:

```powershell
.\run_tests.ps1
```

Or run the pieces manually:

```powershell
python -m py_compile main.pyw viper_audio.py viper_config.py viper_discovery.py viper_ha_package.py viper_ring_discovery.py viper_vision.py tests\test_viper_release.py
python -m unittest discover -s tests -v
```

The current suite checks:

- config normalization for saved Roborock room IDs,
- Roborock map room parsing,
- accessible remote page rendering for vacuum controls,
- web room refresh saving rooms into config,
- web room cleaning service payloads,
- select, number, and child-lock setting routes,
- Cinderella dock-specific error message routing.

## GitHub Release Checklist For 1.1

Before committing:

```powershell
.\run_tests.ps1
```

Check private files are ignored:

```powershell
git status --ignored --short
```

Confirm these are not committed:

- `viper_config.json`
- `.env`
- logs
- generated audio
- `build/`
- `dist/`
- smoke-test appdata
- API usage files

Suggested commit:

```powershell
git add README.md main.pyw templates/remote.html viper_config.py viper_config.example.json
git commit -m "Release Viper Vision 1.1"
git tag -a v1.1 -m "Viper Vision 1.1"
```

If publishing with GitHub CLI:

```powershell
git push origin main
git push origin v1.1
gh release create v1.1 installer\ViperVision-v1.1-Setup.exe ViperVision-v1.1-windows.zip --title "Viper Vision 1.1" --notes "Viper Vision 1.1 release."
```

## Security

Never commit or publish:

- `viper_config.json`
- `.env`
- Home Assistant tokens
- Gemini API keys
- Pushover tokens
- MQTT passwords
- personal Ring topic IDs
- private camera URLs
- debug logs
- generated audio containing private messages

The `.gitignore` is meant to protect these, but always check before pushing.

## Known Limitations

- Gemini TTS is cloud-based and can be slower than local or Edge TTS.
- Gemini warmup calls may be billable.
- IR HVAC control, if added later, will usually be one-way unless separate sensors provide feedback.
- Roborock room IDs come from Home Assistant/Roborock map data and can change if maps are rebuilt.
- Alexa media playback depends on your Home Assistant Alexa media setup.
- RTSP reliability depends on the camera bridge and network.

## License

MIT License. See `LICENSE`.

## Helpful Official Links

- Home Assistant installation: `https://www.home-assistant.io/installation/`
- Home Assistant REST API: `https://developers.home-assistant.io/docs/api/rest`
- Home Assistant Roborock integration: `https://www.home-assistant.io/integrations/roborock`
- Home Assistant Vacuum integration: `https://www.home-assistant.io/integrations/vacuum`
- Home Assistant packages: `https://www.home-assistant.io/docs/configuration/packages/`
