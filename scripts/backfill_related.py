"""One-shot backfill: add `## Related` wiki-link sections to existing memories.

Context: the wiki-link emitter landed in commit 8f1c34a but the miner daemon
stayed on pre-8f1c34a code for ~24h before a restart. Memories mined in that
window never got `## Related` sections, and any memory whose initial
`_find_related` call returned 0 candidates (common for early memories in a
new vault) also lacks the section.

This script scans the whole vault, re-runs `_find_related` on each memory
that currently has no `related` frontmatter or `## Related` body section,
and rewrites the file. Safe to re-run — already-linked memories are skipped.

Usage:
    python scripts/backfill_related.py --dry-run        # default, reports only
    python scripts/backfill_related.py --apply          # actually writes
    python scripts/backfill_related.py --apply --limit 100   # partial run

The miner daemon should be stopped before --apply to avoid lock contention.
"""

import argparse
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Explicit dry-run flag (on by default if --apply omitted)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N rewrites (0 = no limit)")
    ap.add_argument("--progress-every", type=int, default=100,
                    help="Print a progress line every N memories")
    args = ap.parse_args()

    apply = args.apply and not args.dry_run
    mode = "APPLY" if apply else "DRY-RUN"

    # Import memem here so PYTHONPATH is settable via the caller.
    try:
        from memem.obsidian_store import (
            OBSIDIAN_MEMORIES_DIR,
            _find_related,
            _parse_obsidian_memory_file,
            _write_obsidian_memory,
        )
    except ImportError as exc:
        print(f"ERROR: cannot import memem — set PYTHONPATH to the plugin root.\n{exc}")
        return 2

    files = sorted(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
    total = len(files)
    print(f"[{mode}] scanning {total} memory files in {OBSIDIAN_MEMORIES_DIR}")

    skipped_no_parse = 0
    skipped_has_related = 0
    no_matches = 0
    would_link = 0
    wrote = 0
    errors = 0
    start = time.time()

    for idx, path in enumerate(files, 1):
        if idx % args.progress_every == 0:
            elapsed = time.time() - start
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (total - idx) / rate if rate > 0 else 0
            print(f"  [{idx}/{total}] rate={rate:.1f}/s eta={eta:.0f}s "
                  f"skip_linked={skipped_has_related} no_match={no_matches} "
                  f"would_link={would_link} wrote={wrote} errors={errors}")

        mem = _parse_obsidian_memory_file(path)
        if not mem:
            skipped_no_parse += 1
            continue

        # Already has related frontmatter? skip (matches what memem's status counts).
        if mem.get("related"):
            skipped_has_related += 1
            continue
        # Or already has a `## Related` body section? skip.
        raw = path.read_text(errors="replace")
        if "## Related" in raw:
            skipped_has_related += 1
            continue

        try:
            related = _find_related(
                mem.get("essence", ""),
                exclude_id=mem.get("id", ""),
                scope_id=mem.get("project", "default"),
            )
        except Exception as exc:  # noqa: BLE001 — we want to see all failures
            errors += 1
            print(f"  ERROR {path.name}: {exc}")
            continue

        if not related:
            no_matches += 1
            continue

        would_link += 1
        if apply:
            try:
                mem["related"] = related
                _write_obsidian_memory(mem)
                wrote += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  WRITE-ERROR {path.name}: {exc}")

        if args.limit and (wrote if apply else would_link) >= args.limit:
            print(f"  stopping at --limit {args.limit}")
            break

    elapsed = time.time() - start
    print("─" * 60)
    print(f"[{mode}] done in {elapsed:.1f}s")
    print(f"  total files:           {total}")
    print(f"  already linked:        {skipped_has_related}")
    print(f"  unparseable:           {skipped_no_parse}")
    print(f"  no matches found:      {no_matches}")
    print(f"  would-link candidates: {would_link}")
    if apply:
        print(f"  wrote:                 {wrote}")
    print(f"  errors:                {errors}")
    if not apply and would_link:
        print("\n  to apply, re-run with --apply (stop the miner daemon first)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
