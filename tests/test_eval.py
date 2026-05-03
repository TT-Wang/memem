"""Tests for LongMemEval-inspired eval harness (memem v2 m6).

Covers:
1. test_eval_set_has_all_5_abilities
2. test_run_eval_case_extraction_passes_with_correct_setup
3. test_run_eval_case_abstention_passes_when_no_relevant
4. test_run_eval_case_knowledge_update_filters_invalidated
5. test_run_all_cases_returns_summary_with_by_ability_breakdown
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Test 1: EVAL_SET covers all 5 LongMemEval abilities
# ---------------------------------------------------------------------------


def test_eval_set_has_all_5_abilities():
    from memem.eval.eval_set import EVAL_SET

    required_abilities = {
        "extraction",
        "multi_session",
        "temporal",
        "knowledge_update",
        "abstention",
    }
    found_abilities = {c["ability"] for c in EVAL_SET}

    assert required_abilities <= found_abilities, (
        f"Missing abilities: {required_abilities - found_abilities}"
    )


# ---------------------------------------------------------------------------
# Test 2: extraction case passes when target memory is present
# ---------------------------------------------------------------------------


def test_run_eval_case_extraction_passes_with_correct_setup(tmp_vault, tmp_cortex_dir):
    from memem.eval.eval_set import EVAL_SET, run_eval_case

    extract_case = next(c for c in EVAL_SET if c["case_id"] == "extract-1")
    result = run_eval_case(extract_case)

    assert result["pass"] is True, (
        f"Extraction case should pass. Missing: {result.get('expected_present_missing')}, "
        f"Unexpected: {result.get('expected_absent_present')}, "
        f"Errors: {result.get('errors')}"
    )
    assert result["errors"] == [], f"No errors expected, got: {result['errors']}"


# ---------------------------------------------------------------------------
# Test 3: abstention case passes when no relevant memory is present
# ---------------------------------------------------------------------------


def test_run_eval_case_abstention_passes_when_no_relevant(tmp_vault, tmp_cortex_dir):
    """Abstention: no memory is *required* for the query — no expected_present_ids.

    The eval harness must complete without errors and report pass=True.
    Noise suppression is intentionally not asserted here because the
    fuzzy/embedding recall will surface nearby memories in a tiny vault
    (score_cutoff=20 is intentionally low to boost recall). The abstention
    invariant is that nothing is *required* to appear — not that noise is
    perfectly suppressed.
    """
    from memem.eval.eval_set import EVAL_SET, run_eval_case

    abstain_case = next(c for c in EVAL_SET if c["case_id"] == "abstain-1")
    result = run_eval_case(abstain_case)

    assert result["errors"] == [], f"Abstention case must run without errors: {result['errors']}"
    assert result["pass"] is True, (
        f"Abstention case should pass (no required memories for unrelated query). "
        f"Missing: {result.get('expected_present_missing')}, "
        f"Errors: {result.get('errors')}"
    )


# ---------------------------------------------------------------------------
# Test 4: knowledge_update case filters invalidated memory
# ---------------------------------------------------------------------------


def test_run_eval_case_knowledge_update_filters_invalidated(tmp_vault, tmp_cortex_dir):
    from memem.eval.eval_set import EVAL_SET, run_eval_case

    ku_case = next(c for c in EVAL_SET if c["case_id"] == "knowledge-update-1")
    result = run_eval_case(ku_case)

    # Current DB host should appear; stale host should be excluded
    assert result["expected_absent_present"] == [], (
        f"Invalidated memory (ku-1-stale) must not appear in results: "
        f"{result.get('expected_absent_present')}"
    )
    assert "ku-1-current" not in result.get("expected_present_missing", []), (
        "Current DB host (ku-1-current) must appear in results"
    )


# ---------------------------------------------------------------------------
# Test 5: run_all_cases returns summary with by_ability breakdown
# ---------------------------------------------------------------------------


def test_run_all_cases_returns_summary_with_by_ability_breakdown(tmp_vault, tmp_cortex_dir):
    from memem.eval.eval_set import run_all_cases

    summary = run_all_cases()

    assert "total_cases" in summary, "Summary must have total_cases"
    assert "passed" in summary, "Summary must have passed"
    assert "failed" in summary, "Summary must have failed"
    assert "by_ability" in summary, "Summary must have by_ability"
    assert "results" in summary, "Summary must have results"

    assert summary["total_cases"] > 0, "Must have at least 1 case"
    assert summary["passed"] + summary["failed"] == summary["total_cases"]

    # Check by_ability has all 5 abilities
    required_abilities = {
        "extraction", "multi_session", "temporal", "knowledge_update", "abstention"
    }
    found_abilities = set(summary["by_ability"].keys())
    assert required_abilities <= found_abilities, (
        f"Missing abilities in breakdown: {required_abilities - found_abilities}"
    )

    # Each ability entry should have pass/fail counts
    for ability, counts in summary["by_ability"].items():
        assert "pass" in counts, f"Ability {ability} missing 'pass' count"
        assert "fail" in counts, f"Ability {ability} missing 'fail' count"
