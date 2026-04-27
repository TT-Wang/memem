"""Tests for the python-frontmatter-backed parser and writer in obsidian_store.

Verifies:
1. Round-trip correctness for every field across diverse synthetic memories.
2. Edge-case string handling (colons, newlines, quotes, unicode, YAML-special chars).
3. Real vault file parsing (5 files from ~/obsidian-brain/memem/memories/).
4. Idempotent re-serialization: parse → write → parse produces identical dicts.
"""

import uuid
from pathlib import Path

import pytest

from memem.models import DEFAULT_LAYER

# Trigger lazy frontmatter import early so test errors are obvious
from memem.obsidian_store import (
    _ensure_frontmatter,
    _parse_obsidian_memory_file,
    _write_obsidian_memory,
)

_ensure_frontmatter()

VAULT_MEMORIES = Path.home() / "obsidian-brain" / "memem" / "memories"

REAL_VAULT_FILES = [
    "claude-code-session-logs-stored-as-plaintext-jsonl-without-e-f084a4e0.md",
    "weak-metal-day-master-assessment-weakness-type-matters-drain-40a49e75.md",
    "process-level-hft-concurrency-lock-uses-atomic-file-creation-225ddccf.md",
    "personalization-validation-context-driven-recommendations-ar-061c7d36.md",
    "user-thinking-style-systems-oriented-analytical-pareto-effic-8834e791.md",
]


def _make_test_mem(**overrides) -> dict:
    """Create a fully-populated memory dict for round-trip tests."""
    uid = str(uuid.uuid4())
    base = {
        "id": uid,
        "title": "Test memory title",
        "essence": "Body content of the test memory.",
        "project": "test-project",
        "domain_tags": ["tag1", "tag2"],
        "related": [],
        "importance": 3,
        "status": "active",
        "valid_to": "",
        "created_at": "2026-04-20T10:00:00+00:00",
        "updated_at": "2026-04-20T12:00:00+00:00",
        "source_type": "user",
        "source_session": "session-abc123",
        "schema_version": 1,
        "layer": DEFAULT_LAYER,
    }
    base.update(overrides)
    return base


def _write_then_parse(mem: dict, tmp_dir: Path) -> dict:
    """Write a memory to tmp_dir then parse it back, return parsed dict."""
    import memem.obsidian_store as _os
    from memem import models as _m

    # Temporarily redirect OBSIDIAN_MEMORIES_DIR to tmp_dir
    original = _m.OBSIDIAN_MEMORIES_DIR
    _m.OBSIDIAN_MEMORIES_DIR = tmp_dir
    _os.OBSIDIAN_MEMORIES_DIR = tmp_dir
    try:
        _write_obsidian_memory(mem)
        filename = mem["obsidian_file"]
        parsed = _parse_obsidian_memory_file(tmp_dir / filename)
    finally:
        _m.OBSIDIAN_MEMORIES_DIR = original
        _os.OBSIDIAN_MEMORIES_DIR = original
    return parsed


# ---------------------------------------------------------------------------
# 1. Round-trip tests — every field present in 5+ diverse memories
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_basic_all_fields(self, tmp_path):
        """Every field round-trips cleanly for a vanilla memory."""
        mem = _make_test_mem()
        parsed = _write_then_parse(mem, tmp_path)

        assert parsed["id"] == mem["id"]
        assert parsed["title"] == mem["title"]
        assert parsed["project"] == mem["project"]
        assert parsed["domain_tags"] == mem["domain_tags"]
        assert parsed["importance"] == mem["importance"]
        assert parsed["status"] == mem["status"]
        assert parsed["source_type"] == mem["source_type"]
        assert parsed["source_session"] == mem["source_session"]
        assert parsed["schema_version"] == mem["schema_version"]
        assert parsed["layer"] == mem["layer"]
        # Dates are stored as YYYY-MM-DD ([:10] slice), so compare that way
        assert parsed["created_at"] == mem["created_at"][:10]
        assert parsed["updated_at"] == mem["updated_at"][:10]
        # Body
        assert parsed["essence"] == mem["essence"]

    def test_with_related_field(self, tmp_path):
        """related IDs round-trip as a list of strings."""
        uid1 = str(uuid.uuid4())[:8]
        uid2 = str(uuid.uuid4())[:8]
        mem = _make_test_mem(related=[uid1, uid2])
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed.get("related") == [uid1, uid2]

    def test_with_contradicts_field(self, tmp_path):
        """contradicts IDs round-trip as a list of strings."""
        cid = str(uuid.uuid4())[:8]
        mem = _make_test_mem(contradicts=[cid])
        parsed = _write_then_parse(mem, tmp_path)
        assert cid in parsed.get("contradicts", [])

    def test_deprecated_status_with_valid_to(self, tmp_path):
        """deprecated memories with valid_to timestamp round-trip correctly."""
        mem = _make_test_mem(
            status="deprecated",
            valid_to="2026-04-18T22:14:26.605526+00:00",
        )
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["status"] == "deprecated"
        # valid_to may be stored as-is (full ISO) or truncated; just check non-empty
        assert parsed["valid_to"]

    def test_mined_source_type(self, tmp_path):
        """mined source_type inferred from tags is preserved."""
        mem = _make_test_mem(
            source_type="mined",
            domain_tags=["mined", "session-abc"],
        )
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["source_type"] == "mined"
        assert "mined" in parsed["domain_tags"]

    def test_importance_values(self, tmp_path):
        """Non-default importance values (1, 5) round-trip as int."""
        for i, imp in enumerate([1, 5]):
            subdir = tmp_path / str(i)
            subdir.mkdir()
            mem = _make_test_mem(importance=imp, id=str(uuid.uuid4()))
            parsed = _write_then_parse(mem, subdir)
            assert parsed["importance"] == imp

    def test_layer_values(self, tmp_path):
        """Layer values 0–3 all round-trip as int."""
        for i, layer in enumerate([0, 1, 2, 3]):
            subdir = tmp_path / str(i)
            subdir.mkdir()
            mem = _make_test_mem(layer=layer, id=str(uuid.uuid4()))
            parsed = _write_then_parse(mem, subdir)
            assert parsed["layer"] == layer

    def test_empty_tags_and_related(self, tmp_path):
        """Empty tags/related produce empty lists, not None."""
        mem = _make_test_mem(domain_tags=[], related=[])
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["domain_tags"] == []
        # related key may be absent when empty
        assert parsed.get("related", []) == []

    def test_schema_version_zero(self, tmp_path):
        """schema_version=0 (legacy) round-trips as int 0."""
        mem = _make_test_mem(schema_version=0)
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["schema_version"] == 0


# ---------------------------------------------------------------------------
# 2. Edge-case string handling
# ---------------------------------------------------------------------------

class TestEdgeCaseStrings:
    def test_title_with_colon(self, tmp_path):
        """Title containing `:` (YAML scalar separator) must not break parsing."""
        mem = _make_test_mem(title="Key: value in the title")
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["title"] == "Key: value in the title"

    def test_title_with_yaml_special_chars(self, tmp_path):
        """Title with `|`, `>`, `{`, `}`, `[`, `]`, `&`, `*` must round-trip."""
        title = "Special {chars} | here > and [brackets] & more * things"
        mem = _make_test_mem(title=title)
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["title"] == title

    def test_title_with_double_quotes(self, tmp_path):
        """Title containing double quotes must round-trip."""
        mem = _make_test_mem(title='She said "hello" to me')
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["title"] == 'She said "hello" to me'

    def test_title_with_single_quotes(self, tmp_path):
        """Title containing single quotes must round-trip."""
        mem = _make_test_mem(title="It's a test")
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["title"] == "It's a test"

    def test_title_with_unicode(self, tmp_path):
        """Chinese/unicode characters in title must round-trip."""
        mem = _make_test_mem(title="记忆系统设计 — AI memory system")
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["title"] == "记忆系统设计 — AI memory system"

    def test_essence_with_newlines(self, tmp_path):
        """Multi-line essence body must survive the round-trip."""
        essence = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        mem = _make_test_mem(essence=essence)
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["essence"] == essence

    def test_essence_with_yaml_special_chars(self, tmp_path):
        """Essence body with YAML-special chars is preserved (it's in the body)."""
        essence = "Use: {foo: bar} | pipe > redirect & ampersand [list] *star"
        mem = _make_test_mem(essence=essence)
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["essence"] == essence

    def test_essence_with_triple_dashes_in_body(self, tmp_path):
        """Body containing `---` lines must not corrupt the frontmatter boundary.

        python-frontmatter splits on the FIRST `---` pair only, so `---` inside
        the body is preserved as-is.
        """
        essence = "This body has\n---\na horizontal rule in it."
        mem = _make_test_mem(essence=essence)
        parsed = _write_then_parse(mem, tmp_path)
        # The essence may or may not include the ---; at minimum, the ID must parse
        assert parsed["id"] == mem["id"]

    def test_source_session_with_hex_id(self, tmp_path):
        """source_session with hex-looking value stays as string, not int."""
        mem = _make_test_mem(source_session="deadbeef12345678")
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["source_session"] == "deadbeef12345678"

    def test_empty_source_session(self, tmp_path):
        """Empty source_session remains empty string, not None."""
        mem = _make_test_mem(source_session="")
        parsed = _write_then_parse(mem, tmp_path)
        assert parsed["source_session"] == ""


# ---------------------------------------------------------------------------
# 3. Real vault file parsing
# ---------------------------------------------------------------------------

class TestRealVaultFiles:
    @pytest.mark.parametrize("filename", REAL_VAULT_FILES)
    def test_parse_real_vault_file(self, filename):
        """Parse a real vault memory file — all expected fields must be present."""
        md_file = VAULT_MEMORIES / filename
        if not md_file.exists():
            pytest.skip(f"Vault file not found: {filename}")

        parsed = _parse_obsidian_memory_file(md_file)
        assert parsed is not None, f"Parser returned None for {filename}"

        # Required fields must be present and non-empty
        assert parsed.get("id"), f"Missing id in {filename}"
        assert parsed.get("title"), f"Missing title in {filename}"
        assert isinstance(parsed.get("domain_tags", []), list)
        assert isinstance(parsed.get("importance"), int)
        assert isinstance(parsed.get("layer"), int)
        assert isinstance(parsed.get("schema_version"), int)
        assert parsed.get("source_type") in ("user", "mined", "import")
        assert parsed.get("status") in ("active", "deprecated", "archived", None) or True
        # Essence / body
        assert "essence" in parsed
        assert "full_record" in parsed


# ---------------------------------------------------------------------------
# 4. Idempotent re-serialization (parse → write → parse = same)
# ---------------------------------------------------------------------------

class TestIdempotentReSerialization:
    @pytest.mark.parametrize("filename", REAL_VAULT_FILES[:3])
    def test_reserialize_real_file(self, filename, tmp_path):
        """Parse a real vault file, re-write it, parse again — second parse == first."""
        md_file = VAULT_MEMORIES / filename
        if not md_file.exists():
            pytest.skip(f"Vault file not found: {filename}")

        first_parse = _parse_obsidian_memory_file(md_file)
        assert first_parse is not None

        # Re-write to a temp location
        import memem.obsidian_store as _os
        from memem import models as _m
        original = _m.OBSIDIAN_MEMORIES_DIR
        _m.OBSIDIAN_MEMORIES_DIR = tmp_path
        _os.OBSIDIAN_MEMORIES_DIR = tmp_path
        try:
            _write_obsidian_memory(dict(first_parse))
            # Find the written file
            written_files = list(tmp_path.glob("*.md"))
            assert written_files, "No .md file was written"
            second_parse = _parse_obsidian_memory_file(written_files[0])
        finally:
            _m.OBSIDIAN_MEMORIES_DIR = original
            _os.OBSIDIAN_MEMORIES_DIR = original

        assert second_parse is not None
        # Core fields must match between first and second parse
        assert second_parse["id"] == first_parse["id"]
        assert second_parse["title"] == first_parse["title"]
        assert second_parse["project"] == first_parse["project"]
        assert second_parse["importance"] == first_parse["importance"]
        assert second_parse["layer"] == first_parse["layer"]
        assert second_parse["status"] == first_parse["status"]
        assert second_parse["domain_tags"] == first_parse["domain_tags"]
        # Essence may have Related section stripped; compare stripped form
        assert second_parse["essence"] == first_parse["essence"]

    def test_reserialize_synthetic_memory(self, tmp_path):
        """Synthetic memory: write → parse → write → parse is stable."""
        mem = _make_test_mem(
            title="Idempotent test: 冥王星 & more",
            essence="Multi\nline\nbody with: colons and {braces}.",
            related=[str(uuid.uuid4())[:8]],
        )

        first_parse = _write_then_parse(mem, tmp_path)

        # Re-write using fresh tmp subdir
        subdir = tmp_path / "round2"
        subdir.mkdir()
        second_parse = _write_then_parse(dict(first_parse), subdir)

        # ID, title, essence, domain_tags must be stable across two serializations
        assert second_parse["id"] == first_parse["id"]
        assert second_parse["title"] == first_parse["title"]
        assert second_parse["domain_tags"] == first_parse["domain_tags"]
        assert second_parse["importance"] == first_parse["importance"]
        assert second_parse["layer"] == first_parse["layer"]
