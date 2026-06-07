"""Tests for v1.9 gating Layer 1: trivial-query regex gate.

These tests exercise:
- English trivial queries → gating_reason='trivial_query'
- Chinese trivial queries → gating_reason='trivial_query'
- Slash-commands → gating_reason='slash_command'
- MEMEM_INJECTION_MODE=auto bypasses all gating
- Non-trivial queries pass through (no gating_reason set by trivial gate)
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_generate(query: str, monkeypatch, injection_mode: str = "hybrid") -> dict:
    """Call generate_active_memory_slice with gating constants patched."""
    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_INJECTION_MODE", injection_mode)
    # Use a large cadence so cadence gate never fires in trivial tests
    monkeypatch.setattr(settings, "MEMEM_INJECT_CADENCE", 9999)

    # Reset per-session counters to avoid cross-test pollution
    import memem.slice_history as sh
    with sh._session_lock:
        sh._turn_counts.clear()
        sh._empty_streaks.clear()

    from memem.active_slice_engine import generate_active_memory_slice
    return generate_active_memory_slice(
        query,
        scope_id="test",
        environment={"session_id": "test-trivial-session"},
        use_llm=False,
    )


# ---------------------------------------------------------------------------
# English trivial queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "yes", "no", "ok", "okay", "sure", "thanks", "thank you", "ty",
    "k", "kk", "cool", "got it", "yep", "yup", "nope",
    # Case variants
    "Yes", "YES", "OK", "Sure", "Thanks",
])
def test_trivial_en_is_gated(query, monkeypatch, tmp_vault, tmp_cortex_dir):
    result = _call_generate(query, monkeypatch)
    assert result["should_emit_context"] is False, f"Expected gated for EN query: {query!r}"
    assert result.get("gating_reason") == "trivial_query", (
        f"Expected gating_reason='trivial_query' for query: {query!r}, got: {result.get('gating_reason')!r}"
    )


# ---------------------------------------------------------------------------
# Chinese trivial queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "好", "好的", "好吧", "好啊", "嗯", "是", "是的",
    "不", "不是", "对", "对的", "行", "可以", "行吧",
    "谢谢", "谢谢你", "谢了", "多谢", "了解", "明白", "知道了", "收到",
])
def test_trivial_zh_is_gated(query, monkeypatch, tmp_vault, tmp_cortex_dir):
    result = _call_generate(query, monkeypatch)
    assert result["should_emit_context"] is False, f"Expected gated for ZH query: {query!r}"
    assert result.get("gating_reason") == "trivial_query", (
        f"Expected gating_reason='trivial_query' for ZH query: {query!r}, got: {result.get('gating_reason')!r}"
    )


# ---------------------------------------------------------------------------
# Slash-commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "/help", "/clear", "/compact", "/memory", "/status",
])
def test_slash_command_gating_reason(query, monkeypatch, tmp_vault, tmp_cortex_dir):
    result = _call_generate(query, monkeypatch)
    assert result["should_emit_context"] is False, f"Expected gated for slash: {query!r}"
    assert result.get("gating_reason") == "slash_command", (
        f"Expected gating_reason='slash_command' for query: {query!r}, got: {result.get('gating_reason')!r}"
    )


# ---------------------------------------------------------------------------
# auto mode bypasses gating
# ---------------------------------------------------------------------------


def test_auto_mode_gates_pure_ack_query(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, pure-ack trivial queries like 'yes' should be gated (v1.10.1)."""
    import memem.slice_history as sh
    with sh._session_lock:
        sh._turn_counts.clear()
        sh._empty_streaks.clear()

    result = _call_generate("yes", monkeypatch, injection_mode="auto")
    # v1.10.1: auto-mode trivial-ack gate now fires for pure-acknowledgment queries.
    assert result.get("gating_reason") == "trivial_query", (
        f"auto mode should gate pure-ack queries; got {result.get('gating_reason')!r}"
    )


# ---------------------------------------------------------------------------
# Non-trivial queries are not trivially gated
# ---------------------------------------------------------------------------


def test_substantive_query_not_trivially_gated(monkeypatch, tmp_vault, tmp_cortex_dir):
    """A substantive query should not be caught by the trivial-query gate."""
    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_INJECTION_MODE", "hybrid")
    # Set cadence=1 so every turn passes cadence gate, letting us see trivial gate outcome
    monkeypatch.setattr(settings, "MEMEM_INJECT_CADENCE", 1)

    import memem.slice_history as sh
    with sh._session_lock:
        sh._turn_counts.clear()
        sh._empty_streaks.clear()

    from memem.active_slice_engine import generate_active_memory_slice
    result = generate_active_memory_slice(
        "How do I refactor the authentication middleware?",
        scope_id="test",
        environment={"session_id": "test-substantive-session"},
        use_llm=False,
    )
    # Non-trivial query should not have trivial_query or slash_command as gating_reason
    assert result.get("gating_reason") not in ("trivial_query", "slash_command"), (
        f"Substantive query should not be trivially gated; got: {result.get('gating_reason')!r}"
    )
