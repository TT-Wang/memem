"""Single source of truth for memem v2.0 gating constants.

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
# Trivial-query regex patterns (hybrid mode)
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
# Access writeback (telemetry sidecar)
# ---------------------------------------------------------------------------

# When enabled, retrieve() fires a daemon thread to record access counts for
# each cosine hit via telemetry._record_access. Default ON (set to "0" to disable).
MEMEM_WRITEBACK_ENABLED: bool = os.getenv("MEMEM_WRITEBACK_ENABLED", "1") not in ("0", "false", "False", "no")

# ---------------------------------------------------------------------------
# Recency decay scoring (v2.3.0 scaffolded, currently no-op)
# ---------------------------------------------------------------------------

# v2.3.0: NO-OP. Decay scoring was prototyped during v2.3.0 (m4) but reverted
# after a 74% → 70% benchmark regression caused by multiplying negative cosine
# scores. The scaffold (this setting + decay tests + commented call site in
# retrieve.py) is retained for v2.4.0 follow-up, which will land negative-score
# clamping before re-enabling the multiplier. Default is OFF — flipping to "1"
# today has zero effect; flipping it post v2.4.0 will activate decay scoring.
MEMEM_DECAY_ENABLED: bool = os.getenv("MEMEM_DECAY_ENABLED", "0") not in ("0", "false", "False", "no")

