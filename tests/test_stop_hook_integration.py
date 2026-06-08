"""Integration tests for hooks/stop-mine.sh (v2.1.0 event-triggered mining).

Test 1: The hook emits valid Stop hook JSON and exits 0 for a well-formed input
        (with opted-in marker present).
Test 2: The hook fires mine_delta when session_id is provided and opted-in marker
        exists. We detect the launch by watching for a sentinel or checking the
        stdout.  Since mine_delta runs detached, we only check that the hook exits
        cleanly and emits valid JSON — actual mining is left to test_mine_delta.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STOP_HOOK = REPO_ROOT / "hooks" / "stop-mine.sh"


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Minimal env for hook invocation with site-packages preserved."""
    import site
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["MEMEM_DIR"] = str(tmp_path / ".memem")
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_path / "obsidian-brain")
    env["MEMEM_PYTHON"] = sys.executable
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        + os.pathsep
        + site.getusersitepackages()
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    # Suppress recursion guard so the hook doesn't bail early
    env.pop("MEMEM_HOOK_DISABLE", None)
    return env


# ---------------------------------------------------------------------------
# Test 1: hook emits valid JSON and exits 0 (no opt-in marker → no-op path)
# ---------------------------------------------------------------------------


def test_stop_hook_exits_zero_without_opt_in(tmp_path):
    """Without the .miner-opted-in marker the hook must still exit 0 and emit
    a valid Stop hook JSON response."""
    env = _base_env(tmp_path)
    (tmp_path / ".memem").mkdir(parents=True, exist_ok=True)
    # No .miner-opted-in created — hook should no-op and return valid JSON

    hook_input = json.dumps({
        "session_id": "test-session-abc",
        "transcript_path": "/nonexistent/abc.jsonl",
    })

    result = subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, (
        f"hook exited non-zero without opt-in: stderr={result.stderr}"
    )
    # Must emit valid JSON on stdout
    payload = json.loads(result.stdout.strip())
    assert isinstance(payload, dict), "hook must emit a JSON object"
    # Stop hook shape
    assert "hookSpecificOutput" in payload
    assert payload["hookSpecificOutput"].get("hookEventName") == "Stop"


# ---------------------------------------------------------------------------
# Test 2: hook with opt-in emits valid JSON, exits 0, and has correct structure
# ---------------------------------------------------------------------------


def test_stop_hook_with_opt_in_emits_valid_json(tmp_path):
    """With the .miner-opted-in marker present and a valid session_id, the hook
    spawns a detached mine_delta child (fire-and-forget) and emits valid JSON.

    We verify: exit code 0, valid JSON output, correct Stop hook shape.
    We do NOT wait for mine_delta since it's detached — its output is /dev/null.
    """
    env = _base_env(tmp_path)
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir(parents=True, exist_ok=True)
    # Create opt-in marker
    (memem_dir / ".miner-opted-in").touch()

    # Create a minimal JSONL transcript so mine_delta has something to read
    session_id = "test-session-xyz"
    transcript_path = tmp_path / "transcript" / f"{session_id}.jsonl"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        json.dumps({
            "type": "user",
            "message": {"content": "hello"},
        }) + "\n"
    )

    hook_input = json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript_path),
    })

    result = subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, (
        f"hook exited non-zero with opt-in: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Must emit valid JSON
    payload = json.loads(result.stdout.strip())
    assert isinstance(payload, dict), "hook must emit a JSON object"
    assert "hookSpecificOutput" in payload
    assert payload["hookSpecificOutput"].get("hookEventName") == "Stop"
    # Hook must return quickly (mine_delta is detached)
    assert "additionalContext" in payload["hookSpecificOutput"]
