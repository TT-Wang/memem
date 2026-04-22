import json
import subprocess
import sys
from pathlib import Path

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

    if cmd in ("active-slice", "--active-slice"):
        raw_json = "--json" in argv
        use_llm = "--no-llm" not in argv
        scope = "default"
        query_parts = []
        skip_next = False
        for idx, arg in enumerate(argv[2:], start=2):
            if skip_next:
                skip_next = False
                continue
            if arg == "--json" or arg == "--no-llm":
                continue
            if arg == "--scope":
                try:
                    scope = argv[idx + 1]
                    skip_next = True
                except IndexError:
                    pass
                continue
            query_parts.append(arg)
        query = " ".join(query_parts).strip()
        if not query:
            print("No query provided.")
            return
        from memem.active_slice_engine import active_slice_response
        print(active_slice_response(query, scope_id=scope, use_llm=use_llm, raw_json=raw_json))
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
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
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
            scope = argv[3] if len(argv) >= 4 else None
            count = _rebuild_graph(scope_id=scope)
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

    if cmd == "--doctor":
        from memem.capabilities import detect_capabilities, pretty_report, write_capabilities
        caps = detect_capabilities()
        write_capabilities(caps)
        print(pretty_report(caps))
        blockers = not caps.get("mcp") or not caps.get("writable_state_dir") or not caps.get("writable_vault")
        raise SystemExit(1 if blockers else 0)

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
        result = subprocess.run(["bash", wrapper, "status"], capture_output=True, text=True, timeout=5)
        print(f"  Miner:     {result.stdout.strip()}")
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

    if cmd is None:
        _register_server_pid()
        mcp.run(transport="stdio")
        return

    raise SystemExit(f"Unknown command: {cmd}")
