"""Tests for MEMEM_INJECTION_MODE default flip (v2.4.0)."""
from __future__ import annotations

import importlib


def test_default_mode_is_tool_when_env_absent(monkeypatch):
    monkeypatch.delenv("MEMEM_INJECTION_MODE", raising=False)
    import memem.settings as _settings
    importlib.reload(_settings)
    assert _settings.MEMEM_INJECTION_MODE == "tool", (
        f"Expected default 'tool' got {_settings.MEMEM_INJECTION_MODE!r}"
    )


def test_explicit_auto_still_respected(monkeypatch):
    monkeypatch.setenv("MEMEM_INJECTION_MODE", "auto")
    import memem.settings as _settings
    importlib.reload(_settings)
    assert _settings.MEMEM_INJECTION_MODE == "auto"


def test_explicit_hybrid_still_respected(monkeypatch):
    monkeypatch.setenv("MEMEM_INJECTION_MODE", "hybrid")
    import memem.settings as _settings
    importlib.reload(_settings)
    assert _settings.MEMEM_INJECTION_MODE == "hybrid"


def test_explicit_tool_still_respected(monkeypatch):
    monkeypatch.setenv("MEMEM_INJECTION_MODE", "tool")
    import memem.settings as _settings
    importlib.reload(_settings)
    assert _settings.MEMEM_INJECTION_MODE == "tool"


def teardown_module(module):
    """Restore default settings module state."""
    import os
    os.environ.pop("MEMEM_INJECTION_MODE", None)
    import memem.settings as _settings
    importlib.reload(_settings)
