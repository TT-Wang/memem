"""Tests for artifact candidate generation from runtime environment."""

from __future__ import annotations


def test_playbook_artifact_candidate_present(tmp_vault, tmp_cortex_dir):
    from memem.active_slice_engine import generate_candidates
    from memem.models import _normalize_scope_id

    # Use the normalized scope name for the playbook filename so the
    # lookup in _playbook_candidate (which normalizes scope_id) matches.
    normalized = _normalize_scope_id("memem")
    playbook = tmp_vault / "memem" / "playbooks" / f"{normalized}.md"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("# Playbook\n\nKeep rollout constraints visible.\n")

    bundle = generate_candidates("Continue rollout", "memem", environment={}, limit=10)

    assert bundle["playbook_candidate"] is not None
    assert bundle["playbook_candidate"]["candidate_type"] == "playbook"


def test_current_file_becomes_artifact_candidate(tmp_path):
    from memem.artifact_context import artifact_candidates_from_environment
    from memem.environment_context import normalize_runtime_environment

    current_file = tmp_path / "auth.py"
    current_file.write_text("def login():\n    return 'ok'\n")
    env = normalize_runtime_environment({"current_file": str(current_file), "task_mode": "coding"})

    candidates = artifact_candidates_from_environment(env, "memem")

    assert candidates
    assert candidates[0]["source_ref"] == str(current_file)
    assert candidates[0]["source_type"] == "repo_file"


def test_modified_files_become_artifact_candidates(tmp_path):
    from memem.artifact_context import artifact_candidates_from_environment
    from memem.environment_context import normalize_runtime_environment

    modified_a = tmp_path / "api.py"
    modified_b = tmp_path / "README.md"
    modified_a.write_text("def handler():\n    return 1\n")
    modified_b.write_text("# API notes\n")
    env = normalize_runtime_environment({
        "modified_files": [str(modified_a), str(modified_b)],
        "task_mode": "coding",
    })

    candidates = artifact_candidates_from_environment(env, "memem")
    refs = {candidate["source_ref"] for candidate in candidates if candidate.get("source_ref")}

    assert str(modified_a) in refs
    assert str(modified_b) in refs


def test_repo_path_and_task_mode_alone_do_not_create_artifact_candidate():
    from memem.artifact_context import artifact_candidates_from_environment
    from memem.environment_context import normalize_runtime_environment

    env = normalize_runtime_environment({
        "repo_path": "/repo/memem",
        "task_mode": "coding",
    })

    assert artifact_candidates_from_environment(env, "memem") == []


def test_artifact_candidate_cap_respected(tmp_path):
    from memem.active_slice_engine import generate_candidates

    modified_files: list[str] = []
    for index in range(12):
        path = tmp_path / f"file_{index}.py"
        path.write_text(f"VALUE_{index} = {index}\n")
        modified_files.append(str(path))

    bundle = generate_candidates(
        "Continue coding",
        "memem",
        environment={"modified_files": modified_files, "task_mode": "coding"},
        limit=10,
    )

    assert len(bundle["artifact_candidates"]) == 8


def test_repo_file_artifact_identity_stable_across_content_updates(tmp_path):
    from memem.artifact_context import artifact_candidates_from_environment
    from memem.environment_context import normalize_runtime_environment

    current_file = tmp_path / "auth.py"
    current_file.write_text("def login():\n    return 'ok'\n")
    env = normalize_runtime_environment({"current_file": str(current_file), "task_mode": "coding"})
    first = artifact_candidates_from_environment(env, "memem")[0]

    current_file.write_text("def login(user):\n    return user\n")
    second = artifact_candidates_from_environment(env, "memem")[0]

    assert first["artifact_id"] == second["artifact_id"]
    assert first["candidate_id"] == second["candidate_id"]
    assert first["summary"] != second["summary"]
    assert str(current_file) in second["summary"]


def test_research_and_session_start_context_candidates_include_stable_identity():
    from memem.artifact_context import artifact_candidates_from_environment
    from memem.environment_context import normalize_runtime_environment

    research_env = normalize_runtime_environment({
        "task_mode": "research",
        "task_name": "Investigate auth fallback behavior",
        "repo_path": "/repo/memem",
        "branch": "research/auth",
        "stakeholder": "security",
    })
    session_env = normalize_runtime_environment({
        "task_mode": "session-start",
        "task_name": "Resume auth rollout",
        "repo_path": "/repo/memem",
        "branch": "main",
        "open_files": ["/repo/memem/README.md"],
    })

    research_candidates = artifact_candidates_from_environment(research_env, "memem")
    session_candidates = artifact_candidates_from_environment(session_env, "memem")

    assert research_candidates
    assert research_candidates[0]["artifact_id"] == research_candidates[0]["candidate_id"]
    assert "Investigate auth fallback behavior" in research_candidates[0]["summary"]
    assert session_candidates
    assert session_candidates[0]["artifact_id"] == session_candidates[0]["candidate_id"]
    assert "Resume auth rollout" in session_candidates[0]["summary"]


def test_repo_relative_environment_paths_match_absolute_paths(tmp_path):
    from memem.artifact_context import artifact_candidates_from_environment
    from memem.environment_context import normalize_runtime_environment

    repo = tmp_path / "repo"
    current_file = repo / "src" / "auth.py"
    modified_file = repo / "README.md"
    open_file = repo / "docs" / "spec.md"
    artifact_file = repo / "notes" / "draft.md"
    current_file.parent.mkdir(parents=True, exist_ok=True)
    open_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    current_file.write_text("def login():\n    return 'ok'\n")
    modified_file.write_text("# README\n")
    open_file.write_text("Spec details\n")
    artifact_file.write_text("# Draft\n")

    relative_env = normalize_runtime_environment({
        "repo_path": str(repo),
        "current_file": "src/auth.py",
        "modified_files": ["README.md"],
        "open_files": ["docs/spec.md"],
        "artifact_path": "notes/draft.md",
        "task_mode": "coding",
    })
    absolute_env = normalize_runtime_environment({
        "repo_path": str(repo),
        "current_file": str(current_file),
        "modified_files": [str(modified_file)],
        "open_files": [str(open_file)],
        "artifact_path": str(artifact_file),
        "task_mode": "coding",
    })

    assert relative_env["current_file"] == absolute_env["current_file"] == str(current_file.resolve())
    assert relative_env["modified_files"] == absolute_env["modified_files"] == [str(modified_file.resolve())]
    assert relative_env["open_files"] == absolute_env["open_files"] == [str(open_file.resolve())]
    assert relative_env["artifact_path"] == absolute_env["artifact_path"] == str(artifact_file.resolve())

    relative_candidates = artifact_candidates_from_environment(relative_env, "memem")
    absolute_candidates = artifact_candidates_from_environment(absolute_env, "memem")

    assert [
        (candidate["artifact_id"], candidate.get("source_ref", ""))
        for candidate in relative_candidates
    ] == [
        (candidate["artifact_id"], candidate.get("source_ref", ""))
        for candidate in absolute_candidates
    ]
