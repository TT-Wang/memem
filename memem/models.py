"""Core data types, constants, and path definitions for memem."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict


def _env(*names: str, default: str = "") -> str:
    """Return the first env var that is set among `names`, else `default`.

    Used to provide backward-compatible fallbacks for the v0.6.x → v0.7.x
    rename: callers can read MEMEM_DIR or fall back to CORTEX_DIR if a
    legacy shell profile still exports the old name.
    """
    for name in names:
        v = os.environ.get(name)
        if v:
            return v
    return default


# ============================================================================
# Path constants
# ============================================================================

# State dir: prefer the new ~/.memem/, fall back to existing ~/.cortex/ if
# present (so dogfooding users with real data don't lose access on upgrade).
_state_env = _env("MEMEM_DIR", "CORTEX_DIR")
if _state_env:
    MEMEM_DIR = Path(_state_env)
else:
    _new_default = Path.home() / ".memem"
    _legacy_default = Path.home() / ".cortex"
    # If only the legacy dir exists and the new one doesn't, use legacy until
    # migration runs. The bootstrap shim's first-run migration will move data.
    MEMEM_DIR = _legacy_default if (_legacy_default.exists() and not _new_default.exists()) else _new_default

# Backward-compat alias kept for any external code reading the old name.
CORTEX_DIR = MEMEM_DIR

SERVER_PID_FILE = MEMEM_DIR / "mcp-server.pid"
TELEMETRY_FILE = MEMEM_DIR / "telemetry.json"
EVENT_LOG = MEMEM_DIR / "events.jsonl"
SEARCH_DB = MEMEM_DIR / "search.db"
LAST_BRIEF_PATH = MEMEM_DIR / ".last-brief.json"
TOPIC_SHIFTS_LOG = MEMEM_DIR / "topic-shifts.log"
RELEVANCE_SCORES_FILE = MEMEM_DIR / "relevance_scores.json"

# Layer constants for memory stratification (v0.10)
LAYER_L0 = 0  # Always-loaded: cross-project, importance==5
LAYER_L1 = 1  # Session-start index: high-importance
LAYER_L2 = 2  # On-demand: standard memories (default)
LAYER_L3 = 3  # Background: low-importance, rarely surfaced
DEFAULT_LAYER = 2

# Vault root: same fallback pattern.
OBSIDIAN_VAULT = Path(
    _env("MEMEM_OBSIDIAN_VAULT", "CORTEX_OBSIDIAN_VAULT", "MEMEM_VAULT", "CORTEX_VAULT",
         default=str(Path.home() / "obsidian-brain"))
)

# Per-project subdir within the vault. Same fallback for the dogfooding case:
# if `~/obsidian-brain/cortex/` exists and `~/obsidian-brain/memem/` doesn't,
# read from the legacy path until migration runs.
_new_subdir = OBSIDIAN_VAULT / "memem"
_legacy_subdir = OBSIDIAN_VAULT / "cortex"
_VAULT_SUBDIR = _legacy_subdir if (_legacy_subdir.exists() and not _new_subdir.exists()) else _new_subdir

OBSIDIAN_MEMORIES_DIR = _VAULT_SUBDIR / "memories"
INDEX_PATH = _VAULT_SUBDIR / "_index.md"
PLAYBOOK_DIR = _VAULT_SUBDIR / "playbooks"
PLAYBOOK_STAGING_DIR = PLAYBOOK_DIR / ".staging"


# ============================================================================
# Exceptions
# ============================================================================

class ObsidianUnavailableError(RuntimeError):
    """Raised when the Obsidian vault cannot accept writes."""


# ============================================================================
# Types
# ============================================================================

class Memory(TypedDict, total=False):
    """Schema for a memory dict."""
    id: str
    title: str
    essence: str
    domain_tags: list[str]
    project: str
    source_type: str  # "mined" | "user" | "import"
    source_session: str
    importance: int  # 1-5
    status: str  # "active" | "deprecated"
    valid_to: str
    related: list[str]
    contradicts: list[str]
    created_at: str
    updated_at: str
    file: str  # absolute path to markdown file
    obsidian_file: str  # filename only
    schema_version: int
    full_record: str  # read-time alias for essence


# ============================================================================
# Project name normalization
# ============================================================================

_PROJECT_ALIASES = {
    "vibireader": "vibereader",
    "vibe-reader": "vibereader",
    "Vibereader": "vibereader",
    "Vibireader": "vibereader",
    "hft-strategies": "HFT trading system",
    "HFT strategies": "HFT trading system",
    "hft": "HFT trading system",
    "Tech Feed TUI": "techfeed",
    "tailor-reader": "techfeed",
    "tailor-real/techfeed": "techfeed",
    "cortex": "cortex-plugin",
    "notes-api": "express-api",
}


def _normalize_scope_id(scope_id: str) -> str:
    """Normalize project/scope name: maps variants and defaults to 'general'."""
    if not scope_id or scope_id == "default":
        return "general"
    return _PROJECT_ALIASES.get(scope_id, scope_id)


# ============================================================================
# Helpers
# ============================================================================

def now_iso() -> str:
    """Return current time as ISO 8601 string with UTC timezone."""
    return datetime.now(UTC).isoformat()


# Backward-compat alias
_now = now_iso
