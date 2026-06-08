"""memem v2.0.0 renderer — 2 sections: Working + Relevant.

Replaces ~600 LOC of legacy/v1.13 multi-section renderers
(active_slice._render_slice + render_slice_v2). One simple function,
no schema routing, no kind buckets, no env-var legacy flag.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def render_slice(
    query: str,
    results: Iterable[Mapping],
    working_state: Mapping | None = None,
) -> str:
    """Render an active memory slice.

    Args:
        query: the current user query
        results: list of MemoryHit dicts from memem.retrieve.retrieve()
                 each has: id, path, title, project, created, body, score, source
        working_state: optional dict with key 'recent_actions' (list of str)
                       and 'task_mode' (str). Both optional.

    Returns:
        Markdown string with:
          # Active Memory Slice
          - query: ...

          ## Working (only if working_state has content)
          - query: ...
          - action: ...
          - task_mode: ...

          ## Relevant (N by cosine)
          1. [YYYY-MM-DD] [project] **title**
             body[:160]
          ...
    """
    results_list = list(results)
    working = working_state or {}

    lines = ["# Active Memory Slice", f"- query: {query}", ""]

    # Working section (only if there's content)
    recent_actions = list(working.get("recent_actions", []))[:3]
    task_mode = working.get("task_mode")
    if recent_actions or task_mode:
        lines.append("## Working")
        lines.append(f"- query: {query}")
        if task_mode:
            lines.append(f"- task_mode: {task_mode}")
        for action in recent_actions:
            lines.append(f"- action: {str(action)[:120]}")
        lines.append("")

    # Relevant section
    lines.append(f"## Relevant ({len(results_list)} by cosine)")
    if not results_list:
        lines.append("(no matches)")
    for i, hit in enumerate(results_list, 1):
        date = (hit.get("created") or "")[:10] or "----"
        project = (hit.get("project") or "?")[:20]
        title = (hit.get("title") or "(untitled)")[:80]
        body = (hit.get("body") or "").replace("\n", " ")[:160]
        source = hit.get("source", "")
        source_tag = f" [{source}]" if source == "fts" else ""
        lines.append(f"{i}. [{date}] [{project}]{source_tag} **{title}**")
        if body:
            lines.append(f"   {body}")

    return "\n".join(lines)
