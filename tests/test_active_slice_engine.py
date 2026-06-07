"""Tests for Active Memory Slice Engine orchestration."""

import importlib


def test_generate_active_memory_slice_zero_memory_has_goal(tmp_vault, tmp_cortex_dir):
    from memem.active_slice_engine import generate_active_memory_slice

    slice_obj = generate_active_memory_slice(
        "Continue management proposal",
        scope_id="memem",
        environment={"cwd": "/repo"},
        use_llm=False,
    )

    assert slice_obj["goals"]
    assert slice_obj["goals"][0]["source_type"] == "current_query"
    assert slice_obj["activation_mode"] == "heuristic"


def test_generate_active_memory_slice_uses_constraints_and_no_writeback(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="The proposal must address privacy constraints and permission risks for management review.",
        title="Proposal privacy constraints",
        project="memem",
        source_type="user",
        importance=5,
    )
    obsidian_store._save_memory(mem)
    before_files = set((tmp_vault / "memem" / "memories").glob("*.md"))

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice(
        "Continue the management proposal privacy section",
        scope_id="memem",
        use_llm=False,
    )

    after_files = set((tmp_vault / "memem" / "memories").glob("*.md"))
    assert before_files == after_files
    assert slice_obj["constraints"]
    assert "privacy" in slice_obj["constraints"][0]["summary"].lower()


def test_deprecated_memory_not_in_slice(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="Old deprecated design should not enter normal active slices.",
        title="Deprecated design",
        project="memem",
        source_type="user",
    )
    obsidian_store._save_memory(mem)
    obsidian_store._deprecate_memory(mem["id"])

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice("deprecated design", scope_id="memem", use_llm=False)
    rendered_ids = str(slice_obj)
    assert mem["id"] not in rendered_ids


def test_rendered_active_slice_empty_without_recall_candidates(tmp_vault, tmp_cortex_dir):
    from memem.active_slice_engine import active_slice_response

    rendered = active_slice_response("Prepare project review", scope_id="memem", use_llm=False)
    assert rendered == ""


def test_rendered_active_slice_contains_expected_sections_when_memory_matches(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Project review must include concrete risks and next steps.",
        title="Project review constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))

    from memem.active_slice_engine import active_slice_response
    rendered = active_slice_response("Prepare project review risks", scope_id="memem", use_llm=False)
    assert "# Active Memory Slice" in rendered
    assert "## Anchors" in rendered
    assert "## Skills" in rendered
    assert "## Warnings" not in rendered


def test_golden_product_proposal_continuation(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    memories = [
        obsidian_store._make_memory(
            content="The AI memory feature proposal must address privacy constraints and permission review.",
            title="Proposal privacy constraint",
            project="memem",
            source_type="user",
            importance=5,
        ),
        obsidian_store._make_memory(
            content="Decision: keep the management review proposal in MVP framing with concrete examples.",
            title="MVP proposal framing",
            project="memem",
            source_type="user",
            importance=4,
        ),
        obsidian_store._make_memory(
            content="Open tension: feature definition is still fuzzy for management-facing language.",
            title="Proposal fuzzy definition",
            project="memem",
            source_type="user",
            importance=4,
        ),
    ]
    for mem in memories:
        obsidian_store._save_memory(mem)

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice(
        "Continue preparing the AI memory feature proposal for management review",
        scope_id="memem",
        use_llm=False,
    )

    assert slice_obj["goals"]
    assert slice_obj["constraints"]
    assert slice_obj["decisions"]
    assert slice_obj["open_tensions"]


def test_golden_coding_task_continuation(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    for content, title in [
        ("Coding changes must run pytest and ruff before completion.", "Coding verification constraint"),
        ("Failure pattern: previous graph rebuild used unbounded pairwise scoring and timed out.", "Graph rebuild failure pattern"),
    ]:
        obsidian_store._save_memory(obsidian_store._make_memory(
            content=content,
            title=title,
            project="memem",
            source_type="user",
            importance=5,
        ))

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice(
        "Continue coding active slice engine, run pytest ruff, and avoid graph rebuild failure",
        scope_id="memem",
        use_llm=False,
    )

    assert slice_obj["constraints"]
    assert slice_obj["failure_patterns"]


def test_golden_project_review_deprecated_suppression(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    active = obsidian_store._make_memory(
        content="Project review must surface unresolved risks and concrete next steps.",
        title="Project review constraint",
        project="memem",
        source_type="user",
    )
    deprecated = obsidian_store._make_memory(
        content="Deprecated review plan should not enter active slices.",
        title="Deprecated review plan",
        project="memem",
        source_type="user",
    )
    obsidian_store._save_memory(active)
    obsidian_store._save_memory(deprecated)
    obsidian_store._deprecate_memory(deprecated["id"])

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice("Prepare project review", scope_id="memem", use_llm=False)

    assert active["id"] in str(slice_obj)
    assert deprecated["id"] not in str(slice_obj)


def test_generate_candidates_uses_normalized_environment_fields(tmp_path):
    from memem.active_slice_engine import generate_candidates

    current_file = tmp_path / "proposal.md"
    current_file.write_text("# Proposal\n\nOpen rollout questions.\n")

    bundle = generate_candidates(
        "Continue the proposal",
        "memem",
        environment={
            "task_mode": "proposal",
            "repo_path": str(tmp_path),
            "current_file": str(current_file),
            "modified_files": [str(current_file)],
            "branch": "feature/proposal-slice",
            "stakeholder": "product",
        },
        limit=10,
    )

    environment_titles = {candidate["title"] for candidate in bundle["environment_candidates"]}
    artifact_refs = {candidate.get("source_ref", "") for candidate in bundle["artifact_candidates"]}

    assert "task_mode" in environment_titles
    assert "branch" in environment_titles
    assert str(current_file) in artifact_refs


def test_generate_active_memory_slice_resolves_stale_candidate_ids_via_memory_and_artifact_ids(monkeypatch):
    from memem import active_slice_engine
    from memem.active_slice import normalize_artifact_candidate, normalize_memory_candidate

    memory = normalize_memory_candidate({
        "id": "abcdef1234567890",
        "title": "Auth constraint",
        "essence": "Preserve session refresh ordering during auth fixes.",
        "project": "memem",
        "importance": 5,
        "layer": 1,
    }, score=0.91)
    artifact = normalize_artifact_candidate(
        "repo_file",
        "auth.py",
        "Current auth file.",
        path="/tmp/auth.py",
        project="memem",
    )

    monkeypatch.setattr(active_slice_engine, "generate_candidates", lambda *args, **kwargs: {
        "current_goal_candidates": [],
        "memory_candidates": [memory],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [artifact],
        "environment_candidates": [],
    })
    monkeypatch.setattr(active_slice_engine, "judge_activation_heuristically", lambda *args, **kwargs: {
        "constraints": [{
            "candidate_id": "stale-memory-candidate",
            "memory_id": memory["memory_id"],
            "why": "fallback through memory id",
            "score": 0.92,
        }],
        "artifact_context": [{
            "candidate_id": "stale-artifact-candidate",
            "artifact_id": artifact["artifact_id"],
            "why": "fallback through artifact id",
            "score": 0.83,
        }],
        "activation_mode": "heuristic",
        "confidence": 0.77,
    })
    monkeypatch.setattr(active_slice_engine, "propose_deltas_from_slice", lambda slice_obj: [])

    slice_obj = active_slice_engine.generate_active_memory_slice(
        "Continue auth fix",
        scope_id="memem",
        use_llm=False,
    )

    assert slice_obj["constraints"]
    assert slice_obj["constraints"][0]["memory_id"] == memory["memory_id"]
    assert slice_obj["artifacts"]
    assert slice_obj["artifacts"][0]["artifact_id"] == artifact["artifact_id"]


def test_generate_active_memory_slice_keeps_writeback_proposed_count_consistent(monkeypatch):
    from memem import active_slice_engine
    from memem.active_slice import normalize_memory_candidate

    memory = normalize_memory_candidate({
        "id": "feedface12345678",
        "title": "Proposal constraint",
        "essence": "Keep rollout constraints visible.",
        "project": "memem",
        "importance": 4,
        "layer": 2,
    }, score=0.82)

    monkeypatch.setattr(active_slice_engine, "generate_candidates", lambda *args, **kwargs: {
        "current_goal_candidates": [],
        "memory_candidates": [memory],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    })
    monkeypatch.setattr(active_slice_engine, "judge_activation_heuristically", lambda *args, **kwargs: {
        "constraints": [{"memory_id": memory["memory_id"], "why": "constraint memory", "score": 0.88}],
        "activation_mode": "heuristic",
        "confidence": 0.72,
    })
    monkeypatch.setattr(active_slice_engine, "propose_deltas_from_slice", lambda slice_obj: [
        {"delta_id": "delta_1", "delta_type": "add_related_link", "reason": "linked memories"},
        {"delta_id": "delta_2", "delta_type": "save_new_memory", "reason": "durable lesson"},
    ])

    slice_obj = active_slice_engine.generate_active_memory_slice(
        "Continue rollout proposal",
        scope_id="memem",
        use_llm=False,
    )

    assert len(slice_obj["candidate_deltas"]) == 2
    assert slice_obj["writeback_summary"]["proposed_count"] == 2


def test_generate_active_memory_slice_persists_continuity_history(tmp_vault, tmp_cortex_dir):
    from memem import active_slice_engine, obsidian_store, slice_history

    importlib.reload(obsidian_store)
    importlib.reload(slice_history)

    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Auth fixes must preserve session refresh ordering.",
        title="Auth continuity constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))
    current_file = tmp_vault / "auth.py"
    current_file.write_text("def login():\n    return 'ok'\n")

    environment = {
        "task_mode": "coding",
        "session_id": "session-42",
        "current_file": str(current_file),
        "modified_files": [str(current_file)],
        "branch": "fix/auth-continuity",
    }
    first = active_slice_engine.generate_active_memory_slice(
        "Continue the auth fix and keep the refresh ordering intact.",
        scope_id="memem",
        environment=environment,
        use_llm=False,
    )
    current_file.write_text("def login(user):\n    return user\n")
    second = active_slice_engine.generate_active_memory_slice(
        "Finish the auth fix and keep the refresh ordering intact.",
        scope_id="memem",
        environment=environment,
        use_llm=False,
    )

    history = slice_history.load_slice_history(scope_id="memem", session_id="session-42", limit=10)

    assert len(history) == 2
    assert second["previous_slice_id"] == first["slice_id"]
    assert second["artifact_progression"]["stage"] in {"drafting", "revising", "review_ready"}
    assert any("Artifact in progress" in line for line in second["carry_forward_summary"])


def test_generate_active_memory_slice_with_writeback_auto_commit_safe_links_memories(tmp_vault, tmp_cortex_dir):
    from memem import active_slice_engine, obsidian_store

    importlib.reload(obsidian_store)

    constraint = obsidian_store._make_memory(
        content="Constraint: keep rollout safety checks visible during auth changes.",
        title="Rollout safety constraint",
        project="memem",
        source_type="user",
        importance=5,
    )
    failure = obsidian_store._make_memory(
        content="Failure pattern: avoid the previous auth regression during rollout.",
        title="Auth regression failure",
        project="memem",
        source_type="user",
        importance=5,
    )
    obsidian_store._save_memory(constraint)
    obsidian_store._save_memory(failure)

    result = active_slice_engine.generate_active_memory_slice_with_writeback(
        "Continue the auth rollout, keep the safety constraint, and avoid the prior regression.",
        scope_id="memem",
        environment={"task_mode": "coding"},
        use_llm=False,
        auto_commit_safe=True,
        dry_run=False,
    )

    refreshed_constraint = obsidian_store._find_memory(constraint["id"])
    refreshed_failure = obsidian_store._find_memory(failure["id"])

    assert result["delta_results"]
    assert any(item.get("delta_type") == "add_related_link" for item in result["delta_results"])
    assert result["slice"]["writeback_summary"]["status"] in {"committed", "partial"}
    assert failure["id"][:8] in refreshed_constraint.get("related", [])
    assert constraint["id"][:8] in refreshed_failure.get("related", [])


def test_generate_active_memory_slice_does_not_leak_previous_slice_without_continuity_mode(tmp_vault, tmp_cortex_dir):
    from memem import active_slice_engine, obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Constraint: keep auth rollback safe.",
        title="Auth rollback constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))

    first = active_slice_engine.generate_active_memory_slice(
        "Continue the auth rollback task.",
        scope_id="memem",
        environment={"task_mode": "coding", "session_id": "session-a"},
        use_llm=False,
    )
    second = active_slice_engine.generate_active_memory_slice(
        "Start a different auth task.",
        scope_id="memem",
        environment={"task_mode": "coding"},
        use_llm=False,
    )

    assert first["slice_id"]
    assert second["previous_slice_id"] == ""


def test_continuity_context_excludes_writeback_preview_sections(monkeypatch):
    from memem import active_slice_engine
    from memem.active_slice import normalize_memory_candidate

    seen_environments: list[dict] = []
    memory = normalize_memory_candidate({
        "id": "abc1234512345678",
        "title": "Auth constraint",
        "essence": "Keep auth rollback safe.",
        "project": "memem",
        "importance": 5,
        "layer": 1,
    }, score=0.91)

    monkeypatch.setattr(active_slice_engine, "generate_candidates", lambda *args, **kwargs: {
        "current_goal_candidates": [],
        "memory_candidates": [memory],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    })

    def _judge(query, scope_id, environment, candidate_bundle):
        seen_environments.append(dict(environment))
        return {
            "constraints": [{"memory_id": memory["memory_id"], "why": "constraint memory", "score": 0.9}],
            "activation_mode": "heuristic",
            "confidence": 0.75,
        }

    monkeypatch.setattr(active_slice_engine, "judge_activation_heuristically", _judge)
    monkeypatch.setattr(active_slice_engine, "propose_deltas_from_slice", lambda slice_obj: [
        {"delta_id": "delta_1", "delta_type": "add_related_link", "reason": "link"},
    ])

    active_slice_engine.generate_active_memory_slice_with_writeback(
        "Continue auth work.",
        scope_id="memem",
        environment={"task_mode": "coding", "session_id": "session-c"},
        use_llm=False,
        dry_run=True,
    )
    active_slice_engine.generate_active_memory_slice(
        "Continue auth work again.",
        scope_id="memem",
        environment={"task_mode": "coding", "session_id": "session-c"},
        use_llm=False,
    )

    continuity_context = seen_environments[-1].get("continuity_context", "")
    assert continuity_context
    assert "Candidate Deltas" not in continuity_context
    assert "Writeback" not in continuity_context
