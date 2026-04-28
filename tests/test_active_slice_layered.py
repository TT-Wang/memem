"""Tests for layer-aware active slice engine (m2).

Verifies:
1. L0 memories for active scope are always included regardless of query relevance.
2. L3 memories are never included in auto-recall slices.
3. L0, L1, and L2 memories all pass through normally.
4. L0 memories tagged for a different project are NOT included.
5. L0 memories that also match the query appear exactly once (dedup).
"""

from __future__ import annotations

import importlib


def _reload_stores():
    """Reload obsidian_store so vault env changes take effect."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)


def _all_candidate_memory_ids(slice_obj: dict) -> set[str]:
    """Collect all memory_ids from every section of the slice."""
    ids: set[str] = set()
    for section_key in ("goals", "constraints", "active_background", "decisions", "preferences", "failure_patterns"):
        for item in slice_obj.get(section_key, []):
            mid = item.get("memory_id", "")
            if mid:
                ids.add(mid)
    return ids


def test_l0_memories_always_in_slice(tmp_vault, tmp_cortex_dir):
    """L0 memory for the active project should appear in every slice, even for
    an unrelated query."""
    _reload_stores()
    from memem.active_slice_engine import generate_active_memory_slice
    from memem.obsidian_store import _make_memory, _save_memory

    l0_mem = _make_memory(
        content="myproj uses FastAPI with SQLAlchemy and PostgreSQL as the core tech stack.",
        title="myproj tech stack identity",
        project="myproj",
        source_type="user",
        importance=5,
        layer=0,
    )
    _save_memory(l0_mem)

    slice_obj = generate_active_memory_slice(
        "completely unrelated query about cooking recipes",
        scope_id="myproj",
        use_llm=False,
    )

    all_ids = _all_candidate_memory_ids(slice_obj)
    assert l0_mem["id"] in all_ids, (
        f"Expected L0 memory {l0_mem['id']!r} in slice, got ids: {all_ids}"
    )


def test_l3_memories_excluded_from_slice(tmp_vault, tmp_cortex_dir):
    """L3 (archival) memories must never appear in auto-recall slices."""
    _reload_stores()
    from memem.active_slice_engine import generate_active_memory_slice
    from memem.obsidian_store import _make_memory, _save_memory

    l3_mem = _make_memory(
        content="Rare edge case: asyncio.run inside a Jupyter cell raises RuntimeError on Python 3.10.",
        title="asyncio.run Jupyter edge case",
        project="myproj",
        source_type="user",
        importance=2,
        layer=3,
    )
    _save_memory(l3_mem)

    slice_obj = generate_active_memory_slice(
        "asyncio run jupyter edge case",
        scope_id="myproj",
        use_llm=False,
    )

    # Check the slice str representation — L3 id must not appear anywhere.
    slice_str = str(slice_obj)
    assert l3_mem["id"] not in slice_str, (
        f"L3 memory {l3_mem['id']!r} should NOT appear in slice, but it did"
    )


def test_l0_l1_l2_all_pass_through(tmp_vault, tmp_cortex_dir):
    """L0, L1, and L2 memories should all be present in the slice."""
    _reload_stores()
    from memem.active_slice_engine import generate_active_memory_slice
    from memem.obsidian_store import _make_memory, _save_memory

    l0_mem = _make_memory(
        content="myproj is a Django + Celery application deployed on AWS ECS.",
        title="myproj core identity",
        project="myproj",
        source_type="user",
        importance=5,
        layer=0,
    )
    l1_mem = _make_memory(
        content="Always run myproj test suite with pytest -x before merging.",
        title="myproj pytest convention",
        project="myproj",
        source_type="user",
        importance=4,
        layer=1,
    )
    l2_mem = _make_memory(
        content="myproj auth module uses JWT tokens with 24h expiry.",
        title="myproj auth JWT config",
        project="myproj",
        source_type="user",
        importance=3,
        layer=2,
    )
    for mem in (l0_mem, l1_mem, l2_mem):
        _save_memory(mem)

    slice_obj = generate_active_memory_slice(
        "auth module pytest test suite Django Celery AWS JWT",
        scope_id="myproj",
        use_llm=False,
    )

    slice_str = str(slice_obj)
    assert l0_mem["id"] in slice_str, f"L0 memory {l0_mem['id']!r} should be in slice"
    assert l1_mem["id"] in slice_str, f"L1 memory {l1_mem['id']!r} should be in slice"
    assert l2_mem["id"] in slice_str, f"L2 memory {l2_mem['id']!r} should be in slice"


def test_l0_for_other_project_not_included(tmp_vault, tmp_cortex_dir):
    """L0 memory belonging to a different project must NOT appear in slice for myproj."""
    _reload_stores()
    from memem.active_slice_engine import generate_active_memory_slice
    from memem.obsidian_store import _make_memory, _save_memory

    other_l0 = _make_memory(
        content="otherprojXYZ uses React + GraphQL as the frontend stack.",
        title="otherprojXYZ tech stack identity",
        project="otherprojXYZ",
        source_type="user",
        importance=5,
        layer=0,
    )
    _save_memory(other_l0)

    slice_obj = generate_active_memory_slice(
        "React GraphQL frontend otherprojXYZ stack",
        scope_id="myproj",
        use_llm=False,
    )

    slice_str = str(slice_obj)
    assert other_l0["id"] not in slice_str, (
        f"L0 memory for 'other' project {other_l0['id']!r} should NOT appear in myproj slice"
    )


def test_l0_dedup_when_also_matches_query(tmp_vault, tmp_cortex_dir):
    """L0 memory whose content also matches the query should appear exactly once."""
    _reload_stores()
    from memem.active_slice_engine import generate_candidates
    from memem.obsidian_store import _make_memory, _save_memory

    l0_mem = _make_memory(
        content="myproj uses FastAPI SQLAlchemy PostgreSQL for all database access.",
        title="myproj database identity",
        project="myproj",
        source_type="user",
        importance=5,
        layer=0,
    )
    _save_memory(l0_mem)

    # Query that will match the L0 memory via relevance search too.
    bundle = generate_candidates(
        "FastAPI SQLAlchemy PostgreSQL database access myproj",
        scope_id="myproj",
        limit=20,
    )

    memory_candidates = bundle["memory_candidates"]
    mem_id = l0_mem["id"]
    matching = [c for c in memory_candidates if c.get("memory_id") == mem_id]
    assert len(matching) == 1, (
        f"L0 memory {mem_id!r} should appear exactly once in candidates, "
        f"but found {len(matching)} occurrences"
    )
