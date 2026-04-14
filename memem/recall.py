import logging
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime

from memem.models import DEFAULT_LAYER, INDEX_PATH
from memem.obsidian_store import (
    _find_memory,
    _load_obsidian_memories,
    _obsidian_memories,
    _word_set,
)


def _parse_ts(ts: str) -> float:
    """Parse an ISO-8601 timestamp to a float for sorting. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0
from memem.telemetry import (
    _get_telemetry,
    _record_access,
)
from memem.transcripts import transcript_search

log = logging.getLogger("memem-recall")


def _search_memories_fts(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """FTS5-first search: query SQLite index, load full memories from Obsidian."""
    try:
        from memem.search_index import _search_fts
        fts_ids = _search_fts(query, scope_id or "default", limit * 2)
        if not fts_ids:
            return []

        # Preserve FTS order as a position map so scoring is O(1) and
        # immune to ValueError if a memory id ever falls out of fts_ids.
        fts_rank_by_id = {mid: i for i, mid in enumerate(fts_ids)}
        total_fts = len(fts_ids)

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

            # FTS already ranked by relevance, add temporal/importance boost.
            # Falls back to mid-rank (0.5) if the id is unexpectedly missing.
            mem_id = mem.get("id", "")
            rank_pos = fts_rank_by_id.get(mem_id, total_fts // 2)
            fts_rank = 1.0 - (rank_pos / total_fts) if total_fts else 0.5
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


def _format_compact_index_line(mem: dict) -> str:
    """Return a ~50-token compact line for a single memory.

    Format: ``[<8-char-id>] L<layer> <title> — <essence truncated to 80 chars>``
    Used by memory_search, the session-start briefing CLI flag, and /memem.
    """
    mem_id = mem.get("id", "")[:8]
    layer = mem.get("layer", DEFAULT_LAYER)
    title = mem.get("title", "Untitled")
    essence = mem.get("essence") or mem.get("full_record", "") or ""
    essence_line = " ".join(essence.split())[:80]
    return f"[{mem_id}] L{layer} {title} — {essence_line}"


def _format_full_memory(mem: dict) -> str:
    """Return a markdown-formatted full memory with frontmatter metadata.

    Used by memory_get for the layer-2 (full content) fetch.
    """
    mid = mem.get("id", "")[:8]
    layer = mem.get("layer", DEFAULT_LAYER)
    title = mem.get("title", "Untitled")
    tags = mem.get("tags", [])
    related = mem.get("related", [])
    source = mem.get("source_type", "unknown")
    project = mem.get("project", "general")
    essence = mem.get("essence") or mem.get("full_record", "") or ""

    lines = [
        f"### [{mid}] {title}",
        f"- **layer:** L{layer}",
        f"- **project:** {project}",
        f"- **source:** {source}",
    ]
    if tags:
        lines.append(f"- **tags:** {', '.join(tags)}")
    if related:
        lines.append(f"- **related:** {', '.join(r[:8] for r in related)}")
    lines.append("")
    lines.append(essence)
    lines.append("")
    return "\n".join(lines)


def _linked_memories(primary: list[dict]) -> list[dict]:
    """One-hop graph traversal: return memories linked via the `related` field.

    Given a list of primary memories, follow each memory's `related[]` list
    exactly one hop and return the deduplicated set of linked memories that
    aren't already in `primary`. Does not recurse — linked memories' own
    `related[]` is ignored.
    """
    seen = {mem.get("id", "")[:8] for mem in primary if mem.get("id")}
    linked: list[dict] = []
    for mem in primary:
        for related_id in mem.get("related", []) or []:
            key = related_id[:8]
            if key in seen:
                continue
            linked_mem = _find_memory(related_id)
            if linked_mem:
                seen.add(key)
                linked.append(linked_mem)
    return linked


def memory_search(query: str, limit: int = 10, scope_id: str = "default") -> str:
    """Layer 1 (compact index) search — the 3-tier recall entry point.

    Returns a compact markdown index of matching memories (~50 tok/result),
    then a one-hop graph-traversed section of related memories. Use this
    FIRST to narrow candidates, then drill into specific IDs via memory_get.
    """
    memories = _search_memories(query, scope_id=scope_id, limit=limit, record_access=False)
    if not memories:
        return f"No memories found for: {query}"

    linked = _linked_memories(memories)

    lines = [f"### Compact memory index ({len(memories)} results)"]
    for mem in memories:
        lines.append(_format_compact_index_line(mem))

    if linked:
        lines.append("")
        lines.append(f"### Related memories (via graph traversal, {len(linked)} linked)")
        for mem in linked:
            lines.append(_format_compact_index_line(mem))

    lines.append("")
    lines.append("_Use memory_get(ids=[...]) to fetch full content for any of these._")
    return "\n".join(lines)


def memory_get(ids: list[str], scope_id: str = "default") -> str:
    """Layer 2 (full content) fetch — drill into specific memories by ID.

    Accepts a list of memory IDs (8-char prefix supported). Returns full
    markdown content for each, followed by a one-hop graph-traversed section
    of linked memories. Use this after memory_search has narrowed candidates.
    """
    if not ids:
        return "No IDs provided. Call memory_search first, then pass specific IDs here."

    found: list[dict] = []
    missing: list[str] = []
    for mid in ids:
        mem = _find_memory(mid)
        if mem:
            found.append(mem)
        else:
            missing.append(mid)

    lines: list[str] = []
    for mem in found:
        lines.append(_format_full_memory(mem))

    for mid in missing:
        lines.append(f"[not-found: {mid}]")

    linked = _linked_memories(found)
    if linked:
        lines.append(f"### Related memories (via graph traversal, {len(linked)} linked)")
        for mem in linked:
            lines.append(_format_compact_index_line(mem))

    return "\n".join(lines) if lines else f"No memories found for ids: {ids}"


def memory_timeline(
    memory_id: str,
    depth_before: int = 5,
    depth_after: int = 5,
    scope_id: str = "default",
) -> str:
    """Layer 3 (chronological thread) — walk related + creation-time context.

    Finds the anchor memory, then builds a chronological thread using:
      1. The anchor's ``related[]`` list (forward links)
      2. Any memory whose ``related[]`` points back at the anchor (reverse links)
      3. Same-project memories in the chronological window around the anchor

    Returns a markdown timeline header + compact lines in creation order.
    """
    anchor = _find_memory(memory_id)
    if not anchor:
        return f"Anchor memory not found: {memory_id}"

    anchor_id8 = anchor.get("id", "")[:8]
    anchor_project = anchor.get("project", "general")
    anchor_created = _parse_ts(anchor.get("created", ""))

    all_mems = _obsidian_memories()

    # Forward links
    forward_ids = {r[:8] for r in anchor.get("related", []) or []}
    # Reverse links
    reverse_ids = {
        m.get("id", "")[:8]
        for m in all_mems
        if anchor_id8 in {r[:8] for r in m.get("related", []) or []}
    }

    candidates: dict[str, dict] = {}
    for mem in all_mems:
        mid8 = mem.get("id", "")[:8]
        if mid8 == anchor_id8:
            continue
        if mid8 in forward_ids or mid8 in reverse_ids:
            candidates[mid8] = mem
            continue
        # Same-project chronological window
        if mem.get("project") == anchor_project:
            candidates[mid8] = mem

    # Split into before/after by creation time
    before: list[dict] = []
    after: list[dict] = []
    for mem in candidates.values():
        created = _parse_ts(mem.get("created", ""))
        if created < anchor_created:
            before.append(mem)
        else:
            after.append(mem)

    before.sort(key=lambda m: _parse_ts(m.get("created", "")))
    after.sort(key=lambda m: _parse_ts(m.get("created", "")))
    before = before[-depth_before:] if depth_before > 0 else []
    after = after[:depth_after] if depth_after > 0 else []

    lines = [f"### Timeline around [{anchor_id8}] {anchor.get('title', 'Untitled')}"]
    lines.append("")
    if before:
        lines.append(f"**Before ({len(before)}):**")
        for mem in before:
            ts = mem.get("created", "")[:10]
            lines.append(f"- {ts}  {_format_compact_index_line(mem)}")
        lines.append("")
    lines.append("**Anchor:**")
    lines.append(f"- {anchor.get('created', '')[:10]}  {_format_compact_index_line(anchor)}")
    lines.append("")
    if after:
        lines.append(f"**After ({len(after)}):**")
        for mem in after:
            ts = mem.get("created", "")[:10]
            lines.append(f"- {ts}  {_format_compact_index_line(mem)}")
        lines.append("")
    return "\n".join(lines)


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

    # Degraded mode: if claude CLI is unavailable, fall back to keyword recall
    # (which uses FTS5 only). No user-facing failure.
    from memem.capabilities import assembly_available
    if not assembly_available():
        log.info("smart_recall: claude CLI unavailable, falling back to memory_recall (degraded)")
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

    by_project: dict[str, list[dict]] = {}
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
