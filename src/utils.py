"""Cross-cutting utilities: structured logging, secret redaction, networking.

This replaces the legacy ad-hoc ``print``/``log_debug`` helpers with the
standard :mod:`logging` module configured for enterprise use:

* A JSON formatter for machine-readable logs (one object per line).
* A human-readable console formatter.
* A rotating file handler (``logs/blink.log``, 5 x 2 MB).
* A logging *filter* that redacts secrets (API keys, bearer tokens, passwords)
  from every record, regardless of which logger emitted it.

Usage across the codebase::

    from src.utils import get_logger
    log = get_logger(__name__)
    log.info("started", extra={"context": {"port": 8000}})
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

__all__ = ["get_logger", "configure_logging", "redact", "find_available_port", "is_port_in_use"]

# Ensure stdout/stderr can render Unicode on legacy Windows code pages.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\"?\s*[=:]\s*\"?[^\s\"',}]+"),
]


def redact(message: object) -> str:
    """Return ``message`` as a string with detected secrets masked."""
    text = str(message)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class _RedactionFilter(logging.Filter):
    """Scrub secrets from the rendered message and string args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:
            # Never let logging crash the application.
            pass
        return True


class _JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload["context"] = context
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


_CONFIGURED = False


def configure_logging(
    level: Optional[str] = None,
    *,
    json_console: Optional[bool] = None,
    log_dir: Optional[str] = None,
) -> None:
    """Configure the root logger exactly once (idempotent).

    * ``level`` defaults to ``BLINK_LOG_LEVEL`` (``INFO``).
    * ``json_console`` defaults to ``BLINK_LOG_JSON`` (``false``); when false a
      human-readable console formatter is used while the file handler always
      stays JSON.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = (level or os.getenv("BLINK_LOG_LEVEL", "INFO")).upper()
    resolved_level = getattr(logging, level_name, logging.INFO)

    if json_console is None:
        json_console = os.getenv("BLINK_LOG_JSON", "false").lower() in {"1", "true", "yes"}

    root = logging.getLogger()
    root.setLevel(resolved_level)
    redaction = _RedactionFilter()

    # Console handler.
    console = logging.StreamHandler(sys.stdout)
    console.addFilter(redaction)
    if json_console:
        console.setFormatter(_JsonFormatter())
    else:
        console.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(console)

    # Rotating JSON file handler (best effort - never fatal).
    try:
        directory = Path(log_dir or os.getenv("BLINK_LOG_DIR", "logs"))
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / "blink.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.addFilter(redaction)
        file_handler.setFormatter(_JsonFormatter())
        root.addHandler(file_handler)
    except Exception as exc:  # pragma: no cover - environment dependent
        root.warning("Could not initialise file logging: %s", exc)

    # Tame noisy third-party loggers.
    for noisy in ("uvicorn.access", "websockets", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str = "blink") -> logging.Logger:
    """Return a configured logger. Safe to call before explicit configuration."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Networking helpers
# ---------------------------------------------------------------------------
def is_port_in_use(host: str, port: int) -> bool:
    """Return ``True`` if a TCP listener already owns ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def find_available_port(host: str, preferred: int, *, max_attempts: int = 50) -> int:
    """Return ``preferred`` if free, otherwise the next available port.

    Raises :class:`RuntimeError` if no free port is found within
    ``max_attempts`` increments.
    """
    for offset in range(max_attempts):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if not is_port_in_use(host, candidate):
            return candidate
    raise RuntimeError(
        f"No free port found in range {preferred}-{preferred + max_attempts - 1} on {host}"
    )
