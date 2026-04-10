#!/usr/bin/env bash
set -euo pipefail
mkdir -p ~/.cortex/logs
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
LOG_FILE=~/.cortex/logs/mine-cron.log
echo "[$TIMESTAMP] Starting mine-all" >> "$LOG_FILE"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/server.py" --mine-all >> "$LOG_FILE" 2>&1
echo "[$TIMESTAMP] Done" >> "$LOG_FILE"
exit 0
