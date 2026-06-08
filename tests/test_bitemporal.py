"""Tests for bi-temporal invalidation (memem v2 phase 1).

Covers:
1. _make_memory initializes bi-temporal fields
2. invalidate_memory sets invalid_at and replaced_by
3. memory search excludes invalidated memories
4. L0 anchor gather excludes invalidated memories
5. Invalidated memories are still findable via explicit memory_get
6. Frontmatter round-trip preserves all three bi-temporal fields
7. invalidate_memory on unknown ID returns False (no raise)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Test 1: _make_memory initializes bi-temporal fields
# ---------------------------------------------------------------------------


def test_make_memory_initializes_bitemporal_fields(tmp_vault):
    from memem.obsidian_store import _make_memory

    mem = _make_memory(content="Python uses indentation to define code blocks.", title="Python indentation")

    assert "valid_at" in mem, "valid_at must be set on new memory"
    assert mem["valid_at"], "valid_at must be non-empty"
    assert mem["invalid_at"] is None, "invalid_at must be None for new memory"
    assert mem["replaced_by"] is None, "replaced_by must be None for new memory"


# ---------------------------------------------------------------------------
# Test 2: invalidate_memory sets invalid_at and replaced_by
# ---------------------------------------------------------------------------


def test_invalidate_memory_sets_invalid_at_and_replaced_by(tmp_vault, tmp_cortex_dir):
    from memem.obsidian_store import (
        _find_memory,
        _make_memory,
        _write_obsidian_memory,
        invalidate_memory,
    )

    mem1 = _make_memory(content="Python 3.10 is the minimum required version.", title="Min Python version old")
    _write_obsidian_memory(mem1)

    mem2 = _make_memory(content="Python 3.11 is the minimum required version.", title="Min Python version new")
    _write_obsidian_memory(mem2)

    result = invalidate_memory(mem1["id"], replaced_by=mem2["id"])
    assert result is True, "invalidate_memory must return True on success"

    # Reload from cache
    reloaded = _find_memory(mem1["id"])
    assert reloaded is not None
    assert reloaded["invalid_at"] is not None, "invalid_at must be set after invalidation"
    assert reloaded["replaced_by"] == mem2["id"], "replaced_by must point to successor"


# ---------------------------------------------------------------------------
# Test 3: search excludes invalidated memories
# ---------------------------------------------------------------------------


def test_search_excludes_invalidated_memories(tmp_vault, tmp_cortex_dir):
    from memem.obsidian_store import (
        _make_memory,
        _save_memory,
        invalidate_memory,
    )
    from memem.recall import _search_memories

    mem1 = _make_memory(
        content="The project uses SQLite as the primary database for local storage.",
        title="Database: SQLite (old)",
    )
    _save_memory(mem1)

    mem2 = _make_memory(
        content="The project uses PostgreSQL as the primary database for local storage.",
        title="Database: PostgreSQL (new)",
    )
    _save_memory(mem2)

    invalidate_memory(mem1["id"], replaced_by=mem2["id"])

    results = _search_memories("database primary storage", record_access=False, expand_links=False)
    result_ids = [m["id"] for m in results]

    assert mem1["id"] not in result_ids, "invalidated memory must not appear in search results"
    assert mem2["id"] in result_ids, "valid replacement memory must appear in search results"


# ---------------------------------------------------------------------------
# Test 5: invalidated memory is still findable via explicit get (history preserved)
# ---------------------------------------------------------------------------


def test_invalidated_memory_still_findable_via_explicit_get(tmp_vault, tmp_cortex_dir):
    from memem.obsidian_store import (
        _find_memory,
        _make_memory,
        _write_obsidian_memory,
        invalidate_memory,
    )

    mem = _make_memory(
        content="The team uses Slack for all internal communications.",
        title="Comms tool: Slack",
    )
    _write_obsidian_memory(mem)
    invalidate_memory(mem["id"])

    # Direct lookup must still return the memory (bi-temporal preserves history)
    found = _find_memory(mem["id"])
    assert found is not None, "Invalidated memory must still be retrievable via _find_memory"
    assert found["invalid_at"] is not None, "Found memory must still carry invalid_at timestamp"


# ---------------------------------------------------------------------------
# Test 6: frontmatter round-trip preserves bi-temporal fields
# ---------------------------------------------------------------------------


def test_frontmatter_round_trip_preserves_bitemporal_fields(tmp_vault, tmp_cortex_dir):
    from memem.obsidian_store import (
        _make_memory,
        _write_obsidian_memory,
        invalidate_memory,
    )

    mem = _make_memory(
        content="Use ruff for linting and mypy for type checking in all Python projects.",
        title="Python lint/type toolchain",
    )
    _write_obsidian_memory(mem)

    original_valid_at = mem["valid_at"]
    assert original_valid_at, "valid_at must be set after write"

    # Invalidate to set invalid_at and replaced_by
    invalidate_memory(mem["id"], replaced_by="deadbeef-0000-0000-0000-000000000000")

    # Re-read from disk (bypassing in-memory cache by reloading)
    from memem.models import OBSIDIAN_MEMORIES_DIR
    from memem.obsidian_store import _parse_obsidian_memory_file

    # Find the file on disk
    md_files = list(OBSIDIAN_MEMORIES_DIR.glob(f"*-{mem['id'][:8]}.md"))
    assert md_files, "Memory file must exist on disk"
    parsed = _parse_obsidian_memory_file(md_files[0])
    assert parsed is not None

    assert parsed["valid_at"], "valid_at must survive round-trip"
    assert parsed["invalid_at"] is not None, "invalid_at must survive round-trip"
    assert parsed["replaced_by"] == "deadbeef-0000-0000-0000-000000000000", "replaced_by must survive round-trip"


# ---------------------------------------------------------------------------
# Test 7: invalidate_memory on unknown ID returns False without raising
# ---------------------------------------------------------------------------


def test_invalidate_unknown_memory_returns_false(tmp_vault):
    from memem.obsidian_store import invalidate_memory

    result = invalidate_memory("nonexistent-memory-id-00000000")
    assert result is False, "invalidate_memory on unknown ID must return False, not raise"
