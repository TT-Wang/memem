"""Runtime tests for slice-first prompt context generation."""

from __future__ import annotations

import importlib


def test_generate_prompt_context_slice_mode(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="The release proposal must preserve rollout constraints and approval steps.",
        title="Release proposal constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))

    from memem.active_slice_engine import generate_prompt_context

    rendered = generate_prompt_context(
        "Continue the release proposal",
        scope_id="memem",
        use_llm=False,
    )

    assert "# Active Memory Slice" in rendered
    assert "## Goals" in rendered
    assert "## Constraints" in rendered


def test_generate_prompt_context_assembly_mode(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import assembly, obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="The API migration proposal must keep the fallback path documented.",
        title="API migration constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))
    monkeypatch.setattr(assembly, "context_assemble", lambda query, project="default": f"assembled::{project}::{query}")

    from memem.active_slice_engine import generate_prompt_context

    rendered = generate_prompt_context(
        "Continue the API migration proposal",
        scope_id="memem",
        use_llm=False,
        mode="assembly",
    )

    assert rendered == "assembled::memem::Continue the API migration proposal"


def test_slice_fallback_to_heuristic_when_llm_unavailable(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import active_slice_engine, obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="The bug fix must preserve the login constraint path.",
        title="Login constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))
    def _fail_llm(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(active_slice_engine, "judge_activation_with_llm", _fail_llm)

    slice_obj = active_slice_engine.generate_active_memory_slice(
        "Fix the login bug",
        scope_id="memem",
        use_llm=True,
    )

    assert slice_obj["activation_mode"] == "heuristic"
    assert any("used heuristic activation" in warning for warning in slice_obj["warnings"])


def test_slice_metrics_summary():
    from memem.active_slice_engine import summarize_slice_metrics

    metrics = summarize_slice_metrics({
        "slice_id": "slice_123",
        "scope_id": "memem",
        "query": "Continue implementation",
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
        "excluded_candidates": [{"candidate_id": "x", "reason": "duplicate"}],
        "candidate_deltas": [{"delta_type": "promote", "reason": "new constraint"}],
        "should_emit_context": True,
        "confidence": 0.78,
        "warnings": ["LLM activation disabled; used heuristic activation."],
    })

    assert metrics["goals_count"] == 1
    assert metrics["constraints_count"] == 1
    assert metrics["artifacts_count"] == 1
    assert metrics["warnings_count"] == 1
    assert metrics["should_emit_context"] is True


def test_generate_active_memory_slice_with_writeback_defaults_to_dry_run(tmp_vault, tmp_cortex_dir):
    from memem import active_slice_engine, obsidian_store

    importlib.reload(obsidian_store)
    first = obsidian_store._make_memory(
        content="Constraint: keep rollout safety checks visible.",
        title="Rollout safety constraint",
        project="memem",
        source_type="user",
        importance=5,
    )
    second = obsidian_store._make_memory(
        content="Failure pattern: avoid the prior rollout regression.",
        title="Rollout regression failure",
        project="memem",
        source_type="user",
        importance=5,
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    before_first = list(obsidian_store._find_memory(first["id"]).get("related", []))
    before_second = list(obsidian_store._find_memory(second["id"]).get("related", []))

    result = active_slice_engine.generate_active_memory_slice_with_writeback(
        "Continue the rollout work, keep the safety constraint, and avoid the previous regression.",
        scope_id="memem",
        environment={"task_mode": "coding"},
        use_llm=False,
    )

    after_first = list(obsidian_store._find_memory(first["id"]).get("related", []))
    after_second = list(obsidian_store._find_memory(second["id"]).get("related", []))

    assert result["slice"]["writeback_summary"]["dry_run"] is True
    assert result["delta_results"]
    assert after_first == before_first
    assert after_second == before_second
