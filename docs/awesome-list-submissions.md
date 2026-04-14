# Awesome-List Submission Copy

Paste-ready entries for each community list. Submit one PR per list.

---

## awesome-claude-code (hesreallyhim/awesome-claude-code)

**Location in list:** under a "Plugins" or "Memory / Context" section if one exists, otherwise add a new "Memory & Context" section.

**Entry:**

```markdown
- [memem](https://github.com/TT-Wang/memem) — Persistent, self-evolving memory for Claude Code. A background miner extracts durable lessons from completed sessions, stores them as markdown in an Obsidian vault, and assembles query-tailored context briefings at the start of each new session. Local-first, no cloud, no API keys. Install via `/plugin install memem`.
```

**PR title:** `Add memem — persistent memory plugin for Claude Code`

**PR body:**
```
This adds memem to the Claude Code plugin list.

memem is a memory plugin that eliminates the re-explain-your-project friction: it auto-mines completed Claude Code sessions for durable knowledge (decisions, conventions, bug fixes), stores the results as human-readable markdown in an Obsidian vault, and injects a query-tailored context brief at the start of every new session via Claude Haiku.

- Local-first: no cloud services, no API keys, no vendor lock-in
- Self-healing install: `bootstrap.sh` uses uv to sync deps into a plugin-local venv on first run — no separate pip install step
- Graceful degraded mode: works with FTS-only recall if the claude CLI isn't available
- `/memem-doctor` slash command for preflight health checks with explicit fix instructions
- 54 tests, ruff clean, mypy strict
- MIT licensed
```

---

## awesome-mcp-servers (modelcontextprotocol/servers or community mirror)

**Location in list:** Memory / Knowledge / Personal Assistant section.

**Entry:**

```markdown
- [memem](https://github.com/TT-Wang/memem) 🐍 🏠 — Persistent memory for Claude Code. Background miner extracts lessons from sessions, stores as markdown in an Obsidian vault, assembles query-tailored context at session start. Local-first, no cloud.
```

(Emojis follow the list's convention: 🐍 Python, 🏠 local/self-hosted.)

**PR title:** `Add memem — persistent memory MCP server`

**PR body:**
```
Adds memem, an MCP server that provides persistent, self-evolving memory for Claude Code.

Tools exposed:
- memory_save(content, title, tags)
- memory_recall(query, limit)
- memory_list(scope_id)
- memory_import(source_path)
- transcript_search(query)
- context_assemble(query, project)

Storage: local markdown in an Obsidian vault at ~/obsidian-brain/memem/memories/, with a SQLite FTS5 index for fast search. No cloud services, no API keys required.

License: MIT
Language: Python 3.11+
```

---

## Claude Code Plugin Marketplace (TT-Wang/memem-marketplace → already submitted)

**Status:** submitted, awaiting approval.

---

## Generic "new tool" social-proof posts (manual, low priority)

If there are newer community lists worth tracking as they emerge, dump them here for a future batch:

- [ ] `awesome-claude` (non-code focused)
- [ ] `awesome-local-first` (for the local-first angle)
- [ ] `awesome-obsidian` (memem is tightly integrated with Obsidian as a browse layer)

These are secondary — only submit if the list is active (merged PRs in the last 30 days).

---

## Checklist before submitting each PR

- [ ] README has a clear one-paragraph description at the top
- [ ] `llms.txt` is present at repo root
- [ ] License is clearly MIT in both pyproject.toml and LICENSE
- [ ] At least one release tag exists (`v0.5.0` is live)
- [ ] Tests green on the default branch
- [ ] CI workflow is passing (green check on master)
- [ ] No placeholder text ("TODO", "lorem ipsum") anywhere in the README
- [ ] Entry matches the list's alphabetical / categorical convention
- [ ] Entry is under 200 characters (most lists enforce this)
