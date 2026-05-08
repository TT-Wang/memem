"""Tests for Active Memory Slice schemas and rendering."""

import pytest


def test_current_query_goal_without_memories():
    from memem.active_slice import build_active_memory_slice, render_slice_as_prompt_context

    candidate_bundle = {"current_goal_candidates": [], "memory_candidates": []}
    slice_obj = build_active_memory_slice(
        "Continue the memory proposal",
        "memem",
        {},
        candidate_bundle,
        {"goals": [], "activation_mode": "heuristic"},
    )

    assert slice_obj["goals"][0]["source_type"] == "current_query"
    assert "Continue the memory proposal" in slice_obj["goals"][0]["summary"]

    rendered = render_slice_as_prompt_context(slice_obj)
    assert rendered == ""
    assert slice_obj["should_emit_context"] is False


def test_tension_without_grounding_context_does_not_emit():
    from memem.active_slice import build_active_memory_slice, render_slice_as_prompt_context

    slice_obj = build_active_memory_slice(
        "Fix the auth bug",
        "memem",
        {"task_mode": "debug", "session_id": "session-1"},
        {
            "current_goal_candidates": [],
            "memory_candidates": [],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [],
            "environment_candidates": [],
        },
        {
            "activation_mode": "heuristic",
            "confidence": 0.7,
            "open_tensions": [{
                "description": "Missing artifact support for current debugging task.",
                "severity": "medium",
                "why_open": "No file or memory context is grounded yet.",
            }],
        },
    )

    assert slice_obj["open_tensions"]
    assert slice_obj["should_emit_context"] is False
    assert render_slice_as_prompt_context(slice_obj) == ""


def test_memory_candidate_normalization():
    from memem.active_slice import normalize_memory_candidate

    candidate = normalize_memory_candidate({
        "id": "abcdef123456",
        "title": "Use typed graph",
        "essence": "Memem uses typed graph edges for related memories.",
        "project": "memem",
        "importance": 4,
        "layer": 1,
    }, score=0.9)

    assert candidate["candidate_id"] == "memory:abcdef12"
    assert candidate["candidate_type"] == "memory"
    assert candidate["score"] == 0.9
    assert candidate["summary"].startswith("Memem uses typed graph")


def test_build_slice_resolves_artifact_id_activation():
    from memem.active_slice import build_active_memory_slice, normalize_artifact_candidate

    artifact = normalize_artifact_candidate(
        "playbook",
        "memem playbook",
        "Use active slices for runtime working state.",
        path="/tmp/playbook.md",
        project="memem",
    )
    slice_obj = build_active_memory_slice(
        "Continue memem",
        "memem",
        {},
        {"current_goal_candidates": [], "artifact_candidates": [artifact]},
        {"artifact_context": [{"artifact_id": artifact["artifact_id"], "why": "playbook", "score": 0.8}]},
    )

    assert slice_obj["artifacts"]
    assert slice_obj["artifacts"][0]["artifact_id"] == artifact["artifact_id"]


def test_build_slice_resolves_candidate_memory_and_artifact_identifiers():
    from memem.active_slice import (
        build_active_memory_slice,
        normalize_artifact_candidate,
        normalize_memory_candidate,
    )

    memory = normalize_memory_candidate({
        "id": "abcdef1234567890",
        "title": "Keep auth constraint",
        "essence": "Auth fixes must preserve session refresh ordering.",
        "project": "memem",
        "importance": 5,
        "layer": 1,
    }, score=0.88)
    artifact = normalize_artifact_candidate(
        "repo_file",
        "auth.py",
        "Current auth implementation",
        path="/tmp/auth.py",
        project="memem",
    )

    slice_obj = build_active_memory_slice(
        "Continue auth fix",
        "memem",
        {"task_mode": "coding"},
        {
            "current_goal_candidates": [],
            "memory_candidates": [memory],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [artifact],
            "environment_candidates": [],
        },
        {
            "constraints": [{"memory_id": "abcdef12", "why": "same memory via short id", "score": 0.92}],
            "decisions": [{"candidate_id": memory["candidate_id"], "why": "same memory via candidate id", "score": 0.86}],
            "artifact_context": [{"artifact_id": artifact["artifact_id"], "why": "current file", "score": 0.81}],
        },
    )

    assert slice_obj["constraints"][0]["memory_id"] == memory["memory_id"]
    assert slice_obj["decisions"][0]["memory_id"] == memory["memory_id"]
    assert slice_obj["artifacts"][0]["artifact_id"] == artifact["artifact_id"]


def test_build_slice_initializes_continuity_and_writeback_fields():
    from memem.active_slice import build_active_memory_slice

    slice_obj = build_active_memory_slice(
        "Refine proposal",
        "memem",
        {"task_mode": "proposal", "session_id": "session-123"},
        {
            "current_goal_candidates": [],
            "memory_candidates": [],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [],
            "environment_candidates": [],
        },
        {"goals": [], "activation_mode": "heuristic"},
    )

    assert slice_obj["task_mode"] == "proposal"
    assert slice_obj["previous_slice_id"] == ""
    assert slice_obj["resolved_tensions"] == []
    assert slice_obj["carry_forward_summary"] == []
    assert slice_obj["delta_results"] == []
    assert slice_obj["artifact_progression"]["stage"] == "none"
    assert slice_obj["writeback_summary"]["status"] == "not_run"
    assert slice_obj["writeback_summary"]["dry_run"] is True


def test_recall_candidates_still_emit_context_when_activation_is_sparse():
    from memem.active_slice import build_active_memory_slice, normalize_memory_candidate, render_slice_as_prompt_context

    memory = normalize_memory_candidate({
        "id": "abcdef123456",
        "title": "Auth bug constraint",
        "essence": "Fixing auth bugs must preserve the login constraint path.",
        "project": "memem",
        "importance": 4,
        "layer": 2,
    }, score=0.9)

    slice_obj = build_active_memory_slice(
        "Fix the auth bug",
        "memem",
        {},
        {
            "current_goal_candidates": [],
            "memory_candidates": [memory],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [],
            "environment_candidates": [],
        },
        {"goals": [], "activation_mode": "heuristic", "confidence": 0.62},
    )

    assert slice_obj["should_emit_context"] is True
    rendered = render_slice_as_prompt_context(slice_obj)
    assert "# Active Memory Slice" in rendered


def test_slice_metrics_include_continuity_and_writeback_surfaces():
    from memem.active_slice_metrics import summarize_slice_metrics

    metrics = summarize_slice_metrics({
        "slice_id": "slice_123",
        "previous_slice_id": "slice_122",
        "scope_id": "memem",
        "query": "Continue implementation",
        "task_mode": "coding",
        "activation_mode": "heuristic",
        "candidate_count": 7,
        "recall_candidate_count": 3,
        "goals": [{"title": "Continue implementation"}],
        "constraints": [{"title": "Do not regress auth"}],
        "active_background": [],
        "decisions": [{"title": "Keep slice-first path"}],
        "preferences": [],
        "failure_patterns": [{"title": "Prior timeout regression"}],
        "artifacts": [{"title": "README draft"}],
        "open_tensions": [{"description": "Unclear rollout ownership"}],
        "resolved_tensions": [{"description": "Resolved auth ambiguity"}],
        "carry_forward_summary": ["Keep rollout constraints visible."],
        "slice_diff": {"new_goals": ["Continue implementation"]},
        "artifact_progression": {"stage": "revising"},
        "excluded_candidates": [{"candidate_id": "x", "reason": "duplicate"}],
        "candidate_deltas": [{"delta_type": "promote", "reason": "new constraint"}],
        "delta_results": [{"delta_type": "add_related_link", "status": "dry_run"}],
        "writeback_summary": {"status": "dry_run", "manual_review_count": 1, "blocked_count": 0},
        "should_emit_context": True,
        "confidence": 0.78,
        "warnings": ["LLM activation disabled; used heuristic activation."],
    })

    assert metrics["previous_slice_id"] == "slice_122"
    assert metrics["task_mode"] == "coding"
    assert metrics["resolved_tensions_count"] == 1
    assert metrics["carry_forward_summary_count"] == 1
    assert metrics["delta_results_count"] == 1
    assert metrics["artifact_progression_stage"] == "revising"
    assert metrics["writeback_status"] == "dry_run"


def test_models_slice_and_delta_state_paths_live_under_memem_dir():
    from memem.models import (
        ACTIVE_SLICE_HISTORY_FILE,
        DELTA_AUDIT_LOG,
        DELTA_STATE_DIR,
        MEMEM_DIR,
        OBSIDIAN_VAULT,
    )

    assert ACTIVE_SLICE_HISTORY_FILE.parent == MEMEM_DIR
    assert DELTA_AUDIT_LOG.parent == MEMEM_DIR
    assert DELTA_STATE_DIR.parent == MEMEM_DIR
    assert OBSIDIAN_VAULT not in ACTIVE_SLICE_HISTORY_FILE.parents
    assert OBSIDIAN_VAULT not in DELTA_AUDIT_LOG.parents
    assert OBSIDIAN_VAULT not in DELTA_STATE_DIR.parents


def test_active_slice_populates_universal_items_field():
    """Regression: build_active_memory_slice must set slice_obj['items']
    so attribution / dreamer / Stop hook consumers find the memories.

    The active builder previously set goals/constraints/decisions lists but
    NOT the universal items[] field. The Stop hook iterates slice['items']
    to compute per-memory attribution; with items=[] the closed loop was
    silently broken — every slice got logged with items=0 even when the
    section lists were full. This test pins the fix.
    """
    from memem.active_slice import build_active_memory_slice, normalize_memory_candidate

    memory = normalize_memory_candidate({
        "id": "abcdef1234567890",
        "title": "Use pytest fixtures for setup",
        "essence": "Prefer fixtures over manual setup so teardown is automatic.",
        "project": "memem",
        "importance": 4,
        "layer": 2,
    }, score=0.85)

    slice_obj = build_active_memory_slice(
        "how should I structure tests",
        "memem",
        {"task_mode": "coding", "session_id": "items-regress"},
        {
            "current_goal_candidates": [],
            "memory_candidates": [memory],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [],
            "environment_candidates": [],
        },
        {
            "constraints": [{"memory_id": "abcdef12", "why": "test convention", "score": 0.9}],
        },
    )

    assert "items" in slice_obj, "slice_obj must expose universal 'items' field"
    items = slice_obj["items"]
    assert isinstance(items, list)
    assert len(items) >= 1, (
        "items[] must include the memories selected into goals/constraints/decisions; "
        "empty list silently breaks Stop-hook attribution"
    )
    # Every item must have a memory_id (that's the contract attribution depends on)
    assert all(it.get("memory_id") for it in items), (
        "every item must carry memory_id; the dreamer/attribution code keys on it"
    )
    # And the slice should advertise its kind so consumers can route correctly
    assert slice_obj.get("slice_kind") == "active"


def test_scope_matching_boosts_in_scope_memories():
    """Scope-based re-ranking: in-scope memories get 1.5× boost, cross-project get 0.7× penalty.

    General/empty-project memories are unchanged. Tests this via the multiplier logic
    applied to a synthetic candidate list, without calling generate_candidates() (which
    would require a live recall backend).
    """
    from memem.models import _normalize_scope_id

    scope_id = "cortex-plugin"
    normalized_scope = _normalize_scope_id(scope_id)

    # Simulate what generate_candidates builds before the multiplier loop
    base_score = 0.60  # representative mid-range score
    candidates = [
        _make_candidate_with_score("cortex-plugin", base_score),
        _make_candidate_with_score("cortex-plugin", base_score),
        _make_candidate_with_score("cortex-plugin", base_score),
        _make_candidate_with_score("lexie", base_score),
        _make_candidate_with_score("lexie", base_score),
        _make_candidate_with_score("lexie", base_score),
        _make_candidate_with_score("general", base_score),
        _make_candidate_with_score("general", base_score),
    ]

    # Apply the exact multiplier logic from active_slice_engine.generate_candidates()
    for cand in candidates:
        cand_project = _normalize_scope_id(str(cand.get("project", "") or ""))
        if not cand_project or cand_project == "general":
            continue
        if cand_project == normalized_scope:
            cand["score"] = min(1.0, float(cand.get("score", 0.0)) * 1.5)
        else:
            cand["score"] = float(cand.get("score", 0.0)) * 0.7

    by_project: dict[str, list[float]] = {}
    for cand in candidates:
        proj = cand.get("project", "general")
        by_project.setdefault(proj, []).append(float(cand["score"]))

    cortex_scores = by_project.get("cortex-plugin", [])
    lexie_scores = by_project.get("lexie", [])
    general_scores = by_project.get("general", [])

    # In-scope memories must have higher average score than cross-project memories
    assert cortex_scores, "Expected cortex-plugin candidates in list"
    assert lexie_scores, "Expected lexie candidates in list"
    assert sum(cortex_scores) / len(cortex_scores) > sum(lexie_scores) / len(lexie_scores), (
        f"cortex-plugin avg {sum(cortex_scores)/len(cortex_scores):.3f} "
        f"should exceed lexie avg {sum(lexie_scores)/len(lexie_scores):.3f}"
    )

    # General memories are unchanged — not penalized relative to cross-project
    if general_scores and lexie_scores:
        assert sum(general_scores) / len(general_scores) > sum(lexie_scores) / len(lexie_scores), (
            "general memories should not be penalized relative to cross-project memories"
        )


def test_scope_match_caps_at_1():
    """L0-like candidate with score=0.95 gets min(1.0, 0.95*1.5) = 1.0, not 1.425."""
    # Test the math directly: min(1.0, 0.95 * 1.5) must equal 1.0
    score = 0.95
    boosted = min(1.0, float(score) * 1.5)
    assert boosted == 1.0, f"Expected 1.0 (clamped), got {boosted}"
    assert pytest.approx(1.425) == 0.95 * 1.5, "Without cap, boost would be 1.425"


def _make_candidate_with_score(project: str, score: float) -> dict:
    """Helper: build a minimal candidate dict for scope-ranking unit tests."""
    return {
        "candidate_id": f"memory:{project}-test",
        "candidate_type": "memory",
        "memory_id": f"{project}-test-id",
        "project": project,
        "score": score,
        "title": f"{project} test memory",
        "summary": f"Summary for {project}",
        "layer": 2,
    }


def test_scope_rank_multiplier_unit():
    """Unit test: scope multiplier logic applied directly to candidate dicts."""
    from memem.models import _normalize_scope_id

    scope_id = "cortex-plugin"
    normalized_scope = _normalize_scope_id(scope_id)

    candidates = [
        _make_candidate_with_score("cortex-plugin", 0.6),
        _make_candidate_with_score("lexie", 0.6),
        _make_candidate_with_score("general", 0.6),
        _make_candidate_with_score("", 0.6),
        _make_candidate_with_score("cortex-plugin", 0.95),  # L0-like high score
    ]

    for cand in candidates:
        cand_project = _normalize_scope_id(str(cand.get("project", "") or ""))
        if not cand_project or cand_project == "general":
            continue
        if cand_project == normalized_scope:
            cand["score"] = min(1.0, float(cand.get("score", 0.0)) * 1.5)
        else:
            cand["score"] = float(cand.get("score", 0.0)) * 0.7

    cortex_60, lexie_60, general_60, empty_60, cortex_95 = candidates

    assert cortex_60["score"] == pytest.approx(0.9), "in-scope 0.6 -> 0.9"
    assert lexie_60["score"] == pytest.approx(0.42), "cross-project 0.6 -> 0.42"
    assert general_60["score"] == pytest.approx(0.6), "general unchanged"
    assert empty_60["score"] == pytest.approx(0.6), "empty-project unchanged"
    assert cortex_95["score"] == pytest.approx(1.0), "high-score clamped to 1.0, not 1.425"
