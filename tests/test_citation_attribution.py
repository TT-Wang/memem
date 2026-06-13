"""Tests for citation attribution (F1) and related recall_log extensions.

Covers:
  (a) log_citation row shape + append
  (b) log_recall accepts and persists session_id; omitting it produces no key error
  (c) analyze_recalls handles mixed file (old rows, new rows, citation rows,
      malformed line) and computes citation_rate correctly
  (d) mine_delta citation scan: build a fake recall_log + a nested-schema JSONL
      delta whose assistant text mentions one returned id8 and one random hex
      non-id → only the real one cited
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    """Isolated MEMEM_DIR for recall_log tests."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models as _models
    importlib.reload(_models)
    import memem.recall_log as _rl
    importlib.reload(_rl)

    yield {"state_dir": state_dir, "rl": _rl, "models": _models}

    importlib.reload(_models)


@pytest.fixture
def mine_env(tmp_path, monkeypatch):
    """Isolated MEMEM_DIR + MEMEM_OBSIDIAN_VAULT for mine_delta tests."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    vault_dir = tmp_path / "obsidian-brain"
    (vault_dir / "memem" / "memories").mkdir(parents=True)

    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

    import memem.models as _models
    importlib.reload(_models)
    import memem.obsidian_store as _store
    importlib.reload(_store)

    yield {
        "state_dir": state_dir,
        "vault_dir": vault_dir,
        "tmp_path": tmp_path,
    }

    importlib.reload(_models)
    importlib.reload(_store)


# ---------------------------------------------------------------------------
# (a) log_citation row shape + append
# ---------------------------------------------------------------------------


class TestLogCitation:
    def test_log_citation_appends_correct_shape(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        rl.log_citation("sess-abc", ["id1a2b3c", "id2d3e4f"], source="mine_delta")

        lines = log_path.read_text().splitlines()
        assert len(lines) == 1, "Expected exactly 1 line in recall log"
        row = json.loads(lines[0])

        assert row["type"] == "citation", "Citation row must have type='citation'"
        assert row["session_id"] == "sess-abc"
        assert "id1a2b3c" in row["cited_ids"]
        assert "id2d3e4f" in row["cited_ids"]
        assert row["source"] == "mine_delta"
        assert "ts" in row

    def test_log_citation_multiple_calls_append(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        rl.log_citation("sess-1", ["aaaa1111"])
        rl.log_citation("sess-2", ["bbbb2222", "cccc3333"])

        lines = log_path.read_text().splitlines()
        assert len(lines) == 2

    def test_log_citation_silent_on_error(self, log_env, monkeypatch):
        """log_citation must not raise even if the write fails."""
        rl = log_env["rl"]
        # Writes resolve via _current_log_path() (v2.9.0) — patch THAT to a
        # non-writable location; patching _LOG_PATH alone no longer exercises
        # the write-failure path.
        monkeypatch.setattr(
            rl, "_current_log_path", lambda: Path("/nonexistent_dir/recall_log.jsonl")
        )
        # Should not raise
        rl.log_citation("sess", ["aaaa1111"])


# ---------------------------------------------------------------------------
# (b) log_recall accepts session_id; omitting it produces no key error
# ---------------------------------------------------------------------------


class TestLogRecallSessionId:
    def test_log_recall_with_session_id_persists_field(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        rl.log_recall("tool_search", "query about X", ["id1a2b3c"], 50, "mcp", session_id="sess-xyz")

        row = json.loads(log_path.read_text().strip())
        assert row["session_id"] == "sess-xyz", "session_id must be persisted in the row"

    def test_log_recall_without_session_id_no_error(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        # Should not raise; session_id key should be absent
        rl.log_recall("hook_auto", "query B", ["id2d3e4f"], 80, "hook")

        row = json.loads(log_path.read_text().strip())
        assert "session_id" not in row, "session_id must NOT appear when not provided"
        assert row["call_type"] == "hook_auto"

    def test_log_recall_empty_session_id_omitted(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        rl.log_recall("tool_x", "q", [], 5, "test", session_id="")

        row = json.loads(log_path.read_text().strip())
        # Empty string session_id → field should be absent (falsy check in impl)
        assert "session_id" not in row


# ---------------------------------------------------------------------------
# (c) analyze_recalls handles mixed file + computes citation_rate correctly
# ---------------------------------------------------------------------------


class TestAnalyzeRecallsMixed:
    """Fixtures build a hand-crafted .recall_log.jsonl with:
    - old recall rows (no session_id, no type)
    - new recall rows (with session_id)
    - citation rows (type='citation')
    - a malformed/garbled line
    """

    def _write_log(self, path: Path, entries: list) -> None:
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_total_excludes_citation_rows(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        now_ts = datetime.now(UTC).isoformat()
        entries = [
            # 2 recall rows
            {"ts": now_ts, "call_type": "hook_auto", "query": "q1", "returned_ids": ["aabb1122"], "latency_ms": 100, "source": "hook"},
            {"ts": now_ts, "call_type": "tool_search", "query": "q2", "returned_ids": ["ccdd3344"], "latency_ms": 200, "source": "mcp", "session_id": "sess-1"},
            # 1 citation row — must NOT count toward total
            {"ts": now_ts, "type": "citation", "session_id": "sess-1", "cited_ids": ["aabb1122"], "source": "mine_delta"},
        ]
        self._write_log(log_path, entries)

        summary = rl.analyze_recalls(days=7)
        assert summary["total"] == 2, f"total must exclude citation rows; got {summary['total']}"

    def test_malformed_line_skipped(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        now_ts = datetime.now(UTC).isoformat()
        # Write one valid recall row + one malformed line
        with log_path.open("w") as f:
            f.write(json.dumps({"ts": now_ts, "call_type": "hook_auto", "query": "q1", "returned_ids": [], "latency_ms": 50, "source": "hook"}) + "\n")
            f.write("NOT VALID JSON {{{{\n")

        summary = rl.analyze_recalls(days=7)
        assert summary["total"] == 1, "Malformed line must be skipped silently"

    def test_old_rows_without_session_id_do_not_crash(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        # Old-style row: no session_id, no type
        now_ts = datetime.now(UTC).isoformat()
        entry = {"ts": now_ts, "call_type": "hook_auto", "query": "old query", "returned_ids": ["aabb1122"], "latency_ms": 100, "source": "hook"}
        self._write_log(log_path, [entry])

        # Must not raise
        summary = rl.analyze_recalls(days=7)
        assert summary["total"] == 1

    def test_citation_rate_computed_correctly(self, log_env):
        """Build fixture: hook_auto returns 2 ids; mine_delta cites 1 of them.
        Expected citation_rate for hook_auto = 0.5.
        """
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        now_ts = datetime.now(UTC).isoformat()
        entries = [
            # recall row: hook_auto returned 2 ids
            {
                "ts": now_ts, "call_type": "hook_auto", "query": "q",
                "returned_ids": ["aabb1122", "ccdd3344"], "latency_ms": 100, "source": "hook",
            },
            # citation row: only one of those ids was cited
            {
                "ts": now_ts, "type": "citation", "session_id": "sess-x",
                "cited_ids": ["aabb1122"], "source": "mine_delta",
            },
        ]
        self._write_log(log_path, entries)

        summary = rl.analyze_recalls(days=7)
        assert "citation_rate" in summary, "analyze_recalls must include citation_rate"
        rate = summary["citation_rate"].get("hook_auto", None)
        assert rate is not None, "citation_rate must include hook_auto"
        assert abs(rate - 0.5) < 0.01, f"Expected citation_rate 0.5 for hook_auto, got {rate}"

    def test_citation_rate_with_full_uuid_returned_ids(self, log_env):
        """PRODUCTION-SHAPE regression (Phase 4.5): the hook path (retrieve.py)
        logs returned_ids as FULL 36-char uuids while citation rows carry
        8-char prefixes. The rate must normalize both sides to id8 — without
        it, hook_auto citation_rate is structurally 0.0 forever and the
        passive-recall instrumentation reads as a flatline."""
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        full_a = "aabb1122-3344-5566-7788-99aabbccddee"
        full_b = "ccdd3344-5566-7788-99aa-bbccddeeff00"
        now_ts = datetime.now(UTC).isoformat()
        entries = [
            {
                "ts": now_ts, "call_type": "hook_auto", "query": "q",
                "returned_ids": [full_a, full_b], "latency_ms": 100, "source": "hook",
            },
            {
                "ts": now_ts, "type": "citation", "session_id": "sess-x",
                "cited_ids": [full_a[:8]], "source": "mine_delta",
            },
        ]
        self._write_log(log_path, entries)

        summary = rl.analyze_recalls(days=7)
        rate = summary["citation_rate"].get("hook_auto", None)
        assert rate is not None
        assert abs(rate - 0.5) < 0.01, (
            f"Full-uuid returned_ids must normalize to id8 for the intersection; "
            f"expected 0.5, got {rate}"
        )

    def test_top_cited_memories_correct(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        now_ts = datetime.now(UTC).isoformat()
        entries = [
            {"ts": now_ts, "type": "citation", "session_id": "s1", "cited_ids": ["aabb1122", "ccdd3344"], "source": "mine_delta"},
            {"ts": now_ts, "type": "citation", "session_id": "s2", "cited_ids": ["aabb1122"], "source": "mine_delta"},
        ]
        self._write_log(log_path, entries)

        summary = rl.analyze_recalls(days=7)
        assert "top_cited_memories" in summary
        top = dict(summary["top_cited_memories"])
        assert top.get("aabb1122") == 2, f"aabb1122 should appear 2 times, got {top}"
        assert top.get("ccdd3344") == 1, f"ccdd3344 should appear 1 time, got {top}"

    def test_returned_ids_count_proxy(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        now_ts = datetime.now(UTC).isoformat()
        entries = [
            {"ts": now_ts, "call_type": "hook_auto", "query": "q1", "returned_ids": ["a", "b", "c"], "latency_ms": 10, "source": "hook"},
            {"ts": now_ts, "call_type": "tool_search", "query": "q2", "returned_ids": ["d", "e"], "latency_ms": 20, "source": "mcp"},
            # citation row — returned_ids count must NOT include citation rows
            {"ts": now_ts, "type": "citation", "session_id": "s", "cited_ids": ["a"], "source": "mine_delta"},
        ]
        self._write_log(log_path, entries)

        summary = rl.analyze_recalls(days=7)
        assert summary["returned_ids_count"] == 5, f"Expected 5 (3+2), got {summary['returned_ids_count']}"

    def test_citation_rate_key_present_even_when_no_citations(self, log_env):
        rl = log_env["rl"]
        state_dir = log_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        now_ts = datetime.now(UTC).isoformat()
        entries = [
            {"ts": now_ts, "call_type": "hook_auto", "query": "q", "returned_ids": ["a"], "latency_ms": 10, "source": "hook"},
        ]
        self._write_log(log_path, entries)

        summary = rl.analyze_recalls(days=7)
        assert "citation_rate" in summary
        # With no citation rows, citation_rate dict may be empty or 0.0 per type
        # (no cited ids → intersection is empty)
        rate = summary["citation_rate"].get("hook_auto", 0.0)
        assert rate == 0.0, f"citation_rate should be 0.0 when no citations, got {rate}"


# ---------------------------------------------------------------------------
# (d) mine_delta citation scan
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_nested_turns(real_id8: str, fake_hex8: str) -> list[dict]:
    """Build turns where assistant text mentions one real returned id8 and one
    random hex8 that is NOT a returned id. User turns are substantive to pass
    the trivial-delta check.

    Uses the real Claude Code nested schema.
    """
    turns = [
        # User turn with enough content
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    "Please review the architecture design and the memory indexing strategy "
                    "we discussed earlier. The callback pattern and list-of-dicts return type "
                    "should be documented in the codebase so others can follow it consistently. "
                    "The search index needs to support faceted filtering by project and layer. "
                ) * 2,
            },
        },
        # Assistant turn mentioning the real id8 and a fake hex8
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"I reviewed memory {real_id8} which contains the relevant context. "
                            f"The pattern {fake_hex8} is not a memory id but looks similar. "
                            "The architecture decision was to use a priority queue for scheduling. "
                            "I recommend documenting the callback pattern and return type conventions. "
                        ),
                    }
                ],
            },
        },
        # More substantive user + assistant turns
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    "Great analysis. Let's proceed with the implementation plan. "
                    "Make sure the tests cover edge cases for the priority queue logic. "
                ),
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Understood, I'll implement with full test coverage."}],
            },
        },
    ] * 2  # duplicate to ensure enough chars
    return turns


class TestMineDeltaCitationScan:
    """mine_delta.run() emits citation row when assistant mentions a recalled id."""

    def test_only_real_id_cited_not_fake_hex(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.recall_log as rl
        import memem.models as mdl

        # Reload modules to pick up monkeypatched MEMEM_DIR
        importlib.reload(mdl)
        importlib.reload(rl)

        state_dir = mine_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        # The "real" memory id that was recalled in this session
        real_id = "aabb1122ccdd3344"
        real_id8 = real_id[:8]  # "aabb1122"

        # A hex8 that looks like an id but is NOT in any recall row
        fake_hex8 = "deadbeef"

        session_id = "cite-test-session-abc"

        # Pre-populate the recall log with a row for this session returning real_id
        now_ts = datetime.now(UTC).isoformat()
        recall_row = {
            "ts": now_ts,
            "call_type": "tool_search",
            "query": "architecture query",
            "returned_ids": [real_id],
            "latency_ms": 80,
            "source": "mcp",
            "session_id": session_id,
        }
        log_path.write_text(json.dumps(recall_row) + "\n")

        # Mock extract_from_text to return 1 memory so the substantive path runs
        canned_memories = [
            {
                "title": "Architecture decision",
                "project": "test-project",
                "content": "The system uses a priority queue for scheduling tasks efficiently.",
                "importance": 3,
            }
        ]

        def _fake_extract(text, context_hint=""):
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        # Mock episode emission subprocess call
        original_run = subprocess.run

        def _fake_subprocess_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FakeResult:
                    returncode = 0
                    stdout = "The user worked on architecture decisions and scheduling logic."
                    stderr = ""
                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_subprocess_run)

        # Also patch _emit_citations_if_any to use our state_dir
        # (mine_delta._state_dir reads MEMEM_DIR env var which is set)

        # Build transcript with real_id8 and fake_hex8 in assistant text
        transcript = mine_env["tmp_path"] / "session.jsonl"
        turns = _make_nested_turns(real_id8, fake_hex8)
        _write_jsonl(transcript, turns)

        # Run mine_delta
        md.run(session_id=session_id, transcript_path=str(transcript))

        # Read the recall log and find citation rows
        log_lines = log_path.read_text().splitlines()
        citation_rows = []
        for line in log_lines:
            try:
                row = json.loads(line)
                if row.get("type") == "citation":
                    citation_rows.append(row)
            except json.JSONDecodeError:
                pass

        assert len(citation_rows) >= 1, (
            f"Expected at least one citation row to be emitted. "
            f"All rows: {[json.loads(l) for l in log_lines]}"
        )

        # The real id8 must be cited
        all_cited = set()
        for row in citation_rows:
            all_cited.update(row.get("cited_ids", []))

        assert real_id8 in all_cited, (
            f"Real id8 '{real_id8}' must be in cited_ids. Got: {all_cited}"
        )

        # The fake hex8 must NOT be cited
        assert fake_hex8 not in all_cited, (
            f"Fake hex8 '{fake_hex8}' must NOT be cited. Got: {all_cited}"
        )

    def test_no_citation_when_no_match(self, mine_env, monkeypatch):
        """When assistant text has no tokens matching recalled ids, no citation row emitted."""
        import memem.mine_delta as md
        import memem.recall_log as rl
        import memem.models as mdl

        importlib.reload(mdl)
        importlib.reload(rl)

        state_dir = mine_env["state_dir"]
        log_path = state_dir / ".recall_log.jsonl"

        session_id = "no-cite-session-xyz"
        real_id = "ffffffff00000000"

        now_ts = datetime.now(UTC).isoformat()
        recall_row = {
            "ts": now_ts,
            "call_type": "tool_search",
            "query": "some query",
            "returned_ids": [real_id],
            "latency_ms": 50,
            "source": "mcp",
            "session_id": session_id,
        }
        log_path.write_text(json.dumps(recall_row) + "\n")

        canned_memories = [{
            "title": "Test memory",
            "project": "test",
            "content": "Test content about some architectural design decision for the pipeline.",
            "importance": 3,
        }]

        def _fake_extract(text, context_hint=""):
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        original_run = subprocess.run

        def _fake_subprocess_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FakeResult:
                    returncode = 0
                    stdout = "Session summary for no-cite test."
                    stderr = ""
                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_subprocess_run)

        # Assistant text mentions a DIFFERENT hex8 that is not the recalled id
        turns = _make_nested_turns("11223344", "55667788")  # neither matches real_id8
        transcript = mine_env["tmp_path"] / "no-cite-session.jsonl"
        _write_jsonl(transcript, turns)

        md.run(session_id=session_id, transcript_path=str(transcript))

        log_lines = log_path.read_text().splitlines()
        citation_rows = [
            json.loads(l) for l in log_lines
            if json.loads(l).get("type") == "citation"
        ]
        assert len(citation_rows) == 0, (
            f"Expected no citation rows when no match. Got: {citation_rows}"
        )
