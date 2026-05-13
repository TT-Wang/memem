# Changelog

All notable changes to memem (formerly Cortex) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note:** the project was renamed from `cortex` to `memem` in v0.7.0.
> Pre-v0.7.0 entries below describe what was called Cortex at the time —
> they have been left untouched as historical record. See the v0.7.0 entry
> for the rename details, backward-compat strategy, and migration path.

## [1.8.0] - 2026-05-13 — Persistent slice daemon (no more cold-start)

The architectural win promised in v1.7.2's notes: every UserPromptSubmit hook
fire on memem v1.7.x cold-started a Python interpreter and loaded ~500MB of
sentence-transformers from scratch (5-10s, ~30% CPU spike). With N concurrent
Claude Code sessions, that meant N×500MB of redundant RAM usage and 5-10s of
extra latency on every prompt cycle. v1.8.0 makes this single-load.

### Added — m7: `memem.slice_daemon` + `memem.slice_client`

Long-running daemon process that holds the embedding model warm and serves
slice-generation requests over a Unix socket at `~/.memem/slice.sock`.

**Architecture:**
- `memem.slice_daemon` — daemon process. Daemonizes via double-fork, acquires
  `~/.memem/slice.lock` (fcntl, with stale-PID detection mirroring m3's miner
  lock pattern), binds Unix socket at `~/.memem/slice.sock` with **0600
  perms** (umask 0o177 around bind — privacy: requests carry full query
  content as JSON payload).
- `memem.slice_client` — thin client used by hooks. Single function
  `try_slice_via_daemon(...) -> str | None`. Returns slice on success,
  `None` on ANY failure (socket missing, refused, timeout, protocol error).
  Never raises.
- `hooks/auto-recall.sh` — `slice_helper()` now tries the daemon first
  (5s timeout), falls back to the existing `python3 -m memem.server slice`
  subprocess path on any failure. Fallback path is **bit-for-bit unchanged**
  — daemon is purely an accelerator.

**Protocol:** newline-delimited JSON, one request per connection.
- Request: `{"query": "...", "scope": "...", "session_id": "...", "cwd": "...", "task_mode": "...", "use_llm": false}`
- Response: `{"ok": true, "slice": "...", "elapsed_ms": 123}` or `{"ok": false, "error": "...", "elapsed_ms": 123}`

**CLI:** `python -m memem.slice_daemon start | stop | status | run`. `start`
double-forks; `run` stays foreground (for systemd / debugging).

**Resource controls (Phase 4.5 hardened):**
- Per-request timeout: 25s (enforced via `_handle_with_timeout` watcher
  thread; not just declared — the dead-code bug from initial m7 worker
  was caught by final review and fixed before release).
- Concurrency cap: max 8 in-flight requests; overload returns
  `{"ok": false, "error": "overloaded"}`.
- **WORKER_THREADS=1 in v1.8.0.** `active_slice_engine`'s tournament-cache
  write is not thread-safe (file r/w/clobber). Single-worker is the safe
  ship. Still ~10x faster than the cold-start subprocess path because the
  embedding model stays warm. v1.8.1 candidate: file-lock the cache and
  bump workers to 4.

**Anti-recursion:** daemon sets `MEMEM_HOOK_DISABLE=1` at startup so any
subprocess it spawns inherits it. Belt-and-suspenders with v1.7.2's m12.

**Sentence-transformers graceful degradation:** if the optional dep is
unavailable, daemon starts in degraded mode (logs `model_load_degraded`
warning) and serves the no-embedding code path. No crash on `ImportError`.

**Logging:** structlog at INFO to `~/.memem/slice-daemon.log` (separate from
`miner.log`). Logs **never include query content** (privacy). Heartbeat at
`~/.memem/slice-daemon.heartbeat` written per successful request.

**Testing:** 9 new tests in `tests/test_slice_daemon.py` covering: missing
socket → None, connect refused → None, request/response round-trip,
concurrent requests, request timeout enforcement, stale-socket cleanup,
anti-recursion env var, etc. The overload-rejection test is `pytest.skip`'d
in v1.8.0 (single-worker means it's not reachable); will re-enable in v1.8.1.

### Notes

- `miner_daemon.py` deliberately untouched in this release. Slice daemon and
  miner are separate processes with separate failure modes.
- m6 `--status` does not yet surface slice-daemon health. v1.8.1 candidate.
- `pyproject.toml` and `.claude-plugin/plugin.json` bumped to 1.8.0.

## [1.7.3] - 2026-05-13

Hot-fix for v1.7.2's m4 dep pin. The original pin (`ruff~=0.6.0`, `mypy~=1.11.0`)
matched the legacy `>=` lower bounds in the manifest, but local dev was running
ruff 0.15.x and mypy 1.20.x. CI installed the pinned older versions which
flagged code that the dev environment did not (TypedDict literal-key errors in
`activation.py`, `Optional[str]` append in `consolidation.py`). Pin both to
match local dev: `ruff~=0.15.0`, `mypy~=1.20.0`. CI now passes for v1.7.2's
full change set.

## [1.7.2] - 2026-05-13 — Stability batch (anti-recursion, self-healing, observability)

Triggered by a real production incident on this VPS: load average pegged at 15
on a 4-core box, RAM at 5.2 GB / 7.5 GB used + 2.3 GB swap. Diagnosis surfaced
**five operational bugs** working together to make memem unstable. v1.7.2 fixes
each one and adds defense-in-depth.

### Fixed — m12: Recursive hook fan-out (load=15 root cause)

Every `claude -p --model haiku` call from memem (knowledge extractor, tournament
judge, merge, refine, consolidate, activate) ran as a regular Claude Code session.
That session's `UserPromptSubmit` + `Stop` hooks then fired memem's own
`auto-recall.sh` and `post-stop-attribution.sh` — which spawned MORE
`claude -p haiku` calls (slice tournament + extractor) — which fired hooks again,
fan-out exponentially.

- **All 10 `claude -p` call sites now set `MEMEM_HOOK_DISABLE=1`** in the
  subprocess env: `mining.py` (3 sites), `active_slice_engine.py`, `playbook.py`,
  `assembly.py`, `consolidation.py`, `dreamer.py` (2 sites), `activation.py`.
- **All 3 hooks early-exit on `MEMEM_HOOK_DISABLE=1`**: `auto-recall.sh`,
  `post-stop-attribution.sh`, `session-start.sh`. Check is right after
  `set -euo pipefail`, before any work.
- **All `claude -p` calls use `start_new_session=True`** so Python's
  `subprocess.run(timeout=N)` cleanup kills the child's entire process group.
- **Hooks use `setsid timeout --kill-after=5 N`** so when the outer hook timeout
  fires, the whole subtree dies instead of orphaning to PPID=1.

### Fixed — m3: Miner self-healing trio

Three operational bugs we hit in the same hour today:

- **Heartbeat went 13 hours stale while the daemon was actively mining.**
  `_write_heartbeat()` was only called once per outer `while True:` iteration,
  but the inner `for jsonl_path in sessions:` loop processes thousands of
  sessions sequentially at ~150s each. With a long queue, the heartbeat stayed
  frozen for hours while the daemon worked normally. Now also written at the
  top of the inner loop, bounding freshness to per-session duration regardless
  of queue depth.
- **`miner-wrapper.sh stop` lied.** Sent SIGTERM and reported success without
  verifying. Today's daemon survived this and required manual `kill -9`. Now
  polls `kill -0 $PID` after SIGTERM, escalates to SIGKILL after 5s, and
  returns nonzero if the process is still alive.
- **Stale lock file blocked restart after abnormal exit.** When the daemon died
  from SIGKILL/OOM/segfault, `~/.memem/miner.global.lock` lingered with the
  dead PID; the next wrapper start saw the lock and refused. Now
  `_acquire_global_lock` checks if the recorded PID is still alive via
  `os.kill(pid, 0)`; if dead, removes the stale file and retries once.

### Added — m5: Orphan-process reaper (defense in depth)

Even with m12's guard in place, future code paths may forget. New
`memem/reaper.py` exposes `reap_orphan_haiku_procs(min_age_seconds=120)` that
walks `/proc`, finds `claude -p --model haiku` processes with `PPID=1` (adopted
by init) older than 120s, and SIGKILLs them. The miner outer loop calls it once
per iteration; logs `orphan_reaper_sweep` when reap_count > 0.

Today's manual cleanup (before this code existed) reaped 14 such orphans at
once and freed 2.8 GB of RAM. v1.7.2 prevents the accumulation rather than
catching it after the fact.

### Changed — m6: `--status` truth revamp

Today's `--status` falsely reported "Daemon: running" for a zombie process and
"Heartbeat: 145400s ago" without flagging it. New 5-section output:

1. **Process tree** — wrapper vs daemon vs orphan haiku procs with ages + RSS
2. **Heartbeat** with explicit bands: ✓ OK <60s | ⚠ WARN <300s | ✗ FAIL ≥300s
3. **Lock file** — owner PID + ✓ alive vs ✗ STALE
4. **Recent mining activity** — last 5 sessions with duration + memories_saved
5. **Recursion-orphan counter** — total reaped since daemon start

Backward-compatible: legacy `Daemon:`/`Heartbeat:`/`Lock:`/`Circuit breaker:`
labels still emitted at the top so existing scripts grepping for them keep
working. Logic extracted to new `memem/status.py` (`render_status() -> str`)
for testability.

### Changed — m4: CI dependency pinning (no more release-line CI breakage)

Every release since v1.5.0 has failed CI on ruff lint rules added in newer ruff
versions — `pyproject.toml`'s `[project.optional-dependencies].dev` declared
`ruff>=0.6` (lower bound only) so CI pulled latest, which silently added new
rules to already-selected families (C4, SIM). Today's caba5e2 fixed 4 such
errors; the next ruff release will introduce more.

- `ruff>=0.6` → `ruff~=0.6.0` (compatible-release: 0.6.x patches yes, 0.7+ no)
- `mypy>=1.11` → `mypy~=1.11.0` (same rationale)

### Notes

- New tests: `test_subprocess_recursion_guard.py` (10 tests),
  `test_miner_self_healing.py` (4 tests), `test_reaper.py` (8 tests),
  `test_status_output.py` (9 tests). Total: 31 new tests, all passing.
- 3 pre-existing tests in `test_v010_fixes.py` had `fake_run` signatures
  that didn't accept the new `env=` and `start_new_session=` kwargs; updated
  to `**kwargs` to be forward-compatible.
- This release is a **safety patch**; the architectural win (persistent slice
  daemon to eliminate ~500MB sentence-transformers cold-start per UserPromptSubmit
  hook fire) is shipping as v1.8.0.

## [1.7.1] - 2026-05-09

Patch release applying Phase 4.5 final-review followups from v1.7.0.

- **`consolidation.py` Haiku timeout now honors `MEMEM_HAIKU_TIMEOUT`.** Was
  hardcoded to 120s, contradicting the v1.7.0 CHANGELOG claim that all Haiku
  calls were raised to 180s. Now imports `HAIKU_TIMEOUT_SECONDS` from
  `memem.mining` so the env var works on `--consolidate` runs too.
- **`mining.py` `raise TransientMiningError(str(exc)) from exc`** at lines 246
  and 520. The earlier raises dropped `__cause__`, making the
  `isinstance(exc.__cause__, subprocess.TimeoutExpired)` check at line 1109
  dead code. The string-fallback path was catching timeouts in practice, but
  the dead `isinstance` branch was a maintenance hazard. Behavior unchanged;
  code clarity improved.

## [1.7.0] - 2026-05-09 — Book patterns + miner hardening

Triggered by a real miner-death incident on 2026-05-08: the miner hit a huge
active dev-session JSONL, the Haiku CLI subprocess timed out at 120s, and the
classifier marked the timeout as "permanent API/auth error" — the daemon refused
to restart. Manual recovery was needed (skip the session, restart). v1.7 closes
that failure mode and ships three patterns from Antonio Gulli's *Agentic Design
Patterns* (Springer 2025).

### Fixed — m1: Miner daemon hardening

- **Subprocess timeouts reclassified as TRANSIENT.** `subprocess.TimeoutExpired`
  and `"timed out after"` patterns no longer trigger the permanent-error path.
  The miner keeps going on other sessions instead of dying entirely.
- **Per-session timeout cap.** New `timeout_failures` column tracks per-session
  Haiku CLI timeouts. After `MEMEM_MAX_SESSION_TIMEOUTS` (default 3), the
  session is marked COMPLETE with a skip message and the miner moves on.
  Prevents a single huge session from blocking the whole queue indefinitely.
- **Startup stuck-state cleanup.** At daemon start, sessions with
  `STATUS_IN_PROGRESS` and `updated_at` older than `MEMEM_STUCK_CLEANUP_HOURS`
  (default 2) are reset to `STATUS_FAILED` with attempts incremented.
  Cleans up the 938 stuck rows the triggering incident left behind.
- **Haiku CLI timeout 120s → 180s** (configurable via `MEMEM_HAIKU_TIMEOUT`).
  Gives huge sessions more headroom before the per-session cap fires.

### Added — m2: Tournament tie-break ranking (book pp. 336-337)

When the Active Memory Slice's top-K weighted-sum scores are within 10% of
each other (a "tie zone"), the ranking is essentially noise. Tournament
mode runs up to 6 pairwise Haiku judges to break ties on relevance to THIS
query. Cached by (query-fingerprint, candidate-set-hash) at
`~/.memem/.tournament-cache.json` with 24h TTL. Disabled via
`MEMEM_TOURNAMENT_ENABLED=false`. Per-call timeout configurable via
`MEMEM_TOURNAMENT_TIMEOUT` (default 30s).

### Added — m3: Episodic consolidation + contradiction detection (book p. 149)

New `memem/consolidation.py`. Run via `python3 -m memem.server --consolidate`
(recommended weekly cron). Clusters L2 memories by embedding cosine
similarity (threshold 0.85, min cluster size 3); for each cluster, one Haiku
call merges into a canonical memory and flags any internal contradictions.
Source memories are marked deprecated with `replaced_by: <canonical_id>`
(audit trail preserved). Contradiction-flag memories surface in the
SessionStart slice for user review.

### Added — m4: Procedural memory layer (book pp. 146-147)

Mining now runs a SECOND Haiku pass after knowledge extraction: given the
session transcript + current CLAUDE.md + any user corrections, propose
0-3 instruction rewrites. Saved as `kind:procedural-suggestion` memories
with `status: pending_review`. SessionStart slice surfaces up to 3 oldest
pending suggestions at the top of the briefing for user review (NOT
auto-applied — explicit triage). Auto-archives after 7 days
(`MEMEM_PROCEDURAL_TTL_DAYS`). Dedup'd against existing pending suggestions
so recurring corrections don't fill the queue.

### Fixed — post-tier-2 review

- m3: `superseded_by` was set in memory dict but `_write_obsidian_memory`
  never persisted that key — the audit-trail field was silently dropped.
  Fixed by using the existing `replaced_by` schema field that IS persisted,
  plus adding `supersedes:<id8>` tags so the graph_index can build edges.
- m3: contradiction-flag memories were saved but never surfaced in
  SessionStart. Added `_render_contradiction_flags_block` to the slice.
- m1: `tests/test_miner_auth_crush.py` parametrize moved timeout entries
  from FATAL to retryable to match the m1 reclassification.
- m4: procedural-suggestion dedup gap — same correction across N sessions
  filled the queue with N copies. Now skipped when score > 0.6 against an
  existing pending suggestion.
- m2: tournament timeout env-overridable via `MEMEM_TOURNAMENT_TIMEOUT`.

### Stats

- 633+ tests pass (was 633 in v1.6.1)
- 4 new test files: `test_consolidation.py` (10 tests), `test_procedural.py` (5 tests),
  expanded `test_miner_failure_persistence.py` and `test_active_slice.py`
- mypy clean across 52 source files
- ruff clean (added SIM117 + RET505 to ignore list — stylistic preference)
- Triggering incident: miner-death on session 9612f54c-bbd5 (2026-05-08).
  Manually skipped + restarted to recover; v1.7 prevents recurrence.

## [1.6.1] - 2026-05-08

Patch release applying the four warnings from v1.6.0's final release review.

- **Stop hook lock dir uses `$MEMEM_DIR`** instead of hardcoded `$HOME/.memem`.
  Legacy users with `MEMEM_DIR` set elsewhere (e.g. via `CORTEX_DIR`) had
  `.stop-timestamps` and `mine-on-stop.log` written to the wrong tree, silently
  defeating the H-2 concurrency guard for that configuration.
- **Removed dead `_update_compaction_timestamps` function** in `auto-recall.sh`.
  The m2 hardening commit replaced it with a broader lock; the narrow helper
  was orphaned and misled readers into thinking there was a two-level locking
  strategy.
- **Fixed misleading docstring on `_parse_jsonl_session_pairs`** in
  `transcripts.py`. It is a standalone parser (needed for raw `tool_result`
  blocks), not a wrapper over the canonical `parse_jsonl_session` as the prior
  docstring claimed.
- **Fixed two-open TOCTOU in `_extract_conversation_from_offset`.** File size
  is now measured AFTER `parse_jsonl_session` returns, so the stored
  `file_size_at_read` cannot under-count when the file grows mid-parse.
  Previously caused harmless-but-redundant Haiku calls on re-parsed tails.

## [1.6.0] - 2026-05-08

### Cleanup release — applied audit findings (Bundles A, B, C, E, F)

After v1.5.1, a 5-reviewer audit surfaced 30 findings ranked BLOCKER/HIGH/MEDIUM/LOW.
v1.6.0 ships fixes for everything except Bundle D (test coverage for server.py /
recall.py / operations.py — deferred to v1.7).

### Fixed — Bundle A (dedup hardening)
- **BLOCKER B-1:** `save_compaction_checkpoint` now calls `_is_duplicate()` before
  `_save_memory()`. Two close-spaced UserPromptSubmit hooks at the compaction
  threshold no longer both write a checkpoint memory.
- **HIGH H-3:** `_import_file` JSON dict, JSON list, and plaintext branches now
  call `_is_duplicate()` (markdown branch already did). Re-importing the same
  file is now a no-op as the docstring claimed.
- **MEDIUM M-4:** `_commit_new_memory` (delta replay path) now calls
  `_is_duplicate()`.
- **MEDIUM M-1:** Removed duplicate `_normalized_ids()` helper from
  `delta_commit.py` (already importable from `delta_policy.py`).

### Fixed — Bundle B (concurrency races)
- **HIGH H-1:** `working_memory.update_section` now wraps the read-modify-write
  in `fcntl.LOCK_EX` on a sidecar lockfile. Concurrent post-stop hooks from
  multiple Claude Code windows no longer silently lose `current_task` /
  `last_3_actions` updates.
- **HIGH H-2:** `post-stop-attribution.sh` uses atomic `mkdir` as the
  `.stop-timestamps` lock. Concurrent Stop events for the same session no
  longer doubly invoke `mine_session_delta` (no more 2× Haiku cost).
- **MEDIUM M-8:** `auto-recall.sh` compaction-checkpoint write now holds the
  flock across the entire cooldown-check + save + timestamp-write window
  (initial fix only locked the timestamp write).
- **MEDIUM M-9:** `mine_session` now increments the `attempts` counter BEFORE
  writing `STATUS_IN_PROGRESS` so SIGKILL-mid-mine no longer re-queues forever.
  All STATUS_COMPLETE branches also write the bumped count for diagnostic accuracy.
- **MEDIUM M-6:** `working_memory.py` now imports canonical `MEMEM_DIR` from
  `memem.models` instead of re-implementing path resolution.

### Changed — Bundle C (attribution stub made explicit)
- **HIGH H-4:** `judge_score()` now logs a one-time INFO warning that the
  LLM-judge is a stub. Behavior unchanged (still returns None) — the
  `aggregate_signals` pipeline continues to use embedding similarity +
  citation-regex signals. Real LLM-judge integration remains pending.

### Changed — Bundle E (structural consolidation)
- **HIGH H-5:** Canonical `parse_jsonl_session` in `memem/transcripts.py`
  replaces 4 ad-hoc parsers in mining, compaction, and active-slice paths.
  Role-filtering rules now live in one place.
- **MEDIUM M-2:** `now_iso()` consolidated into `memem/models.py`. `lessons.py`,
  `eval_capture.py`, and `dreamer.py` now import the canonical helper.
- **MEDIUM M-3:** `parse_iso_dt()` added to `memem/models.py`. `decay.py`,
  `active_slice_engine.py`, and `recall.py` now use the canonical parser.
- **MEDIUM M-15:** Removed dead `kind:decision` tag-prefix branch from
  `compaction.py` and `active_slice_engine.py` (no writer ever emitted that).

### Fixed — Bundle F (quick wins)
- **MEDIUM M-5:** `hooks/session-start.sh` now imports `OBSIDIAN_MEMORIES_DIR`
  from `memem.models`, so legacy `CORTEX_OBSIDIAN_VAULT` users see the right
  memory count in the session-start banner.
- **MEDIUM M-10:** `miner_daemon.py` asserts `HARD_RETRY_CAP >= MAX_SESSION_FAILURES`
  at module load to catch operator misconfigurations early.
- **MEDIUM M-13:** `storage.py` user-visible warning now points at the real
  `~/.memem/miner.log` instead of the stale `~/.cortex/miner.log`.
- **LOW L-1:** Removed unused `TOPIC_SHIFTS_LOG` constant from `models.py`.
- **LOW L-7:** `lessons.py` `LESSONS_DIR` now resolves via `_lessons_dir()` so
  importlib.reload() in tests picks up monkeypatched vault paths.

### Deferred — Bundle D (to v1.7)

The audit also surfaced thin test coverage on three large modules:
- `server.py` (1090 lines) — MCP dispatch layer with only an import smoke test
- `recall.py` (775 lines) — primary query engine, no dedicated tests
- `operations.py` (164 lines) — all import paths, no dedicated tests

Test coverage for these is deferred to v1.7 as its own focused batch.

### Stats

- 632 tests passing (was 609 in v1.5.1, +23 net)
- mypy clean across 51 source files
- ruff clean

## [1.5.1] - 2026-05-08

Patch release applying the four warnings from v1.5.0's final release review.
See the "Post-release (v1.5.1) — final-review followups" section under v1.5.0
below for the changes.

## [1.5.0] - 2026-05-08

### Added — boundary-crossing batch (m1 m2 m3 m4)

Four modules that push memem past single-session boundaries — sessions that
know where they left off, compaction that survives context exhaustion, recall
that crosses vault registries, and mining that starts the moment a session ends.

- **m1 — SessionStart dense briefing:** new `generate_session_start_slice()`
  produces a structured ~2K-token "where we left off" briefing: working memory
  + recent decisions + active arcs + L0 anchors + recent compaction checkpoint.
  Replaces the encyclopedic SessionStart dump. Configurable via
  `MEMEM_SESSION_START_BUDGET`. ImportError-safe fallback to
  `generate_prompt_context`.

- **m2 — Compaction-survivor checkpoint:** new `memem/compaction.py` polls
  context-usage during UserPromptSubmit; when estimated usage reaches
  `MEMEM_COMPACTION_THRESHOLD` (default 0.80) of the 200K-token window,
  snapshots working memory + last 5 decisions + open tensions + last 5
  code-changes as a `kind:compaction-checkpoint` memory. 60-min per-session
  cooldown. SessionStart slice (m1) re-injects these post-compaction.

- **m3 — `memory_remind` MCP tool (cross-vault recall):** new
  `memem/cross_vault.py` + `memem/vault_registry.py` + new
  `memory_remind(local_context_summary, max_results=3)` MCP tool. Searches
  across all vaults registered in `~/.memem/vaults.json` (default-on: synthetic
  single-vault entry when no registry file exists). Each hit includes a
  `why_relevant` rationale with similarity score.

- **m4 — Mining latency on session-end:** Stop hook now triggers
  `mine_session_delta(session_id)` immediately on first stop per session (with
  marker-file dedup, `MEMEM_MINE_TIMEOUT` guard, 60s default). Daemon backoff
  cap tightened to 900s when retryable sessions exist (was 1800s); falls back
  to 1800s when all sessions are terminal.

### Fixed — post-merge hardening

- **Hook shell-interpolation surface:** session_id now passed via env var
  (`MEMEM_STOP_SESSION_ID`) rather than shell-interpolated in `python3 -c`,
  eliminating any injection surface from session path characters.
- **m1 budget enforcement:** join separator cost is now accounted for when
  enforcing `MEMEM_SESSION_START_BUDGET`, preventing off-by-one overruns on
  dense briefing sections.

### Post-release (v1.5.1) — final-review followups

- **Stop hook timeout:** raised from 35s to 100s to give `mine_session_delta`
  room to finish its Haiku call on cold hosts (35s was cutting mining short
  before it could report success). Mine-on-stop now reliably finishes
  per-session instead of falling back to the daemon's next poll.
- **Budget env-var alias:** `generate_session_start_slice` now honors the
  legacy `MEMEM_SESSION_START_PROMPT_BUDGET` env var as a fallback for
  `MEMEM_SESSION_START_BUDGET`, so pre-m1 configs keep working.
- **Daemon log path:** `start_daemon` failure message now points at the real
  `~/.memem/miner.log` instead of the stale pre-rename `~/.cortex/miner.log`.

### Stats

- 4 new modules: `memem/compaction.py`, `memem/cross_vault.py`,
  `memem/vault_registry.py`, `generate_session_start_slice()` in session-start
- Both hooks updated (Stop hook, UserPromptSubmit hook)
- mypy clean
- ruff clean

## [1.4.0] - 2026-05-07

### Added — six-module batch closing the recall→action gap

After honest self-assessment of where memem helped vs. didn't across a 9-day,
40-turn debug arc, six modules ship together to attack the gaps:

**Tier 1 — directly attacks observed gaps:**

- **m1: Working-memory surface (live state, not encyclopedia).** New file
  `memem/working_memory.py` plus `~/.memem/working_memory.md` — a 5-section
  scratchpad (`current_task`, `active_hypothesis`, `last_3_actions`,
  `stuck_on`, `decided_this_session`). Stop hook updates `current_task` and
  `last_3_actions` from transcript user messages on every assistant turn.
  UserPromptSubmit prepends working memory BEFORE the static slice in
  `additionalContext`. Different shape, different purpose: the slice is an
  encyclopedia; working memory is a scratchpad.

- **m2: Incremental session mining.** Track `offset_bytes` per session in
  state. Each mining attempt reads from offset to EOF, sends to Haiku,
  advances offset only on success. Active sessions get mined incrementally
  without ever processing >100KB at once — the paradox where the most-needed
  session was the one most-rejected by the mining timeout is resolved.
  HARD_RETRY_CAP × incremental mining: capped sessions can re-enter when
  new content has appeared past the stored offset, but never re-mine the
  same content that already failed. Migration safety: pre-v1.4.0 capped
  rows have `offset_bytes=0`; the size-fallback in `session_is_terminal`
  treats those as "fully mined through stored size" (preserves v1.2.2's
  cap protection through the upgrade path).

- **m3: Slice de-duplication across turns.** SHA256 the assembled slice
  body; per-session hash files at `~/.memem/.last-slice-hashes/<sid>.hash`.
  Identical slice across turns → emit `[Active Memory Slice unchanged from
  previous turn — see slice above]` placeholder instead of the full
  ~3-4K body. Working memory is appended on every emit (state changes
  even when slice doesn't); dedup hash is keyed on slice body alone so
  working memory doesn't bust the cache. ~70% token reduction on
  stable-topic sessions.

**Tier 2 — closes loops we built but lie dormant:**

- **m4: Cluster summarization (real, not stub).** `find_cluster_summaries`
  in `memem/dreamer.py` now does the work: cosine-cluster L2 memories by
  project, threshold 0.7 (was 0.85), min cluster size 5 (was 3). For
  qualifying clusters, one Sonnet call synthesizes a single pattern memory.
  Dry-run by default; `apply_diff(dry_run=False)` materializes patterns.
  Constituent memories get a `clustered_into` pointer (in-memory only for
  v1.4.0; persistence to Obsidian frontmatter is v1.5.0 scope).

- **m5: Semantic citation, not just regex.** `citation_match` now has a
  third OR-branch: `embedding_similarity(memory_essence, response_text) >
  0.6`. Catches semantic citations ("the auto-save constraint" without
  literal title match) that the literal-id/title heuristic missed.
  Backwards compatible: existing 3-arg callers unchanged; `memory_essence`
  is an optional 4th arg.

- **m6: Project-scope precision in candidate ranking.** When a candidate's
  `project` matches the active scope, score × 1.5 (capped at 1.0). When
  it doesn't match (and project is not `general` or empty), score × 0.7.
  L0 anchors and cross-cutting `general` memories unchanged. Less noise
  per slice, signal density up.

### Fixed — final-review catches before ship

- **HARD_RETRY_CAP migration regression** — pre-v1.4.0 capped sessions had
  `offset_bytes=0` after the m2 ALTER TABLE migration, which would have
  re-entered them all into the very retry loop v1.2.2's cap was designed
  to prevent. `session_is_terminal` now treats `offset_bytes=0` on a row
  with stored `size` as "fully mined through size", preserving cap
  protection through the upgrade path.
- **Cluster Sonnet subprocess** — added `start_new_session=True` for
  signal isolation, matching the contradiction-judgment subprocess
  pattern (a SIGTERM to the dreamer no longer reaches the Sonnet call).

### Stats

- 14 new tests added (5 m3 + 7 m1 + 7 m2 + 3 m5 + 3 m6 + 8 m4 + 1 migration guard)
- 85 tests pass across the 6 module-specific files
- mypy clean across 48 source files
- Both hooks shell-syntax-clean

### Wire compatibility

- `MEMEM_MINER_HARD_RETRY_CAP` and `MEMEM_MINER_SETTLE_SECONDS` env vars
  unchanged from v1.2.2
- Eval-replay capture (v1.3.0) keeps capturing through these new modules
- Existing `~/.memem/.last-slice-hash` (singular) is untouched; m3 uses
  `.last-slice-hashes/` directory side-by-side
- `clustered_into` field is in-memory only; existing callers unaffected

## [1.3.0] - 2026-05-07

### Added — eval-replay regression-test loop

Inspired by gbrain's `eval capture/export/replay` pattern. Closed-loop
regression test for retrieval quality, with no ground-truth labels needed:

- **Capture (opt-in via `MEMEM_EVAL_CAPTURE=1`)** — every `memory_search`
  and `memory_recall` call writes one NDJSON row to
  `~/.memem/eval_captures.jsonl`: query (PII-scrubbed), retrieved
  memory_ids, retrieval mode, scope, top-K, latency_ms, schema_version.
  Off by default — no surprise data accumulation.
- **PII scrub** — emails, JWTs, Anthropic/OpenAI keys, GitHub PATs, AWS
  keys, Slack tokens, bearer tokens, phones, SSNs, Luhn-verified credit
  cards. Order-aware: structured tokens first, then CC, then phone (so
  16-digit cards don't get partially eaten by the phone regex).
- **Export** — `memem eval export [--since 7d] [--out PATH]` snapshots
  captures to NDJSON. `--since` accepts `7d` / `24h` / `30m` / raw seconds.
- **Replay** — `memem eval replay --against baseline.ndjson [--k 5]`
  re-runs every captured query against current code and reports:
  - `mean jaccard@k` between captured and current retrieved IDs
  - `top-1 stability` (fraction of queries where #1 result matches)
  - `mean latency Δ` (current - baseline, ms)
  - worst-N regressions (lowest jaccard) for inspection
- **Status** — `memem eval status` shows whether capture is enabled,
  current capture file, row count, oldest/newest timestamps.

The loop: capture for ≥7 days during normal use → export baseline → make
ranking change → replay → ship if Jaccard@5 holds and latency doesn't
regress. Replaces "I think this is faster?" with three numbers.

### New files

- `memem/eval_capture.py` — capture + PII scrub + opt-in gate
- `memem/eval_replay.py` — Jaccard / top-1 / latency-Δ math + replay runner
- `tests/test_eval_replay.py` — 23 tests covering scrub patterns, opt-in
  gate, Jaccard math, top-1 stability, error handling, end-to-end capture

### Wire format

NDJSON, `schema_version: 1`. Additive evolution within v1; breaking
changes bump to v2. Each row:
```json
{
  "schema_version": 1,
  "ts": "2026-05-07T15:53:34.061992Z",
  "mode": "search",
  "scope_id": "cortex-plugin",
  "query": "explain auto-recall hook",
  "memory_ids": ["abc12345-...", "deadbeef-..."],
  "limit": 10,
  "latency_ms": 42.5
}
```

### Why this matters

The three silent bugs found in v1.2.1's investigation (auto-recall stdin
field, slice items[] empty, Stop-hook timeout) all affected retrieval
behavior in ways unit tests couldn't catch — they were "still passes its
own tests, just doesn't recall anything useful" failures. With eval
replay, a baseline-vs-current diff would have flagged Jaccard@5 dropping
from 0.85 to 0.0 the moment any of those bugs landed. This is the gating
mechanism every retrieval-quality change should pass through.

## [1.2.2] - 2026-05-06

### Fixed — miner crash-loop on actively-growing JSONLs

A long-running Claude Code session (this conversation's own JSONL, in fact)
hit the miner-kill loop documented as a known design tension: each retry
timed out at 300s on the steadily-growing file, `STATUS_FAILED` was
persisted, but the next eligibility check saw the fingerprint had changed
(file kept growing) and re-included the session. Result: 102 failed mining
attempts before the wrapper's 5-in-60s budget locked it out, daemon dead
for 22+ hours.

Two fixes, both tiny:

- **`HARD_RETRY_CAP` (default 5)** — once a session is in `STATUS_FAILED`
  with `attempts >= HARD_RETRY_CAP`, `session_is_terminal` returns True
  regardless of fingerprint changes. The original "transient failures
  retry on content change" design is preserved below the cap.
- **`SETTLE_SECONDS` default 300 → 1800** — the 5-minute settle window
  was too short for actively-typed-into sessions; the miner picked them
  up between user turns and the Haiku subprocess timed out on a
  still-growing file. 30 minutes ensures the user has stopped typing
  for a meaningful gap before mining is attempted.

Both are env-overridable (`MEMEM_MINER_HARD_RETRY_CAP`,
`MEMEM_MINER_SETTLE_SECONDS`) for users with different traffic patterns.

### Added

- `tests/test_miner_failure_persistence.py::test_failed_session_stays_terminal_past_hard_retry_cap`
- `tests/test_miner_failure_persistence.py::test_failed_session_below_hard_cap_still_re_includes_on_change`
- `tests/test_miner_failure_persistence.py::test_settle_seconds_default_is_30_minutes`

### Operator note

The recommended dreamer cron cadence is **weekly** (e.g. `0 3 * * 0`),
not daily. The dreamer's value comes from accumulated attribution
diversity across many sessions; daily runs on a stable vault are no-ops.
Existing users are encouraged to bump their crontab from `0 3 * * *` to
`0 3 * * 0`.

## [1.2.1] - 2026-05-05

### Fixed — three silent gaps in the closed loop

A field investigation triggered by "are memories actually being recalled?"
uncovered three independent silent failures that had been quietly breaking
recall, attribution, and the cron-driven dreamer. Each was a tiny diff
that nuked a different stage of the loop.

- **Auto-recall hook read the wrong stdin field.** `hooks/auto-recall.sh`
  pulled `message` / `query`, but Claude Code's UserPromptSubmit payload
  uses `user_prompt` (per the official plugin-dev test fixture). The
  hook bailed at line 171 (`if not message: emit_empty()`) on every
  real prompt. Forensics on a multi-day session JSONL: 100 user prompts
  → 1 hook injection (and that one was SessionStart, not auto-recall).
  Topic-shifts log untouched for 8 days. Fix: read `user_prompt` first
  with fallbacks for older Codex-style payloads. The existing test
  passed because it mocked the bug-compatible field — also fixed.

- **Active slice never set the universal `items[]` field.**
  `memem/active_slice.py` built `slice_obj` with `goals`/`constraints`/
  `decisions` lists but skipped the `items[]` surface that Stop-hook
  attribution and the dreamer iterate. Result: every persisted slice
  showed `items: []` even when the section lists were full → zero
  attribution events even when recall succeeded → dreamer's relevance
  signal permanently dark. Fix: assign `items=selected_memory_items`
  and `slice_kind="active"` on the active builder.

- **Stop hook attribution timeout killed 16/17 of the signal.** The
  hook's 10s `MEMEM_ATTRIBUTION_TIMEOUT` (and matching 12s hooks.json
  bracket) was eaten by sentence-transformers cold-load (~5-10s each
  invocation). After model load, the hook computed embedding similarity
  for the first item and the timeout fired before the second iteration.
  Closed-loop signal was ~17× sparser than designed. Fix: bump
  defaults to 30s/35s. End-to-end verified: 17 items now process in
  10.3s with full attribution coverage.

- **Dreamer wrapper invoked the wrong module.** `scripts/run_dreamer.sh`
  called `python3 -m memem.cli`, but `cli.py` has no `__main__` block —
  the module loaded and exited 0 silently. Cron would have looked
  successful forever while doing nothing. Fix: switch to
  `python3 -m memem.server` (the real entrypoint that calls
  `dispatch_cli`). Regression test pins the contract: wrapper must
  produce the `[memem dreamer]` banner on stdout.

### Added

- `tests/test_run_dreamer_wrapper.py` — wrapper regression test
- `tests/test_v011.py::test_auto_recall_reads_official_user_prompt_field`
  — sends ONLY `user_prompt` (no fallback) and asserts non-empty
  additionalContext
- `tests/test_active_slice.py::test_active_slice_populates_universal_items_field`
  — asserts the active builder fills `items[]` with `memory_id`-bearing
  entries

### Why this matters

These are exactly the failures memem itself was supposed to surface and
self-heal. They didn't because every single one passed silently — empty
hook output, empty `items[]`, mid-loop kill, and a no-op cron all return
exit code 0. The fixes restore the closed loop end-to-end: every prompt
now produces a recall slice with populated `items[]`, every Stop event
writes attribution data per memory, and the nightly cron runs the real
dreamer. The first useful dreamer pass arrives once 1-2 weeks of
attribution data accumulate.

## [0.11.0] - 2026-04-14

### Changed — "session-start token diet"

Cuts memem's per-session token cost by ~5-6× by adopting claude-mem's
SessionStart defaults and adding project scoping. Measured on the dev
vault (1926 memories): previous SessionStart injected ~20K tokens,
v0.11.0 injects ~3.7K tokens.

**Defaults overhauled** (matches claude-mem parity):

- **`MEMEM_SESSION_START_LIMIT`** (default **50**, range 1–200) — total
  memories injected at session start. Previous default was 500.
- **`MEMEM_SESSION_START_FULL`** (default **5**, range 0–20) — of the
  top-ranked memories, how many show full content vs compact index
  lines. Previous model loaded all L0 as full content uncapped.
- **`MEMEM_SESSION_START_PROJECT`** (default = `cwd` basename) — scope
  filter. memem now injects only memories from the current working
  directory's project + cross-project `general` memories. Pass `"all"`
  to disable scoping and include every memory. **This is the biggest
  single win** — a user in `/cortex-plugin/` working on one project no
  longer sees memories from 48 other projects.

**Ranking unified** across L0-L3: top-N by importance × recency. The
hard L0/L1/L2/L3 split at session start is gone — the layers still
exist for classification but no longer gate injection. Claude gets a
flat "top N most useful for this project right now" set.

### Fixed — double-fire on first turn

SessionStart and UserPromptSubmit previously both fired on the first
message of a session, injecting the same material twice (~20K from
SessionStart + ~5-10K from context_assemble). SessionStart now writes
`~/.memem/.last-brief.json` with a `primed: true` flag. The immediately-
following UserPromptSubmit sees the flag and returns empty context,
consuming the flag so subsequent prompts use normal topic-shift logic.

**Savings: ~5-10K tokens on the first turn** (previously wasted on
duplicate injection).

### Backward compat

- Legacy `MEMEM_COMPACT_INDEX_LIMIT` env var is still honored as a
  fallback if set.
- `--limit N` CLI argument still works on `--compact-index`.
- Memories still have layer fields; classifier unchanged. Only the
  session-start injection behavior changed, not the classification.

### Tests

- 6 new tests in `tests/test_v011.py` covering the new env vars, full-
  count cap, project scoping, default limit, primed-marker write, and
  auto-recall primed-flag consumption.
- Total suite: **85 tests**, ruff clean.

### Estimated per-session savings

| Dimension | v0.10.2 | v0.11.0 | Savings |
|---|---|---|---|
| SessionStart memory count | 500 + up to 20 L0 full | 45 compact + 5 full | ~10× fewer items |
| SessionStart tokens | ~20K | ~3.7K | ~16K/session |
| First-prompt double-fire | ~5-10K extra | 0 | ~7K/first turn |
| Scope | all projects | current project only | ~10× less noise |
| **Total first-turn cost** | **~25-30K tokens** | **~3.7K tokens** | **~7×** |

## [0.10.2] - 2026-04-14

### Fixed — second-pass code review

Six more bugs found in a second code review of v0.10.0/v0.10.1. All but
one are pre-existing (not introduced by the layered-recall overhaul),
but one of them — `--mine-all` being silently broken — meant our
v0.10.0 migration advice ("run `--rebuild-playbooks` to reclassify")
mined nothing at all for affected users.

**High severity:**

- **`--mine-all` was a no-op for historical sessions.** `cli.py`
  cleared the install-time gate marker, then immediately called
  `mine_all()` → `find_settled_sessions()` → `_get_installed_at()`,
  which lazily recreated the marker with the current timestamp. The
  subsequent filter then rejected every pre-install session. Fix:
  `_get_installed_at()` is now read-only and returns `0.0` when the
  marker is absent. A new `_ensure_installed_at()` is the only path
  that creates the marker (called explicitly by `miner_daemon._run_loop`
  on first startup). `find_settled_sessions()` accepts a `bypass_gate`
  parameter; `mine_all()` defaults it to `True` for the CLI entry point.
- **Topic-shift detection silent-starvation after transient Haiku
  failure.** `auto-recall.sh` wrote `.last-brief.json` BEFORE attempting
  context assembly. If assembly then failed transiently, the next prompt
  with similar keywords would see the updated keyword set, compute high
  overlap, and skip assembly entirely — silently starving Claude of
  context for the rest of the session. Fix: write `.last-brief.json`
  only after a successful assembly. On failure, leave the last-brief
  untouched so the next prompt retries.
- **Transient merge failures silently dropped with session marked
  `STATUS_COMPLETE`.** `mining.py` caught `TransientMiningError` in
  the merge path, logged a warning, and continued — then marked the
  session complete so the miner would never retry. Permanent insight
  loss on transient Haiku errors. Fix: `TransientMiningError` now
  propagates out of the merge path so the outer session finishes in
  `STATUS_FAILED` and is retried. `ValueError` (genuine junk content
  or security threats) is still dropped — those are not retryable.

**Medium severity:**

- **`context_assemble` dropped memories+transcripts on Haiku failure.**
  On subprocess exception or empty result, the function returned
  `playbook_content or ""`, throwing away memories and transcript
  search results that had already been fetched. Fix: fall back to the
  raw `materials` (playbook + memories + transcript), matching the
  degraded-mode branch's behavior.
- **`smart_recall(scope_id=...)` leaked cross-project memories.** The
  Haiku-driven path loaded the global memory index, asked Haiku to pick
  from it, and returned the chosen memories without any scope filter.
  `scope_id` was only honored in the fallback branches. Fix: apply the
  scope filter to `picked_files` after `_load_obsidian_memories`.

**Low severity:**

- **`auto-recall.sh` passed user message as argv — ARG_MAX exposure.**
  Large pasted prompts (huge logs, code blocks) could hit the ~2 MB
  argv size limit and silently fail. Fix: write the raw hook input to
  a tempfile and pass the path, matching the pattern already used in
  `session-start.sh` and `pre-tool-use.sh`.

### Tests

- 6 new regression tests in `tests/test_v010_fixes.py` — one per bug,
  each failing before the fix and passing after. Total suite: 79 tests,
  ruff clean, mypy compatible.

### User impact

If you ran `--mine-all` on v0.10.0 expecting historical sessions to be
processed, run it again on v0.10.2 — this time it will actually work.
Your vault's existing memories are not affected; the re-run just fills
in what was silently skipped.

## [0.10.1] - 2026-04-14

### Fixed — v0.10.0 code-review findings

Post-ship forge code review found six bugs missed by the test suite
(because the tests mocked around the real data paths). All fixed here.

**Correctness:**
- **`memory_timeline` chronological sort was dead code.** Read
  `mem.get("created")` but the parser stores the field as `created_at`.
  Every timestamp parsed as `0.0`, so the **Before** section of every
  timeline was always empty and **After** got all same-project memories
  in undefined order. Fixed six call sites in `memory_timeline` to use
  `created_at`.
- **`pre-tool-use.sh` drained stdin before the Python helper read it.**
  `INPUT=$(cat)` consumed stdin, then the heredoc tried `json.load(sys.stdin)`
  on an empty pipe → `JSONDecodeError` → silent fallthrough to empty
  context. `MEMEM_PRETOOL_GATING=1` was a no-op for every user. Fixed by
  writing stdin to a tempfile and passing the path via argv to the
  Python helper.
- **`classify_layer` read `tags` instead of `domain_tags`.** Memories
  from `_make_memory` and `_parse_obsidian_memory_file` both store the
  tag list under `domain_tags`. The tag-based L0 structural match and
  L1 generic-pattern match never fired — only the title substring
  fallback worked. Fixed to read `mem.get("domain_tags") or mem.get("tags") or []`.
- **`_format_full_memory` (`memory_get` display) had the same bug.**
  Always rendered an empty `**tags:**` line because it read `mem.get("tags", [])`.
  Same fix.

**Performance:**
- **`classify_layer(mem, _obsidian_memories())` inside the mining
  loop.** Each insight re-read all 1895 markdown files from disk. Hoisted
  the snapshot once per session and appended new memories to the
  snapshot for L0 cap accounting.
- **`--compact-index` was unbounded.** With 1895 memories the output
  was 290 KB (~72K tokens) — injected at every session start, eating
  ~36% of Claude's context window before the user typed anything. Added
  a `--limit N` flag (default 500, override via `MEMEM_COMPACT_INDEX_LIMIT`
  env var), ranked by importance + recency. Session-start output is now
  ~79 KB / ~20K tokens for typical vaults.

**Graph traversal:**
- **FTS path silently did 2-hop traversal.** `_search_memories` (FTS
  branch) already expanded linked memories, then `memory_search` called
  `_linked_memories` again on the combined result. Docs, tests, and the
  helper docstring all say "one hop". Added an `expand_links` parameter
  to `_search_memories` — `memory_recall` keeps its existing behavior
  (backward compat), `memory_search` passes `expand_links=False` and
  does its own single-hop expansion via `_linked_memories`.

**MCP schema:**
- **`memory_timeline` `min_length=4`** in the MCP `Field` annotation
  but `_find_memory` requires 8-char prefixes. Claude would follow the
  schema, send 4-char IDs, and get "Anchor memory not found" back.
  Fixed to `min_length=8`.

**Scope filtering:**
- **`memory_timeline`'s `scope_id` parameter was accepted but unused.**
  `_obsidian_memories()` was called without passing `scope_id`, so
  timelines leaked cross-project memories. Fixed.

### Known trade-off
- **PreToolUse hook has ~5s cold-start latency** because each invocation
  spawns a fresh `python -m memem.server --recall` subprocess that
  imports the full memory index. This is why it's opt-in via
  `MEMEM_PRETOOL_GATING=1`. A future release will query `search.db`
  directly via a lightweight helper without importing `memem.server`.

## [0.10.0] - 2026-04-14

### Changed — "layered recall"
Comprehensive overhaul of memory retrieval for both session start and
in-session recall, adopting the best patterns from
[claude-mem](https://github.com/thedotmack/claude-mem) (3-tier progressive
disclosure) and [mem0](https://mem0.ai) (pre-loaded context + topic-shift
detection). This is a retrieval-layer release — mining, storage, and the
Obsidian vault layout are unchanged.

### Added — session start
- **`SessionStart` hook** (`hooks/session-start.sh`) fires before the user
  types their first message, injecting a compact briefing via
  `hookSpecificOutput.additionalContext`. Silent by default; set
  `MEMEM_SHOW_BANNER=1` for a one-line status banner.
- **Compact index injection** at session start instead of full memory content.
  Each L1-L3 memory contributes ~50 tokens (`[id] L<layer> title — snippet`).
  L0 memories still get full content.
- **L0/L1/L2/L3 layer auto-stratification.** New `layer` int frontmatter field
  on every memory, auto-classified at mining time via pure-Python scope
  heuristics (`memem/mining.py:classify_layer`). Rules:
  - L0 = project identity (importance ≥ 4, structural tag, L0 cap per project = 20)
  - L1 = generic conventions (importance ≥ 4, cross-project, or generic tag)
  - L3 = rare/archival (low importance, short content, no related links)
  - L2 = domain-specific (default)
- **`--compact-index` CLI flag** (`python -m memem.server --compact-index`)
  outputs the session-start briefing format (L0 full + L1-L3 index).

### Added — in-session recall
- **3-tier recall workflow** matching claude-mem's progressive disclosure
  pattern. Three new MCP tools on the memem server:
  - **`memory_search(query, limit, scope_id)`** — Layer 1 compact index
    (~50 tok/result). Returns IDs + layer + title + 1-line snippet. Use
    first to narrow candidates cheaply.
  - **`memory_get(ids, scope_id)`** — Layer 2 full content fetch (~500
    tok/result). Use after `memory_search` when you know which memories
    you need. Accepts 8-char ID prefixes.
  - **`memory_timeline(memory_id, depth_before, depth_after, scope_id)`** —
    Layer 3 chronological thread. Walks the `related[]` graph forward AND
    reverse, plus same-project memories in a creation-time window around
    the anchor.
- **Topic-shift detection** (`hooks/auto-recall.sh` rewritten). On each
  `UserPromptSubmit`, computes keyword overlap between the new prompt and
  the last triggered briefing (stored in `~/.memem/.last-brief.json`). If
  overlap < `MEMEM_TOPIC_SHIFT_THRESHOLD` (default `0.3`), re-fires
  `context_assemble` for a fresh brief. Every trigger is logged to
  `~/.memem/topic-shifts.log` for tuning. Closes the "Claude didn't think
  to search" failure mode that's claude-mem's biggest known weakness.
- **One-hop graph traversal** on `memory_search` and `memory_get`. After
  the primary result set, memem follows the `related[]` field exactly one
  hop and includes linked memories in a separate section. Half of your
  memories already have `related[]` populated — this is a free context
  win per query.
- **PreToolUse hook for file-read enrichment** (`hooks/pre-tool-use.sh`).
  When Claude is about to call the `Read` tool, memem searches memories
  for the target file path and injects any matches via `additionalContext`.
  **Opt-in** via `MEMEM_PRETOOL_GATING=1` — off by default. Does NOT
  block the read (too aggressive for v0.10.0); just enriches context.

### Backward compatibility
- **`memory_recall`** MCP tool is UNCHANGED and remains the
  backward-compat alias. Existing clients keep working.
- **`context_assemble`**, **`memory_save`**, **`memory_list`**,
  **`memory_import`**, **`transcript_search`** are unchanged.
- Memories without a `layer` field parse as `LAYER_L2` (=2, the default),
  so existing vaults load cleanly without a migration.
- Legacy `CORTEX_*` env vars still work alongside the new `MEMEM_*` ones.

### New env vars (all optional)
- `MEMEM_SHOW_BANNER=1` — show a one-line status banner at session start
- `MEMEM_PRETOOL_GATING=1` — enable file-read enrichment hook
- `MEMEM_TOPIC_SHIFT_THRESHOLD=0.3` — keyword overlap threshold for topic-shift re-firing

### Tests
- 15 new tests in `tests/test_v010.py` covering layer constants, frontmatter
  roundtrip, compact format, memory_search/get/timeline, graph traversal,
  classify_layer rules (L0 cap, L1/L3 edge cases), CLI smoke test.
- All 58 existing tests continue to pass. Total: 73 tests, ruff clean.

### Migration
No action required. On next session start, memem will auto-inject the new
compact index. Existing memories will classify as L2 (default) until they
pass through the miner again (which reclassifies via the new heuristic).
To force reclassification without waiting, run:
```bash
python3 -m memem.server --rebuild-playbooks
```

## [0.9.3] - 2026-04-14

### Fixed — playbook drift
Playbooks are now derived directly from memories with a staleness hash,
eliminating the drift bug where projects with many memories ended up
with no playbook. Investigation showed 12 projects with ≥10 memories
had no playbook (polymarket-weather with 186 memories, vibereader with
164, techfeed with 131, etc.) because the old staging layer only
populated during active mining batches — projects mined long ago and
not re-touched never got their playbook refreshed.

- **`_playbook_refine(project)` reads memories directly.** The staging
  layer (`_playbook_append` + per-project staging jsonl files) is
  removed entirely. Playbooks are always built from the current
  memory set, never a stale snapshot.
- **Source-hash staleness check.** Each playbook stores a
  `<!-- memem-source-hash:XYZ -->` marker covering all project memory
  ids + content. Re-running refine with unchanged memories is a
  cheap no-op — no Haiku call, no I/O.
- **`_playbook_sweep()`** refines every project with ≥5 memories in a
  single pass. Mining now calls the sweep at end-of-batch instead of
  iterating only `seen_projects`, so no project is ever left without
  a playbook due to batch-gating.
- **Minimum threshold** of 5 memories per project before a playbook is
  written. Tiny projects (typos, one-off experiments) stay unrefined.
- **`--rebuild-playbooks`** now uses the sweep and takes an optional
  `--force` flag to bypass the staleness check.
- **Removed:** `_playbook_append`, `PLAYBOOK_STAGING_DIR` usage from
  mining. Staging jsonl files under `~/obsidian-brain/memem/playbooks/.staging/`
  are no longer written; existing ones can be deleted.

### Migration
On the next mining batch (or manual `--rebuild-playbooks` run), the
sweep will detect all drifted projects, rebuild their playbooks from
current memories, and write the source-hash marker. Expect one-time
Haiku cost proportional to the number of large missing-playbook
projects (~12 calls for a typical drifted install).

## [0.9.2] - 2026-04-14

### Changed
- **`/memem` branches on new-vs-returning user.** Adopts the mature CLI
  pattern (VSCode walkthroughs, gh CLI, Homebrew): brand-new users see
  the full welcome with ASCII art, pitch, and mining options; returning
  users see a compact one-line status only, no pedagogy. Distinction is
  tracked via `~/.memem/.welcome-shown` (created after first welcome) +
  legacy migration marker + memory count. First view is persistent.
- **New `/memem-welcome` skill.** Re-show the full welcome on demand
  regardless of state — the "re-open walkthrough" handle borrowed from
  VSCode's walkthrough pattern. Returning users who want the intro
  again don't have to delete state files.

## [0.9.1] - 2026-04-14

### Fixed
- **Grandfather existing users into opt-in mining.** v0.9.0 made the miner
  strictly opt-in via `~/.memem/.miner-opted-in`, but existing users
  upgrading from v0.8.x had their miners silently go idle on next boot
  because they had no marker. Bootstrap now creates the marker automatically
  on upgrade when any of these signals are present:
  - `~/.memem/miner.pid` exists (daemon was running)
  - `~/.memem/.migrated_from_cortex` exists (legacy install)
  - The vault has at least one mined memory
  Brand-new installs still get the clean opt-in flow via `/memem`.

## [0.9.0] - 2026-04-14

### Changed — "opt-in mining"
Install no longer starts any background processes. The miner daemon is
strictly opt-in, tracked by the marker file `~/.memem/.miner-opted-in`.
Users get a clean silent install, type `/memem` to see the welcome and
mining options, and explicitly choose whether to start mining.

- **Miner is opt-in.** `storage._auto_start_miner()` now early-returns if
  `~/.memem/.miner-opted-in` is absent. The MCP server boots and memories
  are readable, but no background daemon spawns until the user opts in.
- **Opt-in paths** (all create the marker):
  - `/memem-mine` — start the daemon, mine new sessions only
  - `/memem-mine-history` — start the daemon + run `--mine-all` for full history
  - `python3 -m memem.server --miner-opt-in` — CLI opt-in
  - `python3 -m memem.server --mine-all` — implicit opt-in
  - `python3 -m memem.server --miner-start` — implicit opt-in
- **Opt-out:** `python3 -m memem.server --miner-opt-out` stops the daemon
  and removes the marker.
- **Auto-mine on install removed.** The bootstrap auto-mine block added in
  v0.8.0 is gone. Install runs only the environment checks.
- **`/memem` welcome shows miner state.** Branches on whether the user
  has opted in — if not, prominently shows the two options (new sessions
  vs full history) with natural-language trigger phrases and slash
  command alternatives.
- **CLAUDE.md teaches Claude the two modes.** When the user says "start
  mining" or similar, the assistant now knows to identify mode 1 (new
  sessions) vs mode 2 (history + ongoing) and run the right commands —
  or ask if unsure.

### Migration
**Existing users who already had the miner running:** your miner will
keep running for the current session, but on next server boot it won't
auto-start unless you create the marker. Run `/memem-mine` once to
opt in and preserve the old behaviour.

## [0.8.0] - 2026-04-14

### Changed — "quiet onboarding"
Overhauls the new-user experience around a pull model: install → memem
works silently → user types `/memem` when they want the welcome and
status. No more wall-of-text injection into the first user prompt.

- **Auto-mine past sessions on first install.** `bootstrap.sh` now checks
  `~/.claude/projects/` on first run and, if you have ≥5 prior Claude Code
  sessions, spawns `memem.server --mine-all` in the background via `nohup`.
  One-shot, idempotent via `~/.memem/.auto-mined` marker. Converts new
  users from cold-start (0 memories) to warm-start on day one.
  **Opt-out:** `MEMEM_NO_AUTO_MINE=1`.
- **No more welcome wall on first prompt.** `hooks/auto-recall.sh` used to
  inject a ~40-line welcome (ASCII art + tool tutorials + Obsidian pitch +
  mine-history prompt) into the first `UserPromptSubmit` of a zero-memory
  session. That hijacked the user's actual first question. The hook now
  stays silent on zero memories — users learn about memem via `/memem`.
- **`/memem` is now the welcome surface.** Enriched the skill to show:
  bootstrap error (if any), ASCII banner, live status, auto-mine progress,
  how-it-works explainer, commands, MCP tools, and the optional-Obsidian
  note. Pull model: user asks, gets everything in one place.
- **Python auto-install.** If system `python3` is missing or older than
  3.11, bootstrap now runs `uv python install 3.11` and uses that for
  the plugin venv. No more hard bounces on Python 3.10.
- **Bootstrap errors surface to chat.** On any non-zero exit, `die()`
  writes `~/.memem/last-error.md` with the error, exit code, log path,
  and a pointer to `/memem-doctor`. `/memem` reads this file and shows
  it prominently at the top when present. Clears on successful bootstrap.

### Migration
No action required. Existing installs get the quiet-onboarding flow on
next session. The `.auto-mined` marker starts empty, so existing users
with prior sessions will get a one-time background mine on their first
v0.8.0 session unless `MEMEM_NO_AUTO_MINE=1` is set.

## [0.7.1] - 2026-04-14

### Fixed
- **bootstrap: harden `PLUGIN_ROOT` fallback against unexpanded `${CLAUDE_PLUGIN_ROOT}`.**
  In some Claude Code spawn paths the env var was passed through literally
  instead of substituted, causing `bootstrap.sh` to source a venv at the
  literal path `${CLAUDE_PLUGIN_ROOT}/.venv` and exit before the MCP server
  started — surfacing as a silent "MCP unconnected" for the user. Bootstrap
  now validates the candidate is non-empty, not the literal unexpanded
  string, and points to an existing directory; otherwise it falls back to
  `$SCRIPT_DIR`.

### Docs
- README: removed the "Skip memem if" section.
- README: expanded "What does memem save?" from 5 to 10 categories with
  concrete examples (env/tooling, project invariants, failure patterns,
  third-party quirks, domain knowledge).

## [0.7.0] - 2026-04-14

### Renamed
- **Project renamed from `cortex` to `memem`.** Same code, same architecture,
  same behaviour — new identifier across every surface to escape the crowded
  "cortex" namespace in the AI tooling space.

### Added
- `memem/migrate.py` and bootstrap shim hook: one-time copy of
  `~/.cortex/` → `~/.memem/` and `~/obsidian-brain/cortex/` →
  `~/obsidian-brain/memem/` on first run after upgrade. Idempotent via a
  `.migrated_from_cortex` marker file. Copies (not moves) so the legacy
  paths stay intact as a safety net.
- Backward-compatible env var fallback: every `MEMEM_*` env var falls back
  to the corresponding `CORTEX_*` if unset. Existing shell profiles keep
  working with no changes.
- `tests/test_bootstrap.py::test_bootstrap_legacy_cortex_env_fallback`
  regression guard for the env var fallback.

### Changed
- Package directory: `cortex_server/` → `memem/`
- All intra-package imports: `from cortex_server.X import Y` →
  `from memem.X import Y` (124 occurrences across 19 source files)
- FastMCP server name: `FastMCP("cortex")` → `FastMCP("memem")`
- All log channel names: `cortex-storage`, `cortex-recall`, `cortex-miner`,
  `cortex-telemetry`, `cortex-search`, `cortex-playbook`, `cortex-obsidian`,
  `cortex-assembly`, `cortex-capabilities` → `memem-*`
- Console-script entry point: `cortex-server` → `memem`
- PyPI package name: `cortex-plugin` → `memem`
- Plugin name in `plugin.json`: `cortex` → `memem`
- MCP tool namespace seen by Claude Code: `mcp__cortex__*` → `mcp__memem__*`
- Skill directories: `skills/cortex*/` → `skills/memem*/`
- Slash commands: `/cortex`, `/cortex-status`, `/cortex-mine`,
  `/cortex-mine-history`, `/cortex-doctor` → `/memem*`
- State directory: `~/.cortex/` → `~/.memem/` (with auto-migration)
- Vault subdirectory: `~/obsidian-brain/cortex/` → `~/obsidian-brain/memem/`
  (with auto-migration)
- Capabilities JSON key: `writable_cortex_dir` → `writable_state_dir`
- Status banner: `[Cortex] N memories` → `[memem] N memories`
- GitHub repo names: `TT-Wang/cortex-plugin` (private) and the public
  mirror are renamed to `TT-Wang/memem`. GitHub redirects old URLs for
  ~12 months so existing badge embeds keep working.
- Glama listing slug coordination required (manual; see migration notes
  in the release announcement).
- `awesome-mcp-servers` entry follow-up PR filed to update the slug
  (the Knowledge & Memory section entry).

### Notes
- All 55 tests still pass (54 → 55 with the new fallback regression test).
- Ruff clean. Mypy clean.
- Total file touch count: ~80.
- The rename is genuinely cosmetic at the code level — no logic changed,
  no behaviour changed, no schema migration required for memory frontmatter.
- This is a **non-breaking** release for users who run the bootstrap shim,
  thanks to the env-var fallback and the auto-migration. It IS breaking for
  users who hardcoded `import cortex_server` or the `cortex-server` console
  script in their own scripts — they should either pin to v0.6.0 or update
  their imports to `import memem`.

## [0.6.0] - 2026-04-14

### Added
- **Self-healing bootstrap shim** (`bootstrap.sh` at repo root) — first MCP spawn
  installs `uv` if missing, hash-caches a `uv sync` against the committed
  `uv.lock` into a plugin-local `.venv`, canary-tests directory writability,
  writes `~/.cortex/.capabilities`, and exec's the server. ~5s on first run,
  ~100ms on every subsequent run. No separate `pip install` step required.
- `cortex_server/capabilities.py` — runtime feature detection (Python version,
  `mcp` importability, `claude` CLI presence, `uv` availability, vault and
  cortex-dir writability) with atomic JSON serialization to
  `~/.cortex/.capabilities`.
- **`/cortex-doctor` slash command** — preflight health check that runs the
  same probe as the bootstrap shim and pretty-prints a HEALTHY / DEGRADED /
  FAILING report with explicit fix instructions for each blocker.
- **SessionStart status banner** — `hooks/auto-recall.sh` now prepends a
  one-line status to the assembled context: `[Cortex] N memories · miner OK ·
  assembly OK|degraded`.
- `--doctor` CLI command.
- Committed `uv.lock` for deterministic dep installs across machines.
- `.dockerignore` and a minimal `Dockerfile` (used only by Glama for sandbox
  validation; not a normal install path).
- `tests/test_bootstrap.py` (6 subprocess-level smoke tests for the shim).
- `tests/test_packaging.py` runtime tests for `miner-wrapper.sh status`,
  `mine-cron.sh` execution, and `auto-recall.sh` with missing
  `CLAUDE_PLUGIN_ROOT`.
- `tests/test_obsidian_store.py::test_purge_mined_memories_clears_fts_and_index`
  regression guard.
- `llms.txt` at repo root for LLM crawler discovery.
- `SECURITY.md` with vulnerability reporting policy and defence-in-depth
  summary.
- Glama A-tier score badge, CI status badge, MIT license badge, and Python
  version badge in the README.
- Comprehensive MCP tool descriptions: every tool now has a multi-paragraph
  docstring with `Behaviour:` block (read/write classification, auth, rate
  limits, data scope, idempotency, failure modes), `typing.Annotated[...,
  pydantic.Field(description=...)]` on every parameter with length/range
  constraints, explicit sibling differentiation, and worked examples.
  Glama TDQS tier: D → A.

### Changed
- `.claude-plugin/plugin.json` MCP `command` is now `bash bootstrap.sh`
  (previously `python3 -m cortex_server.server` directly). This is the
  load-bearing change that enables install-as-launch.
- `cortex_server/storage.py::_auto_start_miner` is now poll-and-log instead
  of fire-and-forget — Popens the wrapper, then polls the miner PID file for
  up to 2s and logs a clear warning if the daemon doesn't come up. Falls
  back to plain `bash` when `setsid` is missing.
- `cortex_server/assembly.py::context_assemble` and
  `cortex_server/recall.py::smart_recall` enter graceful **degraded mode**
  when `claude` CLI is missing — return raw playbook+memories materials
  instead of failing or returning empty.
- `cortex_server/server.py` builds the FastMCP instance lazily via
  `_build_mcp()`, only called when no CLI args are supplied — non-MCP
  commands (`--status`, `--rebuild-index`, `--doctor`, etc.) no longer
  require the `mcp` package to be importable.
- Renamed `cortex-mcp-server/` → `cortex_server/` (a real Python package
  identifier, dashes are not allowed in module names).
- Renamed `miner-daemon.py` → `miner_daemon.py` (subprocess module target).
- All intra-package imports rewritten to `from cortex_server.X import Y`.
- README rewritten with Q&A section structure for LLM retrieval (sections
  match the exact phrasing an LLM uses internally to search), and a Glama
  badge + CI badge + license badge row added at the top.
- CI now removes the `mypy || true` escape hatch — mypy must pass cleanly.
- CI adds a `build` job that builds a wheel and smoke-tests it in a clean
  venv on every push.
- Tag and `related` field values written to YAML frontmatter are now
  sanitized (newlines, brackets, commas stripped) to prevent frontmatter
  injection from hallucinated or hostile values.
- `_extract_json_string` in mining now uses `json.JSONDecoder.raw_decode`
  to respect string literals, fixing a class of false-negative parse
  failures on Haiku output containing unbalanced brackets inside titles.
- `_check_contradictions` requires word-set containment ≥ 0.3 between new
  and existing content before flagging a contradiction (previously over-
  eager, could trigger wrongful mass deprecations).
- `purge_mined_memories` now removes deleted memories from the FTS5 index
  and `_index.md` (previously orphaned both, leaving phantom hits).
- `save_mined_session_state` now `fsync`s the temp file before rename,
  closing a crash-window data-loss path.
- `_record_access` (telemetry) now uses atomic `tmp + fsync + os.replace`
  writes; corrupt JSON is preserved as `<file>.corrupt.<mtime>` instead
  of being silently overwritten.
- `_save_memory` writes the memory once (computes related links first)
  instead of writing twice with a stale-index window between writes.
- `_yaml_escape` now strips control characters (`\r`, `\n`, `\t`) from
  values before quoting, preventing frontmatter injection via titles.

### Fixed
- Miner could mark malformed-Haiku-output sessions as `STATUS_COMPLETE`,
  causing silent data loss on broken sessions. Now raises
  `TransientMiningError` so failed sessions stay retryable.
- `hooks/auto-recall.sh` no longer guesses a wrong fallback path when
  `CLAUDE_PLUGIN_ROOT` is missing — logs to stderr and skips assembly
  cleanly instead.
- `_auto_start_miner` no longer swallows exceptions silently; logs with
  `exc_info=True` on failure.
- `recall.py` no longer raises `ValueError` on memories with missing IDs
  (replaced `list.index()` with O(1) dict lookup with safe fallback).
- Removed dead `[]` branch in `mining._summarize_session_haiku` that was
  unreachable after the JSON parser change.
- Module-level `RotatingFileHandler` and `basicConfig` in `miner_daemon.py`
  moved into `_configure_logging()` so importing the module is now a
  no-op side-effect-free operation (was clobbering the host process root
  logger and creating real `~/.cortex/miner.log` files from test imports).
- `start_daemon` blind 0.5s sleep replaced with a 10×100ms poll loop on
  the grandchild PID file, eliminating false "Failed to start daemon"
  messages on slow hosts.

### Discovery / packaging
- Submitted to the Anthropic Claude Code plugin marketplace.
- Submitted to `awesome-mcp-servers` (`punkpeye/awesome-mcp-servers`
  PR #4612, awaiting merge).
- Submitted to `awesome-claude-code`
  (`hesreallyhim/awesome-claude-code`, 7-day cooldown, resubmission
  window opens 2026-04-16).
- Validated and listed on Glama at
  https://glama.ai/mcp/servers/TT-Wang/cortex-plugin with **A-tier scores
  across all three dimensions** (license · security · TDQS).
- Repo About description, homepage URL, and 10 GitHub topics set for
  search discoverability.

## [0.5.0] - 2026-04-13

### Added
- `pyproject.toml` with proper package metadata, build system, and dev dependencies
- `CHANGELOG.md` (this file)
- `CONTRIBUTING.md` with dev setup, PR process, commit conventions
- GitHub Actions CI workflow: ruff + mypy + pytest on push/PR
- Pre-commit hooks configuration
- `tests/` directory with real pytest suite (security, obsidian_store, search_index, telemetry, playbook, mining, integration)
- `schema_version` field on memory frontmatter
- `--migrate-schema` CLI command for upgrading old memories
- Atomic file writes via temp-file + `os.replace()` pattern
- `fcntl` file locks on index read-modify-write operations

### Changed
- Split `storage.py` (1,243 lines) into 7 focused modules:
  - `models.py` — data types and constants
  - `security.py` — threat scanning
  - `telemetry.py` — access tracking and event log
  - `search_index.py` — SQLite FTS5
  - `obsidian_store.py` — Obsidian I/O and dedup scoring
  - `playbook.py` — grow and refine
  - `assembly.py` — context assembly and consolidation
- `storage.py` is now a thin facade re-exporting from the new modules for backward compatibility

### Fixed
- Memory file orphaning when titles change (double-file issue)
- Junk content rejection (memories under 10 chars)
- Race conditions on concurrent index updates

## [0.4.1] - 2026-04-12

### Fixed
- Miner auto-start now uses `setsid` to fully detach from MCP server process
- Version bump forces cache refresh for users with stale 0.4.0 install
- Onboarding clarifies that new session mining is automatic, history is opt-in

## [0.4.0] - 2026-04-11

### Added
- `/cortex`, `/cortex-status`, `/cortex-mine`, `/cortex-mine-history` slash commands
- Welcome banner with ASCII logo for first-session onboarding
- Obsidian setup guide in README and onboarding message
- `--status` CLI command showing memory count, projects, miner health
- `--events` CLI command for recent event log
- `--eval` CLI command for quality smoke test
- Auto-create vault and auto-start miner on MCP server startup
- Mining opt-in for existing sessions (default: only mine new sessions going forward)
- `.installed_at` gate file to distinguish pre-install from post-install sessions

### Changed
- Hook timeout: 10s → 35s (context_assemble needs up to 30s)
- Hook uses temp file for input (avoids argv size limits on large prompts)
- Removed duplicate `.mcp.json` (plugin.json handles MCP registration)

## [0.3.0] - 2026-04-11

### Added
- Event log (`~/.cortex/events.jsonl`) — append-only audit trail
- Importance scoring (1-5 scale) at extraction time, used in recall ranking
- SQLite FTS5 sidecar for fast full-text search
- Contradiction detection at save time (negation signal scanning)
- Telemetry sidecar (`~/.cortex/telemetry.json`) — no more write amplification on recall path
- Temporal validity (status=active/deprecated, valid_to) — stopped hard-deleting memories
- Playbook staging — `_playbook_append` writes to JSONL staging, `_playbook_refine` compiles
- Memory consolidation pass ("dreaming") after mining
- `context_assemble` — Haiku-assembled query-tailored briefing
- `--assemble-context` and `--rebuild-search-index` CLI commands
- `context_assemble` registered as MCP tool
- Security scanning on all write paths (prompt injection + credential exfil + invisible unicode)
- Relevance gate at 30% keyword threshold to filter garbage
- Stemming + synonym expansion in keyword matching
- Self-refining memory: merge (0.3-0.6 score) → update, DELETE action on supersedes

### Fixed
- Content-only scoring for dedup/merge (no temporal bias)
- Haiku extractor failures raise TransientMiningError instead of silent data loss
- Security scan on small-playbook path (previously bypassed)
- Consolidation merges deprecate instead of hard-delete
- Merge failures skip insight instead of duplicating

## [0.2.0] - 2026-04-10

### Added
- Incremental playbook grow-and-refine (ACE pattern)
- Self-evolving memory with merge pipeline
- Memory linking via `related` field
- Enriched frontmatter (updated, source_type, source_session, access_count, last_accessed)

## [0.1.0] - 2026-04-09

### Added
- Initial mining pipeline — Haiku extraction from JSONL sessions
- Obsidian vault as single source of truth for memories
- MCP server with memory_save, memory_recall, memory_list, memory_import, transcript_search tools
- UserPromptSubmit hook for auto-recall on first message
- Keyword search with containment scoring
- CLAUDE.md integration for LLM instructions

[1.6.0]: https://github.com/TT-Wang/memem/releases/tag/v1.6.0
[1.5.1]: https://github.com/TT-Wang/memem/releases/tag/v1.5.1
[1.5.0]: https://github.com/TT-Wang/memem/releases/tag/v1.5.0
[1.4.0]: https://github.com/TT-Wang/memem/releases/tag/v1.4.0
[1.3.0]: https://github.com/TT-Wang/memem/releases/tag/v1.3.0
[1.2.2]: https://github.com/TT-Wang/memem/releases/tag/v1.2.2
[1.2.1]: https://github.com/TT-Wang/memem/releases/tag/v1.2.1
[0.11.0]: https://github.com/TT-Wang/memem/releases/tag/v0.11.0
[0.7.0]: https://github.com/TT-Wang/memem/releases/tag/v0.7.0
[0.6.0]: https://github.com/TT-Wang/memem/releases/tag/v0.6.0
[0.5.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.5.0
[0.4.1]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.4.1
[0.4.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.4.0
[0.3.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.3.0
[0.2.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.2.0
[0.1.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.1.0
