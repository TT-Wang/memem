"""Runtime capability detection + serialization.

memem writes a small JSON file at ``~/.memem/.capabilities`` during the
bootstrap shim (bootstrap.sh) and on every ``--doctor`` invocation. The rest
of the package reads it to decide whether optional features (Haiku-powered
assembly, smart recall) should run or fall back to a degraded mode.

Schema (v1)::

    {
      "schema_version": 1,
      "updated_at": "2026-04-14T12:34:56+00:00",
      "python_version": "3.11.6",
      "mcp": true,
      "claude_cli": true,
      "writable_state_dir": true,
      "writable_vault": true,
      "uv": true,
      "notes": []
    }

``claude_cli=false`` means the ``claude`` binary is not on PATH and any
Haiku-backed code path must degrade silently.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from memem.models import MEMEM_DIR, OBSIDIAN_MEMORIES_DIR, now_iso

log = logging.getLogger("memem-capabilities")

CAPABILITIES_FILE = MEMEM_DIR / ".capabilities"
SCHEMA_VERSION = 1


def _can_write(path: Path) -> bool:
    """Probe whether we can actually create and remove a file under ``path``."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        canary = path / ".memem-write-check"
        canary.write_text("ok")
        canary.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _mcp_importable() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("mcp") is not None
    except Exception:
        return False


def _claude_cli_available() -> bool:
    if shutil.which("claude") is None:
        return False
    # Extra sanity: make sure it actually runs (some installs are stale symlinks).
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _uv_available() -> bool:
    return shutil.which("uv") is not None


def detect_capabilities() -> dict[str, Any]:
    """Run every probe and return the capabilities dict. Does not write to disk."""
    caps: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "mcp": _mcp_importable(),
        "claude_cli": _claude_cli_available(),
        "writable_state_dir": _can_write(MEMEM_DIR),
        "writable_vault": _can_write(OBSIDIAN_MEMORIES_DIR),
        "uv": _uv_available(),
        "notes": [],
    }
    return caps


def write_capabilities(caps: dict[str, Any] | None = None) -> dict[str, Any]:
    """Detect (if not supplied) and atomically persist to ``~/.memem/.capabilities``."""
    if caps is None:
        caps = detect_capabilities()
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CAPABILITIES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(caps, indent=2, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CAPABILITIES_FILE)
    return caps


def read_capabilities() -> dict[str, Any]:
    """Return the cached capabilities, or an empty-but-valid dict if unavailable.

    Unknown-state is modeled as ``{"schema_version": 0}`` — callers should
    treat that as "assume degraded / run detect on next preflight".
    """
    if not CAPABILITIES_FILE.exists():
        return {"schema_version": 0}
    try:
        data = json.loads(CAPABILITIES_FILE.read_text())
        if not isinstance(data, dict):
            return {"schema_version": 0}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("capabilities file unreadable (%s); treating as degraded", exc)
        return {"schema_version": 0}


def assembly_available() -> bool:
    """True when Haiku-backed context assembly can run (requires claude CLI)."""
    caps = read_capabilities()
    if caps.get("schema_version", 0) < SCHEMA_VERSION:
        # Cache miss — probe live rather than degrading unconditionally
        caps = detect_capabilities()
    return bool(caps.get("claude_cli", False))


def format_status_banner(memory_count: int, miner_running: bool) -> str:
    """One-line status string suitable for the SessionStart banner."""
    caps = read_capabilities()
    miner_glyph = "✓" if miner_running else "✗"
    assembly_glyph = "✓" if caps.get("claude_cli") else "⚠"
    parts = [
        f"[memem] {memory_count} memories",
        f"miner {miner_glyph}",
        f"assembly {assembly_glyph}",
    ]
    if not caps.get("claude_cli", True):
        parts.append("(claude CLI missing — FTS-only)")
    if not caps.get("writable_vault", True):
        parts.append("(vault read-only!)")
    return " · ".join(parts)


def pretty_report(caps: dict[str, Any] | None = None) -> str:
    """Human-readable multi-line report. Used by ``--doctor``."""
    if caps is None:
        caps = detect_capabilities()
    lines = [
        "memem Doctor",
        "=" * 40,
        f"  Python version     : {caps.get('python_version', '?')}",
        f"  mcp importable     : {'yes' if caps.get('mcp') else 'NO — pip install mcp'}",
        f"  claude CLI on PATH : {'yes' if caps.get('claude_cli') else 'NO — Haiku assembly disabled (degraded)'}",
        f"  uv available       : {'yes' if caps.get('uv') else 'no (bootstrap.sh will install)'}",
        f"  state dir writable : {'yes' if caps.get('writable_state_dir') else 'NO — set MEMEM_DIR env var'}",
        f"  vault writable     : {'yes' if caps.get('writable_vault') else 'NO — set MEMEM_OBSIDIAN_VAULT env var'}",
        f"  updated_at         : {caps.get('updated_at', '?')}",
        "=" * 40,
    ]
    if caps.get("notes"):
        lines.append("Notes:")
        for note in caps["notes"]:
            lines.append(f"  - {note}")
        lines.append("=" * 40)

    blockers = []
    if not caps.get("mcp"):
        blockers.append("mcp package missing — MCP server cannot start")
    if not caps.get("writable_state_dir"):
        blockers.append("~/.memem is not writable")
    if not caps.get("writable_vault"):
        blockers.append("obsidian vault directory is not writable")

    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  ✗ {b}")
        lines.append("=" * 40)
        lines.append("RESULT: FAILING — fix blockers above before first use.")
    else:
        degraded = not caps.get("claude_cli", False)
        status = "DEGRADED (FTS-only recall, no Haiku assembly)" if degraded else "HEALTHY"
        lines.append(f"RESULT: {status}")
    return "\n".join(lines)
