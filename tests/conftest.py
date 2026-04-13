"""Shared pytest fixtures for Cortex tests."""

import importlib

import pytest


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Isolated Obsidian vault for testing."""
    vault = tmp_path / "obsidian-brain"
    (vault / "cortex" / "memories").mkdir(parents=True)
    (vault / "cortex" / "playbooks").mkdir(parents=True)
    monkeypatch.setenv("CORTEX_OBSIDIAN_VAULT", str(vault))
    from cortex_server import models, obsidian_store, playbook
    importlib.reload(models)
    importlib.reload(obsidian_store)
    importlib.reload(playbook)
    return vault


@pytest.fixture
def tmp_cortex_dir(tmp_path, monkeypatch):
    """Isolated Cortex data directory for testing."""
    cortex = tmp_path / ".cortex"
    cortex.mkdir()
    monkeypatch.setenv("CORTEX_DIR", str(cortex))
    from cortex_server import models, search_index, telemetry
    importlib.reload(models)
    importlib.reload(telemetry)
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
