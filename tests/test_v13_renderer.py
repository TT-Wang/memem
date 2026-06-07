"""Tests for memem v1.13 render_slice_v2() and MEMEM_RENDER_LEGACY flag.

All assertions read the rendered MARKDOWN STRING — not the slice dict
(silent-field-drop pattern from forge memory).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_slice(**overrides):
    """Return a minimal ActiveMemorySlice-like dict suitable for rendering."""
    base: dict = {
        "should_emit_context": True,
        "query": "test query",
        "scope_id": "test-project",
        "activation_mode": "heuristic",
        "confidence": 0.75,
        "goals": [],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "open_tensions": [],
        "artifacts": [],
        "episodic_items": [],
        "skill_items": [],
        "case_items": [],
        "recent_actions": [],
    }
    base.update(overrides)
    return base


def _make_item(title: str, summary: str = "", kind: str = "", inferred_kind: str = "", role: str = "background", layer: int = 2, memory_id: str = "") -> dict:
    item: dict = {
        "title": title,
        "summary": summary or title,
        "role": role,
        "layer": layer,
        "memory_id": memory_id or title[:8].replace(" ", "_"),
        "score": 0.8,
    }
    if kind:
        item["kind"] = kind
    if inferred_kind:
        item["inferred_kind"] = inferred_kind
    return item


# ---------------------------------------------------------------------------
# Test 1: All 6 new section headers present when MEMEM_RENDER_LEGACY unset
# ---------------------------------------------------------------------------

def test_v2_all_six_section_headers_present(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    slice_obj = _make_slice(
        goals=[_make_item("Project goal", layer=0, role="goal")],
        episodic_items=[_make_item("Shipped auth module 2024-01-15", kind="episodic")],
        skill_items=[_make_item("Always use typed dicts", kind="skill")],
        case_items=[_make_item("Fixed DB connection bug", kind="case")],
        recent_actions=["ran tests", "committed code"],
        writeback_summary={
            "status": "committed",
            "proposed_count": 2,
            "auto_committed_count": 1,
            "manual_review_count": 1,
            "blocked_count": 0,
            "rejected_count": 0,
        },
    )

    rendered = render_slice_v2(slice_obj)

    assert "## Anchors" in rendered
    assert "## Episodic" in rendered
    assert "## Skills" in rendered
    assert "## Cases" in rendered
    assert "## Working" in rendered
    assert "## Pending" in rendered


# ---------------------------------------------------------------------------
# Test 2: Old section headers ABSENT when MEMEM_RENDER_LEGACY unset
# ---------------------------------------------------------------------------

def test_v2_old_section_headers_absent(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_as_prompt_context

    slice_obj = _make_slice(
        goals=[_make_item("Project goal", role="goal", layer=0)],
        constraints=[_make_item("Never break API", role="constraint")],
        active_background=[_make_item("Background info", role="background")],
        decisions=[_make_item("Chose PostgreSQL", role="decision")],
        failure_patterns=[_make_item("Race condition in auth", role="failure_pattern")],
        preferences=[_make_item("Prefer typed code", role="preference")],
    )

    rendered = render_slice_as_prompt_context(slice_obj)

    # Old headers MUST be absent
    assert "## Constraints" not in rendered
    assert "## Decisions" not in rendered
    assert "## Failure Patterns" not in rendered
    assert "## Open Tensions" not in rendered
    assert "## Preferences" not in rendered
    assert "## Active Background" not in rendered
    # Goals heading from old renderer must be absent too
    assert "## Goals" not in rendered


# ---------------------------------------------------------------------------
# Test 3: MEMEM_RENDER_LEGACY=1 restores old headers (Goals, Constraints)
# ---------------------------------------------------------------------------

def test_legacy_flag_restores_old_headers(monkeypatch):
    monkeypatch.setenv("MEMEM_RENDER_LEGACY", "1")

    from memem.active_slice import render_slice_as_prompt_context

    slice_obj = _make_slice(
        goals=[_make_item("Do the thing", role="goal", layer=0)],
        constraints=[_make_item("Use Python 3.11+", role="constraint")],
        should_emit_context=True,
        confidence=0.9,
    )

    rendered = render_slice_as_prompt_context(slice_obj)

    # Under legacy mode, at least one old header must appear
    old_headers = ["## Goals", "## Constraints", "## Decisions", "## Failure Patterns", "## Active Background"]
    assert any(h in rendered for h in old_headers), (
        f"Expected at least one old header under MEMEM_RENDER_LEGACY=1, got:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Test 4: kind='episodic' routes to ## Episodic section
# ---------------------------------------------------------------------------

def test_episodic_kind_routes_to_episodic_section(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    episodic_item = _make_item("Shipped auth module on 2024-01-15", kind="episodic", memory_id="ep001")
    slice_obj = _make_slice(episodic_items=[episodic_item])

    rendered = render_slice_v2(slice_obj)

    assert "## Episodic" in rendered
    assert "Shipped auth module" in rendered
    # Must appear AFTER the Episodic header
    episodic_pos = rendered.index("## Episodic")
    item_pos = rendered.index("Shipped auth module")
    assert item_pos > episodic_pos


# ---------------------------------------------------------------------------
# Test 5: kind='skill' routes to ## Skills section
# ---------------------------------------------------------------------------

def test_skill_kind_routes_to_skills_section(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    skill_item = _make_item("Always use TypedDict for schemas", kind="skill", memory_id="sk001")
    slice_obj = _make_slice(skill_items=[skill_item])

    rendered = render_slice_v2(slice_obj)

    assert "## Skills" in rendered
    assert "Always use TypedDict" in rendered
    skills_pos = rendered.index("## Skills")
    item_pos = rendered.index("Always use TypedDict")
    assert item_pos > skills_pos


# ---------------------------------------------------------------------------
# Test 6: kind='case' routes to ## Cases section
# ---------------------------------------------------------------------------

def test_case_kind_routes_to_cases_section(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    case_item = _make_item("Fixed DB connection timeout", kind="case", memory_id="ca001")
    slice_obj = _make_slice(case_items=[case_item])

    rendered = render_slice_v2(slice_obj)

    assert "## Cases" in rendered
    assert "Fixed DB connection timeout" in rendered
    cases_pos = rendered.index("## Cases")
    item_pos = rendered.index("Fixed DB connection timeout")
    assert item_pos > cases_pos


# ---------------------------------------------------------------------------
# Test 7: inferred_kind routes correctly (fallback path)
# ---------------------------------------------------------------------------

def test_inferred_kind_routes_to_correct_section(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    # inferred_kind='skill' via active_background
    skill_via_inferred = _make_item("Prefer async patterns", inferred_kind="skill", role="background", memory_id="ik001")
    slice_obj = _make_slice(active_background=[skill_via_inferred])

    rendered = render_slice_v2(slice_obj)

    assert "## Skills" in rendered
    assert "Prefer async patterns" in rendered


# ---------------------------------------------------------------------------
# Test 8: Empty sections are suppressed (no spurious headers)
# ---------------------------------------------------------------------------

def test_empty_sections_are_suppressed(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    # Only provide one episodic item — no skills, cases, anchors, pending
    slice_obj = _make_slice(
        episodic_items=[_make_item("Deployed v1.0", kind="episodic")],
        recent_actions=["deployed"],
    )

    rendered = render_slice_v2(slice_obj)

    assert "## Episodic" in rendered
    # Skills/Cases/Anchors/Pending should be absent (empty)
    assert "## Skills" not in rendered
    assert "## Cases" not in rendered
    assert "## Anchors" not in rendered
    assert "## Pending" not in rendered


# ---------------------------------------------------------------------------
# Test 9: Working section always shows query and scope_id
# ---------------------------------------------------------------------------

def test_working_section_shows_query_and_scope(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    slice_obj = _make_slice(
        query="implement the new feature",
        scope_id="my-project",
        recent_actions=["wrote tests", "updated docs", "pushed code"],
        episodic_items=[_make_item("Did something", kind="episodic")],  # ensure emit
    )

    rendered = render_slice_v2(slice_obj)

    assert "## Working" in rendered
    assert "implement the new feature" in rendered
    assert "my-project" in rendered


# ---------------------------------------------------------------------------
# Test 10: Pending section shown when writeback has activity
# ---------------------------------------------------------------------------

def test_pending_section_shown_when_writeback_active(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    slice_obj = _make_slice(
        episodic_items=[_make_item("Shipped thing", kind="episodic")],
        writeback_summary={
            "status": "partial",
            "proposed_count": 3,
            "auto_committed_count": 2,
            "manual_review_count": 1,
            "blocked_count": 0,
            "rejected_count": 0,
        },
    )

    rendered = render_slice_v2(slice_obj)

    assert "## Pending" in rendered
    assert "proposed=3" in rendered


# ---------------------------------------------------------------------------
# Test 11: Pending section absent when writeback not run
# ---------------------------------------------------------------------------

def test_pending_section_absent_when_no_writeback(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    slice_obj = _make_slice(
        episodic_items=[_make_item("Event 1", kind="episodic")],
        writeback_summary=None,
    )

    rendered = render_slice_v2(slice_obj)

    assert "## Pending" not in rendered


# ---------------------------------------------------------------------------
# Test 12: L0 anchor items route to Anchors section
# ---------------------------------------------------------------------------

def test_l0_items_route_to_anchors(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_v2

    l0_goal = _make_item("Build great software", role="goal", layer=0, memory_id="l0001")
    slice_obj = _make_slice(goals=[l0_goal])

    rendered = render_slice_v2(slice_obj)

    assert "## Anchors" in rendered
    assert "Build great software" in rendered


# ---------------------------------------------------------------------------
# Test 13: should_emit_context=False → empty string from both renderers
# ---------------------------------------------------------------------------

def test_should_not_emit_returns_empty(monkeypatch):
    monkeypatch.delenv("MEMEM_RENDER_LEGACY", raising=False)

    from memem.active_slice import render_slice_as_prompt_context, render_slice_v2

    slice_obj = _make_slice(
        should_emit_context=False,
        episodic_items=[_make_item("Some event", kind="episodic")],
    )

    assert render_slice_v2(slice_obj) == ""
    assert render_slice_as_prompt_context(slice_obj) == ""


# ---------------------------------------------------------------------------
# Test 14: render_slice_as_prompt_context delegates to render_slice_v2 when legacy=0
# ---------------------------------------------------------------------------

def test_render_slice_as_prompt_context_delegates_to_v2(monkeypatch):
    monkeypatch.setenv("MEMEM_RENDER_LEGACY", "0")

    from memem.active_slice import render_slice_as_prompt_context, render_slice_v2

    slice_obj = _make_slice(
        episodic_items=[_make_item("Deployed today", kind="episodic")],
        skill_items=[_make_item("Use ruff for linting", kind="skill")],
        recent_actions=["deployed", "tested"],
    )

    ctx_rendered = render_slice_as_prompt_context(slice_obj)
    v2_rendered = render_slice_v2(slice_obj)

    # Both should produce identical output
    assert ctx_rendered == v2_rendered
    assert "## Episodic" in ctx_rendered
    assert "## Skills" in ctx_rendered
