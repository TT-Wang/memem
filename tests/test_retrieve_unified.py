"""Tests for vault parser extension + status/invalid_at filter in retrieve.py (m1 v2.6).

Covers:
1. related + tags + status extracted correctly, incl. multi-item lists and quoted values
2. tags: [] inline empty → []
3. deprecated memory absent from vault_idx
4. invalid_at memory absent from vault_idx
5. active memory present with status 'active'
6. adjacent-list bleed regression: memory with BOTH tags and related — each list gets only its items
7. end-to-end: retrieve() never returns deprecated/invalidated fixtures even as best BM25 match
"""
from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_retrieve_caches(retrieve_mod):
    """Reset all module-level caches in retrieve.py between tests."""
    retrieve_mod._vault_idx_cache = None
    retrieve_mod._vault_idx_mtime = 0
    retrieve_mod._vault_idx_count = 0
    retrieve_mod._emb_cache = None
    retrieve_mod._bm25_cache = None
    retrieve_mod._bm25_cache_key = (0.0, 0)


def _make_uuid():
    return str(uuid.uuid4())


def _mem_id_short(full_id: str) -> str:
    return full_id[:8]


# ---------------------------------------------------------------------------
# Fixture: isolated vault + retrieve module reload
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Set up an isolated vault and reload models + retrieve with fresh caches."""
    vault = tmp_path / "obsidian-brain"
    memories_dir = vault / "memem" / "memories"
    memories_dir.mkdir(parents=True)

    state_dir = tmp_path / ".memem"
    state_dir.mkdir()

    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models as _models
    importlib.reload(_models)

    import memem.retrieve as _retrieve
    importlib.reload(_retrieve)
    _reset_retrieve_caches(_retrieve)

    return memories_dir, _retrieve


# ---------------------------------------------------------------------------
# Canonical frontmatter templates
# ---------------------------------------------------------------------------

def _write_active_memory(memories_dir: Path, title: str, mem_id: str,
                          tags: list[str], related: list[str] | None = None,
                          extra_fields: str = "") -> Path:
    """Write a minimal active memory file with canonical frontmatter.

    Follows _write_obsidian_memory canonical key order:
    id, schema_version, title, project, tags, [related], created, updated,
    source_type, source_session, importance, status, valid_to, layer, valid_at,
    [invalid_at], last_accessed_at, access_count, decay_immune
    """
    slug = title.lower().replace(" ", "-")[:30]
    filename = f"{slug}-{mem_id[:8]}.md"

    # Serialize tags — block style for non-empty, inline for empty
    if tags:
        tags_yaml = "tags:\n" + "".join(f"- {t}\n" for t in tags)
    else:
        tags_yaml = "tags: []\n"

    # Serialize related — block style, only if non-empty
    related_yaml = ""
    if related:
        related_yaml = "related:\n" + "".join(f"- {r}\n" for r in related)

    content = (
        "---\n"
        f"id: {mem_id}\n"
        "schema_version: 1\n"
        f"title: {title}\n"
        "project: test\n"
        f"{tags_yaml}"
        f"{related_yaml}"
        "created: '2025-01-15T10:00:00'\n"
        "updated: '2025-01-15T10:00:00'\n"
        "source_type: user\n"
        "source_session: ''\n"
        "importance: 3\n"
        "status: active\n"
        "valid_to: ''\n"
        "layer: 2\n"
        "valid_at: '2025-01-15T10:00:00'\n"
        f"{extra_fields}"
        "last_accessed_at: '2025-01-15T10:00:00'\n"
        "access_count: 0\n"
        "decay_immune: false\n"
        "---\n\n"
        f"Body text for {title}. This is the memory content.\n"
    )
    filepath = memories_dir / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


def _write_deprecated_memory(memories_dir: Path, title: str, mem_id: str,
                              tags: list[str] | None = None) -> Path:
    """Write a deprecated memory (status: deprecated)."""
    slug = title.lower().replace(" ", "-")[:30]
    filename = f"{slug}-{mem_id[:8]}.md"
    tags_yaml = "tags: []\n" if not tags else "tags:\n" + "".join(f"- {t}\n" for t in (tags or []))

    content = (
        "---\n"
        f"id: {mem_id}\n"
        "schema_version: 1\n"
        f"title: {title}\n"
        "project: test\n"
        f"{tags_yaml}"
        "created: '2025-01-15T10:00:00'\n"
        "updated: '2025-01-15T10:00:00'\n"
        "source_type: user\n"
        "source_session: ''\n"
        "importance: 3\n"
        "status: deprecated\n"
        "valid_to: '2025-01-16T10:00:00'\n"
        "layer: 2\n"
        "valid_at: '2025-01-15T10:00:00'\n"
        "last_accessed_at: '2025-01-15T10:00:00'\n"
        "access_count: 0\n"
        "decay_immune: false\n"
        "---\n\n"
        f"Body text for {title}. This is deprecated content.\n"
    )
    filepath = memories_dir / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


def _write_invalidated_memory(memories_dir: Path, title: str, mem_id: str,
                               tags: list[str] | None = None) -> Path:
    """Write an invalidated memory (invalid_at set)."""
    slug = title.lower().replace(" ", "-")[:30]
    filename = f"{slug}-{mem_id[:8]}.md"
    tags_yaml = "tags: []\n" if not tags else "tags:\n" + "".join(f"- {t}\n" for t in (tags or []))

    content = (
        "---\n"
        f"id: {mem_id}\n"
        "schema_version: 1\n"
        f"title: {title}\n"
        "project: test\n"
        f"{tags_yaml}"
        "created: '2025-01-15T10:00:00'\n"
        "updated: '2025-01-15T10:00:00'\n"
        "source_type: user\n"
        "source_session: ''\n"
        "importance: 3\n"
        "status: active\n"
        "valid_to: ''\n"
        "layer: 2\n"
        "valid_at: '2025-01-15T10:00:00'\n"
        "invalid_at: '2025-01-16T12:00:00'\n"
        "last_accessed_at: '2025-01-15T10:00:00'\n"
        "access_count: 0\n"
        "decay_immune: false\n"
        "---\n\n"
        f"Body text for {title}. This has been invalidated.\n"
    )
    filepath = memories_dir / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# Test 1: related + tags + status extracted correctly, multi-item + quoted values
# ---------------------------------------------------------------------------


def test_extract_related_tags_status(isolated_vault):
    """Test that related[], tags[], and status are correctly extracted."""
    memories_dir, _retrieve = isolated_vault
    mem_id = _make_uuid()
    related1 = "ab12cd34"
    related2 = "ef56gh78"

    _write_active_memory(
        memories_dir, "Extract Test Memory", mem_id,
        tags=["python", "testing", "memem"],
        related=[related1, related2],
    )
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert mem_id in idx, "Memory should be present in vault_idx"

    mem = idx[mem_id]
    assert mem["status"] == "active", f"Expected status='active', got {mem['status']!r}"
    assert mem["tags"] == ["python", "testing", "memem"], f"Expected tags list, got {mem['tags']}"
    assert mem["related"] == [related1, related2], f"Expected related list, got {mem['related']}"


# ---------------------------------------------------------------------------
# Test 2: tags: [] inline empty → []
# ---------------------------------------------------------------------------


def test_tags_inline_empty(isolated_vault):
    """tags: [] (inline empty) should produce an empty list, not fail."""
    memories_dir, _retrieve = isolated_vault
    mem_id = _make_uuid()

    # Write memory with inline empty tags
    _write_active_memory(memories_dir, "Empty Tags Memory", mem_id, tags=[])  # produces 'tags: []'
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert mem_id in idx, "Memory should be present"

    mem = idx[mem_id]
    assert mem["tags"] == [], f"Expected empty list, got {mem['tags']!r}"


# ---------------------------------------------------------------------------
# Test 3: deprecated memory absent from vault_idx
# ---------------------------------------------------------------------------


def test_deprecated_memory_absent_from_index(isolated_vault):
    """A memory with status: deprecated must not appear in vault_idx."""
    memories_dir, _retrieve = isolated_vault
    dep_id = _make_uuid()
    active_id = _make_uuid()

    _write_deprecated_memory(memories_dir, "Deprecated Memory", dep_id)
    _write_active_memory(memories_dir, "Active Memory", active_id, tags=["test"])
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert dep_id not in idx, "Deprecated memory must be excluded from vault_idx"
    assert active_id in idx, "Active memory must still be present"


# ---------------------------------------------------------------------------
# Test 4: invalid_at memory absent from vault_idx
# ---------------------------------------------------------------------------


def test_invalidated_memory_absent_from_index(isolated_vault):
    """A memory with a non-empty invalid_at must not appear in vault_idx."""
    memories_dir, _retrieve = isolated_vault
    inv_id = _make_uuid()
    active_id = _make_uuid()

    _write_invalidated_memory(memories_dir, "Invalidated Memory", inv_id)
    _write_active_memory(memories_dir, "Active Memory 2", active_id, tags=["active"])
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert inv_id not in idx, "Invalidated memory must be excluded from vault_idx"
    assert active_id in idx, "Active memory must still be present"


# ---------------------------------------------------------------------------
# Test 5: active memory present with status 'active'
# ---------------------------------------------------------------------------


def test_active_memory_present(isolated_vault):
    """An active memory (status: active) should be included in vault_idx with correct status."""
    memories_dir, _retrieve = isolated_vault
    mem_id = _make_uuid()

    _write_active_memory(memories_dir, "Normal Active Memory", mem_id, tags=["test"])
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert mem_id in idx, "Active memory must be in vault_idx"
    assert idx[mem_id]["status"] == "active", "Status field must be 'active'"


# ---------------------------------------------------------------------------
# Test 6: adjacent-list bleed regression
# ---------------------------------------------------------------------------


def test_adjacent_list_bleed_regression(isolated_vault):
    """A memory with both tags AND related must not bleed items between the two lists."""
    memories_dir, _retrieve = isolated_vault
    mem_id = _make_uuid()
    tag1 = "python"
    tag2 = "testing"
    rel1 = "aaaa1111"
    rel2 = "bbbb2222"

    # Write with both tags and related (tags comes before related in canonical order)
    _write_active_memory(
        memories_dir,
        "Adjacent List Memory",
        mem_id,
        tags=[tag1, tag2],
        related=[rel1, rel2],
    )
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert mem_id in idx, "Memory should be in vault_idx"

    mem = idx[mem_id]
    # tags must contain ONLY tag items, no related items
    assert tag1 in mem["tags"], f"{tag1!r} should be in tags"
    assert tag2 in mem["tags"], f"{tag2!r} should be in tags"
    assert rel1 not in mem["tags"], f"{rel1!r} must NOT appear in tags (bleed)"
    assert rel2 not in mem["tags"], f"{rel2!r} must NOT appear in tags (bleed)"
    assert len(mem["tags"]) == 2, f"Expected exactly 2 tags, got {mem['tags']}"

    # related must contain ONLY related items, no tag items
    assert rel1 in mem["related"], f"{rel1!r} should be in related"
    assert rel2 in mem["related"], f"{rel2!r} should be in related"
    assert tag1 not in mem["related"], f"{tag1!r} must NOT appear in related (bleed)"
    assert tag2 not in mem["related"], f"{tag2!r} must NOT appear in related (bleed)"
    assert len(mem["related"]) == 2, f"Expected exactly 2 related, got {mem['related']}"


# ---------------------------------------------------------------------------
# Test 7: end-to-end — retrieve() never returns deprecated/invalidated
# ---------------------------------------------------------------------------


def test_retrieve_never_returns_deprecated_or_invalidated(isolated_vault):
    """retrieve() must not return deprecated or invalidated memories even as best BM25 match.

    We place the deprecated + invalidated memories with the exact query term in title/body
    as the best lexical match, while the one active memory uses different but overlapping
    terms. BM25-only path (no embeddings) must still not surface the excluded memories.
    """
    memories_dir, _retrieve = isolated_vault

    # Unique marker term for the query
    marker = "xyzzy_unique_marker_term"

    dep_id = _make_uuid()
    inv_id = _make_uuid()
    active_id = _make_uuid()

    # Write deprecated memory that would be a perfect lexical match for `marker`
    dep_content = (
        "---\n"
        f"id: {dep_id}\n"
        "schema_version: 1\n"
        f"title: {marker} deprecated title\n"
        "project: test\n"
        "tags:\n"
        f"- {marker}\n"
        "created: '2025-01-15T10:00:00'\n"
        "updated: '2025-01-15T10:00:00'\n"
        "source_type: user\n"
        "source_session: ''\n"
        "importance: 5\n"
        "status: deprecated\n"
        "valid_to: '2025-01-16T10:00:00'\n"
        "layer: 2\n"
        "valid_at: '2025-01-15T10:00:00'\n"
        "last_accessed_at: '2025-01-15T10:00:00'\n"
        "access_count: 100\n"
        "decay_immune: true\n"
        "---\n\n"
        f"{marker} {marker} {marker} This memory is deprecated but has highest token frequency.\n"
    )
    (memories_dir / f"dep-{dep_id[:8]}.md").write_text(dep_content, encoding="utf-8")

    # Write invalidated memory that also matches perfectly
    inv_content = (
        "---\n"
        f"id: {inv_id}\n"
        "schema_version: 1\n"
        f"title: {marker} invalidated title\n"
        "project: test\n"
        "tags: []\n"
        "created: '2025-01-15T10:00:00'\n"
        "updated: '2025-01-15T10:00:00'\n"
        "source_type: user\n"
        "source_session: ''\n"
        "importance: 5\n"
        "status: active\n"
        "valid_to: ''\n"
        "layer: 2\n"
        "valid_at: '2025-01-15T10:00:00'\n"
        "invalid_at: '2025-01-16T12:00:00'\n"
        "last_accessed_at: '2025-01-15T10:00:00'\n"
        "access_count: 100\n"
        "decay_immune: true\n"
        "---\n\n"
        f"{marker} {marker} {marker} This memory is invalidated but has highest token frequency.\n"
    )
    (memories_dir / f"inv-{inv_id[:8]}.md").write_text(inv_content, encoding="utf-8")

    # Write a regular active memory with a single mention of the marker
    _write_active_memory(
        memories_dir,
        f"Active {marker} memory",
        active_id,
        tags=["test"],
    )

    _reset_retrieve_caches(_retrieve)

    # Disable embeddings (BM25-only path)
    monkeypatch_get_model = lambda: None  # noqa: E731
    original_get_model = _retrieve._get_model
    _retrieve._get_model = monkeypatch_get_model
    _retrieve._emb_cache = None

    try:
        results = _retrieve.retrieve(marker, k=10, log_call_type=None)
    finally:
        _retrieve._get_model = original_get_model

    # Verify excluded memories never appear
    returned_ids = {h.get("id") for h in results}
    assert dep_id not in returned_ids, (
        f"Deprecated memory {dep_id[:8]} must NOT be returned by retrieve()"
    )
    assert inv_id not in returned_ids, (
        f"Invalidated memory {inv_id[:8]} must NOT be returned by retrieve()"
    )
    # The active memory (which mentions the marker) should be present
    assert active_id in returned_ids, (
        f"Active memory {active_id[:8]} should be returned by retrieve()"
    )


# ---------------------------------------------------------------------------
# Test 8: quoted timestamp values in status/tags/related are stripped correctly
# ---------------------------------------------------------------------------


def test_quoted_values_stripped(isolated_vault):
    """Quoted values (SafeDumper wraps timestamps) should be stripped of quotes."""
    memories_dir, _retrieve = isolated_vault
    mem_id = _make_uuid()
    rel_id = "abcdef12"

    # Write a memory where related ID is quoted (as SafeDumper might do)
    content = (
        "---\n"
        f"id: {mem_id}\n"
        "schema_version: 1\n"
        "title: Quoted Values Test\n"
        "project: test\n"
        "tags:\n"
        "- 'quoted-tag'\n"
        "- regular-tag\n"
        "related:\n"
        f"- '{rel_id}'\n"
        "created: '2025-01-15T10:00:00'\n"
        "updated: '2025-01-15T10:00:00'\n"
        "source_type: user\n"
        "source_session: ''\n"
        "importance: 3\n"
        "status: active\n"
        "valid_to: ''\n"
        "layer: 2\n"
        "valid_at: '2025-01-15T10:00:00'\n"
        "last_accessed_at: '2025-01-15T10:00:00'\n"
        "access_count: 0\n"
        "decay_immune: false\n"
        "---\n\n"
        "Body text for quoted values test.\n"
    )
    (memories_dir / f"quoted-{mem_id[:8]}.md").write_text(content, encoding="utf-8")
    _reset_retrieve_caches(_retrieve)

    idx = _retrieve.load_vault_index()
    assert mem_id in idx

    mem = idx[mem_id]
    # Quotes should be stripped
    assert "quoted-tag" in mem["tags"], f"Expected 'quoted-tag' stripped of quotes, got {mem['tags']}"
    assert "regular-tag" in mem["tags"]
    assert rel_id in mem["related"], f"Expected rel_id {rel_id!r} stripped of quotes, got {mem['related']}"
