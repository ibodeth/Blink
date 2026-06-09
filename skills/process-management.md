---
name: Process Management
description: Use when the user wants to close, quit, kill, or terminate a running application or process.
keywords: close, kill, quit, exit, terminate, stop app, force quit, end task
---

# Process Management

Close a running application with the `system` command, action `kill_process`.
The target is the process/app name.

```json
{ "type": "system", "action": "kill_process", "target": "spotify", "lang": "en" }
```

## Guidelines

- The host injects a `[RUNNING PROCESSES]` list into the request for close/kill
  intents. Match the user's wording to the closest **actually running** process
  name from that list.
- If nothing matches, reply with a short `chat` command saying you could not
  find that app running.
- "Close everything" is risky — prefer to confirm or target a specific app.
- **Self-Protection:** NEVER attempt to kill the main `python` process or the `Blink` assistant itself, as this will crash the assistant application. Target only user apps (like browsers, players, etc.).
- To *open* an app, use the **App Launcher** skill.

