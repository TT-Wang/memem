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
#   MEMEM_ATTRIBUTION_TIMEOUT — max seconds to spend on this, default 30
#
# Why 30s default: each invocation cold-loads sentence-transformers (~5-10s)
# before computing per-item embeddings (~0.3-1s each). With 10-20 items per
# slice and the prior 10s budget, only the first item's embedding ever
# completed — the rest were killed mid-loop, costing 16/17 of the closed-
# loop signal. 30s covers cold-start + ~20 items comfortably.

set -euo pipefail

# Anti-recursion guard: skip if invoked from a memem-spawned headless claude -p call.
# Without this, every memem mining or tournament Haiku call recursively fires memem hooks → load explosion.
[ -n "${MEMEM_HOOK_DISABLE:-}" ] && exit 0

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
ATTRIBUTION_TIMEOUT="${MEMEM_ATTRIBUTION_TIMEOUT:-30}"
MEMEM_MINE_TIMEOUT="${MEMEM_MINE_TIMEOUT:-60}"

if [ -z "$PLUGIN_ROOT" ] || [ "$PLUGIN_ROOT" = '${CLAUDE_PLUGIN_ROOT}' ]; then
    # Plugin root unset — degrade silently.
    exit 0
fi

# Read hook stdin to a tempfile (avoids ARG_MAX on huge transcripts).
INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE" || true

# Bounded execution — never block the assistant turn for more than N seconds.
setsid timeout --kill-after=5 "$ATTRIBUTION_TIMEOUT" "${MEMEM_PYTHON:-python3}" - "$PLUGIN_ROOT" "$INPUT_FILE" "$MEMEM_DIR" << 'HOOKPY' 2>/dev/null || true
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

# 3. Compute + log attribution (active_slice_engine removed in v2.0.0 — no-op)
try:
    from memem.active_slice_engine import record_slice_attribution  # noqa: F401
    record_slice_attribution(slice_to_attribute, response_text)
except ImportError:
    pass  # v2.0.0: active_slice_engine deleted
except Exception:
    # never break — this is best-effort
    pass

# 4. Update working memory with current_task + last_3_actions from transcript.
# Walk the transcript lines again to collect user messages.
try:
    user_messages = []
    lines = Path(transcript_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "user":
            continue
        msg = event.get("message", {})
        content = msg.get("content", [])
        chunks = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
        elif isinstance(content, str):
            chunks.append(content)
        text_val = "\n".join(chunks).strip()
        if text_val:
            user_messages.append(text_val)

    if user_messages:
        try:
            from memem.working_memory import update_section  # noqa: F401

            # current_task: last user message, truncated to 200 chars
            last_user = user_messages[-1]
            current_task = last_user[:200] if len(last_user) > 200 else last_user
            update_section("current_task", current_task)

            # last_3_actions: 3 most recent user message snippets as bullet list
            recent = user_messages[-3:]
            bullets = "\n".join(
                f"- {m[:100].replace(chr(10), ' ')}" for m in recent
            )
            update_section("last_3_actions", bullets)
        except ImportError:
            pass  # v2.0.0: working_memory deleted
except Exception:
    # never break — this is best-effort
    pass
HOOKPY

# 5. Mine-on-stop: trigger incremental mining for this session immediately
#    instead of waiting for the daemon's next poll cycle. Uses a marker file
#    to avoid double-mining the same session if the hook fires more than once.
if [ -n "$PLUGIN_ROOT" ] && [ "$PLUGIN_ROOT" != '${CLAUDE_PLUGIN_ROOT}' ]; then
    # Extract session_id from the hook input we already read above.
    _STOP_SESSION_ID=$(python3 -c "
import json, sys
try:
    data = json.loads(open('$INPUT_FILE').read() or '{}')
    print(data.get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || true)

    # v1.8.1: validate session_id is path-safe before using as a directory
    # component. Hook input is normally Claude-Code-controlled, but the value
    # is not authenticated; without this an adversarial input like "../foo"
    # would cause `mkdir` to create directories outside the marker base.
    if [ -n "$_STOP_SESSION_ID" ] && [[ "$_STOP_SESSION_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        # H-2: use atomic mkdir as the race-free guard. Only the process that
        # successfully creates the directory runs mine_session_delta; concurrent
        # Stop events for the same session_id get a non-zero exit from mkdir
        # and skip the mine call, preventing doubled Haiku cost.
        _STOP_MARKER_DIR_BASE="$MEMEM_DIR/.stop-timestamps"
        _STOP_LOCK_DIR="$_STOP_MARKER_DIR_BASE/$_STOP_SESSION_ID"
        mkdir -p "$_STOP_MARKER_DIR_BASE"
        if mkdir "$_STOP_LOCK_DIR" 2>/dev/null; then
            # We won the race — fire mine_session_delta under a timeout.
            # Failures are logged but never crash the hook.
            mkdir -p "$MEMEM_DIR/logs"
            # Pass session_id via env to avoid shell injection if the value
            # ever contains characters that would break a Python string literal.
            MEMEM_STOP_SESSION_ID="$_STOP_SESSION_ID" \
            MEMEM_PLUGIN_ROOT="$PLUGIN_ROOT" \
            setsid timeout --kill-after=5 "${MEMEM_MINE_TIMEOUT}" "${MEMEM_PYTHON:-python3}" -c '
import os, sys
sys.path.insert(0, os.environ["MEMEM_PLUGIN_ROOT"])
from memem.mining import mine_session_delta
result = mine_session_delta(os.environ["MEMEM_STOP_SESSION_ID"])
print(result)
' >> "$MEMEM_DIR/logs/mine-on-stop.log" 2>&1 || true
            # Directory stays in place as the marker; future mkdir calls fail.
        fi
    fi
fi

exit 0
