#!/usr/bin/env bash
# scripts/demo-halt-rehearse.sh — Scene-6 HALT recovery rehearsal.
#
# Walks the FULL recovery loop end-to-end in a scratch directory:
#
#   Step 1  Chief writes .fleet/HALT (synthetic — no real DeepSeek consult).
#   Step 2  Verify is_halted() returns True.
#   Step 3  Verify green_light_runner.claim_next refuses with reason
#           starting "audit_gate:halt:".
#   Step 4  Operator simulates the ack: writes .fleet/HALT-ack-<date>.md
#           with the human-readable triage statement.
#   Step 5  Audit team writes the recovery plan to
#           .fleet/audits/<date>-recovery.md (cluster id, root cause,
#           remediation steps, verification command).
#   Step 6  clear_halt() removes .fleet/HALT.
#   Step 7  Verify green_light_runner.claim_next now succeeds.
#   Step 8  Cleanup — wipe scratch dir, restore live HALT if our defensive
#           guard observed any drift on the live superrepo.
#
# Hard rule: this script NEVER touches the live superrepo's .fleet/HALT.
# Everything happens in a tempfile.mkdtemp(prefix="halt-rehearse-") scratch
# git repo, identical to scripts/tests/test_halt_live_fire.py's isolation.
#
# Cost: $0 (no real LLM consults — synthetic decisions only).
#
# Flags:
#   --dry-run          narrate each step, do NOT execute (no chief loop, no
#                      claim, no temp dirs created). Returns 0 if the plan
#                      is self-consistent.
#   --no-cleanup       leave scratch dir behind (for inspection / debugging).
#   --no-color         disable ANSI colour.
#   -h | --help        show this header.
#
# Exit codes:
#   0  rehearsal PASS — all 8 steps verified.
#   1  any step FAIL — see the per-step output for the offender.
#   2  bad CLI args.
#
# This is the Q5 follow-up to M3's halt-live-fire test:
#   * M3 verified HALT writes + clears in scratch dirs.
#   * Q5 packages the SAME contract as a Scene-6 demo cutaway, with explicit
#     operator-ack + recovery-plan steps so Aaron can show the FULL human-in-
#     the-loop arc on camera.
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

DRY_RUN=0
NO_CLEANUP=0
USE_COLOR=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)     DRY_RUN=1 ;;
    --no-cleanup)  NO_CLEANUP=1 ;;
    --no-color)    USE_COLOR=0 ;;
    -h|--help)     grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[halt-rehearse] unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── Pretty ────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ "$USE_COLOR" -eq 1 ]; then
  C_R=$'\033[0;31m'; C_G=$'\033[0;32m'; C_Y=$'\033[0;33m'
  C_C=$'\033[0;36m'; C_M=$'\033[0;35m'; C_D=$'\033[0;90m'
  C_B=$'\033[1m'; C_X=$'\033[0m'
else
  C_R=""; C_G=""; C_Y=""; C_C=""; C_M=""; C_D=""; C_B=""; C_X=""
fi
banner() { printf "${C_M}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_X}\n"; }
step()   { printf "\n${C_B}${C_C}STEP %s — %s${C_X}\n" "$1" "$2"; }
ok()     { printf "  ${C_G}✓${C_X} %s\n" "$*"; }
warn()   { printf "  ${C_Y}⚠${C_X} %s\n" "$*"; }
fail()   { printf "  ${C_R}✗${C_X} %s\n" "$*" >&2; }
info()   { printf "  ${C_D}│${C_X} %s\n" "$*"; }

banner
printf "${C_B}  Scene-6 — HALT Recovery Rehearsal${C_X}\n"
printf "${C_D}  scratch-dir isolation, \$0 synthetic, ZSF compliant${C_X}\n"
banner

# ── Resolve python ────────────────────────────────────────────────────────
PY="${REPO_ROOT}/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  fail "no python3 available — install or run with .venv active"
  exit 1
fi
info "python: $PY"

# ── Defensive live-HALT guard ─────────────────────────────────────────────
# Snapshot live .fleet/HALT before we touch anything, restore on exit if our
# synthetic flow somehow leaked a write to the live repo. M3 used the same
# pattern — the guard SHOULD never fire.
LIVE_HALT="${REPO_ROOT}/.fleet/HALT"
LIVE_HALT_PRE_EXISTED=0
LIVE_HALT_PRE_CONTENT=""
if [ -f "$LIVE_HALT" ]; then
  LIVE_HALT_PRE_EXISTED=1
  LIVE_HALT_PRE_CONTENT="$(cat "$LIVE_HALT" 2>/dev/null || true)"
  info "live .fleet/HALT exists (pre-rehearsal); will restore if we drift"
fi

restore_live_halt() {
  if [ "$LIVE_HALT_PRE_EXISTED" -eq 1 ] && [ ! -f "$LIVE_HALT" ]; then
    mkdir -p "$(dirname "$LIVE_HALT")"
    printf '%s' "$LIVE_HALT_PRE_CONTENT" > "$LIVE_HALT"
    fail "[GUARD] live .fleet/HALT was deleted during rehearsal — restored."
    return 2
  fi
  if [ "$LIVE_HALT_PRE_EXISTED" -eq 0 ] && [ -f "$LIVE_HALT" ]; then
    rm -f "$LIVE_HALT"
    fail "[GUARD] live .fleet/HALT was created during rehearsal — removed."
    return 2
  fi
  return 0
}

# ── Dry-run plan ──────────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  step 1 "[plan] chief writes synthetic .fleet/HALT in scratch dir"
  info "would call: multifleet.audit_log.set_halt(scratch, reason='C-D-04-degradation: …', by='halt-rehearse')"
  step 2 "[plan] verify is_halted(scratch) == True"
  step 3 "[plan] verify green_light_runner.claim_next(scratch) refuses with reason starting 'audit_gate:halt:'"
  step 4 "[plan] write .fleet/HALT-ack-<date>.md (operator triage statement)"
  step 5 "[plan] write .fleet/audits/<date>-recovery.md (cluster id, root cause, remediation, verification cmd)"
  step 6 "[plan] clear_halt(scratch) — removes .fleet/HALT"
  step 7 "[plan] verify claim_next(scratch) now claims G-0001"
  step 8 "[plan] cleanup scratch dir; verify live .fleet/HALT untouched"
  banner
  printf "${C_G}DRY-RUN PASS${C_X} — plan is consistent.\n"
  banner
  exit 0
fi

# ── Cleanup trap ──────────────────────────────────────────────────────────
SCRATCH=""
RC=0
cleanup() {
  RC=$?
  if [ "$NO_CLEANUP" -eq 1 ]; then
    info "[--no-cleanup] scratch left behind: $SCRATCH"
  elif [ -n "${SCRATCH:-}" ] && [ -d "$SCRATCH" ]; then
    rm -rf "$SCRATCH" 2>/dev/null || true
  fi
  if ! restore_live_halt; then
    RC=2
  fi
  printf "\n  cleanup complete (rc=%d)\n" "$RC"
  exit $RC
}
trap cleanup EXIT INT TERM

# ── Build scratch git repo + open green-light item ────────────────────────
SCRATCH="$(mktemp -d -t halt-rehearse)"
info "scratch: $SCRATCH"

# We need a real git repo because green_light_runner uses git ops.
( cd "$SCRATCH" && git init -q --bare origin.git ) || { fail "git init bare failed"; exit 1; }
( cd "$SCRATCH" && git clone -q origin.git work ) || { fail "git clone failed"; exit 1; }
WORK="$SCRATCH/work"
( cd "$WORK" && git config user.email "halt-rehearse@local" && git config user.name "halt-rehearse" ) || true
( cd "$WORK" && git checkout -q -b main 2>/dev/null || git checkout -q main ) || true

mkdir -p "$WORK/.fleet/priorities"
cat > "$WORK/.fleet/priorities/green-light.md" <<'EOF'
# Pool

## Pool

- [ ] [G-0001] halt-rehearsal-test :: scope=test :: evidence=halt-rehearse
EOF
cat > "$WORK/.fleet/priorities/red-light.md" <<'EOF'
# Red

EOF

( cd "$WORK" && git add .fleet/priorities/ && git commit -q -m "seed halt-rehearse pool" && git push -q -u origin main ) \
  || { fail "git seed/push failed"; exit 1; }

# ── STEP 1: chief writes synthetic .fleet/HALT ────────────────────────────
step 1 "chief writes synthetic .fleet/HALT in scratch dir"

PYTHONPATH="${REPO_ROOT}/multi-fleet:${REPO_ROOT}" "$PY" - "$WORK" <<'PYEOF' || { fail "set_halt call failed"; exit 1; }
import sys
from pathlib import Path
work = Path(sys.argv[1])
sys.path.insert(0, str(Path("multi-fleet").resolve()))
from multifleet.audit_log import set_halt, append_decision
# Write the same shape the chief loop produces in M3 live-fire.
set_halt(
    work,
    reason=(
        "C-D-04-degradation: cardio: ELEVATE_TO_CRITICAL — webhook silence "
        "for 720s w/ daemon up + subscription_count=0 is producer-bus failure. "
        "| neuro: ELEVATE_TO_CRITICAL — sustained silence confirms bus down. "
        "(SYNTHETIC REHEARSAL — no real findings)"
    ),
    by="halt-rehearse-2026-05-13",
)
# Also append a synthetic decision row so .fleet/audits/<date>-decisions.md is
# realistic for the camera.
append_decision(
    work,
    cluster_id="C-D-04-degradation-REHEARSE",
    finding_ids=["F-DEMO04-rehearse-1", "F-DEMO04-rehearse-2"],
    decision="HALT_GREEN_LIGHT",
    consensus=1.0,
    iterations=2,
    rationale=(
        "synthetic rehearsal: cardio+neuro unanimous ELEVATE_TO_CRITICAL on "
        "D-04 webhook-dead-air; consensus 1.0 → HALT_GREEN_LIGHT. (Scene-6 demo, no live consult.)"
    ),
    transcript_ref="demo-halt-rehearse.sh",
)
print("step1: .fleet/HALT written + decisions.md appended")
PYEOF

if [ -f "$WORK/.fleet/HALT" ]; then
  ok ".fleet/HALT written"
  info "head: $(head -3 "$WORK/.fleet/HALT" | tr '\n' ' / ')"
else
  fail ".fleet/HALT not present after set_halt()"
  exit 1
fi

# ── STEP 2: is_halted == True ─────────────────────────────────────────────
step 2 "verify is_halted(scratch) == True"
PYTHONPATH="${REPO_ROOT}/multi-fleet:${REPO_ROOT}" "$PY" - "$WORK" <<'PYEOF' || { fail "is_halted check raised"; exit 1; }
import sys
from pathlib import Path
work = Path(sys.argv[1])
sys.path.insert(0, str(Path("multi-fleet").resolve()))
from multifleet.audit_log import is_halted
print("is_halted=", is_halted(work))
assert is_halted(work), "is_halted should be True"
PYEOF
ok "is_halted() returns True"

# ── STEP 3: claim_next refuses with audit_gate:halt: ──────────────────────
step 3 "verify green_light_runner.claim_next refuses (reason: audit_gate:halt:…)"
PYTHONPATH="${REPO_ROOT}/multi-fleet:${REPO_ROOT}" "$PY" - "$WORK" <<'PYEOF' || { fail "claim_next probe raised"; exit 1; }
import sys
from pathlib import Path
work = Path(sys.argv[1])
sys.path.insert(0, str(Path("multi-fleet").resolve()))
# Stub run_detector_suite to [] so the refusal can ONLY come from the HALT flag.
from multifleet import risk_auditor as ra
ra.run_detector_suite = lambda *a, **kw: []
from multifleet.green_light_runner import claim_next
r = claim_next(work, node_id="halt-rehearse")
print("claimed=", r.claimed)
print("reason=", (r.reason or "")[:160])
assert r.claimed is False, "should NOT claim under HALT"
assert (r.reason or "").startswith("audit_gate:halt:"), \
    "reason should start audit_gate:halt:, got " + repr(r.reason)
PYEOF
ok "claim_next refused; reason starts 'audit_gate:halt:' (refusal is HALT-driven)"

# ── STEP 4: operator ack ──────────────────────────────────────────────────
step 4 "operator writes .fleet/HALT-ack-<date>.md (triage statement)"
ACK_PATH="$WORK/.fleet/HALT-ack-$(date +%F).md"
mkdir -p "$WORK/.fleet"
cat > "$ACK_PATH" <<'EOF'
# HALT acknowledgment

Operator: aaron@local
Acknowledged at: $(date -u +%FT%TZ)
Cluster: C-D-04-degradation-REHEARSE
Source HALT: .fleet/HALT (cluster summary above)

Triage statement:
  Webhook bus dead-air observed for 720s while the daemon was up. Two D-04
  CRITICAL findings clustered into C-D-04-degradation. Both surgeons
  unanimous on ELEVATE_TO_CRITICAL → chief HALT was correct. Standing down
  the green-light pool was the right call.

Next: write recovery plan in .fleet/audits/<date>-recovery.md, then clear
the HALT flag.
EOF
# Render $(date) in the heredoc above (we kept it literal so the on-screen
# text shows the same shape Aaron uses in production).
sed -i.bak "s|\$(date -u +%FT%TZ)|$(date -u +%FT%TZ)|g" "$ACK_PATH"
rm -f "${ACK_PATH}.bak"
if [ -f "$ACK_PATH" ]; then
  ok "ack written: ${ACK_PATH#$WORK/}"
else
  fail "ack write failed"
  exit 1
fi

# ── STEP 5: audit team writes recovery plan ──────────────────────────────
step 5 "audit team writes .fleet/audits/<date>-recovery.md"
RECOVERY_PATH="$WORK/.fleet/audits/$(date +%F)-recovery.md"
mkdir -p "$(dirname "$RECOVERY_PATH")"
cat > "$RECOVERY_PATH" <<'EOF'
# Recovery plan — C-D-04-degradation-REHEARSE

Date: $(date +%F)
Trigger: chief HALT_GREEN_LIGHT decision (synthetic rehearsal).
Cluster: C-D-04-degradation-REHEARSE
Findings: F-DEMO04-rehearse-1, F-DEMO04-rehearse-2

## Root cause (rehearsal narrative)

D-04 detector flagged sustained webhook silence
(events_recorded_now=0, prev=247, silence_duration_s=720) while the
fleet daemon was reporting healthy. Both surgeons interpreted this as a
producer-or-bus failure rather than a quiet period: subscription_count=0
on the webhook subject was the smoking gun. Chief HALT was correct.

## Remediation steps

1. Confirm /health.webhook.events_recorded is incrementing again.
   - command: curl -sS http://127.0.0.1:8855/health | jq '.webhook.events_recorded'
2. Confirm subscription_count > 0 on event.webhook.completed.<node>.
3. Re-run scripts/auto-memory-query.sh on a small prompt; verify a
   completion event lands in /health within 5s.
4. Only after all three above pass, proceed to clear the HALT flag.

## Verification command (operator pastes)

  multi-fleet/venv.nosync/bin/python3 -c "
  from multifleet.audit_log import clear_halt
  from pathlib import Path
  print('cleared:', clear_halt(Path('.')))
  "

## Post-clear verification

- Run claim_next from any node: should now succeed.
- Tail .fleet/audits/<date>-decisions.md for any new HALT (should be none).
- If a new HALT fires within 1 hour, the underlying issue was NOT fixed —
  re-open this recovery plan and dig deeper.

## Hand-off

This recovery plan was authored by the audit team during a Scene-6 demo
rehearsal. Real recovery plans must reference real finding IDs, not
'-rehearse' synthetic ones.
EOF
sed -i.bak "s|\$(date +%F)|$(date +%F)|g" "$RECOVERY_PATH"
rm -f "${RECOVERY_PATH}.bak"
if [ -f "$RECOVERY_PATH" ]; then
  ok "recovery plan written: ${RECOVERY_PATH#$WORK/}"
else
  fail "recovery plan write failed"
  exit 1
fi

# ── STEP 6: clear_halt() removes .fleet/HALT ──────────────────────────────
step 6 "clear_halt(scratch) removes .fleet/HALT"
PYTHONPATH="${REPO_ROOT}/multi-fleet:${REPO_ROOT}" "$PY" - "$WORK" <<'PYEOF' || { fail "clear_halt raised"; exit 1; }
import sys
from pathlib import Path
work = Path(sys.argv[1])
sys.path.insert(0, str(Path("multi-fleet").resolve()))
from multifleet.audit_log import clear_halt, is_halted
cleared = clear_halt(work)
print("cleared=", cleared)
print("is_halted=", is_halted(work))
assert cleared is True, "clear_halt should return True when HALT was present"
assert not is_halted(work), "is_halted should be False after clear"
PYEOF

if [ -f "$WORK/.fleet/HALT" ]; then
  fail ".fleet/HALT still present after clear_halt()"
  exit 1
fi
ok ".fleet/HALT removed"

# ── STEP 7: claim_next succeeds ───────────────────────────────────────────
step 7 "verify green_light_runner.claim_next now claims G-0001"
PYTHONPATH="${REPO_ROOT}/multi-fleet:${REPO_ROOT}" "$PY" - "$WORK" <<'PYEOF' || { fail "claim_next post-recovery raised"; exit 1; }
import sys
from pathlib import Path
work = Path(sys.argv[1])
sys.path.insert(0, str(Path("multi-fleet").resolve()))
from multifleet import risk_auditor as ra
ra.run_detector_suite = lambda *a, **kw: []
from multifleet.green_light_runner import claim_next
r = claim_next(work, node_id="halt-rehearse")
print("claimed=", r.claimed, "item_id=", (r.item.item_id if r.item else None))
assert r.claimed, "claim should succeed after HALT cleared, got reason=" + repr(r.reason)
PYEOF
ok "claim_next succeeded post-recovery (G-0001 claimed)"

# ── STEP 8: cleanup verified ──────────────────────────────────────────────
step 8 "cleanup — scratch will be removed; live .fleet/HALT verified untouched"
# The trap will rmtree($SCRATCH). Verify live HALT did not drift.
if ! restore_live_halt; then
  fail "live .fleet/HALT drifted during rehearsal — guard restored it"
  exit 1
fi
ok "live .fleet/HALT untouched"

banner
printf "${C_B}${C_G}HALT REHEARSAL PASS${C_X} — all 8 steps verified.\n"
printf "  scratch dir: %s%s${C_X}\n" "$C_D" "$SCRATCH"
[ "$NO_CLEANUP" -eq 1 ] && printf "  (left in place; --no-cleanup)\n"
banner
RC=0
exit 0
