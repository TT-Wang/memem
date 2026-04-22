"""Activation judgement for Active Memory Slice candidates."""

from __future__ import annotations

import json
import logging
import subprocess

from memem.active_slice import flatten_candidate_bundle
from memem.capabilities import assembly_available

log = logging.getLogger("memem-activation")

_CONSTRAINT_CUES = {"must", "never", "requires", "require", "constraint", "blocked", "cannot", "do not", "should not"}
_FAILURE_CUES = {"bug", "failure", "regression", "failed", "fix", "issue", "risk", "loophole", "avoid"}
_DECISION_CUES = {"decided", "decision", "chosen", "selected", "use ", "architecture", "adopted"}
_PREFERENCE_CUES = {"prefer", "preference", "wants", "likes", "style"}
_OPEN_TENSION_CUES = {"tension", "unclear", "unresolved", "unknown", "tradeoff", "question", "fuzzy"}

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
    "Use only the provided candidates. Do not restate the whole vault. Assign selected "
    "items to roles, exclude semantic duplicates, prefer constraints and unresolved risks "
    "over generic background if budget is tight, and return strict JSON only."
)


def _entry(candidate: dict, why: str, score_boost: float = 0.0) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id", ""),
        "memory_id": candidate.get("memory_id", ""),
        "artifact_id": candidate.get("artifact_id", ""),
        "why": why,
        "score": min(1.0, float(candidate.get("score", 0.5) or 0.5) + score_boost),
    }


def _contains(text: str, cues: set[str]) -> bool:
    lower = text.lower()
    return any(cue in lower for cue in cues)


def judge_activation_heuristically(
    query: str,
    scope_id: str,
    environment: dict,
    candidate_bundle: dict,
) -> dict:
    """Deterministic fallback activation judgement."""
    result = {key: [] for key in _ROLE_KEYS}
    candidates = flatten_candidate_bundle(candidate_bundle)

    for candidate in candidates:
        ctype = candidate.get("candidate_type", "")
        text = f"{candidate.get('title', '')} {candidate.get('summary', '')} {candidate.get('content', '')}"
        if ctype == "current_query":
            result["goals"].append(_entry(candidate, "current user request", 0.2))
        elif ctype in {"playbook", "artifact", "transcript"}:
            result["artifact_context"].append(_entry(candidate, f"{ctype} context", 0.05))
        elif ctype == "environment":
            result["background"].append(_entry(candidate, "runtime environment fact"))
        elif _contains(text, _CONSTRAINT_CUES):
            result["constraints"].append(_entry(candidate, "constraint cue matched", 0.1))
        elif _contains(text, _FAILURE_CUES):
            result["failure_patterns"].append(_entry(candidate, "failure/risk cue matched", 0.1))
        elif _contains(text, _DECISION_CUES):
            result["decisions"].append(_entry(candidate, "decision cue matched", 0.05))
        elif _contains(text, _PREFERENCE_CUES):
            result["preferences"].append(_entry(candidate, "preference cue matched", 0.05))
        else:
            result["background"].append(_entry(candidate, "relevant background candidate"))

        if _contains(text, _OPEN_TENSION_CUES) and ctype != "current_query":
            result["open_tensions"].append({
                "description": candidate.get("summary", candidate.get("title", ""))[:240],
                "severity": "medium",
                "linked_memory_ids": [candidate.get("memory_id", "")] if candidate.get("memory_id") else [],
                "why_open": "unresolved/tension cue matched",
            })

    result["activation_mode"] = "heuristic"
    result["confidence"] = 0.62 if candidates else 0.35
    result["warnings"] = []
    return result


def _bounded_candidates(candidate_bundle: dict, max_candidates: int = 30, max_chars: int = 800) -> list[dict]:
    candidates = sorted(flatten_candidate_bundle(candidate_bundle), key=lambda c: c.get("score", 0.0), reverse=True)
    bounded = []
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


def _extract_json_object(text: str) -> dict | None:
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


def _sanitize_llm_result(data: dict) -> dict:
    result = {key: [] for key in _ROLE_KEYS}
    for key in _ROLE_KEYS:
        value = data.get(key, [])
        result[key] = value if isinstance(value, list) else []
    result["activation_mode"] = "llm"
    result["confidence"] = 0.78
    result["warnings"] = []
    return result


def judge_activation_with_llm(
    query: str,
    scope_id: str,
    environment: dict,
    candidate_bundle: dict,
    timeout: int = 30,
) -> dict:
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
            "goals": [{"candidate_id": "...", "why": "...", "score": 0.9}],
            "constraints": [],
            "background": [],
            "decisions": [],
            "preferences": [],
            "failure_patterns": [],
            "artifact_context": [],
            "open_tensions": [{"description": "...", "severity": "medium", "linked_memory_ids": [], "why_open": "..."}],
            "ignored": [{"candidate_id": "...", "why": "..."}],
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
