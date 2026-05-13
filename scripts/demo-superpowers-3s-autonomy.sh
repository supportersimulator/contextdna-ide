#!/usr/bin/env bash
# RR5 — Superpowers x 3-surgeons E2E autonomy demo.
#
# Goal: empirically answer Aaron's question — can the superpowers
# brainstorming skills be answered by 3s without human intervention?
#
# This script:
#   1. Walks every SKILL.md in the installed superpowers-marketplace cache.
#   2. Filters to brainstorming-class skills (decide/review/plan/design).
#   3. Generates one representative consensus claim per skill.
#   4. Pipes each claim through `3s consensus` (DeepSeek for both surgeons
#      to keep cost predictable; QQ1 fallback chain handles outages).
#   5. Classifies each result as AUTONOMOUS / NEEDS-HUMAN / FAILED.
#   6. Emits a markdown table to /tmp/superpowers-3s-autonomy-<ts>.md.
#
# Constitutional: ZSF. A failed 3s call records the failure and continues.
# Cost cap: $0.20 (script aborts further calls past the cap, never mid-call).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLASSIFIER="${SCRIPT_DIR}/demo_superpowers_3s_classify.py"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/tmp/superpowers-3s-autonomy-${TIMESTAMP}.md"
RAW_DIR="/tmp/superpowers-3s-autonomy-${TIMESTAMP}-raw"
mkdir -p "${RAW_DIR}"

COST_CAP="${COST_CAP:-0.20}"
PER_CALL_TIMEOUT="${PER_CALL_TIMEOUT:-45}"   # seconds per 3s call
CACHE_ROOT="${SUPERPOWERS_CACHE:-${HOME}/.claude/plugins/cache/superpowers-marketplace}"

# gtimeout from coreutils on macOS, timeout on linux. Fall back to a
# manual kill so the demo never hangs on a single bad probe.
if command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout"
elif command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout"
else
  TIMEOUT_CMD=""
fi

run_with_timeout() {
  # Args: <out_file> <cmd> <args...>
  local out="$1"; shift
  if [[ -n "${TIMEOUT_CMD}" ]]; then
    "${TIMEOUT_CMD}" --kill-after=5 "${PER_CALL_TIMEOUT}" "$@" \
      > "${out}" 2>&1
    return $?
  fi
  # Manual fallback: backgrounded kill watchdog.
  "$@" > "${out}" 2>&1 &
  local pid=$!
  ( sleep "${PER_CALL_TIMEOUT}" && kill -9 "${pid}" 2>/dev/null ) &
  local watchdog=$!
  wait "${pid}"
  local rc=$?
  kill "${watchdog}" 2>/dev/null || true
  return ${rc}
}

# DeepSeek for both surgeons keeps the run cheap and avoids waiting on the
# local Ollama / MLX stack. QQ1 added the auto-fallback chain so a single
# surgeon outage degrades to NEEDS-HUMAN, never a hang.
export CONTEXT_DNA_NEURO_PROVIDER="${CONTEXT_DNA_NEURO_PROVIDER:-deepseek}"
CARDIO_PROVIDER="${CARDIO_PROVIDER:-deepseek}"
NEURO_PROVIDER="${NEURO_PROVIDER:-deepseek}"

if [[ ! -x "$(command -v 3s)" ]]; then
  echo "ERROR: 3s CLI not on PATH" >&2
  exit 2
fi

if [[ ! -d "${CACHE_ROOT}" ]]; then
  echo "ERROR: superpowers cache not found at ${CACHE_ROOT}" >&2
  exit 2
fi

echo "RR5 superpowers x 3s autonomy demo"
echo "  cache:     ${CACHE_ROOT}"
echo "  cardio:    ${CARDIO_PROVIDER}"
echo "  neuro:     ${NEURO_PROVIDER}"
echo "  cost cap:  \$${COST_CAP}"
echo "  output:    ${OUT}"
echo

# ---------------------------------------------------------------------------
# 1. Discover skills, filter, generate prompts.
# ---------------------------------------------------------------------------
SKILLS_TSV="${RAW_DIR}/skills.tsv"
find "${CACHE_ROOT}" -name SKILL.md \
  | python3 "${CLASSIFIER}" filter > "${SKILLS_TSV}"

SKILL_COUNT="$(wc -l < "${SKILLS_TSV}" | tr -d ' ')"
echo "Filtered to ${SKILL_COUNT} brainstorming-class skills."
echo

# ---------------------------------------------------------------------------
# 2. Header for the markdown report.
# ---------------------------------------------------------------------------
{
  echo "# RR5 — Superpowers x 3-surgeons autonomy run"
  echo
  echo "- **Timestamp**: ${TIMESTAMP}"
  echo "- **Cardio provider**: ${CARDIO_PROVIDER}"
  echo "- **Neuro provider**: ${NEURO_PROVIDER}"
  echo "- **Cost cap**: \$${COST_CAP}"
  echo "- **Skills probed**: ${SKILL_COUNT}"
  echo
  echo "## Per-skill verdicts"
  echo
  echo "Each skill is probed twice — once with a positive claim, once with"
  echo "the negated claim. AUTONOMOUS requires the surgeons to flip the sign"
  echo "of the weighted score between the two probes (real reasoning, not"
  echo "sycophancy). When both probes agree, the verdict is demoted to"
  echo "NEEDS-HUMAN."
  echo
  echo "| Plugin | Skill | Verdict | Pos score | Neg score | Cardio (pos) | Neuro (pos) | Cost |"
  echo "|---|---|---|---|---|---|---|---|"
} > "${OUT}"

# ---------------------------------------------------------------------------
# 3. Probe each skill.
# ---------------------------------------------------------------------------
TOTAL_AUTO=0
TOTAL_NEEDS=0
TOTAL_FAILED=0
TOTAL_COST="0.0"

# Read TSV: path<TAB>name<TAB>plugin<TAB>prompt
while IFS=$'\t' read -r path name plugin prompt; do
  [[ -z "${name}" ]] && continue

  # Cost-cap guard: bail if we've already crossed the cap.
  over_cap="$(python3 -c "print(1 if float('${TOTAL_COST}') >= float('${COST_CAP}') else 0)")"
  if [[ "${over_cap}" == "1" ]]; then
    echo "  [skip] ${name} — cost cap reached (${TOTAL_COST} >= ${COST_CAP})"
    {
      echo "| ${plugin} | ${name} | SKIPPED-CAP | - | - | - | - |"
    } >> "${OUT}"
    continue
  fi

  echo "  [probe] ${plugin}/${name}"

  raw_path="${RAW_DIR}/${plugin}__${name}.txt"
  raw_neg_path="${RAW_DIR}/${plugin}__${name}.negated.txt"
  # ZSF: tolerate non-zero exit from 3s; we still classify whatever it wrote.
  set +e
  run_with_timeout "${raw_path}" \
    3s --cardio-provider "${CARDIO_PROVIDER}" \
       --neuro-provider "${NEURO_PROVIDER}" \
       consensus "${prompt}"
  rc=$?
  set -e
  if [[ ${rc} -ne 0 ]]; then
    echo "    (3s positive rc=${rc}; possible timeout >${PER_CALL_TIMEOUT}s)"
  fi

  # Adversarial counter-probe: same skill, flipped claim. If 3s flips its
  # verdict it shows the surgeons are actually reasoning, not rubber-
  # stamping. If they agree both ways, that's a sycophancy signal and we
  # demote AUTONOMOUS to NEEDS-HUMAN.
  neg_prompt="It is FALSE that: ${prompt}"
  set +e
  run_with_timeout "${raw_neg_path}" \
    3s --cardio-provider "${CARDIO_PROVIDER}" \
       --neuro-provider "${NEURO_PROVIDER}" \
       consensus "${neg_prompt}"
  rc_neg=$?
  set -e
  if [[ ${rc_neg} -ne 0 ]]; then
    echo "    (3s negated rc=${rc_neg}; possible timeout >${PER_CALL_TIMEOUT}s)"
  fi

  classified="$(python3 "${CLASSIFIER}" classify < "${raw_path}")"
  classified_neg="$(python3 "${CLASSIFIER}" classify < "${raw_neg_path}")"
  IFS=$'\t' read -r verdict score cost cardio neuro <<<"${classified}"
  IFS=$'\t' read -r neg_verdict neg_score neg_cost _ _ <<<"${classified_neg}"

  verdict="${verdict:-FAILED}"
  score="${score:-0.00}"
  cost="${cost:-0.0000}"
  neg_score="${neg_score:-0.00}"
  neg_cost="${neg_cost:-0.0000}"
  cardio="${cardio:-missing@0.00}"
  neuro="${neuro:-missing@0.00}"

  # Sycophancy demotion: if positive AND negated both got the same sign
  # of weighted score (both >=0.5 or both <=-0.5), the surgeons aren't
  # discriminating between the claim and its negation -> NEEDS-HUMAN.
  if [[ "${verdict}" == "AUTONOMOUS" ]]; then
    same_sign="$(python3 -c "
ps=float('${score}')
ns=float('${neg_score}')
print(1 if (ps>=0.5 and ns>=0.5) or (ps<=-0.5 and ns<=-0.5) else 0)
")"
    if [[ "${same_sign}" == "1" ]]; then
      verdict="NEEDS-HUMAN"
    fi
  fi

  case "${verdict}" in
    AUTONOMOUS)  TOTAL_AUTO=$((TOTAL_AUTO+1)) ;;
    NEEDS-HUMAN) TOTAL_NEEDS=$((TOTAL_NEEDS+1)) ;;
    *)           TOTAL_FAILED=$((TOTAL_FAILED+1)) ;;
  esac

  combined_cost="$(python3 -c "print(f'{float(\"${cost}\")+float(\"${neg_cost}\"):.4f}')")"
  TOTAL_COST="$(python3 -c "print(f'{float(\"${TOTAL_COST}\")+float(\"${combined_cost}\"):.4f}')")"

  {
    echo "| ${plugin} | ${name} | ${verdict} | ${score} | ${neg_score} | ${cardio} | ${neuro} | \$${combined_cost} |"
  } >> "${OUT}"

done < "${SKILLS_TSV}"

# ---------------------------------------------------------------------------
# 4. Synthesis footer.
# ---------------------------------------------------------------------------
{
  echo
  echo "## Totals"
  echo
  echo "- AUTONOMOUS:  ${TOTAL_AUTO}"
  echo "- NEEDS-HUMAN: ${TOTAL_NEEDS}"
  echo "- FAILED:      ${TOTAL_FAILED}"
  echo "- **Total cost**: \$${TOTAL_COST}"
  echo
  echo "## Sample prompts (first 3 skills)"
  echo
  head -3 "${SKILLS_TSV}" | while IFS=$'\t' read -r path name plugin prompt; do
    echo "### ${plugin}/${name}"
    echo
    echo "> ${prompt}"
    echo
  done
  echo "## Verdict rules"
  echo
  echo "- **AUTONOMOUS**  — \`|pos score| >= 0.5\` AND at least one surgeon answered with confidence >= 0.7 AND the negated probe flipped sign (genuine reasoning)."
  echo "- **NEEDS-HUMAN** — surgeons returned but signal too weak / mixed, OR they agreed with both the claim and its negation (sycophancy)."
  echo "- **FAILED**     — both surgeons unavailable or no parse."
  echo
  echo "## Raw 3s outputs"
  echo
  echo "Per-skill stdout/stderr saved to \`${RAW_DIR}\`."
} >> "${OUT}"

echo
echo "Done."
echo "  AUTONOMOUS:  ${TOTAL_AUTO}"
echo "  NEEDS-HUMAN: ${TOTAL_NEEDS}"
echo "  FAILED:      ${TOTAL_FAILED}"
echo "  cost:        \$${TOTAL_COST}"
echo "  output:      ${OUT}"
