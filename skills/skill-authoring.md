---
name: Skill Authoring
description: Use after successfully completing a new, reusable task to save it as a brand new skill for next time.
keywords: save this as a skill, remember how to do this, learn this, create a skill, teach yourself
---

# Skill Authoring

When you accomplish something new and reusable that no existing skill covered,
teach yourself by saving a new skill. Emit a `save_skill` command:

```json
{ "type": "save_skill",
  "name": "Clear Downloads Folder",
  "description": "Use when the user wants to empty their Downloads folder.",
  "keywords": "clear downloads, empty downloads, clean downloads folder",
  "instructions": "# Clear Downloads Folder\n\nEmit a system run_command that deletes files in the Downloads directory, then confirm with a short response.",
  "response": "I've saved that as a new skill so I can do it instantly next time.",
  "lang": "en" }
```

## Required fields

- `name` — short, human-readable skill name.
- `description` — one sentence describing *when* to use it (this is what future
  you reads to decide).
- `instructions` — the Markdown body: the steps and the exact JSON command(s) to
  emit. Use `\n` for line breaks inside the JSON string.
- `keywords` (optional) — comma-separated trigger terms.

## When to do this

- Only after the task actually **succeeded** and is likely to recur.
- Do not duplicate an existing skill — check the skill catalog first.
- Keep instructions concrete: name the command `type`/`action` and show an
  example payload, just like the built-in skills.
- The new skill is saved as `skills/<slug>.md` and becomes available immediately.
