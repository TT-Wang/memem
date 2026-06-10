"""Tests for mining pipeline (mocked Haiku)."""



def test_extract_json_string():
    from memem.mining import _extract_json_string
    assert _extract_json_string('[{"title": "test"}]') == '[{"title": "test"}]'
    assert _extract_json_string('prefix [{"title": "x"}] suffix') == '[{"title": "x"}]'
    assert _extract_json_string('no json here') is None


def test_repair_json_bracket_in_string():
    """_repair_json must skip over string literals so a title like
    ``"see [note"`` doesn't fool the bracket counter into appending a
    spurious closer that produces invalid JSON."""
    import json

    from memem.mining import _repair_json

    balanced = '[{"title": "see [note", "content": "ok"}]'
    assert _repair_json(balanced) == balanced
    assert json.loads(_repair_json(balanced)) == [{"title": "see [note", "content": "ok"}]

    truncated_outer = '[{"title": "see [note", "content": "ok"}'
    assert json.loads(_repair_json(truncated_outer)) == [{"title": "see [note", "content": "ok"}]

    # Mid-string truncation: also closes the string
    truncated_mid = '[{"title": "truncated pr'
    parsed = json.loads(_repair_json(truncated_mid))
    assert parsed == [{"title": "truncated pr"}]


def test_normalize_scope_id_alias():
    """Aliases normalize to canonical names so consolidation doesn't skip
    a project tagged under a pre-rename alias (e.g. 'cortex' → 'cortex-plugin')."""
    from memem.models import _normalize_scope_id
    assert _normalize_scope_id("cortex") == "cortex-plugin"
    assert _normalize_scope_id("default") == "general"
    assert _normalize_scope_id("") == "general"
    assert _normalize_scope_id("substrate") == "substrate"


# ---------------------------------------------------------------------------
# C2: keys validation in _mine_one_chunk
# ---------------------------------------------------------------------------

def test_mine_one_chunk_keys_cap_and_sanitize(monkeypatch):
    """_mine_one_chunk caps keys to 8 items and 60 chars each, discards non-strings."""
    import json
    from memem import mining

    raw_keys = [
        "short",
        "a" * 70,   # over 60 chars — should be truncated to 60
        123,         # non-string — should be discarded
        None,        # non-string — should be discarded
        "auth",
        "oauth2",
        "pkce",
        "openid",
        "jwt",       # 9th item — should be dropped (cap 8)
    ]
    haiku_output = json.dumps([{
        "title": "OAuth keys test",
        "content": "Details about OAuth flow with PKCE",
        "project": "general",
        "importance": 3,
        "keys": raw_keys,
    }])

    monkeypatch.setattr(mining, "_run_haiku", lambda _prompt: haiku_output)
    results = mining._mine_one_chunk(["some conversation text"])

    assert len(results) == 1
    entry = results[0]
    keys = entry["keys"]
    assert isinstance(keys, list), "keys should be a list"
    # Non-strings (123, None) discarded; remaining: short, a*70, auth, oauth2, pkce, openid, jwt (cap 8 means first 8 str)
    # After discarding non-strings: short(1), a*70(2), auth(3), oauth2(4), pkce(5), openid(6), jwt(7) = 7 items → all fit in 8
    assert len(keys) <= 8, f"Expected ≤8 keys, got {len(keys)}"
    # Check that the long key was truncated to 60
    for k in keys:
        assert len(k) <= 60, f"Key '{k[:20]}...' exceeds 60 chars"
        assert isinstance(k, str), f"Key should be str, got {type(k)}"
    # Numeric/None items discarded
    assert 123 not in keys
    assert None not in keys
    assert "short" in keys


def test_mine_one_chunk_keys_missing_gives_empty(monkeypatch):
    """_mine_one_chunk sets keys=[] when the field is missing from Haiku output."""
    import json
    from memem import mining

    haiku_output = json.dumps([{
        "title": "No keys field",
        "content": "Some content without keys",
        "project": "general",
        "importance": 3,
        # No "keys" field
    }])

    monkeypatch.setattr(mining, "_run_haiku", lambda _prompt: haiku_output)
    results = mining._mine_one_chunk(["conversation"])
    assert results[0]["keys"] == []


def test_mine_one_chunk_keys_null_gives_empty(monkeypatch):
    """_mine_one_chunk sets keys=[] when keys is explicitly null."""
    import json
    from memem import mining

    haiku_output = json.dumps([{
        "title": "Null keys field",
        "content": "Some content with null keys",
        "project": "general",
        "importance": 3,
        "keys": None,
    }])

    monkeypatch.setattr(mining, "_run_haiku", lambda _prompt: haiku_output)
    results = mining._mine_one_chunk(["conversation"])
    assert results[0]["keys"] == []


def test_mine_one_chunk_keys_over_8_capped(monkeypatch):
    """_mine_one_chunk caps keys to max 8 items."""
    import json
    from memem import mining

    haiku_output = json.dumps([{
        "title": "Many keys",
        "content": "Content with too many keys",
        "project": "general",
        "importance": 3,
        "keys": [f"key{i}" for i in range(15)],  # 15 keys, should be capped to 8
    }])

    monkeypatch.setattr(mining, "_run_haiku", lambda _prompt: haiku_output)
    results = mining._mine_one_chunk(["conversation"])
    assert len(results[0]["keys"]) == 8
