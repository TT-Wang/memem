"""Tests for eval-replay capture + replay loop.

Pins three contracts:
  1. Capture is opt-in (no rows written when MEMEM_EVAL_CAPTURE is unset)
  2. PII scrubbing redacts the high-value patterns (emails, JWTs, API keys, CC)
  3. Replay computes Jaccard@k + top-1 stability + latency Δ correctly
"""
from __future__ import annotations

import importlib
import json

# ---------------------------------------------------------------------------
# PII scrubbing
# ---------------------------------------------------------------------------


def test_scrub_redacts_email():
    from memem.eval_capture import scrub_pii
    out = scrub_pii("contact me at alice@example.com about the leak")
    assert "alice@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_scrub_redacts_jwt():
    from memem.eval_capture import scrub_pii
    fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out = scrub_pii(f"token: {fake_jwt}")
    assert fake_jwt not in out
    assert "[REDACTED_JWT]" in out


def test_scrub_redacts_anthropic_api_key():
    from memem.eval_capture import scrub_pii
    out = scrub_pii("key: sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345")
    assert "[REDACTED_APIKEY]" in out
    assert "sk-ant-api03" not in out


def test_scrub_redacts_credit_card_only_when_luhn_valid():
    from memem.eval_capture import scrub_pii
    # 4111-1111-1111-1111 is the canonical Luhn-valid test card
    out = scrub_pii("card 4111-1111-1111-1111 expires soon")
    assert "[REDACTED_CC]" in out
    assert "4111-1111-1111-1111" not in out


def test_scrub_preserves_random_long_digit_runs_that_arent_credit_cards():
    """Random 16-digit hex IDs that fail Luhn must stay intact (low false-positive)."""
    from memem.eval_capture import scrub_pii
    out = scrub_pii("hash 1234567890123456 belongs to memory")
    # 1234567890123456 fails Luhn → should pass through unchanged
    assert "1234567890123456" in out


def test_scrub_redacts_github_pat():
    from memem.eval_capture import scrub_pii
    out = scrub_pii("export GH_TOKEN=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
    assert "[REDACTED_GITHUB_PAT]" in out
    assert "ghp_" not in out


# ---------------------------------------------------------------------------
# Capture (opt-in gate + file write)
# ---------------------------------------------------------------------------


def test_capture_is_off_by_default(tmp_cortex_dir, monkeypatch):
    """No rows written when MEMEM_EVAL_CAPTURE is unset."""
    monkeypatch.delenv("MEMEM_EVAL_CAPTURE", raising=False)
    from memem import eval_capture
    importlib.reload(eval_capture)
    eval_capture.capture(query="test", mode="search", memory_ids=["a", "b"])
    assert not eval_capture.EVAL_CAPTURE_FILE.exists(), \
        "capture must not write when MEMEM_EVAL_CAPTURE is unset"


def test_capture_writes_when_opted_in(tmp_cortex_dir, monkeypatch):
    """With MEMEM_EVAL_CAPTURE=1, capture writes a record."""
    monkeypatch.setenv("MEMEM_EVAL_CAPTURE", "1")
    from memem import eval_capture
    importlib.reload(eval_capture)
    eval_capture.capture(
        query="explain auto-recall",
        mode="search",
        scope_id="cortex-plugin",
        limit=10,
        memory_ids=["abc12345", "deadbeef"],
        latency_ms=42.5,
    )
    assert eval_capture.EVAL_CAPTURE_FILE.exists()
    lines = eval_capture.EVAL_CAPTURE_FILE.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["mode"] == "search"
    assert row["query"] == "explain auto-recall"
    assert row["memory_ids"] == ["abc12345", "deadbeef"]
    assert row["limit"] == 10
    assert row["latency_ms"] == 42.5
    assert row["schema_version"] == 1


def test_capture_scrubs_query_pii(tmp_cortex_dir, monkeypatch):
    """The captured query has PII scrubbed before disk write."""
    monkeypatch.setenv("MEMEM_EVAL_CAPTURE", "1")
    from memem import eval_capture
    importlib.reload(eval_capture)
    eval_capture.capture(
        query="lookup ticket for alice@example.com please",
        mode="recall",
        memory_ids=[],
    )
    row = json.loads(eval_capture.EVAL_CAPTURE_FILE.read_text().strip())
    assert "alice@example.com" not in row["query"]
    assert "[REDACTED_EMAIL]" in row["query"]


def test_capture_silent_on_io_error(tmp_path, monkeypatch):
    """Recall path must never break because eval-capture failed."""
    monkeypatch.setenv("MEMEM_EVAL_CAPTURE", "1")
    monkeypatch.setenv("MEMEM_DIR", str(tmp_path / "no_perms"))
    from memem import eval_capture
    importlib.reload(eval_capture)
    # Make the parent dir read-only AFTER the capture tries to create child
    eval_capture.EVAL_CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    eval_capture.EVAL_CAPTURE_FILE.parent.chmod(0o555)
    try:
        # Should not raise
        eval_capture.capture(query="x", mode="search", memory_ids=[])
    finally:
        eval_capture.EVAL_CAPTURE_FILE.parent.chmod(0o755)


# ---------------------------------------------------------------------------
# Jaccard / top-1 / replay math
# ---------------------------------------------------------------------------


def test_jaccard_identical():
    from memem.eval_replay import jaccard
    assert jaccard(["a", "b", "c"], ["a", "b", "c"]) == 1.0


def test_jaccard_disjoint():
    from memem.eval_replay import jaccard
    assert jaccard(["a", "b"], ["c", "d"]) == 0.0


def test_jaccard_partial():
    from memem.eval_replay import jaccard
    # |{a,b} ∩ {a,c}| / |{a,b,c}| = 1/3
    j = jaccard(["a", "b"], ["a", "c"])
    assert abs(j - (1 / 3)) < 1e-9


def test_jaccard_at_k_caps_lists():
    from memem.eval_replay import jaccard
    # With k=2: compare {a,b} vs {a,b} → 1.0 even though full lists differ at index 2
    assert jaccard(["a", "b", "c"], ["a", "b", "z"], k=2) == 1.0


def test_jaccard_both_empty_is_one():
    from memem.eval_replay import jaccard
    assert jaccard([], []) == 1.0


def test_top_1_stable_match():
    from memem.eval_replay import top_1_stable
    assert top_1_stable(["a", "b"], ["a", "x"]) is True


def test_top_1_stable_mismatch():
    from memem.eval_replay import top_1_stable
    assert top_1_stable(["a", "b"], ["b", "a"]) is False


def test_top_1_stable_empty_is_false():
    from memem.eval_replay import top_1_stable
    assert top_1_stable([], ["a"]) is False
    assert top_1_stable(["a"], []) is False


# ---------------------------------------------------------------------------
# End-to-end replay (with a baseline file)
# ---------------------------------------------------------------------------


def test_replay_against_empty_baseline_returns_n_zero(tmp_path):
    """Empty baseline → n=0, no errors."""
    from memem.eval_replay import replay
    empty = tmp_path / "empty.ndjson"
    empty.write_text("")
    result = replay(empty)
    assert result["n"] == 0


def test_replay_format_report_renders_clean_when_empty(tmp_path):
    from memem.eval_replay import format_replay_report
    out = format_replay_report({"n": 0, "k": 5})
    assert "No baseline records" in out


def test_replay_report_includes_jaccard_and_top1(tmp_path, monkeypatch):
    """Stub _re_run_query so we can assert the math without a real vault."""
    from memem import eval_replay

    baseline = tmp_path / "baseline.ndjson"
    rows = [
        {"ts": "2026-05-07T00:00:00Z", "mode": "search", "scope_id": "default",
         "query": "q1", "memory_ids": ["a", "b", "c"], "limit": 5, "latency_ms": 10.0},
        {"ts": "2026-05-07T00:00:01Z", "mode": "search", "scope_id": "default",
         "query": "q2", "memory_ids": ["x", "y"], "limit": 5, "latency_ms": 12.0},
    ]
    baseline.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    # Stub the recall side so we control current_ids
    fake_results = {
        "q1": (["a", "b", "z"], 11.0),  # top-1 stable, jaccard=2/4=0.5
        "q2": (["y", "x"], 13.0),       # top-1 NOT stable, jaccard=2/2=1.0
    }
    def fake_re_run(row):
        return fake_results[row["query"]]
    monkeypatch.setattr(eval_replay, "_re_run_query", fake_re_run)

    result = eval_replay.replay(baseline, k=5)
    assert result["n"] == 2
    # Mean jaccard = (0.5 + 1.0) / 2 = 0.75
    assert abs(result["mean_jaccard_at_k"] - 0.75) < 1e-3
    # Top-1 stability: 1 of 2 = 0.5
    assert abs(result["top_1_stability"] - 0.5) < 1e-3
    # Latency Δ: ((11-10) + (13-12)) / 2 = 1.0ms
    assert abs(result["mean_latency_delta_ms"] - 1.0) < 1e-3
    # Worst regression should be q1 (jaccard=0.5)
    assert result["worst_regressions"][0]["query"] == "q1"


def test_replay_handles_query_error_gracefully(tmp_path, monkeypatch):
    """A query that raises during re-run is recorded as jaccard=0, no propagation."""
    from memem import eval_replay

    baseline = tmp_path / "baseline.ndjson"
    baseline.write_text(json.dumps({
        "ts": "2026-05-07T00:00:00Z", "mode": "search", "scope_id": "default",
        "query": "kaboom", "memory_ids": ["a"], "limit": 5, "latency_ms": 10.0,
    }) + "\n")

    def boom(row):
        raise RuntimeError("simulated index corruption")
    monkeypatch.setattr(eval_replay, "_re_run_query", boom)

    result = eval_replay.replay(baseline, k=5)
    assert result["n"] == 1
    assert result["mean_jaccard_at_k"] == 0.0
    # The error must be recorded in worst_regressions
    assert result["worst_regressions"][0].get("error")


# ---------------------------------------------------------------------------
# Integration: capture writes during memory_search when opted in
# ---------------------------------------------------------------------------


def test_memory_search_writes_capture_when_enabled(tmp_cortex_dir, tmp_vault, monkeypatch):
    """End-to-end: opt in, call memory_search, assert capture file exists with the right shape."""
    monkeypatch.setenv("MEMEM_EVAL_CAPTURE", "1")
    # Reload eval_capture so it picks up the new MEMEM_DIR + env
    from memem import eval_capture
    importlib.reload(eval_capture)

    from memem.recall import memory_search
    # tmp_vault has no memories matching this query, so result will be the
    # "No memories found" string, but capture must still fire with []
    memory_search("nonexistent query xyzzy", limit=5)

    assert eval_capture.EVAL_CAPTURE_FILE.exists()
    rows = [json.loads(line) for line in eval_capture.EVAL_CAPTURE_FILE.read_text().splitlines() if line.strip()]
    assert len(rows) >= 1
    last = rows[-1]
    assert last["mode"] == "search"
    assert "nonexistent query xyzzy" in last["query"]
    assert isinstance(last["memory_ids"], list)
    assert isinstance(last["latency_ms"], int | float)
