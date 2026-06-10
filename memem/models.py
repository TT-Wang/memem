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
def _safe_exists(p: Path) -> bool:
    """Path.exists() that swallows OSError (PermissionError, etc.) — module
    import must not crash on an unusual $HOME or unreadable mount. v1.8.3."""
    try:
        return p.exists()
    except OSError:
        return False


if _state_env:
    MEMEM_DIR = Path(_state_env)
else:
    _new_default = Path.home() / ".memem"
    _legacy_default = Path.home() / ".cortex"
    # If only the legacy dir exists and the new one doesn't, use legacy until
    # migration runs. The bootstrap shim's first-run migration will move data.
    MEMEM_DIR = _legacy_default if (_safe_exists(_legacy_default) and not _safe_exists(_new_default)) else _new_default

# Backward-compat alias kept for any external code reading the old name.
CORTEX_DIR = MEMEM_DIR

SERVER_PID_FILE = MEMEM_DIR / "mcp-server.pid"
MINER_OPT_IN_MARKER = MEMEM_DIR / ".miner-opted-in"
TELEMETRY_FILE = MEMEM_DIR / "telemetry.json"
EVENT_LOG = MEMEM_DIR / "events.jsonl"
SEARCH_DB = MEMEM_DIR / "search.db"
GRAPH_DB = MEMEM_DIR / "graph.db"
LAST_BRIEF_PATH = MEMEM_DIR / ".last-brief.json"
RELEVANCE_SCORES_FILE = MEMEM_DIR / "relevance_scores.json"
DELTA_AUDIT_LOG = MEMEM_DIR / "delta-audit.jsonl"
DELTA_STATE_DIR = MEMEM_DIR / "delta-state"

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
_VAULT_SUBDIR = _legacy_subdir if (_safe_exists(_legacy_subdir) and not _safe_exists(_new_subdir)) else _new_subdir

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
    # Self-aliases for the memem/cortex-plugin rename
    "cortex": "cortex-plugin",
    "memem": "cortex-plugin",  # memem package == cortex-plugin project (renamed)
}

# Override loader: merge user-defined aliases from MEMEM_DIR/project_aliases.json.
# File format: {"alias": "canonical", ...} — a flat JSON object.
# Silently no-ops on missing or malformed file; safe to call at import time.
try:
    _aliases_override_path = MEMEM_DIR / "project_aliases.json"
    if _aliases_override_path.exists():
        import json as _json
        with open(_aliases_override_path) as _fh:
            _user_aliases = _json.load(_fh)
        if isinstance(_user_aliases, dict):
            _PROJECT_ALIASES.update({str(k): str(v) for k, v in _user_aliases.items()})
        del _json, _fh, _user_aliases
    del _aliases_override_path
except Exception:  # noqa: BLE001 — import-time; never raise
    pass


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


def parse_iso_dt(s: str) -> datetime | None:
    """Parse an ISO 8601 datetime string to a timezone-aware datetime.

    Handles:
      - Trailing 'Z' suffix (treated as UTC)
      - Explicit '+HH:MM' offsets
      - Naive strings (treated as UTC)

    Returns None on empty input or parse failure.
    """
    if not s:
        return None
    try:
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


# Backward-compat alias
_now = now_iso
