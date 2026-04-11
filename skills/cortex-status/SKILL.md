---
name: cortex-status
description: Show detailed Cortex memory system status — memory count, projects, search DB, miner health.
allowed-tools: [Bash]
---

Run `python3 "${CLAUDE_PLUGIN_ROOT}/cortex-mcp-server/server.py" --status` and display the results.

If the miner is not running, offer to start it:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/cortex-mcp-server/miner-wrapper.sh" start
```
