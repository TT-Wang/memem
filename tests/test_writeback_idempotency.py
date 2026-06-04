"""Tests for H-5 v1.9.3: commit_deltas idempotency.

If the same (scope_id, dry_run, auto_only, deltas) tuple is committed
twice, the second call returns the first call's results tagged with
`deduped: True` instead of re-executing. Prevents double-writeback on
crash-and-resume.

Dry-run results are NOT cached (their purpose is to surface previews).
"""




def _make_delta(delta_id: str = "d1", title: str = "Add a rule") -> dict:
    """Minimal valid DeltaProposal shape for the policy evaluator."""
    return {
        "delta_id": delta_id,
        "delta_type": "save_new_memory",
        "rationale": title,
        "target_memory_id": "",
        "operation": "save",
        "proposed_content": {"title": title, "content": "body"},
    }


def test_op_hash_stable_across_dict_order(tmp_cortex_dir):
    """Identical deltas with differently-ordered dict keys must hash identically."""
    from memem.delta_commit import _writeback_op_hash
    d1 = [{"a": 1, "b": 2, "c": 3}]
    d2 = [{"c": 3, "b": 2, "a": 1}]
    assert _writeback_op_hash(d1, scope_id="s1", dry_run=False, auto_only=False) == \
           _writeback_op_hash(d2, scope_id="s1", dry_run=False, auto_only=False)


def test_op_hash_differs_on_scope(tmp_cortex_dir):
    """Different scopes must hash differently — they're separate cache lanes."""
    from memem.delta_commit import _writeback_op_hash
    d = [{"a": 1}]
    h1 = _writeback_op_hash(d, scope_id="s1", dry_run=False, auto_only=False)
    h2 = _writeback_op_hash(d, scope_id="s2", dry_run=False, auto_only=False)
    assert h1 != h2


def test_op_hash_differs_on_dry_run(tmp_cortex_dir):
    """Dry-run vs real-run must hash differently."""
    from memem.delta_commit import _writeback_op_hash
    d = [{"a": 1}]
    h_dry = _writeback_op_hash(d, scope_id="s1", dry_run=True, auto_only=False)
    h_real = _writeback_op_hash(d, scope_id="s1", dry_run=False, auto_only=False)
    assert h_dry != h_real


def test_idempotency_store_and_lookup_roundtrip(tmp_cortex_dir):
    """Store a result, look it up by same hash — must hit, tagged deduped."""
    from memem.delta_commit import (
        _writeback_idempotency_lookup,
        _writeback_idempotency_store,
    )
    results = [{"delta_id": "d1", "status": "committed"}]
    _writeback_idempotency_store("scope_a", "hashAAA", results, dry_run=False)
    hit = _writeback_idempotency_lookup("scope_a", "hashAAA")
    assert hit is not None
    assert hit[0]["delta_id"] == "d1"
    assert hit[0]["deduped"] is True


def test_idempotency_miss_on_different_hash(tmp_cortex_dir):
    """Different hash for the same scope must miss."""
    from memem.delta_commit import (
        _writeback_idempotency_lookup,
        _writeback_idempotency_store,
    )
    _writeback_idempotency_store("scope_a", "hashAAA", [{"x": 1}], dry_run=False)
    assert _writeback_idempotency_lookup("scope_a", "hashBBB") is None


def test_dry_run_results_not_cached(tmp_cortex_dir):
    """A dry-run store must be a no-op; subsequent lookup misses."""
    from memem.delta_commit import (
        _writeback_idempotency_lookup,
        _writeback_idempotency_store,
    )
    _writeback_idempotency_store("scope_a", "hashAAA", [{"x": 1}], dry_run=True)
    assert _writeback_idempotency_lookup("scope_a", "hashAAA") is None


def test_idempotency_separate_scopes_isolated(tmp_cortex_dir):
    """Cache for scope_a must not satisfy a lookup for scope_b."""
    from memem.delta_commit import (
        _writeback_idempotency_lookup,
        _writeback_idempotency_store,
    )
    _writeback_idempotency_store("scope_a", "shared_hash", [{"x": 1}], dry_run=False)
    assert _writeback_idempotency_lookup("scope_b", "shared_hash") is None


def test_idempotency_file_atomic_safe_after_corruption(tmp_cortex_dir):
    """A corrupted idempotency file must not crash the lookup — return None."""
    from memem.delta_commit import _idempotency_file_path, _writeback_idempotency_lookup

    path = _idempotency_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert _writeback_idempotency_lookup("scope_a", "any_hash") is None


def test_commit_deltas_dedupes_on_second_call(tmp_cortex_dir, tmp_vault):
    """End-to-end: calling commit_deltas twice with the same deltas returns
    the cached result on the second call, with deduped=True markers."""
    from memem.delta_commit import commit_deltas

    deltas = [_make_delta("d1", "First rule")]
    # First call: real execution (dry-run pass — no actual file writes needed
    # because the delta won't match a vault state, but the cache write path
    # still fires for non-dry-run, so we use auto_only to force the auto path).
    first = commit_deltas(deltas, scope_id="t1", dry_run=False, auto_only=True)
    assert all("deduped" not in r for r in first)

    # Second call with identical inputs: hits cache.
    second = commit_deltas(deltas, scope_id="t1", dry_run=False, auto_only=True)
    assert second  # has results
    assert all(r.get("deduped") is True for r in second), (
        f"second call must be deduped, got: {second}"
    )
