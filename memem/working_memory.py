"""Working-memory surface for memem v1.4.0.

Maintains a live 5-section markdown file at ~/.memem/working_memory.md that
represents the LLM's in-session working state. Updated by post-stop hook;
read by auto-recall hook to prepend live context to every slice injection.

The working_memory.md is completely separate from the static slice — it tracks
what's happening *right now* in the session, not what memories are relevant.
"""

import os
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_state_env = os.environ.get("MEMEM_DIR") or os.environ.get("CORTEX_DIR") or ""
if _state_env:
    MEMEM_DIR = Path(_state_env)
else:
    _new_default = Path.home() / ".memem"
    _legacy_default = Path.home() / ".cortex"
    MEMEM_DIR = (
        _legacy_default
        if (_legacy_default.exists() and not _new_default.exists())
        else _new_default
    )

WORKING_MEMORY_FILE = MEMEM_DIR / "working_memory.md"

ALLOWED_SECTIONS = (
    "current_task",
    "active_hypothesis",
    "last_3_actions",
    "stuck_on",
    "decided_this_session",
)

# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def serialize_to_md(sections: dict[str, str]) -> str:
    """5 sections rendered as ``## section_name\\n\\n<body>\\n\\n``."""
    parts: list[str] = []
    for name in ALLOWED_SECTIONS:
        body = sections.get(name, "")
        parts.append(f"## {name}\n\n{body}\n\n")
    return "".join(parts)


def parse_from_md(text: str) -> dict[str, str]:
    """Inverse of serialize_to_md. Tolerant: missing sections return empty string."""
    result: dict[str, str] = {name: "" for name in ALLOWED_SECTIONS}
    current_section: str | None = None
    body_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            # Flush previous section if it was valid
            if current_section is not None and current_section in result:
                result[current_section] = "\n".join(body_lines).strip()
            current_section = line[3:].strip()
            body_lines = []
        else:
            if current_section is not None:
                body_lines.append(line)

    # Flush the last section
    if current_section is not None and current_section in result:
        result[current_section] = "\n".join(body_lines).strip()

    return result


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def read_working_memory() -> dict[str, str]:
    """Read the working memory file.

    Returns dict mapping section name to body. Empty dict if file missing.
    """
    if not WORKING_MEMORY_FILE.exists():
        return {}
    try:
        text = WORKING_MEMORY_FILE.read_text(encoding="utf-8")
        return parse_from_md(text)
    except OSError:
        return {}


def write_working_memory(sections: dict[str, str]) -> None:
    """Atomic write (tempfile + os.replace). Creates parent dir if missing. Silent on OSError."""
    try:
        WORKING_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        text = serialize_to_md(sections)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=WORKING_MEMORY_FILE.parent,
            prefix=".working_memory.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_path_str, WORKING_MEMORY_FILE)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise
    except OSError:
        pass


def update_section(section: str, body: str) -> None:
    """Read, update one section, write atomically.

    Section must be one of the 5 ALLOWED_SECTIONS. Raises ValueError otherwise.
    """
    if section not in ALLOWED_SECTIONS:
        raise ValueError(
            f"Invalid section {section!r}. Must be one of: {', '.join(ALLOWED_SECTIONS)}"
        )
    current = read_working_memory()
    # Ensure all sections are present (read may return {} if file missing)
    for name in ALLOWED_SECTIONS:
        current.setdefault(name, "")
    current[section] = body
    write_working_memory(current)
