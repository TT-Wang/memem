#!/usr/bin/env bash
mkdir -p ~/.cortex/logs
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
LOG_FILE=~/.cortex/logs/mine-cron.log
echo "[$TIMESTAMP] Starting mine-all" >> "$LOG_FILE"
python3 /home/claude-user/cortex-plugin/cortex-mcp-server/server.py --mine-all >> "$LOG_FILE" 2>&1 || true
echo "[$TIMESTAMP] Running gc" >> "$LOG_FILE"
python3 /home/claude-user/cortex-plugin/cortex-mcp-server/server.py --gc >> "$LOG_FILE" 2>&1
echo "[$TIMESTAMP] Done" >> "$LOG_FILE"
exit 0
