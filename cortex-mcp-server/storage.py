import atexit
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("cortex-storage")


CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
TELEMETRY_FILE = CORTEX_DIR / "telemetry.json"
EVENT_LOG = CORTEX_DIR / "events.jsonl"
SEARCH_DB = CORTEX_DIR / "search.db"
SERVER_PID_FILE = CORTEX_DIR / "mcp-server.pid"
OBSIDIAN_VAULT = Path(os.environ.get("CORTEX_OBSIDIAN_VAULT", str(Path.home() / "obsidian-brain")))
OBSIDIAN_MEMORIES_DIR = OBSIDIAN_VAULT / "cortex" / "memories"
INDEX_PATH = OBSIDIAN_VAULT / "cortex" / "_index.md"
PLAYBOOK_DIR = OBSIDIAN_VAULT / "cortex" / "playbooks"
PLAYBOOK_STAGING_DIR = PLAYBOOK_DIR / ".staging"


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True



def _cleanup_pid_file(path: Path, pid: int):
    try:
        if path.read_text().strip() == str(pid):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _ensure_vault_exists():
    """Create Obsidian vault directories if they don't exist."""
    OBSIDIAN_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)


def _auto_start_miner():
    """Start the miner daemon if not already running."""
    try:
        wrapper = Path(__file__).resolve().parent / "miner-wrapper.sh"
        if not wrapper.exists():
            return
        # Check if already running
        result = subprocess.run(
            ["bash", str(wrapper), "status"],
            capture_output=True, text=True, timeout=5,
        )
        if "running" in result.stdout.lower():
            return
        # Start it
        subprocess.Popen(
            ["bash", str(wrapper), "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("Auto-started miner daemon")
    except Exception:
        pass  # Non-fatal — miner is optional


def _register_server_pid():
    _ensure_vault_exists()
    pid = os.getpid()
    SERVER_PID_FILE.write_text(str(pid))
    _auto_start_miner()

    def _cleanup():
        _cleanup_pid_file(SERVER_PID_FILE, pid)

    atexit.register(_cleanup)


class ObsidianUnavailableError(RuntimeError):
    """Raised when the Obsidian vault cannot accept writes."""


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def _stable_mined_memory_id(session_id: str, title: str, content: str) -> str:
    seed = f"{session_id}\n{title.strip()}\n{content.strip()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(op: str, memory_id: str = "", **details) -> None:
    """Append an event to the audit log. Atomic for lines < PIPE_BUF."""
    try:
        CORTEX_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"op": op, "memory_id": memory_id, "timestamp": _now(), **details}
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Non-fatal — don't crash operations for logging failures


# ---------------------------------------------------------------------------
# Memory content security scanning
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    (r"ignore\s+.{0,20}(previous|prior|above|all)\s+.{0,10}instructions", "prompt_injection"),
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)", "bypass_restrictions"),
]

_EXFIL_PATTERNS = [
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access"),
]

_INVISIBLE_CHARS = {
    "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
}


def scan_memory_content(content: str) -> str | None:
    """Scan memory content for injection/exfil patterns.

    Returns error string if blocked, None if clean.
    Memory content is injected into future sessions' context —
    a successful injection here poisons all future interactions.
    """
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: invisible unicode U+{ord(char):04X} (possible injection)"

    text = content if isinstance(content, str) else ""
    for pattern, name in _INJECTION_PATTERNS + _EXFIL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return f"Blocked: matches threat pattern '{name}'"

    return None


def _make_memory(content: str, title: str, tags: list[str] | None = None,
                 project: str = "general", source_type: str = "user",
                 source_session: str = "", importance: int = 3) -> dict:
    threat = scan_memory_content(content)
    if threat:
        raise ValueError(threat)

    if title:
        title_threat = scan_memory_content(title)
        if title_threat:
            raise ValueError(f"Title: {title_threat}")

    normalized_project = (project or "general").strip() or "general"
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "essence": content,
        "domain_tags": tags or [],
        "project": normalized_project,
        "source_type": source_type,
        "source_session": source_session,
        "importance": importance,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _require_obsidian_writable() -> None:
    if not OBSIDIAN_VAULT.exists():
        raise ObsidianUnavailableError(
            f"Obsidian vault not found: {OBSIDIAN_VAULT}"
        )
    OBSIDIAN_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)


def _yaml_escape(value: str) -> str:
    """Quote YAML values that contain special characters."""
    if not value:
        return '""'
    if any(ch in value for ch in (':', '#', '{', '}', '[', ']', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`', '"', "'")):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if value.startswith((' ', '\t')) or value.endswith((' ', '\t')):
        return f'"{value}"'
    return value


def _write_obsidian_memory(mem: dict):
    _require_obsidian_writable()

    title = mem.get("title", "Untitled")
    slug = _slugify(title) or mem["id"][:8]
    filename = f"{slug}-{mem['id'][:8]}.md"

    old_file = mem.get("obsidian_file", "")
    if old_file and old_file != filename:
        old_path = OBSIDIAN_MEMORIES_DIR / old_file
        if old_path.exists():
            old_path.unlink()

    tags = mem.get("domain_tags", [])
    if isinstance(tags, str):
        tags = [tag for tag in tags.split(",") if tag]

    clean_title = re.sub(r"^\[[^\]]+\]\s*", "", mem.get("title", "Untitled"))
    project = mem.get("project", "general")

    frontmatter = (
        f"---\n"
        f"id: {mem['id']}\n"
        f"title: {_yaml_escape(clean_title)}\n"
        f"project: {project}\n"
        f"tags: [{', '.join(tags)}]\n"
    )
    related = mem.get("related", [])
    if related:
        frontmatter += f"related: [{', '.join(related)}]\n"
    frontmatter += (
        f"created: {mem.get('created_at', '')[:10]}\n"
        f"updated: {mem.get('updated_at', '')[:10]}\n"
        f"source_type: {mem.get('source_type', 'user')}\n"
        f"source_session: {mem.get('source_session', '')}\n"
        f"importance: {mem.get('importance', 3)}\n"
        f"status: {mem.get('status', 'active')}\n"
        f"valid_to: {mem.get('valid_to', '')}\n"
    )
    contradicts = mem.get("contradicts", [])
    if contradicts:
        frontmatter += f"contradicts: [{', '.join(contradicts)}]\n"
    frontmatter += "---"

    body = f"\n\n{mem.get('essence', '')}"
    filepath = OBSIDIAN_MEMORIES_DIR / filename
    filepath.write_text(frontmatter + body)
    mem["obsidian_file"] = filename


def _extract_memory_id_from_filename(md_file: Path) -> str:
    stem = md_file.stem
    return stem.rsplit("-", 1)[-1] if "-" in stem else stem


def _parse_obsidian_tags(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def _parse_obsidian_memory_file(md_file: Path) -> dict | None:
    try:
        content = md_file.read_text(errors="ignore")
    except OSError as exc:
        log.warning("Failed to read memory file %s: %s", md_file, exc)
        return None

    body = content.strip()
    mem = {
        "id": _extract_memory_id_from_filename(md_file),
        "title": md_file.stem,
        "project": "general",
        "domain_tags": [],
        "created_at": "",
        "file": str(md_file),
        "source_type": "user",
        "status": "active",
        "valid_to": "",
        "source_session": "",
        "access_count": 0,
        "last_accessed": "",
        "updated_at": "",
        "importance": 3,
    }

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2].strip()
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().strip('"')
                if key == "id" and value:
                    mem["id"] = value
                elif key == "title" and value:
                    mem["title"] = value
                elif key == "project" and value:
                    mem["project"] = value
                elif key == "tags":
                    mem["domain_tags"] = _parse_obsidian_tags(value)
                elif key == "related":
                    mem["related"] = _parse_obsidian_tags(value)
                elif key == "created":
                    mem["created_at"] = value
                elif key == "updated":
                    mem["updated_at"] = value
                elif key == "source_type":
                    mem["source_type"] = value
                elif key == "source_session":
                    mem["source_session"] = value
                elif key == "access_count":
                    try:
                        mem["access_count"] = int(value)
                    except (ValueError, TypeError):
                        mem["access_count"] = 0
                elif key == "last_accessed":
                    mem["last_accessed"] = value
                elif key == "status":
                    mem["status"] = value
                elif key == "valid_to":
                    mem["valid_to"] = value
                elif key == "contradicts":
                    mem["contradicts"] = _parse_obsidian_tags(value)  # reuse existing tag parser
                elif key == "importance":
                    try:
                        mem["importance"] = int(value)
                    except (ValueError, TypeError):
                        mem["importance"] = 3

    mem["essence"] = body
    mem["full_record"] = body  # read-time alias for essence — not stored in markdown
    if not mem["updated_at"]:
        mem["updated_at"] = mem.get("created_at", "")
    if not mem["title"] or mem["title"] == md_file.stem:
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        mem["title"] = first_line[:120] if first_line else mem["id"]

    if "imported" in mem["domain_tags"]:
        mem["source_type"] = "import"
    elif "mined" in mem["domain_tags"]:
        mem["source_type"] = "mined"
    elif not mem.get("source_type"):
        mem["source_type"] = "user"

    return mem


def _normalize_scope_id(scope_id: str) -> str:
    return "general" if not scope_id or scope_id == "default" else scope_id


def _obsidian_memories(scope_id: str | None = None, include_deprecated: bool = False) -> list[dict]:
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return []

    memories = []
    for md_file in sorted(OBSIDIAN_MEMORIES_DIR.glob("*.md")):
        mem = _parse_obsidian_memory_file(md_file)
        if not mem:
            continue
        if not include_deprecated and mem.get("status") == "deprecated":
            continue
        project = mem.get("project", "general")
        normalized = _normalize_scope_id(scope_id) if scope_id is not None else "general"
        if normalized != "general" and project != normalized:
            continue
        memories.append(mem)
    return memories


def _find_memory(memory_id: str) -> dict | None:
    """Find memory by exact ID or 8-char prefix. Single pass."""
    prefix_match = None
    for mem in _obsidian_memories():
        mid = mem.get("id", "")
        if mid == memory_id:
            return mem
        if prefix_match is None and len(memory_id) >= 8 and mid.startswith(memory_id):
            prefix_match = mem
    return prefix_match


_SYNONYMS = {
    "auth": "authentication",
    "authentication": "auth",
    "db": "database",
    "database": "db",
    "config": "configuration",
    "configuration": "config",
    "env": "environment",
    "environment": "env",
    "repo": "repository",
    "repository": "repo",
    "dep": "dependency",
    "dependency": "dep",
    "deps": "dependencies",
    "dependencies": "deps",
    "dir": "directory",
    "directory": "dir",
    "impl": "implementation",
    "implementation": "impl",
    "func": "function",
    "function": "func",
    "param": "parameter",
    "parameter": "param",
    "args": "arguments",
    "arguments": "args",
    "msg": "message",
    "message": "msg",
    "err": "error",
    "error": "err",
    "req": "request",
    "request": "req",
    "res": "response",
    "response": "res",
    "jwt": "token",
    "token": "jwt",
}


def _stem(word: str) -> str:
    """Minimal suffix stemmer — strips common suffixes for better matching."""
    for suffix in ("tion", "sion", "ing", "ment", "ness", "able", "ible", "ous", "ive", "ful", "less", "ize", "ise", "ated", "ates", "ies", "ed", "er", "ly", "al", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def _word_set(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    expanded = set(words)
    for word in words:
        stemmed = _stem(word)
        if stemmed != word:
            expanded.add(stemmed)
        syn = _SYNONYMS.get(word)
        if syn:
            expanded.add(syn)
    return expanded


def _ngram_set(text: str, n: int) -> set:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return set(tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1))


def _containment(a: set, b: set) -> float:
    """What fraction of the smaller set is contained in the larger."""
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    smaller = min(len(a), len(b))
    return overlap / smaller if smaller else 0.0


def _init_search_db() -> sqlite3.Connection:
    """Initialize SQLite FTS5 search index. Returns connection."""
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SEARCH_DB))
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
        "USING fts5(memory_id, title, essence, project, tags)"
    )
    conn.commit()
    return conn


def _index_memory(mem: dict) -> None:
    """Upsert a memory into the FTS5 search index."""
    try:
        conn = _init_search_db()
        mid = mem.get("id", "")
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (mid,))
        conn.execute(
            "INSERT INTO memories_fts (memory_id, title, essence, project, tags) VALUES (?, ?, ?, ?, ?)",
            (mid, mem.get("title", ""), mem.get("essence", ""), mem.get("project", "general"),
             " ".join(mem.get("domain_tags", []))),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("FTS index failed for %s: %s", mem.get("id", "")[:8], exc)


def _remove_from_index(memory_id: str) -> None:
    """Remove a memory from the FTS5 search index."""
    try:
        conn = _init_search_db()
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _search_fts(query: str, scope_id: str = "default", limit: int = 20) -> list[str]:
    """FTS5 search, returns list of memory IDs ranked by relevance."""
    try:
        conn = _init_search_db()
        normalized = _normalize_scope_id(scope_id)
        # Escape FTS5 special chars
        safe_query = query.replace('"', '""')
        if normalized != "general":
            cursor = conn.execute(
                "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ? AND project = ? ORDER BY rank LIMIT ?",
                (safe_query, normalized, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit),
            )
        results = [row[0] for row in cursor.fetchall()]
        conn.close()
        return results
    except Exception as exc:
        log.debug("FTS search failed: %s", exc)
        return []


def _rebuild_search_index() -> int:
    """Rebuild the FTS5 index from all active Obsidian memories. Returns count."""
    try:
        conn = _init_search_db()
        conn.execute("DELETE FROM memories_fts")
        count = 0
        for mem in _obsidian_memories():
            mid = mem.get("id", "")
            conn.execute(
                "INSERT INTO memories_fts (memory_id, title, essence, project, tags) VALUES (?, ?, ?, ?, ?)",
                (mid, mem.get("title", ""), mem.get("essence", ""), mem.get("project", "general"),
                 " ".join(mem.get("domain_tags", []))),
            )
            count += 1
        conn.commit()
        conn.close()
        return count
    except Exception as exc:
        log.warning("FTS rebuild failed: %s", exc)
        return 0


def _find_best_match(content: str, scope_id: str = "default") -> tuple[dict | None, float]:
    """Return (best_mem, best_score) for the closest existing memory to content.

    Uses CONTENT-ONLY scoring (containment) for dedup/merge decisions.
    No temporal or access weighting — those belong in the recall path only.
    Score thresholds: <0.3 = new, 0.3-0.6 = merge candidate, >0.6 = duplicate.
    """
    content_words = _word_set(content)
    if not content_words:
        return None, 0.0

    content_bigrams = _ngram_set(content, 2)
    content_trigrams = _ngram_set(content, 3)

    normalized = _normalize_scope_id(scope_id)
    project = None if normalized == "general" else normalized
    best_score = 0.0
    best_mem = None

    for mem in _obsidian_memories(project):
        mem_text = mem.get("essence", "") + " " + mem.get("title", "")
        mem_words = _word_set(mem_text)
        if not mem_words:
            continue
        word_c = _containment(content_words, mem_words)
        bigram_c = _containment(content_bigrams, _ngram_set(mem_text, 2))
        trigram_c = _containment(content_trigrams, _ngram_set(mem_text, 3))
        score = 0.5 * word_c + 0.3 * bigram_c + 0.2 * trigram_c
        if score > best_score:
            best_score = score
            best_mem = mem

    return best_mem, best_score


def _find_related(content: str, exclude_id: str, scope_id: str = "default", limit: int = 3) -> list[str]:
    """Return up to `limit` memory IDs (8-char prefix) related to content, excluding exclude_id."""
    content_words = _word_set(content)
    if not content_words:
        return []

    content_bigrams = _ngram_set(content, 2)
    content_trigrams = _ngram_set(content, 3)

    normalized = _normalize_scope_id(scope_id)
    project = None if normalized == "general" else normalized

    scored = []
    for mem in _obsidian_memories(project):
        mid = mem.get("id", "")
        if mid == exclude_id or mid.startswith(exclude_id) or exclude_id.startswith(mid[:8]):
            continue
        mem_text = mem.get("essence", "") + " " + mem.get("title", "")
        mem_words = _word_set(mem_text)
        if not mem_words:
            continue
        word_c = _containment(content_words, mem_words)
        bigram_c = _containment(content_bigrams, _ngram_set(mem_text, 2))
        trigram_c = _containment(content_trigrams, _ngram_set(mem_text, 3))
        score = 0.5 * word_c + 0.3 * bigram_c + 0.2 * trigram_c
        if score > 0.2:
            scored.append((score, mid[:8]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [mid for _, mid in scored[:limit]]


def _is_duplicate(content: str, scope_id: str = "default", threshold: float = 0.7,
                  return_match: bool = False) -> dict | bool | None:
    """Check for duplicate via blended word/bigram/trigram overlap against Obsidian memories."""
    content_words = _word_set(content)
    if not content_words:
        return None if return_match else False

    best_mem, best_score = _find_best_match(content, scope_id)

    if best_score >= threshold:
        return best_mem if return_match else True
    return None if return_match else False


_NEGATION_SIGNALS = {
    "removed", "no longer", "replaced", "deprecated", "instead of",
    "stopped using", "switched from", "ripped out", "deleted",
    "no more", "eliminated", "dropped", "abandoned", "reversed",
}


def _check_contradictions(content: str, scope_id: str = "default") -> list[dict]:
    """Check if new content contradicts existing memories. Returns list of conflicts."""
    content_lower = content.lower()

    # Check if new content contains negation signals
    has_negation = any(signal in content_lower for signal in _NEGATION_SIGNALS)
    if not has_negation:
        return []  # No negation in new content — skip expensive search

    # Find similar existing memories
    try:
        fts_ids = _search_fts(content[:200], scope_id, 5)
        candidates = [m for mid in fts_ids if (m := _find_memory(mid))]
    except Exception:
        # FTS unavailable — use content match
        match, score = _find_best_match(content, scope_id)
        candidates = [match] if match and score > 0.2 else []

    contradictions = []
    for mem in candidates:
        if not mem:
            continue
        mem_lower = mem.get("essence", "").lower()
        # Check if existing memory and new content have opposing signals
        # New says "removed X" and old says "use X" (or vice versa)
        for signal in _NEGATION_SIGNALS:
            if signal in content_lower and signal not in mem_lower:
                # New content negates something, existing doesn't — possible contradiction
                contradictions.append({
                    "memory_id": mem.get("id", "")[:8],
                    "title": mem.get("title", "")[:60],
                    "reason": f"new content contains '{signal}' about a topic the existing memory affirms",
                })
                break

    return contradictions


def _save_memory(mem: dict):
    """Save memory to Obsidian vault."""
    _require_obsidian_writable()

    # Check for contradictions
    contradictions = _check_contradictions(mem.get("essence", ""), mem.get("project", "default"))
    if contradictions:
        mem["contradicts"] = [c["memory_id"] for c in contradictions]

    _write_obsidian_memory(mem)
    if INDEX_PATH.exists():
        _append_or_update_index_line(mem)
    _log_event("save", mem.get("id", ""), title=mem.get("title", ""))
    _index_memory(mem)

    # Compute and store related memory links
    content = mem.get("essence", "")
    mem_id = mem.get("id", "")
    if content and mem_id:
        related = _find_related(content, exclude_id=mem_id, scope_id=mem.get("project", "default"))
        if related:
            mem["related"] = related
            _write_obsidian_memory(mem)  # Rewrite with related field


def _delete_memory(memory_id: str) -> bool:
    try:
        mem = _find_memory(memory_id)
        if mem:
            obsidian_path = Path(mem.get("file", ""))
            if obsidian_path.exists():
                obsidian_path.unlink()
        _remove_index_line(memory_id)
        _log_event("delete", memory_id)
        _remove_from_index(memory_id)
        return True
    except Exception:
        return False


def _deprecate_memory(memory_id: str, reason: str = "superseded") -> bool:
    """Mark a memory as deprecated instead of deleting it."""
    mem = _find_memory(memory_id)
    if not mem:
        return False
    mem["status"] = "deprecated"
    mem["valid_to"] = _now()
    _write_obsidian_memory(mem)
    _remove_index_line(memory_id)
    _log_event("deprecate", memory_id, reason=reason)
    _remove_from_index(memory_id)
    return True


def _extract_project(mem: dict) -> str:
    title = mem.get("title") or ""
    match = re.match(r"^\[([^\]]+)\]", title)
    if match:
        return match.group(1)

    project = mem.get("project", "")
    if project and project != "general":
        return project

    return "general"


def _format_index_line(mem: dict) -> str:
    title = mem.get("title") or mem.get("essence", "")[:60]
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    title = title.split("\n")[0].strip()[:120]

    tags_str = " ".join(f"#{tag}" for tag in mem.get("domain_tags") or [] if tag)
    mem_id = mem.get("id", "")[:8]
    line = f"- {title} ({mem_id})"
    if tags_str:
        line += f" {tags_str}"
    return line


def _memory_date_key(mem: dict) -> datetime:
    try:
        return datetime.fromisoformat((mem.get("created_at") or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _generate_index(scope_id: str = "default") -> str:
    memories = _obsidian_memories(scope_id if scope_id != "default" else None)
    projects: dict[str, list[dict]] = {}
    for mem in memories:
        project = _extract_project(mem)
        projects.setdefault(project, []).append(mem)

    sorted_projects = sorted(project for project in projects if project != "general")
    if "general" in projects:
        sorted_projects.append("general")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "# Cortex Memory Index",
        f"Updated: {today} | Total: {len(memories)} memories",
    ]

    for project in sorted_projects:
        mems_sorted = sorted(projects[project], key=_memory_date_key, reverse=True)
        count = len(mems_sorted)
        noun = "memory" if count == 1 else "memories"
        lines.append("")
        lines.append(f"## {project} ({count} {noun})")
        for mem in mems_sorted:
            lines.append(_format_index_line(mem))

    content = "\n".join(lines) + "\n"
    if OBSIDIAN_VAULT.exists():
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(content)
    return content


def _recount_index_sections(lines: list[str]) -> list[str]:
    """Recount section totals and update the Updated header in index lines."""
    for idx, entry in enumerate(lines):
        if entry.startswith("## ") and "(" in entry:
            section_name = entry.split("(")[0].strip().removeprefix("## ").strip()
            count = 0
            for j in range(idx + 1, len(lines)):
                if lines[j].startswith("## "):
                    break
                if lines[j].startswith("- "):
                    count += 1
            noun = "memory" if count == 1 else "memories"
            lines[idx] = f"## {section_name} ({count} {noun})"

    total = sum(1 for entry in lines if entry.startswith("- "))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for idx, entry in enumerate(lines):
        if entry.startswith("Updated:"):
            lines[idx] = f"Updated: {today} | Total: {total} memories"
            break

    return lines


def _append_or_update_index_line(mem: dict):
    if not INDEX_PATH.exists():
        return

    content = INDEX_PATH.read_text()
    mem_id = mem["id"][:8]
    line = _format_index_line(mem)
    project = _extract_project(mem)
    lines = content.split("\n")
    new_lines = [entry for entry in lines if not (entry.startswith("- ") and f"({mem_id})" in entry)]

    section_header = f"## {project} ("
    inserted = False
    for idx, entry in enumerate(new_lines):
        if entry.startswith(section_header):
            new_lines.insert(idx + 1, line)
            inserted = True
            break

    if not inserted:
        new_lines.extend(["", f"## {project} (1 memory)", line])

    new_lines = _recount_index_sections(new_lines)
    INDEX_PATH.write_text("\n".join(new_lines))


def _remove_index_line(memory_id: str):
    if not INDEX_PATH.exists():
        return

    mem_id = memory_id[:8]
    lines = INDEX_PATH.read_text().split("\n")
    new_lines = [entry for entry in lines if not (entry.startswith("- ") and f"({mem_id})" in entry)]

    new_lines = _recount_index_sections(new_lines)
    INDEX_PATH.write_text("\n".join(new_lines))


def _load_obsidian_memories(picked_ids: list[str]) -> list[dict]:
    results = []
    for picked_id in picked_ids:
        mem = _find_memory(picked_id)
        if not mem:
            continue
        results.append({
            "title": mem.get("title", "Untitled"),
            "project": mem.get("project", "general"),
            "body": mem.get("full_record", mem.get("essence", "")),
        })
    return results


def _update_memory(memory_id: str, new_content: str, new_title: str = "") -> None:
    """Update an existing memory's content and optionally its title."""
    threat = scan_memory_content(new_content)
    if threat:
        raise ValueError(f"Update blocked: {threat}")
    mem = _find_memory(memory_id)
    if mem is None:
        raise ValueError(f"Memory not found: {memory_id}")
    mem["essence"] = new_content
    if new_title:
        mem["title"] = new_title
    mem["updated_at"] = _now()
    _write_obsidian_memory(mem)
    _append_or_update_index_line(mem)
    _log_event("update", memory_id)
    _index_memory(mem)


def _get_telemetry(memory_id: str) -> dict:
    """Get access telemetry for a memory from the sidecar file."""
    if not TELEMETRY_FILE.exists():
        return {"access_count": 0, "last_accessed": ""}
    try:
        data = json.loads(TELEMETRY_FILE.read_text())
        entry = data.get(memory_id[:8], {})
        return {
            "access_count": entry.get("access_count", 0),
            "last_accessed": entry.get("last_accessed", ""),
        }
    except (json.JSONDecodeError, OSError):
        return {"access_count": 0, "last_accessed": ""}


def _record_access(memory_id: str) -> None:
    """Record a memory access in the telemetry sidecar file."""
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = TELEMETRY_FILE.with_suffix(".lock")
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = {}
        if TELEMETRY_FILE.exists():
            try:
                data = json.loads(TELEMETRY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        key = memory_id[:8]
        entry = data.get(key, {"access_count": 0, "last_accessed": ""})
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["last_accessed"] = _now()
        data[key] = entry
        TELEMETRY_FILE.write_text(json.dumps(data))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def purge_mined_memories(mined_sessions_file: Path) -> dict:
    deleted = 0
    for mem in _obsidian_memories():
        if "mined" in (mem.get("domain_tags") or []):
            obsidian_path = Path(mem.get("file", ""))
            if obsidian_path.exists():
                obsidian_path.unlink()
                deleted += 1

    if mined_sessions_file.exists():
        mined_sessions_file.write_text("")
    return {"deleted": deleted}


def _playbook_append(project: str, new_memory: dict) -> None:
    """Stage a new memory for the next playbook refinement (grow step)."""
    PLAYBOOK_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging_path = PLAYBOOK_STAGING_DIR / f"{project}.jsonl"
    title = new_memory.get("title", "Untitled")
    essence = new_memory.get("essence", "")
    if not essence:
        return
    entry = json.dumps({
        "title": title,
        "essence": essence,
        "timestamp": _now(),
    })
    with open(staging_path, "a") as f:
        f.write(entry + "\n")


_REFINE_SYSTEM = (
    "You are refining a project knowledge playbook. Reorganize sections by topic. "
    "Merge redundant entries. Remove obsolete information that contradicts newer entries. "
    "Keep it concise and well-structured. Preserve the title line and Updated date at the top. "
    "Output the refined playbook as clean markdown."
)


def _playbook_refine(project: str) -> None:
    """Compile staged entries into the playbook and refine (refine step)."""
    playbook_path = PLAYBOOK_DIR / f"{project}.md"
    staging_path = PLAYBOOK_STAGING_DIR / f"{project}.jsonl"

    # Read existing compiled playbook
    existing = ""
    if playbook_path.exists():
        existing = playbook_path.read_text().strip()
        # Strip old markers
        for marker in ("<!-- refined:", "<!-- cortex-hash:"):
            idx = existing.rfind(marker)
            if idx != -1:
                existing = existing[:idx].rstrip()

    # Read staged entries
    staged_entries = []
    if staging_path.exists():
        try:
            for line in staging_path.read_text().splitlines():
                line = line.strip()
                if line:
                    staged_entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            pass

    # Nothing to do if no staged entries and playbook exists
    if not staged_entries and existing:
        return

    # If no playbook and no staged entries, nothing to do
    if not staged_entries and not existing:
        return

    # Build content for Haiku
    parts = []
    if existing:
        parts.append(existing)
    for entry in staged_entries:
        parts.append(f"## {entry.get('title', 'Untitled')}\n{entry.get('essence', '')}")
    combined = "\n\n".join(parts)

    # If too small, just write without Haiku
    if len(combined) < 2000:
        threat = scan_memory_content(combined)
        if threat:
            log.warning("Playbook blocked for %s: %s", project, threat)
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not existing:
            combined = f"# {project} — Project Playbook\nUpdated: {today}\n\n{combined}"
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        playbook_path.write_text(combined + "\n")
        _log_event("refine", project=project)
        # Clear staging
        if staging_path.exists():
            staging_path.unlink()
        return

    # Haiku refine
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _REFINE_SYSTEM],
            input=combined,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        log.warning("Playbook refine failed for %s: %s", project, exc)
        return

    if result.returncode != 0 or not result.stdout.strip():
        log.warning("Playbook refine empty/error for %s", project)
        return

    refined = result.stdout.strip()
    # Security scan
    threat = scan_memory_content(refined)
    if threat:
        log.warning("Playbook refine blocked for %s: %s", project, threat)
        return

    new_hash = hashlib.sha256(refined.encode()).hexdigest()
    refined += f"\n\n<!-- refined:{new_hash} -->\n"
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    playbook_path.write_text(refined)
    _log_event("refine", project=project)

    # Clear staging after successful compile
    if staging_path.exists():
        staging_path.unlink()


_CONSOLIDATION_SYSTEM = (
    "You are a memory consolidation engine. Review these memory entries for a project "
    "and identify cleanup actions. Output a JSON object with:\n"
    '- "merge": array of [keep_id, remove_id] pairs for redundant memories that say the same thing. '
    "The first ID is kept (merged into), the second is deleted.\n"
    '- "delete": array of IDs for memories that are obsolete, superseded by other memories in the set, '
    "or no longer accurate.\n\n"
    "Be conservative — only flag clear redundancies and obvious obsolescence. "
    "When in doubt, keep the memory.\n"
    "Output ONLY the JSON object, no other text."
)


def _consolidate_project(project: str) -> dict:
    """Consolidate memories for a project — merge redundant, delete obsolete."""
    memories = _obsidian_memories(project)
    if len(memories) < 5:
        return {"merged": 0, "deleted": 0}

    # Build summary for Haiku
    lines = []
    total_chars = 0
    for mem in memories:
        line = f"[{mem['id'][:8]}] {mem.get('title', '')}: {mem.get('essence', '')[:200]}"
        if total_chars + len(line) > 50000:
            break
        lines.append(line)
        total_chars += len(line)

    prompt = "Review these memory entries and identify cleanup actions:\n\n" + "\n".join(lines)

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _CONSOLIDATION_SYSTEM],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        log.warning("Consolidation failed for %s: %s", project, exc)
        return {"merged": 0, "deleted": 0}

    if result.returncode != 0:
        log.warning("Consolidation Haiku error for %s: %s", project, result.stderr.strip()[:200])
        return {"merged": 0, "deleted": 0}

    output = result.stdout.strip()
    if not output:
        return {"merged": 0, "deleted": 0}

    # Parse JSON — find { } with bracket depth
    json_start = output.find("{")
    if json_start == -1:
        return {"merged": 0, "deleted": 0}
    depth = 0
    json_end = -1
    for i in range(json_start, len(output)):
        if output[i] == "{": depth += 1
        elif output[i] == "}":
            depth -= 1
            if depth == 0:
                json_end = i
                break
    if json_end == -1:
        return {"merged": 0, "deleted": 0}

    try:
        actions = json.loads(output[json_start:json_end + 1])
    except json.JSONDecodeError:
        return {"merged": 0, "deleted": 0}

    merged_count = 0
    deleted_count = 0

    # Execute merges
    for pair in actions.get("merge", []):
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        keep_id, remove_id = pair[0], pair[1]
        keep_mem = _find_memory(keep_id)
        remove_mem = _find_memory(remove_id)
        if not keep_mem or not remove_mem:
            continue
        try:
            # Lazy import to avoid circular dependency (mining imports from storage)
            from mining import _merge_memories
            merged_content = _merge_memories(keep_mem.get("essence", ""), remove_mem.get("essence", ""))
            _update_memory(keep_mem["id"], merged_content)
            _deprecate_memory(remove_mem["id"], f"merged_into:{keep_id}")
            merged_count += 1
        except Exception as exc:
            log.warning("Consolidation merge failed: %s", exc)

    # Execute deletes
    for mem_id in actions.get("delete", []):
        if not isinstance(mem_id, str):
            continue
        if _find_memory(mem_id):
            _deprecate_memory(mem_id, "consolidated")
            deleted_count += 1

    return {"merged": merged_count, "deleted": deleted_count}


_ASSEMBLE_SYSTEM = (
    "You are a context assembly engine. Given a user's query and raw knowledge "
    "materials, produce a comprehensive briefing that contains everything an AI "
    "assistant would need to answer the query well. Include all relevant facts, "
    "decisions, conventions, and context. Exclude anything unrelated to the query. "
    "Format as clean markdown. Be thorough — include everything relevant, but "
    "nothing that isn't. Output ONLY the briefing, no meta-commentary."
)


def context_assemble(query: str, project: str = "default") -> str:
    """Assemble a query-tailored context briefing from all available knowledge.

    Gathers playbook, relevant memories, and session history, then uses Haiku
    to produce a focused briefing for the given query.
    """
    normalized = _normalize_scope_id(project)

    # Load playbook if exists
    playbook_content = ""
    playbook_path = PLAYBOOK_DIR / f"{normalized}.md"
    if playbook_path.exists():
        try:
            content = playbook_path.read_text().strip()
            # Strip hash comment at end
            lines = content.split("\n")
            if lines and (lines[-1].strip().startswith("<!-- cortex-hash:") or lines[-1].strip().startswith("<!-- refined:")):
                content = "\n".join(lines[:-1]).strip()
            playbook_content = content
        except OSError:
            pass

    # Get relevant memories (lazy import to avoid circular dep)
    from recall import _search_memories
    memories = _search_memories(query, scope_id=normalized, limit=20, record_access=False)

    # Get transcript search results (lazy import)
    from transcripts import transcript_search
    transcript_results = transcript_search(query, limit=3)

    # Format materials
    parts = []
    parts.append(f"PLAYBOOK:\n{playbook_content or 'No playbook available'}")

    if memories:
        mem_lines = []
        for mem in memories:
            title = mem.get("title", "Untitled")
            essence = mem.get("essence", mem.get("full_record", ""))
            mem_lines.append(f"## {title}\n{essence}")
        parts.append(f"RELEVANT MEMORIES:\n" + "\n\n".join(mem_lines))

    if transcript_results and "No matching" not in transcript_results:
        parts.append(f"RELATED SESSIONS:\n{transcript_results}")

    materials = "\n\n".join(parts)
    # Cap at 50K chars
    if len(materials) > 50000:
        materials = materials[:50000]

    prompt = f"QUERY: {query}\n\n{materials}"

    # Haiku assembles the brief
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _ASSEMBLE_SYSTEM],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        return playbook_content or ""

    if result.returncode != 0 or not result.stdout.strip():
        return playbook_content or ""

    return result.stdout.strip()
