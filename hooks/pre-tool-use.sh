#!/usr/bin/env bash
# memem PreToolUse hook — enriches Read tool calls with relevant memories.
# OPT-IN: only runs when MEMEM_PRETOOL_GATING=1 is set.

set -euo pipefail

# Fast exit if not opted in
if [ "${MEMEM_PRETOOL_GATING:-0}" != "1" ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}'
    exit 0
fi

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
PYBIN="${MEMEM_PYTHON:-python3}"
if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}'
    exit 0
fi

# Read stdin once, write to a temp file, pass path to python helper.
# Previous version consumed stdin into $INPUT and then tried to json.load(sys.stdin)
# in a heredoc — stdin was already drained so every call silently fell through
# to the empty-context fallback. v0.10.1 fix: pass via tempfile argv instead.
INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE"

"$PYBIN" - "$PLUGIN_ROOT" "$INPUT_FILE" <<'PYEOF'
import json, sys, os, subprocess
from pathlib import Path

plugin_root = sys.argv[1]
input_file = sys.argv[2]

def emit_empty():
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}))
    sys.exit(0)

try:
    hook = json.loads(Path(input_file).read_text())
except Exception:
    emit_empty()

tool_name = hook.get("tool_name") or hook.get("toolName") or ""
tool_input = hook.get("tool_input") or hook.get("toolInput") or {}

# Only gate on Read tool
if tool_name != "Read":
    emit_empty()

file_path = tool_input.get("file_path") or tool_input.get("filePath") or ""
if not file_path:
    emit_empty()

# Search memories for this file path (use basename as query for broader matches)
basename = os.path.basename(file_path)
env = os.environ.copy()
env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
try:
    # Use --recall (pure FTS) not --recall-smart (Haiku, 5-30s).
    # Cold-start cost: ~2-4s per call because memem.server imports 1k+ memories
    # into an index at process start. Timeout is 8s to leave margin. This is
    # why PreToolUse is opt-in (MEMEM_PRETOOL_GATING=1) and why it's marked
    # experimental — the per-Read latency cost is noticeable. A future release
    # will query search.db directly without importing memem.server.
    result = subprocess.run(
        [sys.executable, "-m", "memem.server", "--recall", basename],
        capture_output=True, text=True, timeout=8, env=env,
    )
    brief = result.stdout.strip() if result.returncode == 0 else ""
except Exception:
    brief = ""

if not brief or "No memories found" in brief:
    emit_empty()

context = f"memem — relevant memories for `{basename}`:\n\n{brief}"
print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":context}}))
PYEOF
