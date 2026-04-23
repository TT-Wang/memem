"""Golden ongoing-work scenarios for Active Memory Slice."""

from __future__ import annotations

import importlib


def test_golden_proposal_refinement_includes_artifacts_constraints_and_tensions(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="The management proposal must keep privacy and permission constraints visible.",
        title="Proposal privacy constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Decision: frame the proposal as an incremental rollout with concrete examples.",
        title="Proposal framing decision",
        project="memem",
        source_type="user",
        importance=4,
    ))
    proposal_path = tmp_vault / "proposal.md"
    proposal_path.write_text("# Proposal draft\n\nOpen tradeoff on rollout scope.\n")

    from memem.active_slice_engine import generate_active_memory_slice

    slice_obj = generate_active_memory_slice(
        "Refine the proposal draft for management review; the rollout scope is still unresolved.",
        scope_id="memem",
        environment={
            "task_mode": "proposal",
            "artifact_path": str(proposal_path),
            "stakeholder": "management",
            "branch": "proposal/refinement",
        },
        use_llm=False,
    )

    assert slice_obj["constraints"]
    assert slice_obj["decisions"]
    assert slice_obj["artifacts"]
    assert slice_obj["open_tensions"]


def test_golden_coding_task_includes_current_artifacts_and_failure_patterns(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Coding changes must run pytest and preserve auth constraints before completion.",
        title="Coding verification constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Failure pattern: avoid the prior auth regression in fallback handling.",
        title="Auth regression failure pattern",
        project="memem",
        source_type="user",
        importance=5,
    ))
    current_file = tmp_vault / "auth.py"
    current_file.write_text("def login():\n    return 'ok'\n")

    from memem.active_slice_engine import generate_active_memory_slice

    slice_obj = generate_active_memory_slice(
        "Continue coding the auth fix, keep pytest and auth constraints intact, and avoid the previous regression.",
        scope_id="memem",
        environment={
            "task_mode": "coding",
            "current_file": str(current_file),
            "modified_files": [str(current_file)],
            "branch": "fix/auth-fallback",
        },
        use_llm=False,
    )

    artifact_paths = {artifact.get("path", "") for artifact in slice_obj["artifacts"]}

    assert slice_obj["constraints"]
    assert slice_obj["failure_patterns"]
    assert str(current_file) in artifact_paths


def test_golden_debugging_task_surfaces_tensions_without_losing_artifacts(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Bug note: the auth refresh path can fail when fallback ordering regresses.",
        title="Auth refresh bug",
        project="memem",
        source_type="user",
        importance=5,
    ))
    log_file = tmp_vault / "error.log"
    log_file.write_text("ERROR auth refresh blocked by stale fallback ordering\n")

    from memem.active_slice_engine import generate_active_memory_slice

    slice_obj = generate_active_memory_slice(
        "Debug the auth file blocker; the fallback ownership is still unclear.",
        scope_id="memem",
        environment={
            "task_mode": "debug",
            "current_file": str(log_file),
            "branch": "debug/auth-blocker",
        },
        use_llm=False,
    )

    assert slice_obj["failure_patterns"]
    assert slice_obj["artifacts"]
    assert slice_obj["open_tensions"]


def test_golden_proposal_continuity_carries_forward_prior_slice(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="The proposal must keep privacy review constraints visible.",
        title="Proposal privacy review",
        project="memem",
        source_type="user",
        importance=5,
    ))
    proposal_path = tmp_vault / "proposal.md"
    proposal_path.write_text("# Proposal draft\n\nOpen approval questions.\n")

    from memem.active_slice_engine import generate_active_memory_slice

    environment = {
        "task_mode": "proposal",
        "session_id": "proposal-session",
        "artifact_path": str(proposal_path),
        "stakeholder": "management",
        "branch": "proposal/continuity",
    }
    first = generate_active_memory_slice(
        "Refine the proposal draft for management review.",
        scope_id="memem",
        environment=environment,
        use_llm=False,
    )
    proposal_path.write_text("# Proposal draft\n\nUpdated approval path and rollout notes.\n")
    second = generate_active_memory_slice(
        "Finalize the proposal draft for management review while keeping prior constraints visible.",
        scope_id="memem",
        environment=environment,
        use_llm=False,
    )

    assert second["previous_slice_id"] == first["slice_id"]
    assert second["carry_forward_summary"]
    assert second["artifact_progression"]["stage"] in {"drafting", "revising", "review_ready"}
    assert second["artifacts"]
