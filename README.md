# Cortex

**Persistent, self-evolving memory for Claude Code.** Stop re-explaining your project every session.

> For LLM/AI tool discovery, see [llms.txt](./llms.txt).

```
  ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗
 ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝
 ██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝
 ██║     ██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗
 ╚██████╗╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗
  ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
```

## What is Cortex?

Cortex is a Claude Code plugin that gives Claude persistent memory across sessions. A background miner extracts durable lessons (decisions, conventions, bug fixes, preferences) from your completed sessions, stores them as markdown in an Obsidian vault, and automatically surfaces relevant ones at the start of each new session via a query-tailored briefing.

It's **local-first**: no cloud services, no API keys required, no vendor lock-in. Everything lives in `~/obsidian-brain/cortex/memories/` as human-readable markdown.

## When should I use Cortex?

Use Cortex if:
- You use Claude Code daily and keep re-explaining your project to every new session
- You want durable memory you can browse and edit as markdown
- You like local-first tools with zero vendor lock-in
- You already use Obsidian (Cortex plugs straight into your vault)

Skip Cortex if:
- `CLAUDE.md` works fine for you and you don't want a background daemon
- You need cloud-synced memory across machines (Cortex is local-only)
- You're on Python < 3.11

## How is Cortex different from CLAUDE.md?

`CLAUDE.md` is a single hand-edited file per project. Cortex gives you:

- **Automatic extraction** — no manual note-taking, the miner captures lessons from every completed session
- **Query-aware context** — only the memories relevant to your current question get injected, not a static dump
- **Self-evolving** — memories merge, update, and deprecate automatically as your project evolves
- **Cross-project** — works across every Claude Code project you use, not scoped to one repo
- **Security scanning** — every write is scanned for prompt injection and credential exfiltration
- **Browsable** — Obsidian vault with graph view and backlinks for free

## How do I install Cortex?

```bash
/plugin marketplace add TT-Wang/cortex-plugin
/plugin install cortex
```

That's it. On first run, `bootstrap.sh` self-heals everything:

1. Verifies Python ≥ 3.11
2. Installs `uv` if missing (via the official Astral installer)
3. Syncs deps into a plugin-local `.venv` (hash-cached against `uv.lock`)
4. Creates and canary-tests `~/.cortex/` and `~/obsidian-brain/`
5. Writes `~/.cortex/.capabilities` (used for degraded-mode decisions)
6. Execs the real MCP server

**First run:** ~5 seconds. **Every run after:** ~100ms. No separate `pip install` step.

## What happens on my first Claude Code session?

You type your first message. The UserPromptSubmit hook fires and sees zero memories (you just installed it), so it injects a welcome banner into Claude's context. Claude reads the banner, tells you Cortex is active, and — if you have pre-existing Claude Code sessions — offers to mine them via `/cortex-mine-history`.

You work normally. The miner daemon runs silently in the background. When your session ends and settles for 5 minutes, the miner extracts memories from the transcript using Claude Haiku and writes them to your vault.

**Session 2 onwards:** the hook sends your first message to `context_assemble`, which gives Haiku the relevant memories and asks it to synthesize a tailored briefing. You see a status banner like `[Cortex] 12 memories · miner OK · assembly OK` followed by the brief. Claude starts with full context — no re-explaining.

## What does Cortex save?

It saves durable knowledge, not session logs:

- **Architecture decisions** with rationale ("we use RS256 JWTs because …")
- **Conventions** ("tests go in `tests/` not `spec/`")
- **Bug fixes you might forget** ("bcrypt.compare is async — must `await`")
- **User preferences** ("prefer single commits, not stacked PRs")
- **Known issues** ("JWT_SECRET defaults to 'secret' if unset — tracked in #123")

It does NOT save:

- Raw session transcripts (those are searchable via `transcript_search`, not stored as memories)
- Trivial or obvious facts
- Session outcomes ("today I worked on X")

## Where does Cortex store my memories?

| Store | Path | Purpose |
|-------|------|---------|
| Memories | `~/obsidian-brain/cortex/memories/*.md` | Source of truth (human-readable markdown) |
| Playbooks | `~/obsidian-brain/cortex/playbooks/*.md` | Per-project curated briefings |
| Search DB | `~/.cortex/search.db` | SQLite FTS5 index (machine-fast lookup) |
| Telemetry | `~/.cortex/telemetry.json` | Access tracking (atomic writes) |
| Event log | `~/.cortex/events.jsonl` | Append-only audit trail |
| Capabilities | `~/.cortex/.capabilities` | Degraded-mode flags written by bootstrap |
| Bootstrap log | `~/.cortex/bootstrap.log` | First-run diagnostics |

You can point Cortex elsewhere via `CORTEX_DIR` and `CORTEX_OBSIDIAN_VAULT` env vars.

## What are the MCP tools Claude can call?

| Tool | What it does |
|------|------|
| `memory_save(content, title, tags)` | Store a lesson. Security-scanned for prompt injection and credential exfil before writing. |
| `memory_recall(query, limit)` | Search memories. FTS5 + temporal decay + access reinforcement + importance weighting. |
| `memory_list(scope_id)` | List all memories with stats, grouped by project. |
| `memory_import(source_path)` | Bulk import from files, directories, or chat exports. |
| `transcript_search(query)` | Search raw Claude Code session JSONL logs (not the mined memories). |
| `context_assemble(query, project)` | On-demand query-tailored briefing from playbooks + memories + transcripts. |

## What slash commands does Cortex add?

- `/cortex` — welcome, status, help
- `/cortex-status` — memory count, projects, search DB size, miner health
- `/cortex-doctor` — preflight health check with fix instructions for any blocker
- `/cortex-mine` — start the miner daemon manually (normally auto-starts)
- `/cortex-mine-history` — opt-in: mine all your pre-install Claude Code sessions

## What if the `claude` CLI isn't on my PATH?

Cortex enters **degraded mode** — it still works, just without Haiku-powered context assembly and smart recall. You get FTS-only keyword recall instead of query-tailored briefings. Every session shows `[Cortex] N memories · miner OK · assembly degraded (claude CLI missing — FTS-only recall)` at the top of the context, so you know why.

This is by design: missing optional dependencies should degrade, not fail.

## How do I diagnose problems?

Run `/cortex-doctor`. It runs the same preflight the bootstrap shim runs (Python version, `mcp` importable, `claude` CLI on PATH, directory writability, `uv` available), then prints a report labelled **HEALTHY**, **DEGRADED**, or **FAILING** with explicit fix instructions for each blocker.

For deeper debugging:

```bash
tail -f ~/.cortex/bootstrap.log   # first-run shim log
tail -f ~/.cortex/miner.log       # miner daemon log
cat ~/.cortex/events.jsonl        # memory operation audit trail
python3 -m cortex_server.server --status   # detailed status dump
```

## How does the mining pipeline work?

```
Session ends → miner daemon sees the JSONL file in ~/.claude/projects/
  → Waits 5 minutes for the file to "settle" (no more writes)
  → Filters to human messages + assistant prose (strips tool calls, system reminders)
  → One Haiku call with the full context: "extract durable lessons"
  → Haiku returns JSON array of memory candidates
  → Each candidate runs: security scan → dedup check → contradiction detection → save
  → Index rebuilt, per-project playbooks grown and refined
  → Session marked COMPLETE in ~/.cortex/.mined_sessions
```

## How does the recall pipeline work?

```
First message in a new session → auto-recall.sh hook fires
  → Reads ~/.cortex/.capabilities for status banner
  → If claude CLI is available → sends (message, memories) to Haiku
      → Haiku synthesizes a focused briefing (300-800 tokens usually)
      → Brief injected into Claude's context as "Cortex context briefing"
  → If claude CLI is missing → falls back to FTS-only keyword recall
  → Either way, Claude starts its reply with relevant context already loaded
```

## Architecture

Cortex is split into small, focused modules:

- `models.py` — data types, path constants
- `security.py` — prompt injection + credential exfil scanning
- `telemetry.py` — access tracking, event log (atomic writes, fcntl-locked)
- `search_index.py` — SQLite FTS5 index
- `obsidian_store.py` — memory I/O, dedup scoring, contradiction detection
- `playbook.py` — per-project playbook grow + refine
- `assembly.py` — context assembly via Haiku
- `capabilities.py` — runtime feature detection for degraded mode
- `storage.py` — server-lifecycle helpers (PID management, miner auto-start)
- `server.py` — thin MCP entrypoint (FastMCP imported lazily)
- `cli.py` — command dispatcher for non-MCP entrypoints
- `mining.py` — session mining pipeline

**Multi-signal recall scoring:**
- 50% FTS relevance
- 15% recency (0.995^hours decay)
- 15% access history (usage reinforcement)
- 20% importance (1-5 scale from Haiku)

**Memory schema** (markdown frontmatter):
```yaml
---
id: uuid
schema_version: 1
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

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `CORTEX_DIR` | `~/.cortex` | State directory (PID files, search DB, logs) |
| `CORTEX_OBSIDIAN_VAULT` | `~/obsidian-brain` | Vault location |
| `CORTEX_EXTRA_SESSION_DIRS` | (none) | Colon-separated extra session dirs to mine |
| `CORTEX_MINER_SETTLE_SECONDS` | `300` | Seconds to wait before mining a completed session |
| `CORTEX_SKIP_SYNC` | `0` | Bootstrap skips `uv sync` when set to `1` (dev only) |

## Setup Obsidian (optional, recommended)

Cortex works without Obsidian — it just writes markdown. But Obsidian gives you graph view and backlinks for free:

1. Download: https://obsidian.md (free)
2. Open `~/obsidian-brain` as a vault
3. Memories appear in `cortex/memories/`, playbooks in `cortex/playbooks/`
4. Use Graph View to see how memories link via the `related` field

## Requirements

- Claude Code
- Python ≥ 3.11
- `uv` (auto-installed by bootstrap.sh on first run)
- `claude` CLI on PATH (optional — required for Haiku-powered assembly; degraded mode works without it)

## Development

```bash
git clone https://github.com/TT-Wang/cortex-plugin.git
cd cortex-plugin
pip install -e ".[dev]"
pytest             # 54 tests
ruff check .       # lint
mypy cortex_server # type check (strict)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR process and [CHANGELOG.md](CHANGELOG.md) for version history.

## License

MIT
