"""Tests for m7 small-fixes bundle (B10-B24).

Covers regression tests for:
- B11: graph_index._classify_relation supersedes requires exact tag
- B17: obsidian_store clustered_into/references round-trip
- B18: search_index FTS UNINDEXED migration
- B23: models.py aliases override loader (project_aliases.json)
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# B11: _classify_relation — false supersedes edge regression
# ---------------------------------------------------------------------------

def test_classify_relation_no_false_supersedes_on_unrelated_dst(tmp_vault, tmp_cortex_dir):
    """src with 'supersedes:aaaaaaaa' tag + lexically-similar dst with id 'bbbbbbbb...'
    must NOT classify as supersedes (B11 regression)."""
    from memem import graph_index

    src = {
        "id": "aaaaaaaaaaaaaaaa",
        "title": "memory about authentication",
        "essence": "authentication token is used for login with JWT",
        "domain_tags": ["supersedes:aaaaaaaa"],  # points to a different 8-char prefix
        "project": "general",
        "source_type": "user",
        "importance": 3,
        "layer": 2,
        "status": "active",
    }
    dst = {
        "id": "bbbbbbbbbbbbbbbb",  # 8-char prefix is 'bbbbbbbb', NOT 'aaaaaaaa'
        "title": "memory about authentication tokens",
        "essence": "authentication token is used for login with JWT sessions",
        "domain_tags": [],
        "project": "general",
        "source_type": "user",
        "importance": 3,
        "layer": 2,
        "status": "active",
    }

    # These two memories are lexically very similar (high lexical score expected)
    features = graph_index._score_memory_pair(src, dst)
    # Confirm they actually ARE similar so the test is meaningful
    assert features["lexical"] >= 0.12, f"Expected high lexical similarity, got {features['lexical']}"

    # The supersedes tag does NOT match dst's id8 ('bbbbbbbb'), so must NOT classify supersedes
    relation = graph_index._classify_relation(src, dst, features)
    assert relation != "supersedes", (
        f"Expected non-supersedes relation but got {relation!r}. "
        "B11: supersedes must require exact 'supersedes:<dst_id8>' tag match."
    )


def test_classify_relation_exact_supersedes_tag_fires(tmp_vault, tmp_cortex_dir):
    """src with exact 'supersedes:bbbbbbbb' tag + lexically-similar dst id 'bbbbbbbb...'
    MUST classify as supersedes."""
    from memem import graph_index

    src = {
        "id": "aaaaaaaaaaaaaaaa",
        "title": "memory about authentication",
        "essence": "authentication token is used for login with JWT",
        "domain_tags": ["supersedes:bbbbbbbb"],  # exact match for dst's 8-char prefix
        "project": "general",
        "source_type": "user",
        "importance": 3,
        "layer": 2,
        "status": "active",
    }
    dst = {
        "id": "bbbbbbbbbbbbbbbb",  # 8-char prefix is 'bbbbbbbb'
        "title": "memory about authentication tokens",
        "essence": "authentication token is used for login with JWT sessions",
        "domain_tags": [],
        "project": "general",
        "source_type": "user",
        "importance": 3,
        "layer": 2,
        "status": "active",
    }

    features = graph_index._score_memory_pair(src, dst)
    assert features["lexical"] >= 0.12, f"Expected high lexical similarity, got {features['lexical']}"

    relation = graph_index._classify_relation(src, dst, features)
    assert relation == "supersedes", (
        f"Expected supersedes but got {relation!r}. "
        "B11: exact 'supersedes:<dst_id8>' tag must trigger supersedes relation."
    )


# ---------------------------------------------------------------------------
# B17: obsidian_store clustered_into + references round-trip
# ---------------------------------------------------------------------------

def test_clustered_into_roundtrip(tmp_vault, tmp_cortex_dir):
    """clustered_into field persists through write and parses back on read (B17)."""
    from memem.obsidian_store import (
        _make_memory,
        _obsidian_memories,
        _reset_cache,
        _write_obsidian_memory,
    )

    mem = _make_memory(
        content="This memory belongs to a cluster synthesized by the dreamer.",
        title="Cluster constituent memory",
        project="testproject",
        importance=3,
        layer=2,
    )
    # Simulate dreamer writing clustered_into
    mem["clustered_into"] = "deadbeef-1234-5678-9abc-def012345678"
    _write_obsidian_memory(mem)
    _reset_cache()

    mems = _obsidian_memories(include_deprecated=True)
    assert len(mems) == 1, f"Expected 1 memory, got {len(mems)}"
    assert mems[0].get("clustered_into") == "deadbeef-1234-5678-9abc-def012345678", (
        f"clustered_into not persisted correctly: {mems[0].get('clustered_into')!r}"
    )


def test_references_roundtrip(tmp_vault, tmp_cortex_dir):
    """references field persists through write and parses back on read (B17)."""
    from memem.obsidian_store import (
        _make_memory,
        _obsidian_memories,
        _reset_cache,
        _write_obsidian_memory,
    )

    mem = _make_memory(
        content="Contradiction detected between two conflicting memories about the database.",
        title="Contradiction flag: db-choice",
        tags=["kind:contradiction-flag", "contradiction", "pending-review"],
        project="testproject",
        importance=4,
        layer=2,
    )
    # Simulate consolidation writing references
    mem["references"] = ["aabbccdd", "eeff0011"]
    _write_obsidian_memory(mem)
    _reset_cache()

    mems = _obsidian_memories(include_deprecated=True)
    assert len(mems) == 1, f"Expected 1 memory, got {len(mems)}"
    refs = mems[0].get("references", [])
    assert "aabbccdd" in refs, f"aabbccdd not in references: {refs}"
    assert "eeff0011" in refs, f"eeff0011 not in references: {refs}"


def test_clustered_into_absent_when_not_set(tmp_vault, tmp_cortex_dir):
    """clustered_into is absent from parsed memory when not set (B17)."""
    from memem.obsidian_store import (
        _make_memory,
        _obsidian_memories,
        _reset_cache,
        _write_obsidian_memory,
    )

    mem = _make_memory(
        content="A normal memory without clustered_into.",
        title="Normal memory",
        project="testproject",
        importance=3,
        layer=2,
    )
    _write_obsidian_memory(mem)
    _reset_cache()

    mems = _obsidian_memories(include_deprecated=True)
    assert len(mems) == 1
    # clustered_into should not be present (or None/absent) when not set
    assert mems[0].get("clustered_into") is None, (
        f"clustered_into should be absent, got {mems[0].get('clustered_into')!r}"
    )


# ---------------------------------------------------------------------------
# B18: FTS migration — old-version DB triggers repopulate
# ---------------------------------------------------------------------------

def test_fts_migration_from_old_schema_version(tmp_vault, tmp_cortex_dir):
    """Creating a DB with old user_version triggers migration to v3 and repopulates
    FROM THE VAULT (drop+repopulate, vault is source of truth) (B18)."""
    from memem import search_index
    from memem.models import SEARCH_DB
    from memem.obsidian_store import _make_memory, _save_memory

    # Seed the (tmp) vault with one real memory BEFORE migration so we can
    # assert the rebuilt table was repopulated from the vault, not left empty
    # or copied from the stale v1 table.
    mem = _make_memory(
        content="FTS migration repopulation canary: vault is the source of truth.",
        title="Migration canary memory",
    )
    _save_memory(mem)

    # Remove any existing search DB (the save above may have created a v3 one)
    if SEARCH_DB.exists():
        SEARCH_DB.unlink()

    # Create an old-version DB (version < current _FTS_SCHEMA_VERSION) with a
    # bogus row that must NOT survive the drop+repopulate.
    conn = sqlite3.connect(str(SEARCH_DB))
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id, title, essence, project, tags, related_ids)")
    conn.execute(
        "INSERT INTO memories_fts (memory_id, title, essence, project, tags, related_ids) "
        "VALUES ('deadbeef', 'stale v1 row', 'should not survive migration', 'general', '', '')"
    )
    conn.execute("PRAGMA user_version = 1")  # Old version 1
    conn.commit()
    conn.close()

    # Open via the init function — should trigger migration
    conn = search_index._init_search_db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    rows = conn.execute("SELECT memory_id, title FROM memories_fts").fetchall()
    conn.close()

    assert version == search_index._FTS_SCHEMA_VERSION, (
        f"Expected version {search_index._FTS_SCHEMA_VERSION} after migration, got {version}"
    )
    assert search_index._FTS_SCHEMA_VERSION == 3, (
        f"Expected _FTS_SCHEMA_VERSION to be 3, got {search_index._FTS_SCHEMA_VERSION}"
    )
    ids = {r[0] for r in rows}
    assert mem["id"] in ids, (
        f"Vault memory must be repopulated into the migrated FTS table; rows: {rows}"
    )
    assert "deadbeef" not in ids, (
        "Stale v1 row must NOT survive the drop+repopulate migration"
    )


def test_fts_schema_uses_unindexed_columns(tmp_vault, tmp_cortex_dir):
    """The FTS5 table schema declares memory_id and related_ids as UNINDEXED (B18)."""
    from memem.search_index import _CREATE_TABLE_SQL, _FTS_SCHEMA_VERSION

    assert "UNINDEXED" in _CREATE_TABLE_SQL, (
        "FTS5 CREATE TABLE SQL must contain UNINDEXED to prevent id-fragment BM25 pollution"
    )
    assert "memory_id UNINDEXED" in _CREATE_TABLE_SQL, "memory_id must be UNINDEXED"
    assert "related_ids UNINDEXED" in _CREATE_TABLE_SQL, "related_ids must be UNINDEXED"
    assert _FTS_SCHEMA_VERSION == 3, f"Expected schema version 3, got {_FTS_SCHEMA_VERSION}"


# ---------------------------------------------------------------------------
# B23: models.py project_aliases.json override loader
# ---------------------------------------------------------------------------

def test_project_aliases_override_file_merges(tmp_path, monkeypatch):
    """A valid project_aliases.json in MEMEM_DIR is merged over built-in aliases (B23)."""
    # Create a temp MEMEM_DIR with a project_aliases.json
    tmp_memem = tmp_path / ".memem"
    tmp_memem.mkdir()
    aliases_file = tmp_memem / "project_aliases.json"
    aliases_file.write_text(json.dumps({"my-app-v2": "my-app", "legacy-api": "api-service"}))

    monkeypatch.setenv("MEMEM_DIR", str(tmp_memem))

    # Reload models so it picks up the new MEMEM_DIR
    import memem.models as models_mod
    importlib.reload(models_mod)

    try:
        # User-defined aliases should be merged
        assert models_mod._normalize_scope_id("my-app-v2") == "my-app", (
            "User alias 'my-app-v2' -> 'my-app' should be loaded from project_aliases.json"
        )
        assert models_mod._normalize_scope_id("legacy-api") == "api-service", (
            "User alias 'legacy-api' -> 'api-service' should be loaded from project_aliases.json"
        )
        # Built-in self-aliases should still work
        assert models_mod._normalize_scope_id("memem") == "cortex-plugin"
        assert models_mod._normalize_scope_id("cortex") == "cortex-plugin"
    finally:
        importlib.reload(models_mod)


def test_project_aliases_override_file_malformed_ignored(tmp_path, monkeypatch):
    """A malformed project_aliases.json is silently ignored (B23)."""
    tmp_memem = tmp_path / ".memem"
    tmp_memem.mkdir()
    aliases_file = tmp_memem / "project_aliases.json"
    aliases_file.write_text("NOT VALID JSON {{{")

    monkeypatch.setenv("MEMEM_DIR", str(tmp_memem))

    # Reload models — should not raise
    import memem.models as models_mod
    importlib.reload(models_mod)

    try:
        # Built-in aliases should still work after failed override load
        assert models_mod._normalize_scope_id("memem") == "cortex-plugin"
    finally:
        importlib.reload(models_mod)


def test_project_aliases_override_file_absent_ok(tmp_path, monkeypatch):
    """Missing project_aliases.json is silently ignored (B23)."""
    tmp_memem = tmp_path / ".memem"
    tmp_memem.mkdir()
    # No project_aliases.json file

    monkeypatch.setenv("MEMEM_DIR", str(tmp_memem))

    import memem.models as models_mod
    importlib.reload(models_mod)

    try:
        # Built-in aliases should still work
        assert models_mod._normalize_scope_id("memem") == "cortex-plugin"
        assert models_mod._normalize_scope_id("cortex") == "cortex-plugin"
    finally:
        importlib.reload(models_mod)


def test_personal_project_aliases_removed(tmp_path, monkeypatch):
    """Personal project aliases (vibireader, hft, techfeed, notes-api) no longer in built-ins (B23)."""
    import memem.models as models_mod
    importlib.reload(models_mod)

    personal_aliases = [
        "vibireader", "vibe-reader", "Vibereader", "Vibireader",
        "hft-strategies", "HFT strategies", "hft",
        "Tech Feed TUI", "tailor-reader",
        "notes-api",
    ]
    for alias in personal_aliases:
        result = models_mod._normalize_scope_id(alias)
        assert result == alias, (
            f"Personal alias {alias!r} should pass through unchanged (not in built-in aliases), "
            f"but got {result!r}"
        )
