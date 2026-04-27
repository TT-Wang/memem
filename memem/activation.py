"""Activation judgement for Active Memory Slice candidates."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, cast

from memem.active_slice import (
    ActivationEntry,
    ActivationResult,
    ActivationTension,
    Candidate,
    CandidateBundle,
    flatten_candidate_bundle,
)
from memem.capabilities import assembly_available

log = logging.getLogger("memem-activation")

CROSS_PROJECT_SCORE_PENALTY: float = float(os.environ.get("MEMEM_CROSS_PROJECT_PENALTY", "0.3"))
CROSS_PROJECT_EVICT_THRESHOLD: float = float(os.environ.get("MEMEM_CROSS_PROJECT_EVICT_THRESHOLD", "0.5"))

_CONSTRAINT_CUES = {"must", "never", "requires", "require", "constraint", "blocked", "cannot", "do not", "should not"}
_FAILURE_CUES = {"bug", "failure", "regression", "failed", "issue", "risk", "loophole", "avoid"}
_DECISION_CUES = {"decided", "decision", "chosen", "selected", "use ", "architecture", "adopted"}
_PREFERENCE_CUES = {"prefer", "preference", "wants", "likes", "style"}
_OPEN_TENSION_CUES = {
    "tension",
    "unclear",
    "unresolved",
    "unknown",
    "tradeoff",
    "question",
    "fuzzy",
    "not decided",
    "still need",
    "blocker",
}
_ARTIFACT_CUES = {"file", "branch", "draft", "proposal", "implementation", "code", "document"}

_ROLE_KEYS = [
    "goals",
    "constraints",
    "background",
    "decisions",
    "preferences",
    "failure_patterns",
    "artifact_context",
    "open_tensions",
    "ignored",
]

_ACTIVATION_SYSTEM = (
    "You are selecting what should enter an AI system's current working memory. "
    "Use only the provided candidates. Foreground the items needed for the user's "
    "current work right now. Distinguish goals, constraints, decisions, failure "
    "patterns, artifacts, and unresolved tensions. Prefer concrete constraints, "
    "active artifacts, and unresolved risks over generic background when budget is "
    "tight. Identify distractors in ignored. Return strict JSON only."
)


def _entry(
    candidate: Candidate,
    why: str,
    score_boost: float = 0.0,
    *,
    centrality: float | None = None,
    role_confidence: float | None = None,
    drop_reason: str = "",
) -> ActivationEntry:
    score = min(1.0, float(candidate.get("score", 0.5) or 0.5) + score_boost)
    return {
        "candidate_id": candidate.get("candidate_id", ""),
        "memory_id": candidate.get("memory_id", ""),
        "artifact_id": candidate.get("artifact_id", ""),
        "why": why,
        "score": score,
        "centrality": float(centrality if centrality is not None else score),
        "role_confidence": float(role_confidence if role_confidence is not None else min(1.0, score)),
        "drop_reason": drop_reason,
    }


def _empty_activation_result(mode: str) -> ActivationResult:
    return {
        "goals": [],
        "constraints": [],
        "background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifact_context": [],
        "open_tensions": [],
        "ignored": [],
        "activation_mode": cast(Any, mode),
        "confidence": 0.35,
        "warnings": [],
    }


def _contains(text: str, cues: set[str]) -> bool:
    lower = text.lower()
    return any(cue in lower for cue in cues)


def _token_set(text: str) -> set[str]:
    return {token for token in text.lower().replace("/", " ").replace("_", " ").split() if token}


def _query_overlap(query: str, candidate: Candidate) -> float:
    query_terms = _token_set(query)
    candidate_terms = _token_set(
        f"{candidate.get('title', '')} {candidate.get('summary', '')} {candidate.get('content', '')}"
    )
    if not query_terms or not candidate_terms:
        return 0.0
    return len(query_terms & candidate_terms) / max(len(query_terms), 1)


def score_candidate_for_role(candidate: Candidate, role: str, query: str, environment: dict[str, Any]) -> float:
    """Role-aware scoring for heuristic activation."""
    ctype = candidate.get("candidate_type", "")
    text = f"{candidate.get('title', '')} {candidate.get('summary', '')} {candidate.get('content', '')}".lower()
    base = float(candidate.get("score", 0.5) or 0.5) * 0.45
    overlap = _query_overlap(query, candidate)
    importance = min(float(candidate.get("importance", 3) or 3) / 5.0, 1.0) * 0.12
    project_bonus = 0.08 if candidate.get("project") == environment.get("scope_id") else 0.0
    current_file = str(environment.get("current_file", ""))
    modified_files = {str(path) for path in environment.get("modified_files", [])}
    source_ref = str(candidate.get("source_ref", ""))
    artifact_match = 0.12 if source_ref and source_ref in modified_files | ({current_file} if current_file else set()) else 0.0
    task_mode = str(environment.get("task_mode", "") or "")
    continuity_focus = {str(item) for item in environment.get("continuity_focus", []) if str(item)}
    previous_constraint_ids = {str(item) for item in environment.get("previous_constraint_ids", []) if str(item)}
    previous_artifact_ids = {str(item) for item in environment.get("previous_artifact_ids", []) if str(item)}
    previous_artifact_paths = {str(item) for item in environment.get("previous_artifact_paths", []) if str(item)}
    previous_tension_terms = _token_set(" ".join(str(item) for item in environment.get("previous_open_tension_descriptions", [])))

    focus_aliases = {
        "constraints": {"constraints"},
        "decisions": {"decisions"},
        "preferences": {"preferences"},
        "failure_patterns": {"failure_patterns"},
        "artifact_context": {"artifact_context", "artifacts"},
        "background": {"background"},
        "goals": {"goals"},
    }
    focus_bonus = 0.04 if continuity_focus and focus_aliases.get(role, {role}) & continuity_focus else 0.0
    continuity_bonus = 0.0
    if role == "constraints" and candidate.get("memory_id", "") in previous_constraint_ids:
        continuity_bonus += 0.08
    if role == "artifact_context" and (
        candidate.get("artifact_id", "") in previous_artifact_ids
        or (source_ref and source_ref in previous_artifact_paths)
    ):
        continuity_bonus += 0.1
    if previous_tension_terms and _token_set(text) & previous_tension_terms and role in {"constraints", "failure_patterns", "artifact_context"}:
        continuity_bonus += 0.04

    task_mode_bonus = 0.0
    if task_mode == "coding":
        task_mode_bonus = {
            "constraints": 0.08,
            "failure_patterns": 0.1,
            "artifact_context": 0.12,
            "background": -0.03,
        }.get(role, 0.0)
    elif task_mode == "proposal":
        task_mode_bonus = {
            "goals": 0.08,
            "constraints": 0.08,
            "decisions": 0.08,
            "artifact_context": 0.05,
        }.get(role, 0.0)
    elif task_mode == "debug":
        task_mode_bonus = {
            "constraints": 0.08,
            "failure_patterns": 0.12,
            "artifact_context": 0.08,
        }.get(role, 0.0)
    elif task_mode == "research":
        task_mode_bonus = {
            "goals": 0.06,
            "background": 0.09,
            "artifact_context": 0.05,
        }.get(role, 0.0)
    elif task_mode == "maintenance":
        task_mode_bonus = {
            "constraints": 0.08,
            "artifact_context": 0.06,
            "failure_patterns": 0.05,
        }.get(role, 0.0)

    if role == "goals":
        if ctype == "current_query":
            role_score = 1.0
        else:
            role_score = min(1.0, base + importance + (overlap * 0.3) + project_bonus + focus_bonus + task_mode_bonus)
    elif role == "constraints":
        cue_bonus = 0.34 if _contains(text, _CONSTRAINT_CUES) else -0.1
        role_score = min(1.0, max(0.0, base + importance + cue_bonus + (overlap * 0.12) + project_bonus + focus_bonus + continuity_bonus + task_mode_bonus))
    elif role == "decisions":
        cue_bonus = 0.3 if _contains(text, _DECISION_CUES) else -0.08
        role_score = min(1.0, max(0.0, base + importance + cue_bonus + artifact_match + (overlap * 0.14) + project_bonus + focus_bonus + task_mode_bonus))
    elif role == "preferences":
        cue_bonus = 0.28 if _contains(text, _PREFERENCE_CUES) else -0.08
        role_score = min(1.0, max(0.0, base + cue_bonus + (overlap * 0.12) + project_bonus + focus_bonus + task_mode_bonus))
    elif role == "failure_patterns":
        cue_bonus = 0.34 if _contains(text, _FAILURE_CUES) else -0.08
        role_score = min(1.0, max(0.0, base + importance + cue_bonus + artifact_match + (overlap * 0.14) + project_bonus + focus_bonus + continuity_bonus + task_mode_bonus))
    elif role == "artifact_context":
        cue_bonus = 0.35 if ctype in {"playbook", "artifact", "transcript"} else -0.08
        role_score = min(1.0, max(0.0, base + cue_bonus + artifact_match + (overlap * 0.12) + project_bonus + focus_bonus + continuity_bonus + task_mode_bonus))
    elif role == "background":
        role_score = min(1.0, max(0.0, base + (overlap * 0.18) + (importance * 0.5) + focus_bonus + task_mode_bonus))
    else:
        role_score = base

    # Apply cross-project penalty when scope_strict mode is active.
    # Normalize both sides so renamed/aliased projects (memem ↔ cortex-plugin)
    # are treated as same-project, not cross-project.
    if (
        environment.get("scope_strict") is True
        and ctype != "current_query"
    ):
        from memem.models import _normalize_scope_id
        candidate_project = _normalize_scope_id(str(candidate.get("project", "") or ""))
        scope_project = _normalize_scope_id(str(environment.get("scope_id", "") or ""))
        if candidate_project != scope_project:
            role_score = role_score * CROSS_PROJECT_SCORE_PENALTY

    return role_score


def extract_open_tensions(
    query: str,
    candidate_bundle: CandidateBundle,
    activation_result: ActivationResult | None = None,
) -> list[ActivationTension]:
    """Heuristically surface unresolved tensions in the current work state."""
    tensions: list[ActivationTension] = []
    lookup = {
        candidate.get("candidate_id", ""): candidate
        for candidate in flatten_candidate_bundle(candidate_bundle)
        if candidate.get("candidate_id")
    }
    lowered_query = query.lower()

    def add_tension(description: str, severity: str = "medium", linked_memory_ids: list[str] | None = None, why_open: str = "") -> None:
        if not description:
            return
        entry: ActivationTension = {
            "description": description[:240],
            "severity": cast(Any, severity if severity in {"low", "medium", "high"} else "medium"),
            "linked_memory_ids": linked_memory_ids or [],
            "why_open": why_open or "heuristic tension extraction",
            "centrality": 0.6 if severity == "high" else 0.45,
            "role_confidence": 0.7 if severity == "high" else 0.55,
        }
        tensions.append(entry)

    if any(cue in lowered_query for cue in _OPEN_TENSION_CUES):
        add_tension(
            "The current request contains unresolved language or an open tradeoff.",
            severity="high" if "blocker" in lowered_query else "medium",
            why_open="query contains unresolved-language cues",
        )

    selected_constraints = cast(list[ActivationEntry], activation_result.get("constraints", []) if activation_result else [])
    selected_decisions = cast(list[ActivationEntry], activation_result.get("decisions", []) if activation_result else [])
    artifact_candidates = list(candidate_bundle.get("artifact_candidates", []))
    playbook_candidate = candidate_bundle.get("playbook_candidate")

    if selected_constraints and not selected_decisions:
        linked_memory_ids = [
            candidate.get("memory_id", "")
            for entry in selected_constraints
            for candidate in [lookup.get(entry.get("candidate_id", ""))]
            if candidate and candidate.get("memory_id")
        ]
        add_tension(
            "Constraints are active without a supporting decision or implementation memory.",
            severity="medium",
            linked_memory_ids=linked_memory_ids,
            why_open="constraint selected without supporting decision",
        )

    if query and (_contains(lowered_query, _ARTIFACT_CUES) or not (artifact_candidates or playbook_candidate)):
        env_mode = ""
        if isinstance(activation_result, dict):
            env_mode = str(activation_result.get("task_mode", ""))
        if (_contains(lowered_query, _ARTIFACT_CUES) or env_mode in {"coding", "proposal", "debug"}) and not (artifact_candidates or playbook_candidate):
            add_tension(
                "Current work lacks supporting artifact context.",
                severity="high",
                why_open="task appears artifact-driven but no artifact candidates were available",
            )

    for candidate in flatten_candidate_bundle(candidate_bundle):
        if candidate.get("candidate_type") == "current_query":
            continue
        text = f"{candidate.get('title', '')} {candidate.get('summary', '')} {candidate.get('content', '')}"
        if _contains(text, _OPEN_TENSION_CUES):
            linked_memory_ids = [candidate.get("memory_id", "")] if candidate.get("memory_id") else []
            add_tension(
                candidate.get("summary", candidate.get("title", ""))[:240],
                severity="medium",
                linked_memory_ids=linked_memory_ids,
                why_open="candidate contains unresolved-language cues",
            )

    deduped: list[ActivationTension] = []
    seen: set[str] = set()
    severity_rank = {"high": 2, "medium": 1, "low": 0}
    for tension in sorted(
        tensions,
        key=lambda item: (
            severity_rank.get(str(item.get("severity", "medium")), 1),
            float(item.get("role_confidence", 0.0) or 0.0),
            float(item.get("centrality", 0.0) or 0.0),
        ),
        reverse=True,
    ):
        key = str(tension.get("description", "")).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(tension)
    return deduped[:6]


def judge_activation_heuristically(
    query: str,
    scope_id: str,
    environment: dict[str, Any],
    candidate_bundle: CandidateBundle,
) -> ActivationResult:
    """Deterministic fallback activation judgement."""
    result = _empty_activation_result("heuristic")
    env = dict(environment or {})
    # Force-set scope_id from the explicit positional arg. setdefault is wrong
    # here because normalize_runtime_environment injects a 'general' default
    # when the caller doesn't pass scope_id in the environment dict, which
    # would silently mask the real scope and break cross-project comparisons.
    env["scope_id"] = scope_id
    candidates = flatten_candidate_bundle(candidate_bundle)
    role_map = {
        "constraints": "constraint cue matched",
        "decisions": "decision cue matched",
        "preferences": "preference cue matched",
        "failure_patterns": "failure/risk cue matched",
        "artifact_context": "artifact context",
        "background": "relevant background candidate",
    }
    role_buckets: dict[str, list[ActivationEntry]] = {
        "constraints": cast(list[ActivationEntry], result["constraints"]),
        "decisions": cast(list[ActivationEntry], result["decisions"]),
        "preferences": cast(list[ActivationEntry], result["preferences"]),
        "failure_patterns": cast(list[ActivationEntry], result["failure_patterns"]),
        "artifact_context": cast(list[ActivationEntry], result["artifact_context"]),
        "background": cast(list[ActivationEntry], result["background"]),
    }

    for candidate in candidates:
        ctype = candidate.get("candidate_type", "")
        text = f"{candidate.get('title', '')} {candidate.get('summary', '')} {candidate.get('content', '')}"
        if ctype == "current_query":
            result["goals"].append(_entry(candidate, "current user request", 0.0, centrality=1.0, role_confidence=1.0))
            continue

        if ctype == "environment":
            score = score_candidate_for_role(candidate, "background", query, env)
            result["background"].append(_entry(
                candidate,
                "runtime environment fact",
                score - float(candidate.get("score", 0.5) or 0.5),
                centrality=score,
                role_confidence=score,
            ))
            continue

        role_scores = {
            "constraints": score_candidate_for_role(candidate, "constraints", query, env),
            "decisions": score_candidate_for_role(candidate, "decisions", query, env),
            "preferences": score_candidate_for_role(candidate, "preferences", query, env),
            "failure_patterns": score_candidate_for_role(candidate, "failure_patterns", query, env),
            "artifact_context": score_candidate_for_role(candidate, "artifact_context", query, env),
            "background": score_candidate_for_role(candidate, "background", query, env),
        }
        if ctype in {"playbook", "artifact", "transcript"}:
            best_role = "artifact_context"
        elif _contains(text, _CONSTRAINT_CUES) and role_scores["constraints"] >= role_scores["failure_patterns"] - 0.1:
            best_role = "constraints"
        elif _contains(text, _FAILURE_CUES) and role_scores["failure_patterns"] > role_scores["constraints"] + 0.1:
            best_role = "failure_patterns"
        else:
            best_role = max(role_scores, key=lambda role: role_scores[role])
        best_score = role_scores[best_role]

        # Cross-project eviction (FU1): when scope_strict_evict is opt-in,
        # drop cross-project candidates whose best post-penalty role score is
        # below the eviction threshold. Normalize both sides so renamed
        # projects (memem ↔ cortex-plugin) are treated as same-project.
        if (
            env.get("scope_strict_evict") is True
            and ctype != "current_query"
            and best_score < CROSS_PROJECT_EVICT_THRESHOLD
        ):
            from memem.models import _normalize_scope_id
            candidate_project = _normalize_scope_id(str(candidate.get("project", "") or ""))
            scope_project = _normalize_scope_id(str(env.get("scope_id", "") or ""))
            if candidate_project != scope_project:
                continue

        bucket = role_buckets[best_role]
        bucket.append(_entry(
            candidate,
            role_map[best_role],
            best_score - float(candidate.get("score", 0.5) or 0.5),
            centrality=best_score,
            role_confidence=best_score,
        ))

    for role in ("constraints", "decisions", "preferences", "failure_patterns", "artifact_context", "background"):
        result[role] = sorted(
            result[role],
            key=lambda entry: (
                float(entry.get("score", 0.0) or 0.0),
                float(entry.get("role_confidence", 0.0) or 0.0),
                float(entry.get("centrality", 0.0) or 0.0),
            ),
            reverse=True,
        )

    result["open_tensions"] = extract_open_tensions(query, candidate_bundle, result)
    result["confidence"] = 0.68 if candidates else 0.35
    return result


def _bounded_candidates(candidate_bundle: CandidateBundle, max_candidates: int = 30, max_chars: int = 800) -> list[dict[str, Any]]:
    candidates = sorted(flatten_candidate_bundle(candidate_bundle), key=lambda c: c.get("score", 0.0), reverse=True)
    bounded: list[dict[str, Any]] = []
    for candidate in candidates[:max_candidates]:
        bounded.append({
            "candidate_id": candidate.get("candidate_id", ""),
            "memory_id": candidate.get("memory_id", ""),
            "artifact_id": candidate.get("artifact_id", ""),
            "candidate_type": candidate.get("candidate_type", ""),
            "title": candidate.get("title", ""),
            "summary": (candidate.get("summary") or candidate.get("content", ""))[:max_chars],
            "project": candidate.get("project", "general"),
            "score": candidate.get("score", 0.0),
        })
    return bounded


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for idx in range(start, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    if end < 0:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _sanitize_llm_result(data: dict[str, Any]) -> ActivationResult:
    result = _empty_activation_result("llm")
    for role in ("goals", "constraints", "background", "decisions", "preferences", "failure_patterns", "artifact_context"):
        raw_entries = data.get(role, []) if isinstance(data.get(role, []), list) else []
        sanitized: list[ActivationEntry] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            sanitized.append(cast(ActivationEntry, {
                "candidate_id": str(entry.get("candidate_id", "")),
                "memory_id": str(entry.get("memory_id", "")),
                "artifact_id": str(entry.get("artifact_id", "")),
                "why": str(entry.get("why", "")),
                "score": float(entry.get("score", 0.0) or 0.0),
                "centrality": float(entry.get("centrality", entry.get("score", 0.0)) or 0.0),
                "role_confidence": float(entry.get("role_confidence", entry.get("score", 0.0)) or 0.0),
                "drop_reason": str(entry.get("drop_reason", "")),
            }))
        result[role] = sanitized

    raw_tensions = data.get("open_tensions", []) if isinstance(data.get("open_tensions", []), list) else []
    tensions: list[ActivationTension] = []
    for entry in raw_tensions:
        if not isinstance(entry, dict):
            continue
        tensions.append(cast(ActivationTension, {
            "description": str(entry.get("description", "")),
            "severity": entry.get("severity", "medium"),
            "linked_memory_ids": entry.get("linked_memory_ids", []) if isinstance(entry.get("linked_memory_ids", []), list) else [],
            "why_open": str(entry.get("why_open", "")),
            "why": str(entry.get("why", "")),
            "centrality": float(entry.get("centrality", 0.0) or 0.0),
            "role_confidence": float(entry.get("role_confidence", 0.0) or 0.0),
        }))
    result["open_tensions"] = tensions
    return result


def judge_activation(
    query: str,
    scope_id: str,
    environment: dict[str, Any],
    candidate_bundle: CandidateBundle,
    *,
    use_llm: bool = True,
) -> ActivationResult:
    """Public entrypoint — try LLM, fall back to heuristic on any failure."""
    if use_llm:
        try:
            return judge_activation_with_llm(query, scope_id, environment, candidate_bundle)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM activation failed (%s); falling back to heuristic", exc)
    return judge_activation_heuristically(query, scope_id, environment, candidate_bundle)


def judge_activation_with_llm(
    query: str,
    scope_id: str,
    environment: dict[str, Any],
    candidate_bundle: CandidateBundle,
    timeout: int = 30,
) -> ActivationResult:
    """Run bounded Haiku activation, falling back to deterministic judgement."""
    if not assembly_available():
        fallback = judge_activation_heuristically(query, scope_id, environment, candidate_bundle)
        fallback["warnings"] = ["LLM activation unavailable; used heuristic activation."]
        return fallback

    bounded = _bounded_candidates(candidate_bundle)
    payload = {
        "query": query,
        "scope_id": scope_id,
        "environment": {k: environment[k] for k in sorted(environment)[:20]},
        "candidates": bounded,
        "schema": {
            "goals": [{"candidate_id": "...", "why": "...", "score": 0.9, "centrality": 0.9, "role_confidence": 0.9}],
            "constraints": [],
            "background": [],
            "decisions": [],
            "preferences": [],
            "failure_patterns": [],
            "artifact_context": [],
            "open_tensions": [{
                "description": "...",
                "severity": "medium",
                "linked_memory_ids": [],
                "why_open": "...",
                "centrality": 0.7,
                "role_confidence": 0.7,
            }],
            "ignored": [{"candidate_id": "...", "why": "...", "drop_reason": "distractor"}],
        },
    }
    prompt = json.dumps(payload, ensure_ascii=False)
    if len(prompt) > 40000:
        prompt = prompt[:40000]

    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _ACTIVATION_SYSTEM],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        fallback = judge_activation_heuristically(query, scope_id, environment, candidate_bundle)
        fallback["warnings"] = [f"LLM activation failed ({type(exc).__name__}); used heuristic activation."]
        return fallback

    if proc.returncode != 0 or not proc.stdout.strip():
        fallback = judge_activation_heuristically(query, scope_id, environment, candidate_bundle)
        fallback["warnings"] = ["LLM activation returned no usable output; used heuristic activation."]
        return fallback

    parsed = _extract_json_object(proc.stdout)
    if parsed is None:
        fallback = judge_activation_heuristically(query, scope_id, environment, candidate_bundle)
        fallback["warnings"] = ["LLM activation returned malformed JSON; used heuristic activation."]
        return fallback

    return _sanitize_llm_result(parsed)
