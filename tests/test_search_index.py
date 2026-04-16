"""Tests for SQLite FTS5 search index."""



def test_init_db(tmp_cortex_dir):
    from memem.search_index import _init_search_db
    conn = _init_search_db()
    conn.close()


def test_index_and_search(tmp_cortex_dir):
    from memem.search_index import _index_memory, _search_fts
    mem = {
        "id": "test-id-12345",
        "title": "JWT authentication guide",
        "essence": "Use RS256 for JWT signing",
        "project": "general",
        "domain_tags": ["auth", "jwt"],
    }
    _index_memory(mem)
    results = _search_fts("JWT", limit=10)
    assert "test-id-12345" in results


def test_remove_from_index(tmp_cortex_dir):
    from memem.search_index import _index_memory, _remove_from_index, _search_fts
    mem = {"id": "to-remove", "title": "deleteme", "essence": "body", "project": "general", "domain_tags": []}
    _index_memory(mem)
    assert "to-remove" in _search_fts("deleteme")
    _remove_from_index("to-remove")
    assert "to-remove" not in _search_fts("deleteme")


def test_fts_related_ids_roundtrip(tmp_cortex_dir):
    """The FTS5 `related_ids` column (schema v2) must round-trip a memory's
    related-IDs list so graph expansion can read them from a single MATCH
    query without a follow-up vault scan."""
    from memem.search_index import _index_memory, _search_fts_with_graph
    mem = {
        "id": "aaaaaaaa-0000-0000-0000-000000000000",
        "title": "primary",
        "essence": "something about primary topic",
        "project": "general",
        "domain_tags": [],
        "related": ["bbbbbbbb", "cccccccc"],
    }
    _index_memory(mem)
    results = _search_fts_with_graph("primary", limit=10)
    ids = {mid for mid, _ in results}
    assert mem["id"] in ids
    related_for_primary = next((rel for mid, rel in results if mid == mem["id"]), None)
    assert related_for_primary == ["bbbbbbbb", "cccccccc"], (
        f"related_ids round-trip lost data: {related_for_primary}"
    )


def test_fts_graph_matches_vault_related(tmp_vault, tmp_cortex_dir):
    """End-to-end: the related IDs returned by _search_fts_with_graph must
    match what obsidian_store would return from the parsed vault — no drift
    between FTS index and vault source of truth."""
    import importlib

    from memem import models, obsidian_store, search_index
    importlib.reload(models)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)

    topic_a = obsidian_store._make_memory(
        content="Content about async postgresql asyncpg sqlalchemy drivers",
        title="A1",
        project="general", source_type="user",
    )
    topic_b = obsidian_store._make_memory(
        content="Content about async postgresql asyncpg connection pooling",
        title="A2",
        project="general", source_type="user",
    )
    obsidian_store._save_memory(topic_a)
    obsidian_store._save_memory(topic_b)

    # After saves, both FTS and vault should have matching related_ids
    results = search_index._search_fts_with_graph("async postgresql", limit=10)
    for mid, fts_related in results:
        vault_mem = obsidian_store._find_memory(mid)
        if vault_mem is None:
            continue
        vault_related = vault_mem.get("related", [])
        assert set(fts_related) == set(vault_related), (
            f"drift for {mid[:8]}: fts={fts_related} vault={vault_related}"
        )


def test_fts_special_query_chars(tmp_cortex_dir):
    """Queries containing FTS5 operators / special chars must not raise
    or silently return bogus results. Bare AND / OR / NOT and prefix `*`
    used to either leak through as operators or trigger a swallowed
    sqlite parse error that returned []."""
    from memem.search_index import _index_memory, _search_fts
    mem = {
        "id": "tok-query-test",
        "title": "JWT auth notes",
        "essence": "Use RS256 for signing",
        "project": "general",
        "domain_tags": ["auth"],
    }
    _index_memory(mem)
    for q in ["NOT auth", "jwt OR *", "AND OR NOT", "title:jwt", 'with " quote', ""]:
        results = _search_fts(q, limit=10)
        assert isinstance(results, list), f"query {q!r} should return a list, got {type(results)}"
    assert "tok-query-test" in _search_fts("JWT auth", limit=10)
