---
name: memem-status
description: Show detailed memem memory system status — memory count, projects, search DB, miner health.
allowed-tools: [Bash]
---

Run `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --status` and display the results.

If the miner is not running, offer to start it:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/memem/miner-wrapper.sh" start
```
