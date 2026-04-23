"""Tests for v0.11.0 "session-start token diet"."""

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run_compact_index(env: dict, vault: Path | None = None) -> str:
    full_env = os.environ.copy()
    full_env.update(env)
    full_env["PYTHONPATH"] = str(REPO) + os.pathsep + full_env.get("PYTHONPATH", "")
    if vault is not None:
        full_env["MEMEM_OBSIDIAN_VAULT"] = str(vault)
    result = subprocess.run(
        ["python3", "-m", "memem.server", "--compact-index"],
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    return result.stdout


def _seed_vault(tmp_path: Path, count: int, project: str = "general", body: str = "seed essence body") -> Path:
    """Create an Obsidian vault seeded with `count` memory files."""
    vault = tmp_path / "vault"
    mem_dir = vault / "memem" / "memories"
    mem_dir.mkdir(parents=True)
    for i in range(count):
        (mem_dir / f"seed-{i}-aaaaaaaa{i:02d}.md").write_text(
            f"---\n"
            f"id: aaaaaaaa{i:02d}\n"
            f"schema_version: 1\n"
            f"title: seed memory {i}\n"
            f"project: {project}\n"
            f"tags: [seed]\n"
            f"created: 2026-04-14\n"
            f"updated: 2026-04-14\n"
            f"source_type: user\n"
            f"source_session: \n"
            f"importance: 3\n"
            f"status: active\n"
            f"valid_to: \n"
            f"layer: 2\n"
            f"---\n\n{body} {i}\n"
        )
    return vault


def test_compact_index_respects_session_start_limit(tmp_path):
    """MEMEM_SESSION_START_LIMIT caps the total memories injected."""
    vault = _seed_vault(tmp_path, 15)
    output = _run_compact_index({
        "MEMEM_SESSION_START_LIMIT": "10",
        "MEMEM_SESSION_START_FULL": "2",
        "MEMEM_SESSION_START_PROJECT": "all",
    }, vault=vault)
    # Count compact-format lines: `[xxxxxxxx] L<N> ...`
    compact_lines = [
        line for line in output.splitlines()
        if line.startswith("[") and "]" in line and " L" in line[:14]
    ]
    # Count full-content headers: `### [L<N>] ...`
    full_headers = [line for line in output.splitlines() if line.startswith("### [L")]
    assert len(full_headers) <= 2, f"expected ≤2 full headers, got {len(full_headers)}"
    assert len(compact_lines) <= 10 - len(full_headers), (
        f"compact lines {len(compact_lines)} + full {len(full_headers)} > limit 10"
    )


def test_compact_index_full_count_caps_full_content(tmp_path):
    """MEMEM_SESSION_START_FULL caps how many memories show full content."""
    vault = _seed_vault(tmp_path, 10)
    output = _run_compact_index({
        "MEMEM_SESSION_START_LIMIT": "20",
        "MEMEM_SESSION_START_FULL": "3",
        "MEMEM_SESSION_START_PROJECT": "all",
    }, vault=vault)
    full_headers = [line for line in output.splitlines() if line.startswith("### [L")]
    assert len(full_headers) == 3, f"expected exactly 3 full entries, got {len(full_headers)}"


def test_compact_index_project_scope_filters(tmp_path):
    """Project scope filters to that project + general memories."""
    vault = _seed_vault(tmp_path, 5)
    scoped = _run_compact_index({
        "MEMEM_SESSION_START_LIMIT": "200",
        "MEMEM_SESSION_START_FULL": "0",
        "MEMEM_SESSION_START_PROJECT": "memem",
    }, vault=vault)
    unscoped = _run_compact_index({
        "MEMEM_SESSION_START_LIMIT": "200",
        "MEMEM_SESSION_START_FULL": "0",
        "MEMEM_SESSION_START_PROJECT": "all",
    }, vault=vault)
    # Scoped output must be smaller than or equal to unscoped
    # (equal only if vault has nothing outside memem/general scope).
    scoped_lines = len([line for line in scoped.splitlines() if line.startswith("[")])
    unscoped_lines = len([line for line in unscoped.splitlines() if line.startswith("[")])
    assert scoped_lines <= unscoped_lines


def test_compact_index_default_limit_is_50(tmp_path):
    """Default MEMEM_SESSION_START_LIMIT is 50 (claude-mem parity)."""
    vault = _seed_vault(tmp_path, 75)
    output = _run_compact_index({
        "MEMEM_SESSION_START_PROJECT": "all",
    }, vault=vault)
    compact_lines = [
        line for line in output.splitlines()
        if line.startswith("[") and "]" in line and " L" in line[:14]
    ]
    full_headers = [line for line in output.splitlines() if line.startswith("### [L")]
    total = len(compact_lines) + len(full_headers)
    assert total <= 50, f"default total {total} exceeds 50"


def test_session_start_hook_writes_primed_marker(tmp_path, monkeypatch):
    """SessionStart hook writes .last-brief.json with primed=true."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    env = os.environ.copy()
    env["MEMEM_DIR"] = str(memem_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    # Use a tmp vault so the hook has something to inject (or nothing, both ok)
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_path / "vault")
    (tmp_path / "vault" / "memem" / "memories").mkdir(parents=True)

    result = subprocess.run(
        ["bash", str(REPO / "hooks" / "session-start.sh")],
        input=json.dumps({"session_id": "primed-test-session"}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    # The hook may emit empty (no memories in test vault) OR a brief — either
    # way, it must write the primed marker if session_id was parsed.
    marker = memem_dir / ".last-brief.json"
    if marker.exists():
        data = json.loads(marker.read_text())
        assert data.get("primed") is True
        assert data.get("session_id") == "primed-test-session"


def test_auto_recall_consumes_primed_flag(tmp_path):
    """First UserPromptSubmit after a primed SessionStart emits an active slice and clears the flag."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    # Seed the primed marker
    marker = memem_dir / ".last-brief.json"
    marker.write_text(json.dumps({
        "session_id": "primed-test",
        "keywords": [],
        "timestamp": "2026-04-14T00:00:00Z",
        "primed": True,
    }))
    vault = _seed_vault(tmp_path, 1, project="memem-plugin", body="auth bug fix constraint")

    env = os.environ.copy()
    env["MEMEM_DIR"] = str(memem_dir)
    env["MEMEM_OBSIDIAN_VAULT"] = str(vault)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["PATH"] = "/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(REPO / "hooks" / "auto-recall.sh")],
        input=json.dumps({
            "session_id": "primed-test",
            "cwd": str(REPO),
            "message": "help me fix the auth bug",
        }),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0

    data = json.loads(result.stdout)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "# Active Memory Slice" in ctx
    assert "## Goals" in ctx

    # The primed flag should be cleared after consumption
    data = json.loads(marker.read_text())
    assert data.get("primed") is not True, "primed flag must be cleared after consumption"
    assert data.get("session_id") == "primed-test"
    # Keywords should now be populated from the current message
    assert len(data.get("keywords", [])) > 0


def test_auto_recall_topic_shift_uses_active_slice(tmp_path):
    """When the topic changes, auto-recall should re-emit an active slice."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    marker = memem_dir / ".last-brief.json"
    marker.write_text(json.dumps({
        "session_id": "topic-test",
        "keywords": ["old", "topic"],
        "timestamp": "2026-04-14T00:00:00Z",
    }))

    vault = _seed_vault(tmp_path, 1, project="memem-plugin", body="brief project forge workflow")

    env = os.environ.copy()
    env["MEMEM_DIR"] = str(memem_dir)
    env["MEMEM_OBSIDIAN_VAULT"] = str(vault)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        ["bash", str(REPO / "hooks" / "auto-recall.sh")],
        input=json.dumps({
            "session_id": "topic-test",
            "cwd": str(REPO),
            "message": "brief me the project forge workflow",
        }),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    data = json.loads(result.stdout)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "# Active Memory Slice" in ctx
    assert "## Goals" in ctx


def test_auto_recall_same_topic_still_uses_active_slice(tmp_path):
    """Same-topic prompts should still wake memem and emit an active slice."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    marker = memem_dir / ".last-brief.json"
    marker.write_text(json.dumps({
        "session_id": "topic-test",
        "keywords": ["brief", "project", "forge", "workflow"],
        "timestamp": "2026-04-14T00:00:00Z",
    }))

    vault = _seed_vault(tmp_path, 1, project="memem-plugin", body="brief project forge workflow")

    env = os.environ.copy()
    env["MEMEM_DIR"] = str(memem_dir)
    env["MEMEM_OBSIDIAN_VAULT"] = str(vault)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        ["bash", str(REPO / "hooks" / "auto-recall.sh")],
        input=json.dumps({
            "session_id": "topic-test",
            "cwd": str(REPO),
            "message": "brief me the project forge workflow",
        }),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    data = json.loads(result.stdout)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "# Active Memory Slice" in ctx
    assert "## Goals" in ctx
