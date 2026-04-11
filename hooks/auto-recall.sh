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
  # Write INPUT to temp file to avoid argv size limits on large prompts
  INPUT_FILE=$(mktemp)
  trap 'rm -f "$INPUT_FILE"' EXIT
  echo "$INPUT" > "$INPUT_FILE"

  python3 - "$INDEX" "$VAULT" "$INPUT_FILE" << 'HOOKPY'
import sys, json, subprocess, os
from pathlib import Path

index_path = sys.argv[1]
vault = sys.argv[2]
input_file = sys.argv[3] if len(sys.argv) > 3 else ""

# Read hook input from temp file
input_data = ""
if input_file:
    try:
        input_data = Path(input_file).read_text()
    except OSError:
        pass

# Resolve server.py path
plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
if plugin_root:
    server_path = str(Path(plugin_root) / "cortex-mcp-server" / "server.py")
else:
    server_path = str(Path(index_path).resolve().parent / "cortex-mcp-server" / "server.py")

# Extract user message from hook input
message = ""
try:
    hook_input = json.loads(input_data) if input_data else {}
    message = hook_input.get("message", hook_input.get("query", ""))
except (json.JSONDecodeError, TypeError):
    pass

# Check if memories exist before calling expensive assembly
memories_dir = Path(vault) / "cortex" / "memories"
memory_count = len(list(memories_dir.glob("*.md"))) if memories_dir.exists() else 0

# Try context assembly if we have a message AND memories exist
assembled = ""
if message and memory_count > 0:
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
    # Fallback — check if memories exist at all
    index = Path(index_path).read_text() if Path(index_path).exists() else ""
    memories_dir = Path(vault) / "cortex" / "memories"
    memory_count = len(list(memories_dir.glob("*.md"))) if memories_dir.exists() else 0

    if memory_count == 0:
        # Count existing session logs for the mine-existing option
        import glob as _glob
        sessions_dir = Path.home() / ".claude" / "projects"
        existing_sessions = 0
        if sessions_dir.exists():
            existing_sessions = len(_glob.glob(str(sessions_dir / "**" / "*.jsonl"), recursive=True))

        # Brand new user — welcome + onboarding
        mine_option = ""
        if existing_sessions > 0:
            mine_option = (
                f"\n**Quick start — mine your existing sessions:**\n"
                f"You have {existing_sessions} past Claude Code sessions. "
                "Cortex can extract knowledge from them to give you a head start. "
                "Ask me: **\"mine my existing sessions\"** and I'll run "
                "the miner to extract durable knowledge from your history. "
                "This runs in the background and may take a while for large histories.\n"
            )

        output = (
            "```\n"
            "  ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗\n"
            " ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝\n"
            " ██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝ \n"
            " ██║     ██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗ \n"
            " ╚██████╗╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗\n"
            "  ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝\n"
            "  persistent memory for AI\n"
            "```\n\n"
            "Welcome to Cortex! This is your first session with persistent memory enabled.\n\n"
            "**How it works:**\n"
            "- Every session you have builds your memory. Decisions, preferences, lessons, "
            "and conventions are automatically extracted and stored.\n"
            "- Future sessions start with relevant context pre-loaded — no more repeating yourself.\n"
            "- The more you work, the smarter it gets.\n\n"
            + mine_option +
            "\n**Getting started:**\n"
            "1. Just work normally — Cortex runs in the background\n"
            "2. To save something important now: use `memory_save`\n"
            "3. To search past knowledge: use `memory_recall`\n"
            "4. To get a tailored briefing: use `context_assemble`\n\n"
            "**What happens next:**\n"
            "- After this session ends, the miner daemon extracts durable knowledge from your conversation\n"
            "- Next session, you'll see a context briefing tailored to your first message\n"
            "- Over time, playbooks build per project — curated, self-evolving knowledge\n\n"
            "**Browse your memories with Obsidian (optional):**\n"
            "- Download Obsidian: https://obsidian.md (free)\n"
            "- Open `~/obsidian-brain` as a vault\n"
            "- Memories appear in `cortex/memories/`, playbooks in `cortex/playbooks/`\n"
            "- Use Graph View to see how memories link to each other\n\n"
            "Available tools: `memory_save`, `memory_recall`, `memory_list`, "
            "`memory_import`, `transcript_search`, `context_assemble`"
        )
    else:
        # Has memories but assembly failed — dump index + playbook
        playbook_dir = Path(vault) / "cortex" / "playbooks"
        playbook_text = ""
        if playbook_dir.exists():
            for pb_file in sorted(playbook_dir.glob("*.md")):
                content = pb_file.read_text().strip()
                if content:
                    lines = content.split("\n")
                    if lines and (lines[-1].strip().startswith("<!-- cortex-hash:") or lines[-1].strip().startswith("<!-- refined:")):
                        content = "\n".join(lines[:-1]).strip()
                    project_name = pb_file.stem
                    playbook_text += f"\n## Project Playbook: {project_name}\n\n{content}\n\n"

        suffix = (
            "Above is your memory index. Use Cortex MCP tools (memory_recall, "
            "memory_list, context_assemble) for deeper recall. "
            "Save lessons with memory_save as you work."
        )

        parts = []
        if playbook_text:
            parts.append("Cortex project playbooks (curated knowledge):\n" + playbook_text + "---\n")
        parts.append("Cortex memory index (" + str(memory_count) + " memories):\n\n" + index + "\n\n---\n" + suffix)
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
