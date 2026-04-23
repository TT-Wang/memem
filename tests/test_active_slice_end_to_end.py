"""End-to-end runtime tests for continuity-aware slice execution."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]


def _dispatch_json(args: list[str], capsys) -> dict:
    from memem import cli

    importlib.reload(cli)
    cli.dispatch_cli(["memem", *args], SimpleNamespace(run=lambda **_: None))
    out = capsys.readouterr()
    return json.loads(out.out)


def test_slice_cli_json_carries_forward_previous_slice_with_session_context(
    tmp_vault,
    tmp_cortex_dir,
    capsys,
):
    from memem import obsidian_store

    importlib.reload(obsidian_store)
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Proposal work must preserve rollout approvals and auditability.",
        title="Rollout proposal constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))

    repo_path = tmp_vault / "repo"
    repo_path.mkdir()
    draft_path = repo_path / "proposal.md"
    draft_path.write_text("draft v1\n")

    first = _dispatch_json(
        [
            "slice",
            "Continue the rollout proposal",
            "--scope",
            "memem",
            "--session-id",
            "session-proposal",
            "--cwd",
            str(repo_path),
            "--task-mode",
            "proposal",
            "--artifact-path",
            str(draft_path),
            "--json",
            "--no-llm",
        ],
        capsys,
    )

    draft_path.write_text("draft v2 with approvals\n")
    second = _dispatch_json(
        [
            "slice",
            "Refine the rollout proposal",
            "--scope",
            "memem",
            "--session-id",
            "session-proposal",
            "--cwd",
            str(repo_path),
            "--task-mode",
            "proposal",
            "--artifact-path",
            str(draft_path),
            "--json",
            "--no-llm",
        ],
        capsys,
    )

    assert first["slice_id"]
    assert second["previous_slice_id"] == first["slice_id"]
    assert second["task_mode"] == "proposal"
    assert second["artifact_progression"]["stage"] in {"drafting", "revising", "review_ready"}
    assert second["carry_forward_summary"]


def test_slice_cli_auto_commit_safe_links_memories_end_to_end(tmp_vault, tmp_cortex_dir, capsys):
    from memem import obsidian_store

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

    payload = _dispatch_json(
        [
            "slice",
            "Continue the auth rollout, keep the safety constraint, and avoid the prior regression.",
            "--scope",
            "memem",
            "--session-id",
            "session-writeback",
            "--cwd",
            str(tmp_vault / "repo"),
            "--task-mode",
            "coding",
            "--auto-commit-safe",
            "--json",
            "--no-llm",
        ],
        capsys,
    )

    refreshed_constraint = obsidian_store._find_memory(constraint["id"])
    refreshed_failure = obsidian_store._find_memory(failure["id"])

    assert payload["slice"]["writeback_summary"]["status"] in {"committed", "partial"}
    assert any(item.get("delta_type") == "add_related_link" for item in payload["delta_results"])
    assert failure["id"][:8] in refreshed_constraint.get("related", [])
    assert constraint["id"][:8] in refreshed_failure.get("related", [])


def test_auto_recall_hook_persists_continuity_with_session_context(tmp_path, tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store, slice_history

    importlib.reload(obsidian_store)
    importlib.reload(slice_history)

    repo_path = tmp_path / "demo-repo"
    repo_path.mkdir()
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Auth fixes must preserve the rollback constraint and audit trail.",
        title="Auth rollback constraint",
        project=repo_path.name,
        source_type="user",
        importance=5,
    ))

    env = {
        **dict(os.environ),
        "MEMEM_DIR": str(tmp_cortex_dir),
        "MEMEM_OBSIDIAN_VAULT": str(tmp_vault),
        "CLAUDE_PLUGIN_ROOT": str(REPO),
        "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    first = subprocess.run(
        ["bash", str(REPO / "hooks" / "auto-recall.sh")],
        input=json.dumps({
            "session_id": "session-hook",
            "cwd": str(repo_path),
            "message": "fix the auth bug in the login flow",
        }),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    second = subprocess.run(
        ["bash", str(REPO / "hooks" / "auto-recall.sh")],
        input=json.dumps({
            "session_id": "session-hook",
            "cwd": str(repo_path),
            "message": "fix the auth bug and finish the login rollback safely",
        }),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    history = slice_history.load_slice_history(
        scope_id=repo_path.name,
        session_id="session-hook",
        limit=10,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert "# Active Memory Slice" in first_payload["hookSpecificOutput"]["additionalContext"]
    assert "# Active Memory Slice" in second_payload["hookSpecificOutput"]["additionalContext"]
    assert len(history) == 2
    assert history[1]["previous_slice_id"] == history[0]["slice_id"]
    assert history[1]["task_mode"] == "debug"
