"""Tests for D3: procedural memory type.

Covers:
1. _mine_one_chunk validation: kind passes through only for 'procedural'
2. mine_delta tags candidates type:procedural in reconcile-ADD path
3. mine_delta tags candidates type:procedural in fallback path
4. Imperative content lands intact
"""

from __future__ import annotations

import json
from unittest import mock

# ---------------------------------------------------------------------------
# Test 1: _mine_one_chunk passes through kind=='procedural' only
# ---------------------------------------------------------------------------

def test_mine_one_chunk_passes_procedural_kind(monkeypatch):
    """kind=='procedural' must be preserved; all other kinds must be dropped."""
    import memem.mining as mining_mod

    haiku_response = json.dumps([
        {
            "title": "Always X rule",
            "project": "general",
            "content": "Always confirm before overwriting files.",
            "importance": 4,
            "kind": "procedural",
        },
        {
            "title": "Some factual memory",
            "project": "general",
            "content": "The project uses Python 3.12.",
            "importance": 3,
            "kind": "episodic",  # not procedural — must be dropped
        },
        {
            "title": "Another fact",
            "project": "general",
            "content": "Tests live in tests/ directory.",
            "importance": 2,
            # no kind field at all
        },
        {
            "title": "Bad kind value",
            "project": "general",
            "content": "Never skip linting.",
            "importance": 3,
            "kind": "PROCEDURAL",  # wrong case — must be dropped
        },
    ])

    fake_result = mock.Mock(returncode=0, stdout=haiku_response, stderr="")
    monkeypatch.setattr("memem.mining.subprocess.run", lambda *a, **kw: fake_result)

    results = mining_mod._mine_one_chunk(["User: hello\nAssistant: world"])

    assert len(results) == 4, f"Expected 4 results, got {len(results)}"

    # First: kind==procedural should pass through
    assert results[0].get("kind") == "procedural", (
        f"Expected kind='procedural', got {results[0].get('kind')!r}"
    )
    assert results[0]["content"] == "Always confirm before overwriting files."

    # Second: kind=='episodic' should be dropped (not procedural)
    assert "kind" not in results[1], (
        f"Non-procedural kind must be dropped, got {results[1].get('kind')!r}"
    )

    # Third: no kind field — should have no kind
    assert "kind" not in results[2], (
        f"Missing kind must not appear in result, got {results[2].get('kind')!r}"
    )

    # Fourth: wrong-case 'PROCEDURAL' must be dropped
    assert "kind" not in results[3], (
        f"'PROCEDURAL' (wrong case) must be dropped, got {results[3].get('kind')!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: mine_delta reconcile-ADD path tags type:procedural
# ---------------------------------------------------------------------------

def test_reconcile_add_prepends_type_procedural(tmp_vault, tmp_cortex_dir, monkeypatch):
    """When reconcile op is ADD and candidate has kind=='procedural',
    domain_tags must include 'type:procedural' in the saved memory."""
    import memem.mine_delta as md

    # Candidate with kind='procedural'
    candidate = {
        "title": "Always use absolute paths in Bash",
        "content": "Always use absolute paths in Bash tool calls to avoid cwd ambiguity.",
        "project": "general",
        "importance": 4,
        "kind": "procedural",
        "keys": [],
    }

    # Mock reconcile Haiku call to return ADD for the single candidate
    haiku_response = json.dumps([
        {"index": 0, "op": "ADD", "target": None, "content": None,
         "profile": None, "section": None, "line": None, "reason": "new rule"}
    ])
    haiku_result = mock.Mock(returncode=0, stdout=haiku_response, stderr="")

    saved_mems: list[dict] = []

    def fake_save_memory(mem: dict) -> None:
        saved_mems.append(mem)

    # Pre-filter score must be < 0.95 so it survives to Haiku
    monkeypatch.setattr("memem.mine_delta._find_best_match", lambda *a, **kw: (None, 0.0))
    monkeypatch.setattr("memem.mine_delta._ngram_search_candidates", lambda *a, **kw: [])
    monkeypatch.setattr("memem.mine_delta._find_memory", lambda *a, **kw: None)
    monkeypatch.setattr("memem.mine_delta._find_memory_unambiguous", lambda *a, **kw: None)
    monkeypatch.setattr("memem.mine_delta._save_memory", fake_save_memory)
    monkeypatch.setattr("memem.mine_delta.subprocess.run", lambda *a, **kw: haiku_result)
    monkeypatch.setattr("memem.mine_delta._stable_mined_memory_id",
                        lambda *a, **kw: "aabbccdd-0000-0000-0000-000000000001")
    monkeypatch.setattr("memem.mine_delta._log_event", lambda *a, **kw: None)

    saved_list, written, _ = md._reconcile_candidates([candidate], session_id="testsess01")

    assert written == 1, f"Expected 1 memory written, got {written}"
    assert len(saved_mems) == 1, f"Expected 1 saved mem, got {len(saved_mems)}"
    saved_tags = saved_mems[0].get("domain_tags") or []
    assert "type:procedural" in saved_tags, (
        f"Expected 'type:procedural' in domain_tags, got {saved_tags}"
    )
    assert saved_tags[0] == "type:procedural", (
        f"Expected 'type:procedural' to be first tag, got {saved_tags}"
    )


# ---------------------------------------------------------------------------
# Test 3: mine_delta fallback path tags type:procedural
# ---------------------------------------------------------------------------

def test_fallback_add_prepends_type_procedural(tmp_vault, tmp_cortex_dir, monkeypatch):
    """When fallback ADD-all runs and candidate has kind=='procedural',
    domain_tags in the saved memory must include 'type:procedural'."""
    import memem.mine_delta as md

    candidate = {
        "title": "Never run git push --force on main",
        "content": "Never run git push --force on main branch without explicit user request.",
        "project": "general",
        "importance": 5,
        "kind": "procedural",
        "keys": [],
    }

    saved_mems: list[dict] = []

    def fake_save_memory(mem: dict) -> None:
        saved_mems.append(mem)

    monkeypatch.setattr("memem.mine_delta._save_memory", fake_save_memory)
    monkeypatch.setattr("memem.mine_delta._log_event", lambda *a, **kw: None)

    written = md._fallback_add_all([candidate], session_id="testsess02")

    assert written == 1, f"Expected 1 memory written, got {written}"
    assert len(saved_mems) == 1
    saved_tags = saved_mems[0].get("domain_tags") or []
    assert "type:procedural" in saved_tags, (
        f"Expected 'type:procedural' in domain_tags, got {saved_tags}"
    )


# ---------------------------------------------------------------------------
# Test 4: type:procedural NOT added when kind is absent or non-procedural
# ---------------------------------------------------------------------------

def test_fallback_does_not_add_procedural_tag_for_normal_memory(
    tmp_vault, tmp_cortex_dir, monkeypatch
):
    """A candidate without kind=='procedural' must NOT get a type:procedural tag."""
    import memem.mine_delta as md

    candidate_normal = {
        "title": "Project uses SQLite",
        "content": "The project uses SQLite for the search index.",
        "project": "general",
        "importance": 3,
        # No kind field
        "keys": [],
    }
    candidate_wrong_kind = {
        "title": "Some episodic note",
        "content": "The user worked on auth today.",
        "project": "general",
        "importance": 2,
        "kind": "episodic",  # not procedural
        "keys": [],
    }

    saved_mems: list[dict] = []

    def fake_save_memory(mem: dict) -> None:
        saved_mems.append(mem)

    monkeypatch.setattr("memem.mine_delta._save_memory", fake_save_memory)
    monkeypatch.setattr("memem.mine_delta._log_event", lambda *a, **kw: None)

    written = md._fallback_add_all(
        [candidate_normal, candidate_wrong_kind], session_id="testsess03"
    )

    assert written == 2
    for mem in saved_mems:
        tags = mem.get("domain_tags") or []
        assert "type:procedural" not in tags, (
            f"type:procedural must not appear in non-procedural memory, got {tags}"
        )


# ---------------------------------------------------------------------------
# Test 5: Imperative content lands intact through validation
# ---------------------------------------------------------------------------

def test_imperative_content_passes_through_validation(monkeypatch):
    """Imperative procedural content survives _mine_one_chunk validation unchanged."""
    import memem.mining as mining_mod

    imperative_rule = "Always quote file paths that contain spaces with double quotes."
    haiku_response = json.dumps([
        {
            "title": "Quote paths with spaces",
            "project": "general",
            "content": imperative_rule,
            "importance": 4,
            "kind": "procedural",
        }
    ])

    fake_result = mock.Mock(returncode=0, stdout=haiku_response, stderr="")
    monkeypatch.setattr("memem.mining.subprocess.run", lambda *a, **kw: fake_result)

    results = mining_mod._mine_one_chunk(["User: what should I do?\nAssistant: use quotes"])

    assert len(results) == 1
    assert results[0]["kind"] == "procedural"
    assert results[0]["content"] == imperative_rule


# ---------------------------------------------------------------------------
# Test 6: Existing tag list not mutated by reconcile ADD path
# ---------------------------------------------------------------------------

def test_reconcile_add_does_not_mutate_original_tags(tmp_vault, tmp_cortex_dir, monkeypatch):
    """type:procedural prepend must NOT mutate the original candidate tags list."""
    import memem.mine_delta as md

    original_tags = ["type:skill"]
    candidate = {
        "title": "When debugging, add print first",
        "content": "When debugging, add print statements before adding logs.",
        "project": "general",
        "importance": 3,
        "kind": "procedural",
        "tags": original_tags,
        "keys": [],
    }

    haiku_response = json.dumps([
        {"index": 0, "op": "ADD", "target": None, "content": None,
         "profile": None, "section": None, "line": None, "reason": "new"}
    ])
    haiku_result = mock.Mock(returncode=0, stdout=haiku_response, stderr="")

    saved_mems: list[dict] = []
    monkeypatch.setattr("memem.mine_delta._find_best_match", lambda *a, **kw: (None, 0.0))
    monkeypatch.setattr("memem.mine_delta._ngram_search_candidates", lambda *a, **kw: [])
    monkeypatch.setattr("memem.mine_delta._find_memory", lambda *a, **kw: None)
    monkeypatch.setattr("memem.mine_delta._find_memory_unambiguous", lambda *a, **kw: None)
    monkeypatch.setattr("memem.mine_delta._save_memory", lambda m: saved_mems.append(m))
    monkeypatch.setattr("memem.mine_delta.subprocess.run", lambda *a, **kw: haiku_result)
    monkeypatch.setattr("memem.mine_delta._stable_mined_memory_id",
                        lambda *a, **kw: "aabbccdd-0000-0000-0000-000000000002")
    monkeypatch.setattr("memem.mine_delta._log_event", lambda *a, **kw: None)

    md._reconcile_candidates([candidate], session_id="testsess04")

    # Original list must not be mutated
    assert original_tags == ["type:skill"], (
        f"Original tags list was mutated: {original_tags}"
    )
    if saved_mems:
        saved_tags = saved_mems[0].get("domain_tags") or []
        assert "type:procedural" in saved_tags
