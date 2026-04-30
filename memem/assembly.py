"""Explicit assembly projection and memory consolidation.

context_assemble is the secondary path for building a structured context
briefing. After m4, it calls the active slice engine 1-2 times, merges the
resulting slices into a composite "assembled" MemorySlice, and renders it
via render_slice_markdown.

_consolidate_project is the 'dreaming' pass that merges redundant memories
and deprecates obsolete ones.
"""

import json
import logging
import subprocess
from collections.abc import Mapping
from typing import Any

from memem.models import DEFAULT_LAYER, _normalize_scope_id, now_iso
from memem.obsidian_store import (
    _deprecate_memory,
    _find_memory,
    _obsidian_memories,
    _update_memory,
)

log = logging.getLogger("memem-assembly")

# Threshold: if the primary slice has fewer items than this, augment with general scope.
_SPARSE_THRESHOLD = 5


def _active_items_to_memory_items(slice_obj: Mapping[str, Any]) -> "list[dict]":
    """Convert ActiveMemoryItem entries from an ActiveMemorySlice into MemoryItem dicts.

    ActiveMemoryItem has: memory_id, role, title, summary, layer, score, etc.
    MemoryItem needs:     id, title, content, layer, score, snippet, source_type, project.
    """
    seen: set[str] = set()
    items: list[dict] = []

    role_sections = ["goals", "constraints", "active_background", "decisions", "preferences", "failure_patterns"]
    for section_key in role_sections:
        for entry in slice_obj.get(section_key, []):
            mem_id = entry.get("memory_id", "")
            if not mem_id:
                continue
            if mem_id in seen:
                continue
            seen.add(mem_id)
            summary = entry.get("summary", "") or ""
            items.append({
                "id": mem_id,
                "title": entry.get("title", "Untitled"),
                "content": summary,
                "snippet": summary[:80],
                "layer": entry.get("layer", DEFAULT_LAYER),
                "score": entry.get("score", 0.5),
                "source_type": entry.get("source_type", "user"),
                "project": entry.get("project", "general"),
            })
    return items


def _merge_slices(sub_slices: "list[dict]", query: str, project: str) -> "dict":
    """Fold N ActiveMemorySlice-sourced item lists into one composite MemorySlice."""
    from memem.active_slice import _layer_summary_from_items, _stable_id

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

    layer_summary = _layer_summary_from_items(merged_items)  # type: ignore[arg-type]

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
    """Assemble a composite briefing by composing active slice projections.

    This is the secondary projection path. It calls the active slice engine
    once for the active project scope, and optionally a second time for the
    "general" scope when the primary result is sparse. The resulting slices
    are merged into a single composite "assembled" MemorySlice and rendered
    via render_slice_markdown.
    """
    from memem.active_slice import render_slice_markdown
    from memem.active_slice_engine import build_slice

    normalized = _normalize_scope_id(project)

    # Primary slice: active project scope
    primary_slice = build_slice(query, scope_id=normalized, use_llm=False)
    primary_items = _active_items_to_memory_items(primary_slice)
    primary_as_sub: dict = {
        "scope_id": normalized,
        "slice_id": primary_slice.get("slice_id", ""),
        "items": primary_items,
    }

    sub_slices = [primary_as_sub]

    # Cross-project augmentation when primary is sparse
    if len(primary_items) < _SPARSE_THRESHOLD and normalized != "general":
        general_slice = build_slice(query, scope_id="general", use_llm=False)
        general_items = _active_items_to_memory_items(general_slice)
        if general_items:
            sub_slices.append({
                "scope_id": "general",
                "slice_id": general_slice.get("slice_id", ""),
                "items": general_items,
            })

    composite = _merge_slices(sub_slices, query=query, project=normalized)

    # Early return if nothing was assembled
    if not composite.get("items"):
        return ""

    return render_slice_markdown(composite)  # type: ignore[arg-type]


_CONSOLIDATION_SYSTEM = (
    "You are a memory consolidation engine. Review these memory entries for a project "
    "and identify cleanup actions. Output a JSON object with:\n"
    '- "merge": array of [keep_id, remove_id] pairs for redundant memories that say the same thing. '
    "The first ID is kept (merged into), the second is deleted.\n"
    '- "delete": array of IDs for memories that are obsolete, superseded by other memories in the set, '
    "or no longer accurate.\n\n"
    "Be conservative — only flag clear redundancies and obvious obsolescence. "
    "When in doubt, keep the memory.\n"
    "Output ONLY the JSON object, no other text."
)


def _consolidate_project(project: str) -> dict:
    """Consolidate memories for a project — merge redundant, deprecate obsolete."""
    memories = _obsidian_memories(project)
    if len(memories) < 5:
        return {"merged": 0, "deleted": 0}

    # Build summary for Haiku
    lines = []
    total_chars = 0
    for mem in memories:
        line = f"[{mem['id'][:8]}] {mem.get('title', '')}: {mem.get('essence', '')[:200]}"
        if total_chars + len(line) > 50000:
            break
        lines.append(line)
        total_chars += len(line)

    prompt = "Review these memory entries and identify cleanup actions:\n\n" + "\n".join(lines)

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _CONSOLIDATION_SYSTEM],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        log.warning("Consolidation failed for %s: %s", project, exc)
        return {"merged": 0, "deleted": 0}

    if result.returncode != 0:
        log.warning("Consolidation Haiku error for %s: %s", project, result.stderr.strip()[:200])
        return {"merged": 0, "deleted": 0}

    output = result.stdout.strip()
    if not output:
        return {"merged": 0, "deleted": 0}

    # Parse JSON — find { } with bracket depth
    json_start = output.find("{")
    if json_start == -1:
        return {"merged": 0, "deleted": 0}
    depth = 0
    json_end = -1
    for i in range(json_start, len(output)):
        if output[i] == "{": depth += 1
        elif output[i] == "}":
            depth -= 1
            if depth == 0:
                json_end = i
                break
    if json_end == -1:
        return {"merged": 0, "deleted": 0}

    try:
        actions = json.loads(output[json_start:json_end + 1])
    except json.JSONDecodeError:
        return {"merged": 0, "deleted": 0}

    merged_count = 0
    deleted_count = 0

    # Execute merges
    for pair in actions.get("merge", []):
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        keep_id, remove_id = pair[0], pair[1]
        keep_mem = _find_memory(keep_id)
        remove_mem = _find_memory(remove_id)
        if not keep_mem or not remove_mem:
            continue
        try:
            # Lazy import to avoid circular dependency (mining imports from storage)
            from memem.mining import _merge_memories
            merged_content = _merge_memories(keep_mem.get("essence", ""), remove_mem.get("essence", ""))
            _update_memory(keep_mem["id"], merged_content)
            _deprecate_memory(remove_mem["id"], f"merged_into:{keep_id}")
            merged_count += 1
        except Exception as exc:
            log.warning("Consolidation merge failed: %s", exc)

    # Execute deletes
    for mem_id in actions.get("delete", []):
        if not isinstance(mem_id, str):
            continue
        if _find_memory(mem_id):
            _deprecate_memory(mem_id, "consolidated")
            deleted_count += 1

    return {"merged": merged_count, "deleted": deleted_count}
