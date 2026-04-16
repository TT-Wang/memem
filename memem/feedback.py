"""Closed-loop relevance scoring for memem.

Classifies session outcomes by scanning user messages for approval /
correction signals, then updates the relevance scores of memories that
were recalled during that session. This closes the loop from "which
memories were surfaced" → "were they useful" → "adjust future ranking."

Inspired by the MSA paper's end-to-end memory optimization: instead of
training attention weights, we adjust a per-memory relevance-feedback
score via an exponential moving average of session-outcome signals.
"""

import json
import logging
import os
import re

from memem.models import MEMEM_DIR, RELEVANCE_SCORES_FILE
from memem.telemetry import get_session_recalls

log = logging.getLogger("memem-feedback")

# ── Outcome classification ──────────────────────────────────────────

# Patterns are checked against the last N user messages (tail of the
# session). We weight the tail because early messages are often
# exploratory; the last few reveal whether the user was satisfied.
_TAIL_MESSAGES = 10

_POSITIVE_PATTERNS = [
    re.compile(r"\b(yes|perfect|exactly|great|good|correct|nice|thanks|approved|lgtm|ship it)\b", re.I),
    re.compile(r"\b(works?|working|done|merged|pushed|committed)\b", re.I),
]

_NEGATIVE_PATTERNS = [
    re.compile(r"\b(no|wrong|don't|stop|revert|undo|rollback|broken|bug|fail|incorrect)\b", re.I),
    re.compile(r"\b(that's not right|not what I|try again|start over|scrap)\b", re.I),
]


def _classify_session_outcome(messages: list[str]) -> float:
    """Classify a session's outcome from user message signals.

    Pure heuristic — no Haiku call. Scans the last ``_TAIL_MESSAGES``
    user messages for positive (approval, confirmation) and negative
    (correction, rejection) keyword patterns. Returns a score in
    [-1.0, 1.0]:

      -1.0  = strong negative (lots of corrections, reverts)
       0.0  = neutral (mixed signals, short session, or no signal)
       1.0  = strong positive (approvals, successful completion)

    The function filters for user-authored messages (lines starting
    with ``User:``) to avoid counting assistant self-congratulation
    as a positive signal.
    """
    user_msgs = [m for m in messages if m.startswith("User: ")]
    tail = user_msgs[-_TAIL_MESSAGES:] if len(user_msgs) > _TAIL_MESSAGES else user_msgs

    if len(tail) < 2:
        return 0.0

    pos_hits = 0
    neg_hits = 0
    for msg in tail:
        text = msg[6:]  # strip "User: " prefix
        for pat in _POSITIVE_PATTERNS:
            pos_hits += len(pat.findall(text))
        for pat in _NEGATIVE_PATTERNS:
            neg_hits += len(pat.findall(text))

    total = pos_hits + neg_hits
    if total == 0:
        return 0.0

    raw = (pos_hits - neg_hits) / total
    return max(-1.0, min(1.0, raw))


# ── Relevance score updates ─────────────────────────────────────────

_EMA_ALPHA = 0.3  # weight for the new observation; 0.7 for old score


def _load_relevance_scores() -> dict[str, float]:
    if not RELEVANCE_SCORES_FILE.exists():
        return {}
    try:
        return json.loads(RELEVANCE_SCORES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_relevance_scores(scores: dict[str, float]) -> None:
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RELEVANCE_SCORES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(scores, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, RELEVANCE_SCORES_FILE)


def get_relevance_score(memory_id: str) -> float:
    """Read one memory's feedback score. Returns 0.0 (neutral) if unknown."""
    scores = _load_relevance_scores()
    return scores.get(memory_id[:8], 0.0)


def update_relevance_scores(session_id: str, outcome: float) -> None:
    """Update feedback scores for all memories recalled during a session.

    For each memory that was surfaced via ``memory_recall`` or
    ``memory_get`` during ``session_id``, applies an exponential moving
    average: ``new = (1 - α) * old + α * outcome``, where α = 0.3.

    Does nothing if no recalls are recorded for the session (e.g., the
    user didn't trigger any recall during the session, or telemetry
    tracking wasn't active).
    """
    recalled = get_session_recalls(session_id)
    if not recalled:
        return

    scores = _load_relevance_scores()
    for mid in recalled:
        key = mid[:8]
        old = scores.get(key, 0.0)
        scores[key] = (1 - _EMA_ALPHA) * old + _EMA_ALPHA * outcome

    _save_relevance_scores(scores)
    log.info(
        "Updated relevance scores for %d memories (session=%s, outcome=%.2f)",
        len(recalled), session_id[:8], outcome,
    )
