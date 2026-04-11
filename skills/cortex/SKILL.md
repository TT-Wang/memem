---
name: cortex
description: Show Cortex welcome, onboarding, and help. Use when the user wants to learn about Cortex or see available commands.
allowed-tools: [Bash, Read]
---

Show the Cortex welcome banner and help information.

Print this banner:

```
  ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗
 ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝
 ██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝ 
 ██║     ██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗ 
 ╚██████╗╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗
  ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
  persistent memory for AI
```

Then run `python3 "${CLAUDE_PLUGIN_ROOT}/cortex-mcp-server/server.py" --status` to get current system status.

Then show this help:

**Available commands:**
- `/cortex` — this help screen
- `/cortex-status` — detailed memory system status
- `/cortex-mine` — mine existing Claude Code sessions for knowledge

**MCP tools:**
- `memory_save` — store a lesson, pattern, or convention
- `memory_recall` — search memories by keyword
- `memory_list` — list all memories with stats
- `memory_import` — import from files or chat exports
- `transcript_search` — search raw session logs
- `context_assemble` — get a query-tailored briefing

**Setup Obsidian (optional):**
- Download: https://obsidian.md (free)
- Open `~/obsidian-brain` as a vault
- Memories at `cortex/memories/`, playbooks at `cortex/playbooks/`
