"""Runtime environment normalization for Active Memory Slice."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from memem.active_slice import Candidate, normalize_environment_candidate
from memem.models import _normalize_scope_id

_TASK_MODES = {"coding", "proposal", "debug", "research", "maintenance", "session_start"}
_TASK_MODE_ALIASES = {
    "code": "coding",
    "coding": "coding",
    "implementation": "coding",
    "implement": "coding",
    "debug": "debug",
    "debugging": "debug",
    "bugfix": "debug",
    "proposal": "proposal",
    "planning": "proposal",
    "research": "research",
    "researching": "research",
    "investigation": "research",
    "session_start": "session_start",
    "session-start": "session_start",
    "session start": "session_start",
    "startup": "session_start",
}
_CONTINUITY_MODES = {"off", "focused", "full"}
_CONTINUITY_MODE_ALIASES = {
    "0": "off",
    "false": "off",
    "disabled": "off",
    "none": "off",
    "off": "off",
    "1": "focused",
    "auto": "focused",
    "enabled": "focused",
    "focused": "focused",
    "on": "focused",
    "true": "focused",
    "yes": "focused",
    "complete": "full",
    "deep": "full",
    "full": "full",
}
_TASK_MODE_PRESETS: dict[str, dict[str, Any]] = {
    "coding": {
        "continuity_mode": "focused",
        "continuity_focus": ["goals", "constraints", "artifacts", "tensions"],
        "continuity_slice_limit": 6,
        "continuity_summary_limit": 4,
        "continuity_artifact_limit": 4,
        "continuity_include_resolved": True,
    },
    "proposal": {
        "continuity_mode": "full",
        "continuity_focus": ["goals", "constraints", "artifacts", "tensions"],
        "continuity_slice_limit": 8,
        "continuity_summary_limit": 5,
        "continuity_artifact_limit": 4,
        "continuity_include_resolved": True,
    },
    "debug": {
        "continuity_mode": "full",
        "continuity_focus": ["constraints", "failure_patterns", "artifacts", "tensions"],
        "continuity_slice_limit": 8,
        "continuity_summary_limit": 5,
        "continuity_artifact_limit": 4,
        "continuity_include_resolved": True,
    },
    "research": {
        "continuity_mode": "focused",
        "continuity_focus": ["goals", "background", "artifacts", "tensions"],
        "continuity_slice_limit": 5,
        "continuity_summary_limit": 4,
        "continuity_artifact_limit": 3,
        "continuity_include_resolved": True,
    },
    "maintenance": {
        "continuity_mode": "focused",
        "continuity_focus": ["constraints", "artifacts", "tensions"],
        "continuity_slice_limit": 5,
        "continuity_summary_limit": 4,
        "continuity_artifact_limit": 3,
        "continuity_include_resolved": True,
    },
    "session_start": {
        "continuity_mode": "focused",
        "continuity_focus": ["goals", "constraints", "artifacts"],
        "continuity_slice_limit": 3,
        "continuity_summary_limit": 3,
        "continuity_artifact_limit": 2,
        "continuity_include_resolved": False,
    },
}
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


def _normalize_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def _normalize_repo_path(value: Any) -> str:
    path_value = _normalize_string(value)
    if not path_value:
        return ""
    return str(Path(path_value).expanduser().resolve(strict=False))


def _normalize_path_value(value: Any, repo_path: str = "") -> str:
    path_value = _normalize_string(value)
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    if not path.is_absolute() and repo_path:
        path = Path(repo_path) / path
        return str(path.resolve(strict=False))
    if path.is_absolute():
        return str(path.resolve(strict=False))
    return str(path)


def _normalize_path_list(value: Any, repo_path: str = "") -> list[str]:
    if isinstance(value, str):
        items: Iterable[Any] = value.split(":") if ":" in value else [value]
    elif isinstance(value, Iterable):
        items = value
    else:
        items = []

    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        path = _normalize_path_value(item, repo_path)
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _normalize_task_mode(raw: dict[str, Any]) -> str:
    task_mode = _normalize_string(raw.get("task_mode")).lower().replace("-", "_")
    task_mode = _TASK_MODE_ALIASES.get(task_mode, task_mode)
    if task_mode in _TASK_MODES:
        return task_mode
    if _normalize_bool(raw.get("session_start")):
        return "session_start"
    if _normalize_bool(raw.get("debug_mode")):
        return "debug"
    if _normalize_bool(raw.get("research_mode")):
        return "research"
    if raw.get("proposal_path"):
        return "proposal"
    return ""


def _continuity_controls(raw: dict[str, Any], task_mode: str) -> dict[str, Any]:
    preset = _TASK_MODE_PRESETS.get(task_mode, {})
    controls_present = any(
        key in raw
        for key in (
            "continuity",
            "continuity_mode",
            "continuity_enabled",
            "continuity_slice_limit",
            "continuity_limit",
            "continuity_summary_limit",
            "continuity_artifact_limit",
            "continuity_include_resolved",
        )
    )
    if not task_mode and not controls_present:
        return {}

    raw_mode = _normalize_string(
        raw.get("continuity_mode", raw.get("continuity", raw.get("continuity_enabled", "")))
    ).lower()
    continuity_mode = _CONTINUITY_MODE_ALIASES.get(
        raw_mode,
        preset.get("continuity_mode", "focused"),
    )
    if continuity_mode not in _CONTINUITY_MODES:
        continuity_mode = "focused"

    focus_raw = raw.get("continuity_focus", preset.get("continuity_focus", []))
    focus = [item for item in _normalize_path_list(focus_raw) if item]
    if not focus:
        focus = list(preset.get("continuity_focus", []))

    include_resolved_default = bool(preset.get("continuity_include_resolved", True))
    if "continuity_include_resolved" in raw:
        include_resolved = _normalize_bool(raw.get("continuity_include_resolved"))
    else:
        include_resolved = include_resolved_default

    return {
        "continuity_mode": continuity_mode,
        "continuity_focus": focus,
        "continuity_slice_limit": _normalize_int(
            raw.get("continuity_slice_limit", raw.get("continuity_limit")),
            int(preset.get("continuity_slice_limit", 5) or 5),
            minimum=1,
            maximum=12,
        ),
        "continuity_summary_limit": _normalize_int(
            raw.get("continuity_summary_limit"),
            int(preset.get("continuity_summary_limit", 4) or 4),
            minimum=1,
            maximum=8,
        ),
        "continuity_artifact_limit": _normalize_int(
            raw.get("continuity_artifact_limit"),
            int(preset.get("continuity_artifact_limit", 3) or 3),
            minimum=1,
            maximum=8,
        ),
        "continuity_include_resolved": include_resolved,
    }


def normalize_runtime_environment(environment: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize ad hoc runtime environment into a stable activation schema."""
    raw = dict(environment or {})
    normalized: dict[str, Any] = {}

    session_id = _normalize_string(raw.get("session_id"))
    if session_id:
        normalized["session_id"] = session_id

    task_mode = _normalize_task_mode(raw)
    if task_mode:
        normalized["task_mode"] = task_mode
        normalized["task_mode_preset"] = task_mode

    normalized.update(_continuity_controls(raw, task_mode))

    repo_path = _normalize_repo_path(raw.get("repo_path") or raw.get("cwd"))
    if repo_path:
        normalized["repo_path"] = repo_path

    scope_id = _normalize_scope_id(_normalize_string(raw.get("scope_id")))
    if scope_id and scope_id != "default":
        normalized["scope_id"] = scope_id

    current_file = _normalize_path_value(raw.get("current_file"), repo_path)
    if current_file:
        normalized["current_file"] = current_file

    open_files = _normalize_path_list(raw.get("open_files"), repo_path)
    if open_files:
        normalized["open_files"] = open_files

    modified_files = _normalize_path_list(raw.get("modified_files"), repo_path)
    if modified_files:
        normalized["modified_files"] = modified_files

    branch = _normalize_string(raw.get("branch"))
    if branch:
        normalized["branch"] = branch

    artifact_path = _normalize_path_value(
        raw.get("artifact_path") or raw.get("draft_path") or raw.get("proposal_path"),
        repo_path,
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

    if "scope_strict" in raw:
        normalized["scope_strict"] = _normalize_bool(raw.get("scope_strict"))

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
