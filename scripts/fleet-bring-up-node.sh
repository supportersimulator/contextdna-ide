#!/bin/bash
# =============================================================================
# fleet-bring-up-node.sh — YY2 per-node bring-up orchestrator
# =============================================================================
#
# Run on mac2 or mac3 to verify that this machine is fully wired into the
# fleet, from BOTH paths Aaron uses:
#   (a) operator-launched IDE workflow:
#       VS Code + Claude Code extension, Cursor, OpenCode, etc.
#   (b) autonomous worker path:
#       fleet daemon + NATS + capability profile published to fleet-state KV
#       so WW2's idle-worker can pull tasks for this node.
#
# Diagnostic: VV4 found mac2 has NO profile block in KV (only heartbeat
# mirror) — capability-aware dispatch can't reason about it. mac3 has a
# profile (m1_max 64GB) but produced zero work in 4 days. This script gives
# the operator a single command to ratify per-node readiness and (with
# --apply) backfill the missing profile.
#
# Modes:
#   (no flag) | --dry-run    Probe + report. NEVER mutates anything.
#   --apply                  Publish profile to KV (only mutation it does).
#                            Does NOT modify launchd plists. Does NOT start
#                            new services. Does NOT install IDEs.
#   --node <id>              Override MULTIFLEET_NODE_ID for this run.
#
# Exit codes:
#   0   ready (all REQUIRED checks PASS)
#   1   degraded (one or more REQUIRED checks FAIL)
#   2   bad usage
#
# Logs: /tmp/yy2-fleet-bring-up-<node>.log (overwritten each run).
#
# Invariants:
#   * ZSF — every check writes a structured line, never silent.
#   * DRY-RUN DEFAULT — first invocation never mutates.
#   * NO LAUNCHD MUTATION — per YY2 brief.
#   * ADDITIVE — does not edit existing scripts/services.
# =============================================================================

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

MODE="dry-run"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
PORT="${FLEET_NERVE_PORT:-8855}"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) MODE="dry-run"; shift ;;
        --apply)   MODE="apply"; shift ;;
        --node)    NODE_ID="$2"; shift 2 ;;
        -h|--help) sed -n '3,40p' "$0"; exit 0 ;;
        *) echo "[yy2] unknown arg: $1" >&2; exit 2 ;;
    esac
done

LOG="/tmp/yy2-fleet-bring-up-${NODE_ID}.log"
: > "$LOG"

# REQUIRED checks contribute to exit code. INFO checks are observational.
PASS=0; FAIL=0; INFO=0
SUMMARY=()

_log() {
    local level="$1"; shift
    local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s [%s] [%s] %s\n' "$ts" "$NODE_ID" "$level" "$*" | tee -a "$LOG"
}

_record() {
    # $1 = severity (REQ|INFO)  $2 = name  $3 = status (PASS|FAIL|SKIP)  $4 = detail
    SUMMARY+=("$1|$2|$3|$4")
    case "$3" in
        PASS) PASS=$((PASS+1)); _log info "$2 — PASS ($4)" ;;
        FAIL)
            if [ "$1" = "REQ" ]; then FAIL=$((FAIL+1)); else INFO=$((INFO+1)); fi
            _log warn "$2 — FAIL ($4)"
            ;;
        SKIP) INFO=$((INFO+1)); _log info "$2 — SKIP ($4)" ;;
    esac
}

# ── Check 1: VS Code + Claude Code extension ─────────────────────────────────
check_vscode_claude_code() {
    # Claude Code extension marker: ~/.vscode/extensions/anthropic.claude-code-*
    local found=0 marker
    if [ -d "$HOME/.vscode/extensions" ]; then
        marker="$(ls -1 "$HOME/.vscode/extensions" 2>/dev/null | grep -i -E '(anthropic|claude.?code)' | head -1)"
        [ -n "$marker" ] && found=1
    fi
    # Cursor variant also accepted
    if [ -d "$HOME/.cursor/extensions" ]; then
        marker="$(ls -1 "$HOME/.cursor/extensions" 2>/dev/null | grep -i -E '(anthropic|claude.?code)' | head -1)"
        [ -n "$marker" ] && found=1
    fi
    if [ "$found" -eq 1 ]; then
        _record INFO "vscode_claude_code_extension" PASS "$marker"
    else
        _record INFO "vscode_claude_code_extension" FAIL "not found in ~/.vscode/extensions or ~/.cursor/extensions"
    fi
}

# ── Check 2: IDE marker directories (matches 3s _detect_ides) ────────────────
check_ide_markers() {
    # Mirror three_surgeons/cli/main.py:_detect_ides — single source of truth.
    local detected
    detected="$(
        PYTHONPATH="$REPO_ROOT/3-surgeons" python3 - <<'PY' 2>/dev/null || echo "[]"
import json
try:
    from three_surgeons.cli.main import _detect_ides
    print(json.dumps(_detect_ides()))
except Exception:
    print("[]")
PY
    )"
    if [ -z "$detected" ] || [ "$detected" = "[]" ]; then
        _record INFO "ide_markers" FAIL "no IDE markers detected by 3s _detect_ides"
    else
        _record INFO "ide_markers" PASS "$detected"
    fi
}

# ── Check 3: ContextDNA + 3-surgeons venv health ─────────────────────────────
check_venv_health() {
    local rebuild="$REPO_ROOT/scripts/venv-rebuild.sh"
    if [ ! -x "$rebuild" ]; then
        _record REQ "venv_health" FAIL "scripts/venv-rebuild.sh missing or not executable"
        return
    fi
    # --check is non-mutating, runs in both dry-run and apply.
    if bash "$rebuild" --check >>"$LOG" 2>&1; then
        _record REQ "venv_health" PASS "venv-rebuild --check OK"
    else
        _record REQ "venv_health" FAIL "venv-rebuild --check reported missing components (see log)"
    fi
}

# ── Check 4: fleet daemon listening on $PORT ────────────────────────────────
check_fleet_daemon() {
    if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        _record REQ "fleet_daemon_health" PASS "port $PORT /health 2xx"
    else
        _record REQ "fleet_daemon_health" FAIL "no /health on 127.0.0.1:${PORT}"
    fi
}

# ── Check 5: NATS connectivity ──────────────────────────────────────────────
check_nats() {
    # Light TCP probe — full NATS handshake would require nats-py, which is
    # already covered by venv_health.
    local host port
    # Parse nats://host:port
    host="$(echo "$NATS_URL" | sed -E 's#nats://##; s#/.*##; s#:.*##')"
    port="$(echo "$NATS_URL" | sed -E 's#nats://##; s#/.*##' | awk -F: '{print ($2==""?"4222":$2)}')"
    if (echo > "/dev/tcp/${host}/${port}") >/dev/null 2>&1; then
        _record REQ "nats_tcp" PASS "${host}:${port} reachable"
    else
        _record REQ "nats_tcp" FAIL "${host}:${port} not reachable"
    fi
}

# ── Check 6: Capability profile in fleet-state KV ───────────────────────────
# This is THE missing-mac2 fix from VV4.
# Read-only here; --apply mode then calls fleet-profile-publish.sh.
check_profile_in_kv() {
    local present
    present="$(
        PYTHONPATH="$REPO_ROOT/multi-fleet:$REPO_ROOT" python3 - "$NODE_ID" "$NATS_URL" <<'PY' 2>/dev/null
import asyncio, json, sys
node, nats_url = sys.argv[1], sys.argv[2]
try:
    import nats  # type: ignore
except Exception as e:
    print(json.dumps({"ok": False, "reason": f"import: {e}"}))
    sys.exit(0)
async def main():
    try:
        nc = await asyncio.wait_for(nats.connect(nats_url), timeout=4)
    except Exception as e:
        print(json.dumps({"ok": False, "reason": f"nats connect: {e}"}))
        return
    # Schema-tolerant read: the daemon (write_own_state) stores a SHORT
    # profile dict {tier, ram_gb, gpu, role} without node_id, while the
    # standalone publisher (publish_profile_to_kv) stores the full
    # NodeProfile dict. Accept either — both prove "this node registered".
    raw = None
    try:
        js = nc.jetstream()
        kv = await js.key_value(bucket="fleet_state")
        entry = await kv.get(f"{node}.profile")
        if entry and entry.value:
            raw = json.loads(entry.value)
    except Exception as e:
        try:
            await nc.drain()
        except Exception:
            pass
        print(json.dumps({"ok": False, "reason": f"kv read: {e}"}))
        return
    try:
        await nc.drain()
    except Exception:
        pass
    if raw is None:
        print(json.dumps({"ok": False, "reason": "no profile in KV"}))
        return
    # Both shapes are accepted as valid registration evidence.
    print(json.dumps({"ok": True, "profile": raw}))
asyncio.run(main())
PY
    )"
    if echo "$present" | grep -q '"ok": true'; then
        _record REQ "capability_profile_kv" PASS "$present"
    else
        _record REQ "capability_profile_kv" FAIL "$present"
    fi
}

apply_publish_profile() {
    local publisher="$REPO_ROOT/scripts/fleet-profile-publish.sh"
    if [ ! -x "$publisher" ]; then
        _log warn "fleet-profile-publish.sh missing — cannot --apply profile"
        return 1
    fi
    if bash "$publisher" --node "$NODE_ID" --nats-url "$NATS_URL" --apply >>"$LOG" 2>&1; then
        _log info "profile published to KV via fleet-profile-publish.sh"
        return 0
    fi
    _log warn "fleet-profile-publish.sh --apply failed (see log)"
    return 1
}

# ── Main ────────────────────────────────────────────────────────────────────
_log info "starting bring-up node=$NODE_ID mode=$MODE port=$PORT nats=$NATS_URL"

check_vscode_claude_code
check_ide_markers
check_venv_health
check_fleet_daemon
check_nats
check_profile_in_kv

# --apply: backfill the one mutation this script owns — profile in KV.
if [ "$MODE" = "apply" ]; then
    # Only re-publish if the readonly check failed; idempotent regardless.
    _log info "--apply: attempting profile publish for $NODE_ID"
    apply_publish_profile || true
    # Re-check after publish.
    check_profile_in_kv
fi

echo ""
echo "── YY2 bring-up summary for $NODE_ID (mode=$MODE) ──"
for row in "${SUMMARY[@]}"; do
    echo "  $row"
done
echo "  required_pass=$PASS  required_fail=$FAIL  info=$INFO"
echo "  log=$LOG"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
