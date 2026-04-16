import logging
from collections import Counter
from datetime import UTC, datetime

from memem.models import DEFAULT_LAYER, LAST_BRIEF_PATH
from memem.obsidian_store import (
    _find_memory,
    _obsidian_memories,
    _word_set,
)
from memem.telemetry import (
    _get_telemetry,
    _record_access,
)
from memem.transcripts import transcript_search

log = logging.getLogger("memem-recall")


def _get_current_session_id() -> str:
    """Read the current session_id from .last-brief.json (written by auto-recall hook)."""
    try:
        import json as _json

        data = _json.loads(LAST_BRIEF_PATH.read_text())
        return data.get("session_id", "")
    except (OSError, ValueError, _json.JSONDecodeError):
        return ""


def _parse_ts(ts: str) -> float:
    """Parse an ISO-8601 timestamp to a float for sorting. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _search_memories_fts(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """Candidate-union search: parallel FTS + ngram → dedupe → re-rank → top-N.

    Runs two candidate generators in parallel:
      • FTS5 (surface-form keyword match, fast, may miss paraphrases)
      • ngram containment (word/bigram/trigram overlap over the cached vault;
        catches paraphrase matches FTS misses)

    Takes the union of candidate IDs (preserving FTS order for FTS hits,
    appending ngram-only hits at the end). Loads full memories from the
    cache, then applies the 5-signal re-ranker (FTS rank, recency, access
    telemetry, importance, feedback) to the full union BEFORE truncation —
    so ngram-only candidates compete on equal footing with FTS hits and
    don't get silently dropped by an early FTS limit. Strict superset of
    the old FTS-only behavior: every memory FTS would have returned is
    still present in the union.
    """
    from concurrent.futures import ThreadPoolExecutor

    scope = scope_id or "default"
    candidate_limit = limit * 4  # generous — re-ranker filters noise

    try:
        from memem.obsidian_store import _ngram_search_candidates
        from memem.search_index import _search_fts

        with ThreadPoolExecutor(max_workers=2) as pool:
            fts_future = pool.submit(_search_fts, query, scope, candidate_limit)
            ngram_future = pool.submit(_ngram_search_candidates, query, scope, candidate_limit)
            try:
                fts_ids = fts_future.result(timeout=10) or []
            except Exception as exc:
                log.debug("FTS candidate generation failed: %s", exc)
                fts_ids = []
            try:
                ngram_ids = ngram_future.result(timeout=10) or []
            except Exception as exc:
                log.debug("ngram candidate generation failed: %s", exc)
                ngram_ids = []

        if not fts_ids and not ngram_ids:
            return []

        # Preserve FTS rank for its hits; ngram-only hits get no FTS rank.
        fts_rank_by_id = {mid: i for i, mid in enumerate(fts_ids)}
        total_fts = len(fts_ids)
        union_ids: list[str] = list(fts_ids)
        seen = set(fts_ids)
        for mid in ngram_ids:
            if mid not in seen:
                union_ids.append(mid)
                seen.add(mid)

        # Load full memories from cache (O(1) per lookup after m1)
        mems: list[dict] = []
        for mid in union_ids:
            mem = _find_memory(mid)
            if mem and mem.get("status", "active") != "deprecated":
                mems.append(mem)
        if not mems:
            return []

        # 5-signal re-rank across the FULL union (not after any truncation)
        from memem.feedback import get_relevance_score

        scored: list[tuple[float, dict]] = []
        for mem in mems:
            mem_id = mem.get("id", "")
            tel = _get_telemetry(mem_id)
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

            if mem_id in fts_rank_by_id and total_fts:
                rank_pos = fts_rank_by_id[mem_id]
                fts_rank = 1.0 - (rank_pos / total_fts)
            else:
                # ngram-only hit — give a neutral FTS score so ngram signal
                # isn't forced to beat a "0 FTS rank" penalty.
                fts_rank = 0.5

            feedback_raw = get_relevance_score(mem_id)
            feedback_norm = (feedback_raw + 1.0) / 2.0

            score = (
                0.45 * fts_rank
                + 0.15 * recency
                + 0.15 * access_boost
                + 0.15 * importance
                + 0.10 * feedback_norm
            )
            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in scored[:limit]]
    except Exception as exc:
        log.debug("Union search failed, falling back to file scan: %s", exc)
        return []  # Fallback: caller will use file scan


def _expand_graph(seed_mems: list[dict], max_total: int, hops: int = 2) -> list[dict]:
    """Breadth-first graph expansion over `related[]` edges, cap-aware.

    Preserves the ordering guarantee: seeds come first, then 1-hop neighbors,
    then 2-hop neighbors. The cap applies in that order, so 1-hop results
    are never squeezed out by 2-hop results — a strict superset of the
    prior 1-hop-only behavior when the cap is the same.

    Dedupe is by 8-char id prefix (same as the prior code).
    """
    if not seed_mems:
        return []
    seen_ids = {m.get("id", "")[:8] for m in seed_mems}
    results: list[dict] = list(seed_mems)
    frontier = list(seed_mems)
    for _hop in range(hops):
        if len(results) >= max_total:
            break
        next_frontier: list[dict] = []
        for mem in frontier:
            for related_id in mem.get("related", []):
                if related_id in seen_ids:
                    continue
                seen_ids.add(related_id)
                related_mem = _find_memory(related_id)
                if related_mem is None:
                    continue
                results.append(related_mem)
                next_frontier.append(related_mem)
                if len(results) >= max_total:
                    break
            if len(results) >= max_total:
                break
        if not next_frontier:
            break
        frontier = next_frontier
    return results[:max_total]


def _search_memories(
    query: str,
    scope_id: str | None = None,
    limit: int = 10,
    record_access: bool = True,
    expand_links: bool = True,
) -> list[dict]:
    # Try FTS-first path
    fts_results = _search_memories_fts(query, scope_id, limit)
    if fts_results:
        results = (
            _expand_graph(fts_results, max_total=limit * 2, hops=2)
            if expand_links
            else fts_results[:limit]
        )
        if record_access:
            session_id = _get_current_session_id()
            for mem in results:
                mem_id = mem.get("id", "")
                if mem_id:
                    _record_access(mem_id)
                    if session_id:
                        from memem.telemetry import record_session_recall

                        record_session_recall(session_id, mem_id)
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

            # Relevance feedback (closed-loop signal from session outcomes).
            from memem.feedback import get_relevance_score

            feedback_raw = get_relevance_score(mem.get("id", ""))
            feedback_norm = (feedback_raw + 1.0) / 2.0

            score = (
                0.45 * keyword_score
                + 0.15 * recency
                + 0.15 * access_boost
                + 0.15 * importance_score
                + 0.10 * feedback_norm
            )

            result = dict(mem)
            scored.append((score, result))

    scored.sort(key=lambda item: item[0], reverse=True)
    primary = [mem for _, mem in scored[:limit]]

    if expand_links:
        # Expand linked memories (for memory_recall backward-compat path)
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
    else:
        results = primary[:limit]

    # Track access for returned memories (skip for internal/assembly calls)
    if record_access:
        session_id = _get_current_session_id()
        for mem in results:
            mem_id = mem.get("id", "")
            if mem_id:
                _record_access(mem_id)
                if session_id:
                    from memem.telemetry import record_session_recall

                    record_session_recall(session_id, mem_id)

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
    tags = mem.get("domain_tags") or mem.get("tags") or []
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


def _linked_memories(primary: list[dict], hops: int = 2) -> list[dict]:
    """Graph traversal over `related[]`: return linked memories not in primary.

    Defaults to 2-hop since Wave 1 made cache lookups O(1) — the extra hop
    is cheap and widens the net so "neighbor of neighbor" memories get
    surfaced. Always a strict superset of 1-hop: 1-hop results appear
    before 2-hop results in the expansion order, so truncation (if any
    happens at the caller) never drops 1-hop in favor of 2-hop.
    """
    primary_ids = {m.get("id", "")[:8] for m in primary if m.get("id")}
    expanded = _expand_graph(primary, max_total=max(len(primary) * 10, 20), hops=hops)
    return [m for m in expanded if m.get("id", "")[:8] not in primary_ids]


def memory_search(query: str, limit: int = 10, scope_id: str = "default") -> str:
    """Layer 1 (compact index) search — the 3-tier recall entry point.

    Returns a compact markdown index of matching memories (~50 tok/result),
    then a one-hop graph-traversed section of related memories. Use this
    FIRST to narrow candidates, then drill into specific IDs via memory_get.
    """
    memories = _search_memories(
        query, scope_id=scope_id, limit=limit, record_access=False, expand_links=False
    )
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
    anchor_created = _parse_ts(anchor.get("created_at", ""))

    all_mems = _obsidian_memories(scope_id=scope_id)

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
        created = _parse_ts(mem.get("created_at", ""))
        if created < anchor_created:
            before.append(mem)
        else:
            after.append(mem)

    before.sort(key=lambda m: _parse_ts(m.get("created_at", "")))
    after.sort(key=lambda m: _parse_ts(m.get("created_at", "")))
    before = before[-depth_before:] if depth_before > 0 else []
    after = after[:depth_after] if depth_after > 0 else []

    lines = [f"### Timeline around [{anchor_id8}] {anchor.get('title', 'Untitled')}"]
    lines.append("")
    if before:
        lines.append(f"**Before ({len(before)}):**")
        for mem in before:
            ts = mem.get("created_at", "")[:10]
            lines.append(f"- {ts}  {_format_compact_index_line(mem)}")
        lines.append("")
    lines.append("**Anchor:**")
    lines.append(f"- {anchor.get('created_at', '')[:10]}  {_format_compact_index_line(anchor)}")
    lines.append("")
    if after:
        lines.append(f"**After ({len(after)}):**")
        for mem in after:
            ts = mem.get("created_at", "")[:10]
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


