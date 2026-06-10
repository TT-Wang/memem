"""Explicit assembly projection.

context_assemble is the secondary path for building a structured context
briefing. After m4, it calls the active slice engine 1-2 times, merges the
resulting slices into a composite "assembled" MemorySlice, and renders it
via render_slice_markdown.
"""

import logging
from typing import Any

from memem.models import _normalize_scope_id, now_iso

log = logging.getLogger("memem-assembly")

# Threshold: if the primary slice has fewer items than this, augment with general scope.
_SPARSE_THRESHOLD = 5


def _merge_slices(sub_slices: "list[dict]", query: str, project: str) -> "dict":
    """Fold N item lists into one composite slice dict."""
    from memem.recall import _layer_summary_from_items, _stable_id

    seen_ids: set[str] = set()
    merged_items: list[dict] = []

    for sub in sub_slices:
        for item in sub.get("items", []):
            item_id = item.get("id", "")
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            merged_items.append(item)

    layer_summary = _layer_summary_from_items(merged_items)

    n_subs = len(sub_slices)
    strategy = "primary-only" if n_subs == 1 else "primary+general-augmentation"

    return {
        "slice_id": _stable_id("assembled", {"query": query, "project": project, "generated_at": now_iso()}),
        "scope_id": project,
        "query": query,
        "generated_at": now_iso(),
        "slice_kind": "assembled",
        "items": merged_items,
        "layer_summary": layer_summary,
        "sub_slices": sub_slices,
        "composition_strategy": strategy,
    }


def context_assemble(query: str, project: str = "default") -> str:
    """Assemble a composite briefing using the v2.0.0 recall pipeline.

    Calls memory_search for the active project scope, and optionally augments
    with general scope results when the primary result is sparse. Renders via
    the inline recall markdown renderer.
    """
    from memem.recall import (
        _memory_to_item,
        _render_recall_markdown,
        _search_memories,
        _stable_id,
    )

    normalized = _normalize_scope_id(project)

    # Primary slice: search active project scope
    primary_mems = _search_memories(
        query, scope_id=normalized, limit=10, record_access=False, expand_links=False
    )
    primary_items = [_memory_to_item(m, include_snippet=True) for m in primary_mems]
    primary_as_sub: dict = {
        "scope_id": normalized,
        "slice_id": _stable_id("assembled-primary", {"query": query, "project": normalized}),
        "items": primary_items,
    }

    sub_slices = [primary_as_sub]

    # Cross-project augmentation when primary is sparse
    if len(primary_items) < _SPARSE_THRESHOLD and normalized != "general":
        general_mems = _search_memories(
            query, scope_id="general", limit=10, record_access=False, expand_links=False
        )
        general_items = [_memory_to_item(m, include_snippet=True) for m in general_mems]
        if general_items:
            sub_slices.append({
                "scope_id": "general",
                "slice_id": _stable_id("assembled-general", {"query": query}),
                "items": general_items,
            })

    composite = _merge_slices(sub_slices, query=query, project=normalized)

    # Early return if nothing was assembled
    if not composite.get("items"):
        return ""

    composite["slice_kind"] = "search"
    composite["query"] = query
    return _render_recall_markdown(composite)
