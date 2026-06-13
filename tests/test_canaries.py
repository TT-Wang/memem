"""Tests for adversarial canary memories (memem v2 m6).

Covers:
1. test_all_canaries_returns_10_total
2. test_plant_canaries_saves_all_to_vault
3. test_plant_canaries_idempotent
4. test_verify_canaries_intact_passes_after_planting
5. test_verify_detects_layer_drift_for_always_reachable
6. test_verify_detects_promotion_for_never_elevate
7. test_verify_passes_when_canary_missing_for_never_elevate
8. test_check_canaries_in_doctor_prints_canary_check_line (non-blocking advisory)
9. test_check_canaries_in_doctor_prints_warn_when_not_planted (empty vault)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Test 1: all_canaries returns 10 total (5 always + 5 never)
# ---------------------------------------------------------------------------


def test_all_canaries_returns_10_total():
    from memem.eval.canaries import ALWAYS_REACHABLE, NEVER_ELEVATE, all_canaries

    assert len(ALWAYS_REACHABLE) == 5, "Expected 5 always-reachable canaries"
    assert len(NEVER_ELEVATE) == 5, "Expected 5 never-elevate canaries"
    assert len(all_canaries()) == 10, "Expected 10 total canaries"


# ---------------------------------------------------------------------------
# Test 2: plant_canaries saves all 10 to vault
# ---------------------------------------------------------------------------


def test_plant_canaries_saves_all_to_vault(tmp_vault, tmp_cortex_dir):
    from memem.eval.canaries import plant_canaries
    from memem.obsidian_store import _find_memory

    planted_ids = plant_canaries()

    assert len(planted_ids) == 10, f"Expected 10 planted IDs, got {len(planted_ids)}"
    for mem_id in planted_ids:
        mem = _find_memory(mem_id)
        assert mem is not None, f"Planted canary {mem_id} not found in vault"


# ---------------------------------------------------------------------------
# Test 3: plant_canaries is idempotent (second call doesn't error)
# ---------------------------------------------------------------------------


def test_plant_canaries_idempotent(tmp_vault, tmp_cortex_dir):
    from memem.eval.canaries import plant_canaries

    first = plant_canaries()
    # Second call should not raise (dedup catches duplicates)
    second = plant_canaries()

    assert len(first) == 10, "First plant should return 10 IDs"
    # Second may return 0 (all deduped) or 10 (idempotent rewrite) — either OK as long as no error
    assert isinstance(second, list), "Second plant should return a list without raising"


# ---------------------------------------------------------------------------
# Test 4: verify_canaries_intact passes after planting
# ---------------------------------------------------------------------------


def test_verify_canaries_intact_passes_after_planting(tmp_vault, tmp_cortex_dir):
    from memem.eval.canaries import plant_canaries, verify_canaries_intact

    plant_canaries()
    result = verify_canaries_intact()

    assert result["pass"] is True, f"verify_canaries_intact should pass after planting: {result['errors']}"
    assert result["always_reachable_failures"] == [], "No always-reachable failures expected"
    assert result["never_elevate_failures"] == [], "No never-elevate failures expected"


# ---------------------------------------------------------------------------
# Test 5: verify detects layer drift for always-reachable canary
# ---------------------------------------------------------------------------


def test_verify_detects_layer_drift_for_always_reachable(tmp_vault, tmp_cortex_dir):
    from memem.eval.canaries import ALWAYS_REACHABLE, plant_canaries, verify_canaries_intact
    from memem.obsidian_store import _obsidian_memories, _write_obsidian_memory

    plant_canaries()

    # Manually drift the first always-reachable canary from L0 to L2
    target_title = ALWAYS_REACHABLE[0]["title"]
    target_canary_id = ALWAYS_REACHABLE[0]["canary_id"]

    all_mems = _obsidian_memories(scope_id="memem-canary")
    target_mem = next((m for m in all_mems if m.get("title") == target_title), None)
    assert target_mem is not None, f"Could not find canary '{target_title}' in vault"

    # Drift layer 0 → 2
    target_mem["layer"] = 2
    _write_obsidian_memory(target_mem)

    result = verify_canaries_intact()

    assert result["pass"] is False, "verify should fail after layer drift"
    assert target_canary_id in result["always_reachable_failures"], (
        f"Drifted canary {target_canary_id} should be in always_reachable_failures"
    )


# ---------------------------------------------------------------------------
# Test 6: verify detects promotion for never-elevate canary
# ---------------------------------------------------------------------------


def test_verify_detects_promotion_for_never_elevate(tmp_vault, tmp_cortex_dir):
    from memem.eval.canaries import NEVER_ELEVATE, plant_canaries, verify_canaries_intact
    from memem.obsidian_store import _obsidian_memories, _write_obsidian_memory

    plant_canaries()

    # Manually promote the first never-elevate canary from L3 to L0
    target_title = NEVER_ELEVATE[0]["title"]
    target_canary_id = NEVER_ELEVATE[0]["canary_id"]

    all_mems = _obsidian_memories(scope_id="memem-canary")
    target_mem = next((m for m in all_mems if m.get("title") == target_title), None)
    assert target_mem is not None, f"Could not find canary '{target_title}' in vault"

    # Promote layer 3 → 0 (simulate runaway dreamer)
    target_mem["layer"] = 0
    _write_obsidian_memory(target_mem)

    result = verify_canaries_intact()

    assert result["pass"] is False, "verify should fail after promotion"
    assert target_canary_id in result["never_elevate_failures"], (
        f"Promoted canary {target_canary_id} should be in never_elevate_failures"
    )


# ---------------------------------------------------------------------------
# Test 7: verify passes when never-elevate canary is missing (not planted)
# ---------------------------------------------------------------------------


def test_verify_passes_when_canary_missing_for_never_elevate(tmp_vault, tmp_cortex_dir):
    """Never-elevate canaries can be absent without triggering a failure.
    Only promotion (layer drift up) is a failure for this set.
    """
    from memem.eval.canaries import ALWAYS_REACHABLE, verify_canaries_intact
    from memem.obsidian_store import _make_memory, _save_memory

    # Plant only the always-reachable canaries (not never-elevate)
    for c in ALWAYS_REACHABLE:
        mem = _make_memory(
            content=c["content"],
            title=c["title"],
            project=c["project"],
            source_type="user",
            layer=c["expected_layer"],
            tags=["canary", "memem-test"],
        )
        mem["decay_immune"] = True
        _save_memory(mem)

    result = verify_canaries_intact()

    assert result["pass"] is True, (
        f"verify should pass when never-elevate canaries are absent: {result['errors']}"
    )
    assert result["never_elevate_failures"] == [], "Absent never-elevate canaries must not cause failure"


# ---------------------------------------------------------------------------
# Test 8: check_canaries_in_doctor prints a "Canary check" line (non-blocking)
# ---------------------------------------------------------------------------


def test_check_canaries_in_doctor_prints_canary_check_line(
    tmp_vault, tmp_cortex_dir, capsys, monkeypatch
):
    """check_canaries_in_doctor() must always print 'Canary check: N/5 pass' line.

    TEST ISOLATION: retrieve() is monkeypatched to avoid scanning the real
    vault or real session dirs. The monkeypatched retrieve returns empty
    results (WARN path) which is fine — we only need the 'Canary check' line.
    """
    import importlib
    import memem.cli as cli_module
    import memem.retrieve as retrieve_mod

    importlib.reload(cli_module)

    def _fake_retrieve(query, k=8, log_call_type=None, scope_id="", writeback=True, **kwargs):
        return []

    monkeypatch.setattr(retrieve_mod, "retrieve", _fake_retrieve)

    cli_module.check_canaries_in_doctor()
    captured = capsys.readouterr()
    assert "Canary check" in captured.out, (
        f"Expected 'Canary check' in output, got: {captured.out!r}"
    )
    # Non-blocking: no SystemExit raised, function returns normally


def test_check_canaries_in_doctor_prints_warn_when_not_planted(
    tmp_vault, tmp_cortex_dir, capsys, monkeypatch
):
    """When canaries are not planted, each should print WARN (not raise)."""
    import importlib
    import memem.cli as cli_module
    import memem.retrieve as retrieve_mod

    importlib.reload(cli_module)

    # retrieve() returns empty — no canaries in index
    def _fake_retrieve(query, k=8, log_call_type=None, scope_id="", writeback=True, **kwargs):
        return []

    monkeypatch.setattr(retrieve_mod, "retrieve", _fake_retrieve)

    # Should not raise even though all canaries are missing
    cli_module.check_canaries_in_doctor()
    captured = capsys.readouterr()

    # Must print WARN for each canary and a summary line
    assert "WARN" in captured.out or "Canary check" in captured.out
    assert "Canary check: 0/5 pass" in captured.out
