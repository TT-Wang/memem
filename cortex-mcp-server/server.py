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
    """Search memories by keyword overlap. Returns sorted by relevance."""
    query_words = set(query.lower().split())
    if not query_words:
        return []

    scored = []
    for mem in _all_memories(scope_id):
        # Search in essence, title, and tags
        text = " ".join([
            mem.get("essence", ""),
            mem.get("title", ""),
            " ".join(mem.get("domain_tags", [])),
        ]).lower()

        text_words = set(text.split())
        overlap = len(query_words & text_words)
        if overlap > 0:
            # Score: overlap * confidence * (learned boost)
            confidence = float(mem.get("confidence", 0.5))
            learned_boost = 1.5 if mem.get("promotion_status") == "learned" else 1.0
            score = (overlap / len(query_words)) * confidence * learned_boost
            scored.append((score, mem))

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
    return f'<context source="memory" relevance="{confidence:.2f}" memory_id="{mem_id}">\n{content}\n</context>'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_memory(content: str, title: str, memory_type: str = "knowledge",
                 scope_id: str = "default", tags: list[str] | None = None,
                 source_type: str = "auto_seed") -> dict:
    """Create a memory dict without saving."""
    return {
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
        "domain_tags": tags or [],
        "associations": [],
        "contradicts": [],
        "source_type": source_type,
        "created_at": _now(),
        "last_retrieved_at": None,
        "last_validated_at": None,
    }


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
    """Assemble relevant context for a goal by searching memory.
    Call this before starting any task to get relevant knowledge.
    Returns formatted context blocks from memory."""

    memories = _search_memories(goal, scope_id=scope_id, limit=limit)

    # Auto-seed on first use — scan the project and create starter memories
    if not memories and not _was_seeded(scope_id):
        seeded = _auto_seed(scope_id)
        if seeded:
            # Re-search after seeding
            memories = _search_memories(goal, scope_id=scope_id, limit=limit)

    if not memories:
        return "No relevant memories found. This is a fresh topic."

    # Format each memory as context block
    blocks = []
    memory_ids = []
    for mem in memories:
        blocks.append(_format_memory_for_context(mem))
        memory_ids.append(mem["id"])

        # Update retrieval count
        mem["retrieval_count"] = mem.get("retrieval_count", 0) + 1
        mem["last_retrieved_at"] = _now()
        _save_memory(mem)

    # Log retrieval for association tracking
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
    return f"Found {len(memories)} relevant memories:\n\n{context}"


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

    mem = {
        "id": memory_id,
        "essence": content,
        "full_record": content,
        "title": title or content[:60],
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
        "domain_tags": domain_tags,
        "associations": [],
        "contradicts": [],
        "source_type": "user",
        "created_at": _now(),
        "last_retrieved_at": None,
        "last_validated_at": None,
    }

    _save_memory(mem)
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


@mcp.tool()
def memory_import(source_path: str, scope_id: str = "default") -> str:
    """Import knowledge from files or directories into memory.

    Supports:
    - Markdown files (.md) — each heading becomes a memory
    - JSON files (.json) — each key-value or array item becomes a memory
    - Text files (.txt) — entire file becomes one memory
    - Directories — recursively imports all supported files

    Great for importing from Obsidian vaults, note directories, or documentation."""

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

    try:
        content = fpath.read_text(errors="ignore")
    except OSError:
        return 0

    if not content.strip():
        return 0

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
