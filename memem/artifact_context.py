"""Artifact candidate helpers for Active Memory Slice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from memem.active_slice import Candidate, normalize_artifact_candidate
from memem.models import _normalize_scope_id

_TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
_MAX_FILE_BYTES = 256_000
_MAX_CONTENT_CHARS = 1_200


def _stable_artifact_id(source_type: str, source_ref: str, title: str, scope_id: str) -> str:
    payload = {
        "project": _normalize_scope_id(scope_id),
        "source_ref": source_ref,
        "source_type": source_type,
        "title": title,
    }
    encoded = json.dumps(payload, sort_keys=True)
    return f"artifact_{hashlib.sha1(encoded.encode('utf-8')).hexdigest()[:12]}"


def _apply_stable_identity(candidate: Candidate, scope_id: str, source_ref: str) -> Candidate:
    stable_id = _stable_artifact_id(
        candidate.get("source_type", "artifact"),
        source_ref=source_ref,
        title=candidate.get("title", ""),
        scope_id=scope_id,
    )
    candidate["candidate_id"] = stable_id
    candidate["artifact_id"] = stable_id
    if source_ref:
        candidate["source_ref"] = source_ref
    return candidate


def _is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    try:
        sample = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in sample


def _read_excerpt(path: Path, limit: int = _MAX_CONTENT_CHARS) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
    except OSError:
        return ""
    if not _is_probably_text(path):
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit].strip()
    except OSError:
        return ""


def repo_file_candidate(path: str, scope_id: str, score: float = 0.6, reason: str = "repo file context") -> Candidate | None:
    file_path = Path(path).expanduser()
    if not file_path.exists() or not file_path.is_file():
        return None
    excerpt = _read_excerpt(file_path)
    if not excerpt:
        return None
    candidate = normalize_artifact_candidate(
        "repo_file",
        file_path.name,
        f"{file_path}: {excerpt}",
        path=str(file_path),
        score=score,
        project=_normalize_scope_id(scope_id),
    )
    candidate["source_reason"] = reason
    return _apply_stable_identity(candidate, scope_id=scope_id, source_ref=str(file_path))


def _synthetic_artifact(
    title: str,
    content: str,
    scope_id: str,
    score: float,
    reason: str,
    source_ref: str,
) -> Candidate:
    candidate = normalize_artifact_candidate(
        "draft",
        title,
        content,
        path=source_ref,
        score=score,
        project=_normalize_scope_id(scope_id),
    )
    candidate["source_reason"] = reason
    return _apply_stable_identity(candidate, scope_id=scope_id, source_ref=source_ref)


def _task_context_candidate(environment: dict[str, Any], scope_id: str) -> Candidate | None:
    task_mode = str(environment.get("task_mode", "") or "")
    branch = str(environment.get("branch", "") or "")
    task_name = str(environment.get("task_name", "") or "")
    repo_path = str(environment.get("repo_path", "") or "")
    stakeholder = str(environment.get("stakeholder", "") or "")
    artifact_path = str(environment.get("artifact_path", "") or "")
    current_file = str(environment.get("current_file", "") or "")
    open_files = [str(path) for path in environment.get("open_files", []) if str(path)]

    lines: list[str] = []
    if task_mode:
        lines.append(f"Mode: {task_mode}")
    if task_name:
        lines.append(f"Task: {task_name}")
    if branch:
        lines.append(f"Branch: {branch}")
    if repo_path:
        lines.append(f"Repo: {repo_path}")
    if stakeholder:
        lines.append(f"Stakeholder: {stakeholder}")
    if artifact_path:
        lines.append(f"Artifact: {artifact_path}")
    if current_file:
        lines.append(f"Current file: {current_file}")
    if open_files:
        preview = ", ".join(open_files[:3])
        if len(open_files) > 3:
            preview += f", +{len(open_files) - 3} more"
        lines.append(f"Open files: {preview}")
    has_explicit_artifact_anchor = bool(branch or task_name or artifact_path or current_file or open_files)
    if not lines or not has_explicit_artifact_anchor:
        return None

    titles = {
        "coding": "Coding context",
        "proposal": "Proposal context",
        "debug": "Debug context",
        "research": "Research context",
        "session_start": "Session context",
        "maintenance": "Maintenance context",
    }
    title = titles.get(task_mode, "Task context")
    anchor = artifact_path or current_file or branch or task_name or repo_path or _normalize_scope_id(scope_id)
    source_ref = f"context://{task_mode or 'task'}/{anchor}"
    reason = f"{task_mode or 'task'} continuity context"
    return _synthetic_artifact(title, ". ".join(lines), scope_id, 0.58, reason, source_ref)


def artifact_candidates_from_environment(environment: dict[str, Any], scope_id: str) -> list[Candidate]:
    """Build bounded artifact candidates from normalized runtime environment."""
    candidates: list[Candidate] = []
    seen_paths: set[str] = set()

    def add_file(path: str, score: float, reason: str) -> None:
        normalized = str(Path(path).expanduser())
        if not normalized or normalized in seen_paths:
            return
        seen_paths.add(normalized)
        candidate = repo_file_candidate(normalized, scope_id=scope_id, score=score, reason=reason)
        if candidate:
            candidates.append(candidate)

    current_file = environment.get("current_file")
    if isinstance(current_file, str) and current_file:
        add_file(current_file, 0.76, "current file context")

    for path in environment.get("modified_files", []):
        add_file(str(path), 0.74, "modified file context")

    for path in environment.get("open_files", []):
        add_file(str(path), 0.62, "open file context")

    artifact_path = environment.get("artifact_path")
    if isinstance(artifact_path, str) and artifact_path:
        add_file(artifact_path, 0.78, "draft artifact context")

    context_candidate = _task_context_candidate(environment, scope_id)
    if context_candidate:
        candidates.append(context_candidate)

    return candidates
