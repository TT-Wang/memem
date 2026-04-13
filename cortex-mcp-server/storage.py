"""Cortex storage facade.

Re-exports all public symbols from the split modules for backward compatibility.
Lifecycle functions (PID management, vault setup, miner auto-start) live here
because they orchestrate across modules.
"""

import atexit
import logging
import os
import subprocess
from pathlib import Path

# ============================================================================
# Re-exports from split modules
# ============================================================================
from assembly import _consolidate_project, context_assemble
from models import (
    CORTEX_DIR,
    EVENT_LOG,
    INDEX_PATH,
    OBSIDIAN_MEMORIES_DIR,
    OBSIDIAN_VAULT,
    PLAYBOOK_DIR,
    PLAYBOOK_STAGING_DIR,
    SEARCH_DB,
    SERVER_PID_FILE,
    TELEMETRY_FILE,
    Memory,
    ObsidianUnavailableError,
    _normalize_scope_id,
    _now,
    now_iso,
)
from obsidian_store import (
    _append_or_update_index_line,
    _atomic_write,
    _check_contradictions,
    _containment,
    _delete_memory,
    _deprecate_memory,
    _extract_project,
    _find_best_match,
    _find_memory,
    _find_related,
    _format_index_line,
    _generate_index,
    _is_duplicate,
    _load_obsidian_memories,
    _make_memory,
    _memory_date_key,
    _ngram_set,
    _obsidian_memories,
    _parse_obsidian_memory_file,
    _recount_index_sections,
    _remove_index_line,
    _require_obsidian_writable,
    _save_memory,
    _slugify,
    _stable_mined_memory_id,
    _stem,
    _update_memory,
    _with_index_lock,
    _word_set,
    _write_obsidian_memory,
    _yaml_escape,
    purge_mined_memories,
)
from playbook import _playbook_append, _playbook_refine
from search_index import (
    _index_memory,
    _init_search_db,
    _rebuild_search_index,
    _remove_from_index,
    _search_fts,
)
from security import scan_memory_content
from telemetry import _get_telemetry, _log_event, _record_access

log = logging.getLogger("cortex-storage")


# ============================================================================
# Lifecycle functions (stay in storage.py — orchestrate across modules)
# ============================================================================

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
        pass


def _register_server_pid():
    _ensure_vault_exists()
    pid = os.getpid()
    SERVER_PID_FILE.write_text(str(pid))
    _auto_start_miner()

    def _cleanup():
        _cleanup_pid_file(SERVER_PID_FILE, pid)

    atexit.register(_cleanup)
