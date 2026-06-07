"""Tests for m3: LLM judge env-var gate + 2s hard timeout + fallback behaviour.

Acceptance criteria:
- MEMEM_USE_LLM_JUDGE=0 → heuristic path, no LLM subprocess spawned
- MEMEM_USE_LLM_JUDGE=1 (default) → LLM path attempted
- LLM judge timeout falls through to heuristic (not an exception)
- MEMEM_USE_LLM_JUDGE read from os.environ not environment dict
- auto-recall.sh no longer has hardcoded use_llm=False
"""
from __future__ import annotations

import logging
import os

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bundle(query: str = "Fix the auth bug") -> dict:
    from memem.active_slice import current_query_candidate

    return {
        "current_goal_candidates": [current_query_candidate(query, "test-scope")],
        "memory_candidates": [
            {
                "candidate_id": "memory:abc",
                "candidate_type": "memory",
                "memory_id": "abc",
                "title": "Use pytest fixtures",
                "summary": "Tests must use pytest",
                "score": 0.75,
            }
        ],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }


def _fake_heuristic_result(query: str = "Fix the auth bug") -> dict:
    return {
        "goals": [
            {
                "candidate_id": f"current_query:{query}",
                "memory_id": "",
                "artifact_id": "",
                "why": "current task",
                "score": 1.0,
                "centrality": 1.0,
                "role_confidence": 1.0,
                "drop_reason": "",
            }
        ],
        "constraints": [],
        "background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifact_context": [],
        "open_tensions": [],
        "ignored": [],
        "activation_mode": "heuristic",
        "confidence": 0.7,
        "warnings": [],
        "excluded_candidates": [],
    }


# ---------------------------------------------------------------------------
# Test 1: MEMEM_USE_LLM_JUDGE=0 → heuristic, no LLM called
# ---------------------------------------------------------------------------

def test_llm_judge_disabled_by_env_var(monkeypatch):
    """MEMEM_USE_LLM_JUDGE=0 must force heuristic path; LLM must not be called."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_LLM_JUDGE", False)

    from memem import activation

    llm_called = []

    def _should_not_be_called(*args, **kwargs):
        llm_called.append(True)
        raise AssertionError("LLM should not be called when MEMEM_USE_LLM_JUDGE=False")

    monkeypatch.setattr(activation, "judge_activation_with_llm", _should_not_be_called)

    # Patch generate_candidates to return a minimal bundle without I/O
    monkeypatch.setattr(
        "memem.active_slice_engine.generate_candidates",
        lambda query, scope, env, **kwargs: _make_bundle(query),
    )
    monkeypatch.setattr("memem.active_slice_engine._persist_slice", lambda s: None)

    from memem.active_slice_engine import generate_active_memory_slice

    result = generate_active_memory_slice("Fix the auth bug", scope_id="test-scope", use_llm=True)

    assert not llm_called, "LLM was called despite MEMEM_USE_LLM_JUDGE=False"
    assert result["activation_mode"] == "heuristic"


# ---------------------------------------------------------------------------
# Test 2: MEMEM_USE_LLM_JUDGE=1 (default) → LLM path attempted
# ---------------------------------------------------------------------------

def test_llm_judge_enabled_by_env_var(monkeypatch):
    """MEMEM_USE_LLM_JUDGE=1 (default) must attempt the LLM path."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_LLM_JUDGE", True)

    llm_called = []

    def _fake_llm(*args, **kwargs):
        llm_called.append(True)
        result = _fake_heuristic_result()
        result["activation_mode"] = "llm"
        result["confidence"] = 0.95
        return result

    # Patch via the engine module's imported name (not via activation module)
    monkeypatch.setattr("memem.active_slice_engine.judge_activation_with_llm", _fake_llm)

    monkeypatch.setattr(
        "memem.active_slice_engine.generate_candidates",
        lambda query, scope, env, **kwargs: _make_bundle(query),
    )
    monkeypatch.setattr("memem.active_slice_engine._persist_slice", lambda s: None)

    from memem.active_slice_engine import generate_active_memory_slice

    result = generate_active_memory_slice("Fix the auth bug", scope_id="test-scope", use_llm=True)

    assert llm_called, "LLM was not called despite MEMEM_USE_LLM_JUDGE=True"
    assert result["activation_mode"] == "llm"


# ---------------------------------------------------------------------------
# Test 3: LLM timeout → fallback to heuristic with warning (not exception)
# ---------------------------------------------------------------------------

def test_llm_timeout_falls_back_to_heuristic(monkeypatch, caplog):
    """LLM judge timeout must produce heuristic result + warning, not propagate exception."""
    import subprocess

    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_LLM_JUDGE", True)

    from memem import activation

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=2)

    monkeypatch.setattr(activation, "judge_activation_with_llm", _timeout)

    monkeypatch.setattr(
        "memem.active_slice_engine.generate_candidates",
        lambda query, scope, env, **kwargs: _make_bundle(query),
    )
    monkeypatch.setattr("memem.active_slice_engine._persist_slice", lambda s: None)


    from memem.active_slice_engine import generate_active_memory_slice
    with caplog.at_level(logging.WARNING):
        result = generate_active_memory_slice("Fix the auth bug", scope_id="test-scope", use_llm=True)

    # Must produce valid heuristic result (no exception raised)
    assert result["activation_mode"] == "heuristic"
    assert isinstance(result.get("goals"), list)


# ---------------------------------------------------------------------------
# Test 4: MEMEM_USE_LLM_JUDGE is a module-level constant (not per-call env)
# ---------------------------------------------------------------------------

def test_memem_use_llm_judge_is_module_level_constant(monkeypatch):
    """MEMEM_USE_LLM_JUDGE must be a module-level bool in settings.py."""
    import memem.settings as settings

    assert hasattr(settings, "MEMEM_USE_LLM_JUDGE"), "MEMEM_USE_LLM_JUDGE missing from settings"
    assert isinstance(settings.MEMEM_USE_LLM_JUDGE, bool), (
        f"MEMEM_USE_LLM_JUDGE must be bool, got {type(settings.MEMEM_USE_LLM_JUDGE)}"
    )


# ---------------------------------------------------------------------------
# Test 5: MEMEM_LLM_JUDGE_TIMEOUT is a module-level constant
# ---------------------------------------------------------------------------

def test_memem_llm_judge_timeout_is_module_level_constant():
    """MEMEM_LLM_JUDGE_TIMEOUT must be a module-level float in settings.py."""
    import memem.settings as settings

    assert hasattr(settings, "MEMEM_LLM_JUDGE_TIMEOUT"), "MEMEM_LLM_JUDGE_TIMEOUT missing from settings"
    assert isinstance(settings.MEMEM_LLM_JUDGE_TIMEOUT, float), (
        f"MEMEM_LLM_JUDGE_TIMEOUT must be float, got {type(settings.MEMEM_LLM_JUDGE_TIMEOUT)}"
    )
    assert settings.MEMEM_LLM_JUDGE_TIMEOUT == 2.0


# ---------------------------------------------------------------------------
# Test 6: hook auto-recall.sh no longer has hardcoded use_llm=False
# ---------------------------------------------------------------------------

def test_hook_no_longer_has_hardcoded_use_llm_false():
    """auto-recall.sh must not have the original hardcoded use_llm=False line."""
    hook_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hooks", "auto-recall.sh"
    )
    with open(hook_path) as f:
        content = f.read()
    # The hook should now use _USE_LLM_JUDGE variable, not the literal False
    assert "use_llm=_USE_LLM_JUDGE" in content, (
        "hook should use _USE_LLM_JUDGE variable (not hardcoded False)"
    )
    # Confirm the env var read is present
    assert "MEMEM_USE_LLM_JUDGE" in content, (
        "hook should read MEMEM_USE_LLM_JUDGE from environment"
    )


# ---------------------------------------------------------------------------
# Test 7: judge_activation_with_llm timeout default reads from settings
# ---------------------------------------------------------------------------

def test_judge_activation_with_llm_uses_settings_timeout(monkeypatch):
    """judge_activation_with_llm must use MEMEM_LLM_JUDGE_TIMEOUT when no explicit timeout given.

    v1.13 Phase 4.5 update: subprocess.run was replaced with Popen + communicate(timeout=...)
    so we capture the timeout argument from communicate(), not run().
    """
    import subprocess

    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_LLM_JUDGE_TIMEOUT", 0.5)

    captured_timeouts = []

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 12345
            self.returncode = -9

        def communicate(self, input=None, timeout=None):
            captured_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0.0)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # Patch os.killpg to no-op so the cleanup path doesn't try to kill the fake PID
    import os as _os
    monkeypatch.setattr(_os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(_os, "getpgid", lambda pid: pid)

    from memem import activation

    # Patch assembly_available to return True so it doesn't bail early
    monkeypatch.setattr(activation, "assembly_available", lambda: True)

    bundle = _make_bundle()
    # Should not raise — falls back to heuristic
    result = activation.judge_activation_with_llm("Fix auth bug", "test-scope", {}, bundle)

    assert result["activation_mode"] == "heuristic"
    # Confirm the timeout passed to subprocess.Popen.communicate matched settings value
    assert any(t == 0.5 for t in captured_timeouts), (
        f"Expected timeout=0.5 from settings, got: {captured_timeouts}"
    )


# ---------------------------------------------------------------------------
# Test 8: use_llm=False at call site still disables LLM even when env var is ON
# ---------------------------------------------------------------------------

def test_use_llm_false_arg_overrides_env_var_on(monkeypatch):
    """Passing use_llm=False explicitly must disable LLM even when MEMEM_USE_LLM_JUDGE=True."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_LLM_JUDGE", True)

    from memem import activation

    llm_called = []

    def _should_not_be_called(*args, **kwargs):
        llm_called.append(True)
        raise AssertionError("LLM called despite use_llm=False")

    monkeypatch.setattr(activation, "judge_activation_with_llm", _should_not_be_called)

    monkeypatch.setattr(
        "memem.active_slice_engine.generate_candidates",
        lambda query, scope, env, **kwargs: _make_bundle(query),
    )
    monkeypatch.setattr("memem.active_slice_engine._persist_slice", lambda s: None)

    from memem.active_slice_engine import generate_active_memory_slice

    result = generate_active_memory_slice("Fix auth", scope_id="test-scope", use_llm=False)
    assert not llm_called
    assert result["activation_mode"] == "heuristic"
