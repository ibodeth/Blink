"""Pydantic schemas validating all data crossing a trust boundary.

These models are the single source of truth for:

* Inbound WebSocket events from the frontend (``IncomingEvent`` union).
* Outbound WebSocket events broadcast to the frontend (``OutgoingEvent``).
* The AI command queue returned by the LLM (``CommandQueue`` / ``Command``).
* System telemetry payloads.

Validating untrusted input here keeps parsing/validation logic out of the
business layer and prevents malformed or malicious payloads from reaching the
system-control code paths.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# AI command queue
# ---------------------------------------------------------------------------
CommandType = Literal[
    "chat", "callback", "music", "app", "system", "reminder", "weather", "memory",
    # Skill system: request a skill's full instructions, or save a new skill.
    "skill", "save_skill"
]


class Command(BaseModel):
    """A single action emitted by the AI engine.

    Extra keys are allowed because different command types carry different
    payloads (e.g. ``code`` for callbacks, ``seconds`` for reminders). The
    business layer reads them defensively.
    """

    model_config = ConfigDict(extra="allow")

    type: CommandType
    # ``lang`` is retained for schema compatibility but is always English now.
    lang: Literal["en"] = "en"
    action: Optional[str] = None
    # The AI sometimes returns numeric targets (e.g. a volume level of 10).
    # Accept both strings and integers so validation never rejects them.
    target: Optional[Union[str, int]] = None
    response: Optional[str] = None
    message: Optional[str] = None
    code: Optional[str] = None
    key: Optional[str] = None
    value: Optional[Any] = None
    seconds: Optional[int] = Field(default=None, ge=0)
    # Skill system fields (used by the "skill" and "save_skill" command types).
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    keywords: Optional[Union[str, List[str]]] = None


class CommandQueue(BaseModel):
    """The top-level object the LLM must return: ``{"queue": [...]}``."""

    queue: List[Command] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class Telemetry(BaseModel):
    model_config = ConfigDict(extra="allow")

    stats: Dict[str, Any] = Field(default_factory=dict)
    wifi: Dict[str, Any] = Field(default_factory=dict)
    bluetooth: Dict[str, Any] = Field(default_factory=dict)
    audio: Dict[str, Any] = Field(default_factory=dict)
    display: Dict[str, Any] = Field(default_factory=dict)
    processes: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Inbound WebSocket events (frontend -> backend)
# ---------------------------------------------------------------------------
class _AuthedEvent(BaseModel):
    """Base for events; an optional token authenticates privileged actions."""

    model_config = ConfigDict(extra="ignore")
    token: Optional[str] = None


class ManualTextEvent(_AuthedEvent):
    type: Literal["manual_text"]
    text: str = Field(min_length=1, max_length=4000)


class MusicControlEvent(_AuthedEvent):
    type: Literal["music_control"]
    action: Literal["toggle", "stop", "seek", "set_volume"]
    value: Optional[float] = None


class SystemActionEvent(_AuthedEvent):
    type: Literal["system_action"]
    action: str
    target: Optional[str] = None
    value: Optional[Any] = None
    pid: Optional[int] = None
    ssid: Optional[str] = Field(default=None, max_length=64)
    password: Optional[str] = Field(default=None, max_length=128)


class SaveConfigEvent(_AuthedEvent):
    type: Literal["save_config"]
    openrouter_key: str = Field(min_length=1, max_length=400)
    model: str = "openrouter/auto"


class GetConfigEvent(_AuthedEvent):
    type: Literal["get_config"]


IncomingEvent = Union[
    ManualTextEvent,
    MusicControlEvent,
    SystemActionEvent,
    SaveConfigEvent,
    GetConfigEvent,
]


class IncomingEnvelope(BaseModel):
    """Discriminated wrapper used to parse any inbound event safely."""

    event: IncomingEvent = Field(discriminator="type")


# Privileged inbound actions that require a valid auth token.
PRIVILEGED_EVENT_TYPES = {"system_action", "save_config"}


def parse_incoming(raw: Dict[str, Any]) -> IncomingEvent:
    """Validate a raw inbound dict into a typed event (raises on invalid)."""
    return IncomingEnvelope(event=raw).event


# ---------------------------------------------------------------------------
# Outbound WebSocket events (backend -> frontend)
# ---------------------------------------------------------------------------
class OutgoingEvent(BaseModel):
    """Generic outbound envelope: ``{"type": ..., "payload": {...}}``."""

    model_config = ConfigDict(extra="allow")

    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


def make_event(event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a validated outbound event dict ready for JSON serialisation."""
    return OutgoingEvent(type=event_type, payload=payload or {}).model_dump()
