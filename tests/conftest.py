"""Shared pytest fixtures for Cortex tests."""

import sys
from pathlib import Path

import pytest

# Make cortex-mcp-server importable
sys.path.insert(0, str(Path(__file__).parent.parent / "cortex-mcp-server"))


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Isolated Obsidian vault for testing."""
    vault = tmp_path / "obsidian-brain"
    (vault / "cortex" / "memories").mkdir(parents=True)
    (vault / "cortex" / "playbooks").mkdir(parents=True)
    monkeypatch.setenv("CORTEX_OBSIDIAN_VAULT", str(vault))
    # Reload models to pick up env var
    import importlib

    import models
    importlib.reload(models)
    import obsidian_store
    importlib.reload(obsidian_store)
    import playbook
    importlib.reload(playbook)
    return vault


@pytest.fixture
def tmp_cortex_dir(tmp_path, monkeypatch):
    """Isolated Cortex data directory for testing."""
    cortex = tmp_path / ".cortex"
    cortex.mkdir()
    monkeypatch.setenv("CORTEX_DIR", str(cortex))
    import importlib

    import models
    importlib.reload(models)
    import telemetry
    importlib.reload(telemetry)
    import search_index
    importlib.reload(search_index)
    return cortex


@pytest.fixture
def sample_memory():
    """A valid memory dict for testing."""
    return {
        "content": "Use pytest fixtures for test setup",
        "title": "Pytest fixtures",
        "tags": ["testing", "python"],
        "project": "general",
        "source_type": "user",
        "importance": 3,
    }
