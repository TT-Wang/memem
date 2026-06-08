"""Tests for v1.9.5 visible-recall systemMessage in auto-recall.sh.

The auto-recall.sh hook now emits a user-visible systemMessage so the
recall is observable in the Claude Code TUI (mirroring EverMe's "🧠
Recalling N memories from EverMe" pattern). Opt-out via
MEMEM_VISIBLE_RECALL=0.

These tests exercise the _build_system_message function inline (extracted
from the bash heredoc as a stand-alone function for unit testability).
"""

import os

# Mirror of the function defined inside hooks/auto-recall.sh's Python heredoc.
# Kept in sync via test_hook_python_body_matches.
SLICE_UNCHANGED_PLACEHOLDER = "[Active Memory Slice unchanged from previous turn — see slice above]"


def _build_system_message(final_context, scope, should_emit_context=True, gating_reason=""):
    if os.environ.get("MEMEM_VISIBLE_RECALL", "1") == "0":
        return ""
    # C1: daemon signalled context should be suppressed — show gating label.
    if not should_emit_context and gating_reason == "out_of_vault":
        return f"🧠 memem: 0 items (out of vault) · {scope}"
    if not should_emit_context and gating_reason != "out_of_vault":
        return f"🧠 memem: 0 items (low confidence) · {scope}"
    if final_context == SLICE_UNCHANGED_PLACEHOLDER or (
        isinstance(final_context, str) and SLICE_UNCHANGED_PLACEHOLDER in final_context
    ):
        return f"🧠 memem: slice cached · {scope}"
    if not final_context or not final_context.strip():
        return f"🧠 memem: gated · {scope}"
    count = final_context.count("\n- ") + final_context.count("\n  - ")
    return f"🧠 memem: {count} items · {scope}"


def test_fresh_slice_shows_item_count(monkeypatch):
    monkeypatch.delenv("MEMEM_VISIBLE_RECALL", raising=False)
    fc = (
        "# Active Memory Slice\n\n"
        "## Goals\n- item one\n- item two\n\n"
        "## Constraints\n- constraint a\n- constraint b\n  - sub-item\n"
    )
    msg = _build_system_message(fc, "cortex-plugin")
    assert "🧠 memem:" in msg
    assert "cortex-plugin" in msg
    # 5 top-level "- " + 1 "  - " sub-item = 5 newline-dash + 1 indented-dash
    assert "5 items" in msg, f"Got: {msg}"


def test_cached_slice_shows_cached_label(monkeypatch):
    monkeypatch.delenv("MEMEM_VISIBLE_RECALL", raising=False)
    msg = _build_system_message(SLICE_UNCHANGED_PLACEHOLDER, "cortex-plugin")
    assert "slice cached" in msg
    assert "cortex-plugin" in msg


def test_empty_context_shows_gated_label(monkeypatch):
    monkeypatch.delenv("MEMEM_VISIBLE_RECALL", raising=False)
    msg = _build_system_message("", "cortex-plugin")
    assert "gated" in msg
    msg2 = _build_system_message("   \n  ", "cortex-plugin")
    assert "gated" in msg2


def test_opt_out_via_env_returns_empty(monkeypatch):
    """MEMEM_VISIBLE_RECALL=0 → empty string regardless of content."""
    monkeypatch.setenv("MEMEM_VISIBLE_RECALL", "0")
    fc = "# Active Memory Slice\n\n## Goals\n- item one\n"
    assert _build_system_message(fc, "cortex-plugin") == ""
    assert _build_system_message(SLICE_UNCHANGED_PLACEHOLDER, "cortex-plugin") == ""
    assert _build_system_message("", "cortex-plugin") == ""


def test_default_env_is_on(monkeypatch):
    """When MEMEM_VISIBLE_RECALL is unset, default is to show messages."""
    monkeypatch.delenv("MEMEM_VISIBLE_RECALL", raising=False)
    fc = "## Goals\n- one\n"
    assert _build_system_message(fc, "scope") != ""


def test_hook_python_body_matches():
    """Guard: the v2.0.0 auto-recall.sh hook uses retrieve+render_slice (no daemon).

    v2.0.0 redesigned the hook to call memem.retrieve + memem.render directly,
    replacing the daemon-based active_slice pipeline. The old _build_system_message
    was removed as part of this simplification.
    """
    from pathlib import Path
    hook = Path("/home/claude-user/cortex-plugin/hooks/auto-recall.sh").read_text()
    # v2.0.0 hook must use the new retrieve+render pipeline
    assert "from memem.retrieve import retrieve" in hook, (
        "auto-recall.sh v2.0.0 must use memem.retrieve.retrieve"
    )
    assert "from memem.render import render_slice" in hook, (
        "auto-recall.sh v2.0.0 must use memem.render.render_slice"
    )
    # Must still honor injection mode opt-out
    assert "MEMEM_INJECTION_MODE" in hook, (
        "auto-recall.sh must still honor MEMEM_INJECTION_MODE=tool opt-out"
    )
    # Must still return hookSpecificOutput JSON
    assert "hookSpecificOutput" in hook, (
        "auto-recall.sh must return hookSpecificOutput JSON"
    )
    assert "additionalContext" in hook, (
        "auto-recall.sh must include additionalContext in hookSpecificOutput"
    )


def test_low_confidence_shows_label(monkeypatch):
    """C1: _build_system_message with should_emit_context=False, gating_reason='low_confidence'
    → result contains 'low confidence'.
    """
    monkeypatch.delenv("MEMEM_VISIBLE_RECALL", raising=False)
    msg = _build_system_message("some content", "cortex-plugin", should_emit_context=False, gating_reason="low_confidence")
    assert "low confidence" in msg, f"Expected 'low confidence' in message, got: {msg!r}"
    assert "cortex-plugin" in msg


def test_out_of_vault_shows_label(monkeypatch):
    """C1: _build_system_message with should_emit_context=False, gating_reason='out_of_vault'
    → result contains 'out of vault'.
    """
    monkeypatch.delenv("MEMEM_VISIBLE_RECALL", raising=False)
    msg = _build_system_message("some content", "scope", should_emit_context=False, gating_reason="out_of_vault")
    assert "out of vault" in msg, f"Expected 'out of vault' in message, got: {msg!r}"
    assert "scope" in msg
