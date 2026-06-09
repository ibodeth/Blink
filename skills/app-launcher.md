---
name: App Launcher
description: Use when the user asks to open, launch, or start an installed application or a system setting.
keywords: open, launch, start, run app, bring up, fire up
---

# App Launcher

Open an installed application or system setting with the `app` command.

```json
{ "type": "app", "target": "spotify", "response": "Opening Spotify.", "lang": "en" }
```

## Guidelines

- Put the application or setting name in `target` (lowercase is fine).
- Prefer the name as the user said it; the host resolves it against the scanned
  workspace/app list in context.
- If the requested app is clearly not installed (not in the workspace context),
  you may still emit the command — the host reports "Could not find ..." if it is
  missing.
- To *close* an app instead of opening it, use the **Process Management** skill.
