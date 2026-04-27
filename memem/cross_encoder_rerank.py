"""Cross-encoder reranker for memem recall.

Optional dependency: sentence_transformers is NOT required at import time.
The module uses lazy-load singleton pattern per model name and degrades
gracefully to identity ordering when the library is unavailable or the
model fails to load.

Intended usage
--------------
After the 6-signal heuristic rerank produces a sorted candidate list,
callers can optionally pass the top-50 through this module to apply a
neural cross-encoder score. Because cross-encoders jointly encode
(query, passage) pairs, they catch semantic relevance that bag-of-words
heuristics miss (paraphrases, negation, nuanced specificity).

Available models
----------------
_DEFAULT_FAST_MODEL  (default)
    "cross-encoder/ms-marco-MiniLM-L-12-v2"
    ~33M params. ~30-80ms CPU inference on 50 pairs. English-only.

_DEFAULT_HQ_MODEL  (opt-in, not tested by default due to 568MB download)
    "BAAI/bge-reranker-v2-m3"
    ~568M params. Multilingual, higher quality, slower.
    Enable by passing model_name="BAAI/bge-reranker-v2-m3" explicitly.

Thread safety
-------------
model.predict() is thread-safe in sentence-transformers ≥ 2.x.
The load lock prevents duplicate model downloads on first use.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

log = logging.getLogger("memem-cross-encoder")

_DEFAULT_FAST_MODEL = "cross-encoder/ms-marco-MiniLM-L-12-v2"
_DEFAULT_HQ_MODEL = "BAAI/bge-reranker-v2-m3"

# Singleton cache: model_name -> CrossEncoder instance (or None if unavailable)
_model_cache: dict[str, CrossEncoder | None] = {}
_load_lock = threading.Lock()


def _get_model(model_name: str) -> CrossEncoder | None:
    """Return a cached CrossEncoder for *model_name*, or None on failure.

    Thread-safe: double-checked locking with a module-level threading.Lock.
    Caches None permanently so repeated calls after a failed load don't retry.
    """
    if model_name in _model_cache:
        return _model_cache[model_name]

    with _load_lock:
        # Re-check after acquiring lock (another thread may have loaded it)
        if model_name in _model_cache:
            return _model_cache[model_name]

        try:
            from sentence_transformers import CrossEncoder as _CrossEncoder

            model = _CrossEncoder(model_name)
            _model_cache[model_name] = model
            log.debug("cross-encoder loaded: %s", model_name)
            return model
        except ImportError:
            log.warning(
                "sentence_transformers is not installed; cross-encoder rerank disabled. "
                "Install it with: pip install sentence-transformers"
            )
            _model_cache[model_name] = None
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("cross-encoder model load failed (%s): %s", model_name, exc)
            _model_cache[model_name] = None
            return None


def rerank_pairs(
    query: str,
    candidates: list[dict],
    model_name: str | None = None,
    top_k: int = 50,
) -> list[tuple[str, float]]:
    """Return [(memory_id, score), ...] sorted descending by cross-encoder score.

    Parameters
    ----------
    query:
        The search query string.
    candidates:
        List of memory dicts. Each must have 'id'. Text is built from
        'title' + ('essence' or 'content'[:500]).
    model_name:
        Name of the cross-encoder model to load. Defaults to
        _DEFAULT_FAST_MODEL. Pass None explicitly to get identity ordering
        (no model loaded, useful for opt-out / testing).
    top_k:
        Maximum number of candidates to score. Excess candidates beyond
        top_k are dropped before scoring (they were already ranked lower
        by the heuristic pass).

    Returns
    -------
    list[tuple[str, float]]
        (memory_id, score) pairs sorted descending. Scores are raw
        cross-encoder logits (typically in [-10, 10] for ms-marco models).
        When fallback triggers, scores are descending integers: the
        first candidate gets score len(candidates)-1, second gets
        len(candidates)-2, etc. — preserving the original order stably.

    Failure modes
    -------------
    - model_name is None: returns identity ordering immediately (no model load).
    - sentence_transformers unavailable: logs WARNING, returns identity ordering.
    - Model load fails: logs WARNING, returns identity ordering.
    - predict() raises: logs WARNING, returns identity ordering.
    Never raises to the caller.
    """
    working = candidates[:top_k]
    total = len(working)

    def _identity() -> list[tuple[str, float]]:
        return [(c.get("id", ""), float(total - idx - 1)) for idx, c in enumerate(working)]

    if model_name is None:
        return _identity()

    model = _get_model(model_name)
    if model is None:
        return _identity()

    try:
        pairs = []
        for c in working:
            title = c.get("title", "")
            body = c.get("essence", "") or c.get("content", "")
            if body:
                body = body[:500]
            text = f"{title} {body}".strip()[:512]
            pairs.append([query, text])

        scores: list[float] = model.predict(pairs).tolist()

        return sorted(
            zip([c.get("id", "") for c in working], scores, strict=True),
            key=lambda x: x[1],
            reverse=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("cross-encoder predict failed: %s", exc)
        return _identity()
