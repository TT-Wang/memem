import json
import os
from pathlib import Path


def _parse_jsonl_session(jsonl_path: str) -> list[dict]:
    """Parse JSONL session into user/assistant exchange pairs."""
    user_texts = []
    assistant_texts = []
    pairs = []

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


def transcript_search(query: str, limit: int = 5) -> str:
    base_dirs = [Path.home() / ".claude" / "projects"]
    extra = os.environ.get("CORTEX_EXTRA_SESSION_DIRS", "")
    if extra:
        base_dirs.extend(Path(path) for path in extra.split(":") if path)

    query_words = set(query.lower().split())
    if not query_words:
        return "No matching transcripts found"

    scored = []
    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for jsonl_path in base_dir.rglob("*.jsonl"):
            if "/subagents/" in str(jsonl_path):
                continue
            try:
                if jsonl_path.stat().st_size < 5000:
                    continue
                pairs = _parse_jsonl_session(str(jsonl_path))
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
