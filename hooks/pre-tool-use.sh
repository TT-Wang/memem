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
if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}'
    exit 0
fi

INPUT=$(cat)

# Inline python: parse tool_name and tool_input, search for file_path, inject matches
python3 - "$PLUGIN_ROOT" <<'PYEOF'
import json, sys, os, subprocess

plugin_root = sys.argv[1]
try:
    hook = json.loads(sys.stdin.read())
except Exception:
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}))
    sys.exit(0)

tool_name = hook.get("tool_name") or hook.get("toolName") or ""
tool_input = hook.get("tool_input") or hook.get("toolInput") or {}

# Only gate on Read tool
if tool_name != "Read":
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}))
    sys.exit(0)

file_path = tool_input.get("file_path") or tool_input.get("filePath") or ""
if not file_path:
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}))
    sys.exit(0)

# Search memories for this file path (use basename as query for broader matches)
import os.path
basename = os.path.basename(file_path)
env = os.environ.copy()
env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
try:
    result = subprocess.run(
        [sys.executable, "-m", "memem.server", "--recall-smart", basename],
        capture_output=True, text=True, timeout=10, env=env,
    )
    brief = result.stdout.strip() if result.returncode == 0 else ""
except Exception:
    brief = ""

if not brief or "No memories found" in brief:
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}))
    sys.exit(0)

context = f"memem — relevant memories for `{basename}`:\n\n{brief}"
print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":context}}))
PYEOF
