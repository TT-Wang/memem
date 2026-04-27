#!/usr/bin/env python3
"""Identify low-quality memories in the memem Obsidian vault.

Selection criteria (OR logic):
  (a) Duplicate titles — case-insensitive; keep newer, flag older
  (b) confidence field < 0.4 if present
  (c) Noise patterns:
        - title starts with 'task_files'
        - title starts with 'The command requires'
        - body dominated by shell output lines ($ / >>> > 50% of non-empty lines)
        - body length < 50 chars
  (d) status already 'deprecated' (noted separately, excluded from active count)

Usage:
    python3 identify_low_quality_memories.py [--dry-run]

Outputs (on full run):
    .forge/state/memem-vault-cleanup-low-quality-ids.json
    .forge/state/memem-vault-cleanup-low-quality-summary.md
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
STATE_DIR = REPO_ROOT / ".forge" / "state"
IDS_FILE = STATE_DIR / "memem-vault-cleanup-low-quality-ids.json"
SUMMARY_FILE = STATE_DIR / "memem-vault-cleanup-low-quality-summary.md"


def load_memories():
    """Load all memories from the Obsidian vault via memem's store."""
    sys.path.insert(0, str(REPO_ROOT))
    from memem.obsidian_store import _obsidian_memories  # noqa: PLC0415

    return _obsidian_memories(include_deprecated=True)


def _normalize_title(title: str) -> str:
    """Lowercase and strip title for duplicate comparison."""
    return title.strip().lower()


def identify_low_quality(memories: list[dict]) -> dict:
    """Apply all criteria and return a results dict with IDs and breakdown.

    Returns:
        {
          "active_flagged_ids": list[str],   # IDs of active low-quality memories
          "deprecated_ids": list[str],        # IDs of already-deprecated memories
          "breakdown": {
              "duplicate": list[dict],        # {id, title, reason}
              "low_confidence": list[dict],
              "noise_title": list[dict],
              "shell_output": list[dict],
              "short_body": list[dict],
              "deprecated": list[dict],
          }
        }
    """
    active = [m for m in memories if m.get("status") != "deprecated"]
    deprecated = [m for m in memories if m.get("status") == "deprecated"]

    flagged: dict[str, str] = {}  # id -> reason (first reason wins for reporting)

    # -----------------------------------------------------------------------
    # (a) Duplicate titles — keep newest, flag older
    # -----------------------------------------------------------------------
    titles: dict[str, list[dict]] = defaultdict(list)
    for m in active:
        t = _normalize_title(m.get("title", ""))
        if t:
            titles[t].append(m)

    dup_flagged: list[dict] = []
    for _title, group in titles.items():
        if len(group) <= 1:
            continue
        # Sort by created_at DESC, then updated_at DESC; keep first (newest)
        def sort_key(x: dict) -> str:
            return x.get("created_at", "") or x.get("updated_at", "") or ""

        sorted_group = sorted(group, key=sort_key, reverse=True)
        for older in sorted_group[1:]:
            mid = older["id"]
            if mid not in flagged:
                flagged[mid] = "duplicate_title"
                dup_flagged.append({
                    "id": mid,
                    "title": older.get("title", ""),
                    "reason": "duplicate_title",
                    "created_at": older.get("created_at", ""),
                })

    # -----------------------------------------------------------------------
    # (b) confidence < 0.4 if present
    # -----------------------------------------------------------------------
    conf_flagged: list[dict] = []
    for m in active:
        conf = m.get("confidence")
        if conf is not None:
            try:
                fconf = float(conf)
                if fconf < 0.4:
                    mid = m["id"]
                    if mid not in flagged:
                        flagged[mid] = "low_confidence"
                    conf_flagged.append({
                        "id": mid,
                        "title": m.get("title", ""),
                        "reason": f"low_confidence:{fconf:.2f}",
                        "created_at": m.get("created_at", ""),
                    })
            except (TypeError, ValueError):
                pass

    # -----------------------------------------------------------------------
    # (c) Noise patterns
    # -----------------------------------------------------------------------
    noise_title_flagged: list[dict] = []
    shell_output_flagged: list[dict] = []
    short_body_flagged: list[dict] = []

    for m in active:
        mid = m["id"]
        title = m.get("title", "")
        body = m.get("essence", "")

        # Noise title patterns
        if re.match(r"^task_?files", title, re.IGNORECASE) or re.match(
            r"^The command requires", title, re.IGNORECASE
        ):
            if mid not in flagged:
                flagged[mid] = "noise_title"
            noise_title_flagged.append({
                "id": mid,
                "title": title,
                "reason": "noise_title_pattern",
                "created_at": m.get("created_at", ""),
            })
            continue

        # Shell output dominated ($ or >>> starting > 50% of non-empty lines)
        non_empty = [line for line in body.splitlines() if line.strip()]
        if non_empty:
            shell_lines = sum(
                1
                for line in non_empty
                if line.strip().startswith("$") or line.strip().startswith(">>>")
            )
            if shell_lines / len(non_empty) > 0.5:
                if mid not in flagged:
                    flagged[mid] = "shell_output"
                shell_output_flagged.append({
                    "id": mid,
                    "title": title,
                    "reason": f"shell_output_dominated:{shell_lines}/{len(non_empty)}",
                    "created_at": m.get("created_at", ""),
                })
                continue

        # Short body < 50 chars
        if len(body.strip()) < 50:
            if mid not in flagged:
                flagged[mid] = "short_body"
            short_body_flagged.append({
                "id": mid,
                "title": title,
                "reason": f"short_body:{len(body.strip())}chars",
                "created_at": m.get("created_at", ""),
            })

    # -----------------------------------------------------------------------
    # (d) Deprecated — noted separately, excluded from active count
    # -----------------------------------------------------------------------
    deprecated_entries: list[dict] = [
        {
            "id": m["id"],
            "title": m.get("title", ""),
            "reason": "status_deprecated",
            "created_at": m.get("created_at", ""),
        }
        for m in deprecated
    ]

    active_flagged_ids = list(flagged.keys())

    return {
        "active_flagged_ids": active_flagged_ids,
        "deprecated_ids": [m["id"] for m in deprecated],
        "breakdown": {
            "duplicate": dup_flagged,
            "low_confidence": conf_flagged,
            "noise_title": noise_title_flagged,
            "shell_output": shell_output_flagged,
            "short_body": short_body_flagged,
            "deprecated": deprecated_entries,
        },
        "total_active": len(active),
        "total_deprecated": len(deprecated),
    }


def print_stats(results: dict) -> None:
    """Print summary statistics to stdout."""
    bd = results["breakdown"]
    total = len(results["active_flagged_ids"])
    print("=== memem vault low-quality memory identification ===")
    print(f"Total active memories scanned: {results['total_active']}")
    print(f"Total deprecated (already inactive): {results['total_deprecated']}")
    print()
    print(f"Active low-quality memories flagged: {total}")
    print(f"  (a) Duplicate titles:   {len(bd['duplicate'])}")
    print(f"  (b) Low confidence:     {len(bd['low_confidence'])}")
    print(f"  (c) Noise title:        {len(bd['noise_title'])}")
    print(f"  (c) Shell output body:  {len(bd['shell_output'])}")
    print(f"  (c) Short body (<50):   {len(bd['short_body'])}")
    print()
    print(f"Note: {results['total_deprecated']} deprecated memories are excluded from active count.")
    print(f"      They are tracked separately but not included in the {total} flagged IDs.")


def write_outputs(results: dict) -> None:
    """Write JSON IDs file and markdown summary file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    ids = results["active_flagged_ids"]
    IDS_FILE.write_text(json.dumps(ids, indent=2) + "\n", encoding="utf-8")
    print(f"Written: {IDS_FILE}  ({len(ids)} IDs)")

    bd = results["breakdown"]

    # Build top-10 sample (across all criteria in priority order)
    all_entries: list[dict] = (
        bd["duplicate"] + bd["low_confidence"] + bd["noise_title"]
        + bd["shell_output"] + bd["short_body"]
    )
    seen_ids: set[str] = set()
    top10: list[dict] = []
    for entry in all_entries:
        if entry["id"] not in seen_ids:
            seen_ids.add(entry["id"])
            top10.append(entry)
        if len(top10) == 10:
            break

    lines = [
        "# memem Vault Cleanup — Low-Quality Memory Candidates",
        "",
        "Generated by `scripts/identify_low_quality_memories.py` (idempotent, safe to re-run).",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total active memories scanned | {results['total_active']} |",
        f"| **Active low-quality flagged** | **{len(ids)}** |",
        f"| Already deprecated (not counted) | {results['total_deprecated']} |",
        "",
        "## Breakdown by Criterion",
        "",
        "| Criterion | IDs flagged |",
        "|-----------|-------------|",
        f"| (a) Duplicate titles | {len(bd['duplicate'])} |",
        f"| (b) Low confidence (<0.4) | {len(bd['low_confidence'])} |",
        f"| (c) Noise title patterns | {len(bd['noise_title'])} |",
        f"| (c) Shell output dominated | {len(bd['shell_output'])} |",
        f"| (c) Short body (<50 chars) | {len(bd['short_body'])} |",
        f"| **TOTAL (unique, OR logic)** | **{len(ids)}** |",
        "",
        "## Top 10 Sample Entries",
        "",
        "| # | Memory ID | Title | Reason |",
        "|---|-----------|-------|--------|",
    ]

    for i, entry in enumerate(top10, 1):
        mid = entry["id"]
        title = entry["title"].replace("|", "\\|")[:60]
        reason = entry["reason"]
        lines.append(f"| {i} | `{mid[:8]}` | {title} | {reason} |")

    lines += [
        "",
        "## Full ID List",
        "",
        f"See `{IDS_FILE.name}` for the complete array of {len(ids)} memory IDs.",
        "",
        "## Criterion Details",
        "",
        "### (a) Duplicate Titles",
        f"*{len(bd['duplicate'])} memories* — same title as a newer memory (case-insensitive match). "
        "Keep the newest, flag older.",
        "",
        "### (b) Low Confidence",
        f"*{len(bd['low_confidence'])} memories* — confidence field < 0.4.",
        "",
        "### (c) Noise Patterns",
        f"*{len(bd['noise_title'])} noise title + {len(bd['shell_output'])} shell-dominated + "
        f"{len(bd['short_body'])} short body* memories",
        "- Titles starting with `task_files` or `The command requires`",
        "- Body where >50% of non-empty lines start with `$` or `>>>`",
        "- Body shorter than 50 characters",
        "",
        "### (d) Already Deprecated",
        f"*{results['total_deprecated']} memories* — status is `deprecated`. "
        "These are already excluded from active recall and are not counted in the 80-95 target range. "
        "They are documented here for completeness.",
    ]

    SUMMARY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Written: {SUMMARY_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Identify low-quality memories in the memem Obsidian vault."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats without writing output files.",
    )
    args = parser.parse_args()

    print("Loading memories from Obsidian vault...")
    memories = load_memories()
    print(f"Loaded {len(memories)} memories total.")

    results = identify_low_quality(memories)
    print_stats(results)

    n = len(results["active_flagged_ids"])
    if not (80 <= n <= 95):
        print(
            f"\nWARNING: ID count {n} is outside the expected 80-95 range. "
            "Review criteria or report as a concern.",
            file=sys.stderr,
        )

    if args.dry_run:
        print("\n[dry-run] Skipping file writes.")
    else:
        write_outputs(results)


if __name__ == "__main__":
    main()
