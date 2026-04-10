# Cortex

Persistent memory for Claude Code. Remembers across sessions, gets smarter over time.

## Install

```bash
claude plugin marketplace add TT-Wang/cortex-plugin
claude plugin install cortex@cortex-marketplace
```

## What it does

Cortex gives Claude Code a long-term memory. Without it, every session starts from zero. With Cortex, Claude remembers what worked, what failed, and what your project needs.

- **Auto-recalls** on every session — injects relevant memories before you even ask
- **Auto-mines** session logs — a background Haiku-powered daemon extracts insights from completed sessions
- **Auto-saves** lessons as you work — decisions, patterns, and conventions persist across sessions

Over time, the memory grows. The 50th session is dramatically better than the 1st.

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│                      Claude Code Session                     │
│                                                              │
│  ┌──────────────────┐       ┌─────────────────────────────┐ │
│  │ UserPromptSubmit  │──────▶│ Hook injects _index.md      │ │
│  │ hook (1st msg)    │       │ Claude picks relevant files  │ │
│  └──────────────────┘       │ Reads ~/obsidian-brain/      │ │
│                              │   cortex/memories/*.md       │ │
│  ┌──────────────────┐       └─────────────────────────────┘ │
│  │ During work       │                                       │
│  │  memory_save()  ──┼──┐                                    │
│  └──────────────────┘  │                                    │
└────────────────────────┼────────────────────────────────────┘
                         │
                         ▼
          ┌──────────────────────────┐
          │    Obsidian Vault         │
          │    ~/obsidian-brain/      │
          │      cortex/memories/     │
          │      cortex/_index.md     │
          └──────────┬───────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                    Miner Daemon (background)                  │
│                                                              │
│  Polls ~/.claude/projects/*/*.jsonl every 60s                │
│  Session idle 5min → parse exchanges → Haiku extracts        │
│  insights → dedup check → write to Obsidian vault            │
└─────────────────────────────────────────────────────────────┘
```

**Storage:** Obsidian vault is the single source of truth. Each memory is a markdown file with YAML frontmatter. An `_index.md` file catalogs all memories for fast lookup.

**Recall:** On session start, a hook injects the memory index. Claude reads the relevant `.md` files directly with the Read tool — no database layer in between.

**Mining:** A background daemon watches for completed sessions, sends exchanges to Haiku for knowledge extraction, deduplicates against existing memories, and writes new insights to the vault.

## Architecture

```
cortex-mcp-server/
├── server.py          MCP entrypoint (5 tools)
├── storage.py         Obsidian read/write, indexing, dedup
├── recall.py          Keyword search + Haiku smart recall
├── mining.py          Session mining with Haiku extraction
├── operations.py      memory_save, memory_import
├── transcripts.py     JSONL session parsing + search
├── session_state.py   Mined session tracking
├── miner-daemon.py    Background mining daemon
├── miner_protocol.py  Status/version constants
└── cli.py             CLI dispatch (--mine-all, --rebuild-index, etc.)

hooks/
├── auto-recall.sh     UserPromptSubmit hook — injects memory index
└── hooks.json         Hook configuration
```

## MCP Tools

| Tool | What |
|------|------|
| `memory_recall` | Search memories by keyword |
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |

## CLI

```bash
python3 server.py                    # Start MCP server (stdio)
python3 server.py --recall "query"   # Keyword search
python3 server.py --recall-smart "q" # Haiku-assisted recall
python3 server.py --mine-all         # Mine all pending sessions
python3 server.py --mine-session f   # Mine a single JSONL file
python3 server.py --rebuild-index    # Regenerate _index.md
python3 server.py --purge-mined      # Delete all mined memories
python3 miner-daemon.py start        # Start background miner
python3 miner-daemon.py stop         # Stop background miner
python3 miner-daemon.py status       # Check miner status
```

## Configuration

| Env var | Default | What |
|---------|---------|------|
| `CORTEX_OBSIDIAN_VAULT` | `~/obsidian-brain` | Path to your Obsidian vault |
| `CORTEX_EXTRA_SESSION_DIRS` | (none) | Extra session dirs to mine, colon-separated |
| `CORTEX_MINER_SETTLE_SECONDS` | `300` | Wait time before mining a session |

## Requirements

- Claude Code
- Python 3.11+
- Obsidian vault (default `~/obsidian-brain/`, configurable via env var)

## See Also

- **[Forge](https://github.com/TT-Wang/forge)** — Structured planning, parallel execution, and validation for Claude Code

## License

MIT
