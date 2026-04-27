"""Tests for delta commit execution and audit logging."""

from __future__ import annotations

import importlib
import json
from pathlib import Path


def _load_modules():
    from memem import delta, delta_commit, delta_policy, models, obsidian_store, search_index

    importlib.reload(models)
    importlib.reload(search_index)
    importlib.reload(obsidian_store)
    importlib.reload(delta)
    importlib.reload(delta_policy)
    importlib.reload(delta_commit)
    return delta, delta_commit, delta_policy, models, obsidian_store


def test_execute_delta_writeback_dry_run_is_noop_and_audited(tmp_vault, tmp_cortex_dir):
    delta, delta_commit, _delta_policy, models, obsidian_store = _load_modules()

    first = obsidian_store._make_memory(
        content="Keep the writeback audit trail append-only under the memem state dir.",
        title="Writeback audit trail",
        project="memem",
    )
    second = obsidian_store._make_memory(
        content="Dry-run previews should show what would change before any vault mutation happens.",
        title="Dry-run preview",
        project="memem",
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    deltas = delta.propose_deltas_from_slice({
        "slice_id": "slice_dry_run",
        "scope_id": "memem",
        "constraints": [{"memory_id": first["id"]}],
        "decisions": [{"memory_id": second["id"]}],
    })
    related = [item for item in deltas if item["delta_type"] == "add_related_link"]
    assert len(related) == 1

    before_first = Path(obsidian_store._find_memory(first["id"])["file"]).read_text()
    before_second = Path(obsidian_store._find_memory(second["id"])["file"]).read_text()

    batch = delta_commit.execute_delta_writeback(related)

    assert batch["dry_run"] is True
    assert batch["results"][0]["status"] == "dry_run"
    assert batch["writeback_summary"]["status"] == "dry_run"
    assert batch["writeback_summary"]["proposed_count"] == 1
    assert batch["writeback_summary"]["auto_committed_count"] == 0

    refreshed_first = obsidian_store._find_memory(first["id"])
    refreshed_second = obsidian_store._find_memory(second["id"])
    assert refreshed_first is not None
    assert refreshed_second is not None
    assert refreshed_first.get("related", []) == []
    assert refreshed_second.get("related", []) == []
    assert Path(refreshed_first["file"]).read_text() == before_first
    assert Path(refreshed_second["file"]).read_text() == before_second

    audit_lines = models.DELTA_AUDIT_LOG.read_text().splitlines()
    assert len(audit_lines) == 2
    start_entry = json.loads(audit_lines[0])
    audit_entry = json.loads(audit_lines[1])
    assert start_entry["event"] == "execution_started"
    assert audit_entry["dry_run"] is True
    assert audit_entry["status"] == "dry_run"
    assert audit_entry["preview"]["action"] == "add_related_link"
    assert audit_entry["execution_id"] == batch["execution_id"]

    state_payload = json.loads(Path(batch["state_file"]).read_text())
    assert state_payload["dry_run"] is True
    assert state_payload["writeback_summary"]["status"] == "dry_run"
    assert state_payload["results"][0]["status"] == "dry_run"


def test_execute_delta_writeback_commits_auto_safe_related_link(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, delta_policy, models, obsidian_store = _load_modules()

    first = obsidian_store._make_memory(
        content="Store mutations must keep markdown notes as the source of truth.",
        title="Markdown source of truth",
        project="memem",
    )
    second = obsidian_store._make_memory(
        content="Related-link commits should stay symmetric across both memory notes.",
        title="Symmetric related links",
        project="memem",
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    proposal = {
        "delta_id": "delta_auto_link",
        "delta_type": "add_related_link",
        "target_memory_ids": [first["id"], second["id"]],
        "confidence": 0.93,
        "source_slice_id": "slice_commit_safe",
        "scope_id": "memem",
        "requires_user_confirmation": False,
    }
    decision = delta_policy.evaluate_delta_proposal(proposal)
    batch = delta_commit.execute_delta_writeback(
        [proposal],
        dry_run=False,
        policy_decisions=[decision],
    )

    assert batch["results"][0]["status"] == "committed"
    assert batch["results"][0]["commit_policy"] == "auto_safe"
    assert batch["writeback_summary"]["status"] == "committed"
    assert batch["writeback_summary"]["auto_committed_count"] == 1

    refreshed_first = obsidian_store._find_memory(first["id"])
    refreshed_second = obsidian_store._find_memory(second["id"])
    assert refreshed_first is not None
    assert refreshed_second is not None
    assert second["id"][:8] in refreshed_first.get("related", [])
    assert first["id"][:8] in refreshed_second.get("related", [])
    assert "## Related" in Path(refreshed_first["file"]).read_text()
    assert "## Related" in Path(refreshed_second["file"]).read_text()

    audit_entry = json.loads(models.DELTA_AUDIT_LOG.read_text().splitlines()[0])
    assert audit_entry["event"] == "execution_started"
    committed_entry = json.loads(models.DELTA_AUDIT_LOG.read_text().splitlines()[1])
    assert committed_entry["status"] == "committed"
    assert committed_entry["commit_policy"] == "auto_safe"


def test_execute_delta_writeback_requires_approval_for_manual_review_commit(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, delta_policy, models, obsidian_store = _load_modules()

    existing = obsidian_store._make_memory(
        content="The migration should keep the old CLI alias available until rollout is complete.",
        title="CLI alias guardrail",
        project="memem",
    )
    obsidian_store._save_memory(existing)

    proposal = {
        "delta_id": "delta_manual_memory",
        "delta_type": "save_new_memory",
        "target_memory_ids": [existing["id"]],
        "proposed_title": "Preserve the CLI alias during migration",
        "proposed_content": "Keep the old CLI alias available until every existing automation entrypoint has been migrated to the new state directory.",
        "proposed_tags": ["migration"],
        "confidence": 0.86,
        "source_slice_id": "slice_manual",
        "scope_id": "memem",
        "requires_user_confirmation": False,
    }
    decision = delta_policy.evaluate_delta_proposal(proposal)
    assert decision["decision"] == "review"

    blocked = delta_commit.execute_delta_writeback(
        [proposal],
        dry_run=False,
        policy_decisions=[decision],
    )
    assert blocked["results"][0]["status"] == "blocked"
    assert blocked["writeback_summary"]["status"] == "blocked"
    assert len(obsidian_store._obsidian_memories()) == 1

    committed = delta_commit.execute_delta_writeback(
        [proposal],
        dry_run=False,
        approved_delta_ids=["delta_manual_memory"],
        policy_decisions=[decision],
    )
    assert committed["results"][0]["status"] == "committed"
    assert committed["results"][0]["commit_policy"] == "manual_review"
    assert committed["writeback_summary"]["status"] == "committed"
    assert committed["writeback_summary"]["manual_review_count"] == 0

    memories = obsidian_store._obsidian_memories()
    assert len(memories) == 2
    created = next(memory for memory in memories if memory["title"] == proposal["proposed_title"])
    refreshed_existing = obsidian_store._find_memory(existing["id"])
    assert refreshed_existing is not None
    assert existing["id"][:8] in created.get("related", [])
    assert created["id"][:8] in refreshed_existing.get("related", [])

    audit_lines = models.DELTA_AUDIT_LOG.read_text().splitlines()
    assert len(audit_lines) == 4
    blocked_entry = json.loads(audit_lines[0])
    blocked_result_entry = json.loads(audit_lines[1])
    committed_entry = json.loads(audit_lines[2])
    followup_entry = json.loads(audit_lines[3])
    assert blocked_entry["event"] == "execution_started"
    assert blocked_result_entry["status"] == "blocked"
    assert committed_entry["event"] == "execution_started"
    assert followup_entry["status"] == "committed"
    assert followup_entry["commit_policy"] == "manual_review"


def test_execute_delta_writeback_rejects_cross_scope_targets(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, delta_policy, _models, obsidian_store = _load_modules()

    in_scope = obsidian_store._make_memory(
        content="Project-scoped writeback should stay inside the memem project.",
        title="Memem scoped writeback",
        project="memem",
    )
    cross_scope = obsidian_store._make_memory(
        content="This memory belongs to another project and should not be linked from memem scope.",
        title="Other project note",
        project="other-project",
    )
    obsidian_store._save_memory(in_scope)
    obsidian_store._save_memory(cross_scope)

    proposal = {
        "delta_id": "delta_cross_scope_commit",
        "delta_type": "add_related_link",
        "target_memory_ids": [in_scope["id"], cross_scope["id"]],
        "confidence": 0.95,
        "source_slice_id": "slice_cross_scope_commit",
        "scope_id": "memem",
    }
    decision = delta_policy.evaluate_delta_proposal(proposal)
    assert decision["decision"] == "reject"

    batch = delta_commit.execute_delta_writeback(
        [proposal],
        dry_run=False,
        policy_decisions=[decision],
    )

    assert batch["results"][0]["status"] == "rejected"
    assert batch["writeback_summary"]["status"] == "blocked"
    assert any("outside writeback scope memem" in error for error in batch["results"][0]["validation_errors"])

    refreshed = obsidian_store._find_memory(in_scope["id"])
    assert refreshed is not None
    assert refreshed.get("related", []) == []


def test_execute_delta_writeback_repeated_related_link_commit_keeps_single_related_block(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, delta_policy, _models, obsidian_store = _load_modules()

    first = obsidian_store._make_memory(
        content="The serializer should not duplicate generated related sections on repeated writes.",
        title="Serializer idempotence",
        project="memem",
    )
    second = obsidian_store._make_memory(
        content="Repeated related-link commits should stay a true no-op after the first successful write.",
        title="Related link no-op",
        project="memem",
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    proposal = {
        "delta_id": "delta_single_related_block",
        "delta_type": "add_related_link",
        "target_memory_ids": [first["id"], second["id"]],
        "confidence": 0.94,
        "source_slice_id": "slice_single_related_block",
        "scope_id": "memem",
    }
    decision = delta_policy.evaluate_delta_proposal(proposal)
    first_batch = delta_commit.execute_delta_writeback([proposal], dry_run=False, policy_decisions=[decision])
    second_batch = delta_commit.execute_delta_writeback([proposal], dry_run=False, policy_decisions=[decision])

    assert first_batch["results"][0]["status"] == "committed"
    assert second_batch["results"][0]["status"] == "skipped"

    first_text = Path(obsidian_store._find_memory(first["id"])["file"]).read_text()
    second_text = Path(obsidian_store._find_memory(second["id"])["file"]).read_text()
    assert first_text.count("## Related") == 1
    assert second_text.count("## Related") == 1
    assert first_text.count(f"[[{Path(obsidian_store._find_memory(second['id'])['file']).stem}]]") == 1
    assert second_text.count(f"[[{Path(obsidian_store._find_memory(first['id'])['file']).stem}]]") == 1


def test_execute_delta_writeback_stale_invalid_target_is_not_reported_as_skip(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, delta_policy, _models, obsidian_store = _load_modules()

    first = obsidian_store._make_memory(
        content="The initial policy decision will be made against a still-valid target.",
        title="Initial valid target",
        project="memem",
    )
    second = obsidian_store._make_memory(
        content="This target will become deprecated before commit execution.",
        title="Target becomes deprecated",
        project="memem",
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    proposal = {
        "delta_id": "delta_stale_target",
        "delta_type": "add_related_link",
        "target_memory_ids": [first["id"], second["id"]],
        "confidence": 0.93,
        "source_slice_id": "slice_stale_target",
        "scope_id": "memem",
    }
    decision = delta_policy.evaluate_delta_proposal(proposal)
    assert decision["decision"] == "commit_safe"

    obsidian_store._deprecate_memory(second["id"], reason="no longer active")

    batch = delta_commit.execute_delta_writeback(
        [proposal],
        dry_run=False,
        policy_decisions=[decision],
    )

    assert batch["results"][0]["status"] in {"rejected", "blocked"}
    assert batch["writeback_summary"]["status"] == "blocked"
    assert "already existed" not in batch["results"][0]["result_message"]


def test_execute_delta_writeback_surfaces_partial_persistence_failure(tmp_vault, tmp_cortex_dir, monkeypatch):
    _delta, delta_commit, delta_policy, _models, obsidian_store = _load_modules()

    first = obsidian_store._make_memory(
        content="Start-state persistence should exist before any vault mutation happens.",
        title="Start-state persistence",
        project="memem",
    )
    second = obsidian_store._make_memory(
        content="If later persistence fails, the batch should surface a partial failure rather than pretending all metadata landed cleanly.",
        title="Partial persistence warning",
        project="memem",
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    proposal = {
        "delta_id": "delta_partial_persistence",
        "delta_type": "add_related_link",
        "target_memory_ids": [first["id"], second["id"]],
        "confidence": 0.96,
        "source_slice_id": "slice_partial_persistence",
        "scope_id": "memem",
    }
    decision = delta_policy.evaluate_delta_proposal(proposal)

    real_persist = delta_commit._persist_execution_state
    calls = {"count": 0}

    def flaky_persist(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("simulated state persistence failure")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(delta_commit, "_persist_execution_state", flaky_persist)

    batch = delta_commit.execute_delta_writeback(
        [proposal],
        dry_run=False,
        policy_decisions=[decision],
    )

    assert batch["results"][0]["status"] == "committed"
    assert batch["writeback_summary"]["status"] == "partial"
    assert batch["warnings"]
    assert "Writeback persistence failure" in batch["warnings"][0]
    assert Path(batch["state_file"]).exists()

    refreshed = obsidian_store._find_memory(first["id"])
    assert refreshed is not None
    assert second["id"][:8] in refreshed.get("related", [])


def test_commit_delta_public_wrapper_returns_single_result(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, _delta_policy, _models, obsidian_store = _load_modules()

    first = obsidian_store._make_memory(
        content="Public wrappers should expose the single-delta commit API expected by downstream callers.",
        title="Single delta API",
        project="memem",
    )
    second = obsidian_store._make_memory(
        content="The wrapper should preserve the underlying writeback result surface and preview metadata.",
        title="Wrapper result metadata",
        project="memem",
    )
    obsidian_store._save_memory(first)
    obsidian_store._save_memory(second)

    result = delta_commit.commit_delta({
        "delta_id": "delta_public_single",
        "delta_type": "add_related_link",
        "target_memory_ids": [first["id"], second["id"]],
        "confidence": 0.94,
        "source_slice_id": "slice_public_single",
    }, scope_id="memem", dry_run=False)

    assert result["status"] == "committed"
    assert result["commit_policy"] == "auto_safe"
    assert result["execution_id"]
    assert result["preview"]["action"] == "add_related_link"
    assert result["writeback_summary"]["auto_committed_count"] == 1


def test_commit_deltas_public_wrapper_respects_auto_only(tmp_vault, tmp_cortex_dir):
    _delta, delta_commit, _delta_policy, _models, obsidian_store = _load_modules()

    existing = obsidian_store._make_memory(
        content="Auto-only should still allow auto-safe link deltas while skipping manual-review note creation.",
        title="Auto-only base memory",
        project="memem",
    )
    companion = obsidian_store._make_memory(
        content="This companion memory provides the auto-safe related-link target.",
        title="Auto-only companion",
        project="memem",
    )
    obsidian_store._save_memory(existing)
    obsidian_store._save_memory(companion)

    results = delta_commit.commit_deltas([
        {
            "delta_id": "delta_public_auto_safe",
            "delta_type": "add_related_link",
            "target_memory_ids": [existing["id"], companion["id"]],
            "confidence": 0.95,
            "source_slice_id": "slice_public_auto_only",
        },
        {
            "delta_id": "delta_public_manual_review",
            "delta_type": "save_new_memory",
            "target_memory_ids": [existing["id"]],
            "proposed_title": "Skipped by auto_only wrapper",
            "proposed_content": "This durable note proposal requires manual review and should be skipped by the auto_only wrapper path.",
            "confidence": 0.87,
            "source_slice_id": "slice_public_auto_only",
        },
    ], scope_id="memem", dry_run=False, auto_only=True)

    assert len(results) == 2
    assert results[0]["delta_id"] == "delta_public_auto_safe"
    assert results[0]["status"] == "committed"
    assert results[1]["delta_id"] == "delta_public_manual_review"
    assert results[1]["status"] == "skipped"
    assert "auto_only=True" in results[1]["result_message"]

    memories = obsidian_store._obsidian_memories()
    titles = {memory["title"] for memory in memories}
    assert "Skipped by auto_only wrapper" not in titles


def test_commit_deltas_auto_only_preserves_position_with_duplicate_delta_ids(tmp_vault, tmp_cortex_dir):
    """Two deltas with the same delta_id (or empty) must not silently
    overwrite each other in the auto_only path. Regression guard for H1."""
    _delta, delta_commit, _delta_policy, _models, obsidian_store = _load_modules()

    a = obsidian_store._make_memory(
        content="First memory in the duplicate-id auto_only regression test scenario.",
        title="Dup-id A",
        project="memem",
    )
    b = obsidian_store._make_memory(
        content="Second memory in the duplicate-id auto_only regression test scenario.",
        title="Dup-id B",
        project="memem",
    )
    c = obsidian_store._make_memory(
        content="Third memory in the duplicate-id auto_only regression test scenario.",
        title="Dup-id C",
        project="memem",
    )
    obsidian_store._save_memory(a)
    obsidian_store._save_memory(b)
    obsidian_store._save_memory(c)

    # Both deltas share the same delta_id. Pre-fix this would clobber the
    # first result with the second in the deferred dict; post-fix the
    # returned list preserves both by position.
    results = delta_commit.commit_deltas([
        {
            "delta_id": "shared_id",
            "delta_type": "save_new_memory",
            "target_memory_ids": [a["id"]],
            "proposed_title": "Manual-review note one",
            "proposed_content": "First manual-review note proposal that requires gating before it lands.",
            "confidence": 0.55,
            "source_slice_id": "slice_dup_id",
        },
        {
            "delta_id": "shared_id",
            "delta_type": "save_new_memory",
            "target_memory_ids": [b["id"]],
            "proposed_title": "Manual-review note two",
            "proposed_content": "Second manual-review note proposal that also requires gating before it lands.",
            "confidence": 0.55,
            "source_slice_id": "slice_dup_id",
        },
    ], scope_id="memem", dry_run=False, auto_only=True)

    assert len(results) == 2
    # Both proposals were skipped (manual_review under auto_only) but the
    # order matches the input. With the pre-fix dict-keyed bookkeeping
    # both entries would point at the same dict value.
    assert results[0]["status"] == "skipped"
    assert results[1]["status"] == "skipped"
    titles_in_results = [r.get("preview", {}).get("title", "") for r in results]
    # The previews carry per-position content, proving both deltas survived.
    assert titles_in_results[0] != titles_in_results[1]


def test_writeback_status_returns_dry_run_when_persistence_fails(tmp_vault, tmp_cortex_dir):
    """A dry-run batch whose persistence fails must report status=dry_run,
    not partial. Regression guard for H3."""
    _delta, delta_commit, _delta_policy, _models, _obsidian_store = _load_modules()
    summary = delta_commit._summarize_results(
        [{"status": "dry_run"}, {"status": "dry_run"}],
        dry_run=True,
        persistence_failed=True,
    )
    assert summary["status"] == "dry_run"
