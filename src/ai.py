"""AI intent engine (OpenRouter).

Responsibilities:

* Maintain short-term conversation context (:class:`ContextManager`).
* Turn a user utterance + context into a validated JSON action queue via
  :class:`OpenRouterClient`.

Hardening vs. the legacy version:

* The system prompt is **English-only**; the ``lang`` field is always ``"en"``.
* Network access uses a pooled :class:`requests.Session` with timeouts.
* Responses are repaired (tolerant JSON extraction) **and** validated with the
  pydantic :class:`~src.schemas.CommandQueue` schema before use.
* Network/timeout failures raise :class:`OfflineError` so callers can fall back
  to the local keyword engine instead of crashing.
* Secrets are never logged.
"""

from __future__ import annotations

import json
import platform
from typing import Any, Dict, List, Optional

import requests

from src.config import get_settings
from src.schemas import CommandQueue
from src.utils import get_logger

log = get_logger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT = 15.0
MAX_HISTORY = 20

# Ordered fallbacks tried when the configured model fails.
FALLBACK_MODELS: List[str] = [
    "moonshotai/kimi-k2.6:free",
    "qwen/qwen3-coder:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "google/gemma-4-31b-it:free",
    "openrouter/auto",
]


class AIServiceError(Exception):
    """Raised when the AI service returns an unusable response."""


class OfflineError(AIServiceError):
    """Raised when the AI service is unreachable (network/timeout).

    Callers should fall back to the local keyword/command engine.
    """


# ---------------------------------------------------------------------------
# Tolerant JSON extraction / repair
# ---------------------------------------------------------------------------
def repair_json(text: str) -> str:
    """Best-effort extraction of a JSON object/array from an LLM response.

    Models sometimes wrap JSON in markdown fences or trailing prose. This pulls
    out the first balanced ``{...}`` / ``[...]`` region, ignoring braces inside
    string literals. Returns the original text if nothing balanced is found.
    """
    if not text:
        return text

    cleaned = text.strip()
    # Strip markdown code fences if present.
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]

    start = -1
    opener = closer = ""
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            start = i
            opener = ch
            closer = "}" if ch == "{" else "]"
            break
    if start == -1:
        return text

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]
    # Unbalanced: return from the first opener (json.loads will surface error).
    return cleaned[start:]


SYSTEM_PROMPT = """You are Blink, a local desktop voice assistant similar to Jarvis.
You convert the user's request into a strict JSON action queue that the host
application executes. Respond with raw JSON ONLY - do NOT wrap the JSON in
markdown code blocks (no ```json ... ``` fences) and do NOT add any prose
before or after it. Markdown formatting, lists and newlines ARE allowed
*inside* the JSON string fields (e.g. the "response" field) when helpful.

Output contract:
{
  "queue": [ { "type": "<command_type>", ... }, ... ]
}

Global rules:
- ALWAYS respond in professional English. Every item must include "lang": "en".
- Support Turkish queries from the user (e.g., "ney 2 şuan" means "what is 2 right now?"). Translate them internally to English, resolve using the conversation history/telemetry, and respond in English.
- The queue may contain multiple ordered actions.
- Keep spoken "response" fields short, natural and conversational, EXCEPT when the user explicitly asks for a list, history, or detailed information, in which case you must provide the complete requested details.
- CONVERSATION HISTORY RULE (highest priority): When the user asks for conversation history or recent messages in any language (e.g. "son 5 mesajı oku", "son 15 konuşmayı yaz", "whats past 5 messages", "read last 10 messages"), you MUST:
  1. Find the "Recent conversation:" section in the second system message.
  2. Copy the lines VERBATIM from that section — do NOT invent, summarize, or replace with placeholders.
  3. Return a single chat command whose "response" field contains those copied lines, one per line, in chronological order.
  4. Example: if "Recent conversation:" shows "user: hello\nblink: Hi there\nuser: what time is it", and the user asks for last 3 messages, respond with: {"type":"chat","response":"user: hello\nblink: Hi there\nuser: what time is it","lang":"en"}
  5. If fewer turns exist than requested, list all available turns.
  6. NEVER output numbered placeholders like '2. (No previous messages available)' — only real turns from the context.
- Never invent system state; rely on the telemetry and context provided.

Command types:
1. chat       - conversational reply.
   { "type": "chat", "response": "Hello, how can I help?", "lang": "en" }

2. music      - control playback. action: play | pause | resume | stop | next.
   { "type": "music", "action": "play", "target": "Bohemian Rhapsody",
     "response": "Sure, playing Bohemian Rhapsody.", "lang": "en" }

3. app        - open an installed application or system setting.
   { "type": "app", "target": "spotify", "response": "Opening Spotify.", "lang": "en" }

4. system     - hardware/OS control. action examples: kill_process, toggle_wifi,
   toggle_bluetooth, set_volume, set_mute, set_brightness, get_volume,
   get_brightness, lock, sleep, shutdown, restart, screenshot.
   { "type": "system", "action": "kill_process", "target": "spotify", "lang": "en" }

5. callback   - request host-side execution of a shell/Python snippet. Only use
   when no other command type fits. The host may refuse for security reasons.
   { "type": "callback", "code": "<snippet>", "response": "On it.", "lang": "en" }

6. reminder   - schedule a timed reminder.
   { "type": "reminder", "seconds": 3600, "message": "Meeting starts",
     "response": "I'll remind you in one hour.", "lang": "en" }

7. weather    - fetch weather for a city.
   { "type": "weather", "target": "Istanbul", "lang": "en" }

8. memory     - persist or remove a user fact. action: save | delete.
   { "type": "memory", "action": "delete", "key": "favorite_color",
     "response": "I've removed your favorite color from memory.", "lang": "en" }

9. skill      - load the full instructions for one of your skills before acting.
   { "type": "skill", "name": "Weather Reporting", "lang": "en" }

10. save_skill - teach yourself a new, reusable skill after succeeding at a
    novel task. instructions is the Markdown body (use \\n for line breaks).
    { "type": "save_skill", "name": "Clear Downloads",
      "description": "Use when the user wants to empty their Downloads folder.",
      "instructions": "# Clear Downloads\\n\\nEmit a system run_command that ...",
      "response": "Saved as a new skill.", "lang": "en" }

Skills:
- The "Available skills" context block lists your skills as "name: description".
  It tells you WHAT each skill is for, not HOW to do it.
- When a request matches a skill, FIRST return a single { "type": "skill",
  "name": "<exact skill name>" } command. The host loads that skill's full
  instructions and asks you again; THEN return the real command queue.
- DO NOT request a skill that is not listed in the "Available skills" block. If no available skill matches the request, execute it using the standard command types (e.g., chat, callback, etc.) directly.
- If a skill's full instructions are already provided below ("Loaded skill"),
  do NOT request a skill again - just produce the final command queue.
- After completing a brand-new reusable task that no skill covered, you may use
  save_skill to remember how to do it next time.
"""


class ContextManager:
    """Bounded short-term conversation memory (list of {role, text})."""

    def __init__(self, max_history: int = MAX_HISTORY) -> None:
        self.max_history = max_history
        self.history: List[Dict[str, str]] = []

    def add(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.history.append({"role": role, "text": text})
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]

    def seed(self, turns: List[Dict[str, str]]) -> None:
        """Replace history with persisted turns (used on boot)."""
        self.history = list(turns)[-self.max_history :]

    def formatted(self) -> str:
        if not self.history:
            return "(no prior context)"
        return "\n".join(f"{t['role']}: {t['text']}" for t in self.history)


class OpenRouterClient:
    """Thin, resilient OpenRouter chat client returning a validated queue."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "HTTP-Referer": "https://github.com/ibodeth/Blink",
                "X-Title": "Blink Assistant",
                "Content-Type": "application/json",
            }
        )

    def _models_to_try(self, model: str) -> List[str]:
        ordered = [model] + [m for m in FALLBACK_MODELS if m != model]
        return ordered

    def generate_command(
        self,
        user_input: str,
        user_name: str = "",
        long_term_mem: str = "",
        short_term_ctx: str | List[Dict[str, str]] = "",
        now_str: str = "",
        active_music_info: str = "",
        system_telemetry: str = "",
        agent_tools_info: str = "",
        workspace_cache: str = "",
        skills_catalog: str = "",
        skill_instructions: str = "",
    ) -> Dict[str, Any]:
        """Return a validated ``{"queue": [...]}`` dict.

        Raises :class:`OfflineError` on network failure and
        :class:`AIServiceError` on a non-recoverable bad response.
        """
        settings = get_settings()
        if not settings.has_api_key:
            raise AIServiceError("OpenRouter API key is not configured.")

        # Normalize short_term_ctx to both a flat string and a list of dictionary turns
        if isinstance(short_term_ctx, list):
            history_list = short_term_ctx
            if not history_list:
                short_term_ctx_str = "(none)"
            else:
                short_term_ctx_str = "\n".join(
                    f"{t.get('role', 'unknown')}: {t.get('text', '')}" for t in history_list
                )
        else:
            short_term_ctx_str = short_term_ctx or "(none)"
            history_list = []
            if short_term_ctx and short_term_ctx != "(no prior context)":
                for line in short_term_ctx.strip().split("\n"):
                    if ":" in line:
                        role_part, text_part = line.split(":", 1)
                        role_part = role_part.strip().lower()
                        text_part = text_part.strip()
                        if role_part in ("user", "blink", "system", "assistant"):
                            history_list.append({"role": role_part, "text": text_part})

        context_block = (
            f"Current time: {now_str}\n"
            f"User name: {user_name}\n"
            f"Operating system: {platform.system()} {platform.release()}\n\n"
            f"Long-term memory:\n{long_term_mem or '(none)'}\n\n"
            f"Recent conversation:\n{short_term_ctx_str}\n\n"
            f"Active music: {active_music_info or '(none)'}\n\n"
            f"System telemetry:\n{system_telemetry or '(none)'}\n\n"
            f"Available agent tools:\n{agent_tools_info or '(none)'}\n\n"
            f"Available skills:\n{skills_catalog or '(none)'}\n\n"
            f"Workspace (apps & files):\n{workspace_cache or '(none)'}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": context_block},
        ]
        if skill_instructions:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Loaded skill instructions (follow these to produce the "
                        "final command queue; do not request another skill):\n"
                        + skill_instructions
                    ),
                }
            )

        # Enforce strict alternating user/assistant roles.
        # Merge consecutive turns of the same mapped role.
        clean_history = []
        for turn in history_list:
            role = turn.get("role")
            text = turn.get("text", "")
            if not text:
                continue

            if role == "user":
                mapped_role = "user"
                content = text
            elif role == "system":
                mapped_role = "user"
                content = f"[System Event: {text}]"
            elif role in ("blink", "assistant"):
                mapped_role = "assistant"
                content = json.dumps({"queue": [{"type": "chat", "response": text, "lang": "en"}]})
            else:
                continue

            if clean_history and clean_history[-1]["role"] == mapped_role:
                # Merge consecutive turns of the same role
                if mapped_role == "user":
                    clean_history[-1]["content"] += "\n" + content
                else:
                    # For assistant, we can merge responses by combining the chat actions
                    try:
                        existing = json.loads(clean_history[-1]["content"])
                        new_data = json.loads(content)
                        existing["queue"].extend(new_data.get("queue", []))
                        clean_history[-1]["content"] = json.dumps(existing)
                    except Exception:
                        clean_history[-1]["content"] += "\n" + content
            else:
                clean_history.append({"role": mapped_role, "content": content})

        # Ensure the conversation starts with a user turn
        if clean_history and clean_history[0]["role"] == "assistant":
            clean_history.pop(0)

        for turn in clean_history:
            messages.append(turn)

        messages.append({"role": "user", "content": user_input})

        log.info("Prompt sent to OpenRouter:\n%s", json.dumps(messages, indent=2))

        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
        last_error: Optional[Exception] = None
        network_failed = False

        for model in self._models_to_try(settings.model):
            # Note: response_format json_object is intentionally omitted.
            # Several free/auto models ignore context when forced into strict
            # JSON mode, causing hallucinated or empty conversation history.
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
            }
            try:
                resp = self._session.post(
                    API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                network_failed = True
                last_error = exc
                log.warning("Network error contacting OpenRouter (model=%s): %s", model, exc)
                continue
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Request error (model=%s): %s", model, exc)
                continue

            if resp.status_code != 200:
                last_error = AIServiceError(f"HTTP {resp.status_code}")
                log.warning("OpenRouter returned HTTP %s for model %s", resp.status_code, model)
                continue

            try:
                content = resp.json()["choices"][0]["message"]["content"]
                log.info("OpenRouter response content:\n%s", content)
            except (ValueError, KeyError, IndexError) as exc:
                last_error = AIServiceError(f"Malformed response envelope: {exc}")
                log.warning("Malformed OpenRouter envelope for model %s", model)
                continue

            queue = self._parse_and_validate(content)
            if queue is not None:
                log.info("AI queue generated", extra={"context": {"model": model, "items": len(queue['queue'])}})
                return queue
            last_error = AIServiceError("Could not parse a valid command queue.")

        if network_failed:
            raise OfflineError(str(last_error) if last_error else "AI service unreachable")
        raise AIServiceError(str(last_error) if last_error else "AI request failed")

    @staticmethod
    def _parse_and_validate(content: str) -> Optional[Dict[str, Any]]:
        try:
            raw = json.loads(repair_json(content))
        except (ValueError, TypeError):
            return None
        # Tolerate a bare list or a single command object.
        if isinstance(raw, list):
            raw = {"queue": raw}
        elif isinstance(raw, dict) and "queue" not in raw and "type" in raw:
            raw = {"queue": [raw]}
        try:
            validated = CommandQueue.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError
            log.warning("AI response failed schema validation: %s", exc)
            return None
        return validated.model_dump(exclude_none=True)
