"""Regression guard: _save_memory must not emit dead schema fields in vault frontmatter.

The fields below were removed from the schema. This test locks that in so
future refactors cannot accidentally re-introduce them.

Note: access_count, last_accessed_at, and decay_immune are v2 m3 live schema
fields. The old `last_accessed` (without _at) and `access_count` (pre-v2)
were removed; `access_count` is now a live field (m3 re-introduces it with
proper semantics tied to decay). Only the pre-v2 `last_accessed` (no _at
suffix) remains dead.
"""

from __future__ import annotations

import importlib

# Dead fields that must never appear in any written frontmatter.
# Note: access_count is now live (m3 decay schema). last_accessed (no _at) stays dead.
DEAD_FIELDS = {
    "associations",
    "impact_score",
    "success_count",
    "human_approved",
    "scope_type",
    "verified",
    "last_validated_at",
    "tier",
    "full_record",
    "last_accessed",
}


def _load_modules():
    from memem import models, obsidian_store, search_index

    importlib.reload(models)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)
    return obsidian_store


def _parse_frontmatter_keys(md_text: str) -> set[str]:
    """Extract YAML frontmatter key names from a markdown file string."""
    if not md_text.startswith("---"):
        return set()
    parts = md_text.split("---", 2)
    if len(parts) < 3:
        return set()
    frontmatter = parts[1]
    keys = set()
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def test_save_memory_does_not_emit_dead_schema_fields(tmp_vault, tmp_cortex_dir):
    """_save_memory must not write any of the 11 retired schema fields to disk."""
    obsidian_store = _load_modules()

    mem = obsidian_store._make_memory(
        content="The save path must never emit retired schema fields into vault frontmatter.",
        title="Dead field regression guard",
        tags=["regression", "schema"],
        project="memem",
        source_type="user",
        source_session="test-session",
        importance=3,
    )
    obsidian_store._save_memory(mem)

    # Locate the written file via obsidian_file set by _write_obsidian_memory.
    from memem import models

    importlib.reload(models)
    memories_dir = models.OBSIDIAN_MEMORIES_DIR
    written_files = list(memories_dir.glob("*.md"))
    assert written_files, "Expected at least one memory file to be written"

    for md_file in written_files:
        text = md_file.read_text(encoding="utf-8")
        frontmatter_keys = _parse_frontmatter_keys(text)
        dead_found = DEAD_FIELDS & frontmatter_keys
        assert not dead_found, (
            f"File {md_file.name} contains dead frontmatter fields: {dead_found}\n"
            f"Full frontmatter keys present: {frontmatter_keys}"
        )


def test_save_memory_emits_expected_live_fields(tmp_vault, tmp_cortex_dir):
    """Sanity-check that the written frontmatter does contain the known live fields."""
    obsidian_store = _load_modules()

    mem = obsidian_store._make_memory(
        content="Live schema fields should be present in every written memory file.",
        title="Live field presence check",
        tags=["schema"],
        project="memem",
        importance=4,
    )
    obsidian_store._save_memory(mem)

    from memem import models

    importlib.reload(models)
    memories_dir = models.OBSIDIAN_MEMORIES_DIR
    written_files = list(memories_dir.glob("*.md"))
    assert written_files

    # These are the live fields _write_obsidian_memory always emits.
    # v2.8.0: "layer" removed from required set — new memories omit layer field
    # (layer retirement). Explicit layer=N passthrough still works.
    expected_live = {"id", "schema_version", "title", "project", "tags",
                     "created", "updated", "source_type", "source_session",
                     "importance", "status", "valid_to"}

    for md_file in written_files:
        text = md_file.read_text(encoding="utf-8")
        frontmatter_keys = _parse_frontmatter_keys(text)
        missing = expected_live - frontmatter_keys
        assert not missing, (
            f"File {md_file.name} is missing expected live fields: {missing}\n"
            f"Full frontmatter keys present: {frontmatter_keys}"
        )
