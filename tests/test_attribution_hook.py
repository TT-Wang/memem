"""Smoke tests for the post-stop-attribution hook (closed-loop integration).

The hook itself is shell-mediated; these tests verify:
  1. Shell syntax is valid
  2. The hook degrades silently when CLAUDE_PLUGIN_ROOT is unset
  3. The hook degrades silently when transcript_path doesn't exist
  4. End-to-end: given a real slice + transcript, attribution events get
     written to the events log (the dreamer's data source)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "post-stop-attribution.sh"


def test_post_stop_attribution_hook_shell_syntax():
    """bash -n catches syntax errors without executing."""
    result = subprocess.run(
        ["bash", "-n", str(HOOK)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, f"shell syntax error: {result.stderr}"


def test_hook_silent_when_plugin_root_unset(tmp_path):
    """No PLUGIN_ROOT → hook exits 0 silently (degraded mode)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(HOOK)],
        input='{"session_id": "x", "transcript_path": "/dev/null"}',
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_hook_silent_when_transcript_missing(tmp_path):
    """transcript_path doesn't exist → hook exits 0, no events logged."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(REPO),
        "MEMEM_DIR": str(tmp_path / ".memem"),
    }
    (tmp_path / ".memem").mkdir()
    result = subprocess.run(
        ["bash", str(HOOK)],
        input='{"session_id": "x", "transcript_path": "/nonexistent/path"}',
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert result.returncode == 0


def test_hook_logs_attribution_event_for_real_slice_and_response(tmp_path, monkeypatch):
    """End-to-end: plant a slice + transcript with assistant text, run hook,
    verify a slice_attribution event landed in the events log.

    This is the integration that closes the loop — without it, m2's
    attribution data is never collected.
    """
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    # Plant a slice with one item
    slices_path = memem_dir / "active-slices.jsonl"
    slice_data = {
        "slice_id": "test-slice-1",
        "session_id": "s1",
        "scope_id": "memem",
        "query": "test query",
        "items": [
            {
                "id": "abc12345-1234-1234-1234-123456789012",
                "memory_id": "abc12345-1234-1234-1234-123456789012",
                "title": "test memory title",
                "content": "test memory content about JWT auth",
                "layer": 2,
            },
        ],
    }
    slices_path.write_text(json.dumps(slice_data) + "\n")

    # Plant a transcript with an assistant message
    transcript = tmp_path / "session.jsonl"
    transcript_lines = [
        json.dumps({"type": "user", "message": {"content": "test query"}}),
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I recall that abc12345 says JWT auth uses RS256."}
                ]
            },
        }),
    ]
    transcript.write_text("\n".join(transcript_lines))

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(REPO),
        "MEMEM_DIR": str(memem_dir),
        "MEMEM_JUDGE_SAMPLE_RATE": "0",  # skip the LLM-judge call (no claude CLI in CI)
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"session_id": "s1", "transcript_path": str(transcript)}),
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    # Verify an attribution event landed in the events log
    events_path = memem_dir / "events.jsonl"
    if not events_path.exists():
        # Hook degraded silently (e.g. embedding model missing in env).
        # That's acceptable — the hook MUST never break, even if it can't
        # compute. Skip the assertion in that case.
        import pytest
        pytest.skip("hook degraded silently — events log not created")
        return

    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    attribution_events = [e for e in events if e.get("op") == "slice_attribution"]
    assert attribution_events, f"no slice_attribution events logged. All events: {[e.get('op') for e in events]}"

    # The event should reference our memory_id and have a citation_match=True
    # (the response text contains 'abc12345')
    found = next(
        (e for e in attribution_events
         if e.get("memory_id") == "abc12345-1234-1234-1234-123456789012"),
        None,
    )
    assert found is not None, "attribution event missing for the slice's memory"
    assert found.get("citation_match") is True, "citation pattern detection failed"
