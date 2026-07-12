# Viper Vision Architecture

This file is a quick map for finding code without digging through `main.pyw`.

## Startup And Shell

- `main.pyw` owns application startup, the Flask app object, top-level Home Assistant event dispatch, and the `ViperDashboard` frame wiring.
- `viper_runtime.py` owns the shared executor, shutdown event, startup timing markers, and recent runtime events.
- `viper_ui_lifecycle.py` owns the dashboard minimize and quit handlers.
- `viper_ui_common.py` owns reusable wx accessibility helpers.

## Desktop Tabs

Each major desktop tab is a mixin inherited by `ViperDashboard`.

- `viper_ui_dashboard.py`: main dashboard and status overview.
- `viper_ui_doorbell.py`: doorbell setup buttons, video settings, full-flow tests, and manual video analysis UI.
- `viper_ui_setup_status.py`: setup status command center, readiness checklist, setup backups, troubleshooting recipes, and Test Everything.
- `viper_ui_setup_wizard.py`: the guided Home Assistant/Ring/speaker/TTS setup wizard dialogs.
- `viper_ui_speakers.py`: speaker management UI.
- `viper_ui_tts.py`: TTS and voice behavior UI.
- `viper_ui_device_tools.py`: utility/device tooling tab.
- `viper_ui_fridge.py`: refrigerator and ice maker UI.
- `viper_ui_hvac.py`: heat pump UI.
- `viper_ui_vacuum.py`: vacuum UI.
- `viper_ui_diagnostics.py`: diagnostics, health checks, Matter checks, and support bundle UI.

## Home Assistant And Devices

- `viper_ha_listener.py` listens to Home Assistant websocket/poll events and converts them into Viper actions.
- `viper_ha_client.py` is the shared low-level Home Assistant REST client.
- `viper_ha_recovery.py` and PowerShell watchdog scripts diagnose and repair the VirtualBox Home Assistant install.
- `viper_hvac.py` contains heat pump/Home Assistant entity logic.
- `viper_vacuum.py` contains Roborock action and mode helpers.
- `viper_matter.py` installs/checks the Home Assistant Matterbridge package and Viper voice-assistant entities.

## Web And Remote

- `viper_remote_web.py` owns the browser remote page, browser remote form posts, webhook routes, diagnostics web routes, and Cinderella webhook.
- `viper_remote_api.py` owns the JSON control API routes used by Home Assistant/Matter integrations for arm/disarm, global mute, speaker enable switches, and ice maker enable state.
- `templates/remote.html` is the browser remote UI.

## Release Safety

When adding a new source module that the packaged app needs, update:

- `build_exe.ps1`
- `viper_release_audit.py`
- Source-scanning tests in `tests/test_viper_release.py`, if they inspect desktop UI text.
- `accessibility_report.py`, if the file contains desktop controls that should be checked.
