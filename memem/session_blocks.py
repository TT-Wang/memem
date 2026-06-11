"""Session-start context block renderers (v2.8).

Houses render_working_rules() and render_session_start() so session-start.sh
can import them via the existing python heredoc pattern, and tests can exercise
the logic directly without running the full hook.

render_session_start() composes three independently-budgeted blocks:
  1. Profiles block (user + project profile, ~2400 chars combined)
  2. ## Working rules block (~1200 chars)
  3. ## Episode index block (~1600 chars, capped at 25 entries)

Total target ≤ 5200 chars (~1.3k tokens). Uses a SINGLE _obsidian_memories()
call shared across blocks 2 and 3 (avoids double vault load).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def _load_citation_counts(recall_log_path: Path, days: int = 30) -> dict[str, int]:
    """Read last 64KB of recall_log.jsonl and count citation hits per id8.

    Returns a dict mapping 8-char memory id prefix → citation count.
    Only 'citation' row type entries within the last *days* are counted.
    Tolerant of all read/parse errors.
    """
    counts: dict[str, int] = {}
    if not recall_log_path.exists():
        return counts

    _TAIL_BYTES = 64 * 1024
    try:
        with recall_log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
        raw_lines = chunk.splitlines()
        if size > _TAIL_BYTES and raw_lines:
            raw_lines = raw_lines[1:]  # drop possibly-partial first line
    except OSError:
        return counts

    cutoff_ts = None
    if days > 0:
        now = datetime.now(UTC)
        # Build cutoff as ISO prefix — simplest comparison
        from datetime import timedelta
        cutoff_dt = now - timedelta(days=days)
        cutoff_ts = cutoff_dt.isoformat()

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "citation":
            continue
        if cutoff_ts:
            row_ts = row.get("ts", "")
            if row_ts and row_ts < cutoff_ts:
                continue
        for cid in row.get("cited_ids") or []:
            id8 = str(cid)[:8]
            counts[id8] = counts.get(id8, 0) + 1

    return counts


def render_working_rules(
    all_memories: list[dict] | None = None,
    recall_log_path: Path | None = None,
    limit_chars: int = 1200,
) -> str:
    """Render a '## Working rules' block from procedural memories.

    Retrieves memories with 'type:procedural' in their domain_tags, ranks
    them by citation count (last 30 days from recall_log.jsonl) then by
    created_at descending as a tiebreaker, renders as imperative bullet
    points, caps at *limit_chars* (truncated at bullet boundary).

    Returns an empty string when no procedural memories exist or on any
    error (never raises).

    Args:
        all_memories: Pre-loaded list of memory dicts (for testing). When
            None, loaded from obsidian_store._obsidian_memories().
        recall_log_path: Path to .recall_log.jsonl. When None, defaults
            to MEMEM_DIR / '.recall_log.jsonl'.
        limit_chars: Maximum character budget for the rendered block
            (~300 tokens). Truncation happens at bullet boundary.
    """
    try:
        # Load memories if not provided
        if all_memories is None:
            try:
                from memem.obsidian_store import _obsidian_memories
                all_memories = _obsidian_memories()
            except Exception:  # noqa: BLE001
                return ""

        # Filter to procedural memories only
        procedural = [
            m for m in all_memories
            if "type:procedural" in (m.get("domain_tags") or [])
            and not m.get("invalid_at")
        ]
        if not procedural:
            return ""

        # Resolve recall log path
        if recall_log_path is None:
            try:
                from memem.models import MEMEM_DIR as _MEMEM_DIR
                recall_log_path = Path(_MEMEM_DIR) / ".recall_log.jsonl"
            except Exception:  # noqa: BLE001
                env_dir = os.environ.get("MEMEM_DIR") or os.path.expanduser("~/.memem")
                recall_log_path = Path(env_dir) / ".recall_log.jsonl"

        # Load citation counts
        cite_counts = _load_citation_counts(recall_log_path, days=30)

        # Sort descending by citation count, then descending by created_at.
        # For created_at: ISO dates are lexicographically comparable so we negate
        # the sort with a reverse tuple trick — sort ascending by (-count, -created)
        # where -created uses the fact that we flip the string for descending order.
        # Easiest: use a negative multiplier on citations (int) and for the string
        # tiebreak, sort a second time (stable) or use the string comparison trick
        # where we compare with a reversed character trick. Simplest stable solution:
        # sort by (citation_count DESC, created_at DESC) using two separate criteria
        # applied via reverse-flag-aware negative key.
        #
        # Since Python sorts are stable, we can do two passes:
        #   pass 1: sort by created_at descending
        #   pass 2: stable sort by citation count descending
        # This yields: primary=citation DESC, tiebreak=created DESC.
        procedural_sorted = sorted(
            procedural,
            key=lambda m: m.get("created_at") or m.get("created") or "",
            reverse=True,  # newest first (tiebreak)
        )
        procedural_sorted = sorted(
            procedural_sorted,
            key=lambda m: -cite_counts.get((m.get("id") or "")[:8], 0),
        )  # most cited first (primary); stable sort preserves created_at order within ties

        # Render bullets — first line of essence/content as the rule text
        lines: list[str] = ["## Working rules"]
        used_chars = len("## Working rules\n")

        for m in procedural_sorted:
            # Extract first meaningful line from essence or content
            raw = (m.get("essence") or m.get("content") or m.get("full_record") or "").strip()
            if not raw:
                continue
            # Use first non-empty line; truncate to 100 chars
            first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "").strip()
            if not first_line:
                continue
            # Trim markdown headers from first line (## Section)
            if first_line.startswith("#"):
                # Try next non-empty line
                candidates = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
                first_line = candidates[0] if candidates else first_line
            first_line = first_line[:100]
            bullet = f"- {first_line}\n"
            if used_chars + len(bullet) > limit_chars:
                break
            lines.append(f"- {first_line}")
            used_chars += len(bullet)

        if len(lines) <= 1:
            # Only the header, no bullets — don't emit the block
            return ""

        return "\n".join(lines)

    except Exception:  # noqa: BLE001 — never break SessionStart
        return ""


def render_episode_catalog(
    all_memories: list[dict] | None = None,
    limit_chars: int = 1600,
    max_entries: int = 25,
) -> str:
    """Render '## Episode index' block from episodic memories.

    Filters to type:episodic, sorted by created_at DESC, capped at max_entries.
    Total block truncated at bullet boundary to limit_chars.
    Returns '' when no episodic memories exist or on any error.

    Args:
        all_memories: Pre-loaded list of memory dicts (for testing). When None,
            loaded from obsidian_store._obsidian_memories().
        limit_chars: Maximum character budget for the rendered block.
        max_entries: Maximum number of episode bullets to include.
    """
    try:
        if all_memories is None:
            try:
                from memem.obsidian_store import _obsidian_memories
                all_memories = _obsidian_memories()
            except Exception:  # noqa: BLE001
                return ""

        episodic = sorted(
            [m for m in all_memories if "type:episodic" in (m.get("domain_tags") or [])],
            key=lambda m: m.get("created_at") or m.get("created") or "",
            reverse=True,
        )[:max_entries]

        if not episodic:
            return ""

        cat_lines: list[str] = ["## Episode index"]
        used_chars = len("## Episode index\n")

        for m in episodic:
            mid = (m.get("id") or "")[:8]
            date = (m.get("created_at") or m.get("created") or "")[:10] or "----"
            title = (m.get("title") or "(untitled)")[:80]
            bullet = f"- {mid} [{date}]: {title}\n"
            if used_chars + len(bullet) > limit_chars:
                break
            cat_lines.append(f"- {mid} [{date}]: {title}")
            used_chars += len(bullet)

        if len(cat_lines) <= 1:
            return ""

        return "\n".join(cat_lines)

    except Exception:  # noqa: BLE001 — never break SessionStart
        return ""


def render_session_start(scope_id: str) -> str:
    """Compose all three budgeted blocks for SessionStart context injection.

    Blocks (in order):
      1. Profiles block — user + project profile via profiles.render_for_injection().
         Budget: ~2400 chars (~600 tokens) enforced inside render_for_injection.
      2. ## Working rules — procedural memories ranked by citation count.
         Budget: 1200 chars (~300 tokens).
      3. ## Episode index — recent episodic memories, capped at 25.
         Budget: 1600 chars (~400 tokens).

    Total target ≤ 5200 chars (~1.3k tokens). Uses a SINGLE _obsidian_memories()
    call shared across blocks 2 and 3.

    Every block is individually try/except'd. One failing block never kills others.
    Returns '' when all blocks are empty (so hook can emit_empty).
    Never raises.

    Args:
        scope_id: Raw scope string (e.g. 'cortex-plugin', 'default'). Normalized
            internally via models._normalize_scope_id inside render_for_injection.
    """
    parts: list[str] = []

    # --- Block 1: Profiles (user + project profile) ---
    try:
        from memem.profiles import render_for_injection
        profiles_block = render_for_injection(scope_id)
        if profiles_block and profiles_block.strip():
            parts.append(profiles_block.strip())
    except Exception:  # noqa: BLE001 — never break SessionStart
        pass

    # --- Single vault load shared by blocks 2 and 3 ---
    all_mems: list[dict] | None = None
    try:
        from memem.obsidian_store import _obsidian_memories
        all_mems = _obsidian_memories()
    except Exception:  # noqa: BLE001 — blocks will fall back to their own load
        pass

    # --- Block 2: Working rules ---
    try:
        rules_block = render_working_rules(all_memories=all_mems)
        if rules_block:
            parts.append(rules_block)
    except Exception:  # noqa: BLE001
        pass

    # --- Block 3: Episode index ---
    try:
        episodes_block = render_episode_catalog(all_memories=all_mems)
        if episodes_block:
            parts.append(episodes_block)
    except Exception:  # noqa: BLE001
        pass

    if not parts:
        return ""

    content = "\n\n".join(parts)

    # Total budget guard: if combined content exceeds 5500 chars, truncate
    # the episode index block (the last block) at a bullet boundary to fit.
    _TOTAL_BUDGET = 5500
    if len(content) > _TOTAL_BUDGET:
        # Rebuild without the episode block, then re-add a truncated version
        non_episode_parts = [p for p in parts if not p.startswith("## Episode index")]
        non_episode_content = "\n\n".join(non_episode_parts)
        remaining = _TOTAL_BUDGET - len(non_episode_content) - 2  # -2 for "\n\n"
        if remaining > 50:
            # Find and re-render episodes block within remaining budget
            try:
                ep_block = render_episode_catalog(
                    all_memories=all_mems,
                    limit_chars=remaining,
                )
                if ep_block:
                    content = non_episode_content + "\n\n" + ep_block
                else:
                    content = non_episode_content
            except Exception:  # noqa: BLE001
                content = non_episode_content
        else:
            content = non_episode_content

    return content
