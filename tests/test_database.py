"""Persistence tests for the Markdown-backed MemoryManager and ConversationStore."""

import time

from src.database import (
    CONVERSATION_FILE,
    MEMORY_FILE,
    ConversationStore,
    MemoryManager,
    _MarkdownStore,
)


def _mem(tmp_path):
    store = _MarkdownStore(str(tmp_path))
    return MemoryManager(db=store), store


def test_memory_save_and_get(tmp_path):
    mem, _ = _mem(tmp_path)
    mem.save("name", "Ada")
    assert mem.get_value("name") == "Ada"


def test_memory_persists_to_markdown_file(tmp_path):
    mem, store = _mem(tmp_path)
    mem.save("city", "Konya", "prefs")
    text = store.read_memory()
    # Genuinely structured Markdown: heading per category + a table row.
    assert "## prefs" in text
    assert "| Key | Value | Updated (UTC) |" in text
    assert "| city | Konya |" in text
    # A brand new manager reading the same file sees the value (survives restart).
    assert MemoryManager(db=_MarkdownStore(str(tmp_path))).get_value("city") == "Konya"
    assert (tmp_path / MEMORY_FILE).exists()


def test_memory_upsert_overwrites(tmp_path):
    mem, _ = _mem(tmp_path)
    mem.save("city", "Konya")
    mem.save("city", "Istanbul")
    assert mem.get_value("city") == "Istanbul"


def test_memory_delete_and_clear(tmp_path):
    mem, _ = _mem(tmp_path)
    mem.save("a", "1")
    mem.save("b", "2")
    mem.delete("a")
    assert mem.get_value("a") is None
    mem.clear_all()
    assert mem.get_value("b") is None


def test_memory_special_characters_round_trip(tmp_path):
    """Pipes, newlines and Markdown/SQL-looking text must round-trip losslessly
    and never break the table structure."""
    mem, _ = _mem(tmp_path)
    nasty_key = "x'; DROP TABLE memory; --"
    nasty_val = "a | b\nsecond line ## heading | end"
    mem.save(nasty_key, nasty_val)
    assert mem.get_value(nasty_key) == nasty_val
    # Structure is intact: another value is still readable.
    mem.save("sanity", "ok")
    assert mem.get_value("sanity") == "ok"
    assert mem.get_value(nasty_key) == nasty_val


def test_memory_category_expiry(tmp_path, monkeypatch):
    mem, store = _mem(tmp_path)
    # Write a 'status' entry (TTL = 3 days) timestamped 10 days in the past.
    past = time.time() - 10 * 24 * 3600
    monkeypatch.setattr("src.database.time.time", lambda: past)
    mem.save("stale", "v", "status")
    monkeypatch.undo()
    # Re-instantiating prunes expired entries on load.
    mem2 = MemoryManager(db=store)
    assert mem2.get_value("stale") is None


def test_memory_no_expiry_for_untimed_category(tmp_path, monkeypatch):
    mem, store = _mem(tmp_path)
    past = time.time() - 365 * 24 * 3600
    monkeypatch.setattr("src.database.time.time", lambda: past)
    mem.save("name", "Ada", "prefs")  # prefs never expires
    monkeypatch.undo()
    assert MemoryManager(db=store).get_value("name") == "Ada"


def test_conversation_roundtrip_chronological(tmp_path):
    store = _MarkdownStore(str(tmp_path))
    convo = ConversationStore(db=store)
    convo.add("user", "first")
    convo.add("blink", "second")
    convo.add("user", "third")
    recent = convo.load_recent(20)
    assert [t["text"] for t in recent] == ["first", "second", "third"]
    assert recent[0]["role"] == "user"
    assert (tmp_path / CONVERSATION_FILE).exists()


def test_conversation_limit(tmp_path):
    store = _MarkdownStore(str(tmp_path))
    convo = ConversationStore(db=store)
    for i in range(10):
        convo.add("user", f"m{i}")
    recent = convo.load_recent(3)
    assert [t["text"] for t in recent] == ["m7", "m8", "m9"]


def test_conversation_survives_restart(tmp_path):
    store = _MarkdownStore(str(tmp_path))
    ConversationStore(db=store).add("user", "remember me")
    # A fresh store/instance reading the same file replays the turn.
    reopened = ConversationStore(db=_MarkdownStore(str(tmp_path)))
    assert [t["text"] for t in reopened.load_recent(20)] == ["remember me"]
