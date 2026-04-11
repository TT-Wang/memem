---
name: cortex-mine
description: Mine existing Claude Code sessions for knowledge. Extracts durable insights from past conversations.
allowed-tools: [Bash]
---

Mine all existing Claude Code sessions for knowledge. This extracts durable insights (decisions, preferences, conventions, lessons) from your past conversations.

1. First show how many sessions are available:
```bash
find ~/.claude/projects/ -name "*.jsonl" ! -path "*/subagents/*" -size +5k | wc -l
```

2. Tell the user approximately how long it will take (~3-5 seconds per session).

3. Start the mining in the background:
```bash
nohup python3 "${CLAUDE_PLUGIN_ROOT}/cortex-mcp-server/server.py" --mine-all > /dev/null 2>&1 &
echo "Mining started in background (PID $!)"
```

4. Tell the user:
- Mining is running in the background
- They can continue working normally
- New memories will appear in Obsidian as they're extracted
- Run `/cortex-status` to check progress
