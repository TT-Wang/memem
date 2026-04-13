"""Tests for mining pipeline (mocked Haiku)."""

import pytest
from unittest.mock import patch, MagicMock


def test_extract_json_string():
    from mining import _extract_json_string
    assert _extract_json_string('[{"title": "test"}]') == '[{"title": "test"}]'
    assert _extract_json_string('prefix [{"title": "x"}] suffix') == '[{"title": "x"}]'
    assert _extract_json_string('no json here') is None


def test_is_agent_session():
    from mining import _is_agent_session
    assert _is_agent_session(["User: # Planner Module\nYou are a planner"])
    assert _is_agent_session(["User: You are a helpful assistant"])
    assert not _is_agent_session(["User: fix the auth bug"])
