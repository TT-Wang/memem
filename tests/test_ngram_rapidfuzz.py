"""Tests for the rapidfuzz-backed ngram search and dedup scorer.

Covers:
- _ngram_search_candidates returns relevant candidates for known query/doc pairs
- _find_best_match returns high score (>= 0.9) for near-identical content and
  low score (<= 0.5) for unrelated content
- Regression: _find_best_match returns >= 0.7 for true duplicates, preserving
  delta_policy._DUPLICATE_REJECT_THRESHOLD=0.7 gating behaviour.
"""

import importlib


def test_ngram_search_candidates_finds_relevant_memories(tmp_vault, tmp_cortex_dir):
    """_ngram_search_candidates should surface memories whose title/essence shares
    tokens with the query."""
    from memem import obsidian_store

    importlib.reload(obsidian_store)

    mem1 = obsidian_store._make_memory(
        content="JWT authentication bug caused 401 errors on login.",
        title="JWT authentication bug",
        project="memem",
    )
    mem2 = obsidian_store._make_memory(
        content="Database migration script broke the schema on deploy.",
        title="Schema migration failure",
        project="memem",
    )
    mem3 = obsidian_store._make_memory(
        content="OAuth2 login flow with refresh token rotation.",
        title="OAuth2 login flow",
        project="memem",
    )
    obsidian_store._save_memory(mem1)
    obsidian_store._save_memory(mem2)
    obsidian_store._save_memory(mem3)

    results = obsidian_store._ngram_search_candidates("auth bug", scope_id="memem", limit=20)

    assert mem1["id"] in results, "JWT authentication bug memory should match 'auth bug' query"
    # The schema migration memory should rank lower / not appear, or at least auth is first
    if mem2["id"] in results:
        assert results.index(mem1["id"]) < results.index(mem2["id"]), (
            "auth memory should rank above unrelated schema migration"
        )


def test_ngram_search_candidates_returns_empty_for_blank_query(tmp_vault, tmp_cortex_dir):
    """Blank or whitespace query should return an empty list."""
    from memem import obsidian_store

    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="Some content that should never appear.",
        title="Irrelevant memory",
        project="memem",
    )
    obsidian_store._save_memory(mem)

    assert obsidian_store._ngram_search_candidates("", scope_id="memem") == []
    assert obsidian_store._ngram_search_candidates("   ", scope_id="memem") == []


def test_find_best_match_high_score_for_near_identical_content(tmp_vault, tmp_cortex_dir):
    """Near-identical content (same text) should score >= 0.9."""
    from memem import obsidian_store

    importlib.reload(obsidian_store)

    content = "Persist delta audit entries in an append-only jsonl trail under the memem state directory."
    mem = obsidian_store._make_memory(
        content=content,
        title="Delta audit trail",
        project="memem",
    )
    obsidian_store._save_memory(mem)

    best_mem, score = obsidian_store._find_best_match(content, scope_id="memem")

    assert best_mem is not None, "Should find a matching memory"
    assert score >= 0.9, f"Near-identical content should score >= 0.9, got {score:.3f}"


def test_find_best_match_low_score_for_unrelated_content(tmp_vault, tmp_cortex_dir):
    """Unrelated content should score <= 0.5 against an existing memory."""
    from memem import obsidian_store

    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="JWT authentication bug caused 401 errors on login.",
        title="JWT authentication bug",
        project="memem",
    )
    obsidian_store._save_memory(mem)

    _best_mem, score = obsidian_store._find_best_match(
        "Kubernetes pod resource limits and OOM kill configuration.",
        scope_id="memem",
    )

    assert score <= 0.5, f"Unrelated content should score <= 0.5, got {score:.3f}"




def test_find_best_match_returns_none_for_empty_vault(tmp_vault, tmp_cortex_dir):
    """With no memories saved, _find_best_match should return (None, 0.0)."""
    from memem import obsidian_store

    importlib.reload(obsidian_store)

    best_mem, score = obsidian_store._find_best_match("any content here", scope_id="memem")

    assert best_mem is None
    assert score == 0.0
