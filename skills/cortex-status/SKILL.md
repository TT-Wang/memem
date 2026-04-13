---
name: cortex-status
description: Show detailed Cortex memory system status — memory count, projects, search DB, miner health.
allowed-tools: [Bash]
---

Run `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m cortex_server.server --status` and display the results.

If the miner is not running, offer to start it:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/cortex_server/miner-wrapper.sh" start
```
