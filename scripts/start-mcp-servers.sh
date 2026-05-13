#!/usr/bin/env bash
# start-mcp-servers.sh — Start Race Theater (:8877) and Evidence Stream (:8878)
# ZSF: each server is independent; failure of one never affects the other.
# Usage: bash scripts/start-mcp-servers.sh [--dry-run]

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# ── Helpers ──────────────────────────────────────────────────────────────────
is_up() {
  curl -sf --max-time 3 "$1" >/dev/null 2>&1
}

start_server() {
  local name="$1"
  local script="$2"
  local port="$3"
  local health_url="http://127.0.0.1:${port}/health"
  local log_file="/tmp/mcp-${name}.log"

  if is_up "$health_url"; then
    echo "✅ ${name} (:${port}): already running"
    return 0
  fi

  if [[ ! -f "$script" ]]; then
    echo "⚠️  ${name}: server script not found (${script})" >&2
    return 0  # ZSF: skip, don't abort
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "🔍 [dry-run] would start ${name} on :${port} → log: ${log_file}"
    return 0
  fi

  echo "🚀 Starting ${name} on :${port} → log: ${log_file}"
  nohup \
    /usr/bin/env python3 "$script" \
    >>"$log_file" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true

  # Brief wait then health-check
  local attempts=0
  while [[ $attempts -lt 6 ]]; do
    sleep 0.5
    if is_up "$health_url"; then
      echo "✅ ${name} (:${port}): started (pid=${pid})"
      return 0
    fi
    attempts=$((attempts + 1))
  done

  echo "⚠️  ${name} (:${port}): started (pid=${pid}) but /health not yet responding — check ${log_file}" >&2
}

# ── Race Theater — port 8877 ─────────────────────────────────────────────────
start_server \
  "race-theater" \
  "${REPO_ROOT}/mcp-servers/race-theater/server.py" \
  8877 || true

# ── Evidence Stream — port 8878 ──────────────────────────────────────────────
start_server \
  "evidence-stream" \
  "${REPO_ROOT}/mcp-servers/evidence-stream/server.py" \
  8878 || true

# ── Event Bridge — port 8879 ─────────────────────────────────────────────────
start_server \
  "event-bridge" \
  "${REPO_ROOT}/mcp-servers/event-bridge/server.py" \
  8879 || true

echo "Done."
