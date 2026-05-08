"""Tests for memem/vault_registry.py.

Covers:
1. vaults.json does not exist → returns synthetic default with id='default'
2. vaults.json exists with 2 entries → loads both correctly
3. vaults.json has a malformed entry (missing 'path') → skipped; valid ones load
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_vault_registry(monkeypatch, tmp_memem_dir: Path) -> None:
    """Point MEMEM_DIR at tmp_memem_dir and reload vault_registry."""
    monkeypatch.setenv("MEMEM_DIR", str(tmp_memem_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    import memem.models
    importlib.reload(memem.models)
    import memem.vault_registry
    importlib.reload(memem.vault_registry)


# ---------------------------------------------------------------------------
# Test 1: no vaults.json → single synthetic default entry
# ---------------------------------------------------------------------------


def test_no_vaults_json_returns_default(tmp_path, monkeypatch):
    """When vaults.json does not exist, load_vault_registry returns a single
    default entry with id='default' pointing at the current OBSIDIAN_MEMORIES_DIR.
    """
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    vault_dir = tmp_path / "obsidian-brain"
    (vault_dir / "memem" / "memories").mkdir(parents=True)

    monkeypatch.setenv("MEMEM_DIR", str(memem_dir))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

    import memem.models
    importlib.reload(memem.models)
    import memem.vault_registry
    importlib.reload(memem.vault_registry)

    from memem.vault_registry import load_vault_registry
    result = load_vault_registry()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "default"
    assert "memories" in result[0]["path"]


# ---------------------------------------------------------------------------
# Test 2: vaults.json with 2 entries → loads both
# ---------------------------------------------------------------------------


def test_vaults_json_two_entries(tmp_path, monkeypatch):
    """When vaults.json contains 2 valid entries, both are returned."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    vaults_data = [
        {"id": "personal", "path": "/tmp/personal/memories"},
        {"id": "work", "path": "/tmp/work/memories"},
    ]
    (memem_dir / "vaults.json").write_text(json.dumps(vaults_data), encoding="utf-8")

    monkeypatch.setenv("MEMEM_DIR", str(memem_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models
    importlib.reload(memem.models)
    import memem.vault_registry
    importlib.reload(memem.vault_registry)

    from memem.vault_registry import load_vault_registry
    result = load_vault_registry()

    assert len(result) == 2
    ids = {entry["id"] for entry in result}
    assert ids == {"personal", "work"}
    paths = {entry["path"] for entry in result}
    assert "/tmp/personal/memories" in paths
    assert "/tmp/work/memories" in paths


# ---------------------------------------------------------------------------
# Test 3: malformed entry (missing 'path') is skipped; valid ones load
# ---------------------------------------------------------------------------


def test_malformed_entry_skipped(tmp_path, monkeypatch):
    """An entry missing 'path' is skipped; valid entries still load."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()

    vaults_data = [
        {"id": "good-vault", "path": "/tmp/good/memories"},
        {"id": "bad-vault"},          # missing 'path'
        {"path": "/tmp/noid/memories"},  # missing 'id'
    ]
    (memem_dir / "vaults.json").write_text(json.dumps(vaults_data), encoding="utf-8")

    monkeypatch.setenv("MEMEM_DIR", str(memem_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    import memem.models
    importlib.reload(memem.models)
    import memem.vault_registry
    importlib.reload(memem.vault_registry)

    from memem.vault_registry import load_vault_registry
    result = load_vault_registry()

    # Only the valid entry should be returned
    assert len(result) == 1
    assert result[0]["id"] == "good-vault"
    assert result[0]["path"] == "/tmp/good/memories"
