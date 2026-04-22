"""Tests for typed/scored memory graph side index."""

import importlib


def _reload_graph_stack():
    from memem import graph_index, models, obsidian_store

    importlib.reload(models)
    importlib.reload(graph_index)
    importlib.reload(obsidian_store)
    return graph_index, obsidian_store


def test_graph_edge_roundtrip(tmp_cortex_dir):
    graph_index, _obsidian_store = _reload_graph_stack()

    graph_index._upsert_edge(
        "aaaaaaaa-0000-0000-0000-000000000000",
        "bbbbbbbb-0000-0000-0000-000000000000",
        "same_topic",
        0.82,
        {"lexical": 0.7},
    )

    outgoing = graph_index._neighbors("aaaaaaaa", relation_types={"same_topic"})
    incoming = graph_index._reverse_neighbors("bbbbbbbb", relation_types={"same_topic"})

    assert len(outgoing) == 1
    assert outgoing[0]["dst_id"].startswith("bbbbbbbb")
    assert outgoing[0]["relation_type"] == "same_topic"
    assert outgoing[0]["score"] == 0.82
    assert outgoing[0]["evidence"]["lexical"] == 0.7
    assert incoming[0]["src_id"].startswith("aaaaaaaa")


def test_rebuild_graph_creates_typed_edges(tmp_vault, tmp_cortex_dir):
    graph_index, obsidian_store = _reload_graph_stack()

    m1 = obsidian_store._make_memory(
        content="Memem uses SQLite FTS5 search with Obsidian markdown as the durable source of truth.",
        title="Memem FTS source of truth",
        project="memem",
        source_type="user",
        tags=["architecture", "search"],
    )
    m2 = obsidian_store._make_memory(
        content="Memem search uses SQLite FTS5 indexes derived from Obsidian markdown memory files.",
        title="Memem search index architecture",
        project="memem",
        source_type="user",
        tags=["architecture", "search"],
    )
    m3 = obsidian_store._make_memory(
        content="Lexie opportunity scanning uses manual scan buttons and deadline-sensitive recommendations.",
        title="Lexie opportunity scanning",
        project="lexie",
        source_type="user",
        tags=["product"],
    )
    obsidian_store._save_memory(m1)
    obsidian_store._save_memory(m2)
    obsidian_store._save_memory(m3)

    count = graph_index._rebuild_graph()
    assert count >= 2

    edges = graph_index._neighbors(m1["id"], relation_types={"same_topic", "supports"}, limit=10)
    assert any(edge["dst_id"].startswith(m2["id"][:8]) for edge in edges)
    assert not any(edge["dst_id"].startswith(m3["id"][:8]) for edge in edges)
    reverse_edges = graph_index._neighbors(m2["id"], relation_types={"same_topic"}, limit=10)
    assert any(edge["dst_id"].startswith(m1["id"][:8]) for edge in reverse_edges)


def test_save_memory_updates_graph_and_related_frontmatter(tmp_vault, tmp_cortex_dir):
    graph_index, obsidian_store = _reload_graph_stack()

    first = obsidian_store._make_memory(
        content="Graph recall should prefer typed same-topic memory edges over raw related links.",
        title="Typed graph recall",
        project="memem",
        source_type="user",
        tags=["graph", "recall"],
    )
    second = obsidian_store._make_memory(
        content="Related memory graph recall uses typed same-topic edges and scored neighbors.",
        title="Scored graph neighbors",
        project="memem",
        source_type="user",
        tags=["graph", "recall"],
    )

    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    refreshed = obsidian_store._find_memory(second["id"])
    assert first["id"][:8] in refreshed.get("related", [])

    edges = graph_index._neighbors(second["id"], relation_types={"same_topic", "supports"}, limit=10)
    assert any(edge["dst_id"].startswith(first["id"][:8]) for edge in edges)


def test_update_memory_removes_stale_graph_edges(tmp_vault, tmp_cortex_dir):
    graph_index, obsidian_store = _reload_graph_stack()

    anchor = obsidian_store._make_memory(
        content="PostgreSQL asyncpg SQLAlchemy database pooling is the selected backend architecture.",
        title="Database backend",
        project="memem",
        source_type="user",
        tags=["database"],
    )
    changing = obsidian_store._make_memory(
        content="The backend uses PostgreSQL asyncpg SQLAlchemy database pooling for persistence.",
        title="Database persistence",
        project="memem",
        source_type="user",
        tags=["database"],
    )
    obsidian_store._save_memory(anchor)
    obsidian_store._save_memory(changing)
    assert graph_index._neighbors(changing["id"], relation_types={"same_topic", "supports"})

    obsidian_store._update_memory(
        changing["id"],
        "Terminal copy mode uses tmux mouse settings and modifier drag behavior for selection.",
        "Terminal selection behavior",
    )

    stale_edges = [
        edge for edge in graph_index._neighbors(changing["id"], relation_types={"same_topic", "supports"}, limit=20)
        if edge["dst_id"].startswith(anchor["id"][:8])
    ]
    reverse_stale_edges = [
        edge for edge in graph_index._reverse_neighbors(
            changing["id"], relation_types={"same_topic", "supports"}, limit=20
        )
        if edge["src_id"].startswith(anchor["id"][:8])
    ]
    assert stale_edges == []
    assert reverse_stale_edges == []


def test_graph_audit_reports_dead_edges(tmp_vault, tmp_cortex_dir):
    graph_index, obsidian_store = _reload_graph_stack()

    mem = obsidian_store._make_memory(
        content="Graph audit should detect edge destinations that no longer exist in the vault.",
        title="Graph audit dead links",
        project="memem",
        source_type="user",
    )
    obsidian_store._save_memory(mem)
    graph_index._upsert_edge(mem["id"], "ffffffff-0000-0000-0000-000000000000", "same_topic", 0.7)

    audit = graph_index._audit_graph()
    assert audit["dead_links"]
