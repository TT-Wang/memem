# Cortex

Persistent memory for Claude Code. Remembers across sessions, gets smarter over time.

```bash
claude plugin install github:TT-Wang/cortex-plugin
```

## What it does

Cortex gives Claude Code a long-term memory. Without it, every session starts from zero. With it, Claude remembers what worked, what failed, and what your project needs.

**You don't need to do anything.** Once installed, Cortex works invisibly:

1. **Session starts** — Claude silently recalls relevant knowledge from memory
2. **You work normally** — Claude uses recalled context to give better answers
3. **Session ends** — Claude silently saves lessons learned

Over time, the memory grows. The 50th session is dramatically better than the 1st.

## First use

On your first session, Cortex auto-seeds by:

- Scanning your project (README, dependencies, git history, test framework)
- Loading starter packs (30+ universal engineering lessons + language-specific knowledge)
- Creating 40+ memories immediately — no cold start

Starter packs are auto-detected: Python, JavaScript, React, Rust, Go.

## How search works

Cortex uses hybrid search with 5 signals:

- **Semantic similarity** — local embeddings (sentence-transformers, no API key needed)
- **Keyword overlap** — exact term matching
- **Confidence score** — how reliable this memory has proven to be
- **Impact score** — success rate when this memory is used
- **Recency** — recently validated knowledge gets a small boost

Plus domain-aware filtering (auth, database, testing, API, deployment, etc.) that boosts same-domain memories.

## Memory lifecycle

```
New knowledge → candidate (confidence 0.5)
  → Retrieved and task succeeds → confidence increases
  → Retrieved and task fails → confidence decreases
  → retrieval_count >= 5 and success_rate > 0.7 → auto-promoted to "learned"
  → Learned memories are prioritized in future retrieval
```

## Memory hierarchy

- **L0** — Project identity (always loaded): project name, tech stack
- **L1** — Core knowledge (always loaded): high-confidence conventions, critical lessons
- **L2** — Domain knowledge (loaded when relevant): topic-specific memories
- **L3** — Deep details (loaded on search): imported docs, historical context

L0 + L1 are included in every context assembly (~200 tokens). L2 + L3 are searched on demand.

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

Chat exports (ChatGPT, Claude, generic logs) are returned to Claude for intelligent extraction — Claude reads the conversations and saves atomic lessons, not raw text dumps.

## Tools

| Tool | What | When |
|------|------|------|
| `context_assemble` | Search memory, return relevant context | Auto — every session start |
| `memory_save` | Store a lesson/pattern/convention | Auto — after completing work |
| `memory_recall` | Search memory by keyword | When specific info needed |
| `memory_list` | List all memories in scope | On request |
| `memory_feedback` | Report if recalled memory helped | Auto — strengthens/weakens |
| `memory_stats` | System overview | On request |
| `memory_promote` | Candidate → learned | On request |
| `memory_import` | Import from files/directories | On request |

## Where memories live

```
~/.cortex/
├── memories/           One JSON file per memory
│   └── {uuid}.json     {essence, confidence, impact_score, domain, tier, ...}
├── embeddings_cache/   Cached vector embeddings
├── logs/               Retrieval logs (for association tracking)
└── .seeded_scopes      Which projects have been auto-seeded
```

All local. You own the data. No cloud, no sync, no third-party access.

## What Cortex saves

- **Conventions**: "This project uses snake_case for API endpoints"
- **Lessons**: "RS256 is better than HS256 for production JWT"
- **Patterns**: "Always validate token expiry server-side"
- **Failures**: "Library X v2.3 has a race condition in async mode"
- **Preferences**: "User prefers concise responses without summaries"

What it doesn't save: trivial facts, things already in project docs, temporary state.

## Contradiction detection

When a new memory contradicts an existing one, both are flagged. During context assembly, contradictions are surfaced so Claude doesn't use stale knowledge.

## Requirements

- Claude Code
- Python 3.11+
- `sentence-transformers` (installed automatically for semantic search)

## License

MIT
