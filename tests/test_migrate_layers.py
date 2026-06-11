"""Tests for memem/migrate_layers.py and the --migrate-layers CLI handler.

All tests use tmp_vault (from conftest) and monkeypatched subprocess.run so
no live Haiku calls are made.

SAFETY INVARIANT: vault memory files must be BYTE-IDENTICAL before and after
both dry-run and apply. Verified explicitly in relevant tests.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mem(
    tmp_vault: Path,
    mem_id: str,
    title: str,
    layer: int | None = 1,
    project: str = "testproject",
    essence: str = "Test essence",
    status: str = "active",
) -> Path:
    """Write a raw memory markdown file into the vault and return its path."""
    memories_dir = tmp_vault / "memem" / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    slug = title.lower().replace(" ", "-")
    file_name = f"{mem_id}-{slug}.md"
    file_path = memories_dir / file_name

    frontmatter_lines = [
        "---",
        f"id: {mem_id}",
        f"title: {title}",
        f"project: {project}",
        f"status: {status}",
        "importance: 3",
        "schema_version: 1",
        "source_type: user",
        "tags: []",
    ]
    if layer is not None:
        frontmatter_lines.append(f"layer: {layer}")
    frontmatter_lines += [
        "---",
        "",
        f"# {title}",
        "",
        essence,
    ]

    file_path.write_text("\n".join(frontmatter_lines), encoding="utf-8")
    return file_path


def _reload_obsidian(tmp_vault: Path) -> None:
    """Force obsidian_store to re-scan the vault."""
    import memem.obsidian_store as obs
    # Clear the in-memory cache by forcing a reload
    importlib.reload(obs)


def _snapshot_vault(tmp_vault: Path) -> dict[str, bytes]:
    """Return a snapshot of all memory file bytes."""
    memories_dir = tmp_vault / "memem" / "memories"
    if not memories_dir.exists():
        return {}
    return {
        str(p.relative_to(memories_dir)): p.read_bytes()
        for p in sorted(memories_dir.glob("*.md"))
    }


def _canned_haiku(action: str, profile: str | None, section: str | None, line: str | None):
    """Return a fake subprocess.run callable producing a Haiku-style JSON array."""
    def fake_run(cmd, *args, input: str = "", **kwargs):
        items = []
        for mem_line in input.strip().splitlines():
            parts = mem_line.split("|", 2)
            mem_id8 = parts[0] if parts else "?"
            items.append({
                "id": mem_id8,
                "action": action,
                "profile": profile,
                "section": section,
                "line": line,
            })
        stdout = json.dumps(items)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return fake_run


def _failing_haiku():
    """Return a fake subprocess.run callable that returns a non-zero exit."""
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="haiku error")
    return fake_run


# ---------------------------------------------------------------------------
# Reload migrate_layers + profiles after tmp_vault sets vault env var
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reload_modules(tmp_vault):
    import memem.profiles as prof
    importlib.reload(prof)
    import memem.migrate_layers as ml
    importlib.reload(ml)
    yield


@pytest.fixture
def ml(tmp_vault):
    import memem.migrate_layers as m
    importlib.reload(m)
    return m


# ---------------------------------------------------------------------------
# collect_candidates
# ---------------------------------------------------------------------------

class TestCollectCandidates:
    def test_picks_layer0_and_layer1(self, tmp_vault, ml):
        _make_mem(tmp_vault, "aabbcc01", "Layer0 mem", layer=0)
        _make_mem(tmp_vault, "aabbcc02", "Layer1 mem", layer=1)
        _reload_obsidian(tmp_vault)
        candidates = ml.collect_candidates()
        ids = [(c.get("id") or "")[:8] for c in candidates]
        assert "aabbcc01" in ids
        assert "aabbcc02" in ids

    def test_skips_layer2(self, tmp_vault, ml):
        _make_mem(tmp_vault, "aabbcc03", "Layer2 mem", layer=2)
        _reload_obsidian(tmp_vault)
        candidates = ml.collect_candidates()
        ids = [(c.get("id") or "")[:8] for c in candidates]
        assert "aabbcc03" not in ids

    def test_skips_absent_layer(self, tmp_vault, ml):
        _make_mem(tmp_vault, "aabbcc04", "No layer mem", layer=None)
        _reload_obsidian(tmp_vault)
        candidates = ml.collect_candidates()
        ids = [(c.get("id") or "")[:8] for c in candidates]
        assert "aabbcc04" not in ids

    def test_exclude_ids_filter(self, tmp_vault, ml):
        _make_mem(tmp_vault, "aabbcc05", "Excluded mem", layer=1)
        _make_mem(tmp_vault, "aabbcc06", "Kept mem", layer=1)
        _reload_obsidian(tmp_vault)
        candidates = ml.collect_candidates(exclude_ids={"aabbcc05"})
        ids = [(c.get("id") or "")[:8] for c in candidates]
        assert "aabbcc05" not in ids
        assert "aabbcc06" in ids

    def test_skips_deprecated(self, tmp_vault, ml):
        _make_mem(tmp_vault, "aabbcc07", "Deprecated mem", layer=1, status="deprecated")
        _reload_obsidian(tmp_vault)
        candidates = ml.collect_candidates()
        ids = [(c.get("id") or "")[:8] for c in candidates]
        assert "aabbcc07" not in ids


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

class TestPropose:
    def test_profile_proposals_parsed(self, tmp_vault, ml, monkeypatch):
        _make_mem(tmp_vault, "bb000001", "Stack fact", layer=1)
        _reload_obsidian(tmp_vault)
        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "project", "Identity", "Python project"),
        )
        candidates = ml.collect_candidates()
        proposals = ml.propose(candidates)
        assert len(proposals) == 1
        p = proposals[0]
        assert p["action"] == "PROFILE"
        assert p["profile"] == "project"
        assert p["section"] == "Identity"
        assert p["line"] == "Python project"

    def test_keep_proposals(self, tmp_vault, ml, monkeypatch):
        _make_mem(tmp_vault, "cc000001", "Narrative mem", layer=1)
        _reload_obsidian(tmp_vault)
        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("KEEP", None, None, None),
        )
        candidates = ml.collect_candidates()
        proposals = ml.propose(candidates)
        assert len(proposals) == 1
        assert proposals[0]["action"] == "KEEP"

    def test_unknown_section_degrades_to_keep(self, tmp_vault, ml, monkeypatch):
        _make_mem(tmp_vault, "dd000001", "Bad section mem", layer=1)
        _reload_obsidian(tmp_vault)
        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "user", "INVALID_SECTION", "Something"),
        )
        candidates = ml.collect_candidates()
        proposals = ml.propose(candidates)
        assert proposals[0]["action"] == "KEEP"

    def test_haiku_failure_all_keep(self, tmp_vault, ml, monkeypatch):
        _make_mem(tmp_vault, "ee000001", "Mem 1", layer=1)
        _make_mem(tmp_vault, "ee000002", "Mem 2", layer=1)
        _reload_obsidian(tmp_vault)
        monkeypatch.setattr("memem.migrate_layers.subprocess.run", _failing_haiku())
        candidates = ml.collect_candidates()
        proposals = ml.propose(candidates)
        assert all(p["action"] == "KEEP" for p in proposals)
        # note should indicate failure
        assert any("haiku-failed" in (p.get("note") or "") for p in proposals)

    def test_batching_by_project(self, tmp_vault, ml, monkeypatch):
        """Creates >20 memories in same project — must call Haiku multiple times."""
        call_count = {"n": 0}

        def counting_haiku(cmd, *args, input: str = "", **kwargs):
            call_count["n"] += 1
            items = []
            for mem_line in input.strip().splitlines():
                parts = mem_line.split("|", 2)
                mem_id8 = parts[0] if parts else "?"
                items.append({
                    "id": mem_id8,
                    "action": "KEEP",
                    "profile": None,
                    "section": None,
                    "line": None,
                })
            return SimpleNamespace(returncode=0, stdout=json.dumps(items), stderr="")

        # Create 25 layer-1 memories all in same project
        for i in range(25):
            mem_id = f"{i:08x}"
            _make_mem(tmp_vault, mem_id, f"Mem {i}", layer=1, project="sameproject")
        _reload_obsidian(tmp_vault)

        monkeypatch.setattr("memem.migrate_layers.subprocess.run", counting_haiku)
        candidates = ml.collect_candidates()
        _ = ml.propose(candidates, batch_size=20)

        # 25 mems / 20 batch_size = ceil(25/20) = 2 Haiku calls
        assert call_count["n"] == 2

    def test_empty_candidates_returns_empty(self, tmp_vault, ml):
        proposals = ml.propose([])
        assert proposals == []


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_report_contains_summary_counts(self, tmp_vault, tmp_path, ml, monkeypatch):
        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "project", "Identity", "Python stack"),
        )
        _make_mem(tmp_vault, "ff000001", "Stack fact", layer=1)
        _make_mem(tmp_vault, "ff000002", "Narrative", layer=1, project="otherproject")
        _reload_obsidian(tmp_vault)

        # Manually construct proposals
        proposals = [
            {
                "id": "ff000001",
                "title": "Stack fact",
                "project": "testproject",
                "action": "PROFILE",
                "profile": "project",
                "section": "Identity",
                "line": "Python stack",
            },
            {
                "id": "ff000002",
                "title": "Narrative",
                "project": "otherproject",
                "action": "KEEP",
                "profile": None,
                "section": None,
                "line": None,
            },
        ]

        rpt_path = tmp_path / "report.md"
        ml.write_report(proposals, rpt_path)

        content = rpt_path.read_text()
        assert "Total candidates" in content
        assert "PROFILE proposals" in content
        assert "KEEP" in content
        assert "ff000001" in content

    def test_keep_list_collapsed_beyond_10(self, tmp_vault, tmp_path, ml):
        """More than 10 KEEPs → first 10 listed + 'and N more'."""
        proposals = []
        for i in range(15):
            proposals.append({
                "id": f"{i:08x}",
                "title": f"Keep mem {i}",
                "project": "general",
                "action": "KEEP",
                "profile": None,
                "section": None,
                "line": None,
            })

        rpt_path = tmp_path / "keep_report.md"
        ml.write_report(proposals, rpt_path)

        content = rpt_path.read_text()
        assert "and 5 more" in content

    def test_profile_section_breakdown_in_report(self, tmp_vault, tmp_path, ml):
        proposals = [
            {
                "id": "aa000001",
                "title": "User pref",
                "project": "general",
                "action": "PROFILE",
                "profile": "user",
                "section": "Preferences",
                "line": "Prefers dark mode",
            },
        ]
        rpt_path = tmp_path / "profile_report.md"
        ml.write_report(proposals, rpt_path)
        content = rpt_path.read_text()
        assert "profile_user" in content
        assert "Preferences" in content
        assert "Prefers dark mode" in content


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

class TestApply:
    def test_apply_calls_append_fact(self, tmp_vault, ml, monkeypatch):
        called = []

        def mock_append_fact(name, section, line):
            called.append((name, section, line))
            return "appended"

        monkeypatch.setattr("memem.migrate_layers.append_fact", mock_append_fact)

        proposals = [
            {
                "id": "11000001",
                "title": "Stack",
                "project": "testproj",
                "action": "PROFILE",
                "profile": "user",
                "section": "Preferences",
                "line": "Uses vim",
            }
        ]
        summary = ml.apply(proposals)
        assert len(called) == 1
        assert called[0] == ("user", "Preferences", "Uses vim")
        assert summary["appended"] == 1

    def test_apply_idempotent_dedup(self, tmp_vault, ml, monkeypatch):
        """Second apply → duplicate status for all items."""
        monkeypatch.setattr(
            "memem.migrate_layers.append_fact",
            lambda name, section, line: "duplicate",
        )
        proposals = [
            {
                "id": "22000001",
                "title": "Stack",
                "project": "general",
                "action": "PROFILE",
                "profile": "user",
                "section": "Preferences",
                "line": "Uses dark mode",
            }
        ]
        summary = ml.apply(proposals)
        assert summary["duplicate"] == 1
        assert summary["appended"] == 0

    def test_apply_rejected_full(self, tmp_vault, ml, monkeypatch):
        monkeypatch.setattr(
            "memem.migrate_layers.append_fact",
            lambda name, section, line: "rejected_full",
        )
        proposals = [
            {
                "id": "33000001",
                "title": "Big profile",
                "project": "general",
                "action": "PROFILE",
                "profile": "user",
                "section": "Environment",
                "line": "Linux on x86_64",
            }
        ]
        summary = ml.apply(proposals)
        assert summary["rejected_full"] == 1

    def test_apply_keeps_stay_as_keeps(self, tmp_vault, ml, monkeypatch):
        called = []
        monkeypatch.setattr(
            "memem.migrate_layers.append_fact",
            lambda *a: called.append(a) or "appended",
        )
        proposals = [
            {
                "id": "44000001",
                "title": "Narrative",
                "project": "general",
                "action": "KEEP",
                "profile": None,
                "section": None,
                "line": None,
            }
        ]
        summary = ml.apply(proposals)
        assert len(called) == 0
        assert summary["kept"] == 1

    def test_apply_project_profile_uses_project_name(self, tmp_vault, ml, monkeypatch):
        """profile='project' → append_fact called with the memory's project name."""
        called = []

        def mock_append_fact(name, section, line):
            called.append(name)
            return "appended"

        monkeypatch.setattr("memem.migrate_layers.append_fact", mock_append_fact)

        proposals = [
            {
                "id": "55000001",
                "title": "Identity",
                "project": "my-app",
                "action": "PROFILE",
                "profile": "project",
                "section": "Identity",
                "line": "FastAPI REST API",
            }
        ]
        ml.apply(proposals)
        # Profile name should be the project name (possibly normalized)
        assert len(called) == 1
        assert "my-app" in called[0] or "my" in called[0]


# ---------------------------------------------------------------------------
# Memory files not deleted after dry-run or apply
# ---------------------------------------------------------------------------

class TestVaultIntegrity:
    def test_dry_run_does_not_touch_memories(self, tmp_vault, tmp_path, ml, monkeypatch):
        _make_mem(tmp_vault, "77000001", "Important mem", layer=1)
        _reload_obsidian(tmp_vault)
        snap_before = _snapshot_vault(tmp_vault)

        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "user", "Preferences", "Dark mode"),
        )

        report_path = tmp_path / "report.md"
        ml.run_migration(apply_mode=False, yes=False, report_path=report_path)
        snap_after = _snapshot_vault(tmp_vault)
        assert snap_before == snap_after, "dry-run must not modify memory files"

    def test_apply_does_not_touch_memories(self, tmp_vault, tmp_path, ml, monkeypatch):
        _make_mem(tmp_vault, "88000001", "Stack fact", layer=1)
        _reload_obsidian(tmp_vault)
        snap_before = _snapshot_vault(tmp_vault)

        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "user", "Preferences", "Vim user"),
        )

        report_path = tmp_path / "apply_report.md"
        ml.run_migration(apply_mode=True, yes=True, report_path=report_path)
        snap_after = _snapshot_vault(tmp_vault)
        assert snap_before == snap_after, "apply must not modify or delete memory files"


# ---------------------------------------------------------------------------
# CLI integration via dispatch_cli
# ---------------------------------------------------------------------------

def _dispatch(argv, capsys, monkeypatch, tmp_vault, tmp_cortex_dir):
    """Invoke dispatch_cli in-process."""
    from memem import cli
    importlib.reload(cli)
    mcp_stub = SimpleNamespace(run=lambda **_: None)
    try:
        cli.dispatch_cli(["memem", *argv], mcp_stub)
    except SystemExit:
        pass
    return capsys.readouterr()


class TestCliMigrateLayersCommand:
    def test_dry_run_writes_report_and_json(
        self, tmp_vault, tmp_cortex_dir, tmp_path, capsys, monkeypatch
    ):
        _make_mem(tmp_vault, "99000001", "Layer1 mem", layer=1)
        _reload_obsidian(tmp_vault)

        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("KEEP", None, None, None),
        )

        report_path = tmp_path / "test-report.md"
        json_path = tmp_path / "migrate-layers-proposals.json"

        out = _dispatch(
            ["--migrate-layers", "--report", str(report_path)],
            capsys,
            monkeypatch,
            tmp_vault,
            tmp_cortex_dir,
        )

        assert report_path.exists(), "dry-run must write the report file"
        assert json_path.exists(), "dry-run must write the proposals JSON"
        combined = out.out + out.err
        assert "dry-run" in combined.lower() or "review" in combined.lower() or "report" in combined.lower()

    def test_apply_without_yes_succeeds(
        self, tmp_vault, tmp_cortex_dir, tmp_path, capsys, monkeypatch
    ):
        """Phase 4.5 (v2.8): --apply alone is sufficient — the dry-run report
        review is the human gate; requiring an undocumented --yes made every
        documented apply invocation exit 1. --yes remains an accepted no-op."""
        import memem.migrate_layers as ml
        from memem import cli
        importlib.reload(cli)
        mcp_stub = SimpleNamespace(run=lambda **_: None)

        # No candidates in the tmp vault → propose returns empty → apply is a
        # clean no-op; the point is that it must NOT refuse with exit 1.
        rc = 0
        try:
            cli.dispatch_cli(["memem", "--migrate-layers", "--apply"], mcp_stub)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 0

        out = capsys.readouterr()
        assert rc != 1, f"--apply alone must not be refused. stderr: {out.err}"
        assert "requires --yes" not in (out.err or "") + (out.out or "")

    def test_apply_with_yes_calls_append_fact(
        self, tmp_vault, tmp_cortex_dir, tmp_path, capsys, monkeypatch
    ):
        _make_mem(tmp_vault, "aa100001", "Profile mem", layer=1)
        _reload_obsidian(tmp_vault)

        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "user", "Preferences", "Loves Python"),
        )

        called = []
        monkeypatch.setattr(
            "memem.migrate_layers.append_fact",
            lambda name, section, line: called.append((name, section, line)) or "appended",
        )

        report_path = tmp_path / "apply_test_report.md"

        _dispatch(
            ["--migrate-layers", "--apply", "--yes", "--report", str(report_path)],
            capsys,
            monkeypatch,
            tmp_vault,
            tmp_cortex_dir,
        )

        assert len(called) == 1, "append_fact must be called for each PROFILE proposal"
        assert called[0][1] == "Preferences"
        assert called[0][2] == "Loves Python"

    def test_exclude_flag_filters_candidates(
        self, tmp_vault, tmp_cortex_dir, tmp_path, capsys, monkeypatch
    ):
        _make_mem(tmp_vault, "bb100001", "Excluded mem", layer=1)
        _make_mem(tmp_vault, "bb100002", "Kept mem", layer=1)
        _reload_obsidian(tmp_vault)

        seen_ids: list[str] = []

        def capturing_haiku(cmd, *args, input: str = "", **kwargs):
            for mem_line in input.strip().splitlines():
                parts = mem_line.split("|", 2)
                if parts:
                    seen_ids.append(parts[0])
            items = [
                {"id": parts[0].split("|")[0], "action": "KEEP",
                 "profile": None, "section": None, "line": None}
                for parts in [line.split("|", 2) for line in input.strip().splitlines() if line]
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(items), stderr="")

        monkeypatch.setattr("memem.migrate_layers.subprocess.run", capturing_haiku)

        report_path = tmp_path / "excl_report.md"
        _dispatch(
            [
                "--migrate-layers",
                "--report",
                str(report_path),
                "--exclude",
                "bb100001",
            ],
            capsys,
            monkeypatch,
            tmp_vault,
            tmp_cortex_dir,
        )

        assert "bb100001" not in seen_ids, "excluded id must not appear in Haiku batch"
        assert "bb100002" in seen_ids, "non-excluded id must be included"

    def test_no_profiles_modified_on_dry_run_via_cli(
        self, tmp_vault, tmp_cortex_dir, tmp_path, capsys, monkeypatch
    ):
        """Profiles directory must not be created/modified during dry-run."""
        import memem.profiles as prof
        importlib.reload(prof)

        _make_mem(tmp_vault, "cc100001", "Stack mem", layer=1)
        _reload_obsidian(tmp_vault)

        monkeypatch.setattr(
            "memem.migrate_layers.subprocess.run",
            _canned_haiku("PROFILE", "user", "Preferences", "Vim user"),
        )

        profiles_dir = tmp_vault / "memem" / "profiles"
        report_path = tmp_path / "no_profile_report.md"

        _dispatch(
            ["--migrate-layers", "--report", str(report_path)],
            capsys,
            monkeypatch,
            tmp_vault,
            tmp_cortex_dir,
        )

        # On dry-run, profiles directory must NOT be populated
        if profiles_dir.exists():
            profile_files = list(profiles_dir.glob("profile_*.md"))
            assert len(profile_files) == 0, (
                "dry-run must not create profile files"
            )
