"""Eval-replay capture: log every recall call to an NDJSON baseline.

Inspired by gbrain's eval capture/export/replay pattern. Off by default —
set MEMEM_EVAL_CAPTURE=1 to enable. Each captured row records the query
(PII-scrubbed), the retrieved memory_ids, the retrieval mode, scope, top-K,
and latency. `memem eval export` snapshots a baseline; `memem eval replay`
re-runs each query against current code and reports Jaccard@k, top-1
stability, and latency Δ vs. the baseline.

The point: gate retrieval-touching changes on real-world recall regressions
that ground-truth-free unit tests can't catch.

Wire format: NDJSON, one record per line, `schema_version: 1`. Additive
evolution within v1; breaking changes bump to v2.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memem.models import MEMEM_DIR

EVAL_CAPTURE_FILE = MEMEM_DIR / "eval_captures.jsonl"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# PII scrubbing
# ---------------------------------------------------------------------------

# Order matters — more specific patterns first so they don't get partial-matched
# by the general ones. Each redacts to a stable token so diffs stay readable.
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWTs (three base64url segments separated by dots)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
     "[REDACTED_JWT]"),
    # Anthropic / OpenAI API keys
    (re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED_APIKEY]"),
    # GitHub PATs
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_PAT]"),
    # AWS access keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Slack tokens
    (re.compile(r"\bxox[bpoa]-[0-9]+-[A-Za-z0-9-]+\b"), "[REDACTED_SLACK_TOKEN]"),
    # Bearer tokens in headers
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}\b"), "Bearer [REDACTED_TOKEN]"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "[REDACTED_EMAIL]"),
    # Phone numbers — require explicit separators or parens so plain digit
    # runs (like 16-char hash IDs) don't false-positive. Matches:
    #   +1 555 123 4567 / (555) 123-4567 / 555-123-4567 / 555.123.4567
    (re.compile(r"(?:\+\d{1,3}[ .-])?\(?\d{2,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}\b"),
     "[REDACTED_PHONE]"),
    # SSN
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
)


def _luhn_check(digits: str) -> bool:
    """Verify a digit string passes the Luhn checksum (credit card heuristic)."""
    nums = [int(d) for d in digits if d.isdigit()]
    if len(nums) < 13 or len(nums) > 19:
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        checksum += n
    return checksum % 10 == 0


_CC_PATTERN = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def scrub_pii(text: str) -> str:
    """Redact PII from text. Conservative — false positives are fine, leaks aren't.

    Order matters:
      1. Structured-token patterns (JWTs, API keys, GitHub PATs, AWS, Slack, bearer)
      2. Credit cards (Luhn-verified) — BEFORE phone, because the phone regex
         matches greedily and would eat the first 3 segments of a 16-digit card
         like "4111-1111-1111-1111" → "[REDACTED_PHONE]-1111", letting the
         remainder slip past the Luhn gate
      3. Loose patterns (email, phone, SSN)
    """
    if not text:
        return text
    redacted = text
    # 1. Structured tokens first
    for pattern, replacement in _PII_PATTERNS:
        # Skip phone here — apply it AFTER credit cards
        if "PHONE" in replacement:
            continue
        redacted = pattern.sub(replacement, redacted)
    # 2. Credit cards: Luhn-verified to avoid stripping random digit runs
    def _cc_sub(match: re.Match[str]) -> str:
        digits = re.sub(r"[^\d]", "", match.group(0))
        return "[REDACTED_CC]" if _luhn_check(digits) else match.group(0)
    redacted = _CC_PATTERN.sub(_cc_sub, redacted)
    # 3. Phone (deferred from step 1 so CC matches first)
    for pattern, replacement in _PII_PATTERNS:
        if "PHONE" in replacement:
            redacted = pattern.sub(replacement, redacted)
    return redacted


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Capture is opt-in via MEMEM_EVAL_CAPTURE=1."""
    return os.environ.get("MEMEM_EVAL_CAPTURE", "").strip() in ("1", "true", "yes")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def capture(
    *,
    query: str,
    mode: str,
    memory_ids: list[str],
    scope_id: str = "default",
    limit: int | None = None,
    latency_ms: float | None = None,
) -> None:
    """Append one capture record. Silently no-ops when capture is disabled or
    on any I/O error — the recall path must never break because eval-capture
    failed.
    """
    if not is_enabled():
        return
    try:
        EVAL_CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": _now_iso(),
            "mode": mode,
            "scope_id": scope_id,
            "query": scrub_pii(query),
            "memory_ids": list(memory_ids),
            "limit": limit,
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        }
        with open(EVAL_CAPTURE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
    except OSError:
        # Never break recall on a logging failure
        pass


def time_call(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    """Call fn(*args, **kwargs); return (result, elapsed_ms)."""
    t0 = time.monotonic()
    result = fn(*args, **kwargs)
    return result, (time.monotonic() - t0) * 1000.0


# ---------------------------------------------------------------------------
# Read / iterate captures
# ---------------------------------------------------------------------------


def load_captures(
    *,
    path: Path | None = None,
    since_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Read capture records, optionally filtered to those newer than N seconds ago."""
    target = path or EVAL_CAPTURE_FILE
    if not target.exists():
        return []
    cutoff = None
    if since_seconds is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - since_seconds
    rows: list[dict[str, Any]] = []
    with open(target, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff is not None:
                ts = row.get("ts", "")
                try:
                    row_ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    if row_ts < cutoff:
                        continue
                except ValueError:
                    continue
            rows.append(row)
    return rows
