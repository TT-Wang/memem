---
name: memem
description: Show memem welcome, onboarding, status, and help. Use when the user wants to learn about memem, check if it's working, or see available commands.
allowed-tools: [Bash, Read]
---

Show the memem welcome screen. Follow these steps in order.

**Step 1 — Check for a bootstrap error.**
Run: `[ -f ~/.memem/last-error.md ] && cat ~/.memem/last-error.md || true`
If the command returned content, show it to the user at the very top with a clear heading, then continue to Step 2. Do not suppress — the user needs to see it.

**Step 2 — Print the banner.**

```
  ███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗
  ████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║
  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║
  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║
  ██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║
  ╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝
  persistent memory for Claude Code
```

**Step 3 — Show current status.**
Run: `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --status`
Display the output in a code block.

**Step 4 — Check for an active auto-mine in progress.**
Run: `pgrep -f "memem.server --mine-all" > /dev/null && echo "MINING" || echo "IDLE"`
If the output is `MINING`, show this note prominently:

> ⚙️ **Auto-mining past sessions in background.** memem found prior Claude Code sessions and is extracting durable memories (decisions, conventions, lessons) via Claude Haiku. Runs silently — you can keep working. Large histories may take up to an hour.

**Step 5 — Show how memem works.**

**How memem works:**
1. You work normally in Claude Code — nothing to do.
2. A background miner watches for completed sessions.
3. ~5 minutes after a session ends, it extracts durable memories via Claude Haiku and writes them to `~/obsidian-brain/memem/memories/`.
4. Your next session starts with relevant context pre-loaded from memory — no re-explaining the project.

**Step 6 — Show commands and tools.**

**Slash commands:**
- `/memem` — this welcome + status screen
- `/memem-status` — detailed memory system status
- `/memem-doctor` — preflight health check with fix instructions
- `/memem-mine` — start/check the miner daemon
- `/memem-mine-history` — force mine all historical sessions (opt-in, may take hours)

**MCP tools** (Claude calls these automatically when useful; you can also ask for them by name):
- `memory_save` — store a lesson, pattern, or convention
- `memory_recall` — search memories by keyword
- `memory_list` — list all memories with stats
- `memory_import` — import from files or chat exports
- `transcript_search` — search raw session logs
- `context_assemble` — get a query-tailored briefing

**Step 7 — Show optional Obsidian note.**

**Obsidian (optional):** Memories are plain markdown files at `~/obsidian-brain/memem/memories/`. Open that folder as an Obsidian vault for graph view and backlinks. memem works identically without Obsidian.

**Opt out of auto-mining past sessions:** set `MEMEM_NO_AUTO_MINE=1` in your shell profile before installing.
