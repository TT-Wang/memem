"""Tests asserting MCP tool descriptions include trigger-explicit keywords."""
from pathlib import Path

SERVER_SRC = Path("memem/server.py").read_text()


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


def test_memory_recall_is_deprecated():
    # Two acceptable phrasings of deprecation marker
    assert ("Deprecated" in SERVER_SRC) or ("deprecated" in SERVER_SRC)
    assert "memory_search" in SERVER_SRC  # points to replacement
