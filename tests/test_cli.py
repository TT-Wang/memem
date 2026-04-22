"""Tests for the memem CLI dispatcher.

These tests invoke `dispatch_cli` in-process with a real MCP stub, isolated
state/vault dirs, and captured stdout. No subprocess spawning.
"""

import importlib
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
    from memem import transcripts
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    out = _dispatch(["active-slice", "Prepare", "project", "review", "--scope", "memem", "--no-llm"], capsys)
    assert "# Active Memory Slice" in out.out
    assert "## Goals" in out.out


def test_active_slice_cli_json(tmp_vault, tmp_cortex_dir, capsys, monkeypatch):
    from memem import transcripts
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    out = _dispatch(["active-slice", "Prepare review", "--scope", "memem", "--json", "--no-llm"], capsys)
    assert '"goals"' in out.out
    assert '"activation_mode": "heuristic"' in out.out
