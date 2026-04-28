"""End-to-end pipeline tests for layered memory (Phase A, m5).

Exercises the full save → recall → slice → assemble pipeline across all four
layer behaviors introduced in m1-m4.
"""

from __future__ import annotations

import importlib
import re


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_stores():
    """Reload all memem modules so env-var fixtures take effect."""
    from memem import models, obsidian_store, search_index, telemetry
    importlib.reload(models)
    importlib.reload(telemetry)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)


def _candidate_memory_ids(bundle: dict) -> list[str]:
    """Extract all memory_ids from a CandidateBundle's memory_candidates."""
    return [c.get("memory_id", "") for c in bundle.get("memory_candidates", [])]


# ---------------------------------------------------------------------------
# Test 1: L0 memory always appears in active slice for its project
# ---------------------------------------------------------------------------


def test_save_l0_then_active_slice_pins_it(tmp_vault, tmp_cortex_dir):
    """L0 memory is always pinned in the active slice regardless of query."""
    _reload_stores()
    from memem.active_slice_engine import generate_candidates
    from memem.operations import memory_save

    result = memory_save(
        content="proj-x uses Django + Celery + PostgreSQL as its core tech stack.",
        title="proj-x identity stack",
        scope_id="proj-x",
        layer=0,
    )
    assert "Memory saved" in result or "already exists" in result

    # Query is completely unrelated to Django / PostgreSQL
    bundle = generate_candidates(
        "what is the weather like today",
        scope_id="proj-x",
        limit=20,
    )

    memory_ids = _candidate_memory_ids(bundle)
    # Find the saved memory id from Obsidian
    from memem.obsidian_store import _obsidian_memories
    mems = _obsidian_memories()
    l0_mems = [m for m in mems if m.get("layer") == 0]
    assert l0_mems, "Expected at least one L0 memory to be saved"
    l0_id = l0_mems[0]["id"]

    assert l0_id in memory_ids, (
        f"L0 memory {l0_id!r} should be pinned in active slice candidates "
        f"for proj-x regardless of query. Got ids: {memory_ids}"
    )


# ---------------------------------------------------------------------------
# Test 2: L3 memory does NOT appear in active slice
# ---------------------------------------------------------------------------


def test_save_l3_then_active_slice_excludes_it(tmp_vault, tmp_cortex_dir):
    """L3 archival memory must never appear in auto-recall active slice."""
    _reload_stores()
    from memem.active_slice_engine import generate_candidates
    from memem.operations import memory_save

    result = memory_save(
        content="Rare edge case: asyncio.run inside Jupyter raises RuntimeError on Python 3.10.",
        title="asyncio Jupyter rare edge case",
        scope_id="proj-x",
        layer=3,
    )
    assert "Memory saved" in result or "already exists" in result

    from memem.obsidian_store import _obsidian_memories
    mems = _obsidian_memories()
    l3_mems = [m for m in mems if m.get("layer") == 3]
    assert l3_mems, "Expected at least one L3 memory to be saved"
    l3_id = l3_mems[0]["id"]

    # Query that matches the L3 content directly
    bundle = generate_candidates(
        "asyncio.run jupyter RuntimeError Python edge case",
        scope_id="proj-x",
        limit=20,
    )

    memory_ids = _candidate_memory_ids(bundle)
    assert l3_id not in memory_ids, (
        f"L3 memory {l3_id!r} should NOT be in active slice candidates, "
        f"but was found. Got ids: {memory_ids}"
    )


# ---------------------------------------------------------------------------
# Test 3: L3 memory IS findable via explicit memory_search
# ---------------------------------------------------------------------------


def test_save_l3_still_findable_via_explicit_search(tmp_vault, tmp_cortex_dir):
    """L3 memory excluded from active slice is still reachable via memory_search."""
    _reload_stores()
    from memem.operations import memory_save

    result = memory_save(
        content="Legacy workaround: set PYTHONHASHSEED=0 to get deterministic dict ordering on Python 3.5.",
        title="PYTHONHASHSEED deterministic dict ordering",
        scope_id="proj-x",
        layer=3,
    )
    assert "Memory saved" in result or "already exists" in result

    from memem.obsidian_store import _obsidian_memories
    mems = _obsidian_memories()
    l3_mems = [m for m in mems if m.get("layer") == 3]
    assert l3_mems, "Expected at least one L3 memory"
    l3_id = l3_mems[0]["id"]

    # Explicit search should find it — search is the intentional retrieval path
    from memem.recall import memory_search
    search_result = memory_search("PYTHONHASHSEED deterministic dict", scope_id="proj-x")

    assert l3_id[:8] in search_result, (
        f"L3 memory {l3_id[:8]!r} should be findable via memory_search, "
        f"but was not found. Got:\n{search_result}"
    )


# ---------------------------------------------------------------------------
# Test 4: Auto-classify produces at least 2 distinct layers across 3 saves
# ---------------------------------------------------------------------------


def test_save_auto_classify_then_search(tmp_vault, tmp_cortex_dir):
    """Auto-classification heuristic assigns varied layers — not all L2."""
    _reload_stores()
    from memem.obsidian_store import _make_memory, _obsidian_memories, _save_memory

    # L0 candidate: importance=5, structural tag -> classify_layer should pick L0 or L1
    m_high = _make_memory(
        content="autotest-e2e project uses FastAPI + PostgreSQL + Redis as its core infrastructure.",
        title="autotest-e2e core identity",
        project="autotest-e2e",
        source_type="user",
        importance=5,
        tags=["architecture", "identity"],
    )
    _save_memory(m_high)

    # L1 candidate: project=general + importance>=4
    m_generic = _make_memory(
        content="Always run tests with pytest -x to stop on first failure.",
        title="pytest -x stop on failure convention",
        project="general",
        source_type="user",
        importance=4,
    )
    _save_memory(m_generic)

    # L3 candidate: low importance edge case note
    m_low = _make_memory(
        content="One obscure edge case: SQLite WAL mode locks the DB when the disk is full.",
        title="SQLite WAL disk-full edge case",
        project="autotest-e2e",
        source_type="user",
        importance=1,
    )
    _save_memory(m_low)

    all_mems = _obsidian_memories()
    layers = {m["layer"] for m in all_mems}

    assert len(layers) >= 2, (
        f"Expected at least 2 distinct layers across auto-classified saves, "
        f"but got layers: {sorted(layers)}. "
        f"Heuristic may not be firing correctly."
    )


# ---------------------------------------------------------------------------
# Test 5: context_assemble includes L0-pinned memory
# ---------------------------------------------------------------------------


def test_context_assemble_includes_l0_pinned(tmp_vault, tmp_cortex_dir):
    """context_assemble output includes L0 memories saved for the project."""
    _reload_stores()
    from memem.operations import memory_save

    save_l0 = memory_save(
        content="context-proj uses NestJS + TypeORM + PostgreSQL as its primary tech stack.",
        title="context-proj identity",
        scope_id="context-proj",
        layer=0,
    )
    assert "Memory saved" in save_l0 or "already exists" in save_l0

    save_l2 = memory_save(
        content="Always use async/await in context-proj service layer to avoid blocking.",
        title="context-proj async service pattern",
        scope_id="context-proj",
        layer=2,
    )
    assert "Memory saved" in save_l2 or "already exists" in save_l2

    from memem.assembly import context_assemble
    result = context_assemble("tech stack architecture identity", project="context-proj")

    # context_assemble must return a non-empty string
    assert isinstance(result, str), f"Expected str, got {type(result)}"

    # The L0 memory content or title should appear
    assert "NestJS" in result or "context-proj" in result, (
        f"Expected L0 memory content in assembled output, got:\n{result[:500]}"
    )


# ---------------------------------------------------------------------------
# Test 6: save → search → get round-trip preserves layer
# ---------------------------------------------------------------------------


def test_save_search_get_round_trip_preserves_layer(tmp_vault, tmp_cortex_dir):
    """save(layer=1) → search → get → output contains L1."""
    _reload_stores()
    from memem.operations import memory_save

    result = memory_save(
        content="Always write pytest fixtures using the tmp_path built-in for isolated temp dirs.",
        title="pytest tmp_path fixture pattern",
        scope_id="round-trip-proj",
        layer=1,
    )
    assert "Memory saved" in result or "already exists" in result

    # Search to find the saved memory id
    from memem.recall import memory_search
    search_output = memory_search("pytest tmp_path fixture", scope_id="round-trip-proj")

    # Extract an 8-char id from the search results
    pattern = re.compile(r"\[([a-f0-9]{8})\]")
    ids_found = pattern.findall(search_output)
    assert ids_found, f"No memory IDs found in search output:\n{search_output}"

    # Verify L1 appears in search output
    assert "L1" in search_output, (
        f"Expected 'L1' in search output for layer=1 memory, got:\n{search_output}"
    )

    # Fetch the memory by id via memory_get
    from memem.recall import memory_get
    get_output = memory_get([ids_found[0]])

    # The get output should also show L1
    assert "L1" in get_output, (
        f"Expected 'L1' in memory_get output for layer=1 memory, got:\n{get_output}"
    )


# ---------------------------------------------------------------------------
# Test 7: context_assemble augments with general when project is sparse
# ---------------------------------------------------------------------------


def test_context_assemble_composes_with_general_when_sparse(tmp_vault, tmp_cortex_dir):
    """Sparse project + populated general scope → composed output with augmentation."""
    _reload_stores()
    from memem.operations import memory_save

    # Save just one project memory (below sparse threshold of 5)
    save_proj = memory_save(
        content="sparse-proj has a single known memory entry about deployment.",
        title="sparse-proj deployment note",
        scope_id="sparse-proj",
        layer=2,
    )
    assert "Memory saved" in save_proj or "already exists" in save_proj

    # Save several general memories to populate the general scope
    for i in range(3):
        save_gen = memory_save(
            content=f"General convention #{i}: always document your API endpoints with OpenAPI schemas.",
            title=f"OpenAPI documentation convention #{i}",
            scope_id="general",
            layer=1,
        )
        assert "Memory saved" in save_gen or "already exists" in save_gen

    from memem.assembly import context_assemble
    result = context_assemble("deployment API documentation", project="sparse-proj")

    assert isinstance(result, str), f"Expected str, got {type(result)}"
    # Sparse-project augmentation must surface in the rendered composition_strategy
    # line ("_strategy: primary+general-augmentation, ..."). Without this, a future
    # regression that drops the strategy line would silently pass.
    assert "augmentation" in result, (
        f"Expected augmentation strategy in assembled output. Got:\n{result[:500]}"
    )
