import json
import subprocess
import sys
from pathlib import Path

from miner_protocol import FATAL_EXIT_CODE, TRANSIENT_EXIT_CODE
from mining import FatalMiningError, MiningError, mine_all, mine_session
from recall import smart_recall, memory_recall
from storage import INDEX_PATH, _register_server_pid, purge_mined_memories, _generate_index
from session_state import MINED_SESSIONS_FILE


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
        from storage import _obsidian_memories, _playbook_refine
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

    if cmd == "--eval":
        from eval import run_eval
        run_eval()
        return

    if cmd == "--assemble-context":
        query = argv[2] if len(argv) >= 3 else ""
        project = argv[3] if len(argv) >= 4 else "default"
        if not query:
            print("No query provided.")
            return
        from storage import context_assemble
        print(context_assemble(query, project))
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
