#!/usr/bin/env bash
# Tests for hooks/stop-mine.sh
#
# Test cases:
#   1. MEMEM_HOOK_DISABLE=1 → exit 0, valid JSON, no mine_delta spawn
#   2. Missing ~/.memem/.miner-opted-in → exit 0, valid JSON, no spawn
#   3. Normal opted-in path → exit 0, valid JSON with hookEventName=Stop
#   4. JSON validity: output passes python3 JSON parse + field assertion

set -euo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/stop-mine.sh"
PASS=0
FAIL=0

# Cleanup registry
CLEANUP_DIRS=()
cleanup() {
    for d in "${CLEANUP_DIRS[@]:-}"; do
        rm -rf "$d"
    done
}
trap cleanup EXIT

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

STUB_JSON='{"session_id":"test-session-001","transcript_path":"/dev/null"}'

# Create a stub python3 wrapper that:
#   - For "python3 -m memem.mine_delta ..." invocations: writes a sentinel file and exits 0 immediately.
#   - For all other invocations (e.g. the JSON parsing inline script): delegates to real python3.
# This prevents any long-lived background process from keeping pipes open during tests.
STUB_DIR=$(mktemp -d)
CLEANUP_DIRS+=("$STUB_DIR")
STUB_SENTINEL="$STUB_DIR/mine_delta_called"

cat > "$STUB_DIR/python3" << STUBEOF
#!/usr/bin/env bash
# Stub python3: intercept mine_delta invocations; delegate everything else.
args="\$*"
if echo "\$args" | grep -q "mine_delta"; then
    touch "$STUB_SENTINEL"
    exit 0
fi
exec python3 "\$@"
STUBEOF
chmod +x "$STUB_DIR/python3"

echo "=== test_stop_mine_hook.sh ==="
echo ""

# -----------------------------------------------------------------------
# Test 1: MEMEM_HOOK_DISABLE=1 → immediate exit 0, valid JSON, no spawn
# -----------------------------------------------------------------------
echo "Test 1: MEMEM_HOOK_DISABLE guard"
rm -f "$STUB_SENTINEL"

OUT1=$(echo "$STUB_JSON" | MEMEM_HOOK_DISABLE=1 PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
EC1=$?

if [ "$EC1" -eq 0 ]; then
    pass "exit code is 0"
else
    fail "exit code is $EC1, expected 0"
fi

if [ -z "$OUT1" ]; then
    pass "stdout is empty (Stop hook protocol: no JSON envelope)"
else
    fail "stdout should be empty for Stop hook, got: $OUT1"
fi

# MEMEM_HOOK_DISABLE should short-circuit before any spawn.
if [ ! -f "$STUB_SENTINEL" ]; then
    pass "mine_delta was NOT spawned (MEMEM_HOOK_DISABLE guard works)"
else
    fail "mine_delta was spawned despite MEMEM_HOOK_DISABLE=1"
fi

# -----------------------------------------------------------------------
# Test 2: Missing .miner-opted-in → exit 0, valid JSON, no spawn
# -----------------------------------------------------------------------
echo ""
echo "Test 2: Missing opted-in marker"
TEST_HOME2=$(mktemp -d)
CLEANUP_DIRS+=("$TEST_HOME2")
rm -f "$STUB_SENTINEL"

OUT2=$(echo "$STUB_JSON" | HOME="$TEST_HOME2" MEMEM_DIR="$TEST_HOME2/.memem" \
    PATH="$STUB_DIR:$PATH" MEMEM_PYTHON="$STUB_DIR/python3" bash "$HOOK" 2>/dev/null)
EC2=$?

if [ "$EC2" -eq 0 ]; then
    pass "exit code is 0"
else
    fail "exit code is $EC2, expected 0"
fi

if [ -z "$OUT2" ]; then
    pass "stdout is empty (Stop hook protocol: no JSON envelope)"
else
    fail "stdout should be empty for Stop hook, got: $OUT2"
fi

# No opted-in marker → no spawn should occur.
if [ ! -f "$STUB_SENTINEL" ]; then
    pass "mine_delta was NOT spawned (no opted-in marker)"
else
    fail "mine_delta was spawned despite missing opted-in marker"
fi

# -----------------------------------------------------------------------
# Test 3: Normal opted-in path → exit 0, valid JSON
# -----------------------------------------------------------------------
echo ""
echo "Test 3: Normal opted-in path"
TEST_HOME3=$(mktemp -d)
CLEANUP_DIRS+=("$TEST_HOME3")
mkdir -p "$TEST_HOME3/.memem"
touch "$TEST_HOME3/.memem/.miner-opted-in"
rm -f "$STUB_SENTINEL"

# Use MEMEM_PYTHON pointing to our stub so the spawned subprocess exits immediately
# and doesn't keep the output pipe open.
OUT3=$(echo "$STUB_JSON" | HOME="$TEST_HOME3" MEMEM_DIR="$TEST_HOME3/.memem" \
    MEMEM_PYTHON="$STUB_DIR/python3" bash "$HOOK" 2>/dev/null)
EC3=$?

if [ "$EC3" -eq 0 ]; then
    pass "exit code is 0"
else
    fail "exit code is $EC3, expected 0"
fi

if [ -z "$OUT3" ]; then
    pass "output is empty (Stop hook protocol)"
else
    fail "output should be empty for Stop hook, got: $OUT3"
fi

# Give the detached subprocess a moment to write the sentinel.
_wait=0
while [ ! -f "$STUB_SENTINEL" ] && [ "$_wait" -lt 20 ]; do
    sleep 0.1
    _wait=$((_wait + 1))
done

if [ -f "$STUB_SENTINEL" ]; then
    pass "mine_delta was spawned (sentinel file found)"
else
    fail "mine_delta was NOT spawned (sentinel file missing)"
fi

# -----------------------------------------------------------------------
# Test 4: stdout empty (Stop hook protocol)
# -----------------------------------------------------------------------
# Stop hooks must NOT emit a hookSpecificOutput envelope. Claude Code rejects
# the envelope shape with "Hook JSON output validation failed — (root): Invalid
# input". Only SessionStart accepts hookSpecificOutput. The hook itself just
# exits 0 silently; the mining work is detached background.
echo ""
echo "Test 4: stdout strictly empty"
TEST_HOME4=$(mktemp -d)
CLEANUP_DIRS+=("$TEST_HOME4")
mkdir -p "$TEST_HOME4/.memem"
touch "$TEST_HOME4/.memem/.miner-opted-in"

OUT4=$(echo "$STUB_JSON" | HOME="$TEST_HOME4" MEMEM_DIR="$TEST_HOME4/.memem" \
    MEMEM_PYTHON="$STUB_DIR/python3" bash "$HOOK" 2>/dev/null)

if [ -z "$OUT4" ]; then
    pass "Stop hook emits zero bytes on stdout"
else
    fail "Stop hook stdout should be empty, got: $OUT4"
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
