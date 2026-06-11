"""Tests for memem.session_blocks.render_working_rules (D3).

Covers:
1. Empty case — no procedural memories → empty string
2. Renders bullets from procedural memories
3. Citation-count ranking: most-cited appears first
4. Tiebreak by created_at descending
5. Cap at limit_chars (truncated at bullet boundary)
6. Invalid memories (invalid_at set) are excluded
7. Non-procedural memories are excluded
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memem.session_blocks import _load_citation_counts, render_working_rules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc_mem(
    id8: str,
    title: str,
    content: str,
    created_at: str = "2026-01-01T00:00:00Z",
    invalid: bool = False,
) -> dict:
    return {
        "id": id8 + "0" * (36 - len(id8)),
        "title": title,
        "essence": content,
        "domain_tags": ["type:procedural"],
        "project": "general",
        "source_type": "mined",
        "importance": 4,
        "created_at": created_at,
        "invalid_at": "2026-01-01T00:00:01Z" if invalid else None,
    }


def _make_non_proc_mem(id8: str, title: str, content: str) -> dict:
    return {
        "id": id8 + "0" * (36 - len(id8)),
        "title": title,
        "essence": content,
        "domain_tags": ["type:episodic"],
        "project": "general",
        "source_type": "mined",
        "importance": 3,
        "created_at": "2026-01-01T00:00:00Z",
        "invalid_at": None,
    }


def _write_recall_log(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Test 1: Empty case — no procedural memories
# ---------------------------------------------------------------------------


def test_render_working_rules_empty_when_no_procedural():
    """render_working_rules returns '' when no procedural memories exist."""
    all_mems = [_make_non_proc_mem("aabbccdd", "Some fact", "The sky is blue.")]
    result = render_working_rules(all_memories=all_mems)
    assert result == "", f"Expected empty string, got: {result!r}"


# ---------------------------------------------------------------------------
# Test 2: Renders bullets from procedural memories
# ---------------------------------------------------------------------------


def test_render_working_rules_renders_bullets(tmp_path):
    """render_working_rules renders '## Working rules' header and bullet lines."""
    mems = [
        _make_proc_mem("aaaa0001", "Quote paths", "Always quote file paths with spaces."),
        _make_proc_mem("aaaa0002", "Never force push", "Never run git push --force on main."),
    ]
    recall_log = tmp_path / ".recall_log.jsonl"
    result = render_working_rules(all_memories=mems, recall_log_path=recall_log)

    assert result.startswith("## Working rules"), f"Expected header, got: {result!r}"
    assert "- Always quote file paths with spaces." in result
    assert "- Never run git push --force on main." in result


# ---------------------------------------------------------------------------
# Test 3: Citation-count ranking
# ---------------------------------------------------------------------------


def test_render_working_rules_ranks_by_citation_count(tmp_path):
    """Memory with more citations appears before memory with fewer citations."""
    now = datetime.now(UTC)

    mem_low_cite = _make_proc_mem(
        "aabb0001", "Low-cited rule", "Always check logs first.",
        created_at="2026-01-01T00:00:00Z",
    )
    mem_high_cite = _make_proc_mem(
        "aabb0002", "High-cited rule", "Never skip tests before committing.",
        created_at="2025-12-01T00:00:00Z",  # older but more cited
    )

    recall_log = tmp_path / ".recall_log.jsonl"
    recent_ts = (now - timedelta(days=1)).isoformat()
    _write_recall_log(recall_log, [
        # 3 citations for high-cite memory
        {"ts": recent_ts, "type": "citation", "session_id": "s1",
         "cited_ids": ["aabb0002"], "source": "mine_delta"},
        {"ts": recent_ts, "type": "citation", "session_id": "s2",
         "cited_ids": ["aabb0002"], "source": "mine_delta"},
        {"ts": recent_ts, "type": "citation", "session_id": "s3",
         "cited_ids": ["aabb0002"], "source": "mine_delta"},
        # 1 citation for low-cite memory
        {"ts": recent_ts, "type": "citation", "session_id": "s4",
         "cited_ids": ["aabb0001"], "source": "mine_delta"},
    ])

    result = render_working_rules(
        all_memories=[mem_low_cite, mem_high_cite],
        recall_log_path=recall_log,
    )

    lines = [ln for ln in result.splitlines() if ln.startswith("- ")]
    assert len(lines) >= 2, f"Expected at least 2 bullet lines, got: {result!r}"
    # High-cited must appear first
    assert "Never skip tests" in lines[0], (
        f"High-cited rule should be first bullet, got: {lines[0]!r}"
    )
    assert "Always check logs" in lines[1], (
        f"Low-cited rule should be second bullet, got: {lines[1]!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: Tiebreak by created_at descending
# ---------------------------------------------------------------------------


def test_render_working_rules_tiebreaks_by_recency(tmp_path):
    """When citation counts are equal, newer memory appears first."""
    recall_log = tmp_path / ".recall_log.jsonl"
    recall_log.write_text("")  # empty log — all citations == 0

    mem_newer = _make_proc_mem(
        "cccc0001", "Newer rule", "Always run ruff before committing.",
        created_at="2026-03-15T00:00:00Z",
    )
    mem_older = _make_proc_mem(
        "cccc0002", "Older rule", "Never edit generated files directly.",
        created_at="2026-01-01T00:00:00Z",
    )

    result = render_working_rules(
        all_memories=[mem_older, mem_newer],  # reversed order
        recall_log_path=recall_log,
    )

    lines = [ln for ln in result.splitlines() if ln.startswith("- ")]
    assert len(lines) >= 2
    # Newer must be first
    assert "Always run ruff" in lines[0], (
        f"Newer rule should be first, got: {lines[0]!r}"
    )
    assert "Never edit generated" in lines[1], (
        f"Older rule should be second, got: {lines[1]!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Cap at limit_chars (truncated at bullet boundary)
# ---------------------------------------------------------------------------


def test_render_working_rules_caps_at_limit_chars(tmp_path):
    """Block is truncated at bullet boundary when limit_chars is exceeded."""
    recall_log = tmp_path / ".recall_log.jsonl"
    recall_log.write_text("")

    # Create many procedural memories with long content
    mems = [
        _make_proc_mem(
            f"dddd{i:04d}",
            f"Rule {i}",
            f"Always do thing number {i} very carefully and explicitly every single time without exception.",
            created_at=f"2026-01-{i+1:02d}T00:00:00Z",
        )
        for i in range(1, 20)
    ]

    result = render_working_rules(
        all_memories=mems,
        recall_log_path=recall_log,
        limit_chars=300,
    )

    assert len(result) <= 300, (
        f"Block must be ≤300 chars, got {len(result)} chars:\n{result}"
    )
    # Must end at a complete bullet (not mid-sentence)
    lines = result.splitlines()
    # Every non-header line should be a complete bullet
    for ln in lines:
        if ln and not ln.startswith("#") and not ln.startswith("- "):
            pytest.fail(f"Unexpected partial line in output: {ln!r}")


# ---------------------------------------------------------------------------
# Test 6: Invalid memories excluded
# ---------------------------------------------------------------------------


def test_render_working_rules_excludes_invalid_memories(tmp_path):
    """Memories with invalid_at set must not appear in the working rules block."""
    recall_log = tmp_path / ".recall_log.jsonl"
    recall_log.write_text("")

    valid_mem = _make_proc_mem("eeee0001", "Valid rule", "Always verify before delete.")
    invalid_mem = _make_proc_mem("eeee0002", "Stale rule", "Never do this old thing.", invalid=True)

    result = render_working_rules(
        all_memories=[valid_mem, invalid_mem],
        recall_log_path=recall_log,
    )

    assert "Always verify before delete" in result
    assert "Never do this old thing" not in result, (
        "Invalid memory must not appear in working rules block"
    )


# ---------------------------------------------------------------------------
# Test 7: Non-procedural memories excluded
# ---------------------------------------------------------------------------


def test_render_working_rules_excludes_non_procedural(tmp_path):
    """Only type:procedural tagged memories appear in the block."""
    recall_log = tmp_path / ".recall_log.jsonl"
    recall_log.write_text("")

    proc_mem = _make_proc_mem("ffff0001", "A rule", "Always check the diff first.")
    non_proc = _make_non_proc_mem("ffff0002", "A fact", "The project uses Python 3.12.")

    result = render_working_rules(
        all_memories=[proc_mem, non_proc],
        recall_log_path=recall_log,
    )

    assert "Always check the diff first" in result
    assert "The project uses Python 3.12" not in result, (
        "Non-procedural memory must not appear in working rules block"
    )


# ---------------------------------------------------------------------------
# Test 8: _load_citation_counts counts correctly
# ---------------------------------------------------------------------------


def test_load_citation_counts_basic(tmp_path):
    """_load_citation_counts returns correct counts from a recall log."""
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    recent_ts = (now - timedelta(days=1)).isoformat()
    old_ts = (now - timedelta(days=60)).isoformat()

    recall_log = tmp_path / ".recall_log.jsonl"
    _write_recall_log(recall_log, [
        # 2 recent citations for id 'aaaa1111'
        {"ts": recent_ts, "type": "citation", "session_id": "s1",
         "cited_ids": ["aaaa1111"], "source": "mine_delta"},
        {"ts": recent_ts, "type": "citation", "session_id": "s2",
         "cited_ids": ["aaaa1111", "bbbb2222"], "source": "mine_delta"},
        # 1 old citation (>30 days) — must not be counted
        {"ts": old_ts, "type": "citation", "session_id": "s3",
         "cited_ids": ["aaaa1111"], "source": "mine_delta"},
        # Non-citation row — must be ignored
        {"ts": recent_ts, "query": "test", "returned_ids": ["aaaa1111"],
         "latency_ms": 50, "source": "tool", "call_type": "search"},
    ])

    counts = _load_citation_counts(recall_log, days=30)

    assert counts.get("aaaa1111") == 2, (
        f"Expected 2 recent citations for aaaa1111, got {counts.get('aaaa1111')}"
    )
    assert counts.get("bbbb2222") == 1, (
        f"Expected 1 citation for bbbb2222, got {counts.get('bbbb2222')}"
    )
    # Old citation must not be counted
    # (aaaa1111 count is already 2 from the two recent rows — old row would have made it 3)
    assert counts.get("aaaa1111", 0) == 2


# ---------------------------------------------------------------------------
# Test 9: session-start.sh structural check for Working rules block
# ---------------------------------------------------------------------------


def test_session_start_contains_working_rules_block():
    """session-start.sh must assemble the working rules block.

    As of v2.8 (D5), the hook delegates to render_session_start() which
    internally uses render_working_rules(). The hook itself imports from
    session_blocks and references 'Working rules' in its assembly comment.
    """
    repo_root = Path(__file__).resolve().parent.parent
    content = (repo_root / "hooks" / "session-start.sh").read_text()

    assert "session_blocks" in content, (
        "session-start.sh must import from memem.session_blocks"
    )
    # render_session_start composes the working rules block internally;
    # the hook references it directly or via render_session_start.
    assert "render_session_start" in content or "render_working_rules" in content, (
        "session-start.sh must call render_session_start or render_working_rules"
    )
    assert "Working rules" in content or "render_session_start" in content, (
        "session-start.sh must reference the working rules block (directly or via render_session_start)"
    )
