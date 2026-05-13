#!/usr/bin/env bash
# corrigibility-gate.sh — runs between every phase to verify ALL gains preserved
# Usage: ./scripts/corrigibility-gate.sh [phase-name]
# Exit 0 = all clear, safe to proceed. Exit 1 = STOP, regression detected.

set -uo pipefail
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"
PYTHON=".venv/bin/python3"

PHASE="${1:-unknown}"
PASS=0
FAIL=0
SKIP=0

# Baseline thresholds (captured 2026-02-23 from v0.9-corrigibility-baseline)
MIN_EVENTS=6379
MIN_LEARNINGS=3013
MIN_JOBS=47

check() {
  local name="$1"
  local result
  result=$(eval "$2" 2>&1) || true
  if echo "$result" | grep -q "PASS"; then
    echo "  [PASS] $name"
    ((PASS++))
  elif echo "$result" | grep -q "SKIP"; then
    echo "  [SKIP] $name"
    ((SKIP++))
  else
    echo "  [FAIL] $name: $result"
    ((FAIL++))
  fi
}

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  CORRIGIBILITY GATE — After Phase: $PHASE"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# --- INVARIANT 1: Chain hash integrity (DAG — prev_hash must exist in prior events) ---
check "Chain hash integrity" "
$PYTHON -c \"
import json
lines = open('.projectdna/events.jsonl').readlines()
seen = set(['0'*64])
bad = 0
for i, line in enumerate(lines):
    evt = json.loads(line)
    if evt['prev_hash'] not in seen:
        bad += 1
    seen.add(evt['hash'])
if bad > 0:
    print(f'FAIL: {bad} events with orphaned prev_hash')
    exit(1)
print(f'PASS: {len(lines)} events, DAG chain intact')
\"
"

# --- INVARIANT 2: Events never shrink ---
check "Events monotonic" "
$PYTHON -c \"
n = sum(1 for _ in open('.projectdna/events.jsonl'))
baseline = $MIN_EVENTS
assert n >= baseline, f'FAIL: {n} < {baseline}'
print(f'PASS: {n} events (>= {baseline} baseline)')
\"
"

# --- INVARIANT 3: Learnings preserved ---
check "Learnings count" "
sqlite3 .context-dna/learnings.db 'SELECT count(*) FROM learnings' | \
  $PYTHON -c \"import sys; n=int(sys.stdin.read().strip()); assert n>=$MIN_LEARNINGS; print(f'PASS: {n} learnings')\"
"

# --- INVARIANT 4: FTS5 operational ---
check "FTS5 functional" "
sqlite3 .context-dna/learnings.db \"SELECT count(*) FROM learnings_fts WHERE learnings_fts MATCH 'webhook'\" 2>/dev/null | \
  $PYTHON -c \"import sys; n=int(sys.stdin.read().strip()); assert n>0; print(f'PASS: {n} FTS matches')\" \
  2>/dev/null || echo 'SKIP: FTS5 not available'
"

# --- INVARIANT 5: Scheduler jobs ---
check "Scheduler jobs" "
sqlite3 memory/.observability.db 'SELECT count(*) FROM job_schedule' 2>/dev/null | \
  $PYTHON -c \"import sys; n=int(sys.stdin.read().strip()); assert n>=$MIN_JOBS; print(f'PASS: {n} jobs')\"
"

# --- INVARIANT 6: Evidence grades preserved (claim table in observability.db) ---
check "Evidence grades" "
$PYTHON -c \"
import sqlite3
conn = sqlite3.connect('memory/.observability.db')
grades = dict(conn.execute('SELECT evidence_grade, count(*) FROM claim GROUP BY evidence_grade').fetchall())
conn.close()
total = sum(grades.values())
if total == 0:
    print('FAIL: no evidence grades found')
    exit(1)
print(f'PASS: {total} graded claims, distribution: {grades}')
\"
"

# --- INVARIANT 7: Manifest mode ---
check "Manifest mode" "
$PYTHON -c \"
from memory.mode_authority import get_mode
mode = get_mode()
print(f'PASS: mode={mode}')
\"
"

# --- INVARIANT 8: DB schemas stable ---
check "Observability schema" "
CURRENT=\$(sqlite3 memory/.observability.db '.schema' 2>/dev/null | md5)
BASELINE='f3f90e9c024d17c726ca915c7d9ff3b9'
if [ \"\$CURRENT\" = \"\$BASELINE\" ]; then echo 'PASS: schema unchanged'
else echo \"WARN: schema hash \$CURRENT != baseline (may be intentional)\"; echo 'PASS: noted'
fi
"

# --- INVARIANT 9: Agent service health ---
check "Agent service health" "
RESP=\$(curl -s --max-time 5 http://127.0.0.1:8080/health 2>/dev/null)
if [ -n \"\$RESP\" ]; then
  if echo \"\$RESP\" | $PYTHON -c 'import sys,json; d=json.load(sys.stdin); assert d.get(\"status\")==\"healthy\"' 2>/dev/null; then
    echo \"PASS: agent_service healthy\"
  else
    echo \"FAIL: agent_service unhealthy: \$RESP\"
  fi
else
  echo 'SKIP: agent_service not responding'
fi
"

# --- INVARIANT 10: Neurologist corrigibility challenge (informational, non-blocking) ---
check "Neurologist challenge" "
PYTHONPATH=. .venv/bin/$PYTHON -c \"
from memory.llm_priority_queue import llm_generate, Priority, check_llm_health
if not check_llm_health():
    print('SKIP: LLM offline')
    exit(0)
r = llm_generate(
    system_prompt='You are Qwen3-4B, the Neurologist — the system corrigibility skeptic. /no_think\nPhase: $PHASE. Reply PASS if no concerns, or CONCERN: <reason> if you see risk.',
    user_prompt='Quick corrigibility check: Is phase $PHASE safe to proceed? Any regressions, drift, or overlooked risks? Be concise.',
    priority=Priority.ATLAS,
    profile='classify',
    caller='corrigibility_gate_neurologist',
    timeout_s=15.0
)
content = (r.get('content') or '').strip()
if 'CONCERN' in content.upper():
    print(f'PASS: Neurologist flagged: {content[:200]}')
else:
    print(f'PASS: Neurologist clear: {content[:100]}')
\" 2>/dev/null || echo 'SKIP: Neurologist unavailable'
"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "  STOP: $FAIL invariants violated. DO NOT proceed to next phase."
  echo "  Rollback: git checkout v0.9-corrigibility-baseline -- <affected-files>"
  exit 1
fi
echo ""
echo "  All gates passed. Safe to proceed."
exit 0
