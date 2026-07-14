# Viper Core On Home Assistant Green

Viper Core is the path for moving Viper's always-on jobs onto Home Assistant Green while keeping the Windows app as the accessible setup and control panel.

## Why Build This Before The Green Arrives

The hard part is not copying files to the Green. The hard part is separating the jobs that must always run from the Windows desktop UI. The add-on scaffold lets us prove the shape now:

- Home Assistant starts Viper Core automatically.
- Viper Core can use Home Assistant's Supervisor token, so users do not need to paste another long-lived token into the add-on.
- Viper Core has a health page we can open from Home Assistant.
- Viper Core has a clean place to move doorbell, fridge, vacuum, ice maker, HVAC, and Pushover logic.

## What Runs Where

Home Assistant Green should eventually run:

- Doorbell event listening.
- Refrigerator and freezer chimes.
- Ice maker state and counter logic.
- Roborock status announcements.
- Heat pump online checks and command confirmation.
- Pushover outage notifications.
- Matterbridge and Home Assistant package logic.

Windows Viper should keep:

- The accessible desktop setup wizard.
- Speaker and device discovery screens.
- Manual controls and diagnostics.
- Gemini setup and testing.
- Release packaging.

## Install Plan When The Green Arrives

1. Restore or migrate the existing Home Assistant backup to the Green.
2. Confirm Home Assistant, SmartThings, Roborock, Ring, Matterbridge, and ESPHome entities are present.
3. Copy `ha_addons` into the Home Assistant add-ons folder.
4. In Home Assistant, open **Settings**, **Add-ons**, **Add-on Store**, then install **Viper Core** from local add-ons.
5. Start Viper Core and open its health page.
6. Copy `ha_addons/viper_core/viper_core_package.yaml` into `/config/packages/viper_core_package.yaml`.
7. Restart Home Assistant or reload packages.
8. Move one always-on feature at a time from Windows Viper into Viper Core.

## First Migration Targets

Move these first because they hurt the most when the Windows PC or VirtualBox setup flakes out:

1. Fridge and freezer chime listener.
2. Doorbell event listener.
3. Ice maker counter.
4. Heat pump online/offline Pushover alerts.
5. Home Assistant and add-on health monitoring.

This keeps the migration boring and reversible. Each feature can be tested on the Green before removing the Windows-side fallback.

## Direct Add-on Endpoints

Home Assistant automations can call these endpoints on the add-on container:

- `POST http://viper_core:8099/event/doorbell`
- `POST http://viper_core:8099/event/fridge`
- `POST http://viper_core:8099/event/vacuum`
- `POST http://viper_core:8099/event/ice_maker`
- `POST http://viper_core:8099/event/hvac`

The included package file creates `rest_command.viper_core_*` commands for those endpoints.
