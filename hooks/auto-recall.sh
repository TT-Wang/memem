#!/usr/bin/env bash
# memem auto-recall hook — topic-shift detection on UserPromptSubmit.
#
# Flow:
#   1. Load .last-brief.json (keyword set from last successful briefing)
#   2. Compute keyword overlap with current message
#   3. If overlap < MEMEM_TOPIC_SHIFT_THRESHOLD (default 0.30), try active slice
#   4. ONLY on successful slice generation, update .last-brief.json
#   5. On failure/empty, leave .last-brief.json untouched so the next
#      prompt with similar keywords will retry — never silently starve
#
# Silent (empty context) when topic is unchanged.
#
# v0.10.2 fixes:
#   - Move .last-brief.json write AFTER successful assembly (was writing
#     before, which caused silent context starvation after any transient
#     Haiku failure)
#   - Pass the user message via tempfile instead of argv (was hitting
#     ARG_MAX on large pasted-log prompts)

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
mkdir -p "$MEMEM_DIR"

# Read hook input from stdin
INPUT=$(cat)

# Write raw input to a tempfile — Python helper reads from this, avoiding
# ARG_MAX limits on huge first prompts (pasted logs, large code blocks).
INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
printf '%s' "$INPUT" > "$INPUT_FILE"

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
LAST_BRIEF="${MEMEM_DIR}/.last-brief.json"
TOPIC_LOG="${MEMEM_DIR}/topic-shifts.log"

python3 - "$PLUGIN_ROOT" "$INPUT_FILE" "$LAST_BRIEF" "$TOPIC_LOG" "$MEMEM_DIR" << 'HOOKPY'
import sys, json, os, subprocess, re
from pathlib import Path
from datetime import datetime

plugin_root = sys.argv[1]
input_file  = Path(sys.argv[2])
last_brief  = Path(sys.argv[3])
topic_log   = Path(sys.argv[4])
memem_dir   = Path(sys.argv[5])

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

def detect_scope(hook: dict) -> str:
    cwd = hook.get("cwd") or os.environ.get("PWD") or os.getcwd()
    if not cwd:
        return "default"
    cwd = str(cwd).rstrip("/")
    home = os.path.expanduser("~").rstrip("/")
    if not cwd or cwd == "/" or cwd == home:
        return "default"
    return os.path.basename(cwd) or "default"

def run_active_slice(query: str, scope: str) -> str:
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "memem.server", "active-slice", query, "--scope", scope, "--no-llm"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""

# Parse hook input from the tempfile (avoids argv size limits)
try:
    hook = json.loads(input_file.read_text())
except Exception:
    emit_empty()

session_id = hook.get("session_id", "") or ""
message = hook.get("message", hook.get("query", "")) or ""
scope = detect_scope(hook)

# If no plugin root, we cannot assemble an active slice — emit empty
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
last_primed   = last_data.get("primed", False)

# v0.12.0: if SessionStart just primed us for this session, the first
# UserPromptSubmit should still receive an active slice. SessionStart
# injects the broad index; the first real prompt should get the runtime
# working-state slice instead of the older context_assemble briefing.
# Consume the primed flag (rewrite .last-brief.json without it) so the
# next prompt uses normal topic-shift logic.
if last_primed and last_session == session_id:
    assembled = run_active_slice(message, scope)
    if not assembled:
        emit_empty()
    try:
        last_brief.write_text(json.dumps({
            "session_id": session_id,
            "keywords": sorted(current_keywords),
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))
    except Exception:
        pass

    try:
        topic_log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        snippet = message[:100].replace('"', "'").replace('\n', ' ').replace('\r', '')
        with topic_log.open("a") as fh:
            fh.write(f'{ts} session={session_id} overlap=primed msg="{snippet}"\n')
    except Exception:
        pass

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": assembled,
        }
    }))
    sys.exit(0)

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

# If overlap is sufficient, stay silent WITHOUT updating last-brief
# (nothing to update — we're continuing the same topic)
if not is_first_message and overlap >= threshold:
    emit_empty()

# --- Topic shift triggered (or first message) ---

# Run active slice generation
assembled = run_active_slice(message, scope)

# If assembly failed or returned empty, leave last-brief UNTOUCHED so the
# next prompt with similar keywords will retry. Silent starvation was the
# bug we fixed in v0.10.2 — previously .last-brief.json was written before
# this check, causing any transient briefing failure to suppress future recall.
if not assembled:
    emit_empty()

# Assembly succeeded — NOW commit the keyword set + log the shift
try:
    last_brief.parent.mkdir(parents=True, exist_ok=True)
    last_brief.write_text(json.dumps({
        "session_id": session_id,
        "keywords": sorted(current_keywords),
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
except Exception:
    pass

try:
    topic_log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    snippet = message[:100].replace('"', "'").replace('\n', ' ').replace('\r', '')
    with topic_log.open("a") as fh:
        fh.write(f'{ts} session={session_id} overlap={overlap:.2f} msg="{snippet}"\n')
except Exception:
    pass

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": assembled,
    }
}))
HOOKPY
