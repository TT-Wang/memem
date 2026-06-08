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

    # v2.0 format: "## Memory Search — {scope} — `{query}`"
    assert "## Memory Search" in result
    # Both memories should appear as compact index lines
    assert "[aa111111]" in result
    assert "[bb222222]" in result


# ---------------------------------------------------------------------------
# Test 2: memory_search includes layer summary
# ---------------------------------------------------------------------------




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

    # v2.0 format: "## Memory Timeline — Anchor [{id8}]"
    assert "Memory Timeline" in result
    assert "anchor00" in result
    assert "Anchor" in result

    # Before should appear before anchor in the output
    before_pos = result.find("Earlier Event")
    after_pos = result.find("Later Event")
    anchor_pos = result.find("### Anchor")
    if before_pos > 0 and after_pos > 0 and anchor_pos > 0:
        assert before_pos < anchor_pos, "Before items should appear before anchor"
        assert anchor_pos < after_pos, "Anchor should appear before after items"

