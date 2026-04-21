"""Cortex server-lifecycle helpers.

Historically this module was a large facade that re-exported most of the
package. After the v0.5.x facade-tightening pass, callers import directly
from the concrete modules (models, security, telemetry, search_index,
obsidian_store, playbook, assembly). This file now only holds PID management
and the auto-start-miner hook that server.py runs on boot.
"""

import atexit
import logging
import os
import subprocess
import time
from pathlib import Path

from memem.models import INDEX_PATH, MEMEM_DIR, OBSIDIAN_MEMORIES_DIR, PLAYBOOK_DIR, SERVER_PID_FILE

log = logging.getLogger("memem-storage")


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_pid_file(path: Path, pid: int):
    try:
        if path.read_text().strip() == str(pid):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _ensure_vault_exists():
    """Create Obsidian vault directories if they don't exist."""
    OBSIDIAN_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)


MINER_OPT_IN_MARKER = MEMEM_DIR / ".miner-opted-in"


def _is_ephemeral_test_state_dir(path: Path = MEMEM_DIR) -> bool:
    """True for pytest temp state dirs that must not launch persistent miners."""
    if os.environ.get("MEMEM_ALLOW_TEST_MINER"):
        return False
    path_str = str(path)
    return (
        "/pytest-" in path_str
        or "/pytest-of-" in path_str
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _auto_start_miner():
    """Start the miner daemon if the user has explicitly opted in.

    v0.9.0 onwards the miner is strictly opt-in: install no longer starts
    any background processes. The marker ``~/.memem/.miner-opted-in`` is
    created by ``/memem-mine``, ``/memem-mine-history``, or the
    ``--miner-opt-in`` / ``--mine-all`` CLI flags. Without the marker this
    function is a silent no-op — the MCP server still runs, memories are
    still readable, but nothing mines in the background.
    """
    if not MINER_OPT_IN_MARKER.exists():
        return
    if _is_ephemeral_test_state_dir():
        log.warning("auto-start-miner skipped for ephemeral test state: %s", MEMEM_DIR)
        return
    try:
        miner_pid_file = MEMEM_DIR / "miner.pid"
        if miner_pid_file.exists():
            try:
                pid = int(miner_pid_file.read_text().strip())
                if _pid_is_running(pid):
                    return
            except (ValueError, OSError):
                pass

        wrapper = Path(__file__).resolve().parent / "miner-wrapper.sh"
        if not wrapper.exists():
            log.warning("auto-start-miner: wrapper not found at %s", wrapper)
            return

        # Prefer setsid but fall back to plain bash if setsid is missing
        # (e.g. minimal macOS installs without coreutils).
        import shutil as _shutil
        cmd: list[str] = (
            ["setsid", "bash", str(wrapper), "start"]
            if _shutil.which("setsid")
            else ["bash", str(wrapper), "start"]
        )

        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Poll for the daemon's PID file instead of trusting Popen.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if miner_pid_file.exists():
                try:
                    pid = int(miner_pid_file.read_text().strip())
                    if _pid_is_running(pid):
                        log.info("Auto-started miner daemon (PID %d)", pid)
                        return
                except (ValueError, OSError):
                    continue
        log.warning(
            "auto-start-miner: daemon did not come up within 2s — check ~/.cortex/miner.log"
        )
    except Exception:
        log.warning("auto-start-miner failed", exc_info=True)


def _register_server_pid():
    _ensure_vault_exists()
    pid = os.getpid()
    SERVER_PID_FILE.write_text(str(pid))
    _auto_start_miner()

    def _cleanup():
        _cleanup_pid_file(SERVER_PID_FILE, pid)

    atexit.register(_cleanup)
