"""Incremental memory extractor: mine new turns from a JSONL transcript.

CLI:
    python3 -m memem.mine_delta --session-id X --transcript-path Y

Reads only the new bytes since the last run (offset tracked per session),
skips trivial deltas, acquires a per-session flock to prevent concurrent
mining, applies empty-streak backoff, and saves extracted memories to the
Obsidian vault.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import structlog

from memem.haiku_prompts import HAIKU_TIMEOUT_SECONDS
from memem.mining import extract_from_text
from memem.models import MEMEM_DIR
from memem.obsidian_store import _find_memory, _make_memory, _save_memory

log = structlog.get_logger("memem-mine-delta")

# -----------------------------------------------------------------------
# Tuning constants
# -----------------------------------------------------------------------

_MIN_DELTA_CHARS = 100
_ACK_ONLY_RE = re.compile(r"^\s*(ok|yes|go|thanks|sure|nope|no)\s*\.?\s*$", re.IGNORECASE)
_EMPTY_STREAK_THRESHOLD = 3
_EMPTY_STREAK_SKIP = 5

_EPISODE_HAIKU_SYSTEM = (
    "You are summarizing a Claude Code conversation into a single 100-word "
    "narrative paragraph that captures: (1) what the user was working on, "
    "(2) what was decided or accomplished, (3) any open questions or next steps. "
    "Write in third person past tense. Output ONLY the 100-word paragraph, no preamble."
)


# -----------------------------------------------------------------------
# State-file helpers
# -----------------------------------------------------------------------


def _state_dir() -> Path:
    """Return the base state directory, honouring MEMEM_DIR env var."""
    env = os.environ.get("MEMEM_DIR")
    if env:
        return Path(env)
    return MEMEM_DIR


def _offset_path(session_id: str) -> Path:
    p = _state_dir() / ".offsets" / session_id
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _lock_path(session_id: str) -> Path:
    p = _state_dir() / f".mine.{session_id}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty_streak_path(session_id: str) -> Path:
    p = _state_dir() / ".empty_streak" / session_id
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _mined_sessions_path() -> Path:
    return _state_dir() / ".mined_sessions"


# -----------------------------------------------------------------------
# Read / write helpers
# -----------------------------------------------------------------------


def _read_offset(session_id: str) -> int:
    p = _offset_path(session_id)
    try:
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_offset(session_id: str, offset: int) -> None:
    _offset_path(session_id).write_text(str(offset))


def _read_empty_streak(session_id: str) -> tuple[int, int]:
    """Return (streak_count, skip_remaining)."""
    p = _empty_streak_path(session_id)
    try:
        data = json.loads(p.read_text())
        return int(data.get("streak", 0)), int(data.get("skip_remaining", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0, 0


def _write_empty_streak(session_id: str, streak: int, skip_remaining: int) -> None:
    _empty_streak_path(session_id).write_text(
        json.dumps({"streak": streak, "skip_remaining": skip_remaining})
    )


def _record_mined_session(session_id: str) -> None:
    p = _mined_sessions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Append only if not already present
    existing: set[str] = set()
    try:
        existing = set(p.read_text().splitlines())
    except FileNotFoundError:
        pass
    if session_id not in existing:
        with p.open("a") as f:
            f.write(session_id + "\n")


# -----------------------------------------------------------------------
# Delta extraction
# -----------------------------------------------------------------------


def _read_delta(transcript_path: str, offset: int) -> tuple[list[dict], int]:
    """Read JSONL lines from *offset* bytes onwards.

    Returns (list_of_turn_dicts, new_eof_offset).
    """
    path = Path(transcript_path)
    turns: list[dict] = []
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        turns.append(obj)
                except json.JSONDecodeError:
                    pass
            eof = fh.tell()
    except OSError:
        return [], offset
    return turns, eof


def _extract_role_text(t: dict) -> tuple[str, str]:
    """Extract (role, text) from a transcript turn.

    Handles both:
      • Claude Code nested schema: {type:'user'|'assistant', message:{role, content}}
        — `content` is either a string OR a list of blocks like
          [{"type":"text","text":...}, {"type":"tool_use",...}, ...].
      • Legacy/test flat schema: {role:'user'|'assistant', text|content}.

    Returns ("", "") for non-conversational events (permission-mode,
    tool_result wrappers without text, summary records, etc.).
    """
    # Prefer nested shape if message dict is present (real Claude Code format)
    inner = t.get("message") if isinstance(t.get("message"), dict) else None
    if inner is not None:
        role = inner.get("role") or t.get("type") or ""
        content = inner.get("content")
    else:
        role = t.get("role") or t.get("type") or ""
        content = t.get("text") if t.get("text") is not None else t.get("content")

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Concatenate text-type blocks; skip tool_use / tool_result blocks
        text = " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = ""

    return role, text


def _turns_to_text(turns: list[dict]) -> str:
    """Concatenate turn text into a single string for char-count / ack checks."""
    parts: list[str] = []
    for t in turns:
        role, text = _extract_role_text(t)
        if text:
            parts.append(f"{role}: {text}" if role else text)
    return "\n".join(parts)


def _turns_to_messages(turns: list[dict]) -> list[str]:
    """Format turns as 'User: ...' / 'Assistant: ...' strings for Haiku."""
    messages: list[str] = []
    for t in turns:
        role, text = _extract_role_text(t)
        if not text:
            continue
        if role == "user":
            messages.append(f"User: {text}")
        elif role == "assistant":
            messages.append(f"Assistant: {text}")
    return messages


# -----------------------------------------------------------------------
# Skip predicates
# -----------------------------------------------------------------------


def _is_trivial_delta(delta_text: str) -> bool:
    """Return True if delta is too short or ack-only."""
    if len(delta_text) < _MIN_DELTA_CHARS:
        return True
    # Strip role prefixes for ack check
    stripped_lines = [
        re.sub(r"^(user|assistant):\s*", "", line, flags=re.IGNORECASE).strip()
        for line in delta_text.splitlines()
        if line.strip()
    ]
    non_empty = [line for line in stripped_lines if line]
    if not non_empty:
        return True
    combined = " ".join(non_empty)
    return bool(_ACK_ONLY_RE.match(combined))


# -----------------------------------------------------------------------
# Project detection from transcript path (B13)
# -----------------------------------------------------------------------


def _project_from_transcript_path(transcript_path: str) -> str:
    """Derive the project name from the transcript file path.

    Claude Code stores transcripts at:
        ~/.claude/projects/<munged-cwd>/<session>.jsonl
    where munged-cwd is the original cwd with '/' replaced by '-'.

    e.g. /home/claude-user/cortex-plugin → -home-claude-user-cortex-plugin

    Returns the project basename (the final path component after the home
    prefix), 'general' for the home dir itself, or the full dir name as a
    fallback.
    """
    try:
        parent_dir = Path(transcript_path).parent.name
        home = str(Path.home())
        munged_home = home.replace("/", "-")  # e.g. "-home-claude-user"
        if parent_dir == munged_home:
            return "general"
        if parent_dir.startswith(munged_home + "-"):
            return parent_dir[len(munged_home) + 1:]
        return parent_dir or "general"
    except Exception:
        return "general"


# -----------------------------------------------------------------------
# Episode emission
# -----------------------------------------------------------------------


def _emit_session_episode(
    session_id: str,
    turns: list[dict],
    first_user_msg: str,
    transcript_path: str = "",
) -> bool:
    """Emit a single per-session episodic memory summarising the full turn list.

    Calls Haiku with _EPISODE_HAIKU_SYSTEM as the system prompt.
    Returns True if the episode was successfully written, False otherwise.
    """
    messages = _turns_to_messages(turns)
    body = (
        "=== BEGIN CONVERSATION ===\n"
        + "\n\n".join(messages)
        + "\n=== END CONVERSATION ===\n\n"
        "Summarize per the system instructions above."
    )

    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", "haiku",
                "--tools", "",
                "--system-prompt", _EPISODE_HAIKU_SYSTEM,
            ],
            input=body,
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT_SECONDS,
            env={**os.environ, "MEMEM_HOOK_DISABLE": "1"},
            start_new_session=True,
        )
    except Exception as exc:
        log.warning("mine_delta: episode Haiku call failed", session_id=session_id, error=str(exc))
        return False

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        log.warning("mine_delta: episode Haiku non-zero exit", session_id=session_id, detail=detail)
        return False

    summary = result.stdout.strip()
    if not summary:
        log.warning("mine_delta: episode Haiku returned empty output", session_id=session_id)
        return False

    # B13: Detect project from transcript path instead of os.getcwd()
    if transcript_path:
        project = _project_from_transcript_path(transcript_path)
    else:
        try:
            project = os.path.basename(os.getcwd()) or "general"
        except Exception:
            project = "general"

    # B6: Stable episode id — one per session, derived from session_id only.
    episode_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memem-episode:{session_id}"))

    # Check if this episode already exists (second or later delta for the same session).
    existing = _find_memory(episode_id)
    if existing is None:
        # Upgrade path: pre-v2.5.0 episodes were saved under random uuid4 ids.
        # Adopt a legacy episode for this session via its session:<id> tag so
        # re-mining across the upgrade boundary updates it in place instead of
        # creating a duplicate episode.
        try:
            from memem.obsidian_store import _obsidian_memories
            for cand in _obsidian_memories():
                if (
                    cand.get("source_type") == "mined-episode"
                    and f"session:{session_id}" in (cand.get("domain_tags") or [])
                ):
                    existing = cand
                    episode_id = cand.get("id") or episode_id
                    break
        except Exception:  # noqa: BLE001 — adoption is best-effort
            pass
    if existing:
        # Keep the original title so the filename slug stays stable.
        title = existing["title"]
        log.debug("mine_delta: updating existing episode memory", session_id=session_id, episode_id=episode_id[:8])
    else:
        # First emission: build a title from the first real user message.
        # B6 cont.: skip if empty, XML-ish ('<task-notification>', '<system-reminder>',
        # ...), or a 'Caveat:' harness preamble. run() pre-filters with the same rule
        # when selecting first_user_msg; this is the last-line defence for direct calls.
        stripped = first_user_msg.strip()
        title_suffix = ""
        if stripped and not stripped.startswith("<") and not stripped.startswith("Caveat:"):
            title_suffix = stripped[:80]
        title = f"Session {session_id[:8]} — {title_suffix or '(no user prompt)'}"

    tags = ["type:episodic", f"session:{session_id}"]

    try:
        mem = _make_memory(
            content=summary,
            title=title,
            tags=tags,
            project=project,
            source_type="mined-episode",
            source_session=session_id,
            importance=3,
        )
        # Override id to the stable per-session uuid5.
        mem["id"] = episode_id

        # Carry over fields from the existing record so filename slug and
        # created_at stay stable; _write_obsidian_memory will clean up the
        # old file if title/slug differs.
        if existing:
            mem["obsidian_file"] = existing.get("obsidian_file", "")
            mem["file"] = existing.get("file", "")
            mem["created_at"] = existing.get("created_at", mem["created_at"])

        _save_memory(mem)
        log.info("mine_delta: saved episode memory", session_id=session_id, title=title)
        return True
    except Exception as exc:
        log.warning("mine_delta: failed to save episode memory", session_id=session_id, error=str(exc))
        return False


# -----------------------------------------------------------------------
# Main logic
# -----------------------------------------------------------------------


def run(session_id: str, transcript_path: str) -> None:
    """Core extraction logic; extracted for easy testing."""

    # 1. Read offset
    offset = _read_offset(session_id)

    # 2. Read delta turns
    turns, eof = _read_delta(transcript_path, offset)
    if not turns:
        log.debug("mine_delta: no new turns", session_id=session_id)
        _write_offset(session_id, eof)
        # Mark touched so SessionStart stale-sweep doesn't re-spawn forever.
        _record_mined_session(session_id)
        return

    # 3. Skip checks
    delta_text = _turns_to_text(turns)
    if _is_trivial_delta(delta_text):
        log.debug("mine_delta: trivial delta, skipping", session_id=session_id, chars=len(delta_text))
        _write_offset(session_id, eof)
        # Mark touched so SessionStart stale-sweep doesn't re-spawn forever.
        _record_mined_session(session_id)
        return

    # 4. Flock — non-blocking; exit 0 if another invocation holds the lock
    lock_file_path = _lock_path(session_id)
    try:
        lock_fh = lock_file_path.open("w")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.debug("mine_delta: lock held, exiting", session_id=session_id)
        return

    try:
        # 5. Empty-streak backoff
        streak, skip_remaining = _read_empty_streak(session_id)
        if streak >= _EMPTY_STREAK_THRESHOLD and skip_remaining > 0:
            _write_empty_streak(session_id, streak, skip_remaining - 1)
            log.debug(
                "mine_delta: empty-streak backoff",
                session_id=session_id,
                streak=streak,
                skip_remaining=skip_remaining,
            )
            _write_offset(session_id, eof)
            # Mark touched so SessionStart stale-sweep doesn't re-spawn forever.
            _record_mined_session(session_id)
            return

        # 6. Call Haiku
        messages = _turns_to_messages(turns)
        try:
            text_blob = "\n\n".join(messages)
            memories = extract_from_text(text_blob)
        except Exception as exc:
            log.warning("mine_delta: Haiku extraction failed", session_id=session_id, error=str(exc))
            memories = []

        # 7. Update empty-streak state
        if not memories:
            new_streak = streak + 1
            new_skip = _EMPTY_STREAK_SKIP if new_streak >= _EMPTY_STREAK_THRESHOLD else 0
            _write_empty_streak(session_id, new_streak, new_skip)
        else:
            _write_empty_streak(session_id, 0, 0)

        # 8. Write memories to vault
        memories_written = 0
        for mem_dict in memories:
            try:
                content = mem_dict.get("content") or mem_dict.get("essence") or ""
                title = mem_dict.get("title") or "Untitled"
                tags = mem_dict.get("tags") or mem_dict.get("domain_tags") or []
                project = mem_dict.get("project") or "general"
                importance = int(mem_dict.get("importance") or 3)

                mem = _make_memory(
                    content=content,
                    title=title,
                    tags=tags,
                    project=project,
                    source_type="mined",
                    source_session=session_id,
                    importance=importance,
                )
                _save_memory(mem)
                log.info("mine_delta: saved memory", session_id=session_id, title=title)
                memories_written += 1
            except Exception as exc:
                log.warning("mine_delta: failed to save memory", session_id=session_id, error=str(exc))

        # 8b. Emit per-session episode if session was substantive
        if memories_written >= 1 and len(turns) >= 3:
            # B6 cont.: Skip messages that are empty, start with '<' (catches
            # <task-notification>, <system-reminder>, etc.), or start with 'Caveat:'.
            first_user_msg = ""
            for t in turns:
                role, text = _extract_role_text(t)
                if role == "user":
                    stripped = text.strip()
                    if stripped and not stripped.startswith("<") and not stripped.startswith("Caveat:"):
                        first_user_msg = text
                        break
            try:
                _emit_session_episode(session_id, turns, first_user_msg, transcript_path=transcript_path)
            except Exception as exc:
                log.warning(
                    "mine_delta: episode emission raised unexpectedly",
                    session_id=session_id,
                    error=str(exc),
                )

        # 9. Update offset
        _write_offset(session_id, eof)

        # 10. Record session
        _record_mined_session(session_id)

    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incrementally mine memories from a JSONL transcript delta."
    )
    parser.add_argument("--session-id", required=True, help="Unique session identifier")
    parser.add_argument("--transcript-path", required=True, help="Path to JSONL transcript file")
    args = parser.parse_args()

    run(session_id=args.session_id, transcript_path=args.transcript_path)


if __name__ == "__main__":
    main()
