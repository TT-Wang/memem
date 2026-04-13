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
from pathlib import Path

from cortex_server.models import CORTEX_DIR, INDEX_PATH, OBSIDIAN_MEMORIES_DIR, PLAYBOOK_DIR, SERVER_PID_FILE

log = logging.getLogger("cortex-storage")


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
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)


def _auto_start_miner():
    """Start the miner daemon if not already running."""
    try:
        miner_pid_file = CORTEX_DIR / "miner.pid"
        if miner_pid_file.exists():
            try:
                pid = int(miner_pid_file.read_text().strip())
                if _pid_is_running(pid):
                    return
            except (ValueError, OSError):
                pass

        wrapper = Path(__file__).resolve().parent / "miner-wrapper.sh"
        if not wrapper.exists():
            return
        subprocess.Popen(
            ["setsid", "bash", str(wrapper), "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("Auto-started miner daemon")
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
