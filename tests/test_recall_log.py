"""Tests for memem.recall_log."""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)
    yield {"state_dir": state_dir, "rl": _rl}
    importlib.reload(_models)


def test_log_recall_appends_jsonl_line(log_env):
    rl = log_env["rl"]
    rl.log_recall("hook_auto", "query A", ["id1", "id2"], 132, "hook")
    lines = (log_env["state_dir"] / ".recall_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["call_type"] == "hook_auto"
    assert entry["query"] == "query A"
    assert entry["returned_ids"] == ["id1", "id2"]
    assert entry["latency_ms"] == 132
    assert entry["source"] == "hook"
    assert "ts" in entry


def test_log_recall_multiple_calls(log_env):
    rl = log_env["rl"]
    for i in range(5):
        rl.log_recall("tool_active_slice", f"q{i}", [f"id{i}"], 100 + i, "mcp")
    lines = (log_env["state_dir"] / ".recall_log.jsonl").read_text().splitlines()
    assert len(lines) == 5


def test_log_recall_silent_on_error(log_env, monkeypatch):
    # Even if json.dumps somehow fails, log_recall must not raise
    rl = log_env["rl"]
    # passing non-serializable type — should be caught by inner try/except
    class Bad:
        pass
    rl.log_recall("hook_auto", "q", [Bad()], 100, "hook")  # noqa
    # no assertion needed — pass means no exception


def test_analyze_recalls_empty(log_env):
    rl = log_env["rl"]
    summary = rl.analyze_recalls(days=7)
    assert summary["total"] == 0
    assert summary["by_call_type"] == {}
    assert summary["top_queries"] == []


def test_analyze_recalls_summary_sections(log_env):
    rl = log_env["rl"]
    rl.log_recall("hook_auto", "query A", ["id1"], 100, "hook")
    rl.log_recall("hook_auto", "query A", ["id2"], 120, "hook")
    rl.log_recall("tool_active_slice", "query B", ["id3"], 200, "mcp")
    summary = rl.analyze_recalls(days=7)
    assert summary["total"] == 3
    assert summary["by_call_type"]["hook_auto"] == 2
    assert summary["by_call_type"]["tool_active_slice"] == 1
    assert ("query A", 2) in summary["top_queries"]
    assert "hook_auto" in summary["median_latency_per_type"]


def test_analyze_recalls_absent_file(tmp_path, monkeypatch):
    state_dir = tmp_path / ".memem-empty"
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)
    summary = _rl.analyze_recalls(days=7)
    assert summary["total"] == 0
