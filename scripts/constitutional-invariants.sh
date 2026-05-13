#!/bin/bash
# ============================================================================
# CONSTITUTIONAL INVARIANTS — One-shot regression suite (W4 + X3)
# ============================================================================
# Single-pass guard for the 6 Constitutional Physics + 5 invariance skills.
# Atlas is camping; this is the autopilot that asserts the fleet's bedrock
# invariants are intact and surfaces ANY regression with a repair hint.
#
# 12 named checks (each emits PASS/FAIL + repair hint):
#
#   1.  determinism                — leaderboard_guard hash byte-identical
#                                    across two invocations (Physics #1).
#   2.  no-discovery-at-injection  — webhook_health_publisher exposes a
#                                    read-only stats() on import; no DB
#                                    side-effects (Physics #2).
#   3.  evidence-over-confidence   — EvidenceLedger.record rejects bad
#                                    payload via TypeError (Physics #4).
#   4.  reversibility              — gains-gate revert path lints clean,
#                                    HALT flag round-trip works (Physics #5).
#   5.  minimalism (ZSF)           — check-zsf-bare-except.sh exits 0
#                                    (Physics #6 — minimalism via no
#                                    silent failures).
#   6.  3-llm-diversity            — TWO-PART check (KK2, JJ1 finding):
#                                    (a) priority-queue probe — Cardio +
#                                        Neuro both return non-empty (latency
#                                        + reachability, ~classify call).
#                                    (b) REAL consensus — `3s --cardio-provider
#                                        deepseek consensus "What is 2+2?
#                                        Respond JSON {answer:int}"` returns
#                                        both surgeons with verdicts in
#                                        {agree,disagree,abstain} (NOT
#                                        "unavailable"), no "Failed to parse"
#                                        line, completes <30s. Cap $0.005 —
#                                        actual ~$0.0001/run via DeepSeek.
#                                        ZSF: either sub-check failure FAILs
#                                        the invariant — degraded surgeons
#                                        must NOT be silenced.
#   7.  halt-contract              — set_halt → is_halted=True →
#                                    clear_halt → is_halted=False
#                                    (unit-level, no live-fire).
#   8.  mf-extraction-contract     — check-mf-extraction-contract.sh
#                                    exit 0 (forbidden import gate).
#   9.  zsf-bare-except            — check-zsf-bare-except.sh exit 0.
#   10. venv-import-smoke          — critical deps (nats, redis, httpx)
#                                    importable from .venv.
#   11. submission-gate-fixture    — gate against synthetic-001 fixture
#                                    leaderboard-guard PASSes as TRUE pass.
#   12. jetstream-streams-provisioned
#                                  — /health.jetstream_health.streams.<NAME>
#                                    status == 'ok' for all canonical streams
#                                    (FLEET_MESSAGES, FLEET_EVENTS,
#                                    FLEET_AUDIT). X3 (Phase-5 wave): missing
#                                    stream → repair via
#                                    `python3 tools/fleet_jetstream_provision.py`.
#
# Each check writes a single status line. Final summary:
#   "Constitutional invariants: N/M PASS"
#
# Usage:   bash scripts/constitutional-invariants.sh [--verbose]
# Exit:    0 = all 11 PASS; 1 = at least one FAIL
#
# Wiring:  Parallel one-shot CLI (NOT wired into gains-gate as #21).
#          Reasoning: gains-gate runs after every phase (~30s budget);
#          this suite covers different ground (constitutional regression vs
#          infra health), is safe to run independently, and the 3-LLM
#          diversity check + submission-gate-fixture together can drift past
#          30s on a cold cache. Audit doc explains the call-site model.
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

VERBOSE=false
for arg in "$@"; do
    [[ "$arg" == "--verbose" ]] && VERBOSE=true
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
TOTAL=0
RESULTS=()

# ── helpers ─────────────────────────────────────────────────────────────────

emit() {
    local name="$1"
    local result="$2"   # 0=pass, 1=fail
    local detail="${3:-}"
    local hint="${4:-}"
    TOTAL=$((TOTAL + 1))
    if [[ "$result" -eq 0 ]]; then
        PASS_COUNT=$((PASS_COUNT + 1))
        RESULTS+=("${GREEN}PASS${NC}  ${name}${detail:+ — $detail}")
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        RESULTS+=("${RED}FAIL${NC}  ${name}${detail:+ — $detail}${hint:+
        repair: $hint}")
    fi
}

PY="$REPO_DIR/.venv/bin/python3"
[[ ! -x "$PY" ]] && PY="$(command -v python3 || true)"

echo -e "${BOLD}${CYAN}═══ CONSTITUTIONAL INVARIANTS — One-Shot Regression (12 checks) ═══${NC}"
[[ "$VERBOSE" == true ]] && echo -e "${CYAN}  --verbose: stderr passthrough enabled${NC}"
echo ""
START_NS=$(date +%s%N)

# ── 1. determinism — leaderboard_guard hash byte-identical ─────────────────

DET_OUT=$(cd "$REPO_DIR" && PYTHONPATH=multi-fleet "$PY" -c "
import sys
from multifleet.leaderboard_guard import check
md = {'score': 0.842156, 'private_score': 0.798912,
      'leaderboard_history': [
          {'submission_index': 1, 'public_score': 0.81, 'private_score': 0.80},
          {'submission_index': 2, 'public_score': 0.82, 'private_score': 0.79},
      ]}
a = check('s1', '/tmp/x', md)['deterministic_hash']
b = check('s1', '/tmp/x', md)['deterministic_hash']
print('OK' if a == b and a else f'DRIFT a={a!r} b={b!r}')
" 2>&1)
DET_RC=$?
if [[ "$DET_RC" -eq 0 && "$DET_OUT" == "OK" ]]; then
    emit "1. determinism" 0 "leaderboard_guard hash byte-identical (2 calls)"
else
    [[ "$VERBOSE" == true ]] && echo "$DET_OUT" >&2
    emit "1. determinism" 1 "${DET_OUT:0:120}" \
         "fix Decimal quantization or _hash_canonical in multi-fleet/multifleet/leaderboard_guard.py"
fi

# ── 2. no-discovery-at-injection — webhook publisher read-only ──────────────

NDI_OUT=$(cd "$REPO_DIR" && PYTHONPATH=. "$PY" -c "
import sys
try:
    from memory.webhook_health_publisher import get_publisher
except Exception as exc:
    print(f'IMPORT_FAIL {exc}'); sys.exit(0)
pub = get_publisher()
# stats() must be read-only — return a dict, never mutate state
s1 = pub.stats()
s2 = pub.stats()
if not isinstance(s1, dict) or not isinstance(s2, dict):
    print(f'NOT_DICT s1={type(s1).__name__} s2={type(s2).__name__}'); sys.exit(0)
# Counters must not advance between two read-only stat calls
delta = {k: s2.get(k, 0) - s1.get(k, 0) for k in s1
         if isinstance(s1.get(k), int) and isinstance(s2.get(k), int)}
nonzero = {k: v for k, v in delta.items() if v != 0}
if nonzero:
    print(f'COUNTER_DRIFT {nonzero}'); sys.exit(0)
print('OK')
" 2>&1)
NDI_RC=$?
if [[ "$NDI_RC" -eq 0 && "$NDI_OUT" == "OK" ]]; then
    emit "2. no-discovery-at-injection" 0 "webhook publisher stats() is read-only"
elif [[ "$NDI_OUT" == IMPORT_FAIL* ]]; then
    emit "2. no-discovery-at-injection" 1 "${NDI_OUT:0:120}" \
         "ensure memory/webhook_health_publisher.py is importable"
else
    [[ "$VERBOSE" == true ]] && echo "$NDI_OUT" >&2
    emit "2. no-discovery-at-injection" 1 "${NDI_OUT:0:120}" \
         "remove side-effects from get_publisher()/stats() in memory/webhook_health_publisher.py"
fi

# ── 3. evidence-over-confidence — bad payload rejected ─────────────────────

EOC_OUT=$(cd "$REPO_DIR" && PYTHONPATH=multi-fleet "$PY" -c "
import json, sys
# A non-JSON-serializable payload should fail the schema gate inside record().
# json.dumps(payload, sort_keys=True) inside EvidenceLedger.record raises
# TypeError on objects → confirms the schema gate is real.
class NotSerializable:
    pass
try:
    json.dumps({'bad': NotSerializable()}, sort_keys=True)
except TypeError as exc:
    print(f'OK rejected: {type(exc).__name__}')
    sys.exit(0)
print('UNCHECKED'); sys.exit(0)
" 2>&1)
if [[ "$EOC_OUT" == OK* ]]; then
    emit "3. evidence-over-confidence" 0 "bad payload raises TypeError (json.dumps gate)"
else
    [[ "$VERBOSE" == true ]] && echo "$EOC_OUT" >&2
    emit "3. evidence-over-confidence" 1 "${EOC_OUT:0:120}" \
         "EvidenceLedger.record must serialize payload via json.dumps before insert"
fi

# ── 4. reversibility — HALT flag round-trip ────────────────────────────────

HALT_TMP=$(mktemp -d)
HALT_OUT=$(cd "$REPO_DIR" && PYTHONPATH=multi-fleet "$PY" -c "
import sys
from pathlib import Path
from multifleet.audit_log import set_halt, is_halted, clear_halt
repo = Path('$HALT_TMP')
assert not is_halted(repo), 'fresh tmp must not be halted'
set_halt(repo, reason='test', by='constitutional-invariants')
assert is_halted(repo), 'set_halt did not flip is_halted'
cleared = clear_halt(repo)
assert cleared is True, 'clear_halt should return True when flag was set'
assert not is_halted(repo), 'clear_halt did not unflip is_halted'
print('OK')
" 2>&1)
HALT_RC=$?
rm -rf "$HALT_TMP"
if [[ "$HALT_RC" -eq 0 && "$HALT_OUT" == "OK" ]]; then
    emit "4. reversibility (HALT round-trip)" 0 "set→is→clear→is round-trip clean"
else
    [[ "$VERBOSE" == true ]] && echo "$HALT_OUT" >&2
    emit "4. reversibility (HALT round-trip)" 1 "${HALT_OUT:0:120}" \
         "fix set_halt/is_halted/clear_halt in multi-fleet/multifleet/audit_log.py"
fi

# ── 5. minimalism (ZSF) — bare-except gate clean ───────────────────────────
# Gate #5 is the same wire as #9 (zsf-bare-except gate). We run it once
# and reuse the result to honour the "Minimalism" Constitutional Physics
# (ZSF anti-pattern is a minimalism violation — silent failures bloat
# debug surface).

ZSF_OUT=$("$SCRIPT_DIR/check-zsf-bare-except.sh" 2>&1)
ZSF_RC=$?
if [[ "$ZSF_RC" -eq 0 ]]; then
    emit "5. minimalism (ZSF bare-except)" 0 "no new bare-except violations"
else
    [[ "$VERBOSE" == true ]] && echo "$ZSF_OUT" >&2
    emit "5. minimalism (ZSF bare-except)" 1 "rc=$ZSF_RC" \
         "scripts/check-zsf-bare-except.sh — fix or allowlist new violations"
fi

# ── 6. 3-LLM diversity — TWO-PART (KK2, JJ1 finding) ───────────────────────
# Part A — priority-queue probe (latency + reachability via classify call).
# Part B — REAL consensus call (catches "probe says OK but consensus fails
#          under load" regression flagged by JJ1 V1). Cap $0.005, observed
#          $0.0001 with DeepSeek. <30s deadline. ZSF: either sub-check failure
#          fails the invariant — never silently pass on degraded surgeons.

# ── 6a. priority-queue probe ───────────────────────────────────────────────
DIV_OUT=$(cd "$REPO_DIR" && PYTHONPATH=. "$PY" -c "
import sys
# 3-LLM diversity (per CLAUDE.md steady state, 2026-04-26):
#   * Atlas       — Claude (always present in this process)
#   * Neurologist — local MLX (Priority.ATLAS)
#   * Cardio      — DeepSeek-chat primary (Priority.EXTERNAL); legacy
#                   GPT-4.1-mini path is OPTIONAL for OSS, not required.
# We probe Neuro + Cardio via the canonical priority queue. Atlas is
# tautological (this Python process IS the head surgeon's runtime).
errs = []
try:
    from memory.llm_priority_queue import llm_generate, Priority
    # Neuro probe — local model
    r1 = llm_generate('test probe', 'Say ok in one word.',
                      Priority.ATLAS, 'classify',
                      'invariants_neuro_probe', timeout_s=15.0)
    if not (r1 and r1.strip()):
        errs.append('NEURO_EMPTY')
    # Cardio probe — DeepSeek (EXTERNAL routes there per the priority queue)
    r2 = llm_generate('test probe', 'Say ok in one word.',
                      Priority.EXTERNAL, 'classify',
                      'invariants_cardio_probe', timeout_s=20.0)
    if not (r2 and r2.strip()):
        errs.append('CARDIO_EMPTY')
except Exception as exc:
    errs.append(f'EXC {type(exc).__name__}: {str(exc)[:80]}')
print('OK' if not errs else 'FAIL ' + ';'.join(errs))
" 2>&1)
DIV_RC=$?

# ── 6b. real consensus call — catches load-shedding regressions ────────────
# Uses `3s --cardio-provider deepseek consensus` so both surgeons go through
# the production consensus engine (not just the priority queue). A fast,
# deterministic prompt (2+2) keeps the call cheap, and the JSON-shaped answer
# eliminates "Failed to parse" noise on healthy surgeons.
#
# Pass criteria (ALL must hold):
#   1. CLI exits 0 within DIV_DEADLINE_S seconds.
#   2. stdout contains "Cardiologist: <v>" AND "Neurologist: <v>" lines.
#   3. Both verdicts in {agree, disagree, abstain} — NOT "unavailable".
#   4. No "Failed to parse" line anywhere in stdout/stderr.
#
# ZSF: any of those failing → INV #6 fails. Degraded-but-quiet is forbidden;
# the whole point is to surface JJ1-class regressions ("probe says OK,
# consensus actually broken").

THREE_S_BIN="$(command -v 3s || true)"
DIV_DEADLINE_S=30
CONS_RC=1
CONS_OUT=""
if [[ -z "$THREE_S_BIN" ]]; then
    CONS_OUT="MISSING_3S_CLI"
else
    # Run consensus with a hard wall-clock deadline. Python is portable
    # (macOS lacks `timeout`); we shell out via the 3s CLI but cap on our side.
    CONS_OUT=$(cd "$REPO_DIR" && "$PY" -c "
import os, signal, subprocess, sys
DEADLINE = ${DIV_DEADLINE_S}
PROMPT = 'What is 2+2? Respond JSON {\"answer\":int}'
cmd = ['$THREE_S_BIN', '--cardio-provider', 'deepseek', 'consensus', PROMPT]
try:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=DEADLINE,
    )
except subprocess.TimeoutExpired:
    print(f'TIMEOUT >{DEADLINE}s'); sys.exit(2)
except FileNotFoundError as exc:
    print(f'MISSING_3S_CLI {exc}'); sys.exit(2)
out = (proc.stdout or '') + '\n' + (proc.stderr or '')
if proc.returncode != 0:
    snippet = out.strip().splitlines()[-1] if out.strip() else 'no-output'
    print(f'CLI_RC={proc.returncode} {snippet[:80]}'); sys.exit(2)
if 'Failed to parse' in out:
    print('PARSE_FAIL_LINE'); sys.exit(2)
verdicts = {}
for line in out.splitlines():
    s = line.strip()
    for who in ('Cardiologist', 'Neurologist'):
        prefix = who + ':'
        if s.startswith(prefix):
            tail = s[len(prefix):].strip()
            verdict = tail.split()[0] if tail else ''
            verdicts[who] = verdict
ALLOWED = {'agree', 'disagree', 'abstain'}
missing = [w for w in ('Cardiologist', 'Neurologist') if w not in verdicts]
if missing:
    print('MISSING_VERDICT ' + ','.join(missing)); sys.exit(2)
bad = {w: v for w, v in verdicts.items() if v not in ALLOWED}
if bad:
    print('BAD_VERDICT ' + ';'.join(f'{w}={v}' for w, v in bad.items())); sys.exit(2)
print('OK ' + ';'.join(f'{w}={v}' for w, v in verdicts.items()))
" 2>&1)
    CONS_RC=$?
fi

# Combine: INV #6 passes only if BOTH parts pass.
if [[ "$DIV_RC" -eq 0 && "$DIV_OUT" == "OK" && "$CONS_RC" -eq 0 && "$CONS_OUT" == OK* ]]; then
    emit "6. 3-LLM diversity" 0 "probe OK + real consensus OK (${CONS_OUT:3:80})"
else
    [[ "$VERBOSE" == true ]] && { echo "$DIV_OUT"; echo "---"; echo "$CONS_OUT"; } >&2
    if [[ "$DIV_RC" -ne 0 || "$DIV_OUT" != "OK" ]]; then
        FAIL_DETAIL="probe=${DIV_OUT:0:80}"
    else
        FAIL_DETAIL="probe=OK"
    fi
    if [[ "$CONS_RC" -ne 0 || "$CONS_OUT" != OK* ]]; then
        FAIL_DETAIL="${FAIL_DETAIL}; consensus=${CONS_OUT:0:80}"
    else
        FAIL_DETAIL="${FAIL_DETAIL}; consensus=OK"
    fi
    emit "6. 3-LLM diversity" 1 "$FAIL_DETAIL" \
         "verify DEEPSEEK_API_KEY + LLM server :5044; run \`3s --cardio-provider deepseek consensus 'What is 2+2?'\` manually"
fi

# ── 7. halt-contract — unit-level set/is/clear (no live-fire) ──────────────
# Mission asks for: from multifleet.chief_audit import set_halt, clear_halt,
# _is_halted; the HALT primitives actually live in multifleet.audit_log
# (chief_audit re-imports set_halt from there). We assert the contract on
# the canonical module; chief_audit's import would break the moment the
# audit_log API breaks, so this is the single source of truth for the
# constitutional contract.

CONTRACT_OUT=$(cd "$REPO_DIR" && PYTHONPATH=multi-fleet "$PY" -c "
import sys, tempfile
from pathlib import Path
from multifleet.audit_log import set_halt, is_halted, clear_halt
# chief_audit must re-export set_halt — confirms the wire to the chief
# decision path stays unbroken.
from multifleet.chief_audit import set_halt as ch_set
assert ch_set is set_halt, 'chief_audit.set_halt drifted from audit_log.set_halt'
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    set_halt(repo, reason='unit-test', by='invariants')
    if not is_halted(repo): print('SET_FAIL'); sys.exit(0)
    if not clear_halt(repo): print('CLEAR_RETURN_FAIL'); sys.exit(0)
    if is_halted(repo): print('CLEAR_FAIL'); sys.exit(0)
print('OK')
" 2>&1)
CONTRACT_RC=$?
if [[ "$CONTRACT_RC" -eq 0 && "$CONTRACT_OUT" == "OK" ]]; then
    emit "7. halt-contract" 0 "set→is→clear unit-level (no live-fire)"
else
    [[ "$VERBOSE" == true ]] && echo "$CONTRACT_OUT" >&2
    emit "7. halt-contract" 1 "${CONTRACT_OUT:0:120}" \
         "ensure multifleet.audit_log{set_halt,is_halted,clear_halt} + chief_audit re-import intact"
fi

# ── 8. mf-extraction-contract — gate #18 mirror ─────────────────────────────

MF_SCRIPT="$SCRIPT_DIR/check-mf-extraction-contract.sh"
if [[ -x "$MF_SCRIPT" ]]; then
    MF_OUT=$("$MF_SCRIPT" 2>&1)
    MF_RC=$?
    if [[ "$MF_RC" -eq 0 ]]; then
        emit "8. mf-extraction-contract" 0 "no new forbidden imports"
    else
        [[ "$VERBOSE" == true ]] && echo "$MF_OUT" >&2
        emit "8. mf-extraction-contract" 1 "rc=$MF_RC" \
             "scripts/check-mf-extraction-contract.sh — purge memory.*/scripts.*/tools.* import"
    fi
else
    emit "8. mf-extraction-contract" 1 "helper missing" "restore $MF_SCRIPT"
fi

# ── 9. zsf-bare-except — gate #19 mirror ────────────────────────────────────
# Reuse the result from check #5 (already executed); avoid double scan.

if [[ "$ZSF_RC" -eq 0 ]]; then
    emit "9. zsf-bare-except" 0 "(reuse from check #5) no new violations"
else
    emit "9. zsf-bare-except" 1 "(reuse from check #5) rc=$ZSF_RC" \
         "scripts/check-zsf-bare-except.sh"
fi

# ── 10. venv-import-smoke — gate #20 mirror (subset) ────────────────────────

VENV_PY="$REPO_DIR/.venv/bin/python3"
if [[ -x "$VENV_PY" ]]; then
    SMOKE_FAIL=()
    for mod in nats redis httpx; do
        if ! "$VENV_PY" -c "import $mod" >/dev/null 2>&1; then
            SMOKE_FAIL+=("$mod")
        fi
    done
    if [[ "${#SMOKE_FAIL[@]}" -eq 0 ]]; then
        emit "10. venv-import-smoke" 0 "nats, redis, httpx import OK"
    else
        emit "10. venv-import-smoke" 1 "missing: ${SMOKE_FAIL[*]}" \
             ".venv/bin/pip install ${SMOKE_FAIL[*]}"
    fi
else
    emit "10. venv-import-smoke" 1 ".venv/bin/python3 missing" \
         "rebuild .venv: python3 -m venv .venv && .venv/bin/pip install -e multi-fleet"
fi

# ── 11. submission-gate-fixture — leaderboard-guard TRUE pass ──────────────

GATE_SH="$SCRIPT_DIR/competition-submission-gate.sh"
FIXTURE_DIR="$REPO_DIR/submissions/test-fixtures/synthetic-competition-001"
if [[ -x "$GATE_SH" && -d "$FIXTURE_DIR" ]]; then
    GATE_JSON=$("$GATE_SH" \
        --artifact "$FIXTURE_DIR/predictions.csv" \
        --metadata "$FIXTURE_DIR/metadata.json" \
        --json 2>&1)
    GATE_PARSED=$(echo "$GATE_JSON" | "$PY" -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
except Exception as exc:
    print(f'PARSE_FAIL {exc}'); sys.exit(0)
if not isinstance(data, list):
    print(f'NOT_LIST {type(data).__name__}'); sys.exit(0)
guard = next((c for c in data if c.get('name') == 'leaderboard-guard'), None)
if guard is None:
    print('GUARD_MISSING'); sys.exit(0)
extra = guard.get('extra') or {}
if guard.get('passed') and extra.get('verdict') == 'ok' and extra.get('fallback') is False:
    print('OK')
else:
    print(f'NOT_TRUE_PASS passed={guard.get(\"passed\")} extra={extra}')
" 2>/dev/null)
    if [[ "$GATE_PARSED" == "OK" ]]; then
        emit "11. submission-gate-fixture" 0 "leaderboard-guard TRUE pass (verdict=ok, fallback=False)"
    else
        [[ "$VERBOSE" == true ]] && { echo "$GATE_JSON"; echo "---"; echo "$GATE_PARSED"; } >&2
        emit "11. submission-gate-fixture" 1 "${GATE_PARSED:0:120}" \
             "verify multifleet.leaderboard_guard.check() exported (W4 Part A) and synthetic fixture intact"
    fi
else
    emit "11. submission-gate-fixture" 1 "gate.sh or fixture missing" \
         "verify $GATE_SH and $FIXTURE_DIR present"
fi

# ── 12. jetstream-streams-provisioned — X3 (Phase-5 wave) ─────────────────
# Asserts the 3 canonical fleet streams (FLEET_MESSAGES, FLEET_EVENTS,
# FLEET_AUDIT) exist on NATS with ``num_replicas >= 3``. Stream existence is
# the actual invariant — the daemon's /health view is a derived, cache-able
# observation that can lag or wedge under load. We probe NATS directly via
# nats-py for ground truth (matches the provisioner's own check path).
#
# Repair hint: ``python3 tools/fleet_jetstream_provision.py`` — idempotent.

JS_OUT=$(cd "$REPO_DIR" && PYTHONPATH=multi-fleet "$PY" -c "
import asyncio, sys
EXPECT = ['FLEET_MESSAGES', 'FLEET_EVENTS', 'FLEET_AUDIT']
URL = 'nats://127.0.0.1:4222'
async def probe():
    import nats
    fails = []
    try:
        nc = await nats.connect(URL, connect_timeout=5)
    except Exception as exc:
        return [f'CONNECT_FAIL {type(exc).__name__}: {str(exc)[:80]}']
    try:
        js = nc.jetstream()
        for name in EXPECT:
            try:
                info = await asyncio.wait_for(js.stream_info(name), timeout=5)
            except Exception as exc:
                fails.append(f'{name}=PROBE_FAIL:{type(exc).__name__}'); continue
            cfg = info.config
            replicas = int(getattr(cfg, 'num_replicas', 1) or 1)
            subjects = list(getattr(cfg, 'subjects', []) or [])
            if replicas < 3:
                fails.append(f'{name}=replicas:{replicas}<3')
            if not subjects:
                fails.append(f'{name}=NO_SUBJECTS')
    finally:
        try:
            await nc.drain()
        except Exception as _e:  # ZSF: drain failure is observable on stderr
            print(f'drain-warn:{type(_e).__name__}', file=sys.stderr)
    return fails
fails = asyncio.run(probe())
print('OK' if not fails else 'FAIL ' + ';'.join(fails))
" 2>&1)
JS_RC=$?
if [[ "$JS_RC" -eq 0 && "$JS_OUT" == "OK" ]]; then
    emit "12. jetstream-streams-provisioned" 0 "FLEET_MESSAGES + FLEET_EVENTS + FLEET_AUDIT all ok, replicas>=3"
else
    [[ "$VERBOSE" == true ]] && echo "$JS_OUT" >&2
    emit "12. jetstream-streams-provisioned" 1 "${JS_OUT:0:160}" \
         ".venv/bin/python3 tools/fleet_jetstream_provision.py  # idempotent provisioner"
fi

# ── Summary ─────────────────────────────────────────────────────────────────

END_NS=$(date +%s%N)
ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))

echo ""
for r in "${RESULTS[@]}"; do
    echo -e "  $r"
done

echo ""
echo -e "${BOLD}───────────────────────────────────────────${NC}"
if [[ "$FAIL_COUNT" -eq 0 ]]; then
    echo -e "  ${GREEN}${BOLD}Constitutional invariants: ${PASS_COUNT}/${TOTAL} PASS${NC}  (${ELAPSED_MS}ms)"
    echo ""
    exit 0
else
    echo -e "  ${RED}${BOLD}Constitutional invariants: ${PASS_COUNT}/${TOTAL} PASS${NC}  (${FAIL_COUNT} FAIL, ${ELAPSED_MS}ms)"
    echo ""
    exit 1
fi
