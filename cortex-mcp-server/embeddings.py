"""Local embedding support using sentence-transformers.

Falls back gracefully if sentence-transformers is not installed.
Uses all-MiniLM-L6-v2 (80MB, runs on CPU, 384 dimensions).
"""
import json
import os
from pathlib import Path

# Cache embeddings to disk so we don't recompute
CACHE_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex"))) / "embeddings_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_model = None
_model_available = None


def is_available() -> bool:
    """Check if sentence-transformers is installed."""
    global _model_available
    if _model_available is None:
        try:
            import sentence_transformers
            _model_available = True
        except ImportError:
            _model_available = False
    return _model_available


def _get_model():
    """Lazy load the model on first use."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed_text(text: str) -> list[float]:
    """Embed a single text string. Returns 384-dim vector."""
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts at once (faster than one-by-one)."""
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True).tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Both should be normalized."""
    # Since we normalize embeddings, dot product = cosine similarity
    return sum(x * y for x, y in zip(a, b))


def get_or_compute_embedding(memory_id: str, text: str) -> list[float]:
    """Get cached embedding or compute and cache it."""
    cache_file = CACHE_DIR / f"{memory_id}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    embedding = embed_text(text)
    try:
        cache_file.write_text(json.dumps(embedding))
    except OSError:
        pass

    return embedding


def clear_cache(memory_id: str):
    """Remove cached embedding when memory content changes."""
    cache_file = CACHE_DIR / f"{memory_id}.json"
    if cache_file.exists():
        cache_file.unlink()
