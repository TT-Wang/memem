#!/usr/bin/env python3
"""Backfill bi-temporal fields on every memory in the vault.

Phase 1 of memem v2 ("closed-loop relevance"). Adds valid_at/invalid_at/replaced_by
to existing memories that pre-date the schema change. Idempotent: skips memories
already migrated.

Usage:
    python3 scripts/migrate_bitemporal.py            # dry-run
    python3 scripts/migrate_bitemporal.py --apply    # write changes

Behavior:
- valid_at <- created_at if absent
- invalid_at <- None if absent (written as omitted from frontmatter — None means valid)
- replaced_by <- None if absent (written as omitted from frontmatter)
- skips memories that already have valid_at set
"""

from __future__ import annotations

import sys
from pathlib import Path

# Portable path: project root is two levels above this script
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def main() -> None:
    apply = "--apply" in sys.argv

    # Import after sys.path is set up
    import frontmatter as _fm  # type: ignore[import-untyped]

    from memem.models import OBSIDIAN_MEMORIES_DIR

    if not OBSIDIAN_MEMORIES_DIR.exists():
        print(f"Memories directory not found: {OBSIDIAN_MEMORIES_DIR}")
        sys.exit(1)

    all_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
    total = len(all_files)
    skipped = 0
    migrated = 0
    errors = 0

    print(f"{'[DRY RUN] ' if not apply else ''}Scanning {total} memory files in {OBSIDIAN_MEMORIES_DIR}")

    for md_file in sorted(all_files):
        try:
            content = md_file.read_text(errors="ignore")
            post = _fm.loads(content)
            fm_meta = post.metadata

            # Check if already migrated (has valid_at set)
            if fm_meta.get("valid_at"):
                skipped += 1
                continue

            # Determine valid_at from created_at
            created = fm_meta.get("created", "")
            valid_at = created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else "")

            if not valid_at:
                # Fall back to file mtime as last resort
                import datetime as _dt
                mtime = md_file.stat().st_mtime
                valid_at = _dt.datetime.fromtimestamp(mtime, tz=_dt.UTC).isoformat()

            if apply:
                # Write back with new fields — only valid_at; invalid_at and
                # replaced_by are absent (omitted means None/valid).
                post.metadata["valid_at"] = valid_at
                # Write atomically
                import os
                import tempfile
                new_content = _fm.dumps(post)
                fd, tmp = tempfile.mkstemp(dir=str(md_file.parent), prefix=".tmp_", suffix=".md")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    os.replace(tmp, str(md_file))
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                migrated += 1
            else:
                print(f"  would set valid_at={valid_at!r} on {md_file.name}")
                migrated += 1

        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR processing {md_file.name}: {exc}", file=sys.stderr)
            errors += 1

    mode = "Applied" if apply else "Would migrate"
    print(
        f"\nDone. {mode}: {migrated}, already migrated (skipped): {skipped}, errors: {errors}, total: {total}"
    )
    if not apply and migrated > 0:
        print("\nRe-run with --apply to write changes.")


if __name__ == "__main__":
    main()
