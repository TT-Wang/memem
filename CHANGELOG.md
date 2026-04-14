# Changelog

All notable changes to memem (formerly Cortex) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note:** the project was renamed from `cortex` to `memem` in v0.7.0.
> Pre-v0.7.0 entries below describe what was called Cortex at the time —
> they have been left untouched as historical record. See the v0.7.0 entry
> for the rename details, backward-compat strategy, and migration path.

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
