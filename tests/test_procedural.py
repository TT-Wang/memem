"""Tests for M-1: procedural memory layer (queued instruction rewrites)."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_procedural_mem(
    title: str = "Instruction suggestion: do not add comments",
    reason: str = "User said: don't add comments unless asked",
    current_text: str | None = "Add inline comments to complex code",
    proposed_text: str = "Do not add comments unless the user explicitly asks",
    status: str = "pending_review",
    created_iso: str = "",
) -> dict[str, Any]:
    """Build a synthetic procedural-suggestion memory dict."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if current_text:
        body = (
            f"## Instruction rewrite suggestion\n\n"
            f"**Reason:** {reason}\n\n"
            f"**Current:**\n```\n{current_text}\n```\n\n"
            f"**Proposed:**\n```\n{proposed_text}\n```"
        )
    else:
        body = (
            f"## New instruction suggestion\n\n"
            f"**Reason:** {reason}\n\n"
            f"**Proposed:**\n```\n{proposed_text}\n```"
        )
    mem_id = f"proc-{title.replace(' ', '-')[:12]}-{'a' * 8}"
    return {
        "id": mem_id,
        "title": title,
        "essence": body,
        "full_record": body,
        "domain_tags": ["procedural", "suggestion", "pending", "kind:procedural-suggestion"],
        "project": "general",
        "source_type": "mined",
        "source_session": "test1234",
        "importance": 4,
        "layer": 1,
        "status": status,
        "created_at": created_iso or now_str,
        "updated_at": now_str,
        "created_iso": created_iso or now_str,
        "schema_version": 1,
        "invalid_at": None,
        "replaced_by": None,
        "decay_immune": False,
        "access_count": 0,
        "last_accessed_at": now_str,
        "valid_to": "",
        "valid_at": now_str,
    }


# ---------------------------------------------------------------------------
# Test 1: mine_session emits a procedural suggestion when correction is found
# ---------------------------------------------------------------------------


def test_mine_session_emits_procedural_suggestion(tmp_vault, tmp_cortex_dir, tmp_path, monkeypatch):
    """When transcript has a clear user correction and CLAUDE.md exists,
    a kind:procedural-suggestion memory should be saved."""
    import json as _json

    import memem.obsidian_store as obs
    importlib.reload(obs)

    # Create a CLAUDE.md in the session cwd
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    claude_md = project_dir / "CLAUDE.md"
    claude_md.write_text("# Instructions\n\nAdd inline comments to complex code.\n", encoding="utf-8")

    # Create a synthetic JSONL session with a correction message
    session_file = tmp_path / "sessions" / "abc12345.jsonl"
    session_file.parent.mkdir()
    session_file.write_text(
        "\n".join([
            _json.dumps({"type": "user", "cwd": str(project_dir), "message": {"content": "don't add comments unless asked"}}),
            _json.dumps({"type": "assistant", "cwd": str(project_dir), "message": {"content": "understood"}}),
        ]),
        encoding="utf-8",
    )

    # The haiku system prompt returns a rewrite suggestion for the knowledge pass
    knowledge_response = _json.dumps([
        {"title": "No inline comments by default", "project": "myproject", "content": "Don't add comments unless asked.", "importance": 4}
    ])

    # Procedural haiku returns one rewrite
    procedural_response = _json.dumps([
        {
            "current_text": "Add inline comments to complex code.",
            "proposed_text": "Do not add inline comments unless the user explicitly asks.",
            "reason": "User explicitly said: don't add comments unless asked.",
        }
    ])

    saved_memories: list[dict] = []

    def fake_save_memory(mem: dict) -> None:
        saved_memories.append(mem)

    def fake_run(cmd, input="", **kwargs):  # noqa: A002
        # Return appropriate response based on prompt content
        if "CURRENT INSTRUCTIONS" in (input or ""):
            # Procedural pass
            return mock.Mock(returncode=0, stdout=procedural_response, stderr="")
        else:
            # Knowledge extraction pass
            return mock.Mock(returncode=0, stdout=knowledge_response, stderr="")

    monkeypatch.setattr("memem.mining.subprocess.run", fake_run)
    monkeypatch.setattr("memem.mining._save_memory", fake_save_memory)
    monkeypatch.setattr("memem.mining._make_memory", obs._make_memory)
    monkeypatch.setattr("memem.mining._find_best_match", lambda *a, **kw: (None, 0.0))
    monkeypatch.setattr("memem.session_state.load_mined_session_state", lambda: {})
    monkeypatch.setattr("memem.session_state.session_is_complete", lambda p, s: False)
    monkeypatch.setattr("memem.session_state.update_session_state", lambda *a, **kw: None)
    monkeypatch.setattr("memem.mining._generate_index", lambda: None)
    monkeypatch.setattr("memem.mining._obsidian_memories", lambda: [])
    monkeypatch.setattr("memem.mining._deprecate_memory", lambda *a, **kw: None)

    from memem.mining import mine_session

    mine_session(str(session_file))

    # At least one procedural-suggestion memory should be saved
    procedural_mems = [
        m for m in saved_memories
        if "kind:procedural-suggestion" in (m.get("domain_tags") or [])
    ]
    assert len(procedural_mems) >= 1, (
        f"Expected at least 1 procedural-suggestion memory, got {len(procedural_mems)}. "
        f"All saved memories: {[m.get('domain_tags') for m in saved_memories]}"
    )
    ps = procedural_mems[0]
    assert ps.get("status") == "pending_review"
    assert "pending" in (ps.get("domain_tags") or [])
    assert ps.get("importance") == 4


# ---------------------------------------------------------------------------
# Test 2: No CLAUDE.md → procedural pass skipped
# ---------------------------------------------------------------------------


def test_mine_session_no_claudemd_no_suggestion(tmp_vault, tmp_cortex_dir, tmp_path, monkeypatch):
    """If CLAUDE.md does not exist, the procedural pass is skipped entirely."""
    import json as _json

    import memem.obsidian_store as obs
    importlib.reload(obs)

    # Project dir WITHOUT CLAUDE.md
    project_dir = tmp_path / "noconfig"
    project_dir.mkdir()
    # (no CLAUDE.md created)

    session_file = tmp_path / "sessions2" / "def98765.jsonl"
    session_file.parent.mkdir()
    session_file.write_text(
        _json.dumps({"type": "user", "cwd": str(project_dir), "message": {"content": "don't add comments please"}}),
        encoding="utf-8",
    )

    knowledge_response = _json.dumps([
        {"title": "Some fact", "project": "noconfig", "content": "A useful fact.", "importance": 3}
    ])

    procedural_called = []

    def fake_run(cmd, input="", **kwargs):  # noqa: A002
        if "CURRENT INSTRUCTIONS" in (input or ""):
            procedural_called.append(True)
        return mock.Mock(returncode=0, stdout=knowledge_response, stderr="")

    saved_memories: list[dict] = []

    def fake_save_memory(mem: dict) -> None:
        saved_memories.append(mem)

    monkeypatch.setattr("memem.mining.subprocess.run", fake_run)
    monkeypatch.setattr("memem.mining._save_memory", fake_save_memory)
    monkeypatch.setattr("memem.mining._make_memory", obs._make_memory)
    monkeypatch.setattr("memem.mining._find_best_match", lambda *a, **kw: (None, 0.0))
    monkeypatch.setattr("memem.session_state.load_mined_session_state", lambda: {})
    monkeypatch.setattr("memem.session_state.session_is_complete", lambda p, s: False)
    monkeypatch.setattr("memem.session_state.update_session_state", lambda *a, **kw: None)
    monkeypatch.setattr("memem.mining._generate_index", lambda: None)
    monkeypatch.setattr("memem.mining._obsidian_memories", lambda: [])
    monkeypatch.setattr("memem.mining._deprecate_memory", lambda *a, **kw: None)

    from memem.mining import mine_session

    mine_session(str(session_file))

    # Procedural Haiku should NOT have been called
    assert not procedural_called, "Procedural pass was called despite missing CLAUDE.md"
    procedural_mems = [
        m for m in saved_memories
        if "kind:procedural-suggestion" in (m.get("domain_tags") or [])
    ]
    assert len(procedural_mems) == 0, f"Unexpected procedural memories saved: {procedural_mems}"


# ---------------------------------------------------------------------------
# Test 5: Old suggestions get auto-archived after TTL
# ---------------------------------------------------------------------------


def test_old_suggestions_auto_archived(tmp_vault, tmp_cortex_dir, monkeypatch):
    """A procedural-suggestion memory older than MEMEM_PROCEDURAL_TTL_DAYS is archived."""
    import memem.obsidian_store as obs
    importlib.reload(obs)

    now = datetime.now(UTC)
    old_iso = (now - timedelta(days=8)).isoformat()
    old_mem = _make_procedural_mem(
        title="Old suggestion to archive",
        status="pending_review",
        created_iso=old_iso,
    )

    # Track writes
    updated_statuses: list[str] = []

    def fake_write(mem: dict) -> None:
        updated_statuses.append(mem.get("status", ""))

    def fake_find(mem_id: str) -> dict | None:
        if old_mem["id"] == mem_id or old_mem["id"].startswith(mem_id):
            return dict(old_mem)
        return None

    monkeypatch.setattr("memem.miner_daemon.MEMEM_PROCEDURAL_TTL_DAYS", 7)

    with mock.patch("memem.obsidian_store._obsidian_memories", return_value=[old_mem]):
        with mock.patch("memem.obsidian_store._find_memory", side_effect=fake_find):
            with mock.patch("memem.obsidian_store._write_obsidian_memory", side_effect=fake_write):
                with mock.patch("memem.obsidian_store._cache_refresh_from_disk", return_value=None):
                    from memem.miner_daemon import _archive_expired_procedural_suggestions

                    count = _archive_expired_procedural_suggestions()

    assert count == 1, f"Expected 1 archived suggestion, got {count}"
    assert "expired" in updated_statuses, f"Expected status 'expired' in writes, got {updated_statuses}"
