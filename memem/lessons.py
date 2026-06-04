"""A-MemGuard 'lessons' anti-memory store (memem v2 m5).

When a memory is judged to have caused harm, record an explicit
'do NOT use memory M for query class X' annotation rather than deleting M.
Lessons are consulted before retrieval and exclude the targeted memory
from slice candidates when the query matches the lesson's query_class.

Per A-MemGuard (arXiv 2510.02373, NeurIPS 2025): >95% poisoning attack
rate reduction with this pattern.

Storage: ~/obsidian-brain/memem/lessons/<lesson-id>.md

Schema:
  ---
  id: <lesson-id>
  targeted_memory_id: <memory-id>
  query_class: <freeform string — keyword tag, project, or task type>
  anti_pattern: <one-line description of what NOT to do with M for this class>
  evidence: <user correction, judge reasoning, or m4 dreamer judgment>
  created_at: <ISO timestamp>
  source: <user | dreamer | manual>
  ---
  <body — longer explanation, optional>
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from memem.io_utils import atomic_write_text
from memem.models import now_iso

log = logging.getLogger("memem-lessons")


def _lessons_dir() -> Path:
    """Lazy resolution so tests that monkeypatch MEMEM_OBSIDIAN_VAULT mid-process
    get the patched path. A module-level constant captured at import time would
    pin the original vault path."""
    from memem import models  # noqa: PLC0415
    return models.OBSIDIAN_VAULT / "memem" / "lessons"


# Back-compat alias for callers that imported the constant directly.
# Uses _lessons_dir() so reload() in tests picks up monkeypatched OBSIDIAN_VAULT.
LESSONS_DIR = _lessons_dir()


def _slugify(text: str, max_len: int = 40) -> str:
    """Filename-safe slug from text."""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:max_len] or "lesson"


def record_lesson(
    targeted_memory_id: str,
    query_class: str,
    anti_pattern: str,
    evidence: str = "",
    source: str = "manual",
) -> str:
    """Record a new lesson. Returns the lesson_id.

    Multiple lessons can target the same memory_id (different query_classes).
    """
    _lessons_dir().mkdir(parents=True, exist_ok=True)
    lesson_id = str(uuid.uuid4())
    slug = _slugify(anti_pattern[:60])
    short_id = lesson_id[:8]
    fname = f"{slug}-{short_id}.md"
    path = _lessons_dir() / fname

    frontmatter_lines = [
        "---",
        f"id: {lesson_id}",
        f"targeted_memory_id: {targeted_memory_id}",
        f"query_class: {query_class}",
        f"anti_pattern: {anti_pattern[:200]}",
        f"evidence: {evidence[:500]}" if evidence else "evidence: ",
        f"created_at: {now_iso()}",
        f"source: {source}",
        "---",
        "",
    ]
    atomic_write_text(path, "\n".join(frontmatter_lines))
    log.info("recorded lesson %s targeting memory %s", lesson_id, targeted_memory_id)
    return lesson_id


def list_lessons() -> list[dict]:
    """Return all lessons as dicts. Returns empty list if dir doesn't exist."""
    if not _lessons_dir().exists():
        return []
    lessons = []
    for path in _lessons_dir().glob("*.md"):
        lesson = _parse_lesson(path)
        if lesson:
            lessons.append(lesson)
    return lessons


def _parse_lesson(path: Path) -> dict | None:
    """Parse one lesson markdown file. Returns None on parse failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    try:
        # Lightweight parser — frontmatter is simple key: value
        end = text.find("\n---", 3)
        if end < 0:
            return None
        fm_block = text[3:end].strip()
        result = {}
        for line in fm_block.split("\n"):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
        return result
    except Exception:
        return None


def query_class_matches(query: str, query_class: str) -> bool:
    """Does the query match this lesson's query_class?

    Simple substring + word-overlap match. Conservative — false negatives
    are safer than false positives (false negative = candidate not blocked
    when it should be; false positive = useful memory blocked).
    """
    if not query or not query_class:
        return False
    query_lower = query.lower()
    class_lower = query_class.lower()
    if class_lower in query_lower:
        return True
    # Word overlap: at least 2 shared significant words
    qwords = set(re.findall(r"\w+", query_lower))
    cwords = set(re.findall(r"\w+", class_lower))
    short = {"a", "an", "the", "is", "of", "in", "on", "to", "for", "and", "or"}
    qwords -= short
    cwords -= short
    overlap = qwords & cwords
    return len(overlap) >= 2


def excluded_memory_ids_for_query(query: str) -> set[str]:
    """Return the set of memory_ids that should be EXCLUDED from candidates
    for this query, per the recorded lessons."""
    excluded = set()
    for lesson in list_lessons():
        if query_class_matches(query, lesson.get("query_class", "")):
            mem_id = lesson.get("targeted_memory_id")
            if mem_id:
                excluded.add(mem_id)
    return excluded
