"""Bootstrap shim smoke tests.

These tests shell out to ``bootstrap.sh`` the same way Claude Code does,
in an isolated tmp environment, and verify the self-healing behaviour
end-to-end. They are intentionally subprocess-level because the failure
modes we care about (missing uv, unwritable dirs, stale lock hash) only
reproduce in a real child shell.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap.sh"


def _run_bootstrap(args, env_extra, timeout=60):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    # Default: skip uv sync so tests don't rewrite the real .venv on every run.
    env.setdefault("MEMEM_SKIP_SYNC", "1")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(BOOTSTRAP), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def test_bootstrap_help_exits_zero():
    """bootstrap.sh --help must produce usage and exit 0."""
    result = _run_bootstrap(["--help"], env_extra={})
    assert result.returncode == 0
    assert "memem-bootstrap" in result.stdout
    assert "Exit codes" in result.stdout


def test_bootstrap_writes_capabilities(tmp_path):
    """A full shim run with --doctor should populate <state>/.capabilities."""
    state_dir = tmp_path / ".memem"
    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)
    (vault / "memem" / "playbooks").mkdir(parents=True)

    result = _run_bootstrap(
        ["--doctor"],
        env_extra={
            "MEMEM_DIR": str(state_dir),
            "MEMEM_OBSIDIAN_VAULT": str(vault),
        },
    )
    # --doctor may exit 1 if mcp is unimportable in the test env; either 0 or 1 is acceptable
    assert result.returncode in (0, 1), f"unexpected rc={result.returncode}: {result.stderr}"
    caps_file = state_dir / ".capabilities"
    assert caps_file.exists(), f"capabilities file not written; stderr={result.stderr[:300]}"
    data = json.loads(caps_file.read_text())
    assert data["schema_version"] == 1
    assert "python_version" in data
    assert "claude_cli" in data
    assert "writable_state_dir" in data
    assert data["writable_state_dir"] is True  # tmp_path is always writable


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="POSIX file permissions don't restrict root — the canary write "
    "succeeds even on a 0555 dir, so this test cannot meaningfully run as root. "
    "It still runs in normal user CI (GitHub Actions) where this assertion holds.",
)
def test_bootstrap_refuses_unwritable_state_dir(tmp_path):
    """Bootstrap must hard-fail (exit 13) when the state dir is not writable."""
    # Create a read-only parent so mkdir will succeed but canary will fail
    readonly_parent = tmp_path / "readonly"
    readonly_parent.mkdir()
    state_dir = readonly_parent / ".memem"
    state_dir.mkdir()
    os.chmod(state_dir, 0o555)  # r-x for owner

    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)

    try:
        result = _run_bootstrap(
            ["--doctor"],
            env_extra={
                "MEMEM_DIR": str(state_dir),
                "MEMEM_OBSIDIAN_VAULT": str(vault),
            },
        )
        # Must exit 13 (EXIT_WRITE) with a clear message on stderr
        assert result.returncode == 13, f"expected 13, got {result.returncode}. stderr={result.stderr}"
        assert "not writable" in result.stderr
    finally:
        os.chmod(state_dir, 0o755)  # restore for cleanup


def test_bootstrap_skip_sync_env_var(tmp_path):
    """MEMEM_SKIP_SYNC=1 must bypass the uv sync step entirely."""
    state_dir = tmp_path / ".memem"
    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)

    _run_bootstrap(
        ["--doctor"],
        env_extra={
            "MEMEM_DIR": str(state_dir),
            "MEMEM_OBSIDIAN_VAULT": str(vault),
            "MEMEM_SKIP_SYNC": "1",
        },
    )
    # Check the bootstrap log for the skip message
    log_file = state_dir / "bootstrap.log"
    assert log_file.exists()
    log_content = log_file.read_text()
    assert "MEMEM_SKIP_SYNC" in log_content or "skipping uv sync" in log_content


def test_bootstrap_legacy_cortex_env_fallback(tmp_path):
    """The shim must accept legacy CORTEX_DIR / CORTEX_OBSIDIAN_VAULT for backward compat."""
    state_dir = tmp_path / ".cortex"  # legacy name
    vault = tmp_path / "obsidian-brain"
    (vault / "cortex" / "memories").mkdir(parents=True)
    (vault / "cortex" / "playbooks").mkdir(parents=True)

    result = _run_bootstrap(
        ["--doctor"],
        env_extra={
            "CORTEX_DIR": str(state_dir),
            "CORTEX_OBSIDIAN_VAULT": str(vault),
            "CORTEX_SKIP_SYNC": "1",
        },
    )
    # Must work — the bootstrap shim falls back to CORTEX_* when MEMEM_* is unset
    assert result.returncode in (0, 1), f"legacy env fallback broken: {result.stderr}"
    caps_file = state_dir / ".capabilities"
    assert caps_file.exists(), f"capabilities not written when using CORTEX_DIR fallback: {result.stderr[:300]}"


def test_capabilities_module_roundtrip(tmp_path, monkeypatch):
    """In-process roundtrip: write + read + pretty_report."""
    monkeypatch.setenv("MEMEM_DIR", str(tmp_path / ".memem"))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(tmp_path / "vault"))
    (tmp_path / "vault" / "memem" / "memories").mkdir(parents=True)

    import importlib

    from memem import capabilities, models
    importlib.reload(models)
    importlib.reload(capabilities)

    caps = capabilities.detect_capabilities()
    assert caps["schema_version"] == 1
    assert caps["writable_state_dir"] is True
    assert caps["writable_vault"] is True

    capabilities.write_capabilities(caps)
    read_back = capabilities.read_capabilities()
    assert read_back["schema_version"] == 1
    assert read_back["writable_state_dir"] is True

    report = capabilities.pretty_report(caps)
    assert "memem Doctor" in report
    assert "RESULT:" in report


def test_format_status_banner(tmp_path, monkeypatch):
    """Banner must include memory count, miner glyph, assembly glyph."""
    monkeypatch.setenv("MEMEM_DIR", str(tmp_path / ".memem"))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(tmp_path / "vault"))
    (tmp_path / "vault" / "memem" / "memories").mkdir(parents=True)

    import importlib

    from memem import capabilities, models
    importlib.reload(models)
    importlib.reload(capabilities)

    capabilities.write_capabilities()
    banner = capabilities.format_status_banner(memory_count=42, miner_running=True)
    assert "[memem]" in banner
    assert "42 memories" in banner
    assert "miner ✓" in banner
    assert "assembly" in banner
