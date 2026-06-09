---
name: Weather Reporting
description: Use when the user asks about the weather, forecast, temperature, or conditions for a place.
keywords: weather, forecast, temperature, rain, snow, sunny, how hot, how cold, degrees
---

# Weather Reporting

Use this skill when the user wants current weather or a forecast.

## What to emit

Emit a single `weather` command with the target city. If the user does not name
a city, use the user's last known city from long-term memory, otherwise default
to their configured location.

```json
{ "type": "weather", "target": "Istanbul", "lang": "en" }
```

## Guidelines

- Extract the city/place name from the request ("weather in Berlin" -> `Berlin`).
- Do not invent temperatures or conditions; the host fetches real data and
  speaks the result.
- For "do I need an umbrella / a jacket" style questions, still emit the
  `weather` command for the relevant city; the host's reply covers conditions.
