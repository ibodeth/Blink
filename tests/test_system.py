"""System-control safety tests (no real subprocess execution)."""

import src.config as config
from src.system import (
    SystemManager,
    _clamp_int,
    _sanitize_identifier,
)


def _reset_settings(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()


# --- sanitisation helpers --------------------------------------------------
def test_sanitize_identifier_strips_shell_metacharacters():
    assert _sanitize_identifier("my$(rm -rf)net") == "my(rm -rf)net"
    assert ";" not in _sanitize_identifier("a;b")
    assert "|" not in _sanitize_identifier("a|b")
    assert "`" not in _sanitize_identifier("a`b`")


def test_sanitize_identifier_truncates():
    assert len(_sanitize_identifier("x" * 500, max_len=10)) == 10


def test_clamp_int_bounds_and_default():
    assert _clamp_int(150, 0, 100, 50) == 100
    assert _clamp_int(-5, 0, 100, 50) == 0
    assert _clamp_int("not-a-number", 0, 100, 42) == 42
    assert _clamp_int(30, 0, 100, 50) == 30


# --- dangerous-pattern detection ------------------------------------------
def test_is_dangerous_detects_destructive_commands():
    assert SystemManager._is_dangerous("rm -rf /") is True
    assert SystemManager._is_dangerous("echo hello") is False


# --- code execution gating -------------------------------------------------
def test_run_command_blocked_by_default(monkeypatch):
    _reset_settings(monkeypatch)
    monkeypatch.delenv("BLINK_ENABLE_CODE_EXECUTION", raising=False)
    config.get_settings.cache_clear()
    sysmgr = SystemManager()
    result = sysmgr.run_command("echo hi")
    assert "disabled" in result.lower()


def test_execute_python_blocked_by_default(monkeypatch):
    _reset_settings(monkeypatch)
    monkeypatch.delenv("BLINK_ENABLE_CODE_EXECUTION", raising=False)
    config.get_settings.cache_clear()
    sysmgr = SystemManager()
    result = sysmgr.execute_python("print('hi')")
    assert "disabled" in result.lower()


def test_dangerous_command_blocked_even_when_enabled(monkeypatch):
    _reset_settings(monkeypatch, BLINK_ENABLE_CODE_EXECUTION="true")
    sysmgr = SystemManager()
    result = sysmgr.run_command("rm -rf /")
    assert "blocked" in result.lower()


def test_kill_process_sanitizes_and_handles_missing(monkeypatch):
    _reset_settings(monkeypatch)
    sysmgr = SystemManager()
    # A non-existent PID should return a string, never raise.
    result = sysmgr.kill_process(999999999)
    assert isinstance(result, str)


def test_empty_command_messages(monkeypatch):
    _reset_settings(monkeypatch, BLINK_ENABLE_CODE_EXECUTION="true")
    sysmgr = SystemManager()
    assert "No command" in sysmgr.run_command("")
    assert "No code" in sysmgr.execute_python("")


# --- execute() dispatcher: telemetry getters -------------------------------
def test_execute_get_volume_reports_current_volume(monkeypatch):
    _reset_settings(monkeypatch)
    sysmgr = SystemManager()
    monkeypatch.setattr(
        sysmgr, "get_audio_telemetry", lambda: {"volume": 42, "muted": False, "devices": []}
    )
    result = sysmgr.execute("get_volume", None)
    assert result == "Current volume is 42%."


def test_execute_get_brightness_reports_current_brightness(monkeypatch):
    _reset_settings(monkeypatch)
    sysmgr = SystemManager()
    monkeypatch.setattr(
        sysmgr, "get_display_telemetry", lambda: {"brightness": 73}
    )
    result = sysmgr.execute("get_brightness", None)
    assert result == "Current brightness is 73%."


def test_execute_unknown_action_returns_message(monkeypatch):
    _reset_settings(monkeypatch)
    sysmgr = SystemManager()
    result = sysmgr.execute("not_a_real_action", None)
    assert result == "Unknown action: not_a_real_action"
