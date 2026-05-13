#!/bin/bash
# =============================================================================
# Check 3-Surgeon Availability — quick health probe
# =============================================================================
# Usage: ./scripts/check-surgeons.sh [--json] [--quiet]
#
# Checks all 3 surgeons + infrastructure in ~3-5 seconds.
# Exit code: 0 = all surgeons OK, 1 = one or more down
#
# Flags:
#   --json    Output machine-readable JSON (for scripts/hooks)
#   --quiet   Suppress output, exit code only
#   --infra   Also check infrastructure (Redis, ports)
# =============================================================================

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python3"

# Parse flags
JSON=false
QUIET=false
INFRA=false
for arg in "$@"; do
    case "$arg" in
        --json)  JSON=true ;;
        --quiet) QUIET=true ;;
        --infra) INFRA=true ;;
    esac
done

# Colors (disabled for --json/--quiet)
if $QUIET || $JSON; then
    ok()   { :; }
    fail() { :; }
    warn() { :; }
    info() { :; }
    hdr()  { :; }
else
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
    ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
    fail() { echo -e "  ${RED}✗${NC} $1"; }
    warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
    info() { echo -e "  ${CYAN}→${NC} $1"; }
    hdr()  { echo -e "\n${BOLD}$1${NC}"; }
fi

FAILURES=0
NEURO_OK=false
CARDIO_OK=false
ATLAS_OK=true  # always present
NEURO_MS=0
CARDIO_MS=0
CARDIO_PROVIDER=unknown
CARDIO_MODEL=unknown
NEURO_ERR=""
CARDIO_ERR=""
INFRA_RESULTS=""

# ---- Surgeon 1: Neurologist (Qwen3-4B local) ----
hdr "3-Surgeon Availability Check"
hdr "Surgeon 1: Neurologist (Qwen3-4B local)"

NEURO_RESULT=$(cd "$REPO_ROOT" && PYTHONPATH=. "$PYTHON" -c "
import time, json
try:
    from memory.llm_priority_queue import llm_generate, Priority
    t0 = time.time()
    r = llm_generate('You are a test probe.', 'Say operational in one word.', Priority.ATLAS, 'classify', 'check_surgeons', timeout_s=10.0)
    ms = int((time.time() - t0) * 1000)
    if r and len(r.strip()) > 0:
        print(json.dumps({'ok': True, 'ms': ms, 'resp': r.strip()[:40]}))
    else:
        print(json.dumps({'ok': False, 'ms': ms, 'err': 'Empty response'}))
except Exception as e:
    print(json.dumps({'ok': False, 'ms': 0, 'err': str(e)[:100]}))
" 2>/dev/null)

if echo "$NEURO_RESULT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['ok'] else 1)" 2>/dev/null; then
    NEURO_OK=true
    NEURO_MS=$(echo "$NEURO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['ms'])" 2>/dev/null)
    ok "Neurologist — ${NEURO_MS}ms"
else
    NEURO_ERR=$(echo "$NEURO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('err','unknown'))" 2>/dev/null || echo "unreachable")
    fail "Neurologist — $NEURO_ERR"
    FAILURES=$((FAILURES + 1))
fi

# ---- Surgeon 2: Cardiologist (DeepSeek-chat primary, OpenAI fallback) ----
# ZZ5 2026-05-12: flipped from OpenAI-primary to DeepSeek-primary to match
# Aaron's 2026-04-18 cutover. OpenAI preserved as automatic fallback when
# the DeepSeek key is absent or its API errors out (ZSF — never silent: the
# probe emits an explicit 'provider' field so callers see which path won).
hdr "Surgeon 2: Cardiologist (DeepSeek-chat primary, OpenAI fallback)"

CARDIO_RESULT=$(cd "$REPO_ROOT" && PYTHONPATH=. "$PYTHON" -c "
import time, json, os
try:
    # Load .env if needed
    env_path = 'context-dna/.env'
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()
    from openai import OpenAI

    # DeepSeek primary — uses OpenAI-compatible API surface.
    ds_key = (os.environ.get('Context_DNA_Deepseek')
              or os.environ.get('Context_DNA_Deep_Seek')
              or os.environ.get('DEEPSEEK_API_KEY', ''))
    oa_key = (os.environ.get('Context_DNA_OPENAI')
              or os.environ.get('OPENAI_API_KEY', ''))

    def _try(provider, base_url, model, key, cost_in, cost_out):
        client = OpenAI(api_key=key, base_url=base_url)
        t0 = time.time()
        r = client.chat.completions.create(
            model=model, messages=[
                {'role': 'system', 'content': 'You are a test probe.'},
                {'role': 'user', 'content': 'Say operational in one word.'}
            ], max_tokens=32, timeout=15
        )
        ms = int((time.time() - t0) * 1000)
        content = r.choices[0].message.content.strip() if r.choices else ''
        cost = 0.0
        if r.usage:
            cost = (r.usage.prompt_tokens * cost_in
                    + r.usage.completion_tokens * cost_out) / 1_000_000
        return {'ok': True, 'ms': ms, 'resp': content[:40],
                'cost': round(cost, 6), 'provider': provider, 'model': model}

    # Try DeepSeek first.
    last_err = ''
    if ds_key and len(ds_key) >= 20:
        try:
            print(json.dumps(_try('deepseek',
                                  'https://api.deepseek.com/v1',
                                  'deepseek-chat', ds_key, 0.14, 0.28)))
            raise SystemExit(0)
        except Exception as e:
            last_err = f'deepseek: {str(e)[:80]}'
    # OpenAI fallback (ZSF — explicit, never silent).
    if oa_key and len(oa_key) >= 20:
        try:
            print(json.dumps(_try('openai',
                                  'https://api.openai.com/v1',
                                  'gpt-4.1-mini', oa_key, 0.4, 1.6)))
            raise SystemExit(0)
        except Exception as e:
            last_err = f'{last_err} | openai: {str(e)[:80]}' if last_err else f'openai: {str(e)[:80]}'
    err = last_err or 'no Context_DNA_Deepseek/OPENAI key present'
    print(json.dumps({'ok': False, 'ms': 0, 'err': err}))
except SystemExit:
    pass
except Exception as e:
    print(json.dumps({'ok': False, 'ms': 0, 'err': str(e)[:100]}))
" 2>/dev/null)

if echo "$CARDIO_RESULT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['ok'] else 1)" 2>/dev/null; then
    CARDIO_OK=true
    CARDIO_MS=$(echo "$CARDIO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['ms'])" 2>/dev/null)
    CARDIO_COST=$(echo "$CARDIO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('cost',0))" 2>/dev/null)
    CARDIO_PROVIDER=$(echo "$CARDIO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('provider','unknown'))" 2>/dev/null)
    CARDIO_MODEL=$(echo "$CARDIO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('model','unknown'))" 2>/dev/null)
    ok "Cardiologist [${CARDIO_PROVIDER}/${CARDIO_MODEL}] — ${CARDIO_MS}ms (\$${CARDIO_COST})"
else
    CARDIO_ERR=$(echo "$CARDIO_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('err','unknown'))" 2>/dev/null || echo "unreachable")
    fail "Cardiologist — $CARDIO_ERR"
    FAILURES=$((FAILURES + 1))
fi

# ---- Surgeon 3: Atlas (Claude Opus — always present) ----
hdr "Surgeon 3: Atlas/Claude Opus (Head Surgeon)"
ok "Present (running this check)"

# ---- Infrastructure (optional) ----
if $INFRA; then
    hdr "Infrastructure"

    REDIS="docker exec context-dna-redis redis-cli"

    # Redis
    REDIS_KEYS=$($REDIS DBSIZE 2>/dev/null | grep -oE '[0-9]+')
    if [ -n "$REDIS_KEYS" ]; then
        ok "Redis :6379 — ${REDIS_KEYS} keys"
        INFRA_RESULTS="${INFRA_RESULTS}redis:ok,"
    else
        fail "Redis :6379"
        INFRA_RESULTS="${INFRA_RESULTS}redis:down,"
        FAILURES=$((FAILURES + 1))
    fi

    # Service ports
    for entry in "5044:LLM Server" "8080:agent_service" "8888:Synaptic" "8029:ContextDNA API"; do
        PORT="${entry%%:*}"
        NAME="${entry#*:}"
        if curl -s --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
            ok "${NAME} :${PORT}"
            INFRA_RESULTS="${INFRA_RESULTS}${PORT}:ok,"
        else
            fail "${NAME} :${PORT}"
            INFRA_RESULTS="${INFRA_RESULTS}${PORT}:down,"
            FAILURES=$((FAILURES + 1))
        fi
    done
fi

# ---- Summary ----
if ! $QUIET && ! $JSON; then
    echo ""
    if [ $FAILURES -eq 0 ]; then
        echo -e "${GREEN}${BOLD}All surgeons operational.${NC}"
    else
        echo -e "${RED}${BOLD}${FAILURES} check(s) failed.${NC}"
    fi
fi

# ---- JSON output ----
if $JSON; then
    cat <<ENDJSON
{
  "surgeons": {
    "neurologist": {"ok": $NEURO_OK, "latency_ms": $NEURO_MS, "model": "Qwen3-4B-4bit"},
    "cardiologist": {"ok": $CARDIO_OK, "latency_ms": $CARDIO_MS, "model": "$CARDIO_MODEL", "provider": "$CARDIO_PROVIDER"},
    "atlas": {"ok": true, "model": "claude-opus"}
  },
  "all_ok": $([ $FAILURES -eq 0 ] && echo true || echo false),
  "failures": $FAILURES
}
ENDJSON
fi

exit $FAILURES
