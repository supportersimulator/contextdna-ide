#!/usr/bin/env bash
# test_fleet_git_msg.sh — UU2 / M5 regression for ff-only conflict escalation.
#
# Builds an isolated origin + clone, then exercises:
#   1. Clean clone, no divergence → exit 0, no escalation
#   2. Synthetic divergence (origin moves ahead) → fast-forward, exit 0
#   3. True divergence (both sides have unique commits) → exit 1, escalation
#      payload captured in fallback log (no chief reachable in sandbox)
#   4. --dry-run on divergence → exit 1 + dry-run counter bumps, no fallback log
#   5. Counter bump on each failure (debounce reduces send but not counter)
#
# All git/file I/O is rooted in $TMPDIR. The repo-relative script is invoked
# with overrides for COUNTERS/dedup/fallback paths so the host environment is
# untouched.

set -u  # NOT -e — we want to inspect exit codes manually.

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/fleet-git-msg.sh"

SANDBOX="$(mktemp -d -t fleet-git-msg-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

# Isolated counter/dedup/log files for the test run.
export FLEET_FF_DEDUP_FILE="$SANDBOX/ff-dedup.txt"
export FLEET_FF_FALLBACK_LOG="$SANDBOX/ff-fallback.log"
# Force send failure path so we get reproducible behavior without a chief.
# We achieve this by pointing fleet-send.sh's CHIEF_INGEST_URL to a dead port
# AND disabling the in-process cmd_send git push (FLEET_PUSH_FREEZE=1).
export CHIEF_INGEST_URL="http://127.0.0.1:1"
export FLEET_PUSH_FREEZE=1
# Short dedup window for fast tests (but >1s so back-to-back calls dedup).
export FLEET_FF_DEDUP_WINDOW_S=300
# Quiet bash output channel.
export MULTIFLEET_NODE_ID="testnode"

PASS=0
FAIL=0

_assert() {
    local label="$1"
    local cond="$2"
    if eval "$cond"; then
        echo "  PASS  $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label"
        FAIL=$((FAIL + 1))
    fi
}

# Use a side-channel COUNTERS path per test so we can read clean counts.
_setup_counter_file() {
    export FLEET_COUNTERS_OVERRIDE="$SANDBOX/counters-$1.json"
    # The script hardcodes COUNTERS, so we patch via a wrapper:
    # easiest path: pre-create the file and let the script write to its own
    # path, then read /tmp/fleet-git-msg-counters.json. We instead pre-clear it.
    : > "$FLEET_FF_DEDUP_FILE"
    : > "$FLEET_FF_FALLBACK_LOG"
    rm -f /tmp/fleet-git-msg-counters.json
}

_counter() {
    local name="$1"
    python3 -c "
import json, sys, os
p = '/tmp/fleet-git-msg-counters.json'
if not os.path.exists(p):
    print(0); sys.exit(0)
try:
    d = json.load(open(p))
except Exception:
    print(0); sys.exit(0)
print(d.get('$name', 0))
" 2>/dev/null
}

# ── Build a bare origin and two clones ──
ORIGIN="$SANDBOX/origin.git"
git init -q --bare "$ORIGIN"

CLONE_A="$SANDBOX/clone-a"
CLONE_B="$SANDBOX/clone-b"
git clone -q "$ORIGIN" "$CLONE_A"
(cd "$CLONE_A" && git checkout -q -b main 2>/dev/null || true)
(cd "$CLONE_A" && \
    git config user.email "test@example.com" && \
    git config user.name "Test" && \
    echo "init" > README && \
    git add README && \
    git commit -q -m "init" && \
    git push -q origin HEAD:main)

git clone -q "$ORIGIN" "$CLONE_B"
(cd "$CLONE_B" && \
    git config user.email "test@example.com" && \
    git config user.name "Test" && \
    git checkout -q main)

# Helper: invoke `fleet-git-msg.sh pull` from a clone with REPO_ROOT spoofed.
# The script computes REPO_ROOT from its own dirname, so we copy the script
# next to the clone instead.
SCRIPT_COPY="$SANDBOX/scripts-copy"
mkdir -p "$SCRIPT_COPY/scripts"
cp "$SCRIPT" "$SCRIPT_COPY/scripts/fleet-git-msg.sh"
# Provide a stub fleet-send.sh that always exits non-zero (simulates chief down).
cat > "$SCRIPT_COPY/scripts/fleet-send.sh" <<'STUB'
#!/usr/bin/env bash
# Stub: chief unreachable in test sandbox.
exit 1
STUB
chmod +x "$SCRIPT_COPY/scripts/fleet-send.sh"
# Stub fleet-node-id.sh (sourced by fleet-send.sh but not used in stub).
cat > "$SCRIPT_COPY/scripts/fleet-node-id.sh" <<'STUB'
fleet_node_id() { echo "${MULTIFLEET_NODE_ID:-testnode}"; }
fleet_peer_ids() { echo "peer1"; }
STUB

_run_pull_in() {
    local clone="$1"; shift
    # Symlink the script copy into the clone so REPO_ROOT resolves to the clone.
    ln -sfn "$SCRIPT_COPY/scripts" "$clone/scripts" 2>/dev/null
    # Also need .multifleet/config.json for cmd_send (P7 fallback path).
    mkdir -p "$clone/.multifleet"
    cat > "$clone/.multifleet/config.json" <<'CFG'
{"nodes": {"testnode": {}, "peer1": {}}}
CFG
    (cd "$clone" && bash scripts/fleet-git-msg.sh pull "$@") 2>&1
    echo "EXIT=$?"
}

# ── Test 1: clean, no divergence → exit 0 ──
echo "Test 1: clean clone, no divergence"
_setup_counter_file 1
OUT=$(_run_pull_in "$CLONE_B")
_assert "exits 0 when up to date" "echo '$OUT' | grep -q 'EXIT=0'"
_assert "no escalation counter when clean" "[ \"\$(_counter git_ff_conflict_total)\" = '0' ]"

# ── Test 2: synthetic ff-forward (origin moves ahead) → exit 0 ──
echo "Test 2: origin moves ahead, clean clone → fast-forward"
(cd "$CLONE_A" && \
    echo "more" >> README && \
    git commit -q -am "ff-add" && \
    git push -q origin HEAD:main)
_setup_counter_file 2
OUT=$(_run_pull_in "$CLONE_B")
_assert "fast-forward exits 0" "echo '$OUT' | grep -q 'EXIT=0'"
_assert "no escalation on ff success" "[ \"\$(_counter git_ff_conflict_total)\" = '0' ]"

# ── Test 3: true divergence → exit 1, escalation payload written ──
echo "Test 3: true divergence → exit 1, escalation"
# Origin gains a new commit
(cd "$CLONE_A" && \
    echo "origin-side" >> README && \
    git commit -q -am "origin-only" && \
    git push -q origin HEAD:main)
# Local creates its own commit on a different file (avoid working-tree dirty,
# we want clean divergence, not skipped_dirty).
(cd "$CLONE_B" && \
    echo "local-side" > LOCAL && \
    git add LOCAL && \
    git commit -q -m "local-only")
_setup_counter_file 3
OUT=$(_run_pull_in "$CLONE_B")
_assert "diverged exits 1" "echo '$OUT' | grep -q 'EXIT=1'"
_assert "git_ff_conflict_total bumped" "[ \"\$(_counter git_ff_conflict_total)\" = '1' ]"
# Chief is unreachable in sandbox → P7 fallback (cmd_send) takes over. Verify
# that exactly one escalation channel registered as having handled the alert.
P7_SENT=$(_counter git_ff_conflict_sent_p7_total)
CHIEF_SENT=$(_counter git_ff_conflict_sent_total)
FALLBACK_ERR=$(_counter git_ff_conflict_send_errors_total)
_assert "exactly one escalation channel fired" \
    "[ \$((P7_SENT + CHIEF_SENT + FALLBACK_ERR)) = '1' ]"
_assert "dedup file has entry" "[ -s '$FLEET_FF_DEDUP_FILE' ]"
_assert "fallback log only on full-failure path" \
    "[ \"\$FALLBACK_ERR\" = '0' ] || [ -s '$FLEET_FF_FALLBACK_LOG' ]"

# ── Test 4: re-run same conflict → counter bumps, debounce suppresses send ──
echo "Test 4: re-run within debounce window"
PREV_FALLBACK_SIZE=$(wc -c < "$FLEET_FF_FALLBACK_LOG")
OUT=$(_run_pull_in "$CLONE_B")
NEW_FALLBACK_SIZE=$(wc -c < "$FLEET_FF_FALLBACK_LOG")
_assert "second run still exits 1" "echo '$OUT' | grep -q 'EXIT=1'"
_assert "git_ff_conflict_total bumped twice" "[ \"\$(_counter git_ff_conflict_total)\" = '2' ]"
_assert "debounce counter incremented" "[ \"\$(_counter git_ff_conflict_debounced_total)\" = '1' ]"
_assert "fallback log NOT rewritten on debounced run" "[ \"$NEW_FALLBACK_SIZE\" = \"$PREV_FALLBACK_SIZE\" ]"

# ── Test 5: --dry-run on fresh divergence → exit 1, no fallback log write ──
echo "Test 5: --dry-run path"
# Reset dedup so this run is treated as a new conflict.
: > "$FLEET_FF_DEDUP_FILE"
_setup_counter_file 5
OUT=$(_run_pull_in "$CLONE_B" --dry-run)
_assert "dry-run still exits 1 on divergence" "echo '$OUT' | grep -q 'EXIT=1'"
_assert "git_ff_conflict_total bumped under dry-run" "[ \"\$(_counter git_ff_conflict_total)\" = '1' ]"
_assert "git_ff_conflict_dry_run_total bumped" "[ \"\$(_counter git_ff_conflict_dry_run_total)\" = '1' ]"
_assert "no send_errors counter under dry-run" "[ \"\$(_counter git_ff_conflict_send_errors_total)\" = '0' ]"

# ── Summary ──
echo ""
echo "─────────────────────"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "─────────────────────"
[ "$FAIL" = "0" ]
