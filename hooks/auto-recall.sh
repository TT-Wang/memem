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
VAULT="${CORTEX_OBSIDIAN_VAULT:-$HOME/obsidian-brain}"
INDEX="$VAULT/cortex/_index.md"
if [ -f "$INDEX" ]; then
  python3 - "$INDEX" "$VAULT" << 'HOOKPY'
import sys, json
from pathlib import Path

index_path = sys.argv[1]
vault = sys.argv[2]
index = Path(index_path).read_text()
suffix = (
    "Above is your memory index. IMPORTANT: Do not just skim the titles — "
    "actively use the Cortex MCP tools (memory_recall, memory_list) to fetch "
    "full content of memories relevant to the user's request. The index is a "
    "lookup table, not the knowledge itself. You can also read Obsidian markdown "
    "files directly at " + vault + "/cortex/memories/<filename>.md "
    "(filename is slugified title + ID, e.g. cortex-uses-pytest-for-testing-43a80bdd.md). "
    "Default behavior: identify relevant memories from the index, then recall their "
    "full content before responding."
)
output = "Cortex memory index (your full memory catalog):\n\n" + index + "\n\n---\n" + suffix
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": output
    }
}))
HOOKPY
fi
