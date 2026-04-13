# Changelog

All notable changes to Cortex will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.5.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.5.0
[0.4.1]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.4.1
[0.4.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.4.0
[0.3.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.3.0
[0.2.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.2.0
[0.1.0]: https://github.com/TT-Wang/cortex-plugin/releases/tag/v0.1.0
