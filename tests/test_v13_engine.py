"""Integration tests for m4: v1.13 engine kind-bucket wiring.

Tests verify that _populate_v13_kind_buckets() correctly routes kind-classified
items into episodic_items / skill_items / case_items, and that render_slice_v2()
produces markdown with items in the right sections.

Forge memory: tests read the rendered MARKDOWN STRING — never just the dict.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from memem.active_slice import (
    ActiveMemorySlice,
    render_slice_as_prompt_context,
    render_slice_v2,
)
from memem.active_slice_engine import _populate_v13_kind_buckets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slice_with_items(
    episodic_item: dict | None = None,
    skill_item: dict | None = None,
    case_item: dict | None = None,
    l0_item: dict | None = None,
    other_item: dict | None = None,
) -> ActiveMemorySlice:
    """Build a minimal ActiveMemorySlice with specified items pre-routed into goals/constraints/etc."""
    goals: list[Any] = []
    constraints: list[Any] = []
    active_background: list[Any] = []
    decisions: list[Any] = []
    preferences: list[Any] = []
    failure_patterns: list[Any] = []

    if l0_item:
        goals.append(l0_item)
    if episodic_item:
        goals.append(episodic_item)
    if skill_item:
        active_background.append(skill_item)
    if case_item:
        decisions.append(case_item)
    if other_item:
        constraints.append(other_item)

    slice_obj: ActiveMemorySlice = {  # type: ignore[typeddict-item]
        "slice_id": "test-slice",
        "scope_id": "test",
        "query": "test query",
        "input_goal": "test query",
        "generated_at": "2026-06-07T00:00:00Z",
        "goals": goals,
        "constraints": constraints,
        "active_background": active_background,
        "decisions": decisions,
        "preferences": preferences,
        "failure_patterns": failure_patterns,
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "delta_results": [],
        "writeback_summary": {
            "status": "not_run",
            "dry_run": True,
            "proposed_count": 0,
            "auto_committed_count": 0,
            "manual_review_count": 0,
            "blocked_count": 0,
            "rejected_count": 0,
        },
        "candidate_count": 0,
        "recall_candidate_count": 0,
        "should_emit_context": True,
        "activation_mode": "heuristic",
        "confidence": 0.8,
        "warnings": [],
        "items": [],
        "slice_kind": "active",
        "episodic_items": [],
        "skill_items": [],
        "case_items": [],
        "recent_actions": [],
    }
    return slice_obj


def _episodic_item(memory_id: str = "ep1") -> dict:
    return {
        "memory_id": memory_id,
        "role": "goal",
        "title": "Discovered bug on 2026-05-01",
        "summary": "Fixed a crash in the pipeline on 2026-05-01",
        "source_type": "memory",
        "project": "test",
        "importance": 3,
        "layer": 2,
        "score": 0.8,
        "why_activated": "recent event",
        "inferred_kind": "episodic",
    }


def _skill_item(memory_id: str = "sk1") -> dict:
    return {
        "memory_id": memory_id,
        "role": "background",
        "title": "Always use absolute paths",
        "summary": "Must use absolute paths in bash commands",
        "source_type": "memory",
        "project": "test",
        "importance": 3,
        "layer": 2,
        "score": 0.7,
        "why_activated": "coding pattern",
        "inferred_kind": "skill",
    }


def _case_item(memory_id: str = "ca1") -> dict:
    return {
        "memory_id": memory_id,
        "role": "decision",
        "title": "Debug session: fixed OOM issue",
        "summary": "Problem: OOM. Approach: profiling. Result: fixed.",
        "source_type": "memory",
        "project": "test",
        "importance": 3,
        "layer": 2,
        "score": 0.75,
        "why_activated": "relevant case",
        "inferred_kind": "case",
    }


def _l0_anchor_item(memory_id: str = "l0_anchor") -> dict:
    return {
        "memory_id": memory_id,
        "role": "goal",
        "title": "Project: memem",
        "summary": "memem is an AI memory system",
        "source_type": "memory",
        "project": "test",
        "importance": 5,
        "layer": 0,  # L0 anchor
        "score": 1.0,
        "why_activated": "L0 always active",
    }


def _other_item(memory_id: str = "ot1") -> dict:
    return {
        "memory_id": memory_id,
        "role": "constraint",
        "title": "X Y Z",
        "summary": "abc def ghi jkl mno",
        "source_type": "memory",
        "project": "test",
        "importance": 2,
        "layer": 2,
        "score": 0.5,
        "why_activated": "misc",
    }


# ---------------------------------------------------------------------------
# Tests: kind-bucket population (dict checks + rendered markdown checks)
# ---------------------------------------------------------------------------


class TestPopulateV13KindBuckets:
    """Tests for _populate_v13_kind_buckets directly."""

    def test_episodic_item_routes_to_episodic_items(self, monkeypatch):
        """Item with inferred_kind='episodic' appears in episodic_items."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        with patch("memem.active_slice_engine._wm", create=True), \
             patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_file:
            mock_file_obj = MagicMock()
            mock_file_obj.exists.return_value = False
            mock_file = mock_file_obj  # noqa: F841
            slice_obj = _make_slice_with_items(episodic_item=_episodic_item())
            _populate_v13_kind_buckets(slice_obj)

        assert len(slice_obj["episodic_items"]) == 1
        assert slice_obj["episodic_items"][0]["memory_id"] == "ep1"
        assert slice_obj["skill_items"] == []
        assert slice_obj["case_items"] == []

        # ALSO check rendered markdown string (forge constraint: dict checks alone are insufficient)
        rendered = render_slice_v2(slice_obj)
        assert "## Episodic" in rendered

    def test_skill_item_routes_to_skill_items(self, monkeypatch):
        """Item with inferred_kind='skill' appears in skill_items."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items(skill_item=_skill_item())
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        assert len(slice_obj["skill_items"]) == 1
        assert slice_obj["skill_items"][0]["memory_id"] == "sk1"

        # Check rendered markdown
        rendered = render_slice_v2(slice_obj)
        assert "## Skills" in rendered
        assert "Always use absolute paths" in rendered

    def test_case_item_routes_to_case_items(self, monkeypatch):
        """Item with inferred_kind='case' appears in case_items."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items(case_item=_case_item())
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        assert len(slice_obj["case_items"]) == 1
        assert slice_obj["case_items"][0]["memory_id"] == "ca1"

        # Check rendered markdown
        rendered = render_slice_v2(slice_obj)
        assert "## Cases" in rendered

    def test_l0_anchor_excluded_from_kind_buckets(self, monkeypatch):
        """L0 anchor (layer=0) is NOT placed in episodic/skill/case buckets."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items(l0_item=_l0_anchor_item())
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        assert slice_obj["episodic_items"] == []
        assert slice_obj["skill_items"] == []
        assert slice_obj["case_items"] == []

        # L0 item should appear under Anchors in rendered markdown, not kind sections
        rendered = render_slice_v2(slice_obj)
        assert "## Episodic" not in rendered
        assert "## Skills" not in rendered
        assert "## Cases" not in rendered

    def test_legacy_mode_skips_routing(self, monkeypatch):
        """MEMEM_RENDER_LEGACY=1 skips kind routing; all kind buckets are empty."""
        monkeypatch.setenv("MEMEM_RENDER_LEGACY", "1")
        slice_obj = _make_slice_with_items(
            episodic_item=_episodic_item(),
            skill_item=_skill_item(),
        )
        _populate_v13_kind_buckets(slice_obj)

        # Buckets should be empty (routing skipped)
        assert slice_obj["episodic_items"] == []
        assert slice_obj["skill_items"] == []
        assert slice_obj["case_items"] == []

        # Legacy renderer should produce old headers (MEMEM_RENDER_LEGACY=1)
        rendered = render_slice_as_prompt_context(slice_obj)
        assert "## Goals" in rendered or "## Constraints" in rendered or "## Active Background" in rendered

    def test_other_kind_stays_in_source_section(self, monkeypatch):
        """Items with inferred_kind='other' (or no kind) stay in their source sections."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        other = _other_item()
        # Remove any inferred_kind to force heuristic
        other.pop("inferred_kind", None)
        slice_obj = _make_slice_with_items(other_item=other)
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        # 'other' stays in constraints — not moved to kind buckets
        assert slice_obj["episodic_items"] == []
        assert slice_obj["skill_items"] == []
        assert slice_obj["case_items"] == []
        # The constraint item is still in the slice
        assert any(item["memory_id"] == "ot1" for item in slice_obj.get("constraints", []))

    def test_existing_goals_field_preserved(self, monkeypatch):
        """Backward compat: 'goals' field still present after routing pass."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items(
            episodic_item=_episodic_item(),
            l0_item=_l0_anchor_item(),
        )
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        # goals field still present and unchanged
        assert "goals" in slice_obj
        assert isinstance(slice_obj["goals"], list)

    def test_mixed_items_correctly_partitioned(self, monkeypatch):
        """Multiple items across kinds are correctly partitioned into all three buckets."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items(
            episodic_item=_episodic_item("ep_a"),
            skill_item=_skill_item("sk_b"),
            case_item=_case_item("ca_c"),
        )
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        assert any(item["memory_id"] == "ep_a" for item in slice_obj["episodic_items"])
        assert any(item["memory_id"] == "sk_b" for item in slice_obj["skill_items"])
        assert any(item["memory_id"] == "ca_c" for item in slice_obj["case_items"])

        # All three sections must appear in rendered markdown
        rendered = render_slice_v2(slice_obj)
        assert "## Episodic" in rendered
        assert "## Skills" in rendered
        assert "## Cases" in rendered

    def test_no_duplicate_items_across_buckets(self, monkeypatch):
        """An item that appears in goals and active_background is not duplicated across buckets."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        item = _episodic_item("shared_id")
        slice_obj: ActiveMemorySlice = {  # type: ignore[typeddict-item]
            "slice_id": "test",
            "scope_id": "test",
            "query": "test",
            "input_goal": "test",
            "generated_at": "2026-06-07T00:00:00Z",
            "goals": [item],
            "constraints": [],
            "active_background": [item],  # same item appears in two sections
            "decisions": [],
            "preferences": [],
            "failure_patterns": [],
            "artifacts": [],
            "open_tensions": [],
            "resolved_tensions": [],
            "excluded_candidates": [],
            "candidate_deltas": [],
            "delta_results": [],
            "writeback_summary": {
                "status": "not_run", "dry_run": True, "proposed_count": 0,
                "auto_committed_count": 0, "manual_review_count": 0,
                "blocked_count": 0, "rejected_count": 0,
            },
            "candidate_count": 0,
            "recall_candidate_count": 0,
            "should_emit_context": True,
            "activation_mode": "heuristic",
            "confidence": 0.8,
            "warnings": [],
            "items": [],
            "slice_kind": "active",
            "episodic_items": [],
            "skill_items": [],
            "case_items": [],
            "recent_actions": [],
        }
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        # Should appear only once in episodic_items
        episodic_ids = [item["memory_id"] for item in slice_obj["episodic_items"]]
        assert episodic_ids.count("shared_id") == 1

    def test_rendered_working_section_always_present(self, monkeypatch):
        """## Working section always appears (has query + scope_id) even with empty kind buckets."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items()
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        rendered = render_slice_v2(slice_obj)
        assert "## Working" in rendered

    def test_slice_has_all_three_kind_bucket_keys(self, monkeypatch):
        """After _populate_v13_kind_buckets(), slice always has all three bucket keys."""
        monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)
        slice_obj = _make_slice_with_items()
        with patch("memem.working_memory.WORKING_MEMORY_FILE") as mock_wm:
            mock_wm.exists.return_value = False
            _populate_v13_kind_buckets(slice_obj)

        assert "episodic_items" in slice_obj
        assert "skill_items" in slice_obj
        assert "case_items" in slice_obj
        assert isinstance(slice_obj["episodic_items"], list)
        assert isinstance(slice_obj["skill_items"], list)
        assert isinstance(slice_obj["case_items"], list)
