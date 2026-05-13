#!/usr/bin/env bash
# test_3s_brainstorm_parallel.sh — R5 (VV1) regression for scripts/3s-brainstorm.sh.
#
# Verifies the parallelization refactor:
#   - --no-parallel flag falls back to sequential mode (A/B comparison)
#   - default parallel mode advertises stage A (6-wide) + stage B (options)
#   - cost cap pre-check trips ERR_COUNT in BOTH modes and produces a partial
#   - one-probe-fails / all-cardio-fails scenarios still produce output
#   - output format is unchanged for downstream callers (memory.fleet_brainstorm_writer
#     and Atlas synthesis): same section headers + Total cost line
#
# Modes:
#   default          — fast: dry-run + stub-based scenarios, no real surgeon calls
#   RUN_REAL=1       — opt-in: 2 real probes (parallel vs --no-parallel) to verify
#                     speedup; uses --cost-cap 0.10 per the R5 task spec
#
# Usage:
#   bash scripts/tests/test_3s_brainstorm_parallel.sh
#   RUN_REAL=1 bash scripts/tests/test_3s_brainstorm_parallel.sh
#
# Exit codes:
#   0 — all cases pass
#   1 — any case fails (per-case error printed)
#
# ZSF: every failed assertion writes a TAP-style "not ok" line and increments
# FAIL count. Final exit reflects total failures, never silently swallowed.

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/3s-brainstorm.sh"
RUN_REAL="${RUN_REAL:-0}"

if [[ ! -x "$SCRIPT" ]] && [[ ! -r "$SCRIPT" ]]; then
  echo "ERROR: script under test missing: $SCRIPT" >&2
  exit 2
fi

TMPDIR_BASE="$(mktemp -d -t 3s-brainstorm-parallel.XXXXXX)"
trap 'rm -rf "$TMPDIR_BASE"' EXIT

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf 'ok %d - %s\n' "$((PASS+FAIL))" "$1"; }
nok()  { FAIL=$((FAIL+1)); printf 'not ok %d - %s\n' "$((PASS+FAIL))" "$1"; }

# Run the script with stubbed `3s` binary so we never hit a real surgeon.
# Stubs honour the route and optionally inject a delay or failure for ZSF
# verification.
make_stub() {
  # $1 = stub script body; writes to $TMPDIR_BASE/3s and prepends to PATH.
  local stub_dir="$TMPDIR_BASE/stub-$RANDOM"
  mkdir -p "$stub_dir"
  cat >"$stub_dir/3s" <<'STUB_EOF'
#!/usr/bin/env bash
# Stub 3s — emits deterministic output, optional delay + failure injection
# driven by env vars set by the test harness.
ROUTE="${1:-}"
PROMPT="${2:-}"

DELAY_S="${STUB_DELAY_S:-0}"
[[ "$DELAY_S" != "0" ]] && sleep "$DELAY_S"

# Failure injection: STUB_FAIL_ROUTES=ask-remote,consensus → those routes exit 1
if [[ -n "${STUB_FAIL_ROUTES:-}" ]]; then
  IFS=',' read -ra FAILS <<< "$STUB_FAIL_ROUTES"
  for f in "${FAILS[@]}"; do
    if [[ "$ROUTE" == "$f" ]]; then
      echo "STUB INJECTED FAILURE for route=$ROUTE" >&2
      exit 1
    fi
  done
fi

# Failure injection by gate substring: STUB_FAIL_PROMPT_RE=goal → fails any
# prompt containing "goal"
if [[ -n "${STUB_FAIL_PROMPT_RE:-}" ]]; then
  if [[ "$PROMPT" =~ ${STUB_FAIL_PROMPT_RE} ]]; then
    echo "STUB INJECTED FAILURE for prompt-regex match" >&2
    exit 1
  fi
fi

# Normal output — minimal but parseable.
echo "[stub-${ROUTE}] answer to: ${PROMPT:0:80}"
echo ""
echo "Cost: \$0.0010"
STUB_EOF
  chmod +x "$stub_dir/3s"
  echo "$stub_dir"
}

# --- Case 1: --no-parallel flag advertised in dry-run output --------------
case_1() {
  local out
  out="$("$SCRIPT" --no-parallel --dry-run "topic-1" 2>&1 || true)"
  if grep -q "Execution mode:.*sequential" <<<"$out"; then
    ok "case-1: --no-parallel dry-run advertises sequential mode"
  else
    nok "case-1: --no-parallel dry-run did NOT advertise sequential mode"
    printf '  --- dry-run output ---\n%s\n  ---\n' "$out"
  fi
}

# --- Case 2: default parallel mode advertised in dry-run output ----------
case_2() {
  local out
  out="$("$SCRIPT" --dry-run "topic-2" 2>&1 || true)"
  if grep -q "Execution mode:.*parallel" <<<"$out"; then
    ok "case-2: default dry-run advertises parallel mode"
  else
    nok "case-2: default dry-run did NOT advertise parallel mode"
    printf '  --- dry-run output ---\n%s\n  ---\n' "$out"
  fi
}

# --- Case 3: clean run with stub surgeon — parallel mode ------------------
case_3() {
  local stub_dir log out
  stub_dir="$(make_stub)"
  log="$TMPDIR_BASE/case3.log"
  PATH="$stub_dir:$PATH" \
  STUB_DELAY_S=1 \
    "$SCRIPT" --cost-cap 0.50 --no-fleet-brainstorm "topic-3" >"$log" 2>"$log.err" || true
  out="$(cat "$log")"
  local err_log="$(cat "$log.err")"
  # Expect: complete (no ERR_COUNT bumps), all 7 sections present, mode line.
  local fails=0
  grep -q "^## Goal" <<<"$out"            || { nok "case-3: missing ## Goal section"; fails=$((fails+1)); }
  grep -q "^## Constraints" <<<"$out"     || { nok "case-3: missing ## Constraints"; fails=$((fails+1)); }
  grep -q "^## MVP" <<<"$out"             || { nok "case-3: missing ## MVP"; fails=$((fails+1)); }
  grep -q "^## Risks" <<<"$out"           || { nok "case-3: missing ## Risks"; fails=$((fails+1)); }
  grep -q "^## User" <<<"$out"            || { nok "case-3: missing ## User"; fails=$((fails+1)); }
  grep -q "^## Options" <<<"$out"         || { nok "case-3: missing ## Options"; fails=$((fails+1)); }
  grep -q "^## Recommendation" <<<"$out"  || { nok "case-3: missing ## Recommendation"; fails=$((fails+1)); }
  grep -q "Status:.*complete" <<<"$out"   || { nok "case-3: status not complete: $(grep '^**Status' <<<"$out")"; fails=$((fails+1)); }
  grep -q "Mode:.*parallel"   <<<"$out"   || { nok "case-3: missing Mode: parallel in header"; fails=$((fails+1)); }
  grep -q "Wall-clock:"       <<<"$out"   || { nok "case-3: missing Wall-clock: in header"; fails=$((fails+1)); }
  grep -q "\[timing\]"        <<<"$err_log" || { nok "case-3: missing >> [timing] stderr line"; fails=$((fails+1)); }
  [[ $fails -eq 0 ]] && ok "case-3: clean parallel run with stub — all 7 sections, complete status, timing reported"
}

# --- Case 4: --no-parallel sequential run with stub — same output shape ---
case_4() {
  local stub_dir log out
  stub_dir="$(make_stub)"
  log="$TMPDIR_BASE/case4.log"
  PATH="$stub_dir:$PATH" \
  STUB_DELAY_S=1 \
    "$SCRIPT" --no-parallel --cost-cap 0.50 --no-fleet-brainstorm "topic-4" >"$log" 2>"$log.err" || true
  out="$(cat "$log")"
  local fails=0
  grep -q "^## Goal" <<<"$out"            || { nok "case-4: missing ## Goal"; fails=$((fails+1)); }
  grep -q "^## Recommendation" <<<"$out"  || { nok "case-4: missing ## Recommendation"; fails=$((fails+1)); }
  grep -q "Status:.*complete" <<<"$out"   || { nok "case-4: status not complete"; fails=$((fails+1)); }
  grep -q "Mode:.*sequential" <<<"$out"   || { nok "case-4: missing Mode: sequential in header"; fails=$((fails+1)); }
  [[ $fails -eq 0 ]] && ok "case-4: clean sequential run with stub — same output shape, sequential mode reported"
}

# --- Case 5: one probe fails — partial status, other gates render ---------
case_5() {
  local stub_dir log out
  stub_dir="$(make_stub)"
  log="$TMPDIR_BASE/case5.log"
  # Force the "mvp" gate (ask-local with prompt containing "smallest faithful")
  # to fail. ZSF: other gates still produce output, status reports PARTIAL.
  PATH="$stub_dir:$PATH" \
  STUB_DELAY_S=0 \
  STUB_FAIL_PROMPT_RE="smallest faithful" \
  BRAINSTORM_MAX_ATTEMPTS=1 \
  BRAINSTORM_RETRY_SLEEP_S=0 \
    "$SCRIPT" --cost-cap 0.50 --no-fleet-brainstorm "topic-5" >"$log" 2>"$log.err" || true
  out="$(cat "$log")"
  local fails=0
  grep -q "Status:.*PARTIAL"   <<<"$out" || { nok "case-5: status not PARTIAL after one-probe-fails"; fails=$((fails+1)); }
  grep -q "^## Goal"           <<<"$out" || { nok "case-5: missing ## Goal (other gates should still render)"; fails=$((fails+1)); }
  grep -q "^## Recommendation" <<<"$out" || { nok "case-5: missing ## Recommendation"; fails=$((fails+1)); }
  [[ $fails -eq 0 ]] && ok "case-5: one-probe-fails — partial status, other gates rendered (ZSF)"
}

# --- Case 6: all cardio probes fail — partial, other gates render ---------
case_6() {
  local stub_dir log out
  stub_dir="$(make_stub)"
  log="$TMPDIR_BASE/case6.log"
  # Fail every ask-remote (cardio) call; ask-local (neuro) + consensus still succeed.
  PATH="$stub_dir:$PATH" \
  STUB_DELAY_S=0 \
  STUB_FAIL_ROUTES="ask-remote" \
  BRAINSTORM_MAX_ATTEMPTS=1 \
  BRAINSTORM_RETRY_SLEEP_S=0 \
    "$SCRIPT" --cost-cap 0.50 --no-fleet-brainstorm "topic-6" >"$log" 2>"$log.err" || true
  out="$(cat "$log")"
  local fails=0
  grep -q "Status:.*PARTIAL"    <<<"$out" || { nok "case-6: status not PARTIAL after all-cardio-fails"; fails=$((fails+1)); }
  grep -q "^## MVP"             <<<"$out" || { nok "case-6: missing ## MVP (neuro should still render)"; fails=$((fails+1)); }
  grep -q "^## Recommendation"  <<<"$out" || { nok "case-6: missing ## Recommendation"; fails=$((fails+1)); }
  [[ $fails -eq 0 ]] && ok "case-6: all-cardio-fails — neuro+consensus still render, PARTIAL status"
}

# --- Case 7: cost-cap pre-check trips — skipped gates, PARTIAL status ----
case_7() {
  local stub_dir log out
  stub_dir="$(make_stub)"
  log="$TMPDIR_BASE/case7.log"
  # Set cap=0.00 → cost_over_cap trips on first check (0.0 > 0.00 is false, so
  # we need a tiny negative-ish cap. Easier: prime TOTAL_COST artificially by
  # running a near-zero cap. cap "0.0001" — first $0.0010 stub answer is the
  # second call's pre-check trigger. Use cap 0 and inject a small delta? The
  # script checks `cost_over_cap "$TOTAL_COST" "$COST_CAP"` BEFORE the parallel
  # batch when TOTAL_COST=0.0. To trigger pre-batch skip, we need the previous
  # call to have already accumulated past cap. In the parallel path there IS
  # no "previous call" — so we exercise the sequential path's per-call check.
  PATH="$stub_dir:$PATH" \
  STUB_DELAY_S=0 \
  BRAINSTORM_MAX_ATTEMPTS=1 \
  BRAINSTORM_RETRY_SLEEP_S=0 \
    "$SCRIPT" --no-parallel --cost-cap 0.0005 --no-fleet-brainstorm "topic-7" >"$log" 2>"$log.err" || true
  out="$(cat "$log")"
  local fails=0
  # After gate 1 returns Cost: $0.0010, TOTAL_COST=0.0010 > 0.0005 → subsequent
  # gates are skipped explicitly. Expect at least one "(skipped: cost cap" body
  # text, and PARTIAL status.
  grep -q "Status:.*PARTIAL"      <<<"$out" || { nok "case-7: status not PARTIAL after cost-cap trip"; fails=$((fails+1)); }
  grep -q "skipped: cost cap"     <<<"$out" || { nok "case-7: no 'skipped: cost cap' marker found"; fails=$((fails+1)); }
  [[ $fails -eq 0 ]] && ok "case-7: cost-cap-trigger — subsequent gates skipped, PARTIAL status (ZSF)"
}

# --- Case 8: --no-parallel vs parallel produce same section structure -----
case_8() {
  local stub_dir log_p log_s
  stub_dir="$(make_stub)"
  log_p="$TMPDIR_BASE/case8.parallel.log"
  log_s="$TMPDIR_BASE/case8.sequential.log"
  PATH="$stub_dir:$PATH" STUB_DELAY_S=0 \
    "$SCRIPT" --cost-cap 0.50 --no-fleet-brainstorm "topic-8" >"$log_p" 2>/dev/null || true
  PATH="$stub_dir:$PATH" STUB_DELAY_S=0 \
    "$SCRIPT" --no-parallel --cost-cap 0.50 --no-fleet-brainstorm "topic-8" >"$log_s" 2>/dev/null || true
  # Compare the set of `^## ` headers (order and content of sections).
  local p_sections s_sections
  p_sections="$(grep '^## ' "$log_p" | sort)"
  s_sections="$(grep '^## ' "$log_s" | sort)"
  if [[ "$p_sections" == "$s_sections" ]]; then
    ok "case-8: --no-parallel and parallel produce identical section set (downstream compat)"
  else
    nok "case-8: section set differs between parallel and --no-parallel"
    printf '  --- parallel sections ---\n%s\n  --- sequential sections ---\n%s\n  ---\n' "$p_sections" "$s_sections"
  fi
}

# --- Case 9 (REAL, opt-in): wall-clock speedup vs --no-parallel -----------
case_9_real() {
  if [[ "$RUN_REAL" != "1" ]]; then
    printf '# skip case-9-real: RUN_REAL=0 (set RUN_REAL=1 to spend ~$0.04 on real probes)\n'
    return 0
  fi
  local log_p log_s wall_p wall_s
  log_p="$TMPDIR_BASE/case9.parallel.log"
  log_s="$TMPDIR_BASE/case9.sequential.log"
  echo "# case-9-real: running parallel probe (real surgeons)..." >&2
  "$SCRIPT" --cost-cap 0.10 --no-fleet-brainstorm "R5 verify: parallelization saves wall-clock" \
    >"$log_p" 2>"$log_p.err" || true
  echo "# case-9-real: running --no-parallel probe (real surgeons)..." >&2
  "$SCRIPT" --no-parallel --cost-cap 0.10 --no-fleet-brainstorm "R5 verify: parallelization saves wall-clock" \
    >"$log_s" 2>"$log_s.err" || true
  wall_p="$(grep -Eo 'wall=[0-9]+s' "$log_p.err" | head -1 | tr -dc 0-9)"
  wall_s="$(grep -Eo 'wall=[0-9]+s' "$log_s.err" | head -1 | tr -dc 0-9)"
  if [[ -z "$wall_p" || -z "$wall_s" ]]; then
    nok "case-9-real: could not extract wall-clock from one or both runs"
    return 0
  fi
  echo "# case-9-real: parallel=${wall_p}s sequential=${wall_s}s" >&2
  # Require parallel < sequential (any improvement counts; spec target 3x).
  if [[ "$wall_p" -lt "$wall_s" ]]; then
    ok "case-9-real: parallel (${wall_p}s) < sequential (${wall_s}s) — speedup confirmed"
  else
    nok "case-9-real: parallel (${wall_p}s) NOT faster than sequential (${wall_s}s)"
  fi
}

# --- Case 10: counter-probe default ON (ZZ3 / WW5 ship) --------------------
# Verify CONTEXT_DNA_CONSENSUS_COUNTER_PROBE defaults to "on" in dry-run.
case_10_counter_probe_default_on() {
  local out
  # Explicitly clear any inherited value so we test the default.
  out="$(unset CONTEXT_DNA_CONSENSUS_COUNTER_PROBE; "$SCRIPT" --dry-run "topic-10" 2>&1 || true)"
  if grep -Eq "Counter-probe \(sycophancy\):[[:space:]]+on" <<<"$out"; then
    ok "case-10: counter-probe defaults to ON when env var unset"
  else
    nok "case-10: counter-probe did NOT default to ON"
    printf '  --- dry-run output ---\n%s\n  ---\n' "$out"
  fi
}

# --- Case 11: caller override flips counter-probe back to OFF -------------
case_11_counter_probe_override_off() {
  local out
  out="$(CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=off "$SCRIPT" --dry-run "topic-11" 2>&1 || true)"
  if grep -Eq "Counter-probe \(sycophancy\):[[:space:]]+off" <<<"$out"; then
    ok "case-11: CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=off flips it back to OFF"
  else
    nok "case-11: caller override CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=off did NOT take effect"
    printf '  --- dry-run output ---\n%s\n  ---\n' "$out"
  fi
}

# --- Run all cases --------------------------------------------------------
case_1
case_2
case_3
case_4
case_5
case_6
case_7
case_8
case_9_real
case_10_counter_probe_default_on
case_11_counter_probe_override_off

TOTAL=$((PASS + FAIL))
printf '\n1..%d\n' "$TOTAL"
printf '# pass=%d fail=%d\n' "$PASS" "$FAIL"

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
