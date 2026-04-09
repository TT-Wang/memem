#!/usr/bin/env bash
# Cortex auto-recall hook — injects memory index into context.
# Fires on UserPromptSubmit. Only runs once per session (first message).
#
# Flow: inject _index.md → Opus reads it → calls memory tools if needed

set -euo pipefail

CORTEX_DIR="$HOME/.cortex"
SESSION_MARKER_DIR="$CORTEX_DIR/.sessions"
mkdir -p "$SESSION_MARKER_DIR"

# Read hook input from stdin
INPUT=$(cat)

# Extract session_id
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Only run on first message of session
MARKER="$SESSION_MARKER_DIR/$SESSION_ID"
if [ -n "$SESSION_ID" ] && [ -f "$MARKER" ]; then
  exit 0
fi

# Mark session as recalled
if [ -n "$SESSION_ID" ]; then
  touch "$MARKER"
fi

# Clean up old session markers (older than 7 days)
find "$SESSION_MARKER_DIR" -type f -mtime +7 -delete 2>/dev/null || true

# Inject the memory index directly — let Opus decide what to read deeper
INDEX="$HOME/obsidian-brain/cortex/_index.md"
if [ -f "$INDEX" ]; then
  CONTENT=$(cat "$INDEX")
  python3 -c "
import sys, json
index = sys.argv[1]
output = 'Cortex memory index (your full memory catalog):\n\n' + index
output += '\n\n---\nAbove is your memory index. Use mcp__plugin_cortex_cortex__memory_recall to read full content of any memory that seems relevant to the user'\''s request. After completing significant work, save non-obvious lessons using mcp__plugin_cortex_cortex__memory_save.'
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'additionalContext': output
    }
}))
" "$CONTENT"
fi
