"""Tests for memem/status.py render_status() function (v2.1.0 event-triggered).

Daemon-specific tests (HEARTBEAT_FILE, GLOBAL_LOCK_FILE, LOG_FILE) were removed
in the v2.1.0 daemon-removal refactor. Tests now cover the event-triggered
status renderer only.
"""

import importlib

import pytest


@pytest.fixture
def isolated_status(tmp_path, monkeypatch):
    """Redirect MEMEM_DIR and vault into tmp_path for isolated testing."""
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(tmp_path / "obsidian-brain"))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    from memem import models, status
    importlib.reload(models)
    importlib.reload(status)

    yield state

    importlib.reload(models)
    importlib.reload(status)


def _get_render_status():
    """Return the render_status function from the (possibly reloaded) status module."""
    from memem import status as _status_mod
    return _status_mod.render_status


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_render_status_returns_string(isolated_status):
    """render_status() returns a non-empty string without raising."""
    render = _get_render_status()
    result = render()
    assert isinstance(result, str)
    assert len(result) > 0


def test_render_status_contains_activity_section(isolated_status):
    """render_status() output includes [1] Recent mining activity section."""
    render = _get_render_status()
    out = render()
    assert "Recent mining activity" in out


def test_render_status_no_crash_on_empty_dir(isolated_status):
    """render_status() must not raise when .memem dir is empty (no DB yet)."""
    render = _get_render_status()
    # Should run without raising
    out = render()
    assert isinstance(out, str)
