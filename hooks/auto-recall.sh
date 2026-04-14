#!/usr/bin/env bash
# memem auto-recall hook — topic-shift detection on UserPromptSubmit.
#
# Flow:
#   1. Load .last-brief.json (keyword set from previous message)
#   2. Compute keyword overlap with current message
#   3. If overlap < MEMEM_TOPIC_SHIFT_THRESHOLD (default 0.30), re-fire assembly
#   4. Always update .last-brief.json with current message keywords
#
# Silent (empty context) when topic is unchanged.

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
mkdir -p "$MEMEM_DIR"

# Read hook input from stdin
INPUT=$(cat)

# Extract session_id and message
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
MESSAGE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message', d.get('query','')))" 2>/dev/null || echo "")

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
LAST_BRIEF="${MEMEM_DIR}/.last-brief.json"
TOPIC_LOG="${MEMEM_DIR}/topic-shifts.log"

# Inline Python: keyword overlap computation + conditional assembly
python3 - "$PLUGIN_ROOT" "$SESSION_ID" "$MESSAGE" "$LAST_BRIEF" "$TOPIC_LOG" "$MEMEM_DIR" << 'HOOKPY'
import sys, json, os, subprocess, re
from pathlib import Path
from datetime import datetime

plugin_root = sys.argv[1]
session_id  = sys.argv[2]
message     = sys.argv[3]
last_brief  = Path(sys.argv[4])
topic_log   = Path(sys.argv[5])
memem_dir   = Path(sys.argv[6])

STOPWORDS = {
    "a","an","the","is","are","was","were","be","been","being",
    "do","does","did","have","has","had",
    "i","you","he","she","it","we","they",
    "this","that","these","those",
    "and","or","but","not",
    "to","of","in","on","at","for","with","by","as","from",
}

EMPTY_RESPONSE = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ""
    }
})

def tokenize(text: str) -> set:
    words = re.split(r'\W+', text.lower())
    return {w for w in words if w and w not in STOPWORDS and len(w) > 1}

def emit_empty():
    print(EMPTY_RESPONSE)
    sys.exit(0)

# If no plugin root, we cannot assemble — emit empty
if not plugin_root or plugin_root == '${CLAUDE_PLUGIN_ROOT}':
    emit_empty()

if not message:
    emit_empty()

current_keywords = tokenize(message)

# Load last brief state
last_data = {}
if last_brief.exists():
    try:
        last_data = json.loads(last_brief.read_text())
    except Exception:
        last_data = {}

last_keywords = set(last_data.get("keywords", []))
last_session  = last_data.get("session_id", "")

# Compute overlap ratio
if last_keywords and last_session == session_id:
    smaller = min(len(current_keywords), len(last_keywords))
    if smaller > 0:
        overlap = len(current_keywords & last_keywords) / smaller
    else:
        overlap = 0.0
    is_first_message = False
else:
    # Different session or no prior data — always trigger
    overlap = 0.0
    is_first_message = True

threshold = float(os.environ.get("MEMEM_TOPIC_SHIFT_THRESHOLD", "0.3"))

# Update .last-brief.json regardless of trigger
try:
    last_brief.parent.mkdir(parents=True, exist_ok=True)
    last_brief.write_text(json.dumps({
        "session_id": session_id,
        "keywords": sorted(current_keywords),
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
except Exception:
    pass

# If overlap is sufficient, stay silent
if not is_first_message and overlap >= threshold:
    emit_empty()

# --- Topic shift triggered (or first message) ---

# Log the shift
try:
    topic_log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    snippet = message[:100].replace('"', "'")
    with topic_log.open("a") as fh:
        fh.write(f'{ts} session={session_id} overlap={overlap:.2f} msg="{snippet}"\n')
except Exception:
    pass

# Run context assembly
assembled = ""
if plugin_root:
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "memem.server", "--assemble-context", message, "default"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            assembled = result.stdout.strip()
    except Exception as exc:
        pass

if not assembled:
    emit_empty()

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": assembled,
    }
}))
HOOKPY
