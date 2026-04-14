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

**Step 4 — Check miner state and past-session count.**
Run these two commands:
```bash
[ -f ~/.memem/.miner-opted-in ] && echo "OPTED_IN" || echo "NOT_OPTED_IN"
find ~/.claude/projects/ -name "*.jsonl" ! -path "*/subagents/*" -size +5k 2>/dev/null | wc -l | tr -d ' '
```

**Step 5 — Branch on the miner state:**

**If `NOT_OPTED_IN` (miner has not been started yet):**
Show this block prominently:

> ### 🟡 memem is installed but the miner is idle
>
> memem won't extract any memories until you start the miner. You have two options:
>
> - **Mine only new sessions going forward** — from now on, every completed Claude Code session will be extracted into durable memories. No API cost for past history.
>   - Say: **"start mining new sessions"** — or type `/memem-mine`
>
> - **Mine everything, including your past [N] Claude Code sessions** — extracts memories from your full history plus all new sessions going forward. Uses Claude Haiku API credits; may take up to an hour for large histories.
>   - Say: **"start mining everything including history"** — or type `/memem-mine-history`
>
> Replace `[N]` with the number from step 4. If the number is 0, skip the second option.

**If `OPTED_IN` (miner is active):**
Run: `pgrep -f "memem.server --mine-all" > /dev/null && echo "MINING_HISTORY" || echo "IDLE"`
If `MINING_HISTORY`, show:
> ⚙️ **Mining history in background.** memem is extracting memories from your past Claude Code sessions via Claude Haiku. Runs silently — you can keep working.

Otherwise show:
> ✅ **Miner is active** — new Claude Code sessions will be mined automatically ~5 minutes after they end. To stop: `python3 -m memem.server --miner-opt-out`.

**Step 6 — Show how memem works.**

**How memem works:**
1. You work normally in Claude Code — nothing to do.
2. A background miner watches for completed sessions (once you start it).
3. ~5 minutes after a session ends, it extracts durable memories via Claude Haiku and writes them to `~/obsidian-brain/memem/memories/`.
4. Your next session starts with relevant context pre-loaded from memory — no re-explaining the project.

**Step 7 — Show commands and tools.**

**Slash commands:**
- `/memem` — this welcome + status screen
- `/memem-status` — detailed memory system status
- `/memem-doctor` — preflight health check with fix instructions
- `/memem-mine` — start the miner (new sessions only)
- `/memem-mine-history` — start the miner AND mine past history (opt-in, may take hours)

**MCP tools** (Claude calls these automatically when useful; you can also ask for them by name):
- `memory_save` — store a lesson, pattern, or convention
- `memory_recall` — search memories by keyword
- `memory_list` — list all memories with stats
- `memory_import` — import from files or chat exports
- `transcript_search` — search raw session logs
- `context_assemble` — get a query-tailored briefing

**Step 8 — Show optional Obsidian note.**

**Obsidian (optional):** Memories are plain markdown files at `~/obsidian-brain/memem/memories/`. Open that folder as an Obsidian vault for graph view and backlinks. memem works identically without Obsidian.
