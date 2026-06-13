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
    monkeypatch.delenv("MEMEM_TELEMETRY_SOURCE", raising=False)
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


# ---------------------------------------------------------------------------
# New tests: MEMEM_TELEMETRY_SOURCE guard (m3)
# ---------------------------------------------------------------------------

def test_log_recall_test_source_blocks_write(tmp_path, monkeypatch):
    """MEMEM_TELEMETRY_SOURCE=test must prevent any write to the log."""
    state_dir = tmp_path / ".memem-test-block"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.setenv("MEMEM_TELEMETRY_SOURCE", "test")
    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)

    _rl.log_recall("hook_auto", "test query", ["id1"], 100, "hook")
    log_path = state_dir / ".recall_log.jsonl"
    assert not log_path.exists() or log_path.read_text().strip() == "", (
        "FAIL: log_recall wrote to log when MEMEM_TELEMETRY_SOURCE=test"
    )


def test_log_citation_test_source_blocks_write(tmp_path, monkeypatch):
    """MEMEM_TELEMETRY_SOURCE=test must prevent citation writes too."""
    state_dir = tmp_path / ".memem-test-citation-block"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.setenv("MEMEM_TELEMETRY_SOURCE", "test")
    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)

    _rl.log_citation("session123", ["abc12345"], "mine_delta")
    log_path = state_dir / ".recall_log.jsonl"
    assert not log_path.exists() or log_path.read_text().strip() == "", (
        "FAIL: log_citation wrote to log when MEMEM_TELEMETRY_SOURCE=test"
    )


def test_log_recall_benchmark_source_writes_with_source_tag(tmp_path, monkeypatch):
    """MEMEM_TELEMETRY_SOURCE=benchmark must write a row with source_tag='benchmark'."""
    state_dir = tmp_path / ".memem-benchmark"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.setenv("MEMEM_TELEMETRY_SOURCE", "benchmark")
    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)

    _rl.log_recall("hook_auto", "warmup query", ["id1"], 55, "hook")
    log_path = state_dir / ".recall_log.jsonl"
    assert log_path.exists(), "FAIL: no log written for benchmark source"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
    entry = json.loads(lines[0])
    assert entry.get("source_tag") == "benchmark", (
        f"Expected source_tag='benchmark', got: {entry}"
    )


def test_log_recall_untagged_production_has_no_source_tag(log_env):
    """Untagged production calls must not include source_tag field (backward compat)."""
    rl = log_env["rl"]
    rl.log_recall("hook_auto", "prod query", ["id1"], 75, "hook")
    log_path = log_env["state_dir"] / ".recall_log.jsonl"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "source_tag" not in entry, (
        f"Production rows must not have source_tag, got: {entry}"
    )


def test_analyze_recalls_excludes_tagged_rows_by_default(tmp_path, monkeypatch):
    """analyze_recalls must exclude rows with source_tag by default."""
    state_dir = tmp_path / ".memem-analyze-tagged"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("MEMEM_TELEMETRY_SOURCE", raising=False)
    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)

    # Write one production row and one benchmark-tagged row directly
    log_path = state_dir / ".recall_log.jsonl"
    import json as _json
    from datetime import UTC, datetime
    ts = datetime.now(UTC).isoformat()
    prod_row = {"ts": ts, "call_type": "hook_auto", "query": "prod q", "returned_ids": [], "latency_ms": 50, "source": "hook"}
    bm_row = {"ts": ts, "call_type": "hook_auto", "query": "warmup query", "returned_ids": [], "latency_ms": 30, "source": "hook", "source_tag": "benchmark"}
    with open(log_path, "w") as f:
        f.write(_json.dumps(prod_row) + "\n")
        f.write(_json.dumps(bm_row) + "\n")

    # Default: only production row counted
    summary = _rl.analyze_recalls(days=7)
    assert summary["total"] == 1, f"Expected 1 (excluding tagged), got {summary['total']}"
    assert summary["top_queries"] == [("prod q", 1)]

    # With include_tagged=True: both rows counted
    summary_all = _rl.analyze_recalls(days=7, include_tagged=True)
    assert summary_all["total"] == 2, f"Expected 2 (including tagged), got {summary_all['total']}"
