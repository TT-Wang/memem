"""Tests for render_session_start (D5) — budgeted three-block assembly.

Covers:
1. All three blocks present when data exists
2. Per-block budgets enforced (oversized profile/rules/episodes truncated independently)
3. Empty vault → '' (so hook emits empty additionalContext)
4. One block raising → other two still render
5. Single _obsidian_memories() call (monkeypatch counter)
6. Scope normalization (scope passed to render_for_injection)
7. Total length < 6000 chars with maximal fixtures
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memem.session_blocks import (
    render_episode_catalog,
    render_session_start,
    render_working_rules,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc_mem(
    id8: str,
    title: str,
    content: str,
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "id": id8 + "0" * max(0, 36 - len(id8)),
        "title": title,
        "essence": content,
        "domain_tags": ["type:procedural"],
        "project": "general",
        "importance": 4,
        "created_at": created_at,
        "invalid_at": None,
    }


def _make_episodic_mem(
    id8: str,
    title: str,
    created_at: str = "2026-03-01T00:00:00Z",
) -> dict:
    return {
        "id": id8 + "0" * max(0, 36 - len(id8)),
        "title": title,
        "essence": f"Episodic memory: {title}",
        "domain_tags": ["type:episodic"],
        "project": "general",
        "importance": 3,
        "created_at": created_at,
        "invalid_at": None,
    }


def _make_all_mems_fixture() -> list[dict]:
    """Return a mix of procedural + episodic memories."""
    mems = []
    for i in range(5):
        mems.append(_make_proc_mem(
            f"proc{i:04d}",
            f"Rule {i}",
            f"Always follow rule {i} carefully and consistently.",
            created_at=f"2026-0{i+1}-01T00:00:00Z",
        ))
    for i in range(10):
        mems.append(_make_episodic_mem(
            f"epis{i:04d}",
            f"Episode title {i}: work done on project feature",
            created_at=f"2026-0{i%9+1}-{i%28+1:02d}T00:00:00Z",
        ))
    return mems


# ---------------------------------------------------------------------------
# Test 1: All three blocks present when data exists
# ---------------------------------------------------------------------------


def test_render_session_start_all_blocks_present(tmp_path, monkeypatch):
    """render_session_start includes profiles + working rules + episode index."""
    # Set up tmp vault with profiles
    vault = tmp_path / "obsidian-brain"
    profiles_dir = vault / "memem" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "profile_user.md").write_text(
        "# User Profile\n\n## Preferences\n- Prefers direct answers\n"
    )

    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    all_mems = _make_all_mems_fixture()

    # Patch _obsidian_memories in session_blocks' render_session_start
    with patch("memem.obsidian_store._obsidian_memories", return_value=all_mems) as mock_om:
        result = render_session_start("myproject")

    # Profiles block: should contain the user profile heading
    assert "## Preferences" in result or "# User Profile" in result, (
        f"Expected profiles block in result. Got:\n{result[:500]}"
    )

    # Working rules block
    assert "## Working rules" in result, (
        f"Expected working rules block. Got:\n{result[:500]}"
    )

    # Episode index block
    assert "## Episode index" in result, (
        f"Expected episode index block. Got:\n{result[:500]}"
    )


# ---------------------------------------------------------------------------
# Test 2: Per-block budgets enforced
# ---------------------------------------------------------------------------


def test_render_working_rules_budget_enforced():
    """render_working_rules truncates at bullet boundary when limit_chars exceeded."""
    mems = [
        _make_proc_mem(f"rule{i:04d}", f"Rule {i}", "A" * 200, created_at=f"2026-0{(i%9)+1}-01T00:00:00Z")
        for i in range(20)
    ]
    result = render_working_rules(all_memories=mems, limit_chars=300)
    assert len(result) <= 300, f"Expected ≤300 chars, got {len(result)}: {result!r}"
    # No partial lines — every bullet must be complete
    for ln in result.splitlines():
        assert ln.startswith("#") or ln.startswith("- ") or ln == "", (
            f"Unexpected partial line: {ln!r}"
        )


def test_render_episode_catalog_budget_enforced():
    """render_episode_catalog truncates at bullet boundary when limit_chars exceeded."""
    mems = [
        _make_episodic_mem(f"ep{i:06d}", "A" * 80, created_at=f"2026-01-{i%28+1:02d}T00:00:00Z")
        for i in range(30)
    ]
    result = render_episode_catalog(all_memories=mems, limit_chars=400)
    assert len(result) <= 400, f"Expected ≤400 chars, got {len(result)}: {result!r}"
    for ln in result.splitlines():
        assert ln.startswith("#") or ln.startswith("- ") or ln == "", (
            f"Unexpected partial line: {ln!r}"
        )


def test_render_session_start_total_budget(tmp_path, monkeypatch):
    """render_session_start output ≤ 5500 chars with a maximal fixture."""
    vault = tmp_path / "obsidian-brain"
    profiles_dir = vault / "memem" / "profiles"
    profiles_dir.mkdir(parents=True)
    # Write a fat user profile (> 1200 chars)
    big_profile = "# User Profile\n\n## Preferences\n" + "".join(
        f"- Preference item {i}: some long description here\n" for i in range(60)
    )
    (profiles_dir / "profile_user.md").write_text(big_profile)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    # Many procedural + episodic memories
    all_mems = []
    for i in range(30):
        all_mems.append(_make_proc_mem(f"pr{i:06d}", f"Rule {i}", "B" * 200))
    for i in range(30):
        all_mems.append(_make_episodic_mem(f"ep{i:06d}", "C" * 80))

    with patch("memem.obsidian_store._obsidian_memories", return_value=all_mems):
        result = render_session_start("testproject")

    assert len(result) <= 5500, (
        f"Total content must be ≤5500 chars, got {len(result)}"
    )


# ---------------------------------------------------------------------------
# Test 3: Empty vault → ''
# ---------------------------------------------------------------------------


def test_render_session_start_empty_vault(tmp_path, monkeypatch):
    """render_session_start returns '' when vault has no profiles/memories."""
    vault = tmp_path / "obsidian-brain"
    vault.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    with patch("memem.obsidian_store._obsidian_memories", return_value=[]):
        result = render_session_start("someproject")

    assert result == "", (
        f"Expected '' for empty vault, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: One block raising → other two still render
# ---------------------------------------------------------------------------


def test_render_session_start_one_block_raises_others_survive(tmp_path, monkeypatch):
    """When render_working_rules raises, profiles + episode index still render."""
    vault = tmp_path / "obsidian-brain"
    profiles_dir = vault / "memem" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "profile_user.md").write_text(
        "# User Profile\n\n## Preferences\n- Direct answers\n"
    )
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    episodic_mems = [_make_episodic_mem(f"ep{i:06d}", f"Episode {i}") for i in range(3)]

    # Patch render_working_rules to raise
    with patch("memem.session_blocks.render_working_rules", side_effect=RuntimeError("boom")):
        with patch("memem.obsidian_store._obsidian_memories", return_value=episodic_mems):
            result = render_session_start("testproject")

    # Should NOT crash
    assert isinstance(result, str), "render_session_start must not raise"
    # Episode index should still be present
    assert "## Episode index" in result, (
        f"Episode index should survive working_rules failure. Got:\n{result}"
    )


def test_render_session_start_profiles_raises_others_survive(tmp_path, monkeypatch):
    """When render_for_injection raises, rules + episode index still render."""
    vault = tmp_path / "obsidian-brain"
    vault.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    all_mems = _make_all_mems_fixture()

    with patch("memem.profiles.render_for_injection", side_effect=RuntimeError("profile boom")):
        with patch("memem.obsidian_store._obsidian_memories", return_value=all_mems):
            result = render_session_start("testproject")

    assert isinstance(result, str), "render_session_start must not raise"
    # Working rules and episode index should still render
    assert "## Working rules" in result or "## Episode index" in result, (
        f"Rules or episode index should survive profiles failure. Got:\n{result}"
    )


# ---------------------------------------------------------------------------
# Test 5: Single _obsidian_memories() call
# ---------------------------------------------------------------------------


def test_render_session_start_single_vault_load(tmp_path, monkeypatch):
    """_obsidian_memories() is called at most once in render_session_start."""
    vault = tmp_path / "obsidian-brain"
    vault.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    all_mems = _make_all_mems_fixture()
    call_count = {"n": 0}

    def counting_load():
        call_count["n"] += 1
        return all_mems

    with patch("memem.obsidian_store._obsidian_memories", side_effect=counting_load):
        render_session_start("testproject")

    assert call_count["n"] <= 1, (
        f"_obsidian_memories() should be called at most once, was called {call_count['n']} times"
    )


# ---------------------------------------------------------------------------
# Test 6: Scope normalization
# ---------------------------------------------------------------------------


def test_render_session_start_scope_normalized(tmp_path, monkeypatch):
    """render_for_injection receives the raw scope (it normalizes internally)."""
    vault = tmp_path / "obsidian-brain"
    profiles_dir = vault / "memem" / "profiles"
    profiles_dir.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    captured_scope = {}

    def fake_render_for_injection(scope_id: str) -> str:
        captured_scope["scope_id"] = scope_id
        return ""

    with patch("memem.profiles.render_for_injection", side_effect=fake_render_for_injection):
        with patch("memem.obsidian_store._obsidian_memories", return_value=[]):
            render_session_start("my-project")

    assert captured_scope.get("scope_id") == "my-project", (
        f"Expected scope_id='my-project', got {captured_scope!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: Total length sanity with maximal fixtures
# ---------------------------------------------------------------------------


def test_render_session_start_total_length_sanity(tmp_path, monkeypatch):
    """render_session_start always returns < 6000 chars even with maximal fixtures."""
    vault = tmp_path / "obsidian-brain"
    profiles_dir = vault / "memem" / "profiles"
    profiles_dir.mkdir(parents=True)
    # Max-size user profile
    max_profile = "# User Profile\n\n## Preferences\n" + "".join(
        f"- Item {i}: {'X' * 100}\n" for i in range(100)
    )
    (profiles_dir / "profile_user.md").write_text(max_profile)
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    # Max procedural + episodic memories
    all_mems = []
    for i in range(50):
        all_mems.append(_make_proc_mem(f"pr{i:06d}", f"Rule {i}", "Y" * 200))
    for i in range(50):
        all_mems.append(_make_episodic_mem(f"ep{i:06d}", "Z" * 80))

    with patch("memem.obsidian_store._obsidian_memories", return_value=all_mems):
        result = render_session_start("testproject")

    assert len(result) < 6000, (
        f"Total context must be < 6000 chars, got {len(result)}"
    )


# ---------------------------------------------------------------------------
# Test 8: Episode index max_entries cap
# ---------------------------------------------------------------------------


def test_render_episode_catalog_max_entries_capped():
    """render_episode_catalog caps results at max_entries (default 25)."""
    mems = [
        _make_episodic_mem(f"ep{i:06d}", f"Episode {i}", created_at=f"2026-01-{i%28+1:02d}T00:00:00Z")
        for i in range(40)
    ]
    result = render_episode_catalog(all_memories=mems)
    bullet_lines = [ln for ln in result.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) <= 25, (
        f"Expected ≤25 episode entries, got {len(bullet_lines)}"
    )


# ---------------------------------------------------------------------------
# Test 9: render_episode_catalog empty when no episodic memories
# ---------------------------------------------------------------------------


def test_render_episode_catalog_empty_when_no_episodic():
    """render_episode_catalog returns '' when no episodic memories exist."""
    mems = [_make_proc_mem("proc0001", "A rule", "Do this.")]
    result = render_episode_catalog(all_memories=mems)
    assert result == "", f"Expected '' when no episodic mems, got: {result!r}"


# ---------------------------------------------------------------------------
# Test 10: session-start.sh structural check
# ---------------------------------------------------------------------------


def test_session_start_uses_render_session_start():
    """session-start.sh must call render_session_start from session_blocks."""
    repo_root = Path(__file__).resolve().parent.parent
    content = (repo_root / "hooks" / "session-start.sh").read_text()

    assert "render_session_start" in content, (
        "session-start.sh must call render_session_start"
    )
    assert "session_blocks" in content, (
        "session-start.sh must import from session_blocks"
    )


# ---------------------------------------------------------------------------
# Test 11: profiles block appears before episode index (ordering check)
# ---------------------------------------------------------------------------


def test_render_session_start_profiles_before_episodes(tmp_path, monkeypatch):
    """Profiles block appears before ## Episode index in the output."""
    vault = tmp_path / "obsidian-brain"
    profiles_dir = vault / "memem" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "profile_user.md").write_text(
        "# User Profile\n\n## Preferences\n- Prefers directness\n"
    )
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault))

    all_mems = [_make_episodic_mem("ep000001", "My episode")]

    with patch("memem.obsidian_store._obsidian_memories", return_value=all_mems):
        result = render_session_start("someproject")

    if "# User Profile" in result and "## Episode index" in result:
        profile_pos = result.index("# User Profile")
        episode_pos = result.index("## Episode index")
        assert profile_pos < episode_pos, (
            f"Profiles block should appear before episode index. "
            f"Profile at {profile_pos}, Episode at {episode_pos}"
        )
