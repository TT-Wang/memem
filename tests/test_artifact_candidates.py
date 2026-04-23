"""Tests for artifact candidate generation from runtime environment."""

from __future__ import annotations


def test_playbook_artifact_candidate_present(tmp_vault, tmp_cortex_dir):
    from memem.active_slice_engine import generate_candidates

    playbook = tmp_vault / "memem" / "playbooks" / "memem.md"
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
