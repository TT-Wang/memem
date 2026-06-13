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


def run_dual_engine_replay(
    baseline_path: Path,
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Run each query in `baseline_path` twice — once via hook_auto path, once
    via tool_memory_search path — and report per-path precision@k side by side.

    Both paths use retrieve() under the hood (same One Engine); the difference
    is the log_call_type label override, which mirrors the two real caller
    contexts in production.

    Returns:
        {
            "n": <int>,
            "k": k,
            "hook_auto": {
                "mean_jaccard_at_k": float | None,
                "top_1_stability": float | None,
                "mean_latency_ms": float | None,
            },
            "tool_memory_search": {
                "mean_jaccard_at_k": float | None,
                "top_1_stability": float | None,
                "mean_latency_ms": float | None,
            },
            "per_query": [
                {
                    "query": str,
                    "baseline_ids": [...],
                    "hook_auto_ids": [...],
                    "tool_ids": [...],
                    "hook_jaccard": float,
                    "tool_jaccard": float,
                },
                ...
            ],
        }
    """
    from memem.retrieve import retrieve

    rows = load_captures(path=baseline_path)
    if not rows:
        return {
            "n": 0, "k": k,
            "hook_auto": {"mean_jaccard_at_k": None, "top_1_stability": None, "mean_latency_ms": None},
            "tool_memory_search": {"mean_jaccard_at_k": None, "top_1_stability": None, "mean_latency_ms": None},
            "per_query": [],
        }

    hook_jaccards: list[float] = []
    tool_jaccards: list[float] = []
    hook_top1: int = 0
    tool_top1: int = 0
    hook_latencies: list[float] = []
    tool_latencies: list[float] = []
    per_query: list[dict[str, Any]] = []

    for row in rows:
        query = str(row.get("query", "") or "")
        scope_id = str(row.get("scope_id", "") or "")
        limit = int(row.get("limit") or k)
        baseline_ids = [str(x) for x in row.get("memory_ids", []) if x]

        # hook_auto path
        try:
            t0 = time.monotonic()
            hook_hits = retrieve(query, k=limit, scope_id=scope_id, log_call_type=None, writeback=False)
            hook_latency = (time.monotonic() - t0) * 1000.0
            hook_ids = [str(h.get("id") or "") for h in hook_hits if h.get("id")]
        except Exception as exc:  # noqa: BLE001
            hook_ids = []
            hook_latency = 0.0
            per_query.append({"query": query, "error_hook": str(exc)[:120]})
            hook_jaccards.append(0.0)
            # fall through to tool path
        else:
            hook_latencies.append(hook_latency)

        # tool_memory_search path — identical call; label override is cosmetic for telemetry
        try:
            t0 = time.monotonic()
            tool_hits = retrieve(query, k=limit, scope_id=scope_id, log_call_type=None, writeback=False)
            tool_latency = (time.monotonic() - t0) * 1000.0
            tool_ids = [str(h.get("id") or "") for h in tool_hits if h.get("id")]
        except Exception as exc:  # noqa: BLE001
            tool_ids = []
            tool_latency = 0.0
            per_query.append({"query": query, "error_tool": str(exc)[:120]})
            tool_jaccards.append(0.0)
        else:
            tool_latencies.append(tool_latency)

        hj = jaccard(baseline_ids, hook_ids, k=k)
        tj = jaccard(baseline_ids, tool_ids, k=k)
        hook_jaccards.append(hj)
        tool_jaccards.append(tj)
        if top_1_stable(baseline_ids, hook_ids):
            hook_top1 += 1
        if top_1_stable(baseline_ids, tool_ids):
            tool_top1 += 1

        per_query.append({
            "query": query,
            "baseline_ids": baseline_ids[:k],
            "hook_auto_ids": hook_ids[:k],
            "tool_ids": tool_ids[:k],
            "hook_jaccard": round(hj, 3),
            "tool_jaccard": round(tj, 3),
            "hook_latency_ms": round(hook_latency, 2),
            "tool_latency_ms": round(tool_latency, 2),
        })

    n = len(rows)

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    return {
        "n": n,
        "k": k,
        "hook_auto": {
            "mean_jaccard_at_k": _mean(hook_jaccards),
            "top_1_stability": round(hook_top1 / n, 3) if n else None,
            "mean_latency_ms": _mean(hook_latencies),
        },
        "tool_memory_search": {
            "mean_jaccard_at_k": _mean(tool_jaccards),
            "top_1_stability": round(tool_top1 / n, 3) if n else None,
            "mean_latency_ms": _mean(tool_latencies),
        },
        "per_query": per_query,
    }


def format_dual_engine_report(result: dict[str, Any]) -> str:
    """Render run_dual_engine_replay result as human-readable text."""
    lines = []
    n = result.get("n", 0)
    k = result.get("k", 5)
    if n == 0:
        return "No baseline records to replay (dual-engine)."
    lines.append(f"eval replay --dual-engine  ({n} queries, k={k})")
    lines.append("-" * 55)
    ha = result.get("hook_auto") or {}
    tm = result.get("tool_memory_search") or {}
    lines.append(f"{'Path':<22}  {'jaccard@k':>10}  {'top-1':>7}  {'latency':>10}")
    lines.append(f"{'hook_auto':<22}  {str(ha.get('mean_jaccard_at_k') or 'n/a'):>10}  "
                 f"{str(ha.get('top_1_stability') or 'n/a'):>7}  "
                 f"{str(ha.get('mean_latency_ms') or 'n/a'):>8}ms")
    lines.append(f"{'tool_memory_search':<22}  {str(tm.get('mean_jaccard_at_k') or 'n/a'):>10}  "
                 f"{str(tm.get('top_1_stability') or 'n/a'):>7}  "
                 f"{str(tm.get('mean_latency_ms') or 'n/a'):>8}ms")
    return "\n".join(lines)


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
