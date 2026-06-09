---
name: Coding and Automation
description: Use for tasks that require running a shell command or a Python snippet on the host, or multi-step automation.
keywords: run, execute, script, python, command line, terminal, automate, compute, calculate this
---

# Coding and Automation

When a request needs real computation or host actions that no other command
type covers, run code on the host.

## Preferred: explicit system execution

```json
{ "type": "system", "action": "run_command", "code": "echo hello",
  "response": "Running that now.", "lang": "en" }
```

```json
{ "type": "system", "action": "execute_python",
  "code": "print(sum(range(101)))", "response": "Calculating.", "lang": "en" }
```

## Autonomous callback (only when nothing else fits)

```json
{ "type": "callback", "code": "<shell or python>", "response": "On it.", "lang": "en" }
```

The host executes the callback, captures its output, and feeds the **result**
back to you as `[CALLBACK RESULT]: ...`. When you receive that, respond with a
plain `chat` summarising the outcome — do **not** emit another callback for the
same task (there is a strict depth limit).

## Guidelines

- Host code execution may be disabled for security; if it is refused, explain
  briefly via a `chat` command.
- Keep snippets minimal and side-effect-aware. Never fabricate output — wait for
  the real result.
- **Self-Feedback / Graphical Scripts:** If you write code that launches a GUI (like Tkinter, Pygame) or blocks, ALWAYS add print statements (e.g. `print('Window opened successfully')` followed by `sys.stdout.flush()`) BEFORE entering the mainloop or blocking loop. The host captures stdout even if the execution times out, providing you with feedback that the code initialized.
- If you complete a new, reusable multi-step task this way, consider saving it
  as a skill (see the **Skill Authoring** skill).

