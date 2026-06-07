"""Tests for the memem CLI dispatcher.

These tests invoke `dispatch_cli` in-process with a real MCP stub, isolated
state/vault dirs, and captured stdout. No subprocess spawning.
"""

import importlib
import io
import json
import sys
from types import SimpleNamespace


def _dispatch(argv, capsys):
    """Reload cli each call so patched env vars propagate into its imports."""
    from memem import cli
    importlib.reload(cli)
    mcp_stub = SimpleNamespace(run=lambda **_: None)
    cli.dispatch_cli(["memem", *argv], mcp_stub)
    return capsys.readouterr()


def test_status_empty_vault(tmp_vault, tmp_cortex_dir, capsys, monkeypatch):
    # Prevent --status from actually shelling out to the wrapper
    import subprocess
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout="not running", stderr="", returncode=0))
    out = _dispatch(["--status"], capsys)
    assert "memem Status" in out.out
    assert "0 active" in out.out
    assert "not running" in out.out


def test_events_empty(tmp_cortex_dir, capsys):
    out = _dispatch(["--events"], capsys)
    assert "No events yet" in out.out


def test_events_after_log(tmp_cortex_dir, capsys):
    from memem import models, storage, telemetry
    importlib.reload(models)
    importlib.reload(telemetry)
    importlib.reload(storage)
    telemetry._log_event("test_op", "abc12345", detail="hello")
    out = _dispatch(["--events"], capsys)
    assert "test_op" in out.out
    assert "abc12345" in out.out


def test_migrate_schema_no_memories(tmp_vault, capsys):
    out = _dispatch(["--migrate-schema"], capsys)
    assert "0 upgraded" in out.out


def test_migrate_schema_upgrades_v0(tmp_vault, capsys):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    # Write a v0 memory directly (no schema_version)
    mem = obsidian_store._make_memory(
        content="legacy memory without schema_version",
        title="Legacy",
        tags=["legacy"],
    )
    mem.pop("schema_version", None)  # simulate v0
    obsidian_store._save_memory(mem)

    out = _dispatch(["--migrate-schema"], capsys)
    assert "1 upgraded" in out.out or "upgraded" in out.out

    # Re-read — schema_version should now be 1
    importlib.reload(obsidian_store)
    all_mems = obsidian_store._obsidian_memories(include_deprecated=True)
    assert all(m.get("schema_version", 0) >= 1 for m in all_mems)


def test_mine_session_missing_file(tmp_vault, tmp_cortex_dir, capsys):
    # Missing file should be handled gracefully — either skip or raise SystemExit
    try:
        out = _dispatch(["--mine-session", "/nonexistent/session.jsonl"], capsys)
        # If no SystemExit, the result should indicate skip/empty
        assert "skipped" in out.out or "memories_saved" in out.out or out.out == ""
    except SystemExit:
        pass  # Also acceptable


def test_unknown_command(tmp_vault, capsys):
    import pytest
    with pytest.raises(SystemExit, match="Unknown command"):
        _dispatch(["--does-not-exist"], capsys)


def test_rebuild_index(tmp_vault, capsys):
    out = _dispatch(["--rebuild-index"], capsys)
    assert "Index rebuilt" in out.out


def test_active_slice_cli_no_llm(tmp_vault, tmp_cortex_dir, capsys, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")
    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Project review must include concrete risks and next steps.",
        title="Project review constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))

    out = _dispatch(["slice", "Prepare", "project", "review", "--scope", "memem", "--no-llm"], capsys)
    assert "# Active Memory Slice" in out.out
    assert "## Anchors" in out.out


def test_active_slice_cli_json(tmp_vault, tmp_cortex_dir, capsys, monkeypatch):
    from memem import transcripts
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    out = _dispatch(["active-slice", "Prepare review", "--scope", "memem", "--json", "--no-llm"], capsys)
    assert '"goals"' in out.out
    assert '"activation_mode": "heuristic"' in out.out


def test_active_slice_cli_query_file_stdin(capsys, monkeypatch):
    from memem import active_slice_engine, cli
    importlib.reload(cli)

    captured = {}

    def fake_response(query, scope_id="default", environment=None, use_llm=True, raw_json=False):
        captured.update({
            "query": query,
            "scope_id": scope_id,
            "use_llm": use_llm,
            "raw_json": raw_json,
        })
        return "ok"

    monkeypatch.setattr(active_slice_engine, "active_slice_response", fake_response)
    monkeypatch.setattr(sys, "stdin", io.StringIO("large prompt body" * 1000))

    cli.dispatch_cli(
        ["memem", "slice", "--query-file", "-", "--scope", "memem", "--no-llm"],
        SimpleNamespace(run=lambda **_: None),
    )
    out = capsys.readouterr()

    assert out.out.strip() == "ok"
    assert captured["query"].startswith("large prompt body")
    assert captured["scope_id"] == "memem"
    assert captured["use_llm"] is False


def test_active_slice_legacy_alias_still_works(tmp_vault, tmp_cortex_dir, capsys, monkeypatch):
    from memem import transcripts

    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    out = _dispatch(["active-slice", "Prepare review", "--scope", "memem", "--json", "--no-llm"], capsys)

    assert '"goals"' in out.out


def test_active_slice_cli_forwards_runtime_environment(monkeypatch, capsys):
    from memem import active_slice_engine, cli

    importlib.reload(cli)

    captured = {}

    def fake_response(query, scope_id="default", environment=None, use_llm=True, raw_json=False):
        captured.update({
            "query": query,
            "scope_id": scope_id,
            "environment": dict(environment or {}),
            "use_llm": use_llm,
            "raw_json": raw_json,
        })
        return "ok"

    monkeypatch.setattr(active_slice_engine, "active_slice_response", fake_response)

    cli.dispatch_cli(
        [
            "memem",
            "slice",
            "continue auth fix",
            "--scope",
            "memem",
            "--session-id",
            "session-77",
            "--cwd",
            "/tmp/repo",
            "--task-mode",
            "coding",
            "--current-file",
            "src/auth.py",
            "--open-file",
            "README.md",
            "--modified-file",
            "src/auth.py",
            "--branch",
            "feature/auth",
            "--include-history",
            "--include-transcripts",
            "--no-llm",
        ],
        SimpleNamespace(run=lambda **_: None),
    )
    out = capsys.readouterr()

    assert out.out.strip() == "ok"
    assert captured["scope_id"] == "memem"
    assert captured["use_llm"] is False
    assert captured["environment"]["session_id"] == "session-77"
    assert captured["environment"]["repo_path"] == "/tmp/repo"
    assert captured["environment"]["cwd"] == "/tmp/repo"
    assert captured["environment"]["task_mode"] == "coding"
    assert captured["environment"]["current_file"] == "src/auth.py"
    assert captured["environment"]["open_files"] == ["README.md"]
    assert captured["environment"]["modified_files"] == ["src/auth.py"]
    assert captured["environment"]["branch"] == "feature/auth"
    assert captured["environment"]["include_history"] is True
    assert captured["environment"]["include_transcripts"] is True


def test_active_slice_cli_auto_commit_safe_uses_writeback_engine(monkeypatch, capsys):
    from memem import active_slice_engine, cli

    importlib.reload(cli)

    captured = {}
    slice_obj = {
        "slice_id": "slice_123",
        "scope_id": "memem",
        "query": "continue auth fix",
        "activation_mode": "heuristic",
        "confidence": 0.8,
        "goals": [{"title": "Continue auth fix"}],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "candidate_deltas": [],
        "delta_results": [],
        "writeback_summary": {"status": "committed", "dry_run": False},
        "warnings": [],
        "should_emit_context": True,
    }

    def fake_with_writeback(query, scope_id="default", environment=None, use_llm=True, auto_commit_safe=False, dry_run=True):
        captured.update({
            "query": query,
            "scope_id": scope_id,
            "environment": dict(environment or {}),
            "use_llm": use_llm,
            "auto_commit_safe": auto_commit_safe,
            "dry_run": dry_run,
        })
        return {"slice": slice_obj, "delta_results": []}

    monkeypatch.setattr(active_slice_engine, "generate_active_memory_slice_with_writeback", fake_with_writeback)
    monkeypatch.setattr(
        active_slice_engine,
        "generate_prompt_context",
        lambda *args, **kwargs: "rendered slice",
    )

    cli.dispatch_cli(
        [
            "memem",
            "slice",
            "continue auth fix",
            "--scope",
            "memem",
            "--session-id",
            "session-88",
            "--task-mode",
            "debug",
            "--cwd",
            "/tmp/repo",
            "--auto-commit-safe",
            "--no-llm",
        ],
        SimpleNamespace(run=lambda **_: None),
    )
    out = capsys.readouterr()

    assert out.out.strip() == "rendered slice"
    assert captured["scope_id"] == "memem"
    assert captured["environment"]["session_id"] == "session-88"
    assert captured["environment"]["task_mode"] == "debug"
    assert captured["auto_commit_safe"] is True
    assert captured["dry_run"] is False


def test_active_slice_cli_writeback_preview_json_returns_full_result(monkeypatch, capsys):
    from memem import active_slice_engine, cli

    importlib.reload(cli)

    monkeypatch.setattr(
        active_slice_engine,
        "generate_active_memory_slice_with_writeback",
        lambda *args, **kwargs: {
            "slice": {"slice_id": "slice_456", "writeback_summary": {"status": "dry_run"}},
            "delta_results": [{"delta_type": "add_related_link", "status": "not_run"}],
        },
    )

    cli.dispatch_cli(
        ["memem", "slice", "continue auth fix", "--writeback-preview", "--json", "--no-llm"],
        SimpleNamespace(run=lambda **_: None),
    )
    out = capsys.readouterr()
    payload = json.loads(out.out)

    assert payload["slice"]["slice_id"] == "slice_456"
    assert payload["delta_results"][0]["delta_type"] == "add_related_link"
