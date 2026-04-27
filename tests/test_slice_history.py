"""Tests for Active Memory Slice history persistence and diffing."""

from __future__ import annotations


def _slice(
    slice_id: str,
    *,
    goal_titles: list[str] | None = None,
    constraint_titles: list[str] | None = None,
    artifact_entries: list[dict[str, str]] | None = None,
    tensions: list[dict[str, object]] | None = None,
    confidence: float = 0.6,
    activation_mode: str = "heuristic",
) -> dict:
    return {
        "slice_id": slice_id,
        "session_id": "session-1",
        "scope_id": "memem",
        "generated_at": "2026-04-24T00:00:00+00:00",
        "goals": [{"title": title, "summary": title, "source_type": "current_query"} for title in (goal_titles or [])],
        "constraints": [
            {
                "title": title,
                "summary": title,
                "memory_id": f"mem-{'-'.join(title.lower().split())}",
            }
            for title in (constraint_titles or [])
        ],
        "artifacts": artifact_entries or [],
        "open_tensions": tensions or [],
        "resolved_tensions": [],
        "task_mode": "coding",
        "confidence": confidence,
        "activation_mode": activation_mode,
    }


def test_persist_and_load_slice_history_is_bounded(tmp_cortex_dir):
    from memem import models, slice_history

    first = _slice("slice-1", goal_titles=["Plan rollout"])
    second = _slice("slice-2", goal_titles=["Implement rollout"])
    third = _slice("slice-3", goal_titles=["Verify rollout"])

    slice_history.persist_slice_history(first, max_records=2)
    slice_history.persist_slice_history(second, max_records=2)
    stored = slice_history.persist_slice_history(third, max_records=2)
    loaded = slice_history.load_slice_history(scope_id="memem", session_id="session-1", limit=10)

    assert models.ACTIVE_SLICE_HISTORY_FILE.parent == tmp_cortex_dir
    assert [entry["slice_id"] for entry in stored] == ["slice-2", "slice-3"]
    assert [entry["slice_id"] for entry in loaded] == ["slice-2", "slice-3"]


def test_diff_adjacent_slices_tracks_working_state_changes():
    from memem.slice_history import annotate_slice_continuity

    previous = _slice(
        "slice-1",
        goal_titles=["Plan rollout", "Draft approval note"],
        constraint_titles=["Keep auth safe", "Run pytest"],
        artifact_entries=[
            {"artifact_id": "artifact_a", "title": "proposal.md", "path": "/repo/proposal.md", "summary": "Draft v1"},
            {"artifact_id": "artifact_b", "title": "tests.py", "path": "/repo/tests.py", "summary": "Old tests"},
        ],
        tensions=[
            {"tension_id": "tension-scope", "description": "Rollout scope unresolved", "severity": "high"},
            {"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"},
        ],
        confidence=0.55,
        activation_mode="heuristic",
    )
    current = _slice(
        "slice-2",
        goal_titles=["Plan rollout", "Ship final approval note"],
        constraint_titles=["Keep auth safe", "Record release notes"],
        artifact_entries=[
            {"artifact_id": "artifact_a", "title": "proposal.md", "path": "/repo/proposal.md", "summary": "Draft v2"},
            {"artifact_id": "artifact_c", "title": "release.md", "path": "/repo/release.md", "summary": "Release notes"},
        ],
        tensions=[
            {"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"},
            {"tension_id": "tension-risk", "description": "Migration risk unverified", "severity": "medium"},
        ],
        confidence=0.82,
        activation_mode="hybrid",
    )

    annotated = annotate_slice_continuity(current, previous)
    diff = annotated["slice_diff"]

    assert annotated["previous_slice_id"] == "slice-1"
    assert diff["new_goals"] == ["Ship final approval note"]
    assert diff["dropped_goals"] == ["Draft approval note"]
    assert diff["new_constraints"] == ["Record release notes"]
    assert diff["resolved_constraints"] == ["Run pytest"]
    assert diff["new_tensions"] == ["Migration risk unverified"]
    assert diff["resolved_tensions"] == ["Rollout scope unresolved"]
    assert diff["new_artifacts"] == ["/repo/release.md"]
    assert diff["dropped_artifacts"] == ["/repo/tests.py"]
    assert diff["activation_mode_changed"] is True
    assert diff["confidence_delta"] == 0.27
    assert [tension["description"] for tension in annotated["resolved_tensions"]] == ["Rollout scope unresolved"]
    assert annotated["artifact_progression"]["stage"] == "revising"
    assert annotated["artifact_progression"]["previous_artifact_ids"] == ["artifact_a", "artifact_b"]
    assert annotated["artifact_progression"]["current_artifact_ids"] == ["artifact_a", "artifact_c"]
    assert annotated["artifact_progression"]["signals"] == [
        "artifact_added",
        "artifact_removed",
        "artifact_retained",
        "artifact_summary_updated",
        "tension_resolved",
        "tension_open",
    ]


def test_explicit_resolved_tensions_do_not_duplicate_open_tensions():
    from memem.slice_history import annotate_slice_continuity

    previous = _slice(
        "slice-1",
        tensions=[
            {"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"},
            {"tension_id": "tension-risk", "description": "Migration risk unverified", "severity": "medium"},
        ],
    )
    current = _slice(
        "slice-2",
        tensions=[
            {"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"},
        ],
    )
    current["resolved_tensions"] = [
        {"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"},
        {"tension_id": "tension-risk", "description": "Migration risk unverified", "severity": "medium"},
    ]

    annotated = annotate_slice_continuity(current, previous)

    assert [tension["description"] for tension in annotated["open_tensions"]] == ["Owner unclear"]
    assert [tension["description"] for tension in annotated["resolved_tensions"]] == ["Migration risk unverified"]


def test_persist_slice_history_cleans_orphan_tmp_on_failure(tmp_cortex_dir, monkeypatch):
    """If os.replace fails mid-persist, the .tmp file must be unlinked so a
    later writer is not preempted by an in-progress orphan. H2 guard."""
    import pytest

    from memem import models, slice_history

    first = _slice("slice-1", goal_titles=["Persist once"])
    slice_history.persist_slice_history(first, max_records=4)

    history_path = models.ACTIVE_SLICE_HISTORY_FILE
    tmp_path = history_path.with_suffix(".tmp")

    def boom(*_args, **_kwargs):
        raise OSError("simulated cross-device replace failure")

    monkeypatch.setattr(slice_history.os, "replace", boom)

    with pytest.raises(OSError, match="simulated"):
        slice_history.persist_slice_history(_slice("slice-2"), max_records=4)

    assert not tmp_path.exists(), "orphan .tmp must be cleaned up after a failed replace"


def test_persist_slice_history_serializes_concurrent_writers(tmp_cortex_dir):
    """Two threads writing simultaneously must each land a record without
    corruption. Exercises the fcntl LOCK_EX guard."""
    import threading

    from memem import slice_history

    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def writer(i: int) -> None:
        barrier.wait()
        try:
            slice_history.persist_slice_history(
                _slice(f"slice-thread-{i}", goal_titles=[f"Goal {i}"]),
                max_records=64,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writers must not error: {errors}"
    loaded = slice_history.load_slice_history(scope_id="memem", session_id="session-1", limit=64)
    slice_ids = {entry["slice_id"] for entry in loaded}
    for i in range(4):
        assert f"slice-thread-{i}" in slice_ids
