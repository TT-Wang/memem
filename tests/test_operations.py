"""Tests for memem/operations.py — memory_save, memory_import, _import_file."""

from __future__ import annotations

import importlib
import inspect
import json


def _reload_modules():
    from memem import models, obsidian_store, operations, search_index

    importlib.reload(models)
    importlib.reload(obsidian_store)
    importlib.reload(search_index)
    importlib.reload(operations)
    return operations, obsidian_store


# ---------------------------------------------------------------------------
# H-3 dedup: JSON dict branch
# ---------------------------------------------------------------------------

def test_import_json_dict_dedup(tmp_vault, tmp_cortex_dir, tmp_path):
    """Re-importing the same JSON dict file returns 0 new memories on second call."""
    operations, obsidian_store = _reload_modules()

    data = {"key": "value", "description": "A test JSON memory for dedup checks"}
    json_file = tmp_path / "test_dict.json"
    json_file.write_text(json.dumps(data), encoding="utf-8")

    first_count = operations._import_file(json_file, scope_id="default")
    assert first_count == 1, f"Expected 1 memory imported, got {first_count}"

    # Reset cache so second call sees the saved memory via _is_duplicate.
    obsidian_store._reset_cache()

    second_count = operations._import_file(json_file, scope_id="default")
    assert second_count == 0, (
        f"Expected 0 memories on second import (dedup), got {second_count}"
    )


# ---------------------------------------------------------------------------
# H-3 dedup: JSON list branch
# ---------------------------------------------------------------------------

def test_import_json_list_dedup(tmp_vault, tmp_cortex_dir, tmp_path):
    """Re-importing the same JSON list file returns 0 new memories on second call.

    The two list items use very different vocabulary so the fuzzy dedup check
    does NOT falsely deduplicate them against each other on the first import,
    but DOES deduplicate them against themselves on the second import.
    """
    operations, obsidian_store = _reload_modules()

    data = [
        {
            "title": "Python asyncio patterns",
            "body": (
                "asyncio event loop drives coroutines; use asyncio.run() as entry point "
                "and await for non-blocking IO operations in Python 3.7+"
            ),
        },
        {
            "title": "SQL query optimisation",
            "body": (
                "Index columns used in WHERE and JOIN clauses; EXPLAIN ANALYZE reveals "
                "sequential scans that benefit from composite B-tree indexes on Postgres"
            ),
        },
    ]
    json_file = tmp_path / "test_list.json"
    json_file.write_text(json.dumps(data), encoding="utf-8")

    first_count = operations._import_file(json_file, scope_id="default")
    assert first_count == 2, f"Expected 2 memories imported, got {first_count}"

    obsidian_store._reset_cache()

    second_count = operations._import_file(json_file, scope_id="default")
    assert second_count == 0, (
        f"Expected 0 memories on second import (dedup), got {second_count}"
    )


# ---------------------------------------------------------------------------
# H-3 dedup: plaintext branch
# ---------------------------------------------------------------------------

def test_import_plaintext_dedup(tmp_vault, tmp_cortex_dir, tmp_path):
    """Re-importing the same plaintext file returns 0 new memories on second call."""
    operations, obsidian_store = _reload_modules()

    txt_file = tmp_path / "test_note.txt"
    txt_file.write_text(
        "This is a plaintext note that should not be duplicated when imported twice.",
        encoding="utf-8",
    )

    first_count = operations._import_file(txt_file, scope_id="default")
    assert first_count == 1, f"Expected 1 memory imported, got {first_count}"

    obsidian_store._reset_cache()

    second_count = operations._import_file(txt_file, scope_id="default")
    assert second_count == 0, (
        f"Expected 0 memories on second import (dedup), got {second_count}"
    )


# ---------------------------------------------------------------------------
# m5: paths parameter in memory_save()
# ---------------------------------------------------------------------------

def test_memory_save_has_paths_parameter():
    """memory_save() must accept a 'paths' parameter."""
    from memem.operations import memory_save
    params = inspect.signature(memory_save).parameters
    assert "paths" in params, "memory_save() must have a 'paths' parameter"


def test_memory_save_paths_written_to_frontmatter(tmp_vault, tmp_cortex_dir):
    """memory_save(content, title, paths=[...]) writes paths: to frontmatter."""
    operations, obsidian_store = _reload_modules()

    result = operations.memory_save(
        content="A test memory with path context for the server module",
        title="Test paths frontmatter",
        scope_id="default",
        paths=["memem/server.py", "memem/retrieve.py"],
    )
    assert "saved" in result.lower() or "merged" in result.lower() or "exists" in result.lower(), (
        f"memory_save should return a status string, got: {result}"
    )

    # If saved (not rejected), verify paths: appears in the written file
    if "saved" in result.lower():
        from memem.models import OBSIDIAN_MEMORIES_DIR
        md_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
        assert md_files, "At least one memory file should exist after memory_save"

        # Find the file that was just written
        found_paths_field = False
        for md_file in md_files:
            content = md_file.read_text(encoding="utf-8")
            if "paths:" in content and "memem/server.py" in content:
                found_paths_field = True
                break

        assert found_paths_field, (
            "The saved memory file should contain paths: frontmatter with 'memem/server.py'"
        )


def test_memory_save_paths_none_writes_no_paths_field(tmp_vault, tmp_cortex_dir):
    """memory_save() without paths= does not write paths: frontmatter."""
    operations, obsidian_store = _reload_modules()

    result = operations.memory_save(
        content="A test memory without path context for verify no paths field",
        title="Test no paths frontmatter",
        scope_id="default",
        # No paths= argument
    )

    if "saved" in result.lower():
        from memem.models import OBSIDIAN_MEMORIES_DIR
        md_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
        for md_file in md_files:
            content = md_file.read_text(encoding="utf-8")
            if "Test no paths frontmatter" in content:
                # The frontmatter should NOT contain a paths: block
                # Extract just the frontmatter
                front_end = content.find("\n---", 4)
                front = content[:front_end + 4] if front_end > 0 else content[:2000]
                assert "paths:" not in front, (
                    f"Memory saved without paths= should not have paths: in frontmatter"
                )
                break


def test_memory_save_paths_empty_list_writes_no_paths_field(tmp_vault, tmp_cortex_dir):
    """memory_save() with paths=[] does not write paths: frontmatter."""
    operations, obsidian_store = _reload_modules()

    result = operations.memory_save(
        content="A test memory with empty paths list should not have paths field",
        title="Test empty paths list",
        scope_id="default",
        paths=[],
    )

    if "saved" in result.lower():
        from memem.models import OBSIDIAN_MEMORIES_DIR
        md_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
        for md_file in md_files:
            content = md_file.read_text(encoding="utf-8")
            if "Test empty paths list" in content:
                front_end = content.find("\n---", 4)
                front = content[:front_end + 4] if front_end > 0 else content[:2000]
                assert "paths:" not in front, (
                    "Memory saved with paths=[] should not have paths: in frontmatter"
                )
