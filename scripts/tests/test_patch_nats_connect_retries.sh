#!/usr/bin/env bash
# test_patch_nats_connect_retries.sh — AAA2 2026-05-12
#
# Exercises scripts/patch-nats-connect-retries.py against a temp plist so
# the real mac1 LaunchAgent is never touched. Covers dry-run/apply/revert/
# idempotency/validation/ZSF paths.
#
# Assertions:
#   1. --dry-run on a plist missing the flag: exit 0, prints diff with
#      --connect_retries insertion.
#   2. --apply inserts the flag; second --apply is a no-op ("already").
#   3. --apply + --revert round-trip restores byte-identical original.
#   4. Bogus --retries value (e.g. "abc"): exit 1 with clear error.
#   5. Bogus --config-path: exit 1 with clear error.
#   6. ZSF: write-protected plist → exit 1 with verbatim error (original
#      file left intact, no partial write).
#   7. --dry-run on a plist already at the desired value: exit 0,
#      "no changes needed".
set -u
set -o pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_DIR/scripts/patch-nats-connect-retries.py"

if [[ ! -f "$SCRIPT" ]]; then
    echo "FAIL: script not found at $SCRIPT" >&2
    exit 1
fi

WORK="$(mktemp -d -t patch_nats_retries_test.XXXXXX)"
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

pass=0
fail=0
ok() { echo "  PASS: $1"; pass=$((pass + 1)); }
no() { echo "  FAIL: $1"; fail=$((fail + 1)); }

make_plist_without_flag() {
    local out="$1"
    cat > "$out" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>io.contextdna.nats-server</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/local/bin/nats-server</string>
		<string>-p</string>
		<string>4222</string>
		<string>--cluster</string>
		<string>nats://0.0.0.0:6222</string>
		<string>--cluster_name</string>
		<string>contextdna</string>
		<string>--routes</string>
		<string>nats://192.168.1.183:6222</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
</dict>
</plist>
PLIST
}

make_plist_with_flag() {
    local out="$1"
    local n="${2:-120}"
    cat > "$out" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>io.contextdna.nats-server</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/local/bin/nats-server</string>
		<string>-p</string>
		<string>4222</string>
		<string>--cluster</string>
		<string>nats://0.0.0.0:6222</string>
		<string>--cluster_name</string>
		<string>contextdna</string>
		<string>--connect_retries</string>
		<string>${n}</string>
		<string>--routes</string>
		<string>nats://192.168.1.183:6222</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
</dict>
</plist>
PLIST
}

echo "==> 1. --dry-run on plist missing the flag prints diff, exits 0"
PLIST1="$WORK/test1.plist"
make_plist_without_flag "$PLIST1"
out="$(python3 "$SCRIPT" --dry-run --config-path "$PLIST1" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -q -- "--connect_retries" \
    && echo "$out" | grep -q "120" \
    && echo "$out" | grep -q "action:" ; then
    ok "dry-run missing-flag exits 0 with diff"
else
    no "dry-run missing-flag rc=$rc, out: $out"
fi

# Confirm dry-run didn't mutate the file.
if ! grep -q -- "--connect_retries" "$PLIST1"; then
    ok "dry-run did not mutate plist"
else
    no "dry-run mutated plist (it should not)"
fi

echo "==> 2. --apply inserts flag; second --apply is a no-op"
PLIST2="$WORK/test2.plist"
make_plist_without_flag "$PLIST2"
out="$(python3 "$SCRIPT" --apply --config-path "$PLIST2" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]] && grep -q -- "--connect_retries" "$PLIST2" \
    && grep -q "<string>120</string>" "$PLIST2" ; then
    ok "first --apply inserted flag"
else
    no "first --apply rc=$rc, out: $out"
fi
if [[ -f "$PLIST2.bak" ]]; then
    ok ".bak file created"
else
    no ".bak file missing after --apply"
fi
# second apply
out="$(python3 "$SCRIPT" --apply --config-path "$PLIST2" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -qiE "(no changes needed|already)"; then
    ok "second --apply is idempotent"
else
    no "second --apply not idempotent rc=$rc, out: $out"
fi

echo "==> 3. --apply + --revert round-trip restores byte-identical file"
PLIST3="$WORK/test3.plist"
make_plist_without_flag "$PLIST3"
cp "$PLIST3" "$PLIST3.original"
python3 "$SCRIPT" --apply --config-path "$PLIST3" >/dev/null 2>&1
if cmp -s "$PLIST3" "$PLIST3.original"; then
    no "post --apply file unchanged from original (apply did nothing)"
else
    ok "--apply mutated file"
fi
python3 "$SCRIPT" --revert --config-path "$PLIST3" >/dev/null 2>&1
if cmp -s "$PLIST3" "$PLIST3.original"; then
    ok "--revert restored byte-identical original"
else
    no "--revert did not restore byte-identical original"
    diff "$PLIST3" "$PLIST3.original" || true
fi

echo "==> 4. Bogus --retries value exits non-zero with clear error"
PLIST4="$WORK/test4.plist"
make_plist_without_flag "$PLIST4"
out="$(python3 "$SCRIPT" --dry-run --config-path "$PLIST4" --retries abc 2>&1)"
rc=$?
if [[ $rc -ne 0 ]] && echo "$out" | grep -qiE "(invalid|positive integer)"; then
    ok "bogus --retries=abc rejected (rc=$rc)"
else
    no "bogus --retries=abc rc=$rc, out: $out"
fi
# Also reject zero / negative.
out="$(python3 "$SCRIPT" --dry-run --config-path "$PLIST4" --retries 0 2>&1)"
rc=$?
if [[ $rc -ne 0 ]] && echo "$out" | grep -qiE "(invalid|positive integer)"; then
    ok "bogus --retries=0 rejected (rc=$rc)"
else
    no "bogus --retries=0 rc=$rc, out: $out"
fi

echo "==> 5. Bogus --config-path exits non-zero with clear error"
out="$(python3 "$SCRIPT" --dry-run --config-path "$WORK/does-not-exist.plist" 2>&1)"
rc=$?
if [[ $rc -ne 0 ]] && echo "$out" | grep -qiE "(not found|plist)"; then
    ok "missing --config-path rejected (rc=$rc)"
else
    no "missing --config-path rc=$rc, out: $out"
fi

echo "==> 6. ZSF: write-protected target → exit 1, original intact"
PLIST6="$WORK/test6.plist"
make_plist_without_flag "$PLIST6"
cp "$PLIST6" "$PLIST6.untouched"
# Protect the *directory* so neither rename nor backup-write can succeed.
chmod a-w "$WORK"
chmod a-w "$PLIST6" 2>/dev/null || true
out="$(python3 "$SCRIPT" --apply --config-path "$PLIST6" 2>&1)"
rc=$?
# Restore write perms before any assertion that needs them.
chmod u+w "$WORK"
chmod u+w "$PLIST6" 2>/dev/null || true
if [[ $rc -ne 0 ]] && echo "$out" | grep -qiE "(failed|error|permission)"; then
    ok "write-protected target exits non-zero with verbatim error"
else
    no "write-protected target rc=$rc, out: $out"
fi
if cmp -s "$PLIST6" "$PLIST6.untouched"; then
    ok "original plist intact after failed --apply"
else
    no "original plist mutated after failed --apply (ZSF violation)"
fi

echo "==> 7. --dry-run on already-correct plist: exit 0, 'no changes needed'"
PLIST7="$WORK/test7.plist"
make_plist_with_flag "$PLIST7" 120
out="$(python3 "$SCRIPT" --dry-run --config-path "$PLIST7" 2>&1)"
rc=$?
if [[ $rc -eq 0 ]] && echo "$out" | grep -qiE "(no changes needed|already)"; then
    ok "already-correct plist dry-run is no-op"
else
    no "already-correct dry-run rc=$rc, out: $out"
fi

echo ""
echo "Results: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
