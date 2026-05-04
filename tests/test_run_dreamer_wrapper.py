"""Regression test for scripts/run_dreamer.sh.

The wrapper previously called ``python3 -m memem.cli`` which has no
``__main__`` block — it loaded the module and exited 0 with no output.
That made cron silent: success status, no diagnostics, no dream log.

This test invokes the wrapper end-to-end and asserts it actually executes
the dream cycle (banner + dry-run notice in stdout).
"""

import os
import subprocess

WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER_PATH = os.path.join(WORKTREE_ROOT, "scripts", "run_dreamer.sh")


def test_wrapper_actually_runs_dream_cycle(tmp_cortex_dir, tmp_vault):
    """Wrapper must invoke the real CLI dispatch, not silently no-op."""
    env = os.environ.copy()
    env["MEMEM_DIR"] = str(tmp_cortex_dir)
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_vault)
    env.pop("CORTEX_DIR", None)

    result = subprocess.run(
        ["bash", WRAPPER_PATH],
        capture_output=True, text=True, timeout=60, env=env,
    )

    assert result.returncode == 0, f"wrapper failed: {result.stderr}"
    assert "[memem dreamer]" in result.stdout, (
        f"wrapper produced no dreamer output (silent-cron bug): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "DRY-RUN" in result.stdout, "default invocation must be dry-run"
