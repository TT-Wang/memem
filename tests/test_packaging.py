"""Subprocess-level packaging smoke tests.

These tests shell out to a real child Python process to verify that the
package is invocable the same way the plugin + miner wrapper invoke it
in production. Catches regressions in plugin.json entry points,
PYTHONPATH wiring, and the `python -m cortex_server.server` runner that
in-process unit tests cannot see.
"""

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
        [sys.executable, "-m", "cortex_server.server", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT),
    )


def test_module_invocation_unknown_command_exits_nonzero():
    """`python -m cortex_server.server --bogus` must surface as nonzero exit."""
    result = _run_module(["--bogus-flag"])
    assert result.returncode != 0
    assert "Unknown command" in (result.stderr + result.stdout)


def test_module_invocation_status_runs(tmp_path):
    """`python -m cortex_server.server --status` must execute against an isolated vault."""
    env_extra = {
        "CORTEX_DIR": str(tmp_path / ".cortex"),
        "CORTEX_OBSIDIAN_VAULT": str(tmp_path / "obsidian-brain"),
    }
    (tmp_path / "obsidian-brain" / "cortex" / "memories").mkdir(parents=True)
    (tmp_path / "obsidian-brain" / "cortex" / "playbooks").mkdir(parents=True)
    result = _run_module(["--status"], env_extra=env_extra)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Cortex Status" in result.stdout
    assert "0 active" in result.stdout


def test_module_invocation_rebuild_index(tmp_path):
    """`--rebuild-index` must write an index file in the isolated vault."""
    env_extra = {
        "CORTEX_DIR": str(tmp_path / ".cortex"),
        "CORTEX_OBSIDIAN_VAULT": str(tmp_path / "obsidian-brain"),
    }
    (tmp_path / "obsidian-brain" / "cortex" / "memories").mkdir(parents=True)
    (tmp_path / "obsidian-brain" / "cortex" / "playbooks").mkdir(parents=True)
    result = _run_module(["--rebuild-index"], env_extra=env_extra)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert (tmp_path / "obsidian-brain" / "cortex" / "_index.md").exists()


@pytest.mark.skipif(
    "CI" in os.environ and not (REPO_ROOT / ".venv").exists(),
    reason="console-script test requires editable install; skipped when no venv",
)
def test_console_script_entry_exists_in_pyproject():
    """Regression guard: pyproject.toml must declare the cortex-server entry point."""
    content = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'cortex-server = "cortex_server.server:main"' in content


def test_plugin_json_uses_module_invocation():
    """Regression guard: plugin.json must invoke the package via `-m` not a raw path."""
    import json
    plugin_json = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
    mcp = plugin_json["mcpServers"]["cortex"]
    assert mcp["command"] == "python3"
    assert mcp["args"][:2] == ["-m", "cortex_server.server"]
    assert "PYTHONPATH" in mcp["env"]


def test_hook_references_new_package_path():
    """Regression guard: auto-recall.sh must not contain the old cortex-mcp-server path."""
    hook = (REPO_ROOT / "hooks" / "auto-recall.sh").read_text()
    assert "cortex-mcp-server" not in hook
    assert "cortex_server" in hook


def test_miner_wrapper_uses_module_form():
    """Regression guard: miner-wrapper.sh must invoke daemon via `python3 -m`."""
    wrapper = (REPO_ROOT / "cortex_server" / "miner-wrapper.sh").read_text()
    assert "python3 -m cortex_server.miner_daemon" in wrapper
    assert "PYTHONPATH" in wrapper
