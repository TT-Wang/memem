"""Tests for memem/compaction.py — compaction-survivor checkpoint.

Tests:
1. Synthetic transcript at 85% byte threshold triggers checkpoint (detect_compaction_risk returns True)
2. Below threshold (50%) does NOT trigger
3. build_compaction_snapshot returns all 4 sections
4. save_compaction_checkpoint round-trips: save -> read back -> kind == 'compaction-checkpoint'
5. Threshold overridable via MEMEM_COMPACTION_THRESHOLD env var
6. End-to-end: kind survives whitelist normalization (covered by the round-trip test in #4)
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTEXT_WINDOW_BYTES = 200_000 * 4  # 800_000 bytes


def _write_jsonl_transcript(path: Path, size_bytes: int) -> None:
    """Write a synthetic JSONL transcript file of approximately `size_bytes` bytes.

    Fills the file with valid JSON lines representing a minimal assistant/user
    exchange so the parser can iterate without errors.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    base_line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "A" * 500},
            ]
        },
    }) + "\n"
    line_len = len(base_line.encode("utf-8"))
    n_lines = max(1, size_bytes // line_len + 1)

    with path.open("w", encoding="utf-8") as fh:
        for _ in range(n_lines):
            fh.write(base_line)
    # Truncate to exact size if overshot — close enough for threshold tests.
    actual = path.stat().st_size
    if actual > size_bytes:
        with path.open("r+b") as fh:
            fh.truncate(size_bytes)


def _make_minimal_transcript(path: Path, n_tool_use: int = 3) -> None:
    """Write a minimal JSONL transcript with code tool_use blocks and a blocked line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []

    # User message.
    lines.append(json.dumps({
        "type": "user",
        "message": {"content": "Please fix the auth bug"},
    }))

    # Assistant message with tool_use and text containing 'blocked'.
    tool_blocks = []
    for i in range(n_tool_use):
        tool_blocks.append({
            "type": "tool_use",
            "name": "Edit",
            "id": f"tool_{i}",
            "input": {"file_path": f"/tmp/file_{i}.py"},
        })
    tool_blocks.append({
        "type": "text",
        "text": "I am blocked by a circular import issue in the auth module.",
    })

    lines.append(json.dumps({
        "type": "assistant",
        "message": {"content": tool_blocks},
    }))

    # Another assistant message with Bash tool_use.
    lines.append(json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "id": "bash_1",
                    "input": {"command": "pytest tests/"},
                },
                {
                    "type": "text",
                    "text": "The issue is still present in the test suite.",
                },
            ]
        },
    }))

    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reload_compaction(tmp_vault, tmp_cortex_dir, monkeypatch):
    """Reload compaction module with fresh env after vault/dir fixtures set up."""
    monkeypatch.delenv("MEMEM_COMPACTION_THRESHOLD", raising=False)
    # Ensure fresh module import.
    if "memem.compaction" in sys.modules:
        del sys.modules["memem.compaction"]
    # Also reload obsidian_store cache.
    from memem import obsidian_store
    obsidian_store._reset_cache()
    yield
    # Cleanup module cache after test.
    for mod in list(sys.modules.keys()):
        if mod.startswith("memem.compaction"):
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Test 1: 85% transcript triggers risk
# ---------------------------------------------------------------------------

def test_detect_compaction_risk_above_threshold(tmp_path):
    """A transcript at 85% of the context window should trigger compaction risk."""
    transcript = tmp_path / "session.jsonl"
    target_size = int(_CONTEXT_WINDOW_BYTES * 0.85)
    _write_jsonl_transcript(transcript, target_size)

    from memem.compaction import detect_compaction_risk
    assert detect_compaction_risk(str(transcript)) is True


# ---------------------------------------------------------------------------
# Test 2: 50% does NOT trigger
# ---------------------------------------------------------------------------

def test_detect_compaction_risk_below_threshold(tmp_path):
    """A transcript at 50% of the context window should NOT trigger compaction risk."""
    transcript = tmp_path / "session.jsonl"
    target_size = int(_CONTEXT_WINDOW_BYTES * 0.50)
    _write_jsonl_transcript(transcript, target_size)

    from memem.compaction import detect_compaction_risk
    assert detect_compaction_risk(str(transcript)) is False


# ---------------------------------------------------------------------------
# Test 3: build_compaction_snapshot returns all 4 sections
# ---------------------------------------------------------------------------

def test_build_compaction_snapshot_all_sections(tmp_path, tmp_cortex_dir):
    """build_compaction_snapshot must return dict with all 4 required keys."""
    transcript = tmp_path / "session.jsonl"
    _make_minimal_transcript(transcript, n_tool_use=3)

    # Pre-populate working_memory.md.
    wm_path = tmp_cortex_dir / "working_memory.md"
    wm_path.write_text(
        "## current_task\n\nFix auth bug\n\n"
        "## stuck_on\n\nBlocked by circular import\n\n"
        "## active_hypothesis\n\n\n\n"
        "## last_3_actions\n\n\n\n"
        "## decided_this_session\n\n\n\n",
        encoding="utf-8",
    )

    # Patch the working memory path if needed via monkeypatch is not available here
    # but working_memory reads from MEMEM_DIR which is set by tmp_cortex_dir fixture.
    importlib.reload(importlib.import_module("memem.working_memory"))

    from memem.compaction import build_compaction_snapshot

    snapshot = build_compaction_snapshot(
        session_id="test-session-001",
        transcript_path=str(transcript),
        memem_dir=tmp_cortex_dir,
    )

    assert "working_memory" in snapshot, "snapshot missing 'working_memory' key"
    assert "decisions" in snapshot, "snapshot missing 'decisions' key"
    assert "tensions" in snapshot, "snapshot missing 'tensions' key"
    assert "code_changes" in snapshot, "snapshot missing 'code_changes' key"


def test_build_compaction_snapshot_tensions_from_transcript(tmp_path, tmp_cortex_dir):
    """Tensions section should capture 'blocked'/'issue' lines from assistant messages."""
    transcript = tmp_path / "session.jsonl"
    _make_minimal_transcript(transcript, n_tool_use=2)

    from memem.compaction import build_compaction_snapshot

    snapshot = build_compaction_snapshot(
        session_id="test-session-002",
        transcript_path=str(transcript),
        memem_dir=tmp_cortex_dir,
    )

    tensions = snapshot["tensions"]
    assert isinstance(tensions, str)
    # The transcript has 'blocked' and 'issue' in assistant text.
    lower = tensions.lower()
    assert "blocked" in lower or "issue" in lower, (
        f"Expected 'blocked' or 'issue' in tensions, got: {tensions!r}"
    )


def test_build_compaction_snapshot_code_changes(tmp_path, tmp_cortex_dir):
    """code_changes should contain up to 5 Edit/Write/MultiEdit/Bash tool_use blocks."""
    transcript = tmp_path / "session.jsonl"
    _make_minimal_transcript(transcript, n_tool_use=3)

    from memem.compaction import build_compaction_snapshot

    snapshot = build_compaction_snapshot(
        session_id="test-session-003",
        transcript_path=str(transcript),
        memem_dir=tmp_cortex_dir,
    )

    code_changes = snapshot["code_changes"]
    assert isinstance(code_changes, list), "code_changes should be a list"
    assert len(code_changes) > 0, "code_changes should not be empty"
    assert len(code_changes) <= 5, "code_changes should cap at 5"

    for block in code_changes:
        assert block.get("name") in {"Edit", "Write", "MultiEdit", "Bash"}, (
            f"Unexpected tool name in code_changes: {block.get('name')!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: Round-trip — save -> load -> kind == 'compaction-checkpoint'
# ---------------------------------------------------------------------------

def test_save_compaction_checkpoint_roundtrip(tmp_path, tmp_vault, tmp_cortex_dir):
    """save_compaction_checkpoint round-trips: saved memory has kind:compaction-checkpoint tag."""
    transcript = tmp_path / "session.jsonl"
    _make_minimal_transcript(transcript)

    from memem.compaction import build_compaction_snapshot, save_compaction_checkpoint
    from memem.obsidian_store import _find_memory, _reset_cache

    snapshot = build_compaction_snapshot(
        session_id="test-session-rt",
        transcript_path=str(transcript),
        memem_dir=tmp_cortex_dir,
    )

    memory_id = save_compaction_checkpoint(
        snapshot=snapshot,
        session_id="test-session-rt",
        project_id="test-project",
    )

    assert isinstance(memory_id, str)
    assert len(memory_id) >= 8, f"memory_id should be 8 chars, got: {memory_id!r}"

    # Force cache refresh so read picks up from disk.
    _reset_cache()

    mem = _find_memory(memory_id)
    assert mem is not None, f"Memory not found for id: {memory_id!r}"

    tags = mem.get("domain_tags") or []
    assert "kind:compaction-checkpoint" in tags, (
        f"Expected 'kind:compaction-checkpoint' in domain_tags, got: {tags}"
    )
    assert "compaction" in tags
    assert "checkpoint" in tags

    # Source session should be set.
    assert mem.get("source_session") == "test-session-rt"

    # Content should include the kind header.
    body = mem.get("essence") or mem.get("full_record", "")
    assert "compaction-checkpoint" in body, (
        f"Expected 'compaction-checkpoint' in memory body, got: {body[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Threshold overridable via MEMEM_COMPACTION_THRESHOLD env var
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("threshold,size_factor,expected", [
    ("0.50", 0.60, True),   # size=60%, threshold=50% -> triggered
    ("0.50", 0.40, False),  # size=40%, threshold=50% -> not triggered
    ("0.90", 0.85, False),  # size=85%, threshold=90% -> not triggered
    ("0.90", 0.95, True),   # size=95%, threshold=90% -> triggered
])
def test_detect_compaction_risk_env_threshold(tmp_path, monkeypatch, threshold, size_factor, expected):
    """MEMEM_COMPACTION_THRESHOLD env var overrides the default 0.80 threshold."""
    monkeypatch.setenv("MEMEM_COMPACTION_THRESHOLD", threshold)

    # Reload module to pick up env change via _get_threshold().
    if "memem.compaction" in sys.modules:
        del sys.modules["memem.compaction"]

    from memem.compaction import detect_compaction_risk

    transcript = tmp_path / f"session_{threshold}_{size_factor}.jsonl"
    target_size = int(_CONTEXT_WINDOW_BYTES * size_factor)
    _write_jsonl_transcript(transcript, target_size)

    result = detect_compaction_risk(str(transcript))
    assert result is expected, (
        f"threshold={threshold} size_factor={size_factor}: "
        f"expected {expected}, got {result}"
    )


# ---------------------------------------------------------------------------
# Test 6: kind survives whitelist normalization (end-to-end round-trip)
# ---------------------------------------------------------------------------

def test_kind_survives_normalization_roundtrip(tmp_path, tmp_vault, tmp_cortex_dir):
    """End-to-end: kind:compaction-checkpoint tag is preserved through save→load cycle.

    This explicitly validates that no whitelist or normalization layer in
    obsidian_store or the cache drops the kind:compaction-checkpoint tag.
    """
    transcript = tmp_path / "session_e2e.jsonl"
    _make_minimal_transcript(transcript)

    from memem.compaction import build_compaction_snapshot, save_compaction_checkpoint
    from memem.obsidian_store import _obsidian_memories, _reset_cache

    snapshot = build_compaction_snapshot(
        session_id="e2e-session",
        transcript_path=str(transcript),
        memem_dir=tmp_cortex_dir,
    )

    _ = save_compaction_checkpoint(
        snapshot=snapshot,
        session_id="e2e-session",
        project_id="general",
    )

    # Force disk re-read via cache reset.
    _reset_cache()

    # Load via the bulk listing path (same path session-start briefing uses).
    all_mems = _obsidian_memories()
    checkpoint_mems = [
        m for m in all_mems
        if "kind:compaction-checkpoint" in (m.get("domain_tags") or [])
    ]

    assert len(checkpoint_mems) >= 1, (
        "No memories with kind:compaction-checkpoint found. All mems: "
        + str([(m.get("id", "")[:8], m.get("domain_tags")) for m in all_mems])
    )

    # Explicit assertion: kind survives normalization.
    saved = checkpoint_mems[0]
    assert "kind:compaction-checkpoint" in (saved.get("domain_tags") or []), (
        "kind:compaction-checkpoint was dropped by normalization. "
        f"domain_tags={saved.get('domain_tags')!r}"
    )


# ---------------------------------------------------------------------------
# Test: missing transcript is handled gracefully
# ---------------------------------------------------------------------------

def test_detect_compaction_risk_missing_file():
    """detect_compaction_risk returns False when transcript file does not exist."""
    from memem.compaction import detect_compaction_risk
    assert detect_compaction_risk("/nonexistent/path/session.jsonl") is False
