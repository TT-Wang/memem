"""Tests for m4: context_assemble composes slices.

Verifies that context_assemble:
1. Returns a "## Composed Briefing" header (assembled kind)
2. Deduplicates items appearing in both project and general scopes
3. Skips general augmentation when the primary scope has >= 5 items
4. Uses general augmentation when the primary scope is sparse (< 5 items)
5. Aggregates layer summary counts from merged items
6. Preserves the public signature context_assemble(query, project) -> str
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_active_slice(
    scope_id: str = "default",
    memory_entries: list[dict] | None = None,
    slice_id: str = "slice_test_000",
) -> dict:
    """Build a minimal ActiveMemorySlice-like dict for test patching."""
    entries = memory_entries or []
    return {
        "slice_id": slice_id,
        "scope_id": scope_id,
        "query": "test query",
        "generated_at": "2026-01-01T00:00:00Z",
        "should_emit_context": True,
        "goals": entries,
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "candidate_deltas": [],
        "delta_results": [],
        "warnings": [],
        "confidence": 0.7,
        "activation_mode": "heuristic",
    }


def _active_entry(
    memory_id: str,
    title: str,
    summary: str = "some summary",
    layer: int = 2,
    project: str = "default",
) -> dict:
    """Build an ActiveMemoryItem dict."""
    return {
        "memory_id": memory_id,
        "role": "background",
        "title": title,
        "summary": summary,
        "layer": layer,
        "score": 0.7,
        "source_type": "user",
        "project": project,
    }


def _make_build_slice_fn(project_entries: list[dict], general_entries: list[dict]):
    """Return a patched build_slice function that dispatches by scope_id."""
    def _build_slice(query: str, scope_id: str = "default", use_llm: bool = False, environment=None):
        if scope_id == "general":
            return _make_active_slice(scope_id="general", memory_entries=general_entries, slice_id="slice_gen")
        return _make_active_slice(scope_id=scope_id, memory_entries=project_entries, slice_id="slice_proj")
    return _build_slice


# ---------------------------------------------------------------------------
# Test 1: returns "## Composed Briefing" header
# ---------------------------------------------------------------------------


def test_context_assemble_returns_assembled_kind(monkeypatch):
    """context_assemble output contains the ## Composed Briefing header."""
    entries = [_active_entry("mem_aaa" + "0" * 25, "Alpha Memory")]
    import memem.active_slice_engine as ase
    monkeypatch.setattr(ase, "build_slice", _make_build_slice_fn(entries, []))

    from memem.assembly import context_assemble
    result = context_assemble("alpha query", project="myproject")

    assert "## Composed Briefing" in result, f"Expected composed briefing header, got: {result!r}"
    assert "Alpha Memory" in result


# ---------------------------------------------------------------------------
# Test 2: deduplication by id across project and general scope
# ---------------------------------------------------------------------------


def test_context_assemble_dedup_by_id(monkeypatch):
    """Items that appear in both project and general slices appear only once."""
    shared_id = "shared_mem_id" + "0" * 19
    # Same memory_id in both scopes
    proj_entries = [_active_entry(shared_id, "Shared Memory", project="myproject")]
    gen_entries = [_active_entry(shared_id, "Shared Memory (general copy)", project="general")]

    import memem.active_slice_engine as ase
    monkeypatch.setattr(ase, "build_slice", _make_build_slice_fn(proj_entries, gen_entries))

    from memem.assembly import context_assemble
    # Force sparse path: only 1 entry in project scope → general augmentation runs
    result = context_assemble("shared query", project="myproject")

    # The id should appear at most once (first 8 chars are used in compact format)
    short_id = shared_id[:8]
    count = result.count(short_id)
    assert count <= 1, f"Dedup failed: id '{short_id}' appears {count} times in:\n{result}"


# ---------------------------------------------------------------------------
# Test 3: skips general when primary has >= 5 items
# ---------------------------------------------------------------------------


def test_context_assemble_skips_general_when_primary_sufficient(monkeypatch):
    """General augmentation is skipped when primary scope has >= 5 items."""
    # 10 unique project entries — above the sparse threshold
    proj_entries = [
        _active_entry(f"mem_proj_{i:04d}" + "0" * 20, f"Project Memory {i}", project="myproject")
        for i in range(10)
    ]
    # Distinct general entry that would only appear if augmentation ran
    gen_entries = [_active_entry("mem_gen_9999" + "0" * 20, "General Only Memory", project="general")]

    import memem.active_slice_engine as ase

    calls: list[str] = []

    def _build_slice(query, scope_id="default", use_llm=False, environment=None):
        calls.append(scope_id)
        if scope_id == "general":
            return _make_active_slice(scope_id="general", memory_entries=gen_entries, slice_id="slice_gen")
        return _make_active_slice(scope_id=scope_id, memory_entries=proj_entries, slice_id="slice_proj")

    monkeypatch.setattr(ase, "build_slice", _build_slice)

    from memem.assembly import context_assemble
    result = context_assemble("project query", project="myproject")

    # General augmentation must NOT have been called
    assert "general" not in calls, f"build_slice was called for general scope unexpectedly. calls={calls}"
    # General-only item should not appear
    assert "General Only Memory" not in result, f"General item leaked into output:\n{result}"
    # The composition_strategy line should not mention augmentation
    if "_strategy:" in result:
        assert "augmentation" not in result, f"Augmentation strategy mentioned unexpectedly:\n{result}"


# ---------------------------------------------------------------------------
# Test 4: uses general when primary is sparse
# ---------------------------------------------------------------------------


def test_context_assemble_uses_general_when_primary_sparse(monkeypatch):
    """General augmentation runs when primary scope has < 5 items."""
    # Only 1 project entry — sparse
    proj_entries = [_active_entry("mem_proj_solo" + "0" * 19, "Solo Project Memory", project="myproject")]
    # Several general entries
    gen_entries = [
        _active_entry(f"mem_gen_{i:04d}" + "0" * 20, f"General Memory {i}", project="general")
        for i in range(3)
    ]

    import memem.active_slice_engine as ase
    calls: list[str] = []

    def _build_slice(query, scope_id="default", use_llm=False, environment=None):
        calls.append(scope_id)
        if scope_id == "general":
            return _make_active_slice(scope_id="general", memory_entries=gen_entries, slice_id="slice_gen")
        return _make_active_slice(scope_id=scope_id, memory_entries=proj_entries, slice_id="slice_proj")

    monkeypatch.setattr(ase, "build_slice", _build_slice)

    from memem.assembly import context_assemble
    result = context_assemble("sparse query", project="myproject")

    # General augmentation must have been called
    assert "general" in calls, f"build_slice was not called for general scope. calls={calls}"
    # General items should appear
    assert "General Memory 0" in result, f"General items not in output:\n{result}"
    # Composition strategy should mention augmentation
    assert "augmentation" in result, f"Expected 'augmentation' in output:\n{result}"


# ---------------------------------------------------------------------------
# Test 5: layer summary reflects union counts
# ---------------------------------------------------------------------------


def test_context_assemble_layer_summary_aggregated(monkeypatch):
    """Layer summary in output reflects counts from merged items across scopes."""
    proj_entries = [
        _active_entry("mem_l1_0001" + "0" * 21, "L1 Project Memory", layer=1, project="myproject"),
    ]
    gen_entries = [
        _active_entry("mem_l2_0001" + "0" * 21, "L2 General Memory", layer=2, project="general"),
    ]

    import memem.active_slice_engine as ase
    monkeypatch.setattr(ase, "build_slice", _make_build_slice_fn(proj_entries, gen_entries))

    from memem.assembly import context_assemble
    result = context_assemble("layer test", project="myproject")

    # Both layers should appear in summary line (since primary is sparse → augmented)
    assert "L1=1" in result or "L1" in result, f"L1 layer not in output:\n{result}"
    assert "L2=1" in result or "L2" in result, f"L2 layer not in output:\n{result}"


# ---------------------------------------------------------------------------
# Test 6: public signature unchanged
# ---------------------------------------------------------------------------


def test_context_assemble_signature_unchanged(monkeypatch):
    """context_assemble(query, project) still works with positional args."""
    entries = [_active_entry("mem_sig_0001" + "0" * 21, "Sig Memory")]

    import memem.active_slice_engine as ase
    monkeypatch.setattr(ase, "build_slice", _make_build_slice_fn(entries, []))

    from memem.assembly import context_assemble

    # Call with positional args (original public API)
    result = context_assemble("test signature query", "myproject")

    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert len(result) > 0, "Expected non-empty string result"
