#!/usr/bin/env bash
# TRANSITIONAL NO-OP STUB (v2.6.1) — safe to delete in a future major release.
#
# The real post-stop-attribution hook was removed in v2.5.0 (it was a
# guaranteed no-op: it imported two modules deleted in v2.0.0 and read a file
# nothing writes). Its hooks.json Stop registration was removed at the same
# time.
#
# However, Claude Code snapshots a plugin's hook registry at session start.
# Any session that was already running when the upgrade landed still tries to
# execute this path on every Stop event and logs
#   "Stop hook error: ... post-stop-attribution.sh: No such file or directory"
# until /reload-plugins or a session restart. This stub exists solely to keep
# those stale registrations silent during the transition.
#
# Stop hooks must exit 0 with NO stdout (the Stop protocol rejects
# hookSpecificOutput envelopes).
exit 0
