---
name: Take Screenshot
description: Use when the user asks to capture, screenshot, or take a picture of their screen.
keywords: screenshot, screen capture, capture screen, grab screen, take a picture of my screen
---

# Take Screenshot

Capture the screen with the `system` command, action `screenshot`.

```json
{ "type": "system", "action": "screenshot",
  "response": "Taking a screenshot now.", "lang": "en" }
```

## Guidelines

- Keep the spoken response short.
- The host handles where the file is saved; do not invent a path.
