"""Tests for A-MemGuard lessons anti-memory store (memem v2 m5)."""

from __future__ import annotations

import importlib


def _reload_lessons(monkeypatch=None):
    """Reload lessons module so LESSONS_DIR picks up env changes."""
    from memem import lessons, models
    importlib.reload(models)
    importlib.reload(lessons)
    return lessons


# ---------------------------------------------------------------------------
# Test 1: record_lesson creates file in lessons dir
# ---------------------------------------------------------------------------

def test_record_lesson_creates_file_in_lessons_dir(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    lesson_id = lessons.record_lesson(
        targeted_memory_id="mem-abc123",
        query_class="JWT auth",
        anti_pattern="Do not apply JWT secret from mem-abc123 to this auth query",
        evidence="User corrected: that secret is production only",
        source="user",
    )

    assert lesson_id
    lessons_dir = lessons.LESSONS_DIR
    assert lessons_dir.exists()
    files = list(lessons_dir.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert f"id: {lesson_id}" in content
    assert "targeted_memory_id: mem-abc123" in content
    assert "query_class: JWT auth" in content
    assert "source: user" in content


# ---------------------------------------------------------------------------
# Test 2: two calls return different lesson IDs
# ---------------------------------------------------------------------------

def test_record_lesson_returns_unique_lesson_id(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    id1 = lessons.record_lesson("mem-a", "auth", "do not use A for auth")
    id2 = lessons.record_lesson("mem-b", "setup", "do not use B for setup")

    assert id1 != id2
    assert len(id1) == 36  # UUID format
    assert len(id2) == 36


# ---------------------------------------------------------------------------
# Test 3: list_lessons returns all recorded lessons
# ---------------------------------------------------------------------------

def test_list_lessons_returns_recorded_lessons(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    lessons.record_lesson("mem-1", "class-A", "anti-pattern A")
    lessons.record_lesson("mem-2", "class-B", "anti-pattern B")
    lessons.record_lesson("mem-3", "class-C", "anti-pattern C")

    result = lessons.list_lessons()
    assert len(result) == 3
    ids_found = {r["targeted_memory_id"] for r in result}
    assert ids_found == {"mem-1", "mem-2", "mem-3"}


# ---------------------------------------------------------------------------
# Test 4: list_lessons returns empty list when dir doesn't exist
# ---------------------------------------------------------------------------

def test_list_lessons_empty_when_dir_missing(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    # Don't create any lessons — dir shouldn't exist
    assert not lessons.LESSONS_DIR.exists()
    result = lessons.list_lessons()
    assert result == []


# ---------------------------------------------------------------------------
# Test 5: query_class_matches — substring match
# ---------------------------------------------------------------------------

def test_query_class_matches_substring():
    from memem.lessons import query_class_matches

    assert query_class_matches("JWT auth setup", "JWT") is True


# ---------------------------------------------------------------------------
# Test 6: query_class_matches — word overlap match
# ---------------------------------------------------------------------------

def test_query_class_matches_word_overlap():
    from memem.lessons import query_class_matches

    # "how do I configure JWT auth" and "auth configuration" share: auth, configure/configuration
    # Actually "configure" vs "configuration" are different words; let's use direct overlap:
    # query="how do I setup auth configuration" + class="auth configuration" → overlap >= 2
    assert query_class_matches("how do I setup auth configuration", "auth configuration") is True


# ---------------------------------------------------------------------------
# Test 7: query_class_matches — returns False when unrelated
# ---------------------------------------------------------------------------

def test_query_class_matches_returns_false_when_unrelated():
    from memem.lessons import query_class_matches

    assert query_class_matches("weather today", "JWT") is False


# ---------------------------------------------------------------------------
# Test 8: excluded_memory_ids_for_query returns targeted IDs when class matches
# ---------------------------------------------------------------------------

def test_excluded_memory_ids_for_query_returns_targeted_ids(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    lessons.record_lesson("mem-jwt-1", "JWT", "do not use mem-jwt-1 for JWT queries")

    result = lessons.excluded_memory_ids_for_query("JWT setup guide")
    assert "mem-jwt-1" in result


# ---------------------------------------------------------------------------
# Test 9: excluded_memory_ids_for_query skips unmatched classes
# ---------------------------------------------------------------------------

def test_excluded_memory_ids_for_query_skips_unmatched_classes(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    lessons.record_lesson("mem-weather-1", "weather", "do not use mem-weather-1 for weather")

    result = lessons.excluded_memory_ids_for_query("JWT setup guide")
    assert "mem-weather-1" not in result
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 11: multiple lessons target same memory for different classes
# ---------------------------------------------------------------------------

def test_multiple_lessons_target_same_memory_for_different_classes(tmp_vault, tmp_cortex_dir):
    from memem import lessons
    importlib.reload(lessons)

    # Two lessons, same memory, different query classes
    id1 = lessons.record_lesson("shared-mem", "JWT", "do not use shared-mem for JWT")
    id2 = lessons.record_lesson("shared-mem", "OAuth", "do not use shared-mem for OAuth")

    assert id1 != id2

    # Query matching JWT → shared-mem excluded
    result_jwt = lessons.excluded_memory_ids_for_query("JWT authentication flow")
    assert "shared-mem" in result_jwt

    # Query matching OAuth → shared-mem also excluded
    result_oauth = lessons.excluded_memory_ids_for_query("OAuth token validation")
    assert "shared-mem" in result_oauth

    # Either way, it should appear only once (set semantics)
    assert len(result_jwt) == 1
    assert len(result_oauth) == 1
