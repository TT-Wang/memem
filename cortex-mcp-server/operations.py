import json
import os
import re
from pathlib import Path

from storage import _is_duplicate, _make_memory, _save_memory


def _normalize_scope_id(scope_id: str) -> str:
    return "general" if not scope_id or scope_id == "default" else scope_id


def memory_save(content: str, title: str = "", scope_id: str = "default", tags: str = "") -> str:
    domain_tags = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else []
    effective_title = title or content[:60]
    project = _normalize_scope_id(scope_id)

    try:
        existing = _is_duplicate(content, scope_id=scope_id, return_match=True)
        if existing:
            return f'Memory already exists (similar to: "{existing.get("title", "")[:60]}"). Not saved.'

        mem = _make_memory(
            content=content,
            title=effective_title,
            tags=domain_tags,
            project=project,
            source_type="user",
        )
        _save_memory(mem)
        return f'Memory saved: {mem["id"][:8]}... "{effective_title}"'
    except RuntimeError as exc:
        return str(exc)


def _import_file(file_path: Path, scope_id: str) -> int:
    try:
        content = file_path.read_text(errors="ignore")
    except OSError:
        return 0
    if not content.strip() or len(content.strip()) < 20:
        return 0

    ext = file_path.suffix.lower()
    count = 0
    project = _normalize_scope_id(scope_id)

    if ext in (".md", ".markdown"):
        sections = re.split(r"^(#{1,3}\s+.+)$", content, flags=re.MULTILINE)
        current_title = file_path.stem
        current_content = ""
        for part in sections:
            part = part.strip()
            if not part:
                continue
            if re.match(r"^#{1,3}\s+", part):
                if current_content.strip() and len(current_content.strip()) > 20:
                    section_content = current_content.strip()[:2000]
                    if not _is_duplicate(section_content, scope_id=scope_id):
                        _save_memory(_make_memory(
                            content=section_content,
                            title=current_title,
                            tags=["imported"],
                            project=project,
                            source_type="import",
                        ))
                        count += 1
                current_title = part.lstrip("# ").strip()
                current_content = ""
            else:
                current_content += part + "\n"
        if current_content.strip() and len(current_content.strip()) > 20:
            section_content = current_content.strip()[:2000]
            if not _is_duplicate(section_content, scope_id=scope_id):
                _save_memory(_make_memory(
                    content=section_content,
                    title=current_title,
                    tags=["imported"],
                    project=project,
                    source_type="import",
                ))
                count += 1
    elif ext == ".json":
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                # Save the whole file as one memory rather than key-by-key noise
                text = json.dumps(data, indent=2)[:2000]
                _save_memory(_make_memory(
                    content=text,
                    title=file_path.stem,
                    tags=["imported"],
                    project=project,
                    source_type="import",
                ))
                count = 1
            elif isinstance(data, list):
                for item in data[:20]:
                    text = json.dumps(item, indent=2) if not isinstance(item, str) else item
                    if len(text) <= 20:
                        continue
                    _save_memory(_make_memory(
                        content=text[:2000],
                        title=str(item.get("title", file_path.stem))[:60] if isinstance(item, dict) else file_path.stem,
                        tags=["imported"],
                        project=project,
                        source_type="import",
                    ))
                    count += 1
        except json.JSONDecodeError:
            pass
    elif ext in (".txt", ".text", ".rst"):
        _save_memory(_make_memory(
            content=content.strip()[:2000],
            title=file_path.stem,
            tags=["imported"],
            project=project,
            source_type="import",
        ))
        count = 1

    return count


def memory_import(source_path: str, scope_id: str = "default") -> str:
    source = Path(source_path).expanduser().resolve()
    if not str(source).startswith(str(Path.home().resolve())):
        return "Access denied: can only import from within home directory"
    if not source.exists():
        return f"Path not found: {source_path}"

    if source.is_file():
        try:
            content = source.read_text(errors="ignore")
        except OSError:
            return f"Cannot read: {source_path}"

        ext = source.suffix.lower()
        if (
            source.name.lower() == "conversations.json"
            or (ext == ".json" and ('"mapping"' in content[:1000] or '"role"' in content[:1000]))
            or (ext in (".txt", ".log") and re.search(r"(?:User|Human|Assistant|Claude)\s*:", content[:500], re.IGNORECASE))
        ):
            preview = content[:10000]
            suffix = "\n\n[truncated...]" if len(content) > 10000 else ""
            return (
                f"CHAT EXPORT DETECTED: {source_path}\nSize: {len(content)} chars\n\n"
                "Read through this and extract knowledge. For each insight, call memory_save.\n\n"
                f"--- CONTENT ---\n{preview}{suffix}"
            )

        return f"Imported {_import_file(source, scope_id)} memories from {source_path}"

    if source.is_dir():
        imported = 0
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".") and name not in ("node_modules", "__pycache__", ".git", "venv")]
            for filename in sorted(files):
                if filename.startswith("."):
                    continue
                imported += _import_file(Path(root) / filename, scope_id)
        return f"Imported {imported} memories from {source_path}"

    return f"Unsupported path: {source_path}"
