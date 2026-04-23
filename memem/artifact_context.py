"""Artifact candidate helpers for Active Memory Slice."""

from __future__ import annotations

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
    return candidate


def _synthetic_artifact(title: str, content: str, scope_id: str, score: float, reason: str) -> Candidate:
    candidate = normalize_artifact_candidate(
        "draft",
        title,
        content,
        score=score,
        project=_normalize_scope_id(scope_id),
    )
    candidate["source_reason"] = reason
    return candidate


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

    branch = environment.get("branch")
    if isinstance(branch, str) and branch:
        content = f"Current branch: {branch}"
        task_name = environment.get("task_name")
        if isinstance(task_name, str) and task_name:
            content += f". Task: {task_name}"
        candidates.append(_synthetic_artifact("Branch context", content, scope_id, 0.58, "branch/task artifact proxy"))
    elif isinstance(environment.get("task_name"), str) and environment["task_name"]:
        candidates.append(_synthetic_artifact(
            "Task context",
            f"Current task: {environment['task_name']}",
            scope_id,
            0.55,
            "task artifact proxy",
        ))

    return candidates
