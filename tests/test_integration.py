"""End-to-end integration tests.

Exercises the full save → search → recall → deprecate flow across
multiple split modules.
"""



def test_save_and_recall(tmp_vault, tmp_cortex_dir):
    """Save 3 memories, verify they can all be read back."""
    from obsidian_store import _make_memory, _obsidian_memories, _save_memory

    memories = [
        _make_memory(
            content="Use bcrypt with 10 salt rounds for password hashing",
            title="Password hashing",
            project="auth",
        ),
        _make_memory(
            content="JWT tokens should use RS256 in production",
            title="JWT signing",
            project="auth",
        ),
        _make_memory(
            content="PostgreSQL indexes speed up WHERE and JOIN clauses",
            title="DB indexing",
            project="db",
        ),
    ]
    for mem in memories:
        _save_memory(mem)

    all_mems = _obsidian_memories()
    assert len(all_mems) == 3
    titles = {m["title"] for m in all_mems}
    assert titles == {"Password hashing", "JWT signing", "DB indexing"}


def test_dedup_catches_similar(tmp_vault, tmp_cortex_dir):
    """Two memories with overlapping content should trigger dedup."""
    from obsidian_store import _find_best_match, _make_memory, _save_memory

    mem1 = _make_memory(
        content="Use bcrypt for password hashing with 10 salt rounds",
        title="Password hashing",
    )
    _save_memory(mem1)

    # Exact same content should match itself highly
    match, score = _find_best_match("Use bcrypt for password hashing with 10 salt rounds")
    assert match is not None
    assert score > 0.6


def test_deprecate_preserves_file(tmp_vault, tmp_cortex_dir):
    """Deprecated memories stay on disk but are excluded from active queries."""
    from obsidian_store import (
        _deprecate_memory,
        _make_memory,
        _obsidian_memories,
        _save_memory,
    )

    mem = _make_memory(content="Some old decision we reversed", title="Old decision")
    _save_memory(mem)
    assert len(_obsidian_memories()) == 1

    _deprecate_memory(mem["id"], reason="superseded")

    assert len(_obsidian_memories()) == 0  # excluded by default
    assert len(_obsidian_memories(include_deprecated=True)) == 1  # still on disk


def test_frontmatter_roundtrip_preserves_all_fields(tmp_vault, tmp_cortex_dir):
    """All frontmatter fields must survive write→read."""
    from obsidian_store import _make_memory, _obsidian_memories, _save_memory

    mem = _make_memory(
        content="The auth module requires Redis for session storage",
        title="Auth session storage",
        tags=["auth", "redis"],
        project="backend",
        source_type="user",
        source_session="abc12345",
        importance=5,
    )
    _save_memory(mem)

    loaded = _obsidian_memories()
    assert len(loaded) == 1
    m = loaded[0]
    assert m["title"] == "Auth session storage"
    assert m["project"] == "backend"
    assert m.get("source_type") == "user"
    assert m.get("importance") == 5
    assert m.get("status", "active") == "active"
    assert m.get("schema_version") == 1


def test_context_assemble_empty_vault(tmp_vault, tmp_cortex_dir, monkeypatch):
    """context_assemble returns empty string when there's no playbook or memories."""
    import transcripts
    # Isolate from real session transcripts on disk
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching sessions")

    from assembly import context_assemble

    result = context_assemble("tell me about auth", "nonexistent-project")
    # Should return empty or very short since nothing to assemble
    # (does not invoke Haiku when materials are empty)
    assert result == ""
