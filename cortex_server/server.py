#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory for Claude Code.

This file is the thin runtime entrypoint. Core behaviour lives in the split
modules (obsidian_store, recall, transcripts, mining, operations, assembly, cli).

FastMCP is imported lazily inside ``_build_mcp`` so that CLI-only commands
(``--status``, ``--rebuild-index``, etc.) can run without the optional
``mcp`` dependency installed.
"""

import sys

from cortex_server.cli import dispatch_cli


def _build_mcp():
    """Construct the FastMCP instance and register all tools.

    Called only when the CLI dispatcher needs to run the MCP stdio transport.
    Importing ``mcp.server.fastmcp`` here keeps non-MCP commands free of the
    dependency.
    """
    from mcp.server.fastmcp import FastMCP

    from cortex_server.operations import memory_import as _memory_import
    from cortex_server.operations import memory_save as _memory_save
    from cortex_server.recall import memory_list as _memory_list
    from cortex_server.recall import memory_recall as _memory_recall
    from cortex_server.transcripts import transcript_search as _transcript_search

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
        """Assemble a query-tailored context briefing from all available knowledge."""
        from cortex_server.assembly import context_assemble as _assemble
        return _assemble(query, project)

    return mcp


def main() -> None:
    # Build MCP lazily: only CLI command `None` (the default, no-arg run) needs it.
    # All other commands (--status, --rebuild-index, etc.) can run without `mcp`.
    needs_mcp = len(sys.argv) < 2
    mcp = _build_mcp() if needs_mcp else None
    dispatch_cli(sys.argv, mcp)


if __name__ == "__main__":
    main()
