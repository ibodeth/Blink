---
name: System Control
description: Use for hardware/OS control - volume, brightness, mute, Wi-Fi, Bluetooth, lock, sleep, shutdown, restart.
keywords: volume, louder, quieter, mute, brightness, wifi, bluetooth, lock, sleep, shutdown, restart, turn off
---

# System Control

Control the machine with the `system` command and an `action`.

Common actions: `set_volume`, `set_mute`, `set_brightness`, `toggle_wifi`,
`toggle_bluetooth`, `lock`, `sleep`, `shutdown`, `restart`.

## Volume / brightness (numeric target)

`target` may be a number (0-100). Both strings and integers are accepted.

```json
{ "type": "system", "action": "set_volume", "target": 30,
  "response": "Volume set to 30 percent.", "lang": "en" }
```

## Toggles (on/off)

```json
{ "type": "system", "action": "toggle_wifi", "target": "off",
  "response": "Turning Wi-Fi off.", "lang": "en" }
```

## Power / session

```json
{ "type": "system", "action": "lock", "response": "Locking the screen.", "lang": "en" }
```

## Guidelines

- "louder/quieter" without a number: pick a sensible step relative to context.
- Use `"on"`/`"off"` as the `target` for `toggle_wifi` / `toggle_bluetooth`.
- Destructive actions (`shutdown`, `restart`) should include a brief confirming
  `response`.
- For taking a screenshot, use the **Take Screenshot** skill.
