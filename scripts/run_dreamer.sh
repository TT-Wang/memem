#!/usr/bin/env bash
# Run the memem dreamer (offline consolidation pass).
# Default is dry-run; pass --apply to mutate.
#
# Suggested cron: nightly at 3am
#   0 3 * * * bash /path/to/memem/scripts/run_dreamer.sh
#
# Review the diff before applying:
#   bash run_dreamer.sh                # dry-run, writes ~/.memem/dreams/*.json
#   bash run_dreamer.sh --apply        # acts on the proposals

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$REPO_ROOT" python3 -m memem.cli --dream "$@"
