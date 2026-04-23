---
name: memem-status
description: Show detailed memem memory system status — memory count, projects, search DB, miner health.
allowed-tools: [Bash]
---

Run `bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --status` and display the results.

If the miner is not running, report that this is okay when the user only wants hook/MCP recall. Do not offer to start the daemon unless the user explicitly asks for automatic mining.
