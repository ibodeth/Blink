---
name: Memory Management
description: Use when the user asks Blink to remember a personal fact, or to forget something it knows.
keywords: remember, my name is, save this, forget, don't remember, note that, keep in mind
---

# Memory Management

Persist or remove long-term facts with the `memory` command.
`action` is `save` or `delete`.

## Save a fact

```json
{ "type": "memory", "action": "save", "key": "name", "value": "Ada",
  "category": "prefs", "response": "Got it, I'll remember your name is Ada.",
  "lang": "en" }
```

## Forget a fact

```json
{ "type": "memory", "action": "delete", "key": "favorite_color",
  "response": "I've removed your favorite color from memory.", "lang": "en" }
```

## Guidelines

- Choose a short, stable `key` (e.g. `name`, `city`, `favorite_color`).
- Categories control retention: `prefs`, `general`, `agent` never expire;
  `status` expires after 3 days; `events` after 7 days. Use `prefs` for durable
  personal facts.
- To forget everything, use `key: "*"` with `action: delete`.
- Stored facts appear in the "Long-term memory" context block on later turns.
