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
    from memem.cross_vault import load_vault_registry as _load_vault_registry
    from memem.cross_vault import search_across_vaults as _search_across_vaults
    from memem.graph_index import (
        _rebuild_graph as _memory_graph_rebuild,
    )
    from memem.graph_index import (
        format_graph_audit as _memory_graph_audit,
    )
    from memem.graph_index import (
        format_graph_neighbors as _memory_graph_neighbors,
    )
    from memem.operations import memory_import as _memory_import
    from memem.operations import memory_save as _memory_save
    from memem.recall import (
        memory_get as _memory_get,
    )
    from memem.recall import memory_list as _memory_list
    from memem.recall import (
        memory_recall as _memory_recall,
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
    def memory_recall(
        query: Annotated[
            str,
            Field(
                description=(
                    "Search query for recalling memories. Use natural-language phrases "
                    "or keywords describing the topic, decision, or concept to retrieve."
                ),
                min_length=1,
                max_length=500,
            ),
        ],
        scope_id: Annotated[
            str,
            Field(
                description=(
                    "Project scope for retrieval. scope_id is a soft bonus — memories "
                    "in this project rank higher, but strong cross-project results are "
                    "not excluded (changed from hard filter in v2.6.0). Default "
                    "\"default\" applies no scope bonus."
                ),
            ),
        ] = "default",
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of top-ranked memories to return. Results ranked "
                    "by the unified retrieval engine: three-way RRF fusion (semantic + "
                    "BM25 + full-text) with usage/scope/link/importance signals and "
                    "MMR diversification. Linked memories may be expanded beyond this limit."
                ),
                ge=1,
                le=50,
            ),
        ] = 10,
        rerank_model: Annotated[
            str,
            Field(
                description=(
                    "Optional cross-encoder reranker model to apply on top of the "
                    "unified retrieval ranking. When non-empty, the top-50 candidates "
                    "are scored by the named model and reordered before truncation to "
                    "`limit`. Defaults to the MEMEM_RERANK_MODEL env var when unset. "
                    "Fast default: 'cross-encoder/ms-marco-MiniLM-L-12-v2' (~33M, ~50ms "
                    "CPU). High-quality option: 'BAAI/bge-reranker-v2-m3' (~568M, "
                    "multilingual). Model is downloaded on first use."
                ),
                max_length=200,
            ),
        ] = "",
    ) -> str:
        """Deprecated: prefer memory_search + memory_get for token efficiency. This legacy alias combines compact search and full-content fetch in one call. Still functional for backward compatibility."""
        t0 = time.monotonic()
        session_id = _get_current_session_id()
        result = _memory_recall(query, scope_id=scope_id, limit=limit, rerank_model=rerank_model or None)
        try:
            _recall_log.log_recall(
                call_type="tool_memory_recall",
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
    def memory_search(
        query: Annotated[
            str,
            Field(
                description=(
                    "Compact-index search (~50 tok/result). Use FIRST to narrow candidates "
                    "before pulling full content. Returns IDs + titles + 1-line snippets. "
                    "Call this when the user references a topic, person, project, or "
                    "decision that isn't already in conversation context."
                ),
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
            Field(
                description=(
                    'Project scope to search within. Default "default". '
                    "scope_id is a soft bonus — memories in this project rank higher, "
                    "but strong cross-project results are not excluded (changed from "
                    "hard filter in v2.6.0)."
                ),
            ),
        ] = "default",
    ) -> str:
        """Compact-index search (~50 tok/result). Use FIRST to narrow candidates before pulling full content. Returns IDs + titles + 1-line snippets. Call this when the user references a topic, person, project, or decision that isn't already in conversation context."""
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
                description=(
                    "Full content fetch by IDs (~500 tok/result). Use AFTER memory_search "
                    "to retrieve specific memories. Or call directly with an 8-character ID prefix "
                    "from the SessionStart episode catalog."
                ),
                min_length=1,
                max_length=50,
            ),
        ],
        scope_id: Annotated[
            str,
            Field(
                description=(
                    'Project scope. Default "default". Reserved — fetch by ID is '
                    "scope-independent; this parameter is accepted for API compatibility "
                    "but does not filter results."
                ),
            ),
        ] = "default",
    ) -> str:
        """Full content fetch by IDs (~500 tok/result). Use AFTER memory_search to retrieve specific memories. Or call directly with an 8-character ID prefix from the SessionStart episode catalog."""
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
                description=(
                    "Chronological thread via related[] graph + same-project window. "
                    "Use when you need the narrative around a memory (what led to it, "
                    "what came after) — typically for understanding a decision's history."
                ),
                min_length=8,
                max_length=64,
            ),
        ],
        depth_before: Annotated[
            int,
            Field(description="How many memories to include before the anchor.", ge=0, le=20),
        ] = 5,
        depth_after: Annotated[
            int,
            Field(description="How many memories to include after the anchor.", ge=0, le=20),
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
    def memory_graph(
        memory_id: Annotated[
            str,
            Field(
                description="Memory ID or 8-char prefix to inspect in the typed memory graph.",
                min_length=8,
                max_length=64,
            ),
        ],
        include_history: Annotated[
            bool,
            Field(description="Include supersedes/contradicts/history edges, not just normal recall edges."),
        ] = False,
        limit: Annotated[
            int,
            Field(description="Maximum graph edges to return.", ge=1, le=100),
        ] = 20,
    ) -> str:
        """Inspect typed, scored graph neighbors for one memory."""
        return _memory_graph_neighbors(memory_id, include_history=include_history, limit=limit)

    @mcp.tool()
    def memory_graph_audit() -> str:
        """Audit graph quality: orphans, dead links, one-way links, hubs, and stale edges."""
        return _memory_graph_audit()

    @mcp.tool()
    def memory_graph_rebuild(
        scope_id: Annotated[
            str,
            Field(description='Optional project scope. "default" rebuilds all active memories.'),
        ] = "default",
    ) -> str:
        """Rebuild the local typed graph side index from Obsidian memories."""
        scope = None if scope_id in ("", "default", "general") else scope_id
        count = _memory_graph_rebuild(scope_id=scope)
        return f"Graph rebuilt: {count} edges"

    @mcp.tool()
    def active_memory_slice(
        query: Annotated[
            str,
            Field(
                description=(
                    "Call this when you encounter unfamiliar references (version numbers "
                    "like v1.10.1, project names, bug shortcodes the user expects you to "
                    "know), when about to make a decision the user might have already made "
                    "in a prior session, OR when the user uses retrieval-language "
                    "('remember', 'we discussed', 'did we agree'). ~150ms latency. The "
                    "first call in a session is recommended if you don't have project "
                    "context from CLAUDE.md."
                ),
                min_length=1,
                max_length=2000,
            ),
        ],
        task_mode: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Current task mode (optional, surfaced in Working section)."
                ),
            ),
        ] = None,
        scope_id: Annotated[
            str,
            Field(
                description=(
                    "Optional project scope. Memories in this project receive a soft "
                    "ranking bonus; cross-project results are not excluded."
                ),
            ),
        ] = "",
    ) -> dict:
        """Call this when you encounter unfamiliar references (version numbers like v1.10.1, project names, bug shortcodes the user expects you to know), when about to make a decision the user might have already made in a prior session, OR when the user uses retrieval-language ('remember', 'we discussed', 'did we agree'). ~150ms latency. The first call in a session is recommended if you don't have project context from CLAUDE.md."""
        t0 = time.monotonic()
        session_id = _get_current_session_id()
        from memem.render import render_slice
        from memem.retrieve import retrieve

        results = retrieve(query, k=8, log_call_type=None, scope_id=scope_id)
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
                description=(
                    "The full memory body, written as natural-language prose. "
                    "Should be atomic (one idea per save): capture ONE decision, "
                    "convention, lesson, or preference with enough context to be "
                    "useful in a future session. Include the rationale, not just "
                    "the outcome. Max ~2000 characters recommended."
                ),
                min_length=10,
                max_length=5000,
            ),
        ],
        title: Annotated[
            str,
            Field(
                description=(
                    "Short descriptive title, <120 characters. If empty, the first "
                    "60 chars of content are used. A good title is a declarative "
                    "summary (\"Use RS256 JWTs in prod\"), not a question."
                ),
                max_length=120,
            ),
        ] = "",
        scope_id: Annotated[
            str,
            Field(
                description=(
                    "Project scope for the memory. \"default\" for cross-project "
                    "global memories (user preferences, general lessons). A "
                    "specific project name for project-scoped memories (e.g. "
                    "\"memem\", \"my-webapp\")."
                ),
            ),
        ] = "default",
        tags: Annotated[
            str,
            Field(
                description=(
                    "Comma-separated tag list (e.g. \"auth,jwt,security\"). Tags "
                    "are used for filtering in `memory_list` and contribute to "
                    "recall scoring. Keep them lowercase and topical."
                ),
            ),
        ] = "",
        layer: Annotated[
            int | None,
            Field(
                ge=0,
                le=3,
                description=(
                    "Optional layer override (0=identity, 1=generic, 2=domain, 3=archival). "
                    "Omit to auto-classify via the same heuristic the miner uses. "
                    "Use 0 only for project-identity facts ('my-app uses Postgres + Redis'). "
                    "Use 1 for cross-project conventions. "
                    "Use 3 for rare/archival lessons that should be tool-call-only. "
                    "Most saves should leave this unset."
                ),
            ),
        ] = None,
    ) -> str:
        """Save a durable lesson, decision, or convention to persistent memory.

        Use this to capture knowledge that should survive across Claude Code
        sessions: user preferences, architecture decisions with rationale,
        environment quirks, non-obvious bug fixes, or anything you'd otherwise
        have to re-explain in the next session.

        Behaviour:
          - MUTATION. Writes a new markdown file under
            ~/obsidian-brain/memem/memories/, appends to the FTS5 index
            (~/.memem/search.db), updates ~/obsidian-brain/memem/_index.md,
            and writes an append-only entry to ~/.memem/events.jsonl.
            Writes are atomic (tmp + fsync + os.replace) and fcntl-locked.
          - No authentication required. Local-first; no credentials.
          - No rate limits. Typical latency 50-200ms including the security
            scan, dedup check, and write sync.
          - Data access scope: writes stay entirely on the local filesystem.
            Nothing is sent over the network.
          - Not idempotent: deduplication applies on every call. Three-band
            behaviour: score ≥ 0.92 → exact duplicate, rejected with
            "Memory already exists: [id8] title. Not saved."; 0.70–0.92 →
            similar content, merged into existing memory via Haiku
            ("Merged into existing memory [id8] title..."); score < 0.70 →
            new memory saved normally. Merge failures fall back to reject.
          - Failure modes: rejected inputs return a string error ("Memory
            already exists", "Memory rejected: <security threat>"); they
            never raise to the caller.

        Every save goes through:
        1. Prompt-injection + credential-exfil security scan (rejects matches)
        2. Fuzzy deduplication against existing memories (three-band:
           reject exact / merge similar / save new — see Behaviour above)
        3. Automatic related-memory linking (adds `related` frontmatter field)
        4. Write to Obsidian markdown file + FTS5 index + _index.md

        Use `memory_save` for:
        - "We decided to use X because Y" (decision + rationale)
        - "User prefers small focused PRs, not big bundled ones" (preference)
        - "Database connection pool must be at least 20 for prod" (invariant)
        - "bcrypt.compare is async — always await" (gotcha)

        Do NOT use for:
        - "Today I worked on X" (session logs — use transcript_search to find those)
        - Trivial facts easily re-discovered from reading code
        - Speculative or unverified claims
        - Duplicates of existing memories (the dedup check will reject them anyway)

        Returns: A confirmation like "Memory saved: 63d6570e... 'JWT auth uses RS256'"
        on success, or an error message (starting with "Memory already exists" or
        "Memory rejected") on failure.

        Example:
            memory_save(
                content="Use RS256 (not HS256) for JWT in production. HS256 "
                        "requires sharing the signing secret across services "
                        "which leaked via an env var export last quarter (#1247).",
                title="JWT algorithm — RS256 only in prod",
                tags="auth,jwt,security,postmortem",
                scope_id="my-webapp",
            )
        """
        return _memory_save(content, title=title, scope_id=scope_id, tags=tags, layer=layer)

    @mcp.tool()
    def memory_list(
        scope_id: Annotated[
            str,
            Field(
                description=(
                    "Project scope to list. \"default\" lists every memory across "
                    "all projects. A specific project name lists only memories "
                    "tagged with that project."
                ),
            ),
        ] = "default",
    ) -> str:
        """List every memory in a scope, with counts and source breakdown.

        Use this when you want to see the full inventory of what memem has
        stored — e.g. to audit which projects have the most memories, to check
        if a specific memory you wrote earlier is still present, or to find a
        memory whose exact title you remember but whose keywords are ambiguous.

        Behaviour:
          - Read-only. No mutations at all, not even telemetry bumps.
          - No authentication required.
          - No rate limits. Latency scales linearly with memory count;
            typical sub-second on corpora up to ~10k.
          - Data access scope: reads ~/obsidian-brain/memem/memories/
            markdown files via filesystem glob. Nothing leaves the machine.
          - Idempotent and deterministic for a given filesystem state.
          - Failure modes: returns "No memories in scope: <scope>" for an
            empty scope. Never raises.

        Use `memory_list` when:
        - You want to see everything, not a ranked subset
        - You need to audit the current memory inventory
        - You're about to run a cleanup/purge operation and want a preflight
        - You suspect `memory_recall` is missing something and want to confirm

        Do NOT use for:
        - Searching for specific content (use `memory_recall` — faster, ranked)
        - Assembling a context briefing (use `context_assemble`)

        Returns: A markdown listing with the total memory count, source type
        breakdown (mined/user/import), and one line per memory showing its
        short ID, title, and project. Memories are sorted newest-first by
        creation date.

        Example output:
            **47 memories**
            Sources: mined:32, user:12, import:3

            - [63d6570e] JWT algorithm — RS256 only in prod | project:my-webapp
            - [a8b12c44] Pytest fixtures must use tmp_path | project:default
            - ...
        """
        return _memory_list(scope_id=scope_id)

    @mcp.tool()
    def memory_import(
        source_path: Annotated[
            str,
            Field(
                description=(
                    "Absolute path to a file or directory containing content to "
                    "import as memories. Supported formats: plain text files "
                    "(.txt, .md) treated as single memories, directories "
                    "recursively scanned, and structured exports (JSONL chat "
                    "logs). Must be under $HOME for path-traversal safety."
                ),
                min_length=1,
            ),
        ],
        scope_id: Annotated[
            str,
            Field(
                description=(
                    "Project scope to tag imported memories with. Defaults to "
                    "\"default\" (cross-project). Use a specific project name "
                    "to scope the import."
                ),
            ),
        ] = "default",
    ) -> str:
        """Bulk-import memories from a file, directory, or chat export.

        Use this to seed memem with existing notes, CLAUDE.md content,
        documentation excerpts, or chat logs you want to make searchable.
        Each imported item runs through the same security scan and deduplication
        as `memory_save`, so clean imports even from messy sources.

        Behaviour:
          - MUTATION. Writes one or more memory markdown files, updates
            FTS5 index, _index.md, and events.jsonl. Same atomic +
            fcntl-locked write path as `memory_save`.
          - No authentication required.
          - No rate limits, but latency scales with source size —
            importing a 100-item directory can take several seconds.
          - Data access scope: reads the supplied `source_path` from the
            local filesystem. Guarded against path traversal: the resolved
            path must be inside $HOME; anything outside is rejected.
            Nothing is sent over the network.
          - Not idempotent: re-importing the same source triggers the
            dedup check, which rejects duplicates with a summary count.
          - Failure modes: invalid or non-existent source paths return a
            string error. Individual rejected items are counted in the
            summary and do not abort the whole import.

        Use `memory_import` for:
        - Initial bootstrap from an existing `CLAUDE.md` or notes folder
        - Absorbing a team-wide decision log into a project scope
        - One-off batch captures from a conversation export

        Do NOT use for:
        - Incremental per-conversation saves (use `memory_save` for single items)
        - Mining Claude Code session logs (the Stop-event-triggered
          `mine_delta` subprocess handles that automatically; no manual
          import needed)

        Returns: A summary like "Imported 12 memories from 18 candidates
        (rejected 6 duplicates)". Errors are returned as human-readable messages.

        Example:
            memory_import(
                source_path="/home/alice/notes/team-decisions.md",
                scope_id="my-webapp",
            )
        """
        return _memory_import(source_path, scope_id=scope_id)

    @mcp.tool()
    def transcript_search(
        query: Annotated[
            str,
            Field(
                description=(
                    "Search phrase or keyword list. Matches against the raw "
                    "Claude Code session JSONL transcripts (not the mined "
                    "memory corpus). Use this for session-level context "
                    "recovery, not durable knowledge lookup."
                ),
                min_length=1,
                max_length=500,
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of session excerpts to return. Each excerpt "
                    "is a short quote with the session id and date."
                ),
                ge=1,
                le=20,
            ),
        ] = 5,
    ) -> str:
        """Search raw Claude Code session transcripts for past conversation excerpts.

        This is the DIFFERENT from `memory_recall` — it searches the raw session
        JSONL files under `~/.claude/projects/`, not the mined memory corpus.
        Use it when you need to find the actual back-and-forth of a prior
        conversation, not the distilled lesson from it.

        Behaviour:
          - Read-only. Does not modify transcripts, memories, or any index.
          - No authentication required.
          - No rate limits. Latency scales with transcript corpus size;
            typical 100-500ms across a year of daily sessions.
          - Data access scope: reads ~/.claude/projects/**/*.jsonl via
            direct filesystem access. Does NOT read ~/obsidian-brain/
            (that's what `memory_recall` and `memory_list` are for).
            Nothing is sent over the network.
          - Idempotent and deterministic for a given filesystem state.
          - Failure modes: returns "No matching sessions" on empty
            result sets. Sessions older than Claude Code's 30-day
            retention window are not searchable (they've been deleted).

        Use `transcript_search` when:
        - You want to recall "what did I actually say three weeks ago about X"
        - A mined memory references a session and you want the full context
        - You want to find all sessions that touched a specific file or topic
        - You're verifying a memory's source_session or debugging extraction

        Do NOT use for:
        - Looking up durable knowledge (use `memory_recall` — mined, ranked, faster)
        - Listing memories (use `memory_list`)
        - Slice-first working context (use `active_memory_slice`)

        Returns: Markdown-formatted session excerpts with the session id,
        date, and matched text, or "No matching sessions" if nothing matches.
        Transcripts older than Claude Code's 30-day retention are not searchable.

        Example:
            transcript_search(query="redis connection pool size", limit=3)
        """
        return _transcript_search(query, limit=limit)

    @mcp.tool()
    def context_assemble(
        query: Annotated[
            str,
            Field(
                description=(
                    "The question or task you want context for. Typically this "
                    "is the user's first message in a new session — the assembler "
                    "will pull relevant memories and synthesise a focused briefing "
                    "tailored to what they asked."
                ),
                min_length=1,
                max_length=2000,
            ),
        ],
        project: Annotated[
            str,
            Field(
                description=(
                    "Project scope for the assembly. \"default\" pulls from "
                    "cross-project memories. A specific project name pulls that "
                    "project's playbook + memories + session history."
                ),
            ),
        ] = "default",
    ) -> str:
        """Assemble an explicit secondary briefing from all available knowledge.

        `active_memory_slice` is the default runtime path for live work. This
        tool stays available for callers that explicitly want a narrative
        markdown briefing synthesized from playbook, memory, and transcript
        materials instead of the slice-first working state projection.

        Behaviour:
          - Read-only with respect to the memem memory store. Bumps access
            telemetry on memories it reads (same as `memory_recall`).
          - No authentication required by memem itself. The optional
            Haiku synthesis step shells out to the local `claude` CLI,
            which may use Claude Code credentials the user already has
            signed in — memem does not handle those credentials directly.
          - Rate limits: depend on the `claude` CLI backend in the healthy
            path. In degraded mode (claude CLI missing), there are no
            rate limits at all — memem just returns raw materials.
          - Data access scope: reads ~/obsidian-brain/memem/memories/,
            ~/obsidian-brain/memem/playbooks/<project>.md, ~/.memem/search.db,
            and ~/.claude/projects/ transcripts. If the `claude` CLI is
            invoked, the gathered materials (up to 50KB) are sent to Haiku
            via that subprocess — which in turn sends them to Anthropic's
            API under the user's existing Claude Code session. In degraded
            mode nothing leaves the machine.
          - Latency: 3-15 seconds with Haiku; <500ms in degraded mode.
          - Not idempotent at the Haiku level: the same query can produce
            slightly different briefings across calls due to Haiku sampling.
            The underlying memory retrieval step IS deterministic.
          - Failure modes: returns "" on genuinely empty vaults. Never
            raises to the caller; Haiku failures silently fall back to
            returning the raw materials.

        Use `context_assemble` when:
        - You explicitly want a rewritten narrative brief rather than the raw
          working-state slice
        - You are onboarding to a project mid-session and want a compact
          summary spanning playbook, memories, and transcripts
        - You are comparing the assembly projection against the slice-first
          runtime output for debugging or tuning

        Do NOT use for:
        - Simple keyword lookups (use `memory_recall` — faster, no LLM call)
        - Listing memories (use `memory_list`)
        - Finding a specific past conversation (use `transcript_search`)

        Degraded mode: if the `claude` CLI is not available on the host,
        this tool falls back to returning the raw materials (playbook +
        ranked memories) without Haiku synthesis, so it always returns
        SOMETHING useful.

        Returns: A markdown briefing tailored to the query. Length is
        typically 300-800 tokens, with headers, bullet lists, and
        cross-references to memory IDs where relevant.

        Example:
            context_assemble(
                query="help me fix the auth flow on staging",
                project="my-webapp",
            )
            → returns a brief covering: the RS256 JWT decision, the
              known bcrypt.compare gotcha, a link to the staging-specific
              env var issue from last month, etc.
        """
        from memem.assembly import context_assemble as _assemble
        return _assemble(query, project)

    @mcp.tool()
    def memory_remind(
        local_context_summary: Annotated[
            str,
            Field(
                description=(
                    "Compressed view of what's in the current context: the task, "
                    "key entities, decisions made so far, and open questions. "
                    "The remind engine uses this as the search query across all "
                    "registered memory vaults to surface relevant prior knowledge. "
                    "Aim for 1-3 sentences capturing the core topic."
                ),
                min_length=1,
                max_length=2000,
            ),
        ],
        max_results: Annotated[
            int,
            Field(
                description="Maximum number of cross-vault hits to return (across all vaults combined).",
                ge=1,
                le=20,
            ),
        ] = 3,
    ) -> str:
        """Search across all registered memory vaults and return top hits with relevance rationale.

        Use this at session start or on a context shift when you want to pull
        in prior knowledge from multiple memory vaults (e.g. a personal vault
        plus a project-specific vault). Unlike ``memory_search``, which searches
        a single configured vault, ``memory_remind`` reads the vault registry at
        ``~/.memem/vaults.json`` and searches all listed vaults, then merges and
        re-ranks the results.

        When no ``vaults.json`` exists (the common single-vault case), this tool
        behaves identically to a ``memory_search`` on the default vault — no
        configuration required, fully backward-compatible.

        Each result includes a ``why_relevant`` field that explains the match in
        plain language (e.g. ``"matches 0.74 on 'authentication retry'"``), so
        you can quickly assess relevance without reading the full memory body.

        Behaviour:
          - Read-only. Does not modify any memory or index.
          - No authentication required. All searches are local.
          - Latency scales with total vault size and number of vaults.
          - Failure modes: returns a "No cross-vault memories found" message on
            empty result sets. Individual vault failures are logged and skipped.

        Returns: Markdown-formatted listing of cross-vault hits, each with
        vault source, title, content excerpt, and why_relevant rationale.

        Example:
            memory_remind(
                local_context_summary="debugging the auth retry loop in the payments service",
                max_results=5,
            )
            → returns up to 5 memories from any registered vault that mention
              authentication, retry, or payments, with similarity rationale.
        """
        vault_registry = _load_vault_registry()
        hits = _search_across_vaults(local_context_summary, vault_registry, max_results=max_results)

        if not hits:
            return f"No cross-vault memories found for: {local_context_summary}"

        lines = [f"### Cross-vault recall — {len(hits)} hit{'s' if len(hits) != 1 else ''}"]
        lines.append("")
        for hit in hits:
            lines.append(f"**[{hit['vault_id']}] {hit['title']}**")
            lines.append(f"*{hit['why_relevant']}*")
            body_excerpt = hit["content"][:300].strip()
            if body_excerpt:
                lines.append("")
                lines.append(body_excerpt)
            lines.append("")

        return "\n".join(lines).rstrip()

    return mcp


def main() -> None:
    # Build MCP lazily: only CLI command `None` (the default, no-arg run) needs it.
    # All other commands (--status, --rebuild-index, etc.) can run without `mcp`.
    needs_mcp = len(sys.argv) < 2
    mcp = _build_mcp() if needs_mcp else None
    dispatch_cli(sys.argv, mcp)


if __name__ == "__main__":
    main()
