"""Tests asserting MCP tool descriptions include trigger-explicit keywords."""
from pathlib import Path

SERVER_SRC = Path("memem/server.py").read_text()

EXPECTED_TOOLS = frozenset({
    "memory_save",
    "memory_search",
    "memory_get",
    "memory_timeline",
    "transcript_search",
    "active_memory_slice",
})


def test_exactly_6_tools_registered():
    """Server registers exactly the 6 kept tools — no more, no fewer."""
    import sys
    sys.path.insert(0, ".")
    from memem.server import _build_mcp
    mcp = _build_mcp()
    tools = set(mcp._tool_manager._tools.keys())
    assert tools == EXPECTED_TOOLS, (
        f"Expected tools {sorted(EXPECTED_TOOLS)}, got {sorted(tools)}"
    )


def test_active_memory_slice_includes_triggers():
    assert "unfamiliar references" in SERVER_SRC
    assert "retrieval-language" in SERVER_SRC


def test_memory_search_includes_triggers():
    assert "Use FIRST to narrow candidates" in SERVER_SRC


def test_memory_get_includes_triggers():
    assert "Use AFTER memory_search" in SERVER_SRC
    # Catalog uses 8-char UUID prefix format, not synthesized "ep0NN" ids
    assert "SessionStart episode catalog" in SERVER_SRC
    assert "8-character ID prefix" in SERVER_SRC


def test_memory_timeline_includes_triggers():
    assert "narrative around a memory" in SERVER_SRC
    assert "decision's history" in SERVER_SRC


def test_transcript_search_includes_triggers():
    # transcript_search is the highest-confusion-risk tool vs memory_search:
    # the docstring must keep the corpus differentiation language.
    assert "different corpus" in SERVER_SRC
    assert ".jsonl" in SERVER_SRC


def test_memory_save_includes_triggers():
    assert "dedup" in SERVER_SRC
    assert "transcript_search for session logs" in SERVER_SRC
