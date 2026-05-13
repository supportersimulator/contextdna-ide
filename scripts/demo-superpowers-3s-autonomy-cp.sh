#!/usr/bin/env bash
# WW5 — Superpowers x 3-surgeons E2E autonomy demo, TT1 counter-probe edition.
#
# Same skill discovery + filter as the RR5 demo, but every `3s consensus`
# call carries `--counter-probe` so the engine itself runs the negation
# pass and emits the authoritative Verdict line (GENUINE / PARTIAL /
# NO-GENUINE-CONSENSUS / NO-SIGNAL). Demo trusts that verdict.
#
# This means ONE 3s invocation per skill (vs RR5's two), so cost should
# stay well under the $0.20 cap even with the negation pass.
#
# Constitutional: ZSF. A failed 3s call records the failure and continues.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLASSIFIER="${SCRIPT_DIR}/demo_superpowers_3s_classify.py"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/tmp/superpowers-3s-autonomy-${TIMESTAMP}-counter-probe.md"
RAW_DIR="/tmp/superpowers-3s-autonomy-${TIMESTAMP}-counter-probe-raw"
mkdir -p "${RAW_DIR}"

COST_CAP="${COST_CAP:-0.20}"
PER_CALL_TIMEOUT="${PER_CALL_TIMEOUT:-60}"  # +negation pass needs headroom
CACHE_ROOT="${SUPERPOWERS_CACHE:-${HOME}/.claude/plugins/cache/superpowers-marketplace}"

if command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout"
elif command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout"
else
  TIMEOUT_CMD=""
fi

run_with_timeout() {
  local out="$1"; shift
  if [[ -n "${TIMEOUT_CMD}" ]]; then
    "${TIMEOUT_CMD}" --kill-after=5 "${PER_CALL_TIMEOUT}" "$@" \
      > "${out}" 2>&1
    return $?
  fi
  "$@" > "${out}" 2>&1 &
  local pid=$!
  ( sleep "${PER_CALL_TIMEOUT}" && kill -9 "${pid}" 2>/dev/null ) &
  local watchdog=$!
  wait "${pid}"
  local rc=$?
  kill "${watchdog}" 2>/dev/null || true
  return ${rc}
}

# Match RR5's env exactly so the comparison is apples-to-apples.
export CONTEXT_DNA_NEURO_PROVIDER="${CONTEXT_DNA_NEURO_PROVIDER:-deepseek}"
# Engine-level gate ON for all calls. CLI flag also passed for belt-and-suspenders.
export CONTEXT_DNA_CONSENSUS_COUNTER_PROBE="${CONTEXT_DNA_CONSENSUS_COUNTER_PROBE:-on}"
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

echo "WW5 superpowers x 3s autonomy demo (counter-probe)"
echo "  cache:     ${CACHE_ROOT}"
echo "  cardio:    ${CARDIO_PROVIDER}"
echo "  neuro:     ${NEURO_PROVIDER}"
echo "  cp gate:   ${CONTEXT_DNA_CONSENSUS_COUNTER_PROBE}"
echo "  cost cap:  \$${COST_CAP}"
echo "  output:    ${OUT}"
echo

SKILLS_TSV="${RAW_DIR}/skills.tsv"
find "${CACHE_ROOT}" -name SKILL.md \
  | python3 "${CLASSIFIER}" filter > "${SKILLS_TSV}"

SKILL_COUNT="$(wc -l < "${SKILLS_TSV}" | tr -d ' ')"
echo "Filtered to ${SKILL_COUNT} brainstorming-class skills."
echo

{
  echo "# WW5 — Superpowers x 3-surgeons autonomy (counter-probe re-run)"
  echo
  echo "- **Timestamp**: ${TIMESTAMP}"
  echo "- **Cardio provider**: ${CARDIO_PROVIDER}"
  echo "- **Neuro provider**: ${NEURO_PROVIDER}"
  echo "- **Counter-probe gate**: ${CONTEXT_DNA_CONSENSUS_COUNTER_PROBE}"
  echo "- **Cost cap**: \$${COST_CAP}"
  echo "- **Skills probed**: ${SKILL_COUNT}"
  echo
  echo "## Per-skill verdicts"
  echo
  echo "Each skill is probed with \`3s consensus --counter-probe\`."
  echo "The engine itself runs the negation pass and emits an authoritative"
  echo "Verdict line. Demo trusts the engine: GENUINE -> AUTONOMOUS;"
  echo "PARTIAL / NO-GENUINE-CONSENSUS / NO-SIGNAL -> NEEDS-HUMAN."
  echo
  echo "| Plugin | Skill | Verdict | Engine | Pos | Neg | Cardio | Neuro | Cost |"
  echo "|---|---|---|---|---|---|---|---|---|"
} > "${OUT}"

TOTAL_AUTO=0
TOTAL_NEEDS=0
TOTAL_FAILED=0
TOTAL_COST="0.0"

while IFS=$'\t' read -r path name plugin prompt; do
  [[ -z "${name}" ]] && continue

  over_cap="$(python3 -c "print(1 if float('${TOTAL_COST}') >= float('${COST_CAP}') else 0)")"
  if [[ "${over_cap}" == "1" ]]; then
    echo "  [skip] ${name} — cost cap reached (${TOTAL_COST} >= ${COST_CAP})"
    {
      echo "| ${plugin} | ${name} | SKIPPED-CAP | - | - | - | - | - | - |"
    } >> "${OUT}"
    continue
  fi

  echo "  [probe] ${plugin}/${name}"

  raw_path="${RAW_DIR}/${plugin}__${name}.txt"
  set +e
  run_with_timeout "${raw_path}" \
    3s --cardio-provider "${CARDIO_PROVIDER}" \
       --neuro-provider "${NEURO_PROVIDER}" \
       consensus --counter-probe "${prompt}"
  rc=$?
  set -e
  if [[ ${rc} -ne 0 ]]; then
    echo "    (3s rc=${rc}; possible timeout >${PER_CALL_TIMEOUT}s)"
  fi

  classified="$(python3 "${CLASSIFIER}" classify-cp < "${raw_path}")"
  IFS=$'\t' read -r verdict score neg_score cost cardio neuro engine \
    <<<"${classified}"

  verdict="${verdict:-FAILED}"
  score="${score:-0.00}"
  neg_score="${neg_score:-0.00}"
  cost="${cost:-0.0000}"
  cardio="${cardio:-missing@0.00}"
  neuro="${neuro:-missing@0.00}"
  engine="${engine:-MISSING}"

  case "${verdict}" in
    AUTONOMOUS)  TOTAL_AUTO=$((TOTAL_AUTO+1)) ;;
    NEEDS-HUMAN) TOTAL_NEEDS=$((TOTAL_NEEDS+1)) ;;
    *)           TOTAL_FAILED=$((TOTAL_FAILED+1)) ;;
  esac

  TOTAL_COST="$(python3 -c "print(f'{float(\"${TOTAL_COST}\")+float(\"${cost}\"):.4f}')")"

  {
    echo "| ${plugin} | ${name} | ${verdict} | ${engine} | ${score} | ${neg_score} | ${cardio} | ${neuro} | \$${cost} |"
  } >> "${OUT}"

done < "${SKILLS_TSV}"

{
  echo
  echo "## Totals"
  echo
  echo "- AUTONOMOUS:  ${TOTAL_AUTO}"
  echo "- NEEDS-HUMAN: ${TOTAL_NEEDS}"
  echo "- FAILED:      ${TOTAL_FAILED}"
  echo "- **Total cost**: \$${TOTAL_COST}"
  echo
  echo "## Verdict mapping"
  echo
  echo "- **GENUINE**              -> AUTONOMOUS (real reasoning, surgeons flipped on negation)"
  echo "- **PARTIAL**              -> NEEDS-HUMAN (only one surgeon flipped)"
  echo "- **NO-GENUINE-CONSENSUS** -> NEEDS-HUMAN (sycophantic — both agreed both ways)"
  echo "- **NO-SIGNAL**            -> NEEDS-HUMAN (|score| < 0.5 after demotion)"
  echo "- **MISSING / unavailable** -> FAILED (no engine verdict, both surgeons down)"
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
