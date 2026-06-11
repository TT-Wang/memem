"""Profile documents for always-injected user and project context.

Profiles are stored at <vault>/memem/profiles/ — one level ABOVE memories/.
They are NEVER added to the FTS/embedding search corpus. Unlike memories (which
are retrieved on demand by relevance), profiles have a guaranteed consumer:
SessionStart injection. Every session receives the full profile text before any
memory recall occurs.

Why NOT indexed:
  - Memories are corpus entries chosen by relevance scoring. Profiles are
    authoritative reference documents that are ALWAYS shown — indexing them
    would just pollute retrieval ranking with content the LLM already has.
  - Unlike the v1.x write-only playbooks (which had no guaranteed reader),
    profiles have a concrete consumer: the SessionStart m8 injection path reads
    them via render_for_injection(). Guaranteed delivery removes the need for
    retrieval-time competition.

Schemas (which sections each profile file contains):
  profile_user.md    → ["Preferences", "Conventions", "Environment"]
  profile_<proj>.md  → ["Identity", "Stack & Structure", "Conventions"]

File format: '# <Title>' header, then '## <Section>' blocks of '- ' bullets.

Size cap: _PROFILE_CHAR_CAP = 2500 chars per file. When a write would exceed
the cap, a single Haiku compaction call rewrites the profile to ≤2000 chars
before appending. If compaction fails, the write is rejected ('rejected_full')
rather than silently truncating — data loss is worse than a failed write.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import structlog

from memem.haiku_prompts import HAIKU_TIMEOUT_SECONDS
from memem.io_utils import atomic_write_text
from memem.models import OBSIDIAN_MEMORIES_DIR, _normalize_scope_id
from memem.security import scan_memory_content

log = structlog.get_logger("memem-profiles")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROFILE_CHAR_CAP = 2500  # hard cap per profile file
_RENDER_CAP_PER_FILE = 1200  # max chars contributed per file in render_for_injection
_DEDUP_RATIO_THRESHOLD = 85  # rapidfuzz token_set_ratio ≥ this → duplicate

# Profile directory: sibling of memories/, never inside it.
# OBSIDIAN_MEMORIES_DIR is <vault>/memem/memories; parent is <vault>/memem/.
PROFILES_DIR: Path = OBSIDIAN_MEMORIES_DIR.parent / "profiles"
_PROFILES_LOCK_FILE = PROFILES_DIR.parent / ".profiles.lock"

# ---------------------------------------------------------------------------
# Schema: which sections each profile type supports
# ---------------------------------------------------------------------------

PROFILE_SCHEMAS: dict[str, list[str]] = {
    "user": ["Preferences", "Conventions", "Environment"],
    "_project": ["Identity", "Stack & Structure", "Conventions"],
}


def _sections_for(name: str) -> list[str]:
    """Return the canonical section list for this profile name."""
    return PROFILE_SCHEMAS["user"] if name == "user" else PROFILE_SCHEMAS["_project"]


# ---------------------------------------------------------------------------
# Slug helper
# Minimal local copy — avoids pulling in obsidian_store (heavy import).
# Same regex as obsidian_store._slugify; kept in sync by comment.
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 60) -> str:
    """Convert text to a lowercase hyphen-slug (mirrors obsidian_store._slugify)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _current_profiles_dir() -> Path:
    """Return the profiles dir, re-reading MEMEM_OBSIDIAN_VAULT each call.

    This indirection means monkeypatching the env var in tests is enough to
    redirect all profile I/O without reloading the module.
    """
    vault_env = (
        os.environ.get("MEMEM_OBSIDIAN_VAULT")
        or os.environ.get("CORTEX_OBSIDIAN_VAULT")
        or os.environ.get("MEMEM_VAULT")
        or os.environ.get("CORTEX_VAULT")
    )
    vault = Path(vault_env) if vault_env else Path.home() / "obsidian-brain"

    # Mirror the models.py legacy-subdir logic: if cortex/ exists and memem/ doesn't
    new_sub = vault / "memem"
    legacy_sub = vault / "cortex"
    subdir = legacy_sub if legacy_sub.exists() and not new_sub.exists() else new_sub

    return subdir / "profiles"


def profile_path(name: str) -> Path:
    """Return the Path for a profile by name.

    'user' → profile_user.md
    anything else → profile_<slug>.md (using the same _slugify pattern as
    obsidian_store so project names round-trip cleanly).
    """
    pdir = _current_profiles_dir()
    if name == "user":
        filename = "profile_user.md"
    else:
        slug = _slugify(name)
        filename = f"profile_{slug}.md"
    return pdir / filename


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

def _profile_title(name: str) -> str:
    """Return the display title for a profile by name."""
    return "User Profile" if name == "user" else f"{name.replace('-', ' ').title()} Profile"


def _build_template(name: str) -> str:
    """Build a fresh profile file with the correct title and empty sections."""
    sections = _sections_for(name)
    lines = [f"# {_profile_title(name)}", ""]
    for section in sections:
        lines += [f"## {section}", ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Lock helpers (one lock file for all profiles — low write volume)
# ---------------------------------------------------------------------------

def _acquire_profiles_lock() -> Any:
    """Acquire an exclusive flock over all profile writes. Returns open fd."""
    lock_path = _current_profiles_dir().parent / ".profiles.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")  # noqa: SIM115
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_profiles_lock(fd: Any) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    fd.close()


# ---------------------------------------------------------------------------
# Markdown parser/writer
# ---------------------------------------------------------------------------

def _parse_profile(raw: str, name: str) -> dict[str, list[str]]:
    """Parse a profile markdown string into {section: [bullet_lines]}.

    Only returns sections that belong to this profile's schema. Unknown
    sections are silently ignored (forward-compat).
    """
    valid_sections = {s.lower(): s for s in _sections_for(name)}
    result: dict[str, list[str]] = {s: [] for s in _sections_for(name)}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            canonical = valid_sections.get(heading.lower())
            current = canonical  # may be None for unknown sections
        elif current and line.startswith("- "):
            result[current].append(line[2:].strip())
    return result


def _render_profile(name: str, sections: dict[str, list[str]]) -> str:
    """Render a profile dict back to markdown text."""
    lines = [f"# {_profile_title(name)}", ""]
    for section in _sections_for(name):
        lines.append(f"## {section}")
        for bullet in sections.get(section, []):
            lines.append(f"- {bullet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_profile(name: str) -> dict[str, Any]:
    """Load a profile by name.

    Returns {"sections": {section: [lines]}, "raw": str}.
    For a missing file, sections contains the correct empty schema for this
    profile type.
    """
    path = profile_path(name)
    empty_sections: dict[str, list[str]] = {s: [] for s in _sections_for(name)}
    try:
        if not path.exists():
            return {"sections": empty_sections, "raw": ""}
        raw = path.read_text(encoding="utf-8")
        sections = _parse_profile(raw, name)
        return {"sections": sections, "raw": raw}
    except Exception as exc:  # noqa: BLE001
        log.warning("load_profile failed", name=name, exc=str(exc))
        return {"sections": empty_sections, "raw": ""}


def append_fact(name: str, section: str, line: str) -> str:
    """Append a bullet to a profile section.

    Returns one of:
      'appended'           — success
      'duplicate'          — near-duplicate already present (rapidfuzz ≥85)
      'invalid_section'    — section not in this profile's schema
      'compacted+appended' — cap exceeded; Haiku rewrote profile then appended
      'rejected_full'      — cap exceeded and Haiku compaction failed
      'error'              — security threat or unexpected exception

    Tolerant: NEVER raises to the caller.
    """
    try:
        return _append_fact_inner(name, section, line)
    except Exception as exc:  # noqa: BLE001
        log.warning("append_fact unexpected error", name=name, section=section, exc=str(exc))
        return "error"


def _append_fact_inner(name: str, section: str, line: str) -> str:
    """Inner (may raise) implementation of append_fact."""
    # --- section validation (case-insensitive → canonical) ---
    valid_map = {s.lower(): s for s in _sections_for(name)}
    canonical_section = valid_map.get(section.strip().lower())
    if canonical_section is None:
        return "invalid_section"

    # --- line sanitization ---
    line = line.replace("\n", " ").replace("\r", " ").strip()
    if not line:
        return "error"
    if len(line) > 200:
        # reject rather than silently truncate — callers should send atomic facts
        return "error"

    # --- security scan ---
    threat = scan_memory_content(line)
    if threat:
        log.warning("append_fact blocked by security scan", name=name, threat=threat)
        return "error"

    fd = _acquire_profiles_lock()
    try:
        result = _append_fact_locked(name, canonical_section, line)
    finally:
        _release_profiles_lock(fd)

    if not (isinstance(result, tuple) and result[0] == "__compact__"):
        return result

    # Cap exceeded: compact OUTSIDE the lock — the Haiku call can take up to
    # HAIKU_TIMEOUT_SECONDS and holding the profiles lock across it would
    # block every concurrent PROFILE writer (reconciler, migration) for the
    # duration. We compact a snapshot, then re-acquire and re-validate.
    _, new_raw, snapshot_raw = result
    compacted = _compact_profile(new_raw, name)
    if compacted is None or len(compacted) > _PROFILE_CHAR_CAP:
        log.warning("Haiku compaction failed or oversized, rejecting write", name=name)
        return "rejected_full"

    fd = _acquire_profiles_lock()
    try:
        path = profile_path(name)
        current_raw = path.read_text(encoding="utf-8") if path.exists() else ""
        if current_raw != snapshot_raw:
            # Another writer changed the profile while we compacted — retry
            # the plain append once against the new state (it may fit or
            # dedup now); a second cap trip rejects rather than loops.
            retry = _append_fact_locked(name, canonical_section, line)
            if isinstance(retry, tuple):
                return "rejected_full"
            return retry
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, compacted)
        return "compacted+appended"
    finally:
        _release_profiles_lock(fd)


def _append_fact_locked(name: str, section: str, line: str) -> str:
    """Perform the actual append while holding the profiles lock."""
    from rapidfuzz.fuzz import token_set_ratio  # lazy import — only needed here

    path = profile_path(name)

    # --- load or create ---
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        sections = _parse_profile(raw, name)
    else:
        sections = {s: [] for s in _sections_for(name)}
        raw = _render_profile(name, sections)

    # --- fuzzy dedup against existing bullets in this section ---
    for existing_bullet in sections.get(section, []):
        if token_set_ratio(line, existing_bullet) >= _DEDUP_RATIO_THRESHOLD:
            return "duplicate"

    # --- append ---
    if section not in sections:
        sections[section] = []
    sections[section].append(line)
    new_raw = _render_profile(name, sections)

    # --- cap check ---
    if len(new_raw) <= _PROFILE_CHAR_CAP:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, new_raw)
        return "appended"

    # --- cap exceeded: signal the wrapper to compact OUTSIDE the lock ---
    # (holding the profiles lock across a multi-second Haiku call would block
    # every concurrent PROFILE writer; the wrapper re-acquires + re-validates).
    log.info("profile cap exceeded, deferring Haiku compaction", name=name, size=len(new_raw))
    return ("__compact__", new_raw, raw)  # type: ignore[return-value]


def _compact_profile(raw: str, name: str) -> str | None:
    """Call Haiku to rewrite a profile to ≤2000 chars. Returns new text or None on failure."""
    _COMPACT_SYSTEM = (
        "You are a profile compactor for an AI memory system. "
        "Rewrite the given profile markdown preserving ALL substantive facts. "
        "Merge redundant bullets. Keep the section structure (## headings). "
        "Output ONLY the rewritten profile markdown. "
        "Target length: ≤2000 characters. Do not add explanations."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt", _COMPACT_SYSTEM],
            input=raw,
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT_SECONDS,
            env={**os.environ, "MEMEM_HOOK_DISABLE": "1"},
            start_new_session=True,
        )
    except Exception as exc:
        log.warning("Haiku compaction subprocess error", exc=str(exc))
        return None

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        log.warning("Haiku compaction returned non-zero", detail=detail)
        return None

    out = result.stdout.strip()
    if not out:
        log.warning("Haiku compaction returned empty output")
        return None

    return out


def render_for_injection(scope_id: str) -> str:
    """Render user + project profile for injection at SessionStart.

    Each file's contribution is capped at _RENDER_CAP_PER_FILE chars, truncated
    at bullet boundaries (never mid-line). Empty/missing profiles contribute ''.
    Returns '' when both are empty.

    scope_id is normalized via models._normalize_scope_id so e.g. 'memem' maps
    to 'cortex-plugin' (the canonical project name).
    """
    parts: list[str] = []

    # User profile
    user_text = _render_capped(load_profile("user")["raw"], "user")
    if user_text:
        parts.append(user_text)

    # Project profile — only when scope resolves to something concrete
    normalized = _normalize_scope_id(scope_id)
    if normalized and normalized != "general":
        project_text = _render_capped(load_profile(normalized)["raw"], normalized)
        if project_text:
            parts.append(project_text)

    return "\n\n".join(parts)


def _render_capped(raw: str, name: str) -> str:
    """Return up to _RENDER_CAP_PER_FILE chars of a profile, truncated at bullet boundaries."""
    if not raw or not raw.strip():
        return ""
    if len(raw) <= _RENDER_CAP_PER_FILE:
        return raw.rstrip()

    # Truncate at bullet boundaries (never mid-line)
    result_lines: list[str] = []
    char_count = 0
    for line in raw.splitlines():
        line_cost = len(line) + 1  # +1 for newline
        if char_count + line_cost > _RENDER_CAP_PER_FILE:
            break
        result_lines.append(line)
        char_count += line_cost

    return "\n".join(result_lines).rstrip()


def list_profiles() -> list[str]:
    """Return a list of profile names present in the profiles directory."""
    pdir = _current_profiles_dir()
    if not pdir.exists():
        return []
    names: list[str] = []
    for p in sorted(pdir.glob("profile_*.md")):
        stem = p.stem  # e.g. "profile_user" or "profile_cortex-plugin"
        if stem == "profile_user":
            names.append("user")
        elif stem.startswith("profile_"):
            names.append(stem[len("profile_"):])
    return names
