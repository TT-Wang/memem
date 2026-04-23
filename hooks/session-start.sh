#!/usr/bin/env bash
# memem SessionStart hook — primes slice-first runtime context before first input.
#
# Flow:
#   1. Parse session_id + cwd from hook stdin
#   2. Generate a slice-first prompt context for the current project scope
#   3. If non-empty, write .last-brief.json with primed=true
#   4. Emit the rendered slice as additionalContext
#
# Silent by default; MEMEM_SHOW_BANNER=1 prepends a short status banner.

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"

emit_empty() {
    echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
    exit 0
}

if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    emit_empty
fi

mkdir -p "$MEMEM_DIR" 2>/dev/null || true

INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE" || true

"${MEMEM_PYTHON:-python3}" - "$PLUGIN_ROOT" "$INPUT_FILE" "$MEMEM_DIR" << 'HOOKPY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

plugin_root = sys.argv[1]
input_file = Path(sys.argv[2])
memem_dir = Path(sys.argv[3])

EMPTY_RESPONSE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "",
    }
})


def emit_empty() -> None:
    print(EMPTY_RESPONSE)
    raise SystemExit(0)


def detect_scope(cwd: str) -> str:
    trimmed = (cwd or "").rstrip("/")
    home = str(Path.home()).rstrip("/")
    if not trimmed or trimmed in {"", "/", home}:
        return "default"
    return os.path.basename(trimmed) or "default"


def parse_budget() -> int:
    raw = os.environ.get("MEMEM_SESSION_START_PROMPT_BUDGET", "4000").strip()
    try:
        value = int(raw)
    except ValueError:
        return 4000
    return max(1000, min(value, 12000))


def memory_count() -> int:
    vault_root = Path(os.environ.get("MEMEM_OBSIDIAN_VAULT", str(Path.home() / "obsidian-brain")))
    memories_dir = vault_root / "memem" / "memories"
    try:
        return len(list(memories_dir.glob("*.md")))
    except OSError:
        return 0


if not plugin_root or plugin_root == "${CLAUDE_PLUGIN_ROOT}":
    emit_empty()

sys.path.insert(0, plugin_root)

try:
    hook = json.loads(input_file.read_text() or "{}")
except Exception:
    hook = {}

session_id = str(hook.get("session_id", "") or "")
cwd = str(hook.get("cwd") or os.environ.get("PWD") or os.getcwd())
scope = detect_scope(cwd)
query = "Prime the current working state for this session"
if scope != "default":
    query += f" in project {scope}"

environment = {
    "session_id": session_id,
    "task_mode": "session_start",
    "repo_path": cwd,
    "cwd": cwd,
    "scope_id": scope,
    "prompt_budget_chars": parse_budget(),
}

try:
    from memem.active_slice_engine import generate_prompt_context

    content = generate_prompt_context(
        query,
        scope_id=scope,
        environment=environment,
        use_llm=False,
        mode="slice",
    )
except Exception:
    content = ""

if not content:
    emit_empty()

banner = ""
if os.environ.get("MEMEM_SHOW_BANNER", "0") == "1":
    banner = f"[memem] {memory_count()} memories · slice-first runtime active\n\n"

if session_id:
    try:
        memem_dir.mkdir(parents=True, exist_ok=True)
        (memem_dir / ".last-brief.json").write_text(json.dumps({
            "session_id": session_id,
            "keywords": [],
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "primed": True,
        }))
    except OSError:
        pass

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": banner + content,
    }
}))
HOOKPY
