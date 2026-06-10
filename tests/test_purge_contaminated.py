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


# ---------------------------------------------------------------------------
# --exclude flag tests
# ---------------------------------------------------------------------------

def test_purge_exclude_in_dry_run(tmp_vault, tmp_cortex_dir, capsys):
    """Excluded memory should not appear in dry-run listing; count line reflects exclusion."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont1 = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: to exclude")
    cont2 = _make_contaminated_memory(obsidian_store, "Below is a coding conversation", title="Artifact: to list")

    id1_prefix = (cont1.get("id") or "")[:8]

    out = _dispatch(["--purge-contaminated", "--exclude", id1_prefix], capsys)
    output_text = out.out + out.err

    # The excluded item must NOT appear in the listing
    assert id1_prefix not in output_text, "Excluded id must not appear in dry-run listing"
    # The non-excluded item must appear (by id prefix)
    id2_prefix = (cont2.get("id") or "")[:8]
    assert id2_prefix in output_text, "Non-excluded contaminated id must appear in dry-run"
    # The summary line must show 'excluded: 1'
    assert "excluded: 1" in output_text, "Dry-run output must show 'excluded: 1'"
    # Count of active contaminated must be 1
    assert "Total: 1" in output_text, "Dry-run total must be 1 (the non-excluded one)"


def test_purge_exclude_with_apply(tmp_vault, tmp_cortex_dir, capsys):
    """Excluded memory survives --apply; non-excluded contaminated memories are deleted."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    clean = _make_clean_memory(obsidian_store, title="Clean survivor")
    cont_keep = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: excluded keep")
    cont_del = _make_contaminated_memory(obsidian_store, "Below is a coding conversation", title="Artifact: to delete")

    id_keep_prefix = (cont_keep.get("id") or "")[:8]

    out = _dispatch(["--purge-contaminated", "--apply", "--exclude", id_keep_prefix], capsys)
    output_text = out.out + out.err

    # Output should mention excluded
    assert "excluded: 1" in output_text, "Apply output must show 'excluded: 1'"

    # Vault: clean + excluded contaminated should remain; the other contaminated should be gone
    importlib.reload(obsidian_store)
    remaining = obsidian_store._obsidian_memories(include_deprecated=True)
    remaining_titles = {m["title"] for m in remaining}
    assert "Clean survivor" in remaining_titles, "Clean memory must survive"
    assert "Artifact: excluded keep" in remaining_titles, "Excluded contaminated memory must survive"
    assert "Artifact: to delete" not in remaining_titles, "Non-excluded contaminated memory must be deleted"


def test_purge_exclude_apply_order_reversed(tmp_vault, tmp_cortex_dir, capsys):
    """--exclude before --apply (reversed order) works the same."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont_keep = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: keep reversed")
    cont_del = _make_contaminated_memory(obsidian_store, "<task-notification type='stop'>", title="Artifact: del reversed")

    id_keep_prefix = (cont_keep.get("id") or "")[:8]

    # --exclude comes before --apply
    out = _dispatch(["--purge-contaminated", "--exclude", id_keep_prefix, "--apply"], capsys)
    output_text = out.out + out.err

    assert "excluded: 1" in output_text

    importlib.reload(obsidian_store)
    remaining = obsidian_store._obsidian_memories(include_deprecated=True)
    remaining_titles = {m["title"] for m in remaining}
    assert "Artifact: keep reversed" in remaining_titles
    assert "Artifact: del reversed" not in remaining_titles


def test_purge_exclude_invalid_token_rejected(tmp_vault, tmp_cortex_dir, capsys):
    """Invalid --exclude token (not 8 lowercase hex chars) prints error and exits cleanly."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: should not be touched")
    vault_files_before = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))

    # Token too short
    out = _dispatch(["--purge-contaminated", "--apply", "--exclude", "abc123"], capsys)
    output_text = out.out + out.err
    assert "invalid" in output_text.lower() or "error" in output_text.lower() or "abc123" in output_text, (
        "Must report invalid token"
    )

    # Vault must be unchanged
    vault_files_after = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))
    assert vault_files_before == vault_files_after, "Vault must be unchanged after invalid --exclude token"


def test_purge_exclude_invalid_uppercase_rejected(tmp_vault, tmp_cortex_dir, capsys):
    """Uppercase hex is not valid (must be lowercase)."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: uppercase test")
    vault_files_before = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))

    out = _dispatch(["--purge-contaminated", "--exclude", "ABCDEF12"], capsys)
    output_text = out.out + out.err
    assert "invalid" in output_text.lower() or "ABCDEF12" in output_text, (
        "Uppercase hex token must be rejected"
    )
    vault_files_after = set(Path(tmp_vault / "memem" / "memories").glob("*.md"))
    assert vault_files_before == vault_files_after


def test_purge_exclude_multiple_ids(tmp_vault, tmp_cortex_dir, capsys):
    """Multiple comma-separated ids are all excluded."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont1 = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: multi 1")
    cont2 = _make_contaminated_memory(obsidian_store, "Below is a coding conversation", title="Artifact: multi 2")
    cont3 = _make_contaminated_memory(obsidian_store, "<task-notification type='stop'>", title="Artifact: multi 3 del")

    id1 = (cont1.get("id") or "")[:8]
    id2 = (cont2.get("id") or "")[:8]

    out = _dispatch(["--purge-contaminated", "--apply", "--exclude", f"{id1},{id2}"], capsys)
    output_text = out.out + out.err

    assert "excluded: 2" in output_text, "Must show 'excluded: 2'"

    importlib.reload(obsidian_store)
    remaining = obsidian_store._obsidian_memories(include_deprecated=True)
    remaining_titles = {m["title"] for m in remaining}
    assert "Artifact: multi 1" in remaining_titles
    assert "Artifact: multi 2" in remaining_titles
    assert "Artifact: multi 3 del" not in remaining_titles


def test_purge_exclude_spaces_tolerated(tmp_vault, tmp_cortex_dir, capsys):
    """Spaces around comma-separated ids are stripped and accepted."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont1 = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: spaces 1")
    cont2 = _make_contaminated_memory(obsidian_store, "Below is a coding conversation", title="Artifact: spaces 2 del")

    id1 = (cont1.get("id") or "")[:8]

    # Include spaces around the comma and id
    out = _dispatch(["--purge-contaminated", "--apply", "--exclude", f"  {id1}  "], capsys)
    output_text = out.out + out.err

    # Should succeed (no error), excluded count should be 1
    assert "invalid" not in output_text.lower() or "excluded: 1" in output_text, (
        "Spaces around id token must be tolerated"
    )
    assert "excluded: 1" in output_text

    importlib.reload(obsidian_store)
    remaining = obsidian_store._obsidian_memories(include_deprecated=True)
    remaining_titles = {m["title"] for m in remaining}
    assert "Artifact: spaces 1" in remaining_titles, "Excluded memory must survive"
    assert "Artifact: spaces 2 del" not in remaining_titles, "Non-excluded memory must be deleted"


def test_purge_exclude_unknown_id_silently_ignored(tmp_vault, tmp_cortex_dir, capsys):
    """An --exclude id that matches no contaminated memory is silently ignored."""
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    cont = _make_contaminated_memory(obsidian_store, "=== BEGIN CONVERSATION ===", title="Artifact: unknown excl")

    # Use a valid hex8 id that simply doesn't match any memory
    out = _dispatch(["--purge-contaminated", "--exclude", "00000000"], capsys)
    output_text = out.out + out.err

    # Must not error; the contaminated memory should still appear in dry-run
    assert "invalid" not in output_text.lower(), "Unknown id must not cause an error"
    assert "1" in output_text, "Contaminated memory count must still show 1"
    assert "--apply" in output_text
