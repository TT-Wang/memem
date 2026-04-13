"""Core data types, constants, and path definitions for Cortex."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict


# ============================================================================
# Path constants
# ============================================================================

CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
SERVER_PID_FILE = CORTEX_DIR / "mcp-server.pid"
TELEMETRY_FILE = CORTEX_DIR / "telemetry.json"
EVENT_LOG = CORTEX_DIR / "events.jsonl"
SEARCH_DB = CORTEX_DIR / "search.db"

OBSIDIAN_VAULT = Path(os.environ.get("CORTEX_OBSIDIAN_VAULT", str(Path.home() / "obsidian-brain")))
OBSIDIAN_MEMORIES_DIR = OBSIDIAN_VAULT / "cortex" / "memories"
INDEX_PATH = OBSIDIAN_VAULT / "cortex" / "_index.md"
PLAYBOOK_DIR = OBSIDIAN_VAULT / "cortex" / "playbooks"
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
    return datetime.now(timezone.utc).isoformat()


# Backward-compat alias
_now = now_iso
