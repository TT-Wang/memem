---
name: memem-mine
description: Start the memem miner daemon to automatically mine new sessions going forward.
allowed-tools: [Bash]
---

Start the memem miner daemon. It runs in the background and automatically extracts knowledge from new Claude Code sessions as they complete.

1. Check if the miner is already running:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/memem/miner-wrapper.sh" status
```

2. If not running, start it:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/memem/miner-wrapper.sh" start
```

3. If already running, tell the user:
- The miner is active and mining new sessions automatically
- It polls every 60 seconds for completed sessions
- Run `/memem-status` to check memory count and health

The miner only processes sessions created after memem was installed. For mining older history, use `/memem-mine-history`.
