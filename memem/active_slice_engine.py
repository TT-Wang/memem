"""Active Memory Slice Engine orchestration."""

from __future__ import annotations

import dataclasses
import json
import os

# Ensure structlog writes to stderr by default (e.g. in test or CLI contexts
# where the daemon has not called structlog.configure yet). configure_once is
# a no-op if the daemon has already configured structlog.
import sys as _sys
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import structlog

structlog.configure_once(logger_factory=structlog.PrintLoggerFactory(_sys.stderr))

import memem.settings as _memem_settings
from memem.activation import judge_activation_heuristically, judge_activation_with_llm
from memem.active_slice import (
    ActivationResult,
    ActiveMemoryItem,
    ActiveMemorySlice,
    Candidate,
    CandidateBundle,
    DeltaWritebackResult,
    WritebackSummary,
    build_active_memory_slice,
    flatten_candidate_bundle,
    render_slice_as_compact_context,
    render_slice_as_prompt_context,
)
from memem.boundaries import apply_post_boundaries, apply_pre_boundaries
from memem.delta import propose_deltas_from_slice
from memem.delta_commit import commit_deltas
from memem.delta_policy import evaluate_delta_proposals
from memem.environment_context import (
    normalize_runtime_environment,
)
from memem.kind_classifier import infer_kind as _infer_kind
from memem.models import LAYER_L0, _normalize_scope_id, parse_iso_dt
from memem.slice_history import (
    annotate_slice_continuity,
    get_empty_streak,
    increment_empty_streak,
    increment_turn_count,
    load_slice_history,
    persist_slice_history,
    reset_empty_streak,
)

log = structlog.get_logger("memem-active-slice")

PromptContextMode = Literal["slice", "assembly"]
WritebackExecutionMode = Literal["policy_only", "preview", "commit"]


@dataclasses.dataclass(frozen=True)
class SliceGenRequest:
    """Parameters for _generate_active_memory_slice_internal (F-001)."""

    scope_id: str
    environment: dict | None
    use_llm: bool = True
    writeback_mode: WritebackExecutionMode = "policy_only"
    auto_commit_safe: bool = False
    dry_run: bool = True
    persist_history: bool = True


_OOV_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "and", "or", "but", "not",
    "to", "of", "in", "on", "at", "for", "with", "by", "as", "from",
})

# v1.10.1: pure-acknowledgment tokens for the auto-mode trivial-ack gate.
# Conservative set — only the most unambiguous single-word acknowledgements.
_AUTO_PURE_ACK_TOKENS = frozenset({
    "yes", "no", "ok", "okay", "go", "sure", "thanks", "thx",
    "y", "n", "true", "false", "确认", "好", "好的", "是", "对",
})


# v1.11.0: inlined from former active_slice_metrics.py — single helper, single caller.
def summarize_slice_metrics(slice_obj: ActiveMemorySlice) -> dict[str, Any]:
    """Return a compact, structured summary of a generated slice."""
    writeback_summary = slice_obj.get("writeback_summary", {})
    artifact_progression = slice_obj.get("artifact_progression", {})
    slice_diff = slice_obj.get("slice_diff", {})
    return {
        "slice_id": slice_obj.get("slice_id", ""),
        "previous_slice_id": slice_obj.get("previous_slice_id", ""),
        "scope_id": slice_obj.get("scope_id", "default"),
        "query": slice_obj.get("query", ""),
        "task_mode": slice_obj.get("task_mode", ""),
        "activation_mode": slice_obj.get("activation_mode", "heuristic"),
        "candidate_count": int(slice_obj.get("candidate_count", 0) or 0),
        "recall_candidate_count": int(slice_obj.get("recall_candidate_count", 0) or 0),
        "goals_count": len(slice_obj.get("goals", [])),
        "constraints_count": len(slice_obj.get("constraints", [])),
        "active_background_count": len(slice_obj.get("active_background", [])),
        "decisions_count": len(slice_obj.get("decisions", [])),
        "preferences_count": len(slice_obj.get("preferences", [])),
        "failure_patterns_count": len(slice_obj.get("failure_patterns", [])),
        "artifacts_count": len(slice_obj.get("artifacts", [])),
        "open_tensions_count": len(slice_obj.get("open_tensions", [])),
        "resolved_tensions_count": len(slice_obj.get("resolved_tensions", [])),
        "carry_forward_summary_count": len(slice_obj.get("carry_forward_summary", [])),
        "excluded_candidates_count": len(slice_obj.get("excluded_candidates", [])),
        "candidate_deltas_count": len(slice_obj.get("candidate_deltas", [])),
        "delta_results_count": len(slice_obj.get("delta_results", [])),
        "slice_diff_keys": len(slice_diff) if isinstance(slice_diff, dict) else 0,
        "artifact_progression_stage": artifact_progression.get("stage", "") if isinstance(artifact_progression, dict) else "",
        "writeback_status": writeback_summary.get("status", "") if isinstance(writeback_summary, dict) else "",
        "writeback_manual_review_count": int(writeback_summary.get("manual_review_count", 0) or 0) if isinstance(writeback_summary, dict) else 0,
        "writeback_blocked_count": int(writeback_summary.get("blocked_count", 0) or 0) if isinstance(writeback_summary, dict) else 0,
        "should_emit_context": bool(slice_obj.get("should_emit_context", False)),
        "confidence": float(slice_obj.get("confidence", 0.0) or 0.0),
        "warnings_count": len(slice_obj.get("warnings", [])),
    }


def _detect_out_of_vault(query: str, candidate_bundle: CandidateBundle, threshold: float) -> bool:
    """Return True if query has no vault match.

    Heuristic: max memory candidate score < threshold AND no L0 anchor's
    title/tags contain any query token. Returns False (in-vault) when
    threshold <= 0.0 to act as a clean opt-out guard.
    """
    import re
    if threshold <= 0.0:
        return False
    all_candidates = flatten_candidate_bundle(candidate_bundle)
    memory_candidates = [c for c in all_candidates if c.get("candidate_type") == "memory"]
    max_memory_score = max((float(c.get("score", 0.0)) for c in memory_candidates), default=0.0)
    if max_memory_score >= threshold:
        return False
    # Check L0 anchor overlap
    query_tokens = set(re.findall(r'\w{3,}', query.lower())) - _OOV_STOPWORDS
    if not query_tokens:
        return False  # too-short query — don't claim out-of-vault
    l0_anchors = [c for c in all_candidates if c.get("layer") == LAYER_L0]
    for anchor in l0_anchors:
        title = str(anchor.get("title", "")).lower()
        if any(tok in title for tok in query_tokens):
            return False
    return True



# F-002b: extracted to candidate_generation.py — import and re-export for backward compat.
# Phase 4.5 fix: expanded from 2 → 11 names (incl. _MAX_* constants) so all CHANGELOG-promised
# re-exports actually resolve, and scripts/slice_quality_probe.py keeps working.
from memem.candidate_generation import (  # noqa: E402, F401
    _MAX_ARTIFACT_CANDIDATES,
    _MAX_GRAPH_CANDIDATES,
    _MAX_MEMORY_CANDIDATES,
    _MAX_TRANSCRIPT_CANDIDATES,
    _dedupe_candidates,
    _detect_tie_zone,
    _gather_l0_anchors,
    _graph_candidates,
    _playbook_candidate,
    _tournament_break_ties,
    _transcript_candidates,
    generate_candidates,
)


def build_slice(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool | None = None,
) -> ActiveMemorySlice:
    """Build and return a structured ActiveMemorySlice dict without rendering.

    This is the preferred entry point for callers that need the slice structure
    (e.g. context_assemble merging multiple slices). When use_llm is None (the
    default), the MEMEM_USE_LLM_JUDGE env-var setting is consulted. Pass
    use_llm=False explicitly to force heuristic regardless of the env var.

    History persistence is intentionally disabled: assembly callers will call
    this for multiple scopes and should not create spurious history records.
    """
    if use_llm is None:
        use_llm = _memem_settings._llm_judge_enabled()
    return _generate_active_memory_slice_internal(
        query,
        SliceGenRequest(
            scope_id=scope_id,
            environment=environment,
            use_llm=use_llm,
            writeback_mode="policy_only",
            auto_commit_safe=False,
            dry_run=True,
            persist_history=False,
        ),
    )


def _make_gating_stub(
    query: str,
    scope_id: str,
    session_id: str,
    gating_reason: str,
) -> ActiveMemorySlice:
    """Return a minimal ActiveMemorySlice that signals the caller to skip injection."""
    return cast(ActiveMemorySlice, {
        "slice_id": "",
        "session_id": session_id,
        "scope_id": scope_id,
        "query": query,
        "input_goal": query,
        "generated_at": "",
        "goals": [],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "delta_results": [],
        "candidate_count": 0,
        "recall_candidate_count": 0,
        "should_emit_context": False,
        "activation_mode": "heuristic",
        "confidence": 0.0,
        "warnings": [],
        "items": [],
        "slice_kind": "active",
        "gating_reason": gating_reason,
    })


def generate_active_memory_slice(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> ActiveMemorySlice:
    """Main Active Memory Slice Engine entrypoint.

    v1.9 gating: unless MEMEM_INJECTION_MODE=='auto', trivial queries (short
    acknowledgements, slash-commands) are short-circuited before the full
    pipeline.  Non-trivial queries are subject to cadence gating with
    exponential backoff when the pipeline repeatedly returns empty results.
    """
    # -----------------------------------------------------------------
    # Read ALL gating constants from memem.settings (NOT from environment).
    # The normalize_runtime_environment() whitelist silently drops unknown
    # keys, making env-dict gating unreliable.
    # -----------------------------------------------------------------
    injection_mode = _memem_settings.MEMEM_INJECTION_MODE

    # 'auto' → legacy behaviour, skip gating entirely.
    # Also skip gating when no session_id is provided — per-session counters
    # are meaningless without a stable session identity.
    #
    # CARVE-OUT: when session_id is empty, gating is silently skipped and the
    # full pipeline runs. Callers (CLI smoke tests, ad-hoc scripts) that omit
    # session_id will NOT see hybrid/tool mode behaviour. Hook callers always
    # pass session_id, so this affects only opt-out / debug code paths.
    env = environment or {}
    session_id = str(env.get("session_id", "") or "")

    # v1.10.1: auto-mode trivial-ack gate. Only fires for pure-acknowledgment queries
    # (≤3 tokens, all tokens in the ack set). Conservative on purpose — false-positive
    # cost (skipping a legit query) is much higher than false-negative (still injecting).
    if injection_mode == "auto" and session_id:
        _query_tokens = (query or "").strip().lower().split()
        if 0 < len(_query_tokens) <= 3 and all(t in _AUTO_PURE_ACK_TOKENS for t in _query_tokens):
            _sid = str(env.get("session_id", "") or "")
            return _make_gating_stub(query, scope_id, _sid, "trivial_query")

    if injection_mode != "auto" and session_id:

        # --- Gate 1: trivial query (regex) ---
        stripped = query.strip()
        if _memem_settings.MEMEM_TRIVIAL_REGEX_EN.fullmatch(stripped):
            return _make_gating_stub(query, scope_id, session_id, "trivial_query")
        if _memem_settings.MEMEM_TRIVIAL_REGEX_ZH.fullmatch(stripped):
            return _make_gating_stub(query, scope_id, session_id, "trivial_query")
        if stripped.startswith("/"):
            return _make_gating_stub(query, scope_id, session_id, "slash_command")

        # --- Gate 2: turn cadence with empty-streak backoff ---
        turn = increment_turn_count(session_id)
        base_cadence = _memem_settings.MEMEM_INJECT_CADENCE
        streak = get_empty_streak(session_id)
        if streak > 0:
            max_multiplier = _memem_settings.MEMEM_EMPTY_STREAK_MAX
            effective_cadence = min(base_cadence * (2 ** streak), base_cadence * max_multiplier)
        else:
            effective_cadence = base_cadence
        # Turn 1 always runs, then every Nth turn thereafter.
        # (turn-1) % cadence == 0  →  run; otherwise skip.
        if (turn - 1) % effective_cadence != 0:
            return _make_gating_stub(query, scope_id, session_id, "cadence_skip")

    # -----------------------------------------------------------------
    # Gate 3: topic-shift detection via cached query embedding.
    # Only active when injection_mode != 'auto' and a session_id exists.
    # -----------------------------------------------------------------
    _current_emb: list[float] | None = None
    _topic_shift_active = injection_mode != "auto" and bool(session_id)
    if _topic_shift_active:
        try:
            from memem.embedding_index import _get_model  # singleton, do not re-instantiate
            model = _get_model()
            if model is not None:
                _current_emb = model.encode(
                    query, convert_to_numpy=True, show_progress_bar=False,
                ).tolist()
        except Exception:  # noqa: BLE001 — silently skip on any failure
            _current_emb = None

        if _current_emb is not None:
            try:
                from memem.slice_daemon import (
                    get_cached_query_embedding,
                    get_cached_slice,
                )
                _cached_emb = get_cached_query_embedding(session_id)
                _cached_slice = get_cached_slice(session_id)
                if _cached_emb is not None and _cached_slice is not None:
                    try:
                        import numpy as _np
                        _a = _np.asarray(_current_emb, dtype="float32")
                        _b = _np.asarray(_cached_emb, dtype="float32")
                        _norm_a = _np.linalg.norm(_a)
                        _norm_b = _np.linalg.norm(_b)
                        _sim = (
                            float(_np.dot(_a, _b) / (_norm_a * _norm_b))
                            if _norm_a > 0 and _norm_b > 0
                            else 0.0
                        )
                    except ImportError:
                        # Pure-Python fallback for cosine similarity
                        _dot = sum(x * y for x, y in zip(_current_emb, _cached_emb, strict=False))
                        _mag_a = sum(x * x for x in _current_emb) ** 0.5
                        _mag_b = sum(x * x for x in _cached_emb) ** 0.5
                        _sim = _dot / (_mag_a * _mag_b) if _mag_a > 0 and _mag_b > 0 else 0.0

                    if _sim >= _memem_settings.MEMEM_TOPIC_SHIFT_THRESHOLD:
                        # Topic unchanged — reuse cached slice. Overwrite the identity
                        # fields so downstream telemetry attributes the reuse to the
                        # CURRENT turn, not the cached previous turn.
                        from datetime import datetime
                        _reuse = cast(ActiveMemorySlice, dict(_cached_slice))
                        _reuse["gating_reason"] = "topic_shift_reuse"
                        _reuse["query"] = query
                        _reuse["session_id"] = session_id
                        _reuse["scope_id"] = scope_id
                        _reuse["generated_at"] = datetime.now(UTC).isoformat()
                        if not _cached_slice.get("should_emit_context", True):
                            pass  # C5: stale low-confidence cache must not be reused — fall through to full pipeline
                        else:
                            return _reuse
            except Exception:  # noqa: BLE001 — silently fall through on any failure
                pass

    # -----------------------------------------------------------------
    # Full pipeline — gating passed (or auto mode).
    # Wrap in try/except so a pipeline failure still observes the empty-streak
    # counter (treat failure as empty result so backoff kicks in).
    # -----------------------------------------------------------------
    try:
        result = _generate_active_memory_slice_internal(
            query,
            SliceGenRequest(
                scope_id=scope_id,
                environment=environment,
                use_llm=use_llm,
                writeback_mode="policy_only",
                auto_commit_safe=False,
                dry_run=True,
                persist_history=True,
            ),
        )
    except Exception:
        if injection_mode != "auto" and session_id:
            increment_empty_streak(session_id)
        raise

    # After full pipeline: update the embedding+slice cache if we computed an embedding.
    if _topic_shift_active and _current_emb is not None:
        try:
            from memem.slice_daemon import set_cached_embedding_and_slice
            set_cached_embedding_and_slice(session_id, _current_emb, result)
        except Exception:  # noqa: BLE001 — cache update failure must never break recall
            pass

    # Observe result for empty-streak tracking (only when gating is active with session).
    if injection_mode != "auto" and session_id:
        sid = session_id
        memories = result.get("items") or []
        has_content = bool(
            memories
            or result.get("goals")
            or result.get("constraints")
            or result.get("active_background")
            or result.get("artifacts")
        )
        if result.get("should_emit_context") and has_content:
            reset_empty_streak(sid)
        else:
            increment_empty_streak(sid)

    return result


def _load_previous_slice(environment: dict[str, Any], scope_id: str) -> ActiveMemorySlice | None:
    continuity_mode = str(environment.get("continuity_mode", "") or "")
    if not continuity_mode or continuity_mode == "off":
        return None

    limit = int(environment.get("continuity_slice_limit", 10) or 10)
    session_id = str(environment.get("session_id", "") or "")
    if not session_id:
        return None

    records = load_slice_history(
        scope_id=scope_id,
        session_id=session_id,
        limit=max(limit, 1),
    )
    return records[-1] if records else None


def _continuity_context_slice(previous_slice: ActiveMemorySlice) -> ActiveMemorySlice:
    context_slice = cast(ActiveMemorySlice, dict(previous_slice))
    context_slice["candidate_deltas"] = []
    context_slice["delta_results"] = []
    context_slice["warnings"] = []
    context_slice["writeback_summary"] = {
        "status": "not_run",
        "dry_run": True,
        "proposed_count": 0,
        "auto_committed_count": 0,
        "manual_review_count": 0,
        "blocked_count": 0,
        "rejected_count": 0,
    }
    return context_slice


def _continuity_environment(
    environment: dict[str, Any],
    previous_slice: ActiveMemorySlice | None,
) -> dict[str, Any]:
    enriched = dict(environment)
    if not previous_slice:
        return enriched

    continuity_context = render_slice_as_compact_context(_continuity_context_slice(previous_slice), max_chars=1200)
    if continuity_context:
        enriched["continuity_context"] = continuity_context
    enriched["previous_slice_id"] = str(previous_slice.get("slice_id", "") or "")
    enriched["previous_constraint_ids"] = [
        str(item.get("memory_id", "") or "")
        for item in previous_slice.get("constraints", [])
        if str(item.get("memory_id", "") or "")
    ]
    enriched["previous_artifact_ids"] = [
        str(item.get("artifact_id", "") or "")
        for item in previous_slice.get("artifacts", [])
        if str(item.get("artifact_id", "") or "")
    ]
    enriched["previous_artifact_paths"] = [
        str(item.get("path", "") or "")
        for item in previous_slice.get("artifacts", [])
        if str(item.get("path", "") or "")
    ]
    enriched["previous_open_tension_descriptions"] = [
        str(item.get("description", "") or "")
        for item in previous_slice.get("open_tensions", [])
        if str(item.get("description", "") or "")
    ]
    return enriched


def _policy_result_status(decision: dict[str, Any]) -> str:
    if decision.get("decision") == "reject" or decision.get("commit_policy") == "blocked":
        return "rejected"
    return "not_run"


def _policy_results(
    candidate_deltas: list[dict[str, Any]],
    policy_decisions: list[dict[str, Any]],
) -> list[DeltaWritebackResult]:
    results: list[DeltaWritebackResult] = []
    for delta, decision in zip(candidate_deltas, policy_decisions, strict=False):
        target_memory_ids = [
            str(memory_id)[:8]
            for memory_id in decision.get("target_memory_ids", delta.get("target_memory_ids", []))
            if str(memory_id)
        ]
        results.append({
            "delta_id": str(delta.get("delta_id", "") or ""),
            "delta_type": str(delta.get("delta_type", "") or ""),
            "status": cast(Any, _policy_result_status(decision)),
            "commit_policy": cast(Any, decision.get("commit_policy", "blocked") or "blocked"),
            "confidence": float(decision.get("confidence", delta.get("confidence", 0.0)) or 0.0),
            "dry_run": True,
            "requires_user_confirmation": bool(decision.get("requires_user_confirmation", False)),
            "source_slice_id": str(delta.get("source_slice_id", "") or ""),
            "affected_memory_ids": target_memory_ids,
            "validation_errors": list(decision.get("validation_errors", [])),
            "warnings": list(decision.get("warnings", [])),
            "result_message": str(decision.get("result_message", "") or ""),
        })
    return results


def _writeback_summary_from_results(
    results: list[dict[str, Any]],
    *,
    proposed_count: int,
    dry_run: bool,
    status: str,
) -> WritebackSummary:
    return {
        "status": cast(Any, status),
        "dry_run": dry_run,
        "proposed_count": proposed_count,
        "auto_committed_count": sum(1 for result in results if result.get("status") == "committed"),
        "manual_review_count": sum(1 for result in results if result.get("commit_policy") == "manual_review"),
        "blocked_count": sum(1 for result in results if result.get("commit_policy") == "blocked"),
        "rejected_count": sum(1 for result in results if result.get("status") == "rejected"),
    }


def _persist_slice(slice_obj: ActiveMemorySlice) -> None:
    try:
        persist_slice_history(slice_obj)
    except Exception as exc:
        warnings = list(slice_obj.get("warnings", []))
        warnings.append(f"Slice history persistence failed: {type(exc).__name__}.")
        slice_obj["warnings"] = warnings
        log.warning("slice history persistence failed", exc=exc)


def _populate_v13_kind_buckets(slice_obj: ActiveMemorySlice) -> None:
    """Post-processing pass: route kind-classified items into the v1.13 bucket fields.

    Reads items from goals/constraints/active_background/decisions/preferences/
    failure_patterns, calls infer_kind() on each, and populates:
      episodic_items — items with kind/inferred_kind == 'episodic'
      skill_items    — items with kind/inferred_kind == 'skill'
      case_items     — items with kind/inferred_kind == 'case'

    L0 anchors (layer==0) are excluded from all three buckets; they remain in
    goals and are rendered under ## Anchors by render_slice_v2().

    Items that classify as 'other' are left in their existing section fields
    (goals/constraints/etc.) and will fall through the role-based routing in
    render_slice_v2().

    This pass is skipped entirely when MEMEM_RENDER_LEGACY='1'.
    """
    if _memem_settings._render_legacy_enabled():
        # Legacy mode: skip routing pass; old section fields drive old renderer
        slice_obj["episodic_items"] = []
        slice_obj["skill_items"] = []
        slice_obj["case_items"] = []
        return

    episodic_items: list[ActiveMemoryItem] = []
    skill_items: list[ActiveMemoryItem] = []
    case_items: list[ActiveMemoryItem] = []
    placed_ids: set[str] = set()

    all_section_items: list[ActiveMemoryItem] = []
    for section_key in ("goals", "constraints", "active_background", "decisions", "preferences", "failure_patterns"):
        all_section_items.extend(cast(list[ActiveMemoryItem], slice_obj.get(section_key, [])))

    for item in all_section_items:
        # L0 anchors always go to Anchors rendering path — never to kind buckets
        layer = int(item.get("layer", 2) if item.get("layer") is not None else 2)
        if layer == 0:
            continue

        # Determine effective kind: explicit 'kind' field takes priority over inferred
        eff_kind = str(item.get("kind") or "")
        if not eff_kind:
            # Build a minimal memory-like dict so infer_kind() can analyse it.
            # v1.13 Phase 4.5 fix: pass the propagated tags so the classifier's
            # tag-priority path (kind_classifier.py) can honor explicit type:* tags
            # instead of always falling back to heuristics.
            mem_proxy: dict[str, Any] = {
                "title": item.get("title", ""),
                "content": item.get("summary", "") or item.get("why_activated", ""),
                "tags": list(item.get("tags") or []),
            }
            # If inferred_kind is already set on the item, use it directly
            existing_inferred = str(item.get("inferred_kind") or "")
            if existing_inferred:
                eff_kind = existing_inferred
            else:
                eff_kind = _infer_kind(mem_proxy)
                # Write inferred_kind back onto item so render_slice_v2 can read it
                item["inferred_kind"] = eff_kind  # type: ignore[typeddict-unknown-key]

        mid = str(item.get("memory_id") or "")

        if eff_kind == "episodic":
            if not mid or mid not in placed_ids:
                episodic_items.append(item)
                if mid:
                    placed_ids.add(mid)
        elif eff_kind == "skill":
            if not mid or mid not in placed_ids:
                skill_items.append(item)
                if mid:
                    placed_ids.add(mid)
        elif eff_kind == "case":
            if not mid or mid not in placed_ids:
                case_items.append(item)
                if mid:
                    placed_ids.add(mid)
        # 'other' → stays in source section; role-based routing in render_slice_v2 handles it

    slice_obj["episodic_items"] = episodic_items
    slice_obj["skill_items"] = skill_items
    slice_obj["case_items"] = case_items

    # Populate recent_actions from working_memory last_3_actions section
    try:
        import memem.working_memory as _wm
        wm = _wm.read_working_memory()
        last_3_raw = wm.get("last_3_actions", "") or ""
        recent_actions: list[str] = [
            line.strip().lstrip("- ").strip()
            for line in last_3_raw.splitlines()
            if line.strip() and line.strip() not in ("-", "")
        ]
        slice_obj["recent_actions"] = recent_actions[:3]
    except Exception:  # noqa: BLE001 — never break slice generation
        slice_obj["recent_actions"] = []


def _generate_active_memory_slice_internal(
    query: str,
    req: SliceGenRequest,
) -> ActiveMemorySlice:
    scope_id = req.scope_id
    environment = req.environment
    use_llm = req.use_llm
    writeback_mode = req.writeback_mode
    auto_commit_safe = req.auto_commit_safe
    dry_run = req.dry_run
    persist_history = req.persist_history
    env = normalize_runtime_environment(environment)
    normalized_scope = _normalize_scope_id(scope_id)
    previous_slice = _load_previous_slice(env, normalized_scope)
    activation_env = _continuity_environment(env, previous_slice)
    candidate_bundle = generate_candidates(query, normalized_scope, env, use_llm=use_llm)
    _oov_threshold = _memem_settings.MEMEM_RECALL_OOV_THRESHOLD
    if _oov_threshold > 0.0 and _detect_out_of_vault(query, candidate_bundle, _oov_threshold):
        _sid = str(env.get("session_id", "") or "")
        return _make_gating_stub(query, normalized_scope, _sid, "out_of_vault")
    all_candidates = flatten_candidate_bundle(candidate_bundle)
    include_history = bool(env.get("history_mode") or env.get("include_history"))

    pre = apply_pre_boundaries(all_candidates, normalized_scope, include_history=include_history)
    filtered_candidates = pre["candidates"]
    filtered_bundle = _bundle_from_candidates(filtered_candidates, candidate_bundle)

    # Gate LLM judge on both caller flag and the module-level env-var setting.
    # MEMEM_USE_LLM_JUDGE=0 disables LLM across all entry points regardless of use_llm arg.
    _effective_use_llm = use_llm and _memem_settings._llm_judge_enabled()
    if _effective_use_llm:
        try:
            activation = judge_activation_with_llm(query, normalized_scope, activation_env, filtered_bundle)
        except Exception as exc:
            log.warning("LLM activation failed; falling back to heuristic", exc=exc)
            activation = judge_activation_heuristically(query, normalized_scope, activation_env, filtered_bundle)
            activation["warnings"] = list(activation.get("warnings", [])) + [
                f"LLM activation raised {type(exc).__name__}; used heuristic activation.",
            ]
    else:
        activation = judge_activation_heuristically(query, normalized_scope, activation_env, filtered_bundle)
    if not _effective_use_llm:
        activation["warnings"] = list(activation.get("warnings", [])) + ["LLM activation disabled; used heuristic activation."]

    activation["excluded_candidates"] = list(activation.get("excluded_candidates", [])) + pre["excluded_candidates"]
    activation = cast(
        ActivationResult,
        apply_post_boundaries(cast(dict[str, Any], activation), filtered_candidates, normalized_scope, include_history=include_history),
    )
    slice_obj = build_active_memory_slice(query, normalized_scope, env, filtered_bundle, activation)
    carry_forward_limit = int(env.get("continuity_summary_limit", 4) or 4)
    slice_obj = annotate_slice_continuity(slice_obj, previous_slice, carry_forward_limit=carry_forward_limit)
    # v1.13: populate 6-section kind buckets (episodic_items / skill_items / case_items)
    _populate_v13_kind_buckets(slice_obj)
    candidate_deltas = propose_deltas_from_slice(slice_obj)
    policy_decisions = evaluate_delta_proposals(candidate_deltas)
    slice_obj["candidate_deltas"] = candidate_deltas
    slice_obj["delta_results"] = _policy_results(
        cast(list[dict[str, Any]], candidate_deltas),
        cast(list[dict[str, Any]], policy_decisions),
    )
    slice_obj["writeback_summary"] = _writeback_summary_from_results(
        cast(list[dict[str, Any]], slice_obj.get("delta_results", [])),
        proposed_count=len(candidate_deltas),
        dry_run=True,
        status="not_run",
    )

    if writeback_mode != "policy_only" and candidate_deltas:
        public_results = commit_deltas(
            candidate_deltas,
            scope_id=normalized_scope,
            dry_run=dry_run,
            auto_only=auto_commit_safe,
        )
        slice_obj["delta_results"] = cast(list[DeltaWritebackResult], public_results)
        summary = cast(dict[str, Any], public_results[0].get("writeback_summary", {})) if public_results else {}
        if summary:
            slice_obj["writeback_summary"] = cast(WritebackSummary, summary)
        else:
            slice_obj["writeback_summary"] = _writeback_summary_from_results(
                cast(list[dict[str, Any]], public_results),
                proposed_count=len(candidate_deltas),
                dry_run=dry_run,
                status="dry_run" if dry_run else ("committed" if auto_commit_safe else "partial"),
            )
        writeback_warnings: list[str] = []
        for result in public_results:
            writeback_warnings.extend(str(warning) for warning in result.get("warnings", []) if str(warning))
            writeback_warnings.extend(str(error) for error in result.get("validation_errors", []) if str(error))
        if writeback_warnings:
            existing = list(slice_obj.get("warnings", []))
            for warning in writeback_warnings:
                if warning not in existing:
                    existing.append(warning)
            slice_obj["warnings"] = existing

    if persist_history:
        _persist_slice(slice_obj)
    log.info("active_memory_slice_generated", **summarize_slice_metrics(slice_obj))
    return slice_obj


def generate_active_memory_slice_with_writeback(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
    auto_commit_safe: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate a continuity-aware slice and optionally execute writeback."""
    normalized_scope = _normalize_scope_id(scope_id)
    slice_obj = _generate_active_memory_slice_internal(
        query,
        SliceGenRequest(
            scope_id=normalized_scope,
            environment=environment,
            use_llm=use_llm,
            writeback_mode="preview" if dry_run else "commit",
            auto_commit_safe=auto_commit_safe,
            dry_run=dry_run,
            persist_history=True,
        ),
    )
    return {
        "slice": slice_obj,
        "delta_results": list(slice_obj.get("delta_results", [])),
    }


def _bundle_from_candidates(candidates: list[Candidate], original: CandidateBundle) -> CandidateBundle:
    bundle: CandidateBundle = {
        "current_goal_candidates": [],
        "memory_candidates": [],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }
    for candidate in candidates:
        ctype = candidate.get("candidate_type")
        if ctype == "current_query":
            bundle["current_goal_candidates"].append(candidate)
        elif ctype == "memory":
            bundle["memory_candidates"].append(candidate)
        elif ctype == "playbook":
            bundle["playbook_candidate"] = candidate
        elif ctype == "transcript":
            bundle["transcript_candidates"].append(candidate)
        elif ctype == "environment":
            bundle["environment_candidates"].append(candidate)
        else:
            bundle["artifact_candidates"].append(candidate)
    return bundle


def record_slice_attribution(slice_data: dict, response_text: str) -> None:
    """Compute the 3 signals for every memory in the slice and log to telemetry.

    Called by host integration (hook scripts, MCP wrappers) after the assistant
    has produced a response that referenced this slice. m2 ships this without
    auto-wiring — collection happens lazily as integrations adopt it.
    """
    from memem.attribution import (
        aggregate_signals,
        citation_match,
        embedding_similarity,
        judge_score,
        should_run_judge,
    )
    from memem.telemetry import log_slice_attribution

    slice_id = slice_data.get("slice_id", "")
    items = slice_data.get("items", []) or []
    # Also check pinned/role-categorized fields
    for field in ("goals", "constraints", "active_background", "decisions",
                  "preferences", "failure_patterns"):
        items.extend(slice_data.get(field, []) or [])

    seen_ids: set[str] = set()
    for item in items:
        mem_id = item.get("memory_id") or item.get("id") or ""
        if not mem_id or mem_id in seen_ids:
            continue
        seen_ids.add(mem_id)

        title = item.get("title", "")
        essence = item.get("content", "") or item.get("summary", "") or ""

        emb = embedding_similarity(essence, response_text)
        cite = citation_match(mem_id, title, response_text, memory_essence=essence)
        judge = judge_score(essence, response_text, slice_data.get("query", "")) if should_run_judge() else None  # always None — stub, see judge_score docstring
        agg = aggregate_signals(emb, cite, judge)

        log_slice_attribution(slice_id, mem_id, emb, cite, judge, agg)


def generate_session_start_slice(
    scope_id: str,
    session_id: str,
    memem_dir: str | None = None,
) -> str:
    """Generate a structured 'where we left off' briefing for SessionStart hook injection.

    Sections (in priority order):
      1. Prior working memory  — highest priority; trimmed to ~600 chars/section
      2. Recent decisions       — top 3 with kind='decision' or tag 'decision', last 7 days
      3. Active arcs            — memories with arc_id set and not closed
      4. L0 anchors             — always included via _gather_l0_anchors
      5. Compaction checkpoint  — most recent in last 24h (tagged kind:compaction-checkpoint)

    Char budget: MEMEM_SESSION_START_BUDGET env var, default 2000.
    """
    from pathlib import Path as _Path

    from memem.models import _normalize_scope_id

    budget = _parse_session_start_budget()
    normalized_scope = _normalize_scope_id(scope_id)
    memem_dir_path = _Path(memem_dir) if memem_dir else None

    # -------------------------------------------------------------------
    # Section 1: Prior working memory
    # -------------------------------------------------------------------
    wm_sections = _read_working_memory_for_slice(memem_dir_path)
    wm_block = _render_working_memory_block(wm_sections)

    # -------------------------------------------------------------------
    # Section 1b: Pending instruction rewrite suggestions (M-1 procedural)
    # -------------------------------------------------------------------
    procedural_block = _render_procedural_suggestions_block()

    # -------------------------------------------------------------------
    # Section 1c: Pending contradiction flags (M-3 consolidation)
    # -------------------------------------------------------------------
    contradiction_block = _render_contradiction_flags_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 2: Recent decisions (last 7 days, top 3 by strength)
    # -------------------------------------------------------------------
    decisions_block = _render_decisions_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 3: Active arcs
    # -------------------------------------------------------------------
    arcs_block = _render_active_arcs_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 4: L0 anchors
    # -------------------------------------------------------------------
    l0_block = _render_l0_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 5: Compaction checkpoint
    # -------------------------------------------------------------------
    checkpoint_block = _render_checkpoint_block(normalized_scope, session_id)

    # -------------------------------------------------------------------
    # Budget enforcement: working_memory is highest priority
    # -------------------------------------------------------------------
    parts = [
        ("working_memory", wm_block),
        ("procedural_suggestions", procedural_block),
        ("contradiction_flags", contradiction_block),
        ("decisions", decisions_block),
        ("arcs", arcs_block),
        ("l0_anchors", l0_block),
        ("checkpoint", checkpoint_block),
    ]

    output_parts: list[str] = []
    chars_used = 0
    sep_len = 2  # len("\n\n") between joined sections

    for section_name, block in parts:
        if not block:
            continue
        # Reserve space for the separator that join() will add before this part
        # (only if there's already content to join after).
        sep_cost = sep_len if output_parts else 0
        block_len = len(block)
        remaining = budget - chars_used - sep_cost
        if section_name == "working_memory":
            # working_memory is never cut unless it alone exceeds budget
            if block_len > budget:
                block = block[:budget]
                block_len = budget
            output_parts.append(block)
            chars_used += block_len + sep_cost
        else:
            if remaining <= 0:
                break
            if block_len > remaining:
                block = block[:remaining]
                block_len = remaining
            output_parts.append(block)
            chars_used += block_len + sep_cost

    return "\n\n".join(p for p in output_parts if p)


def _parse_session_start_budget() -> int:
    """Return char budget for session-start slice from env var (default 2000).

    MEMEM_SESSION_START_BUDGET is preferred. MEMEM_SESSION_START_PROMPT_BUDGET
    is honored as a legacy alias so configs from the pre-m1 hook keep working.
    """
    raw = (
        os.environ.get("MEMEM_SESSION_START_BUDGET")
        or os.environ.get("MEMEM_SESSION_START_PROMPT_BUDGET")
        or "2000"
    ).strip()
    try:
        val = int(raw)
        return max(100, val)
    except ValueError:
        return 2000


def _read_working_memory_for_slice(memem_dir: Any | None) -> dict[str, str]:
    """Read working memory, optionally overriding MEMEM_DIR for test isolation."""
    try:
        import memem.working_memory as wm_mod

        if memem_dir is not None:
            from pathlib import Path as _Path
            wm_file = _Path(memem_dir) / "working_memory.md"
            # Read directly without relying on module-level WORKING_MEMORY_FILE
            if not wm_file.exists():
                return {}
            try:
                text = wm_file.read_text(encoding="utf-8")
                return wm_mod.parse_from_md(text)
            except OSError:
                return {}
        else:
            return wm_mod.read_working_memory()
    except Exception:
        return {}


_WM_SECTION_LABELS = {
    "current_task": "Current task",
    "active_hypothesis": "Active hypothesis",
    "last_3_actions": "Last 3 actions",
    "stuck_on": "Stuck on",
    "decided_this_session": "Decided this session",
}

_WM_SECTION_CAP = 600


def _render_working_memory_block(sections: dict[str, str]) -> str:
    """Render non-empty working memory sections, each capped at _WM_SECTION_CAP chars."""
    if not sections:
        return ""
    parts = ["## Prior working memory"]
    for key, label in _WM_SECTION_LABELS.items():
        body = sections.get(key, "").strip()
        if body:
            if len(body) > _WM_SECTION_CAP:
                body = body[:_WM_SECTION_CAP]
            parts.append(f"**{label}:** {body}")
    if len(parts) == 1:
        return ""
    return "\n\n".join(parts)


def _render_procedural_suggestions_block() -> str:
    """Render pending instruction-rewrite suggestions (kind:procedural-suggestion, status: pending_review).

    Queries all memories with the ``kind:procedural-suggestion`` tag and
    ``status: pending_review``. Sorts by ``created_iso`` ascending (oldest
    first) and surfaces the top 3. Omits the section entirely when none exist.
    """
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    all_mems = _obsidian_memories()
    pending: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:procedural-suggestion" not in tags:
            continue
        if mem.get("status") != "pending_review":
            continue
        pending.append(mem)

    if not pending:
        return ""

    # Sort oldest-first by created_iso (falls back to created_at)
    def _sort_key(m: dict) -> str:
        return str(m.get("created_iso") or m.get("created_at") or "")

    pending.sort(key=_sort_key)
    top3 = pending[:3]

    lines = [f"## Pending instruction rewrites\n\n{len(pending)} suggestion(s) await your review (oldest first):"]
    for idx, mem in enumerate(top3, start=1):
        created_iso = str(mem.get("created_iso") or mem.get("created_at") or "")
        date_str = created_iso[:10] if created_iso else "unknown date"
        reason = ""
        essence = mem.get("essence") or mem.get("full_record") or ""
        # Extract reason line from body
        for line in essence.splitlines():
            if line.startswith("**Reason:**"):
                reason = line[len("**Reason:**"):].strip()
                break
        # Extract current/proposed from body
        current_snippet = ""
        proposed_snippet = ""
        in_current = False
        in_proposed = False
        for line in essence.splitlines():
            if line.startswith("**Current:**"):
                in_current = True
                in_proposed = False
            elif line.startswith("**Proposed:**"):
                in_current = False
                in_proposed = True
            elif line.strip() == "```":
                pass
            elif in_current and line.strip() and not current_snippet:
                current_snippet = line.strip()[:100]
                in_current = False
            elif in_proposed and line.strip() and not proposed_snippet:
                proposed_snippet = line.strip()[:100]
                in_proposed = False
        entry_lines = [f"\n{idx}. **From session {date_str}:** {reason}"]
        if current_snippet:
            entry_lines.append(f'   - Current: "{current_snippet}"')
        entry_lines.append(f'   - Proposed: "{proposed_snippet}"')
        lines.append("\n".join(entry_lines))

    return "\n".join(lines)


def _render_contradiction_flags_block(normalized_scope: str) -> str:
    """Render pending contradiction flags from M-3 consolidation pass.

    Queries memories tagged ``kind:contradiction-flag`` (NOT deprecated) for
    this scope. Surfaces up to 3 oldest-first so the user reviews and resolves
    them. Omits the section entirely when none exist.
    """
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    all_mems = _obsidian_memories()
    flags: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:contradiction-flag" not in tags:
            continue
        if mem.get("status") == "deprecated":
            continue
        # Scope filter: same project, OR project='general' (consolidation-flag is cross-cutting)
        proj = _normalize_scope_id(str(mem.get("project", "general") or "general"))
        if proj != normalized_scope and proj != "general":
            continue
        flags.append(mem)

    if not flags:
        return ""

    def _sort_key(m: dict) -> str:
        return str(m.get("created_iso") or m.get("created_at") or "")

    flags.sort(key=_sort_key)
    top3 = flags[:3]

    lines = [f"## Pending contradiction review\n\n{len(flags)} unresolved contradiction(s) found by consolidation:"]
    for idx, mem in enumerate(top3, start=1):
        created = str(mem.get("created_iso") or mem.get("created_at") or "")
        date_str = created[:10] if created else "unknown date"
        title = str(mem.get("title", "") or "(untitled)")[:80]
        essence = str(mem.get("essence") or mem.get("full_record") or "")[:200]
        lines.append(f"\n{idx}. **{date_str}** — {title}\n   {essence}")

    return "\n".join(lines)


def _render_decisions_block(normalized_scope: str) -> str:
    """Render top-3 decision memories from the last 7 days, ranked by decay strength."""
    from datetime import UTC

    try:
        from memem import decay as _decay_mod
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=7)

    all_mems = _obsidian_memories()
    decision_mems: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        # canonical: bare "decision" tag; "kind:<value>" tag prefix is reserved for compaction-checkpoint and similar
        has_decision = "decision" in tags
        if not has_decision:
            continue
        # Check recency: parse created_at
        created_str = mem.get("created_at", "") or ""
        created_dt = parse_iso_dt(created_str)
        if created_dt is not None and created_dt < cutoff:
            continue
        decision_mems.append(mem)

    # Rank by compute_strength (access_count * recency_decay)
    def _score(mem: dict) -> float:
        try:
            return _decay_mod.compute_strength(mem, now=now)
        except Exception:
            return 0.0

    decision_mems.sort(key=_score, reverse=True)
    top3 = decision_mems[:3]

    if not top3:
        return ""

    lines = ["## Recent decisions"]
    for mem in top3:
        title = mem.get("title", "Untitled")
        essence = (mem.get("essence") or mem.get("full_record", ""))[:300]
        lines.append(f"- **{title}**: {essence}")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:])


def _render_active_arcs_block(normalized_scope: str) -> str:
    """Render memories with arc_id set and not closed (no 'closed' tag, no closed=True field)."""
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    all_mems = _obsidian_memories()
    arc_mems: list[dict[str, Any]] = []
    for mem in all_mems:
        arc_id = mem.get("arc_id") or ""
        if not arc_id:
            continue
        # Skip if closed
        tags = mem.get("domain_tags") or []
        if "closed" in tags:
            continue
        if mem.get("closed") is True or str(mem.get("closed", "")).lower() == "true":
            continue
        arc_mems.append(mem)

    if not arc_mems:
        return ""

    lines = ["## Active arcs"]
    for mem in arc_mems:
        title = mem.get("title", "Untitled")
        content = (mem.get("essence") or mem.get("full_record", ""))[:200]
        arc_id = mem.get("arc_id", "")
        lines.append(f"- **[arc:{arc_id}] {title}**: {content}")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:])


def _render_l0_block(normalized_scope: str) -> str:
    """Render L0 anchor memories for the given scope."""
    anchors = _gather_l0_anchors(normalized_scope)
    if not anchors:
        return "## L0 anchors\n\n(none for scope)"

    lines = ["## L0 anchors"]
    for anchor in anchors:
        title = anchor.get("title", "Untitled")
        content = (anchor.get("content", "") or "")[:200]
        lines.append(f"- **{title}**: {content}")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:])


def _render_checkpoint_block(normalized_scope: str, session_id: str) -> str:
    """Render the most recent compaction checkpoint from the last 24h."""
    from datetime import UTC

    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    all_mems = _obsidian_memories()
    checkpoints: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:compaction-checkpoint" not in tags:
            continue
        # Check project matches
        from memem.models import _normalize_scope_id as _nsi
        mem_project = _nsi(str(mem.get("project", "general") or "general"))
        if mem_project != normalized_scope and normalized_scope != "general":
            continue
        # Check recency
        created_str = mem.get("created_at", "") or ""
        created_dt = parse_iso_dt(created_str)
        if created_dt is not None and created_dt < cutoff:
            continue
        checkpoints.append(mem)

    if not checkpoints:
        return ""

    # Sort by created_at descending, take most recent
    checkpoints.sort(key=lambda m: m.get("created_at", "") or "", reverse=True)
    latest = checkpoints[0]

    title = latest.get("title", "Compaction checkpoint")
    essence = (latest.get("essence") or latest.get("full_record", ""))[:600]
    return f"## Compaction checkpoint\n\n**{title}**\n\n{essence}"


def active_slice_response(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
    raw_json: bool = False,
) -> str:
    slice_obj = generate_active_memory_slice(query, scope_id=scope_id, environment=environment, use_llm=use_llm)
    if raw_json:
        return json.dumps(slice_obj, indent=2, sort_keys=True)
    return generate_prompt_context(
        query,
        scope_id=scope_id,
        environment=environment,
        use_llm=use_llm,
        mode="slice",
        slice_obj=slice_obj,
    )


def generate_prompt_context(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
    mode: PromptContextMode = "slice",
    slice_obj: ActiveMemorySlice | None = None,
) -> str:
    """Generate runtime prompt context using slice-first projection.

    History persistence is the caller's responsibility. The internal slice
    fallback path (when ``slice_obj`` is None) deliberately runs with
    ``persist_history=False`` so that wrappers like
    ``active_slice_response`` — which already persist — don't double-write
    history records for what is logically one turn.
    """
    if slice_obj is None:
        current_slice = _generate_active_memory_slice_internal(
            query,
            SliceGenRequest(
                scope_id=scope_id,
                environment=environment,
                use_llm=use_llm,
                writeback_mode="policy_only",
                auto_commit_safe=False,
                dry_run=True,
                persist_history=False,
            ),
        )
    else:
        current_slice = slice_obj
    if not current_slice.get("should_emit_context", True):
        return ""

    if mode == "slice":
        compact_budget = environment.get("prompt_budget_chars") if isinstance(environment, dict) else None
        if isinstance(compact_budget, int) and compact_budget > 0:
            return render_slice_as_compact_context(current_slice, max_chars=compact_budget)
        return render_slice_as_prompt_context(current_slice)

    if mode != "assembly":
        raise ValueError(f"Unknown prompt context mode: {mode}")

    try:
        from memem.assembly import context_assemble

        assembled = context_assemble(query, project=scope_id)
    except Exception as exc:
        log.warning("assembly projection failed; falling back to slice projection", exc=exc)
        return render_slice_as_prompt_context(current_slice)

    return assembled or render_slice_as_prompt_context(current_slice)
