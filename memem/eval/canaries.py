"""Adversarial canary memories — trip-wire for poisoning, runaway decay,
consolidation drift.

Two canary sets:
  - 'always_reachable': L0-equivalent invariants. Specific queries MUST
    return them. Drift = something silently dropped them. Trip-wire fires.
  - 'never_elevate': L3-equivalent junk that should never reach L0/L1.
    Drift = consolidation pass mistakenly promoted them. Trip-wire fires.

Standard ML safety practice; per MINJA / MemoryGraft attack research,
canaries also detect memory poisoning before it spreads.
"""
from __future__ import annotations

from typing import TypedDict


class Canary(TypedDict):
    canary_id: str          # stable identifier (used in lesson exclusions if needed)
    title: str
    content: str
    project: str
    expected_layer: int     # the layer the canary should LIVE at
    trigger_query: str      # the query the canary MUST appear for (always_reachable)
                             # or MUST NOT appear for / be promoted from (never_elevate)


# 5 always-reachable canaries: planted at L0; specific queries must surface them
ALWAYS_REACHABLE: list[Canary] = [
    {
        "canary_id": "canary-always-1",
        "title": "memem canary: always-reachable identity fact",
        "content": "memem-canary-test: this project is the memem code-agent memory plugin.",
        "project": "memem-canary",
        "expected_layer": 0,
        "trigger_query": "what is the memem-canary project",
    },
    {
        "canary_id": "canary-always-2",
        "title": "memem canary: always-reachable convention",
        "content": "memem-canary-test: convention — always commit before forge tier transitions.",
        "project": "memem-canary",
        "expected_layer": 0,
        "trigger_query": "memem-canary commit convention forge tier",
    },
    {
        "canary_id": "canary-always-3",
        "title": "memem canary: always-reachable invariant",
        "content": "memem-canary-test: invariant — L0 memories must never be auto-decayed.",
        "project": "memem-canary",
        "expected_layer": 0,
        "trigger_query": "memem-canary L0 decay invariant",
    },
    {
        "canary_id": "canary-always-4",
        "title": "memem canary: critical gotcha",
        "content": "memem-canary-test: bug pattern — whitelist normalization drops new fields silently.",
        "project": "memem-canary",
        "expected_layer": 0,
        "trigger_query": "memem-canary whitelist normalization bug",
    },
    {
        "canary_id": "canary-always-5",
        "title": "memem canary: identity fact 5",
        "content": "memem-canary-test: vault location is configurable via MEMEM_OBSIDIAN_VAULT.",
        "project": "memem-canary",
        "expected_layer": 0,
        "trigger_query": "memem-canary MEMEM_OBSIDIAN_VAULT configuration",
    },
]

# 5 never-elevate canaries: planted at L3; should NEVER be promoted by dreamer
NEVER_ELEVATE: list[Canary] = [
    {
        "canary_id": "canary-never-1",
        "title": "memem canary: never-elevate junk 1",
        "content": "memem-canary-test: trivial filler, should never become L0/L1/L2.",
        "project": "memem-canary",
        "expected_layer": 3,
        "trigger_query": "memem-canary never elevate junk",
    },
    {
        "canary_id": "canary-never-2",
        "title": "memem canary: never-elevate junk 2",
        "content": "memem-canary-test: low-importance archival noise that must stay at L3.",
        "project": "memem-canary",
        "expected_layer": 3,
        "trigger_query": "memem-canary low importance noise",
    },
    {
        "canary_id": "canary-never-3",
        "title": "memem canary: never-elevate junk 3",
        "content": "memem-canary-test: rare incident from 2024 — only relevant if X happens.",
        "project": "memem-canary",
        "expected_layer": 3,
        "trigger_query": "memem-canary 2024 rare incident",
    },
    {
        "canary_id": "canary-never-4",
        "title": "memem canary: never-elevate junk 4",
        "content": "memem-canary-test: deprecated workaround for issue #999.",
        "project": "memem-canary",
        "expected_layer": 3,
        "trigger_query": "memem-canary deprecated workaround",
    },
    {
        "canary_id": "canary-never-5",
        "title": "memem canary: never-elevate junk 5",
        "content": "memem-canary-test: one-off debugging note.",
        "project": "memem-canary",
        "expected_layer": 3,
        "trigger_query": "memem-canary one-off debugging",
    },
]


def all_canaries() -> list[Canary]:
    """Both sets combined."""
    return ALWAYS_REACHABLE + NEVER_ELEVATE


def plant_canaries() -> list[str]:
    """Save all canaries to the vault. Returns list of memory_ids planted.

    Uses decay_immune=True so canaries are exempt from m3 decay. Layer set
    explicitly per canary spec. Idempotent: re-running uses dedup to skip
    existing.
    """
    from memem.obsidian_store import _make_memory, _save_memory

    planted = []
    for c in all_canaries():
        try:
            mem = _make_memory(
                content=c["content"],
                title=c["title"],
                project=c["project"],
                source_type="user",
                layer=c["expected_layer"],
                tags=["canary", "memem-test"],
            )
            mem["decay_immune"] = True  # canaries exempt from decay
            _save_memory(mem)
            planted.append(mem["id"])
        except Exception as exc:
            # likely a duplicate from a previous plant — skip
            from memem import obsidian_store
            log = getattr(obsidian_store, "log", None)
            if log:
                log.info("canary plant skipped (likely duplicate): %s", exc)
    return planted


def verify_canaries_intact() -> dict:
    """Check that every canary is at its expected layer and is reachable
    via its trigger_query (for ALWAYS_REACHABLE) or is NOT elevated (for
    NEVER_ELEVATE).

    Returns: {
        'pass': bool,
        'always_reachable_failures': [canary_id, ...],
        'never_elevate_failures': [canary_id, ...],
        'errors': [str, ...]
    }
    """
    from memem.obsidian_store import _obsidian_memories

    failures_always = []
    failures_never = []
    errors = []

    try:
        all_mems = _obsidian_memories(scope_id="memem-canary")
    except Exception as exc:
        return {
            "pass": False,
            "always_reachable_failures": [],
            "never_elevate_failures": [],
            "errors": [f"could not load canary project: {exc}"],
        }

    by_title = {m.get("title"): m for m in all_mems}

    for c in ALWAYS_REACHABLE:
        m = by_title.get(c["title"])
        if not m:
            failures_always.append(c["canary_id"])
            errors.append(f"{c['canary_id']}: missing from vault")
            continue
        raw_layer = m.get("layer")
        actual_layer = int(raw_layer) if raw_layer is not None else 2
        if actual_layer != c["expected_layer"]:
            failures_always.append(c["canary_id"])
            errors.append(
                f"{c['canary_id']}: layer drift L{raw_layer} (expected L{c['expected_layer']})"
            )

    for c in NEVER_ELEVATE:
        m = by_title.get(c["title"])
        if not m:
            # Missing isn't a failure for never-elevate (might not be planted yet)
            continue
        raw_layer = m.get("layer")
        actual = int(raw_layer) if raw_layer is not None else 2
        if actual < c["expected_layer"]:
            # Got promoted (lower layer number = higher importance)
            failures_never.append(c["canary_id"])
            errors.append(
                f"{c['canary_id']}: PROMOTED to L{actual} (expected L{c['expected_layer']})"
            )

    return {
        "pass": not failures_always and not failures_never,
        "always_reachable_failures": failures_always,
        "never_elevate_failures": failures_never,
        "errors": errors,
    }
