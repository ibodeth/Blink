"""Shared pytest fixtures.

The project root is added to ``sys.path`` so ``import src...`` works without an
installed package, and configuration is reset between tests so the cached
``Settings`` singleton never leaks state across cases.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _clean_settings_cache(monkeypatch, tmp_path):
    """Isolate settings + working directory for every test."""
    # Point any file writes (logs, db) at a temp dir.
    monkeypatch.chdir(tmp_path)
    # Point config to a temp .env file to isolate from the developer's real .env
    monkeypatch.setenv("BLINK_ENV_FILE", str(tmp_path / ".env"))
    # Ensure a known-clean environment.
    for key in [
        "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "BLINK_HOST", "BLINK_PORT",
        "BLINK_ENABLE_CODE_EXECUTION", "BLINK_COMMAND_TIMEOUT", "BLINK_WS_TOKEN",
        "BLINK_ALLOWED_ORIGINS", "BLINK_LOG_LEVEL",
    ]:
        monkeypatch.delenv(key, raising=False)
    try:
        from src.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass
    yield
    try:
        from src.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass
