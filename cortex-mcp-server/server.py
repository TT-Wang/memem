#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory for Claude Code.

Storage backends: ChromaDB (vector search) + Obsidian markdown vault.

Sections:
  Storage:     ChromaDB + Obsidian dual-write CRUD
  Generation:  _make_memory, domain detection, dedup
  Auto-seed:   Scan workspace projects + load starter packs on first run
  Retrieval:   Vector similarity search via ChromaDB
  MCP Tools:   memory_recall, memory_save, memory_list, memory_import
  Transcript:  Search raw Claude Code JSONL session files
  Mining:      Parse JSONL sessions, extract insights, save as memories
  GC:          Decay stale, merge duplicates, prune excess
  CLI:         --mine-session, --mine-all, --gc, --purge-mined, --migrate, --install-cron
"""

import atexit
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from mcp.server.fastmcp import FastMCP

# ─── Storage ──────────────────────────────────────────────────────

CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
MEMORIES_DIR = CORTEX_DIR / "memories"  # kept for migration
LOGS_DIR = CORTEX_DIR / "logs"
SERVER_PID_FILE = CORTEX_DIR / "mcp-server.pid"
MINER_PID_FILE = CORTEX_DIR / "miner.pid"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

CHROMADB_DIR = CORTEX_DIR / "chromadb"
_chroma_client = chromadb.PersistentClient(path=str(CHROMADB_DIR))
_collection = _chroma_client.get_or_create_collection("memories")

OBSIDIAN_VAULT = Path(os.environ.get("CORTEX_OBSIDIAN_VAULT", str(Path.home() / "obsidian-brain")))
OBSIDIAN_MEMORIES_DIR = OBSIDIAN_VAULT / "cortex" / "memories"
if OBSIDIAN_VAULT.exists():
    OBSIDIAN_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None

    if _pid_is_running(pid):
        return pid

    path.unlink(missing_ok=True)
    return None


def _register_server_pid():
    SERVER_PID_FILE.write_text(str(os.getpid()))

    def _cleanup():
        try:
            if SERVER_PID_FILE.read_text().strip() == str(os.getpid()):
                SERVER_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_cleanup)


def _allowed_writer_pids() -> set[int]:
    raw = os.environ.get("CORTEX_ALLOWED_WRITER_PIDS", "")
    allowed = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowed.add(int(part))
        except ValueError:
            continue
    return allowed


def _require_exclusive_writer(operation: str):
    allowed = _allowed_writer_pids()
    for role, path in (("mcp-server", SERVER_PID_FILE), ("miner", MINER_PID_FILE)):
        pid = _read_pid_file(path)
        if pid and pid not in allowed and pid != os.getpid():
            raise RuntimeError(
                f"Refusing {operation}: active {role} process {pid} is already using {CHROMADB_DIR}"
            )


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
        if k == "domain_tags":
            mem[k] = [x for x in v.split(",") if x] if v else []
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
    """Flatten memory dict to ChromaDB-compatible metadata (str/int/float/bool only).
    Schema fields: title, domain_tags, project, source_type, created_at."""
    meta = {}
    for k, v in mem.items():
        if k in ("id", "essence"):
            continue
        if v is None:
            meta[k] = ""
        elif isinstance(v, list):
            meta[k] = ",".join(str(x) for x in v)
        elif isinstance(v, (str, int, float)):
            meta[k] = v
        else:
            meta[k] = str(v)
    return meta


def _stable_mined_memory_id(session_id: str, title: str, content: str) -> str:
    seed = f"{session_id}\n{title.strip()}\n{content.strip()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


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

    clean_title = re.sub(r'^\[[^\]]+\]\s*', '', mem.get("title", "Untitled"))
    project = mem.get("project", "general")

    frontmatter = f"""---
title: {clean_title}
project: {project}
tags: [{', '.join(tags)}]
created: {mem.get('created_at', '')[:10]}
---"""

    # Body — just the content
    essence = mem.get("essence", "")
    body = f"\n\n{essence}"

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


def _make_memory(content: str, title: str, tags: list[str] | None = None,
                 project: str = "general", source_type: str = "user") -> dict:
    """Create a memory dict — minimal AI-native schema."""
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "essence": content,
        "domain_tags": tags or [],
        "project": project,
        "source_type": source_type,
        "created_at": _now(),
    }


# ─── Retrieval ────────────────────────────────────────────────────

def _search_memories(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """Search memories by scanning Obsidian markdown files with keyword matching."""
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return []

    query_words = set(query.lower().split())
    if not query_words:
        return []

    scored = []
    for md_file in OBSIDIAN_MEMORIES_DIR.glob("*.md"):
        try:
            content = md_file.read_text(errors="ignore")
        except OSError:
            continue

        # Parse frontmatter and body
        title = ""
        tags = []
        project = ""
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                body = parts[2].strip()
                for line in frontmatter.splitlines():
                    if line.startswith("title:"):
                        title = line.split(":", 1)[1].strip()
                    elif line.startswith("tags:"):
                        tags_str = line.split(":", 1)[1].strip().strip("[]")
                        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
                    elif line.startswith("project:"):
                        project = line.split(":", 1)[1].strip()

        # Scope filter
        if scope_id and scope_id != "default" and project and project != scope_id:
            continue

        # Score by keyword overlap across title + tags + body
        text_words = set((title + " " + " ".join(tags) + " " + body).lower().split())
        score = len(query_words & text_words) / len(query_words)
        if score > 0:
            mem_id = md_file.stem.rsplit("-", 1)[-1] if "-" in md_file.stem else md_file.stem
            scored.append((score, {
                "id": mem_id,
                "title": title,
                "essence": body[:300],
                "full_record": body,
                "source_type": "mined" if any("mined" in t for t in tags) else "user",
                "file": str(md_file),
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:limit]]


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

    Pipeline: Obsidian file keyword search -> raw transcript search -> format as markdown."""

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
        for label, key in [("From past sessions", "mined"),
                           ("Saved knowledge", "user"), ("Imported", "import")]:
            mems = by_source.get(key, [])
            if mems:
                lines.append(f"### {label}")
                for m in mems:
                    lines.append(_format_memory_as_bullet(m))
                lines.append("")
        sections.append("\n".join(lines))

    if transcript_results and "No matching" not in transcript_results:
        sections.append(f"### Related Session Logs\n\n{transcript_results}")

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

    mem = _make_memory(content=content, title=effective_title, tags=domain_tags,
                       project=scope_id or "general", source_type="user")
    _save_memory(mem)

    return f"Memory saved: {mem['id'][:8]}... \"{effective_title}\""


@mcp.tool()
def memory_list(scope_id: str = "default") -> str:
    """List all memories with stats."""
    memories = _all_memories(scope_id)
    if not memories:
        return f"No memories in scope: {scope_id}"

    memories.sort(key=lambda m: m.get("created_at", ""), reverse=True)

    from collections import Counter
    sources = Counter(m.get("source_type", "?") for m in memories)

    lines = [
        f"**{len(memories)} memories**",
        f"Sources: {', '.join(f'{k}:{v}' for k, v in sources.most_common())}",
        "",
    ]
    for mem in memories:
        mid = mem.get("id", "")[:8]
        title = mem.get("title", "Untitled")[:50]
        project = mem.get("project", "general")
        lines.append(f"- [{mid}] {title} | project:{project}")

    return "\n".join(lines)



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
                                              tags=["imported"], project=scope_id or "general", source_type="import"))
                    count += 1
                current_title = part.lstrip("# ").strip()
                current_content = ""
            else:
                current_content += part + "\n"
        if current_content.strip() and len(current_content.strip()) > 20:
            _save_memory(_make_memory(content=current_content.strip()[:2000], title=current_title,
                                      tags=["imported"], project=scope_id or "general", source_type="import"))
            count += 1
    elif ext == ".json":
        try:
            data = json.loads(content)
            items = data.items() if isinstance(data, dict) else enumerate(data[:50]) if isinstance(data, list) else []
            for key, value in items:
                text = f"{key}: {json.dumps(value)}" if not isinstance(value, str) else f"{key}: {value}"
                if len(text) > 20:
                    _save_memory(_make_memory(content=text[:2000], title=str(key)[:60],
                                              tags=["imported"], project=scope_id or "general", source_type="import"))
                    count += 1
        except json.JSONDecodeError:
            pass
    elif ext in (".txt", ".text", ".rst"):
        _save_memory(_make_memory(content=content.strip()[:2000], title=fpath.stem,
                                  tags=["imported"], project=scope_id or "general", source_type="import"))
        count = 1

    return count


# ─── Transcript Search ───────────────────────────────────────────

@mcp.tool()
def transcript_search(query: str, limit: int = 5) -> str:
    """Search raw Claude Code JSONL session files still on disk."""
    base_dirs = [Path.home() / ".claude" / "projects"]
    # Also scan additional session dirs (e.g. other users) via CORTEX_EXTRA_SESSION_DIRS env var
    extra = os.environ.get("CORTEX_EXTRA_SESSION_DIRS", "")
    if extra:
        base_dirs.extend(Path(p) for p in extra.split(":") if p)

    query_words = set(query.lower().split())
    if not query_words:
        return "No matching transcripts found"

    scored = []
    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
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
    "- Each memory should be self-contained and useful without the original conversation\n"
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
    """Parse JSONL session into user/assistant exchange pairs.

    Includes all content: text, tool calls, tool results, errors.
    Groups consecutive messages by role, flushes on role switch."""
    user_texts = []
    assistant_texts = []
    pairs = []

    def _flush():
        if user_texts and assistant_texts:
            pairs.append({
                "user_text": "\n".join(user_texts),
                "assistant_text": "\n".join(assistant_texts),
            })
        user_texts.clear()
        assistant_texts.clear()

    def _extract_all_content(content) -> str:
        """Extract all meaningful content from a message, including tool calls/results."""
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                t = block.get("text", "").strip()
                if t:
                    parts.append(t)
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                # Summarize tool call — include key args but truncate large content
                if name in ("Read", "Glob", "Grep"):
                    parts.append(f"[Tool: {name} {inp.get('file_path', inp.get('pattern', ''))}]")
                elif name in ("Edit", "Write"):
                    path = inp.get("file_path", "")
                    parts.append(f"[Tool: {name} {path}]")
                elif name == "Bash":
                    cmd = inp.get("command", "")[:200]
                    parts.append(f"[Tool: Bash] {cmd}")
                else:
                    parts.append(f"[Tool: {name}]")
            elif btype == "tool_result":
                result_content = block.get("content", "")
                error = block.get("is_error", False)
                if error:
                    result_text = result_content if isinstance(result_content, str) else str(result_content)
                    parts.append(f"[Error] {result_text[:500]}")
                elif isinstance(result_content, str) and result_content.strip():
                    parts.append(f"[Result] {result_content[:500]}")
                elif isinstance(result_content, list):
                    for sub in result_content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            t = sub.get("text", "").strip()
                            if t:
                                parts.append(f"[Result] {t[:500]}")
        return "\n".join(parts)

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

            content = obj.get("message", {}).get("content", "")
            text = _extract_all_content(content)

            if not text:
                continue

            # When role switches from assistant→user, flush the previous exchange
            if msg_type == "user" and assistant_texts:
                _flush()

            if msg_type == "user":
                user_texts.append(text)
            else:
                assistant_texts.append(text)

    _flush()  # final exchange
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
        if _is_duplicate(ins["content"][:2000], threshold=0.5):
            duplicates_skipped += 1
            continue
        # Use who as project tag
        tags = ["mined", session_id[:8]]
        if ins["who"] != "general":
            tags.append(ins["who"])
        mem = _make_memory(
            content=ins["content"][:2000],
            title=ins["title"],
            tags=tags,
            project=ins["who"] if ins["who"] != "general" else "general",
            source_type="mined",
        )
        mem["id"] = _stable_mined_memory_id(session_id, ins["title"], ins["content"][:2000])
        _save_memory(mem)
        memories_saved += 1

    _mark_mined(session_id)
    return {"session_id": session_id, "chunks_processed": len(pairs),
            "memories_saved": memories_saved, "duplicates_skipped": duplicates_skipped, "skipped": False}


def mine_all() -> dict:
    base_dirs = [Path.home() / ".claude" / "projects"]
    # Also scan additional session dirs (e.g. other users) via CORTEX_EXTRA_SESSION_DIRS env var
    extra = os.environ.get("CORTEX_EXTRA_SESSION_DIRS", "")
    if extra:
        base_dirs.extend(Path(p) for p in extra.split(":") if p)
    total, newly_mined, already_mined, total_memories = 0, 0, 0, 0

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
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



# ─── Index Generation ────────────────────────────────────────────

INDEX_PATH = OBSIDIAN_VAULT / "cortex" / "_index.md"



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
    """Format one memory as an index line: - Title (id) #tag1 #tag2"""
    title = mem.get("title") or mem.get("essence", "")[:60]
    title = re.sub(r'^\[[^\]]+\]\s*', '', title)
    title = title.split("\n")[0].strip()[:120]

    domain_tags = mem.get("domain_tags") or []
    tags_str = " ".join(f"#{t}" for t in domain_tags if t)

    mem_id = mem.get("id", "")[:8]
    line = f"- {title} ({mem_id})"
    if tags_str:
        line += f" {tags_str}"
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
            meta = {"title": "Untitled", "project": "general", "why": "discovered", "body": ""}
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
                            if key in ("title", "project", "why"):
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
        by_project.setdefault(mem["project"], []).append(mem)

    sections = []
    sorted_projects = sorted(p for p in by_project if p != "general")
    if "general" in by_project:
        sorted_projects.append("general")

    for proj in sorted_projects:
        mems = by_project[proj]
        lines = [f"### {proj}"]
        for m in mems:
            entry = f"- **{m['title']}**"
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
        _require_exclusive_writer("mine session")
        print(json.dumps(mine_session(_sys.argv[2])))
    elif cmd == "--mine-all":
        _require_exclusive_writer("mine all sessions")
        print(json.dumps(mine_all()))
    elif cmd == "--purge-mined":
        _require_exclusive_writer("purge mined memories")
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
        _require_exclusive_writer("migrate memories")
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
        _require_exclusive_writer("rebuild index")
        result = _generate_index()
        print(f"Index rebuilt: {INDEX_PATH}")
    elif cmd in ("--miner-start", "--miner-stop", "--miner-status"):
        import subprocess as _sp
        wrapper = str(Path(__file__).resolve().parent / "miner-wrapper.sh")
        action = cmd.replace("--miner-", "")
        _sp.run(["bash", wrapper, action])
    elif cmd is None:
        _require_exclusive_writer("start MCP server")
        _register_server_pid()
        mcp.run(transport="stdio")
    else:
        raise SystemExit(f"Unknown command: {cmd}")
