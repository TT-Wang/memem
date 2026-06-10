"""Tests for the --purge-contaminated CLI command.

Verifies:
  - Dry-run (default): prints contaminated memories, does NOT delete vault files
  - --apply: deletes only contaminated memories, leaves clean ones intact
  - Uses tmp vault via MEMEM_OBSIDIAN_VAULT/MEMEM_DIR env overrides
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace


def _dispatch(argv, capsys):
    """Reload cli each call so patched env vars propagate into its imports."""
    from memem import cli
    importlib.reload(cli)
    mcp_stub = SimpleNamespace(run=lambda **_: None)
    cli.dispatch_cli(["memem", *argv], mcp_stub)
    return capsys.readouterr()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_clean_memory(obsidian_store, title="Clean memory", content=None):
    """Create and save a clean (non-contaminated) memory."""
    if content is None:
        content = "This is a normal memory about pytest testing patterns."
    mem = obsidian_store._make_memory(content=content, title=title, source_type="user")
    obsidian_store._save_memory(mem)
    return mem


def _make_contaminated_memory(obsidian_store, marker, title=None):
    """Create and save a contaminated memory containing a mining marker.

    Simulates what happened when mine_delta accidentally stored the mining
    prompt as the memory content. The marker is injected into the essence
    field directly, then the file is written and the cache is refreshed.
    """
    if title is None:
        title = "Contaminated: " + marker[:30]
    mem = obsidian_store._make_memory(
        content="This memory contains mining artifact content that should be purged.",
        title=title,
        source_type="mined",
    )
    # Inject the marker into the essence directly.
    mem["essence"] = marker + "\nSome additional content here."
    # Write to disk and refresh cache so _obsidian_memories() sees the contaminated essence.
    obsidian_store._write_obsidian_memory(mem)
    obsidian_store._cache_refresh_from_disk(mem.get("id", ""))
    return mem


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_purge_contaminated_dry_run_no_vault_change(tmp_vault, tmp_cortex_dir, capsys):
    """Dry-run must print contaminated memories but NEVER delete any vault file."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    clean = _make_clean_memory(obsidian_store)
    contaminated = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===")

    vault_files_before = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))
    assert len(vault_files_before) == 2, "Expected 2 memory files before dry-run"

    out = _dispatch(["--purge-contaminated"], capsys)

    # Vault must be unchanged
    vault_files_after = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))
    assert vault_files_before == vault_files_after, (
        "Dry-run must not delete any vault files"
    )

    # Output must mention the contaminated memory
    output_text = out.out + out.err
    assert "contaminated" in output_text.lower() or "dry" in output_text.lower(), (
        "Dry-run output must mention contaminated count"
    )
    # Must show count of 1
    assert "1" in output_text, "Output must show count of contaminated memories"
    # Must mention --apply
    assert "--apply" in output_text, "Dry-run output must tell user to run with --apply"


def test_purge_contaminated_apply_deletes_only_contaminated(tmp_vault, tmp_cortex_dir, capsys):
    """--apply must delete only contaminated memories, leaving clean ones intact."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    clean = _make_clean_memory(obsidian_store, title="Clean memory to keep")
    cont1 = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: begin conv")
    cont2 = _make_contaminated_memory(obsidian_store, "Below is a coding conversation", title="Artifact: coding conv")

    vault_before = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))
    assert len(vault_before) == 3

    out = _dispatch(["--purge-contaminated", "--apply"], capsys)

    # Check output
    output_text = out.out + out.err
    assert "2" in output_text, "Output must report 2 deleted memories"

    # Vault: only the clean memory should remain
    importlib.reload(obsidian_store)
    remaining = obsidian_store._obsidian_memories(include_deprecated=True)
    assert len(remaining) == 1, f"Only 1 clean memory should remain, got {len(remaining)}"
    assert remaining[0]["title"] == "Clean memory to keep"


def test_purge_contaminated_apply_task_notification_marker(tmp_vault, tmp_cortex_dir, capsys):
    """--apply must also detect '<task-notification' marker."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont = _make_contaminated_memory(
        obsidian_store, "<task-notification type='stop'>", title="Task notification artifact"
    )
    clean = _make_clean_memory(obsidian_store, title="A normal clean memory")

    out = _dispatch(["--purge-contaminated", "--apply"], capsys)

    importlib.reload(obsidian_store)
    remaining = obsidian_store._obsidian_memories(include_deprecated=True)
    assert len(remaining) == 1
    assert remaining[0]["title"] == "A normal clean memory"


def test_purge_contaminated_no_contamination(tmp_vault, tmp_cortex_dir, capsys):
    """When vault has no contaminated memories, output says none found."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    _make_clean_memory(obsidian_store, title="All clean memory 1")
    _make_clean_memory(obsidian_store, title="All clean memory 2", content="Another normal memory for testing purposes here.")

    out = _dispatch(["--purge-contaminated"], capsys)
    output_text = out.out + out.err
    assert "no contaminated" in output_text.lower() or "0" in output_text, (
        "Output must indicate no contaminated memories found"
    )


def test_purge_contaminated_dry_run_contaminated_title(tmp_vault, tmp_cortex_dir, capsys):
    """Dry-run detects contamination in the title field (not just essence)."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    # Directly create a memory where the title contains the marker
    mem = obsidian_store._make_memory(
        content="Normal content that is long enough to pass length check.",
        title="Normal memory title",
        source_type="mined",
    )
    # Inject marker into title field directly
    mem["title"] = "=== BEGIN CONVERSATION === mining residue"
    obsidian_store._write_obsidian_memory(mem)

    out = _dispatch(["--purge-contaminated"], capsys)
    output_text = out.out + out.err
    assert "1" in output_text, "Contaminated title must be detected in dry-run"
    assert "--apply" in output_text
