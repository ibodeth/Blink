"""Tests for the Markdown-based skill system (src/skills.py)."""

from src.skills import SkillManager, slugify, _parse_front_matter


SAMPLE = """---
name: Weather Reporting
description: Use when the user asks about the weather.
keywords: weather, forecast, temperature
---

# Weather Reporting

Emit a weather command with the target city.
"""


def _write(dir_path, filename, text):
    p = dir_path / filename
    p.write_text(text, encoding="utf-8")
    return p


def test_slugify():
    assert slugify("Weather Reporting") == "weather-reporting"
    assert slugify("  Clear Downloads!!  ") == "clear-downloads"
    assert slugify("") == "skill"
    # Traversal characters must be stripped.
    assert "/" not in slugify("../../etc/passwd")
    assert ".." not in slugify("../../etc/passwd")


def test_parse_front_matter():
    meta, body = _parse_front_matter(SAMPLE)
    assert meta["name"] == "Weather Reporting"
    assert meta["description"].startswith("Use when")
    assert meta["keywords"] == "weather, forecast, temperature"
    assert body.startswith("# Weather Reporting")
    assert "target city" in body


def test_parse_front_matter_missing():
    meta, body = _parse_front_matter("# Just a body\n\nNo front matter here.")
    assert meta == {}
    assert body.startswith("# Just a body")


def test_discovery_and_catalog(tmp_path):
    _write(tmp_path, "weather-reporting.md", SAMPLE)
    _write(tmp_path, "README.md", "# ignore me")
    mgr = SkillManager(base=str(tmp_path))
    names = mgr.names()
    assert "Weather Reporting" in names
    # README.md must be ignored.
    assert len(names) == 1
    catalog = mgr.catalog()
    assert "Weather Reporting" in catalog
    assert "Use when the user asks about the weather." in catalog


def test_get_by_name_and_slug(tmp_path):
    _write(tmp_path, "weather-reporting.md", SAMPLE)
    mgr = SkillManager(base=str(tmp_path))
    body_by_name = mgr.get("Weather Reporting")
    body_by_slug = mgr.get("weather-reporting")
    body_ci = mgr.get("weather reporting")
    assert body_by_name and "target city" in body_by_name
    assert body_by_name == body_by_slug == body_ci
    assert mgr.get("nonexistent") is None
    assert mgr.exists("Weather Reporting") is True
    assert mgr.exists("nope") is False


def test_find_relevant(tmp_path):
    _write(tmp_path, "weather-reporting.md", SAMPLE)
    mgr = SkillManager(base=str(tmp_path))
    hit = mgr.find_relevant("what is the forecast for tomorrow")
    assert hit is not None and hit.name == "Weather Reporting"
    assert mgr.find_relevant("play some jazz") is None


def test_empty_catalog(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    assert mgr.names() == []
    assert mgr.catalog() == "(no skills installed yet)"


def test_save_skill_round_trip(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    slug = mgr.save_skill(
        name="Clear Downloads",
        description="Use when the user wants to empty their Downloads folder.",
        instructions="# Clear Downloads\n\nEmit a system run_command to delete files.",
        keywords=["clear downloads", "empty downloads"],
    )
    assert slug == "clear-downloads"
    # File exists on disk with front matter.
    saved = (tmp_path / "clear-downloads.md").read_text(encoding="utf-8")
    assert saved.startswith("---")
    assert "name: Clear Downloads" in saved
    assert "keywords: clear downloads, empty downloads" in saved
    # Available immediately after save (reload happened internally).
    assert mgr.exists("Clear Downloads")
    body = mgr.get("Clear Downloads")
    assert body and "run_command" in body


def test_save_skill_rejects_empty(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    assert mgr.save_skill(name="", description="x", instructions="y") is None
    assert mgr.save_skill(name="X", description="x", instructions="") is None
    assert mgr.names() == []


def test_save_skill_no_overwrite_by_default(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    assert mgr.save_skill("Test", "d", "# body one") == "test"
    # Second save without overwrite is rejected.
    assert mgr.save_skill("Test", "d", "# body two") is None
    assert "body one" in mgr.get("Test")
    # With overwrite it replaces.
    assert mgr.save_skill("Test", "d", "# body two", overwrite=True) == "test"
    assert "body two" in mgr.get("Test")


def test_save_skill_keywords_as_string(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    mgr.save_skill("K", "d", "# body", keywords="a, b; c")
    saved = (tmp_path / "k.md").read_text(encoding="utf-8")
    assert "keywords: a, b, c" in saved


def test_delete_skill(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    mgr.save_skill("Temp", "d", "# body")
    assert mgr.exists("Temp")
    assert mgr.delete_skill("Temp") is True
    assert not mgr.exists("Temp")
    assert mgr.delete_skill("Temp") is False


def test_reload_picks_up_new_files(tmp_path):
    mgr = SkillManager(base=str(tmp_path))
    assert mgr.names() == []
    _write(tmp_path, "weather-reporting.md", SAMPLE)
    mgr.reload()
    assert "Weather Reporting" in mgr.names()
