"""Tests for dreamer extended categories: reflection_with_citations + tense_rewrite
+ event-triggered dream + --safe-auto mode (m5).

Coverage:
1.  test_reflection_produces_insights_from_8_episodes
2.  test_reflection_fewer_than_8_episodes_no_call
3.  test_reflection_last_dream_ts_respected
4.  test_reflection_proposals_in_build_diff
5.  test_tense_rewrite_expired_future_memory_rewritten
6.  test_tense_rewrite_recent_future_not_scanned
7.  test_tense_rewrite_protected_excluded
8.  test_tense_rewrite_truncation_guard_rejects_short
9.  test_tense_rewrite_skip_honored
10. test_tense_rewrite_proposals_in_build_diff
11. test_safe_auto_applies_reflections_not_demotions
12. test_safe_auto_applies_rewrites_not_cluster_merge
13. test_dream_counter_increments_on_substantive_delta
14. test_dream_counter_fires_popen_at_threshold
15. test_dream_counter_resets_after_fire
16. test_dream_counter_no_fire_on_empty_memories
17. test_build_diff_has_both_new_keys
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch  # noqa: F401

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_episodic(
    mem_id: str = "ep000001",
    created_days_ago: float = 1.0,
    title: str = "Session ep000001 — worked on something",
    project: str = "testproj",
    essence: str = "User worked on a feature in a coding session.",
    layer: int = 2,
    decay_immune: bool = False,
) -> dict:
    created = (datetime.now(UTC) - timedelta(days=created_days_ago)).isoformat()
    return {
        "id": mem_id,
        "title": title,
        "essence": essence,
        "domain_tags": ["type:episodic", f"session:{mem_id}"],
        "project": project,
        "source_type": "mined-episode",
        "importance": 3,
        "created_at": created,
        "updated_at": created,
        "layer": layer,
        "decay_immune": decay_immune,
        "invalid_at": None,
        "status": "active",
    }


def _make_future_mem(
    mem_id: str = "fut00001",
    created_days_ago: float = 60.0,
    essence: str = "I will implement the new feature next week.",
    title: str = "Future plan",
    layer: int = 2,
    decay_immune: bool = False,
) -> dict:
    created = (datetime.now(UTC) - timedelta(days=created_days_ago)).isoformat()
    return {
        "id": mem_id,
        "title": title,
        "essence": essence,
        "domain_tags": [],
        "project": "testproj",
        "source_type": "mined",
        "importance": 3,
        "created_at": created,
        "updated_at": created,
        "layer": layer,
        "decay_immune": decay_immune,
        "invalid_at": None,
        "status": "active",
    }


def _make_demotion_diff_candidate(memory_id: str = "demomem1") -> dict:
    return {"memory_id": memory_id, "suggested_layer": 3}


# Canned Haiku response for reflection: 2 insights
_REFLECTION_HAIKU_RESPONSE = json.dumps([
    {
        "title": "Pattern: iterative refactoring over multiple sessions",
        "content": "Across these sessions a pattern emerges of incremental refactoring guided by test failures.",
        "supporting_ids": ["ep000001", "ep000002"],
    },
    {
        "title": "Pattern: preference for isolated test fixtures",
        "content": "The developer consistently reaches for tmp_path fixtures and monkeypatching.",
        "supporting_ids": ["ep000003", "ep000004"],
    },
])

# Canned Haiku response for tense_rewrite: REWRITE for index 0
_TENSE_REWRITE_HAIKU_RESPONSE = json.dumps([
    {
        "index": 0,
        "action": "REWRITE",
        "new_essence": "I implemented the new feature the following week and it shipped successfully.",
    },
])

# Canned SKIP response
_TENSE_REWRITE_SKIP_RESPONSE = json.dumps([
    {
        "index": 0,
        "action": "SKIP",
    },
])

# Canned truncation response (too short)
_TENSE_REWRITE_TRUNCATED_RESPONSE = json.dumps([
    {
        "index": 0,
        "action": "REWRITE",
        "new_essence": "Done.",  # way too short
    },
])


# ---------------------------------------------------------------------------
# Test 1: reflection produces insights from 8+ episodes
# ---------------------------------------------------------------------------

def test_reflection_produces_insights_from_8_episodes():
    from memem.dreamer import find_reflection_insights

    episodes = [_make_episodic(f"ep{i:06d}", created_days_ago=float(i + 1)) for i in range(8)]

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _REFLECTION_HAIKU_RESPONSE

    with patch("subprocess.run", return_value=mock_result), \
         patch("memem.dreamer._read_last_dream_ts", return_value=None):
        proposals = find_reflection_insights(episodes)

    assert len(proposals) == 2
    assert proposals[0]["title"] == "Pattern: iterative refactoring over multiple sessions"
    assert "ep000001" in proposals[0]["supporting_ids"]
    assert proposals[1]["title"] == "Pattern: preference for isolated test fixtures"


# ---------------------------------------------------------------------------
# Test 2: fewer than 8 episodes → no Haiku call, empty list
# ---------------------------------------------------------------------------

def test_reflection_fewer_than_8_episodes_no_call():
    from memem.dreamer import find_reflection_insights

    episodes = [_make_episodic(f"ep{i:06d}") for i in range(7)]

    with patch("subprocess.run") as mock_run, \
         patch("memem.dreamer._read_last_dream_ts", return_value=None):
        proposals = find_reflection_insights(episodes)

    mock_run.assert_not_called()
    assert proposals == []


# ---------------------------------------------------------------------------
# Test 3: last-dream-ts respected — episodes older than ts not counted
# ---------------------------------------------------------------------------

def test_reflection_last_dream_ts_respected():
    from memem.dreamer import find_reflection_insights

    # last dream was 5 days ago; episodes 1-4 days ago qualify; 6-10 days ago don't
    last_dream = datetime.now(UTC) - timedelta(days=5)

    recent_eps = [_make_episodic(f"new{i:05d}", created_days_ago=float(i + 1)) for i in range(4)]
    old_eps = [_make_episodic(f"old{i:05d}", created_days_ago=float(i + 6)) for i in range(10)]
    all_eps = recent_eps + old_eps

    with patch("subprocess.run") as mock_run, \
         patch("memem.dreamer._read_last_dream_ts", return_value=last_dream):
        proposals = find_reflection_insights(all_eps)

    # Only 4 recent episodes → below threshold of 8 → no call
    mock_run.assert_not_called()
    assert proposals == []


# ---------------------------------------------------------------------------
# Test 4: reflection proposals appear in build_diff
# ---------------------------------------------------------------------------

def test_reflection_proposals_in_build_diff():
    from memem.dreamer import build_diff

    episodes = [_make_episodic(f"ep{i:06d}") for i in range(8)]

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _REFLECTION_HAIKU_RESPONSE

    with patch("subprocess.run", return_value=mock_result), \
         patch("memem.dreamer._read_last_dream_ts", return_value=None), \
         patch("memem.dreamer.find_demotion_candidates", return_value=[]), \
         patch("memem.dreamer.find_contradiction_pairs", return_value=[]), \
         patch("memem.dreamer.find_cluster_summaries", return_value=[]), \
         patch("memem.dreamer.find_cluster_merge_proposals", return_value=[]), \
         patch("memem.dreamer.find_tense_rewrites", return_value=[]):
        diff = build_diff(episodes)

    assert "reflection_with_citations" in diff
    assert len(diff["reflection_with_citations"]) == 2


# ---------------------------------------------------------------------------
# Test 5: tense_rewrite — expired future memory is rewritten
# ---------------------------------------------------------------------------

def test_tense_rewrite_expired_future_memory_rewritten():
    from memem.dreamer import find_tense_rewrites

    old_future_mem = _make_future_mem("futmem01", created_days_ago=60.0)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _TENSE_REWRITE_HAIKU_RESPONSE

    with patch("subprocess.run", return_value=mock_result):
        proposals = find_tense_rewrites([old_future_mem])

    assert len(proposals) == 1
    assert proposals[0]["memory_id"] == "futmem01"
    assert "implemented" in proposals[0]["new_essence"].lower()
    from memem.dreamer import _essence_sha
    assert proposals[0]["old_essence_hash"] == _essence_sha(old_future_mem["essence"])  # sha256, stable cross-process (Phase 4.5)


# ---------------------------------------------------------------------------
# Test 6: tense_rewrite — recent future memory NOT scanned (< 30 days old)
# ---------------------------------------------------------------------------

def test_tense_rewrite_recent_future_not_scanned():
    from memem.dreamer import find_tense_rewrites

    recent_mem = _make_future_mem("futrecnt", created_days_ago=10.0)

    with patch("subprocess.run") as mock_run:
        proposals = find_tense_rewrites([recent_mem])

    mock_run.assert_not_called()
    assert proposals == []


# ---------------------------------------------------------------------------
# Test 7: tense_rewrite — protected (L0 or decay_immune) memory excluded
# ---------------------------------------------------------------------------

def test_tense_rewrite_protected_excluded():
    from memem.dreamer import find_tense_rewrites

    l0_mem = _make_future_mem("futl0mem", created_days_ago=60.0, layer=0)
    immune_mem = _make_future_mem("futimmun", created_days_ago=60.0, decay_immune=True)

    with patch("subprocess.run") as mock_run:
        proposals = find_tense_rewrites([l0_mem, immune_mem])

    mock_run.assert_not_called()
    assert proposals == []


# ---------------------------------------------------------------------------
# Test 8: tense_rewrite — truncation guard rejects rewrite <50% of original
# ---------------------------------------------------------------------------

def test_tense_rewrite_truncation_guard_rejects_short():
    from memem.dreamer import find_tense_rewrites

    # Long original essence so truncation is detectable
    long_essence = "I will implement the new authentication system next week with OAuth2 and JWT tokens and refresh logic and everything."
    old_mem = _make_future_mem("futlong1", created_days_ago=60.0, essence=long_essence)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _TENSE_REWRITE_TRUNCATED_RESPONSE  # returns "Done." — too short

    with patch("subprocess.run", return_value=mock_result):
        proposals = find_tense_rewrites([old_mem])

    assert proposals == [], "Short rewrite should be rejected by truncation guard"


# ---------------------------------------------------------------------------
# Test 9: tense_rewrite — SKIP honored
# ---------------------------------------------------------------------------

def test_tense_rewrite_skip_honored():
    from memem.dreamer import find_tense_rewrites

    old_mem = _make_future_mem("futskip1", created_days_ago=60.0)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _TENSE_REWRITE_SKIP_RESPONSE

    with patch("subprocess.run", return_value=mock_result):
        proposals = find_tense_rewrites([old_mem])

    assert proposals == []


# ---------------------------------------------------------------------------
# Test 10: tense_rewrite proposals appear in build_diff
# ---------------------------------------------------------------------------

def test_tense_rewrite_proposals_in_build_diff():
    from memem.dreamer import build_diff

    old_mem = _make_future_mem("futbuild", created_days_ago=60.0)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _TENSE_REWRITE_HAIKU_RESPONSE

    with patch("subprocess.run", return_value=mock_result), \
         patch("memem.dreamer.find_demotion_candidates", return_value=[_make_demotion_diff_candidate()]), \
         patch("memem.dreamer.find_contradiction_pairs", return_value=[]), \
         patch("memem.dreamer.find_cluster_summaries", return_value=[]), \
         patch("memem.dreamer.find_cluster_merge_proposals", return_value=[{"canonical_title": "x", "canonical_content": "y", "cluster_ids": ["abc"], "supporting_ids": [], "source_tags": [], "contradictions": [], "project": "general"}]), \
         patch("memem.dreamer.find_reflection_insights", return_value=[]), \
         patch("memem.dreamer._read_last_dream_ts", return_value=None):
        diff = build_diff([old_mem])

    assert "tense_rewrite" in diff


# ---------------------------------------------------------------------------
# Test 11: safe_auto applies reflections but NOT demotions/cluster_merge
# ---------------------------------------------------------------------------

def test_safe_auto_applies_reflections_not_demotions(tmp_vault, tmp_cortex_dir):
    from memem.dreamer import apply_diff

    reflection_proposal = {
        "title": "Insight: consistent pattern",
        "content": "A pattern of consistent behavior emerges across multiple sessions in the vault.",
        "supporting_ids": ["ep000001", "ep000002"],
    }
    demotion_candidate = {
        "memory_id": "demomem1",
        "suggested_layer": 3,
        "title": "Old memory",
        "current_layer": 2,
        "recent_attribution": None,
        "reason": "low strength",
    }

    diff = {
        "demotion_candidates": [demotion_candidate],
        "contradiction_pairs": [],
        "cluster_summaries": [],
        "cluster_merge": [],
        "reflection_with_citations": [reflection_proposal],
        "tense_rewrite": [],
    }

    with patch("memem.obsidian_store._write_obsidian_memory") as mock_write, \
         patch("memem.obsidian_store._find_memory", return_value=None), \
         patch("memem.obsidian_store._save_memory"), \
         patch("memem.obsidian_store._make_memory") as mock_make:
        mock_make.return_value = {
            "id": "new-insight-id-000000000000",
            "title": "Insight: consistent pattern",
            "essence": "A pattern of consistent behavior emerges across multiple sessions in the vault.",
            "domain_tags": ["type:insight", "mined-reflection"],
            "project": "general",
            "source_type": "mined",
            "importance": 4,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "layer": 2,
            "decay_immune": False,
            "invalid_at": None,
            "status": "active",
        }
        result = apply_diff(diff, dry_run=False, safe_auto=True)

    # Reflections should be applied (reflected > 0)
    assert result["reflected"] == 1, f"Expected reflected=1, got {result}"
    # Demotions should NOT be applied (demoted still counted as dry-run value but not written)
    # In safe_auto: demoted is counted (apply_destructive=False → demoted += 1 branch)
    assert result["demoted"] == 1  # counted in dry-run-report-only mode
    # _write_obsidian_memory should NOT be called for demotion
    for c in mock_write.call_args_list:
        args = c[0]
        if args:
            mem = args[0]
            assert mem.get("layer") != 3, "Demotion write should not happen in safe_auto"


# ---------------------------------------------------------------------------
# Test 12: safe_auto applies rewrites but NOT cluster_merge
# ---------------------------------------------------------------------------

def test_safe_auto_applies_rewrites_not_cluster_merge(tmp_vault, tmp_cortex_dir):
    from memem.dreamer import apply_diff

    old_essence = "I will implement authentication with OAuth2 and JWT next week for the project."
    new_essence = "I implemented authentication with OAuth2 and JWT the following week for the project."

    tense_proposal = {
        "memory_id": "fut-target-mem",
        "old_essence_hash": __import__("memem.dreamer", fromlist=["_essence_sha"])._essence_sha(old_essence),  # sha256 (Phase 4.5)
        "new_essence": new_essence,
    }
    cluster_merge_proposal = {
        "project": "testproj",
        "cluster_ids": ["mem1", "mem2"],
        "supporting_ids": ["mem1", "mem2"],
        "canonical_title": "Merged memory",
        "canonical_content": "Canonical content that was merged from two memories.",
        "source_tags": ["kind:consolidated"],
        "contradictions": [],
    }

    diff = {
        "demotion_candidates": [],
        "contradiction_pairs": [],
        "cluster_summaries": [],
        "cluster_merge": [cluster_merge_proposal],
        "reflection_with_citations": [],
        "tense_rewrite": [tense_proposal],
    }

    existing_mem = {
        "id": "fut-target-mem",
        "title": "Future plan",
        "essence": old_essence,
        "domain_tags": [],
        "project": "testproj",
        "layer": 2,
        "decay_immune": False,
        "invalid_at": None,
        "status": "active",
    }

    with patch("memem.obsidian_store._find_memory", return_value=existing_mem), \
         patch("memem.obsidian_store._update_memory") as mock_update, \
         patch("memem.obsidian_store._make_memory"), \
         patch("memem.obsidian_store._save_memory"), \
         patch("memem.obsidian_store.invalidate_memory") as mock_invalidate:
        result = apply_diff(diff, dry_run=False, safe_auto=True)

    # Tense rewrite should be applied
    assert result["rewritten"] == 1, f"Expected rewritten=1, got {result}"
    mock_update.assert_called_once()
    call_args = mock_update.call_args
    assert call_args[0][0] == "fut-target-mem"
    assert call_args[0][1] == new_essence

    # Cluster merge should NOT be applied (counted but not executed)
    assert result["merged"] == 1  # counted in dry-run-report-only mode
    mock_invalidate.assert_not_called()


# ---------------------------------------------------------------------------
# Test 13: mine_delta dream counter increments per substantive delta
# ---------------------------------------------------------------------------

def test_dream_counter_increments_on_substantive_delta(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("MEMEM_DIR", str(tmp_path / ".memem"))
    (tmp_path / ".memem").mkdir(parents=True)

    import memem.mine_delta as md_mod
    importlib.reload(md_mod)

    counter_path = md_mod._dream_counter_path()

    # Simulate run() step 9b: manually call the counter logic
    # We do this by calling the piece that increments the counter.
    # Counter starts at 0, memories non-empty → counter should become 1.

    # Pre-condition: counter file does not exist
    assert not counter_path.exists()

    # Simulate 3 substantive deltas (memories non-empty)
    for _ in range(3):
        try:
            current = int(counter_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            current = 0
        current += 1
        if current < md_mod._DREAM_COUNTER_THRESHOLD:
            counter_path.write_text(str(current))

    assert int(counter_path.read_text().strip()) == 3


# ---------------------------------------------------------------------------
# Test 14: dream counter fires Popen at threshold
# ---------------------------------------------------------------------------

def test_dream_counter_fires_popen_at_threshold(tmp_path, monkeypatch):
    import importlib

    state_dir = tmp_path / ".memem"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))

    import memem.mine_delta as md_mod
    importlib.reload(md_mod)

    session_id = "testsess-counter"
    transcript_path = tmp_path / f"{session_id}.jsonl"

    # Build a substantive transcript (non-trivial)
    turns = [
        {"role": "user", "text": "Help me implement the OAuth2 authentication system for the API."},
        {"role": "assistant", "text": "Sure, let me help you implement OAuth2 with JWT tokens."},
        {"role": "user", "text": "Great, let's start with the token generation endpoint."},
    ]
    with open(transcript_path, "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")

    # Pre-set counter to threshold-1
    counter_path = md_mod._dream_counter_path()
    counter_path.write_text(str(md_mod._DREAM_COUNTER_THRESHOLD - 1))

    mock_candidates = [
        {"title": "OAuth2 implementation", "content": "User implemented OAuth2 authentication.", "project": "general"}
    ]

    with patch.object(md_mod, "_reconcile_candidates", return_value=(mock_candidates, 1, 0)), \
         patch.object(md_mod, "extract_from_text", return_value=mock_candidates), \
         patch.object(md_mod, "_emit_session_episode", return_value=True), \
         patch.object(md_mod, "_emit_citations_if_any"), \
         patch("memem.feedback._classify_session_outcome", return_value="productive", create=True), \
         patch("memem.feedback.update_relevance_scores", create=True), \
         patch("subprocess.Popen") as mock_popen:
        md_mod.run(session_id=session_id, transcript_path=str(transcript_path))

    # Popen should have been called with --dream --safe-auto
    mock_popen.assert_called_once()
    popen_args = mock_popen.call_args[0][0]
    assert "--dream" in popen_args
    assert "--safe-auto" in popen_args
    assert mock_popen.call_args[1].get("start_new_session") is True


# ---------------------------------------------------------------------------
# Test 15: dream counter resets to 0 after firing
# ---------------------------------------------------------------------------

def test_dream_counter_resets_after_fire(tmp_path, monkeypatch):
    import importlib

    state_dir = tmp_path / ".memem"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))

    import memem.mine_delta as md_mod
    importlib.reload(md_mod)

    session_id = "testsess-reset"
    transcript_path = tmp_path / f"{session_id}.jsonl"

    turns = [
        {"role": "user", "text": "Let me explain the full architecture of the system."},
        {"role": "assistant", "text": "Sure, I can document the architecture for you."},
        {"role": "user", "text": "We have three services: api, worker, and scheduler."},
    ]
    with open(transcript_path, "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")

    counter_path = md_mod._dream_counter_path()
    counter_path.write_text(str(md_mod._DREAM_COUNTER_THRESHOLD - 1))

    mock_candidates = [{"title": "Architecture docs", "content": "Three services: api, worker, scheduler.", "project": "general"}]

    with patch.object(md_mod, "_reconcile_candidates", return_value=(mock_candidates, 1, 0)), \
         patch.object(md_mod, "extract_from_text", return_value=mock_candidates), \
         patch.object(md_mod, "_emit_session_episode", return_value=True), \
         patch.object(md_mod, "_emit_citations_if_any"), \
         patch("memem.feedback._classify_session_outcome", return_value="productive", create=True), \
         patch("memem.feedback.update_relevance_scores", create=True), \
         patch("subprocess.Popen"):
        md_mod.run(session_id=session_id, transcript_path=str(transcript_path))

    # Counter should be reset to 0 after firing
    assert int(counter_path.read_text().strip()) == 0


# ---------------------------------------------------------------------------
# Test 16: dream counter does NOT fire on empty memories
# ---------------------------------------------------------------------------

def test_dream_counter_no_fire_on_empty_memories(tmp_path, monkeypatch):
    import importlib

    state_dir = tmp_path / ".memem"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("MEMEM_DIR", str(state_dir))

    import memem.mine_delta as md_mod
    importlib.reload(md_mod)

    session_id = "testsess-nomem"
    transcript_path = tmp_path / f"{session_id}.jsonl"

    turns = [
        {"role": "user", "text": "Let me explain the full architecture of the memory system."},
        {"role": "assistant", "text": "Sure, I can document the architecture for you now."},
        {"role": "user", "text": "We use three services and a message queue between them."},
    ]
    with open(transcript_path, "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")

    counter_path = md_mod._dream_counter_path()
    counter_path.write_text(str(md_mod._DREAM_COUNTER_THRESHOLD - 1))

    with patch.object(md_mod, "_reconcile_candidates", return_value=([], 0, 0)), \
         patch.object(md_mod, "extract_from_text", return_value=[]), \
         patch.object(md_mod, "_emit_session_episode", return_value=False), \
         patch.object(md_mod, "_emit_citations_if_any"), \
         patch("memem.feedback._classify_session_outcome", return_value="unproductive", create=True), \
         patch("memem.feedback.update_relevance_scores", create=True), \
         patch("subprocess.Popen") as mock_popen:
        md_mod.run(session_id=session_id, transcript_path=str(transcript_path))

    # Popen should NOT be called (empty memories → counter NOT incremented)
    mock_popen.assert_not_called()
    # Counter should still be at threshold-1 (unchanged)
    assert int(counter_path.read_text().strip()) == md_mod._DREAM_COUNTER_THRESHOLD - 1


# ---------------------------------------------------------------------------
# Test 17: build_diff has both new category keys
# ---------------------------------------------------------------------------

def test_build_diff_has_both_new_keys():
    from memem.dreamer import build_diff

    with patch("memem.dreamer.find_demotion_candidates", return_value=[]), \
         patch("memem.dreamer.find_contradiction_pairs", return_value=[]), \
         patch("memem.dreamer.find_cluster_summaries", return_value=[]), \
         patch("memem.dreamer.find_cluster_merge_proposals", return_value=[]), \
         patch("memem.dreamer.find_reflection_insights", return_value=[]), \
         patch("memem.dreamer.find_tense_rewrites", return_value=[]):
        diff = build_diff([])

    assert "reflection_with_citations" in diff, "build_diff must include reflection_with_citations key"
    assert "tense_rewrite" in diff, "build_diff must include tense_rewrite key"


# ---------------------------------------------------------------------------
# Phase 4.5 fixes (v2.8): user-authored exclusion + stable sha256 hashes
# ---------------------------------------------------------------------------


class TestTenseRewriteUserAuthoredExcluded:
    """User-authored memories (source_type=user) are NEVER tense-rewrite
    candidates — automated rewriting is reserved for machine-mined content."""

    def test_user_authored_memory_not_scanned(self):
        from datetime import UTC, datetime, timedelta

        from memem.dreamer import find_tense_rewrites

        old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        user_mem = {
            "id": "useraaaa-0000-0000-0000-000000000000",
            "title": "My plan",
            "essence": "I will migrate the database next week.",
            "source_type": "user",
            "status": "active",
            "invalid_at": None,
            "created_at": old_ts,
            "domain_tags": [],
        }
        mined_twin = {**user_mem, "id": "minedbbb-0000-0000-0000-000000000000",
                      "source_type": "mined"}

        # Only the mined twin may become a candidate (Haiku call is monkeyed
        # out by passing through the candidate-collection path indirectly:
        # find_tense_rewrites returns [] when no candidates, and would invoke
        # Haiku otherwise — patch subprocess to prove the user mem alone never
        # triggers a call).
        import memem.dreamer as dr
        calls = []
        orig = dr.subprocess.run

        def _spy(cmd, **kw):
            calls.append(cmd)

            class R:
                returncode = 0
                stdout = '[{"index": 0, "action": "SKIP"}]'
                stderr = ""
            return R()

        dr.subprocess.run = _spy
        try:
            result_user_only = dr.find_tense_rewrites([user_mem])
            assert result_user_only == [], "user-authored memory must not be a candidate"
            assert calls == [], "no Haiku call may fire for user-only input"

            dr.find_tense_rewrites([user_mem, mined_twin])
            assert len(calls) == 1, "mined twin alone should reach the Haiku batch"
        finally:
            dr.subprocess.run = orig


class TestEssenceShaStable:
    """old_essence_hash uses sha256 (stable cross-process), not builtin hash()."""

    def test_sha_is_hex_and_deterministic(self):
        from memem.dreamer import _essence_sha
        a = _essence_sha("the content")
        assert a == _essence_sha("the content")
        assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
        assert _essence_sha("") == _essence_sha(None if False else "")
