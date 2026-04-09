#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory for Claude Code.

Storage backends: ChromaDB (vector search) + Obsidian markdown vault.

Sections:
  Storage:     ChromaDB + Obsidian dual-write CRUD
  Generation:  _make_memory, domain detection, dedup
  Auto-seed:   Scan workspace projects + load starter packs on first run
  Retrieval:   Vector similarity search via ChromaDB
  MCP Tools:   memory_recall, memory_save, memory_list, memory_feedback, memory_import
  Transcript:  Search raw Claude Code JSONL session files
  Mining:      Parse JSONL sessions, extract insights, save as memories
  GC:          Decay stale, merge duplicates, prune excess
  CLI:         --mine-session, --mine-all, --gc, --purge-mined, --migrate, --install-cron
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from mcp.server.fastmcp import FastMCP
from starter_packs import detect_packs, get_pack_memories

# ─── Storage ──────────────────────────────────────────────────────

CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
MEMORIES_DIR = CORTEX_DIR / "memories"  # kept for migration
LOGS_DIR = CORTEX_DIR / "logs"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

CHROMADB_DIR = CORTEX_DIR / "chromadb"
_chroma_client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
_collection = _chroma_client.get_or_create_collection("memories")

OBSIDIAN_VAULT = Path.home() / "obsidian-brain"
OBSIDIAN_MEMORIES_DIR = OBSIDIAN_VAULT / "cortex" / "memories"
if OBSIDIAN_VAULT.exists():
    OBSIDIAN_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(text: str, max_len: int = 60) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:max_len]


def _reconstruct_memory(result: dict, idx: int) -> dict:
    """Reconstruct a memory dict from ChromaDB result."""
    mem = {"id": result["ids"][idx], "essence": result["documents"][idx]}
    meta = result["metadatas"][idx]
    for k, v in meta.items():
        # Restore lists from comma-separated strings
        if k in ("domain_tags", "contradicts"):
            mem[k] = [x for x in v.split(",") if x] if v else []
        # Restore bools
        elif k in ("verified",):
            mem[k] = v == "true" if isinstance(v, str) else bool(v)
        # Restore None from empty string
        elif v == "":
            mem[k] = None
        else:
            mem[k] = v
    return mem


def _load_memory(memory_id: str) -> dict | None:
    try:
        result = _collection.get(ids=[memory_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        return _reconstruct_memory(result, 0)
    except Exception:
        return None


def _flatten_meta(mem: dict) -> dict:
    """Flatten memory dict to ChromaDB-compatible metadata (str/int/float/bool only)."""
    meta = {}
    for k, v in mem.items():
        if k in ("id", "essence"):
            continue
        if v is None:
            meta[k] = ""
        elif isinstance(v, list):
            meta[k] = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            meta[k] = str(v).lower()
        elif isinstance(v, (str, int, float)):
            meta[k] = v
        else:
            meta[k] = str(v)
    return meta


def _save_memory(mem: dict, skip_obsidian: bool = False):
    """Save to ChromaDB (primary), then Obsidian (if vault exists)."""
    meta = _flatten_meta(mem)

    # ChromaDB first (source of truth)
    _collection.upsert(
        ids=[mem["id"]],
        documents=[mem.get("essence", "")],
        metadatas=[meta]
    )

    # Obsidian dual-write (skip for retrieval count updates)
    if not skip_obsidian and OBSIDIAN_MEMORIES_DIR.parent.exists():
        _write_obsidian_memory(mem)
        if mem.get("obsidian_file"):
            # Update ChromaDB metadata with obsidian filename
            meta["obsidian_file"] = mem["obsidian_file"]
            _collection.update(ids=[mem["id"]], metadatas=[meta])

    # Update index
    if not skip_obsidian and INDEX_PATH.exists():
        _append_or_update_index_line(mem)


def _write_obsidian_memory(mem: dict):
    """Write a memory as markdown to the Obsidian vault."""
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return
    title = mem.get("title", "Untitled")
    slug = _slugify(title)
    if not slug:
        slug = mem["id"][:8]
    filename = f"{slug}-{mem['id'][:8]}.md"

    # Delete old file if slug changed (prevents orphans)
    old_file = mem.get("obsidian_file", "")
    if old_file and old_file != filename:
        old_path = OBSIDIAN_MEMORIES_DIR / old_file
        if old_path.exists():
            old_path.unlink()

    # YAML frontmatter
    tags = mem.get("domain_tags", [])
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t]

    who = _extract_project(mem)
    why = _map_memory_type_to_why(
        mem.get("memory_type", "knowledge"),
        mem.get("source_type", ""),
        mem.get("essence", "")
    )
    clean_title = re.sub(r'^\[[^\]]+\]\s*', '', mem.get("title", "Untitled"))

    frontmatter = f"""---
title: {clean_title}
who: {who}
why: {why}
tags: [{', '.join(tags)}]
confidence: {mem.get('confidence', 0.5)}
created: {mem.get('created_at', '')[:10]}
related: []
---"""

    # Body
    essence = mem.get("essence", "")
    body = f"\n\n{essence}"

    # Full record as blockquote
    full_record = mem.get("full_record")
    if full_record and full_record != essence:
        body += f"\n\n> {full_record[:500]}"

    filepath = OBSIDIAN_MEMORIES_DIR / filename
    filepath.write_text(frontmatter + body)

    # Store filename in mem for deletion tracking
    mem["obsidian_file"] = filename


def _delete_memory(memory_id: str) -> bool:
    try:
        # Try to find and delete obsidian file
        result = _collection.get(ids=[memory_id], include=["metadatas"])
        if result["ids"]:
            obsidian_file = result["metadatas"][0].get("obsidian_file", "")
            if obsidian_file:
                obsidian_path = OBSIDIAN_MEMORIES_DIR / obsidian_file
                if obsidian_path.exists():
                    obsidian_path.unlink()

        _collection.delete(ids=[memory_id])
        _remove_index_line(memory_id)
        return True
    except Exception:
        return False


def _all_memories(scope_id: str | None = None) -> list[dict]:
    try:
        if scope_id:
            result = _collection.get(where={"scope_id": scope_id}, include=["documents", "metadatas"])
        else:
            result = _collection.get(include=["documents", "metadatas"])
        return [_reconstruct_memory(result, i) for i in range(len(result["ids"]))]
    except Exception:
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_memory(memory_id: str) -> dict | None:
    """Find a memory by full ID or prefix (min 8 chars)."""
    mem = _load_memory(memory_id)
    if mem:
        return mem
    if len(memory_id) >= 8:
        # Prefix match — get all IDs and filter
        all_ids = _collection.get(include=[])["ids"]
        for mid in all_ids:
            if mid.startswith(memory_id):
                return _load_memory(mid)
    return None


# ─── Generation ───────────────────────────────────────────────────

DOMAIN_KEYWORDS = {
    "auth": ["auth", "login", "jwt", "token", "oauth", "session", "password", "credential", "permission", "rbac"],
    "database": ["database", "sql", "query", "migration", "schema", "orm", "postgres", "mysql", "redis", "mongo", "table", "index"],
    "testing": ["test", "pytest", "jest", "mock", "assert", "fixture", "coverage", "spec", "unit test", "integration"],
    "api": ["api", "endpoint", "rest", "graphql", "http", "request", "response", "route", "middleware", "cors"],
    "deployment": ["deploy", "docker", "kubernetes", "ci", "cd", "pipeline", "container", "nginx", "aws", "cloud"],
    "frontend": ["react", "vue", "angular", "css", "html", "component", "dom", "browser", "ui", "ux"],
    "security": ["security", "vulnerability", "xss", "csrf", "injection", "encrypt", "hash", "ssl", "tls", "cert"],
    "performance": ["performance", "cache", "optimize", "latency", "throughput", "memory", "cpu", "profil", "benchmark"],
    "architecture": ["architecture", "pattern", "design", "refactor", "modular", "monolith", "microservice", "layer"],
    "git": ["git", "commit", "branch", "merge", "rebase", "pull request", "pr", "version control"],
    "config": ["config", "environment", "env", "settings", "variable", "secret", "dotenv"],
    "error-handling": ["error", "exception", "try", "catch", "throw", "handle", "retry", "fallback", "debug"],
}


def _detect_domain_room(content: str, title: str = "", tags: list[str] | None = None) -> tuple[str, str]:
    """Auto-detect domain and room from content."""
    text = f"{title} {content} {' '.join(tags or [])}".lower()

    def _kw_in_text(kw: str, txt: str) -> bool:
        if " " in kw:
            return kw in txt
        return bool(re.search(r'\b' + re.escape(kw) + r'\b', txt))

    domain_scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if _kw_in_text(kw, text))
        if score > 0:
            domain_scores[domain] = score

    if not domain_scores:
        return ("general", "")

    domain = max(domain_scores, key=domain_scores.get)
    room_keywords = DOMAIN_KEYWORDS[domain]
    matched = [kw for kw in room_keywords if _kw_in_text(kw, text)]
    room = matched[0] if matched else ""
    return (domain, room)


def _is_duplicate(content: str, scope_id: str = "default", threshold: float = 0.3, return_match: bool = False):
    """Check if content is too similar to an existing memory using ChromaDB similarity.
    threshold: ChromaDB distance below which content is considered duplicate (lower = more similar)."""
    try:
        kwargs = {"query_texts": [content], "n_results": 1, "include": ["documents", "metadatas", "distances"]}
        if scope_id:
            kwargs["where"] = {"scope_id": scope_id}
        result = _collection.query(**kwargs)
        if not result["ids"] or not result["ids"][0]:
            return None if return_match else False
        distance = result["distances"][0][0]
        if distance < threshold:
            if return_match:
                flat = {"ids": result["ids"][0], "documents": result["documents"][0], "metadatas": result["metadatas"][0]}
                return _reconstruct_memory(flat, 0)
            return True
    except Exception:
        pass
    return None if return_match else False


def _make_memory(content: str, title: str, memory_type: str = "knowledge",
                 scope_id: str = "default", tags: list[str] | None = None,
                 source_type: str = "auto_seed", confidence: float = 0.6,
                 promotion_status: str = "candidate", verified: bool = False,
                 source_session_id: str | None = None,
                 full_record: str | None = None) -> dict:
    """Create a memory dict. Single schema builder for all creation paths."""
    domain, room = _detect_domain_room(content, title, tags)
    effective_full_record = None if (full_record is None or full_record == content) else full_record
    return {
        "id": str(uuid.uuid4()),
        "essence": content,
        "full_record": effective_full_record,
        "title": title,
        "memory_type": memory_type,
        "scope_id": scope_id,
        "confidence": confidence,
        "impact_score": 0.0,
        "success_count": 0,
        "failure_count": 0,
        "retrieval_count": 0,
        "promotion_status": promotion_status,
        "verified": verified,
        "domain": domain,
        "room": room,
        "domain_tags": tags or [],
        "contradicts": [],
        "source_type": source_type,
        "source_session_id": source_session_id,
        "created_at": _now(),
        "last_retrieved_at": None,
        "last_validated_at": None,
    }


# ─── Auto-seed ───────────────────────────────────────────────────

SEED_MARKER_FILE = CORTEX_DIR / ".seeded_scopes"

_PROJECT_INDICATORS = {
    ".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "Gemfile", "pom.xml", "build.gradle", "requirements.txt",
    "composer.json", "Makefile", "CMakeLists.txt",
}

_SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", ".venv", "dist", "build",
    ".cache", ".local", ".config", ".ssh", ".gnupg", ".claude",
    ".cortex", ".npm", ".cargo", ".rustup",
}


def _was_seeded(scope_id: str) -> bool:
    if not SEED_MARKER_FILE.exists():
        return False
    return scope_id in set(s for s in SEED_MARKER_FILE.read_text().strip().split("\n") if s)


def _mark_seeded(scope_id: str):
    existing = ""
    if SEED_MARKER_FILE.exists():
        existing = SEED_MARKER_FILE.read_text().strip()
    seeded = set(s for s in existing.split("\n") if s) if existing else set()
    seeded.add(scope_id)
    tmp = SEED_MARKER_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(seeded)))
    tmp.replace(SEED_MARKER_FILE)


def _detect_workspace_projects(workspace: str | None = None) -> list[str]:
    workspace = workspace or os.environ.get("HOME", os.path.expanduser("~"))
    if not os.path.isdir(workspace):
        return []
    projects = []
    try:
        for entry in sorted(os.listdir(workspace)):
            if entry.startswith(".") or entry in _SKIP_DIRS:
                continue
            full_path = os.path.join(workspace, entry)
            if not os.path.isdir(full_path):
                continue
            try:
                contents = set(os.listdir(full_path))
            except OSError:
                continue
            if contents & _PROJECT_INDICATORS:
                projects.append(full_path)
    except OSError:
        pass
    return projects


def _auto_seed_project(scope_id: str, project_dir: str) -> list[dict]:
    """Seed memories from a single project directory."""
    cwd = project_dir
    if not os.path.isdir(cwd):
        return []

    name = os.path.basename(cwd)
    memories = []

    # README / CLAUDE.md
    for doc_name in ["CLAUDE.md", "README.md", "readme.md"]:
        doc_path = os.path.join(cwd, doc_name)
        if os.path.exists(doc_path):
            try:
                content = open(doc_path).read()[:2000]
                lines = content.split("\n")
                title_line = next((l for l in lines if l.startswith("# ")), "")
                title = title_line.lstrip("# ").strip() or doc_name
                paragraphs = re.split(r'\n\n+', content)
                desc = next((p for p in paragraphs if len(p.strip()) > 20 and not p.startswith("#")), "")
                if desc:
                    memories.append(_make_memory(
                        content=f"Project: {title}. {desc.strip()[:500]}",
                        title=f"[{name}] Project overview: {title}",
                        tags=["project", "overview", name],
                    ))
            except OSError:
                pass

    # Tech stack
    tech_detectors = [
        ("pyproject.toml", "Python"), ("package.json", "JavaScript/Node.js"),
        ("Cargo.toml", "Rust"), ("go.mod", "Go"), ("requirements.txt", "Python"),
    ]
    detected = [lang for fn, lang in tech_detectors if os.path.exists(os.path.join(cwd, fn))]
    if detected:
        stack_str = ", ".join(set(detected))
        memories.append(_make_memory(
            content=f"[{name}] Tech stack: {stack_str}",
            title=f"[{name}] Tech stack: {stack_str}",
            tags=["tech-stack", name] + [s.lower().split("/")[0] for s in detected],
        ))

    # Dependencies
    for cfg, parser in [("pyproject.toml", lambda c: re.findall(r'"([a-zA-Z][a-zA-Z0-9_-]+)', c)),
                         ("package.json", lambda c: list(json.loads(c).get("dependencies", {}).keys()) +
                          list(json.loads(c).get("devDependencies", {}).keys()))]:
        cfg_path = os.path.join(cwd, cfg)
        if os.path.exists(cfg_path):
            try:
                deps = [d for d in set(parser(open(cfg_path).read())) if len(d) > 2][:15]
                if deps:
                    memories.append(_make_memory(
                        content=f"[{name}] Key dependencies: {', '.join(sorted(deps))}",
                        title=f"[{name}] Project dependencies",
                        tags=["dependencies", name],
                    ))
            except (OSError, json.JSONDecodeError):
                pass

    # Directory structure
    try:
        top_dirs = [e for e in sorted(os.listdir(cwd))
                    if os.path.isdir(os.path.join(cwd, e)) and not e.startswith(".")
                    and e not in ("node_modules", "__pycache__", "venv", ".venv", "dist", "build", ".git")]
        if top_dirs:
            memories.append(_make_memory(
                content=f"[{name}] Project structure: {', '.join(top_dirs[:15])}",
                title=f"[{name}] Directory structure",
                tags=["architecture", "structure", name],
            ))
    except OSError:
        pass

    # Git history
    try:
        import subprocess
        result = subprocess.run(["git", "log", "--oneline", "-10", "--no-decorate"],
                                capture_output=True, text=True, cwd=cwd, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            memories.append(_make_memory(
                content=f"[{name}] Recent git history:\n{result.stdout.strip()}",
                title=f"[{name}] Recent development activity",
                tags=["git", "history", name],
            ))
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Test framework
    if os.path.exists(os.path.join(cwd, "pyproject.toml")):
        try:
            if "pytest" in open(os.path.join(cwd, "pyproject.toml")).read():
                memories.append(_make_memory(
                    content=f"[{name}] Test framework: pytest",
                    title=f"[{name}] Uses pytest for testing",
                    memory_type="convention", tags=["testing", "pytest", name],
                ))
        except OSError:
            pass

    for mem in memories:
        _save_memory(mem)
    return memories


def _auto_seed_workspace(scope_id: str) -> list[dict]:
    """Scan workspace projects and load starter packs."""
    if _was_seeded(scope_id):
        return []

    projects = _detect_workspace_projects()
    if not projects:
        return []

    all_packs = set()
    all_seeded = []
    for project_dir in projects:
        all_packs.update(detect_packs(project_dir))
        all_seeded.extend(_auto_seed_project(scope_id, project_dir))

    # Starter packs — once across all projects
    for title, content, mem_type, tags in get_pack_memories(list(all_packs)):
        mem = _make_memory(content=content, title=title, memory_type=mem_type,
                           scope_id=scope_id, tags=tags + ["starter-pack"],
                           source_type="starter_pack", confidence=0.7,
                           promotion_status="learned", verified=True)
        _save_memory(mem)
        all_seeded.append(mem)

    if all_seeded:
        _mark_seeded(scope_id)
    return all_seeded


# ─── Retrieval ────────────────────────────────────────────────────

def _search_memories(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """Search memories using ChromaDB native vector similarity."""
    try:
        kwargs = {"query_texts": [query], "n_results": limit, "include": ["documents", "metadatas", "distances"]}
        if scope_id:
            kwargs["where"] = {"scope_id": scope_id}
        result = _collection.query(**kwargs)
        if not result["ids"] or not result["ids"][0]:
            return []
        flat = {"ids": result["ids"][0], "documents": result["documents"][0], "metadatas": result["metadatas"][0]}
        return [_reconstruct_memory(flat, i) for i in range(len(flat["ids"]))]
    except Exception:
        return []


# ─── MCP Tools ────────────────────────────────────────────────────

mcp = FastMCP("cortex")


def _format_memory_as_bullet(mem: dict) -> str:
    """Format a single memory as a markdown bullet for LLM consumption."""
    title = mem.get("title", "Untitled")
    essence = mem.get("essence", "")
    full_record = mem.get("full_record")

    line = f"- **{title}**"
    if essence and essence[:60] != title[:60]:
        line += f" — {essence[:300]}"

    if full_record and full_record != essence:
        line += f"\n  > {full_record[:500]}"

    return line


@mcp.tool()
def memory_recall(query: str, scope_id: str = "default", limit: int = 10) -> str:
    """Search all memory sources for a query. Returns structured markdown.

    Pipeline: ChromaDB vector search -> raw transcript search -> format as markdown."""

    memories = _search_memories(query, scope_id=scope_id, limit=limit)
    transcript_results = transcript_search(query, limit=3)

    if not memories and ("No matching" in transcript_results or not transcript_results):
        return f"No memories found for: {query}"

    sections = []

    if memories:
        by_source = {}
        for m in memories:
            by_source.setdefault(m.get("source_type", "other"), []).append(m)

        lines = []
        for label, key in [("From past sessions", "mined"), ("Project knowledge", "auto_seed"),
                           ("Saved knowledge", "user"), ("Best practices", "starter_pack"),
                           ("Imported", "import")]:
            mems = by_source.get(key, [])
            if mems:
                lines.append(f"### {label}")
                for m in mems:
                    lines.append(_format_memory_as_bullet(m))
                lines.append("")
        sections.append("\n".join(lines))

    if transcript_results and "No matching" not in transcript_results:
        sections.append(f"### Related Session Logs\n\n{transcript_results}")

    # Update retrieval counts (skip Obsidian rewrite — just metadata update)
    for mem in memories:
        mem["retrieval_count"] = mem.get("retrieval_count", 0) + 1
        mem["last_retrieved_at"] = _now()
        _save_memory(mem, skip_obsidian=True)

    return "\n".join(sections) if sections else f"No memories found for: {query}"


@mcp.tool()
def memory_save(content: str, title: str = "", memory_type: str = "lesson",
                scope_id: str = "default", tags: str = "") -> str:
    """Save a piece of knowledge to memory. Tags should be comma-separated."""
    domain_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    effective_title = title or content[:60]

    existing = _is_duplicate(content, scope_id=scope_id, return_match=True)
    if existing:
        return f"Memory already exists (similar to: \"{existing.get('title', '')[:60]}\"). Not saved."

    mem = _make_memory(content=content, title=effective_title, memory_type=memory_type,
                       scope_id=scope_id, tags=domain_tags, source_type="user", confidence=0.5)
    _save_memory(mem)

    return f"Memory saved: {mem['id'][:8]}... \"{effective_title}\""


@mcp.tool()
def memory_list(scope_id: str = "default") -> str:
    """List all memories with stats."""
    memories = _all_memories(scope_id)
    if not memories:
        return f"No memories in scope: {scope_id}"

    memories.sort(key=lambda m: float(m.get("confidence", 0)), reverse=True)

    from collections import Counter
    sources = Counter(m.get("source_type", "?") for m in memories)
    learned = sum(1 for m in memories if m.get("promotion_status") == "learned")
    total_retrievals = sum(m.get("retrieval_count", 0) for m in memories)

    lines = [
        f"**{len(memories)} memories** ({learned} learned) | {total_retrievals} total retrievals",
        f"Sources: {', '.join(f'{k}:{v}' for k, v in sources.most_common())}",
        "",
    ]
    for mem in memories:
        conf = float(mem.get("confidence", 0.5))
        ret = mem.get("retrieval_count", 0)
        mid = mem.get("id", "")[:8]
        title = mem.get("title", "Untitled")[:50]
        lines.append(f"- [{mid}] {title} | conf:{conf:.1f} | ret:{ret}x")

    return "\n".join(lines)


@mcp.tool()
def memory_feedback(memory_ids: str, approved: bool, promote: bool = False) -> str:
    """Report whether recalled memories were useful.
    memory_ids: comma-separated IDs (8+ chars). promote=true forces learned status."""
    ids = [mid.strip() for mid in memory_ids.split(",") if mid.strip()]
    updated = 0

    for target_id in ids:
        mem = _find_memory(target_id)
        if not mem:
            continue

        if promote:
            mem["promotion_status"] = "learned"
            mem["confidence"] = max(float(mem.get("confidence", 0.5)), 0.7)
        elif approved:
            mem["confidence"] = min(1.0, float(mem.get("confidence", 0.5)) + 0.05)
            mem["success_count"] = mem.get("success_count", 0) + 1
        else:
            mem["confidence"] = max(0.0, float(mem.get("confidence", 0.5)) - 0.1)
            mem["failure_count"] = mem.get("failure_count", 0) + 1

        s = mem.get("success_count", 0)
        f = mem.get("failure_count", 0)
        mem["impact_score"] = s / max(1, s + f)
        mem["last_validated_at"] = _now()

        # Auto-promote
        if (mem.get("promotion_status") == "candidate"
                and mem.get("retrieval_count", 0) >= 5
                and mem.get("impact_score", 0) > 0.7):
            mem["promotion_status"] = "learned"

        _save_memory(mem)
        updated += 1

    return f"Updated {updated} memories (approved={approved}, promote={promote})"


@mcp.tool()
def memory_import(source_path: str, scope_id: str = "default") -> str:
    """Import knowledge from files or directories into memory.
    For chat exports: returns content for Claude to extract knowledge from.
    For other files: imports directly (markdown by heading, JSON by key, text as-is)."""

    source = Path(source_path).expanduser().resolve()
    if not str(source).startswith(str(Path.home().resolve())):
        return "Access denied: can only import from within home directory"
    if not source.exists():
        return f"Path not found: {source_path}"

    # Single file
    if source.is_file():
        try:
            content = source.read_text(errors="ignore")
        except OSError:
            return f"Cannot read: {source_path}"

        # Chat export → return for Claude to extract
        ext = source.suffix.lower()
        if (source.name.lower() == "conversations.json"
                or (ext == ".json" and ('"mapping"' in content[:1000] or '"role"' in content[:1000]))
                or (ext in (".txt", ".log") and re.search(r'(?:User|Human|Assistant|Claude)\s*:', content[:500], re.IGNORECASE))):
            preview = content[:10000]
            return (f"CHAT EXPORT DETECTED: {source_path}\nSize: {len(content)} chars\n\n"
                    f"Read through this and extract knowledge. For each insight, call memory_save.\n\n"
                    f"--- CONTENT ---\n{preview}"
                    + ("\n\n[truncated...]" if len(content) > 10000 else ""))

        # Direct import
        return f"Imported {_import_file(source, scope_id)} memories from {source_path}"

    # Directory
    if source.is_dir():
        imported = 0
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv")]
            for fname in sorted(files):
                if not fname.startswith("."):
                    imported += _import_file(Path(root) / fname, scope_id)
        return f"Imported {imported} memories from {source_path}"

    return f"Unsupported path: {source_path}"


def _import_file(fpath: Path, scope_id: str) -> int:
    """Import a single file into memory."""
    try:
        content = fpath.read_text(errors="ignore")
    except OSError:
        return 0
    if not content.strip() or len(content.strip()) < 20:
        return 0

    ext = fpath.suffix.lower()
    count = 0

    if ext in (".md", ".markdown"):
        # Split by headings
        sections = re.split(r'^(#{1,3}\s+.+)$', content, flags=re.MULTILINE)
        current_title = fpath.stem
        current_content = ""
        for part in sections:
            part = part.strip()
            if not part:
                continue
            if re.match(r'^#{1,3}\s+', part):
                if current_content.strip() and len(current_content.strip()) > 20:
                    _save_memory(_make_memory(content=current_content.strip()[:2000], title=current_title,
                                              scope_id=scope_id, tags=["imported"], source_type="import"))
                    count += 1
                current_title = part.lstrip("# ").strip()
                current_content = ""
            else:
                current_content += part + "\n"
        if current_content.strip() and len(current_content.strip()) > 20:
            _save_memory(_make_memory(content=current_content.strip()[:2000], title=current_title,
                                      scope_id=scope_id, tags=["imported"], source_type="import"))
            count += 1
    elif ext == ".json":
        try:
            data = json.loads(content)
            items = data.items() if isinstance(data, dict) else enumerate(data[:50]) if isinstance(data, list) else []
            for key, value in items:
                text = f"{key}: {json.dumps(value)}" if not isinstance(value, str) else f"{key}: {value}"
                if len(text) > 20:
                    _save_memory(_make_memory(content=text[:2000], title=str(key)[:60],
                                              scope_id=scope_id, tags=["imported"], source_type="import"))
                    count += 1
        except json.JSONDecodeError:
            pass
    elif ext in (".txt", ".text", ".rst"):
        _save_memory(_make_memory(content=content.strip()[:2000], title=fpath.stem,
                                  scope_id=scope_id, tags=["imported"], source_type="import"))
        count = 1

    return count


# ─── Transcript Search ───────────────────────────────────────────

@mcp.tool()
def transcript_search(query: str, limit: int = 5) -> str:
    """Search raw Claude Code JSONL session files still on disk."""
    base_dir = Path.home() / ".claude" / "projects"
    if not base_dir.exists():
        return "No matching transcripts found"

    query_words = set(query.lower().split())
    if not query_words:
        return "No matching transcripts found"

    scored = []
    for jsonl_path in base_dir.rglob("*.jsonl"):
        if "/subagents/" in str(jsonl_path):
            continue
        # Skip tiny sessions (likely Haiku expansion calls)
        try:
            if jsonl_path.stat().st_size < 5000:
                continue
            pairs = _parse_jsonl_session(str(jsonl_path))
        except Exception:
            continue
        for pair in pairs:
            text_words = set((pair["user_text"] + " " + pair["assistant_text"]).lower().split())
            score = len(query_words & text_words) / len(query_words)
            if score > 0:
                scored.append((score, pair, jsonl_path.name))

    if not scored:
        return "No matching transcripts found"

    scored.sort(key=lambda x: x[0], reverse=True)
    lines = []
    for score, pair, filename in scored[:limit]:
        lines.append(f"- **Q:** {pair['user_text'][:300]}\n"
                     f"  **A:** {pair['assistant_text'][:500]}\n"
                     f"  *Session: {filename}*")

    return "\n\n".join(lines)


# ─── Session Mining ──────────────────────────────────────────────

MINED_SESSIONS_FILE = CORTEX_DIR / ".mined_sessions"

_HAIKU_MINE_SYSTEM = (
    "You are a knowledge extractor for a coding project memory system. "
    "Read conversation exchanges and extract anything worth remembering. "
    "Output structured memories in this exact format, one per block:\n\n"
    "MEMORY\n"
    "title: short descriptive title (no project prefix)\n"
    "who: project-name (or general if unclear)\n"
    "why: decided|learned|shipped|failed|convention|prefers|discovered|designed|blocked-by\n"
    "content: one paragraph summary of what to remember\n\n"
    "Rules:\n"
    "- Extract decisions, lessons, things shipped, failures, conventions, preferences, discoveries\n"
    "- Be generous — if in doubt, extract it\n"
    "- Each memory should be self-contained and useful without the original conversation\n"
    "- Skip trivial exchanges (greetings, simple file reads, routine git commands)\n"
    "- If nothing worth saving, output NONE\n"
    "- Do NOT add knowledge you weren't told — only extract what's in the conversation"
)


def _was_mined(session_id: str) -> bool:
    if not MINED_SESSIONS_FILE.exists():
        return False
    for line in MINED_SESSIONS_FILE.read_text().splitlines():
        if line.split("\t")[0].strip() == session_id:
            return True
    return False


def _mark_mined(session_id: str):
    with open(MINED_SESSIONS_FILE, "a") as f:
        f.write(f"{session_id}\t{_now()}\n")


def _extract_text_from_content(content, role: str) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _parse_jsonl_session(jsonl_path: str) -> list[dict]:
    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue
            text = _extract_text_from_content(obj.get("message", {}).get("content", ""), msg_type)
            if text:
                messages.append({"role": msg_type, "text": text})

    pairs = []
    i = 0
    while i < len(messages):
        if messages[i]["role"] == "user":
            if i + 1 < len(messages) and messages[i + 1]["role"] == "assistant":
                pairs.append({"user_text": messages[i]["text"], "assistant_text": messages[i + 1]["text"]})
                i += 2
            else:
                i += 1
        else:
            i += 1
    return pairs


def _extract_insights_haiku(pairs: list[dict], batch_size: int = 5) -> list[dict]:
    """Use Haiku to extract structured memories from conversation exchanges."""
    import subprocess as _sp

    _WHY_TO_TYPE = {
        "decided": "knowledge", "learned": "lesson", "shipped": "knowledge",
        "failed": "failure", "convention": "convention", "prefers": "preference",
        "discovered": "knowledge", "designed": "knowledge", "blocked-by": "failure",
    }

    all_insights = []

    # Process in batches to stay within token limits
    for i in range(0, len(pairs), batch_size):
        batch = pairs[i:i + batch_size]

        # Format exchanges for Haiku
        exchanges = []
        for j, pair in enumerate(batch):
            user = pair["user_text"][:1000]
            asst = pair["assistant_text"][:2000]
            exchanges.append(f"--- Exchange {j+1} ---\nUser: {user}\nAssistant: {asst}")
        prompt = "\n\n".join(exchanges)

        try:
            result = _sp.run(
                ["claude", "-p", "--model", "haiku", "--tools", "",
                 "--system-prompt", _HAIKU_MINE_SYSTEM],
                input=prompt,
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue

            # Parse Haiku output into structured memories
            output = result.stdout.strip()
            if output == "NONE":
                continue

            for block in output.split("MEMORY"):
                block = block.strip()
                if not block:
                    continue
                mem = {}
                for line in block.split("\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        key = key.strip().lower()
                        val = val.strip()
                        if key in ("title", "who", "why", "content"):
                            mem[key] = val
                if mem.get("content") and mem.get("title"):
                    why = mem.get("why", "discovered")
                    if why not in _WHY_TO_TYPE:
                        why = "discovered"
                    all_insights.append({
                        "title": mem["title"][:120],
                        "content": mem["content"][:2000],
                        "who": mem.get("who", "general"),
                        "why": why,
                        "memory_type": _WHY_TO_TYPE[why],
                    })
        except Exception:
            continue

    return all_insights


def mine_session(jsonl_path: str) -> dict:
    path = Path(jsonl_path)
    if not path.exists():
        return {"skipped": True, "reason": "file not found"}

    session_id = path.stem
    if _was_mined(session_id):
        return {"skipped": True, "reason": "already mined"}

    pairs = _parse_jsonl_session(jsonl_path)
    if not pairs:
        _mark_mined(session_id)
        return {"session_id": session_id, "chunks_processed": 0,
                "memories_saved": 0, "duplicates_skipped": 0, "skipped": False}

    # Use Haiku to extract structured insights
    insights = _extract_insights_haiku(pairs)
    memories_saved = 0
    duplicates_skipped = 0

    for ins in insights:
        if _is_duplicate(ins["content"][:2000]):
            duplicates_skipped += 1
            continue
        # Use who as project tag
        tags = ["mined", session_id[:8]]
        if ins["who"] != "general":
            tags.append(ins["who"])
        mem = _make_memory(
            content=ins["content"][:2000],
            title=ins["title"],
            memory_type=ins["memory_type"],
            source_type="mined",
            confidence=0.5,
            tags=tags,
            source_session_id=session_id,
        )
        _save_memory(mem)
        memories_saved += 1

    _mark_mined(session_id)
    return {"session_id": session_id, "chunks_processed": len(pairs),
            "memories_saved": memories_saved, "duplicates_skipped": duplicates_skipped, "skipped": False}


def mine_all() -> dict:
    base_dir = Path.home() / ".claude" / "projects"
    total, newly_mined, already_mined, total_memories = 0, 0, 0, 0

    if base_dir.exists():
        for path in base_dir.rglob("*.jsonl"):
            if "/subagents/" in str(path):
                continue
            total += 1
            result = mine_session(str(path))
            if result.get("skipped"):
                already_mined += 1
            else:
                newly_mined += 1
                total_memories += result.get("memories_saved", 0)

    return {"total_sessions": total, "newly_mined": newly_mined,
            "already_mined": already_mined, "total_memories_saved": total_memories}


# ─── Garbage Collection ──────────────────────────────────────────

def garbage_collect() -> dict:
    decayed = merged = pruned = 0
    deleted_ids: set[str] = set()
    thirty_days_ago = datetime.now(timezone.utc).timestamp() - 30 * 86400

    # Decay: low confidence + never retrieved + old
    for mem in _all_memories():
        if float(mem.get("confidence", 0.5)) < 0.3 and int(mem.get("retrieval_count", 0)) == 0:
            try:
                if datetime.fromisoformat(mem.get("created_at", "")).timestamp() < thirty_days_ago:
                    if _delete_memory(mem["id"]):
                        deleted_ids.add(mem["id"])
                        decayed += 1
            except (ValueError, TypeError):
                pass

    # Merge: find near-duplicates via ChromaDB similarity search
    remaining = [m for m in _all_memories() if m["id"] not in deleted_ids]
    for mem in remaining:
        if mem["id"] in deleted_ids:
            continue
        try:
            result = _collection.query(query_texts=[mem.get("essence", "")], n_results=2,
                                       include=["documents", "metadatas", "distances"])
            if not result["ids"] or len(result["ids"][0]) < 2:
                continue
            # First result is self, second is nearest neighbor
            neighbor_id = result["ids"][0][1]
            distance = result["distances"][0][1]
            if distance < 0.1 and neighbor_id not in deleted_ids:  # very similar
                neighbor = _load_memory(neighbor_id)
                if not neighbor:
                    continue
                # Keep higher confidence
                if float(mem.get("confidence", 0)) >= float(neighbor.get("confidence", 0)):
                    loser_id = neighbor_id
                else:
                    loser_id = mem["id"]
                if _delete_memory(loser_id):
                    deleted_ids.add(loser_id)
                    merged += 1
        except Exception:
            continue

    # Prune: cap at 500
    remaining = [m for m in _all_memories() if m["id"] not in deleted_ids]
    if len(remaining) > 500:
        pruneable = sorted(
            [m for m in remaining if m.get("promotion_status") != "learned"],
            key=lambda m: float(m.get("confidence", 0.5)) * 0.5 + float(m.get("impact_score", 0)) * 0.3 +
                          min(int(m.get("retrieval_count", 0)) / 10, 0.2))
        for mem in pruneable[:len(remaining) - 500]:
            if _delete_memory(mem["id"]):
                deleted_ids.add(mem["id"])
                pruned += 1

    # Clean up old retrieval logs (>30 days)
    logs_cleaned = 0
    for log_file in LOGS_DIR.glob("retrieval-*.json"):
        try:
            if log_file.stat().st_mtime < thirty_days_ago:
                log_file.unlink()
                logs_cleaned += 1
        except OSError:
            pass

    return {"decayed": decayed, "merged": merged, "pruned": pruned,
            "logs_cleaned": logs_cleaned, "remaining": len(_all_memories())}


# ─── Index Generation ────────────────────────────────────────────

INDEX_PATH = OBSIDIAN_VAULT / "cortex" / "_index.md"


def _map_memory_type_to_why(memory_type: str, source_type: str, essence: str) -> str:
    """Map old memory_type to new why: verb based on schema spec section 5."""
    if source_type == "auto_seed":
        return "discovered"

    e = (essence or "").lower()

    if memory_type == "lesson":
        return "learned"
    elif memory_type == "knowledge":
        if any(w in e for w in ("decided", "chose", "went with")):
            return "decided"
        elif any(w in e for w in ("shipped", "added", "built")):
            return "shipped"
        elif any(w in e for w in ("found", "discovered")):
            return "discovered"
        elif any(w in e for w in ("designed", "architecture")):
            return "designed"
        else:
            return "discovered"
    elif memory_type == "convention":
        return "convention"
    elif memory_type == "preference":
        return "prefers"
    elif memory_type == "failure":
        if any(w in e for w in ("blocked", "can't", "doesn't work")):
            return "blocked-by"
        else:
            return "failed"
    else:
        return "discovered"


def _extract_project(mem: dict) -> str:
    """Extract project name from memory title prefix or domain_tags."""
    title = mem.get("title") or ""
    m = re.match(r'^\[([^\]]+)\]', title)
    if m:
        return m.group(1)

    domain_tags = mem.get("domain_tags") or []
    known_projects = {"cortex-plugin", "substrate", "forge", "vibereader", "obsidian-brain"}
    for tag in domain_tags:
        if tag in known_projects:
            return tag

    return "general"


def _format_index_line(mem: dict) -> str:
    """Format one memory as an index line: - [MM-DD] verb: description | #tag1 #tag2"""
    created_at = mem.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%m-%d")
    except Exception:
        date_str = "00-00"

    memory_type = mem.get("memory_type") or "knowledge"
    source_type = mem.get("source_type") or ""
    essence = mem.get("essence") or ""
    verb = _map_memory_type_to_why(memory_type, source_type, essence)

    title = mem.get("title") or essence[:60]
    # Strip [project-name] prefix and ensure single-line
    description = re.sub(r'^\[[^\]]+\]\s*', '', title)
    description = description.split("\n")[0].strip()[:120]

    domain_tags = mem.get("domain_tags") or []
    tags_str = " ".join(f"#{t}" for t in domain_tags if t)

    mem_id = mem.get("id", "")[:8]
    line = f"- [{date_str}] {verb}: {description} ({mem_id})"
    if tags_str:
        line += f" | {tags_str}"
    return line


def _generate_index(scope_id: str = "default") -> str:
    """Generate the full _index.md from all ChromaDB memories."""
    memories = _all_memories(scope_id)

    # Group by project
    projects: dict[str, list[dict]] = {}
    for mem in memories:
        project = _extract_project(mem)
        projects.setdefault(project, []).append(mem)

    # Sort projects alphabetically, general last
    sorted_projects = sorted(p for p in projects if p != "general")
    if "general" in projects:
        sorted_projects.append("general")

    # Build markdown
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = len(memories)
    lines = [
        "# Cortex Memory Index",
        f"Updated: {today} | Total: {total} memories",
    ]

    for project in sorted_projects:
        mems = projects[project]
        # Sort newest first
        def _date_key(m):
            try:
                return datetime.fromisoformat((m.get("created_at") or "").replace("Z", "+00:00"))
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        mems_sorted = sorted(mems, key=_date_key, reverse=True)
        lines.append("")
        lines.append(f"## {project} ({len(mems_sorted)} memories)")
        for mem in mems_sorted:
            lines.append(_format_index_line(mem))

    content = "\n".join(lines) + "\n"

    # Write to obsidian vault
    if OBSIDIAN_VAULT.exists():
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(content)

    return content


def _append_or_update_index_line(mem: dict):
    """Append or update a memory's line in _index.md. No-op if index doesn't exist."""
    if not INDEX_PATH.exists():
        return
    content = INDEX_PATH.read_text()
    mem_id = mem["id"][:8]
    line = _format_index_line(mem)
    project = _extract_project(mem)

    lines = content.split("\n")
    # Remove existing line for this memory if present (match by id fragment)
    new_lines = [l for l in lines if not (l.startswith("- [") and mem_id in l)]

    # Find the right project section to append to
    section_header = f"## {project} ("
    inserted = False
    for i, l in enumerate(new_lines):
        if l.startswith(section_header):
            new_lines.insert(i + 1, line)
            inserted = True
            break

    if not inserted:
        # Project section doesn't exist — add it before "## general" or at end
        general_idx = None
        for i, l in enumerate(new_lines):
            if l.startswith("## general"):
                general_idx = i
                break
        new_section = ["", f"## {project} (1 memories)", line]
        if general_idx is not None:
            for s in reversed(new_section):
                new_lines.insert(general_idx, s)
        else:
            new_lines.extend(new_section)

    # Recount total and update header line
    total = sum(1 for l in new_lines if l.startswith("- ["))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i, l in enumerate(new_lines):
        if l.startswith("Updated:"):
            new_lines[i] = f"Updated: {today} | Total: {total} memories"
            break

    INDEX_PATH.write_text("\n".join(new_lines))


def _remove_index_line(memory_id: str):
    """Remove a memory's line from _index.md. No-op if index doesn't exist."""
    if not INDEX_PATH.exists():
        return
    mem_id = memory_id[:8]
    content = INDEX_PATH.read_text()
    lines = content.split("\n")
    new_lines = [l for l in lines if not (l.startswith("- [") and mem_id in l)]
    # Update total count
    total = sum(1 for l in new_lines if l.startswith("- ["))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i, l in enumerate(new_lines):
        if l.startswith("Updated:"):
            new_lines[i] = f"Updated: {today} | Total: {total} memories"
            break
    INDEX_PATH.write_text("\n".join(new_lines))


def _migrate_memory_frontmatter(mem: dict) -> bool:
    """Rewrite an existing Obsidian memory file with the new frontmatter schema (who, why fields)."""
    obsidian_file = mem.get("obsidian_file")
    if not obsidian_file:
        return False
    filepath = OBSIDIAN_MEMORIES_DIR / obsidian_file
    if not filepath.exists():
        return False

    existing = filepath.read_text()

    # Extract body: everything after the closing --- of old frontmatter
    # If file starts with ---, find the second --- and take content after it
    if existing.startswith("---"):
        # Find closing ---
        end_idx = existing.find("---", 3)
        if end_idx != -1:
            body = existing[end_idx + 3:]
        else:
            # Malformed frontmatter — treat entire file as body
            body = existing
    else:
        # No frontmatter — treat entire file as body
        body = existing

    # Compute new frontmatter fields
    who = _extract_project(mem)
    why = _map_memory_type_to_why(
        mem.get("memory_type", "knowledge"),
        mem.get("source_type", ""),
        mem.get("essence", "")
    )

    # Strip [project] prefix from title
    raw_title = mem.get("title") or mem.get("essence", "")[:60]
    title = re.sub(r'^\[[^\]]+\]\s*', '', raw_title)

    tags = mem.get("domain_tags") or []
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t]

    confidence = mem.get("confidence", 0.8)

    created_at = mem.get("created_at") or ""
    created_date = created_at[:10] if created_at else ""

    tags_str = ", ".join(tags)

    new_frontmatter = f"""---
title: {title}
who: {who}
why: {why}
tags: [{tags_str}]
confidence: {confidence}
created: {created_date}
related: []
---"""

    filepath.write_text(new_frontmatter + body)
    return True


# ─── Smart Recall (LLM-based index scanning) ────────────────────

def _load_obsidian_memories(picked_ids: list[str]) -> list[dict]:
    """Load memory content directly from Obsidian files by ID prefix."""
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return []

    # Build a map of id_prefix → file path
    file_map = {}
    for f in OBSIDIAN_MEMORIES_DIR.glob("*.md"):
        # Filename format: {slug}-{id[:8]}.md
        name = f.stem  # e.g. "cortex-unified-recall-abc12345"
        if len(name) >= 8:
            id_prefix = name[-8:]
            file_map[id_prefix] = f

    results = []
    for pid in picked_ids:
        filepath = file_map.get(pid)
        if not filepath or not filepath.exists():
            continue
        try:
            content = filepath.read_text()
            # Parse frontmatter
            meta = {"title": "Untitled", "who": "general", "why": "discovered", "body": ""}
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = parts[1].strip()
                    body = parts[2].strip()
                    for line in fm.split("\n"):
                        if ":" in line:
                            key, val = line.split(":", 1)
                            key = key.strip()
                            val = val.strip()
                            if key in ("title", "who", "why"):
                                meta[key] = val
                    meta["body"] = body
            else:
                meta["body"] = content.strip()
            results.append(meta)
        except OSError:
            continue
    return results


def _smart_recall(prompt: str, scope_id: str = "default") -> str:
    """Haiku scans _index.md, picks generously, loads full content.
    Falls back to memory_recall if index doesn't exist or Haiku fails."""
    import subprocess as _sp

    # Read index
    if not INDEX_PATH.exists():
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    index_content = INDEX_PATH.read_text()
    if not index_content.strip():
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    # Haiku picks relevant memories from index
    system_prompt = (
        "You are a memory selector. Output ONLY 8-character memory IDs, one per line, nothing else."
    )
    user_prompt = (
        f"USER MESSAGE:\n{prompt}\n\n"
        f"MEMORY INDEX:\n{index_content}\n\n"
        "Select any memories that could be relevant to the user's message. "
        "Be generous — include anything that might help, even loosely related. "
        "Let the user's intent guide you. Output ONLY the 8-char IDs from parentheses, one per line."
    )

    picked_ids = []
    try:
        result = _sp.run(
            ["claude", "-p", "--model", "haiku", "--tools", "",
             "--system-prompt", system_prompt],
            input=user_prompt,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Extract 8-char hex IDs from output
            for line in result.stdout.strip().split("\n"):
                line = line.strip().strip("-").strip()
                # Match 8-char hex ID
                match = re.search(r'\b([0-9a-f]{8})\b', line)
                if match:
                    picked_ids.append(match.group(1))
    except Exception:
        pass

    if not picked_ids:
        # Fallback to old recall
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    # Load full content from Obsidian files (not ChromaDB)
    picked_files = _load_obsidian_memories(picked_ids)

    if not picked_files:
        return memory_recall(prompt, scope_id=scope_id, limit=10)

    # Format output grouped by project
    by_project = {}
    for mem in picked_files:
        by_project.setdefault(mem["who"], []).append(mem)

    sections = []
    sorted_projects = sorted(p for p in by_project if p != "general")
    if "general" in by_project:
        sorted_projects.append("general")

    for project in sorted_projects:
        mems = by_project[project]
        lines = [f"### {project}"]
        for m in mems:
            entry = f"- **{m['title']}** ({m['why']})"
            if m["body"]:
                entry += f"\n  {m['body'][:500]}"
            lines.append(entry)
        sections.append("\n".join(lines))

    # Also include transcript search
    transcript_results = transcript_search(prompt, limit=3)
    if transcript_results and "No matching" not in transcript_results:
        sections.append(f"### Related Session Logs\n\n{transcript_results}")

    return "\n\n".join(sections) if sections else f"No memories found for: {prompt}"


# ─── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys

    cmd = _sys.argv[1] if len(_sys.argv) >= 2 else None

    if cmd == "--recall-smart":
        query = " ".join(_sys.argv[2:]) if len(_sys.argv) >= 3 else ""
        if query:
            print(_smart_recall(query))
        else:
            print("No query provided.")
    elif cmd == "--recall":
        query = " ".join(_sys.argv[2:]) if len(_sys.argv) >= 3 else ""
        if query:
            result = memory_recall(query, limit=10)
            print(result)
        else:
            print("No query provided.")
    elif cmd == "--mine-session" and len(_sys.argv) >= 3:
        print(json.dumps(mine_session(_sys.argv[2])))
    elif cmd == "--mine-all":
        print(json.dumps(mine_all()))
    elif cmd == "--gc":
        print(json.dumps(garbage_collect()))
    elif cmd == "--purge-mined":
        deleted = 0
        try:
            result = _collection.get(where={"source_type": "mined"}, include=["metadatas"])
            ids = result["ids"]
            for mid, meta in zip(ids, result["metadatas"]):
                obsidian_file = meta.get("obsidian_file", "")
                if obsidian_file:
                    obsidian_path = OBSIDIAN_MEMORIES_DIR / obsidian_file
                    if obsidian_path.exists():
                        obsidian_path.unlink()
            if ids:
                _collection.delete(ids=ids)
                deleted = len(ids)
        except Exception:
            pass
        if MINED_SESSIONS_FILE.exists():
            MINED_SESSIONS_FILE.write_text("")
        print(json.dumps({"deleted": deleted}))
    elif cmd == "--migrate":
        migrated = 0
        errors = 0
        json_dir = MEMORIES_DIR
        if json_dir.exists():
            for f in json_dir.glob("*.json"):
                try:
                    mem = json.loads(f.read_text())
                    _save_memory(mem)  # This now writes to ChromaDB + Obsidian
                    migrated += 1
                except Exception as e:
                    errors += 1
        print(json.dumps({"migrated": migrated, "errors": errors}))
    elif cmd == "--install-cron":
        import subprocess as _sp
        cron_script = str(Path(__file__).resolve().parent / "mine-cron.sh")
        cron_entry = f"0 * * * * bash {cron_script}"
        result = _sp.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
        if "mine-cron.sh" in existing:
            print("Cron already installed.")
        else:
            new_crontab = existing.rstrip("\n") + ("\n" if existing else "") + cron_entry + "\n"
            _sp.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            print(f"Installed: {cron_entry}")
    elif cmd == "--rebuild-index":
        result = _generate_index()
        print(f"Index rebuilt: {INDEX_PATH}")
    elif cmd in ("--miner-start", "--miner-stop", "--miner-status"):
        import subprocess as _sp
        wrapper = str(Path(__file__).resolve().parent / "miner-wrapper.sh")
        action = cmd.replace("--miner-", "")
        _sp.run(["bash", wrapper, action])
    elif cmd == "--migrate-schema":
        memories = _all_memories("default")
        migrated = 0
        skipped = 0
        for mem in memories:
            if _migrate_memory_frontmatter(mem):
                migrated += 1
            else:
                skipped += 1
        # Rebuild index after migration
        _generate_index("default")
        print(json.dumps({"migrated": migrated, "skipped": skipped}))
    else:
        if not _was_seeded("default"):
            _auto_seed_workspace("default")
        mcp.run(transport="stdio")
