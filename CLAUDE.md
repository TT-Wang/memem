# memem — Persistent Memory & Context Assembly

You have access to a persistent memory system via memem. This memory persists across sessions, self-evolves, and gets smarter over time.

Storage: Obsidian vault at `~/obsidian-brain/memem/memories/` (markdown files, single source of truth).
Machine index: SQLite FTS5 at `~/.memem/search.db` for fast retrieval.

## Auto-recall

In `auto` mode, the UserPromptSubmit hook fires on every message and builds an `active_memory_slice` from the current query. As of v2.4.0, `tool` is the default mode — auto-injection is off; Claude pulls memory on demand via the MCP tools below.

For on-demand recall, use the MCP tools below.

## Layered recall (v0.10)

Memories are organized into layers:
- **L0 (always-loaded):** project identity — tech stack, repo structure, core conventions. L0 memories are pre-seeded in MMR (decay_immune) so they anchor every retrieval.
- **L1 (generic conventions):** broadly useful patterns (testing, style, commit conventions)
- **L2 (domain-specific):** most memories — the default bucket
- **L3 (rare/archival):** niche failure modes, one-off lessons

**At session start** you receive only the Episode index (up to 25 recent `type:episodic` memories listed by title). There is no L0 briefing injected at session start. Use `memory_search` or `memory_get` to pull L0 project-identity content on demand.

**During the session**, use the 3-tier recall workflow:

1. **`memory_search(query)`** — compact index (~50 tok/result). Returns IDs + titles + 1-line snippets. Use FIRST to narrow candidates cheaply.
2. **`memory_get(ids=[...])`** — full content (~500 tok/result). Use AFTER memory_search when you know which specific memories you need.
3. **`memory_timeline(memory_id)`** — chronological thread via `related[]` graph + same-project window. Use when you need the narrative around a memory (what led to it, what came after).

`memory_recall` (legacy) still works as a backward-compat alias that's equivalent to memory_search + memory_get on top results.

**Always-wake recall (auto mode only)** — when `MEMEM_INJECTION_MODE=auto`, the `UserPromptSubmit` hook runs `active_memory_slice` on every prompt. In the default `tool` mode this hook produces no auto-injection; you call `active_memory_slice` manually when context is needed.

**Injection mode (`MEMEM_INJECTION_MODE`)** — controls whether the hook auto-injects context into every prompt (v1.9+):

**As of v2.4.0, `tool` is the default.** Existing users with `MEMEM_INJECTION_MODE=auto` in their shell profile keep auto behavior unchanged.

| Value | Behaviour |
|-------|-----------|
| `auto` | Hook injects the active memory slice on every prompt. Equivalent to pre-v2.4.0 behaviour. To restore: `export MEMEM_INJECTION_MODE=auto`. |
| `tool` | **Default (v2.4.0+).** Hook produces no auto-injection. You control recall entirely via the `active_memory_slice` MCP tool. Zero hook overhead; no passive context. |

`hybrid` was removed in v2.5.0 — it was documented but never implemented.

Set this in your Claude Code environment or shell profile (e.g. `export MEMEM_INJECTION_MODE=auto`).

Selective-recall tunables (v1.9.6+, env vars, all optional):

| Var | Default | Behaviour |
|-----|---------|-----------|
| `MEMEM_RECALL_MIN_ITEM_SCORE` | `0.0` | Per-item composite-score floor for recall results (0.0 = disabled). L0 project-identity anchors are always exempt. Clamped to `[0.0, 1.0]`. |

**Graph traversal** — `memory_search` and `memory_get` automatically follow the `related[]` field one hop and include linked memories in a separate section.

## Auto-save

When you complete significant work, save non-obvious lessons:

Call `mcp__memem__memory_save` with:
- `content`: The key insight (one per save, keep it atomic)
- `title`: Short descriptive title
- `tags`: Comma-separated relevant tags

**Save these (durable knowledge):**
- User preferences, corrections, conventions
- Architecture decisions with rationale
- Environment facts, tool quirks, project structure
- Non-obvious lessons learned from failures

**Do NOT save these (use transcript_search instead):**
- Task progress, session outcomes, what was worked on today
- Completed-work logs or TODO state
- Trivial or obvious facts easily re-discovered from code

## Kind tags (v1.10)

Tag `memory_save` calls with a `type:*` tag to help recall-time grouping:

- `type:episodic` — timestamped event (decision made, problem encountered, status update at a moment in time)
- `type:skill` — reusable approach, pattern, or convention (something to do again)
- `type:case` — full task narrative: problem → approach → result

**Multi-label OK:** a memory can carry both `type:episodic` and `type:case` (e.g., a debugging session that tells a full story).

**Backward compat:** these tags drive recall-time grouping only; they do not change storage. Untagged memories fall back to heuristic detection.

## Mining (event-triggered, opt-in)

memem mining triggers automatically on every Claude Code Stop event when the opt-in marker exists. No daemon to start or stop.

**To enable:**
```bash
mkdir -p ~/.memem && touch ~/.memem/.miner-opted-in
```

That's it. Each Stop event spawns a detached `mine_delta` subprocess that extracts memories from the delta of new turns since the last invocation (offset-tracked per session). Hook overhead is ~50ms; the Haiku call happens in background.

**To backfill history:**
```bash
mkdir -p ~/.memem && touch ~/.memem/.miner-opted-in
python3 -m memem.server --mine-all
```

This iterates over historic JSONL sessions and invokes mine_delta for each. Takes minutes for large histories; uses Haiku API credits.

**To opt out:**
```bash
rm ~/.memem/.miner-opted-in
```

(No daemon to stop — the hook just no-ops without the marker.)

**Safety net:** SessionStart fires a "stale-session sweep" that scans for JSONL files older than 10 min that aren't in `~/.memem/.mined_sessions` and spawns mine_delta for up to 3 of them. Catches sessions where Stop never fired (Claude crash, kill -9, network drop). The sweep skips headless mining transcripts (detected by marker phrases in the first 20 lines + file size ≤ 30 lines) to prevent self-mining contamination loops.

**Purging contaminated memories:** if mining artifacts were saved to the vault before the v2.5.0 stale-sweep guard, run `python3 -m memem.server --purge-contaminated` to identify them (dry-run by default). Add `--apply` to permanently delete the flagged memories.

## Available tools

| Tool | What |
|------|------|
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_search` | **[Layer 1]** Compact index search — returns ~50 tok/result, use first |
| `memory_get` | **[Layer 2]** Full content fetch by IDs — use after memory_search |
| `memory_timeline` | **[Layer 3]** Chronological thread via related[] graph |
| `memory_recall` | (legacy) Search + fetch full content — prefer search+get for token efficiency |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |
| `context_assemble` | On-demand query-tailored briefing from all knowledge |
| `active_memory_slice` | On-demand runtime working-state slice from active recall candidates |

**CLI commands** (run in your terminal, not via MCP):
- `python3 -m memem.server --analyze-recalls` — summarize recall telemetry from `~/.memem/.recall_log.jsonl`: which tools were called, which memories retrieved most often, recall frequency per session. Use this to understand how Claude is (or isn't) pulling memory.
- `python3 -m memem.server --purge-contaminated [--apply]` — identify (dry-run) or delete memories mined from headless mining transcripts. Default is dry-run; pass `--apply` to permanently delete.

## Episodic consolidation (v1.7)

Run `python3 -m memem.server --consolidate` to cluster near-duplicate memories by
embedding similarity and merge them into canonical memories (flagging contradictions).
Recommended cadence: weekly cron. The cron itself is not installed by this module —
wire it manually if desired (e.g. `0 2 * * 0 python3 -m memem.server --consolidate`).

## Backward compatibility

memem was renamed from `cortex` in v0.7.0. Existing users with data under
`~/.cortex/` and `~/obsidian-brain/cortex/` are auto-migrated to `~/.memem/`
and `~/obsidian-brain/memem/` on first run via a one-time copy. Legacy paths
remain intact as a safety net. Legacy `CORTEX_*` env vars are still read as
fallbacks for `MEMEM_*`.

**Project aliases (v2.5.0):** create `~/.memem/project_aliases.json` (or `$MEMEM_DIR/project_aliases.json`) to canonicalize project names. Format: `{"alias": "canonical"}` — the KEY is the old/alternate name, the VALUE is the canonical scope memories should consolidate under. Example: `{"vibe-reader": "vibereader", "old-repo-name": "new-repo-name"}`. Do NOT map a canonical name back to one of its aliases (e.g. `{"cortex-plugin": "memem"}` would conflict with the built-in `memem → cortex-plugin` mapping and split one project across two scopes). Used everywhere scope is normalized: recall filtering, FTS indexing, and mining project assignment.
