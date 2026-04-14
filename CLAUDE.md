# memem — Persistent Memory & Context Assembly

You have access to a persistent memory system via memem. This memory persists across sessions, self-evolves, and gets smarter over time.

Storage: Obsidian vault at `~/obsidian-brain/memem/memories/` (markdown files, single source of truth).
Machine index: SQLite FTS5 at `~/.memem/search.db` for fast retrieval.

## Auto-recall

The UserPromptSubmit hook fires on your first message and assembles a query-tailored context briefing using `context_assemble`. This uses your message to find relevant memories and produce a focused brief — not a raw index dump.

For deeper recall during the session, use the MCP tools below.

## Auto-save

When you complete significant work, save non-obvious lessons:

Call `mcp__memem__memory_save` with:
- `content`: The key insight (one per save, keep it atomic)
- `title`: Short descriptive title
- `tags`: Comma-separated relevant tags

**Save these (durable knowledge):**
- User preferences, corrections, conventions
- Architecture decisions with rationale
- Environment facts, tool quirks, project structure
- Non-obvious lessons learned from failures

**Do NOT save these (use transcript_search instead):**
- Task progress, session outcomes, what was worked on today
- Completed-work logs or TODO state
- Trivial or obvious facts easily re-discovered from code

## Starting the miner (opt-in)

memem is **opt-in** as of v0.9.0 — install does not start any background processes. The miner daemon only runs once the user explicitly enables it. Opt-in is tracked by the marker file `~/.memem/.miner-opted-in`.

When the user asks to start memem, start mining, enable memory extraction, or similar — **identify which of the two modes they want** and run the matching commands:

**Mode 1 — "start mining new sessions" / "start the miner" / "enable memem" / "start memem"** (no history):
```bash
mkdir -p ~/.memem && touch ~/.memem/.miner-opted-in
bash "${CLAUDE_PLUGIN_ROOT}/memem/miner-wrapper.sh" start
```
Then tell the user: the miner is running, it will mine new sessions automatically ~5 min after they end, and it will auto-start on future Claude Code launches.

**Mode 2 — "mine everything" / "mine history" / "mine all my past sessions" / "include history"** (full history + ongoing):
```bash
mkdir -p ~/.memem && touch ~/.memem/.miner-opted-in
nohup PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --mine-all > ~/.memem/mine-all.log 2>&1 &
bash "${CLAUDE_PLUGIN_ROOT}/memem/miner-wrapper.sh" start
```
Then tell the user: history mining is running in the background (log at `~/.memem/mine-all.log`), the ongoing miner is also running, and they can continue working normally. Warn them if the session count is large that this may take up to an hour and uses Haiku API credits.

**If unsure which mode the user wants, ask.** Don't default — the difference matters (API cost, time).

**To stop / opt out:** `python3 -m memem.server --miner-opt-out` (stops daemon and removes marker so it won't auto-start next time).

## Available tools

| Tool | What |
|------|------|
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_recall` | Search memories (FTS5 + keyword + temporal ranking) |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |
| `context_assemble` | On-demand query-tailored briefing from all knowledge |

## Backward compatibility

memem was renamed from `cortex` in v0.7.0. Existing users with data under
`~/.cortex/` and `~/obsidian-brain/cortex/` are auto-migrated to `~/.memem/`
and `~/obsidian-brain/memem/` on first run via a one-time copy. Legacy paths
remain intact as a safety net. Legacy `CORTEX_*` env vars are still read as
fallbacks for `MEMEM_*`.
