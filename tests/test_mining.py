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
