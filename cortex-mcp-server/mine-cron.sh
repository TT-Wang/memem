#!/usr/bin/env bash
mkdir -p ~/.cortex/logs
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] Starting mine-all" >> ~/.cortex/logs/mine-cron.log
python3 /home/claude-user/cortex-plugin/cortex-mcp-server/server.py --mine-all >> ~/.cortex/logs/mine-cron.log 2>&1 || true
echo "[$TIMESTAMP] Done" >> ~/.cortex/logs/mine-cron.log
exit 0
