"""Tests for B4 (incremental embedding upsert + mtime cache invalidation)
and B22 (lazy sentence-transformers import in retrieve.py).

B22: retrieve.py must import cleanly when sentence-transformers is absent and
     retrieve() must return [] or FTS-only results gracefully.

B4a: _load_index() must reload when embeddings.npy mtime advances.
B4b: _upsert_embedding() must append/replace a row and update embedding_ids.json.
B4c: _save_memory() must call _upsert_embedding and the id appears in the file.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
import uuid
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# B22: lazy import — retrieve module must load without sentence-transformers
# ---------------------------------------------------------------------------


def test_retrieve_imports_without_sentence_transformers(monkeypatch):
    """retrieve.py must import cleanly even when sentence-transformers is absent."""
    # Install a dummy blocker for sentence_transformers
    blocked = types.ModuleType("sentence_transformers")
    blocked.__spec__ = None  # type: ignore[attr-defined]

    class _BlockedFinder:
        @staticmethod
        def find_module(name, path=None):
            if name == "sentence_transformers" or name.startswith("sentence_transformers."):
                return _BlockedFinder()
            return None

        @staticmethod
        def load_module(name):
            raise ImportError(f"sentence_transformers blocked in test: {name}")

    # Temporarily remove sentence_transformers from sys.modules and block reimport
    orig = sys.modules.pop("sentence_transformers", None)
    sys.meta_path.insert(0, _BlockedFinder())
    # Also remove memem.retrieve so a fresh import is forced
    for k in list(sys.modules):
        if k == "memem.retrieve" or k.startswith("memem.retrieve."):
            sys.modules.pop(k, None)
    try:
        import memem.retrieve  # noqa: PLC0415 — intentional fresh import
        assert hasattr(memem.retrieve, "retrieve"), "retrieve() function must exist"
    finally:
        sys.meta_path.pop(0)
        if orig is not None:
            sys.modules["sentence_transformers"] = orig
        # Reload to restore normal state
        for k in list(sys.modules):
            if k == "memem.retrieve" or k.startswith("memem.retrieve."):
                sys.modules.pop(k, None)
        importlib.import_module("memem.retrieve")


def test_retrieve_degrades_gracefully_without_sentence_transformers(tmp_path, monkeypatch):
    """retrieve() must return [] (not crash) when sentence-transformers absent."""
    # Set up minimal isolated vault
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    vault_dir = tmp_path / "obsidian-brain"
    memories_dir = vault_dir / "memem" / "memories"
    memories_dir.mkdir(parents=True)

    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

    # Write a minimal memory file
    mem_id = str(uuid.uuid4())
    (memories_dir / f"test-{mem_id[:8]}.md").write_text(
        f"---\nid: {mem_id}\ntitle: Test memory\nproject: test\ncreated: '2025-01-01T00:00:00'\n---\n\nTest body.\n",
        encoding="utf-8",
    )

    import memem.models as _models
    importlib.reload(_models)

    # Patch _get_model to simulate sentence-transformers unavailable
    import memem.retrieve as _retrieve
    importlib.reload(_retrieve)
    monkeypatch.setattr(_retrieve, "_model", None)
    original_get_model = _retrieve._get_model

    def _failing_get_model():
        return None  # simulate sentence-transformers absent

    monkeypatch.setattr(_retrieve, "_get_model", _failing_get_model)
    # Clear caches
    _retrieve._vault_idx_cache = None
    _retrieve._vault_idx_mtime = 0
    _retrieve._vault_idx_count = 0
    _retrieve._emb_cache = None

    try:
        result = _retrieve.retrieve("test query")
        # Should return a list (possibly empty or BM25-only), never raise
        assert isinstance(result, list)
    finally:
        importlib.reload(_models)
        importlib.reload(_retrieve)


# ---------------------------------------------------------------------------
# B4a: _load_index mtime invalidation
# ---------------------------------------------------------------------------


def test_load_index_invalidates_on_mtime_change(tmp_path, monkeypatch):
    """_load_index must reload when embeddings.npy mtime is newer than cache."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models as _models
    importlib.reload(_models)
    import memem.embedding_index as ei
    importlib.reload(ei)

    # Write initial index (1 row)
    dim = 8
    ids_v1 = ["aaa"]
    matrix_v1 = np.ones((1, dim), dtype=np.float32)
    emb_path = state_dir / "embeddings.npy"
    ids_path = state_dir / "embedding_ids.json"
    np.save(str(emb_path), matrix_v1)
    ids_path.write_text(json.dumps({"ids": ids_v1}))

    # First load
    assert ei._load_index() is True
    assert ei._index_ids == ids_v1

    # Simulate upsert writing new file — update on disk
    ids_v2 = ["aaa", "bbb"]
    matrix_v2 = np.ones((2, dim), dtype=np.float32) * 0.5
    # Write atomically with a slightly later mtime
    import time as _time
    _time.sleep(0.01)  # ensure mtime advances
    np.save(str(emb_path), matrix_v2)
    ids_path.write_text(json.dumps({"ids": ids_v2}))
    # Touch to ensure mtime is strictly newer
    emb_path.touch()

    # Second load — cache should be stale, must reload
    result = ei._load_index()
    assert result is True
    assert ei._index_ids == ids_v2, f"Expected reload to show ['aaa','bbb'], got {ei._index_ids}"
    assert ei._index_matrix.shape[0] == 2


# ---------------------------------------------------------------------------
# B4b: _upsert_embedding append and replace
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("sentence_transformers"),
    reason="sentence-transformers not installed",
)
def test_upsert_embedding_appends_new_id(tmp_path, monkeypatch):
    """_upsert_embedding must append a new id to the index."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models as _models
    importlib.reload(_models)
    import memem.embedding_index as ei
    importlib.reload(ei)

    mem_id = str(uuid.uuid4())
    ok = ei._upsert_embedding(mem_id, "Test title — Test essence content for embedding")
    assert ok is True

    ids_data = json.loads((state_dir / "embedding_ids.json").read_text())
    ids = ids_data.get("ids", ids_data) if isinstance(ids_data, dict) else ids_data
    assert mem_id in ids, f"{mem_id} not found in {ids}"

    matrix = np.load(str(state_dir / "embeddings.npy"))
    assert matrix.shape[0] == len(ids), "matrix row count must match ids length"


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("sentence_transformers"),
    reason="sentence-transformers not installed",
)
def test_upsert_embedding_replaces_existing_id(tmp_path, monkeypatch):
    """_upsert_embedding must replace the row for an existing id (no duplicates)."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models as _models
    importlib.reload(_models)
    import memem.embedding_index as ei
    importlib.reload(ei)

    mem_id = str(uuid.uuid4())
    # Upsert twice with same id
    ei._upsert_embedding(mem_id, "First version of the text")
    ei._upsert_embedding(mem_id, "Second updated version of the text")

    ids_data = json.loads((state_dir / "embedding_ids.json").read_text())
    ids = ids_data.get("ids", ids_data) if isinstance(ids_data, dict) else ids_data
    assert ids.count(mem_id) == 1, f"id must appear exactly once, got {ids.count(mem_id)}"

    matrix = np.load(str(state_dir / "embeddings.npy"))
    assert matrix.shape[0] == 1, "Only one row expected for one unique id"


# ---------------------------------------------------------------------------
# B4c: _save_memory wires into _upsert_embedding
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("sentence_transformers"),
    reason="sentence-transformers not installed",
)
def test_save_memory_triggers_embedding_upsert(tmp_vault, tmp_cortex_dir):
    """After _save_memory, the memory id must appear in embedding_ids.json."""
    import importlib

    from memem import models, obsidian_store, search_index
    importlib.reload(models)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)

    import memem.embedding_index as ei
    importlib.reload(ei)

    mem = obsidian_store._make_memory(
        content="Embedding upsert integration test content for save_memory wiring",
        title="Embedding upsert integration test",
        tags=["embedding", "test"],
        project="test",
    )
    obsidian_store._save_memory(mem)

    mem_id = mem["id"]
    ids_path = models.MEMEM_DIR / "embedding_ids.json"
    assert ids_path.exists(), "embedding_ids.json should exist after _save_memory"

    ids_data = json.loads(ids_path.read_text())
    ids = ids_data.get("ids", ids_data) if isinstance(ids_data, dict) else ids_data
    assert mem_id in ids, (
        f"memory id {mem_id[:8]}... not found in embedding_ids.json after _save_memory; "
        f"found: {[i[:8] for i in ids]}"
    )
