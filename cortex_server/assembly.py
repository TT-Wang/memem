"""Context assembly and memory consolidation.

context_assemble uses Haiku to produce a query-tailored briefing from
playbooks, memories, and transcript search results.

_consolidate_project is the 'dreaming' pass that merges redundant memories
and deprecates obsolete ones.
"""

import json
import logging
import subprocess

from cortex_server.models import PLAYBOOK_DIR, _normalize_scope_id
from cortex_server.obsidian_store import (
    _deprecate_memory,
    _find_memory,
    _obsidian_memories,
    _update_memory,
)

log = logging.getLogger("cortex-assembly")


_ASSEMBLE_SYSTEM = (
    "You are a context assembly engine. Given a user's query and raw knowledge "
    "materials, produce a comprehensive briefing that contains everything an AI "
    "assistant would need to answer the query well. Include all relevant facts, "
    "decisions, conventions, and context. Exclude anything unrelated to the query. "
    "Format as clean markdown. Be thorough — include everything relevant, but "
    "nothing that isn't. Output ONLY the briefing, no meta-commentary."
)


def context_assemble(query: str, project: str = "default") -> str:
    """Assemble a query-tailored context briefing from all available knowledge.

    Gathers playbook, relevant memories, and session history, then uses Haiku
    to produce a focused briefing for the given query.
    """
    normalized = _normalize_scope_id(project)

    # Load playbook if exists
    playbook_content = ""
    playbook_path = PLAYBOOK_DIR / f"{normalized}.md"
    if playbook_path.exists():
        try:
            content = playbook_path.read_text().strip()
            # Strip hash comment at end
            lines = content.split("\n")
            if lines and (lines[-1].strip().startswith("<!-- cortex-hash:") or lines[-1].strip().startswith("<!-- refined:")):
                content = "\n".join(lines[:-1]).strip()
            playbook_content = content
        except OSError:
            pass

    # Get relevant memories (lazy import to avoid circular dep)
    from cortex_server.recall import _search_memories
    memories = _search_memories(query, scope_id=normalized, limit=20, record_access=False)

    # Get transcript search results (lazy import)
    from cortex_server.transcripts import transcript_search
    transcript_results = transcript_search(query, limit=3)

    # Early return if nothing to assemble
    if not playbook_content and not memories and ("No matching" in transcript_results or not transcript_results):
        return ""

    # Format materials
    parts = []
    if playbook_content:
        parts.append(f"PLAYBOOK:\n{playbook_content}")

    if memories:
        mem_lines = []
        for mem in memories:
            title = mem.get("title", "Untitled")
            essence = mem.get("essence", mem.get("full_record", ""))
            mem_lines.append(f"## {title}\n{essence}")
        parts.append("RELEVANT MEMORIES:\n" + "\n\n".join(mem_lines))

    if transcript_results and "No matching" not in transcript_results:
        parts.append(f"RELATED SESSIONS:\n{transcript_results}")

    materials = "\n\n".join(parts)
    # Cap at 50K chars
    if len(materials) > 50000:
        materials = materials[:50000]

    prompt = f"QUERY: {query}\n\n{materials}"

    # Haiku assembles the brief
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _ASSEMBLE_SYSTEM],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        return playbook_content or ""

    if result.returncode != 0 or not result.stdout.strip():
        return playbook_content or ""

    return result.stdout.strip()


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
            from cortex_server.mining import _merge_memories
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
