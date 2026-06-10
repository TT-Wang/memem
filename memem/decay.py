"""Damped decay with L0 pin (memem v2 m3).

Computes per-memory strength using a MemoryBank-style Ebbinghaus formula
modulated by RISM-style 3-phase dynamic attenuation to prevent runaway
reinforcement. L0 memories and decay_immune-flagged memories are exempt.

This module is COMPUTE + READ only. It produces 'should_demote' suggestions
that m4 dreamer may execute. m3 never mutates layer assignments directly.

Access counts are read from the telemetry sidecar (~/.memem/telemetry.json),
NOT from the frontmatter access_count field. The frontmatter field is
initialized to 0 and is never incremented; real counts live in the sidecar.

References:
  - MemoryBank (arXiv 2305.10250): exponential strength + access reinforcement
  - Neural Howlround / RISM (arXiv 2504.07992): 3-phase dynamic attenuation
    against runaway reinforcement
  - A-MEM (arXiv 2502.12110): reconsolidation on access
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

from memem.models import DEFAULT_LAYER, LAYER_L0, parse_iso_dt
from memem.telemetry import _get_telemetry

# Tunables — keep conservative; the dreamer (m4) will rarely act on borderline scores
DECAY_HALF_LIFE_DAYS = 30.0     # base — a memory accessed once and then ignored loses half its strength in 30 days
ACCESS_REINFORCEMENT = 0.5      # m += this on each access (MemoryBank parameter)
DEMOTE_THRESHOLD = 0.2          # below this strength, suggest demotion
PHI_DAMPING_KNEE = 5            # access_count beyond which phi damping kicks in
LOG_DAMPING_KNEE = 20           # access_count beyond which log damping dominates


def _hours_since(ts: str, now: datetime | None = None) -> float:
    parsed = parse_iso_dt(ts)
    if not parsed:
        return 0.0
    now = now or datetime.now(UTC)
    delta = (now - parsed).total_seconds() / 3600.0
    return max(0.0, delta)


def compute_decay_factor(access_count: int) -> float:
    """3-phase dynamic attenuation (RISM-style) on the access reinforcement.

    Returns multiplier in (0, 1]: how much each additional access reinforces.

    Phase 1 (exponential): early accesses fully reinforce (multiplier == 1.0)
    Phase 2 (phi-function): mid range — multiplier sublinearly grows
    Phase 3 (log dampening): heavy users get diminishing reinforcement —
                              prevents always-injected memories from runaway

    This is the closed-loop safety: a memory injected on every prompt cannot
    grow its strength without bound.
    """
    n = max(0, int(access_count))
    if n <= PHI_DAMPING_KNEE:
        return 1.0  # exponential phase: every access counts fully
    if n <= LOG_DAMPING_KNEE:
        # phi-function (golden-ratio inspired smooth damping)
        x = (n - PHI_DAMPING_KNEE) / max(1, LOG_DAMPING_KNEE - PHI_DAMPING_KNEE)
        return 1.0 - 0.5 * x  # 1.0 at knee, 0.5 at log knee
    # KNOWN: non-monotonic at phase boundary, recalibration deferred to v2.6
    # log dampening — heavy reinforcement is heavily damped
    return 0.5 / (1.0 + math.log1p(n - LOG_DAMPING_KNEE))


def compute_strength(memory: dict, now: datetime | None = None) -> float:
    """MemoryBank-style strength with damped reinforcement and importance weight.

    S = importance_weight * exp(-hours_since_access / (HALF_LIFE * effective_m))
    where effective_m = 1 + ACCESS_REINFORCEMENT * access_count * damping_factor

    Access counts and last-accessed timestamp are read from the telemetry
    sidecar (~/.memem/telemetry.json), not from frontmatter fields.

    Returns float in (0, 1+importance_bonus]. Higher = stronger / fresher.
    """
    now = now or datetime.now(UTC)
    mem_id = memory.get("id", "")
    tel = _get_telemetry(mem_id)
    access_count = int(tel.get("access_count", 0) or 0)
    importance = float(memory.get("importance", 3) or 3)
    # Use telemetry last_accessed first; fall back to frontmatter fields then
    # created_at. `updated_at` is in the chain so decay and the recall.py
    # scoring paths (which fall back to updated_at) compute the same
    # effective age for the same memory.
    last_access = (
        tel.get("last_accessed")
        or memory.get("last_accessed_at")
        or memory.get("updated_at")
        or memory.get("created_at", "")
    )

    hours = _hours_since(last_access, now)
    half_life_hours = DECAY_HALF_LIFE_DAYS * 24.0

    damping = compute_decay_factor(access_count)
    effective_m = 1.0 + ACCESS_REINFORCEMENT * access_count * damping

    # Convert half-life to time constant: tau = half_life / ln(2)
    tau = (half_life_hours * effective_m) / math.log(2)
    base_strength = math.exp(-hours / tau) if tau > 0 else 0.0

    # Importance weight (1-5 scale -> 0.6 to 1.4 multiplier)
    importance_weight = 0.6 + 0.2 * (importance - 1)

    return base_strength * importance_weight


def is_immune(memory: dict) -> bool:
    """Hard immunity check — L0 always immune; decay_immune=True opts in others."""
    raw_layer = memory.get("layer")
    # Use explicit None check so layer=0 (L0) is not treated as falsy
    layer = int(raw_layer) if raw_layer is not None else DEFAULT_LAYER
    if layer == LAYER_L0:
        return True
    return bool(memory.get("decay_immune", False))


def should_demote(memory: dict, now: datetime | None = None) -> int | None:
    """Suggest a new layer if strength has dropped below threshold.

    Returns:
        None  — no change suggested (immune, or strength still adequate)
        int   — suggested new layer (typically current+1, capped at L3)

    m3 is read-only — this is a SUGGESTION; m4 dreamer decides whether to act.
    """
    if is_immune(memory):
        return None
    strength = compute_strength(memory, now)
    if strength >= DEMOTE_THRESHOLD:
        return None
    raw_layer = memory.get("layer")
    current_layer = int(raw_layer) if raw_layer is not None else DEFAULT_LAYER
    if current_layer >= 3:
        return None  # already at L3
    return current_layer + 1
