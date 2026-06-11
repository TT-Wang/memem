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

# Anti-recursion guard: skip if invoked from a memem-spawned headless claude -p call.
# Without this, every memem mining or tournament Haiku call recursively fires memem hooks → load explosion.
[ -n "${MEMEM_HOOK_DISABLE:-}" ] && exit 0

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

# `|| emit_empty`: if the python heredoc itself dies before emitting JSON
# (interpreter missing, import explosion), hand the harness a valid empty
# envelope instead of silence + non-zero exit under `set -e`.
"${MEMEM_PYTHON:-python3}" - "$PLUGIN_ROOT" "$INPUT_FILE" "$MEMEM_DIR" << 'HOOKPY' || emit_empty
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
    try:
        from memem.models import OBSIDIAN_MEMORIES_DIR
        memories_dir = OBSIDIAN_MEMORIES_DIR
    except ImportError:
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
# session-start context = three-block budgeted assembly (v2.8):
#   1. Profiles block (user + project profile, ~2400 chars)
#   2. ## Working rules (procedural memories, ~1200 chars)
#   3. ## Episode index (recent episodic memories, ~1600 chars, capped at 25)
# All blocks composed by render_session_start; total target ≤ 5200 chars.
content = ""
try:
    from memem.session_blocks import render_session_start
    content = render_session_start(scope) or ""
except Exception:  # noqa: BLE001 — never break SessionStart
    pass

# v2.0.0 Phase 4.5 fix: write .last-brief.json BEFORE the emit_empty() check.
# Otherwise the marker file is never written when content is empty (the v2.0.0
# default for session-start), and recall.py:_get_current_session_id always
# returns "", silently killing session-scoped recall telemetry across MCP tools.
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

if not content:
    emit_empty()

banner = ""
if os.environ.get("MEMEM_SHOW_BANNER", "0") == "1":
    banner = f"[memem] {memory_count()} memories · slice-first runtime active\n\n"

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": banner + content,
    }
}))
HOOKPY

# v2.1.0 stale-session sweep — catch sessions where Stop hook never fired (Claude crash, kill -9, etc.)
# Fire-and-forget detached mine_delta processes for un-mined JSONLs older than 10 min, cap at 3 parallel.

MEMEM_STATE_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
if [ -f "$MEMEM_STATE_DIR/.miner-opted-in" ]; then
    MINED_LIST="$MEMEM_STATE_DIR/.mined_sessions"
    touch "$MINED_LIST" 2>/dev/null || true
    SPAWNED=0
    for jsonl in $(find "$HOME/.claude/projects" -maxdepth 3 -name "*.jsonl" -type f -mmin +10 2>/dev/null); do
        [ "$SPAWNED" -ge 3 ] && break
        SID=$(basename "$jsonl" .jsonl)
        # Skip if already mined
        grep -Fxq "$SID" "$MINED_LIST" 2>/dev/null && continue
        # Skip mining artifacts: headless `claude -p` transcripts spawned by mine_delta/mining.
        # Detection = marker anywhere in the first 20 raw JSONL lines AND a small file
        # (headless calls produce only a handful of JSONL records — even a huge embedded
        # prompt lives inside ONE user line). The line-count conjunct protects real
        # conversations that merely QUOTE a marker phrase: a falsely-skipped session would
        # be PERMANENTLY excluded from auto-mining via the zombie guard below. Residual
        # risk: a <=30-line real session quoting a marker is skipped — recoverable via
        # `python3 -m memem.server --mine-session <id>`.
        # Note: cli.py --purge-contaminated additionally checks '<task-notification' —
        # intentional asymmetry: that marker identifies already-saved vault memories whose
        # episode titles inherited task-notification text, not transcript-level artifacts.
        if head -20 "$jsonl" 2>/dev/null | grep -qm1 -F -e '=== BEGIN CONVERSATION ===' -e 'Below is a coding conversation' \
           && [ "$(wc -l < "$jsonl" 2>/dev/null || echo 0)" -le 30 ]; then
            # Zombie-session guard: append to mined list so sweep ignores it on future SessionStart.
            grep -Fxq "$SID" "$MINED_LIST" 2>/dev/null || printf '%s\n' "$SID" >> "$MINED_LIST"
            continue
        fi
        # Spawn detached
        setsid nohup "${MEMEM_PYTHON:-python3}" -m memem.mine_delta --session-id "$SID" --transcript-path "$jsonl" </dev/null >/dev/null 2>&1 &
        disown 2>/dev/null || true
        SPAWNED=$((SPAWNED + 1))
    done
fi
