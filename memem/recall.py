import hashlib
import logging
from collections import Counter
from datetime import UTC, datetime

from memem.models import DEFAULT_LAYER, LAST_BRIEF_PATH, LAYER_L0, now_iso, parse_iso_dt
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
    dt = parse_iso_dt(ts)
    return dt.timestamp() if dt is not None else 0.0


def _stable_id(kind: str, payload: object) -> str:
    """Deterministic 12-char hex id from kind + payload repr."""
    raw = f"{kind}:{payload!r}"
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:12]  # noqa: S324


def _layer_summary_from_items(items: list[dict]) -> dict:
    """Return a {layer: count} summary dict from a list of item dicts."""
    summary: dict[int, int] = {}
    for item in items:
        layer = int(item.get("layer") or DEFAULT_LAYER)
        summary[layer] = summary.get(layer, 0) + 1
    return {str(k): v for k, v in sorted(summary.items())}


def _render_recall_markdown(slice_data: dict) -> str:
    """Inline renderer: convert a slice dict to markdown for recall/search/get tools.

    Replaces the old render_slice_markdown dependency on active_slice.py.
    Renders a compact but complete markdown representation.
    """
    slice_kind = slice_data.get("slice_kind", "search")
    scope_id = slice_data.get("scope_id", "default")
    query = slice_data.get("query", "")
    items = slice_data.get("items", [])
    linked = slice_data.get("linked", [])
    missing_ids = slice_data.get("missing_ids", [])
    anchor_id = slice_data.get("anchor_id", "")
    anchor_title = slice_data.get("anchor_title", "")
    before_items = slice_data.get("before_items", [])
    after_items = slice_data.get("after_items", [])

    lines: list[str] = []

    if slice_kind == "timeline":
        lines.append(f"## Memory Timeline — {anchor_title} [{anchor_id}]")
        lines.append("")
        if before_items:
            lines.append("### Before")
            for item in before_items:
                lines.append(_format_compact_index_line(item))
            lines.append("")
        lines.append("### Anchor")
        anchor_item = next((i for i in items if i.get("id", "")[:8] == anchor_id), None)
        if anchor_item:
            lines.append(_format_full_memory(anchor_item))
        else:
            lines.append(f"[{anchor_id}] {anchor_title}")
        lines.append("")
        if after_items:
            lines.append("### After")
            for item in after_items:
                lines.append(_format_compact_index_line(item))
            lines.append("")
    elif slice_kind == "get":
        if query:
            lines.append(f"## Memory Get — {scope_id}")
            lines.append(f"> query: {query}")
        else:
            lines.append(f"## Memory Get — {scope_id}")
        lines.append("")
        for item in items:
            lines.append(_format_full_memory(item))
        if missing_ids:
            lines.append(f"_Not found: {', '.join(missing_ids)}_")
            lines.append("")
    else:
        # search / recall
        header = f"## Memory {'Recall' if slice_kind == 'recall' else 'Search'} — {scope_id}"
        if query:
            header += f" — `{query}`"
        lines.append(header)
        lines.append("")
        for item in items:
            lines.append(_format_compact_index_line(item))
        lines.append("")

    if linked:
        lines.append("### Related")
        for item in linked:
            lines.append(_format_compact_index_line(item))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _memory_to_item(
    mem: dict,
    score: float | None = None,
    include_snippet: bool = False,
    position: int | None = None,
    parent_id: str | None = None,
) -> dict:
    """Convert a raw memory dict to a plain dict for slice output."""
    essence = mem.get("essence") or mem.get("full_record", "") or ""
    snippet = " ".join(essence.split())[:80] if include_snippet else ""
    raw_layer = mem.get("layer")
    layer = int(raw_layer) if raw_layer is not None else DEFAULT_LAYER
    item: dict = {
        "id": mem.get("id", ""),
        "title": mem.get("title", "Untitled"),
        "content": essence,
        "layer": layer,
        "project": mem.get("project", "general"),
        "tags": mem.get("domain_tags") or mem.get("tags") or [],
        "created_at": mem.get("created_at", ""),
        "updated_at": mem.get("updated_at", ""),
        "related": mem.get("related", []),
    }
    # source_type — validate or default
    raw_source = mem.get("source_type", "user")
    if raw_source in ("user", "mined", "import", "transcript", "playbook"):
        item["source_type"] = raw_source
    if score is not None:
        item["score"] = float(score)
    if snippet:
        item["snippet"] = snippet
    if position is not None:
        item["position"] = position
    if parent_id is not None:
        item["parent_id"] = parent_id
    if mem.get("importance") is not None:
        item["importance"] = int(mem.get("importance", 3) or 3)
    # v2 schema fields
    if mem.get("valid_at"):
        item["valid_at"] = mem["valid_at"]
    if mem.get("invalid_at") is not None:
        item["invalid_at"] = mem["invalid_at"]
    if mem.get("replaced_by") is not None:
        item["replaced_by"] = mem["replaced_by"]
    if mem.get("last_accessed_at"):
        item["last_accessed_at"] = mem["last_accessed_at"]
    if mem.get("access_count") is not None:
        item["access_count"] = int(mem.get("access_count", 0) or 0)
    if mem.get("decay_immune") is not None:
        item["decay_immune"] = bool(mem.get("decay_immune", False))
    return item


def _search_memories_fts(query: str, scope_id: str | None = None, limit: int = 10, rerank_model: str | None = None) -> list[dict]:
    """Candidate-union search: parallel FTS + ngram → dedupe → re-rank → top-N.

    Runs two candidate generators in parallel:
      • FTS5 (surface-form keyword match, fast, may miss paraphrases)
      • ngram containment (word/bigram/trigram overlap over the cached vault;
        catches paraphrase matches FTS misses)

    Takes the union of candidate IDs (preserving FTS order for FTS hits,
    appending ngram-only hits at the end). Loads full memories from the
    cache, then applies a 5-signal re-ranker (FTS rank, recency, access
    telemetry, importance, feedback) to the full union BEFORE truncation.
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

        # Preserve FTS rank for its hits; ngram-only hits get a neutral FTS-rank.
        fts_rank_by_id = {mid: i for i, mid in enumerate(fts_ids)}
        total_fts = len(fts_ids)
        union_ids: list[str] = list(fts_ids)
        seen = set(fts_ids)
        for mid in ngram_ids:
            if mid not in seen:
                union_ids.append(mid)
                seen.add(mid)

        # Load full memories from cache
        mems: list[dict] = []
        for mid in union_ids:
            mem = _find_memory(mid)
            if mem and mem.get("status", "active") != "deprecated":
                # Bi-temporal filter: exclude memories that have been invalidated
                if mem.get("invalid_at") is not None:
                    continue
                mems.append(mem)
        if not mems:
            return []

        # 5-signal re-rank across the FULL union.
        # Weights: FTS 0.35 + recency 0.20 + access 0.10 + importance 0.20 + feedback 0.15 = 1.00
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
                fts_rank = 0.5

            feedback_raw = get_relevance_score(mem_id)
            feedback_norm = (feedback_raw + 1.0) / 2.0

            score = (
                0.35 * fts_rank
                + 0.20 * recency
                + 0.10 * access_boost
                + 0.20 * importance
                + 0.15 * feedback_norm
            )
            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)

        import memem.settings as _memem_settings
        _min_item_score = _memem_settings.MEMEM_RECALL_MIN_ITEM_SCORE
        if _min_item_score > 0.0:
            scored = [
                (s, m) for s, m in scored
                if s >= _min_item_score or m.get("layer") == LAYER_L0
            ]

        # Optional cross-encoder rerank: take top-50, score with cross-encoder.
        if rerank_model:
            top50 = [mem for _, mem in scored[:50]]
            try:
                from memem.cross_encoder_rerank import rerank_pairs

                ce_pairs = rerank_pairs(query, top50, model_name=rerank_model)
                id_to_mem = {mem.get("id", ""): mem for mem in top50}
                reranked = [id_to_mem[mid] for mid, _score in ce_pairs if mid in id_to_mem]
                seen_reranked = {mem.get("id", "") for mem in reranked}
                for mem in top50:
                    if mem.get("id", "") not in seen_reranked:
                        reranked.append(mem)
                return reranked[:limit]
            except Exception as exc:  # noqa: BLE001
                log.warning("cross-encoder rerank failed, using heuristic order: %s", exc)

        return [mem for _, mem in scored[:limit]]
    except Exception as exc:
        log.debug("Union search failed, falling back to file scan: %s", exc)
        return []  # Fallback: caller will use file scan


def _expand_graph(seed_mems: list[dict], max_total: int, hops: int = 2) -> list[dict]:
    """Breadth-first graph expansion over typed graph edges, cap-aware.

    Uses graph.db when available and falls back to Markdown `related[]`.
    Normal recall excludes historical/conflict edges so deprecated decisions
    don't leak into compact recall unless the caller asks for a timeline.
    """
    graph_results = _expand_graph_index(seed_mems, max_total=max_total, hops=hops)
    if len(graph_results) > len(seed_mems):
        return graph_results
    return _expand_related_frontmatter(seed_mems, max_total=max_total, hops=hops)


def _expand_graph_index(seed_mems: list[dict], max_total: int, hops: int = 2) -> list[dict]:
    """Typed graph expansion via graph.db. Returns seeds only if unavailable."""
    if not seed_mems:
        return []
    try:
        from memem.graph_index import _NORMAL_RECALL_TYPES, _neighbors
    except Exception as exc:
        log.debug("graph index unavailable; using related[] fallback: %s", exc)
        return list(seed_mems)

    seen_ids = {m.get("id", "")[:8] for m in seed_mems}
    results: list[dict] = list(seed_mems)
    frontier = list(seed_mems)
    min_scores = [0.18, 0.30]
    for hop in range(hops):
        if len(results) >= max_total:
            break
        next_frontier: list[dict] = []
        min_score = min_scores[min(hop, len(min_scores) - 1)]
        for mem in frontier:
            for edge in _neighbors(
                mem.get("id", ""),
                relation_types=_NORMAL_RECALL_TYPES,
                limit=max_total,
                min_score=min_score,
            ):
                related_id = edge.get("dst_id", "")
                rid8 = related_id[:8]
                if not rid8 or rid8 in seen_ids:
                    continue
                related_mem = _find_memory(related_id)
                if related_mem is None or related_mem.get("status", "active") == "deprecated":
                    continue
                seen_ids.add(rid8)
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


def _expand_related_frontmatter(seed_mems: list[dict], max_total: int, hops: int = 2) -> list[dict]:
    """Legacy BFS over Markdown `related[]` edges.

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
                if related_mem is None or related_mem.get("status", "active") == "deprecated":
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
    rerank_model: str | None = None,
) -> list[dict]:
    # Try FTS-first path
    fts_results = _search_memories_fts(query, scope_id, limit, rerank_model=rerank_model)
    if fts_results:
        results = (
            _expand_graph(fts_results, max_total=limit * 2, hops=2)
            if expand_links
            else fts_results[:limit]
        )
        if record_access:
            from memem.obsidian_store import bump_access
            session_id = _get_current_session_id()
            for mem in results:
                mem_id = mem.get("id", "")
                if mem_id:
                    _record_access(mem_id)
                    try:
                        bump_access(mem_id)
                    except Exception as _exc:
                        log.debug("bump_access failed for %s: %s", mem_id[:8], _exc)
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
        # Bi-temporal filter: exclude memories that have been invalidated
        if mem.get("invalid_at") is not None:
            continue
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
        max_total = limit * 2
        results = _expand_graph(primary, max_total=max_total, hops=2)
    else:
        results = primary[:limit]

    # Track access for returned memories (skip for internal/assembly calls)
    if record_access:
        from memem.obsidian_store import bump_access
        session_id = _get_current_session_id()
        for mem in results:
            mem_id = mem.get("id", "")
            if mem_id:
                _record_access(mem_id)
                try:
                    bump_access(mem_id)
                except Exception as _exc:
                    log.debug("bump_access failed for %s: %s", mem_id[:8], _exc)
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
    essence = mem.get("essence") or mem.get("content") or mem.get("full_record", "") or ""
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
    essence = mem.get("essence") or mem.get("content") or mem.get("full_record", "") or ""

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
    import time

    from memem.eval_capture import capture as _eval_capture
    _t0 = time.monotonic()
    memories = _search_memories(
        query, scope_id=scope_id, limit=limit, record_access=False, expand_links=False
    )
    _eval_capture(
        query=query, mode="search", scope_id=scope_id, limit=limit,
        memory_ids=[str(m.get("id") or "") for m in memories if m.get("id")],
        latency_ms=(time.monotonic() - _t0) * 1000.0,
    )
    if not memories:
        return f"No memories found for: {query}"

    linked = _linked_memories(memories)

    items = [_memory_to_item(m, include_snippet=True) for m in memories]
    linked_items = [_memory_to_item(m, include_snippet=True) for m in linked]
    layer_summary = _layer_summary_from_items(items)

    slice_data: dict = {
        "slice_id": _stable_id("search", (query, scope_id, limit)),
        "slice_kind": "search",
        "scope_id": scope_id,
        "query": query,
        "generated_at": now_iso(),
        "ordering": "relevance",
        "items": items,
        "linked": linked_items,
        "layer_summary": layer_summary,
    }
    return _render_recall_markdown(slice_data)


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

    items = [_memory_to_item(m, include_snippet=False) for m in found]
    linked = _linked_memories(found)
    linked_items = [_memory_to_item(m, include_snippet=True) for m in linked]

    if not items and not missing:
        return f"No memories found for ids: {ids}"

    slice_data: dict = {
        "slice_id": _stable_id("get", (tuple(ids), scope_id)),
        "slice_kind": "get",
        "scope_id": scope_id,
        "generated_at": now_iso(),
        "ordering": "manual",
        "items": items,
        "missing_ids": missing,
        "linked": linked_items,
        "layer_summary": _layer_summary_from_items(items),
    }
    return _render_recall_markdown(slice_data)


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
    try:
        from memem.graph_index import _HISTORY_TYPES, _neighbors, _reverse_neighbors
        for edge in _neighbors(anchor_id8, relation_types=_HISTORY_TYPES, limit=50):
            forward_ids.add(edge.get("dst_id", "")[:8])
        for edge in _reverse_neighbors(anchor_id8, relation_types=_HISTORY_TYPES, limit=50):
            reverse_ids.add(edge.get("src_id", "")[:8])
    except Exception as exc:
        log.debug("graph timeline expansion unavailable; using related[] only: %s", exc)

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

    anchor_item = _memory_to_item(anchor, include_snippet=True)
    before_items = [_memory_to_item(m, include_snippet=True, position=i) for i, m in enumerate(before)]
    after_items = [_memory_to_item(m, include_snippet=True, position=i) for i, m in enumerate(after)]
    all_items = [anchor_item, *before_items, *after_items]

    slice_data: dict = {
        "slice_id": _stable_id("timeline", (memory_id, scope_id, depth_before, depth_after)),
        "slice_kind": "timeline",
        "scope_id": scope_id,
        "generated_at": now_iso(),
        "ordering": "chronological",
        "items": all_items,
        "anchor_id": anchor_id8,
        "anchor_title": anchor.get("title", "Untitled"),
        "before_items": before_items,
        "after_items": after_items,
        "layer_summary": _layer_summary_from_items(all_items),
    }
    return _render_recall_markdown(slice_data)


def _format_memory_as_bullet(mem: dict) -> str:
    title = mem.get("title", "Untitled")
    body = mem.get("full_record") or mem.get("essence", "")

    line = f"- **{title}**"
    if body and body[:60] != title[:60]:
        line += f"\n  {body[:500]}"
    return line


def memory_recall(query: str, scope_id: str = "default", limit: int = 10, rerank_model: str | None = None) -> str:
    import time

    from memem.eval_capture import capture as _eval_capture
    _t0 = time.monotonic()
    memories = _search_memories(query, scope_id=scope_id, limit=limit, rerank_model=rerank_model)
    _eval_capture(
        query=query, mode="recall", scope_id=scope_id, limit=limit,
        memory_ids=[str(m.get("id") or "") for m in memories if m.get("id")],
        latency_ms=(time.monotonic() - _t0) * 1000.0,
    )
    transcript_results = transcript_search(query, limit=3)

    if not memories and ("No matching" in transcript_results or not transcript_results):
        return f"No memories found for: {query}"

    items = [_memory_to_item(m, include_snippet=True) for m in memories]
    layer_summary = _layer_summary_from_items(items)

    slice_data: dict = {
        "slice_id": _stable_id("recall", (query, scope_id, limit)),
        "slice_kind": "recall",
        "scope_id": scope_id,
        "query": query,
        "generated_at": now_iso(),
        "ordering": "relevance",
        "items": items,
        "linked": [],
        "layer_summary": layer_summary,
    }
    result = _render_recall_markdown(slice_data) if memories else ""

    sections = []
    if result:
        sections.append(result)
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
