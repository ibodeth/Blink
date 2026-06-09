"""Markdown persistence layer (thread-safe).

Blink no longer needs a SQLite database to keep track of its state. LLMs are
excellent at reading and manipulating Markdown, so the persistence layer is now
backed by two human- and model-readable ``.md`` files instead of
``blink_mem.db``:

* ``blink_memory.md``  - long-term key/value memory grouped under one Markdown
  heading (``## category``) per category, each holding a table of entries.
  Backed by :class:`MemoryManager` (category-based TTL expiry preserved).
* ``blink_conversation.md`` - a rolling conversation transcript stored as a
  single Markdown table (most recent turn at the bottom). Backed by
  :class:`ConversationStore`; on boot the last N turns are replayed into the AI
  context.

The public API (``MemoryManager`` / ``ConversationStore`` and their methods) is
unchanged, so this is a drop-in replacement for the previous SQLite backend.

Design notes (mirroring the guarantees of the old SQLite layer):

* A single process-wide :class:`_MarkdownStore` owns both files and serialises
  every read-modify-write behind a re-entrant lock (the Markdown analogue of the
  old guarded single connection).
* Writes are atomic: content is written to a temp file in the same directory and
  then ``os.replace``-d into place, so a crash mid-write never corrupts state.
* Pipe and newline characters inside keys/values are backslash-escaped, so
  arbitrary text (including things that look like SQL or Markdown syntax) round
  trips losslessly and can never break the table structure or be "injected".
* All filesystem errors are caught specifically (:class:`OSError`) and logged via
  structured logging.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.utils import get_logger

log = get_logger(__name__)

# File names (replacing the old single ``blink_mem.db``).
MEMORY_FILE = "blink_memory.md"
CONVERSATION_FILE = "blink_conversation.md"

# Category-based expiry windows (seconds). ``None`` means never expire.
CATEGORY_TTL: Dict[str, Optional[int]] = {
    "prefs": None,
    "general": None,
    "agent": None,
    "status": 3 * 24 * 3600,   # 3 days
    "events": 7 * 24 * 3600,   # 7 days
}

DEFAULT_CATEGORY = "general"


# ---------------------------------------------------------------------------
# Encoding helpers
#
# Markdown table cells are delimited by ``|`` and rows by newlines. To let a
# value contain those characters without breaking the table we backslash-escape
# them on write and decode them on read. A single forward scan handles every
# case (``\\`` -> ``\``, ``\|`` -> ``|``, ``\n`` -> newline).
# ---------------------------------------------------------------------------


def _encode_cell(value: str) -> str:
    out: List[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "|":
            out.append("\\|")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            continue
        else:
            out.append(ch)
    return "".join(out)


def _split_row(row: str) -> List[str]:
    """Split a Markdown table row (``| a | b |``) into decoded cell strings."""
    cells: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(row)
    while i < n:
        ch = row[i]
        if ch == "\\" and i + 1 < n:
            nxt = row[i + 1]
            if nxt == "\\":
                buf.append("\\")
            elif nxt == "|":
                buf.append("|")
            elif nxt == "n":
                buf.append("\n")
            else:
                buf.append(nxt)
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))
    # A well-formed row starts and ends with '|', producing empty leading and
    # trailing fragments which we drop.
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(c and set(c) <= {"-", ":"} for c in cells)


def _now() -> float:
    return time.time()


def _epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_epoch(value: str) -> float:
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (ValueError, TypeError):
        return 0.0


class _MarkdownStore:
    """Owns the Markdown state files and serialises access across threads.

    ``base`` is a directory; the two state files live inside it. A single
    re-entrant lock guards every read-modify-write so concurrent callers (the
    audio thread, the server loop, the watchdog) can never interleave a partial
    update - the Markdown analogue of the old single guarded SQLite connection.
    """

    def __init__(self, base: str = ".") -> None:
        self.base = Path(base)
        self.lock = threading.RLock()
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Could not create state directory '%s': %s", self.base, exc)
        self.memory_path = self.base / MEMORY_FILE
        self.conversation_path = self.base / CONVERSATION_FILE

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            log.error("Failed to read state file '%s': %s", path, exc)
            return ""

    def _atomic_write(self, path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.error("Failed to write state file '%s': %s", path, exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def read_memory(self) -> str:
        return self._read(self.memory_path)

    def write_memory(self, text: str) -> None:
        self._atomic_write(self.memory_path, text)

    def read_conversation(self) -> str:
        return self._read(self.conversation_path)

    def write_conversation(self, text: str) -> None:
        self._atomic_write(self.conversation_path, text)

    def close(self) -> None:  # kept for API symmetry with the old backend
        return None


# A single process-wide store keeps file access serialised and lets the memory
# and conversation managers share one lock (acts like the old pool-of-one).
_STORE_SINGLETON: Optional[_MarkdownStore] = None
_STORE_SINGLETON_LOCK = threading.Lock()


def get_store(base: str = ".") -> _MarkdownStore:
    global _STORE_SINGLETON
    with _STORE_SINGLETON_LOCK:
        if _STORE_SINGLETON is None:
            _STORE_SINGLETON = _MarkdownStore(base)
        return _STORE_SINGLETON


# Backwards-compatible alias for any external caller that imported the old name.
get_database = get_store


class MemoryManager:
    """Long-term key/value memory with category-based expiry (Markdown-backed).

    On disk this is ``blink_memory.md``: one ``## category`` heading per
    category, each followed by a Markdown table of ``Key | Value | Updated``
    rows. Keys are unique across the whole store (an upsert moves a key to its
    new category).
    """

    def __init__(self, db: Optional[_MarkdownStore] = None) -> None:
        # Parameter kept named ``db`` for drop-in compatibility with callers.
        self._store = db or get_store()
        self._prune_expired()

    # -- internal markdown (de)serialisation ------------------------------

    def _parse(self) -> Dict[str, Tuple[str, str, float]]:
        """Return ``{key: (value, category, timestamp)}`` parsed from disk."""
        text = self._store.read_memory()
        entries: Dict[str, Tuple[str, str, float]] = {}
        category = DEFAULT_CATEGORY
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                category = stripped[3:].strip() or DEFAULT_CATEGORY
                continue
            if not stripped.startswith("|"):
                continue
            cells = _split_row(stripped)
            if len(cells) < 3:
                continue
            if _is_separator_row(cells):
                continue
            if cells[0].lower() == "key" and cells[1].lower() == "value":
                continue  # header row
            key, value, updated = cells[0], cells[1], cells[2]
            if not key:
                continue
            entries[key] = (value, category, _iso_to_epoch(updated))
        return entries

    def _render(self, entries: Dict[str, Tuple[str, str, float]]) -> str:
        # Group keys by category, preserving the canonical category ordering and
        # appending any unknown categories afterwards.
        by_category: Dict[str, List[Tuple[str, str, float]]] = {}
        for key, (value, category, ts) in entries.items():
            by_category.setdefault(category, []).append((key, value, ts))

        ordered = list(CATEGORY_TTL.keys())
        for category in by_category:
            if category not in ordered:
                ordered.append(category)

        lines: List[str] = [
            "# Blink Memory",
            "",
            "<!-- Managed automatically by Blink. Persistent key/value memory grouped by category. -->",
            "<!-- Pipe and newline characters in values are backslash-escaped. -->",
            "",
        ]
        for category in ordered:
            rows = by_category.get(category, [])
            lines.append(f"## {category}")
            lines.append("")
            lines.append("| Key | Value | Updated (UTC) |")
            lines.append("| --- | --- | --- |")
            for key, value, ts in sorted(rows, key=lambda r: r[2], reverse=True):
                lines.append(
                    f"| {_encode_cell(key)} | {_encode_cell(value)} | {_epoch_to_iso(ts)} |"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _prune_expired(self) -> None:
        now = _now()
        with self._store.lock:
            entries = self._parse()
            changed = False
            for key in list(entries.keys()):
                _value, category, ts = entries[key]
                ttl = CATEGORY_TTL.get(category)
                if ttl is not None and ts < now - ttl:
                    del entries[key]
                    changed = True
            if changed:
                self._store.write_memory(self._render(entries))

    # -- public API -------------------------------------------------------

    def save(self, key: str, val: str, category: str = DEFAULT_CATEGORY) -> None:
        if not key:
            return
        if category not in CATEGORY_TTL:
            category = DEFAULT_CATEGORY
        with self._store.lock:
            entries = self._parse()
            entries[str(key)] = (str(val), category, _now())
            self._store.write_memory(self._render(entries))

    def delete(self, key: str) -> None:
        with self._store.lock:
            entries = self._parse()
            if str(key) in entries:
                del entries[str(key)]
                self._store.write_memory(self._render(entries))

    def clear_all(self) -> None:
        with self._store.lock:
            self._store.write_memory(self._render({}))

    def get_value(self, key: str) -> Optional[str]:
        with self._store.lock:
            entry = self._parse().get(str(key))
        return entry[0] if entry else None

    def get_relevant_memories(self, limit: int = 50) -> str:
        """Return a newline-formatted block of stored memories for the prompt."""
        self._prune_expired()
        with self._store.lock:
            entries = self._parse()
        if not entries:
            return ""
        ordered = sorted(entries.items(), key=lambda kv: kv[1][2], reverse=True)
        ordered = ordered[: int(limit)]
        return "\n".join(
            f"- [{category}] {key}: {value}"
            for key, (value, category, _ts) in ordered
        )


class ConversationStore:
    """Rolling persistent conversation history (survives restarts).

    On disk this is ``blink_conversation.md``: a single Markdown table with one
    row per turn (oldest first, newest at the bottom). History is trimmed to the
    most recent ``max_rows`` turns.
    """

    def __init__(self, db: Optional[_MarkdownStore] = None, max_rows: int = 500) -> None:
        self._store = db or get_store()
        self._max_rows = max_rows

    # -- internal markdown (de)serialisation ------------------------------

    def _parse(self) -> List[Dict[str, str]]:
        text = self._store.read_conversation()
        turns: List[Dict[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = _split_row(stripped)
            if len(cells) < 4:
                continue
            if _is_separator_row(cells):
                continue
            if cells[0] == "#" and cells[1].lower() == "role":
                continue  # header row
            role, text_val = cells[1], cells[3]
            turns.append({"role": role, "text": text_val})
        return turns

    def _render(self, turns: List[Dict[str, str]]) -> str:
        lines: List[str] = [
            "# Blink Conversation Log",
            "",
            "<!-- Rolling conversation history (oldest first, newest last). -->",
            "<!-- Pipe and newline characters in text are backslash-escaped. -->",
            "",
            "| # | Role | Timestamp (UTC) | Text |",
            "| --- | --- | --- | --- |",
        ]
        for idx, turn in enumerate(turns, start=1):
            lines.append(
                f"| {idx} | {_encode_cell(turn['role'])} | "
                f"{turn.get('timestamp', '')} | {_encode_cell(turn['text'])} |"
            )
        return "\n".join(lines).rstrip() + "\n"

    # -- public API -------------------------------------------------------

    def add(self, role: str, text: str) -> None:
        role = (role or "").strip().lower() or "user"
        text = (text or "").strip()
        if not text:
            return
        with self._store.lock:
            turns = self._parse_with_ts()
            turns.append({"role": role, "text": text, "timestamp": _epoch_to_iso(_now())})
            if len(turns) > self._max_rows:
                turns = turns[-self._max_rows:]
            self._store.write_conversation(self._render_with_ts(turns))

    def load_recent(self, limit: int = 20) -> List[Dict[str, str]]:
        """Return the last ``limit`` turns in chronological order."""
        with self._store.lock:
            turns = self._parse()
        if limit is not None:
            turns = turns[-int(limit):]
        return [{"role": t["role"], "text": t["text"]} for t in turns]

    def clear(self) -> None:
        with self._store.lock:
            self._store.write_conversation(self._render_with_ts([]))

    # The timestamp column is informational; these helpers preserve it across a
    # read-modify-write while ``load_recent`` exposes only role/text (as before).
    def _parse_with_ts(self) -> List[Dict[str, str]]:
        text = self._store.read_conversation()
        turns: List[Dict[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = _split_row(stripped)
            if len(cells) < 4:
                continue
            if _is_separator_row(cells):
                continue
            if cells[0] == "#" and cells[1].lower() == "role":
                continue
            turns.append({"role": cells[1], "timestamp": cells[2], "text": cells[3]})
        return turns

    def _render_with_ts(self, turns: List[Dict[str, str]]) -> str:
        return self._render(turns)
