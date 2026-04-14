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
from memem.recall import memory_recall, smart_recall
from memem.session_state import MINED_SESSIONS_FILE
from memem.storage import _register_server_pid


def dispatch_cli(argv: list[str], mcp) -> None:
    cmd = argv[1] if len(argv) >= 2 else None

    if cmd == "--recall-smart":
        query = " ".join(argv[2:]) if len(argv) >= 3 else ""
        print(smart_recall(query) if query else "No query provided.")
        return

    if cmd == "--recall":
        query = " ".join(argv[2:]) if len(argv) >= 3 else ""
        print(memory_recall(query, limit=10) if query else "No query provided.")
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
        clear_installed_at()  # Mine ALL sessions, including pre-install history
        try:
            print(json.dumps(mine_all()))
        except MiningError as exc:
            print(str(exc), file=sys.stderr)
            exit_code = FATAL_EXIT_CODE if isinstance(exc, FatalMiningError) else TRANSIENT_EXIT_CODE
            raise SystemExit(exit_code)
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
        from memem.obsidian_store import _obsidian_memories
        from memem.playbook import _playbook_refine
        seen = set()
        count = 0
        for mem in _obsidian_memories():
            project = mem.get("project", "general")
            if project not in seen:
                seen.add(project)
                _playbook_refine(project)
                count += 1
                print(f"  Refined: {project}")
        print(f"Refined {count} playbooks")
        return

    if cmd == "--rebuild-search-index":
        from memem.search_index import _rebuild_search_index
        count = _rebuild_search_index()
        print(f"Search index rebuilt: {count} memories indexed")
        return

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
        wrapper = str(Path(__file__).resolve().parent / "miner-wrapper.sh")
        action = cmd.replace("--miner-", "")
        subprocess.run(["bash", wrapper, action])
        return

    if cmd is None:
        _register_server_pid()
        mcp.run(transport="stdio")
        return

    raise SystemExit(f"Unknown command: {cmd}")
