# Cortex — Persistent Memory & Context Assembly

You have access to a persistent memory system via Cortex. This memory persists across sessions, self-evolves, and gets smarter over time.

Storage: Obsidian vault at `~/obsidian-brain/cortex/memories/` (markdown files, single source of truth).
Machine index: SQLite FTS5 at `~/.cortex/search.db` for fast retrieval.

## Auto-recall

The UserPromptSubmit hook fires on your first message and assembles a query-tailored context briefing using `context_assemble`. This uses your message to find relevant memories and produce a focused brief — not a raw index dump.

For deeper recall during the session, use the MCP tools below.

## Auto-save

When you complete significant work, save non-obvious lessons:

Call `mcp__plugin_cortex_cortex__memory_save` with:
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

## Mining existing sessions

By default, Cortex only mines sessions created after installation. When a user asks to mine their existing history (e.g. "mine my existing sessions", "mine my history"), run this in the background:

```bash
nohup python3 "${CLAUDE_PLUGIN_ROOT}/cortex-mcp-server/server.py" --mine-all > /dev/null 2>&1 &
```

The `--mine-all` flag clears the install-time gate so all historical sessions are included. Tell the user it's running in the background and they can continue working normally.

## Available tools

| Tool | What |
|------|------|
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_recall` | Search memories (FTS5 + keyword + temporal ranking) |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |
| `context_assemble` | On-demand query-tailored briefing from all knowledge |
