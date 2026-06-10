# memem SOTA Improvement Plan — June 2026

Synthesis of: (1) line-by-line review of all 43 core + supporting modules and 7 hooks,
(2) competitor survey (Mem0, Zep/Graphiti, Letta, LangMem, Cognee, ChatGPT memory,
Basic Memory, A-Mem, claude-mem, MemOS, Memobase, Supermemory), (3) academic survey
(~25 papers, 2023–2026), (4) live vault + telemetry data (2,360 memories, recall_log,
events.jsonl, index freshness).

Constraints honored throughout: local-first, daemon-free, markdown source-of-truth,
no required external APIs on the core path, data-before-roadmap, deletion-as-progress.

---

## 0. Where memem actually stands

**The architecture is validated.** The research frontier converged on exactly memem's
skeleton: a rich index over lossless plaintext (hippocampal indexing theory, Basic
Memory, Anthropic's context-engineering doctrine), hybrid BM25+vector+RRF retrieval
(Zep, SuperLocalMemory), and event-triggered offline consolidation (ChatGPT Dreaming,
Letta sleep-time — the Stop hook IS sleep-time compute). Local-first stacks score
within a few points of cloud systems. Nothing in the credible evidence requires a
daemon, a graph DB, or an external API.

**The implementation has three systemic diseases:**

1. **Severed loops.** Attribution, feedback EMA, decay reinforcement, lessons,
   canaries, playbooks, capabilities — all built, none wired. Retrieval is
   effectively static while docs describe a self-evolving system.
2. **Two of everything.** Two retrieval engines (the benchmarked one doesn't serve
   the default path), two vault parsers, two access-count stores, three consolidation
   engines, three project-identity conventions, two atomic-write helpers.
3. **Write-path poverty.** Mining is ADD-only: no reconciliation, no supersession
   (the miner emits `supersedes`; nothing consumes it), no idempotency
   (`_stable_mined_memory_id` has zero callers), no enrichment keys, prose-only
   input, and an active self-mining contamination loop.

Research consensus on where the wins are: **write-time processing and temporal/update
handling — not retrieval algorithms.** memem's retrieval base layer is already at
consensus; its miner is generations behind its own retriever.

---

## 1. Confirmed defects (evidence-grade, fix regardless of strategy)

### Correctness bugs
| # | Bug | Evidence |
|---|-----|----------|
| B1 | Self-mining loop: SessionStart stale-sweep mines headless `claude -p` transcripts; vault contains mining-prompt artifacts (7 "Below is a coding conversation" + 26 task-notification memories; new ones today 05:27) | session-start.sh:203-212; events.jsonl |
| B2 | Temporal boost +0.2 vs RRF scores capped at ~0.033 → any in-window memory hard-overrides all semantic ranking | retrieve.py:517 |
| B3 | MMR mixes RRF-scale relevance (~0.03) with cosine-scale diversity (0–1) → diversity dominates ~10:1 | retrieve.py:372 |
| B4 | Embeddings index 426 memories stale (1,934 vs 2,360; 47h old) — no on-save upsert, no in-process mtime invalidation; newest 2 days invisible to vector path | embedding_index.py:100-101; ~/.memem/embeddings.npy |
| B5 | graph.db has no `edges` table on this machine — typed graph never built; all graph calls silently fall back | ~/.memem/graph.db |
| B6 | Episode duplication: Stop fires per turn → 9 "episodes" for session 9612f54c, titles from delta-local first message (incl. `<task-notification>` XML) | mine_delta.py:296-305, 423-431 |
| B7 | post-stop-attribution.sh: registered no-op on every Stop — imports 2 deleted modules, reads a never-written file | post-stop-attribution.sh:131-136,170-184 |
| B8 | Decay reads frontmatter `access_count` (always 0); real counts live in telemetry sidecar → reinforcement dead, demotion = pure age | decay.py:73 vs telemetry.py |
| B9 | feedback weight (15%) reads `relevance_scores.json` that is never written (writer has zero callers) → constant 0.075 dead weight | recall.py:266; feedback.py |
| B10 | dreamer model id `claude-sonnet-4-7` doesn't exist → all dream LLM calls silently fail | dreamer.py:44 |
| B11 | False `supersedes` graph edges: any `supersedes:*` tag matches any mildly-similar dst, force-boosted to 0.80 | graph_index.py:358-360,514-515 |
| B12 | `bump_access` rewrites the full markdown file per recall hit → mtime churn invalidates vault cache, BM25 cache, and triggers msgpack flush per recall | obsidian_store.py:812-823; recall.py:421-434 |
| B13 | mine_delta episode project from `os.getcwd()` — wrong for stale-sweep-spawned mining (cwd = whatever session you're starting) | mine_delta.py:297 |
| B14 | L0 briefing dead since v2.0.0: session-start imports deleted `active_slice_engine`, always ImportError → content="" | session-start.sh:116-129 |
| B15 | Episode catalog pads to 50 with arbitrary non-episodes | session-start.sh:145-151 |
| B16 | Hybrid mode is fiction: hook only branches on `tool`; 7/10 settings knobs have zero consumers; CLAUDE.md documents nonexistent gating | auto-recall.sh:17; settings.py |
| B17 | dreamer/consolidation write fields the store silently drops (`clustered_into`, `references`) | dreamer.py:474; consolidation.py:422 |
| B18 | FTS indexes `memory_id`/`related_ids` as searchable columns → BM25 relevance pollution; should be UNINDEXED | search_index.py:24-27 |
| B19 | pre-tool-use hook: harness timeout 5s < internal 8s + 2-4s cold start → killed before completing | hooks.json; pre-tool-use.sh:60-67 |
| B20 | sqlite connections never closed in session_state_db (`with conn` ≠ close) → fd leak in MCP server | session_state_db.py:111+ |
| B21 | eval_set can write into the production vault outside pytest fixtures | eval_set.py:117-135 |
| B22 | top-level `from sentence_transformers import ...` in retrieve.py → module hard-fails when optional dep missing | retrieve.py:31 |
| B23 | Shipped personal default `-home-claude-user-lexie` exclusion; hardcoded personal project aliases | session_state.py:36; models.py:126 |
| B24 | retrieve() labels every call `hook_auto` incl. CLI/slice; tool_memory_search logs `returned_ids=[]` → citation telemetry can't attribute | retrieve.py:541; server.py:160-167 |

### Dead code (delete, ~2,000+ LOC)
compaction.py (364) + reaper.py (150) + tests; post-stop-attribution.sh + hooks.json
entry; `assembly._consolidate_project`; `eval_replay.export_baseline`; dead settings
knobs (7); `_stable_mined_memory_id` (or wire it — P2); `_fts_temporal_search` (or
wire — P1); attribution.py (or rewire-lite — P5); capabilities read-side;
`storage.py` fold-in; duplicate `_atomic_write` in obsidian_store; recall_log dead
validation constants; `session_is_complete`; `status._pid_alive`;
`quarantine._FRONTMATTER_STRICT_MODE`; lessons.py (unless wired — P5 decides).

---

## 2. Live telemetry baseline (the numbers the plan must move)

- 2,360 memories: 89% mined, 33 episodes, 174 user. 462 L0 + 431 L1 (38% "high
  priority" — tier inflation; the L0 heuristic failed).
- recall_log since v2.4.0: 11 rows, **zero `tool_*` entries** → in tool mode the LLM
  pulled memory 0 times in ~1 day. The passive-mode bet is unproven and currently
  losing; only more days of data will tell.
- Fixed context cost in tool mode today: ~4–5k tokens of MCP tool descriptions
  (14 tools, some 100-line docstrings) + ~1k episode catalog — **more than the
  ~1.5k auto-injection that was removed**, with zero observed pulls.
- 603+ memories with access_count 0 in frontmatter (and the field is never
  incremented anyway — B8).

---

## 3. The plan

Phases are sequential releases. Each has a **data gate** — per the
data-before-roadmap principle, later phases proceed only if the gate metric
justifies them.

### Phase A — v2.5 "Repair & Prune" (fix bugs, delete dead weight)
*Effort: S-M. No new capabilities. Pure correctness + hygiene.*

1. **Kill the self-mining loop** (B1): stale-sweep skips transcripts whose first
   user message contains the mining envelope marker (`=== BEGIN CONVERSATION ===` /
   "Below is a coding conversation") or whose session has only 1-2 turns with
   `--tools ""`. Also: one-time vault purge of the ~33 contaminated memories +
   task-notification-titled episodes.
2. **Fix ranking scale bugs** (B2, B3): normalize RRF scores to [0,1] within the
   candidate pool before temporal boost and MMR; temporal boost becomes a bounded
   multiplier or rank-tier promotion, benchmarked before/after.
3. **Index freshness** (B4): incremental embedding upsert in `_save_memory` (encode
   one memory, append to npy + ids) + mtime check in `_load_index`. Embeddings can
   never again lag the vault. (FTS is already event-driven.)
4. **One access-count store** (B8, B12): drop `bump_access` markdown rewrites from
   the recall path; sidecar (`telemetry.json`) is the single store; decay and
   rerank read it. Frontmatter keeps `last_accessed_at` updated lazily by the
   consolidation pass only (one write, not per-recall).
5. **Episode emission correctness** (B6, B13, B15): one episode per session —
   stable uuid5 id per session_id, update-in-place on later deltas; title from the
   first *human* message (filter task-notifications/XML); project derived from the
   transcript path (`~/.claude/projects/<munged-cwd>/`), not `os.getcwd()`;
   catalog lists only true episodes (cap 25).
6. **Delete dead code** (§1 list) + de-document hybrid mode (B16) + fix B10, B11,
   B17–B23. Single honest CHANGELOG entry: "deletion as progress."
7. **Telemetry labeling** (B24): correct call_type per caller; log returned_ids for
   every tool; this is the instrumentation the gates below depend on.

**Gate A→B:** replay eval (existing harness) shows no regression; recall_log now
produces interpretable per-tool rows. Ship immediately — nothing here is speculative.

### Phase B — v2.6 "One Engine" (retrieval unification)
*Effort: M. The benchmarked engine serves every path.*

1. `memory_search` / `memory_recall` / hook / `active_memory_slice` all call
   `retrieve()`. The recall.py 5-signal heuristic and the duplicate regex vault
   parser are deleted. FTS5 remains inside retrieve() as a candidate generator
   (it currently only fires on version/date literals — generalize: FTS top-20 joins
   the BM25+cosine RRF fusion as a third ranked list).
2. **Degraded mode**: lazy-import sentence-transformers; without it, retrieve() runs
   BM25+FTS-only (no crash — B22).
3. **Scope as a soft signal**: same-project memories get a bounded rank bonus
   (not a hard filter; cross-project hits stay reachable). Project normalization
   unified into one function used by FTS, playbooks, compact-index.
4. **Usage strength in rerank** (ACT-R / Generative Agents / MemoryBank, all three
   surveys agree): final = RRF_norm + α·recency-since-last-access decay +
   β·capped log(access_count) + γ·importance. Read from the sidecar. Capped
   frequency term (retrieval-induced-forgetting caution). Calibrate α,β,γ on the
   replay set; decay.py's broken parallel implementation is deleted.
5. **1-hop link activation spread** (HippoRAG-lite): retrieved notes give a
   discounted bonus to `related[]` neighbors. SQLite/in-memory; replaces the
   current bolt-on "linked" section with an integrated ranking signal.
6. Optional cross-encoder via `MEMEM_RERANK_MODEL` env (off by default).

**Gate B→C:** 18-query benchmark ≥74% (no regression) AND replay Jaccard\@k vs v2.5
shows the unified engine ≥ heuristic engine on captured real queries.

### Phase C — v2.7 "Write Path" (the SOTA-defining release)
*Effort: M-L. This is where memem catches the frontier; both surveys rank it #1.*

1. **Reconcile-at-write (Mem0's update phase, minus DELETE).** For each extracted
   candidate: retrieve top-5 similar existing memories from the index; one Haiku
   call decides **ADD / UPDATE / SUPERSEDE / NOOP** with structured output.
   - UPDATE → `_update_memory` (exists)
   - SUPERSEDE → `invalidate_memory(old, replaced_by=new)` (exists, currently
     unreachable) — bi-temporal discipline: invalidate, never delete
   - The miner's `supersedes` output finally gets consumed.
   - `memory_save` near-duplicates (≥0.7): return AI-native response — "similar
     memory [id] exists: <title>. Saved as UPDATE / rejected as exact dup" using
     `_merge_memories` (exists, currently 1 caller) instead of discarding new info.
2. **Mining idempotency**: wire `_stable_mined_memory_id` (uuid5 of
   session+title+content) + content-hash skip (Cognee pattern) → re-mining and
   backfills become safe.
3. **Fact-augmented key expansion** (LongMemEval-measured; #1 ROI in the academic
   survey): extractor emits `keywords`, `entities`, `aliases` per memory → stored
   in frontmatter, indexed in FTS as additional keys. Near-zero marginal cost
   (same Haiku call), directly lifts recall.
4. **Let the miner see the work**: include a compact tool-trace digest in the
   extraction input (files touched, commands run, errors seen — capped) instead of
   stripping all tool blocks. Decisions live in diffs, not prose.
5. **Quality gates** (experience-following paper, +10% absolute): minimum-substance
   check, structured-output schema validation (Anatomy-of-Agentic-Memory's "weak
   models silently corrupt state during writes"), and per-session extraction cap.

**Gate C→D:** after 2 weeks: contradiction rate (new memories whose reconcile op was
SUPERSEDE/UPDATE vs ADD) — if >10% of mined facts are updates, the reconciler is
earning its Haiku cost; duplicate-title count trends to zero.

### Phase D — v2.8 "Vault Structure" (types that earn their existence)
*Effort: M. Replaces the failed L0–L3 system with evidence-backed types.*

1. **Profile documents** (LangMem/Memobase/Letta convergence): `profile_user.md`
   + `profile_<project>.md` — schema-shaped, patch-in-place, size-bounded
   (~300 tokens each). The reconciler routes stable facts (preferences,
   conventions, project identity) into profiles instead of the collection.
   Profiles are injected in full at SessionStart — this **replaces** the dead L0
   promise (B14) with something that actually ships tokens.
2. **Retire the L0–L3 layer system.** The data falsified it (462 L0s). Migration:
   L0/L1 memories become reconcile candidates for profiles; layer field ignored,
   then removed from new writes. Deletion as progress.
3. **Procedural type** (best-evidenced cluster for coding agents: AWM, Memp,
   Reflexion): miner detects failure→fix and correction sequences → `type:procedural`
   lesson notes ("when X, do Y, because Z"). Injected as a compact imperative
   rules block at SessionStart (budgeted, ~300 tokens), separate from facts.
4. **One consolidation engine.** Keep consolidation.py's clustering, adopt
   dreamer's diff/dry-run discipline, respect `supporting_ids`, fix dropped
   fields (B17), delete the other two engines. Add two evidence-backed passes:
   - **Reflection with citations** (Generative Agents): when accumulated mined
     importance exceeds a threshold, generate 2–3 insight memories that wiki-link
     their supporting episodes.
   - **Tense rewriting** (ChatGPT Dreaming): expired future-tense facts get
     rewritten past-tense ("planning to migrate X" → "migrated X in May 2026").
   Trigger: every N Stop events (counter file), no daemon, no cron.
5. **SessionStart assembly becomes budgeted blocks** (Letta): `[profiles ~600] +
   [procedural rules ~300] + [episode index ~400]` ≈ 1.3k tokens, each section
   independently capped.

**Gate D→E:** user-reminder rate ("you forgot", "remember we") trends down;
profile hit rate measurable in recall_log (profiles cited without being searched).

### Phase E — v2.9 "Interface Economy" (AI-native surface)
*Effort: S-M. Token cost down, pull rate up.*

1. **Tool diet: 14 → 6.** Keep `memory_search`, `memory_get`, `memory_timeline`,
   `memory_save`, `transcript_search`, `active_memory_slice`. Fold/remove:
   `memory_recall` (deprecated), graph tools → CLI, `context_assemble` (docstring
   oversells; assembly = two search calls), `memory_remind` → merge into search
   (`vaults=all` param), `memory_list` → CLI, `memory_import` → CLI/skill.
2. **Docstring diet**: every tool description ≤ 12 lines. Target: tool-schema
   overhead < 1.5k tokens (from ~4-5k). The tool descriptions are the de facto
   injection in tool mode — treat them with the same token discipline as the slice.
3. **Progressive disclosure as the documented contract** (claude-mem/Anthropic):
   search (~50 tok/hit, now including valid_at date + project) → timeline →
   get. CLAUDE.md rewritten to match reality (kills the hybrid fiction, the L0
   fiction, the assembly fiction).
4. **Path-scoped memories** (Claude Code rules pattern): optional `paths:`
   frontmatter; rank bonus when the session's recently-read files match. Cheap,
   familiar convention.
5. **transcript_search gets an FTS table** (currently re-parses every JSONL per
   query — seconds at scale).

**Gate E→F:** recall_log pull rate per session rises vs Phase A baseline; tokens
per session-start measured before/after.

### Phase F — continuous: "Close the Loop" (telemetry-driven curation)
*Effort: M, incremental. Uses parts that already exist.*

1. **Wire feedback EMA** (one call): Stop hook → `_classify_session_outcome`
   (exists, format-compatible per review) → `update_relevance_scores` (exists,
   zero callers). The 15% feedback weight in rerank becomes live signal.
2. **Citation attribution-lite** (replaces the dead 3-signal attribution):
   post-stop, grep the transcript delta for 8-char memory ids that appeared in
   injected/pulled content → `cited: true` rows in recall_log. This yields the
   metric that matters: **citation rate per retrieval** — Supermemory's
   "recall@token-budget" framing, computed from production data.
3. **Lessons wiring decision**: either one filter line in retrieve()
   (`candidates -= excluded_memory_ids_for_query(query)`) or delete lessons.py.
   Decide by whether F.2 ever shows a repeatedly-cited-but-wrong memory.
4. **Canaries in --doctor**: trip-wire verification runs on every doctor/release,
   and actually executes the trigger queries (today it only checks presence).
5. **Eval honesty**: replay dispatches on captured engine mode (today it replays
   tool captures through the wrong engine); internal eval set extended with
   knowledge-update + abstention categories (LongMemEval's two hardest abilities);
   all release gates report recall@k *at a fixed token budget*.

---

## 4. What NOT to do (evidence-backed skips)

1. **Extracted-triple KGs / Neo4j / GraphRAG** — HippoRAG2's own data shows
   graph-RAG below plain RAG on factual recall; Mem0g bought +2% with a graph DB.
   Wiki-links between whole notes capture the associative gain. (graph.db stays
   the lightweight typed side-index it is — after B5/B11 fixes — or gets folded
   into frontmatter links entirely.)
2. **Hard deletion / Ebbinghaus forgetting** — measurable harm in the literature;
   decay belongs in ranking, removal = invalidation only.
3. **Memory-OS scheduling layers, multi-agent memory managers, 6-type taxonomies**
   — validated almost solely on LoCoMo, where a grep agent scores 74%.
4. **Parametric/latent memory** — zero portability.
5. **Deep RAPTOR hierarchies** — static-corpus assumption; two levels (notes +
   reflection digests) capture the value.
6. **LoCoMo chasing** — broken benchmark (Zep/Mem0 fiasco; full-context beats
   the "SOTA" products on it). LongMemEval abilities + own production telemetry.
7. **More injection modes / Haiku gating layers** — already rejected; the data
   gate framework (Phase F) answers mode questions empirically.
8. **Body-chunking, aggressive summarize-and-discard** — lossless episodes +
   rich index keys beat compressed content (LongMemEval round-level finding,
   hippocampal-indexing logic).

---

## 5. Effort & sequencing summary

| Phase | Theme | Size | Risk | Depends on |
|-------|-------|------|------|-----------|
| A v2.5 | Repair & prune | S-M | None — pure fixes | — |
| B v2.6 | One engine | M | Benchmark regression (gated) | A |
| C v2.7 | Write path: reconcile + bi-temporal + keys | M-L | Haiku cost/latency at Stop (off critical path) | A; B helps |
| D v2.8 | Profiles + procedural + one consolidator | M | Migration of 2,360 memories (script + dry-run) | C |
| E v2.9 | Tool diet + progressive disclosure | S-M | None | A |
| F cont. | Closed loop | M incremental | None | A.7 |

A and E are independent and could ship together. C is the release that makes memem
state-of-the-art rather than state-of-the-consensus: reconciliation + bi-temporal
supersession + key expansion is precisely the trio that LongMemEval-credible systems
(Zep, Supermemory) win with, implemented here with zero new infrastructure —
the `invalidate_memory` machinery, the `supersedes` extraction, and the FTS index
all already exist; they're just not connected.
