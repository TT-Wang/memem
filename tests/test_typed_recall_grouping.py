"""Tests for typed recall grouping: _classify_kinds, _render_typed_sections, _render_get_slice.

Covers:
- Tag-driven kind classification (explicit type: tags)
- Heuristic kind classification (episodic, skill, case)
- Edge cases: no match, non-type tags, bare memory
- _render_typed_sections section headers and counts
- _render_get_slice dedup behavior (typed items not repeated in default block)
- Legacy memory renders only in default section
- Bonus: MemoryItem tags field (verifies dual-field lookup introduced in m1)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Test 1: explicit tag "type:skill"
# ---------------------------------------------------------------------------


def test_classify_kinds_explicit_tag():
    from memem.active_slice import _classify_kinds

    result = _classify_kinds({"domain_tags": ["type:skill"]})
    assert result == ["skill"]


# ---------------------------------------------------------------------------
# Test 2: multiple explicit type tags, order preserved
# ---------------------------------------------------------------------------


def test_classify_kinds_multi_label_explicit():
    from memem.active_slice import _classify_kinds

    result = _classify_kinds({"domain_tags": ["type:episodic", "type:case"]})
    assert result == ["episodic", "case"]


# ---------------------------------------------------------------------------
# Test 3: heuristic episodic — source_session + ISO-date title
# ---------------------------------------------------------------------------


def test_classify_kinds_heuristic_episodic():
    from memem.active_slice import _classify_kinds

    mem = {
        "source_session": "abc",
        "title": "On 2026-06-05 something happened",
        "content": "We had a meeting about the architecture.",
    }
    result = _classify_kinds(mem)
    assert "episodic" in result


# ---------------------------------------------------------------------------
# Test 4: heuristic skill — importance >= 4, related >= 2, imperative in first 200 chars
# ---------------------------------------------------------------------------


def test_classify_kinds_heuristic_skill():
    from memem.active_slice import _classify_kinds

    mem = {
        "importance": 5,
        "related": ["m1", "m2"],
        "content": "Always use the rsync command when copying recursively. Avoid raw cp -r.",
    }
    result = _classify_kinds(mem)
    assert "skill" in result


# ---------------------------------------------------------------------------
# Test 5: heuristic case — source_session + long content with trigger keywords
# ---------------------------------------------------------------------------


def test_classify_kinds_heuristic_case():
    from memem.active_slice import _classify_kinds

    long_content = (
        "We tried a new approach to the database migration. "
        "The approach involved batching writes to reduce lock contention. "
        "The result was a 40% reduction in timeout errors. "
        "We also tried disabling autocommit, which helped further. "
        + "x" * 400  # pad to well over 500 chars
    )
    assert len(long_content) > 500, "content must exceed 500 chars for case heuristic"

    mem = {
        "source_session": "abc",
        "content": long_content,
    }
    result = _classify_kinds(mem)
    assert "case" in result


# ---------------------------------------------------------------------------
# Test 6: bare memory {} returns []
# ---------------------------------------------------------------------------


def test_classify_kinds_no_match():
    from memem.active_slice import _classify_kinds

    result = _classify_kinds({})
    assert result == []


# ---------------------------------------------------------------------------
# Test 7: non-type tags (no "type:" prefix) fall through to heuristics;
#          bare memory with no heuristic signals → []
# ---------------------------------------------------------------------------


def test_classify_kinds_topic_tag_ignored():
    from memem.active_slice import _classify_kinds

    # Tags without "type:" prefix are not kind tags — they should fall through
    # to heuristics. With no other fields set, heuristics also return [].
    result = _classify_kinds({"domain_tags": ["mining", "haiku"]})
    assert result == []


# ---------------------------------------------------------------------------
# Test 8: _render_typed_sections produces correct section headers with counts
# ---------------------------------------------------------------------------


def test_render_typed_sections_headers():
    from memem.active_slice import _render_typed_sections

    episodic_item = {
        "id": "ep000001",
        "title": "On 2026-01-10 deployment",
        "content": "We deployed the new feature.",
        "layer": 2,
        "source_session": "session-ep",
        "domain_tags": ["type:episodic"],
        "snippet": "",
    }
    skill_item = {
        "id": "sk000001",
        "title": "Prefer rsync over cp",
        "content": "Always use rsync when copying large trees recursively. Avoid cp -r.",
        "layer": 1,
        "importance": 5,
        "related": ["r1", "r2"],
        "domain_tags": ["type:skill"],
        "snippet": "",
    }

    result = _render_typed_sections([episodic_item, skill_item])

    assert "## Episodic memory (1)" in result
    assert "## Skills (1)" in result


# ---------------------------------------------------------------------------
# Test 9: _render_get_slice dedup — typed items don't repeat in default block
# ---------------------------------------------------------------------------


def test_render_get_slice_typed_index_plus_full_content():
    """Typed sections render as a compact navigation index AT THE TOP;
    the per-item loop below still emits the FULL content block for every
    item. Both should appear, so memory_get's contract (full content for
    requested IDs) is preserved even for typed memories.
    """
    from memem.active_slice import _render_get_slice

    skill_item = {
        "id": "sk000002",
        "memory_id": "sk000002",
        "title": "Use rsync for file copies",
        "content": "Always use rsync instead of cp when copying files. Avoid raw cp -r for large dirs.",
        "layer": 1,
        "importance": 5,
        "related": ["rel-a", "rel-b"],
        "domain_tags": ["type:skill"],
        "snippet": "",
        "project": "general",
        "source_type": "user",
        "tags": [],
    }

    slice_data = {
        "slice_kind": "get",
        "items": [skill_item],
        "missing_ids": [],
        "linked": [],
    }

    result = _render_get_slice(slice_data)  # type: ignore[arg-type]

    # Typed section header present
    assert "## Skills" in result
    # Full content block present (the per-item ### header)
    assert "### [sk000002]" in result
    # Full content body preserved (not truncated to compact snippet)
    assert "Always use rsync instead of cp when copying files" in result


# ---------------------------------------------------------------------------
# Test 10: bare memory with no kind signals renders only in default section
# ---------------------------------------------------------------------------


def test_legacy_memory_no_kind_tags_renders_default():
    from memem.active_slice import _render_get_slice

    bare_item = {
        "id": "bare0001",
        "memory_id": "bare0001",
        "title": "Old untyped memory",
        "content": "Some general note about nothing in particular.",
        "layer": 2,
        "importance": 2,
        "related": [],
        "domain_tags": [],
        "tags": [],
        "snippet": "",
        "project": "general",
        "source_type": "user",
    }

    slice_data = {
        "slice_kind": "get",
        "items": [bare_item],
        "missing_ids": [],
        "linked": [],
    }

    result = _render_get_slice(slice_data)  # type: ignore[arg-type]

    # Typed sections should NOT appear (no kind matches)
    assert "## Episodic memory" not in result
    assert "## Skills" not in result
    assert "## Cases" not in result

    # But the item must still be rendered in the default block
    assert "Old untyped memory" in result
    assert "bare0001"[:8] in result


# ---------------------------------------------------------------------------
# Test 11 (bonus): MemoryItem uses 'tags' field, not 'domain_tags'
# ---------------------------------------------------------------------------


def test_classify_kinds_memoryitem_tags_field():
    """Verifies the dual-field lookup: MemoryItem.tags path works correctly."""
    from memem.active_slice import _classify_kinds

    result = _classify_kinds({"tags": ["type:skill"]})
    assert result == ["skill"]
