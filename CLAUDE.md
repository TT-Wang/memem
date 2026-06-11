# memem — Persistent Memory & Context Assembly

You have access to a persistent memory system via memem. This memory persists across sessions, self-evolves, and gets smarter over time.

Storage: Obsidian vault at `~/obsidian-brain/memem/memories/` (markdown files, single source of truth).
Machine index: SQLite FTS5 at `~/.memem/search.db` for fast retrieval.

## Auto-recall

In `auto` mode, the UserPromptSubmit hook fires on every message and builds an `active_memory_slice` from the current query. As of v2.4.0, `tool` is the default mode — auto-injection is off; Claude pulls memory on demand via the MCP tools below.

For on-demand recall, use the MCP tools below.

## Context model (v2.8+)

v2.8.0 replaces the L0–L3 layer system with a three-tier context model. Layers are retired — the data falsified them.

### Tier 1 — Profiles (always-injected)

Profile documents at `<vault>/memem/profiles/` are injected at every session start before any memory recall occurs. Two document types:

- **`profile_user.md`** — sections: Preferences / Conventions / Environment
- **`profile_<project>.md`** — sections: Identity / Stack & Structure / Conventions

Profiles are NOT in the retrieval corpus (you will never see them in `memory_search` results). Their consumer is the SessionStart hook. The miner writes to profiles via the `PROFILE` reconcile op; you can also populate them via `--migrate-layers --apply`.

### Tier 2 — Working rules (procedural, citation-ranked)

At session start you receive a `## Working rules` block containing `type:procedural` memories — failure→fix patterns and correction knowledge — ranked by citation count (last 30 days) then by recency, capped to ≤1200 chars. These are the rules you have cited most recently in your work.

### Tier 3 — Episode index + on-demand retrieval

At session start you also receive a `## Episode index` of up to 25 recent `type:episodic` memory titles. Everything else is available on demand via the recall tools below.

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

Selective-recall tunables (env vars, all optional):

| Var | Default | Behaviour |
|-----|---------|-----------|
| `MEMEM_RECALL_MIN_ITEM_SCORE` | `0.0` | Per-item composite-score floor for recall results (0.0 = disabled). Clamped to `[0.0, 1.0]`. |
| `MEMEM_RERANK_MODEL` | `""` | Cross-encoder reranker model name (e.g. `cross-encoder/ms-marco-MiniLM-L-12-v2`). When set, all `memory_search`/`memory_recall` calls apply a CE reranking pass over the top-50 unified-engine candidates before truncating to `limit`. Model downloaded on first use. Not required — three-way RRF produces strong results without CE. |
| `MEMEM_DREAM_AUTO` | `1` | Set `0` to disable the autonomous every-25-deltas dream pass (unattended Haiku spend + auto-applied additive vault mutations). Manual `--dream` is unaffected. |

**Retrieval engine (v2.6.0+)** — `memory_search`, `memory_recall`, and `active_memory_slice` all use the same unified engine: three-way RRF (cosine + BM25 + FTS5) with a rerank signal bundle (usage, scope, link, importance) and MMR diversification. There is no separate heuristic engine.

**Scope semantics (v2.6.0+)** — `scope_id` is a **soft bonus** (not a hard filter). Memories in the named project rank higher, but strong cross-project results are not excluded. Default `"default"` applies no scope bonus.

**Graph traversal** — `memory_search` and `memory_get` automatically follow the `related[]` field one hop and include linked memories in a separate section.

### Layer field (deprecated)

The `layer` parameter on `memory_save` is still accepted for backward compatibility but deprecated — new memories are written without a `layer:` field and the value has no effect on retrieval. Legacy memories with `layer:` frontmatter remain readable; L0 / `decay_immune` memories retain dreamer protection. To migrate your existing L0/L1 content into profiles, run `python3 -m memem.server --migrate-layers`.

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

**Deduplication behavior (v2.7.0):** `memory_save` applies three-band dedup on every call:
- Score **≥ 0.92** — near-exact duplicate; returns "Memory already exists: [id8] title. Not saved."
- Score **0.70–0.92** — similar content; Haiku merges the new content into the existing memory and unions tags. Returns "Merged into existing memory [id8] title..." Merge failures fall back to reject.
- Score **< 0.70** — genuinely new; saved normally.

## Kind tags

Tag `memory_save` calls with a `type:*` tag to help recall-time grouping:

- `type:episodic` — timestamped event (decision made, problem encountered, status update at a moment in time)
- `type:skill` — reusable approach, pattern, or convention (something to do again)
- `type:case` — full task narrative: problem → approach → result
- `type:procedural` — failure→fix or correction knowledge (imperative phrasing); miner emits this automatically; surfaced in `## Working rules` at session start, ranked by citation count

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

**Purging contaminated memories:** if mining artifacts were saved to the vault before the v2.5.0 stale-sweep guard, run `python3 -m memem.server --purge-contaminated` to identify them (dry-run by default). Add `--apply` to permanently delete the flagged memories. Use `--exclude id8[,id8,...]` to skip specific memories by 8-char id prefix.

**Reconciliation (v2.7.0+):** the miner no longer blindly adds every extracted candidate. Before saving, `_reconcile_candidates` compares each candidate against its top-5 vault neighbors (ngram search) in one batched Haiku call and assigns one of five ops:

- **ADD** — new information; saved normally with a stable deterministic id (`uuid5(session_id+content)` — idempotent re-mining).
- **UPDATE** — candidate refines a neighbor; `_update_memory` merges content and unions tags/keys. Safety rail: merged content must be ≥ 10 chars and ≥ 30% of neighbor's essence, otherwise degrades to ADD.
- **SUPERSEDE** — candidate contradicts/replaces a neighbor; new memory saved, neighbor bi-temporally invalidated (`invalid_at`, `replaced_by` frontmatter). Safety rail: `decay_immune` memories are never superseded.
- **NOOP** — candidate is fully redundant; skipped.
- **PROFILE** (v2.8.0) — candidate is written directly to a user or project profile document instead of saving a vault memory. Capped at ≤3 PROFILE ops per delta. Schema-validated against the profile's allowed section list. Degrades to ADD on any failure.

Cap: at most 5 UPDATE+SUPERSEDE ops per delta (PROFILE ops have their own cap of 3). Any reconcile exception falls back to ADD-all. Every op writes an audit event (`reconcile_add/update/supersede/noop/reconcile_profile`) to `~/.memem/events.jsonl`.

**Key expansion (v2.7.0):** Haiku now emits up to 8 `keys` per memory (synonyms, entity names, abbreviations, error strings). Keys are stored in frontmatter (`keys:` block), FTS-indexed via the `tags` column (no schema bump), and included in BM25 text so keyword queries find memories by alias.

## Available tools

| Tool | What |
|------|------|
| `memory_save` | Store a lesson, pattern, or convention |
| `memory_search` | Compact index search — returns ~50 tok/result, use first |
| `memory_get` | Full content fetch by IDs — use after memory_search |
| `memory_timeline` | Chronological thread via related[] graph |
| `memory_recall` | (legacy) Search + fetch full content — prefer search+get for token efficiency |
| `memory_list` | List all memories with stats |
| `memory_import` | Import from files, directories, or chat exports |
| `transcript_search` | Search raw Claude Code session logs |
| `context_assemble` | On-demand query-tailored briefing from all knowledge |
| `active_memory_slice` | On-demand runtime working-state slice from active recall candidates |

**CLI commands** (run in your terminal, not via MCP):
- `python3 -m memem.server --analyze-recalls` — summarize recall telemetry from `~/.memem/.recall_log.jsonl`: which tools were called, which memories retrieved most often, recall frequency per session, citation rate per tool (fraction of returned memories later cited in assistant text), top cited memories, and returned-ids count (token-budget proxy). Use this to understand how Claude is (or isn't) pulling memory.
- `python3 -m memem.server --purge-contaminated [--apply]` — identify (dry-run) or delete memories mined from headless mining transcripts. Default is dry-run; pass `--apply` to permanently delete.
- `python3 -m memem.server --purge-contaminated --exclude id8[,id8,...] [--apply]` — skip specific memories by 8-char id prefix when purging.
- `python3 -m memem.server --migrate-layers [--apply] [--exclude id8,...]` — dry-run report (default) or apply migration of legacy L0/L1 memories into profile documents. **HUMAN REVIEW REQUIRED before `--apply`**. Idempotent and additive-only — memories are never deleted.
- `python3 -m memem.server --dream [--safe-auto]` — run a dream pass. `--safe-auto` auto-applies additive categories (reflection_with_citations, tense_rewrite) and leaves destructive categories (demotions, cluster merges) as dry-run-report-only.
- `python3 -m memem.server --consolidate` — back-compat alias for `--dream` filtered to cluster_merge proposals only.

## Dream cycles (v2.8+)

The dreamer runs background maintenance on the vault. Categories:

- **`cluster_merge`** — embedding-based greedy clustering; Haiku merges clusters into canonical memories and bi-temporally invalidates only the `supporting_ids` members (not all cluster members). Destructive — requires `--apply` or explicit approval.
- **`reflection_with_citations`** — when ≥8 new episodic memories have accumulated since last dream, synthesizes ≤3 `type:insight` memories with `related[]` links. Additive — auto-applied in `--safe-auto`.
- **`tense_rewrite`** — rewrites expired future-tense memories (older than 30 days) to past/present tense. Content-preserving — auto-applied in `--safe-auto`. `type:procedural`, `type:skill`, and `type:insight` memories are excluded.

**Automatic dream trigger**: every 25 substantive mining deltas (deltas that produce ≥1 vault write) spawn a detached `--dream --safe-auto` pass. A `.dream.lock` flock prevents double-firing.

## Backward compatibility

memem was renamed from `cortex` in v0.7.0. Existing users with data under
`~/.cortex/` and `~/obsidian-brain/cortex/` are auto-migrated to `~/.memem/`
and `~/obsidian-brain/memem/` on first run via a one-time copy. Legacy paths
remain intact as a safety net. Legacy `CORTEX_*` env vars are still read as
fallbacks for `MEMEM_*`.

**Project aliases (v2.5.0):** create `~/.memem/project_aliases.json` (or `$MEMEM_DIR/project_aliases.json`) to canonicalize project names. Format: `{"alias": "canonical"}` — the KEY is the old/alternate name, the VALUE is the canonical scope memories should consolidate under. Example: `{"vibe-reader": "vibereader", "old-repo-name": "new-repo-name"}`. Do NOT map a canonical name back to one of its aliases (e.g. `{"cortex-plugin": "memem"}` would conflict with the built-in `memem → cortex-plugin` mapping and split one project across two scopes). Used everywhere scope is normalized: recall filtering, FTS indexing, and mining project assignment.
