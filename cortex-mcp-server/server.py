#!/usr/bin/env python3
"""
Cortex MCP Server — persistent memory and context assembly for Claude Code.

Stores memories as JSON files in ~/.cortex/memories/
No database required — works out of the box.

Tools:
  - context_assemble: Assemble relevant context for a goal
  - memory_save: Save a piece of knowledge
  - memory_recall: Search memory by keyword
  - memory_list: List all memories in a scope
  - memory_feedback: Report whether recalled memories helped
  - memory_stats: Show memory system statistics
  - memory_promote: Promote a candidate memory to learned
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
            query_embedding = get_or_compute_embedding(f"query_{hash(query)}", query)
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
        learned_boost = 1.3 if mem.get("promotion_status") == "learned" else 1.0
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

        # Combined score
        if use_vectors:
            # Hybrid: 45% vector, 25% keyword, 10% confidence, 10% impact, 10% recency
            combined = (0.45 * vector_score + 0.25 * keyword_score +
                       0.1 * confidence + 0.1 * impact + 0.1 * recency_boost) * learned_boost
        else:
            # Keyword only: 55% keyword, 20% confidence, 15% impact, 10% recency
            combined = (0.55 * keyword_score + 0.2 * confidence +
                       0.15 * impact + 0.1 * recency_boost) * learned_boost

        # Domain match boost/penalty
        if query_domain and query_domain != "general":
            mem_domain = mem.get("domain", "")
            if mem_domain == query_domain:
                domain_match = 1.4  # 40% boost for same domain
            elif mem_domain == "general" or not mem_domain:
                domain_match = 1.0  # neutral for unclassified
            else:
                domain_match = 0.7  # penalty for different domain
            combined = combined * domain_match

        if combined > 0.05:  # minimum threshold
            scored.append((combined, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def _format_memory_for_context(mem: dict) -> str:
    """Format a memory as a tagged context block."""
    title = mem.get("title", "Untitled")
    mem_type = mem.get("memory_type", "unknown")
    confidence = float(mem.get("confidence", 0.5))
    impact = float(mem.get("impact_score", 0.0))
    essence = mem.get("essence", "")

    content = f"## {title}\n"
    content += f"*Type: {mem_type} | Confidence: {confidence:.1f} | Impact: {impact:.2f}*\n\n"
    content += essence

    tags = mem.get("domain_tags", [])
    if tags:
        content += f"\n\nTags: {', '.join(tags)}"

    mem_id = mem.get("id", "unknown")
    tier = mem.get("tier", "L2")
    return f'<context source="memory" relevance="{confidence:.2f}" memory_id="{mem_id}" tier="{tier}">\n{content}\n</context>'


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
    has_negation = bool(new_words & opposition_words)

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

        # High similarity (same topic) + negation = likely contradiction
        if similarity >= threshold and has_negation:
            contradicting_ids.append(mem["id"])

        # Very high similarity but different conclusion (detect by title difference)
        if similarity >= 0.85:
            new_title_words = set(new_mem.get("title", "").lower().split())
            mem_title_words = set(mem.get("title", "").lower().split())
            if new_title_words and mem_title_words:
                title_overlap = len(new_title_words & mem_title_words) / max(len(new_title_words | mem_title_words), 1)
                if title_overlap > 0.5:
                    # Same topic, possibly updated knowledge
                    mem_words_set = set(mem_text.lower().split())
                    if bool(mem_words_set & opposition_words) or has_negation:
                        contradicting_ids.append(mem["id"])

    return list(set(contradicting_ids))


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
                 source_type: str = "auto_seed") -> dict:
    """Create a memory dict without saving."""
    domain, room = _detect_domain_room(content, title, tags)
    mem = {
        "id": str(uuid.uuid4()),
        "essence": content,
        "full_record": content,
        "title": title,
        "memory_type": memory_type,
        "scope_type": "project",
        "scope_id": scope_id,
        "confidence": 0.6,  # slightly above default — seeded knowledge
        "impact_score": 0.0,
        "success_count": 0,
        "failure_count": 0,
        "retrieval_count": 0,
        "promotion_status": "candidate",
        "verified": False,
        "human_approved": False,
        "domain": domain,
        "room": room,
        "domain_tags": tags or [],
        "associations": [],
        "contradicts": [],
        "source_type": source_type,
        "created_at": _now(),
        "last_retrieved_at": None,
        "last_validated_at": None,
    }
    mem["tier"] = _assign_tier(mem)
    return mem


# ─── Auto-seed ───────────────────────────────────────────────────

SEED_MARKER_FILE = CORTEX_DIR / ".seeded_scopes"


def _was_seeded(scope_id: str) -> bool:
    """Check if a scope has already been seeded."""
    if not SEED_MARKER_FILE.exists():
        return False
    seeded = set(SEED_MARKER_FILE.read_text().strip().split("\n"))
    return scope_id in seeded


def _mark_seeded(scope_id: str):
    """Mark a scope as seeded so we don't re-scan."""
    existing = ""
    if SEED_MARKER_FILE.exists():
        existing = SEED_MARKER_FILE.read_text().strip()
    seeded = set(existing.split("\n")) if existing else set()
    seeded.add(scope_id)
    SEED_MARKER_FILE.write_text("\n".join(sorted(seeded)))


def _auto_seed(scope_id: str, project_dir: str | None = None) -> list[dict]:
    """Scan a project and create starter memories.

    Extracts knowledge from:
    - README.md / CLAUDE.md (project description, conventions)
    - package.json / pyproject.toml / Cargo.toml (tech stack, deps)
    - .gitignore patterns (what the project cares about)
    - git log (recent activity patterns)
    - Directory structure (architecture)
    """
    if _was_seeded(scope_id):
        return []

    cwd = project_dir or os.getcwd()
    if not os.path.isdir(cwd):
        return []

    memories = []

    # 1. README / CLAUDE.md — project description
    for doc_name in ["CLAUDE.md", "README.md", "readme.md"]:
        doc_path = os.path.join(cwd, doc_name)
        if os.path.exists(doc_path):
            try:
                content = open(doc_path).read()[:2000]
                # Extract first heading + first paragraph
                lines = content.split("\n")
                title_line = next((l for l in lines if l.startswith("# ")), "")
                title = title_line.lstrip("# ").strip() or doc_name
                # Get first meaningful paragraph
                paragraphs = re.split(r'\n\n+', content)
                desc = next((p for p in paragraphs if len(p.strip()) > 20
                            and not p.startswith("#")), "")
                if desc:
                    mem = _make_memory(
                        content=f"Project: {title}. {desc.strip()[:500]}",
                        title=f"Project overview: {title}",
                        memory_type="knowledge",
                        scope_id=scope_id,
                        tags=["project", "overview"],
                    )
                    memories.append(mem)
            except OSError:
                pass

    # 2. Tech stack from config files
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
            content=f"Tech stack: {stack_str}",
            title=f"Tech stack: {stack_str}",
            memory_type="knowledge",
            scope_id=scope_id,
            tags=["tech-stack"] + [s.lower().split("/")[0] for s in detected_stack],
        )
        memories.append(mem)

    # 3. Dependencies (extract key deps from config)
    pyproject = os.path.join(cwd, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            content = open(pyproject).read()
            # Extract dependency names
            deps_match = re.findall(r'"([a-zA-Z][a-zA-Z0-9_-]+)', content)
            key_deps = [d for d in set(deps_match) if len(d) > 2][:15]
            if key_deps:
                mem = _make_memory(
                    content=f"Key dependencies: {', '.join(sorted(key_deps))}",
                    title="Project dependencies",
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["dependencies"],
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
                    content=f"Key dependencies: {', '.join(sorted(all_deps[:15]))}",
                    title="Project dependencies",
                    memory_type="knowledge",
                    scope_id=scope_id,
                    tags=["dependencies"],
                )
                memories.append(mem)
        except (OSError, json.JSONDecodeError):
            pass

    # 4. Directory structure — architecture overview
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
            content=f"Project structure: {', '.join(top_dirs[:15])}",
            title="Directory structure",
            memory_type="knowledge",
            scope_id=scope_id,
            tags=["architecture", "structure"],
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
                content=f"Recent git history:\n{commits}",
                title="Recent development activity",
                memory_type="knowledge",
                scope_id=scope_id,
                tags=["git", "history"],
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
                    # Only if tests/ dir exists
                    if not os.path.isdir(os.path.join(cwd, "tests")):
                        continue
                mem = _make_memory(
                    content=f"Test framework: {framework}",
                    title=f"Uses {framework} for testing",
                    memory_type="convention",
                    scope_id=scope_id,
                    tags=["testing", framework.replace(" ", "-")],
                )
                memories.append(mem)
                break

    # 7. Starter packs — universal + language-specific
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
        mem["confidence"] = 0.7  # starter pack knowledge is pre-vetted
        mem["promotion_status"] = "learned"  # start as learned, not candidate
        mem["verified"] = True
        memories.append(mem)

    # Save all and mark seeded
    for mem in memories:
        _save_memory(mem)

    if memories:
        _mark_seeded(scope_id)

    return memories


# ─── MCP Server ──────────────────────────────────────────────────

mcp = FastMCP("cortex")


@mcp.tool()
def context_assemble(goal: str, scope_id: str = "default", limit: int = 10) -> str:
    """Assemble relevant context using memory hierarchy.
    L0+L1 always loaded. L2 loaded by domain match. L3 searched on demand."""

    all_mems = _all_memories(scope_id)

    # Auto-seed if needed (existing logic)
    if not all_mems and not _was_seeded(scope_id):
        seeded = _auto_seed(scope_id)
        if seeded:
            all_mems = _all_memories(scope_id)

    if not all_mems:
        return "No relevant memories found. This is a fresh topic."

    # Reassign tiers (they may have changed since last save)
    for mem in all_mems:
        mem["tier"] = _assign_tier(mem)

    # L0 + L1: Always loaded
    always_load = [m for m in all_mems if m.get("tier") in ("L0", "L1")]

    # L2 + L3: Search for relevant ones
    searched = _search_memories(goal, scope_id=scope_id, limit=limit)
    # Exclude duplicates already in always_load
    always_ids = {m["id"] for m in always_load}
    searched = [m for m in searched if m["id"] not in always_ids]

    # Combine: always_load first, then searched
    final = always_load + searched

    # Update retrieval counts
    blocks = []
    memory_ids = []
    for mem in final:
        blocks.append(_format_memory_for_context(mem))
        memory_ids.append(mem["id"])
        mem["retrieval_count"] = mem.get("retrieval_count", 0) + 1
        mem["last_retrieved_at"] = _now()
        _save_memory(mem)

    # Log retrieval
    log_entry = {
        "id": str(uuid.uuid4()),
        "goal": goal,
        "memory_ids": memory_ids,
        "scope_id": scope_id,
        "timestamp": _now(),
    }
    log_path = LOGS_DIR / f"retrieval-{log_entry['id']}.json"
    log_path.write_text(json.dumps(log_entry, indent=2))

    context = "\n\n".join(blocks)

    # Check for contradictions among retrieved memories
    retrieved_ids = set(memory_ids)
    contradictions = []
    for mem in final:
        contras = mem.get("contradicts", [])
        for contra_id in contras:
            if contra_id in retrieved_ids:
                contradictions.append((mem.get("title", ""), contra_id))

    if contradictions:
        context += "\n\n\u26a0\ufe0f **Contradictions detected:** Some retrieved memories may conflict with each other. Review carefully."

    l0_count = sum(1 for m in final if m.get("tier") == "L0")
    l1_count = sum(1 for m in final if m.get("tier") == "L1")
    l2_count = sum(1 for m in final if m.get("tier") in ("L2", "L3"))

    return f"Context assembled: {len(final)} memories (L0:{l0_count} L1:{l1_count} searched:{l2_count})\n\n{context}"


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

    memory_id = str(uuid.uuid4())
    domain_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    effective_title = title or content[:60]
    domain, room = _detect_domain_room(content, effective_title, domain_tags)

    mem = {
        "id": memory_id,
        "essence": content,
        "full_record": content,
        "title": effective_title,
        "memory_type": memory_type,
        "scope_type": "project",
        "scope_id": scope_id,
        "confidence": 0.5,
        "impact_score": 0.0,
        "success_count": 0,
        "failure_count": 0,
        "retrieval_count": 0,
        "promotion_status": "candidate",
        "verified": False,
        "human_approved": False,
        "domain": domain,
        "room": room,
        "domain_tags": domain_tags,
        "associations": [],
        "contradicts": [],
        "source_type": "user",
        "created_at": _now(),
        "last_retrieved_at": None,
        "last_validated_at": None,
    }

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

    return f"Memory saved: {memory_id[:8]}... \"{title or content[:60]}\""


@mcp.tool()
def memory_recall(query: str, scope_id: str = "default", limit: int = 10) -> str:
    """Search memory by keyword. Returns matching memories with their content."""

    memories = _search_memories(query, scope_id=scope_id, limit=limit)

    if not memories:
        return f"No memories found for: {query}"

    lines = [f"Found {len(memories)} memories:\n"]
    for mem in memories:
        status = mem.get("promotion_status", "candidate")
        confidence = float(mem.get("confidence", 0.5))
        title = mem.get("title", "Untitled")
        essence = mem.get("essence", "")
        mem_id = mem.get("id", "")[:8]
        lines.append(f"**[{mem_id}] {title}** ({status}, conf: {confidence:.1f})")
        lines.append(f"  {essence[:200]}")
        lines.append("")

    return "\n".join(lines)


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

    # Find memories by prefix match
    all_mems = _all_memories()
    for target_id in ids:
        for mem in all_mems:
            if mem["id"].startswith(target_id):
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
                break

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

    for mem in _all_memories():
        if mem["id"].startswith(memory_id):
            if mem.get("promotion_status") == "learned":
                return f"Memory {memory_id} is already learned."
            mem["promotion_status"] = "learned"
            mem["human_approved"] = True
            _save_memory(mem)
            return f"Memory promoted to learned: {mem.get('title', memory_id)}"

    return f"Memory not found: {memory_id}"


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
    - ChatGPT exports (conversations.json) — extracts assistant insights
    - Claude exports (JSON with messages) — extracts assistant responses
    - Chat logs (.txt/.log with speaker labels) — extracts AI responses
    - Directories — recursively imports all supported files

    Great for importing from Obsidian vaults, note directories, chat exports, or documentation."""

    source = Path(source_path).expanduser()
    if not source.exists():
        return f"Path not found: {source_path}"

    imported = 0

    if source.is_file():
        imported = _import_file(source, scope_id)
    elif source.is_dir():
        for root, dirs, files in os.walk(source, followlinks=False):
            # Skip hidden and noise
            dirs[:] = [d for d in dirs if not d.startswith(".")
                      and d not in ("node_modules", "__pycache__", ".git", "venv")]
            for fname in sorted(files):
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                imported += _import_file(fpath, scope_id)

    return f"Imported {imported} memories from {source_path}"


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


# ─── Run ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
