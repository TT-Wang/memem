# Cortex

Persistent memory for Claude Code. Remembers across sessions, gets smarter over time.

## Install

```bash
# Add the marketplace
claude plugin marketplace add TT-Wang/cortex-plugin

# Install
claude plugin install cortex@cortex-marketplace
```

Or from the Claude Code prompt:
```
/plugin marketplace add TT-Wang/cortex-plugin
/plugin install cortex@cortex-marketplace
```

## What it does

Cortex gives Claude Code a long-term memory. Without it, every session starts from zero. With it, Claude remembers what worked, what failed, and what your project needs.

Over time, the memory grows. The 50th session is dramatically better than the 1st.

## Architecture

```
Memory Generation                Memory Retrieval
  ├── auto-seed (first run)        ├── Hook injects Obsidian index
  ├── memory_save (Claude saves)   └── Claude reads Obsidian markdown files
  ├── Haiku-powered mining              directly via Read tool
  └── memory_import (files/dirs)
          │
          ▼
   ChromaDB (search index)
   Obsidian vault (human-readable)
```

**Dual-write storage**: every memory is saved to both ChromaDB (for vector search) and your Obsidian vault (for human browsing). Retrieval goes through Obsidian files directly — Claude reads the source markdown, not ChromaDB results.

## First use

On your first session, Cortex auto-seeds by:

- Scanning your workspace projects (README, dependencies, git history, test framework)
- Loading starter packs (30+ engineering best practices + language-specific knowledge)
- Creating 60+ memories immediately — no cold start

Starter packs are auto-detected: Python, JavaScript, React, Rust, Go.

## How search works

ChromaDB native vector similarity — one query, ranked results, no manual scoring.

```
memory_recall("fix auth bug")
  → ChromaDB vector search (semantic similarity)
  → transcript search (raw JSONL session history)
  → structured markdown → Claude reads it
```

No LLM calls in the search pipeline. Zero API cost. ~50ms latency.

**96.7% recall** on 30 hard benchmark questions (indirect phrasing, cross-domain, negation, scenario-based).

## Conversation mining

A persistent Haiku-powered miner daemon runs in the background, watching for completed Claude Code sessions and extracting insights:

```
~/.claude/projects/*/*.jsonl  (30-day TTL)
  → miner daemon polls every 60s for settled sessions (5min idle)
  → parse JSONL → extract exchange pairs
  → Haiku extracts structured insights (decisions, lessons, conventions)
  → dedup against existing memories (ChromaDB similarity check)
  → save to ChromaDB + Obsidian
```

Raw session logs stay in `~/.claude/` (auto-deleted after 30 days). Extracted insights persist forever as memories.

The miner scans both user and root session directories.

## Memory lifecycle

```
New knowledge → candidate (confidence 0.4-0.6)
  → Retrieved and task succeeds → confidence increases (+0.05)
  → Retrieved and task fails → confidence decreases (-0.10)
  → retrieval_count >= 5 and success_rate > 0.7 → auto-promoted to "learned"
  → Learned memories rank higher in search results
```

## Import existing knowledge

```
# Import from Obsidian vault
memory_import("~/obsidian-vault")

# Import ChatGPT export
memory_import("~/Downloads/conversations.json")
# → Claude reads the conversations and extracts knowledge

# Import any markdown/text directory
memory_import("~/notes/project-docs")
```

## Tools

| Tool | What |
|------|------|
| `memory_recall` | Search all memory sources — returns structured markdown |
| `memory_save` | Store a lesson/pattern/convention (dedup-checked) |
| `memory_list` | List all memories with stats |
| `memory_feedback` | Report if recalled memory helped (also promotes) |
| `memory_import` | Import from files/directories |
| `transcript_search` | Search raw Claude Code JSONL session files |

## Where memories live

```
~/.cortex/
├── chromadb/           ChromaDB database (vector search index)
├── logs/               Retrieval logs
├── .seeded_scopes      Which projects have been auto-seeded
└── .mined_sessions     Which JSONL sessions have been mined

~/obsidian-brain/cortex/memories/
└── *.md                Human-readable markdown (YAML frontmatter + content)
```

ChromaDB is the search index. Obsidian vault is the human-readable mirror. Both updated on every write.

## Garbage collection

```bash
python3 server.py --gc
```

Runs automatically after hourly mining. Three operations:
- **Decay**: delete memories with confidence < 0.3, never retrieved, older than 30 days
- **Merge**: find near-duplicates via ChromaDB similarity, keep higher confidence
- **Prune**: cap at 500 memories, protect "learned" status

## Requirements

- Claude Code
- Python 3.11+
- `chromadb` (vector database)

## License

MIT
