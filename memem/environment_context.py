"""Runtime environment normalization for Active Memory Slice."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from memem.active_slice import Candidate, normalize_environment_candidate
from memem.models import _normalize_scope_id

_TASK_MODES = {"coding", "proposal", "debug", "research", "maintenance", "session_start"}
_FIELD_SCORES = {
    "task_mode": 0.72,
    "current_file": 0.72,
    "modified_files": 0.7,
    "artifact_path": 0.7,
    "branch": 0.65,
    "task_name": 0.64,
    "stakeholder": 0.6,
    "deadline": 0.58,
    "repo_path": 0.56,
    "open_files": 0.55,
}
_FIELD_ORDER = (
    "task_mode",
    "repo_path",
    "current_file",
    "modified_files",
    "open_files",
    "artifact_path",
    "branch",
    "task_name",
    "stakeholder",
    "deadline",
)


def _normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _normalize_string(value).lower()
    return normalized in {"1", "true", "yes", "on"}


def _normalize_path_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items: Iterable[Any] = value.split(":") if ":" in value else [value]
    elif isinstance(value, Iterable):
        items = value
    else:
        items = []

    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        path = _normalize_string(item)
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def normalize_runtime_environment(environment: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize ad hoc runtime environment into a stable activation schema."""
    raw = dict(environment or {})
    normalized: dict[str, Any] = {}

    session_id = _normalize_string(raw.get("session_id"))
    if session_id:
        normalized["session_id"] = session_id

    task_mode = _normalize_string(raw.get("task_mode")).lower()
    if task_mode not in _TASK_MODES:
        task_mode = "debug" if _normalize_bool(raw.get("debug_mode")) else ("proposal" if raw.get("proposal_path") else "")
    if task_mode:
        normalized["task_mode"] = task_mode

    repo_path = _normalize_string(raw.get("repo_path") or raw.get("cwd"))
    if repo_path:
        normalized["repo_path"] = repo_path

    scope_id = _normalize_scope_id(_normalize_string(raw.get("scope_id")))
    if scope_id and scope_id != "default":
        normalized["scope_id"] = scope_id

    current_file = _normalize_string(raw.get("current_file"))
    if current_file:
        normalized["current_file"] = current_file

    open_files = _normalize_path_list(raw.get("open_files"))
    if open_files:
        normalized["open_files"] = open_files

    modified_files = _normalize_path_list(raw.get("modified_files"))
    if modified_files:
        normalized["modified_files"] = modified_files

    branch = _normalize_string(raw.get("branch"))
    if branch:
        normalized["branch"] = branch

    artifact_path = _normalize_string(
        raw.get("artifact_path") or raw.get("draft_path") or raw.get("proposal_path")
    )
    if artifact_path:
        normalized["artifact_path"] = artifact_path

    task_name = _normalize_string(raw.get("task_name"))
    if task_name:
        normalized["task_name"] = task_name

    deadline = _normalize_string(raw.get("deadline"))
    if deadline:
        normalized["deadline"] = deadline

    stakeholder = _normalize_string(raw.get("stakeholder"))
    if stakeholder:
        normalized["stakeholder"] = stakeholder

    if "include_history" in raw or "history_mode" in raw:
        normalized["include_history"] = _normalize_bool(raw.get("include_history") or raw.get("history_mode"))

    if "include_transcripts" in raw:
        normalized["include_transcripts"] = _normalize_bool(raw.get("include_transcripts"))

    return normalized


def _format_environment_value(key: str, value: Any) -> str:
    if isinstance(value, list):
        preview = ", ".join(value[:4])
        if len(value) > 4:
            preview += f", +{len(value) - 4} more"
        return preview
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    return _normalize_string(value)


def environment_candidates_from_environment(environment: dict[str, Any], scope_id: str) -> list[Candidate]:
    """Convert normalized runtime environment into activation-aware candidates."""
    normalized_scope = _normalize_scope_id(scope_id)
    candidates: list[Candidate] = []
    for key in _FIELD_ORDER:
        if key not in environment:
            continue
        value = environment[key]
        formatted = _format_environment_value(key, value)
        if not formatted:
            continue
        candidate = normalize_environment_candidate(key, formatted, score=_FIELD_SCORES.get(key, 0.5))
        candidate["project"] = normalized_scope
        candidate["source_reason"] = "normalized runtime environment"
        candidates.append(candidate)
    return candidates
