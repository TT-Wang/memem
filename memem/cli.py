import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from memem.miner_protocol import FATAL_EXIT_CODE, TRANSIENT_EXIT_CODE
from memem.mining import FatalMiningError, MiningError, mine_all, mine_session
from memem.models import INDEX_PATH
from memem.obsidian_store import (
    _generate_index,
    purge_mined_memories,
)
from memem.recall import memory_recall
from memem.session_state import MINED_SESSIONS_FILE
from memem.storage import _register_server_pid


def _run_integrity_check(verbose: bool = True) -> bool:
    """Run SQLite PRAGMA integrity_check on the three WAL DBs.

    Returns True if any DB failed the check (caller treats as a blocker).
    Returns False (no failures) if all DBs are healthy OR if a DB file is
    absent (fresh install).
    """
    import sqlite3
    from pathlib import Path

    from memem.models import MEMEM_DIR

    dbs = [
        ("search.db", MEMEM_DIR / "search.db"),
        ("graph.db", MEMEM_DIR / "graph.db"),
        ("mined_sessions.db", MEMEM_DIR / "mined_sessions.db"),
    ]
    any_failed = False
    for label, path in dbs:
        p = Path(path)
        if not p.exists():
            if verbose:
                print(f"  [skip] {label}: not present")
            continue
        try:
            # timeout=5.0 sets SQLite busy-timeout so this read doesn't
            # immediately fail if the miner happens to be mid-write
            conn = sqlite3.connect(str(p), timeout=5.0)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            status = result[0] if result else "no result"
            if status == "ok":
                if verbose:
                    print(f"  [ok]   {label}: integrity_check passed")
            else:
                any_failed = True
                if verbose:
                    print(f"  [FAIL] {label}: {status}")
        except sqlite3.Error as exc:
            any_failed = True
            if verbose:
                print(f"  [FAIL] {label}: sqlite error: {exc}")
    return any_failed


def _append_env_list(environment: dict[str, Any], key: str, value: str) -> None:
    values = environment.setdefault(key, [])
    if not isinstance(values, list):
        values = []
        environment[key] = values
    values.append(value)


def _parse_slice_command(argv: list[str]) -> tuple[str, str, bool, bool, dict[str, Any], bool, bool]:
    raw_json = "--json" in argv
    use_llm = "--no-llm" not in argv
    scope = "default"
    query_file = ""
    query_parts: list[str] = []
    environment: dict[str, Any] = {}
    writeback_preview = "--writeback-preview" in argv
    auto_commit_safe = "--auto-commit-safe" in argv
    skip_next = False

    for idx, arg in enumerate(argv[2:], start=2):
        if skip_next:
            skip_next = False
            continue
        if arg in {"--json", "--no-llm", "--writeback-preview", "--auto-commit-safe"}:
            continue
        if arg == "--query-file":
            try:
                query_file = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--scope":
            try:
                scope = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--session-id":
            try:
                environment["session_id"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg in {"--repo-path", "--cwd"}:
            try:
                value = argv[idx + 1]
                environment["repo_path"] = value
                environment["cwd"] = value
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--task-mode":
            try:
                environment["task_mode"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--current-file":
            try:
                environment["current_file"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--open-file":
            try:
                _append_env_list(environment, "open_files", argv[idx + 1])
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--modified-file":
            try:
                _append_env_list(environment, "modified_files", argv[idx + 1])
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--artifact-path":
            try:
                environment["artifact_path"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--branch":
            try:
                environment["branch"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--task-name":
            try:
                environment["task_name"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--stakeholder":
            try:
                environment["stakeholder"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--deadline":
            try:
                environment["deadline"] = argv[idx + 1]
                skip_next = True
            except IndexError:
                pass
            continue
        if arg == "--include-history":
            environment["include_history"] = True
            continue
        if arg == "--include-transcripts":
            environment["include_transcripts"] = True
            continue
        query_parts.append(arg)

    if query_file:
        if query_file == "-":
            query = sys.stdin.read().strip()
        else:
            try:
                query = Path(query_file).read_text(encoding="utf-8", errors="ignore").strip()
            except OSError as exc:
                raise SystemExit(f"Failed to read query file: {exc}") from exc
    else:
        query = " ".join(query_parts).strip()

    return query, scope, use_llm, raw_json, environment, writeback_preview, auto_commit_safe


def dispatch_cli(argv: list[str], mcp) -> None:
    cmd = argv[1] if len(argv) >= 2 else None

    if cmd == "--compact-index":
        # v0.11.0 "session-start token diet" — matches claude-mem's defaults.
        #
        # MEMEM_SESSION_START_LIMIT (default 50, range 1-200): total memories
        # injected at session start (full + compact combined).
        # MEMEM_SESSION_START_FULL (default 5, range 0-20): of the top
        # ranked memories, how many show full content vs compact.
        # MEMEM_SESSION_START_PROJECT (default = cwd basename): scope filter.
        # "all" disables scoping and includes every memory.
        #
        # Ranking: importance × recency, uniform across L0-L3. Per-project
        # scoping massively reduces noise for users working in one project.
        import os

        from memem.obsidian_store import _obsidian_memories
        from memem.recall import _format_compact_index_line

        def _env_int(name: str, default: int, lo: int, hi: int) -> int:
            try:
                val = int(os.environ.get(name, str(default)))
            except ValueError:
                return default
            return max(lo, min(hi, val))

        # Legacy env var MEMEM_COMPACT_INDEX_LIMIT still honored as a fallback
        legacy_limit = os.environ.get("MEMEM_COMPACT_INDEX_LIMIT")
        default_limit = int(legacy_limit) if legacy_limit else 50
        total_limit = _env_int("MEMEM_SESSION_START_LIMIT", default_limit, 1, 200)
        full_count = _env_int("MEMEM_SESSION_START_FULL", 5, 0, 20)
        full_count = min(full_count, total_limit)

        # --limit N arg overrides env (legacy path for manual CLI use)
        if "--limit" in argv:
            try:
                total_limit = max(1, min(200, int(argv[argv.index("--limit") + 1])))
            except (ValueError, IndexError):
                pass

        # Project scope — default to cwd basename, override via env var,
        # pass "all" to disable scoping.
        scope = os.environ.get("MEMEM_SESSION_START_PROJECT", "").strip()
        if not scope:
            scope = os.path.basename(os.getcwd()) or "all"

        memories = _obsidian_memories()

        # Filter by scope unless "all". Always include cross-project "general"
        # memories alongside project-scoped ones.
        if scope != "all":
            memories = [
                m for m in memories
                if m.get("project") == scope or m.get("project") == "general"
            ]

        # Rank by importance × recency (uniform across layers).
        def _rank_key(m: dict) -> tuple:
            importance = m.get("importance", 3)
            if not isinstance(importance, int | float):
                importance = 3
            updated = m.get("updated_at") or m.get("created_at", "")
            return (importance, updated)

        memories.sort(key=_rank_key, reverse=True)
        selected = memories[:total_limit]

        full_memories = selected[:full_count]
        compact_memories = selected[full_count:]

        # Print full-content section first (top N ranked)
        if full_memories:
            print(f"## memem — top {len(full_memories)} memories (scope: {scope})\n")
            for mem in full_memories:
                title = mem.get("title", "Untitled")
                layer = mem.get("layer", 2)
                essence = mem.get("essence", "") or mem.get("full_record", "")
                print(f"### [L{layer}] {title}")
                print(essence)
                print()
        # Then compact index for the rest
        if compact_memories:
            total_in_scope = len(memories)
            header = (
                f"## Memory index ({len(compact_memories)} compact"
                + (f", {total_in_scope} in scope" if total_in_scope > total_limit else "")
                + ")"
            )
            print(header)
            print("_Use `memory_get(ids=[...])` to fetch full content, "
                  "or `memory_search(query=...)` for more._\n")
            for mem in compact_memories:
                print(_format_compact_index_line(mem))
        return

    if cmd == "--recall":
        query = " ".join(argv[2:]) if len(argv) >= 3 else ""
        print(memory_recall(query, limit=10) if query else "No query provided.")
        return

    if cmd in ("slice", "active-slice", "--active-slice"):
        query, scope, use_llm, raw_json, environment, writeback_preview, auto_commit_safe = _parse_slice_command(argv)
        if not query:
            print("No query provided.")
            return
        # Injection-mode gate: when MEMEM_INJECTION_MODE=tool, the hook path
        # (which calls `server slice …`) must produce NO output so Claude Code
        # does not receive auto-injected context. The MCP tool `active_memory_slice`
        # is called directly via server.py/_build_mcp and bypasses this branch,
        # so it continues to work normally regardless of the mode setting.
        # Use module-attribute access (not `from memem.settings import …`) so
        # tests can monkeypatch `memem.settings.MEMEM_INJECTION_MODE` and have
        # this branch react — value-imports bind at import time and bypass patches.
        import memem.settings as _memem_settings
        if _memem_settings.MEMEM_INJECTION_MODE == "tool":
            return
        # v2.0.0: active_slice_engine removed. The slice CLI path now calls
        # retrieve()+render_slice() directly (same pipeline as auto-recall.sh).
        try:
            from memem.render import render_slice
            from memem.retrieve import retrieve
        except ImportError:
            print("memem.retrieve / memem.render not available.")
            return

        results = retrieve(query, k=8)
        working: dict = {}
        if environment:
            if environment.get("task_mode"):
                working["task_mode"] = environment["task_mode"]
            if environment.get("recent_actions"):
                working["recent_actions"] = list(environment["recent_actions"])[:3]

        md = render_slice(query, results, working)
        if raw_json:
            print(json.dumps({"content": md, "query": query, "scope_id": scope}))
        else:
            print(md)
        return

    if cmd == "--mine-session" and len(argv) >= 3:
        try:
            print(json.dumps(mine_session(argv[2])))
        except MiningError as exc:
            print(str(exc), file=sys.stderr)
            exit_code = FATAL_EXIT_CODE if isinstance(exc, FatalMiningError) else TRANSIENT_EXIT_CODE
            raise SystemExit(exit_code)
        return

    if cmd == "--mine-all":
        from memem.session_state import clear_installed_at
        from memem.storage import MINER_OPT_IN_MARKER
        clear_installed_at()  # Mine ALL sessions, including pre-install history
        # Explicit --mine-all is an opt-in signal; create the marker so the
        # miner daemon will also auto-start on future server boots.
        MINER_OPT_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        MINER_OPT_IN_MARKER.touch()
        try:
            print(json.dumps(mine_all()))
        except MiningError as exc:
            print(str(exc), file=sys.stderr)
            exit_code = FATAL_EXIT_CODE if isinstance(exc, FatalMiningError) else TRANSIENT_EXIT_CODE
            raise SystemExit(exit_code)
        return

    if cmd == "--miner-opt-in":
        from memem.storage import MINER_OPT_IN_MARKER, _auto_start_miner
        MINER_OPT_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        MINER_OPT_IN_MARKER.touch()
        _auto_start_miner()
        print("memem miner opted in and started. Future server boots will auto-start it.")
        return

    if cmd == "--miner-opt-out":
        from memem.storage import MINER_OPT_IN_MARKER
        wrapper = str(Path(__file__).resolve().parent / "miner-wrapper.sh")
        subprocess.run(["bash", wrapper, "stop"], capture_output=True)
        MINER_OPT_IN_MARKER.unlink(missing_ok=True)
        print("memem miner opted out and stopped. It will not auto-start until you run --miner-opt-in or /memem-mine.")
        return

    if cmd == "--purge-mined":
        print(json.dumps(purge_mined_memories(MINED_SESSIONS_FILE)))
        return

    if cmd == "--install-cron":
        cron_script = str(Path(__file__).resolve().parent / "mine-cron.sh")
        cron_entry = f"0 * * * * bash {cron_script}"
        crontab_result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = crontab_result.stdout if crontab_result.returncode == 0 else ""
        if "mine-cron.sh" in existing:
            print("Cron already installed.")
        else:
            new_crontab = existing.rstrip("\n") + ("\n" if existing else "") + cron_entry + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            print(f"Installed: {cron_entry}")
        return

    if cmd == "--rebuild-index":
        _generate_index()
        print(f"Index rebuilt: {INDEX_PATH}")
        return

    if cmd == "--rebuild-playbooks":
        from memem.playbook import _playbook_sweep
        force = "--force" in argv
        print("Sweeping playbooks" + (" (force)" if force else "") + "...")
        totals = _playbook_sweep(force=force)
        print(
            f"Playbook sweep complete: "
            f"refreshed={totals['refreshed']} "
            f"unchanged={totals['noop']} "
            f"below-threshold={totals['skipped']} "
            f"failed={totals['failed']}"
        )
        return

    if cmd == "--rebuild-search-index":
        from memem.search_index import _rebuild_search_index
        count = _rebuild_search_index()
        print(f"Search index rebuilt: {count} memories indexed")
        return

    if cmd in ("graph", "--graph") and len(argv) >= 3:
        action = argv[2]
        if action == "rebuild":
            from memem.graph_index import _rebuild_graph
            graph_scope: str | None = argv[3] if len(argv) >= 4 else None
            count = _rebuild_graph(scope_id=graph_scope)
            print(f"Graph rebuilt: {count} edges")
            return
        if action == "audit":
            from memem.graph_index import format_graph_audit
            print(format_graph_audit())
            return
        if action == "stats":
            from memem.graph_index import _graph_stats
            print(json.dumps(_graph_stats(), indent=2, sort_keys=True))
            return
        if action == "neighbors" and len(argv) >= 4:
            from memem.graph_index import format_graph_neighbors
            include_history = "--history" in argv
            print(format_graph_neighbors(argv[3], include_history=include_history))
            return
        raise SystemExit("Usage: memem graph {rebuild|audit|stats|neighbors <memory_id> [--history]}")

    if cmd == "--migrate-schema":
        from memem.obsidian_store import _obsidian_memories, _write_obsidian_memory
        from memem.telemetry import _log_event

        migrated = 0
        already = 0
        for mem in _obsidian_memories(include_deprecated=True):
            current_version = mem.get("schema_version", 0)
            if current_version >= 1:
                already += 1
                continue
            # Upgrade: set schema_version, fill missing fields
            mem["schema_version"] = 1
            if "status" not in mem:
                mem["status"] = "active"
            if "source_type" not in mem:
                # Infer from tags
                if "mined" in mem.get("domain_tags", []):
                    mem["source_type"] = "mined"
                elif "imported" in mem.get("domain_tags", []):
                    mem["source_type"] = "import"
                else:
                    mem["source_type"] = "user"
            if "importance" not in mem:
                mem["importance"] = 3
            _write_obsidian_memory(mem)
            _log_event("migrate", mem.get("id", ""), from_version=current_version, to_version=1)
            migrated += 1

        print(f"Schema migration: {migrated} upgraded, {already} already at v1")
        return

    if cmd == "--eval":
        from memem.eval import run_eval
        run_eval()
        return

    if cmd == "eval" and len(argv) >= 3:
        # `memem eval export [--since 7d] [--out PATH]`
        # `memem eval replay [--against PATH] [--k 5]`
        # `memem eval status`
        sub = argv[2]
        if sub == "status":
            from memem.eval_capture import EVAL_CAPTURE_FILE, is_enabled, load_captures
            print(f"capture enabled: {is_enabled()} (set MEMEM_EVAL_CAPTURE=1 to enable)")
            print(f"capture file:    {EVAL_CAPTURE_FILE}")
            if EVAL_CAPTURE_FILE.exists():
                rows = load_captures()
                print(f"captured rows:   {len(rows)}")
                if rows:
                    print(f"oldest:          {rows[0].get('ts','?')}")
                    print(f"newest:          {rows[-1].get('ts','?')}")
            else:
                print("captured rows:   0 (file does not exist)")
            return

        if sub == "export":
            from memem.eval_capture import load_captures

            since_seconds = None
            output_path: Path | None = None
            i = 3
            while i < len(argv):
                arg = argv[i]
                if arg == "--since" and i + 1 < len(argv):
                    spec = argv[i + 1]
                    # Accept "7d", "24h", "30m", or raw seconds
                    if spec.endswith("d"):
                        since_seconds = float(spec[:-1]) * 86400
                    elif spec.endswith("h"):
                        since_seconds = float(spec[:-1]) * 3600
                    elif spec.endswith("m"):
                        since_seconds = float(spec[:-1]) * 60
                    else:
                        since_seconds = float(spec)
                    i += 2
                elif arg == "--out" and i + 1 < len(argv):
                    output_path = Path(argv[i + 1])
                    i += 2
                else:
                    i += 1
            rows = load_captures(since_seconds=since_seconds)
            if output_path:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, sort_keys=True))
                        fh.write("\n")
                print(f"Wrote {len(rows)} captures to {output_path}", file=sys.stderr)
            else:
                for row in rows:
                    print(json.dumps(row, sort_keys=True))
            return

        if sub == "replay":
            from memem.eval_replay import format_replay_report, replay

            baseline: Path | None = None
            k = 5
            i = 3
            while i < len(argv):
                arg = argv[i]
                if arg == "--against" and i + 1 < len(argv):
                    baseline = Path(argv[i + 1])
                    i += 2
                elif arg == "--k" and i + 1 < len(argv):
                    k = int(argv[i + 1])
                    i += 2
                else:
                    i += 1
            if not baseline or not baseline.exists():
                raise SystemExit("Usage: memem eval replay --against <baseline.ndjson> [--k 5]")
            result = replay(baseline, k=k)
            print(format_replay_report(result))
            return

        raise SystemExit(f"Unknown eval subcommand: {sub} (try: status, export, replay)")

    if cmd == "--doctor":
        from memem.capabilities import detect_capabilities, pretty_report, write_capabilities
        caps = detect_capabilities()
        write_capabilities(caps)
        print(pretty_report(caps))
        # SQLite integrity check on all 3 WAL DBs (search / graph / session-state).
        # Surfaces corruption that PRAGMA integrity_check can detect: torn pages,
        # malformed btree, page-checksum failures (WAL mode does NOT itself ensure
        # this; it just gives concurrent-access semantics).
        integrity_failed = _run_integrity_check(verbose=True)
        blockers = not caps.get("mcp") or not caps.get("writable_state_dir") or not caps.get("writable_vault") or integrity_failed
        raise SystemExit(1 if blockers else 0)

    if cmd == "--integrity-check":
        integrity_failed = _run_integrity_check(verbose=True)
        raise SystemExit(1 if integrity_failed else 0)

    if cmd == "--events":
        from memem.models import EVENT_LOG
        if not EVENT_LOG.exists():
            print("No events yet.")
            return
        lines = EVENT_LOG.read_text().splitlines()
        for line in lines[-20:]:  # Last 20 events
            try:
                event = json.loads(line)
                ts = event.get("timestamp", "?")[:19]
                op = event.get("op", "?")
                mid = event.get("memory_id", "")[:8]
                detail = ""
                for k, v in event.items():
                    if k not in ("op", "memory_id", "timestamp"):
                        detail += f" {k}={v}"
                print(f"  [{ts}] {op:10} {mid}{detail}")
            except Exception:
                print(f"  {line[:80]}")
        return

    if cmd == "--assemble-context":
        query = argv[2] if len(argv) >= 3 else ""
        project = argv[3] if len(argv) >= 4 else "default"
        if not query:
            print("No query provided.")
            return
        from memem.assembly import context_assemble
        print(context_assemble(query, project))
        return

    if cmd == "--status":
        from memem.models import (
            EVENT_LOG,
            MEMEM_DIR,
            OBSIDIAN_VAULT,
            PLAYBOOK_DIR,
            SEARCH_DB,
        )
        from memem.obsidian_store import _obsidian_memories
        # Memory stats
        all_mems = _obsidian_memories(include_deprecated=True)
        active = sum(1 for m in all_mems if m.get("status", "active") == "active")
        deprecated = sum(1 for m in all_mems if m.get("status") == "deprecated")
        mined = sum(1 for m in all_mems if m.get("source_type") == "mined")
        user = sum(1 for m in all_mems if m.get("source_type") == "user")
        projects = len(set(m.get("project", "general") for m in all_mems))
        with_related = sum(1 for m in all_mems if m.get("related"))

        print("memem Status")
        print("=" * 40)
        print(f"  Vault:     {OBSIDIAN_VAULT}")
        print(f"  Data:      {MEMEM_DIR}")
        print(f"  Memories:  {active} active, {deprecated} deprecated")
        print(f"  Sources:   {mined} mined, {user} user")
        print(f"  Projects:  {projects}")
        print(f"  Linked:    {with_related} ({with_related * 100 // len(all_mems) if all_mems else 0}%)")
        print(f"  Search DB: {'yes' if SEARCH_DB.exists() else 'no'} ({SEARCH_DB.stat().st_size // 1024}KB)" if SEARCH_DB.exists() else "  Search DB: not built")
        print(f"  Playbooks: {len(list(PLAYBOOK_DIR.glob('*.md')))} projects" if PLAYBOOK_DIR.exists() else "  Playbooks: none")
        print(f"  Events:    {sum(1 for _ in open(EVENT_LOG)) if EVENT_LOG.exists() else 0} logged")

        # Miner status
        wrapper = str(Path(__file__).resolve().parent / "miner-wrapper.sh")
        miner_status = subprocess.run(["bash", wrapper, "status"], capture_output=True, text=True, timeout=5)
        print(f"  Miner:     {miner_status.stdout.strip()}")
        print("=" * 40)
        return

    if cmd in ("--miner-start", "--miner-stop", "--miner-status"):
        from memem.storage import MINER_OPT_IN_MARKER
        wrapper = str(Path(__file__).resolve().parent / "miner-wrapper.sh")
        action = cmd.replace("--miner-", "")
        if action == "start":
            # Starting the miner is an opt-in signal.
            MINER_OPT_IN_MARKER.parent.mkdir(parents=True, exist_ok=True)
            MINER_OPT_IN_MARKER.touch()
        subprocess.run(["bash", wrapper, action])
        return

    if cmd == "--consolidate":
        from memem.consolidation import run_consolidation_pass
        from memem.models import LAYER_L2

        dry_run = "--dry-run" in argv
        layer = LAYER_L2
        min_cluster_size = 3
        threshold = 0.85

        # Parse optional overrides
        i = 2
        while i < len(argv):
            arg = argv[i]
            if arg == "--layer" and i + 1 < len(argv):
                try:
                    layer = int(argv[i + 1])
                except ValueError:
                    pass
                i += 2
            elif arg == "--min-cluster" and i + 1 < len(argv):
                try:
                    min_cluster_size = int(argv[i + 1])
                except ValueError:
                    pass
                i += 2
            elif arg == "--threshold" and i + 1 < len(argv):
                try:
                    threshold = float(argv[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1

        mode = "DRY-RUN" if dry_run else "LIVE"
        print(
            f"[memem consolidation] {mode} — layer={layer} "
            f"min_cluster={min_cluster_size} threshold={threshold}"
        )
        cons_result = run_consolidation_pass(
            layer=layer,
            min_cluster_size=min_cluster_size,
            similarity_threshold=threshold,
            dry_run=dry_run,
        )
        print(f"  Clusters processed:        {cons_result.clusters_processed}")
        print(f"  Memories consolidated:     {cons_result.memories_consolidated}")
        print(f"  Contradictions flagged:    {cons_result.contradictions_flagged}")
        print(f"  Canonical memories created:{len(cons_result.canonical_memories_created)}")
        print(f"  Superseded memories:       {len(cons_result.superseded_memories)}")
        if cons_result.errors:
            print(f"  Errors ({len(cons_result.errors)}):")
            for err in cons_result.errors:
                print(f"    - {err}")
        if dry_run:
            print("  (Pass without --dry-run to execute)")
        return

    if cmd == "--dream":
        from memem.dreamer import run_dream_cycle
        apply = "--apply" in argv
        result = run_dream_cycle(dry_run=not apply)
        diff = result["diff"]
        apply_result = result["apply_result"]
        mode = "DRY-RUN" if result["dry_run"] else "APPLIED"
        print(f"[memem dreamer] {mode}")
        print(f"  Diff log:            {result['diff_path']}")
        print(f"  Vault size:          {diff['vault_size']}")
        print(f"  Demotion candidates: {len(diff['demotion_candidates'])}")
        print(f"  Contradiction pairs: {len(diff['contradiction_pairs'])}")
        print(f"  Cluster summaries:   {len(diff['cluster_summaries'])}")
        if apply_result:
            print(f"  Demoted:             {apply_result['demoted']}")
            print(f"  Invalidated:         {apply_result['invalidated']}")
            if apply_result.get("errors"):
                print(f"  Errors ({len(apply_result['errors'])}):")
                for err in apply_result["errors"]:
                    print(f"    - {err}")
        if result["dry_run"]:
            print("  (Pass --apply to execute proposals)")
        return

    if cmd == "--record-lesson":
        # A-MemGuard: record a lesson (anti-memory annotation).
        # Usage: --record-lesson --memory-id <id> --query-class <class>
        #        --anti-pattern <desc> [--evidence <text>] [--source manual|user|dreamer]
        memory_id = ""
        query_class = ""
        anti_pattern = ""
        evidence = ""
        source = "manual"
        i = 2
        while i < len(argv):
            arg = argv[i]
            if arg == "--memory-id" and i + 1 < len(argv):
                memory_id = argv[i + 1]
                i += 2
            elif arg == "--query-class" and i + 1 < len(argv):
                query_class = argv[i + 1]
                i += 2
            elif arg == "--anti-pattern" and i + 1 < len(argv):
                anti_pattern = argv[i + 1]
                i += 2
            elif arg == "--evidence" and i + 1 < len(argv):
                evidence = argv[i + 1]
                i += 2
            elif arg == "--source" and i + 1 < len(argv):
                source = argv[i + 1]
                i += 2
            else:
                i += 1
        if not memory_id or not query_class or not anti_pattern:
            raise SystemExit(
                "Usage: --record-lesson --memory-id <id> --query-class <class> "
                "--anti-pattern <desc> [--evidence <text>] [--source manual|user|dreamer]"
            )
        from memem.lessons import record_lesson
        lesson_id = record_lesson(
            targeted_memory_id=memory_id,
            query_class=query_class,
            anti_pattern=anti_pattern,
            evidence=evidence,
            source=source,
        )
        print(f"Lesson recorded: {lesson_id}")
        return

    if cmd is None:
        _register_server_pid()
        mcp.run(transport="stdio")
        return

    raise SystemExit(f"Unknown command: {cmd}")
