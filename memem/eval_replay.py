"""Eval-replay: re-run captured queries against current code, report deltas.

Pairs with eval_capture.py. Reads an NDJSON baseline, re-issues each query,
and compares the new memory_id list to the captured list. Reports per-query
Jaccard@k overlap, top-1 stability, and latency Δ — the three numbers gbrain
uses to gate retrieval-touching changes.

Use:
    memem eval export --since 7d > baseline.ndjson
    # ... change ranking code ...
    memem eval replay --against baseline.ndjson
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from memem.eval_capture import load_captures
from memem.recall import _search_memories

REPLAY_KEY_LIMIT = 10  # K for Jaccard@k cap (most baselines won't exceed this)


def _re_run_query(row: dict[str, Any]) -> tuple[list[str], float]:
    """Re-run a captured query against current code, return (memory_ids, latency_ms).

    Dispatches by row['mode']:
    - 'search' / 'recall': use _search_memories (unified retrieve() delegate, v2.6)
    - 'tool_active_slice': use retrieve() directly (already the One Engine path)
    - unknown/missing: falls back to _search_memories

    UPDATED(v2.6): Added mode-based dispatch so replay comparisons route to the
    correct engine for each captured mode. 'tool_active_slice' rows bypass the
    obsidian_store._find_memory mapping overhead since active_slice only needs hit IDs.
    """
    query = str(row.get("query", "") or "")
    scope_id = str(row.get("scope_id", "default") or "default")
    limit = int(row.get("limit") or REPLAY_KEY_LIMIT)
    mode = str(row.get("mode") or "search")

    t0 = time.monotonic()
    if mode == "tool_active_slice":
        from memem.retrieve import retrieve
        _scope = scope_id if scope_id and scope_id != "default" else ""
        hits = retrieve(query, k=limit, scope_id=_scope, log_call_type=None, writeback=False)  # replay must not mutate telemetry
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        ids = [str(h.get("id") or "") for h in hits if h.get("id")]
    else:
        # 'search', 'recall', or any unknown mode → _search_memories (unified engine)
        memories = _search_memories(
            query, scope_id=scope_id, limit=limit, record_access=False, expand_links=False
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        ids = [str(m.get("id") or "") for m in memories if m.get("id")]
    return ids, elapsed_ms


def jaccard(a: list[str], b: list[str], *, k: int | None = None) -> float:
    """Jaccard similarity between two lists, optionally truncated to top-k."""
    aset = set(a[:k] if k else a)
    bset = set(b[:k] if k else b)
    if not aset and not bset:
        return 1.0  # both empty = trivially identical
    union = aset | bset
    if not union:
        return 1.0
    return len(aset & bset) / len(union)


def top_1_stable(a: list[str], b: list[str]) -> bool:
    """True iff both lists have a #1 result and they match."""
    if not a or not b:
        return False
    return a[0] == b[0]


def replay(
    baseline_path: Path,
    *,
    k: int = 5,
    worst_n: int = 5,
) -> dict[str, Any]:
    """Re-run every query in `baseline_path`; return aggregate stats + worst regressions.

    Returns:
        {
            "n": <int>,                  # number of replayed queries
            "mean_jaccard_at_k": <float>,
            "top_1_stability": <float>,  # fraction
            "mean_latency_delta_ms": <float>,  # current - baseline (ms)
            "worst_regressions": [
                {"query": str, "jaccard": float, "baseline_ids": [...], "current_ids": [...]},
                ...
            ],
            "k": k,
        }
    """
    rows = load_captures(path=baseline_path)
    if not rows:
        return {"n": 0, "k": k, "mean_jaccard_at_k": None,
                "top_1_stability": None, "mean_latency_delta_ms": None,
                "worst_regressions": []}

    jaccards: list[float] = []
    top1_hits = 0
    latency_deltas: list[float] = []
    per_query: list[dict[str, Any]] = []

    for row in rows:
        baseline_ids = [str(x) for x in row.get("memory_ids", []) if x]
        baseline_latency = row.get("latency_ms")
        try:
            current_ids, current_latency = _re_run_query(row)
        except Exception as exc:  # noqa: BLE001
            per_query.append({
                "query": row.get("query", ""),
                "error": str(exc)[:120],
                "jaccard": 0.0,
                "baseline_ids": baseline_ids,
                "current_ids": [],
            })
            jaccards.append(0.0)
            continue
        j = jaccard(baseline_ids, current_ids, k=k)
        jaccards.append(j)
        if top_1_stable(baseline_ids, current_ids):
            top1_hits += 1
        if isinstance(baseline_latency, int | float):
            latency_deltas.append(current_latency - float(baseline_latency))
        per_query.append({
            "query": row.get("query", ""),
            "jaccard": round(j, 3),
            "baseline_ids": baseline_ids[:k],
            "current_ids": current_ids[:k],
            "baseline_latency_ms": baseline_latency,
            "current_latency_ms": round(current_latency, 2),
        })

    # Worst-N regressions: lowest jaccard
    worst = sorted(per_query, key=lambda r: r.get("jaccard", 0.0))[:worst_n]

    return {
        "n": len(rows),
        "k": k,
        "mean_jaccard_at_k": round(sum(jaccards) / len(jaccards), 3) if jaccards else None,
        "top_1_stability": round(top1_hits / len(rows), 3) if rows else None,
        "mean_latency_delta_ms": (
            round(sum(latency_deltas) / len(latency_deltas), 2)
            if latency_deltas else None
        ),
        "worst_regressions": worst,
    }


def format_replay_report(result: dict[str, Any]) -> str:
    """Render the replay result dict as human-readable text."""
    lines = []
    n = result.get("n", 0)
    k = result.get("k", 5)
    if n == 0:
        return "No baseline records to replay."
    lines.append(f"eval replay  ({n} queries, k={k})")
    lines.append("-" * 50)
    mj = result.get("mean_jaccard_at_k")
    t1 = result.get("top_1_stability")
    ld = result.get("mean_latency_delta_ms")
    lines.append(f"  mean jaccard@{k}:    {mj:.3f}" if mj is not None else "  mean jaccard@k:    n/a")
    if t1 is not None:
        lines.append(f"  top-1 stability:   {t1:.1%}")
    if ld is not None:
        sign = "+" if ld >= 0 else ""
        lines.append(f"  mean latency Δ:    {sign}{ld:.1f}ms")
    worst = result.get("worst_regressions") or []
    if worst:
        lines.append("")
        lines.append(f"  worst {len(worst)} regressions (lowest jaccard):")
        for r in worst:
            q = (r.get("query") or "")[:60]
            j = r.get("jaccard", 0.0)
            lines.append(f"    j={j:.2f}  {q!r}")
            err = r.get("error")
            if err:
                lines.append(f"           error: {err}")
    return "\n".join(lines)
