import re
import subprocess

from storage import INDEX_PATH, _load_obsidian_memories, _obsidian_memories, _word_set
from transcripts import transcript_search


def _search_memories(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    query_words = _word_set(query)
    if not query_words:
        return []

    scored = []
    for mem in _obsidian_memories(scope_id):
        title = mem.get("title", "")
        tags = mem.get("domain_tags", [])
        body = mem.get("full_record", "")
        mem_words = _word_set(title + " " + " ".join(tags) + " " + body)
        # Title matches count double
        title_words = _word_set(title + " " + " ".join(tags))
        title_hits = len(query_words & title_words)
        body_hits = len(query_words & mem_words) - title_hits
        score = (title_hits * 2 + body_hits) / len(query_words)
        if score > 0:
            result = dict(mem)
            scored.append((score, result))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [mem for _, mem in scored[:limit]]


def _format_memory_as_bullet(mem: dict) -> str:
    title = mem.get("title", "Untitled")
    body = mem.get("full_record") or mem.get("essence", "")

    line = f"- **{title}**"
    if body and body[:60] != title[:60]:
        line += f"\n  {body[:500]}"
    return line


def memory_recall(query: str, scope_id: str = "default", limit: int = 10) -> str:
    memories = _search_memories(query, scope_id=scope_id, limit=limit)
    transcript_results = transcript_search(query, limit=3)

    if not memories and ("No matching" in transcript_results or not transcript_results):
        return f"No memories found for: {query}"

    sections = []
    if memories:
        lines = ["### Memories"]
        for mem in memories:
            lines.append(_format_memory_as_bullet(mem))
        lines.append("")
        sections.append("\n".join(lines))

    if transcript_results and "No matching" not in transcript_results:
        sections.append(f"### Related Session Logs\n\n{transcript_results}")

    return "\n".join(sections) if sections else f"No memories found for: {query}"


def memory_list(scope_id: str = "default") -> str:
    memories = _obsidian_memories(scope_id if scope_id != "default" else None)
    if not memories:
        return f"No memories in scope: {scope_id}"

    memories.sort(key=lambda mem: mem.get("created_at", ""), reverse=True)

    from collections import Counter

    sources = Counter(mem.get("source_type", "?") for mem in memories)
    lines = [
        f"**{len(memories)} memories**",
        f"Sources: {', '.join(f'{key}:{value}' for key, value in sources.most_common())}",
        "",
    ]
    for mem in memories:
        lines.append(
            f"- [{mem.get('id', '')[:8]}] {mem.get('title', 'Untitled')[:50]} | project:{mem.get('project', 'general')}"
        )
    return "\n".join(lines)


def _smart_recall(prompt: str, scope_id: str = "default") -> str:
    if not INDEX_PATH.exists():
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    index_content = INDEX_PATH.read_text()
    if not index_content.strip():
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    system_prompt = "You are a memory selector. Output ONLY 8-character memory IDs, one per line, nothing else."
    user_prompt = (
        f"USER MESSAGE:\n{prompt}\n\n"
        f"MEMORY INDEX:\n{index_content}\n\n"
        "Select any memories that could be relevant to the user's message. "
        "Be generous — include anything that might help, even loosely related. "
        "Let the user's intent guide you. Output ONLY the 8-char IDs from parentheses, one per line."
    )

    picked_ids = []
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--system-prompt", system_prompt],
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                match = re.search(r"\b([0-9a-f]{8})\b", line.strip().strip("-").strip())
                if match:
                    picked_ids.append(match.group(1))
    except Exception:
        pass

    if not picked_ids:
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    picked_files = _load_obsidian_memories(picked_ids)
    if not picked_files:
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    by_project = {}
    for mem in picked_files:
        by_project.setdefault(mem["project"], []).append(mem)

    sections = []
    sorted_projects = sorted(project for project in by_project if project != "general")
    if "general" in by_project:
        sorted_projects.append("general")

    for project in sorted_projects:
        lines = [f"### {project}"]
        for mem in by_project[project]:
            entry = f"- **{mem['title']}**"
            if mem["body"]:
                entry += f"\n  {mem['body'][:500]}"
            lines.append(entry)
        sections.append("\n".join(lines))

    transcript_results = transcript_search(prompt, limit=3)
    if transcript_results and "No matching" not in transcript_results:
        sections.append(f"### Related Session Logs\n\n{transcript_results}")

    return "\n\n".join(sections) if sections else f"No memories found for: {prompt}"
