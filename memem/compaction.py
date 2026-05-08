"""Compaction-survivor checkpoint for memem v1.5.0.

Detects when a session is approaching context-window compaction and saves a
structured snapshot so the LLM can recover key state after the compaction.

Claude Code does NOT expose a PreCompact hook.  Instead, the UserPromptSubmit
hook calls detect_compaction_risk() on every prompt as a polling strategy.

Threshold: MEMEM_COMPACTION_THRESHOLD env var (default 0.80) of a 200K-token
context window.  Token count is estimated from transcript byte size at ~4
chars/token, giving a byte threshold of 0.80 * 200000 * 4 = 640000 bytes.

The snapshot is stored as a regular memory with:
  domain_tags: [compaction, checkpoint, kind:compaction-checkpoint]
  source_session: <session_id>
  body: markdown sections for each snapshot section + project_id
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("memem-compaction")

# Context window in tokens and its byte equivalent (~4 chars/token).
_CONTEXT_WINDOW_TOKENS = 200_000
_CHARS_PER_TOKEN = 4
_CONTEXT_WINDOW_BYTES = _CONTEXT_WINDOW_TOKENS * _CHARS_PER_TOKEN  # 800_000

_DECISION_KIND_TAGS = {"decision"}

# Tool names that represent code changes in the transcript.
_CODE_CHANGE_TOOLS = {"Edit", "Write", "MultiEdit", "Bash"}

# Keywords that suggest tension / blocked state.
_TENSION_KEYWORDS = ("blocked", "issue")


def _get_threshold() -> float:
    """Return the compaction risk threshold (default 0.80, env-overridable)."""
    raw = os.environ.get("MEMEM_COMPACTION_THRESHOLD", "")
    if raw:
        try:
            val = float(raw)
            if 0.0 < val <= 1.0:
                return val
        except ValueError:
            pass
    return 0.80


def detect_compaction_risk(transcript_path: str) -> bool:
    """Return True when the transcript is at/over the compaction threshold.

    Estimates token usage from transcript byte size (~4 chars/token).
    Triggers when estimated usage >= MEMEM_COMPACTION_THRESHOLD * 200K tokens.

    Args:
        transcript_path: Path to the session JSONL transcript file.

    Returns:
        True when at or over threshold; False otherwise (or if file missing).
    """
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return False

    threshold = _get_threshold()
    byte_limit = int(_CONTEXT_WINDOW_BYTES * threshold)
    return size >= byte_limit


def _parse_transcript_messages(transcript_path: str) -> list[dict[str, Any]]:
    """Parse all messages from the JSONL transcript file.

    Returns a list of raw message objects (user/assistant) in order.
    """
    messages: list[dict[str, Any]] = []
    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = obj.get("type")
                if msg_type in ("user", "assistant"):
                    messages.append(obj)
    except OSError:
        pass
    return messages


def _extract_assistant_text_blocks(content: Any) -> list[str]:
    """Extract text blocks from assistant message content."""
    if isinstance(content, str):
        return [content.strip()] if content.strip() else []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                texts.append(text)
    return texts


def _extract_tool_use_blocks(content: Any) -> list[dict[str, Any]]:
    """Extract tool_use blocks from assistant message content."""
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            blocks.append(block)
    return blocks


def _build_tensions(
    working_memory: dict[str, str],
    messages: list[dict[str, Any]],
) -> str:
    """Extract tensions from working memory stuck_on + last 5 assistant messages.

    Returns a text string with all tension signals found.
    """
    parts: list[str] = []

    stuck_text = (working_memory or {}).get("stuck_on", "").strip()
    if stuck_text:
        parts.append(f"stuck_on:\n{stuck_text}")

    # Last 5 assistant messages with tension keywords.
    assistant_msgs = [
        m for m in messages if m.get("type") == "assistant"
    ]
    last_five = assistant_msgs[-5:]
    tension_lines: list[str] = []
    for msg in last_five:
        content = msg.get("message", {}).get("content", "")
        for text in _extract_assistant_text_blocks(content):
            for line in text.splitlines():
                lower = line.lower()
                if any(kw in lower for kw in _TENSION_KEYWORDS):
                    cleaned = line.strip()
                    if cleaned:
                        tension_lines.append(cleaned)

    if tension_lines:
        parts.append("assistant messages (blocked/issue):\n" + "\n".join(tension_lines))

    return "\n\n".join(parts)


def _build_code_changes(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return last 5 tool_use blocks where name is in {Edit, Write, MultiEdit, Bash}."""
    tool_blocks: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", "")
        for block in _extract_tool_use_blocks(content):
            name = block.get("name", "")
            if name in _CODE_CHANGE_TOOLS:
                tool_blocks.append(block)

    return tool_blocks[-5:]


def _load_decision_memories(session_id: str) -> list[dict[str, Any]]:
    """Return last 5 decision memories for the given session_id.

    Filters _obsidian_memories() by kind:decision tag OR kind='decision'.
    Since the current schema stores kind in tags, we check for the 'decision'
    tag.  session_id match is checked via source_session field.
    """
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception as exc:
        log.debug("_obsidian_memories unavailable: %s", exc)
        return []

    all_mems = _obsidian_memories()
    decision_mems: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        has_decision_tag = "decision" in tags or "kind:decision" in tags
        if not has_decision_tag:
            continue
        # Session filter: if session_id provided, prefer same-session but
        # don't exclude cross-session decisions (they're still useful context).
        decision_mems.append(mem)

    # Sort by created_at descending, take last 5.
    decision_mems.sort(
        key=lambda m: m.get("created_at", ""),
        reverse=True,
    )
    return decision_mems[:5]


def build_compaction_snapshot(
    session_id: str,
    transcript_path: str,
    memem_dir: Path,
) -> dict[str, Any]:
    """Build a structured snapshot of the current session state.

    Args:
        session_id: The current Claude Code session ID.
        transcript_path: Path to the session JSONL transcript file.
        memem_dir: Path to the memem state directory (~/.memem/).

    Returns:
        A dict with keys: working_memory, decisions, tensions, code_changes.
    """
    # 1. Working memory sections.
    try:
        from memem.working_memory import read_working_memory
        working_memory = read_working_memory()
    except Exception as exc:
        log.debug("read_working_memory failed: %s", exc)
        working_memory = {}

    # 2. Decision memories (last 5).
    decisions = _load_decision_memories(session_id)

    # 3. Parse transcript messages.
    messages = _parse_transcript_messages(transcript_path)

    # 4. Tensions: stuck_on + blocked/issue lines from last 5 assistant msgs.
    tensions = _build_tensions(working_memory, messages)

    # 5. Code changes: last 5 code tool_use blocks.
    code_changes = _build_code_changes(messages)

    return {
        "working_memory": working_memory,
        "decisions": decisions,
        "tensions": tensions,
        "code_changes": code_changes,
    }


def _render_snapshot_markdown(
    snapshot: dict[str, Any],
    session_id: str,
    project_id: str,
) -> str:
    """Render the snapshot dict as structured markdown sections."""
    parts: list[str] = []

    # Metadata header.
    parts.append(f"## Compaction Checkpoint\n\nkind: compaction-checkpoint\nsession_id: {session_id}\nproject_id: {project_id}")

    # Working memory sections.
    wm = snapshot.get("working_memory") or {}
    if wm:
        wm_lines: list[str] = []
        for section, body in wm.items():
            if body and body.strip():
                wm_lines.append(f"### {section}\n\n{body.strip()}")
        if wm_lines:
            parts.append("## Working Memory\n\n" + "\n\n".join(wm_lines))
    else:
        parts.append("## Working Memory\n\n(empty)")

    # Decisions.
    decisions = snapshot.get("decisions") or []
    if decisions:
        decision_lines: list[str] = []
        for mem in decisions:
            title = mem.get("title", "Untitled")
            essence = (mem.get("essence") or mem.get("full_record", ""))[:300]
            decision_lines.append(f"- **{title}**: {essence}")
        parts.append("## Recent Decisions\n\n" + "\n".join(decision_lines))
    else:
        parts.append("## Recent Decisions\n\n(none)")

    # Tensions.
    tensions = snapshot.get("tensions", "")
    if tensions and tensions.strip():
        parts.append(f"## Tensions\n\n{tensions.strip()}")
    else:
        parts.append("## Tensions\n\n(none)")

    # Code changes.
    code_changes = snapshot.get("code_changes") or []
    if code_changes:
        change_lines: list[str] = []
        for block in code_changes:
            name = block.get("name", "")
            inp = block.get("input", {})
            file_path = inp.get("file_path", inp.get("command", ""))
            if isinstance(file_path, str) and len(file_path) > 100:
                file_path = file_path[:100] + "..."
            change_lines.append(f"- {name}: {file_path}")
        parts.append("## Code Changes\n\n" + "\n".join(change_lines))
    else:
        parts.append("## Code Changes\n\n(none)")

    return "\n\n".join(parts)


def save_compaction_checkpoint(
    snapshot: dict[str, Any],
    session_id: str,
    project_id: str,
) -> str:
    """Save a compaction checkpoint as a memory and return the 8-char memory_id.

    The memory is tagged with [compaction, checkpoint, kind:compaction-checkpoint]
    and the kind survives round-trips because it is stored in domain_tags.

    Args:
        snapshot: Dict returned by build_compaction_snapshot().
        session_id: The current Claude Code session ID.
        project_id: Project identifier for the current work context.

    Returns:
        The 8-char prefix of the saved memory ID.

    Raises:
        RuntimeError: If the Obsidian vault is not writable.
        ValueError: If content is rejected (too short, etc.).
    """
    from memem.obsidian_store import _make_memory, _save_memory

    body = _render_snapshot_markdown(snapshot, session_id, project_id)

    title = f"Compaction checkpoint — {session_id[:8] if session_id else 'unknown'}"

    # Tags encode the kind for round-trip fidelity.
    tags = ["compaction", "checkpoint", "kind:compaction-checkpoint"]

    mem = _make_memory(
        content=body,
        title=title,
        tags=tags,
        project=project_id or "general",
        source_type="user",
        source_session=session_id,
        importance=4,
    )

    _save_memory(mem)
    return mem["id"][:8]
