# Cortex

Persistent memory for Claude Code. Remembers across sessions, gets smarter over time.

## Install

```bash
claude plugin marketplace add TT-Wang/cortex-plugin
claude plugin install cortex@cortex-marketplace
```

## What it does

Cortex gives Claude Code a long-term memory. Without it, every session starts from zero. With Cortex, Claude remembers what worked, what failed, and what your project needs.

- **Auto-seeds** on first use — scans your workspace and loads starter packs (60+ memories, no cold start)
- **Auto-recalls** on every session — injects relevant memories before you even ask
- **Auto-mines** session logs — a background Haiku-powered daemon extracts insights from completed sessions
- **Auto-saves** lessons as you work — decisions, patterns, and conventions persist across sessions

Over time, the memory grows. The 50th session is dramatically better than the 1st.

## How it works

```
Storage:    ChromaDB (vector search) + Obsidian vault (human-readable markdown)
Retrieval:  Hook injects memory index → Claude reads relevant Obsidian files directly
Mining:     Background daemon watches for completed sessions → Haiku extracts insights
Lifecycle:  Candidate → feedback strengthens/weakens → auto-promote to "learned"
```

## MCP Tools

| Tool | What |
|------|------|
| `memory_recall` | Search all memory sources |
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_list` | List all memories with stats |
| `memory_feedback` | Report if recalled memory helped |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |

## Requirements

- Claude Code
- Python 3.11+
- `chromadb`, `sentence-transformers`
- Obsidian vault (memories stored as markdown at `~/obsidian-brain/cortex/`)

## See Also

- **[Forge](https://github.com/TT-Wang/forge)** — Structured planning, parallel execution, and validation for Claude Code

## License

MIT
