#!/usr/bin/env bash
# auto-recall.sh — v2.0.0
# UserPromptSubmit hook: read JSON envelope from stdin → extract query →
# call memem.retrieve + memem.render directly → return {additionalContext}.
# No daemon, no socket, no fallback chain.
# Disabled when MEMEM_INJECTION_MODE=tool.

set -euo pipefail

# Anti-recursion guard: skip if invoked from a memem-spawned headless claude -p call.
[ -n "${MEMEM_HOOK_DISABLE:-}" ] && exit 0

# Read envelope
INPUT=$(cat)

# Honor user opt-out
if [ "${MEMEM_INJECTION_MODE:-auto}" = "tool" ]; then
    echo '{}'
    exit 0
fi

# Find python — must have memem importable
PYTHON="${MEMEM_PYTHON:-python3}"

# Pass envelope via tempfile (env var hits OS ARG_MAX on huge prompts; tempfile is safe).
ENVELOPE_TMP=$(mktemp -t memem-hook-envelope.XXXXXX.json)
trap 'rm -f "$ENVELOPE_TMP"' EXIT
printf '%s' "$INPUT" > "$ENVELOPE_TMP"
export MEMEM_HOOK_ENVELOPE_PATH="$ENVELOPE_TMP"

# Also export plugin root for sys.path insertion
export MEMEM_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"

"$PYTHON" -c '
import json
import os
import sys

with open(os.environ["MEMEM_HOOK_ENVELOPE_PATH"], "r") as _f:
    envelope = json.load(_f)
query = (
    envelope.get("prompt")
    or envelope.get("user_prompt")
    or envelope.get("message")
    or envelope.get("query")
    or ""
).strip()
if not query:
    print(json.dumps({}))
    sys.exit(0)

plugin_root = os.environ.get("MEMEM_PLUGIN_ROOT", "")
if plugin_root:
    sys.path.insert(0, plugin_root)

try:
    from memem.retrieve import retrieve
    from memem.render import render_slice
except ImportError:
    # memem not on PYTHONPATH — silent no-op, dont break user prompts
    print(json.dumps({}))
    sys.exit(0)

try:
    results = retrieve(query, k=8)
except Exception:
    print(json.dumps({}))
    sys.exit(0)

working = {}
if envelope.get("task_mode"):
    working["task_mode"] = envelope["task_mode"]
if envelope.get("recent_actions"):
    working["recent_actions"] = list(envelope["recent_actions"])[:3]

try:
    md = render_slice(query, results, working)
except Exception:
    print(json.dumps({}))
    sys.exit(0)

# Optional opt-out: if rendered output has empty Relevant, skip
if "## Relevant (0" in md:
    print(json.dumps({}))
    sys.exit(0)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": md,
    }
}))
'
