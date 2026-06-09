"""AI engine tests with a fully mocked OpenRouter HTTP layer."""

import requests

from src.ai import (
    AIServiceError,
    ContextManager,
    OfflineError,
    OpenRouterClient,
    repair_json,
)


# --- repair_json -----------------------------------------------------------
def test_repair_json_strips_markdown_fence():
    raw = '```json\n{"queue": [{"type": "chat", "response": "hi"}]}\n```'
    import json
    parsed = json.loads(repair_json(raw))
    assert parsed["queue"][0]["type"] == "chat"


def test_repair_json_extracts_embedded_object():
    raw = 'Sure! {"queue": []} hope that helps'
    assert repair_json(raw).strip() == '{"queue": []}'


def test_repair_json_ignores_braces_in_strings():
    raw = '{"queue": [{"type": "chat", "response": "use } carefully"}]}'
    import json
    parsed = json.loads(repair_json(raw))
    assert parsed["queue"][0]["response"] == "use } carefully"


# --- ContextManager --------------------------------------------------------
def test_context_manager_bounded_and_formatted():
    ctx = ContextManager(max_history=3)
    for i in range(5):
        ctx.add("user", f"msg {i}")
    assert len(ctx.history) == 3
    assert "msg 4" in ctx.formatted()
    assert "msg 0" not in ctx.formatted()


def test_context_manager_seed_replaces_history():
    ctx = ContextManager()
    ctx.seed([{"role": "user", "text": "restored"}])
    assert "restored" in ctx.formatted()


# --- OpenRouterClient (mocked network) ------------------------------------
class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def _configure_key(monkeypatch):
    import src.config as config
    config.get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    config.get_settings.cache_clear()


def test_generate_command_returns_validated_queue(monkeypatch):
    _configure_key(monkeypatch)
    client = OpenRouterClient()

    content = '{"queue": [{"type": "chat", "response": "Hello!", "lang": "en"}]}'
    fake = _FakeResponse({"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(client._session, "post", lambda *a, **k: fake)

    result = client.generate_command("hi there")
    assert "queue" in result
    assert result["queue"][0]["type"] == "chat"
    assert result["queue"][0]["response"] == "Hello!"


def test_generate_command_raises_offline_on_network_error(monkeypatch):
    _configure_key(monkeypatch)
    client = OpenRouterClient()

    def _boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(client._session, "post", _boom)

    try:
        client.generate_command("hi")
        assert False, "expected OfflineError"
    except OfflineError:
        pass


def test_generate_command_requires_api_key(monkeypatch):
    import src.config as config
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config.get_settings.cache_clear()
    client = OpenRouterClient()
    try:
        client.generate_command("hi")
        assert False, "expected AIServiceError"
    except AIServiceError:
        pass
