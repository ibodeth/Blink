---
name: Music Control
description: Use when the user wants to play, pause, resume, stop, or skip music or a song.
keywords: play, song, music, pause, resume, stop music, next track, skip, put on
---

# Music Control

Control playback with the `music` command. The `action` is one of
`play | pause | resume | stop | next`.

## Play a track

```json
{ "type": "music", "action": "play", "target": "Bohemian Rhapsody",
  "response": "Sure, playing Bohemian Rhapsody.", "lang": "en" }
```

## Pause / resume / stop / next

```json
{ "type": "music", "action": "pause", "response": "Paused.", "lang": "en" }
```

## Guidelines

- For "play X", put the song/artist/query in `target`.
- For pause/resume/stop/next, omit `target`.
- Keep the spoken `response` short and natural.
- If music is already playing (see the "Active music" context), "stop the song"
  means `action: stop`, and "skip" means `action: next`.
