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
if [ "${MEMEM_INJECTION_MODE:-tool}" = "tool" ]; then
    # v2.4.0 Phase 4.5 fix: telemetry skip log written directly via bash
    # (NOT python3 -c) to avoid 80-200ms cold-start subprocess on every
    # UserPromptSubmit. POSIX guarantees atomic O_APPEND for writes under
    # PIPE_BUF (4096B); this 160-byte line is well within bounds.
    {
        TS=$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
        MEMEM_DIR_RESOLVED="${MEMEM_DIR:-$HOME/.memem}"
        mkdir -p "$MEMEM_DIR_RESOLVED" 2>/dev/null
        printf '{"ts":"%s","call_type":"hook_tool_skip","query":"","returned_ids":[],"latency_ms":0,"source":"hook"}\n' \
            "$TS" >> "$MEMEM_DIR_RESOLVED/.recall_log.jsonl" 2>/dev/null
    } 2>/dev/null || true
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
