"""Shared pytest fixtures for memem tests."""

import importlib

import pytest
import structlog


@pytest.fixture(autouse=True)
def reset_structlog_after_each_test():
    """Reset structlog global configuration after every test.

    Prevents structlog's PrintLogger from retaining a reference to a
    capsys-captured stdout file after the capsys context ends, which
    causes 'ValueError: I/O operation on closed file' in subsequent tests
    that call logging code (e.g. test_attribution_hook.py contaminating
    test_cli.py tests).
    """
    yield
    structlog.reset_defaults()


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Isolated Obsidian vault for testing."""
    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)
    (vault / "memem" / "playbooks").mkdir(parents=True)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)
    # UPDATED(v2.6): retrieve module added to reload list because _search_memories
    # now delegates to retrieve(), whose module-level _vault_idx_cache must be
    # invalidated when the vault path changes between tests.
    from memem import models, obsidian_store, playbook, retrieve
    importlib.reload(models)
    importlib.reload(obsidian_store)
    importlib.reload(playbook)
    importlib.reload(retrieve)
    return vault


@pytest.fixture
def tmp_cortex_dir(tmp_path, monkeypatch):
    """Isolated memem state directory for testing.

    Fixture is named tmp_cortex_dir for legacy reasons; the directory it
    creates is now ~/.memem/, not ~/.cortex/. Tests that take this fixture
    don't need to be renamed — they get whatever the package considers the
    current state dir.

    Telemetry isolation: sets MEMEM_TELEMETRY_SOURCE=test so that any
    log_recall / log_citation calls during the test are silently discarded,
    and reloads recall_log so _LOG_PATH points at the tmp state dir (not the
    live ~/.memem/ path baked in at module import time).
    """
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.setenv("MEMEM_TELEMETRY_SOURCE", "test")
    from memem import graph_index, models, recall_log, search_index, telemetry
    importlib.reload(models)
    importlib.reload(recall_log)
    importlib.reload(telemetry)
    importlib.reload(search_index)
    importlib.reload(graph_index)
    return state


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
