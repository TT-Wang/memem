"""Tests for m1: Working-memory surface (memem v1.4.0).

Covers the public API of memem.working_memory and the post-stop hook
integration that updates working_memory.md after each assistant turn.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "post-stop-attribution.sh"


# ===========================================================================
# Helpers — import working_memory with a patched MEMEM_DIR so tests use tmp
# ===========================================================================


def _wm_module(tmp_path: Path):
    """Return working_memory module with WORKING_MEMORY_FILE pointing to tmp_path."""
    import memem.working_memory as wm_mod

    # Patch the module-level constants to use tmp_path.
    patched_file = tmp_path / "working_memory.md"
    with mock.patch.object(wm_mod, "WORKING_MEMORY_FILE", patched_file):
        yield wm_mod, patched_file


# ===========================================================================
# 1. serialize_to_md / parse_from_md round-trip
# ===========================================================================


def test_serialize_round_trip():
    from memem.working_memory import ALLOWED_SECTIONS, parse_from_md, serialize_to_md

    sections = {name: f"body for {name}" for name in ALLOWED_SECTIONS}
    md = serialize_to_md(sections)
    recovered = parse_from_md(md)

    for name in ALLOWED_SECTIONS:
        assert recovered[name] == sections[name], (
            f"Section {name!r} did not round-trip: got {recovered[name]!r}"
        )


# ===========================================================================
# 2. parse_from_md tolerates missing sections
# ===========================================================================


def test_parse_tolerates_missing_sections():
    from memem.working_memory import ALLOWED_SECTIONS, parse_from_md

    partial_md = "## current_task\n\nfix the bug\n\n## stuck_on\n\nnot sure yet\n\n"
    result = parse_from_md(partial_md)

    assert result["current_task"] == "fix the bug"
    assert result["stuck_on"] == "not sure yet"

    # Missing sections must be empty strings, not KeyError
    for name in ALLOWED_SECTIONS:
        assert name in result, f"Missing key: {name}"
        if name not in ("current_task", "stuck_on"):
            assert result[name] == "", f"Expected empty string for {name}, got {result[name]!r}"


# ===========================================================================
# 3. write_working_memory is atomic
# ===========================================================================


def test_write_atomic(tmp_path):
    import memem.working_memory as wm_mod

    wm_file = tmp_path / "working_memory.md"
    sections = {name: f"initial {name}" for name in wm_mod.ALLOWED_SECTIONS}

    with mock.patch.object(wm_mod, "WORKING_MEMORY_FILE", wm_file):
        wm_mod.write_working_memory(sections)
        assert wm_file.exists(), "working_memory.md was not created"
        content = wm_file.read_text(encoding="utf-8")
        # All sections should appear
        for name in wm_mod.ALLOWED_SECTIONS:
            assert f"## {name}" in content

        # Write again with different content — prior file should be fully replaced
        new_sections = {name: f"updated {name}" for name in wm_mod.ALLOWED_SECTIONS}
        wm_mod.write_working_memory(new_sections)
        new_content = wm_file.read_text(encoding="utf-8")
        assert "updated current_task" in new_content
        assert "initial current_task" not in new_content


# ===========================================================================
# 4. update_section changes only the target section
# ===========================================================================


def test_update_section_only_changes_target(tmp_path):
    import memem.working_memory as wm_mod

    wm_file = tmp_path / "working_memory.md"
    original = {name: f"value_for_{name}" for name in wm_mod.ALLOWED_SECTIONS}

    with mock.patch.object(wm_mod, "WORKING_MEMORY_FILE", wm_file):
        wm_mod.write_working_memory(original)
        wm_mod.update_section("current_task", "NEW TASK")
        result = wm_mod.read_working_memory()

    assert result["current_task"] == "NEW TASK"
    for name in wm_mod.ALLOWED_SECTIONS:
        if name != "current_task":
            assert result[name] == original[name], (
                f"Section {name!r} was unexpectedly mutated"
            )


# ===========================================================================
# 5. read_working_memory returns empty dict when file is missing
# ===========================================================================


def test_read_returns_empty_dict_when_missing(tmp_path):
    import memem.working_memory as wm_mod

    missing_file = tmp_path / "nonexistent" / "working_memory.md"
    with mock.patch.object(wm_mod, "WORKING_MEMORY_FILE", missing_file):
        result = wm_mod.read_working_memory()

    assert result == {}, f"Expected empty dict, got {result!r}"


# ===========================================================================
# 6. update_section rejects invalid section names
# ===========================================================================


def test_invalid_section_rejected(tmp_path):
    import memem.working_memory as wm_mod

    wm_file = tmp_path / "working_memory.md"
    with mock.patch.object(wm_mod, "WORKING_MEMORY_FILE", wm_file), \
         pytest.raises(ValueError, match="Invalid section"):
        wm_mod.update_section("not_a_section", "some body")


# ===========================================================================
# 7. Hook integration: post-stop hook updates current_task + last_3_actions
# ===========================================================================


def _make_transcript(tmp_path: Path, user_messages: list[str], assistant_text: str) -> Path:
    """Write a synthetic JSONL transcript and return its path."""
    transcript = tmp_path / "transcript.jsonl"
    lines = []
    for msg in user_messages:
        lines.append(json.dumps({
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": msg}]
            }
        }))
    lines.append(json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": assistant_text}]
        }
    }))
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


def _make_active_slices(tmp_path: Path, session_id: str) -> Path:
    """Write a minimal active-slices.jsonl so the hook doesn't exit early."""
    slices_file = tmp_path / "active-slices.jsonl"
    slices_file.write_text(
        json.dumps({"session_id": session_id, "items": [], "query": "test"}) + "\n",
        encoding="utf-8",
    )
    return slices_file


def _make_stub_plugin_root(tmp_path: Path) -> Path:
    """Create a minimal plugin root with working_memory.py importable."""
    stub_root = tmp_path / "plugin_root"
    stub_root.mkdir()
    # Symlink the real memem package so working_memory is importable
    real_memem = REPO / "memem"
    (stub_root / "memem").symlink_to(real_memem)
    return stub_root


def test_post_stop_hook_updates_working_memory(tmp_path):
    """Simulate the post-stop hook with a synthetic transcript and verify working_memory.md."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    session_id = "test-session-wm-update"

    user_messages = ["first task", "second task", "implement the feature"]
    transcript = _make_transcript(tmp_path, user_messages, "I implemented it.")
    _make_active_slices(memem_dir, session_id)
    stub_root = _make_stub_plugin_root(tmp_path)

    hook_input = json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript),
    })

    env = os.environ.copy()
    env["MEMEM_DIR"] = str(memem_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(stub_root)
    env["PYTHONPATH"] = str(stub_root)

    result = subprocess.run(
        ["bash", str(HOOK)],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=40,
        env=env,
    )
    # Hook is silent on success (exit 0); may also exit 0 if attribution import fails.
    # We only check working_memory.md was written.
    wm_file = memem_dir / "working_memory.md"
    assert wm_file.exists(), (
        f"working_memory.md was not created.\nhook stderr: {result.stderr}\nhook stdout: {result.stdout}"
    )

    content = wm_file.read_text(encoding="utf-8")
    # current_task must contain the last user message (possibly truncated)
    assert "implement the feature" in content, (
        f"current_task not set correctly. File content:\n{content}"
    )
    # last_3_actions must contain bullet points
    assert "- " in content, f"last_3_actions bullets missing. File content:\n{content}"
    assert "second task" in content or "first task" in content, (
        f"last_3_actions does not contain expected messages. File content:\n{content}"
    )
