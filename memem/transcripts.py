import json
import re
from pathlib import Path

from memem.session_state import SESSIONS_DIRS

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND_TAGS_RE = re.compile(
    r"<(?:local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout)>.*?</(?:local-command-caveat|command-name|"
    r"command-message|command-args|local-command-stdout)>",
    re.DOTALL,
)


def _strip_system_noise(text: str) -> str:
    """Remove system-reminder tags and hook-injected content from text."""
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text = _COMMAND_TAGS_RE.sub("", text)
    return text.strip()


def _extract_text_only(content) -> str:
    """Extract only text blocks from content, skip tool calls and tool results."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _extract_tool_use_summary(content) -> str | None:
    """Extract a compact summary of tool_use blocks, or None if none present."""
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "")
            tool_input = block.get("input", {})
            if name in ("Read", "Glob", "Grep"):
                parts.append(f"[Tool: {name} {tool_input.get('file_path', tool_input.get('pattern', ''))}]")
            elif name in ("Edit", "Write"):
                parts.append(f"[Tool: {name} {tool_input.get('file_path', '')}]")
            elif name == "Bash":
                parts.append(f"[Tool: Bash] {tool_input.get('command', '')[:200]}")
            else:
                parts.append(f"[Tool: {name}]")
    return "\n".join(parts) if parts else None


def parse_jsonl_session(
    path: str | Path,
    start_offset: int = 0,
) -> list[dict]:
    """Canonical JSONL session parser.

    Parses a Claude Code session JSONL file and returns a list of message dicts.
    Each dict has:
      - "role": "user" | "assistant" | "system"
      - "text": str (text content of the message, empty string if none)
      - "ts":   str | None (timestamp from the record, if present)
      - "tool_use": str | None (compact summary of tool_use blocks, if any)

    Args:
        path:         Path to the JSONL file.
        start_offset: Byte offset to seek to before reading (for incremental
                      mining). Lines before the offset are skipped. Defaults to 0
                      (read from the beginning).

    Returns:
        List of message dicts. Messages with empty text AND no tool_use are
        omitted. Role values other than "user", "assistant", and "system" are
        dropped.

    Divergence note:
        Earlier per-module parsers only kept "user" and "assistant" roles and
        dropped "system". This canonical adds "system" to the kept set because
        the schema spec explicitly lists it. Callers that previously filtered to
        user+assistant can filter on role=="system" themselves.
    """
    messages: list[dict] = []

    try:
        with open(path, "rb") as fh:
            if start_offset:
                fh.seek(start_offset)
            raw = fh.read()
    except OSError:
        return messages

    text_lines = raw.decode("utf-8", errors="ignore").splitlines()
    for line in text_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = obj.get("type", "")
        if role not in ("user", "assistant", "system"):
            continue

        content = obj.get("message", {}).get("content", "")
        text = _extract_text_only(content)
        tool_use = _extract_tool_use_summary(content)
        ts: str | None = obj.get("timestamp") or obj.get("ts") or None

        if not text and not tool_use:
            continue

        messages.append({
            "role": role,
            "text": text,
            "ts": ts,
            "tool_use": tool_use,
        })

    return messages


# ---------------------------------------------------------------------------
# Internal alias for backward-compatibility (callers inside this package used
# the old pair-returning _parse_jsonl_session; they've been updated below).
# ---------------------------------------------------------------------------

def _parse_jsonl_session_pairs(jsonl_path: str) -> list[dict]:
    """Parse JSONL session into user/assistant exchange pairs.

    LEGACY: used only by transcript_search. Standalone JSONL pair-extractor
    that includes raw content blocks (tool_result blocks in particular) needed
    by transcript_search. Cannot be replaced by parse_jsonl_session because
    the canonical only emits a tool_use summary string, not full content
    blocks. If transcript_search is ever rewritten to not need tool_result
    payloads, this can be migrated.
    """
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    pairs: list[dict] = []

    def _flush():
        if user_texts and assistant_texts:
            pairs.append({
                "user_text": "\n".join(user_texts),
                "assistant_text": "\n".join(assistant_texts),
            })
        user_texts.clear()
        assistant_texts.clear()

    def _extract_all_content(content) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""

        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
            elif block_type == "tool_use":
                name = block.get("name", "")
                tool_input = block.get("input", {})
                if name in ("Read", "Glob", "Grep"):
                    parts.append(f"[Tool: {name} {tool_input.get('file_path', tool_input.get('pattern', ''))}]")
                elif name in ("Edit", "Write"):
                    parts.append(f"[Tool: {name} {tool_input.get('file_path', '')}]")
                elif name == "Bash":
                    parts.append(f"[Tool: Bash] {tool_input.get('command', '')[:200]}")
                else:
                    parts.append(f"[Tool: {name}]")
            elif block_type == "tool_result":
                result_content = block.get("content", "")
                if block.get("is_error", False):
                    result_text = result_content if isinstance(result_content, str) else str(result_content)
                    parts.append(f"[Error] {result_text[:500]}")
                elif isinstance(result_content, str) and result_content.strip():
                    parts.append(f"[Result] {result_content[:500]}")
                elif isinstance(result_content, list):
                    for sub in result_content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            text = sub.get("text", "").strip()
                            if text:
                                parts.append(f"[Result] {text[:500]}")
        return "\n".join(parts)

    with open(jsonl_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            content = obj.get("message", {}).get("content", "")
            text = _extract_all_content(content)
            if not text:
                continue

            if msg_type == "user" and assistant_texts:
                _flush()

            if msg_type == "user":
                user_texts.append(text)
            else:
                assistant_texts.append(text)

    _flush()
    return pairs


def _extract_conversation(jsonl_path: str) -> list[str]:
    """Extract human messages and assistant prose from a session, stripped of noise.

    Returns a list of lines like "User: ..." and "Assistant: ..." with tool
    calls, tool results, and system reminders stripped out.
    """
    lines = []
    msgs = parse_jsonl_session(jsonl_path)
    for msg in msgs:
        role = msg["role"]
        text = msg["text"]
        if role == "user":
            cleaned = _strip_system_noise(text)
            if cleaned:
                lines.append(f"User: {cleaned}")
        elif role == "assistant":
            if text:
                lines.append(f"Assistant: {text}")
    return lines


def transcript_search(query: str, limit: int = 5) -> str:
    query_words = set(query.lower().split())
    if not query_words:
        return "No matching transcripts found"

    scored = []
    for base_dir in SESSIONS_DIRS:
        if not base_dir.exists():
            continue
        for jsonl_path in base_dir.rglob("*.jsonl"):
            if "/subagents/" in str(jsonl_path):
                continue
            try:
                if jsonl_path.stat().st_size < 5000:
                    continue
                pairs = _parse_jsonl_session_pairs(str(jsonl_path))
            except Exception:
                continue
            for pair in pairs:
                text_words = set((pair["user_text"] + " " + pair["assistant_text"]).lower().split())
                score = len(query_words & text_words) / len(query_words)
                if score > 0:
                    scored.append((score, pair, jsonl_path.name))

    if not scored:
        return "No matching transcripts found"

    scored.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for _, pair, filename in scored[:limit]:
        lines.append(
            f"- **Q:** {pair['user_text'][:300]}\n"
            f"  **A:** {pair['assistant_text'][:500]}\n"
            f"  *Session: {filename}*"
        )
    return "\n\n".join(lines)
