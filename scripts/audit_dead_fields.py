#!/usr/bin/env python3
"""
Audit and clean dead schema fields from vault frontmatter.

Dead fields: associations, impact_score, success_count, human_approved,
             scope_type, verified, last_validated_at, tier, full_record,
             access_count, last_accessed

Walks /home/claude-user/obsidian-brain/memem/memories/*.md, parses each
frontmatter block, reports any file that has ANY of the dead fields, and
atomically rewrites those files with the dead fields removed.

Outputs a report at:
  /home/claude-user/cortex-plugin/.forge/state/memem-vault-cleanup-dead-fields-audit.json
"""

import json
import os
import tempfile
from pathlib import Path

VAULT_DIR = Path("/home/claude-user/obsidian-brain/memem/memories")
REPORT_PATH = Path(
    "/home/claude-user/cortex-plugin/.forge/state/"
    "memem-vault-cleanup-dead-fields-audit.json"
)

DEAD_FIELDS = {
    "associations",
    "impact_score",
    "success_count",
    "human_approved",
    "scope_type",
    "verified",
    "last_validated_at",
    "tier",
    "full_record",
    "access_count",
    "last_accessed",
}


def parse_and_clean_file(path: Path) -> tuple[bool, int, str]:
    """
    Parse a vault .md file and remove any dead frontmatter fields.

    Returns (has_dead_fields, field_count_removed, cleaned_content).
    """
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)

    # Must start with ---
    if not lines or lines[0].rstrip() != "---":
        return False, 0, raw

    # Find closing ---
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            end_idx = i
            break

    if end_idx is None:
        # Malformed frontmatter — skip
        return False, 0, raw

    frontmatter_lines = lines[1:end_idx]
    body_lines = lines[end_idx:]  # includes closing --- and everything after

    # Filter out dead field lines (key: value at top-level yaml)
    dead_count = 0
    cleaned_fm = []
    for line in frontmatter_lines:
        # Check if this line is a top-level key matching a dead field
        stripped = line.lstrip()
        if ":" in stripped:
            key = stripped.split(":")[0].strip()
            if key in DEAD_FIELDS:
                dead_count += 1
                continue  # drop this line
        cleaned_fm.append(line)

    if dead_count == 0:
        return False, 0, raw

    cleaned_content = "---\n" + "".join(cleaned_fm) + "".join(body_lines)
    return True, dead_count, cleaned_content


def rewrite_atomic(path: Path, content: str) -> None:
    """Atomically rewrite file using tmp+replace in same directory."""
    parent = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".audit_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up tmp on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> None:
    md_files = sorted(VAULT_DIR.glob("*.md"))
    files_scanned = len(md_files)
    files_with_dead_fields = 0
    files_modified = 0
    dead_field_occurrences = 0

    print(f"Scanning {files_scanned} files in {VAULT_DIR}...")

    for path in md_files:
        try:
            has_dead, count, cleaned = parse_and_clean_file(path)
        except Exception as exc:
            print(f"  ERROR reading {path.name}: {exc}")
            continue

        if has_dead:
            files_with_dead_fields += 1
            dead_field_occurrences += count
            print(f"  FOUND {count} dead field(s) in {path.name} — rewriting...")
            try:
                rewrite_atomic(path, cleaned)
                files_modified += 1
            except Exception as exc:
                print(f"  ERROR rewriting {path.name}: {exc}")

    report = {
        "files_scanned": files_scanned,
        "files_with_dead_fields": files_with_dead_fields,
        "files_modified": files_modified,
        "dead_field_occurrences": dead_field_occurrences,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print("=== Dead Fields Audit Report ===")
    print(f"  files_scanned:          {files_scanned}")
    print(f"  files_with_dead_fields: {files_with_dead_fields}")
    print(f"  files_modified:         {files_modified}")
    print(f"  dead_field_occurrences: {dead_field_occurrences}")
    print(f"  Report written to: {REPORT_PATH}")

    if files_modified == 0:
        print("RESULT: Vault is clean — zero dead fields found.")
    else:
        print(f"RESULT: Removed dead fields from {files_modified} file(s).")


if __name__ == "__main__":
    main()
