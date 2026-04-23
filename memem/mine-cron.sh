#!/usr/bin/env bash
set -euo pipefail
# Prefer MEMEM_DIR, fall back to legacy CORTEX_DIR for users mid-migration.
MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
mkdir -p "$MEMEM_DIR/logs"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
LOG_FILE="$MEMEM_DIR/logs/mine-cron.log"
echo "[$TIMESTAMP] Starting mine-all" >> "$LOG_FILE"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYBIN="${MEMEM_PYTHON:-python3}"
"$PYBIN" -m memem.server --mine-all >> "$LOG_FILE" 2>&1
echo "[$TIMESTAMP] Done" >> "$LOG_FILE"
exit 0
