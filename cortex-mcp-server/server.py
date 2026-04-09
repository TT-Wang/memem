#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory and context assembly for Claude Code.

Stores memories as JSON files in ~/.cortex/memories/
No database required — works out of the box.

Tools:
  - context_assemble: Assemble relevant context for a goal
  - memory_save: Save a piece of knowledge
  - memory_recall: Search memory by keyword
  - memory_list: List all memories in a scope
  - memory_feedback: Report whether recalled memories helped
  - memory_stats: Show memory system statistics
  - memory_promote: Promote a candidate memory to learned
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ─── Storage ──────────────────────────────────────────────────────

CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
MEMORIES_DIR = CORTEX_DIR / "memories"
LOGS_DIR = CORTEX_DIR / "logs"

MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _load_memory(memory_id: str) -> dict | None:
    path = MEMORIES_DIR / f"{memory_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def _save_memory(mem: dict):
    path = MEMORIES_DIR / f"{mem['id']}.json"
    path.write_text(json.dumps(mem, indent=2, default=str))


def _delete_memory(memory_id: str) -> bool:
    path = MEMORIES_DIR / f"{memory_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _all_memories(scope_id: str | None = None) -> list[dict]:
    memories = []
    for f in MEMORIES_DIR.glob("*.json"):
        try:
            mem = json.loads(f.read_text())
            if scope_id and mem.get("scope_id") != scope_id:
                continue
            memories.append(mem)
        except (json.JSONDecodeError, OSError):
            continue
    return memories


def _search_memories(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """Search memories by keyword overlap. Returns sorted by relevance."""
    query_words = set(query.lower().split())
    if not query_words:
        return []

    scored = []
    for mem in _all_memories(scope_id):
        # Search in essence, title, and tags
        text = " ".join([
            mem.get("essence", ""),
            mem.get("title", ""),
            " ".join(mem.get("domain_tags", [])),
        ]).lower()

        text_words = set(text.split())
        overlap = len(query_words & text_words)
        if overlap > 0:
            # Score: overlap * confidence * (learned boost)
            confidence = float(mem.get("confidence", 0.5))
            learned_boost = 1.5 if mem.get("promotion_status") == "learned" else 1.0
            score = (overlap / len(query_words)) * confidence * learned_boost
            scored.append((score, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def _format_memory_for_context(mem: dict) -> str:
    """Format a memory as a tagged context block."""
    title = mem.get("title", "Untitled")
    mem_type = mem.get("memory_type", "unknown")
    confidence = float(mem.get("confidence", 0.5))
    impact = float(mem.get("impact_score", 0.0))
    essence = mem.get("essence", "")

    content = f"## {title}\n"
    content += f"*Type: {mem_type} | Confidence: {confidence:.1f} | Impact: {impact:.2f}*\n\n"
    content += essence

    tags = mem.get("domain_tags", [])
    if tags:
        content += f"\n\nTags: {', '.join(tags)}"

    mem_id = mem.get("id", "unknown")
    return f'<context source="memory" relevance="{confidence:.2f}" memory_id="{mem_id}">\n{content}\n</context>'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── MCP Server ──────────────────────────────────────────────────

mcp = FastMCP("cortex")


@mcp.tool()
def context_assemble(goal: str, scope_id: str = "default", limit: int = 10) -> str:
    """Assemble relevant context for a goal by searching memory.
    Call this before starting any task to get relevant knowledge.
    Returns formatted context blocks from memory."""

    memories = _search_memories(goal, scope_id=scope_id, limit=limit)

    if not memories:
        return "No relevant memories found. This is a fresh topic."

    # Format each memory as context block
    blocks = []
    memory_ids = []
    for mem in memories:
        blocks.append(_format_memory_for_context(mem))
        memory_ids.append(mem["id"])

        # Update retrieval count
        mem["retrieval_count"] = mem.get("retrieval_count", 0) + 1
        mem["last_retrieved_at"] = _now()
        _save_memory(mem)

    # Log retrieval for association tracking
    log_entry = {
        "id": str(uuid.uuid4()),
        "goal": goal,
        "memory_ids": memory_ids,
        "scope_id": scope_id,
        "timestamp": _now(),
    }
    log_path = LOGS_DIR / f"retrieval-{log_entry['id']}.json"
    log_path.write_text(json.dumps(log_entry, indent=2))

    context = "\n\n".join(blocks)
    return f"Found {len(memories)} relevant memories:\n\n{context}"


@mcp.tool()
def memory_save(
    content: str,
    title: str = "",
    memory_type: str = "lesson",
    scope_id: str = "default",
    tags: str = "",
) -> str:
    """Save a piece of knowledge to memory.
    Use after discovering patterns, conventions, lessons, or failures.
    Tags should be comma-separated."""

    memory_id = str(uuid.uuid4())
    domain_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    mem = {
        "id": memory_id,
        "essence": content,
        "full_record": content,
        "title": title or content[:60],
        "memory_type": memory_type,
        "scope_type": "project",
        "scope_id": scope_id,
        "confidence": 0.5,
        "impact_score": 0.0,
        "success_count": 0,
        "failure_count": 0,
        "retrieval_count": 0,
        "promotion_status": "candidate",
        "verified": False,
        "human_approved": False,
        "domain_tags": domain_tags,
        "associations": [],
        "contradicts": [],
        "source_type": "user",
        "created_at": _now(),
        "last_retrieved_at": None,
        "last_validated_at": None,
    }

    _save_memory(mem)
    return f"Memory saved: {memory_id[:8]}... \"{title or content[:60]}\""


@mcp.tool()
def memory_recall(query: str, scope_id: str = "default", limit: int = 10) -> str:
    """Search memory by keyword. Returns matching memories with their content."""

    memories = _search_memories(query, scope_id=scope_id, limit=limit)

    if not memories:
        return f"No memories found for: {query}"

    lines = [f"Found {len(memories)} memories:\n"]
    for mem in memories:
        status = mem.get("promotion_status", "candidate")
        confidence = float(mem.get("confidence", 0.5))
        title = mem.get("title", "Untitled")
        essence = mem.get("essence", "")
        mem_id = mem.get("id", "")[:8]
        lines.append(f"**[{mem_id}] {title}** ({status}, conf: {confidence:.1f})")
        lines.append(f"  {essence[:200]}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def memory_list(scope_id: str = "default") -> str:
    """List all memories in a scope."""

    memories = _all_memories(scope_id)
    if not memories:
        return f"No memories in scope: {scope_id}"

    # Sort by confidence descending
    memories.sort(key=lambda m: float(m.get("confidence", 0)), reverse=True)

    lines = [f"Memories in scope '{scope_id}': {len(memories)}\n"]
    for mem in memories:
        status = mem.get("promotion_status", "candidate")
        confidence = float(mem.get("confidence", 0.5))
        impact = float(mem.get("impact_score", 0.0))
        title = mem.get("title", "Untitled")
        mem_id = mem.get("id", "")[:8]
        retrievals = mem.get("retrieval_count", 0)
        lines.append(f"- [{mem_id}] **{title}** | {status} | conf:{confidence:.1f} | impact:{impact:.2f} | retrieved:{retrievals}x")

    return "\n".join(lines)


@mcp.tool()
def memory_feedback(memory_ids: str, approved: bool) -> str:
    """Report whether recalled memories were useful.
    memory_ids: comma-separated memory IDs (first 8 chars or full).
    approved: true if the output was good, false if bad."""

    ids = [mid.strip() for mid in memory_ids.split(",") if mid.strip()]
    updated = 0

    # Find memories by prefix match
    all_mems = _all_memories()
    for target_id in ids:
        for mem in all_mems:
            if mem["id"].startswith(target_id):
                if approved:
                    mem["confidence"] = min(1.0, float(mem.get("confidence", 0.5)) + 0.05)
                    mem["success_count"] = mem.get("success_count", 0) + 1
                else:
                    mem["confidence"] = max(0.0, float(mem.get("confidence", 0.5)) - 0.1)
                    mem["failure_count"] = mem.get("failure_count", 0) + 1

                # Recalculate impact
                s = mem.get("success_count", 0)
                f = mem.get("failure_count", 0)
                mem["impact_score"] = s / max(1, s + f)
                mem["last_validated_at"] = _now()

                # Auto-promote check
                if (mem.get("promotion_status") == "candidate"
                        and mem.get("retrieval_count", 0) >= 5
                        and mem.get("impact_score", 0) > 0.7):
                    mem["promotion_status"] = "learned"
                    mem["verified"] = True

                _save_memory(mem)
                updated += 1
                break

    return f"Updated {updated} memories (approved={approved})"


@mcp.tool()
def memory_stats(scope_id: str = "default") -> str:
    """Show memory system statistics."""

    memories = _all_memories(scope_id)
    total = len(memories)
    learned = sum(1 for m in memories if m.get("promotion_status") == "learned")
    candidates = sum(1 for m in memories if m.get("promotion_status") == "candidate")

    confidences = [float(m.get("confidence", 0.5)) for m in memories]
    avg_conf = sum(confidences) / max(len(confidences), 1)

    impacts = [float(m.get("impact_score", 0.0)) for m in memories]
    avg_impact = sum(impacts) / max(len(impacts), 1)

    total_retrievals = sum(m.get("retrieval_count", 0) for m in memories)

    # Domain coverage
    domain_counts: dict[str, int] = {}
    for mem in memories:
        for tag in mem.get("domain_tags", []):
            domain_counts[tag] = domain_counts.get(tag, 0) + 1

    lines = [
        f"**Memory Stats** (scope: {scope_id})\n",
        f"- Total: {total}",
        f"- Learned: {learned}",
        f"- Candidates: {candidates}",
        f"- Avg confidence: {avg_conf:.2f}",
        f"- Avg impact: {avg_impact:.2f}",
        f"- Total retrievals: {total_retrievals}",
    ]

    if domain_counts:
        lines.append(f"\n**Domains:** {', '.join(f'{k}({v})' for k, v in sorted(domain_counts.items(), key=lambda x: -x[1]))}")

    return "\n".join(lines)


@mcp.tool()
def memory_promote(memory_id: str) -> str:
    """Promote a candidate memory to learned status.
    memory_id: first 8 chars or full ID."""

    for mem in _all_memories():
        if mem["id"].startswith(memory_id):
            if mem.get("promotion_status") == "learned":
                return f"Memory {memory_id} is already learned."
            mem["promotion_status"] = "learned"
            mem["human_approved"] = True
            _save_memory(mem)
            return f"Memory promoted to learned: {mem.get('title', memory_id)}"

    return f"Memory not found: {memory_id}"


# ─── Run ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
