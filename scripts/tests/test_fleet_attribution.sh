#!/usr/bin/env bash
# test_fleet_attribution.sh — verify fleet-commit.sh trailer injection
# and fleet-attribution-audit.sh tally logic.
#
# Runs in a throwaway git repo under $TMPDIR so the host history is
# untouched. Prints PASS/FAIL per assertion and exits non-zero on any
# failure.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO_ROOT" ] || { echo "must run from inside the superrepo"; exit 2; }

FLEET_COMMIT="$REPO_ROOT/scripts/fleet-commit.sh"
ATTR_AUDIT="$REPO_ROOT/scripts/fleet-attribution-audit.sh"
PREPARE_HOOK="$REPO_ROOT/scripts/git-hooks/prepare-commit-msg-fleet.sh"

[ -x "$FLEET_COMMIT" ] || chmod +x "$FLEET_COMMIT"
[ -x "$ATTR_AUDIT" ]   || chmod +x "$ATTR_AUDIT"
[ -x "$PREPARE_HOOK" ] || chmod +x "$PREPARE_HOOK"

TESTDIR="$(mktemp -d -t fleet-attr-test.XXXXXX)"
trap 'rm -rf "$TESTDIR"' EXIT

PASS=0
FAIL=0
assert() {
    local label="$1" expect="$2" got="$3"
    if [ "$expect" = "$got" ]; then
        printf 'PASS  %s\n' "$label"
        PASS=$((PASS+1))
    else
        printf 'FAIL  %s\n      want: %s\n      got:  %s\n' "$label" "$expect" "$got"
        FAIL=$((FAIL+1))
    fi
}
assert_contains() {
    local label="$1" needle="$2" hay="$3"
    if printf '%s' "$hay" | grep -Fq "$needle"; then
        printf 'PASS  %s\n' "$label"
        PASS=$((PASS+1))
    else
        printf 'FAIL  %s\n      missing: %s\n      in: %s\n' "$label" "$needle" "$hay"
        FAIL=$((FAIL+1))
    fi
}

cd "$TESTDIR"
git init -q -b main
git config user.email "test@example.com"
git config user.name  "Test User"
git config commit.gpgsign false

# --- T1: fleet-commit.sh injects trailer when env var set -------------
echo "hello" > a.txt
git add a.txt
MULTIFLEET_NODE_ID=mac2 "$FLEET_COMMIT" -m "feat: first commit" >/dev/null 2>&1
LAST_MSG="$(git log -1 --pretty=%B)"
assert_contains "T1: trailer injected when MULTIFLEET_NODE_ID=mac2" \
    "Co-Authored-By: mac2-atlas <mac2@fleet.local>" "$LAST_MSG"

# --- T2: no trailer when env var unset and hostname unrecognised -------
echo "more" > b.txt
git add b.txt
# Force hostname-detect path to fail by clearing env and using a wrapper
# that overrides hostname output.
HOST_BIN="$TESTDIR/hostname"
cat > "$HOST_BIN" <<'SH'
#!/usr/bin/env bash
echo "unknown-host"
SH
chmod +x "$HOST_BIN"
PATH="$TESTDIR:$PATH" env -u MULTIFLEET_NODE_ID "$FLEET_COMMIT" -m "chore: second commit" >/dev/null 2>&1
LAST_MSG="$(git log -1 --pretty=%B)"
if printf '%s' "$LAST_MSG" | grep -Fq "Co-Authored-By:"; then
    printf 'FAIL  T2: unexpected trailer when env unset\n      got: %s\n' "$LAST_MSG"
    FAIL=$((FAIL+1))
else
    printf 'PASS  T2: no trailer when env unset and hostname unknown\n'
    PASS=$((PASS+1))
fi

# --- T3: idempotent — running twice doesn't duplicate -----------------
echo "third" > c.txt
git add c.txt
MULTIFLEET_NODE_ID=mac1 "$FLEET_COMMIT" \
    -m "feat: third commit

Co-Authored-By: mac1-atlas <mac1@fleet.local>" >/dev/null 2>&1
LAST_MSG="$(git log -1 --pretty=%B)"
COUNT=$(printf '%s\n' "$LAST_MSG" | grep -cF "Co-Authored-By: mac1-atlas")
assert "T3: trailer not duplicated on idempotent run" "1" "$COUNT"

# --- T4: prepare-commit-msg hook augments editor-style commits --------
mkdir -p .githooks
cp "$PREPARE_HOOK" .githooks/prepare-commit-msg
chmod +x .githooks/prepare-commit-msg
git config core.hooksPath .githooks
echo "fourth" > d.txt
git add d.txt
MULTIFLEET_NODE_ID=mac3 git commit -m "feat: fourth via hook" >/dev/null 2>&1
LAST_MSG="$(git log -1 --pretty=%B)"
assert_contains "T4: prepare-commit-msg hook injects trailer" \
    "Co-Authored-By: mac3-atlas <mac3@fleet.local>" "$LAST_MSG"

# --- T5: audit script tallies per node correctly ----------------------
git config --unset core.hooksPath
# Add one cloud commit so we cover all four nodes
echo "fifth" > e.txt
git add e.txt
MULTIFLEET_NODE_ID=cloud "$FLEET_COMMIT" -m "chore: fifth" >/dev/null 2>&1

AUDIT_JSON=$("$ATTR_AUDIT" --json 2>/dev/null)
# Extract via python so we don't need jq.
EXPECT_NODES=$(printf '%s' "$AUDIT_JSON" | python3 -c '
import json,sys
data=json.load(sys.stdin)
print(",".join(sorted(data.keys())))
')
assert_contains "T5a: audit JSON contains mac1" "mac1" "$EXPECT_NODES"
assert_contains "T5b: audit JSON contains mac2" "mac2" "$EXPECT_NODES"
assert_contains "T5c: audit JSON contains mac3" "mac3" "$EXPECT_NODES"
assert_contains "T5d: audit JSON contains cloud" "cloud" "$EXPECT_NODES"
assert_contains "T5e: audit JSON tracks unattributed" "(unattributed)" "$EXPECT_NODES"

MAC1_COMMITS=$(printf '%s' "$AUDIT_JSON" | python3 -c '
import json,sys
print(json.load(sys.stdin)["mac1"]["commits"])
')
assert "T5f: mac1 commit count == 1" "1" "$MAC1_COMMITS"

# --- summary ----------------------------------------------------------
printf '\n--- %d passed, %d failed ---\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
