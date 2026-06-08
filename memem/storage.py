"""Cortex server-lifecycle helpers.

Historically this module was a large facade that re-exported most of the
package. After the v0.5.x facade-tightening pass, callers import directly
from the concrete modules (models, security, telemetry, search_index,
obsidian_store, playbook, assembly). This file now only holds PID management
and the `MINER_OPT_IN_MARKER` constant used by the Stop hook and the CLI
opt-in/out paths (v2.1.0: the auto-start-miner hook was removed when the
daemon was deleted).
"""

import atexit
import logging
import os
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


def _register_server_pid():
    _ensure_vault_exists()
    pid = os.getpid()
    SERVER_PID_FILE.write_text(str(pid))

    def _cleanup():
        _cleanup_pid_file(SERVER_PID_FILE, pid)

    atexit.register(_cleanup)
