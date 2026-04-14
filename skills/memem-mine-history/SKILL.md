---
name: memem-mine-history
description: Mine ALL existing Claude Code sessions, including history from before memem was installed.
allowed-tools: [Bash]
---

Mine all historical Claude Code sessions for knowledge. This includes sessions from before memem was installed — by default only new sessions are mined.

1. Show how many sessions exist:
```bash
find ~/.claude/projects/ -name "*.jsonl" ! -path "*/subagents/*" -size +5k | wc -l
```

2. Tell the user approximately how long it will take (~3-5 seconds per session).

3. Confirm with the user before starting — this can take hours for large histories and makes many API calls.

4. Start the mining in the background:
```bash
nohup PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --mine-all > /dev/null 2>&1 &
echo "History mining started in background (PID $!)"
```

5. Tell the user:
- History mining is running in the background
- They can continue working normally
- New memories will appear as they're extracted
- Run `/memem-status` to check progress
- The miner daemon continues mining new sessions in parallel
