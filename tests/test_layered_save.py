"""End-to-end tests for save-side layer behavior (v2.8.0 layer retirement).

Verifies:
1. memory_save without layer param stores memory with NO layer field (retired).
2. memory_save with explicit layer=0 stores layer 0.
3. memory_save with explicit layer=3 stores layer 3.
4. memory_save with invalid layer returns an error string.
5. New memories written without layer param have no layer field in the dict.
"""

import importlib


def _reload_all(monkeypatch=None):
    """Reload memem modules so env vars take effect."""
    from memem import models, obsidian_store, search_index, telemetry
    importlib.reload(models)
    importlib.reload(telemetry)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)


def test_memory_save_default_no_layer_field(tmp_vault, tmp_cortex_dir):
    """v2.8.0: Saving without layer param results in a memory with NO layer field."""
    _reload_all()
    from memem.obsidian_store import _obsidian_memories
    from memem.operations import memory_save

    result = memory_save(
        content="Use asyncpg for all PostgreSQL connections — it is significantly faster than psycopg2.",
        title="asyncpg preferred over psycopg2",
        scope_id="default",
    )
    assert "Memory saved" in result or "Memory already exists" in result

    mems = _obsidian_memories()
    saved = [m for m in mems if "asyncpg" in m.get("essence", "")]
    assert len(saved) >= 1, f"Expected saved memory but got: {result}"
    # v2.8.0: layer field must be absent when not explicitly passed
    assert "layer" not in saved[0], (
        f"Expected no layer field (retired), got layer={saved[0].get('layer')}"
    )


def test_memory_save_explicit_layer_honored(tmp_vault, tmp_cortex_dir):
    """Saving with layer=0 stores layer 0 in the memory record."""
    _reload_all()
    from memem.obsidian_store import _obsidian_memories
    from memem.operations import memory_save

    content = "my-app uses Postgres + Redis for persistence and caching."
    result = memory_save(
        content=content,
        title="my-app storage identity",
        scope_id="my-app",
        layer=0,
    )
    assert "Memory saved" in result or "Memory already exists" in result

    mems = _obsidian_memories()
    saved = [m for m in mems if "Postgres" in m.get("essence", "")]
    assert len(saved) >= 1, f"Expected saved memory but got: {result}"
    assert saved[0]["layer"] == 0, f"Expected layer 0, got {saved[0]['layer']}"


def test_memory_save_l3_archival(tmp_vault, tmp_cortex_dir):
    """Saving with layer=3 stores layer 3 in the memory record."""
    _reload_all()
    from memem.obsidian_store import _obsidian_memories
    from memem.operations import memory_save

    content = "One rare edge case: asyncio.run() inside a Jupyter cell raises RuntimeError."
    result = memory_save(
        content=content,
        title="asyncio.run Jupyter edge case",
        scope_id="default",
        layer=3,
    )
    assert "Memory saved" in result or "Memory already exists" in result

    mems = _obsidian_memories()
    saved = [m for m in mems if "asyncio" in m.get("essence", "")]
    assert len(saved) >= 1, f"Expected saved memory but got: {result}"
    assert saved[0]["layer"] == 3, f"Expected layer 3, got {saved[0]['layer']}"


def test_memory_save_invalid_layer_rejected(tmp_vault, tmp_cortex_dir):
    """Saving with layer=5 should return a rejection string, not crash."""
    _reload_all()
    from memem.operations import memory_save

    result = memory_save(
        content="This content would be valid if the layer were not out of range.",
        title="Invalid layer test",
        scope_id="default",
        layer=5,
    )
    # The ValueError from _make_memory is caught and returned as a string
    assert "rejected" in result.lower() or "layer" in result.lower(), (
        f"Expected rejection message, got: {result!r}"
    )


def test_absent_layer_new_memories(tmp_vault, tmp_cortex_dir):
    """v2.8.0: new memories without explicit layer have no layer field (retired)."""
    # DELETED(v2.8): layer system retired — classify_layer no longer fires.
    # Rewritten to assert the NEW behavior: no auto-classify; layer field absent.
    _reload_all()
    from memem.obsidian_store import _make_memory, _obsidian_memories, _save_memory

    # No layer param passed — layer field must be absent in new memories
    m_high = _make_memory(
        content="memem uses Obsidian vault as the primary storage backend for memories.",
        title="memem architecture — Obsidian vault",
        project="memem",
        source_type="user",
        importance=5,
        tags=["architecture", "storage"],
    )
    assert "layer" not in m_high, (
        f"Expected no layer field from _make_memory (retired), got layer={m_high.get('layer')}"
    )
    _save_memory(m_high)

    m_generic = _make_memory(
        content="Always write tests before shipping: TDD prevents regression bugs.",
        title="TDD convention — tests before shipping",
        project="general",
        source_type="user",
        importance=4,
    )
    assert "layer" not in m_generic
    _save_memory(m_generic)

    m_low = _make_memory(
        content="Use black --line-length 88 for formatting in this project.",
        title="black formatting line length",
        project="my-project",
        source_type="user",
        importance=2,
    )
    assert "layer" not in m_low
    _save_memory(m_low)

    # All saved memories should have no layer field (absent = retired)
    mems = _obsidian_memories()
    for m in mems:
        assert "layer" not in m, (
            f"Memory '{m.get('title')}' unexpectedly has layer={m.get('layer')}"
        )
