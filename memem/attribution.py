"""Three-signal usage attribution for memory slice injection.

Per the closed-loop memory research (mem0, Letta, A-MEM, GaRAGe), no single
signal reliably distinguishes 'memory M was injected and influenced the
response' from 'memory M was injected and ignored.' This module computes
three cheap-or-sampled signals; m3+ aggregate them.

References:
  - GaRAGe (arXiv 2506.07671): citation faithfulness benchmark
  - Mem0 (arXiv 2504.19413): per-memory observability
  - A-MemGuard (arXiv 2510.02373): consensus required, single-judge unsafe
"""
from __future__ import annotations

import logging
import os
import random
import re

log = logging.getLogger("memem-attribution")

# 8-char id mention pattern, e.g. "[abc12345]" or "abc12345..." in plain text.
_ID_MENTION_RE = re.compile(r"\b[a-f0-9]{8}\b")


def embedding_similarity(memory_essence: str, response_text: str) -> float:
    """Cosine similarity between memory and response, [0,1]. Cheap.

    Uses memem.embedding_index for the model. Returns 0.0 on any failure.
    """
    if not memory_essence or not response_text:
        return 0.0
    try:
        from memem.embedding_index import _embed_text
        v_mem = _embed_text(memory_essence[:1000])
        v_resp = _embed_text(response_text[:1000])
        if v_mem is None or v_resp is None:
            return 0.0
        # Cosine
        import numpy as np
        a = np.asarray(v_mem, dtype="float32")
        b = np.asarray(v_resp, dtype="float32")
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
        return float(np.dot(a, b) / denom)
    except Exception:
        return 0.0


def citation_match(
    memory_id: str,
    memory_title: str,
    response_text: str,
    memory_essence: str = "",
    semantic_threshold: float = 0.6,
) -> bool:
    """True if the response appears to cite or quote this memory.

    Heuristics (any one is sufficient):
      - The 8-char id prefix appears as a token in the response
      - The first 5 words of the title appear consecutively in the response
        (lowercased, punctuation-tolerant)
      - Embedding similarity between memory_essence and response_text exceeds
        semantic_threshold (only when memory_essence is non-empty)
    """
    if not response_text:
        return False
    short_id = (memory_id or "")[:8].lower()
    if short_id and short_id in response_text.lower():
        return True
    if memory_title:
        # Take first 5 significant words
        words = re.findall(r"\w+", memory_title.lower())[:5]
        if len(words) >= 3:
            phrase = " ".join(words)
            if phrase in re.sub(r"\s+", " ", response_text.lower()):
                return True
    if memory_essence:
        if embedding_similarity(memory_essence, response_text) > semantic_threshold:
            return True
    return False


def should_run_judge(sample_rate: float | None = None) -> bool:
    """Stochastic gate for the expensive LLM-judge call.

    Default sample rate from env MEMEM_JUDGE_SAMPLE_RATE (default 0.05).
    Set to 0 in CI for fast tests.
    """
    if sample_rate is None:
        sample_rate = float(os.environ.get("MEMEM_JUDGE_SAMPLE_RATE", "0.05") or 0.0)
    if sample_rate <= 0:
        return False
    return random.random() < sample_rate


_judge_score_stub_warned = False


def judge_score(memory_essence: str, response_text: str, query: str) -> float | None:
    """LLM-judge memory<->response attribution score.

    **STUB:** This function is a permanent placeholder until the LLM-judge
    integration ships. It always returns None, which causes the
    aggregate_signals() pipeline to skip the LLM-judge signal and rely on
    the embedding-similarity + citation-regex signals alone.

    Callers should treat None as "judge not available" rather than "judge
    says irrelevant" — there is no relevance information from this signal
    until the stub is replaced.
    """
    global _judge_score_stub_warned
    if not _judge_score_stub_warned:
        log.info(
            "judge_score is a stub — LLM-judge attribution disabled; "
            "aggregate_signals will rely on embedding + citation only"
        )
        _judge_score_stub_warned = True
    return None


def aggregate_signals(emb_sim: float, citation: bool, judge: float | None) -> float:
    """Combine the three signals into a single attribution score [0, 1].

    Weights chosen so that:
      - judge dominates when present (it's the most reliable single signal)
      - citation_match is a strong positive boost (~0.3) — high precision
      - embedding similarity contributes a noisy gradient

    Returns 0 when all three are weak — interpretable as 'no evidence of use'.
    """
    score = 0.5 * emb_sim
    if citation:
        score += 0.3
    if judge is not None:
        score = 0.4 * score + 0.6 * judge  # judge is dominant when present
    return min(max(score, 0.0), 1.0)
