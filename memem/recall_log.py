"""Synchronous append-only telemetry for memem recall events.

Logs every recall call (auto, hook-skipped, tool-initiated) to
~/.memem/.recall_log.jsonl for downstream analysis.

Single-writer append is atomic for lines under PIPE_BUF on Linux —
no fcntl lock needed. NO threads, NO queue. Synchronous I/O only.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from memem.models import MEMEM_DIR

_LOG_PATH = Path(MEMEM_DIR) / ".recall_log.jsonl"


def log_recall(
    call_type: str,
    query: str,
    returned_ids: list[str],
    latency_ms: int,
    source: str,
) -> None:
    """Append one JSONL line. Silent no-op on any error (telemetry must
    never break the caller)."""
    try:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "call_type": call_type,
            "query": query[:500],  # cap to bound file size
            "returned_ids": list(returned_ids)[:20],
            "latency_ms": int(latency_ms),
            "source": source,
        }
        Path(MEMEM_DIR).mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


def analyze_recalls(days: int = 7) -> dict:
    """Read .recall_log.jsonl and return summary for last N days."""
    if not _LOG_PATH.exists():
        return {"total": 0, "by_call_type": {}, "top_queries": [],
                "median_latency_per_type": {}, "calls_per_day": {}, "days": days}
    cutoff = datetime.now(UTC).timestamp() - (days * 86400)
    entries = []
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"]).timestamp()
                    if ts >= cutoff:
                        entries.append(e)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except OSError:
        return {"total": 0, "by_call_type": {}, "top_queries": [],
                "median_latency_per_type": {}, "calls_per_day": {}, "days": days}
    # by_call_type
    by_type: dict[str, int] = {}
    latencies: dict[str, list[int]] = {}
    queries: dict[str, int] = {}
    days_count: dict[str, int] = {}
    for e in entries:
        ct = e.get("call_type", "?")
        by_type[ct] = by_type.get(ct, 0) + 1
        latencies.setdefault(ct, []).append(e.get("latency_ms", 0))
        q = e.get("query", "")[:80]
        queries[q] = queries.get(q, 0) + 1
        day = e["ts"][:10]
        days_count[day] = days_count.get(day, 0) + 1

    def _median(xs: list[int]) -> int:
        if not xs:
            return 0
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2

    return {
        "total": len(entries),
        "by_call_type": by_type,
        "top_queries": sorted(queries.items(), key=lambda x: -x[1])[:10],
        "median_latency_per_type": {k: _median(v) for k, v in latencies.items()},
        "calls_per_day": days_count,
        "days": days,
    }
