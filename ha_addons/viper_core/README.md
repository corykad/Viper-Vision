# Viper Core

Viper Core is the Home Assistant add-on version of Viper's always-on brain.

This first version is intentionally small. It starts inside Home Assistant, reads its add-on options, exposes a health page, and checks that it can reach the Home Assistant API through the Supervisor token. That gives us a safe base to move doorbell, fridge, HVAC, vacuum, and notification logic onto Home Assistant Green one piece at a time.

## What This Add-on Does Now

- Starts automatically with Home Assistant.
- Uses the Home Assistant Supervisor token when available.
- Falls back to a configured Home Assistant URL and token when needed.
- Exposes a health page on the add-on ingress panel.
- Logs Home Assistant API connectivity every health check interval.
- Keeps secrets out of the health page.
- Receives Home Assistant events at `/event/doorbell`, `/event/fridge`, `/event/vacuum`, `/event/ice_maker`, and `/event/hvac`.
- Creates Home Assistant notifications or calls configured notification/speaker services.

## What Comes Next

The Windows Viper app should stay the friendly setup and control panel. Viper Core should eventually take over the jobs that must run even when the Windows PC is asleep, rebooting, or closed:

- Doorbell event listener and AI dispatch.
- Refrigerator and freezer door chimes.
- Speaker routing and mute state.
- Ice maker counter logic.
- Roborock status announcements.
- Heat pump presence and command confirmation.
- Pushover alerts for outages and repairs.

## Local Development

Run the Python module directly:

```powershell
python -m py_compile ha_addons\viper_core\viper_core\__main__.py ha_addons\viper_core\viper_core\config.py ha_addons\viper_core\viper_core\ha.py ha_addons\viper_core\viper_core\health_server.py
```

## Installing On Home Assistant Green

When the Green arrives, copy the `ha_addons` folder to the Home Assistant add-ons folder by Samba or SSH, then open **Settings**, **Add-ons**, **Add-on Store**, **Local add-ons**, and install **Viper Core**. Copy `viper_core_package.yaml` into `/config/packages` when you are ready for Home Assistant automations to call the add-on directly.

The add-on does not replace the Windows app yet. It is the foundation for moving the always-on pieces safely.
