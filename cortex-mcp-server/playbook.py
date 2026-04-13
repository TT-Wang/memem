"""Playbook grow-and-refine logic for Cortex.

Manages staged memory entries (grow step) and Haiku-powered
refinement (refine step) for per-project knowledge playbooks.
"""

import hashlib
import json
import logging
import subprocess
from datetime import UTC, datetime

from models import PLAYBOOK_DIR, PLAYBOOK_STAGING_DIR, now_iso
from obsidian_store import _atomic_write
from security import scan_memory_content
from telemetry import _log_event

log = logging.getLogger("cortex-playbook")

_now = now_iso


_REFINE_SYSTEM = (
    "You are refining a project knowledge playbook. Reorganize sections by topic. "
    "Merge redundant entries. Remove obsolete information that contradicts newer entries. "
    "Keep it concise and well-structured. Preserve the title line and Updated date at the top. "
    "Output the refined playbook as clean markdown."
)


def _playbook_append(project: str, new_memory: dict) -> None:
    """Stage a new memory for the next playbook refinement (grow step)."""
    PLAYBOOK_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging_path = PLAYBOOK_STAGING_DIR / f"{project}.jsonl"
    title = new_memory.get("title", "Untitled")
    essence = new_memory.get("essence", "")
    if not essence:
        return
    entry = json.dumps({
        "title": title,
        "essence": essence,
        "timestamp": _now(),
    })
    with open(staging_path, "a") as f:
        f.write(entry + "\n")


def _playbook_refine(project: str) -> None:
    """Compile staged entries into the playbook and refine (refine step)."""
    playbook_path = PLAYBOOK_DIR / f"{project}.md"
    staging_path = PLAYBOOK_STAGING_DIR / f"{project}.jsonl"

    # Read existing compiled playbook
    existing = ""
    if playbook_path.exists():
        existing = playbook_path.read_text().strip()
        # Strip old markers
        for marker in ("<!-- refined:", "<!-- cortex-hash:"):
            idx = existing.rfind(marker)
            if idx != -1:
                existing = existing[:idx].rstrip()

    # Read staged entries
    staged_entries = []
    if staging_path.exists():
        try:
            for line in staging_path.read_text().splitlines():
                line = line.strip()
                if line:
                    staged_entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            pass

    # Nothing to do if no staged entries and playbook exists
    if not staged_entries and existing:
        return

    # If no playbook and no staged entries, nothing to do
    if not staged_entries and not existing:
        return

    # Build content for Haiku
    parts = []
    if existing:
        parts.append(existing)
    for entry in staged_entries:
        parts.append(f"## {entry.get('title', 'Untitled')}\n{entry.get('essence', '')}")
    combined = "\n\n".join(parts)

    # If too small, just write without Haiku
    if len(combined) < 2000:
        threat = scan_memory_content(combined)
        if threat:
            log.warning("Playbook blocked for %s: %s", project, threat)
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if not existing:
            combined = f"# {project} — Project Playbook\nUpdated: {today}\n\n{combined}"
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(playbook_path, combined + "\n")
        _log_event("refine", project=project)
        # Clear staging
        if staging_path.exists():
            staging_path.unlink()
        return

    # Haiku refine
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _REFINE_SYSTEM],
            input=combined,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        log.warning("Playbook refine failed for %s: %s", project, exc)
        return

    if result.returncode != 0 or not result.stdout.strip():
        log.warning("Playbook refine empty/error for %s", project)
        return

    refined = result.stdout.strip()
    # Security scan
    threat = scan_memory_content(refined)
    if threat:
        log.warning("Playbook refine blocked for %s: %s", project, threat)
        return

    new_hash = hashlib.sha256(refined.encode()).hexdigest()
    refined += f"\n\n<!-- refined:{new_hash} -->\n"
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(playbook_path, refined)
    _log_event("refine", project=project)

    # Clear staging after successful compile
    if staging_path.exists():
        staging_path.unlink()
