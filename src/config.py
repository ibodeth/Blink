"""Typed configuration & security policy backed by ``pydantic-settings``.

All runtime configuration is parsed and validated from environment variables
(and an optional ``.env`` file). The rest of the application reads a single
immutable :class:`Settings` instance via :func:`get_settings` rather than
scattering ``os.getenv`` calls.

The :class:`Settings` object also owns the **security policy**: whether the AI
is allowed to execute generated shell/Python (off by default), the command
timeout, the loopback bind address, the WebSocket auth token, and the allowed
CORS/WebSocket origins.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import List, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.utils import get_logger

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("BLINK_ENV_FILE", str(PROJECT_ROOT / ".env")))

DEFAULT_MODEL = "openrouter/auto"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Credentials / model ----------------------------------------------
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    model: str = Field(default=DEFAULT_MODEL, alias="OPENROUTER_MODEL")

    # --- Server -----------------------------------------------------------
    host: str = Field(default=DEFAULT_HOST, alias="BLINK_HOST")
    port: int = Field(default=DEFAULT_PORT, alias="BLINK_PORT", ge=1, le=65535)

    # --- Security policy --------------------------------------------------
    enable_code_execution: bool = Field(default=False, alias="BLINK_ENABLE_CODE_EXECUTION")
    command_timeout: int = Field(default=10, alias="BLINK_COMMAND_TIMEOUT", ge=1, le=120)
    ws_auth_token: str = Field(default="", alias="BLINK_WS_TOKEN")
    allowed_origins: Any = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        alias="BLINK_ALLOWED_ORIGINS",
    )

    # --- Logging ----------------------------------------------------------
    log_level: str = Field(default="INFO", alias="BLINK_LOG_LEVEL")

    @field_validator("openrouter_api_key", "model", "host", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ws_auth_token", mode="before")
    @classmethod
    def _default_token(cls, value):
        # Auto-generate a per-process token when the operator did not pin one,
        # so privileged WebSocket actions are still authenticated locally.
        if value:
            return value
        return secrets.token_urlsafe(24)

    @property
    def has_api_key(self) -> bool:
        return bool(self.openrouter_api_key)

    def __init__(self, *args, **kwargs):
        env_file_path = os.getenv("BLINK_ENV_FILE")
        if env_file_path:
            kwargs.setdefault("_env_file", env_file_path)
        else:
            kwargs.setdefault("_env_file", str(ENV_FILE))
        super().__init__(*args, **kwargs)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached, validated :class:`Settings` snapshot."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read settings (used after saving the API key)."""
    get_settings.cache_clear()
    return get_settings()


def load_config() -> bool:
    """Backwards-compatible boolean: ``True`` when an API key is configured."""
    settings = reload_settings()
    if settings.has_api_key:
        os.environ["OPENROUTER_API_KEY"] = settings.openrouter_api_key
        os.environ["OPENROUTER_MODEL"] = settings.model
        return True
    return False


def save_config(openrouter_key: str, model: str = DEFAULT_MODEL) -> None:
    """Persist credentials to ``.env`` atomically with 0600 permissions."""
    openrouter_key = (openrouter_key or "").strip()
    model = (model or DEFAULT_MODEL).strip()

    if not openrouter_key:
        log.warning("save_config called with an empty API key; ignoring.")
        return

    payload = (
        "# Blink configuration. Contains secrets - never commit this file.\n"
        f"OPENROUTER_API_KEY={openrouter_key}\n"
        f"OPENROUTER_MODEL={model}\n"
    )

    fd, tmp_path = tempfile.mkstemp(dir=str(PROJECT_ROOT), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, ENV_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    os.environ["OPENROUTER_API_KEY"] = openrouter_key
    os.environ["OPENROUTER_MODEL"] = model
    reload_settings()
    log.info("Configuration saved to .env (credentials redacted).")
