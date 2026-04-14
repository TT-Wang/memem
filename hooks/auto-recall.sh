#!/usr/bin/env bash
# memem auto-recall hook — injects memory index into context.
# Fires on UserPromptSubmit. Only runs once per session (first message).
#
# Flow: inject _index.md → assistant reads it → calls memory tools if needed

set -euo pipefail

MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
SESSION_MARKER_DIR="$MEMEM_DIR/.sessions"
mkdir -p "$SESSION_MARKER_DIR"

# Read hook input from stdin
INPUT=$(cat)

# Extract session_id
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Only run on first message of session
MARKER="$SESSION_MARKER_DIR/$SESSION_ID"
if [ -n "$SESSION_ID" ] && [ -f "$MARKER" ]; then
  exit 0
fi

# Clean up old session markers (older than 7 days)
find "$SESSION_MARKER_DIR" -type f -mtime +7 -delete 2>/dev/null || true

# Inject the memory index directly — let the assistant decide what to read deeper
VAULT="${MEMEM_OBSIDIAN_VAULT:-${CORTEX_OBSIDIAN_VAULT:-$HOME/obsidian-brain}}"
# Prefer new layout (~/obsidian-brain/memem/) but fall back to legacy (~/.../cortex/)
if [ -f "$VAULT/memem/_index.md" ]; then
  INDEX="$VAULT/memem/_index.md"
  VAULT_SUBDIR="memem"
elif [ -f "$VAULT/cortex/_index.md" ]; then
  INDEX="$VAULT/cortex/_index.md"
  VAULT_SUBDIR="cortex"
else
  INDEX="$VAULT/memem/_index.md"
  VAULT_SUBDIR="memem"
fi

if [ -f "$INDEX" ] || [ -d "$VAULT/$VAULT_SUBDIR/memories" ]; then
  # Write INPUT to temp file to avoid argv size limits on large prompts
  INPUT_FILE=$(mktemp)
  trap 'rm -f "$INPUT_FILE"' EXIT
  echo "$INPUT" > "$INPUT_FILE"

  python3 - "$INDEX" "$VAULT" "$INPUT_FILE" "$VAULT_SUBDIR" "$MEMEM_DIR" << 'HOOKPY'
import sys, json, subprocess, os
from pathlib import Path

index_path = sys.argv[1]
vault = sys.argv[2]
input_file = sys.argv[3] if len(sys.argv) > 3 else ""
vault_subdir = sys.argv[4] if len(sys.argv) > 4 else "memem"
memem_dir = sys.argv[5] if len(sys.argv) > 5 else os.path.expanduser("~/.memem")

# Read hook input from temp file
input_data = ""
if input_file:
    try:
        input_data = Path(input_file).read_text()
    except OSError:
        pass

# Resolve plugin root for PYTHONPATH (package runs via `python -m memem.server`)
plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
if not plugin_root:
    # No sensible fallback — without plugin root we cannot locate the package.
    # Surface it on stderr instead of silently guessing the wrong directory.
    print("memem auto-recall: CLAUDE_PLUGIN_ROOT not set, skipping assembly", file=sys.stderr)
    plugin_root = None

# Extract user message from hook input
message = ""
try:
    hook_input = json.loads(input_data) if input_data else {}
    message = hook_input.get("message", hook_input.get("query", ""))
except (json.JSONDecodeError, TypeError):
    pass

# Check if memories exist before calling expensive assembly
memories_dir = Path(vault) / vault_subdir / "memories"
memory_count = len(list(memories_dir.glob("*.md"))) if memories_dir.exists() else 0

# Try context assembly if we have a message AND memories exist AND we know where the package lives
assembled = ""
if message and memory_count > 0 and plugin_root:
    try:
        sub_env = os.environ.copy()
        sub_env["PYTHONPATH"] = plugin_root + os.pathsep + sub_env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "memem.server", "--assemble-context", message, "default"],
            capture_output=True, text=True, timeout=30, env=sub_env,
        )
        if result.returncode == 0 and result.stdout.strip():
            assembled = result.stdout.strip()
        elif result.returncode != 0:
            print(f"memem auto-recall: assemble-context failed (rc={result.returncode}): {result.stderr.strip()[:200]}", file=sys.stderr)
    except Exception as exc:
        print(f"memem auto-recall: assemble-context errored: {exc}", file=sys.stderr)

# Build a one-line status banner from <state>/.capabilities (written by
# bootstrap.sh and the --doctor command). Silent no-op if the file is missing
# or the schema is older than we expect. Reads the new path first, then legacy.
def _build_status_banner(memories_dir_path):
    try:
        import json as _json
        caps_path = Path(memem_dir) / ".capabilities"
        if not caps_path.exists():
            legacy = Path.home() / ".cortex" / ".capabilities"
            if legacy.exists():
                caps_path = legacy
            else:
                return None
        caps = _json.loads(caps_path.read_text())
        if not isinstance(caps, dict) or caps.get("schema_version", 0) < 1:
            return None
        count = 0
        if memories_dir_path and Path(memories_dir_path).exists():
            count = len(list(Path(memories_dir_path).glob("*.md")))

        # Check miner state via pidfile (avoid shelling out)
        miner_ok = False
        for pid_path in (Path(memem_dir) / "miner.pid", Path.home() / ".cortex" / "miner.pid"):
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text().strip())
                    os.kill(pid, 0)
                    miner_ok = True
                    break
                except (OSError, ValueError):
                    continue

        miner_glyph = "OK" if miner_ok else "DOWN"
        assembly_glyph = "OK" if caps.get("claude_cli") else "degraded"
        parts = [f"[memem] {count} memories", f"miner {miner_glyph}", f"assembly {assembly_glyph}"]
        if not caps.get("claude_cli", True):
            parts.append("(claude CLI missing — FTS-only recall)")
        if not caps.get("writable_vault", True):
            parts.append("(vault read-only!)")
        return " · ".join(parts)
    except Exception:
        return None


if assembled:
    # Assembly succeeded — inject the tailored brief
    suffix = (
        "Above is a query-tailored context briefing assembled by memem. "
        "For deeper recall, use the memem MCP tools (memory_recall, memory_list, "
        "context_assemble). When saving memories, ALWAYS dual-write: save to "
        "memem (via memory_save) AND to Claude Code's built-in auto memory system."
    )
    banner = _build_status_banner(memories_dir)
    banner_prefix = (banner + "\n\n") if banner else ""
    output = banner_prefix + "memem context briefing:\n\n" + assembled + "\n\n---\n" + suffix
else:
    # Fallback — check if memories exist at all
    index = Path(index_path).read_text() if Path(index_path).exists() else ""

    if memory_count == 0:
        # Quiet onboarding: do NOT inject a welcome wall on the user's first
        # prompt. Users discover memem via /memem when they want it; this hook
        # stays silent so the first question is answered without interruption.
        sys.exit(0)
    else:
        # Has memories but assembly failed — dump index + playbook
        playbook_dir = Path(vault) / vault_subdir / "playbooks"
        playbook_text = ""
        if playbook_dir.exists():
            for pb_file in sorted(playbook_dir.glob("*.md")):
                content = pb_file.read_text().strip()
                if content:
                    lines = content.split("\n")
                    if lines and (lines[-1].strip().startswith("<!-- cortex-hash:")
                                  or lines[-1].strip().startswith("<!-- memem-hash:")
                                  or lines[-1].strip().startswith("<!-- refined:")):
                        content = "\n".join(lines[:-1]).strip()
                    project_name = pb_file.stem
                    playbook_text += f"\n## Project Playbook: {project_name}\n\n{content}\n\n"

        suffix = (
            "Above is your memory index. Use the memem MCP tools (memory_recall, "
            "memory_list, context_assemble) for deeper recall. "
            "Save lessons with memory_save as you work."
        )

        parts = []
        if playbook_text:
            parts.append("memem project playbooks (curated knowledge):\n" + playbook_text + "---\n")
        parts.append("memem memory index (" + str(memory_count) + " memories):\n\n" + index + "\n\n---\n" + suffix)
        output = "\n".join(parts)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": output
    }
}))
HOOKPY

  # Mark session as recalled AFTER successful execution
  if [ -n "$SESSION_ID" ]; then
    touch "$MARKER"
  fi
fi
