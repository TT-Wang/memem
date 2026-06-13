#!/usr/bin/env python3
"""
memem MCP Server — persistent memory for Claude Code.

This file is the thin runtime entrypoint. Core behaviour lives in the split
modules (obsidian_store, recall, transcripts, mining, operations, assembly, cli).

FastMCP is imported lazily inside ``_build_mcp`` so non-MCP CLI commands
(``--status``, ``--rebuild-index``, etc.) avoid paying the FastMCP import
cost and can run even when ``mcp`` is uninstallable on the host (e.g. a
minimal diagnostic environment). ``mcp`` remains a required dependency
declared in ``pyproject.toml`` — this is purely an import-time deferral.
"""

import sys

from memem.cli import dispatch_cli


def _build_mcp():
    """Construct the FastMCP instance and register all tools.

    Called only when the CLI dispatcher needs to run the MCP stdio transport.
    Importing ``mcp.server.fastmcp`` here keeps non-MCP commands free of the
    dependency.
    """
    import re
    import time
    from typing import Annotated

    from mcp.server.fastmcp import FastMCP
    from pydantic import Field

    def _extract_ids_from_markdown(md: str) -> list[str]:
        """Extract 8-hex memory IDs from compact result lines (e.g. '[abcd1234] ...')."""
        return re.findall(r'\[([0-9a-f]{8})\]', md)

    import memem.recall_log as _recall_log
    from memem.recall import _get_current_session_id
    from memem.operations import memory_save as _memory_save
    from memem.recall import (
        memory_get as _memory_get,
    )
    from memem.recall import (
        memory_search as _memory_search,
    )
    from memem.recall import (
        memory_timeline as _memory_timeline,
    )
    from memem.transcripts import transcript_search as _transcript_search

    mcp = FastMCP("memem")

    @mcp.tool()
    def memory_search(
        query: Annotated[
            str,
            Field(
                description="Search query against the mined memory vault (not raw transcripts).",
                min_length=1,
                max_length=500,
            ),
        ],
        limit: Annotated[
            int,
            Field(description="Max results to return.", ge=1, le=50),
        ] = 10,
        scope_id: Annotated[
            str,
            Field(description='Project scope (soft bonus, not hard filter). Default "default".'),
        ] = "default",
    ) -> str:
        """Compact-index search (~50 tok/result). Use FIRST to narrow candidates before pulling full content — returns IDs + titles + 1-line snippets. Call when the user references a topic, person, project, or decision not already in context."""
        t0 = time.monotonic()
        session_id = _get_current_session_id()
        result = _memory_search(query, limit=limit, scope_id=scope_id)
        try:
            _recall_log.log_recall(
                call_type="tool_memory_search",
                query=query,
                returned_ids=_extract_ids_from_markdown(result),
                latency_ms=int((time.monotonic() - t0) * 1000),
                source="mcp",
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    @mcp.tool()
    def memory_get(
        ids: Annotated[
            list[str],
            Field(
                description="List of 8-character ID prefixes to fetch.",
                min_length=1,
                max_length=50,
            ),
        ],
        scope_id: Annotated[
            str,
            Field(description='Project scope. Default "default". Does not filter results.'),
        ] = "default",
    ) -> str:
        """Full content fetch by IDs (~500 tok/result). Use AFTER memory_search when you know which specific memories you need, or call directly with an 8-character ID prefix from the SessionStart episode catalog."""
        t0 = time.monotonic()
        session_id = _get_current_session_id()
        result = _memory_get(ids, scope_id=scope_id)
        try:
            _recall_log.log_recall(
                call_type="tool_memory_get",
                query=" ".join(ids),
                returned_ids=list(ids),
                latency_ms=int((time.monotonic() - t0) * 1000),
                source="mcp",
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    @mcp.tool()
    def memory_timeline(
        memory_id: Annotated[
            str,
            Field(
                description="Anchor memory ID (8-char prefix).",
                min_length=8,
                max_length=64,
            ),
        ],
        depth_before: Annotated[
            int,
            Field(description="Memories to include before the anchor.", ge=0, le=20),
        ] = 5,
        depth_after: Annotated[
            int,
            Field(description="Memories to include after the anchor.", ge=0, le=20),
        ] = 5,
        scope_id: Annotated[
            str,
            Field(description='Project scope. Default "default".'),
        ] = "default",
    ) -> str:
        """Chronological thread via related[] graph + same-project window. Use when you need the narrative around a memory (what led to it, what came after) — typically for understanding a decision's history."""
        t0 = time.monotonic()
        session_id = _get_current_session_id()
        result = _memory_timeline(
            memory_id,
            depth_before=depth_before,
            depth_after=depth_after,
            scope_id=scope_id,
        )
        try:
            _recall_log.log_recall(
                call_type="tool_memory_timeline",
                query=memory_id,
                returned_ids=[memory_id],
                latency_ms=int((time.monotonic() - t0) * 1000),
                source="mcp",
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    @mcp.tool()
    def active_memory_slice(
        query: Annotated[
            str,
            Field(
                description="Query describing current task or topic.",
                min_length=1,
                max_length=2000,
            ),
        ],
        task_mode: Annotated[
            str | None,
            Field(
                default=None,
                description="Current task mode (optional).",
            ),
        ] = None,
        scope_id: Annotated[
            str,
            Field(description="Optional project scope (soft bonus)."),
        ] = "",
        paths_context: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Recently-accessed file paths in the current session (e.g. ['memem/server.py']). Memories whose paths: frontmatter globs match receive a small 1.05x bonus.",
            ),
        ] = None,
    ) -> dict:
        """Query-shaped working-state slice (~150ms). Call this when you encounter unfamiliar references (version numbers like v1.10.1, project names, bug shortcodes the user expects you to know), when about to make a decision the user might have already made in a prior session, OR when the user uses retrieval-language ('remember', 'we discussed', 'did we agree')."""
        t0 = time.monotonic()
        session_id = _get_current_session_id()
        from memem.render import render_slice
        from memem.retrieve import retrieve

        results = retrieve(query, k=8, log_call_type=None, scope_id=scope_id, paths_context=paths_context)
        working = {"task_mode": task_mode} if task_mode else {}
        md = render_slice(query, results, working)
        try:
            _recall_log.log_recall(
                call_type="tool_active_slice",
                query=query,
                # IDs come from the MemoryHit dicts, not the rendered markdown
                # (render_slice output carries no 8-hex ids) — this is the
                # citation-rate telemetry for the passive-mode evaluation.
                returned_ids=[(h.get("id") or "")[:8] for h in results if h.get("id")],
                latency_ms=int((time.monotonic() - t0) * 1000),
                source="mcp",
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            pass
        return {"result": md}

    @mcp.tool()
    def memory_save(
        content: Annotated[
            str,
            Field(
                description="Atomic lesson body — one decision, convention, or preference with rationale.",
                min_length=10,
                max_length=5000,
            ),
        ],
        title: Annotated[
            str,
            Field(
                description='Declarative title <120 chars (e.g. "Use RS256 JWTs in prod"). Defaults to first 60 chars of content.',
                max_length=120,
            ),
        ] = "",
        scope_id: Annotated[
            str,
            Field(description='"default" for global memories; project name (e.g. "memem") for project-scoped.'),
        ] = "default",
        tags: Annotated[
            str,
            Field(
                description="Comma-separated lowercase tags (e.g. \"auth,jwt\"). Boost memory_search recall scoring.",
            ),
        ] = "",
        layer: Annotated[
            int | None,
            Field(
                ge=0,
                le=3,
                description="DEPRECATED (v2.8.0): no effect — omit. Accepted for backward compatibility only.",
            ),
        ] = None,
        paths: Annotated[
            list[str] | None,
            Field(description="File paths this memory applies to (glob-matched for scope boost). Optional."),
        ] = None,
    ) -> str:
        """Store one atomic durable lesson to persistent memory (decisions, preferences, architecture rationale, env quirks). Applies dedup: score ≥0.92 rejects as duplicate; 0.70–0.92 merges into existing memory; <0.70 saves new. Use transcript_search for session logs, not this tool."""
        return _memory_save(content, title=title, scope_id=scope_id, tags=tags, layer=layer, paths=paths)

    @mcp.tool()
    def transcript_search(
        query: Annotated[
            str,
            Field(
                description="Search phrase or keyword list.",
                min_length=1,
                max_length=500,
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description="Max session excerpts to return.",
                ge=1,
                le=20,
            ),
        ] = 5,
    ) -> str:
        """Raw session logs search — different corpus from the vault. Searches ~/.claude/projects/**/*.jsonl (not mined memories). Use when you need the actual back-and-forth of a prior conversation; use memory_search/memory_get for durable knowledge lookup."""
        return _transcript_search(query, limit=limit)

    return mcp


def main() -> None:
    # Build MCP lazily: only CLI command `None` (the default, no-arg run) needs it.
    # All other commands (--status, --rebuild-index, etc.) can run without `mcp`.
    needs_mcp = len(sys.argv) < 2
    mcp = _build_mcp() if needs_mcp else None
    dispatch_cli(sys.argv, mcp)


if __name__ == "__main__":
    main()
