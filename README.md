# Cortex

**Claude Code that remembers your project across every session.**

Stop re-explaining your conventions, architecture, and lessons. Cortex gives Claude Code a persistent memory that learns from every conversation and surfaces what matters when you need it.

```
  ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗
 ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝
 ██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝ 
 ██║     ██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗ 
 ╚██████╗╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗
  ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
```

## The problem

Every Claude Code session starts from zero. You re-explain:
- What your project does
- Which framework you're using
- Why you made that weird decision three weeks ago
- What you tried last time that didn't work
- Your coding conventions

Multiply by every session, every week, forever.

## What Cortex does

Cortex runs in the background and turns your conversations into durable knowledge. When you start a new session, it assembles a tailored briefing from everything it's learned about your project.

- **Auto-extracts** decisions, conventions, and lessons from every session
- **Self-evolves** — merges related memories, deprecates outdated ones, consolidates redundancy
- **Query-aware** — your first message triggers a Haiku-assembled context brief, not a raw index dump
- **Human-readable** — every memory is a markdown file in your Obsidian vault
- **Secure** — scans every write for prompt injection and credential exfiltration

## Install

```bash
claude plugin marketplace add TT-Wang/cortex-plugin
claude plugin install cortex@cortex-marketplace
```

That's it. On first run, Cortex creates an Obsidian vault at `~/obsidian-brain` and starts a background miner daemon. Your existing Claude Code sessions stay private by default — run `/cortex-mine-history` if you want to mine them.

## First session

```
You: help me fix the auth bug

Cortex: [injects context automatically]
- JWT tokens use RS256 in production (memory from last week)
- bcrypt.compare is async — must await (memory from 2 days ago)  
- Auth routes in src/routes/auth.ts
- JWT_SECRET defaults to 'secret' if not set (known issue)

Claude: Let me look at src/routes/auth.ts...
```

No re-explaining. Claude starts with context.

## How it works

```
Session → auto-recall hook fires on first message
  → context_assemble(query) → Haiku assembles tailored brief
  → Relevant memories injected, zero tool calls needed

During session → memory_recall / memory_save / context_assemble available
  → Save lessons as you discover them

Background → miner daemon extracts knowledge from completed sessions
  → AUDN pipeline: Add / Update / Noop / Deprecate
  → Consolidation merges redundant, deprecates obsolete
  → Playbooks grow per-project, refine periodically

Storage → Obsidian vault (source of truth, human-readable)
  → SQLite FTS5 (machine-fast search index)
  → Telemetry sidecar (access tracking, no write amplification)
  → Event log (append-only audit trail)
```

## Why not just use vanilla Claude Code?

Vanilla Claude Code gives you `CLAUDE.md` — a single file you edit by hand. Cortex gives you:

- **Automatic knowledge capture** — no manual note-taking
- **Query-aware context** — only relevant memories injected per session
- **Self-evolving** — memories merge, update, and deprecate automatically
- **Cross-project** — works across all your Claude Code projects
- **Security scanning** — blocks prompt injection, credential leaks
- **Obsidian integration** — browse your AI's knowledge visually

## Who it's for

Cortex is for developers who:
- Use Claude Code daily and keep re-explaining their project
- Want local-first storage — no cloud services, no API keys, no vendor lock-in
- Care about human-readable memories they can browse and edit
- Like the Obsidian ecosystem

## Commands

Slash commands (type in Claude Code):
- `/cortex` — welcome, status, help
- `/cortex-status` — memory count, projects, miner health
- `/cortex-mine` — start the miner daemon
- `/cortex-mine-history` — opt-in to mine pre-install sessions

CLI (from terminal):
```bash
python3 server.py --status                # System status
python3 server.py --assemble-context "q"  # Context assembly
python3 server.py --events                # Recent audit log
python3 server.py --eval                  # Quality smoke test
python3 server.py --rebuild-search-index  # Rebuild FTS5 index
```

## MCP tools (for the LLM)

| Tool | What |
|------|------|
| `memory_recall` | Search memories (FTS5 + keyword + temporal + importance) |
| `memory_save` | Store a lesson (security scanned) |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, chat exports |
| `transcript_search` | Search raw Claude Code session logs |
| `context_assemble` | On-demand query-tailored briefing |

## Architecture highlights

**Memory schema** (stored as markdown frontmatter):
```yaml
---
id: uuid
title: "descriptive title"
project: project-name
tags: [mined, project-name]
related: [id1, id2, id3]
created: 2026-04-13
updated: 2026-04-13
source_type: mined | user | import
source_session: abc12345
importance: 1-5
status: active | deprecated
valid_to:                     # set when deprecated
contradicts: [id1]            # flagged conflicts
---
```

**Multi-signal scoring** (recall ranking):
- 50% keyword/FTS relevance
- 15% recency (0.995^hours decay)
- 15% access history (usage reinforcement)
- 20% importance (1-5 scale from Haiku)

**Three-layer memory flow**:
- **Raw memories** — atomic units, markdown files
- **Playbooks** — compiled per-project knowledge, refined periodically
- **Context briefs** — query-tailored assembly via Haiku

## Setup Obsidian (optional but recommended)

Cortex works without Obsidian — it just writes markdown files. But Obsidian makes browsing your AI's knowledge visual:

1. Download: https://obsidian.md (free)
2. Open `~/obsidian-brain` as a vault
3. Memories appear in `cortex/memories/`, playbooks in `cortex/playbooks/`
4. Use Graph View to see how memories link via the `related` field

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `CORTEX_OBSIDIAN_VAULT` | `~/obsidian-brain` | Vault location |
| `CORTEX_EXTRA_SESSION_DIRS` | (none) | Extra session dirs to mine |
| `CORTEX_MINER_SETTLE_SECONDS` | `300` | Wait before mining a session |

## Requirements

- Claude Code
- Python 3.11+
- `mcp` Python package (`pip install mcp`)

## Data locations

| Store | Path | Purpose |
|-------|------|---------|
| Memories | `~/obsidian-brain/cortex/memories/*.md` | Source of truth |
| Playbooks | `~/obsidian-brain/cortex/playbooks/*.md` | Per-project briefings |
| Search DB | `~/.cortex/search.db` | SQLite FTS5 index |
| Telemetry | `~/.cortex/telemetry.json` | Access tracking |
| Event log | `~/.cortex/events.jsonl` | Audit trail |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, testing, and PR process.

```bash
git clone https://github.com/TT-Wang/cortex-plugin.git
cd cortex-plugin
pip install -e ".[dev]"
pytest
```

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Architecture

Cortex is split into focused modules:
- `models.py` — data types, constants, path definitions
- `security.py` — threat scanning (prompt injection, credential exfil)
- `telemetry.py` — access tracking, event log
- `search_index.py` — SQLite FTS5
- `obsidian_store.py` — memory I/O, dedup, scoring
- `playbook.py` — grow and refine
- `assembly.py` — context assembly
- `storage.py` — thin facade re-exporting the above

## Contributing

Issues, PRs, and ideas welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
