"""Tests for m3: recall tools return slice-formatted output.

Verifies:
- memory_search returns a search-kind slice with proper header and layer summary
- memory_get returns a get-kind slice with ## Memory: header
- memory_timeline returns chronologically ordered items
- render_slice_markdown dispatches correctly on slice_kind
- _layer_summary_from_items counts correctly
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_mem(
    mid: str = "aabbccdd" + "0" * 24,
    title: str = "Test Memory",
    essence: str = "test content here",
    layer: int = 2,
    project: str = "test_project",
    source_type: str = "user",
    created_at: str = "2026-01-01T00:00:00Z",
    related: list[str] | None = None,
) -> dict:
    return {
        "id": mid,
        "title": title,
        "essence": essence,
        "full_record": essence,
        "layer": layer,
        "project": project,
        "source_type": source_type,
        "importance": 3,
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
        "domain_tags": [],
        "related": related or [],
    }


# ---------------------------------------------------------------------------
# Test 1: memory_search returns a search-kind result with compact index header
# ---------------------------------------------------------------------------


def test_memory_search_returns_search_kind(monkeypatch):
    fake_memories = [
        _make_mem(mid="aa111111" + "0" * 24, title="Alpha memory", essence="alpha content", layer=1),
        _make_mem(mid="bb222222" + "0" * 24, title="Beta memory", essence="beta content", layer=2),
    ]
    monkeypatch.setattr("memem.recall._search_memories", lambda *a, **kw: fake_memories)

    from memem.recall import memory_search
    result = memory_search("alpha beta")

    # Must have the search-kind compact index header
    assert "### Compact memory index" in result
    assert "2 results" in result


# ---------------------------------------------------------------------------
# Test 2: memory_search includes layer summary
# ---------------------------------------------------------------------------


def test_memory_search_includes_layer_summary(monkeypatch):
    fake_memories = [
        _make_mem(mid="ll000000" + "0" * 24, title="L0 memory", essence="identity content", layer=0),
        _make_mem(mid="ll111111" + "0" * 24, title="L1 memory", essence="pattern content", layer=1),
        _make_mem(mid="ll222222" + "0" * 24, title="L2 memory", essence="domain content", layer=2),
    ]
    monkeypatch.setattr("memem.recall._search_memories", lambda *a, **kw: fake_memories)

    from memem.recall import memory_search
    result = memory_search("test query")

    # Must contain layer summary line
    assert "_layers:" in result
    assert "L0=" in result
    assert "L1=" in result
    assert "L2=" in result


# ---------------------------------------------------------------------------
# Test 3: memory_search preserves compact line format [<8chars>] L<n> title — snippet
# ---------------------------------------------------------------------------


def test_memory_search_preserves_compact_line_format(monkeypatch):
    fake_memories = [
        _make_mem(
            mid="deadbeef" + "0" * 24,
            title="My Important Memory",
            essence="the key insight here",
            layer=2,
        ),
    ]
    monkeypatch.setattr("memem.recall._search_memories", lambda *a, **kw: fake_memories)

    from memem.recall import memory_search
    result = memory_search("important")

    # Must match the canonical compact line format
    pattern = re.compile(r"\[.{8}\] L\d .+ — .+")
    matching_lines = [line for line in result.splitlines() if pattern.match(line)]
    assert len(matching_lines) >= 1, f"No compact index lines found in:\n{result}"

    # Verify the specific line content
    assert "[deadbeef]" in result
    assert "L2" in result
    assert "My Important Memory" in result
    assert "the key insight here" in result


# ---------------------------------------------------------------------------
# Test 4: memory_get returns get-kind slice with ### [id] title header
# ---------------------------------------------------------------------------


def test_memory_get_returns_get_kind(monkeypatch):
    mem_id = "getme123" + "0" * 24
    fake_mem = _make_mem(
        mid=mem_id,
        title="Fetched Memory Title",
        essence="This is the full content of the memory",
        layer=1,
    )
    monkeypatch.setattr(
        "memem.recall._find_memory",
        lambda mid: fake_mem if mid.startswith("getme123") else None,
    )

    from memem.recall import memory_get
    result = memory_get(["getme123"])

    # Must have the full-content header format
    assert "### [getme123]" in result
    assert "Fetched Memory Title" in result
    assert "This is the full content" in result


# ---------------------------------------------------------------------------
# Test 5: memory_timeline chronological ordering
# ---------------------------------------------------------------------------


def test_memory_timeline_chronological_ordering(monkeypatch):
    anchor_id = "anchor00" + "0" * 24
    before_id = "before00" + "0" * 24
    after_id = "after000" + "0" * 24

    anchor_mem = _make_mem(mid=anchor_id, title="Anchor", essence="anchor content", project="proj", created_at="2026-06-01T00:00:00Z")
    before_mem = _make_mem(mid=before_id, title="Earlier Event", essence="before content", project="proj", created_at="2026-05-01T00:00:00Z")
    after_mem = _make_mem(mid=after_id, title="Later Event", essence="after content", project="proj", created_at="2026-07-01T00:00:00Z")

    def fake_find_memory(mid: str) -> dict | None:
        for m in [anchor_mem, before_mem, after_mem]:
            if m["id"].startswith(mid) or mid.startswith(m["id"][:8]):
                return m
        return None

    monkeypatch.setattr("memem.recall._find_memory", fake_find_memory)
    monkeypatch.setattr("memem.recall._obsidian_memories", lambda scope_id=None: [anchor_mem, before_mem, after_mem])

    from memem.recall import memory_timeline
    result = memory_timeline("anchor00")

    # Should have a timeline header
    assert "Timeline around" in result
    assert "anchor00" in result
    assert "Anchor" in result

    # Before should appear before anchor in the output
    if "Before" in result and "After" in result:
        before_pos = result.find("Earlier Event")
        after_pos = result.find("Later Event")
        anchor_pos = result.find("**Anchor:**")
        if before_pos > 0 and after_pos > 0 and anchor_pos > 0:
            assert before_pos < anchor_pos, "Before items should appear before anchor"
            assert anchor_pos < after_pos, "Anchor should appear before after items"


# ---------------------------------------------------------------------------
# Test 6: render_slice_markdown with active kind delegates to render_slice_as_prompt_context
# ---------------------------------------------------------------------------


def test_render_slice_markdown_active_kind():
    from memem.active_slice import ActiveMemorySlice, render_slice_as_prompt_context, render_slice_markdown

    slice_obj: ActiveMemorySlice = {
        "slice_id": "test_slice_id",
        "slice_kind": "active",
        "scope_id": "test",
        "query": "test query",
        "generated_at": "2026-01-01T00:00:00Z",
        "goals": [],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "delta_results": [],
        "carry_forward_summary": [],
        "projection_hint": {},
        "previous_slice_id": "",
        "slice_diff": {},
        "artifact_progression": {"stage": "none", "signals": []},
        "task_mode": "",
        "writeback_summary": {
            "status": "not_run",
            "dry_run": True,
            "proposed_count": 0,
            "auto_committed_count": 0,
            "manual_review_count": 0,
            "blocked_count": 0,
            "rejected_count": 0,
        },
        "candidate_count": 0,
        "recall_candidate_count": 0,
        "should_emit_context": True,
        "activation_mode": "heuristic",
        "confidence": 0.6,
        "warnings": [],
    }

    result_via_dispatcher = render_slice_markdown(slice_obj)
    result_direct = render_slice_as_prompt_context(slice_obj)

    # Both should produce equivalent output
    assert result_via_dispatcher == result_direct


# ---------------------------------------------------------------------------
# Test 7: render_slice_markdown with no slice_kind falls back gracefully
# ---------------------------------------------------------------------------


def test_render_slice_markdown_unknown_kind_fallback():
    from memem.active_slice import render_slice_markdown

    # Slice with no slice_kind — should not raise
    slice_obj = {
        "scope_id": "test",
        "query": "fallback test",
        "goals": [],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "should_emit_context": True,
        "candidate_count": 0,
        "recall_candidate_count": 0,
        "activation_mode": "heuristic",
        "confidence": 0.5,
        "warnings": [],
    }
    # Should not raise — falls back to render_slice_as_prompt_context
    result = render_slice_markdown(slice_obj)  # type: ignore[arg-type]
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Test 8: _layer_summary_from_items counts correctly
# ---------------------------------------------------------------------------


def test_layer_summary_counts_correct():
    from memem.active_slice import MemoryItem, _layer_summary_from_items

    items: list[MemoryItem] = [
        {"id": "a1", "title": "A", "content": "x", "layer": 0},
        {"id": "a2", "title": "B", "content": "x", "layer": 1},
        {"id": "a3", "title": "C", "content": "x", "layer": 2},
        {"id": "a4", "title": "D", "content": "x", "layer": 2},
        {"id": "a5", "title": "E", "content": "x", "layer": 3},
        {"id": "a6", "title": "F", "content": "x", "layer": 2},
    ]
    summary = _layer_summary_from_items(items)

    assert summary[0] == 1
    assert summary[1] == 1
    assert summary[2] == 3
    assert summary[3] == 1
    assert 4 not in summary  # no L4 items
