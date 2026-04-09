#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory for Claude Code.

Stores memories as JSON files in ~/.cortex/memories/
No database required — works out of the box.

Tools:
  - memory_recall: Search all memory sources (Haiku-expanded query + vector search + transcript logs)
  - memory_save: Save a piece of knowledge
  - memory_list: List all memories in a scope
  - memory_feedback: Report whether recalled memories helped
  - memory_stats: Show memory system statistics
  - memory_promote: Promote a candidate memory to learned
  - transcript_search: Search raw Claude Code JSONL session files
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starter_packs import detect_packs, get_pack_memories

# ─── Storage ──────────────────────────────────────────────────────

CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
MEMORIES_DIR = CORTEX_DIR / "memories"
LOGS_DIR = CORTEX_DIR / "logs"

MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _load_memory(memory_id: str) -> dict | None:
    path = MEMORIES_DIR / f"{memory_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def _save_memory(mem: dict):
    path = MEMORIES_DIR / f"{mem['id']}.json"
    path.write_text(json.dumps(mem, indent=2, default=str))


def _delete_memory(memory_id: str) -> bool:
    path = MEMORIES_DIR / f"{memory_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _all_memories(scope_id: str | None = None) -> list[dict]:
    memories = []
    for f in MEMORIES_DIR.glob("*.json"):
        try:
            mem = json.loads(f.read_text())
            if scope_id and mem.get("scope_id") != scope_id:
                continue
            memories.append(mem)
        except (json.JSONDecodeError, OSError):
            continue
    return memories


def _search_memories(query: str, scope_id: str | None = None, limit: int = 10) -> list[dict]:
    """Hybrid search: keyword overlap + vector similarity + domain filtering.
    Uses local embeddings if available, falls back to keyword-only.
    Boosts memories matching the query's detected domain."""
    all_mems = _all_memories(scope_id)
    if not all_mems:
        return []

    query_lower = query.lower()
    query_words = set(query_lower.split())

    # Detect query domain for filtering boost
    query_domain, query_room = _detect_domain_room(query)

    # Try vector search
    use_vectors = False
    query_embedding = None
    try:
        from embeddings import is_available, get_or_compute_embedding, cosine_similarity
        if is_available():
            import hashlib
            query_hash = hashlib.md5(query.encode()).hexdigest()[:12]
            query_embedding = get_or_compute_embedding(f"query_{query_hash}", query)
            use_vectors = True
    except Exception:
        pass

    scored = []
    for mem in all_mems:
        # Keyword score
        text = " ".join([
            mem.get("essence", ""),
            mem.get("title", ""),
            " ".join(mem.get("domain_tags", [])),
        ]).lower()
        text_words = set(text.split())

        if query_words:
            keyword_score = len(query_words & text_words) / len(query_words)
        else:
            keyword_score = 0.0

        # Vector similarity score
        vector_score = 0.0
        if use_vectors and query_embedding:
            try:
                mem_text = mem.get("essence", "") or mem.get("title", "")
                if mem_text:
                    mem_embedding = get_or_compute_embedding(mem["id"], mem_text)
                    vector_score = max(0.0, cosine_similarity(query_embedding, mem_embedding))
            except Exception:
                pass

        # Confidence and promotion boosts
        confidence = float(mem.get("confidence", 0.5))
        learned_boost = 1.1 if mem.get("promotion_status") == "learned" else 1.0
        impact = float(mem.get("impact_score", 0.0))

        # Recency boost: recently validated memories get a small boost
        recency_boost = 0.0
        last_validated = mem.get("last_validated_at")
        if last_validated:
            try:
                validated_dt = datetime.fromisoformat(last_validated.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - validated_dt).total_seconds() / 86400
                if days_ago < 1:
                    recency_boost = 0.1
                elif days_ago < 7:
                    recency_boost = 0.05
                elif days_ago < 30:
                    recency_boost = 0.02
            except (ValueError, TypeError):
                pass

        # Combined score — vector-first like MemPalace (semantic similarity dominates)
        if use_vectors:
            # Vector-dominant: semantic similarity is the primary signal
            # Keyword acts as exact-match boost, not a gate
            combined = (0.70 * vector_score + 0.15 * keyword_score +
                       0.05 * confidence + 0.05 * impact + 0.05 * recency_boost) * learned_boost
        else:
            # Keyword only fallback (no vectors available)
            combined = (0.55 * keyword_score + 0.20 * confidence +
                       0.15 * impact + 0.10 * recency_boost) * learned_boost

        # Domain match boost (additive, not multiplicative — prevents excluding valid results)
        if query_domain and query_domain != "general":
            mem_domain = mem.get("domain", "")
            if mem_domain == query_domain:
                combined += 0.15  # boost for same domain
            elif mem_domain and mem_domain != "general":
                combined -= 0.02  # slight penalty for different domain (never kills a result)

        if combined > 0.05:  # minimum threshold
            scored.append((combined, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def _format_memory_as_bullet(mem: dict) -> str:
    """Format a single memory as a markdown bullet for LLM consumption."""
    title = mem.get("title", "Untitled")
    essence = mem.get("essence", "")
    full_record = mem.get("full_record")

    line = f"- **{title}**"
    # Add essence if it's different from title
    if essence and essence[:60] != title[:60]:
        line += f" — {essence[:300]}"

    # Attach raw exchange log for mined memories
    if full_record and full_record != essence:
        line += f"\n  > {full_record[:500]}"

    return line


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Domain detection keywords
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
        """Check if keyword appears in text with word boundary awareness."""
        # Multi-word keywords use simple substring match
        if " " in kw:
            return kw in txt
        # Single-word keywords use word boundary regex to avoid partial matches
        return bool(re.search(r'\b' + re.escape(kw) + r'\b', txt))

    # Score each domain
    domain_scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if _kw_in_text(kw, text))
        if score > 0:
            domain_scores[domain] = score

    if not domain_scores:
        return ("general", "")

    # Best domain
    domain = max(domain_scores, key=domain_scores.get)

    # Room = most specific keyword that matched within that domain
    room_keywords = DOMAIN_KEYWORDS[domain]
    matched = [kw for kw in room_keywords if _kw_in_text(kw, text)]
    room = matched[0] if matched else ""

    return (domain, room)


def _check_contradictions(new_mem: dict, scope_id: str = "default", threshold: float = 0.7) -> list[str]:
    """Check if a new memory contradicts existing ones.

    Two memories contradict if they:
    1. Are in the same domain (high similarity)
    2. But have different/opposing content

    Detection heuristic: high similarity + negation patterns.
    Returns list of contradicting memory IDs.
    """
    existing = _all_memories(scope_id)
    if not existing:
        return []

    new_text = new_mem.get("essence", "")
    new_domain = new_mem.get("domain", "general")
    if not new_text:
        return []

    contradicting_ids = []

    # Negation/opposition indicators
    opposition_words = {"not", "never", "avoid", "don't", "dont", "instead", "rather", "but",
                        "however", "unlike", "wrong", "bad", "deprecated", "obsolete",
                        "no longer", "stop", "remove"}

    new_words = set(new_text.lower().split())
    new_has_negation = bool(new_words & opposition_words)

    for mem in existing:
        if mem["id"] == new_mem.get("id"):
            continue

        mem_domain = mem.get("domain", "general")
        mem_text = mem.get("essence", "")
        if not mem_text:
            continue

        # Must be same domain to contradict
        if new_domain != "general" and mem_domain != "general" and new_domain != mem_domain:
            continue

        # Check similarity (topic overlap)
        try:
            from embeddings import is_available, get_or_compute_embedding, cosine_similarity
            if is_available():
                new_emb = get_or_compute_embedding(f"contra_{new_mem.get('id', 'new')}", new_text)
                mem_emb = get_or_compute_embedding(mem["id"], mem_text)
                similarity = cosine_similarity(new_emb, mem_emb)
            else:
                # Keyword overlap as fallback
                mem_words = set(mem_text.lower().split())
                shared = len(new_words & mem_words)
                similarity = shared / max(len(new_words | mem_words), 1)
        except Exception:
            continue

        # Check negation in BOTH directions
        mem_words_set = set(mem_text.lower().split())
        mem_has_negation = bool(mem_words_set & opposition_words)
        either_has_negation = new_has_negation or mem_has_negation

        # High similarity (same topic) + negation in either = likely contradiction
        if similarity >= threshold and either_has_negation:
            contradicting_ids.append(mem["id"])

        # Very high similarity — may be updated knowledge
        elif similarity >= 0.85:
            new_title_words = set(new_mem.get("title", "").lower().split())
            mem_title_words = set(mem.get("title", "").lower().split())
            if new_title_words and mem_title_words:
                title_overlap = len(new_title_words & mem_title_words) / max(len(new_title_words | mem_title_words), 1)
                if title_overlap > 0.5 and either_has_negation:
                    contradicting_ids.append(mem["id"])

    return list(set(contradicting_ids))


def _is_duplicate(content: str, scope_id: str = "default", threshold: float = 0.8, return_match: bool = False):
    """Check if content is too similar to an existing memory.

    When return_match=False (default): return bool.
    When return_match=True: return the matching memory dict if found, or None if no duplicate.
    """
    content_words = set(content.lower().split())
    if not content_words:
        return None if return_match else False

    existing = _all_memories(scope_id)
    for mem in existing:
        mem_text = mem.get("essence", "")
        if not mem_text:
            continue
        mem_words = set(mem_text.lower().split())
        if not mem_words:
            continue
        overlap = len(content_words & mem_words) / max(len(content_words), len(mem_words))
        if overlap >= threshold:
            return mem if return_match else True
    return None if return_match else False


def _assign_tier(mem: dict) -> str:
    """Assign memory tier based on type, confidence, and retrieval patterns.

    L0: Identity — project name, tech stack (always loaded, ~50 tokens)
    L1: Core — high-confidence conventions, critical lessons (always loaded, ~150 tokens)
    L2: Domain — topic-specific knowledge (loaded when domain matches)
    L3: Deep — detailed records, historical context (loaded on explicit search)
    """
    memory_type = mem.get("memory_type", "")
    confidence = float(mem.get("confidence", 0.5))
    impact = float(mem.get("impact_score", 0.0))
    retrieval_count = int(mem.get("retrieval_count", 0))
    source_type = mem.get("source_type", "")
    title = mem.get("title", "").lower()

    # L0: Project identity
    if any(kw in title for kw in ["project overview", "tech stack", "directory structure"]):
        return "L0"
    if source_type == "auto_seed" and memory_type == "knowledge":
        return "L0"

    # L1: High-value proven knowledge
    if confidence >= 0.8 and retrieval_count >= 3:
        return "L1"
    if memory_type == "convention" and mem.get("promotion_status") == "learned":
        return "L1"
    if memory_type == "preference":
        return "L1"

    # L3: Deep/historical
    if memory_type in ("procedure", "knowledge") and len(mem.get("essence", "")) > 500:
        return "L3"
    if source_type == "import" and retrieval_count == 0:
        return "L3"

    # L2: Everything else
    return "L2"


def _make_memory(content: str, title: str, memory_type: str = "knowledge",
                 scope_id: str = "default", tags: list[str] | None = None,
                 source_type: str = "auto_seed", confidence: float = 0.6,
                 promotion_status: str = "candidate", verified: bool = False,
                 source_session_id: str | None = None,
                 full_record: str | None = None) -> dict:
    """Create a memory dict without saving."""
    domain, room = _detect_domain_room(content, title, tags)
    # full_record: store None if it's the same as content (no duplication)
    effective_full_record = None if (full_record is None or full_record == content) else full_record
    mem = {
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
    mem["tier"] = _assign_tier(mem)
    return mem


# ─── Auto-seed ───────────────────────────────────────────────────

SEED_MARKER_FILE = CORTEX_DIR / ".seeded_scopes"

# Indicators that a directory is a project (not just a random folder)
_PROJECT_INDICATORS = {
    ".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "Gemfile", "pom.xml", "build.gradle", "requirements.txt",
    "composer.json", "Makefile", "CMakeLists.txt",
}

# Directories to skip when scanning workspace
_SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", ".venv", "dist", "build",
    ".cache", ".local", ".config", ".ssh", ".gnupg", ".claude",
    ".cortex", ".npm", ".cargo", ".rustup",
}


def _detect_workspace_projects(workspace: str | None = None) -> list[str]:
    """Find all project directories in the user's workspace.

    Scans immediate children of the workspace directory for directories
    that contain project indicators (e.g. .git, package.json, pyproject.toml).
    """
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
            # Check if this directory has any project indicators
            try:
                contents = set(os.listdir(full_path))
            except OSError:
                continue
            if contents & _PROJECT_INDICATORS:
                projects.append(full_path)
    except OSError:
        pass

    return projects


def _was_seeded(scope_id: str) -> bool:
    """Check if a scope has already been seeded."""
    if not SEED_MARKER_FILE.exists():
        return False
    seeded = set(s for s in SEED_MARKER_FILE.read_text().strip().split("\n") if s)
    return scope_id in seeded


def _mark_seeded(scope_id: str):
    """Mark a scope as seeded so we don't re-scan. Atomic write."""
    existing = ""
    if SEED_MARKER_FILE.exists():
        existing = SEED_MARKER_FILE.read_text().strip()
    seeded = set(s for s in existing.split("\n") if s) if existing else set()
    seeded.add(scope_id)
    # Atomic write via temp file + rename
    tmp = SEED_MARKER_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(seeded)))
    tmp.replace(SEED_MARKER_FILE)



def _auto_seed_workspace(scope_id: str) -> list[dict]:
    """Scan all project directories in the user's workspace and seed memories.

    Detects the workspace from $HOME, finds all child directories that look
    like projects, and runs _auto_seed on each one. Starter packs are
    collected across all projects to cover all detected languages.
    """
    if _was_seeded(scope_id):
        return []

    projects = _detect_workspace_projects()
    if not projects:
        return []

    # Collect starter packs across all projects so we get all languages
    all_packs = set()
    for project_dir in projects:
        all_packs.update(detect_packs(project_dir))

    all_seeded = []
    for project_dir in projects:
        mems = _auto_seed_single_project(scope_id, project_dir,
                                          include_starter_packs=False)
        all_seeded.extend(mems)

    # Add starter packs once, covering all detected languages
    pack_memories = get_pack_memories(list(all_packs))
    for title, content, mem_type, tags in pack_memories:
        mem = _make_memory(
            content=content,
            title=title,
            memory_type=mem_type,
            scope_id=scope_id,
            tags=tags + ["starter-pack"],
            source_type="starter_pack",
        )
        mem["confidence"] = 0.7
        mem["promotion_status"] = "learned"
        mem["verified"] = True
        _save_memory(mem)
        all_seeded.append(mem)

    if all_seeded:
        _mark_seeded(scope_id)

    return all_seeded


def _auto_seed_single_project(scope_id: str, project_dir: str,
                               include_starter_packs: bool = True) -> list[dict]:
    """Seed memories from a single project directory without marking as seeded.

    This is the inner scan logic extracted so _auto_seed_workspace can call it
    for multiple projects under one scope.
    """
    cwd = project_dir
    if not os.path.isdir(cwd):
        return []

    project_name = os.path.basename(cwd)
    memories = []

    # 1. README / CLAUDE.md — project description
    for doc_name in ["CLAUDE.md", "README.md", "readme.md"]:
        doc_path = os.path.join(cwd, doc_name)
        if os.path.exists(doc_path):
            try:
                content = open(doc_path).read()[:2000]
                lines = content.split("\n")
                title_line = next((l for l in lines if l.startswith("# ")), "")
                title = title_line.lstrip("# ").strip() or doc_name
                paragraphs = re.split(r'\n\n+', content)
                desc = next((p for p in paragraphs if len(p.strip()) > 20
                            and not p.startswith("#")), "")
                if desc:
                    mem = _make_memory(
                        content=f"Project: {title}. {desc.strip()[:500]}",
                        title=f"[{project_name}] Project overview: {title}",
                        memory_type="knowledge",
                        scope_id=scope_id,
                        tags=["project", "overview", project_name],
                    )
                    memories.append(mem)
            except OSError:
                pass

    # 2. Tech stack
    tech_detectors = [
        ("pyproject.toml", "Python"),
        ("package.json", "JavaScript/Node.js"),
        ("Cargo.toml", "Rust"),
        ("go.mod", "Go"),
        ("Gemfile", "Ruby"),
        ("pom.xml", "Java/Maven"),
        ("build.gradle", "Java/Gradle"),
        ("requirements.txt", "Python"),
        ("composer.json", "PHP"),
    ]
    detected_stack = []
    for filename, lang in tech_detectors:
        if os.path.exists(os.path.join(cwd, filename)):
            detected_stack.append(lang)

    if detected_stack:
        stack_str = ", ".join(set(detected_stack))
        mem = _make_memory(
            content=f"[{project_name}] Tech stack: {stack_str}",
            title=f"[{project_name}] Tech stack: {stack_str}",
            memory_type="knowledge",
            scope_id=scope_id,
            tags=["tech-stack", project_name] + [s.lower().split("/")[0] for s in detected_stack],
        )
        memories.append(mem)

    # 3. Dependencies
    pyproject = os.path.join(cwd, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            content = open(pyproject).read()
            deps_match = re.findall(r'"([a-zA-Z][a-zA-Z0-9_-]+)', content)
            key_deps = [d for d in set(deps_match) if len(d) > 2][:15]
            if key_deps:
                mem = _make_memory(
                    content=f"[{project_name}] Key dependencies: {', '.join(sorted(key_deps))}",
                    title=f"[{project_name}] Project dependencies",
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["dependencies", project_name],
                )
                memories.append(mem)
        except OSError:
            pass

    pkg_json = os.path.join(cwd, "package.json")
    if os.path.exists(pkg_json):
        try:
            data = json.loads(open(pkg_json).read())
            all_deps = list(data.get("dependencies", {}).keys()) + list(data.get("devDependencies", {}).keys())
            if all_deps:
                mem = _make_memory(
                    content=f"[{project_name}] Key dependencies: {', '.join(sorted(all_deps[:15]))}",
                    title=f"[{project_name}] Project dependencies",
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["dependencies", project_name],
                )
                memories.append(mem)
        except (OSError, json.JSONDecodeError):
            pass

    # 4. Directory structure
    top_dirs = []
    try:
        for entry in sorted(os.listdir(cwd)):
            full = os.path.join(cwd, entry)
            if os.path.isdir(full) and not entry.startswith(".") and entry not in (
                "node_modules", "__pycache__", "venv", ".venv", "dist", "build", ".git"
            ):
                top_dirs.append(entry)
    except OSError:
        pass

    if top_dirs:
        mem = _make_memory(
            content=f"[{project_name}] Project structure: {', '.join(top_dirs[:15])}",
            title=f"[{project_name}] Directory structure",
            memory_type="knowledge",
            scope_id=scope_id,
            tags=["architecture", "structure", project_name],
        )
        memories.append(mem)

    # 5. Git — recent patterns
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--no-decorate"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            commits = result.stdout.strip()
            mem = _make_memory(
                content=f"[{project_name}] Recent git history:\n{commits}",
                title=f"[{project_name}] Recent development activity",
                memory_type="knowledge",
                scope_id=scope_id,
                tags=["git", "history", project_name],
            )
            memories.append(mem)
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 6. Test framework detection
    test_patterns = {
        "pytest": ["conftest.py", "pytest.ini", "pyproject.toml"],
        "jest": ["jest.config.js", "jest.config.ts"],
        "vitest": ["vitest.config.ts", "vitest.config.js"],
        "mocha": [".mocharc.yml", ".mocharc.json"],
        "cargo test": ["Cargo.toml"],
    }
    for framework, indicators in test_patterns.items():
        for indicator in indicators:
            path = os.path.join(cwd, indicator)
            if os.path.exists(path):
                if framework == "pytest" and indicator == "pyproject.toml":
                    try:
                        if "pytest" not in open(path).read():
                            continue
                    except OSError:
                        continue
                if framework == "cargo test" and indicator == "Cargo.toml":
                    if not os.path.isdir(os.path.join(cwd, "tests")):
                        continue
                mem = _make_memory(
                    content=f"[{project_name}] Test framework: {framework}",
                    title=f"[{project_name}] Uses {framework} for testing",
                    memory_type="convention",
                    scope_id=scope_id,
                    tags=["testing", framework.replace(" ", "-"), project_name],
                )
                memories.append(mem)
                break

    # 7. Starter packs — only include once across all projects
    if include_starter_packs:
        detected_packs = detect_packs(cwd)
        pack_memories = get_pack_memories(detected_packs)
        for title, content, mem_type, tags in pack_memories:
            mem = _make_memory(
                content=content,
                title=title,
                memory_type=mem_type,
                scope_id=scope_id,
                tags=tags + ["starter-pack"],
                source_type="starter_pack",
            )
            mem["confidence"] = 0.7
            mem["promotion_status"] = "learned"
            mem["verified"] = True
            memories.append(mem)

    # Save all
    for mem in memories:
        _save_memory(mem)

    return memories


# ─── MCP Server ──────────────────────────────────────────────────

mcp = FastMCP("cortex")


def _expand_query(query: str) -> str:
    """Use Haiku to expand a search query with synonyms and related terms.
    Falls back to original query if expansion fails."""
    try:
        import subprocess as _sp
        result = _sp.run(
            ["claude", "-p", "--model", "haiku",
             f"Add synonyms, abbreviations, and related terms to this search query. "
             f"Return ONLY the expanded query as a single line, nothing else.\n"
             f"Query: {query}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            expanded = result.stdout.strip()
            # Combine original + expanded for best coverage
            return f"{query} {expanded}"
    except Exception:
        pass
    return query


@mcp.tool()
def memory_save(
    content: str,
    title: str = "",
    memory_type: str = "lesson",
    scope_id: str = "default",
    tags: str = "",
) -> str:
    """Save a piece of knowledge to memory.
    Use after discovering patterns, conventions, lessons, or failures.
    Tags should be comma-separated."""

    domain_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    effective_title = title or content[:60]

    # Check for duplicates before creating a new memory
    existing = _is_duplicate(content, scope_id=scope_id, return_match=True)
    if existing:
        existing_title = existing.get("title", "unknown")[:60]
        return f"Memory already exists (similar to: \"{existing_title}\"). Not saved."

    mem = _make_memory(content=content, title=effective_title, memory_type=memory_type,
                       scope_id=scope_id, tags=domain_tags, source_type="user",
                       confidence=0.5)

    # Check for contradictions
    contra_ids = _check_contradictions(mem, scope_id=scope_id)
    if contra_ids:
        mem["contradicts"] = contra_ids
        # Also update the contradicted memories to point back
        for contra_id in contra_ids:
            contra_mem = _load_memory(contra_id)
            if contra_mem:
                existing_contras = contra_mem.get("contradicts", [])
                if mem["id"] not in existing_contras:
                    existing_contras.append(mem["id"])
                    contra_mem["contradicts"] = existing_contras
                    _save_memory(contra_mem)

    _save_memory(mem)

    # Pre-compute embedding for faster future searches
    try:
        from embeddings import is_available, get_or_compute_embedding
        if is_available():
            get_or_compute_embedding(mem["id"], content)
    except Exception:
        pass

    return f"Memory saved: {mem['id'][:8]}... \"{title or content[:60]}\""


@mcp.tool()
def memory_recall(query: str, scope_id: str = "default", limit: int = 10) -> str:
    """Search all memory sources for a query. Returns structured markdown.

    Pipeline: expand query (Haiku) → search memories (vector+keyword) →
    search raw transcripts (live JSONL) → format as markdown → return.

    Use this for any memory retrieval — session start, mid-conversation,
    or explicit search. One tool for all recall needs."""

    # 1. Expand query with Haiku for better semantic coverage
    expanded = _expand_query(query)

    # 2. Search saved memories (all types)
    memories = _search_memories(expanded, scope_id=scope_id, limit=limit)

    # 3. Search raw session transcripts (live JSONL files)
    transcript_results = transcript_search(expanded, limit=3)

    if not memories and ("No matching" in transcript_results or not transcript_results):
        return f"No memories found for: {query}"

    # 4. Format as structured markdown
    sections = []

    if memories:
        by_source = {}
        for m in memories:
            src = m.get("source_type", "other")
            by_source.setdefault(src, []).append(m)

        lines = []
        for src_label, src_key in [("From past sessions", "mined"),
                                    ("Project knowledge", "auto_seed"),
                                    ("Saved knowledge", "user"),
                                    ("Best practices", "starter_pack"),
                                    ("Imported", "import")]:
            mems = by_source.get(src_key, [])
            if mems:
                lines.append(f"### {src_label}")
                for m in mems:
                    lines.append(_format_memory_as_bullet(m))
                lines.append("")

        sections.append("\n".join(lines))

    if transcript_results and "No matching" not in transcript_results:
        sections.append(f"### Related Session Logs\n\n{transcript_results}")

    # 5. Update retrieval counts
    for mem in memories:
        mem["retrieval_count"] = mem.get("retrieval_count", 0) + 1
        mem["last_retrieved_at"] = _now()
        _save_memory(mem)

    # Log retrieval
    log_entry = {
        "id": str(uuid.uuid4()),
        "goal": query,
        "memory_ids": [m["id"] for m in memories],
        "scope_id": scope_id,
        "timestamp": _now(),
    }
    log_path = LOGS_DIR / f"retrieval-{log_entry['id']}.json"
    log_path.write_text(json.dumps(log_entry, indent=2))

    return "\n".join(sections) if sections else f"No memories found for: {query}"


@mcp.tool()
def memory_list(scope_id: str = "default") -> str:
    """List all memories in a scope."""

    memories = _all_memories(scope_id)
    if not memories:
        return f"No memories in scope: {scope_id}"

    # Sort by confidence descending
    memories.sort(key=lambda m: float(m.get("confidence", 0)), reverse=True)

    lines = [f"Memories in scope '{scope_id}': {len(memories)}\n"]
    for mem in memories:
        status = mem.get("promotion_status", "candidate")
        confidence = float(mem.get("confidence", 0.5))
        impact = float(mem.get("impact_score", 0.0))
        title = mem.get("title", "Untitled")
        mem_id = mem.get("id", "")[:8]
        retrievals = mem.get("retrieval_count", 0)
        lines.append(f"- [{mem_id}] **{title}** | {status} | conf:{confidence:.1f} | impact:{impact:.2f} | retrieved:{retrievals}x")

    return "\n".join(lines)


@mcp.tool()
def memory_feedback(memory_ids: str, approved: bool) -> str:
    """Report whether recalled memories were useful.
    memory_ids: comma-separated memory IDs (first 8 chars or full).
    approved: true if the output was good, false if bad."""

    ids = [mid.strip() for mid in memory_ids.split(",") if mid.strip()]
    updated = 0

    for target_id in ids:
        # Try direct load first (full UUID), then prefix match
        mem = _load_memory(target_id)
        if not mem:
            # Prefix match — require at least 8 chars to prevent broad matches
            if len(target_id) < 8:
                continue
            for f in MEMORIES_DIR.glob("*.json"):
                if f.stem.startswith(target_id):
                    mem = _load_memory(f.stem)
                    break
        if not mem:
            continue

        if approved:
            mem["confidence"] = min(1.0, float(mem.get("confidence", 0.5)) + 0.05)
            mem["success_count"] = mem.get("success_count", 0) + 1
        else:
            mem["confidence"] = max(0.0, float(mem.get("confidence", 0.5)) - 0.1)
            mem["failure_count"] = mem.get("failure_count", 0) + 1

        # Recalculate impact
        s = mem.get("success_count", 0)
        f = mem.get("failure_count", 0)
        mem["impact_score"] = s / max(1, s + f)
        mem["last_validated_at"] = _now()

        # Auto-promote check
        if (mem.get("promotion_status") == "candidate"
                and mem.get("retrieval_count", 0) >= 5
                and mem.get("impact_score", 0) > 0.7):
            mem["promotion_status"] = "learned"
            mem["verified"] = True

        _save_memory(mem)
        updated += 1

    return f"Updated {updated} memories (approved={approved})"


@mcp.tool()
def memory_stats(scope_id: str = "default") -> str:
    """Show memory system statistics."""

    memories = _all_memories(scope_id)
    total = len(memories)
    learned = sum(1 for m in memories if m.get("promotion_status") == "learned")
    candidates = sum(1 for m in memories if m.get("promotion_status") == "candidate")

    confidences = [float(m.get("confidence", 0.5)) for m in memories]
    avg_conf = sum(confidences) / max(len(confidences), 1)

    impacts = [float(m.get("impact_score", 0.0)) for m in memories]
    avg_impact = sum(impacts) / max(len(impacts), 1)

    total_retrievals = sum(m.get("retrieval_count", 0) for m in memories)

    # Domain coverage
    domain_counts: dict[str, int] = {}
    for mem in memories:
        for tag in mem.get("domain_tags", []):
            domain_counts[tag] = domain_counts.get(tag, 0) + 1

    lines = [
        f"**Memory Stats** (scope: {scope_id})\n",
        f"- Total: {total}",
        f"- Learned: {learned}",
        f"- Candidates: {candidates}",
        f"- Avg confidence: {avg_conf:.2f}",
        f"- Avg impact: {avg_impact:.2f}",
        f"- Total retrievals: {total_retrievals}",
    ]

    if domain_counts:
        lines.append(f"\n**Domains:** {', '.join(f'{k}({v})' for k, v in sorted(domain_counts.items(), key=lambda x: -x[1]))}")

    return "\n".join(lines)


@mcp.tool()
def memory_promote(memory_id: str) -> str:
    """Promote a candidate memory to learned status.
    memory_id: first 8 chars or full ID."""

    mem = _load_memory(memory_id)
    if not mem and len(memory_id) >= 8:
        for f in MEMORIES_DIR.glob("*.json"):
            if f.stem.startswith(memory_id):
                mem = _load_memory(f.stem)
                break
    if not mem:
        return f"Memory not found: {memory_id}"
    if mem.get("promotion_status") == "learned":
        return f"Memory {memory_id} is already learned."
    mem["promotion_status"] = "learned"
    _save_memory(mem)
    return f"Memory promoted to learned: {mem.get('title', memory_id)}"


def _import_chatgpt_export(filepath: Path, scope_id: str) -> int:
    """Import from ChatGPT data export (conversations.json).

    ChatGPT export format:
    [
      {
        "title": "conversation title",
        "mapping": {
          "node_id": {
            "message": {
              "author": {"role": "user"|"assistant"},
              "content": {"parts": ["text"]},
            }
          }
        }
      }
    ]
    """
    try:
        data = json.loads(filepath.read_text(errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(data, list):
        return 0

    count = 0
    for convo in data:
        title = convo.get("title", "Untitled conversation")
        mapping = convo.get("mapping", {})

        # Extract assistant messages (the useful content)
        assistant_parts = []
        for node_id, node in mapping.items():
            msg = node.get("message")
            if not msg:
                continue
            author = msg.get("author", {}).get("role", "")
            if author != "assistant":
                continue
            content = msg.get("content", {})
            parts = content.get("parts", [])
            text = " ".join(str(p) for p in parts if isinstance(p, str))
            if text and len(text) > 50:
                assistant_parts.append(text)

        if not assistant_parts:
            continue

        # Combine assistant messages, take key parts
        combined = "\n\n".join(assistant_parts)

        # Extract meaningful chunks (split by paragraphs, take substantive ones)
        paragraphs = re.split(r'\n\n+', combined)
        substantive = [p.strip() for p in paragraphs if len(p.strip()) > 100]

        # Save up to 3 memories per conversation
        for i, para in enumerate(substantive[:3]):
            mem = _make_memory(
                content=para[:2000],
                title=f"{title} (part {i+1})" if len(substantive) > 1 else title,
                memory_type="knowledge",
                scope_id=scope_id,
                tags=["imported", "chatgpt"],
                source_type="import",
            )
            _save_memory(mem)
            count += 1

    return count


def _import_claude_export(filepath: Path, scope_id: str) -> int:
    """Import from Claude conversation export (JSON lines or JSON array).

    Claude exports vary but typically contain messages with role and content.
    """
    try:
        content = filepath.read_text(errors="ignore")
    except OSError:
        return 0

    # Try JSON array first
    messages = []
    try:
        data = json.loads(content)
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = data.get("messages", data.get("chat_messages", []))
    except json.JSONDecodeError:
        # Try JSON lines
        for line in content.strip().split("\n"):
            try:
                msg = json.loads(line)
                messages.append(msg)
            except json.JSONDecodeError:
                continue

    count = 0
    for msg in messages:
        role = msg.get("role", msg.get("sender", ""))
        text = msg.get("content", msg.get("text", ""))

        if isinstance(text, list):
            text = " ".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in text)

        if role not in ("assistant", "claude") or not text or len(text) < 100:
            continue

        # Extract substantive paragraphs
        paragraphs = re.split(r'\n\n+', text)
        substantive = [p.strip() for p in paragraphs if len(p.strip()) > 100]

        for para in substantive[:2]:
            mem = _make_memory(
                content=para[:2000],
                title=para[:60],
                memory_type="knowledge",
                scope_id=scope_id,
                tags=["imported", "claude-export"],
                source_type="import",
            )
            _save_memory(mem)
            count += 1

    return count


def _import_chat_log(filepath: Path, scope_id: str) -> int:
    """Import from generic chat logs (plain text with speaker labels).

    Handles formats like:
    User: some question
    Assistant: some response

    Or timestamped:
    [2026-01-15 14:30] User: question
    [2026-01-15 14:31] Assistant: response
    """
    try:
        content = filepath.read_text(errors="ignore")
    except OSError:
        return 0

    # Split by speaker turns
    turns = re.split(r'\n(?=(?:User|Assistant|Human|AI|Claude|ChatGPT|System)\s*:)', content, flags=re.IGNORECASE)

    count = 0
    for turn in turns:
        # Only keep assistant/AI responses
        if not re.match(r'(?:Assistant|AI|Claude|ChatGPT)\s*:', turn, re.IGNORECASE):
            continue

        # Remove the speaker label
        text = re.sub(r'^(?:Assistant|AI|Claude|ChatGPT)\s*:\s*', '', turn, flags=re.IGNORECASE).strip()

        if len(text) < 100:
            continue

        mem = _make_memory(
            content=text[:2000],
            title=text[:60],
            memory_type="knowledge",
            scope_id=scope_id,
            tags=["imported", "chat-log"],
            source_type="import",
        )
        _save_memory(mem)
        count += 1

    return count


@mcp.tool()
def memory_import(source_path: str, scope_id: str = "default") -> str:
    """Import knowledge from files or directories into memory.

    Supports:
    - Markdown files (.md) — each heading becomes a memory
    - JSON files (.json) — each key-value or array item becomes a memory
    - Text files (.txt) — entire file becomes one memory
    - Directories — recursively imports all supported files

    For chat exports (ChatGPT conversations.json, Claude exports, chat logs):
    Returns the conversation content for YOU (Claude) to extract knowledge from.
    Read the returned content, identify decisions/lessons/facts/conventions,
    and call memory_save for each piece of knowledge you extract.

    Great for importing from Obsidian vaults, note directories, or documentation."""

    source = Path(source_path).expanduser().resolve()
    home = Path.home().resolve()
    if not str(source).startswith(str(home)):
        return f"Access denied: can only import from within home directory"
    if not source.exists():
        return f"Path not found: {source_path}"

    # Check if this is a chat export — return content for Claude to extract
    if source.is_file():
        content = ""
        try:
            content = source.read_text(errors="ignore")
        except OSError:
            return f"Cannot read: {source_path}"

        name = source.name.lower()
        ext = source.suffix.lower()

        is_chat = False
        if name == "conversations.json" or (ext == ".json" and '"mapping"' in content[:1000]):
            is_chat = True
        elif ext == ".json" and ('"role"' in content[:1000] or '"sender"' in content[:1000]):
            is_chat = True
        elif ext in (".txt", ".log") and re.search(r'(?:User|Human|Assistant|Claude)\s*:', content[:500], re.IGNORECASE):
            is_chat = True

        if is_chat:
            # Return content for Claude to extract knowledge from
            # Truncate to reasonable size
            preview = content[:10000]
            return (
                f"CHAT EXPORT DETECTED: {source_path}\n"
                f"Size: {len(content)} chars\n\n"
                f"Read through this content and extract knowledge. "
                f"For each decision, lesson, convention, fact, or preference you find, "
                f"call memory_save with atomic, self-contained content.\n\n"
                f"--- CONTENT ---\n{preview}"
                + ("\n\n[truncated — file continues...]" if len(content) > 10000 else "")
            )

    # Non-chat files: import directly (markdown, JSON, text)
    imported = 0

    if source.is_file():
        imported = _import_file(source, scope_id)
    elif source.is_dir():
        chat_files = []
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                      and d not in ("node_modules", "__pycache__", ".git", "venv")]
            for fname in sorted(files):
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                imported += _import_file(fpath, scope_id)

    result = f"Imported {imported} memories from {source_path}"
    return result


def _import_file(fpath: Path, scope_id: str) -> int:
    """Import a single file into memory. Returns count of memories created."""
    ext = fpath.suffix.lower()
    name = fpath.name.lower()

    try:
        content = fpath.read_text(errors="ignore")
    except OSError:
        return 0

    if not content.strip():
        return 0

    # Detect ChatGPT export
    if name == "conversations.json" or (ext == ".json" and '"mapping"' in content[:1000]):
        return _import_chatgpt_export(fpath, scope_id)

    # Detect Claude/chat JSON export
    if ext == ".json" and ('"role"' in content[:1000] or '"sender"' in content[:1000]):
        return _import_claude_export(fpath, scope_id)

    # Detect plain text chat log
    if ext in (".txt", ".log") and re.search(r'(?:User|Human|Assistant|Claude)\s*:', content[:500], re.IGNORECASE):
        return _import_chat_log(fpath, scope_id)

    count = 0
    rel_name = fpath.name

    if ext in (".md", ".markdown"):
        count = _import_markdown(content, rel_name, scope_id)
    elif ext == ".json":
        count = _import_json(content, rel_name, scope_id)
    elif ext in (".txt", ".text", ".rst"):
        # One memory per file
        if len(content.strip()) > 20:
            mem = _make_memory(
                content=content.strip()[:2000],
                title=fpath.stem,
                memory_type="knowledge",
                scope_id=scope_id,
                tags=["imported", fpath.stem.lower()],
                source_type="import",
            )
            _save_memory(mem)
            count = 1

    return count


def _import_markdown(content: str, filename: str, scope_id: str) -> int:
    """Split markdown by headings, create one memory per section."""
    sections = re.split(r'^(#{1,3}\s+.+)$', content, flags=re.MULTILINE)

    count = 0
    current_title = filename.replace(".md", "")
    current_content = ""

    for part in sections:
        part = part.strip()
        if not part:
            continue
        if re.match(r'^#{1,3}\s+', part):
            # Save previous section
            if current_content.strip() and len(current_content.strip()) > 20:
                mem = _make_memory(
                    content=current_content.strip()[:2000],
                    title=current_title,
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["imported", filename.replace(".md", "").lower()],
                    source_type="import",
                )
                _save_memory(mem)
                count += 1
            current_title = part.lstrip("# ").strip()
            current_content = ""
        else:
            current_content += part + "\n"

    # Last section
    if current_content.strip() and len(current_content.strip()) > 20:
        mem = _make_memory(
            content=current_content.strip()[:2000],
            title=current_title,
            memory_type="knowledge",
            scope_id=scope_id,
            tags=["imported", filename.replace(".md", "").lower()],
            source_type="import",
        )
        _save_memory(mem)
        count += 1

    return count


def _import_json(content: str, filename: str, scope_id: str) -> int:
    """Import JSON — each top-level key or array item becomes a memory."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return 0

    count = 0
    if isinstance(data, dict):
        for key, value in data.items():
            text = f"{key}: {json.dumps(value)}" if not isinstance(value, str) else f"{key}: {value}"
            if len(text) > 20:
                mem = _make_memory(
                    content=text[:2000],
                    title=key,
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["imported", filename.replace(".json", "").lower()],
                    source_type="import",
                )
                _save_memory(mem)
                count += 1
    elif isinstance(data, list):
        for i, item in enumerate(data[:50]):  # cap at 50
            text = json.dumps(item) if not isinstance(item, str) else item
            if len(text) > 20:
                mem = _make_memory(
                    content=text[:2000],
                    title=f"{filename} item {i+1}",
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["imported", filename.replace(".json", "").lower()],
                    source_type="import",
                )
                _save_memory(mem)
                count += 1

    return count


# ─── Session Mining ──────────────────────────────────────────────

MINED_SESSIONS_FILE = CORTEX_DIR / ".mined_sessions"

# Pattern definitions: (regex_pattern, memory_type)
_MINE_PATTERNS = [
    # Decisions
    (re.compile(r"[^.!?]*\b(we decided|chose|went with|the approach is)\b[^.!?]*[.!?]?", re.IGNORECASE), "knowledge"),
    # Lessons
    (re.compile(r"[^.!?]*\b(the issue was|fixed by|turns out|the problem was|root cause)\b[^.!?]*[.!?]?", re.IGNORECASE), "lesson"),
    # Conventions
    (re.compile(r"[^.!?]*\b(this project uses|always use|never use|the convention is)\b[^.!?]*[.!?]?", re.IGNORECASE), "convention"),
    # Preferences
    (re.compile(r"[^.!?]*\b(I prefer|do not do|stop doing|user wants|user prefers)\b[^.!?]*[.!?]?", re.IGNORECASE), "preference"),
    # Knowledge/facts
    (re.compile(r"[^.!?]*\b(depends on|requires|is configured at|stored in)\b[^.!?]*[.!?]?", re.IGNORECASE), "knowledge"),
]

# Patterns for explicit user preference signals
_USER_PREF_PATTERNS = [
    re.compile(r"[^.!?]*\b(don't do|i want|always do|never do|please don't|stop doing)\b[^.!?]*[.!?]?", re.IGNORECASE),
]


def _was_mined(session_id: str) -> bool:
    """Check if a session has already been mined."""
    if not MINED_SESSIONS_FILE.exists():
        return False
    for line in MINED_SESSIONS_FILE.read_text().splitlines():
        if line.split("\t")[0].strip() == session_id:
            return True
    return False


def _mark_mined(session_id: str):
    """Append session_id + timestamp to the mined sessions file."""
    with open(MINED_SESSIONS_FILE, "a") as f:
        f.write(f"{session_id}\t{_now()}\n")


def _extract_text_from_content(content, role: str) -> str:
    """Extract plain text from a message content field (str or list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text.strip())
            elif btype == "tool_result":
                # skip tool_result blocks in user messages
                continue
            # skip tool_use, thinking, etc.
        return "\n".join(parts).strip()
    return ""


def _parse_jsonl_session(jsonl_path: str) -> list[dict]:
    """Parse a JSONL session file and return a list of exchange pairs."""
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
            content = obj.get("message", {}).get("content", "")
            text = _extract_text_from_content(content, msg_type)
            if text:
                messages.append({"role": msg_type, "text": text})

    # Pair consecutive user + assistant messages
    pairs = []
    i = 0
    while i < len(messages):
        if messages[i]["role"] == "user":
            user_text = messages[i]["text"]
            # Look ahead for assistant response
            if i + 1 < len(messages) and messages[i + 1]["role"] == "assistant":
                assistant_text = messages[i + 1]["text"]
                pairs.append({"user_text": user_text, "assistant_text": assistant_text})
                i += 2
            else:
                i += 1
        else:
            i += 1
    return pairs


def _extract_insights_from_pair(pair: dict) -> list[dict]:
    """Extract heuristic insights from an exchange pair using pattern matching."""
    insights = []
    assistant_text = pair["assistant_text"]
    user_text = pair["user_text"]

    def _clean_text(text: str) -> str:
        """Strip code blocks, paths, and other noisy content from text."""
        # Strip code fences
        clean = re.sub(r'```[\s\S]*?```', '', text)
        # Strip inline code
        clean = re.sub(r'`[^`]+`', '', clean)
        # Strip markdown table rows (lines containing |)
        clean = re.sub(r'^.*\|.*\|.*$', '', clean, flags=re.MULTILINE)
        # Strip command output lines (starting with $ or >)
        clean = re.sub(r'^[\$>].*$', '', clean, flags=re.MULTILINE)
        # Strip lines with filesystem paths
        clean = re.sub(r'^.*(?:/home/|/usr/|/etc/|/tmp/|/var/).*$', '', clean, flags=re.MULTILINE)
        return clean

    clean_assistant = _clean_text(assistant_text)
    clean_user = _clean_text(user_text)

    # Scan assistant text for saveable insights
    for pattern, memory_type in _MINE_PATTERNS:
        for match in pattern.finditer(clean_assistant):
            snippet = match.group(0).strip()
            # Minimum 40 chars for assistant insights
            if len(snippet) < 40:
                continue
            # Maximum 500 chars
            snippet = snippet[:500]
            # Require at least 60% alpha characters (filters code/paths)
            alpha_ratio = sum(c.isalpha() or c.isspace() for c in snippet) / max(len(snippet), 1)
            if alpha_ratio < 0.6:
                continue
            insights.append({"content": snippet, "memory_type": memory_type})

    # Check user text for explicit preference signals
    for pattern in _USER_PREF_PATTERNS:
        for match in pattern.finditer(clean_user):
            snippet = match.group(0).strip()
            if len(snippet) > 10:
                insights.append({"content": snippet, "memory_type": "preference"})

    # Deduplicate by content
    seen = set()
    unique = []
    for ins in insights:
        key = ins["content"][:80]
        if key not in seen:
            seen.add(key)
            unique.append(ins)
    return unique


def mine_session(jsonl_path: str) -> dict:
    """Mine a single JSONL session file and save insights as Cortex memories."""
    import sys as _sys

    path = Path(jsonl_path)
    if not path.exists():
        return {"skipped": True, "reason": "file not found", "memories_saved": 0, "chunks_processed": 0}

    # Extract session_id from filename (UUID before .jsonl)
    session_id = path.stem  # e.g. "eeb998a8-2d76-45ce-b277-c1961844333d"

    if _was_mined(session_id):
        return {"skipped": True, "reason": "already mined"}

    pairs = _parse_jsonl_session(jsonl_path)
    memories_saved = 0
    duplicates_skipped = 0

    for pair in pairs:
        insights = _extract_insights_from_pair(pair)
        for ins in insights:
            content = ins["content"]
            memory_type = ins["memory_type"]
            if _is_duplicate(content[:2000]):
                duplicates_skipped += 1
                continue
            mem = _make_memory(content=content[:2000], title=content[:80],
                               memory_type=memory_type, source_type="mined",
                               confidence=0.4, tags=["mined", session_id[:8]],
                               source_session_id=session_id,
                               full_record=f"User: {pair['user_text'][:500]}\n\nAssistant: {pair['assistant_text'][:500]}")
            _save_memory(mem)
            memories_saved += 1

    _mark_mined(session_id)
    return {
        "session_id": session_id,
        "chunks_processed": len(pairs),
        "memories_saved": memories_saved,
        "duplicates_skipped": duplicates_skipped,
        "skipped": False,
    }


def mine_all() -> dict:
    """Discover and mine all unmined Claude Code JSONL session files."""
    base_dir = Path.home() / ".claude" / "projects"
    total_sessions = 0
    newly_mined = 0
    already_mined = 0
    total_memories_saved = 0

    if base_dir.exists():
        for path in base_dir.rglob("*.jsonl"):
            if "/subagents/" in str(path):
                continue
            total_sessions += 1
            result = mine_session(str(path))
            if result.get("skipped"):
                already_mined += 1
            else:
                newly_mined += 1
                total_memories_saved += result.get("memories_saved", 0)

    return {
        "total_sessions": total_sessions,
        "newly_mined": newly_mined,
        "already_mined": already_mined,
        "total_memories_saved": total_memories_saved,
    }


# ─── Transcript Search ───────────────────────────────────────────

@mcp.tool()
def transcript_search(query: str, limit: int = 5) -> str:
    """Search raw Claude Code JSONL session files still on disk.

    Discovers all JSONL files in ~/.claude/projects/ (excluding subagents/ dirs)
    and searches across user+assistant exchange pairs using keyword overlap scoring.

    Returns the top matching exchanges showing the user message, assistant response,
    session filename, and relevance score."""

    base_dir = Path.home() / ".claude" / "projects"
    if not base_dir.exists():
        return "No matching transcripts found"

    query_lower = query.lower()
    query_words = set(query_lower.split())
    if not query_words:
        return "No matching transcripts found"

    scored = []
    for jsonl_path in base_dir.rglob("*.jsonl"):
        if "/subagents/" in str(jsonl_path):
            continue
        try:
            pairs = _parse_jsonl_session(str(jsonl_path))
        except (OSError, Exception):
            continue
        for pair in pairs:
            exchange_text = (pair["user_text"] + " " + pair["assistant_text"]).lower()
            text_words = set(exchange_text.split())
            score = len(query_words & text_words) / len(query_words)
            if score > 0:
                scored.append((score, pair, jsonl_path.name))

    if not scored:
        return "No matching transcripts found"

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    lines = []
    for score, pair, filename in top:
        user_msg = pair["user_text"][:300]
        asst_msg = pair["assistant_text"][:500]
        lines.append(
            f"- **Q:** {user_msg}\n"
            f"  **A:** {asst_msg}\n"
            f"  *Session: {filename}*"
        )

    return "\n\n".join(lines)


# ─── Garbage Collection ──────────────────────────────────────────

def garbage_collect() -> dict:
    """Decay stale low-value memories, merge near-duplicates, prune excess."""
    decayed = 0
    merged = 0
    pruned = 0
    deleted_ids: set[str] = set()
    now = datetime.now(timezone.utc)
    thirty_days_ago = now.timestamp() - 30 * 86400

    # 1. DECAY — delete stale low-value memories
    for mem in _all_memories():
        if mem["id"] in deleted_ids:
            continue
        confidence = float(mem.get("confidence", 0.5))
        retrieval_count = int(mem.get("retrieval_count", 0))
        created_at_str = mem.get("created_at")
        if confidence < 0.3 and retrieval_count == 0 and created_at_str:
            try:
                created_dt = datetime.fromisoformat(created_at_str)
                if created_dt.timestamp() < thirty_days_ago:
                    if _delete_memory(mem["id"]):
                        deleted_ids.add(mem["id"])
                        decayed += 1
            except (ValueError, TypeError):
                pass

    # 2. MERGE — combine near-duplicates (>90% word overlap)
    remaining_mems = [m for m in _all_memories() if m["id"] not in deleted_ids]
    for i in range(len(remaining_mems)):
        mem_a = remaining_mems[i]
        if mem_a["id"] in deleted_ids:
            continue
        for j in range(i + 1, len(remaining_mems)):
            mem_b = remaining_mems[j]
            if mem_b["id"] in deleted_ids:
                continue
            text_a = mem_a.get("essence", "")
            text_b = mem_b.get("essence", "")
            if not text_a or not text_b:
                continue
            words_a = set(text_a.lower().split())
            words_b = set(text_b.lower().split())
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
            if overlap > 0.9:
                # Decide which to keep
                conf_a = float(mem_a.get("confidence", 0.5))
                conf_b = float(mem_b.get("confidence", 0.5))
                if conf_a > conf_b:
                    to_delete = mem_b
                elif conf_b > conf_a:
                    to_delete = mem_a
                else:
                    # Equal confidence — keep newer one (later created_at)
                    try:
                        dt_a = datetime.fromisoformat(mem_a.get("created_at", ""))
                        dt_b = datetime.fromisoformat(mem_b.get("created_at", ""))
                        to_delete = mem_a if dt_b >= dt_a else mem_b
                    except (ValueError, TypeError):
                        to_delete = mem_b
                if to_delete["id"] not in deleted_ids:
                    if _delete_memory(to_delete["id"]):
                        deleted_ids.add(to_delete["id"])
                        merged += 1

    # 3. PRUNE — enforce max count of 500
    remaining_mems = [m for m in _all_memories() if m["id"] not in deleted_ids]
    if len(remaining_mems) > 500:
        def _score(m: dict) -> float:
            confidence = float(m.get("confidence", 0.5))
            impact = float(m.get("impact_score", 0.0))
            retrieval = min(int(m.get("retrieval_count", 0)) / 10, 0.2)
            return confidence * 0.5 + impact * 0.3 + retrieval

        pruneable = [m for m in remaining_mems if m.get("promotion_status") != "learned"]
        pruneable.sort(key=_score)
        excess = len(remaining_mems) - 500
        for mem in pruneable[:excess]:
            if mem["id"] not in deleted_ids:
                if _delete_memory(mem["id"]):
                    deleted_ids.add(mem["id"])
                    pruned += 1

    remaining = len([m for m in _all_memories() if m["id"] not in deleted_ids])
    return {"decayed": decayed, "merged": merged, "pruned": pruned, "remaining": remaining}


# ─── Run ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) >= 3 and _sys.argv[1] == "--mine-session":
        result = mine_session(_sys.argv[2])
        print(json.dumps(result))
        _sys.exit(0)

    elif len(_sys.argv) >= 2 and _sys.argv[1] == "--mine-all":
        result = mine_all()
        print(json.dumps(result))
        _sys.exit(0)

    elif len(_sys.argv) >= 2 and _sys.argv[1] == "--purge-mined":
        deleted = 0
        for mem_file in MEMORIES_DIR.glob("*.json"):
            try:
                mem = json.loads(mem_file.read_text())
                if mem.get("source_type") == "mined":
                    mem_file.unlink()
                    deleted += 1
            except Exception:
                pass
        if MINED_SESSIONS_FILE.exists():
            MINED_SESSIONS_FILE.write_text("")
        print(json.dumps({"deleted": deleted}))
        _sys.exit(0)

    elif len(_sys.argv) >= 2 and _sys.argv[1] == "--fix-schema":
        migrated = 0
        fields_removed = 0
        tiers_fixed = 0
        dead_fields = {"associations", "scope_type", "human_approved"}
        for mem_file in MEMORIES_DIR.glob("*.json"):
            try:
                mem = json.loads(mem_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            changed = False
            # Remove dead fields
            for field in dead_fields:
                if field in mem:
                    del mem[field]
                    fields_removed += 1
                    changed = True
            # Normalize full_record: if equals essence, set to None
            if mem.get("full_record") == mem.get("essence"):
                mem["full_record"] = None
                changed = True
            # Fix missing tier
            if "tier" not in mem:
                mem["tier"] = _assign_tier(mem)
                tiers_fixed += 1
                changed = True
            if changed:
                mem_file.write_text(json.dumps(mem, indent=2, default=str))
                migrated += 1
        print(json.dumps({"migrated": migrated, "fields_removed": fields_removed, "tiers_fixed": tiers_fixed}))
        _sys.exit(0)

    elif len(_sys.argv) >= 2 and _sys.argv[1] == "--install-cron":
        import subprocess as _subprocess
        _cron_script = str(Path(__file__).resolve().parent / "mine-cron.sh")
        _cron_entry = f"0 * * * * bash {_cron_script}"
        _result = _subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        _existing = _result.stdout if _result.returncode == 0 else ""
        if "mine-cron.sh" in _existing:
            print("Cron entry already present — no changes made.")
        else:
            _new_crontab = _existing.rstrip("\n") + ("\n" if _existing else "") + _cron_entry + "\n"
            _subprocess.run(["crontab", "-"], input=_new_crontab, text=True, check=True)
            print(f"Cron entry installed: {_cron_entry}")
        _sys.exit(0)

    elif len(_sys.argv) >= 2 and _sys.argv[1] == "--gc":
        result = garbage_collect()
        print(json.dumps(result))
        _sys.exit(0)

    # Auto-seed on startup so starter packs are ready before any tool call
    if not _was_seeded("default"):
        _auto_seed_workspace("default")
    mcp.run(transport="stdio")
