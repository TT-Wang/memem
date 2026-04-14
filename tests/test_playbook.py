"""Tests for playbook refinement.

v0.9.3: playbooks are now derived directly from memories (no staging).
Refine is gated on a source-hash so re-running is a no-op when nothing
changed, and a minimum-memory threshold avoids writing playbooks for
projects with too little content to be useful.
"""


def _make_memory(project: str, title: str, essence: str, mid: str) -> dict:
    return {
        "id": mid,
        "title": title,
        "essence": essence,
        "project": project,
        "status": "active",
        "source_type": "mined",
    }


def test_playbook_refine_skips_below_threshold(tmp_vault, monkeypatch):
    from memem.playbook import MIN_MEMORIES_FOR_PLAYBOOK, _playbook_refine
    memories = [_make_memory("tiny", f"Lesson {i}", f"content {i}", f"id{i}")
                for i in range(MIN_MEMORIES_FOR_PLAYBOOK - 1)]
    monkeypatch.setattr("memem.playbook._obsidian_memories", lambda: memories)
    result = _playbook_refine("tiny")
    assert result["action"] == "skipped_few_memories"
    assert result["count"] == MIN_MEMORIES_FOR_PLAYBOOK - 1


def test_playbook_refine_writes_raw_when_small(tmp_vault, monkeypatch):
    from memem.models import PLAYBOOK_DIR
    from memem.playbook import _playbook_refine
    memories = [_make_memory("myproj", f"Lesson {i}", f"short content {i}", f"id{i}")
                for i in range(6)]
    monkeypatch.setattr("memem.playbook._obsidian_memories", lambda: memories)
    result = _playbook_refine("myproj")
    assert result["action"] == "written_raw"
    playbook = PLAYBOOK_DIR / "myproj.md"
    assert playbook.exists()
    text = playbook.read_text()
    assert "myproj — Project Playbook" in text
    assert "Lesson 0" in text
    assert "<!-- memem-source-hash:" in text


def test_playbook_refine_skips_unchanged(tmp_vault, monkeypatch):
    from memem.playbook import _playbook_refine
    memories = [_make_memory("myproj", f"Lesson {i}", f"content {i}", f"id{i}")
                for i in range(6)]
    monkeypatch.setattr("memem.playbook._obsidian_memories", lambda: memories)
    _playbook_refine("myproj")
    # Second call with identical memories should be a no-op.
    result = _playbook_refine("myproj")
    assert result["action"] == "skipped_unchanged"


def test_playbook_refine_rewrites_when_memories_change(tmp_vault, monkeypatch):
    from memem.playbook import _playbook_refine
    memories = [_make_memory("myproj", f"Lesson {i}", f"content {i}", f"id{i}")
                for i in range(6)]
    monkeypatch.setattr("memem.playbook._obsidian_memories", lambda: memories)
    _playbook_refine("myproj")
    # Add a new memory — source hash should change and trigger a rewrite.
    memories.append(_make_memory("myproj", "New lesson", "new content", "id6"))
    monkeypatch.setattr("memem.playbook._obsidian_memories", lambda: memories)
    result = _playbook_refine("myproj")
    assert result["action"] in ("written_raw", "written_refined")


def test_playbook_sweep_covers_all_projects(tmp_vault, monkeypatch):
    from memem.playbook import MIN_MEMORIES_FOR_PLAYBOOK, _playbook_sweep
    memories = []
    # Two projects above threshold, one below.
    for i in range(6):
        memories.append(_make_memory("proj_a", f"A{i}", f"a-content-{i}", f"a{i}"))
    for i in range(6):
        memories.append(_make_memory("proj_b", f"B{i}", f"b-content-{i}", f"b{i}"))
    for i in range(MIN_MEMORIES_FOR_PLAYBOOK - 1):
        memories.append(_make_memory("proj_tiny", f"T{i}", f"t-{i}", f"t{i}"))
    monkeypatch.setattr("memem.playbook._obsidian_memories", lambda: memories)

    totals = _playbook_sweep()
    assert totals["refreshed"] == 2  # proj_a and proj_b
    assert totals["skipped"] == 1    # proj_tiny (below threshold)
    assert totals["failed"] == 0

    # Second sweep should be a no-op for the two refreshed projects.
    totals2 = _playbook_sweep()
    assert totals2["refreshed"] == 0
    assert totals2["noop"] == 2
