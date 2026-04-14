"""Playbook refinement logic for memem.

Playbooks are per-project curated knowledge documents derived directly
from memories. Memories are the single source of truth; playbooks are
a derived view, refreshed via Claude Haiku whenever the project's
memory set changes.

v0.9.3 removed the staging layer (`_playbook_append` + staging jsonl)
that existed before — it was batch-gated and caused drift where
projects with many memories ended up with no playbook. The refine
function now reads all active memories for a project directly, with a
staleness hash check so re-running is a no-op when nothing changed.
"""

import hashlib
import logging
import subprocess
from datetime import UTC, datetime

from memem.models import PLAYBOOK_DIR
from memem.obsidian_store import _atomic_write, _obsidian_memories
from memem.security import scan_memory_content
from memem.telemetry import _log_event

log = logging.getLogger("memem-playbook")

# Minimum memories before a project gets its own playbook. Below this
# threshold the refinement is noise, not signal.
MIN_MEMORIES_FOR_PLAYBOOK = 5

# Below this combined size, skip Haiku and write the raw concatenation.
HAIKU_BYPASS_SIZE = 2000

_REFINE_SYSTEM = (
    "You are refining a project knowledge playbook. Reorganize sections by topic. "
    "Merge redundant entries. Remove obsolete information that contradicts newer entries. "
    "Keep it concise and well-structured. Preserve the title line and Updated date at the top. "
    "Output the refined playbook as clean markdown."
)


def _project_memories(project: str) -> list[dict]:
    """All active memories where mem['project'] == project."""
    return [m for m in _obsidian_memories() if m.get("project", "general") == project]


def _source_hash(memories: list[dict]) -> str:
    """Stable hash over the set of memory ids and their content.

    Sorting by id makes the hash insensitive to file-order variation.
    If any memory is added, removed, or mutated, the hash changes and
    the playbook is rebuilt. Otherwise refine is a cheap no-op.
    """
    h = hashlib.sha256()
    for mem in sorted(memories, key=lambda m: m.get("id", "")):
        h.update(mem.get("id", "").encode())
        h.update(b":")
        h.update(mem.get("essence", "").encode())
        h.update(b"\n")
    return h.hexdigest()


def _read_existing_source_hash(playbook_path) -> str | None:
    """Extract the <!-- memem-source-hash: XYZ --> marker from the current playbook."""
    if not playbook_path.exists():
        return None
    try:
        text = playbook_path.read_text()
    except OSError:
        return None
    marker = "<!-- memem-source-hash:"
    idx = text.rfind(marker)
    if idx == -1:
        return None
    end = text.find("-->", idx)
    if end == -1:
        return None
    return text[idx + len(marker):end].strip()


def _playbook_refine(project: str, force: bool = False) -> dict:
    """Rebuild the playbook for ``project`` from its current memories.

    Returns a dict describing the action taken, useful for CLI output and
    the mining batch sweep:
        {"action": "skipped_few_memories", "count": int}
        {"action": "skipped_unchanged"}
        {"action": "written_raw" | "written_refined", "count": int}
        {"action": "failed", "reason": str}

    ``force=True`` bypasses the staleness hash check.
    """
    playbook_path = PLAYBOOK_DIR / f"{project}.md"

    memories = _project_memories(project)
    if len(memories) < MIN_MEMORIES_FOR_PLAYBOOK:
        return {"action": "skipped_few_memories", "count": len(memories)}

    # Staleness check: skip if memories haven't changed since the last refine.
    src_hash = _source_hash(memories)
    if not force:
        existing_hash = _read_existing_source_hash(playbook_path)
        if existing_hash == src_hash:
            return {"action": "skipped_unchanged"}

    # Build combined content from all memories.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    parts = [f"# {project} — Project Playbook", f"Updated: {today}", ""]
    for mem in memories:
        title = mem.get("title", "Untitled")
        essence = mem.get("essence", "").strip()
        if not essence:
            continue
        parts.append(f"## {title}")
        parts.append(essence)
        parts.append("")
    combined = "\n".join(parts).strip()

    threat = scan_memory_content(combined)
    if threat:
        log.warning("Playbook blocked for %s: %s", project, threat)
        return {"action": "failed", "reason": f"security scan: {threat}"}

    # Skip Haiku entirely when the combined content is small — the raw
    # concatenation is already clean enough and there's nothing to merge.
    if len(combined) < HAIKU_BYPASS_SIZE:
        final = combined + f"\n\n<!-- memem-source-hash:{src_hash} -->\n"
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(playbook_path, final)
        _log_event("refine", project=project)
        return {"action": "written_raw", "count": len(memories)}

    # Haiku refine for larger projects.
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _REFINE_SYSTEM],
            input=combined,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception as exc:
        log.warning("Playbook refine failed for %s: %s", project, exc)
        return {"action": "failed", "reason": str(exc)}

    if result.returncode != 0 or not result.stdout.strip():
        log.warning("Playbook refine empty/error for %s: %s", project, result.stderr[:200])
        return {"action": "failed", "reason": f"haiku rc={result.returncode}"}

    refined = result.stdout.strip()
    threat = scan_memory_content(refined)
    if threat:
        log.warning("Playbook refine blocked for %s: %s", project, threat)
        return {"action": "failed", "reason": f"security scan: {threat}"}

    final = refined + f"\n\n<!-- memem-source-hash:{src_hash} -->\n"
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(playbook_path, final)
    _log_event("refine", project=project)
    return {"action": "written_refined", "count": len(memories)}


def _all_projects_with_memories() -> set[str]:
    """Distinct project names across all active memories."""
    return {m.get("project", "general") for m in _obsidian_memories()}


def _playbook_sweep(force: bool = False) -> dict:
    """Refine every project with enough memories. Used by mining batch end + CLI.

    Returns aggregate counts: {"refreshed": N, "skipped": N, "failed": N, "noop": N}.
    Thanks to the staleness hash, calling this repeatedly is cheap — only
    projects that actually changed pay the Haiku cost.
    """
    totals = {"refreshed": 0, "skipped": 0, "failed": 0, "noop": 0}
    for project in sorted(_all_projects_with_memories()):
        result = _playbook_refine(project, force=force)
        action = result.get("action", "")
        if action in ("written_raw", "written_refined"):
            totals["refreshed"] += 1
        elif action == "skipped_unchanged":
            totals["noop"] += 1
        elif action == "skipped_few_memories":
            totals["skipped"] += 1
        elif action == "failed":
            totals["failed"] += 1
    return totals
