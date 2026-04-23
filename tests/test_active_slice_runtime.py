"""Runtime regression tests for active-slice hook behavior."""

import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_active_slice_response_empty_without_recall_candidates(tmp_vault, tmp_memem_dir):
    from memem import active_slice_engine
    importlib.reload(active_slice_engine)

    rendered = active_slice_engine.active_slice_response(
        "just a prompt with no memories",
        scope_id="memem",
        use_llm=False,
    )

    assert rendered == ""


def test_active_slice_response_emits_when_memory_matches(tmp_vault, tmp_memem_dir):
    from memem import obsidian_store
    importlib.reload(obsidian_store)

    mem = obsidian_store._make_memory(
        content="Portable hooks must route through bootstrap for public installs.",
        title="Portable hook runtime",
        project="memem",
        source_type="user",
        importance=5,
    )
    obsidian_store._save_memory(mem)

    from memem import active_slice_engine
    importlib.reload(active_slice_engine)
    rendered = active_slice_engine.active_slice_response(
        "portable hooks bootstrap",
        scope_id="memem",
        use_llm=False,
    )

    assert "# Active Memory Slice" in rendered
    assert "Portable hook runtime" in rendered


def test_active_slice_cli_reads_query_file_stdin(capsys, monkeypatch):
    from memem import active_slice_engine, cli
    importlib.reload(cli)

    captured = {}

    def fake_response(query, scope_id="default", environment=None, use_llm=True, raw_json=False):
        captured.update({
            "query": query,
            "scope_id": scope_id,
            "use_llm": use_llm,
            "raw_json": raw_json,
        })
        return "ok"

    monkeypatch.setattr(active_slice_engine, "active_slice_response", fake_response)
    monkeypatch.setattr(sys, "stdin", io.StringIO("large prompt body" * 1000))

    cli.dispatch_cli(
        ["memem", "active-slice", "--query-file", "-", "--scope", "memem", "--no-llm"],
        SimpleNamespace(run=lambda **_: None),
    )
    out = capsys.readouterr()

    assert out.out.strip() == "ok"
    assert captured["query"].startswith("large prompt body")
    assert captured["scope_id"] == "memem"
    assert captured["use_llm"] is False


def test_codex_hook_manifest_excludes_pretooluse():
    codex_hooks = json.loads((REPO_ROOT / "hooks" / "codex-hooks.json").read_text())
    claude_hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())

    assert "PreToolUse" not in codex_hooks["hooks"]
    assert "PreToolUse" in claude_hooks["hooks"]


def test_auto_recall_hook_handles_large_prompt_without_argv_overflow(tmp_path):
    state = tmp_path / ".memem"
    vault = tmp_path / "obsidian-brain"
    (vault / "memem" / "memories").mkdir(parents=True)
    (vault / "memem" / "playbooks").mkdir(parents=True)

    env = os.environ.copy()
    env.update({
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "MEMEM_DIR": str(state),
        "MEMEM_OBSIDIAN_VAULT": str(vault),
        "MEMEM_PYTHON": sys.executable,
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
    })
    hook_input = json.dumps({
        "session_id": "large-prompt",
        "cwd": str(REPO_ROOT),
        "message": "large prompt " * 30000,
    })

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "auto-recall.sh")],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert payload["hookSpecificOutput"]["additionalContext"] == ""
    assert "Argument list too long" not in result.stderr
