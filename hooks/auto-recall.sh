#!/usr/bin/env bash
# Cortex auto-recall hook — reads user's prompt from stdin, recalls relevant memories.
# Fires on UserPromptSubmit. Only runs once per session (first message).

set -euo pipefail

CORTEX_DIR="$HOME/.cortex"
SESSION_MARKER_DIR="$CORTEX_DIR/.sessions"
mkdir -p "$SESSION_MARKER_DIR"

# Read hook input from stdin
INPUT=$(cat)

# Extract session_id and prompt
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
PROMPT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('prompt',''))" 2>/dev/null || echo "")

# Skip if no prompt
if [ -z "$PROMPT" ]; then
  exit 0
fi

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

# Get the server.py path (relative to this script)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER="$SCRIPT_DIR/../cortex-mcp-server/server.py"

# Use Haiku to rewrite the prompt into vector-DB-friendly search keywords
SMART_QUERY=$(claude -p --model haiku --tools "" \
  --system-prompt "You are a keyword extractor. Output ONLY search phrases, never full sentences or explanations." \
  "TASK: Convert this into 3-5 search phrases for a coding knowledge database. Each phrase should be 3-6 words, keyword-rich. Include project names and technical terms mentioned or implied.

NO markdown. NO explanations. Just the search phrases, one per line.

User message: $PROMPT" 2>/dev/null || echo "$PROMPT")

# Fallback to raw prompt if Haiku returned empty
if [ -z "$SMART_QUERY" ]; then
  SMART_QUERY="$PROMPT"
fi

# Call memory_recall with the smart query
RESULT=$(python3 "$SERVER" --recall "$SMART_QUERY" 2>/dev/null || echo "")

# If we got results, inject as additional context
if [ -n "$RESULT" ] && [ "$RESULT" != "No query provided." ]; then
  python3 -c "
import sys, json
context = sys.argv[1]
output = 'Cortex memory (recalled automatically):\n\n' + context
output += '\n\n---\nAfter completing significant work this session, save non-obvious lessons, decisions, or patterns using mcp__plugin_cortex_cortex__memory_save. One insight per save. Do not save trivial facts or things already in code.'
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'additionalContext': output
    }
}))
" "$RESULT"
fi
