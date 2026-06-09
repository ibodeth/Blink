---
name: Reminders
description: Use when the user wants to be reminded of something after a delay or at a relative time.
keywords: remind, reminder, in minutes, in an hour, alarm, timer, don't let me forget
---

# Reminders

Schedule a timed reminder with the `reminder` command. `seconds` is the delay
from now; `message` is what to say when it fires.

```json
{ "type": "reminder", "seconds": 3600, "message": "Your meeting starts",
  "response": "I'll remind you in one hour.", "lang": "en" }
```

## Guidelines

- Convert natural language to seconds: "in 10 minutes" -> 600, "in 2 hours" ->
  7200, "in 30 seconds" -> 30.
- Always include a clear `message` describing the reminder.
- Confirm in the `response` using the user's own units ("in one hour").
- If you cannot determine a duration, reply with a `chat` command asking for
  how long.
