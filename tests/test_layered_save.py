"""End-to-end tests for save-side layer auto-classification.

Verifies:
1. memory_save without layer param auto-classifies (assigns an int 0-3).
2. memory_save with explicit layer=0 stores layer 0.
3. memory_save with explicit layer=3 stores layer 3.
4. memory_save with invalid layer returns an error string.
5. The classify_layer heuristic actually fires and produces varied results
   (not every memory gets L2).
"""

import importlib


def _reload_all(monkeypatch=None):
    """Reload memem modules so env vars take effect."""
    from memem import models, obsidian_store, search_index, telemetry
    importlib.reload(models)
    importlib.reload(telemetry)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)


def test_memory_save_default_auto_classifies(tmp_vault, tmp_cortex_dir):
    """Saving without layer param results in a memory with layer int 0-3."""
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
    assert isinstance(saved[0]["layer"], int)
    assert 0 <= saved[0]["layer"] <= 3


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


def test_classify_layer_runs_when_unset(tmp_vault, tmp_cortex_dir):
    """When layer is unset, classify_layer heuristic fires; not all become L2."""
    _reload_all()
    from memem.obsidian_store import _make_memory, _obsidian_memories, _save_memory

    # High importance + structural tag -> L0 or L1
    m_high = _make_memory(
        content="memem uses Obsidian vault as the primary storage backend for memories.",
        title="memem architecture — Obsidian vault",
        project="memem",
        source_type="user",
        importance=5,
        tags=["architecture", "storage"],
    )
    _save_memory(m_high)

    # Project=general + importance>=4 -> L1
    m_generic = _make_memory(
        content="Always write tests before shipping: TDD prevents regression bugs.",
        title="TDD convention — tests before shipping",
        project="general",
        source_type="user",
        importance=4,
    )
    _save_memory(m_generic)

    # Low importance + short content -> L3
    m_low = _make_memory(
        content="Use black --line-length 88 for formatting in this project.",
        title="black formatting line length",
        project="my-project",
        source_type="user",
        importance=2,
    )
    _save_memory(m_low)

    layers = {m["layer"] for m in _obsidian_memories()}
    # Assert that not all memories ended up at L2 (the heuristic must have fired)
    assert layers != {2}, (
        f"All memories classified as L2 — classify_layer heuristic did not fire. "
        f"Layers: {[m['layer'] for m in _obsidian_memories()]}"
    )
