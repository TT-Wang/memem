#!/usr/bin/env bash
# fix_permissions_audit.sh — Audit .git/ and vault/memem/memories/ for non-claude-user owned files.
#
# Usage:
#   ./scripts/fix_permissions_audit.sh
#
# Exit codes:
#   0 — all files owned by claude-user (clean)
#   1 — one or more non-claude-user-owned files remain
#
# This script cannot elevate privileges itself. If non-claude-user files are
# found, it prints the sudo commands needed and exits 1.

set -euo pipefail

GIT_DIR="/home/claude-user/cortex-plugin/.git"
VAULT_DIR="/home/claude-user/obsidian-brain/memem/memories"

echo "=== fix_permissions_audit.sh ==="
echo "Auditing: $GIT_DIR"
echo "Auditing: $VAULT_DIR"
echo ""

# ---- audit both trees ----
git_bad=$(find "$GIT_DIR" -not -user claude-user 2>/dev/null || true)
vault_bad=$(find "$VAULT_DIR" -not -user claude-user 2>/dev/null || true)

found_problems=0

if [ -n "$git_bad" ]; then
    echo "FAIL: Non-claude-user files found in $GIT_DIR:"
    echo "$git_bad"
    echo ""
    echo "Fix with:"
    echo "  sudo chown -R claude-user:claude-user $GIT_DIR"
    found_problems=1
else
    echo "OK: $GIT_DIR — fully owned by claude-user"
fi

if [ -n "$vault_bad" ]; then
    echo "FAIL: Non-claude-user files found in $VAULT_DIR:"
    echo "$vault_bad"
    echo ""
    echo "Fix with:"
    echo "  sudo chown -R claude-user:claude-user $VAULT_DIR"
    found_problems=1
else
    echo "OK: $VAULT_DIR — fully owned by claude-user"
fi

echo ""
if [ "$found_problems" -eq 0 ]; then
    echo "=== AUDIT PASSED: all files owned by claude-user ==="
    exit 0
else
    echo "=== AUDIT FAILED: run the sudo command(s) above, then re-run this script ==="
    exit 1
fi
