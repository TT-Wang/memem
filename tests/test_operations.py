"""Tests for memem/operations.py — memory_save, memory_import, _import_file."""

from __future__ import annotations

import importlib
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
