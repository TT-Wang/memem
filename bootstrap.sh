#!/usr/bin/env bash
# memem bootstrap shim — runs every time Claude Code spawns the MCP server.
#
# Responsibilities (in order):
#   1. Verify Python 3.11+
#   2. Install `uv` if missing (via the official installer)
#   3. Sync the plugin-local .venv against uv.lock, short-circuited by a
#      sha256 hash of uv.lock so warm starts are fast
#   4. One-time migration: if ~/.cortex/ exists and ~/.memem/ doesn't,
#      copy the data over (idempotent via .migrated_from_cortex marker)
#   5. Create ~/.memem and the obsidian vault dir, canary-test writability
#   6. Detect the `claude` CLI and write ~/.memem/.capabilities
#   7. Exec the real MCP server: `python -m memem.server "$@"`
#
# On any unrecoverable error the shim prints a clear one-line diagnostic to
# stderr and exits with a distinct code so the host (Claude Code) can surface
# it. Degraded mode (missing `claude` CLI) is NOT an error — it's recorded
# in .capabilities and the server degrades gracefully.
#
# Backward compatibility:
#   - `MEMEM_*` env vars take precedence; `CORTEX_*` are read as fallbacks
#     so users with legacy shell-profile exports keep working.
#   - State data under ~/.cortex/ is migrated to ~/.memem/ on first run
#     (copy, not move — the legacy dir stays as a safety net).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT_CANDIDATE="${CLAUDE_PLUGIN_ROOT:-}"
if [ -n "$PLUGIN_ROOT_CANDIDATE" ] \
    && [ "$PLUGIN_ROOT_CANDIDATE" != '${CLAUDE_PLUGIN_ROOT}' ] \
    && [ -d "$PLUGIN_ROOT_CANDIDATE" ]; then
    PLUGIN_ROOT="$PLUGIN_ROOT_CANDIDATE"
else
    PLUGIN_ROOT="$SCRIPT_DIR"
fi
VENV_DIR="$PLUGIN_ROOT/.venv"
LOCK_HASH_FILE="$VENV_DIR/.lock-hash"
UV_LOCK="$PLUGIN_ROOT/uv.lock"
PYPROJECT="$PLUGIN_ROOT/pyproject.toml"

# State dir: prefer MEMEM_DIR, then legacy CORTEX_DIR, then default to ~/.memem
MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
LEGACY_CORTEX_DIR="$HOME/.cortex"
BOOTSTRAP_LOG="$MEMEM_DIR/bootstrap.log"

# Vault root: same fallback pattern
MEMEM_VAULT="${MEMEM_OBSIDIAN_VAULT:-${CORTEX_OBSIDIAN_VAULT:-$HOME/obsidian-brain}}"

# Exit codes
EXIT_PYTHON=10
EXIT_UV=11
EXIT_SYNC=12
EXIT_WRITE=13

log() {
    mkdir -p "$MEMEM_DIR" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$BOOTSTRAP_LOG" 2>/dev/null || true
}

die() {
    local code="$1"; shift
    echo "memem-bootstrap: $*" >&2
    log "FATAL ($code): $*"
    # Surface the failure to the user via /memem (reads last-error.md).
    mkdir -p "$MEMEM_DIR" 2>/dev/null || true
    {
        echo "# memem bootstrap failed"
        echo ""
        echo "**Error:** $*"
        echo ""
        echo "**Exit code:** $code"
        echo ""
        echo "**Log:** \`$BOOTSTRAP_LOG\`"
        echo ""
        echo "**Next step:** run \`/memem-doctor\` for guided diagnostics."
    } > "$MEMEM_DIR/last-error.md" 2>/dev/null || true
    exit "$code"
}

# ---- usage / self-test mode --------------------------------------------------
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<EOF
memem-bootstrap - self-healing MCP server launcher

Usage: bootstrap.sh [SERVER_ARGS...]

What it does:
  1. Ensure Python >= 3.11 is available
  2. Install uv (https://docs.astral.sh/uv/) if missing
  3. Sync deps into \$PLUGIN_ROOT/.venv (hash-cached against uv.lock)
  4. One-time migrate ~/.cortex/ → ~/.memem/ + vault subdir (copy, idempotent)
  5. Ensure ~/.memem and the Obsidian vault dir exist and are writable
  6. Write ~/.memem/.capabilities (used for degraded-mode decisions)
  7. Exec: python -m memem.server \$@

Environment variables:
  CLAUDE_PLUGIN_ROOT     Root of the plugin checkout (default: script dir)
  MEMEM_DIR              State dir (default: ~/.memem; falls back to legacy CORTEX_DIR)
  MEMEM_OBSIDIAN_VAULT   Obsidian vault (default: ~/obsidian-brain; falls back to CORTEX_OBSIDIAN_VAULT)
  MEMEM_SKIP_SYNC=1      Skip uv sync (useful during development; falls back to CORTEX_SKIP_SYNC)

Exit codes:
  0    success (or degraded mode)
  10   Python >= 3.11 not found
  11   uv install failed
  12   uv sync failed
  13   ~/.memem or vault dir is not writable
EOF
    exit 0
fi

log "bootstrap start — PLUGIN_ROOT=$PLUGIN_ROOT  args=$*"

# ---- 1. Python version check (self-heals via uv if too old) ----------------
NEEDS_UV_PYTHON=0
if ! command -v python3 >/dev/null 2>&1; then
    log "python3 not found on PATH — will install Python 3.11 via uv"
    NEEDS_UV_PYTHON=1
else
    PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    PYMAJ="${PYVER%%.*}"
    PYMIN="${PYVER##*.}"
    if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
        log "python $PYVER too old — will install Python 3.11 via uv"
        NEEDS_UV_PYTHON=1
    else
        log "python ok: $PYVER"
    fi
fi

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

# ---- 2.5. Install Python 3.11 via uv if system python is missing/too old ---
if [ "$NEEDS_UV_PYTHON" = "1" ]; then
    log "installing Python 3.11 via uv (first-run only, ~30MB download)"
    if ! uv python install 3.11 >> "$BOOTSTRAP_LOG" 2>&1; then
        die "$EXIT_PYTHON" "uv python install 3.11 failed. See $BOOTSTRAP_LOG."
    fi
    log "uv-managed python 3.11 installed"
fi

# ---- 3. Venv sync (hash-cached) ---------------------------------------------
SKIP_SYNC="${MEMEM_SKIP_SYNC:-${CORTEX_SKIP_SYNC:-0}}"
if [ "$SKIP_SYNC" = "1" ]; then
    log "MEMEM_SKIP_SYNC=1 — skipping uv sync"
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

# ---- 4. One-time data migration: ~/.cortex/ → ~/.memem/ ----------------------
# Idempotent via marker file. Copy (not move) so the legacy dir survives as a
# safety net. Same logic for the obsidian vault subdir.
MIGRATION_MARKER="$MEMEM_DIR/.migrated_from_cortex"
if [ ! -f "$MIGRATION_MARKER" ]; then
    if [ -d "$LEGACY_CORTEX_DIR" ] && [ "$LEGACY_CORTEX_DIR" != "$MEMEM_DIR" ]; then
        log "migration: detected legacy data at $LEGACY_CORTEX_DIR, copying to $MEMEM_DIR"
        mkdir -p "$MEMEM_DIR"
        # Use cp -an: archive mode, no-clobber, so re-runs are safe.
        cp -an "$LEGACY_CORTEX_DIR/." "$MEMEM_DIR/" 2>>"$BOOTSTRAP_LOG" || \
            log "migration warning: cp from $LEGACY_CORTEX_DIR failed (non-fatal)"
        log "migration: state dir copy complete"
    fi
    LEGACY_VAULT_SUB="$MEMEM_VAULT/cortex"
    NEW_VAULT_SUB="$MEMEM_VAULT/memem"
    if [ -d "$LEGACY_VAULT_SUB" ] && [ ! -d "$NEW_VAULT_SUB" ]; then
        log "migration: copying $LEGACY_VAULT_SUB → $NEW_VAULT_SUB"
        mkdir -p "$NEW_VAULT_SUB"
        cp -an "$LEGACY_VAULT_SUB/." "$NEW_VAULT_SUB/" 2>>"$BOOTSTRAP_LOG" || \
            log "migration warning: vault copy failed (non-fatal — legacy path remains as fallback)"
        log "migration: vault subdir copy complete"
    fi
    # Mark migration done so we never run it again (even if it was a no-op).
    mkdir -p "$MEMEM_DIR"
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$MIGRATION_MARKER" 2>/dev/null || true
fi

# ---- 5. Directory + writability check ---------------------------------------
mkdir -p "$MEMEM_DIR" "$MEMEM_VAULT/memem/memories" "$MEMEM_VAULT/memem/playbooks" 2>/dev/null || true

canary() {
    local dir="$1"
    local f="$dir/.memem-write-check"
    if ! ( echo ok > "$f" ) 2>/dev/null; then return 1; fi
    rm -f "$f" 2>/dev/null || true
    return 0
}

if ! canary "$MEMEM_DIR"; then
    die "$EXIT_WRITE" "$MEMEM_DIR is not writable. Set MEMEM_DIR to a writable path."
fi
if ! canary "$MEMEM_VAULT/memem/memories"; then
    die "$EXIT_WRITE" "$MEMEM_VAULT/memem/memories is not writable. Set MEMEM_OBSIDIAN_VAULT."
fi
log "writability ok: state=$MEMEM_DIR vault=$MEMEM_VAULT"

# ---- 6. Pick the Python interpreter + write capabilities --------------------
if [ -x "$VENV_DIR/bin/python" ]; then
    PYBIN="$VENV_DIR/bin/python"
else
    PYBIN="python3"
fi

export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export MEMEM_DIR MEMEM_VAULT
# Re-export legacy names too so any subprocess that still reads them works.
export CORTEX_DIR="${CORTEX_DIR:-$MEMEM_DIR}"
export CORTEX_OBSIDIAN_VAULT="${CORTEX_OBSIDIAN_VAULT:-$MEMEM_VAULT}"

# Write capabilities file — degraded mode if this fails, not fatal
if ! "$PYBIN" -c "from memem.capabilities import write_capabilities; write_capabilities()" >> "$BOOTSTRAP_LOG" 2>&1; then
    log "warning: capabilities probe failed — server will run without status banner"
fi

# Clear any stale error-surface file — we made it this far, so bootstrap succeeded.
rm -f "$MEMEM_DIR/last-error.md" 2>/dev/null || true

# ---- 7. Exec the server ------------------------------------------------------
log "exec: $PYBIN -m memem.server $*"
exec "$PYBIN" -m memem.server "$@"
