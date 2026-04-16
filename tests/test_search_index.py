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
