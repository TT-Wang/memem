---
name: memem-welcome
description: Re-show the full memem welcome screen regardless of whether the user has seen it before. Use when the user wants to see the intro, tutorial, or welcome again.
allowed-tools: [Bash, Read]
---

Force-display the full memem welcome (the brand-new-user view), even if the user has already dismissed it. This is the "re-open walkthrough" handle — it exists so returning users who want the intro again don't have to delete state files manually.

**Step 1 — Print the banner:**

```
  ███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗
  ████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║
  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║
  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║
  ██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║
  ╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝
  persistent memory for Claude Code
```

**Step 2 — One-sentence pitch:**

> memem gives Claude Code persistent memory across sessions. It extracts durable lessons (decisions, conventions, bug fixes, preferences) from your completed sessions into markdown files, then surfaces the relevant ones at the start of each new session.

**Step 3 — How it works:**

1. You work normally in Claude Code — nothing to do.
2. A background miner watches for completed sessions (once you've opted in via `/memem-mine` or `/memem-mine-history`).
3. ~5 minutes after a session ends, it extracts durable memories via Claude Haiku and writes them to `~/obsidian-brain/memem/memories/`.
4. Your next session starts with relevant context pre-loaded from memory — no re-explaining the project.

**Step 4 — Mining options:**

- **`/memem-mine`** — start the miner, mine only new sessions going forward
- **`/memem-mine-history`** — start the miner + mine all past history (uses Haiku API credits)

Or just tell Claude:

- "start mining new sessions"
- "start mining everything including history"

If the user is unsure, recommend **`/memem-mine`** as the default.

**Step 5 — Commands and tools:**

**Slash commands:**
- `/memem` — status (full welcome for new users, compact status for returning users)
- `/memem-welcome` — this screen (re-show intro)
- `/memem-status` — detailed memory system status
- `/memem-doctor` — preflight health check with fix instructions
- `/memem-mine` — start the miner (new sessions only)
- `/memem-mine-history` — start the miner + mine past history

**MCP tools** (Claude calls these automatically when useful):
- `memory_save`, `memory_recall`, `memory_list`, `memory_import`, `transcript_search`, `context_assemble`

**Step 6 — Optional Obsidian note:**

Memories are plain markdown files at `~/obsidian-brain/memem/memories/`. Open that folder as an Obsidian vault for graph view and backlinks. memem works identically without Obsidian.

**Step 7 — First action suggestion:**

If the user asks "what should I do now?" or seems undecided, answer with one short recommendation:

> Start with `/memem-mine`. It enables memory for new sessions going forward without spending Haiku credits on old history.

**Step 8 — Opt-out:**

To stop the miner and opt out: `python3 -m memem.server --miner-opt-out`
