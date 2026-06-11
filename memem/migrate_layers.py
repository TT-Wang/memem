"""Layer migration tooling — build initial profiles from legacy L0/L1 memories.

CLI entry point: python3 -m memem.server --migrate-layers [--report PATH] [--apply]  (--yes accepted as deprecated no-op)
                                                           [--exclude id8,...]

Lifecycle
---------
DRY-RUN (default):
  collect_candidates() → propose() → write_report() + write proposals JSON
  Prints a summary and tells the user to review the report, then rerun with --apply.

APPLY:
  Loads the proposals JSON written by the dry-run (if present and fresh).
  Calls profiles.append_fact() for every PROFILE item.
  Returns a summary dict {appended, duplicate, compacted+appended, rejected_full, error}.

Memories are NEVER deleted — profiles are additive views only.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import structlog

from memem.haiku_prompts import HAIKU_TIMEOUT_SECONDS
from memem.mining import _extract_json_string, _repair_json
from memem.profiles import PROFILE_SCHEMAS, append_fact

log = structlog.get_logger("memem-migrate-layers")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BATCH_SIZE = 20
_PROPOSALS_PATH = Path("docs/migrate-layers-proposals.json")
_REPORT_PATH = Path("docs/migrate-layers-report.md")
_PROPOSALS_MAX_AGE_DAYS = 7

# Valid sections per profile type (flattened for quick lookup)
_VALID_SECTIONS_USER = set(PROFILE_SCHEMAS["user"])
_VALID_SECTIONS_PROJECT = set(PROFILE_SCHEMAS["_project"])
_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")

_MIGRATE_SYSTEM = (
    "You are a memory profiler for an AI memory system.\n\n"
    "You receive a list of memories (from a legacy knowledge vault). For each memory, "
    "decide whether it represents a STABLE fact that belongs in an always-on profile "
    "(PROFILE) or whether it should remain as a regular memory (KEEP).\n\n"
    "PROFILE: stable identity/stack/convention/preference facts. These are things "
    "that an AI assistant should ALWAYS know for every future session. "
    "Examples: user's preferred language, project stack, coding conventions.\n\n"
    "KEEP: narratives, lessons, one-off debugging sessions, episodic events, "
    "anything that is useful on-demand but not always-relevant.\n\n"
    "Output a strict JSON array with one object per input memory (same order). "
    "Each object:\n"
    '{"id": "<8-char-id>", "action": "PROFILE"|"KEEP", '
    '"profile": "user"|"project"|null, '
    '"section": "<section name>"|null, '
    '"line": "<distilled fact ≤200 chars>"|null}\n\n'
    "Valid sections for profile='user': Preferences, Conventions, Environment\n"
    "Valid sections for profile='project': Identity, Stack & Structure, Conventions\n\n"
    "Rules:\n"
    "- For PROFILE: set profile, section, and line. target and content must be null.\n"
    "- For KEEP: action='KEEP', profile=null, section=null, line=null.\n"
    "- line must be ≤200 chars for PROFILE items.\n"
    "- Output ONLY the JSON array, no other text.\n"
    "- When in doubt, prefer KEEP — profiles are for permanently-relevant facts only."
)


# ---------------------------------------------------------------------------
# Step 1: collect_candidates
# ---------------------------------------------------------------------------

def collect_candidates(exclude_ids: set[str] | None = None) -> list[dict]:
    """Return active memories whose layer field is 0 or 1.

    Memories with no layer field (the new default after v2.8.0) are NOT
    candidates. Deprecated memories are excluded.
    """
    from memem.obsidian_store import _obsidian_memories

    exclude = exclude_ids or set()
    candidates = []
    for mem in _obsidian_memories(include_deprecated=False):
        layer = mem.get("layer")
        if layer not in (0, 1):
            continue
        mem_id8 = (mem.get("id") or "")[:8]
        if mem_id8 in exclude:
            continue
        candidates.append(mem)
    return candidates


# ---------------------------------------------------------------------------
# Step 2: propose
# ---------------------------------------------------------------------------

def propose(
    candidates: list[dict],
    batch_size: int = _BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Call Haiku in batches grouped by project, returning one proposal per candidate.

    Each proposal dict has the candidate memory plus the Haiku decision fields:
      action: "PROFILE" | "KEEP"
      profile: "user" | "project" | None
      section: str | None
      line: str | None
      note: str (optional, set on failure)
    """
    if not candidates:
        return []

    # Group by project
    by_project: dict[str, list[dict]] = {}
    for mem in candidates:
        project = mem.get("project") or "general"
        by_project.setdefault(project, []).append(mem)

    proposals: list[dict] = []

    for project, mems in by_project.items():
        # Split into batches of batch_size
        n_batches = math.ceil(len(mems) / batch_size)
        for batch_idx in range(n_batches):
            batch = mems[batch_idx * batch_size : (batch_idx + 1) * batch_size]
            batch_proposals = _call_haiku_batch(batch, project)
            proposals.extend(batch_proposals)

    return proposals


def _call_haiku_batch(batch: list[dict], project: str) -> list[dict[str, Any]]:
    """Call Haiku for one batch. Returns per-memory proposals."""
    # Build input envelope
    lines = []
    for mem in batch:
        mem_id8 = (mem.get("id") or "")[:8]
        title = mem.get("title") or "Untitled"
        essence = (mem.get("essence") or mem.get("full_record") or "")[:300]
        lines.append(f"{mem_id8}|{title}|{essence}")
    prompt = "\n".join(lines)

    # All-KEEP fallback (used on Haiku failure)
    def _all_keep(note: str) -> list[dict]:
        result = []
        for mem in batch:
            p = dict(mem)
            p["action"] = "KEEP"
            p["profile"] = None
            p["section"] = None
            p["line"] = None
            p["note"] = note
            result.append(p)
        return result

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                "haiku",
                "--tools",
                "",
                "--system-prompt",
                _MIGRATE_SYSTEM,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT_SECONDS,
            env={**os.environ, "MEMEM_HOOK_DISABLE": "1"},
            start_new_session=True,
        )
    except Exception as exc:
        log.warning("Haiku migration batch error", project=project, exc=str(exc))
        return _all_keep(f"haiku-failed: {exc}")

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        log.warning("Haiku migration non-zero", project=project, detail=detail)
        return _all_keep(f"haiku-failed: {detail}")

    raw_out = result.stdout.strip()
    if not raw_out:
        return _all_keep("haiku-failed: empty output")

    # Parse JSON
    json_str = _extract_json_string(raw_out)
    if json_str is None:
        json_str = _repair_json(raw_out)
    try:
        parsed = json.loads(json_str)  # type: ignore[arg-type]
    except Exception:
        return _all_keep("haiku-failed: json parse error")

    if not isinstance(parsed, list):
        return _all_keep("haiku-failed: expected JSON array")

    # Build id→result map
    id_to_haiku: dict[str, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item_id = (item.get("id") or "")[:8]
        if item_id:
            id_to_haiku[item_id] = item

    # Validate and build proposals
    proposals = []
    for mem in batch:
        mem_id8 = (mem.get("id") or "")[:8]
        haiku_item = id_to_haiku.get(mem_id8)
        p = dict(mem)

        if haiku_item is None:
            p["action"] = "KEEP"
            p["profile"] = None
            p["section"] = None
            p["line"] = None
            p["note"] = "haiku-no-result"
        else:
            action = haiku_item.get("action", "KEEP")
            if action not in ("PROFILE", "KEEP"):
                action = "KEEP"

            profile_name = haiku_item.get("profile")
            section = haiku_item.get("section")
            line = haiku_item.get("line")

            # Validate section for PROFILE items. Canonicalize common LLM
            # spelling variants first ('Stack and Structure' → 'Stack &
            # Structure') — same tolerance as mine_delta's PROFILE handler,
            # so the migration path and the live miner accept identical
            # Haiku paraphrases.
            if action == "PROFILE":
                valid_sections = (
                    _VALID_SECTIONS_USER
                    if profile_name == "user"
                    else _VALID_SECTIONS_PROJECT
                )
                if isinstance(section, str):
                    _norm = section.lower().strip().replace(" and ", " & ")
                    for _vs in valid_sections:
                        if _vs.lower() == _norm:
                            section = _vs
                            break
                if section not in valid_sections:
                    # Unknown section → KEEP
                    action = "KEEP"
                    profile_name = None
                    section = None
                    line = None
                    p["note"] = f"unknown-section: {haiku_item.get('section')!r}"

            p["action"] = action
            p["profile"] = profile_name
            p["section"] = section
            p["line"] = line

        proposals.append(p)

    return proposals


# ---------------------------------------------------------------------------
# Step 3: write_report
# ---------------------------------------------------------------------------

def write_report(proposals: list[dict], path: Path | None = None) -> Path:
    """Write a human-readable markdown report of migration proposals.

    Returns the path written.
    """
    if path is None:
        path = _resolve_report_path()

    # Summarize
    total = len(proposals)
    profile_items = [p for p in proposals if p.get("action") == "PROFILE"]
    keep_items = [p for p in proposals if p.get("action") == "KEEP"]
    failures = [p for p in keep_items if "haiku-failed" in (p.get("note") or "")]

    # Count by profile/section
    by_profile_section: dict[str, dict[str, int]] = {}
    for p in profile_items:
        pname = p.get("profile") or "?"
        sec = p.get("section") or "?"
        by_profile_section.setdefault(pname, {}).setdefault(sec, 0)
        by_profile_section[pname][sec] += 1

    lines: list[str] = [
        "# Layer Migration Report",
        "",
        "## Summary",
        "",
        f"- **Total candidates** (layer 0/1): {total}",
        f"- **PROFILE proposals**: {len(profile_items)}",
    ]
    for pname, sec_counts in sorted(by_profile_section.items()):
        for sec, cnt in sorted(sec_counts.items()):
            lines.append(f"  - profile_{pname} / {sec}: {cnt}")
    lines += [
        f"- **KEEP** (stay as memories): {len(keep_items)}",
        f"- **Haiku failures** (all→KEEP): {len(failures)}",
        "",
    ]

    # Group PROFILE proposals by project
    by_project_profile: dict[str, list[dict]] = {}
    for p in profile_items:
        proj = p.get("project") or "general"
        by_project_profile.setdefault(proj, []).append(p)

    if by_project_profile:
        lines.append("## PROFILE Proposals")
        lines.append("")
        for proj in sorted(by_project_profile):
            lines.append(f"### Project: {proj}")
            lines.append("")
            for p in by_project_profile[proj]:
                mid8 = (p.get("id") or "")[:8]
                title = p.get("title") or "Untitled"
                pname = p.get("profile") or "?"
                sec = p.get("section") or "?"
                fact = p.get("line") or ""
                lines.append(f'- [{mid8}] {title} → profile_{pname} / {sec}: "{fact}"')
            lines.append("")

    # KEEP section — first 10 + count
    if keep_items:
        lines.append("## KEEP (stay as memories)")
        lines.append("")
        lines.append(f"Total: {len(keep_items)}")
        lines.append("")
        shown = keep_items[:10]
        for p in shown:
            mid8 = (p.get("id") or "")[:8]
            title = p.get("title") or "Untitled"
            note = p.get("note") or ""
            note_str = f" ({note})" if note else ""
            lines.append(f"- [{mid8}] {title}{note_str}")
        if len(keep_items) > 10:
            lines.append(f"- ... and {len(keep_items) - 10} more")
        lines.append("")

    content = "\n".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("migration report written", path=str(path))
    return path


# ---------------------------------------------------------------------------
# Step 4: apply
# ---------------------------------------------------------------------------

def apply(proposals: list[dict]) -> dict[str, int]:
    """Apply PROFILE proposals by calling profiles.append_fact.

    Returns a summary dict with counts: appended, duplicate,
    compacted+appended, rejected_full, error, kept.
    Idempotent by construction (append_fact deduplicates).
    """
    from memem.telemetry import _log_event

    summary: dict[str, int] = {
        "appended": 0,
        "duplicate": 0,
        "compacted+appended": 0,
        "rejected_full": 0,
        "error": 0,
        "kept": 0,
    }

    for p in proposals:
        action = p.get("action")
        if action != "PROFILE":
            summary["kept"] += 1
            continue

        profile_name = p.get("profile")
        section = p.get("section")
        line = p.get("line")
        mem_id8 = (p.get("id") or "")[:8]

        if not profile_name or not section or not line:
            log.warning("migrate apply: incomplete PROFILE proposal", id=mem_id8)
            summary["error"] += 1
            continue

        # For project profile, use the memory's normalized project name.
        # 'general'-project memories route to the USER profile instead:
        # render_for_injection never loads a 'general' project profile, so a
        # profile_general.md would be a silently-never-injected dead end —
        # cross-cutting conventions belong in the user profile's Conventions.
        if profile_name == "project":
            from memem.models import _normalize_scope_id
            project = p.get("project") or "general"
            normalized = _normalize_scope_id(project) or project
            if normalized == "general":
                profile_name = "user"
                section = "Conventions"
            else:
                profile_name = normalized

        status = append_fact(profile_name, section, line)
        if status in summary:
            summary[status] += 1
        else:
            summary["error"] += 1

        _log_event(
            "migrate_profile",
            mem_id8,
            profile=profile_name,
            section=section,
            status=status,
        )

    return summary


# ---------------------------------------------------------------------------
# Proposals JSON helpers
# ---------------------------------------------------------------------------

def _resolve_report_path(base: Path | None = None) -> Path:
    """Resolve the report path. Defaults to MEMEM_DIR (alongside other memem
    state) — a CWD-relative docs/ default would silently scatter reports into
    whatever directory the user happened to run the CLI from."""
    if base is not None:
        return base
    from memem.models import MEMEM_DIR
    return MEMEM_DIR / "migrate-layers-report.md"


def _resolve_proposals_json_path(report_path: Path) -> Path:
    """Proposals JSON lives next to the report with a .json extension."""
    return report_path.parent / "migrate-layers-proposals.json"


def _write_proposals_json(proposals: list[dict], path: Path) -> None:
    """Write machine-readable proposals JSON.

    Only serializes fields needed for apply (strips heavy 'full_record', etc.).
    """
    slim: list[dict] = []
    for p in proposals:
        slim.append({
            "id": p.get("id") or "",
            "title": p.get("title") or "",
            "project": p.get("project") or "general",
            "action": p.get("action", "KEEP"),
            "profile": p.get("profile"),
            "section": p.get("section"),
            "line": p.get("line"),
            "note": p.get("note"),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_proposals_json(path: Path) -> list[dict] | None:
    """Load proposals JSON; returns None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("migrate: failed to load proposals JSON", path=str(path), exc=str(exc))
        return None


def _proposals_json_is_fresh(path: Path, candidates: list[dict]) -> tuple[bool, str]:
    """Check freshness: age < 7 days and same candidate count.

    Returns (is_fresh, warning_message).
    """
    import time

    try:
        age_seconds = time.time() - path.stat().st_mtime
        age_days = age_seconds / 86400
    except OSError:
        return False, "proposals file not found"

    if age_days > _PROPOSALS_MAX_AGE_DAYS:
        return False, f"proposals file is {age_days:.1f} days old (max {_PROPOSALS_MAX_AGE_DAYS})"

    loaded = _load_proposals_json(path)
    if loaded is None:
        return False, "proposals file could not be parsed"

    vault_count = len(candidates)
    file_count = len(loaded)
    if file_count != vault_count:
        return (
            False,
            f"candidate count changed ({vault_count} now vs {file_count} in file)",
        )

    return True, ""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_migration(
    apply_mode: bool = False,
    yes: bool = False,
    report_path: Path | None = None,
    exclude_ids: set[str] | None = None,
) -> int:
    """Main entry point for --migrate-layers.

    Returns an exit code (0 = success, 1 = error/refused).
    """
    # --apply alone is sufficient: the dry-run report review IS the human
    # gate (every doc surface instructs dry-run → review → --apply, and the
    # apply is additive + idempotent). --yes is accepted as a no-op alias for
    # callers who pass it. (Phase 4.5 fix: requiring an undocumented --yes
    # made every documented apply invocation exit 1.)
    _ = yes  # accepted for compatibility; no additional gate

    rpt_path = _resolve_report_path(report_path)
    json_path = _resolve_proposals_json_path(rpt_path)

    if apply_mode:
        # Collect fresh candidates to check freshness
        candidates = collect_candidates(exclude_ids=exclude_ids)

        if json_path.exists():
            fresh, warn = _proposals_json_is_fresh(json_path, candidates)
            if not fresh:
                import sys
                print(f"Warning: {warn}. Re-running proposal step.", file=sys.stderr)
                proposals = propose(candidates)
                _write_proposals_json(proposals, json_path)
                write_report(proposals, rpt_path)
            else:
                loaded = _load_proposals_json(json_path)
                proposals = loaded if loaded is not None else propose(candidates)
        else:
            print("No proposals file found — running proposal step first.")
            proposals = propose(candidates)
            _write_proposals_json(proposals, json_path)
            write_report(proposals, rpt_path)

        print(f"Applying {len([p for p in proposals if p.get('action') == 'PROFILE'])} PROFILE proposals...")
        summary = apply(proposals)
        print(
            f"Migration complete: "
            f"appended={summary.get('appended', 0)}, "
            f"duplicate={summary.get('duplicate', 0)}, "
            f"compacted+appended={summary.get('compacted+appended', 0)}, "
            f"rejected_full={summary.get('rejected_full', 0)}, "
            f"error={summary.get('error', 0)}, "
            f"kept={summary.get('kept', 0)}"
        )
        return 0

    # Dry-run
    print("Collecting layer 0/1 candidates...")
    candidates = collect_candidates(exclude_ids=exclude_ids)
    print(f"Found {len(candidates)} candidates.")

    if not candidates:
        print("No layer 0/1 memories found. Nothing to migrate.")
        return 0

    print(f"Proposing migrations (batch_size={_BATCH_SIZE})...")
    proposals = propose(candidates)

    write_report(proposals, rpt_path)
    _write_proposals_json(proposals, json_path)

    n_profile = sum(1 for p in proposals if p.get("action") == "PROFILE")
    n_keep = sum(1 for p in proposals if p.get("action") == "KEEP")
    print(
        f"Dry-run complete: {len(candidates)} candidates, "
        f"{n_profile} PROFILE, {n_keep} KEEP."
    )
    print(f"Report: {rpt_path}")
    print(f"Proposals JSON: {json_path}")
    print("Review the report, then run with --apply to execute.")
    return 0
