"""Tests for memem.kind_classifier (m2: heuristic kind backfill).

Covers:
- Tag-priority path: type:episodic / type:skill / type:case tags win
- Episodic detection: ISO date + past-tense verb
- Skill detection: imperative title + must/always/never signals
- Case detection: numbered step structure + 1. Tried: pattern
- Other fallback: no signals match
- Idempotency: calling infer_kind twice yields same result
- Ephemeral-only: inferred_kind not in tags after call
- Recall pipeline: memories returned by _search_memories have inferred_kind
- domain_tags field path (alternate tag field name)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mem(
    mid: str = "aabb1234" + "0" * 24,
    title: str = "Test Memory",
    body: str = "some content",
    tags: list[str] | None = None,
    domain_tags: list[str] | None = None,
    created_at: str = "2025-01-01T00:00:00Z",
) -> dict:
    return {
        "id": mid,
        "title": title,
        "full_record": body,
        "essence": body,
        "tags": tags or [],
        "domain_tags": domain_tags or [],
        "created_at": created_at,
        "status": "active",
        "layer": 2,
        "project": "test",
        "importance": 3,
        "source_type": "user",
    }


# ---------------------------------------------------------------------------
# 1. Tag-priority path: type:episodic tag
# ---------------------------------------------------------------------------


def test_tag_priority_episodic():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="unrelated neutral title",
        body="nothing episodic here",
        tags=["type:episodic", "project:foo"],
    )
    result = infer_kind(mem)
    assert result == "episodic"
    assert mem["inferred_kind"] == "episodic"


# ---------------------------------------------------------------------------
# 2. Tag-priority path: type:skill tag
# ---------------------------------------------------------------------------


def test_tag_priority_skill():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="something non-skill-looking",
        body="happened yesterday and was completed",  # episodic signals
        tags=["type:skill"],
    )
    result = infer_kind(mem)
    assert result == "skill"
    assert mem["inferred_kind"] == "skill"


# ---------------------------------------------------------------------------
# 3. Tag-priority path: type:case tag
# ---------------------------------------------------------------------------


def test_tag_priority_case():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="nothing case-like",
        body="just some text",
        tags=["project:bar", "type:case"],
    )
    result = infer_kind(mem)
    assert result == "case"
    assert mem["inferred_kind"] == "case"


# ---------------------------------------------------------------------------
# 4. Tag-priority path: domain_tags field (alternate field name)
# ---------------------------------------------------------------------------


def test_tag_priority_domain_tags():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="something",
        body="nothing",
        domain_tags=["type:skill", "layer:L2"],
    )
    result = infer_kind(mem)
    assert result == "skill"


# ---------------------------------------------------------------------------
# 5. Episodic detection: ISO date + past-tense verb in body
# ---------------------------------------------------------------------------


def test_heuristic_episodic_date_and_past_tense():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="Deploy fallback",
        body="On 2025-11-15 we deployed the hotfix and the error was resolved.",
    )
    result = infer_kind(mem)
    assert result == "episodic", f"expected 'episodic', got {result!r}"
    assert mem["inferred_kind"] == "episodic"


# ---------------------------------------------------------------------------
# 6. Episodic detection: past-tense verb in title
# ---------------------------------------------------------------------------


def test_heuristic_episodic_past_tense_title():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="Shipped v1.12 with embedding support on 2025-10-03",
        body="We released a new build. The pipeline completed.",
    )
    result = infer_kind(mem)
    assert result == "episodic"


# ---------------------------------------------------------------------------
# 7. Skill detection: imperative title + reusability signal
# ---------------------------------------------------------------------------


def test_heuristic_skill_imperative_and_signal():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="Always use context managers for file IO",
        body="You must close file handles. This is a convention across the codebase. "
             "Prefer the 'with' statement.",
    )
    result = infer_kind(mem)
    assert result == "skill", f"expected 'skill', got {result!r}"
    assert mem["inferred_kind"] == "skill"


# ---------------------------------------------------------------------------
# 8. Skill detection: 'never' / 'avoid' keywords
# ---------------------------------------------------------------------------


def test_heuristic_skill_avoid_keywords():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="Avoid bare except clauses in Python",
        body="Never catch BaseException unless you re-raise. Always prefer specific exceptions. "
             "This is the pattern used in this codebase.",
    )
    result = infer_kind(mem)
    assert result == "skill"


# ---------------------------------------------------------------------------
# 9. Case detection: numbered step structure (3+ steps)
# ---------------------------------------------------------------------------


def test_heuristic_case_numbered_steps():
    from memem.kind_classifier import infer_kind

    body = (
        "Debugging session for OOM error.\n"
        "1. Checked memory usage — 98% RAM used\n"
        "2. Identified the offending subprocess\n"
        "3. Added a hard memory cap to the daemon\n"
        "4. Restarted and confirmed stable"
    )
    mem = _make_mem(title="OOM debugging", body=body)
    result = infer_kind(mem)
    assert result == "case", f"expected 'case', got {result!r}"
    assert mem["inferred_kind"] == "case"


# ---------------------------------------------------------------------------
# 10. Case detection: "1. Tried:" EverMe-style pattern
# ---------------------------------------------------------------------------


def test_heuristic_case_tried_pattern():
    from memem.kind_classifier import infer_kind

    body = (
        "Problem: tests kept failing in CI.\n"
        "1. Tried: disabling the flaky test — still failing\n"
        "2. Tried: increasing timeout — no change\n"
        "Result: found race condition in fixture teardown."
    )
    mem = _make_mem(title="CI flakiness investigation", body=body)
    result = infer_kind(mem)
    assert result == "case", f"expected 'case', got {result!r}"


# ---------------------------------------------------------------------------
# 11. Other fallback: no signals match
# ---------------------------------------------------------------------------


def test_heuristic_other_fallback():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="General notes",
        body="Some generic content with no strong signals at all.",
    )
    result = infer_kind(mem)
    assert result == "other"
    assert mem["inferred_kind"] == "other"


# ---------------------------------------------------------------------------
# 12. Idempotency: calling twice yields same result
# ---------------------------------------------------------------------------


def test_infer_kind_idempotent():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="Use ruff for linting",
        body="Always run ruff before committing. This is the convention.",
    )
    result1 = infer_kind(mem)
    result2 = infer_kind(mem)
    assert result1 == result2
    assert mem["inferred_kind"] == result1


# ---------------------------------------------------------------------------
# 13. Ephemeral only: inferred_kind NOT in tags after call
# ---------------------------------------------------------------------------


def test_inferred_kind_not_written_to_tags():
    from memem.kind_classifier import infer_kind

    mem = _make_mem(
        title="Fix the broken webhook",
        body="The webhook was broken. We resolved it on 2025-09-01.",
    )
    infer_kind(mem)
    # inferred_kind should be set on the dict
    assert "inferred_kind" in mem
    # But tags should NOT be modified
    assert not any("inferred_kind" in t for t in mem.get("tags", []))
    assert not any("inferred_kind" in t for t in mem.get("domain_tags", []))


# ---------------------------------------------------------------------------
# 14. Recall pipeline: memories returned by _search_memories have inferred_kind
# ---------------------------------------------------------------------------


def test_recall_pipeline_inferred_kind_present(monkeypatch):
    """Memories returned by _search_memories must all have inferred_kind set."""
    import memem.recall as _recall

    fake_mems = [
        _make_mem(mid="cc001111" + "0" * 24, title="Alpha memory", body="alpha"),
        _make_mem(mid="cc002222" + "0" * 24, title="Beta memory", body="beta"),
    ]

    # Patch _search_memories_fts to return our fake mems (avoids DB)
    monkeypatch.setattr(_recall, "_search_memories_fts", lambda *a, **kw: list(fake_mems))
    # Patch _expand_graph to return same list (no graph expansion needed)
    monkeypatch.setattr(_recall, "_expand_graph", lambda mems, **kw: list(mems))
    # Patch telemetry / access side effects
    monkeypatch.setattr(_recall, "_record_access", lambda *a, **kw: None)
    monkeypatch.setattr(_recall, "_get_current_session_id", lambda: "")

    results = _recall._search_memories("alpha", record_access=False)
    assert len(results) > 0, "Expected at least one result"
    for mem in results:
        assert "inferred_kind" in mem, f"Memory missing inferred_kind: {mem.get('title')}"
        assert mem["inferred_kind"] in ("episodic", "skill", "case", "other")
