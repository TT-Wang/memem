"""Cross-vault memory search for the memory_remind MCP tool.

Searches across multiple memem vaults and returns a ranked set of hits,
each annotated with a ``why_relevant`` rationale string.

The search scans each vault's memories directory directly (file scan) without
touching the module-level cache or OBSIDIAN_MEMORIES_DIR global, so callers
can pass arbitrary vault paths and get back clean results.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict

import structlog

_vault_log = structlog.get_logger("memem-vault-registry")


def load_vault_registry() -> list[dict]:
    """Load the vault registry from ~/.memem/vaults.json.

    Returns a list of dicts, each with at least ``id`` and ``path`` keys.

    When vaults.json does not exist, returns a synthetic single-element list
    pointing at the current default vault so callers work without configuration.
    Invalid entries (missing ``id`` or ``path``) are skipped with a warning.

    (v1.11.0: moved here from former ``memem/vault_registry.py`` — this is the
    module that actually consumes the registry, so the loader belongs here.)
    """
    from memem.models import MEMEM_DIR, OBSIDIAN_MEMORIES_DIR

    vaults_json = MEMEM_DIR / "vaults.json"
    default = [{"id": "default", "path": str(OBSIDIAN_MEMORIES_DIR)}]

    if not vaults_json.exists():
        return default

    try:
        raw = json.loads(vaults_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _vault_log.warning("vaults.json read failed", error=str(exc))
        return default

    if not isinstance(raw, list):
        _vault_log.warning("vaults.json must be a JSON array")
        return default

    valid: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            _vault_log.warning("invalid vault entry not a dict", entry=repr(entry))
            continue
        vault_id = entry.get("id")
        vault_path = entry.get("path")
        if not vault_id:
            _vault_log.warning("vault entry missing id", entry=repr(entry))
            continue
        if not vault_path:
            _vault_log.warning("vault entry missing path", vault_id=vault_id, entry=repr(entry))
            continue
        valid.append({"id": str(vault_id), "path": str(vault_path)})

    if not valid:
        _vault_log.warning("vaults.json had no valid entries")
        return default

    return valid


class ReminderHit(TypedDict):
    """A single cross-vault recall result."""

    vault_id: str
    memory_id: str
    title: str
    content: str
    why_relevant: str
    similarity: float


def _word_tokens(text: str) -> list[str]:
    """Return a list of lowercase word tokens from *text*."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _top_overlap_phrase(query: str, target: str) -> str | None:
    """Find the longest 1-2 token overlap between *query* and *target*.

    Returns the overlapping token(s) as a single string, or None if no
    meaningful overlap was found.  Prefers 2-token bigrams over single tokens.
    """
    q_tokens = _word_tokens(query)
    t_tokens = _word_tokens(target)
    t_set = set(t_tokens)

    # Try bigrams first (up to 2 consecutive query tokens that both appear in target)
    for i in range(len(q_tokens) - 1):
        bigram_a = q_tokens[i]
        bigram_b = q_tokens[i + 1]
        if bigram_a in t_set and bigram_b in t_set:
            return f"{bigram_a} {bigram_b}"

    # Fall back to longest single token overlap (excluding very short stop words)
    q_set = set(q_tokens)
    overlap = q_set & t_set
    stop = {"a", "an", "the", "and", "or", "in", "on", "at", "to", "of", "is", "it", "be", "as"}
    meaningful = overlap - stop
    if meaningful:
        # Prefer longer tokens as they are more distinctive
        return max(meaningful, key=len)

    return None


def _compute_similarity(query: str, title: str, content: str) -> float:
    """Compute a simple word-overlap similarity score in [0, 1].

    Uses a keyword scoring approach (title matches weighted double over body)
    similar to the fallback path in recall._search_memories.
    """
    from memem.obsidian_store import _word_set  # type: ignore[attr-defined]

    q_words = _word_set(query)
    if not q_words:
        return 0.0

    snippet = (title + " " + content[:100]).strip()
    t_words = _word_set(snippet)
    title_words = _word_set(title)

    title_hits = len(q_words & title_words)
    body_words = t_words - title_words
    body_hits = len(q_words & body_words)

    # Title matches count double
    raw_score = (title_hits * 2 + body_hits) / len(q_words)
    # Clamp to [0, 1]
    return min(1.0, raw_score)


def _make_why_relevant(query: str, title: str, content: str, sim: float) -> str:
    """Build the why_relevant string for a single hit.

    Format: ``"matches {sim:.2f} on '{phrase}'"`` where *phrase* is the
    longest 1-2 token overlap.  Falls back to ``"matches {sim:.2f} on title"``
    when no overlap is found.
    """
    target = title + " " + content[:100]
    phrase = _top_overlap_phrase(query, target)
    if phrase:
        return f"matches {sim:.2f} on '{phrase}'"
    return f"matches {sim:.2f} on title"


def _scan_vault_memories(vault_path: str) -> list[dict]:
    """Scan all *.md files in *vault_path* and return parsed memory dicts.

    Uses the same ``_parse_obsidian_memory_file`` parser as the main vault,
    but reads directly from the given path rather than from the module-level
    OBSIDIAN_MEMORIES_DIR global — so arbitrary vault paths work cleanly.
    """
    memories_dir = Path(vault_path)
    if not memories_dir.exists():
        return []

    from memem.obsidian_store import _parse_obsidian_memory_file  # type: ignore[attr-defined]

    results: list[dict] = []
    for md_file in sorted(memories_dir.glob("*.md")):
        try:
            mem = _parse_obsidian_memory_file(md_file)
        except Exception as exc:  # noqa: BLE001
            _vault_log.debug("failed to parse memory file", path=str(md_file), error=str(exc))
            continue
        if mem is None:
            continue
        if mem.get("status", "active") == "deprecated":
            continue
        if mem.get("invalid_at") is not None:
            continue
        results.append(mem)
    return results


def search_across_vaults(
    query: str,
    vault_registry: list[dict],
    max_results: int = 3,
) -> list[ReminderHit]:
    """Search *query* across all vaults in *vault_registry*.

    Each vault entry must have ``id`` and ``path`` keys.  Results are merged,
    ranked by descending similarity score, and the top *max_results* are
    returned.

    Each result is a :class:`ReminderHit` dict with keys:
    ``vault_id``, ``memory_id``, ``title``, ``content``, ``why_relevant``,
    ``similarity``.

    The ``why_relevant`` field format is:
    ``"matches {sim:.2f} on '{top_overlap_phrase}'"``
    or ``"matches {sim:.2f} on title"`` when no overlap phrase is found.
    """
    if not query.strip():
        return []

    all_scored: list[tuple[float, dict, str]] = []  # (score, mem, vault_id)

    for vault_entry in vault_registry:
        vault_id = vault_entry.get("id", "unknown")
        vault_path = vault_entry.get("path", "")
        if not vault_path:
            _vault_log.warning("skipping vault with empty path", vault_id=vault_id)
            continue

        try:
            memories = _scan_vault_memories(vault_path)
        except Exception as exc:  # noqa: BLE001
            _vault_log.warning("failed to scan vault", vault_id=vault_id, vault_path=vault_path, error=str(exc))
            continue

        for mem in memories:
            title = mem.get("title", "Untitled")
            content = mem.get("essence") or mem.get("full_record", "") or ""
            sim = _compute_similarity(query, title, content)
            if sim > 0.0:
                all_scored.append((sim, mem, vault_id))

    # Sort by similarity descending, stable (Python sort is stable)
    all_scored.sort(key=lambda t: t[0], reverse=True)

    hits: list[ReminderHit] = []
    for sim, mem, vault_id in all_scored[:max_results]:
        title = mem.get("title", "Untitled")
        content = mem.get("essence") or mem.get("full_record", "") or ""
        why = _make_why_relevant(query, title, content, sim)
        hits.append(
            ReminderHit(
                vault_id=vault_id,
                memory_id=mem.get("id", ""),
                title=title,
                content=content,
                why_relevant=why,
                similarity=sim,
            )
        )

    return hits
