# Viper Vision v1.2.4 Release Checklist

Use this checklist before committing, tagging, or showing Viper Vision to anyone.

## Automated Checks

Run:

```powershell
.\run_tests.ps1
```

Expected result:

- Python compile check passes.
- Unit tests pass.
- No real Home Assistant, Gemini, Ring, Roborock, or speaker calls are made.

## Build Checks

Build the installer:

```powershell
.\build_installer.ps1
```

Confirm:

- `installer\ViperVision-v1.2.4-Setup.exe` exists.
- `dist\ViperVision\ViperVision.exe` exists.
- `dist\ViperVision\_internal\ffmpeg.exe` exists.
- `dist\ViperVision\_internal\help\index.html` exists.
- `ViperVision.iss` still reports version `1.2.4`.

## Installer Smoke Test

Use a clean Windows user profile or VM when possible.

Run the repeatable smoke script after building the installer:

```powershell
.\smoke_installer.ps1
```

1. Run `installer\ViperVision-v1.2.4-Setup.exe`.
2. Launch Viper Vision.
3. Confirm the New User Setup Assistant opens if no config exists.
4. Press F1 and confirm local help opens.
5. Open the web remote at `http://localhost:5050/remote`.
6. Open Diagnostics and confirm it renders.
7. Create a support bundle.
8. Open the support zip and confirm secrets are redacted.
9. Close Viper from Exit Application and relaunch.
10. Confirm no previous-crash warning appears after a clean exit.

## Manual Feature Tests

Home Assistant:

- Test Find HA with Home Assistant off or unreachable.
- Test Find HA with Home Assistant reachable.
- Test a bad long-lived token.
- Test a valid long-lived token.
- Confirm entity discovery counts are reasonable.
- Confirm HA Status reports listener state.

Doorbell and RTSP:

- Test missing RTSP URL messaging.
- Test invalid RTSP URL messaging.
- Test valid front RTSP capture.
- Test valid back RTSP capture.
- Trigger front doorbell state in Home Assistant.
- Trigger back doorbell state in Home Assistant.
- Confirm cooldown suppresses duplicate events.

Ring setup:

- Use Ring Setup Assistant before discovery.
- Use it after discovery with no Ring entities.
- Use it with trigger entities but no RTSP URL.
- Use it with trigger entities and RTSP URL.
- Confirm help opens to Ring setup.

Audio:

- Test Gemini TTS.
- Test Edge TTS.
- Test regular Google TTS.
- Test Windows SAPI fallback.
- Test manual broadcast.
- Test front and back chimes.
- Test quiet hours behavior.

Roborock:

- Refresh vacuum controls.
- Refresh room list.
- Toggle room checklist with Space.
- Send a safe command only after confirming the selected vacuum.
- Confirm Cinderella messages still render and save.

Diagnostics:

- Run Diagnostics from desktop Diagnostics, Tests & Support.
- Run Diagnostics with Home Assistant checking enabled and confirm it reports both HA Core and HA Observer health.
- Create Support Bundle from desktop Diagnostics, Tests & Support.
- Open About Viper Vision And Data Folders and confirm version, data folder, config file, log file, support bundle location, and remote URL are readable.
- Show Diagnostics from web remote.
- Create Support Bundle from web remote.
- Confirm `ha_token`, Gemini key, Pushover keys, MQTT password, Ring camera IDs, and Ring topic roots are not visible in the bundle.

Home Assistant hang watch:

- Start `.\watch_ha_health.ps1` before a long test run.
- If Home Assistant appears frozen, check whether the CSV says `core_hung_vm_alive`.
- If the VM is powered off, run `.\harden_ha_virtualbox.ps1 -MoveOutOfDownloads` to move the VM out of Downloads and apply safer disk settings.

## Known Limitations To Tell Users

- Viper can guide VirtualBox/Home Assistant OS setup on Windows x64 and can install Mosquitto/Ring-MQTT through Home Assistant Supervisor when the Home Assistant token has permission. Users may still prefer Home Assistant Green, a mini PC, or an existing Home Assistant server.
- Gemini warmup may create billable API requests.
- Doorbell AI requires a live RTSP stream; Home Assistant snapshots are not used by default.
- Some Roborock controls only appear if Home Assistant exposes matching entities.
- Windows SmartScreen may warn because the installer is unsigned.
- If Home Assistant Observer on port `4357` is healthy but port `8123` hangs, the VM is alive and Home Assistant Core is the likely problem.

## Fresh-PC Screen Reader Acceptance Script

Use this script with JAWS or NVDA before calling the installer ready.

1. Launch Viper from the Start menu.
2. Confirm speech/focus lands on an enabled Viper setup window, not "Viper Vision unavailable."
3. Tab through the setup wizard and confirm every button has a clear action label.
4. Press Alt+Tab away and back to the setup wizard. Confirm focus returns to an enabled Viper window.
5. Open Diagnostics, Tests & Support.
6. Press About Viper Vision And Data Folders.
7. Confirm the About dialog reads version, data folder, config file, log file, support bundle location, and remote URL.
8. Press Copy Data Folder and confirm the path can be pasted.
9. Press Open Remote and confirm the remote page headings match the desktop product areas.
10. Close Viper cleanly, relaunch, and confirm there is no previous-crash warning.

## Support Instructions

Ask users for:

- What they were trying to do.
- What happened instead.
- Whether Home Assistant opens in their browser.
- Whether `http://home-assistant-ip:4357` opens when `http://home-assistant-ip:8123` does not.
- Whether they can open `http://localhost:5050/remote`.
- Viper version and data folder from Diagnostics, About Viper Vision And Data Folders.
- A Viper support bundle created from Diagnostics, Tests & Support, Create Support Report To Email Developer.
- The latest `ha_health_watch.csv` if they ran the watcher.

Do not ask users to paste API keys, Home Assistant tokens, MQTT passwords, or full Ring IDs.
