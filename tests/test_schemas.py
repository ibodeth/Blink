"""Schema/validation tests for inbound WebSocket events and the AI queue."""

import pytest

from src.schemas import (
    CommandQueue,
    PRIVILEGED_EVENT_TYPES,
    make_event,
    parse_incoming,
)


def test_parse_manual_text_event():
    event = parse_incoming({"type": "manual_text", "text": "hello"})
    assert event.type == "manual_text"
    assert event.text == "hello"


def test_manual_text_rejects_empty():
    with pytest.raises(Exception):
        parse_incoming({"type": "manual_text", "text": ""})


def test_parse_system_action_preserves_fields():
    event = parse_incoming({
        "type": "system_action",
        "action": "connect_wifi",
        "ssid": "HomeNet",
        "password": "secret",
        "token": "abc",
    })
    assert event.action == "connect_wifi"
    assert event.ssid == "HomeNet"
    assert event.password == "secret"
    assert event.token == "abc"


def test_save_config_uses_openrouter_key():
    event = parse_incoming({
        "type": "save_config",
        "openrouter_key": "sk-or-x",
        "model": "openrouter/auto",
    })
    assert event.openrouter_key == "sk-or-x"
    assert event.model == "openrouter/auto"


def test_unknown_event_type_rejected():
    with pytest.raises(Exception):
        parse_incoming({"type": "definitely_not_a_real_event"})


def test_privileged_event_set():
    assert "system_action" in PRIVILEGED_EVENT_TYPES
    assert "save_config" in PRIVILEGED_EVENT_TYPES
    assert "manual_text" not in PRIVILEGED_EVENT_TYPES


def test_make_event_shape():
    payload = make_event("status", {"status": "idle"})
    assert payload["type"] == "status"
    assert payload["payload"]["status"] == "idle"


def test_command_queue_validation():
    q = CommandQueue.model_validate({"queue": [{"type": "chat", "response": "hi"}]})
    assert len(q.queue) == 1
    assert q.queue[0].type == "chat"
    assert q.queue[0].lang == "en"


def test_command_queue_rejects_bad_type():
    with pytest.raises(Exception):
        CommandQueue.model_validate({"queue": [{"type": "not_a_command"}]})
