"""Tests for M-4 v1.9.3: strict frontmatter validation in
_parse_obsidian_memory_file.

Prior behaviour (v1.9.2 and earlier): files lacking `---` frontmatter were
silently parsed with the entire file body treated as content and
schema_version=0. They entered the corpus and polluted FTS results.

New behaviour: missing/malformed frontmatter is dispatched via
MEMEM_FRONTMATTER_STRICT:
  - skip       : log + return None (file stays in place)
  - quarantine : move to ~/.memem/quarantine/, log + return None (default)
  - raise      : raise ValueError
"""

import importlib
from pathlib import Path

import pytest


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _reload_with_mode(mode: str, monkeypatch):
    """Reload obsidian_store with a specific MEMEM_FRONTMATTER_STRICT mode."""
    monkeypatch.setenv("MEMEM_FRONTMATTER_STRICT", mode)
    from memem import obsidian_store
    importlib.reload(obsidian_store)
    return obsidian_store


def test_well_formed_file_still_parses(tmp_cortex_dir, tmp_vault, monkeypatch):
    """A normal file with frontmatter must still parse — regression guard."""
    ob = _reload_with_mode("quarantine", monkeypatch)
    p = _write(
        tmp_vault / "valid.md",
        "---\nid: abc123\ntitle: Valid\nproject: t1\n---\n\nbody text\n",
    )
    mem = ob._parse_obsidian_memory_file(p)
    assert mem is not None
    assert mem["id"] == "abc123"


def test_missing_frontmatter_returns_none_skip_mode(tmp_cortex_dir, tmp_vault, monkeypatch):
    """skip mode: no frontmatter → None, file left in place."""
    ob = _reload_with_mode("skip", monkeypatch)
    p = _write(tmp_vault / "noheader.md", "just a body, no frontmatter at all\n")
    mem = ob._parse_obsidian_memory_file(p)
    assert mem is None
    assert p.exists(), "skip mode must leave file in place"


def test_missing_frontmatter_quarantine_mode(tmp_cortex_dir, tmp_vault, monkeypatch):
    """quarantine mode: file moved to ~/.memem/quarantine/, returns None."""
    ob = _reload_with_mode("quarantine", monkeypatch)
    from memem.models import MEMEM_DIR

    p = _write(tmp_vault / "noheader.md", "just a body\n")
    mem = ob._parse_obsidian_memory_file(p)
    assert mem is None
    assert not p.exists(), "quarantine must move the file"
    quarantine_files = list((MEMEM_DIR / "quarantine").glob("*_noheader.md"))
    assert len(quarantine_files) == 1
    assert quarantine_files[0].read_text() == "just a body\n"


def test_missing_frontmatter_raise_mode(tmp_cortex_dir, tmp_vault, monkeypatch):
    """raise mode: ValueError on missing frontmatter."""
    ob = _reload_with_mode("raise", monkeypatch)
    p = _write(tmp_vault / "noheader.md", "just a body\n")
    with pytest.raises(ValueError, match="missing_frontmatter"):
        ob._parse_obsidian_memory_file(p)


def test_quarantine_collision_disambiguated(tmp_cortex_dir, tmp_vault, monkeypatch):
    """Two files named 'memory.md' from different subdirs both quarantine
    successfully without clobbering each other (hash prefix disambiguates)."""
    ob = _reload_with_mode("quarantine", monkeypatch)
    from memem.models import MEMEM_DIR

    a = _write(tmp_vault / "scopeA" / "memory.md", "body A\n")
    b = _write(tmp_vault / "scopeB" / "memory.md", "body B\n")
    ob._parse_obsidian_memory_file(a)
    ob._parse_obsidian_memory_file(b)
    quarantined = sorted((MEMEM_DIR / "quarantine").glob("*_memory.md"))
    assert len(quarantined) == 2, f"both must quarantine, got: {quarantined}"
    contents = {f.read_text() for f in quarantined}
    assert contents == {"body A\n", "body B\n"}


def test_leading_whitespace_before_frontmatter_accepted(tmp_cortex_dir, tmp_vault, monkeypatch):
    """A leading blank line before `---` is acceptable (lstrip)."""
    ob = _reload_with_mode("quarantine", monkeypatch)
    p = _write(
        tmp_vault / "padded.md",
        "\n\n---\nid: xyz\ntitle: Padded\n---\nbody\n",
    )
    mem = ob._parse_obsidian_memory_file(p)
    assert mem is not None
    assert mem["id"] == "xyz"
