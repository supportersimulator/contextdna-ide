#!/usr/bin/env bash
# aaron-actions-unblock.sh — WaveO single source-of-truth Aaron-action sheet.
# Root cause: WaitingOnAaron items fragmented across 20+ Wave audits.
# Features: dep-ordered, idempotent, --dry-run default, --apply, --only N, ZSF.
# Source:    .fleet/audits/2026-05-12-WaveE-bigpicture-plan-alignment.md
# Companion: .fleet/audits/2026-05-12-WaveO-aaron-action-queue-unblocker-root-cause.md

set -u
# NOTE: no `set -e` — each step must surface SKIP/OK/ABORT-ASK explicitly.

REPO="/Users/aarontjomsland/dev/er-simulator-superrepo"
MODE="dry-run"
ONLY=""
LOG="/tmp/aaron-actions-unblock-$(date +%Y%m%d-%H%M%S).log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)   MODE="apply"; shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --only)    ONLY="${2:-}"; shift 2 ;;
    --help|-h) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ----- output helpers --------------------------------------------------------
if [[ -t 1 ]]; then RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; CLR=$'\033[0m'
else RED=""; GRN=""; YLW=""; CYN=""; CLR=""; fi

STEP_NO=0; STEP_TOTAL=13
COUNT_OK=0; COUNT_SKIP=0; COUNT_ABORT=0; COUNT_FAIL=0

banner() { printf "\n${CYN}=== %s ===${CLR}\n" "$*" | tee -a "$LOG"; }
say()    { printf "%s\n" "$*" | tee -a "$LOG"; }

step() {  # step "NAME"  — opens a step (STEP_NO already advanced by should_run)
  printf "${CYN}[%s]${CLR} %s ... " "${CUR_TAG:-?/?}" "$1" | tee -a "$LOG"
}
ok()       { COUNT_OK=$((COUNT_OK + 1));       printf "${GRN}OK${CLR} (%s)\n"       "$*" | tee -a "$LOG"; }
skip()     { COUNT_SKIP=$((COUNT_SKIP + 1));   printf "${YLW}SKIP${CLR} (%s)\n"     "$*" | tee -a "$LOG"; }
abort()    { COUNT_ABORT=$((COUNT_ABORT + 1)); printf "${YLW}ABORT-ASK${CLR} (%s)\n" "$*" | tee -a "$LOG"; }
fail()     { COUNT_FAIL=$((COUNT_FAIL + 1));   printf "${RED}FAIL${CLR} (%s)\n"     "$*" | tee -a "$LOG"; }

# Should this step actually run? (--only filter). Increments STEP_NO either way
# so the filter compares against the canonical step number, not 0.
should_run() {
  STEP_NO=$((STEP_NO + 1))
  if [[ -z "$ONLY" || "$ONLY" == "$STEP_NO" ]]; then
    CUR_TAG="${STEP_NO}/${STEP_TOTAL}"
    return 0
  fi
  return 1
}

# Apply guard — in dry-run, no mutation runs. Returns 0 if mutation allowed.
allow_apply() { [[ "$MODE" == "apply" ]]; }

# ----- Already-Done header --------------------------------------------------
banner "WaveO Aaron-Action Unblocker — mode=$MODE  log=$LOG"

cat <<EOF | tee -a "$LOG"
Already-shipped this week (no Aaron action needed — verified by Wave audits):
  - admin.contextdna.io @ origin/main, 0 ahead    (KK3 monetization stub on origin)
  - landing-page         @ origin/main, 0 ahead   (MM4 signup/login/dashboard scaffolded)
  - superrepo            push-freeze thawed       (NN5 verification)
  - DeepSeek primary bridge live on :8855          (BBB3 7/7 tests PASS)
  - multifleet plist drift sentinel               (M3 shipped)
  - NATS JS replicas + quorum self-heal           (N3, O4 shipped)
  - Permission Governor allowlist (UU3 R3)        (shipped)
  - Fleet auto-heal M1-M5                         (5/5 per WW8; M2 disputed)
  - v3-priorities R1-R5                           (5/5 per WW8)
Aaron actions REMAINING after this script: see step list below.
EOF

# ============================================================================
# Step 1 — mac2 connectivity preflight (read-only, fails-loud)
# ============================================================================
if should_run; then
  step "preflight: mac2 fleet daemon /health"
  if curl -sf -m 2 http://127.0.0.1:8855/health >/dev/null 2>&1; then
    ok "daemon reachable"
  else
    fail "daemon unreachable — fix WW7-C/E wedge first (P0 blocker for downstream steps)"
  fi
fi

# ============================================================================
# Step 2 — AWS IAM rotation (MM1) — SECURITY, must precede secret-dependent steps
# ============================================================================
if should_run; then
  step "AWS IAM key rotation (delete AKIA4DIQ...FGMC)"
  abort "Aaron-only: AWS console action. See MM1 audit. Rotate before #11 (PyPI) which may need fresh creds."
fi

# ============================================================================
# Step 3 — Cloudflare token rotation (MM1)
# ============================================================================
if should_run; then
  step "Cloudflare token rotation"
  abort "Aaron-only: Cloudflare dashboard action. Companion to #2."
fi

# ============================================================================
# Step 4 — KV orphan delete: aarons-mbp (TT5 risk #1)
# ============================================================================
if should_run; then
  step "KV orphan delete: aarons-mbp from fleet-state"
  if grep -q "aarons-mbp" "$REPO/fleet-state.json" 2>/dev/null; then
    if allow_apply; then
      python3 - <<'PY' >>"$LOG" 2>&1 && ok "removed from fleet-state.json" || fail "delete failed (see log)"
import json, pathlib
p = pathlib.Path("/Users/aarontjomsland/dev/er-simulator-superrepo/fleet-state.json")
d = json.loads(p.read_text())
for k in ("nodes", "peers"):
    if isinstance(d.get(k), dict) and "aarons-mbp" in d[k]:
        del d[k]["aarons-mbp"]
p.write_text(json.dumps(d, indent=2) + "\n")
PY
    else
      abort "dry-run: would remove aarons-mbp"
    fi
  else
    skip "aarons-mbp not present in fleet-state.json"
  fi
fi

# ============================================================================
# Step 5 — Tailscale duplicate node-key delete (VV5)
# ============================================================================
if should_run; then
  step "Tailscale duplicate node-key cleanup"
  abort "Aaron-only: tailscale admin console. Inspect duplicates: 'tailscale status | grep -i offline'"
fi

# ============================================================================
# Step 6 — Install Tailscale on mac1 (mac1 absent from tailnet)
# ============================================================================
if should_run; then
  step "Tailscale enrollment on mac1"
  abort "Aaron-only: needs physical/SSH access to mac1 + 'tailscale up' interactive auth. mac1 currently absent from tailnet (verified)."
fi

# ============================================================================
# Step 7 — MLX routing for mac2 → mac3 (WW7-C; mac2 is Intel x86_64, no MLX)
# ============================================================================
if should_run; then
  step "MLX routing: mac2 → mac3 over LAN"
  # Look for routing env in launchd plists or proxy config
  if grep -q "MLX_REMOTE_HOST\|mlx.*mac3" "$REPO/tools/launch-llm-proxy.sh" 2>/dev/null; then
    skip "MLX remote routing already configured"
  else
    abort "Engineering follow-on, not Aaron-only paste. Defer to next agent wave (WW7-C). Routes mac2 LLM calls to mac3."
  fi
fi

# ============================================================================
# Step 8 — Push-freeze thaw verify (HH1)
# ============================================================================
if should_run; then
  step "push-freeze thaw verify (superrepo + 2 submodules)"
  AHEAD=0
  for d in "$REPO" "$REPO/admin.contextdna.io" "$REPO/landing-page"; do
    if [[ -d "$d/.git" || -f "$d/.git" ]]; then
      n=$(cd "$d" && git rev-list --count @{u}..HEAD 2>/dev/null || echo 0)
      AHEAD=$((AHEAD + n))
    fi
  done
  if [[ "$AHEAD" -eq 0 ]]; then
    ok "0 unpushed commits (freeze stays thawed per NN5)"
  else
    abort "$AHEAD unpushed commits — run 'git push origin main' in each repo manually"
  fi
fi

# ============================================================================
# Step 9 — Flip neuro DeepSeek cutover bit (PP3) — depends on AWS rotation (#2)
#          for clean credential state, but functionally independent.
# ============================================================================
if should_run; then
  step "neuro DeepSeek cutover bit (PP3)"
  if [[ ! -x "$REPO/scripts/patch-neuro-cutover.py" && ! -f "$REPO/scripts/patch-neuro-cutover.py" ]]; then
    fail "scripts/patch-neuro-cutover.py missing — runbook prerequisite broken"
  else
    # Check whether enable is already applied
    if launchctl list 2>/dev/null | grep -q "io.contextdna.fleet-nats\|io.contextdna.fleet-nerve" \
       && (launchctl print "gui/$(id -u)/io.contextdna.fleet-nerve" 2>/dev/null | grep -q "CONTEXT_DNA_NEURO_PROVIDER = deepseek"); then
      skip "CONTEXT_DNA_NEURO_PROVIDER=deepseek already set in fleet-nerve plist"
    elif allow_apply; then
      if python3 "$REPO/scripts/patch-neuro-cutover.py" --enable >>"$LOG" 2>&1; then
        ok "patcher applied; restart with 'bash scripts/sync-node-config.sh --restart'"
      else
        fail "patch-neuro-cutover.py exited non-zero (see log)"
      fi
    else
      abort "dry-run: would run patch-neuro-cutover.py --enable"
    fi
  fi
fi

# ============================================================================
# Step 10 — Cardio default preset flip (WaveE finding) — hybrid.yaml OpenAI→DeepSeek
# ============================================================================
if should_run; then
  step "cardio default preset (hybrid.yaml → DeepSeek)"
  HY="$REPO/3-surgeons/config/presets/hybrid.yaml"
  if [[ ! -f "$HY" ]]; then
    fail "hybrid.yaml missing"
  elif ! grep -q "provider: openai" "$HY"; then
    skip "hybrid.yaml already DeepSeek-primary"
  elif allow_apply; then
    cp "$HY" "$HY.bak.$(date +%s)"
    python3 - "$HY" <<'PY' >>"$LOG" 2>&1 && ok "hybrid.yaml flipped (backup saved)" || fail "yaml flip failed"
import sys, pathlib, re
p = pathlib.Path(sys.argv[1])
t = p.read_text()
# Conservative replace inside cardiologist block — leaves comments intact
t = re.sub(r'(cardiologist:.*?)provider: openai',  r'\1provider: deepseek', t, count=1, flags=re.S)
t = re.sub(r'(cardiologist:.*?)endpoint: https://api\.openai\.com/v1',
           r'\1endpoint: https://api.deepseek.com/v1', t, count=1, flags=re.S)
t = re.sub(r'(cardiologist:.*?)model: gpt-4\.1-mini',
           r'\1model: deepseek-chat', t, count=1, flags=re.S)
t = re.sub(r'(cardiologist:.*?)api_key_env: Context_DNA_OPENAI',
           r'\1api_key_env: Context_DNA_Deepseek', t, count=1, flags=re.S)
p.write_text(t)
PY
  else
    abort "dry-run: would flip cardiologist block in hybrid.yaml to DeepSeek"
  fi
fi

# ============================================================================
# Step 11 — PyPI multifleet 5.2.0 upload (PP5) — depends on AWS rotation (#2)
# ============================================================================
if should_run; then
  step "PyPI multifleet 5.2.0 upload"
  LATEST=$(pip index versions multifleet 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [[ "$LATEST" == "5.2.0" || "$LATEST" > "5.2.0" ]]; then
    skip "PyPI already at $LATEST"
  else
    abort "Aaron-only: requires PyPI token. See docs/runbooks/multifleet-pypi-publish.md (5 min). Latest on PyPI: ${LATEST:-unknown}"
  fi
fi

# ============================================================================
# Step 12 — Run revenue-thaw runbook (NN5 + OO1) — DEPENDS on #2, #3 (secrets rotated)
# ============================================================================
if should_run; then
  step "revenue-thaw runbook (CMI + Hire-Panel + Path B)"
  abort "Aaron-only: paste 'bash <(sed -n /\\\`\\\`\\\`/,/\\\`\\\`\\\`/p docs/runbooks/aaron-revenue-thaw.md)' OR run sections manually. 5-10 min. Highest leverage in queue."
fi

# ============================================================================
# Step 13 — HHH1 channel_scoring canary promotion to mac1 (post-WaveH data)
# ============================================================================
if should_run; then
  step "HHH1 channel_scoring promotion mac1"
  abort "Engineering follow-on; needs WaveH canary data first. Promote with: 'bash scripts/promote-channel-scoring.sh --node mac1' once canary green."
fi

# ----- summary --------------------------------------------------------------
banner "Summary (mode=$MODE)"
printf "  ${GRN}OK${CLR}=%d  ${YLW}SKIP${CLR}=%d  ${YLW}ABORT-ASK${CLR}=%d  ${RED}FAIL${CLR}=%d   (of %d)\n" \
  "$COUNT_OK" "$COUNT_SKIP" "$COUNT_ABORT" "$COUNT_FAIL" "$STEP_TOTAL" | tee -a "$LOG"
say "Log: $LOG"

if [[ "$COUNT_FAIL" -gt 0 ]]; then
  say "${RED}ZSF: at least one step FAIL'd. Investigate before proceeding.${CLR}"
  exit 1
fi
if [[ "$MODE" == "dry-run" ]]; then
  say "Dry-run complete. Re-run with --apply to execute idempotent steps."
fi
exit 0
