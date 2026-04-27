"""Regression guard: cosmetic 'candidate clusters' tension must never fire.

The heuristic that emitted "Multiple strong candidate clusters suggest an
unresolved tradeoff." was removed because it fired on retrieval-state
conditions (two semantically-divergent high-scoring candidates) rather than
real project tensions, adding noise to every slice.

This test ensures the specific string cannot re-appear in open_tensions.
"""
from memem.activation import judge_activation_heuristically
from memem.active_slice import current_query_candidate

_COSMETIC_STRING = "Multiple strong candidate clusters suggest an unresolved tradeoff."


def _make_bundle_with_divergent_candidates() -> dict:
    """Two high-score candidates with minimal term overlap (< 0.15)."""
    return {
        "current_goal_candidates": [current_query_candidate("debug the auth flow", "memem")],
        "memory_candidates": [
            {
                "candidate_id": "memory:x1",
                "candidate_type": "memory",
                "memory_id": "x1",
                "title": "Authentication middleware pattern",
                "summary": "JWT validation approach for auth middleware",
                "score": 0.92,
            },
            {
                "candidate_id": "memory:x2",
                "candidate_type": "memory",
                "memory_id": "x2",
                "title": "SQLite FTS5 indexing",
                "summary": "Full-text search optimisation for recall pipeline",
                "score": 0.88,
            },
        ],
    }


def test_divergent_clusters_do_not_emit_cosmetic_tension():
    """A slice with two strong but semantically divergent candidates must NOT
    include the cosmetic cluster-tension string in open_tensions."""
    bundle = _make_bundle_with_divergent_candidates()
    result = judge_activation_heuristically("debug the auth flow", "memem", {}, bundle)

    tensions = result.get("open_tensions", [])
    descriptions = [t.get("description", "") for t in tensions]
    assert _COSMETIC_STRING not in descriptions, (
        f"Cosmetic tension re-appeared in open_tensions: {descriptions}"
    )


def test_cosmetic_tension_absent_with_many_divergent_candidates():
    """Even with many semantically-unrelated strong candidates the string must
    not appear."""
    bundle = {
        "current_goal_candidates": [current_query_candidate("refactor miner module", "memem")],
        "memory_candidates": [
            {
                "candidate_id": f"memory:y{i}",
                "candidate_type": "memory",
                "memory_id": f"y{i}",
                "title": title,
                "summary": summary,
                "score": 0.85,
            }
            for i, (title, summary) in enumerate(
                [
                    ("Docker networking", "bridge vs host mode container networking"),
                    ("Obsidian plugin API", "workspace leaf and view registration"),
                    ("Ruff linting rules", "pyproject toml configuration for ruff checks"),
                    ("SQLite WAL mode", "write-ahead logging for concurrent reads"),
                ]
            )
        ],
    }
    result = judge_activation_heuristically("refactor miner module", "memem", {}, bundle)

    tensions = result.get("open_tensions", [])
    descriptions = [t.get("description", "") for t in tensions]
    assert _COSMETIC_STRING not in descriptions, (
        f"Cosmetic tension re-appeared in open_tensions: {descriptions}"
    )
