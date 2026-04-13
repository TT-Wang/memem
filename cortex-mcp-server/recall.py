import logging
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime

from storage import (
    INDEX_PATH,
    _find_memory,
    _get_telemetry,
    _load_obsidian_memories,
    _obsidian_memories,
    _record_access,
    _word_set,
)
from transcripts import transcript_search

log = logging.getLogger("cortex-recall")


def _search_memories_fts(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """FTS5-first search: query SQLite index, load full memories from Obsidian."""
    try:
        from storage import _search_fts
        fts_ids = _search_fts(query, scope_id or "default", limit * 2)
        if not fts_ids:
            return []

        results = []
        for mid in fts_ids:
            mem = _find_memory(mid)
            if mem and mem.get("status", "active") != "deprecated":
                results.append(mem)

        # Apply temporal + importance weighting
        tel_scored = []
        for mem in results:
            tel = _get_telemetry(mem.get("id", ""))
            last_touch = tel.get("last_accessed") or mem.get("updated_at") or mem.get("created_at", "")
            try:
                if last_touch:
                    dt = datetime.fromisoformat(last_touch.replace("Z", "+00:00"))
                    hours_old = max(0, (datetime.now(UTC) - dt).total_seconds() / 3600)
                    recency = 0.995 ** hours_old
                else:
                    recency = 0.5
            except (ValueError, TypeError):
                recency = 0.5

            access_boost = min(tel.get("access_count", 0) / 10.0, 1.0)
            importance = mem.get("importance", 3) / 5.0

            # FTS already ranked by relevance, add temporal/importance boost
            fts_rank = 1.0 - (fts_ids.index(mem.get("id", "")) / len(fts_ids))  # 1.0 for top, 0.0 for bottom
            score = 0.5 * fts_rank + 0.15 * recency + 0.15 * access_boost + 0.2 * importance
            tel_scored.append((score, mem))

        tel_scored.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in tel_scored[:limit]]
    except Exception as exc:
        log.debug("FTS search failed, falling back to file scan: %s", exc)
        return []  # Fallback: caller will use file scan


def _search_memories(query: str, scope_id: str | None = None, limit: int = 10, record_access: bool = True) -> list[dict]:
    # Try FTS-first path
    fts_results = _search_memories_fts(query, scope_id, limit)
    if fts_results:
        # Expand linked memories
        seen_ids = {mem.get("id", "")[:8] for mem in fts_results}
        linked = []
        for mem in fts_results:
            for related_id in mem.get("related", []):
                if related_id in seen_ids:
                    continue
                seen_ids.add(related_id)
                related_mem = _find_memory(related_id)
                if related_mem:
                    linked.append(related_mem)
        results = (fts_results + linked)[:limit * 2]
        if record_access:
            for mem in results:
                mem_id = mem.get("id", "")
                if mem_id:
                    _record_access(mem_id)
        return results

    # Fallback to file scan (existing code continues below)
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
        keyword_score = (title_hits * 2 + body_hits) / len(query_words)
        if keyword_score >= 0.3:
            # Temporal + access weighting (telemetry from sidecar)
            tel = _get_telemetry(mem.get("id", ""))
            last_touch = tel.get("last_accessed") or mem.get("updated_at") or mem.get("created_at", "")
            try:
                if last_touch:
                    dt = datetime.fromisoformat(last_touch.replace("Z", "+00:00"))
                    hours_old = max(0, (datetime.now(UTC) - dt).total_seconds() / 3600)
                    recency = 0.995 ** hours_old
                else:
                    recency = 0.5
            except (ValueError, TypeError):
                recency = 0.5

            access_count = tel.get("access_count", 0)
            access_boost = min(access_count / 10.0, 1.0)

            importance = mem.get("importance", 3)
            importance_score = importance / 5.0

            score = 0.5 * keyword_score + 0.15 * recency + 0.15 * access_boost + 0.2 * importance_score

            result = dict(mem)
            scored.append((score, result))

    scored.sort(key=lambda item: item[0], reverse=True)
    primary = [mem for _, mem in scored[:limit]]

    # Expand linked memories
    seen_ids = {mem.get("id", "")[:8] for mem in primary}
    linked = []
    for mem in primary:
        for related_id in mem.get("related", []):
            if related_id in seen_ids:
                continue
            seen_ids.add(related_id)
            related_mem = _find_memory(related_id)
            if related_mem:
                linked.append(related_mem)

    max_total = limit * 2
    results = (primary + linked)[:max_total]

    # Track access for returned memories (skip for internal/assembly calls)
    if record_access:
        for mem in results:
            mem_id = mem.get("id", "")
            if mem_id:
                _record_access(mem_id)

    return results


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
    memories = _obsidian_memories(scope_id)
    if not memories:
        return f"No memories in scope: {scope_id}"

    memories.sort(key=lambda mem: mem.get("created_at", ""), reverse=True)

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


def smart_recall(prompt: str, scope_id: str = "default") -> str:
    """Use Claude Haiku to intelligently select relevant memories from the index.

    Unlike `memory_recall` which performs keyword search, this function sends the
    full memory index to Haiku and asks it to pick relevant memory IDs based on
    semantic understanding of the prompt. Falls back to `memory_recall` when the
    index is unavailable or Haiku returns no results.
    """
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
    except Exception as exc:
        log.debug("Smart recall Haiku failed: %s", exc)

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
