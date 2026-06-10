"""Tests for C5: memory_save merge-don't-reject (three-band dedup logic).

Bands:
  score >= 0.92  → reject as exact duplicate
  0.70 <= score < 0.92  → merge via Haiku into existing memory
  score < 0.70   → save as new memory

Monkeypatching notes: operations.py imports _find_best_match, _save_memory,
_update_memory directly from obsidian_store (bound at import/reload time).
After _reload_operations(), those names live in memem.operations namespace,
so tests must patch "memem.operations.<name>" — NOT "memem.obsidian_store.<name>".
_merge_memories is a lazy import inside memory_save, so patch "memem.mining._merge_memories".
"""

from __future__ import annotations

import importlib
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_memory(mem_id: str = "abcd1234efgh5678", title: str = "Existing title") -> dict:
    """Return a minimal fake memory dict."""
    return {
        "id": mem_id,
        "title": title,
        "essence": "existing memory content",
    }


def _reload_operations():
    from memem import models, obsidian_store, operations
    importlib.reload(models)
    importlib.reload(obsidian_store)
    importlib.reload(operations)
    return operations


# ---------------------------------------------------------------------------
# Band 1: score >= 0.92 → reject
# ---------------------------------------------------------------------------

def test_exact_duplicate_rejected(tmp_vault, tmp_cortex_dir, monkeypatch):
    """score >= 0.92 → Memory already exists: [id8] "title". Not saved."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="abcd1234efgh5678", title="My exact memory")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.95),
    )

    result = operations.memory_save("some content", scope_id="default")

    assert "Memory already exists" in result
    assert "abcd1234" in result
    assert "My exact memory" in result
    assert "Not saved" in result


def test_exact_duplicate_at_boundary_0_92(tmp_vault, tmp_cortex_dir, monkeypatch):
    """score == 0.92 → rejected (boundary is inclusive at top)."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="aabbccdd11223344", title="Boundary test")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.92),
    )

    result = operations.memory_save("boundary content", scope_id="default")

    assert "Memory already exists" in result
    assert "Not saved" in result


# ---------------------------------------------------------------------------
# Band 2: 0.70 <= score < 0.92 → merge
# ---------------------------------------------------------------------------

def test_similar_content_merged(tmp_vault, tmp_cortex_dir, monkeypatch):
    """0.70 <= score < 0.92 → merge via Haiku, return Merged into existing message."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="aabb1122ccdd3344", title="Similar memory title")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.80),
    )
    monkeypatch.setattr(
        "memem.mining._merge_memories",
        lambda existing, new: "Merged content from Haiku",
    )
    monkeypatch.setattr(
        "memem.operations._update_memory",
        lambda memory_id, new_content, new_title="", extra_tags=None, extra_keys=None: None,
    )

    result = operations.memory_save("slightly different content", scope_id="default")

    assert "Merged into existing memory" in result
    assert "aabb1122" in result
    assert "Similar memory title" in result
    assert "merged via Haiku" in result


def test_similar_content_merged_at_boundary_0_70(tmp_vault, tmp_cortex_dir, monkeypatch):
    """score == 0.70 → merged (boundary is inclusive at bottom of merge band)."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="11223344aabbccdd", title="Low boundary merge")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.70),
    )
    monkeypatch.setattr(
        "memem.mining._merge_memories",
        lambda existing, new: "merged result",
    )
    monkeypatch.setattr(
        "memem.operations._update_memory",
        lambda memory_id, new_content, new_title="", extra_tags=None, extra_keys=None: None,
    )

    result = operations.memory_save("low boundary content", scope_id="default")

    assert "Merged into existing memory" in result


# ---------------------------------------------------------------------------
# Band 2 fallback: merge failure → reject with (merge unavailable)
# ---------------------------------------------------------------------------

def test_merge_failure_falls_back_to_reject(tmp_vault, tmp_cortex_dir, monkeypatch):
    """When _merge_memories raises RuntimeError, fall back to reject message."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="deadbeef12345678", title="Merge fail target")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.85),
    )
    monkeypatch.setattr(
        "memem.mining._merge_memories",
        lambda existing, new: (_ for _ in ()).throw(RuntimeError("Haiku timed out")),
    )

    result = operations.memory_save("content to merge", scope_id="default")

    assert "Memory already exists" in result
    assert "deadbeef" in result
    assert "merge unavailable" in result


def test_update_memory_failure_falls_back_to_reject(tmp_vault, tmp_cortex_dir, monkeypatch):
    """When _update_memory raises, fall back to reject message."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="feedface87654321", title="Update fail target")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.75),
    )
    monkeypatch.setattr(
        "memem.mining._merge_memories",
        lambda existing, new: "merged ok",
    )
    monkeypatch.setattr(
        "memem.operations._update_memory",
        lambda memory_id, new_content, new_title="": (_ for _ in ()).throw(
            RuntimeError("update failed")
        ),
    )

    result = operations.memory_save("content to update", scope_id="default")

    assert "Memory already exists" in result
    assert "feedface" in result
    assert "merge unavailable" in result


# ---------------------------------------------------------------------------
# Band 2: security rejection on merged content (_update_memory raises ValueError)
# ---------------------------------------------------------------------------

def test_security_rejection_on_merged_content(tmp_vault, tmp_cortex_dir, monkeypatch):
    """When _update_memory raises ValueError (security scan), fall back to reject."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="cafebabe12345678", title="Security blocked")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.78),
    )
    monkeypatch.setattr(
        "memem.mining._merge_memories",
        lambda existing, new: "injected prompt content",
    )
    monkeypatch.setattr(
        "memem.operations._update_memory",
        lambda memory_id, new_content, new_title="": (_ for _ in ()).throw(
            ValueError("Update blocked: prompt injection detected")
        ),
    )

    result = operations.memory_save("triggered security scan", scope_id="default")

    # Falls back to reject (merge unavailable) because ValueError is caught in
    # the merge try/except block
    assert "Memory already exists" in result
    assert "cafebabe" in result


# ---------------------------------------------------------------------------
# Band 3: score < 0.70 → save as new
# ---------------------------------------------------------------------------

def test_low_score_saves_as_new(tmp_vault, tmp_cortex_dir, monkeypatch):
    """score < 0.70 → save as new memory normally."""
    operations = _reload_operations()
    fake_mem = _make_fake_memory(mem_id="00000000ffffffff", title="Unrelated memory")

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (fake_mem, 0.50),
    )

    save_calls = []

    def _fake_save(mem):
        save_calls.append(mem)

    monkeypatch.setattr("memem.operations._save_memory", _fake_save)

    result = operations.memory_save(
        "completely different topic about databases",
        title="New memory",
        scope_id="default",
    )

    assert "Memory saved" in result
    assert len(save_calls) == 1


def test_no_existing_memories_saves_as_new(tmp_vault, tmp_cortex_dir, monkeypatch):
    """When _find_best_match returns (None, 0.0), save as new memory."""
    operations = _reload_operations()

    monkeypatch.setattr(
        "memem.operations._find_best_match",
        lambda content, scope_id="default": (None, 0.0),
    )

    save_calls = []

    def _fake_save(mem):
        save_calls.append(mem)

    monkeypatch.setattr("memem.operations._save_memory", _fake_save)

    result = operations.memory_save("brand new unique content", scope_id="default")

    assert "Memory saved" in result
    assert len(save_calls) == 1


# ---------------------------------------------------------------------------
# Server import smoke test
# ---------------------------------------------------------------------------

def test_server_import_smoke():
    """Importing memem.server should not raise."""
    import memem.server  # noqa: F401
    assert True
