---
name: memem-mine
description: Opt in to memem's event-triggered mining. New sessions are mined automatically via the Stop hook.
allowed-tools: [Bash]
---

Opt in to memem mining. In v2.1.0+ mining is event-triggered: it fires on every Claude Code Stop event when the opt-in marker exists. There is no daemon to start.

1. Create the opt-in marker:
```bash
mkdir -p ~/.memem && touch ~/.memem/.miner-opted-in
```

2. Tell the user:
- Mining is now active — every Stop event spawns a detached `mine_delta` subprocess that extracts memories from new turns since the last invocation
- Hook overhead is ~50ms; the Haiku call runs in background
- Run `/memem-status` to check Stop-hook registration, opt-in marker, and last mine run
- Safety net: SessionStart fires a stale-session sweep for any JSONLs > 10 min old not yet in `~/.memem/.mined_sessions` (catches sessions where Stop never fired)
- To opt out: `rm ~/.memem/.miner-opted-in` (no daemon to stop — the hook just no-ops)

For backfilling pre-existing sessions, use `/memem-mine-history`.
