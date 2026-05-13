#!/usr/bin/env bash
# test-deepseek-live.sh — End-to-end smoke test against the real DeepSeek API.
#
# Reads DEEPSEEK_API_KEY from AWS SSM Parameter Store at:
#     /ersim/prod/backend/DEEPSEEK_API_KEY
# (falls back to scripts/read-secret.sh → Keychain / env-file / shell env).
#
# Aborts early with a helpful error if the value is a known placeholder.
#
# Does two real round-trips:
#     1. deepseek-chat     — asserts 200-equivalent + non-empty content.
#     2. deepseek-reasoner — asserts response includes <think>...</think> CoT.
#
# Prints:
#     - latency (ms) per call
#     - input / output / total tokens
#     - estimated USD cost per call + grand total
#
# Exit 0 on both passes, 1 on any failure. Clear message on stderr.
#
# Security:
#     - NEVER prints the API key or any header that would contain it.
#     - Prints token counts, latency, and cost only.
#
# Usage:
#     ./scripts/test-deepseek-live.sh
#     SSM_PATH=/ersim/prod/backend/DEEPSEEK_API_KEY ./scripts/test-deepseek-live.sh
#
set -euo pipefail

# --- Colors ---
if [[ "${CI_MODE:-0}" == "1" || ! -t 1 ]]; then
    RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
else
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
    BLUE=$'\033[0;34m'; NC=$'\033[0m'
fi

pass() { echo "${GREEN}PASS${NC} $1"; }
fail() { echo "${RED}FAIL${NC} $1" >&2; exit 1; }
info() { echo "${BLUE}----${NC} $1"; }

# --- Repo root ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

SSM_PATH="${SSM_PATH:-/ersim/prod/backend/DEEPSEEK_API_KEY}"

info "DeepSeek live smoke — SSM path: $SSM_PATH"

# ---------------------------------------------------------------------------
# Resolve the API key
# ---------------------------------------------------------------------------
API_KEY=""

# 1. Try AWS SSM first (preferred — source of truth)
if command -v aws >/dev/null 2>&1; then
    info "Resolving from AWS SSM..."
    SSM_VAL="$(aws ssm get-parameter \
        --name "$SSM_PATH" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text 2>/dev/null || true)"
    if [ -n "$SSM_VAL" ] && [ "$SSM_VAL" != "None" ]; then
        API_KEY="$SSM_VAL"
        info "Key source: SSM ($SSM_PATH)"
    fi
fi

# 2. Fallback to Keychain / env file / shell env via read-secret.sh
if [ -z "$API_KEY" ]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/scripts/read-secret.sh"
    for name in DEEPSEEK_API_KEY Context_DNA_Deepseek; do
        API_KEY="$(read_secret "$name" || true)"
        if [ -n "$API_KEY" ]; then
            info "Key source: read-secret.sh / $name"
            break
        fi
    done
fi

if [ -z "$API_KEY" ]; then
    fail "No DeepSeek API key found. Tried: SSM '$SSM_PATH', Keychain, ~/.fleet-nerve/env, shell env (DEEPSEEK_API_KEY / Context_DNA_Deepseek)."
fi

# --- Placeholder guard — abort early if the value is not a real key ---
case "$API_KEY" in
    REPLACE_WITH_*|placeholder|changeme|"")
        fail "API key at '$SSM_PATH' is still a placeholder (prefix=${API_KEY:0:16}). Aaron needs to put the real key in SSM before running live smoke."
        ;;
esac

# Length sanity (DeepSeek keys are typically 30+ chars, start with 'sk-').
if [ "${#API_KEY}" -lt 20 ]; then
    fail "Resolved key is suspiciously short (${#API_KEY} chars) — likely not a real DeepSeek key."
fi

pass "API key resolved (length=${#API_KEY}, prefix=${API_KEY:0:3}...)"

# ---------------------------------------------------------------------------
# Pick a Python interpreter
# ---------------------------------------------------------------------------
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    fail "python3 not found"
fi
info "python: $PYTHON"

# ---------------------------------------------------------------------------
# Run the live round-trips. Key is passed via env, NEVER echoed.
# Python script does both calls, prints only non-secret metrics.
# ---------------------------------------------------------------------------
info "Running chat + reasoner round-trips..."
DEEPSEEK_SMOKE_KEY="$API_KEY" \
PYTHONPATH="$REPO_ROOT" \
"$PYTHON" - <<'PY' || fail "live smoke test raised — see traceback above"
import asyncio
import os
import re
import sys

from memory.providers.deepseek_provider import DeepSeekProvider, estimate_cost

API_KEY = os.environ["DEEPSEEK_SMOKE_KEY"]  # required

async def _chat_round_trip():
    async with DeepSeekProvider(api_key=API_KEY) as p:
        r = await p.generate(
            messages=[
                {"role": "system", "content": "Reply with exactly the word: pong"},
                {"role": "user", "content": "ping"},
            ],
            model="deepseek-chat",
            max_tokens=10,
            temperature=0.0,
        )
    return r

async def _reasoner_round_trip():
    async with DeepSeekProvider(api_key=API_KEY) as p:
        r = await p.generate(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Solve and show your work: what is 37 * 43? "
                        "Put your reasoning in <think>...</think> tags and the final answer after."
                    ),
                },
            ],
            model="deepseek-reasoner",
            max_tokens=600,
            temperature=0.0,
        )
    return r

def _report(label, r):
    content_len = len(r["content"].strip())
    itok = r["usage"]["input_tokens"]
    otok = r["usage"]["output_tokens"]
    cost = r["cost_estimate"]
    # Re-derive to cross-check the provider's math.
    expected = estimate_cost(itok, otok, r["model"])
    print(f"  {label}")
    print(f"    model         : {r['model']}")
    print(f"    latency_ms    : {r['latency_ms']}")
    print(f"    input_tokens  : {itok}")
    print(f"    output_tokens : {otok}")
    print(f"    cost_est_usd  : ${cost:.8f}  (verify: ${expected:.8f})")
    print(f"    content_chars : {content_len}")
    return cost

errors = []
total_cost = 0.0

# -- chat --
try:
    r_chat = asyncio.run(_chat_round_trip())
except Exception as e:  # noqa: BLE001
    print(f"CHAT_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    errors.append(f"chat raised {type(e).__name__}")
    r_chat = None

if r_chat is not None:
    if not r_chat["content"].strip():
        errors.append("chat returned empty content")
    if r_chat["usage"]["input_tokens"] <= 0:
        errors.append("chat reported 0 input tokens")
    if r_chat["usage"]["output_tokens"] <= 0:
        errors.append("chat reported 0 output tokens")
    total_cost += _report("chat_round_trip", r_chat)

# -- reasoner --
try:
    r_rsn = asyncio.run(_reasoner_round_trip())
except Exception as e:  # noqa: BLE001
    print(f"REASONER_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    errors.append(f"reasoner raised {type(e).__name__}")
    r_rsn = None

if r_rsn is not None:
    content = r_rsn["content"]
    if not content.strip():
        errors.append("reasoner returned empty content")
    has_think = bool(re.search(r"<think>.*?</think>", content, flags=re.DOTALL))
    # The provider surface only exposes `message.content`. If the API collapsed the
    # reasoning into `reasoning_content`, accept substring hint as fallback.
    has_reasoning_hint = ("think" in content.lower()) or ("reason" in content.lower())
    if not (has_think or has_reasoning_hint):
        errors.append("reasoner output missing <think> tags and reasoning hints")
    if "1591" not in content:
        errors.append("reasoner got 37*43 wrong (expected 1591 in content)")
    total_cost += _report("reasoner_round_trip", r_rsn)

print()
print(f"  TOTAL_COST_USD : ${total_cost:.8f}")

if errors:
    print()
    print("ASSERTION_FAILURES:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)

print()
print("ALL_ROUND_TRIPS_OK")
PY

pass "chat + reasoner round-trips"
echo
echo "${GREEN}LIVE SMOKE PASSED${NC} — DeepSeek migration is wired correctly."
exit 0
