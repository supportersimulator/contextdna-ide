#!/usr/bin/env bash
# test_mlx_warm_active.sh — RACE AB3 verification
#
# Confirms that the local MLX server (Qwen3-4B) is warm and managed by launchd
# so the 3-surgeons neurologist can be a *truly distinct* LLM (not a deepseek
# fallback). This restores Constitutional Physics #5 (3 distinct LLMs).
#
# Checks:
#   1. plist template is committed (scripts/launchd/io.contextdna.mlx-warm.plist)
#   2. warm script is committed and executable (scripts/warm-mlx-on-boot.sh)
#   3. LaunchAgent is loaded (~/Library/LaunchAgents/io.contextdna.mlx-warm.plist)
#   4. launchctl reports the job
#   5. Port 5044 is bound and serves /v1/models
#   6. /v1/models lists at least one Qwen* model (or fallback id "default_model"
#      with a Qwen3 server fingerprint via /v1/chat/completions)
#   7. (optional) ~/.3surgeons/config.yaml points neurologist endpoint at 5044
#
# Exit 0 if all required checks pass. Exit 1 on first failure (with reason).
#
# Usage:
#   bash scripts/tests/test_mlx_warm_active.sh
#   bash scripts/tests/test_mlx_warm_active.sh --skip-config-check
set -uo pipefail

REPO_ROOT="${FLEET_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PLIST_TEMPLATE="$REPO_ROOT/scripts/launchd/io.contextdna.mlx-warm.plist"
WARM_SCRIPT="$REPO_ROOT/scripts/warm-mlx-on-boot.sh"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/io.contextdna.mlx-warm.plist"
MLX_HOST="127.0.0.1"
MLX_PORT="5044"
SKIP_CONFIG_CHECK=0

for arg in "$@"; do
    case "$arg" in
        --skip-config-check) SKIP_CONFIG_CHECK=1 ;;
    esac
done

pass() { printf "PASS: %s\n" "$1"; }
fail() { printf "FAIL: %s\n" "$1" >&2; exit 1; }

# --- 1. plist template ---
[[ -f "$PLIST_TEMPLATE" ]] || fail "plist template missing: $PLIST_TEMPLATE"
grep -q "io.contextdna.mlx-warm" "$PLIST_TEMPLATE" \
    || fail "plist template missing Label io.contextdna.mlx-warm"
pass "plist template present at $PLIST_TEMPLATE"

# --- 2. warm script ---
[[ -f "$WARM_SCRIPT" ]] || fail "warm script missing: $WARM_SCRIPT"
[[ -x "$WARM_SCRIPT" ]] || fail "warm script not executable: $WARM_SCRIPT"
pass "warm script present and executable"

# --- 3. LaunchAgent installed ---
[[ -f "$INSTALLED_PLIST" ]] || fail "LaunchAgent not installed at $INSTALLED_PLIST (run: cp $PLIST_TEMPLATE $INSTALLED_PLIST && launchctl load $INSTALLED_PLIST)"
pass "LaunchAgent installed"

# --- 4. launchctl reports the job ---
if ! launchctl list 2>/dev/null | grep -q "io.contextdna.mlx-warm"; then
    fail "launchctl does not list io.contextdna.mlx-warm (try: launchctl load $INSTALLED_PLIST)"
fi
pass "launchctl lists io.contextdna.mlx-warm"

# --- 5. port 5044 bound + /v1/models responds ---
MODELS_BODY="$(curl -sS -m 5 "http://${MLX_HOST}:${MLX_PORT}/v1/models" 2>/dev/null || true)"
if [[ -z "$MODELS_BODY" ]]; then
    fail "/v1/models returned empty/connection-refused on ${MLX_HOST}:${MLX_PORT}"
fi
pass "/v1/models responds on ${MLX_HOST}:${MLX_PORT}"

# --- 6. Qwen model is loaded (or default_model + Qwen3 fingerprint) ---
if echo "$MODELS_BODY" | grep -qi -E '"id"[[:space:]]*:[[:space:]]*"[^"]*qwen'; then
    pass "/v1/models lists a Qwen* model id"
else
    # mlx_lm.server may report id as "default_model" — verify Qwen3 via chat
    # endpoint's system_fingerprint or by hitting a short prompt.
    CHAT_BODY="$(curl -sS -m 30 -X POST "http://${MLX_HOST}:${MLX_PORT}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"messages":[{"role":"user","content":"hi /no_think"}],"max_tokens":5}' 2>/dev/null || true)"
    if [[ -z "$CHAT_BODY" ]]; then
        fail "/v1/models did not list Qwen and /v1/chat/completions failed"
    fi
    if ! echo "$CHAT_BODY" | grep -q "system_fingerprint"; then
        fail "chat completion missing system_fingerprint (server not OpenAI-compatible?)"
    fi
    # Confirm the underlying process command line references Qwen.
    if ! pgrep -fl mlx_lm.server 2>/dev/null | grep -qi qwen; then
        fail "no mlx_lm.server process serving a Qwen model found"
    fi
    pass "Qwen3 confirmed via mlx_lm.server process + chat endpoint"
fi

# --- 7. (optional) 3-surgeons config points at 5044 ---
if [[ "$SKIP_CONFIG_CHECK" -eq 0 ]] && [[ -f "$HOME/.3surgeons/config.yaml" ]]; then
    if grep -A2 "neurologist:" "$HOME/.3surgeons/config.yaml" \
        | grep -q "localhost:5044\|127.0.0.1:5044"; then
        pass "~/.3surgeons/config.yaml neurologist endpoint -> 5044 (truly distinct LLM)"
    else
        printf "WARN: ~/.3surgeons/config.yaml neurologist not on port 5044 — distinctness not enforced.\n" >&2
    fi
fi

echo ""
echo "All required checks passed. MLX warm-on-boot active; 3-distinct-LLM invariance restored."
exit 0
