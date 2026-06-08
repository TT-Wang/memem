---
name: memem-status
description: Show detailed memem memory system status — memory count, projects, search DB, miner health.
allowed-tools: [Bash]
---

Run `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --status` and display the results.

Mining is event-triggered in v2.1.0+: it fires automatically on every Claude Code Stop event when `~/.memem/.miner-opted-in` exists. There is no daemon to start. If the opt-in marker is missing, suggest: `touch ~/.memem/.miner-opted-in`
