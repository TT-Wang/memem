## Summary

Comparing 23 queries between the **v2.5 heuristic** engine (pre-v2.6) and the **v2.6 unified** engine (v2.6 three-way RRF). Mean Jaccard@10 is **0.30** and mean Jaccard@5 is **0.17**, reflecting the expected divergence when a fundamentally different scoring algorithm replaces the heuristic baseline — partial overlap is normal and desired.
Top-1 stability is **1/23** (4.3%): the new engine surfaces a different best match for most queries, which is the intended consequence of adding FTS as a third RRF channel with usage and scope signals.
14/23 queries (61%) have Jaccard@10 < 0.3 — these show the strongest engine divergence, typically on broad/ambiguous queries where the heuristic file-scan and the RRF ranker weight relevance signals very differently.
Mean absolute rank delta for shared hits is **3.5 positions** — shared memories move substantially in rank, consistent with the new composite scoring (usage×0.15, scope×0.10, link×0.10, importance×0.10) overriding pure keyword proximity.
This report is advisory transparency, not a pass/fail gate. The v2.6 engine is validated separately via the 18-query benchmark (≥74% precision).

## Aggregate statistics

| Metric | Value |
|--------|-------|
| Queries compared | 23 |
| Mean Jaccard@10 | 0.296 |
| Mean Jaccard@5 | 0.167 |
| Top-1 stability | 4.3% (1/23) |
| Mean abs rank delta (shared) | 3.48 positions |
| Queries with Jaccard@10 < 0.3 | 14/23 |

## Per-query breakdown

| # | Category | J@10 | J@5 | Top-1 ✓ | Shared | Abs Δrank | Query |
|---|----------|------|-----|---------|--------|-----------|-------|
| 1 | episodic | 0.18 | 0.00 |  | 3 | 2.7 | What did I work on yesterday related to memem v1.13.0? |
| 2 | episodic | 0.25 | 0.00 |  | 4 | 4.0 | what was the v1.12.0 release scope |
| 3 | episodic | 0.54 | 0.00 |  | 7 | 4.4 | what did we discuss about EverMe extraction mechanism |
| 4 | skill | 0.33 | 0.25 |  | 5 | 3.0 | how to debug a stuck forge worker |
| 5 | skill | 0.33 | 0.11 |  | 5 | 2.8 | how to mine session JSONL files |
| 6 | skill | 0.54 | 0.25 | ✓ | 7 | 2.7 | how do I push to private origin only |
| 7 | case | 0.33 | 0.00 |  | 5 | 3.6 | how did we fix the v1.12.0 active_memory_slice import b |
| 8 | case | 0.25 | 0.25 |  | 4 | 2.8 | what was the resolution for the strudel hh sound not lo |
| 9 | case | 0.33 | 0.11 |  | 5 | 3.2 | how was forge worktree clobber prevented across tiers |
| 10 | cross-scope | 0.25 | 0.43 |  | 4 | 3.2 | what SSH command do I use for my VPS lexie |
| 11 | cross-scope | 0.11 | 0.25 |  | 2 | 2.5 | how does vibereader config validation work |
| 12 | cross-scope | 0.18 | 0.43 |  | 3 | 2.7 | what is the architecture of HFT arb strategy |
| 13 | identity | 0.82 | 0.11 |  | 9 | 4.4 | what is the cortex-plugin tech stack |
| 14 | identity | 0.18 | 0.11 |  | 3 | 4.7 | what does memem do |
| 15 | identity | 0.05 | 0.00 |  | 1 | 4.0 | where is the obsidian vault located |
| 16 | edge | 0.67 | 0.25 |  | 8 | 4.0 | weather forecast for tomorrow |
| 17 | edge | 0.67 | 0.67 |  | 8 | 2.0 | memem |
| 18 | edge | 0.18 | 0.25 |  | 3 | 1.3 | explain the deep philosophy of the 5-signal hybrid rank |
| 19 | episode | 0.11 | 0.00 |  | 2 | 4.0 | memem v2.6 one engine unification |
| 20 | episode | 0.25 | 0.25 |  | 4 | 2.5 | forge worker retry overseer |
| 21 | episode | 0.05 | 0.00 |  | 1 | 9.0 | obsidian vault memory persistence |
| 22 | episode | 0.11 | 0.11 |  | 2 | 3.0 | session start compact index briefing |
| 23 | episode | 0.11 | 0.00 |  | 2 | 3.5 | mining session delta haiku |

## Low-overlap queries (Jaccard@10 < 0.3)

### `What did I work on yesterday related to memem v1.13.0?`
- Jaccard@10: 0.176  |  J@5: 0.000  |  Shared: 3
- Only in v2.5 heuristic: 77193dc9-4 "BUG: empty-string slice result incorrect"; 1336735a-b "memem uses 'bigger-net foundation tier' "; 3d9a176e-f "Memem-forge integration: decision NOT to"; 79e00e27-c "Forge project: JavaScript CLI at TT-Wang"; 86936fb9-4 "memem 18-query benchmark is topic-keywor"
- Only in v2.6 unified: 48c3b954-7 "Phase 4.5 Lens B: 5 concurrency and life"; 5551cecb-d "m3 v1.13.0: auto-recall.sh hook now env-"; 811bb05b-d "Primary memem user interface is `/memem`"; 0f17a069-9 "memem canary: never-elevate junk 5"; 3b10316b-3 "memem security review v1.7.1 → v1.8.0 fi"

### `what was the v1.12.0 release scope`
- Jaccard@10: 0.250  |  J@5: 0.000  |  Shared: 4
- Only in v2.5 heuristic: 614fa2a1-6 "Honest release principle: approve when c"; 1336735a-b "memem uses 'bigger-net foundation tier' "; f28bf26a-5 "What to save in memem vs. what not to sa"; 022e2601-b "Query-targeted context retrieval with to"; dfd9b17b-5 "Frame failure lessons prescriptively, no"
- Only in v2.6 unified: e276fa5e-5 "Benchmark-reality gap: keyword-anchored "; a2f75eba-c "memem v1.13.0: Incomplete backward compa"; cf76276a-0 "Phase 4.5 three-perspective review basel"; 648f426a-4 "Project scope isolation is critical for "; 113fb706-7 "Memem uses deliberate release promotion "

### `what was the resolution for the strudel hh sound not loading`
- Jaccard@10: 0.250  |  J@5: 0.250  |  Shared: 4
- Only in v2.5 heuristic: 145273a4-c "Encode lessons learned prescriptively, n"; 1336735a-b "memem uses 'bigger-net foundation tier' "; 005ef5b4-b "/clear command does not delete JSONL ses"; f28bf26a-5 "What to save in memem vs. what not to sa"; 4307cd1d-7 "If I have shell access, fix env issues d"
- Only in v2.6 unified: 8f0f9434-5 "Vibereader: Article model and feed fetch"; d2da9a1c-b "vibereader feeds module: async httpx + f"; eeadd6c0-2 "Strudel Composition Audio Analysis — Spe"; 19ebba55-8 "Streaming JSON requires --output-format "; eed213c6-9 "HFT Infrastructure Review — Scope Mismat"

### `what SSH command do I use for my VPS lexie`
- Jaccard@10: 0.250  |  J@5: 0.429  |  Shared: 4
- Only in v2.5 heuristic: 005ef5b4-b "/clear command does not delete JSONL ses"; 4307cd1d-7 "If I have shell access, fix env issues d"; 427a080c-2 "User's deployment context — macOS local "; 26a9ba7e-c "Implement telemetry logging to enable da"; 4c9f7ff7-6 "Lexie H1 bug fix: command validation bef"
- Only in v2.6 unified: 5acafb85-a "Lexie user wants to upload and analyze t"; ee620c7f-f "Lexie MVP architecture: Claude CLI subpr"; f525a269-1 "Lexie user preference — split recommenda"; c2e184bf-9 "Lexie architecture: separate project usi"; e5b21e34-f "Vibereader: Push articles to terminal du"

### `how does vibereader config validation work`
- Jaccard@10: 0.111  |  J@5: 0.250  |  Shared: 2
- Only in v2.5 heuristic: 98172bfd-4 "polymarket-weather Project Architecture "; b62afe1f-4 "Project structure: cortex-plugin is cano"; 6996a49d-9 "Forge documentation should prioritize ar"; 1336735a-b "memem uses 'bigger-net foundation tier' "; 5e1e6396-4 "Aggressive filtering is preferred patter"
- Only in v2.6 unified: 3b215793-8 "Vibereader Storage Locations and Config"; 34001654-1 "Vibereader: Storage and configuration co"; 5cc06069-4 "Vibereader agent detection extensibility"; 6688b0af-3 "VibereaderApp interface contracts and co"; 59b2de1b-5 "Vibereader uses local JSON deduplication"

### `what is the architecture of HFT arb strategy`
- Jaccard@10: 0.176  |  J@5: 0.429  |  Shared: 3
- Only in v2.5 heuristic: 7cf27715-1 "Memem Layered Memory Recall (v0.10)"; c2e184bf-9 "Lexie architecture: separate project usi"; 792c4291-3 "Lexie frontend: single-file vanilla HTML"; 6996a49d-9 "Forge documentation should prioritize ar"; 1336735a-b "memem uses 'bigger-net foundation tier' "
- Only in v2.6 unified: 3795a141-6 "Polymarket HFT strategy module structure"; 38219d3b-5 "entry-helpers.mjs: shared module providi"; f4ab7083-6 "Arb Strategy Refactoring: Risk Object Pr"; 2e2c0933-6 "HFT Arb Strategy: getArbProofTemp Math.m"; e1a2eed4-8 "HFT: arb-strategy architecture—pre-compu"

### `what does memem do`
- Jaccard@10: 0.176  |  J@5: 0.111  |  Shared: 3
- Only in v2.5 heuristic: 43d6f00f-9 "Everme's journal-shaped storage beats me"; 1336735a-b "memem uses 'bigger-net foundation tier' "; 005ef5b4-b "/clear command does not delete JSONL ses"; d6ec6af2-e "Structured memory schema: pre-extract en"; b3738e9e-1 "Why Full Conversation Arc Beats Decontex"
- Only in v2.6 unified: c102b100-e "Memem project structure: cortex-plugin ("; 02340c9f-8 "Codex Memem migration uses shared claude"; 7cf27715-1 "Memem Layered Memory Recall (v0.10)"; 5c3d1e16-c "memem canary: always-reachable identity "; 811bb05b-d "Primary memem user interface is `/memem`"

### `where is the obsidian vault located`
- Jaccard@10: 0.053  |  J@5: 0.000  |  Shared: 1
- Only in v2.5 heuristic: 02340c9f-8 "Codex Memem migration uses shared claude"; ba0d2e6b-a "CLAUDE.md as Project Instruction Source "; 7b26838d-6 "Data Structure Coupling Bug Pattern — In"; cd2a4d7b-9 "Arb strategy filters stale bins using is"; 637c49d5-9 "TOCTOU race in multi-file index loading "
- Only in v2.6 unified: eda7d9f5 "Obsidian brain vault location"; e8eb48c1-2 "obsidian-vault"; dcacc465-2 "Obsidian brain vault structure and organ"; 3ace8231-6 "Obsidian wiki-links convention for memor"; b878166d-1 "Separate Obsidian vault projects via MEM"

### `explain the deep philosophy of the 5-signal hybrid ranking custom re-ranking scheme`
- Jaccard@10: 0.176  |  J@5: 0.250  |  Shared: 3
- Only in v2.5 heuristic: 5c3d1e16-c "memem canary: always-reachable identity "; 663b0fcc-4 "Use production recall telemetry instead "; 70adce30-b "Keep auth constraint"; 07bd601c-a "forge-mcp-server: stdio communication pr"; 327e2299-7 "Reranker upgrade options for memem: mxba"
- Only in v2.6 unified: f7702a98-a "Poker Hand Evaluation – Rank + Tiebreake"; 75622913-5 "Vector database skepticism: hybrid BM25 "; 356ced0a-5 "RRF scale parameter is high-impact ranki"; 76429bec-4 "tailor-reader: Topic Filtering & Ranking"; b9635878-8 "memem v2.6.0 'One Engine' Architecture —"

### `memem v2.6 one engine unification`
- Jaccard@10: 0.111  |  J@5: 0.000  |  Shared: 2
- Only in v2.5 heuristic: a3ac5ce1-9 "SSH config ServerAliveInterval: keep tun"; 9a9a6661-c "Push memem changes to both origin and pu"; 648f426a-4 "Project scope isolation is critical for "; 673bb825-5 "Lexie project identity and scope"; 97766c8c-5 "Forge mandatory rules: Phase 1b approval"
- Only in v2.6 unified: a40ba857-9 "memem v2.6.0 Quality Gates and Implement"; 6ed73345-7 "memem canary: always-reachable conventio"; 4d59e5a1-3 "Lexie architecture: personalized recomme"; 5c3d1e16-c "memem canary: always-reachable identity "; b0d67707-1 "memem canary: never-elevate junk 4"

### `forge worker retry overseer`
- Jaccard@10: 0.250  |  J@5: 0.250  |  Shared: 4
- Only in v2.5 heuristic: b0d67707-1 "memem canary: never-elevate junk 4"; bf08025e-a "Forge auto-WIP commits between tiers pre"; 4304b6c8-3 "Forge: Prioritize CLI skills-first frami"; a925f55b-6 "Auto-WIP-commit between forge tiers prev"; 70adce30-b "Keep auth constraint"
- Only in v2.6 unified: fd2ecf28-f "Forge principle: Constraint-first guidan"; a549dcf0-c "Forge Session Resumability: State Snapsh"; eedaacbc-5 "Forge documentation: emphasize CLI comma"; b2907a06-0 "Forge Orchestration Framework: Three-Age"; d3b092bf-7 "Lock file stale-check race: PermissionEr"

### `obsidian vault memory persistence`
- Jaccard@10: 0.053  |  J@5: 0.000  |  Shared: 1
- Only in v2.5 heuristic: 63cfffcb-a "Forge work requires personal Obsidian va"; bb787480-a "Obsidian-brain vault auto-push every 30 "; 3dc76d69-5 "Final project structure: memem v1.0.0 wi"; a5350041-2 "ChromaDB removed from Cortex — Obsidian "; ed4e4157-7 "Bi-temporal invalidation (valid_at/inval"
- Only in v2.6 unified: e8eb48c1-2 "obsidian-vault"; dcacc465-2 "Obsidian brain vault structure and organ"; eda7d9f5 "Obsidian brain vault location"; ff8b3baf-b "Cortex 0.3 — ChromaDB Removal & Single-S"; 2cad48a0-6 "User prioritizes memory deduplication co"

### `session start compact index briefing`
- Jaccard@10: 0.111  |  J@5: 0.111  |  Shared: 2
- Only in v2.5 heuristic: d5edca25-4 "Instruction suggestion: ## Layered memor"; 54a19465-b "L0 layer metadata corrupted by fallback "; 5e1e6396-4 "Aggressive filtering is preferred patter"; a11d31bb-d "Always fetch memem project memories when"; 4dda33ae-a "Cortex Mining: Hourly Cron Required—Sess"
- Only in v2.6 unified: 306b0717-b "Forge integration not needed — focus on "; c52fb1b2-9 "Audit process: Extract explicit user cor"; 3dc76d69-5 "Final project structure: memem v1.0.0 wi"; fe76c78f-d "memem layer 0 (L0) contains project iden"; e2af7028-1 "User prefers richness and accuracy over "

### `mining session delta haiku`
- Jaccard@10: 0.111  |  J@5: 0.000  |  Shared: 2
- Only in v2.5 heuristic: 7cf27715-1 "Memem Layered Memory Recall (v0.10)"; 3d9a176e-f "Memem-forge integration: decision NOT to"; 11090cce-2 "Claude Code logging: JSONL storage, 30-d"; 5e1e6396-4 "Aggressive filtering is preferred patter"; 4dda33ae-a "Cortex Mining: Hourly Cron Required—Sess"
- Only in v2.6 unified: 9fc53e74-5 "Session Mining Performance: Noise Filter"; 046d9dc8-b "Mining: detect and skip agent/module ses"; fd5c1c52-7 "Mining Noise Filter: Keep Human+Assistan"; b3738e9e-1 "Why Full Conversation Arc Beats Decontex"; 6217b7b9-4 "Project tag detection from cwd to avoid "
