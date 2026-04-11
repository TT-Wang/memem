#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory for Claude Code.

This file is the thin runtime entrypoint. Core behavior lives in:
  storage.py    Obsidian access, indexing helpers
  recall.py     recall and smart-recall flows
  transcripts.py raw session transcript search/parsing
  mining.py     session mining and mined-session bookkeeping
  operations.py manual save/import operations
  cli.py        command dispatch for non-MCP entrypoints
"""

from mcp.server.fastmcp import FastMCP

from cli import dispatch_cli
from operations import memory_import as _memory_import, memory_save as _memory_save
from recall import memory_list as _memory_list, memory_recall as _memory_recall
from transcripts import transcript_search as _transcript_search


mcp = FastMCP("cortex")


@mcp.tool()
def memory_recall(query: str, scope_id: str = "default", limit: int = 10) -> str:
    return _memory_recall(query, scope_id=scope_id, limit=limit)


@mcp.tool()
def memory_save(content: str, title: str = "", scope_id: str = "default", tags: str = "") -> str:
    return _memory_save(content, title=title, scope_id=scope_id, tags=tags)


@mcp.tool()
def memory_list(scope_id: str = "default") -> str:
    return _memory_list(scope_id=scope_id)


@mcp.tool()
def memory_import(source_path: str, scope_id: str = "default") -> str:
    return _memory_import(source_path, scope_id=scope_id)


@mcp.tool()
def transcript_search(query: str, limit: int = 5) -> str:
    return _transcript_search(query, limit=limit)


@mcp.tool()
def context_assemble(query: str, project: str = "default") -> str:
    """Assemble a query-tailored context briefing from all available knowledge.

    Uses memories, playbooks, and session history to produce a comprehensive
    briefing for the given query. Returns formatted markdown.
    """
    from storage import context_assemble as _assemble
    return _assemble(query, project)


if __name__ == "__main__":
    import sys

    dispatch_cli(sys.argv, mcp)
