---
name: memem-mine-history
description: Mine ALL existing Claude Code sessions, including history from before memem was installed.
allowed-tools: [Bash]
---

Mine all historical Claude Code sessions for knowledge. This includes sessions from before memem was installed — by default only new sessions are mined. This also opts the user into the ongoing miner daemon, so new sessions are mined automatically going forward.

1. Show how many sessions exist:
```bash
find ~/.claude/projects/ -name "*.jsonl" ! -path "*/subagents/*" -size +5k | wc -l
```

2. Tell the user approximately how long it will take (~3-5 seconds per session, plus Haiku API costs).

3. Confirm with the user before starting — this can take hours for large histories and makes many API calls.

4. Record the opt-in, start mining history in the background, and start the ongoing miner daemon:
```bash
mkdir -p "${MEMEM_DIR:-$HOME/.memem}"
nohup bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --mine-all > "${MEMEM_DIR:-$HOME/.memem}/mine-all.log" 2>&1 &
echo "History mining started in background (PID $!)"
bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --miner-start
```

5. Tell the user:
- History mining is running in the background (log at `${MEMEM_DIR:-$HOME/.memem}/mine-all.log`)
- The ongoing miner daemon is now running — new sessions will be mined automatically too
- They can continue working normally
- New memories will appear as they're extracted
- Run `/memem-status` to check progress
- To stop everything: `bash "${CLAUDE_PLUGIN_ROOT}/bootstrap.sh" --miner-opt-out`
