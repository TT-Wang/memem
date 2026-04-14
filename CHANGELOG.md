# Changelog

All notable changes to memem (formerly Cortex) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note:** the project was renamed from `cortex` to `memem` in v0.7.0.
> Pre-v0.7.0 entries below describe what was called Cortex at the time —
> they have been left untouched as historical record. See the v0.7.0 entry
> for the rename details, backward-compat strategy, and migration path.

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

[0.7.0]: https://github.com/TT-Wang/memem/releases/tag/v0.7.0
[0.6.0]: https://github.com/TT-Wang/memem/releases/tag/v0.6.0
[0.5.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.5.0
[0.4.1]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.4.1
[0.4.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.4.0
[0.3.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.3.0
[0.2.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.2.0
[0.1.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.1.0
