#!/usr/bin/env bash
# Cortex bootstrap shim — runs every time Claude Code spawns the MCP server.
#
# Responsibilities (in order):
#   1. Verify Python 3.11+
#   2. Install `uv` if missing (via the official installer)
#   3. Sync the plugin-local .venv against uv.lock, short-circuited by a
#      sha256 hash of uv.lock so warm starts are fast
#   4. Create ~/.cortex and the obsidian vault dir, canary-test writability
#   5. Detect the `claude` CLI and write ~/.cortex/.capabilities
#   6. Exec the real MCP server: `python -m cortex_server.server "$@"`
#
# On any unrecoverable error the shim prints a clear one-line diagnostic to
# stderr and exits with a distinct code so the host (Claude Code) can surface
# it. Degraded mode (missing `claude` CLI) is NOT an error — it's recorded
# in .capabilities and the server degrades gracefully.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$SCRIPT_DIR}"
VENV_DIR="$PLUGIN_ROOT/.venv"
LOCK_HASH_FILE="$VENV_DIR/.lock-hash"
UV_LOCK="$PLUGIN_ROOT/uv.lock"
PYPROJECT="$PLUGIN_ROOT/pyproject.toml"
CORTEX_DIR="${CORTEX_DIR:-$HOME/.cortex}"
BOOTSTRAP_LOG="$CORTEX_DIR/bootstrap.log"

# Exit codes
EXIT_PYTHON=10
EXIT_UV=11
EXIT_SYNC=12
EXIT_WRITE=13

log() {
    mkdir -p "$CORTEX_DIR" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$BOOTSTRAP_LOG" 2>/dev/null || true
}

die() {
    local code="$1"; shift
    echo "cortex-bootstrap: $*" >&2
    log "FATAL ($code): $*"
    exit "$code"
}

# ---- usage / self-test mode --------------------------------------------------
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<EOF
cortex-bootstrap - self-healing MCP server launcher

Usage: bootstrap.sh [SERVER_ARGS...]

What it does:
  1. Ensure Python >= 3.11 is available
  2. Install uv (https://docs.astral.sh/uv/) if missing
  3. Sync deps into \$PLUGIN_ROOT/.venv (hash-cached against uv.lock)
  4. Ensure ~/.cortex and the Obsidian vault dir exist and are writable
  5. Write ~/.cortex/.capabilities (used for degraded-mode decisions)
  6. Exec: python -m cortex_server.server \$@

Environment variables:
  CLAUDE_PLUGIN_ROOT     Root of the plugin checkout (default: script dir)
  CORTEX_DIR             State dir (default: ~/.cortex)
  CORTEX_OBSIDIAN_VAULT  Obsidian vault (default: ~/obsidian-brain)
  CORTEX_SKIP_SYNC=1     Skip uv sync (useful during development)

Exit codes:
  0    success (or degraded mode)
  10   Python >= 3.11 not found
  11   uv install failed
  12   uv sync failed
  13   ~/.cortex or vault dir is not writable
EOF
    exit 0
fi

log "bootstrap start — PLUGIN_ROOT=$PLUGIN_ROOT  args=$*"

# ---- 1. Python version check -------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    die "$EXIT_PYTHON" "python3 not found on PATH. Install Python >= 3.11 and retry."
fi
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYMAJ="${PYVER%%.*}"
PYMIN="${PYVER##*.}"
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
    die "$EXIT_PYTHON" "Python $PYVER detected; Cortex requires >= 3.11."
fi
log "python ok: $PYVER"

# ---- 2. uv install -----------------------------------------------------------
ensure_uv_on_path() {
    # uv's installer places the binary in ~/.local/bin by default.
    for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -x "$candidate/uv" ] && ! command -v uv >/dev/null 2>&1; then
            export PATH="$candidate:$PATH"
        fi
    done
}

ensure_uv_on_path
if ! command -v uv >/dev/null 2>&1; then
    log "uv not found — installing via official installer"
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh >> "$BOOTSTRAP_LOG" 2>&1; then
        die "$EXIT_UV" "uv install failed. See $BOOTSTRAP_LOG. Manual: https://docs.astral.sh/uv/"
    fi
    ensure_uv_on_path
    if ! command -v uv >/dev/null 2>&1; then
        die "$EXIT_UV" "uv installed but not found on PATH. Check \$HOME/.local/bin."
    fi
fi
log "uv ok: $(uv --version 2>/dev/null || echo unknown)"

# ---- 3. Venv sync (hash-cached) ---------------------------------------------
if [ "${CORTEX_SKIP_SYNC:-0}" = "1" ]; then
    log "CORTEX_SKIP_SYNC=1 — skipping uv sync"
else
    if [ -f "$UV_LOCK" ]; then
        LOCK_HASH="$(sha256sum "$UV_LOCK" 2>/dev/null | awk '{print $1}')"
    elif [ -f "$PYPROJECT" ]; then
        LOCK_HASH="$(sha256sum "$PYPROJECT" 2>/dev/null | awk '{print $1}')"
    else
        LOCK_HASH="nolock"
    fi
    CACHED_HASH=""
    [ -f "$LOCK_HASH_FILE" ] && CACHED_HASH="$(cat "$LOCK_HASH_FILE" 2>/dev/null || true)"
    if [ -d "$VENV_DIR" ] && [ "$LOCK_HASH" = "$CACHED_HASH" ]; then
        log "venv cache hit ($LOCK_HASH) — skipping uv sync"
    else
        log "venv cache miss — syncing deps (this may take a few seconds on first run)"
        (
            cd "$PLUGIN_ROOT"
            if [ -f "$UV_LOCK" ]; then
                uv sync --frozen >> "$BOOTSTRAP_LOG" 2>&1
            else
                # No lockfile yet (first-ever bootstrap): generate one
                uv sync >> "$BOOTSTRAP_LOG" 2>&1
            fi
        ) || die "$EXIT_SYNC" "uv sync failed. See $BOOTSTRAP_LOG for details."
        echo "$LOCK_HASH" > "$LOCK_HASH_FILE"
        log "venv sync ok"
    fi
fi

# ---- 4. Directory + writability check ---------------------------------------
CORTEX_VAULT="${CORTEX_OBSIDIAN_VAULT:-$HOME/obsidian-brain}"
mkdir -p "$CORTEX_DIR" "$CORTEX_VAULT/cortex/memories" "$CORTEX_VAULT/cortex/playbooks" 2>/dev/null || true

canary() {
    local dir="$1"
    local f="$dir/.cortex-write-check"
    if ! ( echo ok > "$f" ) 2>/dev/null; then return 1; fi
    rm -f "$f" 2>/dev/null || true
    return 0
}

if ! canary "$CORTEX_DIR"; then
    die "$EXIT_WRITE" "$CORTEX_DIR is not writable. Set CORTEX_DIR to a writable path."
fi
if ! canary "$CORTEX_VAULT/cortex/memories"; then
    die "$EXIT_WRITE" "$CORTEX_VAULT/cortex/memories is not writable. Set CORTEX_OBSIDIAN_VAULT."
fi
log "writability ok: cortex=$CORTEX_DIR vault=$CORTEX_VAULT"

# ---- 5. Pick the Python interpreter + write capabilities --------------------
if [ -x "$VENV_DIR/bin/python" ]; then
    PYBIN="$VENV_DIR/bin/python"
else
    PYBIN="python3"
fi

export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Write capabilities file — degraded mode if this fails, not fatal
if ! "$PYBIN" -c "from cortex_server.capabilities import write_capabilities; write_capabilities()" >> "$BOOTSTRAP_LOG" 2>&1; then
    log "warning: capabilities probe failed — server will run without status banner"
fi

# ---- 6. Exec the server ------------------------------------------------------
log "exec: $PYBIN -m cortex_server.server $*"
exec "$PYBIN" -m cortex_server.server "$@"
