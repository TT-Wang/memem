"""Slice quality baseline probe.

Runs 20 synthesized queries against generate_active_memory_slice and captures
4 quality metrics:
  (a) cross_project_hit_pct — % activated memories with project != 'memem'
  (b) duplicate_rejection_count — candidates deduped before activation
  (c) cosmetic_tension_count — tensions with the cosmetic cluster description
  (d) avg_activation_count — mean total entries per query

Usage (run from the memem repo root):
    PYTHONPATH=. python3 scripts/slice_quality_probe.py \
        --output .forge/state/memem-vault-cleanup-baseline.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

COSMETIC_TENSION_DESC = "Multiple strong candidate clusters suggest an unresolved tradeoff."

PROBE_QUERIES = [
    "how does memem mining extract memories from sessions",
    "memem retrieval pipeline recall and search",
    "active memory slice rendering and prompt injection",
    "memem schema fields and frontmatter structure",
    "hooks and MCP server integration",
    "deduplication and duplicate rejection during writeback",
    "activation scoring and candidate selection",
    "security scanning memory content validation",
    "memem writeback policy and delta commit",
    "audit log and telemetry events",
    "embedding index build and similarity search",
    "FTS full text search index rebuild",
    "related link graph construction and traversal",
    "vault file permissions and ownership",
    "Obsidian integration wiki links and graph view",
    "marketplace plugin installation and bootstrap",
    "miner daemon session JSONL processing",
    "layer tier classification L0 L1 L2 L3",
    "project scope isolation cross-project leakage",
    "confidence threshold and quality filtering for saved memories",
]

SCOPE_ID = "memem"


def _count_dedup_rejections(query: str) -> int:
    """Count candidates rejected by _dedupe_candidates during generate_candidates."""
    from memem.active_slice import normalize_memory_candidate
    from memem.active_slice_engine import _MAX_GRAPH_CANDIDATES, _MAX_MEMORY_CANDIDATES, _graph_candidates
    from memem.models import _normalize_scope_id
    from memem.recall import _search_memories

    normalized_scope = _normalize_scope_id(SCOPE_ID)

    # Reproduce the memory + graph candidate fetch from generate_candidates
    try:
        memories = _search_memories(
            query,
            scope_id=normalized_scope,
            limit=_MAX_MEMORY_CANDIDATES,
            record_access=False,
            expand_links=False,
        )
        memory_candidates = [
            normalize_memory_candidate(mem, source_reason="recall", score=0.75 - (idx * 0.02))
            for idx, mem in enumerate(memories[:_MAX_MEMORY_CANDIDATES])
        ]
    except Exception:
        memory_candidates = []

    graph_candidates = _graph_candidates(memory_candidates)

    raw_pool = (memory_candidates + graph_candidates)[: _MAX_MEMORY_CANDIDATES + _MAX_GRAPH_CANDIDATES]
    raw_count = len(raw_pool)

    # Apply the same dedup logic as generate_candidates
    seen: set[str] = set()
    deduped: list[Any] = []
    for candidate in raw_pool:
        key = candidate.get("memory_id") or candidate.get("artifact_id") or candidate.get("candidate_id", "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    return raw_count - len(deduped)


def _compute_query_metrics(query: str, slice_obj: dict[str, Any], dedup_rejections: int) -> dict[str, Any]:
    """Compute per-query metrics from a generated slice."""
    # Collect all activated memory items (excludes artifacts)
    memory_sections = ["goals", "constraints", "active_background", "decisions", "preferences", "failure_patterns"]
    activated_memory_items = []
    for section in memory_sections:
        activated_memory_items.extend(slice_obj.get(section, []))

    # (a) cross_project_hit_pct — count items where normalized project != normalized scope.
    # Normalize both sides so renamed/aliased projects (memem ↔ cortex-plugin) are
    # treated as same-project. Only count items from memories (have a memory_id).
    from memem.models import _normalize_scope_id
    normalized_scope = _normalize_scope_id(SCOPE_ID)
    memory_items_with_project = [
        item for item in activated_memory_items
        if item.get("memory_id")  # skip current_query synthetic entries
    ]
    cross_project_count = sum(
        1 for item in memory_items_with_project
        if _normalize_scope_id(str(item.get("project", "") or "")) != normalized_scope
    )
    total_memory_items = len(memory_items_with_project)
    cross_project_pct = (cross_project_count / total_memory_items * 100.0) if total_memory_items > 0 else 0.0

    # (c) cosmetic_tension_count — tensions with the exact cosmetic description
    open_tensions = slice_obj.get("open_tensions", [])
    cosmetic_tensions = sum(
        1 for t in open_tensions
        if t.get("description", "") == COSMETIC_TENSION_DESC
    )

    # (d) activation_count — total entries across all sections including artifact_context
    artifact_context_items = slice_obj.get("artifacts", [])
    total_activation_count = (
        sum(len(slice_obj.get(sec, [])) for sec in memory_sections)
        + len(artifact_context_items)
    )

    return {
        "cross_project_count": cross_project_count,
        "total_memory_items": total_memory_items,
        "cross_project_pct": round(cross_project_pct, 2),
        "duplicate_rejection_count": dedup_rejections,
        "cosmetic_tension_count": cosmetic_tensions,
        "activation_count": total_activation_count,
    }


def run_probe(output_path: str) -> None:
    from memem.active_slice_engine import generate_active_memory_slice

    query_results = []
    total_cross_project_count = 0
    total_memory_items_count = 0
    total_dedup_rejections = 0
    total_cosmetic_tensions = 0
    total_activation_count = 0

    print(f"Running {len(PROBE_QUERIES)} probe queries against scope_id='{SCOPE_ID}'...", flush=True)
    print(flush=True)

    for idx, query in enumerate(PROBE_QUERIES, start=1):
        print(f"  [{idx:02d}/{len(PROBE_QUERIES)}] {query[:60]}...", flush=True)

        # Count dedup rejections BEFORE generating slice (same recall path)
        dedup_rejections = _count_dedup_rejections(query)

        slice_obj = generate_active_memory_slice(
            query,
            scope_id=SCOPE_ID,
            environment={},
            use_llm=False,
        )

        metrics = _compute_query_metrics(query, slice_obj, dedup_rejections)

        total_cross_project_count += metrics["cross_project_count"]
        total_memory_items_count += metrics["total_memory_items"]
        total_dedup_rejections += metrics["duplicate_rejection_count"]
        total_cosmetic_tensions += metrics["cosmetic_tension_count"]
        total_activation_count += metrics["activation_count"]

        query_results.append({
            "query": query,
            "metrics": {
                "cross_project_count": metrics["cross_project_count"],
                "total_memory_items": metrics["total_memory_items"],
                "cross_project_pct": metrics["cross_project_pct"],
                "duplicate_rejection_count": metrics["duplicate_rejection_count"],
                "cosmetic_tension_count": metrics["cosmetic_tension_count"],
                "activation_count": metrics["activation_count"],
            },
        })

    n_queries = len(PROBE_QUERIES)
    overall_cross_project_pct = (
        (total_cross_project_count / total_memory_items_count * 100.0)
        if total_memory_items_count > 0
        else 0.0
    )
    avg_activation_count = total_activation_count / n_queries if n_queries > 0 else 0.0

    summary = {
        "cross_project_hit_pct": round(overall_cross_project_pct, 2),
        "duplicate_rejection_count": total_dedup_rejections,
        "cosmetic_tension_count": total_cosmetic_tensions,
        "avg_activation_count": round(avg_activation_count, 2),
    }

    timestamp = datetime.now(UTC).isoformat()

    output = {
        "queries": query_results,
        "summary": summary,
        "timestamp": timestamp,
    }

    import os
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(flush=True)
    print("=" * 60, flush=True)
    print("BASELINE SUMMARY", flush=True)
    print("=" * 60, flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    print(f"\nFull results written to: {output_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Slice quality baseline probe")
    parser.add_argument(
        "--output",
        default="/home/claude-user/cortex-plugin/.forge/state/memem-vault-cleanup-baseline.json",
        help="Output JSON file path",
    )
    args = parser.parse_args()
    run_probe(args.output)


if __name__ == "__main__":
    main()
