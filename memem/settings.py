"""Single source of truth for memem v1.9 gating constants.

All constants are read from environment variables at import time.
Tests that need to override values should patch the attribute directly:
    monkeypatch.setattr('memem.settings.MEMEM_INJECT_CADENCE', 3)
or reload the module after setting the env var.
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Injection mode
# ---------------------------------------------------------------------------

MEMEM_INJECTION_MODE: str = os.getenv("MEMEM_INJECTION_MODE", "auto")

# ---------------------------------------------------------------------------
# Cadence and backoff
# ---------------------------------------------------------------------------

# Cadence is clamped to >= 1: a value of 0 would cause ZeroDivisionError in the
# modulo check that decides cadence-skip. Negative values are similarly nonsense.
MEMEM_INJECT_CADENCE: int = max(1, int(os.getenv("MEMEM_INJECT_CADENCE", "2")))
# Topic-shift threshold is clamped to [0.0, 1.0] — cosine similarity range.
MEMEM_TOPIC_SHIFT_THRESHOLD: float = min(1.0, max(0.0, float(os.getenv("MEMEM_TOPIC_SHIFT_THRESHOLD", "0.85"))))
# Minimum activation confidence to emit recall context (C2 — tunable threshold).
MEMEM_RECALL_MIN_CONFIDENCE: float = min(1.0, max(0.0, float(os.getenv("MEMEM_RECALL_MIN_CONFIDENCE", "0.45"))))
# Minimum per-item score floor for recall results (C3 — 0.0 = disabled by default).
MEMEM_RECALL_MIN_ITEM_SCORE: float = min(1.0, max(0.0, float(os.getenv("MEMEM_RECALL_MIN_ITEM_SCORE", "0.0"))))
# Out-of-vault detection threshold (C4 — 0.0 = disabled by default; opt-in via env).
MEMEM_RECALL_OOV_THRESHOLD: float = min(1.0, max(0.0, float(os.getenv("MEMEM_RECALL_OOV_THRESHOLD", "0.0"))))
# Empty-streak cap is clamped to >= 0 (0 disables backoff, negative is nonsense).
MEMEM_EMPTY_STREAK_MAX: int = max(0, int(os.getenv("MEMEM_EMPTY_STREAK_MAX", "8")))

# ---------------------------------------------------------------------------
# Trivial-query regex patterns
# ---------------------------------------------------------------------------

# English trivial queries — short acknowledgements, affirmations, negations, etc.
MEMEM_TRIVIAL_REGEX_EN: re.Pattern[str] = re.compile(
    r"^(?:"
    r"yes|no|ok|okay|sure|thanks|thank\s+you|ty|k|kk|cool|got\s+it"
    r"|yep|yup|nope|nah|uh[-\s]?huh|uh[-\s]?uh|mm[-\s]?hmm|hmm"
    r"|right|alright|all\s+right|fine|great|nice|good|sounds?\s+good"
    r"|perfect|awesome|noted|understood|copy\s+that|roger"
    r"|continue|go\s+on|proceed|next|more|keep\s+going"
    r"|done|ready|wait|hold\s+on"
    r")$",
    re.IGNORECASE,
)

# Chinese trivial queries — short acknowledgements, affirmations, negations, etc.
MEMEM_TRIVIAL_REGEX_ZH: re.Pattern[str] = re.compile(
    r"^(?:"
    r"好|好的|好吧|好啊|嗯|是|是的|不|不是|对|对的|行|可以|行吧"
    r"|谢谢|谢谢你|谢了|多谢|了解|明白|知道了|收到|继续|好的继续"
    r"|没问题|没事|算了|等等|稍等|继续|下一步|好了|准备好了"
    r")$",
)

# ---------------------------------------------------------------------------
# LLM judge gate (v1.13.0)
# ---------------------------------------------------------------------------

# Enable/disable the LLM (Haiku) activation judge. Default ON ('1').
# Set MEMEM_USE_LLM_JUDGE=0 to force the heuristic path on all entry points.
# CRITICAL: read from os.environ at import time — NOT from the per-call environment
# dict (which is filtered by normalize_runtime_environment whitelist).
MEMEM_USE_LLM_JUDGE: bool = os.getenv("MEMEM_USE_LLM_JUDGE", "1") != "0"

# Hard timeout (seconds) for the LLM judge subprocess on the hook hot path.
# Default 2s — the hook cannot block longer or prompt injection is delayed.
# Clamped to >= 0.5 to avoid pathological values.
MEMEM_LLM_JUDGE_TIMEOUT: float = max(0.5, float(os.getenv("MEMEM_LLM_JUDGE_TIMEOUT", "2.0")))

# ---------------------------------------------------------------------------
# Embedding retrieval gate (v1.13.0)
# ---------------------------------------------------------------------------

# Enable/disable embedding-based retrieval in the hybrid recall pipeline.
# Default ON ('1'). Set MEMEM_USE_EMBEDDINGS=0 to fall back to FTS-only recall.
# CRITICAL: read from os.environ at import time — NOT from the per-call environment
# dict (which is filtered by normalize_runtime_environment whitelist).
# Tests override this via monkeypatch.setattr(settings, 'MEMEM_USE_EMBEDDINGS', ...).
MEMEM_USE_EMBEDDINGS: bool = os.getenv("MEMEM_USE_EMBEDDINGS", "1") != "0"


def _embeddings_enabled() -> bool:
    """Return True if embedding retrieval is enabled.

    Re-reads `MEMEM_USE_EMBEDDINGS` from os.environ on every call so that
    runtime env changes (e.g. emergency rollback against a long-running
    daemon) take effect immediately. Falls back to the module attribute if
    the env var is unset, so monkeypatch.setattr-based tests still work.
    """
    raw = os.environ.get("MEMEM_USE_EMBEDDINGS")
    if raw is None:
        return bool(MEMEM_USE_EMBEDDINGS)
    return raw != "0"


def _llm_judge_enabled() -> bool:
    """Return True if the LLM activation judge is enabled.

    Re-reads `MEMEM_USE_LLM_JUDGE` from os.environ on every call so that
    runtime env changes (e.g. emergency rollback against a long-running
    daemon) take effect immediately. Falls back to the module attribute if
    the env var is unset, so monkeypatch.setattr-based tests still work.
    """
    raw = os.environ.get("MEMEM_USE_LLM_JUDGE")
    if raw is None:
        return bool(MEMEM_USE_LLM_JUDGE)
    return raw != "0"


def _render_legacy_enabled() -> bool:
    """Return True if the v1.12.0 legacy renderer should be used.

    Re-reads `MEMEM_RENDER_LEGACY` from os.environ on every call so users
    can flip the flag at runtime to roll back the v1.13.0 schema without
    restarting the slice daemon.
    """
    return os.environ.get("MEMEM_RENDER_LEGACY") == "1"
