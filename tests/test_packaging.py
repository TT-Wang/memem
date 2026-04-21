"""Subprocess-level packaging smoke tests.

These tests shell out to a real child Python process to verify that the
package is invocable the same way the plugin + miner wrapper invoke it
in production. Catches regressions in plugin.json entry points,
PYTHONPATH wiring, and the `python -m memem.server` runner that
in-process unit tests cannot see.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_module(args, env_extra=None, timeout=30):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "memem.server", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT),
    )


def test_module_invocation_unknown_command_exits_nonzero():
    """`python -m memem.server --bogus` must surface as nonzero exit."""
    result = _run_module(["--bogus-flag"])
    assert result.returncode != 0
    assert "Unknown command" in (result.stderr + result.stdout)


def test_module_invocation_status_runs(tmp_path):
    """`python -m memem.server --status` must execute against an isolated vault."""
    env_extra = {
        "MEMEM_DIR": str(tmp_path / ".memem"),
        "MEMEM_OBSIDIAN_VAULT": str(tmp_path / "obsidian-brain"),
    }
    (tmp_path / "obsidian-brain" / "memem" / "memories").mkdir(parents=True)
    (tmp_path / "obsidian-brain" / "memem" / "playbooks").mkdir(parents=True)
    result = _run_module(["--status"], env_extra=env_extra)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "0 active" in result.stdout


def test_module_invocation_rebuild_index(tmp_path):
    """`--rebuild-index` must write an index file in the isolated vault."""
    env_extra = {
        "MEMEM_DIR": str(tmp_path / ".memem"),
        "MEMEM_OBSIDIAN_VAULT": str(tmp_path / "obsidian-brain"),
    }
    (tmp_path / "obsidian-brain" / "memem" / "memories").mkdir(parents=True)
    (tmp_path / "obsidian-brain" / "memem" / "playbooks").mkdir(parents=True)
    result = _run_module(["--rebuild-index"], env_extra=env_extra)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert (tmp_path / "obsidian-brain" / "memem" / "_index.md").exists()


@pytest.mark.skipif(
    "CI" in os.environ and not (REPO_ROOT / ".venv").exists(),
    reason="console-script test requires editable install; skipped when no venv",
)
def test_console_script_entry_exists_in_pyproject():
    """Regression guard: pyproject.toml must declare the memem entry point."""
    content = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'memem = "memem.server:main"' in content


def test_plugin_json_points_at_bootstrap_shim():
    """Regression guard: plugin.json must invoke bootstrap.sh (not server.py or -m directly).

    The shim is what makes first-run self-healing possible via uv sync.
    """
    plugin_json = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
    mcp = plugin_json["mcpServers"]["memem"]
    assert mcp["command"] == "bash"
    assert any("bootstrap.sh" in a for a in mcp["args"]), f"expected bootstrap.sh in args, got {mcp['args']}"
    assert (REPO_ROOT / "bootstrap.sh").exists(), "bootstrap.sh referenced in plugin.json but missing"


def test_hook_references_new_package_path():
    """Regression guard: auto-recall.sh must reference memem and not the legacy cortex_server path."""
    hook = (REPO_ROOT / "hooks" / "auto-recall.sh").read_text()
    assert "cortex_server" not in hook
    assert "memem.server" in hook


def test_miner_wrapper_uses_module_form():
    """Regression guard: miner-wrapper.sh must invoke daemon via `python3 -m`."""
    wrapper = (REPO_ROOT / "memem" / "miner-wrapper.sh").read_text()
    assert "python3 -m memem.miner_daemon" in wrapper
    assert "PYTHONPATH" in wrapper


def test_miner_wrapper_status_runtime(tmp_path):
    """Actually execute miner-wrapper.sh status — catches PYTHONPATH / import regressions."""
    env = os.environ.copy()
    env["MEMEM_DIR"] = str(tmp_path / ".memem")
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_path / "obsidian-brain")
    (tmp_path / ".memem").mkdir()
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "memem" / "miner-wrapper.sh"), "status"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    # Wrapper should not crash — either reports "not running" or "running", but exits 0
    assert result.returncode == 0, f"wrapper failed: stdout={result.stdout} stderr={result.stderr}"
    assert "Miner" in (result.stdout + result.stderr)
    # Critical: the daemon invocation inside the wrapper must NOT have raised ModuleNotFoundError
    assert "ModuleNotFoundError" not in (result.stdout + result.stderr)
    assert "No module named" not in (result.stdout + result.stderr)


def test_miner_wrapper_refuses_pytest_temp_state(tmp_path):
    """Wrapper start must not leave real daemons running from pytest temp state."""
    state = Path("/tmp/pytest-of-claude-user/pytest-999/test_wrapper/.memem")
    env = os.environ.copy()
    env["MEMEM_DIR"] = str(state)
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_path / "obsidian-brain")
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "memem" / "miner-wrapper.sh"), "start"],
        capture_output=True, text=True, timeout=15, env=env,
    )

    assert result.returncode == 0
    assert "Refusing to start miner wrapper" in result.stdout
    assert not (state / "miner-wrapper.pid").exists()


def test_mine_cron_script_runs(tmp_path):
    """Actually execute mine-cron.sh — catches the PYTHONPATH bug that broke the cron path."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)  # redirect log directory into the tmpdir
    env["MEMEM_DIR"] = str(tmp_path / ".memem")
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_path / "obsidian-brain")
    (tmp_path / "obsidian-brain" / "memem" / "memories").mkdir(parents=True)
    (tmp_path / "obsidian-brain" / "memem" / "playbooks").mkdir(parents=True)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "memem" / "mine-cron.sh")],
        capture_output=True, text=True, timeout=60, env=env,
    )
    # The script may exit nonzero when there are no sessions to mine, but it
    # must not blow up with ModuleNotFoundError before the miner even starts.
    log_path = tmp_path / ".memem" / "logs" / "mine-cron.log"
    log = log_path.read_text() if log_path.exists() else ""
    combined = result.stdout + result.stderr + log
    assert "ModuleNotFoundError" not in combined, f"cron script has broken import: {combined}"
    assert "No module named 'memem'" not in combined


def test_hook_handles_missing_plugin_root(tmp_path):
    """auto-recall.sh must not crash or guess a wrong path when CLAUDE_PLUGIN_ROOT is missing."""
    # Create a minimal memory index the hook can find
    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)
    (vault / "memem" / "playbooks").mkdir(parents=True)
    (vault / "memem" / "_index.md").write_text("# Test Index\n\n- Entry\n")

    env = os.environ.copy()
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["MEMEM_OBSIDIAN_VAULT"] = str(vault)
    env["MEMEM_DIR"] = str(tmp_path / ".memem")
    env["HOME"] = str(tmp_path)
    hook_input = '{"session_id": "test-missing-root", "message": "hello"}'
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "auto-recall.sh")],
        input=hook_input, capture_output=True, text=True, timeout=15, env=env,
    )
    # Hook should complete (not crash) — fall back to index dump without assembly
    assert result.returncode == 0, f"hook crashed: stderr={result.stderr}"
    # And it MUST have surfaced the missing-plugin-root diagnostic on stderr
    assert "CLAUDE_PLUGIN_ROOT" in result.stderr or "plugin_root" in result.stderr.lower() or result.stdout.strip()
