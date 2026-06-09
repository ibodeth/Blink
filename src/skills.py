"""Skill system for Blink (Claude-style "Agent Skills").

Instead of cramming every capability into one giant system prompt, Blink's
know-how is split into small, self-contained Markdown skill files under the
``skills/`` directory. Each skill is a ``.md`` file with a YAML-style front
matter header and a Markdown body:

    ---
    name: Take Screenshot
    description: Use when the user asks to capture or screenshot their screen.
    keywords: screenshot, screen capture, capture screen
    ---

    # Take Screenshot

    When the user asks to capture their screen, emit:
    {"type": "system", "action": "screenshot", "response": "Taking a screenshot.", "lang": "en"}

Why this design:

* **Always knows its skill names.** :meth:`SkillManager.catalog` injects a
  compact ``name: description`` list into every prompt, so the model always
  knows which skills exist and when to use them.
* **Progressive disclosure.** Only the short catalog is in the base prompt; the
  full instructions for a skill are loaded on demand (via a ``skill`` command),
  keeping the prompt small and cheap.
* **Runtime-extensible.** Dropping a new ``.md`` file into ``skills/`` (or
  calling :meth:`save_skill`) makes it available immediately after a reload —
  no code change.
* **Self-authored skills.** When Blink accomplishes something new, it can emit a
  ``save_skill`` command and :meth:`save_skill` writes a brand new ``.md`` skill
  so it can do it again next time.

The parser is intentionally dependency-free (no PyYAML required): the front
matter is a simple ``key: value`` block delimited by ``---`` fences.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.utils import get_logger

log = get_logger(__name__)

SKILLS_DIR = "skills"


@dataclass
class Skill:
    """Parsed metadata + body for a single skill file."""

    name: str
    description: str
    slug: str
    path: Path
    keywords: List[str] = field(default_factory=list)
    body: str = ""


def slugify(name: str) -> str:
    """Turn a skill name into a safe, traversal-proof filename stem."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "skill"


def _parse_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    """Split ``---`` front matter (simple ``key: value`` lines) from the body."""
    meta: Dict[str, str] = {}
    body = text
    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        end = stripped.find("\n---", 3)
        if end != -1:
            block = stripped[3:end]
            body = stripped[end + 4:]
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip()
    return meta, body.strip()


def _split_keywords(raw: str) -> List[str]:
    if not raw:
        return []
    return [k.strip().lower() for k in re.split(r"[,;]", raw) if k.strip()]


class SkillManager:
    """Discovers, exposes, loads, and persists Markdown skills (thread-safe)."""

    def __init__(self, base: str = SKILLS_DIR) -> None:
        self.base = Path(base)
        self._lock = threading.RLock()
        self._skills: Dict[str, Skill] = {}
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Could not create skills directory '%s': %s", self.base, exc)
        self.reload()

    # -- discovery --------------------------------------------------------

    def reload(self) -> None:
        """Re-scan the skills directory from disk."""
        skills: Dict[str, Skill] = {}
        with self._lock:
            try:
                files = sorted(self.base.glob("*.md"))
            except OSError as exc:
                log.error("Failed to list skills directory: %s", exc)
                files = []
            for path in files:
                if path.name.lower() in {"readme.md", "index.md"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    log.warning("Could not read skill '%s': %s", path, exc)
                    continue
                meta, body = _parse_front_matter(text)
                name = meta.get("name") or path.stem.replace("-", " ").title()
                description = meta.get("description", "").strip()
                skill = Skill(
                    name=name,
                    description=description,
                    slug=slugify(name),
                    path=path,
                    keywords=_split_keywords(meta.get("keywords", "")),
                    body=body,
                )
                skills[skill.slug] = skill
            self._skills = skills
        log.info("Loaded %d skill(s): %s", len(skills), ", ".join(s.name for s in skills.values()))

    # -- read API ---------------------------------------------------------

    def names(self) -> List[str]:
        with self._lock:
            return [s.name for s in self._skills.values()]

    def all(self) -> List[Skill]:
        with self._lock:
            return list(self._skills.values())

    def catalog(self) -> str:
        """A compact ``- name: description`` list for the system prompt."""
        with self._lock:
            skills = list(self._skills.values())
        if not skills:
            return "(no skills installed yet)"
        return "\n".join(
            f"- {s.name}: {s.description}" if s.description else f"- {s.name}"
            for s in skills
        )

    def _lookup(self, name: str) -> Optional[Skill]:
        if not name:
            return None
        target = slugify(name)
        with self._lock:
            if target in self._skills:
                return self._skills[target]
            # Fall back to a case-insensitive exact name match.
            for skill in self._skills.values():
                if skill.name.lower() == name.strip().lower():
                    return skill
        return None

    def exists(self, name: str) -> bool:
        return self._lookup(name) is not None

    def get(self, name: str) -> Optional[str]:
        """Return the full instruction body of a skill, or ``None``."""
        skill = self._lookup(name)
        return skill.body if skill else None

    def find_relevant(self, text: str) -> Optional[Skill]:
        """Best-effort local keyword match (used as an optional fast path)."""
        low = (text or "").lower()
        if not low:
            return None
        with self._lock:
            for skill in self._skills.values():
                for kw in skill.keywords:
                    if kw and kw in low:
                        return skill
        return None

    # -- write API --------------------------------------------------------

    def save_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        keywords: Optional[List[str]] = None,
        overwrite: bool = False,
    ) -> Optional[str]:
        """Persist a new skill as a Markdown file and reload.

        Returns the slug on success, or ``None`` if it was rejected (missing
        fields, or a name clash when ``overwrite`` is false).
        """
        name = (name or "").strip()
        description = (description or "").strip()
        instructions = (instructions or "").strip()
        if not name or not instructions:
            log.warning("Refusing to save skill with empty name/instructions.")
            return None

        slug = slugify(name)
        path = self.base / f"{slug}.md"
        with self._lock:
            if path.exists() and not overwrite:
                log.info("Skill '%s' already exists; not overwriting.", name)
                return None
            kw = keywords or []
            if isinstance(kw, str):
                kw = _split_keywords(kw)
            front = [
                "---",
                f"name: {name}",
                f"description: {description}",
            ]
            if kw:
                front.append("keywords: " + ", ".join(kw))
            front.append("---")
            content = "\n".join(front) + "\n\n" + instructions.rstrip() + "\n"
            try:
                self.base.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                log.error("Failed to save skill '%s': %s", name, exc)
                return None
        self.reload()
        log.info("Saved new skill '%s' -> %s", name, path.name)
        return slug

    def delete_skill(self, name: str) -> bool:
        skill = self._lookup(name)
        if not skill:
            return False
        with self._lock:
            try:
                skill.path.unlink()
            except OSError as exc:
                log.error("Failed to delete skill '%s': %s", name, exc)
                return False
        self.reload()
        return True


# Process-wide singleton so every component shares one skill registry.
_SKILLS_SINGLETON: Optional[SkillManager] = None
_SKILLS_SINGLETON_LOCK = threading.Lock()


def get_skill_manager(base: str = SKILLS_DIR) -> SkillManager:
    global _SKILLS_SINGLETON
    with _SKILLS_SINGLETON_LOCK:
        if _SKILLS_SINGLETON is None:
            _SKILLS_SINGLETON = SkillManager(base)
        return _SKILLS_SINGLETON
