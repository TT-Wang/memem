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

memem is a Claude Code plugin that gives Claude persistent memory across sessions. An event-triggered miner (Stop-hook → detached `mine_delta` subprocess) extracts durable lessons (decisions, conventions, bug fixes, preferences) from each new conversation turn, stores them as markdown in an Obsidian vault, and automatically surfaces relevant ones as an Active Memory Slice working state. An explicit narrative assembly path still exists, but the default runtime context is slice-first.

It's **local-first**: no cloud services, no API keys required, no vendor lock-in. Everything lives in `~/obsidian-brain/memem/memories/` as human-readable markdown.

### What's new in v2.3.0 (hybrid retrieval)

`active_memory_slice` now uses a two-stage hybrid retrieval pipeline: BM25 + cosine Reciprocal Rank Fusion (RRF) builds a top-20 candidate pool, then Maximal Marginal Relevance (MMR, λ=0.7) selects the final 8 results to suppress near-duplicate memories. Access writeback is on by default (`MEMEM_WRITEBACK_ENABLED=1`); each recall fires a daemon thread that increments `access_count` in a JSON sidecar at `~/.memem/telemetry.json` (NOT in memory frontmatter — deliberate, to keep `load_vault_index`'s mtime cache stable). Net benchmark result: **75.3% precision** (+1.3 pp vs v2.0.0 baseline), 133ms warm latency. Recency decay scoring was prototyped but reverted due to a negative-cosine ranking regression — see CHANGELOG for details.

### What's new in v2.2.0 (episodic seeds)

Two architectural additions targeting the episodic-query gap vs everme. (a) `retrieve.py` parses temporal phrases in queries ("yesterday" / "last week" / "N days ago") and re-ranks candidates by `created:` proximity (+0.2 boost). Zero behavior change for non-temporal queries. (b) `mine_delta.py` emits one per-session "episode" memory after substantive Stop events (tagged `type:episodic`, Haiku-generated 100-word narrative). Benchmark is unchanged at 74% in this release — the gains are forward-looking and accrue as the vault accumulates v2.2.0-shaped episodes. Backward-compat is 100%.

### What's new in v2.1.0 (event-triggered mining)

The miner daemon is gone. `miner_daemon.py`, `miner-wrapper.sh`, `miner_circuit_breaker.py`, `miner_errors.py`, and `miner_protocol.py` (~1,500 LOC) have been deleted. Mining now triggers on every Claude Code Stop event via a detached subprocess.

- **Stop hook** (`hooks/stop-mine.sh`) spawns `mine_delta` as a detached background process on every `Stop` event. Hook overhead is ~50ms; extraction happens in background after the hook returns.
- **`memem/mine_delta.py`** — new module (~200 LOC): reads the JSONL session file from a byte offset tracked per session, filters to new turns since the last invocation, calls the same Haiku `extract_from_text` function, and marks the session in `~/.memem/.mined_sessions`.
- **Stale-session sweep** — the `SessionStart` hook now scans for JSONL files older than 10 min that aren't in `.mined_sessions` and spawns up to 3 parallel `mine_delta` processes. Catches sessions where Stop never fired (Claude crash, `kill -9`, network drop).
- **Per-session flock** — `mine_delta` acquires an `fcntl.flock` on a lock file per session so concurrent Stop events on the same session don't race.
- **Adaptive empty-streak backoff** — if the last 3 consecutive Stop events yielded zero memories, the next 5 Haiku calls are skipped. Resets on any non-empty result.
- **Token cost** is ~5–20× higher per session vs v2.0.0's session-end batching (many small Haiku calls instead of one big one), but mining feels real-time — memories appear seconds after each conversation turn.
- **Extraction quality unchanged** — the same Haiku prompt and `extract_from_text` function from `mining.py` are used. The 18-query benchmark still passes at ≥70% precision.

### What's new in v2.0.0 ("less is more")

BREAKING — schema rebuild from 18 sections → 2 (Working + Relevant). Retrieval pipeline rewritten from ~12,400 LOC to ~210 LOC (POC v3b architecture). Net delete: **87 files, +915 / -19,941 LOC**.

- **NEW `memem/retrieve.py` (~145 LOC) + `memem/render.py` (~65 LOC)** — query → embed → cosine top-K + FTS-conditional supplement for version/date literals, then a 2-section renderer. Pure embedding similarity, no scope filter, no kind classifier, no LLM judge, no daemon.
- **Slice schema collapsed to 2 sections**: `## Working` (current state) + `## Relevant` (ranked list). The v1.13 schema (Anchors / Episodic / Skills / Cases / Working / Pending) is gone.
- **`active_memory_slice` MCP tool slimmed** from 8 params to 2 (`query`, `task_mode`). Backward-incompatible.
- **Deleted (~14,500 LOC)**: 15 memem modules (active_slice*, activation, candidate_generation, kind_classifier, slice_daemon, slice_client, slice_history, delta*, working_memory, boundaries, artifact_context, environment_context), 36 legacy test files, all v1.13 env-var flags (`MEMEM_USE_LLM_JUDGE`, `MEMEM_USE_EMBEDDINGS`, `MEMEM_RENDER_LEGACY`, `MEMEM_LLM_JUDGE_TIMEOUT`, `MEMEM_AUTO_SLICE_DAEMON` — all no-op now).
- **Preserved**: all 14 MCP tools (same names + return shapes), all 7 CLI flags, mining pipeline, vault format, embedding model + cache.
- **Benchmark (18 queries × 6 categories)**: 74% precision (vs v1.13's 24% — 3× improvement) | 98ms warm latency (vs v1.13's 675ms — 6× faster) | 24/8 cross-scope hits (lexie/SSH/HFT queries that v1.13 returned 0 results for).
- **Daemon retired**: `slice_daemon` and `MEMEM_AUTO_SLICE_DAEMON` removed. Retrieval is now in-process via `memem.retrieve`; the hook spawns python directly per prompt. After upgrade run `pkill -f slice_daemon` once to clear any old process.
- **Hook envelope** now uses tempfile (avoids ARG_MAX on large prompts).
- **Embedding writes are atomic**: `embeddings.npy` via tmpfile + `os.replace`, `embedding_ids.json` written first so readers never see torn-write or shape mismatch.

### What's new in v1.9.4 (data correctness pass)

Two release pair (v1.9.3 + v1.9.4) targeting silent-corruption paths. All changes are no-ops on the happy path.

- **Atomic writes everywhere** — shared `atomic_write_text` helper (tempfile + fsync + `os.replace`) applied to 5 previously non-atomic data paths (embedding ID map, tournament cache, lesson frontmatter, dreamer output, mined-sessions reset). `MEMEM_FSYNC=0` opts out per-call. Power-loss / NFS-jitter / SIGKILL no longer torn-writes vault data.
- **WAL on every SQLite DB** — `graph.db` and `search.db` now use `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`, matching `session_state_db.py` since v1.6. Concurrent reads from the slice engine no longer race with miner writes. New `memem --integrity-check` CLI command (also called from `--doctor`) runs `PRAGMA integrity_check` on all three DBs.
- **Strict frontmatter validation** — files without `---` frontmatter are no longer silently ingested with `schema_version=0`. New `MEMEM_FRONTMATTER_STRICT` env var: `quarantine` (default — move to `~/.memem/quarantine/<hash>_<name>`), `skip` (log + ignore), or `raise`.
- **Writeback idempotency cache** — `commit_deltas` hashes `(scope_id, dry_run, auto_only, deltas, DELTA_WRITEBACK_VERSION)` on entry; matching hits return cached result with `deduped: True` markers. Cache at `~/.memem/writeback-idempotency.json`. Dry-runs and partial-failure batches are not cached. `force_writeback=True` bypasses the lookup. RMW guarded by `fcntl.flock`.
- **Daemon-side subprocess-timeout accounting** (v1.9.2) — fixed an infinite-loop where a huge JSONL session would re-queue forever because the daemon's SIGKILL preempted `mine_session`'s in-process timeout cap. Now the daemon itself increments `timeout_failures` and permanently skips after `MEMEM_MAX_SESSION_TIMEOUTS` (default 3).

### What's new in v1.9 (smart injection gating)

Four layered gating heuristics between the `UserPromptSubmit` hook and the active-slice engine, plus a new `MEMEM_INJECTION_MODE` env (`auto` / `hybrid` / `tool`). Hybrid mode reduces hook overhead on trivial turns via: (1) trivial-query regex EN+ZH, (2) per-session turn cadence (`MEMEM_INJECT_CADENCE`, default 2), (3) empty-streak exponential backoff (`MEMEM_EMPTY_STREAK_MAX`, default 8), (4) topic-shift cosine via cached query embedding (`MEMEM_TOPIC_SHIFT_THRESHOLD`, default 0.85). Persistent slice daemon since v1.8 eliminates cold-start cost. See `CLAUDE.md` for the full tunables table.

### What's new in v1.1

- **Layered memory becomes real end-to-end.** Every memory now lives in one of four layers (L0/L1/L2/L3) at save time, not just at mining time. `memory_save` accepts an optional `layer` param (Claude can override) and auto-classifies otherwise. The slice engine pins L0 (project identity) on every prompt and gates L3 (rare archival) behind explicit search.
- **Slice as universal recall format.** `memory_search`, `memory_get`, `memory_timeline`, `memory_recall`, and `context_assemble` all return slice-formatted output via a single `render_slice_markdown` dispatcher. `context_assemble` composes via `active_memory_slice` rather than rolling its own briefing.

### What's new in v1.0 (miner hardening)

A 16-module refactor closed the entire spawn-storm class of bugs that had previously taken down hosts. The miner now uses `start_new_session=True` + `os.killpg` for process-group cleanup on timeout, an inverted `TransientError`/`PermanentError` taxonomy with `PermanentError` as default, persisted attempt counters with DLQ at MAX_FAILURES, a SIGTERM-drained graceful shutdown, SQLite WAL state storage, a hand-rolled circuit breaker, structured JSON logs with `RotatingFileHandler`, and a 5-in-60s wrapper crash guard.

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

## Architecture — slice-first runtime

memem uses layered recall plus a slice-first runtime kernel inspired by [claude-mem](https://github.com/thedotmack/claude-mem) and [mem0](https://mem0.ai). Instead of treating memory as one big briefing, it first turns recall results into an explicit working state:

```
   Session start / user prompt
   ┌─────────────────────────────┐
   │ Candidate generation        │
   │   • memories / graph        │
   │   • playbooks               │
   │   • runtime environment     │
   │   • current artifacts       │
   └──────────┬──────────────────┘
              │
              ▼
   ┌─────────────────────────────┐
   │ Activation judgement        │
   │   • goals                   │
   │   • constraints             │
   │   • decisions / failures    │
   │   • artifacts / tensions    │
   └─────────────────────────────┘
              │
              ▼
   ┌─────────────────────────────┐
   │ Active Memory Slice         │ → rendered markdown working state
   │ generate_prompt_context()   │    used by hooks, MCP, and CLI
   └─────────────────────────────┘
```

The lower-level recall tools still exist for explicit drilling:

1. `memory_search(query)` -> compact index
2. `memory_get(ids=[...])` -> full content
3. `memory_timeline(id)` -> chronological thread
4. `context_assemble(query, project)` -> optional secondary narrative briefing

**Memory layers (auto-classified at save AND mining time; Claude can override):**

| Layer | Purpose | Slice behavior |
|-------|---------|-----------|
| **L0** | Project identity — tech stack, repo structure, core conventions | **Always pinned** in every active slice for that project (anchor score 0.95) |
| **L1** | Generic conventions — testing, style, commit patterns | Ranked + scored alongside L2 |
| **L2** | Domain-specific — most memories (default) | Ranked + scored (default search hits) |
| **L3** | Rare/archival — niche failure modes, one-off lessons | **Excluded from auto-recall**; only via explicit `memory_search`/`memory_get` |

A heuristic (`mining.py:classify_layer`) assigns layers based on importance, structural tags, content length, and the per-project L0 cap. `memory_save(content, ..., layer=N)` accepts an explicit override (0-3) when Claude judges better than the heuristic.

Token efficiency: session start injects L0 verbatim plus a compact index for L1-L2 (~50 tokens per entry: ID + L<n> + title + snippet). Claude drills into specific memories via `memory_get(ids=[...])` only when it needs full detail.

**Active Memory Slice runtime kernel:**

For ongoing work, `active_memory_slice(query, task_mode?)` is the default
runtime path. It uses `memory_search`/FTS/graph/playbooks/transcripts plus
runtime environment and current artifacts as candidate generation, then
activates a structured working state:

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
If you pass `session_id` together with runtime context such as `task_mode` and
`repo_path`, memem also carries forward continuity across slices and records
slice history under `~/.memem/`.

Default runtime behavior is still non-mutating. Delta proposals are validated
and surfaced in the slice, but safe writeback only runs when you opt in via
`writeback_preview=True` or `auto_commit_safe=True`.

Opt-in features:
- **`MEMEM_SHOW_BANNER=1`** — show a one-line status banner at session start (off by default)
- **`MEMEM_PRETOOL_GATING=1`** — enrich Read tool calls with memories about the target file (off by default)

Hybrid retrieval gating (v1.9+) — opt in to reduce hook overhead on trivial turns:
- **`MEMEM_INJECTION_MODE`** — `auto` (default — no gating, current behavior), `hybrid` (apply gating heuristics), `tool` (silence hook, MCP only)
- **`MEMEM_INJECT_CADENCE=2`** — when `hybrid` is on, run full slice every Nth turn
- **`MEMEM_TOPIC_SHIFT_THRESHOLD=0.85`** — cosine-similarity threshold for reusing the previous turn's slice (`hybrid` only)
- **`MEMEM_EMPTY_STREAK_MAX=8`** — cap on the exponential backoff after consecutive empty slices

Selective recall (v1.9.6+) — suppress context injection when the slice is low-confidence or out-of-vault:
- **`MEMEM_RECALL_MIN_CONFIDENCE=0.45`** — minimum activation confidence required to emit context. Below this, the hook emits a "0 items (low confidence)" systemMessage and suppresses `additionalContext`.
- **`MEMEM_RECALL_MIN_ITEM_SCORE=0.0`** — per-item composite-score floor for recall results (0.0 = disabled). L0 project-identity anchors are always exempt.
- **`MEMEM_RECALL_OOV_THRESHOLD=0.0`** — out-of-vault detection threshold (0.0 = disabled). When set (e.g. 0.3), queries with no L0 keyword overlap and all candidate scores below threshold emit "0 items (out of vault)" and suppress context. Env-var changes take effect on the next hook invocation (no daemon to restart in v2.1.0+).

Recommended for high-frequency sessions: `export MEMEM_INJECTION_MODE=hybrid`

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

At session start, the SessionStart hook tries to prime a slice-first working state for the current project scope. On each user prompt, the UserPromptSubmit hook regenerates the slice for the current query. If you just installed memem and have no relevant context yet, the hooks stay quiet and Claude proceeds normally.

You work normally. When each conversation turn completes, the Stop hook spawns `mine_delta` in the background to extract memories from the new turns using Claude Haiku and write them to your vault. No daemon, no 5-minute wait — memories appear seconds after each turn.

**During the session:** every user prompt goes through `active_memory_slice`, which builds a structured working-state briefing from the relevant memories, playbooks, transcripts, graph neighbors, environment facts, and current artifacts. The hooks automatically pass session id and working directory, and the prompt hook infers a task mode when the host does not provide one, so ongoing work can carry constraints, artifacts, and tensions forward across slices. You see an active slice prompt with goals, constraints, background, decisions, failure patterns, open tensions, and artifacts. Claude starts with the current working state instead of a generic briefing.

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

All recall tools return **slice-formatted markdown** via a unified `render_slice_markdown` dispatcher (introduced in v1.1) so output structure is consistent across tools.

| Tool | What it does |
|------|------|
| `memory_save(content, title, tags, layer?)` | Store a lesson. Security-scanned for prompt injection and credential exfil before writing. `layer` is optional (0-3); auto-classifies via `classify_layer` if omitted. |
| `memory_search(query, limit, scope_id)` | **[L1]** Compact index slice — IDs + layer + title + 1-line snippet. Use first to narrow candidates. |
| `memory_get(ids, scope_id)` | **[L2]** Full content slice for specific memory IDs. Use after `memory_search`. |
| `memory_timeline(memory_id, scope_id)` | **[L3]** Chronological thread via `related[]` graph + same-project window. |
| `memory_recall(query, scope_id, limit)` | Legacy alias — search + full content in one slice. |
| `memory_list(scope_id)` | List all memories with stats, grouped by project. |
| `memory_import(source_path)` | Bulk import from files, directories, or chat exports. |
| `transcript_search(query)` | Search raw Claude Code session JSONL logs (not the mined memories). |
| `context_assemble(query, project)` | Composite briefing: calls `active_memory_slice` 1-2 times (project + general scope when sparse), merges into one assembled slice. |
| `memory_graph(memory_id)` | Inspect typed/scored graph neighbors for one memory. |
| `memory_graph_audit()` | Report graph quality issues: orphans, dead links, hubs, stale edges. |
| `memory_graph_rebuild(scope_id)` | Rebuild the graph side index from the Obsidian vault. |
| `active_memory_slice(query, task_mode?)` | v2.3.0: thin wrapper over `retrieve()` + `render_slice()`. Returns markdown with `## Working` + `## Relevant`. Retrieval uses BM25 + cosine RRF fusion (top-20 pool) followed by MMR diversification (λ=0.7) to select the final 8 results. FTS supplement retained for version/date literals. L0 project-identity memories are exempt from MMR diversity penalty. |

## How do I inspect slices or writeback manually?

Use the CLI when you want raw slice JSON, continuity debugging, or explicit
writeback preview:

```bash
python3 -m memem.server slice "continue auth rollout" --scope memem --session-id sess-42 --cwd "$PWD" --task-mode coding --json --no-llm
python3 -m memem.server slice "continue auth rollout" --scope memem --session-id sess-42 --cwd "$PWD" --task-mode coding --writeback-preview --json --no-llm
python3 -m memem.server slice "continue auth rollout" --scope memem --session-id sess-42 --cwd "$PWD" --task-mode coding --auto-commit-safe --json --no-llm
```

Semantics:
- default `slice` is read-side and non-mutating
- `--writeback-preview` runs the delta pipeline in dry-run mode
- `--auto-commit-safe` commits only deltas classified as auto-safe

## What slash commands does memem add?

- `/memem` — welcome, status, help
- `/memem-status` — memory count, projects, search DB size, miner health
- `/memem-doctor` — preflight health check with fix instructions for any blocker
- `/memem-mine` — opt in to event-triggered mining (touches `~/.memem/.miner-opted-in`; new sessions mined automatically via the Stop hook)
- `/memem-mine-history` — opt-in + backfill all pre-install Claude Code sessions

## What if the `claude` CLI isn't on my PATH?

memem enters **degraded mode** — it still works, just without Haiku-powered context assembly and smart recall. You get FTS-only keyword recall instead of query-tailored briefings. Every session shows `[memem] N memories · miner OK · assembly degraded (claude CLI missing — FTS-only recall)` at the top of the context, so you know why.

This is by design: missing optional dependencies should degrade, not fail.

## How do I diagnose problems?

Run `/memem-doctor`. It runs the same preflight the bootstrap shim runs (Python version, `mcp` importable, `claude` CLI on PATH, directory writability, `uv` available) **plus a SQLite integrity check on all three WAL DBs** (v1.9.3+), then prints a report labelled **HEALTHY**, **DEGRADED**, or **FAILING** with explicit fix instructions for each blocker.

For deeper debugging:

```bash
tail -f ~/.memem/bootstrap.log              # first-run shim log
cat ~/.memem/events.jsonl                   # memory operation audit trail
cat ~/.memem/mine_delta.log                 # stop-hook mining log (v2.1.0+)
python3 -m memem.server --status            # detailed status dump
python3 -m memem.server --integrity-check   # PRAGMA integrity_check on every DB
```

## How does the mining pipeline work?

```
Claude Code Stop event fires → stop-mine.sh hook spawns mine_delta (detached, ~50ms)
  → mine_delta reads session JSONL from byte offset (new turns only)
  → Filters to human messages + assistant prose (strips tool calls, system reminders)
  → One Haiku call with the delta context: "extract durable lessons"
  → Haiku returns JSON array of memory candidates
  → Each candidate runs: security scan → dedup check → contradiction detection → save
  → Offset advanced; session marked in ~/.memem/.mined_sessions
  → SessionStart stale-sweep catches any sessions where Stop never fired (crash, kill -9)
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
- `retrieve.py` — v2.0.0: cosine top-K + FTS-conditional supplement for version/date literals. Mtime-invalidated vault index + embedding caches.
- `render.py` — v2.0.0: 2-section renderer (`## Working` + `## Relevant`).
- `obsidian_store.py` — memory I/O, dedup scoring, contradiction detection, layer auto-classification on save
- `recall.py` — slice-format recall tools (`memory_search`/`memory_get`/`memory_timeline`/`memory_recall`) — surgically rewritten in v2.0.0 with inline `_render_recall_markdown` (the legacy `active_slice` renderer is gone)
- `playbook.py` — per-project playbook grow + refine
- `assembly.py` — `context_assemble` composes via `recall` pipeline
- `capabilities.py` — runtime feature detection for degraded mode
- `storage.py` — server-lifecycle helpers (PID management, miner auto-start)
- `server.py` — thin MCP entrypoint (FastMCP imported lazily)
- `cli.py` — command dispatcher for non-MCP entrypoints
- `mining.py` — session mining pipeline (Haiku extraction, `extract_from_text`)
- `mine_delta.py` — v2.1.0: event-triggered delta miner; reads new turns since last offset, calls `extract_from_text`, marks session complete
- `session_state.py` / `session_state_db.py` — SQLite WAL state for the miner (auto-migrates from JSONL on first run)

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
| `MEMEM_MINER_SETTLE_SECONDS` | `1800` | (legacy) Settle-window seconds. In v2.1.0 both the Stop hook AND `--mine-all` bypass this gate; retained only for forward-compat with future tooling that may opt into it. |
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
pytest             # ~391 tests (14 skipped)
ruff check .       # lint
mypy memem         # type check
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
