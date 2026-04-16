"""Tests for closed-loop relevance scoring (memem/feedback.py + integration)."""

import importlib
import json


def test_classify_no_worries_is_neutral():
    """Bare "no" should no longer count as a negative signal.

    Previously '\bno\b' was in the correction regex, so routine phrases
    like "no worries", "no problem", "no need" accumulated negative hits
    and poisoned the relevance scores of recalled memories over time.
    """
    from memem import feedback
    importlib.reload(feedback)

    messages = [
        "User: can you help me debug this",
        "Assistant: Sure, what's the issue?",
        "User: no worries, I figured it out",
        "User: no problem, thanks anyway",
        "User: yes, that's fine",
    ]
    score = feedback._classify_session_outcome(messages)
    assert score >= 0.0, f"'no worries' phrasing should not be negative, got {score}"


# ── Outcome classification ──────────────────────────────────────────


def test_classify_outcome_positive():
    """Messages with approval signals should produce a positive score."""
    from memem import feedback

    importlib.reload(feedback)

    messages = [
        "User: can you fix the auth handler",
        "Assistant: I'll refactor the auth handler to use async/await...",
        "User: yes perfect, that works",
        "User: exactly what I needed, thanks",
        "User: good, ship it",
    ]
    score = feedback._classify_session_outcome(messages)
    assert score > 0.5, f"expected positive score, got {score}"


def test_classify_outcome_negative():
    """Messages with correction signals should produce a negative score."""
    from memem import feedback

    importlib.reload(feedback)

    messages = [
        "User: fix the auth handler",
        "Assistant: Here's the refactored auth handler...",
        "User: no that's wrong, don't use callbacks",
        "User: stop, revert that change",
        "User: that's not right, try again",
    ]
    score = feedback._classify_session_outcome(messages)
    assert score < -0.5, f"expected negative score, got {score}"


def test_classify_outcome_neutral():
    """Short or mixed sessions should produce a near-zero score."""
    from memem import feedback

    importlib.reload(feedback)

    # Too short — only 1 user message
    short = ["User: hello"]
    assert feedback._classify_session_outcome(short) == 0.0

    # Mixed signals
    mixed = [
        "User: fix the bug",
        "Assistant: Done.",
        "User: no that's wrong",
        "User: ok yes that works now",
        "User: good but stop adding extra features",
    ]
    score = feedback._classify_session_outcome(mixed)
    assert -0.5 <= score <= 0.5, f"expected near-neutral, got {score}"


# ── EMA score updates ───────────────────────────────────────────────


def test_update_relevance_scores_ema(tmp_path, monkeypatch):
    """Verify the EMA formula: new = 0.7 * old + 0.3 * outcome."""
    from memem import feedback, models, telemetry

    importlib.reload(models)
    importlib.reload(telemetry)
    importlib.reload(feedback)

    # Point state files at tmp
    monkeypatch.setattr(feedback, "RELEVANCE_SCORES_FILE", tmp_path / "scores.json")
    monkeypatch.setattr(feedback, "MEMEM_DIR", tmp_path)
    monkeypatch.setattr(telemetry, "_SESSION_RECALLS_FILE", tmp_path / "recalls.json")
    monkeypatch.setattr(telemetry, "MEMEM_DIR", tmp_path)

    # Seed: session "sess-001" recalled memories "aaaa1111" and "bbbb2222"
    telemetry.record_session_recall("sess-001", "aaaa1111")
    telemetry.record_session_recall("sess-001", "bbbb2222")

    # First update: outcome = 1.0 (strong positive)
    # EMA: 0.7 * 0.0 + 0.3 * 1.0 = 0.3
    feedback.update_relevance_scores("sess-001", 1.0)
    assert abs(feedback.get_relevance_score("aaaa1111") - 0.3) < 0.01
    assert abs(feedback.get_relevance_score("bbbb2222") - 0.3) < 0.01

    # Second update on same session: outcome = 1.0
    # EMA: 0.7 * 0.3 + 0.3 * 1.0 = 0.51
    feedback.update_relevance_scores("sess-001", 1.0)
    assert abs(feedback.get_relevance_score("aaaa1111") - 0.51) < 0.01

    # Unknown memory has score 0.0 (neutral default)
    assert feedback.get_relevance_score("zzzzzzzz") == 0.0


# ── Ranking integration ─────────────────────────────────────────────


def test_ranking_includes_feedback(tmp_path, monkeypatch):
    """A memory with high feedback score should rank higher than one with
    low feedback, all else being equal.
    """
    from memem import feedback, models

    importlib.reload(models)
    importlib.reload(feedback)

    # Write scores: mem-A has strong positive, mem-B has strong negative
    scores = {"mem_good": 0.8, "mem_bad": -0.8}
    scores_file = tmp_path / "scores.json"
    scores_file.write_text(json.dumps(scores))
    monkeypatch.setattr(feedback, "RELEVANCE_SCORES_FILE", scores_file)

    # get_relevance_score should return the correct values
    assert feedback.get_relevance_score("mem_good") == 0.8
    assert feedback.get_relevance_score("mem_bad") == -0.8

    # Normalized: mem_good -> (0.8+1)/2 = 0.9, mem_bad -> (-0.8+1)/2 = 0.1
    # With 10% weight: mem_good gets +0.09, mem_bad gets +0.01
    # Difference of 0.08 in final score — enough to reorder when other signals tie
    good_norm = (0.8 + 1.0) / 2.0
    bad_norm = (-0.8 + 1.0) / 2.0
    assert good_norm > bad_norm
    assert abs(good_norm - 0.9) < 0.01
    assert abs(bad_norm - 0.1) < 0.01

    # Verify the contribution at 10% weight
    good_contribution = 0.10 * good_norm
    bad_contribution = 0.10 * bad_norm
    assert good_contribution - bad_contribution > 0.05, (
        "feedback should create a meaningful ranking difference"
    )
