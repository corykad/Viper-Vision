# Viper Core On Home Assistant Green

Viper Core is the path for moving Viper's always-on jobs onto Home Assistant Green while keeping the Windows app as an optional setup and packaging tool.

## Why Build This Before The Green Arrives

The hard part is not copying files to the Green. The hard part is separating the jobs that must always run from the Windows desktop UI. The add-on scaffold lets us prove the shape now:

- Home Assistant starts Viper Core automatically.
- Viper Core can use Home Assistant's Supervisor token, so users do not need to paste another long-lived token into the add-on.
- Viper Core has a web control panel at `http://<home-assistant-ip>:8099/`.
- Viper Core can manage speakers, chime files, doorbell AI settings, Pushover keys, heat pumps, the vacuum, and refrigerator basics without the Windows app running.
- Viper Core has a clean place to keep doorbell, fridge, vacuum, ice maker, HVAC, and Pushover logic running on Home Assistant.

## What Runs Where

Home Assistant Green runs or is being prepared to run:

- Doorbell event listening.
- Refrigerator and freezer chimes.
- Ice maker state and counter logic.
- Roborock status announcements.
- Heat pump online checks and command confirmation.
- Pushover outage notifications.
- Speaker routing and mute controls.
- Chime file hosting and event-to-chime assignment.
- Gemini doorbell image descriptions.
- Matterbridge and Home Assistant package logic.

Windows Viper should keep:

- The accessible desktop setup wizard.
- Release packaging.
- Optional diagnostics while the PC app still exists.

## Install Plan When The Green Arrives

1. Restore or migrate the existing Home Assistant backup to the Green.
2. Confirm Home Assistant, SmartThings, Roborock, Ring, Matterbridge, and ESPHome entities are present.
3. Copy `ha_addons` into the Home Assistant add-ons folder.
4. In Home Assistant, open **Settings**, **Add-ons**, **Add-on Store**, then install **Viper Core** from local add-ons.
5. Start Viper Core and open its web panel at `http://<home-assistant-ip>:8099/`.
6. Copy `ha_addons/viper_core/viper_core_package.yaml` into `/config/packages/viper_core_package.yaml`.
7. Restart Home Assistant or reload packages.
8. Confirm speakers, chimes, Pushover, Gemini, doorbell routes, heat pumps, vacuum, and ice maker controls in the Viper Core web panel.

## Current Web Panel

Open `http://<home-assistant-ip>:8099/` from a browser on the LAN. The page includes:

- Arm/disarm and global mute.
- Speaker enable/disable controls and doorbell/fridge/utilities routes.
- Chime upload, delete, test, and event assignment.
- Gemini, camera, external URL, and Pushover settings.
- Current heat pump, vacuum, and refrigerator status.
- Heat pump, vacuum, ice maker, doorbell, fridge, freezer, broadcast, and Pushover test controls.

The Pushover test is intentionally silent: it sends a phone push without speaking on any speaker.

## Direct Add-on Endpoints

Home Assistant automations can call these endpoints on the add-on container:

- `POST http://local-viper-core:8099/event/doorbell`
- `POST http://local-viper-core:8099/event/fridge`
- `POST http://local-viper-core:8099/event/vacuum`
- `POST http://local-viper-core:8099/event/ice_maker`
- `POST http://local-viper-core:8099/event/hvac`

The included package file creates `rest_command.viper_core_*` commands for those endpoints.
