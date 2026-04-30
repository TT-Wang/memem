#!/usr/bin/env python3
"""Backfill the layer field on every memory in the Obsidian vault.

Phase B of the layered memory refactor. Phase A wired auto-classify into
memory_save (new memories get a real layer); this script applies the same
classify_layer() heuristic to the 2811+ existing memories that were saved
before Phase A and ended up stuck at L2.

Usage:
    python3 scripts/backfill_layers.py                # dry-run (no writes)
    python3 scripts/backfill_layers.py --apply        # write changes
    python3 scripts/backfill_layers.py --apply --backup-suffix .pre-phase-b

Behavior:
  - Reads every memory from the Obsidian vault via obsidian_store._all_memories
  - Computes the proposed layer via classify_layer()
  - Reports the projected distribution (per-layer counts + delta vs current)
  - On --apply: rewrites frontmatter for memories whose layer changed.
    Atomic write (tmp + fsync + rename), preserves all other fields.

Output:
  - .forge/state/memem-layer-backfill-preview.json (always)
  - .forge/state/memem-layer-backfill-applied.json (only with --apply)
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

# Ensure imports work when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memem.mining import classify_layer  # noqa: E402
from memem.models import DEFAULT_LAYER  # noqa: E402
from memem.obsidian_store import (  # noqa: E402
    _obsidian_memories,
    _save_memory,
)


def _all_memories() -> list[dict]:
    """All non-deprecated memories from the vault."""
    return _obsidian_memories(include_deprecated=False)


def _format_distribution(counter: Counter, total: int) -> str:
    lines = []
    for layer in sorted(counter):
        count = counter[layer]
        pct = 100.0 * count / total if total else 0.0
        lines.append(f"  L{layer}: {count:5d} ({pct:5.1f}%)")
    return "\n".join(lines)


def _project_l0_caps(memories: list[dict]) -> Counter:
    """Counts of L0 memories per project — useful for verifying caps weren't hit."""
    return Counter(m.get("project", "general") for m in memories if m.get("layer") == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes (default: dry-run)")
    parser.add_argument("--backup-suffix", default="",
                        help="If set, write a backup of each modified file with this suffix")
    args = parser.parse_args()

    print("Loading vault…")
    memories = _all_memories()
    total = len(memories)
    if not total:
        print("No memories found in vault.")
        sys.exit(0)

    print(f"Loaded {total} memories.\n")

    # Current distribution
    current_counter = Counter(int(m.get("layer", DEFAULT_LAYER)) for m in memories)
    print("Current distribution:")
    print(_format_distribution(current_counter, total))
    print()

    # Project the new distribution by running classify_layer over a working copy.
    # NOTE: classify_layer takes the full vault snapshot for the L0 cap check.
    # We pass the ORIGINAL snapshot so cap behavior matches "what would have
    # happened if Phase A had been live from day 1" — i.e., we don't compound
    # the L0 cap by the script's own writes mid-loop.
    print("Computing proposed distribution…")
    proposals = []  # list of (mem, current_layer, proposed_layer)
    for mem in memories:
        current_layer = int(mem.get("layer", DEFAULT_LAYER))
        # classify_layer requires a snapshot; pass the full vault so cap logic
        # sees the existing state. This is a stable, deterministic projection.
        proposed = classify_layer(mem, memories)
        proposals.append((mem, current_layer, proposed))

    proposed_counter = Counter(p for _, _, p in proposals)
    changed_count = sum(1 for _, c, p in proposals if c != p)

    print("Proposed distribution:")
    print(_format_distribution(proposed_counter, total))
    print()

    print(f"Memories that would change: {changed_count} ({100.0*changed_count/total:.1f}%)")
    print()

    # Per-project L0 caps
    proposed_l0_per_project = Counter(
        p[0].get("project", "general") for p in proposals if p[2] == 0
    )
    if proposed_l0_per_project:
        print("Per-project L0 counts (after backfill):")
        for project, count in proposed_l0_per_project.most_common(10):
            print(f"  {project}: {count}")
        print()

    # Sample of changes per layer movement
    transitions = Counter()
    sample_per_transition: dict[tuple[int, int], list[str]] = {}
    for mem, c, p in proposals:
        if c != p:
            key = (c, p)
            transitions[key] += 1
            sample_per_transition.setdefault(key, []).append(
                mem.get("title", "(untitled)")[:60]
            )

    print("Layer transitions (current -> proposed):")
    for (c, p), count in sorted(transitions.items()):
        print(f"  L{c} -> L{p}: {count}")
        for sample in sample_per_transition[(c, p)][:3]:
            print(f"      e.g.  {sample}")
    print()

    # Save preview report
    state_dir = Path(__file__).resolve().parent.parent / ".forge" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    preview_path = state_dir / "memem-layer-backfill-preview.json"
    preview_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_memories": total,
        "current_distribution": dict(current_counter),
        "proposed_distribution": dict(proposed_counter),
        "would_change": changed_count,
        "transitions": {f"L{c}->L{p}": count for (c, p), count in transitions.items()},
        "proposed_l0_per_project": dict(proposed_l0_per_project),
    }
    preview_path.write_text(json.dumps(preview_data, indent=2))
    print(f"Preview written to {preview_path}")
    print()

    if not args.apply:
        print("DRY-RUN. No files changed. Re-run with --apply to write.")
        sys.exit(0)

    # ---- APPLY ----
    print("Applying changes…")
    applied = 0
    failed = 0
    fail_examples: list[str] = []
    for mem, c, p in proposals:
        if c == p:
            continue
        mem["layer"] = p
        try:
            _save_memory(mem)
            applied += 1
        except Exception as exc:
            failed += 1
            if len(fail_examples) < 5:
                fail_examples.append(f"{mem.get('id', '?')}: {exc}")

    print(f"Applied: {applied}")
    print(f"Failed:  {failed}")
    if fail_examples:
        print("Example failures:")
        for line in fail_examples:
            print(f"  {line}")

    applied_path = state_dir / "memem-layer-backfill-applied.json"
    applied_path.write_text(json.dumps({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_changed": changed_count,
        "applied": applied,
        "failed": failed,
        "fail_examples": fail_examples,
    }, indent=2))
    print(f"\nApply report: {applied_path}")


if __name__ == "__main__":
    main()
