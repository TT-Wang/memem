"""Single source of truth for memem v2.0 gating constants.

All constants are read from environment variables at import time.
Tests that need to override values should patch the attribute directly:
    monkeypatch.setattr('memem.settings.MEMEM_INJECTION_MODE', 'tool')
or reload the module after setting the env var.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Injection mode
# ---------------------------------------------------------------------------

MEMEM_INJECTION_MODE: str = os.getenv("MEMEM_INJECTION_MODE", "tool")

# ---------------------------------------------------------------------------
# Recall scoring
# ---------------------------------------------------------------------------

# Minimum per-item score floor for recall results (0.0 = disabled by default).
MEMEM_RECALL_MIN_ITEM_SCORE: float = min(1.0, max(0.0, float(os.getenv("MEMEM_RECALL_MIN_ITEM_SCORE", "0.0"))))

# ---------------------------------------------------------------------------
# Access writeback (telemetry sidecar)
# ---------------------------------------------------------------------------

# When enabled, retrieve() fires a daemon thread to record access counts for
# each cosine hit via telemetry._record_access. Default ON (set to "0" to disable).
MEMEM_WRITEBACK_ENABLED: bool = os.getenv("MEMEM_WRITEBACK_ENABLED", "1") not in ("0", "false", "False", "no")
