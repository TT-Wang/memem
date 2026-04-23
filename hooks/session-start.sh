#!/usr/bin/env bash
# memem SessionStart hook — fires before the user types the first message.
#
# v0.11.0 "session-start token diet":
#   - Injects top-N memories (default 50 via MEMEM_SESSION_START_LIMIT)
#   - Top 5 get full content, rest are compact-index lines
#   - Scoped to current project by default (cwd basename)
#   - Seeds ~/.memem/.last-brief.json with `primed=true` so the
#     immediately-following UserPromptSubmit hook skips context_assemble
#     (avoids double-fire on first turn).
#
# Silent by default; MEMEM_SHOW_BANNER=1 for a one-line status banner.

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
PYBIN="${MEMEM_PYTHON:-python3}"

emit_empty() {
    echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
    exit 0
}

# If plugin root is missing or unexpanded, stay silent
if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    emit_empty
fi

mkdir -p "$MEMEM_DIR" 2>/dev/null || true

# Read hook input stdin (may contain session_id)
INPUT=$(cat 2>/dev/null || echo "{}")

# Parse session_id for the primed-marker write
SESSION_ID=$(printf '%s' "$INPUT" | "$PYBIN" -c "
import sys, json
try:
    print(json.load(sys.stdin).get('session_id',''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Run the compact-index helper and capture stdout to a tempfile
BRIEF_FILE=$(mktemp)
trap 'rm -f "$BRIEF_FILE"' EXIT
PYTHONPATH="$PLUGIN_ROOT" "$PYBIN" -m memem.server --compact-index \
    > "$BRIEF_FILE" 2>/dev/null || true

if [ ! -s "$BRIEF_FILE" ]; then
    emit_empty
fi

# Optional status banner — prepended to the brief
BANNER=""
if [ "${MEMEM_SHOW_BANNER:-0}" = "1" ]; then
    MEM_COUNT=$(find "${MEMEM_OBSIDIAN_VAULT:-$HOME/obsidian-brain}/memem/memories" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    BANNER="[memem] $MEM_COUNT memories · /memem for status

"
fi

# Seed .last-brief.json with primed=true so the first UserPromptSubmit
# skips context_assemble (SessionStart already injected the same material).
if [ -n "$SESSION_ID" ]; then
    "$PYBIN" - "$MEMEM_DIR/.last-brief.json" "$SESSION_ID" << 'PRIMEPY' 2>/dev/null || true
import sys, json
from pathlib import Path
from datetime import datetime, timezone
last_brief = Path(sys.argv[1])
session_id = sys.argv[2]
last_brief.parent.mkdir(parents=True, exist_ok=True)
last_brief.write_text(json.dumps({
    "session_id": session_id,
    "keywords": [],
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "primed": True,
}))
PRIMEPY
fi

# Emit the brief via Python for safe multiline escaping
"$PYBIN" - "$BRIEF_FILE" "$BANNER" << 'HOOKPY'
import sys, json
from pathlib import Path
brief = Path(sys.argv[1]).read_text()
banner = sys.argv[2]
content = banner + brief
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": content,
    }
}))
HOOKPY
