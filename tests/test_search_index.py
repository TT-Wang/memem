"""Tests for SQLite FTS5 search index."""



def test_init_db(tmp_cortex_dir):
    from search_index import _init_search_db
    conn = _init_search_db()
    conn.close()


def test_index_and_search(tmp_cortex_dir):
    from search_index import _index_memory, _search_fts
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
    from search_index import _index_memory, _remove_from_index, _search_fts
    mem = {"id": "to-remove", "title": "deleteme", "essence": "body", "project": "general", "domain_tags": []}
    _index_memory(mem)
    assert "to-remove" in _search_fts("deleteme")
    _remove_from_index("to-remove")
    assert "to-remove" not in _search_fts("deleteme")
