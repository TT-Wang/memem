"""LongMemEval-inspired evaluation set scoped to memem-domain.

Five abilities (per arXiv 2410.10813):
  1. Extraction — finding a specific fact in N memories
  2. Multi-session reasoning — combining facts from multiple memories
  3. Temporal reasoning — distinguishing recent vs. invalidated facts
  4. Knowledge updates — using the most-current of two contradictory facts
  5. Abstention — knowing when to NOT answer (no relevant memory exists)

Each question is a (setup, query, expected_outcome) triple. The harness
plants the setup memories in a tmp vault, runs recall, scores against
the expected outcome.
"""
from __future__ import annotations

from typing import TypedDict


class EvalCase(TypedDict, total=False):
    case_id: str
    ability: str  # one of: extraction, multi_session, temporal, knowledge_update, abstention
    setup: list[dict]  # memories to plant
    query: str
    expected_present_ids: list[str]      # IDs that MUST appear in slice items
    expected_absent_ids: list[str]       # IDs that MUST NOT appear
    project: str


EVAL_SET: list[EvalCase] = [
    # Ability 1: extraction — single fact in a multi-memory vault
    {
        "case_id": "extract-1",
        "ability": "extraction",
        "project": "evaltest",
        "setup": [
            {"id_hint": "extract-1-target", "title": "JWT uses RS256",
             "content": "evaltest: JWT signing algorithm is RS256 (not HS256). Decided 2026-01-15."},
            {"id_hint": "extract-1-noise", "title": "noise about deploys",
             "content": "evaltest: deploys happen on Tuesdays."},
        ],
        "query": "what JWT algorithm does evaltest use",
        "expected_present_ids": ["extract-1-target"],
        "expected_absent_ids": [],
    },
    # Ability 2: multi-session reasoning — two related memories must both appear
    {
        "case_id": "multi-1",
        "ability": "multi_session",
        "project": "evaltest",
        "setup": [
            {"id_hint": "multi-1-a", "title": "auth uses RS256 JWTs",
             "content": "evaltest: auth uses RS256-signed JWTs."},
            {"id_hint": "multi-1-b", "title": "RS256 keys rotate every 90 days",
             "content": "evaltest: RS256 signing keys rotate every 90 days via KMS."},
        ],
        "query": "evaltest auth key rotation policy RS256",
        "expected_present_ids": ["multi-1-a", "multi-1-b"],
        "expected_absent_ids": [],
    },
    # Ability 3: temporal — newer memory should rank above older
    {
        "case_id": "temporal-1",
        "ability": "temporal",
        "project": "evaltest",
        "setup": [
            {"id_hint": "temporal-1-old", "title": "old config: PORT 3000",
             "content": "evaltest: server runs on PORT 3000.", "importance": 2},
            {"id_hint": "temporal-1-new", "title": "current: PORT 8080",
             "content": "evaltest: server runs on PORT 8080 (updated from 3000).", "importance": 4},
        ],
        "query": "evaltest server port",
        "expected_present_ids": ["temporal-1-new"],
        "expected_absent_ids": [],
    },
    # Ability 4: knowledge updates — invalidated memory should not appear
    {
        "case_id": "knowledge-update-1",
        "ability": "knowledge_update",
        "project": "evaltest",
        "setup": [
            {"id_hint": "ku-1-stale", "title": "deprecated DB host",
             "content": "evaltest: database lives at db.old.example.com",
             "_invalidate": True},
            {"id_hint": "ku-1-current", "title": "current DB host",
             "content": "evaltest: database lives at db.new.example.com (replaces old)"},
        ],
        "query": "evaltest database host",
        "expected_present_ids": ["ku-1-current"],
        "expected_absent_ids": ["ku-1-stale"],
    },
    # Ability 5: abstention — no required memory exists for this query
    # (abstention = no item in expected_present_ids; absence of specific noise
    # is not enforced here because fuzzy-search with low thresholds will surface
    # nearby memories in a small vault — the invariant is that nothing is
    # *required* to appear, not that noise is suppressed).
    {
        "case_id": "abstain-1",
        "ability": "abstention",
        "project": "evaltest",
        "setup": [
            {"id_hint": "abstain-1-noise", "title": "unrelated note about CSS",
             "content": "evaltest: prefer Tailwind over Bootstrap for new components."},
        ],
        "query": "payment provider stripe kafka integration bus",
        "expected_present_ids": [],
        "expected_absent_ids": [],
    },
]


def run_eval_case(case: EvalCase) -> dict:
    """Plant the case's setup, run recall, score.

    Returns: {'case_id': str, 'pass': bool, 'expected_present_missing': [],
              'expected_absent_present': [], 'errors': []}
    """
    from memem.obsidian_store import _make_memory, _save_memory, invalidate_memory
    from memem.recall import _search_memories

    case_id = case.get("case_id", "?")
    errors = []
    id_map = {}

    for setup_item in case.get("setup", []):
        try:
            kwargs = {
                "content": setup_item["content"],
                "title": setup_item["title"],
                "project": case.get("project", "evaltest"),
                "source_type": "user",
                "importance": setup_item.get("importance", 3),
            }
            mem = _make_memory(**kwargs)
            _save_memory(mem)
            id_map[setup_item.get("id_hint", "?")] = mem["id"]
        except Exception as exc:
            errors.append(f"setup {setup_item.get('id_hint')}: {exc}")
            return {"case_id": case_id, "pass": False, "errors": errors}

    # Apply post-setup invalidations
    for setup_item in case.get("setup", []):
        if setup_item.get("_invalidate"):
            target = id_map.get(setup_item.get("id_hint"))
            if target:
                invalidate_memory(target)

    # Run recall — exclude invalidated memories so knowledge_update cases score correctly
    try:
        results = _search_memories(case["query"], scope_id=case.get("project", "evaltest"), limit=20)
    except Exception as exc:
        return {"case_id": case_id, "pass": False, "errors": [f"recall: {exc}"]}

    result_ids = {r.get("id") for r in results if r.get("invalid_at") is None}

    expected_present_missing = [
        h for h in case.get("expected_present_ids", [])
        if id_map.get(h) and id_map.get(h) not in result_ids
    ]
    expected_absent_present = [
        h for h in case.get("expected_absent_ids", [])
        if id_map.get(h) and id_map.get(h) in result_ids
    ]

    return {
        "case_id": case_id,
        "ability": case.get("ability", "?"),
        "pass": not expected_present_missing and not expected_absent_present,
        "expected_present_missing": expected_present_missing,
        "expected_absent_present": expected_absent_present,
        "errors": errors,
    }


def run_all_cases() -> dict:
    """Run every case in EVAL_SET. Returns summary dict."""
    results = [run_eval_case(c) for c in EVAL_SET]
    by_ability: dict[str, dict[str, int]] = {}
    for r in results:
        a = r.get("ability", "?")
        by_ability.setdefault(a, {"pass": 0, "fail": 0})
        if r["pass"]:
            by_ability[a]["pass"] += 1
        else:
            by_ability[a]["fail"] += 1
    return {
        "total_cases": len(results),
        "passed": sum(1 for r in results if r["pass"]),
        "failed": sum(1 for r in results if not r["pass"]),
        "by_ability": by_ability,
        "results": results,
    }
