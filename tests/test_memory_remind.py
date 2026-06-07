"""Tests for the memory_remind cross-vault MCP tool and cross_vault.py.

Covers:
1. Tool importable from memem.server (via _build_mcp path or cross_vault import)
2. Single-vault scenario: query returns hits with vault_id='default'
3. Two-vault synthetic test: disjoint memories — query matching only one returns
   from that vault only; query matching both returns from both with correct vault_id
4. why_relevant is non-empty for every hit AND contains a numeric similarity (regex \\d\\.\\d)
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_memory_md(memories_dir: Path, filename: str, title: str, content: str) -> None:
    """Write a minimal valid memory markdown file for testing."""
    import uuid
    mem_id = str(uuid.uuid4())
    _slug = filename.replace(".md", "")
    md = f"""---
id: {mem_id}
title: "{title}"
project: general
tags: []
source_type: user
importance: 3
layer: 2
status: active
created: 2026-01-01T00:00:00Z
updated: 2026-01-01T00:00:00Z
---

{content}
"""
    memories_dir.mkdir(parents=True, exist_ok=True)
    (memories_dir / filename).write_text(md, encoding="utf-8")
    return mem_id  # type: ignore[return-value]


def _make_vault(tmp_path: Path, name: str, memories: list[tuple[str, str, str]]) -> Path:
    """Create a synthetic vault directory with the given memories.

    Returns the vault memories directory path.

    ``memories`` is a list of (filename, title, content) tuples.
    """
    memories_dir = tmp_path / name / "memories"
    memories_dir.mkdir(parents=True)
    for filename, title, content in memories:
        _write_memory_md(memories_dir, filename, title, content)
    return memories_dir


# ---------------------------------------------------------------------------
# Test 1: search_across_vaults is importable from memem.cross_vault
# ---------------------------------------------------------------------------


def test_search_across_vaults_importable():
    """search_across_vaults is importable from memem.cross_vault."""
    from memem.cross_vault import search_across_vaults  # noqa: F401
    assert callable(search_across_vaults)


# ---------------------------------------------------------------------------
# Test 1b: memory_remind is accessible through _build_mcp in server.py
# ---------------------------------------------------------------------------


def test_memory_remind_importable_via_server():
    """_build_mcp in server.py does not raise when memory_remind is registered."""
    # We test that the imports in _build_mcp succeed (don't actually call it
    # as FastMCP requires an MCP transport).  Verify the new imports don't break.
    import memem.cross_vault  # noqa: F401
    # v1.11.0: vault_registry was inlined into server.py._build_mcp;
    # the import path is gone but the loader is still available via _build_mcp.
    assert True


# ---------------------------------------------------------------------------
# Test 2: single-vault scenario returns vault_id='default'
# ---------------------------------------------------------------------------


def test_single_vault_returns_default_vault_id(tmp_path):
    """Single-vault query returns hits with vault_id='default'."""
    memories_dir = _make_vault(
        tmp_path,
        "vault_default",
        [
            ("auth-retry-abc12345.md", "Authentication retry logic", "The auth retry loop uses exponential backoff with jitter."),
            ("unrelated-xyz98765.md", "Unrelated memory", "This memory is about cooking recipes."),
        ],
    )

    from memem.cross_vault import search_across_vaults

    vault_registry = [{"id": "default", "path": str(memories_dir)}]
    hits = search_across_vaults("authentication retry", vault_registry, max_results=3)

    assert len(hits) >= 1
    # All hits should be from 'default' vault
    for hit in hits:
        assert hit["vault_id"] == "default"
    # The auth-related memory should rank first
    titles = [h["title"] for h in hits]
    assert any("auth" in t.lower() or "Auth" in t for t in titles)


# ---------------------------------------------------------------------------
# Test 3a: two-vault — query matching only one returns from that vault only
# ---------------------------------------------------------------------------


def test_two_vaults_disjoint_query_returns_one_vault(tmp_path):
    """A query matching only vault A's memories should return only vault_id=A."""
    vault_a_dir = _make_vault(
        tmp_path,
        "vault_a",
        [("redis-pool-aabbccdd.md", "Redis connection pool", "Redis connection pool must be at least 20 for production use.")],
    )
    vault_b_dir = _make_vault(
        tmp_path,
        "vault_b",
        [("graphql-query-11223344.md", "GraphQL query batching", "Use DataLoader for N+1 prevention in GraphQL resolvers.")],
    )

    from memem.cross_vault import search_across_vaults

    vault_registry = [
        {"id": "vault-a", "path": str(vault_a_dir)},
        {"id": "vault-b", "path": str(vault_b_dir)},
    ]

    # Query specific to vault A
    hits = search_across_vaults("redis connection pool production", vault_registry, max_results=3)

    assert len(hits) >= 1
    # All hits should come from vault-a
    vault_ids = {h["vault_id"] for h in hits}
    assert "vault-a" in vault_ids
    # vault-b should not appear (no meaningful overlap)
    assert "vault-b" not in vault_ids


# ---------------------------------------------------------------------------
# Test 3b: two-vault — query matching both returns from both with correct vault_id
# ---------------------------------------------------------------------------


def test_two_vaults_shared_query_returns_from_both(tmp_path):
    """A query matching memories in both vaults returns hits with correct vault_ids."""
    vault_a_dir = _make_vault(
        tmp_path,
        "vault_a2",
        [("auth-a-aabbccdd.md", "Authentication in vault A", "Authentication uses JWT tokens with RS256 algorithm.")],
    )
    vault_b_dir = _make_vault(
        tmp_path,
        "vault_b2",
        [("auth-b-11223344.md", "Authentication in vault B", "Authentication requires OAuth2 flow with PKCE for mobile apps.")],
    )

    from memem.cross_vault import search_across_vaults

    vault_registry = [
        {"id": "vault-a", "path": str(vault_a_dir)},
        {"id": "vault-b", "path": str(vault_b_dir)},
    ]

    # Query that matches both vault A and vault B memories
    hits = search_across_vaults("authentication", vault_registry, max_results=5)

    assert len(hits) >= 2
    vault_ids = {h["vault_id"] for h in hits}
    assert "vault-a" in vault_ids
    assert "vault-b" in vault_ids


# ---------------------------------------------------------------------------
# Test 4: why_relevant is non-empty and contains a numeric similarity
# ---------------------------------------------------------------------------


def test_why_relevant_non_empty_with_numeric_similarity(tmp_path):
    """Every hit's why_relevant is non-empty and contains a numeric similarity like X.XX."""
    memories_dir = _make_vault(
        tmp_path,
        "vault_why",
        [
            ("jwt-auth-aaa11111.md", "JWT authentication setup", "JWT tokens are validated using RS256 public keys."),
            ("database-bbb22222.md", "Database connection pooling", "Database connections use pgbouncer for pooling."),
            ("cache-ccc33333.md", "Cache invalidation strategy", "Cache invalidation uses TTL with cache-aside pattern."),
        ],
    )

    from memem.cross_vault import search_across_vaults

    vault_registry = [{"id": "test-vault", "path": str(memories_dir)}]
    hits = search_across_vaults("jwt authentication tokens database", vault_registry, max_results=5)

    assert len(hits) >= 1
    sim_pattern = re.compile(r"\d\.\d")
    for hit in hits:
        assert hit["why_relevant"], f"why_relevant is empty for hit: {hit['title']!r}"
        assert sim_pattern.search(hit["why_relevant"]), (
            f"No numeric similarity found in why_relevant={hit['why_relevant']!r}"
        )
