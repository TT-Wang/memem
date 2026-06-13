"""Synchronous append-only telemetry for memem recall events.

Logs every recall call (auto, hook-skipped, tool-initiated) to
~/.memem/.recall_log.jsonl for downstream analysis.

Single-writer append is atomic for lines under PIPE_BUF on Linux —
no fcntl lock needed. NO threads, NO queue. Synchronous I/O only.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from memem.models import MEMEM_DIR

_LOG_PATH = Path(MEMEM_DIR) / ".recall_log.jsonl"


def _current_log_path() -> Path:
    """Return the recall log path resolved from the current MEMEM_DIR env var.

    This is called at write time (not import time) so that tests that
    monkeypatch MEMEM_DIR automatically redirect writes to their tmp dir
    without needing to reload this module. ``_LOG_PATH`` (module-level) is
    kept for read paths — they stay correct either via importlib.reload()
    in fixture setup (conftest) or via local ``from memem.recall_log import
    _LOG_PATH`` statements that re-resolve the module attribute at call time
    (dreamer.py, cli.py). Both see the current value after a reload.
    """
    state_dir = os.environ.get("MEMEM_DIR") or os.environ.get("CORTEX_DIR") or str(MEMEM_DIR)
    return Path(state_dir) / ".recall_log.jsonl"


def log_recall(
    call_type: str,
    query: str,
    returned_ids: list[str],
    latency_ms: int,
    source: str,
    session_id: str = "",
) -> None:
    """Append one JSONL line. Silent no-op on any error (telemetry must
    never break the caller).

    Telemetry source guard (MEMEM_TELEMETRY_SOURCE env var):
    - "test"      → silently discarded; no write.
    - "<other>"   → written with an extra source_tag field for filtering.
    - unset/empty → written as production row (no source_tag — backward compat).
    """
    src = os.environ.get("MEMEM_TELEMETRY_SOURCE", "")
    if src == "test":
        return
    try:
        log_path = _current_log_path()
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "call_type": call_type,
            "query": query[:500],  # cap to bound file size
            "returned_ids": list(returned_ids)[:20],
            "latency_ms": int(latency_ms),
            "source": source,
        }
        if session_id:
            entry["session_id"] = session_id
        if src:
            entry["source_tag"] = src
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


def log_citation(
    session_id: str,
    cited_ids: list[str],
    source: str = "mine_delta",
) -> None:
    """Append a citation row to the recall log. Silent no-op on any error.

    Telemetry source guard (MEMEM_TELEMETRY_SOURCE env var):
    - "test"      → silently discarded; no write.
    - "<other>"   → written with an extra source_tag field for filtering.
    - unset/empty → written as production row (no source_tag — backward compat).
    """
    src = os.environ.get("MEMEM_TELEMETRY_SOURCE", "")
    if src == "test":
        return
    try:
        log_path = _current_log_path()
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "type": "citation",
            "session_id": session_id,
            "cited_ids": list(cited_ids),
            "source": source,
        }
        if src:
            entry["source_tag"] = src
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


def analyze_recalls(days: int = 7, include_tagged: bool = False) -> dict:
    """Read .recall_log.jsonl and return summary for last N days.

    Row types:
      - recall rows: no ``type`` field (legacy + current log_recall rows)
      - citation rows: ``type == 'citation'`` (written by log_citation)

    Citation rows are counted separately and excluded from recall aggregations
    (by_call_type, latency, queries, etc.).

    By default rows with a ``source_tag`` field are excluded (these originate
    from benchmark / non-production callers).  Pass ``include_tagged=True`` to
    include them (e.g. for inspecting benchmark traffic).
    """
    if not _LOG_PATH.exists():
        return {"total": 0, "by_call_type": {}, "top_queries": [],
                "median_latency_per_type": {}, "calls_per_day": {}, "days": days,
                "citation_rate": {}, "top_cited_memories": [], "returned_ids_count": 0}
    cutoff = datetime.now(UTC).timestamp() - (days * 86400)
    recall_entries: list[dict] = []
    citation_entries: list[dict] = []
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
                        # Skip tagged rows unless caller opts in. Note: this
                        # also excludes CITATION rows from tagged sessions
                        # (e.g. benchmark runs), which is deliberate — the
                        # citation_rate metric stays a production-only signal.
                        if not include_tagged and e.get("source_tag"):
                            continue
                        if e.get("type") == "citation":
                            citation_entries.append(e)
                        else:
                            # Treat rows without type as recall rows (backward compat)
                            recall_entries.append(e)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except OSError:
        return {"total": 0, "by_call_type": {}, "top_queries": [],
                "median_latency_per_type": {}, "calls_per_day": {}, "days": days,
                "citation_rate": {}, "top_cited_memories": [], "returned_ids_count": 0}

    # by_call_type (recall rows only)
    by_type: dict[str, int] = {}
    latencies: dict[str, list[int]] = {}
    queries: dict[str, int] = {}
    days_count: dict[str, int] = {}
    for e in recall_entries:
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

    # Citation rate per call_type:
    #   union of all cited_ids across citation rows / distinct returned_ids
    #   across recall rows that have returned_ids, grouped by call_type.
    # Build a set of all cited ids (global; cross-session)
    all_cited: set[str] = set()
    for e in citation_entries:
        for cid in e.get("cited_ids") or []:
            if cid:
                all_cited.add(str(cid)[:8])  # defensive — writers emit 8-char

    # Per call_type: returned ids union.
    # NORMALIZE to 8-char prefixes: returned_ids forms differ by call site
    # (retrieve.py hook path logs FULL uuids; server.py tools log 8-char) while
    # citation rows always carry 8-char ids. Without [:8] here the intersection
    # below is empty by construction for full-uuid rows and the hook path's
    # citation_rate reads as a permanent 0.0.
    type_returned: dict[str, set[str]] = {}
    for e in recall_entries:
        rids = e.get("returned_ids")
        if not rids:
            continue
        ct = e.get("call_type", "?")
        type_returned.setdefault(ct, set()).update(
            str(r)[:8] for r in rids if r
        )

    citation_rate: dict[str, float] = {}
    for ct, rids_set in type_returned.items():
        cited_for_type = rids_set & all_cited
        citation_rate[ct] = len(cited_for_type) / len(rids_set) if rids_set else 0.0

    # top_cited_memories: count how often each id appears in cited_ids lists
    cited_counts: dict[str, int] = {}
    for e in citation_entries:
        for cid in e.get("cited_ids") or []:
            cited_counts[cid] = cited_counts.get(cid, 0) + 1
    top_cited = sorted(cited_counts.items(), key=lambda x: -x[1])[:5]

    # Token-budget proxy: total count of returned_ids across all recall rows
    returned_ids_count = sum(len(e.get("returned_ids") or []) for e in recall_entries)

    return {
        "total": len(recall_entries),
        "by_call_type": by_type,
        "top_queries": sorted(queries.items(), key=lambda x: -x[1])[:10],
        "median_latency_per_type": {k: _median(v) for k, v in latencies.items()},
        "calls_per_day": days_count,
        "days": days,
        "citation_rate": citation_rate,
        "top_cited_memories": top_cited,
        "returned_ids_count": returned_ids_count,
    }
