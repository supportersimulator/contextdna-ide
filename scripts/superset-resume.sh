#!/usr/bin/env bash
# superset-resume.sh — WaveQ continuity ship.
#
# Aaron's question: "will superset carry on when this claude code session
# limit is reached?" Root cause: Superset stores TASKS (cloud DB, durable)
# but agent sessions are bound to a registered deviceId. Tasks survive
# Claude Code death. Agent processes survive only if device's Superset
# desktop app stays up.
#
# This script lets Aaron (or a fresh Claude Code session) RESUME context:
#   superset-resume.sh tasks                  # list all tasks + statuses
#   superset-resume.sh task <task_id>         # one task's full state
#   superset-resume.sh devices                # which devices reachable
#   superset-resume.sh continuity             # full grade A-F report
#
# ZSF: bumps fleet daemon counter `superset_resume_invocations_total` so
# every resume is observable on /health.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

CMD="${1:-continuity}"
shift || true

# Bump counter (best-effort; no failure if daemon down)
curl -sf -X POST http://127.0.0.1:8855/counter/superset_resume_invocations_total \
  -H "Content-Type: application/json" -d '{"delta":1}' >/dev/null 2>&1 || true

case "$CMD" in
  tasks)
    PYTHONPATH=multi-fleet python3 -c "
from multifleet.superset_bridge import SupersetBridge
import json
b = SupersetBridge()
tasks = b.list_tasks()
print(json.dumps([{
    'id': t.get('id'),
    'title': (t.get('title') or '')[:80],
    'status': t.get('statusName'),
    'priority': t.get('priority'),
} for t in tasks], indent=2))
"
    ;;
  task)
    [[ -z "${1:-}" ]] && { echo "usage: superset-resume.sh task <task_id>" >&2; exit 2; }
    PYTHONPATH=multi-fleet python3 -c "
from multifleet.superset_bridge import SupersetBridge
import json
b = SupersetBridge()
print(json.dumps(b.get_task('$1'), indent=2, default=str))
"
    ;;
  devices)
    PYTHONPATH=multi-fleet python3 -c "
from multifleet.superset_bridge import SupersetBridge
import json, datetime as dt
b = SupersetBridge()
now = dt.datetime.now(dt.timezone.utc)
for d in b.list_devices():
    seen = d.get('lastSeenAt','')
    age = '?'
    try:
        t = dt.datetime.fromisoformat(seen.replace('Z','+00:00'))
        age = f'{int((now-t).total_seconds()/60)}min ago'
    except Exception: pass
    print(f\"  {d.get('deviceName'):20s} {d.get('deviceId')[:12]}  last_seen={age}\")
"
    ;;
  bundle)
    bash "$(dirname "$0")/superset-memory-bundle.sh"
    ;;
  continuity)
    echo "=== Superset Continuity Report (WaveQ) ==="
    echo
    echo "[i] Tasks executing server-side after Claude Code dies?"
    echo "    Tasks are TICKETS in cloud DB. They persist but DO NOT"
    echo "    auto-execute. Need device's Superset app to run agent."
    echo "    Grade: B (durable storage, no autonomous compute)"
    echo
    echo "[ii] Results retrievable post-session?"
    echo "     YES via get_task / list_tasks (cloud MCP). No"
    echo "     agent_session status endpoint though — running agent"
    echo "     progress not pollable from cloud API."
    echo "     Grade: C (task state yes, live agent progress no)"
    echo
    echo "[iii] Aaron can resume + interact from another tab/device?"
    echo "      YES via Superset web UI / Tribunal panel + this"
    echo "      script. Cross-device via deviceId. But no built-in"
    echo "      'session shepherd' that bridges Claude Code <-> Superset."
    echo "      Grade: C+ (manual resume works, no auto-bridge)"
    echo
    echo "--- Live state ---"
    echo "Tasks in cloud:"
    "$0" tasks 2>/dev/null | head -30
    echo
    echo "Registered devices:"
    "$0" devices 2>/dev/null
    echo
    echo "=== Gap-closing ship needed ==="
    echo "1. session_limit_hit NATS event → Superset agent writes"
    echo "   'parent rate-limited, my task survives' to known inbox."
    echo "2. Periodic checkpoint: every long task pushes state to"
    echo "   a Superset task description so resume is one-call."
    echo "3. Web UI link surfaced in xbar after push_task."
    echo
    echo "=== Memory Context Bundle ==="
    bash "$(dirname "$0")/superset-memory-bundle.sh" 2>/dev/null || echo "(bundle unavailable)"
    ;;
  *)
    echo "usage: $0 {tasks|task <id>|devices|continuity|bundle}" >&2
    exit 2
    ;;
esac
