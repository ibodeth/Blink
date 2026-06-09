# Blink Skills

Blink's capabilities are split into small, self-contained **skills** instead of
one giant system prompt. Each skill is a single Markdown file in this folder.

## Format

Every skill file looks like this:

```markdown
---
name: Take Screenshot
description: Use when the user asks to capture or screenshot their screen.
keywords: screenshot, screen capture, capture screen
---

# Take Screenshot

Step-by-step instructions and the exact JSON command(s) Blink should emit.
```

### Front matter fields

| Field | Required | Purpose |
| --- | --- | --- |
| `name` | yes | Human-readable skill name (shown in the catalog). |
| `description` | yes | One sentence: *when* to use this skill. The model reads this to decide. |
| `keywords` | no | Comma-separated terms for fast local matching. |

The body (everything after the second `---`) is the full instruction set. It is
loaded **on demand** only when the skill is selected, so the base prompt stays
small (progressive disclosure).

## How selection works

1. On every turn, Blink injects a compact catalog (`name: description` for every
   skill) into the prompt, so it always knows which skills exist.
2. If a skill matches the request, the model emits
   `{"type": "skill", "name": "<skill name>"}`.
3. Blink loads that skill's full body and re-runs the turn with those
   instructions in context, then executes the resulting commands.

## Adding skills

- **Manually:** drop a new `.md` file here using the format above. It is picked
  up on the next reload (or restart).
- **Automatically:** Blink can author its own skill after doing something new by
  emitting a `save_skill` command — see `skill-authoring.md`.

> These starter skills were adapted to Blink's JSON command contract from the
> common open-source "Agent Skills" (`SKILL.md`) pattern. Add or edit freely.
