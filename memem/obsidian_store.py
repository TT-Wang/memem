"""Obsidian I/O, memory CRUD, dedup scoring, and contradiction detection.

This module owns all interactions with the Obsidian vault:
- Reading/writing memory markdown files
- Deduplication scoring (word/bigram/trigram containment)
- Contradiction detection
- Index management (_index.md)
- Save/update/delete/deprecate operations
"""

import fcntl
import functools
import logging
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

from memem.models import (
    INDEX_PATH,
    OBSIDIAN_MEMORIES_DIR,
    OBSIDIAN_VAULT,
    ObsidianUnavailableError,
    _normalize_scope_id,
    now_iso,
)
from memem.search_index import _index_memory, _remove_from_index
from memem.security import scan_memory_content
from memem.telemetry import _log_event

log = logging.getLogger("memem-obsidian")

# Alias for backward compat
_now = now_iso


# ---------------------------------------------------------------------------
# Slugify / YAML helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def _yaml_escape(value: str) -> str:
    """Quote YAML values that contain special characters.

    Newlines and carriage returns are collapsed to spaces — a raw newline
    inside a frontmatter value would otherwise let a malicious title inject
    fake frontmatter fields (`value\\n---\\ncreated: 1970-01-01`).
    """
    if not value:
        return '""'
    # Strip control characters that could break YAML framing
    value = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if any(ch in value for ch in (':', '#', '{', '}', '[', ']', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`', '"', "'")):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if value.startswith((' ', '\t')) or value.endswith((' ', '\t')):
        return f'"{value}"'
    return value


def _stable_mined_memory_id(session_id: str, title: str, content: str) -> str:
    seed = f"{session_id}\n{title.strip()}\n{content.strip()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _parse_obsidian_tags(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


# ---------------------------------------------------------------------------
# Memory factory
# ---------------------------------------------------------------------------

def _make_memory(content: str, title: str, tags: list[str] | None = None,
                 project: str = "general", source_type: str = "user",
                 source_session: str = "", importance: int = 3) -> dict:
    # Reject junk content
    stripped = content.strip().strip(".")
    if len(stripped) < 10:
        raise ValueError(f"Content too short ({len(stripped)} chars): rejected as junk")

    threat = scan_memory_content(content)
    if threat:
        raise ValueError(threat)

    if title:
        title_threat = scan_memory_content(title)
        if title_threat:
            raise ValueError(f"Title: {title_threat}")

    normalized_project = (project or "general").strip() or "general"
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "essence": content,
        "domain_tags": tags or [],
        "project": normalized_project,
        "source_type": source_type,
        "source_session": source_session,
        "importance": importance,
        "created_at": _now(),
        "updated_at": _now(),
        "schema_version": 1,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _require_obsidian_writable() -> None:
    if not OBSIDIAN_VAULT.exists():
        raise ObsidianUnavailableError(
            f"Obsidian vault not found: {OBSIDIAN_VAULT}"
        )
    OBSIDIAN_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)


def _write_obsidian_memory(mem: dict):
    _require_obsidian_writable()

    title = mem.get("title", "Untitled")
    slug = _slugify(title) or mem["id"][:8]
    filename = f"{slug}-{mem['id'][:8]}.md"

    # Clean up old file if title changed (check both obsidian_file and file fields)
    old_file = mem.get("obsidian_file", "")
    if not old_file and mem.get("file"):
        old_file = Path(mem["file"]).name
    if old_file and old_file != filename:
        old_path = OBSIDIAN_MEMORIES_DIR / old_file
        if old_path.exists():
            old_path.unlink()

    tags = mem.get("domain_tags", [])
    if isinstance(tags, str):
        tags = [tag for tag in tags.split(",") if tag]

    clean_title = re.sub(r"^\[[^\]]+\]\s*", "", mem.get("title", "Untitled"))
    project = mem.get("project", "general")

    # Sanitize tags and related ids so a hallucinated or hostile value
    # containing `]`, `[`, `,`, or newlines cannot corrupt the frontmatter
    # by introducing phantom fields or truncating the list early.
    def _safe_tag(value: str) -> str:
        return (
            value.replace("\r", " ")
            .replace("\n", " ")
            .replace("[", "")
            .replace("]", "")
            .replace(",", "")
            .strip()
        )

    safe_tags = [_safe_tag(t) for t in tags if _safe_tag(t)]
    frontmatter = (
        f"---\n"
        f"id: {mem['id']}\n"
        f"schema_version: {mem.get('schema_version', 1)}\n"
        f"title: {_yaml_escape(clean_title)}\n"
        f"project: {_safe_tag(project)}\n"
        f"tags: [{', '.join(safe_tags)}]\n"
    )
    related = mem.get("related", [])
    if related:
        safe_related = [_safe_tag(r) for r in related if _safe_tag(r)]
        frontmatter += f"related: [{', '.join(safe_related)}]\n"
    frontmatter += (
        f"created: {mem.get('created_at', '')[:10]}\n"
        f"updated: {mem.get('updated_at', '')[:10]}\n"
        f"source_type: {mem.get('source_type', 'user')}\n"
        f"source_session: {mem.get('source_session', '')}\n"
        f"importance: {mem.get('importance', 3)}\n"
        f"status: {mem.get('status', 'active')}\n"
        f"valid_to: {mem.get('valid_to', '')}\n"
    )
    contradicts = mem.get("contradicts", [])
    if contradicts:
        frontmatter += f"contradicts: [{', '.join(contradicts)}]\n"
    frontmatter += "---"

    body = f"\n\n{mem.get('essence', '')}"
    filepath = OBSIDIAN_MEMORIES_DIR / filename
    _atomic_write(filepath, frontmatter + body)
    mem["obsidian_file"] = filename


def _parse_obsidian_memory_file(md_file: Path) -> dict | None:
    try:
        content = md_file.read_text(errors="ignore")
    except OSError as exc:
        log.warning("Failed to read memory file %s: %s", md_file, exc)
        return None

    body = content.strip()
    mem: dict[str, Any] = {
        "id": _extract_memory_id_from_filename(md_file),
        "title": md_file.stem,
        "project": "general",
        "domain_tags": [],
        "created_at": "",
        "file": str(md_file),
        "source_type": "user",
        "status": "active",
        "valid_to": "",
        "source_session": "",
        "access_count": 0,
        "last_accessed": "",
        "updated_at": "",
        "importance": 3,
        "schema_version": 0,
    }

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2].strip()
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().strip('"')
                if key == "id" and value:
                    mem["id"] = value
                elif key == "title" and value:
                    mem["title"] = value
                elif key == "project" and value:
                    mem["project"] = value
                elif key == "tags":
                    mem["domain_tags"] = _parse_obsidian_tags(value)
                elif key == "related":
                    mem["related"] = _parse_obsidian_tags(value)
                elif key == "created":
                    mem["created_at"] = value
                elif key == "updated":
                    mem["updated_at"] = value
                elif key == "source_type":
                    mem["source_type"] = value
                elif key == "source_session":
                    mem["source_session"] = value
                elif key == "access_count":
                    try:
                        mem["access_count"] = int(value)
                    except (ValueError, TypeError):
                        mem["access_count"] = 0
                elif key == "last_accessed":
                    mem["last_accessed"] = value
                elif key == "status":
                    mem["status"] = value
                elif key == "valid_to":
                    mem["valid_to"] = value
                elif key == "contradicts":
                    mem["contradicts"] = _parse_obsidian_tags(value)  # reuse existing tag parser
                elif key == "importance":
                    try:
                        mem["importance"] = int(value)
                    except (ValueError, TypeError):
                        mem["importance"] = 3
                elif key == "schema_version":
                    try:
                        mem["schema_version"] = int(value)
                    except (ValueError, TypeError):
                        mem["schema_version"] = 0

    mem["essence"] = body
    mem["full_record"] = body  # read-time alias for essence — not stored in markdown
    if not mem["updated_at"]:
        mem["updated_at"] = mem.get("created_at", "")
    if not mem["title"] or mem["title"] == md_file.stem:
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        mem["title"] = first_line[:120] if first_line else mem["id"]

    if "imported" in mem["domain_tags"]:
        mem["source_type"] = "import"
    elif "mined" in mem["domain_tags"]:
        mem["source_type"] = "mined"
    elif not mem.get("source_type"):
        mem["source_type"] = "user"

    return mem


def _extract_memory_id_from_filename(md_file: Path) -> str:
    stem = md_file.stem
    return stem.rsplit("-", 1)[-1] if "-" in stem else stem


def _obsidian_memories(scope_id: str | None = None, include_deprecated: bool = False) -> list[dict]:
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return []

    memories = []
    for md_file in sorted(OBSIDIAN_MEMORIES_DIR.glob("*.md")):
        mem = _parse_obsidian_memory_file(md_file)
        if not mem:
            continue
        if not include_deprecated and mem.get("status") == "deprecated":
            continue
        project = mem.get("project", "general")
        normalized = _normalize_scope_id(scope_id) if scope_id is not None else "general"
        if normalized != "general" and project != normalized:
            continue
        memories.append(mem)
    return memories


def _find_memory(memory_id: str) -> dict | None:
    """Find memory by exact ID or 8-char prefix. Single pass."""
    prefix_match = None
    for mem in _obsidian_memories():
        mid = mem.get("id", "")
        if mid == memory_id:
            return mem
        if prefix_match is None and len(memory_id) >= 8 and mid.startswith(memory_id):
            prefix_match = mem
    return prefix_match


def _load_obsidian_memories(picked_ids: list[str]) -> list[dict]:
    results = []
    for picked_id in picked_ids:
        mem = _find_memory(picked_id)
        if not mem:
            continue
        results.append({
            "title": mem.get("title", "Untitled"),
            "project": mem.get("project", "general"),
            "body": mem.get("full_record", mem.get("essence", "")),
        })
    return results


# ---------------------------------------------------------------------------
# Scoring (dedup)
# ---------------------------------------------------------------------------

_SYNONYMS = {
    "auth": "authentication",
    "authentication": "auth",
    "db": "database",
    "database": "db",
    "config": "configuration",
    "configuration": "config",
    "env": "environment",
    "environment": "env",
    "repo": "repository",
    "repository": "repo",
    "dep": "dependency",
    "dependency": "dep",
    "deps": "dependencies",
    "dependencies": "deps",
    "dir": "directory",
    "directory": "dir",
    "impl": "implementation",
    "implementation": "impl",
    "func": "function",
    "function": "func",
    "param": "parameter",
    "parameter": "param",
    "args": "arguments",
    "arguments": "args",
    "msg": "message",
    "message": "msg",
    "err": "error",
    "error": "err",
    "req": "request",
    "request": "req",
    "res": "response",
    "response": "res",
    "jwt": "token",
    "token": "jwt",
}


def _stem(word: str) -> str:
    """Minimal suffix stemmer — strips common suffixes for better matching."""
    for suffix in ("tion", "sion", "ing", "ment", "ness", "able", "ible", "ous", "ive", "ful", "less", "ize", "ise", "ated", "ates", "ies", "ed", "er", "ly", "al", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _word_set(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    expanded = set(words)
    for word in words:
        stemmed = _stem(word)
        if stemmed != word:
            expanded.add(stemmed)
        syn = _SYNONYMS.get(word)
        if syn:
            expanded.add(syn)
    return expanded


def _ngram_set(text: str, n: int) -> set:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return set(tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1))


def _containment(a: set, b: set) -> float:
    """What fraction of the smaller set is contained in the larger."""
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    smaller = min(len(a), len(b))
    return overlap / smaller if smaller else 0.0


def _find_best_match(content: str, scope_id: str = "default") -> tuple[dict | None, float]:
    """Return (best_mem, best_score) for the closest existing memory to content.

    Uses CONTENT-ONLY scoring (containment) for dedup/merge decisions.
    No temporal or access weighting — those belong in the recall path only.
    Score thresholds: <0.3 = new, 0.3-0.6 = merge candidate, >0.6 = duplicate.
    """
    content_words = _word_set(content)
    if not content_words:
        return None, 0.0

    content_bigrams = _ngram_set(content, 2)
    content_trigrams = _ngram_set(content, 3)

    normalized = _normalize_scope_id(scope_id)
    project = None if normalized == "general" else normalized
    best_score = 0.0
    best_mem = None

    for mem in _obsidian_memories(project):
        mem_text = mem.get("essence", "") + " " + mem.get("title", "")
        mem_words = _word_set(mem_text)
        if not mem_words:
            continue
        word_c = _containment(content_words, mem_words)
        bigram_c = _containment(content_bigrams, _ngram_set(mem_text, 2))
        trigram_c = _containment(content_trigrams, _ngram_set(mem_text, 3))
        score = 0.5 * word_c + 0.3 * bigram_c + 0.2 * trigram_c
        if score > best_score:
            best_score = score
            best_mem = mem

    return best_mem, best_score


def _is_duplicate(content: str, scope_id: str = "default", threshold: float = 0.7,
                  return_match: bool = False) -> dict | bool | None:
    """Check for duplicate via blended word/bigram/trigram overlap against Obsidian memories."""
    content_words = _word_set(content)
    if not content_words:
        return None if return_match else False

    best_mem, best_score = _find_best_match(content, scope_id)

    if best_score >= threshold:
        return best_mem if return_match else True
    return None if return_match else False


def _find_related(content: str, exclude_id: str, scope_id: str = "default", limit: int = 3) -> list[str]:
    """Return up to `limit` memory IDs (8-char prefix) related to content, excluding exclude_id."""
    content_words = _word_set(content)
    if not content_words:
        return []

    content_bigrams = _ngram_set(content, 2)
    content_trigrams = _ngram_set(content, 3)

    normalized = _normalize_scope_id(scope_id)
    project = None if normalized == "general" else normalized

    scored = []
    for mem in _obsidian_memories(project):
        mid = mem.get("id", "")
        if mid == exclude_id or mid.startswith(exclude_id) or exclude_id.startswith(mid[:8]):
            continue
        mem_text = mem.get("essence", "") + " " + mem.get("title", "")
        mem_words = _word_set(mem_text)
        if not mem_words:
            continue
        word_c = _containment(content_words, mem_words)
        bigram_c = _containment(content_bigrams, _ngram_set(mem_text, 2))
        trigram_c = _containment(content_trigrams, _ngram_set(mem_text, 3))
        score = 0.5 * word_c + 0.3 * bigram_c + 0.2 * trigram_c
        if score > 0.2:
            scored.append((score, mid[:8]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mid for _, mid in scored[:limit]]


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------

_NEGATION_SIGNALS = {
    "removed", "no longer", "replaced", "deprecated", "instead of",
    "stopped using", "switched from", "ripped out", "deleted",
    "no more", "eliminated", "dropped", "abandoned", "reversed",
}


def _check_contradictions(content: str, scope_id: str = "default") -> list[dict]:
    """Check if new content contradicts existing memories. Returns list of conflicts."""
    content_lower = content.lower()

    # Check if new content contains negation signals
    has_negation = any(signal in content_lower for signal in _NEGATION_SIGNALS)
    if not has_negation:
        return []  # No negation in new content — skip expensive search

    # Find similar existing memories
    try:
        from memem.search_index import _search_fts
        fts_ids = _search_fts(content[:200], scope_id, 5)
        candidates = [m for mid in fts_ids if (m := _find_memory(mid))]
    except Exception as exc:
        # FTS unavailable — log and fall back to content match
        log.warning("contradiction-check FTS lookup failed: %s", exc)
        match, score = _find_best_match(content, scope_id)
        candidates = [match] if match and score > 0.2 else []

    # Only flag when the existing memory and the new content share a meaningful
    # topic overlap — previously any FTS match with any negation word in the
    # new content was treated as a contradiction, which caused wrongful mass
    # deprecations during consolidation.
    new_words = _word_set(content)
    contradictions = []
    for mem in candidates:
        if not mem:
            continue
        mem_essence = mem.get("essence", "")
        mem_lower = mem_essence.lower()
        overlap = _containment(new_words, _word_set(mem_essence))
        if overlap < 0.3:
            continue  # Too little topic overlap to be a real contradiction
        for signal in _NEGATION_SIGNALS:
            if signal in content_lower and signal not in mem_lower:
                contradictions.append({
                    "memory_id": mem.get("id", "")[:8],
                    "title": mem.get("title", "")[:60],
                    "reason": f"new content contains '{signal}' about a topic the existing memory affirms",
                })
                break

    return contradictions


# ---------------------------------------------------------------------------
# Save / update / delete / deprecate
# ---------------------------------------------------------------------------

def _save_memory(mem: dict):
    """Save memory to Obsidian vault."""
    _require_obsidian_writable()

    # Check for contradictions
    contradictions = _check_contradictions(mem.get("essence", ""), mem.get("project", "default"))
    if contradictions:
        mem["contradicts"] = [c["memory_id"] for c in contradictions]

    # Compute related links first so we only write the memory once.
    content = mem.get("essence", "")
    mem_id = mem.get("id", "")
    if content and mem_id:
        related = _find_related(content, exclude_id=mem_id, scope_id=mem.get("project", "default"))
        if related:
            mem["related"] = related

    _write_obsidian_memory(mem)
    if INDEX_PATH.exists():
        _append_or_update_index_line(mem)
    _log_event("save", mem.get("id", ""), title=mem.get("title", ""))
    _index_memory(mem)


def _update_memory(memory_id: str, new_content: str, new_title: str = "") -> None:
    """Update an existing memory's content and optionally its title."""
    threat = scan_memory_content(new_content)
    if threat:
        raise ValueError(f"Update blocked: {threat}")
    mem = _find_memory(memory_id)
    if mem is None:
        raise ValueError(f"Memory not found: {memory_id}")
    mem["essence"] = new_content
    if new_title:
        mem["title"] = new_title
    mem["updated_at"] = _now()
    _write_obsidian_memory(mem)
    _append_or_update_index_line(mem)
    _log_event("update", memory_id)
    _index_memory(mem)


def _delete_memory(memory_id: str) -> bool:
    try:
        mem = _find_memory(memory_id)
        if mem:
            obsidian_path = Path(mem.get("file", ""))
            if obsidian_path.exists():
                obsidian_path.unlink()
        _remove_index_line(memory_id)
        _log_event("delete", memory_id)
        _remove_from_index(memory_id)
        return True
    except Exception:
        return False


def _deprecate_memory(memory_id: str, reason: str = "superseded") -> bool:
    """Mark a memory as deprecated instead of deleting it."""
    mem = _find_memory(memory_id)
    if not mem:
        return False
    mem["status"] = "deprecated"
    mem["valid_to"] = _now()
    _write_obsidian_memory(mem)
    _remove_index_line(memory_id)
    _log_event("deprecate", memory_id, reason=reason)
    _remove_from_index(memory_id)
    return True


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

def _extract_project(mem: dict) -> str:
    title = mem.get("title") or ""
    match = re.match(r"^\[([^\]]+)\]", title)
    if match:
        return match.group(1)

    project = mem.get("project", "")
    if project and project != "general":
        return project

    return "general"


def _format_index_line(mem: dict) -> str:
    title = mem.get("title") or mem.get("essence", "")[:60]
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    title = title.split("\n")[0].strip()[:120]

    tags_str = " ".join(f"#{tag}" for tag in mem.get("domain_tags") or [] if tag)
    mem_id = mem.get("id", "")[:8]
    line = f"- {title} ({mem_id})"
    if tags_str:
        line += f" {tags_str}"
    return line


def _memory_date_key(mem: dict) -> datetime:
    try:
        return datetime.fromisoformat((mem.get("created_at") or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=UTC)


def _generate_index(scope_id: str = "default") -> str:
    memories = _obsidian_memories(scope_id if scope_id != "default" else None)
    projects: dict[str, list[dict]] = {}
    for mem in memories:
        project = _extract_project(mem)
        projects.setdefault(project, []).append(mem)

    sorted_projects = sorted(project for project in projects if project != "general")
    if "general" in projects:
        sorted_projects.append("general")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        "# Cortex Memory Index",
        f"Updated: {today} | Total: {len(memories)} memories",
    ]

    for project in sorted_projects:
        mems_sorted = sorted(projects[project], key=_memory_date_key, reverse=True)
        count = len(mems_sorted)
        noun = "memory" if count == 1 else "memories"
        lines.append("")
        lines.append(f"## {project} ({count} {noun})")
        for mem in mems_sorted:
            lines.append(_format_index_line(mem))

    content = "\n".join(lines) + "\n"
    if OBSIDIAN_VAULT.exists():
        _atomic_write(INDEX_PATH, content)
    return content


def _recount_index_sections(lines: list[str]) -> list[str]:
    """Recount section totals and update the Updated header in index lines."""
    for idx, entry in enumerate(lines):
        if entry.startswith("## ") and "(" in entry:
            section_name = entry.split("(")[0].strip().removeprefix("## ").strip()
            count = 0
            for j in range(idx + 1, len(lines)):
                if lines[j].startswith("## "):
                    break
                if lines[j].startswith("- "):
                    count += 1
            noun = "memory" if count == 1 else "memories"
            lines[idx] = f"## {section_name} ({count} {noun})"

    total = sum(1 for entry in lines if entry.startswith("- "))
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    for idx, entry in enumerate(lines):
        if entry.startswith("Updated:"):
            lines[idx] = f"Updated: {today} | Total: {total} memories"
            break

    return lines


def _with_index_lock(func):
    """Decorator: acquire exclusive lock on index file during read-modify-write."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        lock_path = INDEX_PATH.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return func(*args, **kwargs)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
    return wrapper


@_with_index_lock
def _append_or_update_index_line(mem: dict):
    if not INDEX_PATH.exists():
        return

    content = INDEX_PATH.read_text()
    mem_id = mem["id"][:8]
    line = _format_index_line(mem)
    project = _extract_project(mem)
    lines = content.split("\n")
    new_lines = [entry for entry in lines if not (entry.startswith("- ") and f"({mem_id})" in entry)]

    section_header = f"## {project} ("
    inserted = False
    for idx, entry in enumerate(new_lines):
        if entry.startswith(section_header):
            new_lines.insert(idx + 1, line)
            inserted = True
            break

    if not inserted:
        new_lines.extend(["", f"## {project} (1 memory)", line])

    new_lines = _recount_index_sections(new_lines)
    _atomic_write(INDEX_PATH, "\n".join(new_lines))


@_with_index_lock
def _remove_index_line(memory_id: str):
    if not INDEX_PATH.exists():
        return

    mem_id = memory_id[:8]
    lines = INDEX_PATH.read_text().split("\n")
    new_lines = [entry for entry in lines if not (entry.startswith("- ") and f"({mem_id})" in entry)]

    new_lines = _recount_index_sections(new_lines)
    _atomic_write(INDEX_PATH, "\n".join(new_lines))


# ---------------------------------------------------------------------------
# Purge helper (for cli.py compatibility)
# ---------------------------------------------------------------------------

def purge_mined_memories(mined_sessions_file: Path) -> dict:
    from memem.search_index import _remove_from_index

    deleted = 0
    for mem in _obsidian_memories():
        if "mined" in (mem.get("domain_tags") or []):
            obsidian_path = Path(mem.get("file", ""))
            if obsidian_path.exists():
                obsidian_path.unlink()
                mem_id = mem.get("id", "")
                if mem_id:
                    _remove_from_index(mem_id)
                    _remove_index_line(mem_id)
                deleted += 1

    if mined_sessions_file.exists():
        mined_sessions_file.write_text("")
    return {"deleted": deleted}
