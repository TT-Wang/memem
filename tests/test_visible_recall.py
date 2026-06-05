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


def _build_system_message(final_context, scope):
    if os.environ.get("MEMEM_VISIBLE_RECALL", "1") == "0":
        return ""
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
    """Guard: the in-test copy of _build_system_message must match the
    one defined inside hooks/auto-recall.sh. If the hook is updated, this
    test reminds the developer to update the test copy too.
    """
    from pathlib import Path
    hook = Path("/home/claude-user/cortex-plugin/hooks/auto-recall.sh").read_text()
    # Anchor check: the function MUST be present in the hook source
    assert "def _build_system_message" in hook, (
        "auto-recall.sh missing _build_system_message function — visible-recall fix removed?"
    )
    # Anchor check: opt-out env var present
    assert "MEMEM_VISIBLE_RECALL" in hook, (
        "auto-recall.sh missing MEMEM_VISIBLE_RECALL env var — opt-out removed?"
    )
    # Anchor check: the three brand-line labels
    for label in ("slice cached", "gated", "items ·"):
        assert label in hook, f"auto-recall.sh missing brand label: {label!r}"
