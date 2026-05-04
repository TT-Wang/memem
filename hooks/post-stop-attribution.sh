#!/usr/bin/env bash
# memem post-stop-attribution hook — closed-loop relevance signal capture.
#
# Fires on Stop event (assistant finished generating). Reads the most recent
# slice from ~/.memem/active-slices.jsonl + the most recent assistant message
# from the session transcript JSONL, then logs 3-signal attribution per
# memory in the slice. The dreamer (m4) consumes these signals to identify
# inject-but-not-cited memories for demotion.
#
# Silent on success. Logs warnings on failure but never breaks the assistant
# turn — attribution is best-effort.
#
# Configurable via env:
#   MEMEM_DIR                — state dir (default ~/.memem)
#   MEMEM_JUDGE_SAMPLE_RATE  — LLM-judge sample rate, default 0.05 (m2)
#   MEMEM_ATTRIBUTION_TIMEOUT — max seconds to spend on this, default 10

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
ATTRIBUTION_TIMEOUT="${MEMEM_ATTRIBUTION_TIMEOUT:-10}"

if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    # Plugin root unset — degrade silently.
    exit 0
fi

# Read hook stdin to a tempfile (avoids ARG_MAX on huge transcripts).
INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE" || true

# Bounded execution — never block the assistant turn for more than N seconds.
timeout "$ATTRIBUTION_TIMEOUT" "${MEMEM_PYTHON:-python3}" - "$PLUGIN_ROOT" "$INPUT_FILE" "$MEMEM_DIR" << 'HOOKPY' 2>/dev/null || true
import json, sys, os
from pathlib import Path

plugin_root = sys.argv[1]
input_file = Path(sys.argv[2])
memem_dir = Path(sys.argv[3])

sys.path.insert(0, plugin_root)

try:
    hook_input = json.loads(input_file.read_text() or "{}")
except Exception:
    sys.exit(0)

session_id = hook_input.get("session_id", "")
transcript_path = hook_input.get("transcript_path") or hook_input.get("transcript", "")
if not transcript_path:
    sys.exit(0)

# 1. Pull the most recent slice from active-slices.jsonl
slices_path = memem_dir / "active-slices.jsonl"
if not slices_path.exists():
    sys.exit(0)

# Read the LAST line (most recent slice). For session-scoped attribution, prefer
# the most recent slice for THIS session_id; fall back to whichever is last.
latest_slice = None
session_slice = None
try:
    with open(slices_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest_slice = s
            if session_id and s.get("session_id") == session_id:
                session_slice = s
except OSError:
    sys.exit(0)

slice_to_attribute = session_slice or latest_slice
if not slice_to_attribute:
    sys.exit(0)

# 2. Pull the most recent assistant message text from the transcript JSONL
response_text = ""
try:
    transcript = Path(transcript_path)
    if not transcript.exists():
        sys.exit(0)
    # Walk lines bottom-up looking for the last assistant message.
    lines = transcript.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        msg = event.get("message", {})
        content = msg.get("content", [])
        # Concatenate any text blocks
        chunks = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
        elif isinstance(content, str):
            chunks.append(content)
        if chunks:
            response_text = "\n".join(chunks)
            break
except OSError:
    sys.exit(0)

if not response_text:
    sys.exit(0)

# 3. Compute + log attribution
try:
    from memem.active_slice_engine import record_slice_attribution
    record_slice_attribution(slice_to_attribute, response_text)
except Exception:
    # never break — this is best-effort
    pass
HOOKPY

exit 0
