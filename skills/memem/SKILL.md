---
name: memem
description: Show memem welcome, onboarding, and help. Use when the user wants to learn about memem or see available commands.
allowed-tools: [Bash, Read]
---

Show the memem welcome banner and help information.

Print this banner:

```
  ███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗
  ████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║
  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║
  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║
  ██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║
  ╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝
  persistent memory for Claude Code
```

Then run `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --status` to get current system status.

Then show this help:

**Available commands:**
- `/memem` — this help screen
- `/memem-status` — detailed memory system status
- `/memem-doctor` — preflight health check with fix instructions
- `/memem-mine` — start/check the miner daemon (new sessions)
- `/memem-mine-history` — mine all historical sessions (opt-in, may take hours)

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
- Memories at `memem/memories/`, playbooks at `memem/playbooks/`
