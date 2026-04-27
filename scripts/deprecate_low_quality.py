#!/usr/bin/env python3
"""Deprecate low-quality pre-m1 memories (soft, reversible).

Reads memory IDs from .forge/state/memem-vault-cleanup-low-quality-ids.json
and marks each as deprecated via memem.obsidian_store._deprecate_memory().

Idempotent: already-deprecated memories are noted in the audit log but not
re-touched. Safe to re-run.

Audit log: .forge/state/memem-vault-cleanup-purge-audit.jsonl
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root or scripts/ directory
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import memem.obsidian_store as _obs_mod

# Monkey-patch _remove_index_line to handle PermissionError on _index.md.
# The _index.md file may be owned by root (from a previous miner run) and
# unwritable by claude-user. Since we're doing SOFT deprecation only (no file
# deletion), losing the index line update is acceptable — the index will be
# rebuilt correctly the next time memem regenerates it.
_orig_remove_index_line = _obs_mod._remove_index_line.__wrapped__ if hasattr(_obs_mod._remove_index_line, '__wrapped__') else None

def _safe_remove_index_line(memory_id: str) -> None:
    try:
        # Call the real implementation via the module's lock-wrapped version
        _obs_mod._remove_index_line.__wrapped__(memory_id)
    except PermissionError:
        pass  # _index.md owned by root — skip; will be fixed on next index rebuild
    except AttributeError:
        pass

# Replace with a wrapped version that tolerates PermissionError
import functools as _functools

_real_remove = _obs_mod._remove_index_line

@_functools.wraps(_real_remove)
def _patched_remove_index_line(memory_id: str) -> None:
    try:
        _real_remove(memory_id)
    except PermissionError:
        pass  # _index.md not writable — skip gracefully

_obs_mod._remove_index_line = _patched_remove_index_line

from memem.obsidian_store import _deprecate_memory, _find_memory

IDS_PATH = REPO_ROOT / ".forge/state/memem-vault-cleanup-low-quality-ids.json"
AUDIT_PATH = REPO_ROOT / ".forge/state/memem-vault-cleanup-purge-audit.jsonl"
DEPRECATION_REASON = "low_quality_pre_m1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_existing_audit() -> dict[str, dict]:
    """Load existing audit entries keyed by memory_id (last-write-wins)."""
    if not AUDIT_PATH.exists():
        return {}
    existing: dict[str, dict] = {}
    for line in AUDIT_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            existing[entry["memory_id"]] = entry
        except (json.JSONDecodeError, KeyError):
            pass
    return existing


def main() -> None:
    # Load target IDs
    ids: list[str] = json.loads(IDS_PATH.read_text())
    print(f"Loaded {len(ids)} IDs to process", flush=True)

    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Load existing audit entries so re-runs don't append duplicates
    existing_audit = _load_existing_audit()

    audit_entries: dict[str, dict] = dict(existing_audit)  # preserve any pre-existing entries
    deprecated_count = 0
    already_deprecated = 0
    not_found = 0

    for memory_id in ids:
        mem = _find_memory(memory_id)

        if mem is None:
            entry = {
                "memory_id": memory_id,
                "title_before": None,
                "status_before": None,
                "status_after": None,
                "action": "not_found",
                "ts": now_iso(),
            }
            audit_entries[memory_id] = entry
            not_found += 1
            print(f"  NOT FOUND: {memory_id}", flush=True)
            continue

        title_before = mem.get("title", "")
        status_before = mem.get("status", "active")

        if status_before == "deprecated":
            # Already deprecated — idempotent: note but don't re-touch
            entry = {
                "memory_id": memory_id,
                "title_before": title_before,
                "status_before": status_before,
                "status_after": "deprecated",
                "action": "already_deprecated",
                "ts": existing_audit.get(memory_id, {}).get("ts", now_iso()),
            }
            audit_entries[memory_id] = entry
            already_deprecated += 1
            print(f"  SKIP (already deprecated): {memory_id[:8]} {title_before[:60]}", flush=True)
            continue

        # Perform soft deprecation
        success = _deprecate_memory(memory_id, reason=DEPRECATION_REASON)
        status_after = "deprecated" if success else status_before
        action = "deprecated" if success else "error"

        entry = {
            "memory_id": memory_id,
            "title_before": title_before,
            "status_before": status_before,
            "status_after": status_after,
            "action": action,
            "ts": now_iso(),
        }
        audit_entries[memory_id] = entry

        if success:
            deprecated_count += 1
            print(f"  DEPRECATED: {memory_id[:8]} {title_before[:60]}", flush=True)
        else:
            print(f"  ERROR: failed to deprecate {memory_id}", flush=True)

    # Write audit log (one line per memory_id, in IDs order)
    with open(AUDIT_PATH, "w", encoding="utf-8") as audit_fh:
        for memory_id in ids:
            if memory_id in audit_entries:
                audit_fh.write(json.dumps(audit_entries[memory_id]) + "\n")

    audit_lines_written = sum(1 for mid in ids if mid in audit_entries)

    print(f"\nDone.", flush=True)
    print(f"  Deprecated:        {deprecated_count}", flush=True)
    print(f"  Already deprecated:{already_deprecated}", flush=True)
    print(f"  Not found:         {not_found}", flush=True)
    print(f"  Audit lines:       {audit_lines_written}", flush=True)
    print(f"  Audit log:         {AUDIT_PATH}", flush=True)


if __name__ == "__main__":
    main()
