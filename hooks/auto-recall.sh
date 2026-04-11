#!/usr/bin/env bash
# Cortex auto-recall hook — injects memory index into context.
# Fires on UserPromptSubmit. Only runs once per session (first message).
#
# Flow: inject _index.md → Opus reads it → calls memory tools if needed

set -euo pipefail

CORTEX_DIR="$HOME/.cortex"
SESSION_MARKER_DIR="$CORTEX_DIR/.sessions"
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

# Inject the memory index directly — let Opus decide what to read deeper
VAULT="${CORTEX_OBSIDIAN_VAULT:-$HOME/obsidian-brain}"
INDEX="$VAULT/cortex/_index.md"
if [ -f "$INDEX" ]; then
  python3 - "$INDEX" "$VAULT" "$INPUT" << 'HOOKPY'
import sys, json, subprocess, os
from pathlib import Path

index_path = sys.argv[1]
vault = sys.argv[2]
input_data = sys.argv[3] if len(sys.argv) > 3 else ""

# Resolve server.py path
plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
if plugin_root:
    server_path = str(Path(plugin_root) / "cortex-mcp-server" / "server.py")
else:
    server_path = str(Path(index_path).resolve().parent.parent / "cortex-mcp-server" / "server.py")

# Extract user message from hook input
message = ""
try:
    hook_input = json.loads(input_data) if input_data else {}
    message = hook_input.get("message", hook_input.get("query", ""))
except (json.JSONDecodeError, TypeError):
    pass

# Try context assembly if we have a message
assembled = ""
if message:
    try:
        result = subprocess.run(
            [sys.executable, server_path, "--assemble-context", message, "default"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            assembled = result.stdout.strip()
    except Exception:
        pass

if assembled:
    # Assembly succeeded — inject the tailored brief
    suffix = (
        "Above is a query-tailored context briefing assembled by Cortex. "
        "For deeper recall, use Cortex MCP tools (memory_recall, memory_list, "
        "context_assemble). When saving memories, ALWAYS dual-write: save to "
        "Cortex (via memory_save) AND to Claude Code's built-in auto memory system."
    )
    output = "Cortex context briefing:\n\n" + assembled + "\n\n---\n" + suffix
else:
    # Fallback — dump index + playbook (original behavior)
    index = Path(index_path).read_text()

    playbook_dir = Path(vault) / "cortex" / "playbooks"
    playbook_text = ""
    if playbook_dir.exists():
        for pb_file in sorted(playbook_dir.glob("*.md")):
            content = pb_file.read_text().strip()
            if content:
                # Strip the hash comment at the end
                lines = content.split("\n")
                if lines and lines[-1].strip().startswith("<!-- cortex-hash:"):
                    content = "\n".join(lines[:-1]).strip()
                project_name = pb_file.stem
                playbook_text += f"\n## Project Playbook: {project_name}\n\n{content}\n\n"

    suffix = (
        "Above is your memory index. IMPORTANT: Do not just skim the titles — "
        "actively use the Cortex MCP tools (memory_recall, memory_list, context_assemble) "
        "to fetch full content of memories relevant to the user's request. "
        "When saving memories, ALWAYS dual-write: save to Cortex (via memory_save) AND "
        "to Claude Code's built-in auto memory system."
    )

    parts = []
    if playbook_text:
        parts.append("Cortex project playbooks (curated knowledge):\n" + playbook_text + "---\n")
    parts.append("Cortex memory index (your full memory catalog):\n\n" + index + "\n\n---\n" + suffix)
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
