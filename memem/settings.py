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

MEMEM_INJECT_CADENCE: int = int(os.getenv("MEMEM_INJECT_CADENCE", "2"))
MEMEM_TOPIC_SHIFT_THRESHOLD: float = float(os.getenv("MEMEM_TOPIC_SHIFT_THRESHOLD", "0.85"))
MEMEM_EMPTY_STREAK_MAX: int = int(os.getenv("MEMEM_EMPTY_STREAK_MAX", "8"))

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
