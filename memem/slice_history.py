"""Persistence and continuity helpers for Active Memory Slice history."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from memem import models
from memem.active_slice import ActiveMemorySlice, ActiveTension, ArtifactProgression, SliceDiff

_DEFAULT_HISTORY_LIMIT = 24


def _history_path(path: Path | None = None) -> Path:
    return path or models.ACTIVE_SLICE_HISTORY_FILE


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _history_record(slice_obj: Mapping[str, Any]) -> ActiveMemorySlice:
    return cast(ActiveMemorySlice, json.loads(json.dumps(dict(slice_obj), sort_keys=True, default=str)))


def _memory_item_key(item: Mapping[str, Any]) -> str:
    memory_id = _normalize_text(item.get("memory_id"))
    if memory_id:
        return memory_id
    title = _normalize_text(item.get("title")).casefold()
    if title:
        return f"title:{title}"
    summary = _normalize_text(item.get("summary")).casefold()
    if summary:
        return f"summary:{summary[:180]}"
    return ""


def _memory_item_label(item: Mapping[str, Any]) -> str:
    return _normalize_text(item.get("title")) or _normalize_text(item.get("summary")) or _memory_item_key(item)


def _artifact_key(artifact: Mapping[str, Any]) -> str:
    artifact_id = _normalize_text(artifact.get("artifact_id"))
    if artifact_id:
        return artifact_id
    path = _normalize_text(artifact.get("path"))
    if path:
        return f"path:{path}"
    title = _normalize_text(artifact.get("title")).casefold()
    if title:
        return f"title:{title}"
    return ""


def _artifact_label(artifact: Mapping[str, Any]) -> str:
    return _normalize_text(artifact.get("path")) or _normalize_text(artifact.get("title")) or _artifact_key(artifact)


def _artifact_id(artifact: Mapping[str, Any]) -> str:
    return _normalize_text(artifact.get("artifact_id"))


def _tension_key(tension: Mapping[str, Any]) -> str:
    tension_id = _normalize_text(tension.get("tension_id"))
    if tension_id:
        return tension_id
    description = _normalize_text(tension.get("description")).casefold()
    if not description:
        return ""
    linked = sorted(_normalize_text(item)[:8] for item in tension.get("linked_memory_ids", []) if _normalize_text(item))
    if linked:
        return f"{description}|{'|'.join(linked)}"
    return description


def _tension_label(tension: Mapping[str, Any]) -> str:
    return _normalize_text(tension.get("description")) or _tension_key(tension)


def _normalized_tension(tension: Mapping[str, Any]) -> ActiveTension:
    severity = _normalize_text(tension.get("severity")).lower()
    if severity not in {"low", "medium", "high"}:
        severity = "medium"
    linked = [
        _normalize_text(item)
        for item in tension.get("linked_memory_ids", [])
        if _normalize_text(item)
    ]
    return {
        "tension_id": _normalize_text(tension.get("tension_id")),
        "description": _normalize_text(tension.get("description")),
        "severity": cast(Any, severity),
        "linked_memory_ids": linked,
        "why_open": _normalize_text(tension.get("why_open")),
    }


def _ordered_labels(
    items: Sequence[Mapping[str, Any]],
    key_fn: Any,
    label_fn: Any,
) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        key = key_fn(item)
        label = label_fn(item)
        if not key or not label or key in seen:
            continue
        seen.add(key)
        ordered.append((key, label))
    return ordered


def persist_slice_history(
    slice_obj: ActiveMemorySlice,
    *,
    history_file: Path | None = None,
    max_records: int = _DEFAULT_HISTORY_LIMIT,
) -> list[ActiveMemorySlice]:
    """Persist a bounded JSONL slice history outside the Obsidian vault."""
    path = _history_path(history_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    tmp_path = path.with_suffix(".tmp")
    fd = open(lock_path, "w")
    # v1.8.1: 0600 file perms — slice records contain full user query text.
    # Use post-creation chmod instead of process-global umask to avoid races
    # in concurrent test runs (umask is per-process, not per-thread).
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        records = load_slice_history(history_file=path, limit=0)
        records.append(_history_record(slice_obj))
        if max_records > 0:
            records = records[-max_records:]
        try:
            with open(tmp_path, "w", encoding="utf-8") as out:
                for record in records:
                    out.write(json.dumps(record, sort_keys=True, default=str))
                    out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, path)
        except Exception:
            # Clean up the orphan .tmp before another lock holder reuses
            # the fixed path. Without this, an interrupted write leaves a
            # truncated file that the next caller would treat as a half-
            # finished commit.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return records
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def load_slice_history(
    *,
    history_file: Path | None = None,
    scope_id: str = "",
    session_id: str = "",
    limit: int = 10,
) -> list[ActiveMemorySlice]:
    """Load bounded slice history records, optionally filtered by scope or session."""
    path = _history_path(history_file)
    if not path.exists():
        return []

    normalized_scope = models._normalize_scope_id(scope_id) if scope_id else ""
    records: list[ActiveMemorySlice] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    entry = cast(ActiveMemorySlice, json.loads(raw))
                except json.JSONDecodeError:
                    continue
                if normalized_scope and models._normalize_scope_id(str(entry.get("scope_id", ""))) != normalized_scope:
                    continue
                if session_id and str(entry.get("session_id", "")) != session_id:
                    continue
                records.append(entry)
    except OSError:
        return []

    if limit > 0:
        return records[-limit:]
    return records


def resolve_slice_tensions(
    previous_slice: Mapping[str, Any] | None,
    current_slice: Mapping[str, Any],
) -> tuple[list[ActiveTension], list[ActiveTension]]:
    """Return resolved and still-open tensions across adjacent slices."""
    previous_tensions = _list_of_dicts(previous_slice.get("open_tensions", []) if previous_slice else [])
    current_open = _list_of_dicts(current_slice.get("open_tensions", []))
    current_explicit_resolved = _list_of_dicts(current_slice.get("resolved_tensions", []))

    current_open_keys = {_tension_key(tension) for tension in current_open if _tension_key(tension)}
    resolved: list[ActiveTension] = []
    seen_resolved: set[str] = set()

    for tension in previous_tensions:
        key = _tension_key(tension)
        if not key or key in current_open_keys or key in seen_resolved:
            continue
        seen_resolved.add(key)
        resolved.append(_normalized_tension(tension))

    for tension in current_explicit_resolved:
        key = _tension_key(tension)
        if not key or key in current_open_keys or key in seen_resolved:
            continue
        seen_resolved.add(key)
        resolved.append(_normalized_tension(tension))

    still_open: list[ActiveTension] = []
    seen_open: set[str] = set()
    for tension in current_open:
        key = _tension_key(tension)
        if not key or key in seen_open:
            continue
        seen_open.add(key)
        still_open.append(_normalized_tension(tension))

    return resolved, still_open


def diff_adjacent_slices(
    previous_slice: Mapping[str, Any] | None,
    current_slice: Mapping[str, Any],
) -> SliceDiff:
    """Compute working-state diffs between two adjacent slices."""
    previous_goals = _ordered_labels(
        _list_of_dicts(previous_slice.get("goals", []) if previous_slice else []),
        _memory_item_key,
        _memory_item_label,
    )
    current_goals = _ordered_labels(_list_of_dicts(current_slice.get("goals", [])), _memory_item_key, _memory_item_label)
    previous_constraints = _ordered_labels(
        _list_of_dicts(previous_slice.get("constraints", []) if previous_slice else []),
        _memory_item_key,
        _memory_item_label,
    )
    current_constraints = _ordered_labels(
        _list_of_dicts(current_slice.get("constraints", [])),
        _memory_item_key,
        _memory_item_label,
    )
    previous_artifacts = _ordered_labels(
        _list_of_dicts(previous_slice.get("artifacts", []) if previous_slice else []),
        _artifact_key,
        _artifact_label,
    )
    current_artifacts = _ordered_labels(
        _list_of_dicts(current_slice.get("artifacts", [])),
        _artifact_key,
        _artifact_label,
    )
    previous_tensions = _ordered_labels(
        _list_of_dicts(previous_slice.get("open_tensions", []) if previous_slice else []),
        _tension_key,
        _tension_label,
    )
    current_tensions = _ordered_labels(_list_of_dicts(current_slice.get("open_tensions", [])), _tension_key, _tension_label)
    resolved_tensions, _ = resolve_slice_tensions(previous_slice, current_slice)
    resolved_pairs = _ordered_labels(resolved_tensions, _tension_key, _tension_label)

    previous_goal_keys = {key for key, _ in previous_goals}
    current_goal_keys = {key for key, _ in current_goals}
    previous_constraint_keys = {key for key, _ in previous_constraints}
    current_constraint_keys = {key for key, _ in current_constraints}
    previous_artifact_keys = {key for key, _ in previous_artifacts}
    current_artifact_keys = {key for key, _ in current_artifacts}
    previous_tension_keys = {key for key, _ in previous_tensions}

    previous_confidence = float(previous_slice.get("confidence", 0.0) or 0.0) if previous_slice else 0.0
    current_confidence = float(current_slice.get("confidence", 0.0) or 0.0)
    previous_activation_mode = _normalize_text(previous_slice.get("activation_mode")) if previous_slice else ""

    return {
        "new_goals": [label for key, label in current_goals if key not in previous_goal_keys],
        "dropped_goals": [label for key, label in previous_goals if key not in current_goal_keys],
        "new_constraints": [label for key, label in current_constraints if key not in previous_constraint_keys],
        "resolved_constraints": [label for key, label in previous_constraints if key not in current_constraint_keys],
        "new_tensions": [label for key, label in current_tensions if key not in previous_tension_keys],
        "resolved_tensions": [label for _, label in resolved_pairs],
        "new_artifacts": [label for key, label in current_artifacts if key not in previous_artifact_keys],
        "dropped_artifacts": [label for key, label in previous_artifacts if key not in current_artifact_keys],
        "activation_mode_changed": bool(previous_slice)
        and previous_activation_mode != _normalize_text(current_slice.get("activation_mode")),
        "confidence_delta": round(current_confidence - previous_confidence, 3),
    }


def summarize_artifact_progression(
    previous_slice: Mapping[str, Any] | None,
    current_slice: Mapping[str, Any],
) -> ArtifactProgression:
    """Summarize how artifact context progressed across adjacent slices."""
    previous_artifacts = _list_of_dicts(previous_slice.get("artifacts", []) if previous_slice else [])
    current_artifacts = _list_of_dicts(current_slice.get("artifacts", []))
    previous_pairs = _ordered_labels(previous_artifacts, _artifact_key, _artifact_label)
    current_pairs = _ordered_labels(current_artifacts, _artifact_key, _artifact_label)
    previous_compare_keys = [key for key, _ in previous_pairs]
    current_compare_keys = [key for key, _ in current_pairs]
    previous_ids = [_artifact_id(artifact) for artifact in previous_artifacts if _artifact_id(artifact)]
    current_ids = [_artifact_id(artifact) for artifact in current_artifacts if _artifact_id(artifact)]
    previous_set = set(previous_compare_keys)
    current_set = set(current_compare_keys)
    shared_keys = [key for key in current_compare_keys if key in previous_set]
    changed = previous_compare_keys != current_compare_keys

    previous_summaries = {
        _artifact_key(artifact): _normalize_text(artifact.get("summary"))
        for artifact in previous_artifacts
        if _artifact_key(artifact)
    }
    current_summaries = {
        _artifact_key(artifact): _normalize_text(artifact.get("summary"))
        for artifact in current_artifacts
        if _artifact_key(artifact)
    }
    summary_changed = any(current_summaries.get(key, "") != previous_summaries.get(key, "") for key in shared_keys)
    resolved_tensions, still_open_tensions = resolve_slice_tensions(previous_slice, current_slice)

    signals: list[str] = []
    if current_set - previous_set:
        signals.append("artifact_added")
    if previous_set - current_set:
        signals.append("artifact_removed")
    if shared_keys:
        signals.append("artifact_retained")
    if summary_changed:
        signals.append("artifact_summary_updated")
    if resolved_tensions:
        signals.append("tension_resolved")
    if still_open_tensions:
        signals.append("tension_open")

    if not current_compare_keys:
        stage = "none"
    elif not previous_compare_keys:
        stage = "discovered"
    elif changed or summary_changed:
        stage = "revising" if shared_keys else "drafting"
    elif not still_open_tensions:
        stage = "review_ready"
    else:
        stage = "stalled"

    retained_labels = [label for key, label in current_pairs if key in previous_set][:2]
    added_labels = [label for key, label in current_pairs if key not in previous_set][:2]
    if stage == "none":
        summary = "No artifact context is currently tracked."
    elif stage == "discovered":
        summary = f"Tracking {len(current_compare_keys)} artifact(s) for the first active slice."
    elif stage == "drafting":
        summary = f"Artifact context shifted to {len(current_compare_keys)} tracked item(s)."
    elif stage == "revising":
        detail = retained_labels[0] if retained_labels else str(len(shared_keys))
        summary = f"Revising ongoing artifact work around {detail}."
    elif stage == "review_ready":
        summary = "Artifact context is stable and no open tensions remain."
    else:
        summary = "Artifact context is unchanged while open tensions still need resolution."
    if added_labels:
        summary += f" Added: {', '.join(added_labels)}."

    return {
        "stage": cast(Any, stage),
        "summary": summary,
        "current_artifact_ids": current_ids,
        "previous_artifact_ids": previous_ids,
        "changed": bool(changed or summary_changed),
        "signals": signals,
    }


def summarize_carry_forward(
    previous_slice: Mapping[str, Any] | None,
    current_slice: Mapping[str, Any],
    *,
    max_items: int = 4,
) -> list[str]:
    """Summarize continuity items that remain active across slices."""
    if not previous_slice or max_items <= 0:
        return []

    lines: list[str] = []
    seen: set[str] = set()

    previous_constraints = {
        key
        for key, _ in _ordered_labels(_list_of_dicts(previous_slice.get("constraints", [])), _memory_item_key, _memory_item_label)
    }
    for key, label in _ordered_labels(_list_of_dicts(current_slice.get("constraints", [])), _memory_item_key, _memory_item_label):
        if key in previous_constraints:
            entry = f"Constraint: {label}"
            if entry not in seen:
                seen.add(entry)
                lines.append(entry)

    previous_tensions = {
        key
        for key, _ in _ordered_labels(_list_of_dicts(previous_slice.get("open_tensions", [])), _tension_key, _tension_label)
    }
    for key, label in _ordered_labels(_list_of_dicts(current_slice.get("open_tensions", [])), _tension_key, _tension_label):
        if key in previous_tensions:
            entry = f"Open tension: {label}"
            if entry not in seen:
                seen.add(entry)
                lines.append(entry)

    previous_artifacts = {
        key
        for key, _ in _ordered_labels(_list_of_dicts(previous_slice.get("artifacts", [])), _artifact_key, _artifact_label)
    }
    for key, label in _ordered_labels(_list_of_dicts(current_slice.get("artifacts", [])), _artifact_key, _artifact_label):
        if key in previous_artifacts:
            entry = f"Artifact in progress: {label}"
            if entry not in seen:
                seen.add(entry)
                lines.append(entry)

    return lines[:max_items]


def annotate_slice_continuity(
    current_slice: ActiveMemorySlice,
    previous_slice: Mapping[str, Any] | None,
    *,
    carry_forward_limit: int = 4,
) -> ActiveMemorySlice:
    """Populate continuity fields on a slice without changing engine entrypoints."""
    enriched = _history_record(current_slice)
    resolved_tensions, _ = resolve_slice_tensions(previous_slice, enriched)
    enriched["previous_slice_id"] = _normalize_text(previous_slice.get("slice_id")) if previous_slice else ""
    enriched["resolved_tensions"] = resolved_tensions
    enriched["slice_diff"] = diff_adjacent_slices(previous_slice, enriched)
    enriched["carry_forward_summary"] = summarize_carry_forward(
        previous_slice,
        enriched,
        max_items=carry_forward_limit,
    )
    enriched["artifact_progression"] = summarize_artifact_progression(previous_slice, enriched)
    return enriched


# ---------------------------------------------------------------------------
# Per-session in-memory counters (turn count + empty-streak backoff)
# ---------------------------------------------------------------------------
# State is intentionally in-memory only — the daemon process holds it for the
# lifetime of the session. No disk persistence needed.

_session_lock: threading.Lock = threading.Lock()
_turn_counts: dict[str, int] = {}
_empty_streaks: dict[str, int] = {}


def get_session_turn_count(session_id: str) -> int:
    """Return the current turn count for *session_id* (0 if unseen)."""
    with _session_lock:
        return _turn_counts.get(session_id, 0)


def increment_turn_count(session_id: str) -> int:
    """Increment and return the new turn count for *session_id*."""
    with _session_lock:
        count = _turn_counts.get(session_id, 0) + 1
        _turn_counts[session_id] = count
        return count


def get_empty_streak(session_id: str) -> int:
    """Return the current consecutive-empty-result streak for *session_id* (0 if none)."""
    with _session_lock:
        return _empty_streaks.get(session_id, 0)


def increment_empty_streak(session_id: str) -> int:
    """Increment and return the new empty streak for *session_id*."""
    with _session_lock:
        streak = _empty_streaks.get(session_id, 0) + 1
        _empty_streaks[session_id] = streak
        return streak


def reset_empty_streak(session_id: str) -> None:
    """Reset the empty streak for *session_id* to zero."""
    with _session_lock:
        _empty_streaks[session_id] = 0
