"""Comprehensive tests for v0.10.0 features: layered recall, compact index.

Note: classify_layer tests (Tests 10-13) removed in v2.8.0 — layer system retired.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mem(
    mid: str | None = None,
    title: str = "Test memory",
    essence: str = "Some essence text",
    layer: int | None = None,
    project: str = "myproj",
    importance: int = 3,
    tags: list[str] | None = None,
    source_type: str = "user",
    related: list[str] | None = None,
) -> dict:
    mem: dict = {
        "id": mid or str(uuid.uuid4()),
        "title": title,
        "essence": essence,
        "project": project,
        "importance": importance,
        "source_type": source_type,
        "status": "active",
        "tags": tags or [],
        "domain_tags": tags or [],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "schema_version": 1,
    }
    if layer is not None:
        mem["layer"] = layer
    if related is not None:
        mem["related"] = related
    return mem


# ---------------------------------------------------------------------------
# Test 1: Layer constants importable
# ---------------------------------------------------------------------------

def test_layer_constants_importable():
    from memem.models import (
        DEFAULT_LAYER,
        LAST_BRIEF_PATH,
        LAYER_L0,
        LAYER_L1,
        LAYER_L2,
        LAYER_L3,
    )
    assert LAYER_L0 == 0
    assert LAYER_L1 == 1
    assert LAYER_L2 == 2
    assert LAYER_L3 == 3
    assert DEFAULT_LAYER == 2
    assert isinstance(LAST_BRIEF_PATH, Path)


# ---------------------------------------------------------------------------
# Test 2: Layer frontmatter roundtrip
# ---------------------------------------------------------------------------

def test_layer_frontmatter_roundtrip(tmp_vault):
    from memem.models import LAYER_L1
    from memem.obsidian_store import _parse_obsidian_memory_file, _write_obsidian_memory

    mem = _make_mem(
        mid=str(uuid.uuid4()),
        title="Pytest fixtures rock",
        essence="Use fixtures for test isolation",
        layer=LAYER_L1,
    )
    _write_obsidian_memory(mem)

    filename = mem["obsidian_file"]
    from memem.models import OBSIDIAN_MEMORIES_DIR
    parsed = _parse_obsidian_memory_file(OBSIDIAN_MEMORIES_DIR / filename)

    assert parsed is not None
    assert parsed["layer"] == 1
    assert isinstance(parsed["layer"], int)


# ---------------------------------------------------------------------------
# Test 3: Absent layer field in frontmatter → no layer key in parsed dict (v2.8.0)
# ---------------------------------------------------------------------------

def test_layer_absent_when_missing_from_frontmatter(tmp_vault):
    """v2.8.0: absent layer: frontmatter → layer key absent in parsed dict (not DEFAULT_LAYER).
    DELETED(v2.8): layer system retired — parser no longer injects DEFAULT_LAYER for
    memories missing the layer: field. Existing vault files without layer: parse
    with no layer key; callers use .get("layer", DEFAULT_LAYER) for compat.
    """
    from memem.models import OBSIDIAN_MEMORIES_DIR
    from memem.obsidian_store import _parse_obsidian_memory_file, _write_obsidian_memory

    # Write without layer field
    mem = _make_mem(
        mid=str(uuid.uuid4()),
        title="No layer field",
        essence="This memory has no layer set",
    )
    # Do not include 'layer' key at all
    mem.pop("layer", None)
    _write_obsidian_memory(mem)

    # Manually strip `layer:` line from the written file to simulate old format
    filename = mem["obsidian_file"]
    md_path = OBSIDIAN_MEMORIES_DIR / filename
    content = md_path.read_text()
    # Remove layer line from frontmatter
    lines = [line for line in content.splitlines() if not line.startswith("layer:")]
    md_path.write_text("\n".join(lines))

    parsed = _parse_obsidian_memory_file(md_path)
    assert parsed is not None
    # v2.8.0: absent layer → key absent (not DEFAULT_LAYER injection)
    assert "layer" not in parsed, (
        f"Expected no layer key for absent-layer memory, got layer={parsed.get('layer')}"
    )


# ---------------------------------------------------------------------------
# Test 4: _format_compact_index_line
# ---------------------------------------------------------------------------

def test_format_compact_index_line():
    from memem.recall import _format_compact_index_line

    mem = {
        "id": "abc12345deadbeef",
        "layer": 1,
        "title": "Use pytest",
        "essence": "prefer pytest over unittest",
    }
    result = _format_compact_index_line(mem)
    assert "[abc12345]" in result
    assert "L1" in result
    assert "Use pytest" in result
    assert "prefer pytest over unittest" in result


# ---------------------------------------------------------------------------
# Test 5: _format_compact_index_line uses DEFAULT_LAYER when layer missing
# ---------------------------------------------------------------------------

def test_format_compact_line_default_layer():
    from memem.recall import _format_compact_index_line

    mem = {
        "id": "ff001122aabbccdd",
        "title": "No layer here",
        "essence": "some content",
    }
    result = _format_compact_index_line(mem)
    assert "L2" in result  # DEFAULT_LAYER == 2


# ---------------------------------------------------------------------------
# Test 6: memory_search returns compact format
# ---------------------------------------------------------------------------

def test_memory_search_compact_format(monkeypatch):
    fake_memories = [
        _make_mem(mid="aaa11111" + "x" * 24, title="test alpha", essence="alpha test content", layer=1),
        _make_mem(mid="bbb22222" + "x" * 24, title="test beta", essence="beta test content", layer=2),
        _make_mem(mid="ccc33333" + "x" * 24, title="test gamma", essence="gamma test content", layer=3),
    ]

    monkeypatch.setattr("memem.recall._search_memories", lambda *a, **kw: fake_memories)

    from memem.recall import memory_search
    result = memory_search("test")

    # v2.0 format: "## Memory Search — {scope} — `{query}`"
    assert "## Memory Search" in result
    # 3 compact lines — each contains [id8] format
    compact_lines = [line for line in result.splitlines() if line.startswith("[")]
    assert len(compact_lines) == 3


# ---------------------------------------------------------------------------
# Test 7: memory_get fetches by ID returning full content
# ---------------------------------------------------------------------------

def test_memory_get_fetches_by_id(monkeypatch):
    mem_id = "abc12345" + "0" * 28
    fake_mem = _make_mem(
        mid=mem_id,
        title="Deep dive memory",
        essence="This is the full essence content for the deep dive",
        layer=2,
    )

    monkeypatch.setattr("memem.recall._find_memory", lambda mid: fake_mem if mid.startswith("abc12345") else None)

    from memem.recall import memory_get
    result = memory_get(["abc12345"])

    assert "Deep dive memory" in result
    assert "This is the full essence content" in result
    # Full content format, not just compact index line
    assert "### [abc12345]" in result


# ---------------------------------------------------------------------------
# Test 8: memory_get handles not-found gracefully
# ---------------------------------------------------------------------------

def test_memory_get_handles_not_found(monkeypatch):
    monkeypatch.setattr("memem.recall._find_memory", lambda mid: None)

    from memem.recall import memory_get
    # Should not raise
    result = memory_get(["nope9999"])
    # v2.0 format: "_Not found: {id}_"
    assert "nope9999" in result
    assert "not" in result.lower()


# ---------------------------------------------------------------------------
# Test 9: graph traversal one hop
# ---------------------------------------------------------------------------

def test_graph_traversal_two_hops(monkeypatch):
    """Default is 2-hop expansion (per m4). A→B→C chain: searching for A
    must surface B (1-hop) AND C (2-hop) in the Related section."""
    id_a = "aaaa0001" + "0" * 28
    id_b = "bbbb0002" + "0" * 28
    id_c = "cccc0003" + "0" * 28

    mem_a = _make_mem(mid=id_a, title="Alpha memory", essence="alpha content test keyword",
                      layer=1, related=[id_b[:8]])
    mem_b = _make_mem(mid=id_b, title="Beta memory", essence="beta linked to alpha",
                      layer=2, related=[id_c[:8]])
    mem_c = _make_mem(mid=id_c, title="Gamma memory", essence="gamma two hops away",
                      layer=2)

    monkeypatch.setattr("memem.recall._search_memories", lambda *a, **kw: [mem_a])

    def fake_find_memory(mid: str) -> dict | None:
        for m in [mem_a, mem_b, mem_c]:
            if m["id"].startswith(mid) or mid.startswith(m["id"][:8]):
                return m
        return None

    monkeypatch.setattr("memem.recall._find_memory", fake_find_memory)

    from memem.recall import memory_search
    result = memory_search("keyword")
    # Both 1-hop (B) and 2-hop (C) should surface
    assert "Beta memory" in result or "bbbb0002" in result
    assert "Gamma memory" in result or "cccc0003" in result


# DELETED(v2.6): test_union_search_includes_ngram_only_candidates removed because
# _search_memories_fts and the ngram union path no longer exist in recall.py.
# The unified retrieve() engine (three-way RRF: cosine+BM25+FTS) subsumes
# both FTS and ngram signal channels, so ngram-only recall is covered by BM25+FTS.


def test_embedding_graceful_degrade_without_dep():
    """If sentence-transformers isn't installed, _search_embedding must
    return [] and is_available() must return False — no exception raised
    and the union-rank path upstream still works."""
    # The venv doesn't have sentence-transformers, so we expect graceful degrade.
    # If someone installs it later, this test will still pass because we
    # use mocking to force the ImportError path.
    import sys

    from memem import embedding_index
    saved = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = None  # forces ImportError in _try_import
    embedding_index._model = None  # clear any cached model
    embedding_index._unavailable_logged = False
    try:
        assert embedding_index.is_available() is False
        assert embedding_index._search_embedding("anything", limit=5) == []
    finally:
        if saved is None:
            sys.modules.pop("sentence_transformers", None)
        else:
            sys.modules["sentence_transformers"] = saved


# DELETED(v2.6): test_union_search_survives_embedding_timeout removed because
# _search_memories_fts no longer exists. The unified retrieve() engine already
# handles embedding failures gracefully (cosine channel degrades to BM25+FTS-only).
# retrieve() embedding-failure resilience is tested in tests/test_retrieve_unified.py.




def test_graph_traversal_two_hop_is_superset_of_one_hop():
    """Regression guarantee: _expand_graph with hops=2 returns a strict
    superset of hops=1 for the same seed set. No 1-hop memory is dropped
    in favor of 2-hop ones."""
    from memem.recall import _expand_graph

    mem_a = {"id": "aaaa" + "1" * 28, "related": ["bbbb1111"]}
    mem_b = {"id": "bbbb" + "1" * 28, "related": ["cccc1111"]}
    mem_c = {"id": "cccc" + "1" * 28, "related": []}

    import memem.recall as recall
    # fake cache
    fake_store = {m["id"][:8]: m for m in [mem_a, mem_b, mem_c]}
    orig_find = recall._find_memory
    recall._find_memory = lambda mid: fake_store.get(mid[:8])
    try:
        one_hop = _expand_graph([mem_a], max_total=20, hops=1)
        two_hop = _expand_graph([mem_a], max_total=20, hops=2)
        assert {m["id"] for m in one_hop}.issubset({m["id"] for m in two_hop})
        assert mem_c["id"] in {m["id"] for m in two_hop}
        assert mem_c["id"] not in {m["id"] for m in one_hop}
    finally:
        recall._find_memory = orig_find


# ---------------------------------------------------------------------------
# Tests 10-13: classify_layer tests
# DELETED(v2.8): layer system retired — classify_layer function deleted from
# mining.py. Layer auto-classification no longer fires for new memories.
# Explicit layer passthrough (layer=0..3) still works via _make_memory.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 14: topic shift threshold env parses as float
# ---------------------------------------------------------------------------

def test_topic_shift_threshold_env_parse():
    raw = os.environ.get("MEMEM_TOPIC_SHIFT_THRESHOLD", "0.3")
    parsed = float(raw)
    assert isinstance(parsed, float)
    assert 0.0 <= parsed <= 1.0


# ---------------------------------------------------------------------------
# Test 15: session start CLI --compact-index runs without crash
# ---------------------------------------------------------------------------

def test_session_start_cli_runs_without_crash(tmp_vault):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parent.parent)
    env["MEMEM_OBSIDIAN_VAULT"] = str(tmp_vault)

    result = subprocess.run(
        [sys.executable, "-m", "memem.server", "--compact-index"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0
