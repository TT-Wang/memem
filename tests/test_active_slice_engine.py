"""Tests for Active Memory Slice Engine orchestration."""

import importlib


def test_generate_active_memory_slice_zero_memory_has_goal(tmp_vault, tmp_cortex_dir):
    from memem.active_slice_engine import generate_active_memory_slice

    slice_obj = generate_active_memory_slice(
        "Continue management proposal",
        scope_id="memem",
        environment={"cwd": "/repo"},
        use_llm=False,
    )

    assert slice_obj["goals"]
    assert slice_obj["goals"][0]["source_type"] == "current_query"
    assert slice_obj["activation_mode"] == "heuristic"


def test_generate_active_memory_slice_uses_constraints_and_no_writeback(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="The proposal must address privacy constraints and permission risks for management review.",
        title="Proposal privacy constraints",
        project="memem",
        source_type="user",
        importance=5,
    )
    obsidian_store._save_memory(mem)
    before_files = set((tmp_vault / "memem" / "memories").glob("*.md"))

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice(
        "Continue the management proposal privacy section",
        scope_id="memem",
        use_llm=False,
    )

    after_files = set((tmp_vault / "memem" / "memories").glob("*.md"))
    assert before_files == after_files
    assert slice_obj["constraints"]
    assert "privacy" in slice_obj["constraints"][0]["summary"].lower()


def test_deprecated_memory_not_in_slice(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="Old deprecated design should not enter normal active slices.",
        title="Deprecated design",
        project="memem",
        source_type="user",
    )
    obsidian_store._save_memory(mem)
    obsidian_store._deprecate_memory(mem["id"])

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice("deprecated design", scope_id="memem", use_llm=False)
    rendered_ids = str(slice_obj)
    assert mem["id"] not in rendered_ids


def test_rendered_active_slice_empty_without_recall_candidates(tmp_vault, tmp_cortex_dir):
    from memem.active_slice_engine import active_slice_response

    rendered = active_slice_response("Prepare project review", scope_id="memem", use_llm=False)
    assert rendered == ""


def test_rendered_active_slice_contains_expected_sections_when_memory_matches(tmp_vault, tmp_cortex_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    obsidian_store._save_memory(obsidian_store._make_memory(
        content="Project review must include concrete risks and next steps.",
        title="Project review constraint",
        project="memem",
        source_type="user",
        importance=5,
    ))

    from memem.active_slice_engine import active_slice_response
    rendered = active_slice_response("Prepare project review risks", scope_id="memem", use_llm=False)
    assert "# Active Memory Slice" in rendered
    assert "## Goals" in rendered
    assert "## Constraints" in rendered
    assert "## Artifacts" in rendered


def test_golden_product_proposal_continuation(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    memories = [
        obsidian_store._make_memory(
            content="The AI memory feature proposal must address privacy constraints and permission review.",
            title="Proposal privacy constraint",
            project="memem",
            source_type="user",
            importance=5,
        ),
        obsidian_store._make_memory(
            content="Decision: keep the management review proposal in MVP framing with concrete examples.",
            title="MVP proposal framing",
            project="memem",
            source_type="user",
            importance=4,
        ),
        obsidian_store._make_memory(
            content="Open tension: feature definition is still fuzzy for management-facing language.",
            title="Proposal fuzzy definition",
            project="memem",
            source_type="user",
            importance=4,
        ),
    ]
    for mem in memories:
        obsidian_store._save_memory(mem)

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice(
        "Continue preparing the AI memory feature proposal for management review",
        scope_id="memem",
        use_llm=False,
    )

    assert slice_obj["goals"]
    assert slice_obj["constraints"]
    assert slice_obj["decisions"]
    assert slice_obj["open_tensions"]


def test_golden_coding_task_continuation(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    for content, title in [
        ("Coding changes must run pytest and ruff before completion.", "Coding verification constraint"),
        ("Failure pattern: previous graph rebuild used unbounded pairwise scoring and timed out.", "Graph rebuild failure pattern"),
    ]:
        obsidian_store._save_memory(obsidian_store._make_memory(
            content=content,
            title=title,
            project="memem",
            source_type="user",
            importance=5,
        ))

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice(
        "Continue coding active slice engine, run pytest ruff, and avoid graph rebuild failure",
        scope_id="memem",
        use_llm=False,
    )

    assert slice_obj["constraints"]
    assert slice_obj["failure_patterns"]


def test_golden_project_review_deprecated_suppression(tmp_vault, tmp_cortex_dir, monkeypatch):
    from memem import obsidian_store, transcripts
    importlib.reload(obsidian_store)
    monkeypatch.setattr(transcripts, "transcript_search", lambda *a, **kw: "No matching transcripts found")

    active = obsidian_store._make_memory(
        content="Project review must surface unresolved risks and concrete next steps.",
        title="Project review constraint",
        project="memem",
        source_type="user",
    )
    deprecated = obsidian_store._make_memory(
        content="Deprecated review plan should not enter active slices.",
        title="Deprecated review plan",
        project="memem",
        source_type="user",
    )
    obsidian_store._save_memory(active)
    obsidian_store._save_memory(deprecated)
    obsidian_store._deprecate_memory(deprecated["id"])

    from memem.active_slice_engine import generate_active_memory_slice
    slice_obj = generate_active_memory_slice("Prepare project review", scope_id="memem", use_llm=False)

    assert active["id"] in str(slice_obj)
    assert deprecated["id"] not in str(slice_obj)
