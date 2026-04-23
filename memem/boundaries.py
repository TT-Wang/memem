"""Boundary rules for Active Memory Slice construction."""

from __future__ import annotations

from typing import Any

from memem.active_slice import Candidate
from memem.models import _normalize_scope_id
from memem.obsidian_store import _word_set

ROLE_BUDGETS = {
    "goals": 3,
    "constraints": 6,
    "background": 8,
    "decisions": 5,
    "preferences": 4,
    "failure_patterns": 5,
    "artifact_context": 6,
    "open_tensions": 6,
}


def _candidate_key(candidate: Candidate) -> str:
    return candidate.get("memory_id") or candidate.get("artifact_id") or candidate.get("candidate_id", "")


def _scope_allowed(candidate: Candidate, scope_id: str) -> bool:
    normalized = _normalize_scope_id(scope_id)
    if normalized in {"", "default", "general"}:
        return True
    if candidate.get("candidate_type") in {"current_query", "environment", "transcript"}:
        return True
    project = _normalize_scope_id(candidate.get("project", "general"))
    return project in {normalized, "general"}


def _is_deprecated(candidate: Candidate) -> bool:
    return candidate.get("status", "active") == "deprecated"


def _overlap(a: str, b: str) -> float:
    aw = _word_set(a)
    bw = _word_set(b)
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(min(len(aw), len(bw)), 1)


def apply_pre_boundaries(
    candidates: list[Candidate],
    scope_id: str,
    include_history: bool = False,
) -> dict[str, Any]:
    """Filter candidates before LLM/heuristic activation."""
    filtered: list[Candidate] = []
    excluded: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    kept_texts: list[tuple[str, str, float]] = []

    for candidate in sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True):
        key = _candidate_key(candidate)
        if key and key in seen_ids:
            excluded.append({"candidate_id": candidate.get("candidate_id", key), "reason": "duplicate_id"})
            continue
        if _is_deprecated(candidate) and not include_history:
            excluded.append({"candidate_id": candidate.get("candidate_id", key), "reason": "deprecated"})
            continue
        if not _scope_allowed(candidate, scope_id):
            excluded.append({"candidate_id": candidate.get("candidate_id", key), "reason": "cross_scope"})
            continue

        text = f"{candidate.get('title', '')} {candidate.get('summary', '')}"
        duplicate = False
        for kept_id, kept_text, kept_score in kept_texts:
            if _overlap(text, kept_text) >= 0.92:
                duplicate = True
                reason = "semantic_duplicate" if candidate.get("score", 0.0) <= kept_score else "semantic_duplicate_weaker_prior"
                excluded.append({
                    "candidate_id": candidate.get("candidate_id", key),
                    "reason": reason,
                    "kept_candidate_id": kept_id,
                })
                break
        if duplicate:
            continue

        if key:
            seen_ids.add(key)
        kept_texts.append((candidate.get("candidate_id", key), text, float(candidate.get("score", 0.0))))
        filtered.append(candidate)

    return {"candidates": filtered, "excluded_candidates": excluded}


def _cap_entries(entries: list[dict[str, Any]], cap: int, ignored: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    sorted_entries = sorted(entries, key=lambda e: e.get("score", 0.0), reverse=True)
    kept = sorted_entries[:cap]
    for entry in sorted_entries[cap:]:
        ignored.append({"candidate_id": entry.get("candidate_id") or entry.get("memory_id", ""), "reason": "role_budget", "role": role})
    return kept


def apply_post_boundaries(
    activation_result: dict[str, Any],
    candidates: list[Candidate],
    scope_id: str,
    include_history: bool = False,
) -> dict[str, Any]:
    """Enforce role caps and re-check selected IDs after activation."""
    by_id = {}
    for candidate in candidates:
        cid = candidate.get("candidate_id", "")
        if cid:
            by_id[cid] = candidate
        mid = candidate.get("memory_id", "")
        if mid:
            by_id[mid] = candidate
            by_id[mid[:8]] = candidate
        aid = candidate.get("artifact_id", "")
        if aid:
            by_id[aid] = candidate

    ignored = list(activation_result.get("ignored", []))
    filtered_result = dict(activation_result)
    selected_keys: set[str] = set()

    for role, cap in ROLE_BUDGETS.items():
        if role == "open_tensions":
            tensions = activation_result.get(role, [])[:cap]
            for tension in activation_result.get(role, [])[cap:]:
                ignored.append({"candidate_id": tension.get("tension_id", ""), "reason": "role_budget", "role": role})
            filtered_result[role] = tensions
            continue

        entries = []
        for entry in activation_result.get(role, []):
            key = entry.get("candidate_id") or entry.get("memory_id") or entry.get("artifact_id", "")
            candidate = by_id.get(key, {})
            if not candidate:
                ignored.append({"candidate_id": key, "reason": "unknown_candidate", "role": role})
                continue
            if _is_deprecated(candidate) and not include_history:
                ignored.append({"candidate_id": key, "reason": "deprecated", "role": role})
                continue
            if not _scope_allowed(candidate, scope_id):
                ignored.append({"candidate_id": key, "reason": "cross_scope", "role": role})
                continue
            selected_key = _candidate_key(candidate)
            if selected_key and selected_key in selected_keys and role != "artifact_context":
                ignored.append({"candidate_id": key, "reason": "duplicate_selected", "role": role})
                continue
            if selected_key:
                selected_keys.add(selected_key)
            entries.append(entry)
        filtered_result[role] = _cap_entries(entries, cap, ignored, role)

    filtered_result["ignored"] = ignored
    return filtered_result
