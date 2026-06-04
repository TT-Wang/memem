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


def test_commit_deltas_returns_cache_when_hash_matches(tmp_cortex_dir, tmp_vault):
    """End-to-end: when the cache holds a committed result for this scope and
    the new commit_deltas call hashes identically, the cached list is returned
    with deduped=True markers — re-execution is skipped.

    We seed the cache directly (rather than relying on a real commit to land
    a 'committed' status — which requires a vault state we don't have here)
    so the test focuses on the lookup-and-return path.
    """
    from memem.delta_commit import (
        _scoped_delta,
        _writeback_idempotency_store,
        _writeback_op_hash,
        commit_deltas,
    )

    deltas = [_make_delta("d1", "First rule")]
    scoped = [_scoped_delta(d, "t1") for d in deltas]
    op_hash = _writeback_op_hash(scoped, scope_id="t1", dry_run=False, auto_only=True)

    cached_committed = [{"delta_id": "d1", "status": "committed"}]
    _writeback_idempotency_store("t1", op_hash, cached_committed, dry_run=False)

    result = commit_deltas(deltas, scope_id="t1", dry_run=False, auto_only=True)
    assert result == [{"delta_id": "d1", "status": "committed", "deduped": True}]


def test_force_writeback_bypasses_cache_and_executes(tmp_cortex_dir, tmp_vault):
    """v1.9.4: force_writeback=True must skip the cache lookup even when a
    matching cached entry exists. Result is whatever the real executor
    returns, not the cached value."""
    from memem.delta_commit import (
        _scoped_delta,
        _writeback_idempotency_store,
        _writeback_op_hash,
        commit_deltas,
    )

    deltas = [_make_delta("d1", "First rule")]
    scoped = [_scoped_delta(d, "t1") for d in deltas]
    op_hash = _writeback_op_hash(scoped, scope_id="t1", dry_run=False, auto_only=True)

    cached = [{"delta_id": "d1", "status": "committed", "marker": "from_cache"}]
    _writeback_idempotency_store("t1", op_hash, cached, dry_run=False)

    forced = commit_deltas(
        deltas, scope_id="t1", dry_run=False, auto_only=True, force_writeback=True
    )
    assert forced
    assert all(r.get("marker") != "from_cache" for r in forced), (
        "force_writeback must skip cache lookup; got cached marker"
    )
    assert all("deduped" not in r for r in forced)


def test_partial_failure_batch_not_cached(tmp_cortex_dir):
    """v1.9.4: a batch where any result is non-committed must NOT enter the
    cache. Re-running with the same inputs must produce a fresh execution
    (no deduped markers) so transient failures get retried."""
    from memem.delta_commit import (
        _writeback_idempotency_lookup,
        _writeback_idempotency_store,
    )
    results = [
        {"delta_id": "d1", "status": "committed"},
        {"delta_id": "d2", "status": "blocked"},  # partial failure
    ]
    _writeback_idempotency_store("scope_a", "hashAAA", results, dry_run=False)
    assert _writeback_idempotency_lookup("scope_a", "hashAAA") is None, (
        "partial-failure batch must not be cached"
    )


def test_writeback_version_in_hash(tmp_cortex_dir):
    """v1.9.4: hash must use DELTA_WRITEBACK_VERSION, not MINER_STATE_VERSION.
    Bumping DELTA_WRITEBACK_VERSION must change the hash; bumping
    MINER_STATE_VERSION must NOT (they are orthogonal version concerns).
    """
    import memem.delta_commit as dc

    deltas = [{"a": 1}]
    h_before = dc._writeback_op_hash(deltas, scope_id="s1", dry_run=False, auto_only=False)
    original = dc.DELTA_WRITEBACK_VERSION
    try:
        dc.DELTA_WRITEBACK_VERSION = "9999"
        h_after = dc._writeback_op_hash(deltas, scope_id="s1", dry_run=False, auto_only=False)
    finally:
        dc.DELTA_WRITEBACK_VERSION = original
    assert h_before != h_after, "DELTA_WRITEBACK_VERSION must participate in the hash"
