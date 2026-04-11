# Cortex

Persistent memory and context assembly for Claude Code. Remembers across sessions, self-evolves, gets smarter over time.

## Install

### Step 1: Install the plugin

```bash
claude plugin marketplace add TT-Wang/cortex-plugin
claude plugin install cortex@cortex-marketplace
```

### Step 2: Install Obsidian (optional but recommended)

Cortex stores memories as markdown files in an Obsidian vault. You don't *need* Obsidian to use Cortex — it works with plain files. But Obsidian lets you browse, search, and visualize your AI's knowledge.

**Download Obsidian:** https://obsidian.md (free for personal use, Mac/Windows/Linux)

After installing:
1. Open Obsidian
2. Choose "Open folder as vault"
3. Select `~/obsidian-brain` (Cortex creates this automatically on first run)
4. Your memories will appear under `cortex/memories/` as markdown files
5. Project playbooks appear under `cortex/playbooks/`

**Useful Obsidian features for Cortex:**
- **Graph view** — see how memories link to each other via the `related` field
- **Search** — full-text search across all memories
- **Dataview plugin** — query memories by frontmatter fields (importance, project, status)
- **Tags** — click any tag to see related memories

### Step 3: Verify setup

Start Claude Code and the plugin loads automatically. Or check manually:

```bash
# Check status (run from the plugin directory)
python3 ~/.claude/plugins/cache/cortex-marketplace/cortex/*/cortex-mcp-server/server.py --status
```

The miner daemon starts automatically when the MCP server loads. The vault directory (`~/obsidian-brain/cortex/`) is created automatically on first run.

### Custom vault location

If you already have an Obsidian vault elsewhere:

```bash
# In your shell profile (~/.bashrc or ~/.zshrc)
export CORTEX_OBSIDIAN_VAULT="$HOME/my-obsidian-vault"
```

Cortex will create a `cortex/memories/` folder inside your existing vault.

## What it does

Cortex gives Claude Code a long-term memory. Without it, every session starts from zero. With Cortex, Claude remembers what worked, what failed, and what your project needs.

- **Context assembly** — first message triggers a query-tailored briefing assembled by Haiku
- **Auto-mining** — background daemon extracts durable knowledge from completed sessions
- **Self-evolving** — memories merge, deprecate, and consolidate automatically
- **Security scanned** — all memory writes checked for prompt injection and credential exfil

## Architecture

```
Session starts → User types first message
  → Hook fires → context_assemble(query) → Haiku assembles tailored brief
  → LLM starts working with relevant context, zero tool calls needed

During session:
  → memory_recall / context_assemble available for deeper dives
  → memory_save for persisting new knowledge

Background (miner daemon):
  → Extract conversation from JSONL → Haiku extracts memories
  → AUDN pipeline: Add / Update (merge) / Noop (skip) / Deprecate
  → Consolidation: merge redundant, deprecate obsolete
  → Playbook: grow (stage) + refine (compile)

Storage:
  → Obsidian vault (source of truth, human-readable)
  → SQLite FTS5 (search index, machine-fast)
  → Telemetry sidecar (access tracking)
  → Event log (audit trail)
```

## MCP Tools

| Tool | What |
|------|------|
| `memory_recall` | Search memories (FTS5 + keyword + temporal + importance scoring) |
| `memory_save` | Store a lesson, pattern, or convention (security scanned) |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |
| `context_assemble` | On-demand query-tailored briefing from all knowledge |

## Memory Schema

Each memory is a markdown file with YAML frontmatter:

```yaml
---
id: uuid
title: "descriptive title"
project: project-name
tags: [mined, project-name]
related: [id1, id2, id3]
created: 2026-04-11
updated: 2026-04-11
source_type: mined | user | import
source_session: abc12345
importance: 3        # 1-5 scale
status: active       # active | deprecated
valid_to:            # set when deprecated
contradicts: [id1]   # flagged conflicts
---

Memory content here.
```

## Scoring

Recall uses multi-signal scoring:
- **50%** keyword/FTS relevance (with stemming + synonym expansion)
- **15%** recency (exponential decay, 0.995^hours)
- **15%** access history (usage reinforcement)
- **20%** importance (1-5 scale from extraction)

Dedup/merge uses content-only containment scoring (no temporal bias).

## CLI

```bash
python3 server.py                          # Start MCP server
python3 server.py --assemble-context "q"   # Context assembly
python3 server.py --recall "query"         # Keyword search
python3 server.py --recall-smart "query"   # Haiku-assisted recall
python3 server.py --mine-all              # Mine all pending sessions
python3 server.py --mine-session file.jsonl # Mine single session
python3 server.py --rebuild-index          # Regenerate _index.md
python3 server.py --rebuild-playbooks      # Refine all playbooks
python3 server.py --rebuild-search-index   # Rebuild SQLite FTS5
python3 server.py --eval                   # Run quality eval
python3 server.py --events                 # View recent event log
python3 server.py --purge-mined            # Delete all mined memories
python3 miner-daemon.py start|stop|status  # Miner daemon
bash miner-wrapper.sh start|stop|status    # Miner with auto-restart
```

## Configuration

| Env var | Default | What |
|---------|---------|------|
| `CORTEX_OBSIDIAN_VAULT` | `~/obsidian-brain` | Path to Obsidian vault |
| `CORTEX_EXTRA_SESSION_DIRS` | (none) | Extra session dirs to mine |
| `CORTEX_MINER_SETTLE_SECONDS` | `300` | Wait before mining a session |

## Data Locations

| Store | Path | Purpose |
|-------|------|---------|
| Memories | `~/obsidian-brain/cortex/memories/*.md` | Source of truth |
| Index | `~/obsidian-brain/cortex/_index.md` | Flat catalog |
| Playbooks | `~/obsidian-brain/cortex/playbooks/*.md` | Per-project briefings |
| Search DB | `~/.cortex/search.db` | SQLite FTS5 index |
| Telemetry | `~/.cortex/telemetry.json` | Access tracking |
| Event log | `~/.cortex/events.jsonl` | Audit trail |
| Miner state | `~/.cortex/.mined_sessions` | Mining progress |

## Requirements

- Claude Code
- Python 3.11+
- Obsidian vault (default `~/obsidian-brain/`, configurable)

## License

MIT
