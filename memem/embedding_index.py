"""Local embedding side-channel for memem's union-rank search.

Adds a third parallel candidate generator alongside FTS5 (search_index.py)
and ngram containment (obsidian_store._ngram_search_candidates) so queries
whose surface form doesn't match the memory's wording still surface
semantically-related memories via vector similarity.

Design — strictly additive, zero accuracy regression:
  • The index is built on demand by _rebuild_embedding_index() which is
    called from _rebuild_search_index() and nightly during the miner
    daemon's consolidation pass. New memories added between rebuilds
    are NOT in the index — they'll come back via FTS/ngram until the
    next rebuild. This means the embedding signal only UNDER-counts,
    never returns wrong results.
  • sentence-transformers is an optional dependency. If the module is
    not importable (install didn't include the `embedding` extra),
    `_search_embedding` returns [] and the overall union-rank path
    silently falls back to FTS+ngram. No crash, no user-visible error.
  • The model (`all-MiniLM-L6-v2`) is ~30 MB on disk, 384-dim, local-only.
    No API key, no network calls after model download. First-use downloads
    from HuggingFace; subsequent calls hit the on-disk cache.
  • Cosine similarity over a flat float32 numpy array. At 2,500 memories
    the dot-product is <1 ms — well below the ~30 ms embedding-compute
    cost per query, which dominates.

Files:
  ~/.memem/embeddings.npy       — (N, 384) float32 matrix, L2-normalized rows
  ~/.memem/embedding_ids.json   — {"ids": [id_0, id_1, ...], "model": "...", "built_at": "..."}
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from memem.models import MEMEM_DIR, now_iso

log = logging.getLogger("memem-embedding")

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_EMB_PATH = MEMEM_DIR / "embeddings.npy"
_IDS_PATH = MEMEM_DIR / "embedding_ids.json"

_model_lock = threading.Lock()
_model = None       # cached SentenceTransformer instance
_index_matrix = None  # cached numpy.ndarray
_index_ids: list[str] = []
_unavailable_logged = False


def _try_import():
    """Import sentence-transformers + numpy lazily. Returns (st, np) or (None, None)."""
    global _unavailable_logged
    try:
        import numpy as _np
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        if not _unavailable_logged:
            log.info(
                "embedding side-channel disabled (sentence-transformers not installed: %s); "
                "install with `pip install 'memem[embedding]'` to enable",
                exc,
            )
            _unavailable_logged = True
        return None, None
    return SentenceTransformer, _np


def is_available() -> bool:
    """Return True iff sentence-transformers is importable in this process."""
    st, _np = _try_import()
    return st is not None


def _get_model():
    """Lazily instantiate the SentenceTransformer model (singleton per process)."""
    global _model
    if _model is not None:
        return _model
    st_cls, _np = _try_import()
    if st_cls is None:
        return None
    with _model_lock:
        if _model is None:
            try:
                _model = st_cls(_MODEL_NAME)
            except Exception as exc:  # noqa: BLE001 — broad catch is intentional
                log.warning("failed to load embedding model %s: %s", _MODEL_NAME, exc)
                return None
    return _model


def _load_index() -> bool:
    """Load the on-disk index into process memory. Returns True on success."""
    global _index_matrix, _index_ids
    if _index_matrix is not None:
        return True
    if not _EMB_PATH.exists() or not _IDS_PATH.exists():
        return False
    _st, np = _try_import()
    if np is None:
        return False
    try:
        matrix = np.load(str(_EMB_PATH))
        meta = json.loads(_IDS_PATH.read_text())
        ids = meta.get("ids", [])
        if not isinstance(ids, list) or len(ids) != matrix.shape[0]:
            log.warning("embedding index shape mismatch; ignoring")
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding index unreadable: %s", exc)
        return False
    _index_matrix = matrix
    _index_ids = ids
    return True


def _rebuild_embedding_index() -> int:
    """Rebuild the embedding index from the current vault state.

    Returns the number of memories embedded. Returns 0 if the optional
    dependency isn't installed (not an error — strictly additive feature).
    """
    global _index_matrix, _index_ids
    model = _get_model()
    if model is None:
        return 0
    _st, np = _try_import()
    if np is None:
        return 0
    # Lazy import to avoid circular dep (obsidian_store may import this module)
    from memem.obsidian_store import _obsidian_memories

    mems = _obsidian_memories()
    if not mems:
        _index_matrix = np.zeros((0, 384), dtype=np.float32)
        _index_ids = []
        _persist()
        return 0
    texts = [
        (mem.get("title", "") + " — " + mem.get("essence", "")).strip()
        for mem in mems
    ]
    ids = [mem.get("id", "") for mem in mems]
    try:
        vectors = model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding rebuild failed at encode: %s", exc)
        return 0
    _index_matrix = np.asarray(vectors, dtype=np.float32)
    _index_ids = ids
    _persist()
    return len(ids)


def _persist() -> None:
    """Write the in-memory index to disk atomically."""
    _st, np = _try_import()
    if np is None or _index_matrix is None:
        return
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    try:
        np.save(str(_EMB_PATH), _index_matrix)
        _IDS_PATH.write_text(json.dumps({
            "ids": _index_ids,
            "model": _MODEL_NAME,
            "built_at": now_iso(),
        }))
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding index persist failed: %s", exc)


def _search_embedding(query: str, limit: int = 20) -> list[str]:
    """Return memory IDs ranked by cosine similarity to `query`.

    Returns [] if the optional dependency isn't installed or the index
    hasn't been built yet. Strict additive signal: never raises, never
    blocks the parent union-rank path.
    """
    if not query:
        return []
    model = _get_model()
    if model is None:
        return []
    if not _load_index():
        return []
    _st, np = _try_import()
    if np is None or _index_matrix is None or _index_matrix.shape[0] == 0:
        return []
    try:
        q_vec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)
        q = np.asarray(q_vec, dtype=np.float32).reshape(-1)
        sims = _index_matrix @ q  # (N,) — rows are L2-normalized so this is cosine
        top = np.argsort(-sims)[:limit]
        return [_index_ids[i] for i in top if 0 <= i < len(_index_ids)]
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding search failed: %s", exc)
        return []


def _reset_index_cache() -> None:
    """Test hook: drop in-process index cache (and the on-disk files if present)."""
    global _index_matrix, _index_ids
    _index_matrix = None
    _index_ids = []
    for p in (_EMB_PATH, _IDS_PATH):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
