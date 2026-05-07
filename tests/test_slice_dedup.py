"""Tests for m3: slice de-duplication across turns (memem v1.4.0).

The auto-recall hook hashes the assembled slice body and emits a placeholder
on the second invocation when the body is identical. These tests drive that
behaviour through real subprocess invocations of hooks/auto-recall.sh,
stubbing the `memem.server slice` sub-command with a tiny wrapper script
so the tests are self-contained and do not require a real vault or
sentence-transformer model.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "auto-recall.sh"

PLACEHOLDER = "[Active Memory Slice unchanged from previous turn — see slice above]"

# Fixed slice body that the stub script will echo.
STUB_SLICE_BODY = "# Active Memory Slice\n\n## Goals\n- test goal\n"


def _make_stub_cwd(stub_cwd: Path, body: str) -> None:
    """Seed *stub_cwd* with a minimal memem package whose server.py handles
    `python3 -m memem.server slice …` by printing *body* and exiting 0.

    Python always has '' (CWD) at the front of sys.path.  The inner
    subprocess in run_active_slice also prepends plugin_root to PYTHONPATH,
    but '' (CWD) wins for the subprocess IF we set cwd=stub_cwd when running
    the outer bash hook — bash itself doesn't change directory, but the Python
    invocation via `sys.executable` inherits the same CWD.
    """
    pkg = stub_cwd / "memem"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "server.py").write_text(
        "import sys\n"
        "if __name__ == '__main__':\n"
        "    args = sys.argv[1:]\n"
        "    if 'slice' in args:\n"
        f"        print({body!r}, end='')\n"
        "        sys.exit(0)\n"
        "    sys.exit(1)\n"
    )


def _run_hook(
    tmp_path: Path,
    memem_dir: Path,
    session_id: str,
    stub_body: str = STUB_SLICE_BODY,
) -> subprocess.CompletedProcess:
    # The stub CWD has a fake memem package.  Python's '' sys.path entry
    # resolves to CWD, so the inner `python3 -m memem.server slice` picks up
    # the stub even before PYTHONPATH entries.
    stub_cwd = tmp_path / "stub_cwd"
    stub_cwd.mkdir(exist_ok=True)
    _make_stub_cwd(stub_cwd, stub_body)

    env = os.environ.copy()
    env["MEMEM_DIR"] = str(memem_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(stub_cwd)
    env["PYTHONPATH"] = str(stub_cwd)

    payload = json.dumps({
        "session_id": session_id,
        "cwd": str(REPO),
        "user_prompt": "help me implement the feature",
    })

    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=35,
        env=env,
        cwd=str(stub_cwd),
    )


# ---------------------------------------------------------------------------
# Test 1: same session_id + identical body → 2nd call emits placeholder
# ---------------------------------------------------------------------------

def test_dedup_same_session_identical_body(tmp_path):
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    r1 = _run_hook(tmp_path, memem_dir, session_id="sess-dedup-1")
    assert r1.returncode == 0, f"first call failed: {r1.stderr}"
    d1 = json.loads(r1.stdout)
    ctx1 = d1["hookSpecificOutput"]["additionalContext"]
    # First call: full body
    assert STUB_SLICE_BODY.strip() in ctx1, f"expected full body on first call, got: {ctx1!r}"
    assert ctx1 != PLACEHOLDER

    r2 = _run_hook(tmp_path, memem_dir, session_id="sess-dedup-1")
    assert r2.returncode == 0, f"second call failed: {r2.stderr}"
    d2 = json.loads(r2.stdout)
    ctx2 = d2["hookSpecificOutput"]["additionalContext"]
    # Second call: placeholder
    assert ctx2 == PLACEHOLDER, f"expected placeholder on second call, got: {ctx2!r}"


# ---------------------------------------------------------------------------
# Test 2: same session_id + DIFFERENT body → both emit full content
# ---------------------------------------------------------------------------

def test_dedup_same_session_different_body(tmp_path):
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    body_a = "# Active Memory Slice\n\n## Goals\n- alpha goal\n"
    body_b = "# Active Memory Slice\n\n## Goals\n- beta goal\n"

    r1 = _run_hook(tmp_path, memem_dir, session_id="sess-dedup-2", stub_body=body_a)
    assert r1.returncode == 0
    ctx1 = json.loads(r1.stdout)["hookSpecificOutput"]["additionalContext"]
    assert body_a.strip() in ctx1
    assert ctx1 != PLACEHOLDER

    r2 = _run_hook(tmp_path, memem_dir, session_id="sess-dedup-2", stub_body=body_b)
    assert r2.returncode == 0
    ctx2 = json.loads(r2.stdout)["hookSpecificOutput"]["additionalContext"]
    assert body_b.strip() in ctx2
    assert ctx2 != PLACEHOLDER


# ---------------------------------------------------------------------------
# Test 3: fresh session_id (no prior hash file) → always emits full body
# ---------------------------------------------------------------------------

def test_dedup_fresh_session_emits_full(tmp_path):
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    # Ensure no stale hash files exist
    hash_dir = memem_dir / ".last-slice-hashes"
    assert not hash_dir.exists()

    r = _run_hook(tmp_path, memem_dir, session_id="brand-new-session-xyz")
    assert r.returncode == 0, f"hook failed: {r.stderr}"
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert STUB_SLICE_BODY.strip() in ctx
    assert ctx != PLACEHOLDER


# ---------------------------------------------------------------------------
# Test 4: missing / unreadable hash dir → silent fallback to full body
# ---------------------------------------------------------------------------

def test_dedup_unreadable_hash_dir_fallback(tmp_path):
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    # Create the hash dir and make it unreadable
    hash_dir = memem_dir / ".last-slice-hashes"
    hash_dir.mkdir()
    hash_dir.chmod(0o000)

    try:
        r = _run_hook(tmp_path, memem_dir, session_id="sess-unreadable")
        assert r.returncode == 0, f"hook failed (should not crash): {r.stderr}"
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        # Should get full body (silent fallback), not crash
        assert STUB_SLICE_BODY.strip() in ctx
    finally:
        # Restore permissions so tmp_path cleanup works
        hash_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# Test 5: user_prompt field gets non-empty additionalContext on first invocation
# (pin the existing contract from test_v011.py::test_auto_recall_reads_official_user_prompt_field)
# ---------------------------------------------------------------------------

def test_user_prompt_field_non_empty_first_invocation(tmp_path):
    """First invocation for a fresh session must return non-empty additionalContext."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    r = _run_hook(tmp_path, memem_dir, session_id="user-prompt-field-test")
    assert r.returncode == 0, f"hook failed: {r.stderr}"
    data = json.loads(r.stdout)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert ctx.strip(), (
        "auto-recall returned empty additionalContext on first invocation. "
        "The user_prompt field must produce a non-empty active slice."
    )
    assert ctx != PLACEHOLDER, (
        "placeholder must not appear on the very first invocation (no prior hash)."
    )


# ---------------------------------------------------------------------------
# Test 6 (m1): working memory changes do NOT bust the slice dedup cache
# ---------------------------------------------------------------------------

def test_dedup_hash_independent_of_working_memory(tmp_path):
    """When working_memory.md changes but slice body is identical, the 2nd call
    must still emit the dedup placeholder (not the full slice body).

    Key invariant: dedup hash is keyed on slice body only; working-memory
    updates must not cause a full re-emit of the slice.
    """
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    session_id = "sess-wm-independent"

    # First call — no working_memory.md yet; full slice emitted + hash committed.
    r1 = _run_hook(tmp_path, memem_dir, session_id=session_id)
    assert r1.returncode == 0, f"first call failed: {r1.stderr}"
    ctx1 = json.loads(r1.stdout)["hookSpecificOutput"]["additionalContext"]
    assert STUB_SLICE_BODY.strip() in ctx1, f"expected full body on first call, got: {ctx1!r}"
    assert PLACEHOLDER not in ctx1

    # Now write a working_memory.md with content.
    wm_content = (
        "## current_task\n\nfix the auth bug\n\n"
        "## active_hypothesis\n\n\n\n"
        "## last_3_actions\n\n- wrote tests\n\n"
        "## stuck_on\n\n\n\n"
        "## decided_this_session\n\n\n\n"
    )
    (memem_dir / "working_memory.md").write_text(wm_content, encoding="utf-8")

    # Second call — same slice body but working_memory.md changed.
    # The dedup hash should still be a hit → placeholder is emitted for the SLICE part.
    # Working memory will be prepended, so the full output is:
    #   <working block> + PLACEHOLDER
    # The slice itself must NOT be re-emitted as full content.
    r2 = _run_hook(tmp_path, memem_dir, session_id=session_id)
    assert r2.returncode == 0, f"second call failed: {r2.stderr}"
    ctx2 = json.loads(r2.stdout)["hookSpecificOutput"]["additionalContext"]

    # Working memory block should be present in the output
    assert "Working Memory" in ctx2, (
        f"working_memory block should be in output on second call. Got: {ctx2!r}"
    )
    # The PLACEHOLDER must be in the output (slice was not re-emitted)
    assert PLACEHOLDER in ctx2, (
        f"dedup placeholder must appear when slice body is unchanged, even if "
        f"working_memory.md changed. Got:\n{ctx2!r}"
    )
    # The raw STUB_SLICE_BODY must NOT be in the output (it was deduped)
    assert STUB_SLICE_BODY.strip() not in ctx2, (
        f"full slice body must not re-appear when dedup hash matches. Got:\n{ctx2!r}"
    )
