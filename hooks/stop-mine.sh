#!/usr/bin/env bash
# memem Stop hook — fires mine_delta as a detached fire-and-forget subprocess.
#
# Flow:
#   1. Anti-recursion guard: skip if MEMEM_HOOK_DISABLE is set
#   2. Opt-in check: skip if ~/.memem/.miner-opted-in does not exist
#   3. Parse session_id and transcript_path from hook stdin JSON
#   4. Spawn mine_delta as a detached subprocess (setsid nohup ... disown)
#   5. Emit valid Stop hook JSON and exit 0 immediately (<100ms total)
#
# The mine_delta process runs detached — this hook never waits for it.

set -euo pipefail

# Anti-recursion guard: skip if invoked from a memem-spawned headless claude -p call.
# Without this, every memem mining or tournament Haiku call recursively fires memem hooks.
if [ -n "${MEMEM_HOOK_DISABLE:-}" ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":""}}'
    exit 0
fi

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"

# Opt-in check: do nothing unless the user has explicitly enabled the miner.
if [ ! -f "$MEMEM_DIR/.miner-opted-in" ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":""}}'
    exit 0
fi

# Read stdin to a temp file to avoid ARG_MAX issues with large transcripts.
INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE" || true

# Extract session_id and transcript_path via python3 for portability.
read -r SID TP < <(
    "${MEMEM_PYTHON:-python3}" - "$INPUT_FILE" << 'PYEOF'
import json, sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text() or "{}")
except Exception:
    data = {}

sid = str(data.get("session_id") or "")
tp = str(data.get("transcript_path") or "")
print(sid, tp)
PYEOF
) || true

# Spawn mine_delta as a fully detached subprocess — fire and forget.
# setsid creates a new session (detaches from current process group).
# nohup prevents SIGHUP from reaching the child.
# </dev/null >/dev/null 2>&1 ensures no I/O inherits from the hook process.
# disown removes the child from the shell's job table.
# The subshell `(...)` closes all extra file descriptors before exec so that
# no inherited pipes from the calling environment can keep this hook's parent
# process alive waiting for the child.
if [ -n "$SID" ]; then
    (
        # Close all file descriptors above stderr so no inherited pipes leak.
        for _fd in $(ls /proc/$$/fd 2>/dev/null | grep -v '^[012]$'); do
            eval "exec ${_fd}>&-" 2>/dev/null || true
        done
        setsid nohup "${MEMEM_PYTHON:-python3}" -m memem.mine_delta \
            --session-id "$SID" \
            --transcript-path "$TP" \
            </dev/null >/dev/null 2>&1 &
        disown
    )
fi

# Emit valid Stop hook JSON and return immediately.
echo '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":""}}'
exit 0
