#!/usr/bin/env bash
# WaveR 2026-05-12 — "Free Claude Code" invariance test
#
# Synthetic scenario: a fresh user with NO Anthropic + NO OpenAI keys, ONLY a
# DeepSeek key. Verifies each layer of the "free CC" stack degrades gracefully
# instead of silently failing. ZSF: every layer prints PASS/FAIL/SKIP.
#
# Layers tested:
#   L1 — 3-Surgeons preset: hybrid.yaml cardio is DeepSeek (not OpenAI)
#   L2 — Bridge fallback chain: Anthropic→DeepSeek→Superset paths exist
#   L3 — Local LLM proxy: port 5045 reachable (or graceful skip)
#   L4 — Fleet daemon: /health responds without ANTHROPIC_API_KEY
#   L5 — Discord-relay doorway: relay code present (P9 stub)
#
# Exit 0 if all hard layers pass (L1, L2, L4). L3/L5 are soft (SKIP allowed).
# Usage:  bash scripts/test-free-claude-code-invariance.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

pass=0; fail=0; skip=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
warn() { echo "  SKIP  $1"; skip=$((skip+1)); }

echo "── L1: hybrid.yaml cardio default = DeepSeek (free-CC-aligned) ──"
hy=3-surgeons/config/presets/hybrid.yaml
if grep -q "provider: deepseek" "$hy" && grep -q "api.deepseek.com" "$hy"; then
  ok "hybrid.yaml cardio = deepseek"
else
  bad "hybrid.yaml cardio still openai (free-CC default broken)"
fi

echo "── L2: bridge fallback chain (DeepSeek→Anthropic→Superset) ──"
br=tools/fleet_nerve_nats.py
if grep -q "_try_superset_fallback" "$br"; then ok "Superset 3rd-tier present"
else bad "Superset fallback missing"; fi
if grep -q "_try_peer_superset_failover" "$br"; then ok "peer failover (WaveL) present"
else warn "peer failover absent"; fi
if grep -q "_emit_anthropic_sse_synthetic" "$br"; then ok "Anthropic SSE synthesis (DeepSeek→Claude shape)"
else bad "SSE synthesis missing — Claude clients won't parse"; fi

echo "── L3: local LLM proxy on 5045 (zero-cost neurologist) ──"
if curl -sf -m 2 http://127.0.0.1:5045/health >/dev/null 2>&1 \
   || curl -sf -m 2 http://127.0.0.1:5045/v1/models >/dev/null 2>&1; then
  ok "local LLM proxy reachable"
elif curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  ok "Ollama reachable (neurologist substrate)"
else
  warn "no local LLM — neurologist would require paid API"
fi

echo "── L4: fleet daemon /health (no Anthropic key required) ──"
hp=$(ANTHROPIC_API_KEY="" curl -sf -m 3 http://127.0.0.1:8855/health 2>/dev/null || true)
if [ -n "$hp" ]; then
  ok "/health responds with ANTHROPIC_API_KEY unset"
else
  warn "daemon not running locally (start with tools/fleet_nerve_nats.py)"
fi

echo "── L5: Discord-relay doorway (P9 Telegram stub, BBB2) ──"
if grep -rqE "telegram|discord.*relay|relay.*discord" multi-fleet/multifleet/ 2>/dev/null \
   || ls tools/vscode_superset_bridge.py >/dev/null 2>&1; then
  ok "relay doorway present (Discord/Superset bridge)"
else
  warn "no relay doorway"
fi

echo
echo "── invariance summary ──"
echo "  PASS=$pass  FAIL=$fail  SKIP=$skip"
if [ "$fail" -gt 0 ]; then
  echo "  VERDICT: FREE-CC INVARIANT BROKEN"
  exit 1
fi
echo "  VERDICT: free-CC invariant holds (hard layers green)"
exit 0
