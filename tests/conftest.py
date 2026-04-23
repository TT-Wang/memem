"""Shared pytest fixtures for memem tests."""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Isolated Obsidian vault for testing."""
    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)
    (vault / "memem" / "playbooks").mkdir(parents=True)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)
    from memem import models, obsidian_store, playbook
    importlib.reload(models)
    importlib.reload(obsidian_store)
    importlib.reload(playbook)
    return vault


@pytest.fixture
def tmp_cortex_dir(tmp_path, monkeypatch):
    """Isolated memem state directory for testing.

    Fixture is named tmp_cortex_dir for legacy reasons; the directory it
    creates is now ~/.memem/, not ~/.cortex/. Tests that take this fixture
    don't need to be renamed — they get whatever the package considers the
    current state dir.
    """
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    from memem import models, search_index, telemetry
    importlib.reload(models)
    importlib.reload(telemetry)
    importlib.reload(search_index)
    return state


@pytest.fixture
def tmp_memem_dir(tmp_cortex_dir):
    return tmp_cortex_dir


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
