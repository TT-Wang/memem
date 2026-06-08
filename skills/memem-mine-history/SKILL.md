---
name: memem-mine-history
description: Mine ALL existing Claude Code sessions, including history from before memem was installed.
allowed-tools: [Bash]
---

Mine all historical Claude Code sessions for knowledge. By default the Stop hook only handles new sessions; this command backfills existing JSONL transcripts. It also opts you in to event-triggered mining for future sessions.

1. Show how many sessions exist:
```bash
find ~/.claude/projects/ -name "*.jsonl" ! -path "*/subagents/*" -size +5k | wc -l
```

2. Tell the user approximately how long it will take (~3-5 seconds per session, plus Haiku API costs).

3. Confirm with the user before starting — this can take hours for large histories and makes many Haiku API calls.

4. Record the opt-in and start history backfill in the background:
```bash
mkdir -p ~/.memem && touch ~/.memem/.miner-opted-in
nohup PYTHONPATH="${CLAUDE_PLUGIN_ROOT}" python3 -m memem.server --mine-all > ~/.memem/mine-all.log 2>&1 &
echo "History mining started in background (PID $!)"
```

5. Tell the user:
- History mining is running in the background (log at `~/.memem/mine-all.log`)
- Future sessions will also be mined automatically — every Claude Code Stop event spawns `mine_delta` to extract new turns
- They can continue working normally
- New memories appear as they're extracted (Haiku-extracted, written to `~/obsidian-brain/memem/memories/`)
- Run `/memem-status` to check progress and Stop-hook registration
- To opt out: `rm ~/.memem/.miner-opted-in` (no daemon to stop)
