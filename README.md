# memem

**Persistent, self-evolving memory for Claude Code.** Stop re-explaining your project every session.

<!--
The Glama badge URL below intentionally uses the legacy `cortex-plugin`
slug. Glama listing slugs are fixed-once-created and the project was
renamed cortex → memem in v0.7.0. The badge keeps rendering A-tier under
the old slug via GitHub's repo-rename redirect. Re-listing under the
new slug requires manual coordination via https://glama.ai/discord.
DO NOT "fix" this URL — `glama.ai/mcp/servers/TT-Wang/memem` returns 404.
-->
[![CI](https://github.com/TT-Wang/memem/actions/workflows/ci.yml/badge.svg)](https://github.com/TT-Wang/memem/actions/workflows/ci.yml) [![memem MCP server](https://glama.ai/mcp/servers/TT-Wang/cortex-plugin/badges/score.svg?v=4)](https://glama.ai/mcp/servers/TT-Wang/cortex-plugin) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> For LLM/AI tool discovery, see [llms.txt](./llms.txt).

```
  ███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗
  ████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║
  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║
  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║
  ██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║
  ╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝
  persistent memory for Claude Code
```

## What is memem?

memem is a Claude Code plugin that gives Claude persistent memory across sessions. A background miner extracts durable lessons (decisions, conventions, bug fixes, preferences) from your completed sessions, stores them as markdown in an Obsidian vault, and automatically surfaces relevant ones at the start of each new session via a query-tailored briefing.

It's **local-first**: no cloud services, no API keys required, no vendor lock-in. Everything lives in `~/obsidian-brain/memem/memories/` as human-readable markdown.

## When should I use memem?

Use memem if:
- You use Claude Code daily and keep re-explaining your project to every new session
- You want durable memory you can browse and edit as markdown
- You like local-first tools with zero vendor lock-in
- You already use Obsidian (memem plugs straight into your vault)

## How is memem different from CLAUDE.md?

`CLAUDE.md` is a single hand-edited file per project. memem gives you:

- **Automatic extraction** — no manual note-taking, the miner captures lessons from every completed session
- **Query-aware context** — only the memories relevant to your current question get injected, not a static dump
- **Self-evolving** — memories merge, update, and deprecate automatically as your project evolves
- **Cross-project** — works across every Claude Code project you use, not scoped to one repo
- **Security scanning** — every write is scanned for prompt injection and credential exfiltration
- **Browsable** — Obsidian vault with graph view and backlinks for free

## Architecture — layered recall

memem uses a 3-tier recall workflow inspired by [claude-mem](https://github.com/thedotmack/claude-mem) and [mem0](https://mem0.ai). Instead of dumping full memory content on every recall, it lets Claude progressively drill into relevant memories:

```
   Session start
   ┌─────────────────────────────┐
   │ SessionStart hook injects:  │
   │   • L0 full content (~20)   │
   │   • Compact index of rest   │
   └──────────┬──────────────────┘
              │
              ▼
   During session
   ┌─────────────────────────────┐
   │ 1. memory_search(query)     │ → compact index (50 tok/result)
   │ 2. memory_get(ids=[...])    │ → full content (500 tok/result)
   │ 3. memory_timeline(id)      │ → chronological thread
   └─────────────────────────────┘
              │
              ▼
   ┌─────────────────────────────┐
   │ UserPromptSubmit hook       │ → always runs active slice
   │ (active_slice engine)       │    and refreshes working state
   └─────────────────────────────┘
```

**Memory layers (auto-classified at mining time):**

| Layer | Purpose | Injection |
|-------|---------|-----------|
| **L0** | Project identity — tech stack, repo structure, core conventions | Full content at session start |
| **L1** | Generic conventions — testing, style, commit patterns | Compact index at session start |
| **L2** | Domain-specific — most memories (default) | Compact index at session start |
| **L3** | Rare/archival — niche failure modes, one-off lessons | Compact index at session start |

Token efficiency: session start injects ~50 tokens per L1-L3 memory (ID + title + snippet) instead of full content. Claude drills into specific memories via `memory_get(ids=[...])` only when it needs full detail.

**Active Memory Slice runtime kernel:**

For ongoing work, `active_memory_slice(query, scope_id)` sits above recall. It
uses `memory_search`/FTS/graph/playbooks/transcripts as candidate generation,
then activates a structured working state:

```text
Memory Vault
→ Candidate Generation
→ Activation Judgement
→ Active Memory Slice
→ Delta Proposals
→ Memory Vault
```

The slice explicitly separates goals, constraints, background, decisions,
preferences, failure patterns, artifacts, open tensions, and candidate deltas.
Delta proposals are advisory in v1 and are not auto-committed to the vault.

Opt-in features:
- **`MEMEM_SHOW_BANNER=1`** — show a one-line status banner at session start (off by default)
- **`MEMEM_PRETOOL_GATING=1`** — enrich Read tool calls with memories about the target file (off by default)
- **`MEMEM_TOPIC_SHIFT_THRESHOLD=0.3`** — keyword overlap threshold for topic-shift re-firing (default 0.3)

## How do I install memem?

Copy-paste:

```bash
claude plugin marketplace add TT-Wang/memem
claude plugin install memem@memem-marketplace
```

If you already added the marketplace once, future installs only need the second command.

Then:

1. restart Claude Code if it was already open
2. open any project
3. send your first normal message
4. memem will show a welcome/status message and offer the mining options

That's it. On first run, `bootstrap.sh` self-heals everything:

1. Verifies Python ≥ 3.11 — or installs it via `uv python install 3.11` if your system Python is too old
2. Installs `uv` if missing (via the official Astral installer)
3. Syncs deps into a plugin-local `.venv` (hash-cached against `uv.lock`)
4. Creates and canary-tests `~/.memem/` and `~/obsidian-brain/`
5. Writes `~/.memem/.capabilities` (used for degraded-mode decisions)
6. Execs the real MCP server

**First run:** ~5 seconds. **Every run after:** ~100ms. No separate `pip install` step.

**Nothing mines until you opt in.** memem is strictly opt-in as of v0.9.0 — install does not start the miner or touch your sessions. Type `/memem` to see status and choose what to do next. You can start mining two ways:

- `/memem-mine` — mine **new sessions only** (from now on)
- `/memem-mine-history` — mine **everything, including past history** (uses Haiku API credits)

Or just tell Claude "start mining new sessions" / "start mining everything including history" — it knows what to do.

### Recommended first-run choice

- choose **`/memem-mine`** if you only want memory from new sessions going forward
- choose **`/memem-mine-history`** if you want memem to process your old Claude Code sessions too

If you are unsure, start with **`/memem-mine`**. It is the safer and cheaper default.

## What happens on my first Claude Code session?

You type your first message. The UserPromptSubmit hook fires and sees zero memories (you just installed it), so it injects a welcome banner into Claude's context. Claude reads the banner, tells you memem is active, and — if you have pre-existing Claude Code sessions — offers to mine them via `/memem-mine-history`.

You work normally. The miner daemon runs silently in the background. When your session ends and settles for 5 minutes, the miner extracts memories from the transcript using Claude Haiku and writes them to your vault.

**During the session:** every user prompt goes through `active_memory_slice`, which builds a structured working-state briefing from the relevant memories, playbooks, transcripts, and graph neighbors. You see an active slice prompt with goals, constraints, background, decisions, failure patterns, open tensions, and artifacts. Claude starts with the current working state instead of a generic briefing.

## 30-Second Setup

```bash
claude plugin marketplace add TT-Wang/memem
claude plugin install memem@memem-marketplace
```

Then in Claude Code:

```text
/memem
```

And choose one:

```text
/memem-mine
```

or

```text
/memem-mine-history
```

## What does memem save?

It saves durable knowledge, not session logs:

- **Architecture decisions** with rationale ("we use RS256 JWTs because HS256 can't be verified by third parties without sharing the secret")
- **Conventions** ("tests go in `tests/` not `spec/`", "commit messages use imperative mood", "never import from `internal/` outside its package")
- **Bug fixes you might forget** ("`bcrypt.compare` is async — must `await`", "timezone math must use `dayjs.utc()` or DST shifts the result by an hour")
- **User preferences** ("prefer single commits, not stacked PRs", "terse responses — no trailing summaries", "ask before running migrations in prod")
- **Known issues & workarounds** ("`JWT_SECRET` defaults to `'secret'` if unset — tracked in #123", "pnpm install hangs on corporate VPN, use `--network-timeout=600000`")
- **Environment & tooling facts** ("project uses Poetry, not pip", "CI runs on Node 20 but local defaults to 22 — pin with `nvm use`", "Redis must be running on :6380 not :6379")
- **Project structure & invariants** ("auth middleware requires Redis", "all DB writes go through `repo/` layer, never raw SQL in handlers")
- **Failure patterns & post-mortems** ("mocking the DB hid a broken migration last quarter — integration tests must hit a real DB", "don't ship on Fridays after the 2025-11 rollback incident")
- **Third-party quirks** ("Stripe webhooks retry for 3 days — idempotency key is mandatory", "OpenAI streaming drops the final token if client closes early")
- **Domain knowledge** ("a 'merchant' in our schema is what the legal team calls a 'counterparty'", "revenue is recognized at ship time, not order time")

It does NOT save:

- Raw session transcripts (those are searchable via `transcript_search`, not stored as memories)
- Trivial or obvious facts
- Session outcomes ("today I worked on X")

## Where does memem store my memories?

| Store | Path | Purpose |
|-------|------|---------|
| Memories | `~/obsidian-brain/memem/memories/*.md` | Source of truth (human-readable markdown) |
| Playbooks | `~/obsidian-brain/memem/playbooks/*.md` | Per-project curated briefings |
| Search DB | `~/.memem/search.db` | SQLite FTS5 index (machine-fast lookup) |
| Graph DB | `~/.memem/graph.db` | Rebuildable typed/scored memory-edge index |
| Telemetry | `~/.memem/telemetry.json` | Access tracking (atomic writes) |
| Event log | `~/.memem/events.jsonl` | Append-only audit trail |
| Capabilities | `~/.memem/.capabilities` | Degraded-mode flags written by bootstrap |
| Bootstrap log | `~/.memem/bootstrap.log` | First-run diagnostics |

You can point memem elsewhere via `MEMEM_DIR` and `MEMEM_OBSIDIAN_VAULT` env vars.

## What are the MCP tools Claude can call?

| Tool | What it does |
|------|------|
| `memory_save(content, title, tags)` | Store a lesson. Security-scanned for prompt injection and credential exfil before writing. |
| `memory_recall(query, limit)` | Search memories. FTS5 + temporal decay + access reinforcement + importance weighting. |
| `memory_list(scope_id)` | List all memories with stats, grouped by project. |
| `memory_import(source_path)` | Bulk import from files, directories, or chat exports. |
| `transcript_search(query)` | Search raw Claude Code session JSONL logs (not the mined memories). |
| `context_assemble(query, project)` | On-demand query-tailored briefing from playbooks + memories + transcripts. |
| `memory_graph(memory_id)` | Inspect typed/scored graph neighbors for one memory. |
| `memory_graph_audit()` | Report graph quality issues: orphans, dead links, hubs, stale edges. |
| `memory_graph_rebuild(scope_id)` | Rebuild the graph side index from the Obsidian vault. |
| `active_memory_slice(query, scope_id)` | Build a structured runtime working state for current work. |

## What slash commands does memem add?

- `/memem` — welcome, status, help
- `/memem-status` — memory count, projects, search DB size, miner health
- `/memem-doctor` — preflight health check with fix instructions for any blocker
- `/memem-mine` — start the miner daemon manually (normally auto-starts)
- `/memem-mine-history` — opt-in: mine all your pre-install Claude Code sessions

## What if the `claude` CLI isn't on my PATH?

memem enters **degraded mode** — it still works, just without Haiku-powered context assembly and smart recall. You get FTS-only keyword recall instead of query-tailored briefings. Every session shows `[memem] N memories · miner OK · assembly degraded (claude CLI missing — FTS-only recall)` at the top of the context, so you know why.

This is by design: missing optional dependencies should degrade, not fail.

## How do I diagnose problems?

Run `/memem-doctor`. It runs the same preflight the bootstrap shim runs (Python version, `mcp` importable, `claude` CLI on PATH, directory writability, `uv` available), then prints a report labelled **HEALTHY**, **DEGRADED**, or **FAILING** with explicit fix instructions for each blocker.

For deeper debugging:

```bash
tail -f ~/.memem/bootstrap.log   # first-run shim log
tail -f ~/.memem/miner.log       # miner daemon log
cat ~/.memem/events.jsonl        # memory operation audit trail
bash "${CLAUDE_PLUGIN_ROOT:-.}/bootstrap.sh" --status   # detailed status dump
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
  → Session marked COMPLETE in ~/.memem/.mined_sessions
```

## How does the recall pipeline work?

```
First message in a new session → auto-recall.sh hook fires
  → Reads ~/.memem/.capabilities for status banner
  → Builds an active memory slice from recall candidates + graph/playbook/transcript context
  → Emits a structured "Active Memory Slice" prompt block
  → If the slice engine is unavailable → falls back to compact recall
  → Either way, Claude starts its reply with active work-state context already loaded
```

## Architecture

memem is split into small, focused modules:

- `models.py` — data types, path constants
- `security.py` — prompt injection + credential exfil scanning
- `telemetry.py` — access tracking, event log (atomic writes, fcntl-locked)
- `search_index.py` — SQLite FTS5 index
- `graph_index.py` — typed/scored related-memory graph side index
- `active_slice.py` — active slice schemas and markdown projection
- `activation.py` — heuristic + bounded LLM activation judgement
- `boundaries.py` — scope/deprecated/budget/redundancy filters
- `delta.py` — non-mutating candidate delta proposals
- `active_slice_engine.py` — candidate generation and slice orchestration
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

**Related-memory graph:**

The Obsidian markdown files remain the source of truth. The `related: [...]`
frontmatter stays intentionally simple so memories are portable and readable.
memem also builds `~/.memem/graph.db`, a local SQLite side index with typed,
scored edges such as `same_topic`, `supports`, `depends_on`, `supersedes`, and
`contradicts`. Recall uses this graph when available and falls back to the
Markdown `related` field if the graph has not been built yet.

Useful maintenance commands:

```bash
memem graph rebuild
memem graph audit
memem graph stats
memem graph neighbors <memory-id>
```

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
| `MEMEM_DIR` | `~/.memem` | State directory (PID files, search DB, logs) |
| `MEMEM_OBSIDIAN_VAULT` | `~/obsidian-brain` | Vault location |
| `MEMEM_EXTRA_SESSION_DIRS` | (none) | Colon-separated extra session dirs to mine |
| `MEMEM_MINER_SETTLE_SECONDS` | `300` | Seconds to wait before mining a completed session |
| `MEMEM_SKIP_SYNC` | `0` | Bootstrap skips `uv sync` when set to `1` (dev only) |

## Setup Obsidian (optional, recommended)

memem works without Obsidian — it just writes markdown. But Obsidian gives you graph view and backlinks for free:

1. Download: https://obsidian.md (free)
2. Open `~/obsidian-brain` as a vault
3. Memories appear in `memem/memories/`, playbooks in `memem/playbooks/`
4. Use Graph View to see how memories link via the `related` field

## Requirements

- Claude Code
- Python ≥ 3.11
- `uv` (auto-installed by bootstrap.sh on first run)
- `claude` CLI on PATH (optional — required for Haiku-powered assembly; degraded mode works without it)

## Development

```bash
git clone https://github.com/TT-Wang/memem.git
cd memem
pip install -e ".[dev]"
pytest             # 54 tests
ruff check .       # lint
mypy memem # type check (strict)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR process and [CHANGELOG.md](CHANGELOG.md) for version history.

## Works great with

- **[forge](https://github.com/TT-Wang/forge)** — Structured planning,
  parallel execution, and deep validation for Claude Code. memem + forge
  is the recommended pairing: forge plans and executes multi-file
  changes, memem remembers what worked across runs. Forge's
  `memory_save` patterns land in memem's recall index, so next week's
  run starts with last week's lessons already loaded.

## License

MIT
