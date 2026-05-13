#!/bin/bash
# =============================================================================
# fleet-profile-publish.sh — YY2 one-shot capability-profile publisher
# =============================================================================
#
# Backfills mac2's missing `profile` block in the fleet-state KV bucket
# WITHOUT requiring a daemon restart. (VV4 audit: mac2 silent because its
# daemon predates the auto-publish path; restarting the daemon at the
# wrong moment risks dropping JetStream peer state.)
#
# Uses multifleet.node_profile.{detect_local_profile, publish_profile_to_kv}
# — the same functions tools/fleet_nerve_nats.py calls at startup. Result is
# byte-identical to a daemon-startup publish.
#
# Modes:
#   (none) | --dry-run    Detect + print the profile that WOULD be published.
#   --apply               Connect to NATS and write to KV. Idempotent.
#   --node <id>           Override node id (default: $MULTIFLEET_NODE_ID or hostname)
#   --nats-url <url>      Override NATS URL (default: nats://127.0.0.1:4222)
#
# Exit codes: 0 OK, 1 publish failed, 2 bad usage.
# Log: /tmp/yy2-fleet-profile-publish-<node>.log
# =============================================================================

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

MODE="dry-run"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  MODE="dry-run"; shift ;;
        --apply)    MODE="apply"; shift ;;
        --node)     NODE_ID="$2"; shift 2 ;;
        --nats-url) NATS_URL="$2"; shift 2 ;;
        -h|--help)  sed -n '3,25p' "$0"; exit 0 ;;
        *) echo "[yy2-pub] unknown arg: $1" >&2; exit 2 ;;
    esac
done

LOG="/tmp/yy2-fleet-profile-publish-${NODE_ID}.log"
: > "$LOG"

_log() {
    local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s [%s] %s\n' "$ts" "$NODE_ID" "$*" | tee -a "$LOG"
}

_log "publisher start mode=$MODE nats=$NATS_URL"

# Detect + publish in a single python subprocess for atomicity.
PYTHONPATH="$REPO_ROOT/multi-fleet:$REPO_ROOT" python3 - "$NODE_ID" "$NATS_URL" "$MODE" <<'PY'
import asyncio, json, sys
node, nats_url, mode = sys.argv[1:4]
try:
    from multifleet.node_profile import (
        detect_local_profile, publish_profile_to_kv,
    )
except Exception as e:
    print(f"ERR import: {e}")
    sys.exit(1)

profile = detect_local_profile(node_id=node)
print(f"DETECTED {json.dumps(profile.to_dict())}")

if mode != "apply":
    print("DRY-RUN no mutation performed")
    sys.exit(0)

try:
    import nats  # type: ignore
except Exception as e:
    print(f"ERR nats-py missing: {e}")
    sys.exit(1)

async def main():
    try:
        nc = await asyncio.wait_for(nats.connect(nats_url), timeout=5)
    except Exception as e:
        print(f"ERR nats connect: {e}")
        return 1
    try:
        ok = await publish_profile_to_kv(nc, profile)
    finally:
        try:
            await nc.drain()
        except Exception:
            pass
    if ok:
        print("PUBLISHED to fleet_state KV")
        return 0
    print("ERR publish_profile_to_kv returned False")
    return 1

rc = asyncio.run(main())
sys.exit(int(rc or 0))
PY
RC=$?

if [ "$RC" -eq 0 ]; then
    _log "publisher exit OK"
else
    _log "publisher exit FAIL rc=$RC"
fi
exit "$RC"
