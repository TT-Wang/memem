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


def test_search_embedding_with_scores_returns_tuples(monkeypatch):
    """_search_embedding_with_scores must return (id, cosine) pairs.

    Regression guard: the rerank now uses the cosine score as the 6th
    signal. If this function silently regresses to returning IDs only,
    the rerank's embedding_score lookup falls back to 0.0 for every
    candidate, which kills the semantic signal in the union path.
    """
    from memem import embedding_index

    fake_ids = ["mem-a", "mem-b", "mem-c"]

    class FakeArr:
        def __init__(self, vals):
            self._vals = list(vals)
            self.shape = (len(vals),)

        def __matmul__(self, _other):
            return self

        def __getitem__(self, i):
            return self._vals[i]

        def __neg__(self):
            return FakeArr([-v for v in self._vals])

        def reshape(self, *_args, **_kwargs):
            return self

    class FakeNp:
        float32 = "f32"

        @staticmethod
        def asarray(_x, dtype=None):  # noqa: ARG004
            return FakeArr([0.0])

        @staticmethod
        def argsort(_arr):
            return [0, 1, 2]

    class FakeMatrix:
        shape = (3, 384)

        def __matmul__(self, _q):
            return FakeArr([0.91, 0.62, 0.34])

    class FakeModel:
        def encode(self, _xs, **_kwargs):
            return [[0.0]]

    monkeypatch.setattr(embedding_index, "_get_model", lambda: FakeModel())
    monkeypatch.setattr(embedding_index, "_load_index", lambda: True)
    monkeypatch.setattr(embedding_index, "_try_import", lambda: ("st", FakeNp))
    monkeypatch.setattr(embedding_index, "_index_matrix", FakeMatrix())
    monkeypatch.setattr(embedding_index, "_index_ids", fake_ids)

    results = embedding_index._search_embedding_with_scores("any query", limit=5)
    assert results == [("mem-a", 0.91), ("mem-b", 0.62), ("mem-c", 0.34)], (
        f"expected (id, cosine) tuples in descending order; got {results!r}"
    )

    # Backwards-compat: bare ID API still works.
    bare = embedding_index._search_embedding("any query", limit=5)
    assert bare == ["mem-a", "mem-b", "mem-c"]


# ---------------------------------------------------------------------------
# C2: keys field in FTS index
# ---------------------------------------------------------------------------

def test_fts_finds_memory_by_key_term(tmp_cortex_dir):
    """FTS must find a memory by a key term that appears nowhere in title or essence.

    keys piggyback on the tags FTS column — searching for a key synonym should
    surface the memory even if the synonym isn't in the visible content fields.
    """
    from memem.search_index import _index_memory, _search_fts

    mem = {
        "id": "key-fts-test-001",
        "title": "Database connection pool configuration",
        "essence": "Set pool_size=20 and max_overflow=40 for production workloads",
        "project": "general",
        "domain_tags": ["database"],
        "keys": ["pgpool", "conn-pool", "asyncpg-pool"],  # synonyms not in title/essence
    }
    _index_memory(mem)

    # Search by a key synonym that does NOT appear in title or essence
    results = _search_fts("pgpool", limit=10)
    assert "key-fts-test-001" in results, (
        "FTS should find memory by key synonym 'pgpool' piggybacked in tags column"
    )

    # Also searchable by another key
    results2 = _search_fts("asyncpg-pool", limit=10)
    assert "key-fts-test-001" in results2


def test_fts_keys_and_tags_combined(tmp_cortex_dir):
    """FTS index should find memory by both regular tags and keys."""
    from memem.search_index import _index_memory, _search_fts

    mem = {
        "id": "key-tag-combo-002",
        "title": "Auth service setup",
        "essence": "Configure the authentication service",
        "project": "general",
        "domain_tags": ["auth", "security"],
        "keys": ["jwt-auth", "bearer-token", "openid-connect"],
    }
    _index_memory(mem)

    # Find by regular tag
    assert "key-tag-combo-002" in _search_fts("security", limit=10)
    # Find by key
    assert "key-tag-combo-002" in _search_fts("bearer-token", limit=10)
    # Find by title keyword
    assert "key-tag-combo-002" in _search_fts("Auth", limit=10)


def test_fts_empty_keys_no_error(tmp_cortex_dir):
    """Memory with empty keys list indexes and searches without error."""
    from memem.search_index import _index_memory, _search_fts

    mem = {
        "id": "no-keys-mem-003",
        "title": "Memory without keys",
        "essence": "This memory has no keys field at all",
        "project": "general",
        "domain_tags": ["testing"],
    }
    # keys missing entirely — should work fine
    _index_memory(mem)
    results = _search_fts("testing", limit=10)
    assert "no-keys-mem-003" in results
