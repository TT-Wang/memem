#!/usr/bin/env bash
# memem auto-recall hook — always-wake slice-first runtime context on UserPromptSubmit.
#
# Flow:
#   1. Load .last-brief.json for telemetry and session bookkeeping
#   2. Infer or forward runtime task mode and continuity context
#   3. Run slice-first prompt generation for every user prompt
#   3. Update .last-brief.json only after successful slice generation
#
# The hook no longer gates activation on keyword overlap. The active slice
# engine is the decision layer; overlap is kept only for logging and tuning.
#
# v0.10.2+ fixes:
#   - Move .last-brief.json write AFTER successful assembly (was writing
#     before, which caused silent context starvation after any transient
#     Haiku failure)
#   - Pass the user message via tempfile instead of argv (was hitting
#     ARG_MAX on large pasted-log prompts)

set -euo pipefail

# Anti-recursion guard: skip if invoked from a memem-spawned headless claude -p call.
# Without this, every memem mining or tournament Haiku call recursively fires memem hooks → load explosion.
[ -n "${MEMEM_HOOK_DISABLE:-}" ] && exit 0

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

"${MEMEM_PYTHON:-python3}" - "$PLUGIN_ROOT" "$INPUT_FILE" "$LAST_BRIEF" "$TOPIC_LOG" "$MEMEM_DIR" << 'HOOKPY'
import sys, json, os, subprocess, re, hashlib
from pathlib import Path
from datetime import datetime, timezone

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

SLICE_UNCHANGED_PLACEHOLDER = "[Active Memory Slice unchanged from previous turn — see slice above]"

def _session_hash_path(memem_dir: Path, session_id: str) -> Path:
    hash_dir = memem_dir / ".last-slice-hashes"
    if not session_id:
        return memem_dir / ".last-slice-hash"
    safe_name = hashlib.sha256(session_id.encode()).hexdigest()
    return hash_dir / f"{safe_name}.hash"

def read_prior_hash(memem_dir: Path, session_id: str) -> str:
    try:
        p = _session_hash_path(memem_dir, session_id)
        if p.exists():
            return p.read_text().strip()
    except OSError:
        pass
    return ""

def write_current_hash(memem_dir: Path, session_id: str, current_hash: str) -> None:
    try:
        p = _session_hash_path(memem_dir, session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(current_hash)
        tmp.replace(p)
    except OSError:
        pass

def dedup_assembled(assembled: str, memem_dir: Path, session_id: str) -> str:
    current_hash = hashlib.sha256(assembled.encode()).hexdigest()
    prior_hash = read_prior_hash(memem_dir, session_id)
    if current_hash == prior_hash:
        return SLICE_UNCHANGED_PLACEHOLDER
    write_current_hash(memem_dir, session_id, current_hash)
    return assembled

def detect_scope(hook: dict) -> str:
    cwd = hook.get("cwd") or os.environ.get("PWD") or os.getcwd()
    if not cwd:
        return "default"
    cwd = str(cwd).rstrip("/")
    home = os.path.expanduser("~").rstrip("/")
    if not cwd or cwd == "/" or cwd == home:
        return "default"
    return os.path.basename(cwd) or "default"

def infer_task_mode(hook: dict, message: str) -> str:
    explicit = str(
        hook.get("task_mode")
        or hook.get("taskMode")
        or os.environ.get("MEMEM_HOOK_TASK_MODE", "")
    ).strip().lower().replace("-", "_")
    aliases = {
        "code": "coding",
        "coding": "coding",
        "implement": "coding",
        "implementation": "coding",
        "debug": "debug",
        "debugging": "debug",
        "bugfix": "debug",
        "proposal": "proposal",
        "plan": "proposal",
        "planning": "proposal",
        "spec": "proposal",
        "research": "research",
        "investigate": "research",
        "investigation": "research",
        "maintenance": "maintenance",
        "cleanup": "maintenance",
    }
    if explicit:
        mapped = aliases.get(explicit, explicit)
        if mapped in {"coding", "proposal", "debug", "research", "maintenance", "session_start"}:
            return mapped

    lower = message.lower()
    debug_terms = ("error", "failing", "failure", "bug", "regression", "traceback", "stack trace", "broken", "fix")
    proposal_terms = ("proposal", "spec", "design", "plan", "roadmap", "review")
    research_terms = ("research", "compare", "evaluate", "investigate", "tradeoff", "alternatives")
    maintenance_terms = ("cleanup", "upgrade", "bump", "chore", "refactor", "maintenance")

    if any(term in lower for term in debug_terms):
        return "debug"
    if any(term in lower for term in proposal_terms):
        return "proposal"
    if any(term in lower for term in research_terms):
        return "research"
    if any(term in lower for term in maintenance_terms):
        return "maintenance"
    return "coding"


def run_active_slice(query: str, scope: str, session_id: str, cwd: str, task_mode: str) -> str:
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, "-m", "memem.server", "slice", "--query-file", "-", "--scope", scope, "--no-llm"]
        if session_id:
            cmd.extend(["--session-id", session_id])
        if cwd:
            cmd.extend(["--cwd", cwd])
        if task_mode:
            cmd.extend(["--task-mode", task_mode])
        result = subprocess.run(
            cmd,
            input=query,
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
message = (
    hook.get("user_prompt")
    or hook.get("prompt")
    or hook.get("message")
    or hook.get("query")
    or ""
)
scope = detect_scope(hook)
cwd = str(hook.get("cwd") or os.environ.get("PWD") or os.getcwd())
task_mode = infer_task_mode(hook, message)

# If no plugin root, we cannot generate slice-first runtime context — emit empty
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

# Run active slice generation
assembled = run_active_slice(message, scope, session_id, cwd, task_mode)

# If slice generation failed or returned empty, leave last-brief UNTOUCHED so the
# next prompt with similar keywords will retry. Silent starvation was the
# bug we fixed in v0.10.2 — previously .last-brief.json was written before
# this check, causing any transient projection failure to suppress future recall.
if not assembled:
    emit_empty()

# Compute overlap ratio for telemetry only.
if last_keywords and last_session == session_id:
    smaller = min(len(current_keywords), len(last_keywords))
    if smaller > 0:
        overlap = len(current_keywords & last_keywords) / smaller
    else:
        overlap = 0.0
else:
    overlap = 0.0

# Slice projection succeeded — NOW commit the keyword set + log the wakeup.
try:
    last_brief.parent.mkdir(parents=True, exist_ok=True)
    last_brief.write_text(json.dumps({
        "session_id": session_id,
        "keywords": sorted(current_keywords),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
except Exception:
    pass

try:
    topic_log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    snippet = message[:100].replace('"', "'").replace('\n', ' ').replace('\r', '')
    with topic_log.open("a") as fh:
        mode = "primed" if last_primed and last_session == session_id else "wakeup"
        fh.write(f'{ts} session={session_id} overlap={overlap:.2f} mode={mode} msg="{snippet}"\n')
except Exception:
    pass

# Dedup is computed on the ORIGINAL assembled body (without working memory) so
# that working-memory changes do NOT bust the slice cache. Only slice content
# changes should trigger a full re-emit.
output_context = dedup_assembled(assembled, memem_dir, session_id)

# Load working memory and prepend to the output context if present.
# This runs AFTER the dedup hash is committed so working-memory updates
# are always included in the emit (even on the dedup-placeholder turn),
# while the dedup hash itself stays keyed on slice content only.
working_block = ""
working_md_path = memem_dir / "working_memory.md"
if working_md_path.exists():
    try:
        wm_text = working_md_path.read_text(encoding="utf-8").strip()
        if wm_text:
            working_block = "## Working Memory\n\n" + wm_text + "\n\n---\n\n"
    except OSError:
        pass

final_context = working_block + output_context

# ---------------------------------------------------------------------------
# Compaction checkpoint — polled here because Claude Code has no PreCompact hook.
# If the transcript is at/over the risk threshold AND no checkpoint was saved
# in the last 60 minutes for this session, save one now.
# ---------------------------------------------------------------------------
COMPACTION_TIMESTAMPS_FILE = memem_dir / ".compaction-checkpoint-timestamps.json"
COMPACTION_COOLDOWN_SECONDS = 3600  # 60 minutes


def _load_compaction_timestamps() -> dict:
    try:
        if COMPACTION_TIMESTAMPS_FILE.exists():
            return json.loads(COMPACTION_TIMESTAMPS_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_compaction_timestamps(ts_data: dict) -> None:
    try:
        COMPACTION_TIMESTAMPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = COMPACTION_TIMESTAMPS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ts_data))
        tmp.replace(COMPACTION_TIMESTAMPS_FILE)
    except Exception:
        pass


def _find_transcript_path(session_id: str) -> str:
    """Try to find the JSONL transcript file for the current session."""
    if not session_id:
        return ""
    # Claude Code stores transcripts under ~/.claude/projects/ by session id.
    home = Path.home()
    for search_root in [home / ".claude" / "projects", home / ".config" / "claude" / "projects"]:
        if not search_root.exists():
            continue
        for jsonl in search_root.rglob(f"*{session_id}*.jsonl"):
            return str(jsonl)
        # Also look by session prefix (first 8 chars).
        sid8 = session_id[:8]
        for jsonl in search_root.rglob(f"*{sid8}*.jsonl"):
            return str(jsonl)
    return ""


def maybe_save_compaction_checkpoint():
    """Save a compaction checkpoint if conditions are met (non-fatal)."""
    if not session_id:
        return

    transcript_path = _find_transcript_path(session_id)
    if not transcript_path:
        return

    try:
        sys.path.insert(0, plugin_root)
        from memem.compaction import (
            build_compaction_snapshot,
            detect_compaction_risk,
            save_compaction_checkpoint,
        )
    except Exception:
        return

    try:
        if not detect_compaction_risk(transcript_path):
            return
    except Exception:
        return

    # M-8: hold the compaction-timestamps lock across the ENTIRE
    # cooldown-check + save + timestamp-write window. The original fix only
    # locked the timestamp write, leaving the cooldown read-then-act gap open
    # — two concurrent UserPromptSubmit hooks could both pass the cooldown
    # check and both save a checkpoint before either updated the timestamp.
    import fcntl as _fcntl
    from datetime import datetime as _dt, timezone as _tz
    lock_path = COMPACTION_TIMESTAMPS_FILE.parent / ".compaction-timestamps.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    try:
        with open(lock_path, "a+") as lockf:
            _fcntl.flock(lockf.fileno(), _fcntl.LOCK_EX)
            try:
                # Re-read inside the lock — another window may have just saved.
                ts_data = _load_compaction_timestamps()
                last_ts_str = ts_data.get(session_id, "")
                if last_ts_str:
                    try:
                        last_ts = _dt.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                        elapsed = (_dt.now(_tz.utc) - last_ts).total_seconds()
                        if elapsed < COMPACTION_COOLDOWN_SECONDS:
                            return
                    except Exception:
                        pass
                # Inside the lock — we are the sole writer for this session.
                snapshot = build_compaction_snapshot(
                    session_id=session_id,
                    transcript_path=transcript_path,
                    memem_dir=memem_dir,
                )
                project_id = scope or "general"
                save_compaction_checkpoint(snapshot, session_id, project_id)
                ts_data[session_id] = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _save_compaction_timestamps(ts_data)
            finally:
                _fcntl.flock(lockf.fileno(), _fcntl.LOCK_UN)
    except Exception:
        pass  # Checkpoint failure must never break slice injection.


maybe_save_compaction_checkpoint()

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": final_context,
    }
}))
HOOKPY
