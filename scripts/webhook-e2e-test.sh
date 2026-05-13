#!/bin/bash
# Webhook E2E Test (Cycle 6 F3)
# =============================================================================
# Verifies the Context DNA webhook injection flow end-to-end:
#   1. Snapshot /health.webhook.events_recorded BEFORE
#   2. Trigger producer (auto-memory-query.sh OR direct Python invocation)
#   3. Snapshot /health.webhook.events_recorded AFTER  → expect advance
#   4. Generate the 9-section injection in-process and verify each section
#      (S0,S1,S2,S3,S4,S5,S6,S7,S8,S10) returned non-empty content
#   5. Verify /tmp/webhook-publish.err did not gain new lines (ZSF check)
#   6. Verify webhook_publish_errors counter delta = 0
#
# Modes:
#   --quick : counters only, no producer trigger (last-known-good check)
#   --full  : trigger + per-section verification (DEFAULT)
#
# CLAUDE.md WEBHOOK = #1 PRIORITY. Broken webhook = Atlas blind. This test
# is the canary so regressions surface immediately.
#
# Per-section legend:
#   ✓ filled   → section returned non-empty content
#   ✗ empty    → section returned empty / placeholder ("no data") only
#   ? skipped  → section not attempted (config-disabled or short-prompt gate)
#
# Exit codes:
#   0  all sections filled, counter advanced, no errors
#   1  one or more sections empty (broken)
#   2  counter did not advance (publisher broken)
#   3  ZSF violation (publisher errors detected)
#   4  setup error (cannot reach daemon, etc.)
# =============================================================================

set -u  # nounset; do NOT set -e — we want full reporting

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
REPO_DIR="${CONTEXT_DNA_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
DAEMON_URL="${FLEET_DAEMON_URL:-http://127.0.0.1:8855}"
ERR_LOG="/tmp/webhook-publish.err"

# Use system python3 if venv missing (broken symlink seen on mac2 2026-05-04)
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    PYTHON="$REPO_DIR/.venv/bin/python"
elif [ -x "$REPO_DIR/.venv/bin/python3" ]; then
    PYTHON="$REPO_DIR/.venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

# Force /usr/bin/curl to bypass any shell aliases / rtk wrapping that produces
# a schema view instead of the JSON body.
CURL="/usr/bin/curl"

MODE="full"
TEST_PROMPT="${WEBHOOK_E2E_TEST_PROMPT:-ship the bridge load test and verify queue cap}"

# -----------------------------------------------------------------------------
# ARG PARSING
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --quick) MODE="quick"; shift ;;
        --full)  MODE="full"; shift ;;
        --prompt) shift; TEST_PROMPT="$1"; shift ;;
        --help|-h)
            echo "Usage: $0 [--quick|--full] [--prompt 'text']"
            echo ""
            echo "  --quick   Counter snapshot only (no producer trigger)"
            echo "  --full    Trigger producer + per-section verification (default)"
            echo "  --prompt  Override test prompt (must be >5 words)"
            exit 0
            ;;
        *) echo "unknown arg: $1"; exit 4 ;;
    esac
done

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
DIM=$'\033[2m'
NC=$'\033[0m'

log()   { printf '%s\n' "$*"; }
ok()    { printf '%s✓%s %s\n' "$GREEN" "$NC" "$*"; }
fail()  { printf '%s✗%s %s\n' "$RED"   "$NC" "$*"; }
warn()  { printf '%s?%s %s\n' "$YELLOW" "$NC" "$*"; }
hr()    { printf '%s\n' "----------------------------------------------------"; }

# Snapshot /health.webhook block. Returns JSON or "" on failure.
fetch_webhook_health() {
    "$CURL" -s --max-time 3 "${DAEMON_URL}/health" 2>/dev/null \
        | "$PYTHON" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(json.dumps(d.get('webhook', {})))
except Exception as e:
    sys.stderr.write(f'fetch_webhook_health parse failed: {e}\n')
    print('{}')
"
}

# Snapshot daemon stats block (for webhook_publish_errors — note: that
# counter lives in the publisher process, not the daemon. We surface it via
# the publisher CLI stats line below).
get_field() {
    local json="$1" path="$2"
    "$PYTHON" -c "
import sys, json
try:
    d = json.loads('''$json''')
    parts = '$path'.split('.')
    for p in parts:
        if isinstance(d, dict):
            d = d.get(p)
        else:
            d = None
            break
    print(d if d is not None else '')
except Exception as e:
    sys.stderr.write(f'get_field failed: {e}\n')
"
}

# -----------------------------------------------------------------------------
# PRE-FLIGHT
# -----------------------------------------------------------------------------
hr
log "Webhook E2E Test — mode=$MODE"
log "Repo:    $REPO_DIR"
log "Daemon:  $DAEMON_URL"
log "Python:  $PYTHON"
log "Prompt:  ${TEST_PROMPT:0:80}"
hr

# Word-count check — the ≤5 word gate would skip injection entirely.
WORDS=$(printf '%s' "$TEST_PROMPT" | wc -w | tr -d ' ')
if [ "$WORDS" -le 5 ]; then
    fail "Test prompt is only $WORDS words — would hit the ≤5 word bypass gate"
    fail "Use --prompt with a longer description (the test must exercise S0-S8)"
    exit 4
fi
ok "Prompt word count: $WORDS (>5 word gate cleared)"

# Daemon reachable?
if ! "$CURL" -sf --max-time 3 "${DAEMON_URL}/health" >/dev/null 2>&1; then
    fail "Cannot reach daemon at ${DAEMON_URL}/health"
    exit 4
fi
ok "Daemon reachable"

# -----------------------------------------------------------------------------
# SNAPSHOT BEFORE
# -----------------------------------------------------------------------------
WH_BEFORE=$(fetch_webhook_health)
if [ -z "$WH_BEFORE" ] || [ "$WH_BEFORE" = "{}" ]; then
    fail "Daemon did not return a webhook block — aggregator may be disabled"
    exit 4
fi
EVENTS_BEFORE=$(get_field "$WH_BEFORE" "events_recorded")
RECV_ERR_BEFORE=$(get_field "$WH_BEFORE" "receive_errors")
log ""
log "BEFORE: events_recorded=$EVENTS_BEFORE  receive_errors=$RECV_ERR_BEFORE"

# Capture current /tmp/webhook-publish.err size for ZSF delta check.
ERR_SIZE_BEFORE=0
if [ -f "$ERR_LOG" ]; then
    ERR_SIZE_BEFORE=$(wc -c < "$ERR_LOG" | tr -d ' ')
fi

# -----------------------------------------------------------------------------
# QUICK MODE EARLY EXIT
# -----------------------------------------------------------------------------
if [ "$MODE" = "quick" ]; then
    log ""
    log "[quick mode] No producer trigger. Reporting last-known-good only."
    log ""
    log "  events_recorded:   $EVENTS_BEFORE"
    log "  last_total_ms:     $(get_field "$WH_BEFORE" "last_total_ms")"
    log "  last_age_s:        $(get_field "$WH_BEFORE" "last_webhook_age_s")"
    log "  cache_hit_rate:    $(get_field "$WH_BEFORE" "cache_hit_rate")"
    log "  publish_err_size:  $ERR_SIZE_BEFORE bytes"
    if [ "$EVENTS_BEFORE" = "0" ] || [ -z "$EVENTS_BEFORE" ]; then
        warn "events_recorded=0 — no producer has fired yet (run --full to verify)"
        exit 2
    fi
    ok "Last-known-good: events_recorded=$EVENTS_BEFORE"
    exit 0
fi

# =============================================================================
# FULL MODE
# =============================================================================
log ""
hr
log "PHASE 1 — Trigger producer (auto-memory-query.sh, layered branch)"
hr

# auto-memory-query.sh expects either an arg, $PROMPT env, or stdin JSON.
# It also dedupes within 2s — clear that file so our trigger is honored.
rm -f /tmp/.context-dna-hook-dedup 2>/dev/null

# Run the producer. It backgrounds the publish itself; we wait briefly.
"$REPO_DIR/scripts/auto-memory-query.sh" "$TEST_PROMPT" \
    > /tmp/webhook-e2e-producer.out 2>/tmp/webhook-e2e-producer.err
PRODUCER_RC=$?
log "Producer exit code: $PRODUCER_RC"
if [ "$PRODUCER_RC" -ne 0 ]; then
    warn "Producer exited non-zero — check /tmp/webhook-e2e-producer.err"
fi

# Backgrounded publisher uses --wait-s 1.0; give it some headroom.
sleep 2

log ""
hr
log "PHASE 2 — Verify counter advance"
hr
WH_AFTER=$(fetch_webhook_health)
EVENTS_AFTER=$(get_field "$WH_AFTER" "events_recorded")
DELTA=$(( ${EVENTS_AFTER:-0} - ${EVENTS_BEFORE:-0} ))
log "AFTER:  events_recorded=$EVENTS_AFTER  (delta=$DELTA)"

if [ "$DELTA" -lt 1 ]; then
    fail "Counter did not advance — producer wired but publish lost"
    fail "  Check: NATS reachable, nats-py installed, daemon subject subscribed"
    COUNTER_OK=0
else
    ok "Counter advanced by $DELTA"
    COUNTER_OK=1
fi

# -----------------------------------------------------------------------------
# PHASE 3 — Per-section coverage. We invoke generate_context_injection
# directly (the function the publish hook lives in) so we can inspect
# *which* sections returned content. The producer trigger above only
# publishes a stub `layered:1:ok` marker; it does not exercise S0-S8.
# This phase is the actual S0-S8 contract verification.
# -----------------------------------------------------------------------------
log ""
hr
log "PHASE 3 — Per-section S0-S8/S10 content coverage"
hr

INJECT_RAW=$(mktemp)
cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" "$PYTHON" - >"$INJECT_RAW" 2>/tmp/webhook-e2e-inject.err <<PYEOF
import json, sys, os
# Force determinism: use a stable session id so anticipation doesn't drift.
os.environ.setdefault("CLAUDE_SESSION_ID", "webhook-e2e-test-session")
try:
    from memory.persistent_hook_structure import generate_context_injection
    r = generate_context_injection(
        "${TEST_PROMPT//\"/\\\"}",
        mode="hybrid",
    )
    keys = ("section_0","section_1","section_2","section_3","section_4",
            "section_5","section_6","section_7","section_8","section_10")
    timings = r.section_timings or {}
    label_to_key = {
        "safety":"section_0","foundation":"section_1","wisdom":"section_2",
        "awareness":"section_3","deep_context":"section_4","protocol":"section_5",
        "synaptic_to_atlas":"section_6","acontext_library":"section_7",
        "synaptic_8th_intelligence":"section_8","vision_contextual_awareness":"section_10",
    }
    included_keys = {label_to_key.get(lbl, lbl) for lbl in (r.sections_included or [])}
    out = {}
    for k in keys:
        # timings dict keys vary: sometimes bare label ("safety"), sometimes
        # full ("section_0"). Look up both forms.
        latency = timings.get(k)
        if latency is None:
            for lbl, kk in label_to_key.items():
                if kk == k:
                    latency = timings.get(lbl)
                    break
        if k in included_keys:
            out[k] = {"status":"filled", "latency_ms": latency}
        elif latency is not None:
            out[k] = {"status":"empty", "latency_ms": latency}
        else:
            out[k] = {"status":"skipped", "latency_ms": None}
    # Marker-tagged single line so the shell can grep it out reliably even
    # when LLM/Redis modules dump warnings to stdout during import.
    print("__E2E_RESULT__" + json.dumps({
        "ok": True,
        "content_len": len(r.content or ""),
        "sections": out,
        "raw_included": list(r.sections_included or []),
        "raw_timings": timings,
    }))
except Exception as e:
    print("__E2E_RESULT__" + json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
PYEOF
INJECT_JSON=$(grep '^__E2E_RESULT__' "$INJECT_RAW" | tail -1 | sed 's/^__E2E_RESULT__//')
rm -f "$INJECT_RAW"

if [ -z "$INJECT_JSON" ]; then
    fail "Direct injection call returned empty — see /tmp/webhook-e2e-inject.err"
    exit 1
fi

# Parse and report — pass JSON via env var to avoid shell quoting hell.
INJECT_JSON="$INJECT_JSON" "$PYTHON" - <<'PYEOF'
import json, os, sys
raw = os.environ.get("INJECT_JSON", "")
if not raw:
    print("FAIL  no JSON received from injection call")
    sys.exit(1)
try:
    data = json.loads(raw)
except Exception as e:
    print(f"FAIL  JSON parse error: {e}")
    print(f"raw[:200]: {raw[:200]!r}")
    sys.exit(1)
if not data.get("ok"):
    print("FAIL  injection call errored:", data.get("error"))
    sys.exit(1)
print(f"  Content length: {data['content_len']} chars")
print(f"  Raw included:   {data['raw_included']}")
print(f"  Raw timings:    {data['raw_timings']}")
print()
labels = {
    "section_0":"S0 SAFETY",
    "section_1":"S1 FOUNDATION",
    "section_2":"S2 WISDOM",
    "section_3":"S3 AWARENESS",
    "section_4":"S4 DEEP_CONTEXT",
    "section_5":"S5 PROTOCOL",
    "section_6":"S6 HOLISTIC",
    "section_7":"S7 FULL_LIBRARY",
    "section_8":"S8 8TH_INTELLIGENCE",
    "section_10":"S10 STRATEGIC",
}
GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[1;33m"; NC="\033[0m"
empties = []
for k, lbl in labels.items():
    info = data["sections"].get(k, {})
    st = info.get("status","?")
    lat = info.get("latency_ms")
    lat_s = f" ({lat}ms)" if lat is not None else ""
    if st == "filled":
        print(f"  {GREEN}✓{NC} {lbl:<25} filled{lat_s}")
    elif st == "empty":
        print(f"  {RED}✗{NC} {lbl:<25} EMPTY{lat_s}")
        empties.append(k)
    else:
        print(f"  {YELLOW}?{NC} {lbl:<25} skipped{lat_s}")
        empties.append(k)
if empties:
    print()
    print(f"  Empty/skipped sections: {empties}")
    sys.exit(1)
PYEOF
SECTIONS_RC=$?

# -----------------------------------------------------------------------------
# PHASE 4 — ZSF check (publisher errors)
# -----------------------------------------------------------------------------
log ""
hr
log "PHASE 4 — ZSF check (/tmp/webhook-publish.err)"
hr

ZSF_OK=1
ERR_SIZE_AFTER=0
if [ -f "$ERR_LOG" ]; then
    ERR_SIZE_AFTER=$(wc -c < "$ERR_LOG" | tr -d ' ')
fi
ERR_DELTA=$(( ERR_SIZE_AFTER - ERR_SIZE_BEFORE ))
if [ "$ERR_DELTA" -gt 0 ]; then
    fail "/tmp/webhook-publish.err grew by $ERR_DELTA bytes (publisher errors)"
    log "  Last 5 lines:"
    tail -5 "$ERR_LOG" | sed 's/^/    /'
    ZSF_OK=0
else
    ok "/tmp/webhook-publish.err clean (delta=$ERR_DELTA)"
fi

# Publisher counter delta — best-effort. Daemon /health doesn't expose
# the publisher's own counters (those live in the producer process).
# Instead we check daemon /health.webhook.receive_errors which would
# bump if events arrived but failed to aggregate.
RECV_ERR_AFTER=$(get_field "$WH_AFTER" "receive_errors")
RECV_DELTA=$(( ${RECV_ERR_AFTER:-0} - ${RECV_ERR_BEFORE:-0} ))
if [ "$RECV_DELTA" -gt 0 ]; then
    fail "daemon receive_errors increased by $RECV_DELTA"
    ZSF_OK=0
else
    ok "daemon receive_errors clean (delta=$RECV_DELTA)"
fi

# -----------------------------------------------------------------------------
# SUMMARY
# -----------------------------------------------------------------------------
log ""
hr
log "SUMMARY"
hr
log "  Counter advance:    $([ "$COUNTER_OK" = 1 ] && echo PASS || echo FAIL)  (delta=$DELTA)"
log "  Section coverage:   $([ "$SECTIONS_RC" = 0 ] && echo PASS || echo FAIL)"
log "  ZSF (no errors):    $([ "$ZSF_OK" = 1 ] && echo PASS || echo FAIL)"
log ""

# Determine exit code (priority: setup > zsf > counter > sections)
if [ "$ZSF_OK" != "1" ]; then
    exit 3
fi
if [ "$COUNTER_OK" != "1" ]; then
    exit 2
fi
if [ "$SECTIONS_RC" != "0" ]; then
    exit 1
fi
ok "ALL CHECKS PASSED"
exit 0
