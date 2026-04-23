---
name: memem-mine
description: Start the memem miner daemon to automatically mine new sessions going forward.
allowed-tools: [Bash]
---

Start the memem miner daemon. It runs in the background and automatically extracts knowledge from new Claude Code sessions as they complete. This also opts the user into auto-starting the miner on future Claude Code launches.

1. Check if the miner is already running:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --miner-status
```

2. If not running, start it and record the opt-in:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --miner-start
```

3. Tell the user:
- The miner is now active and will mine new sessions automatically
- It polls every 60 seconds for completed sessions
- Mining fires ~5 minutes after a session ends (so it only processes settled transcripts)
- Run `/memem-status` to check memory count and health
- The miner will also auto-start on future Claude Code launches
- To stop it permanently: `bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --miner-opt-out`

The miner only processes sessions created after memem was installed. For mining older history, use `/memem-mine-history`.
