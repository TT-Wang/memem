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
    from typing import Annotated

    from mcp.server.fastmcp import FastMCP
    from pydantic import Field

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
                    "Natural-language query describing what you want to find. "
                    "Can be a question (\"how did we handle auth?\"), a topic "
                    "(\"jwt rotation\"), or a fragment of a decision you vaguely "
                    "remember. Keyword match is fuzzy (FTS5 + synonym expansion) "
                    "so approximate terms work."
                ),
                min_length=1,
                max_length=500,
            ),
        ],
        scope_id: Annotated[
            str,
            Field(
                description=(
                    "Project scope to search within. Defaults to \"default\" which "
                    "searches across all projects. Pass a specific project name to "
                    "narrow the search to that project's memories only."
                ),
            ),
        ] = "default",
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of top-ranked memories to return. Results are "
                    "ranked by 50% FTS relevance + 15% recency + 15% access "
                    "history + 20% importance. Linked memories may be expanded "
                    "beyond this limit."
                ),
                ge=1,
                le=50,
            ),
        ] = 10,
    ) -> str:
        """Search memem memories for durable knowledge relevant to a query.

        Use this when you need to recall prior decisions, conventions, bug fixes,
        user preferences, or lessons learned from past Claude Code sessions. It
        searches the full Obsidian-backed memory store via SQLite FTS5, ranks
        results with a multi-signal scorer (relevance + recency + usage + importance),
        and expands related-memory links.

        Behaviour:
          - Read-only. Does not modify any memory, index, or sidecar file.
            Only side effect is a bump to the access-count telemetry sidecar
            (~/.memem/telemetry.json), which influences future ranking.
          - No authentication required. memem is local-first; there are no
            credentials, tokens, or API keys.
          - No rate limits. Typical latency is under 100ms on corpora up to
            ~10k memories; pathological queries can take up to ~500ms.
          - Data access scope: reads from ~/obsidian-brain/memem/memories/
            and ~/.memem/search.db. Nothing leaves the local machine.
          - Idempotent: calling twice with the same query returns the same
            results (modulo the access-count telemetry bump).
          - Failure modes: returns "No memories found for: <query>" on empty
            result sets. Never raises to the caller; internal errors fall
            back to a slower file-scan path.

        Use `memory_recall` when:
        - You need specific facts ("what auth library did we pick?")
        - You want to check if a topic has prior context before making a decision
        - You're debugging and want to find if this bug was fixed before

        Do NOT use for:
        - Session-level what-I-did-today logs (use `transcript_search` instead)
        - On-demand query-tailored briefings (use `context_assemble` instead)
        - Listing every memory (use `memory_list` instead)

        Returns: Markdown-formatted memory entries, grouped under a "### Memories"
        header. Each entry has the memory title and body excerpt. If no matches
        are found, returns "No memories found for: <query>".

        Example:
            memory_recall(query="jwt auth algorithm", limit=5)
            → returns the top 5 memories mentioning JWT auth, such as a memory
              documenting the decision to use RS256 in production.
        """
        return _memory_recall(query, scope_id=scope_id, limit=limit)

    @mcp.tool()
    def memory_search(
        query: Annotated[
            str,
            Field(
                description=(
                    "Natural-language query describing what you want to find. "
                    "Returns a compact index — IDs, titles, layer, and a 1-line "
                    "essence snippet — not full content."
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
            Field(description='Project scope to search within. Default "default".'),
        ] = "default",
    ) -> str:
        """Layer-1 compact index search — the entry point to the 3-tier recall workflow.

        Use this FIRST when you need to find memories. Returns ~50 tokens per
        result (ID + layer + title + snippet) plus a one-hop graph-traversed
        section of related memories. Then drill into specific IDs via
        `memory_get` for full content, or trace the narrative around one via
        `memory_timeline`.

        Behaviour:
          - Read-only, idempotent, no access-count bump (unlike memory_recall)
          - Local-only SQLite FTS5 + Obsidian vault reads
          - One-hop graph traversal via the `related` field — linked memories
            are included under a separate "Related memories" section
          - No authentication, no rate limits, no network I/O

        Use when:
          - You want to scan many candidates cheaply before committing tokens
          - You're not sure exactly which memory you need and want to see titles
          - Session start or topic shift — claude-mem pattern

        Do NOT use for:
          - Fetching full content when you already know the ID — use `memory_get`
          - Chronological narrative — use `memory_timeline`

        Returns: Markdown with `### Compact memory index` header, compact lines,
        and optionally a `### Related memories` section.

        Example:
            memory_search(query="jwt auth", limit=5)
            → returns 5 compact lines + any related memories linked by graph
        """
        return _memory_search(query, limit=limit, scope_id=scope_id)

    @mcp.tool()
    def memory_get(
        ids: Annotated[
            list[str],
            Field(
                description=(
                    "List of memory IDs (8-char prefix supported). Fetch the "
                    "full content of these specific memories."
                ),
                min_length=1,
                max_length=50,
            ),
        ],
        scope_id: Annotated[
            str,
            Field(description='Project scope. Default "default".'),
        ] = "default",
    ) -> str:
        """Layer-2 full content fetch — drill into specific memories by ID.

        Use this AFTER `memory_search` has given you a compact index and you
        want the full content of specific candidates. ~500 tokens per result.
        Follows the `related` graph one hop and includes linked memories.

        Behaviour:
          - Read-only, idempotent, no telemetry side effects
          - Accepts 8-char ID prefixes (same format as the compact index)
          - IDs that can't be resolved produce a `[not-found: <id>]` marker,
            the call never fails

        Use when:
          - You've seen titles via `memory_search` and want full bodies
          - You need specific known memories by ID (e.g. from a prior brief)

        Do NOT use for:
          - Open-ended search — use `memory_search`
          - Chronological context — use `memory_timeline`

        Returns: Markdown with full content per requested memory, then a
        `### Related memories` section via one-hop graph traversal.

        Example:
            memory_get(ids=["abc12345", "def67890"])
            → returns full bodies of those 2 memories + any linked ones
        """
        return _memory_get(ids, scope_id=scope_id)

    @mcp.tool()
    def memory_timeline(
        memory_id: Annotated[
            str,
            Field(
                description=(
                    "Anchor memory ID (8-char prefix supported). The timeline "
                    "is built around this memory."
                ),
                min_length=4,
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
        """Layer-3 chronological thread — narrative context around a memory.

        Builds a chronological thread around an anchor memory using:
          1. The anchor's `related[]` list (forward links)
          2. Any memory whose `related[]` points back at the anchor (reverse links)
          3. Same-project memories in the chronological window before/after

        Use when:
          - You found a memory via `memory_search` and want to understand
            what led to it (decisions before) and what came after (consequences)
          - Reconstructing the history of a decision or design evolution
          - Debugging "why did we end up here?" questions

        Do NOT use for:
          - Open-ended search — use `memory_search`
          - Fetching specific known memories — use `memory_get`

        Returns: Markdown with `### Timeline around ...` header, **Before**
        section (chronological), the anchor, and **After** section.

        Example:
            memory_timeline(memory_id="abc12345", depth_before=5, depth_after=5)
            → returns 5 memories that led to abc12345 + abc12345 itself + 5 that followed
        """
        return _memory_timeline(
            memory_id,
            depth_before=depth_before,
            depth_after=depth_after,
            scope_id=scope_id,
        )

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
          - Not idempotent: calling twice with identical content triggers
            the dedup check and the second call returns a "Memory already
            exists" error instead of a duplicate write.
          - Failure modes: rejected inputs return a string error ("Memory
            already exists", "Memory rejected: <security threat>"); they
            never raise to the caller.

        Every save goes through:
        1. Prompt-injection + credential-exfil security scan (rejects matches)
        2. Fuzzy deduplication against existing memories (word+bigram+trigram
           overlap — rejects near-duplicates with clear reason)
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
        return _memory_save(content, title=title, scope_id=scope_id, tags=tags)

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
        - Mining Claude Code session logs (the background miner handles that
          automatically; no manual import needed)

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
        - Query-tailored context briefings (use `context_assemble`)

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
        """Assemble a query-tailored context briefing from all available knowledge.

        This is the highest-value memem tool. It gathers the relevant subset
        of memories, the project's playbook, and related session transcripts,
        then uses Claude Haiku to synthesise a focused markdown briefing for
        the given query. The result is a ready-to-read summary, NOT a raw
        memory dump — usually 300-800 tokens of distilled relevant knowledge.

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
        - Starting a new session and you want the assistant loaded with context
          before the first real question (the auto-recall hook does this on
          `UserPromptSubmit`, but you can also call it manually)
        - Onboarding to a project mid-session — ask "what do I know about X?"
        - Before making a decision in an area where prior decisions exist

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

    return mcp


def main() -> None:
    # Build MCP lazily: only CLI command `None` (the default, no-arg run) needs it.
    # All other commands (--status, --rebuild-index, etc.) can run without `mcp`.
    needs_mcp = len(sys.argv) < 2
    mcp = _build_mcp() if needs_mcp else None
    dispatch_cli(sys.argv, mcp)


if __name__ == "__main__":
    main()
