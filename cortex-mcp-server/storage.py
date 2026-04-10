import atexit
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
SERVER_PID_FILE = CORTEX_DIR / "mcp-server.pid"
OBSIDIAN_VAULT = Path(os.environ.get("CORTEX_OBSIDIAN_VAULT", str(Path.home() / "obsidian-brain")))
OBSIDIAN_MEMORIES_DIR = OBSIDIAN_VAULT / "cortex" / "memories"
INDEX_PATH = OBSIDIAN_VAULT / "cortex" / "_index.md"


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


def _cleanup_pid_file(path: Path, pid: int):
    try:
        if path.read_text().strip() == str(pid):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _register_server_pid():
    pid = os.getpid()
    SERVER_PID_FILE.write_text(str(pid))

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


def _make_memory(content: str, title: str, tags: list[str] | None = None,
                 project: str = "general", source_type: str = "user") -> dict:
    normalized_project = (project or "general").strip() or "general"
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "essence": content,
        "domain_tags": tags or [],
        "project": normalized_project,
        "source_type": source_type,
        "created_at": _now(),
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
        f"created: {mem.get('created_at', '')[:10]}\n"
        f"---"
    )

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
    except OSError:
        return None

    body = content.strip()
    mem = {
        "id": _extract_memory_id_from_filename(md_file),
        "title": md_file.stem,
        "project": "general",
        "domain_tags": [],
        "created_at": "",
        "file": str(md_file),
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
                elif key == "created":
                    mem["created_at"] = value

    mem["essence"] = body
    mem["full_record"] = body
    if not mem["title"] or mem["title"] == md_file.stem:
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        mem["title"] = first_line[:120] if first_line else mem["id"]

    if "imported" in mem["domain_tags"]:
        mem["source_type"] = "import"
    elif "mined" in mem["domain_tags"]:
        mem["source_type"] = "mined"
    else:
        mem["source_type"] = "user"

    return mem


def _obsidian_memories(scope_id: str | None = None) -> list[dict]:
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return []

    memories = []
    for md_file in sorted(OBSIDIAN_MEMORIES_DIR.glob("*.md")):
        mem = _parse_obsidian_memory_file(md_file)
        if not mem:
            continue
        project = mem.get("project", "general")
        if scope_id and scope_id != "default" and project != scope_id:
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


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _is_duplicate(content: str, scope_id: str = "default", threshold: float = 0.7,
                  return_match: bool = False) -> dict | bool | None:
    """Check for duplicate via word overlap against Obsidian memories."""
    content_words = _word_set(content)
    if not content_words:
        return None if return_match else False

    project = None if not scope_id or scope_id == "default" else scope_id
    best_score = 0.0
    best_mem = None

    for mem in _obsidian_memories(project):
        mem_words = _word_set(mem.get("essence", "") + " " + mem.get("title", ""))
        if not mem_words:
            continue
        overlap = len(content_words & mem_words)
        union = len(content_words | mem_words)
        score = overlap / union if union else 0
        if score > best_score:
            best_score = score
            best_mem = mem

    if best_score >= threshold:
        return best_mem if return_match else True
    return None if return_match else False


def _save_memory(mem: dict):
    """Save memory to Obsidian vault."""
    _require_obsidian_writable()
    _write_obsidian_memory(mem)
    if INDEX_PATH.exists():
        _append_or_update_index_line(mem)


def _delete_memory(memory_id: str) -> bool:
    try:
        mem = _find_memory(memory_id)
        if mem:
            obsidian_path = Path(mem.get("file", ""))
            if obsidian_path.exists():
                obsidian_path.unlink()
        _remove_index_line(memory_id)
        return True
    except Exception:
        return False


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

    # Recount section totals
    for idx, entry in enumerate(new_lines):
        if entry.startswith("## ") and "(" in entry:
            section_name = entry.split("(")[0].strip().removeprefix("## ").strip()
            count = 0
            for j in range(idx + 1, len(new_lines)):
                if new_lines[j].startswith("## "):
                    break
                if new_lines[j].startswith("- "):
                    count += 1
            noun = "memory" if count == 1 else "memories"
            new_lines[idx] = f"## {section_name} ({count} {noun})"

    total = sum(1 for entry in new_lines if entry.startswith("- "))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for idx, entry in enumerate(new_lines):
        if entry.startswith("Updated:"):
            new_lines[idx] = f"Updated: {today} | Total: {total} memories"
            break

    INDEX_PATH.write_text("\n".join(new_lines))


def _remove_index_line(memory_id: str):
    if not INDEX_PATH.exists():
        return

    mem_id = memory_id[:8]
    lines = INDEX_PATH.read_text().split("\n")
    new_lines = [entry for entry in lines if not (entry.startswith("- ") and f"({mem_id})" in entry)]

    # Recount section totals
    for idx, entry in enumerate(new_lines):
        if entry.startswith("## ") and "(" in entry:
            section_name = entry.split("(")[0].strip().removeprefix("## ").strip()
            count = 0
            for j in range(idx + 1, len(new_lines)):
                if new_lines[j].startswith("## "):
                    break
                if new_lines[j].startswith("- "):
                    count += 1
            noun = "memory" if count == 1 else "memories"
            new_lines[idx] = f"## {section_name} ({count} {noun})"

    total = sum(1 for entry in new_lines if entry.startswith("- "))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for idx, entry in enumerate(new_lines):
        if entry.startswith("Updated:"):
            new_lines[idx] = f"Updated: {today} | Total: {total} memories"
            break
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
            "why": "discovered",
            "body": mem.get("full_record", mem.get("essence", "")),
        })
    return results


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
