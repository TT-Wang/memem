#!/usr/bin/env bash
# memem SessionStart hook — fires before the user types.
# Injects L0 briefing + compact index via hookSpecificOutput.additionalContext.
# Silent by default; banner only when MEMEM_SHOW_BANNER=1.

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"

# If plugin root is missing, emit empty context (don't fail the session)
if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
    exit 0
fi

# Read stdin (session_id etc., not needed here but read so stdin closes)
INPUT=$(cat 2>/dev/null || echo "{}")

# Run the compact index helper and capture output to a temp file
BRIEF_FILE=$(mktemp)
trap 'rm -f "$BRIEF_FILE"' EXIT

PYTHONPATH="$PLUGIN_ROOT" python3 -m memem.server --compact-index > "$BRIEF_FILE" 2>/dev/null || true

if [ ! -s "$BRIEF_FILE" ]; then
    # No memories yet — stay silent (new user discovers via /memem)
    echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
    exit 0
fi

# Optional banner
if [ "${MEMEM_SHOW_BANNER:-0}" = "1" ]; then
    MEM_COUNT=$(find "${MEMEM_OBSIDIAN_VAULT:-$HOME/obsidian-brain}/memem/memories" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    BANNER_FILE=$(mktemp)
    trap 'rm -f "$BRIEF_FILE" "$BANNER_FILE"' EXIT
    echo "[memem] $MEM_COUNT memories · /memem for status" > "$BANNER_FILE"
    echo "" >> "$BANNER_FILE"
    cat "$BRIEF_FILE" >> "$BANNER_FILE"
    mv "$BANNER_FILE" "$BRIEF_FILE"
fi

# Emit as hookSpecificOutput JSON using python (safer escaping for multiline content)
python3 - "$BRIEF_FILE" << 'PYEOF'
import json, sys
from pathlib import Path
brief_path = sys.argv[1]
content = Path(brief_path).read_text()
print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":content}}))
PYEOF
