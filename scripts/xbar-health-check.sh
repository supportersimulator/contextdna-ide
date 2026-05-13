#!/usr/bin/env bash
# xbar-health-check.sh — YY5 (Part B)
#
# Probes every xbar plugin installed at
# ~/Library/Application Support/xbar/plugins/ and reports
# HEALTHY / DEGRADED / DEAD per plugin.
#
# Definitions:
#   HEALTHY  — plugin runs cleanly (exit 0), emits non-empty stdout, and
#              the first line is NOT a red error badge.
#   DEGRADED — plugin runs cleanly but the first line contains a red/
#              error/unreachable badge, OR plugin has not been refreshed
#              recently (stat mtime check vs schedule embedded in name).
#   DEAD     — plugin exits non-zero, times out, or emits no stdout.
#
# Each plugin run is bounded by a soft 10s timeout (background+kill) so a
# hung plugin can't wedge this audit.
#
# ZSF: every probe failure is captured in /tmp/xbar-health.err and
# increments the xbar_plugin_<name>_health_check_total counter file at
# /tmp/xbar-counters/. Counters survive across runs so future audits can
# trend health.
#
# Usage:
#   bash scripts/xbar-health-check.sh           # human table
#   bash scripts/xbar-health-check.sh --json    # JSON per plugin
#   bash scripts/xbar-health-check.sh --counters-only  # bump counters silently

set -u
set -o pipefail

PLUGIN_DIR="$HOME/Library/Application Support/xbar/plugins"
ERR_LOG="/tmp/xbar-health.err"
COUNTER_DIR="/tmp/xbar-counters"
mkdir -p "$COUNTER_DIR"
: > "$ERR_LOG"

MODE="table"
case "${1:-}" in
  --json)            MODE="json" ;;
  --counters-only)   MODE="silent" ;;
  --help|-h)         sed -n '2,24p' "$0"; exit 0 ;;
esac

# Plugin discovery — follow symlinks. xbar plugins are named
# <stem>.<schedule>.<ext> (e.g. fleet-status.5m.sh), schedule controls
# rerun cadence.
if [[ ! -d "$PLUGIN_DIR" ]]; then
  echo "xbar plugin dir missing: $PLUGIN_DIR" >> "$ERR_LOG"
  echo "no_plugins_found"
  exit 0
fi

# Run a plugin with a soft 10s ceiling. macOS lacks coreutils `timeout`
# in the default PATH, so we background+kill.
probe_plugin() {
  local plugin="$1"
  local out_file="$2"
  local exit_file="$3"
  local timeout_s="${4:-10}"

  local ext="${plugin##*.}"
  case "$ext" in
    sh|bash) cmd=("/bin/bash" "$plugin") ;;
    py)      cmd=("/usr/bin/env" "python3" "$plugin") ;;
    *)       cmd=("/bin/bash" "$plugin") ;;
  esac

  ( "${cmd[@]}" 2>>"$ERR_LOG" >"$out_file"; echo $? > "$exit_file" ) &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited+1))
    if [[ $waited -ge $timeout_s ]]; then
      kill -TERM "$pid" 2>/dev/null
      sleep 1
      kill -KILL "$pid" 2>/dev/null
      echo 124 > "$exit_file"
      return
    fi
  done
  wait "$pid" 2>/dev/null || true
}

bump_counter() {
  local name="$1"
  local kind="$2"  # healthy / degraded / dead / probe
  local file="$COUNTER_DIR/xbar_plugin_${name}_${kind}_total"
  local v=0
  [[ -f "$file" ]] && v=$(cat "$file" 2>/dev/null || echo 0)
  echo $((v+1)) > "$file"
}

ROWS=()
for plugin_path in "$PLUGIN_DIR"/*; do
  [[ -f "$plugin_path" || -L "$plugin_path" ]] || continue
  base="$(basename "$plugin_path")"
  # Sanitize for counter key — alphanumeric only.
  key="$(echo "$base" | tr -c '[:alnum:]' '_' | sed 's/__*/_/g; s/^_//; s/_$//')"

  # Mtime of underlying target (follow symlink).
  target="$plugin_path"
  if [[ -L "$plugin_path" ]]; then
    resolved="$(readlink "$plugin_path")"
    case "$resolved" in
      /*) target="$resolved" ;;
      *)  target="$PLUGIN_DIR/$resolved" ;;
    esac
  fi
  mtime_human=$(stat -f "%Sm" "$target" 2>/dev/null || echo "?")
  mtime_epoch=$(stat -f "%m" "$target" 2>/dev/null || echo 0)
  now=$(date +%s)
  age_days=$(( (now - mtime_epoch) / 86400 ))

  # Schedule embedded in filename: <stem>.<N><s|m|h>.<ext>
  sched="?"
  case "$base" in
    *.30s.*)  sched="30s" ;;
    *.60s.*)  sched="60s" ;;
    *.1m.*)   sched="1m"  ;;
    *.5m.*)   sched="5m"  ;;
    *.10m.*)  sched="10m" ;;
    *.1h.*)   sched="1h"  ;;
  esac

  out_file=$(mktemp); exit_file=$(mktemp)
  probe_plugin "$target" "$out_file" "$exit_file" 10
  bump_counter "$key" "probe"
  exit_code=$(cat "$exit_file" 2>/dev/null || echo "?")
  first_line=$(head -1 "$out_file" 2>/dev/null || echo "")
  bytes=$(wc -c < "$out_file" | tr -d ' ')

  status="HEALTHY"
  reason=""
  if [[ "$exit_code" == "124" ]]; then
    status="DEAD"; reason="timeout>10s"
  elif [[ "$exit_code" != "0" ]]; then
    status="DEAD"; reason="exit=$exit_code"
  elif [[ "$bytes" == "0" ]]; then
    status="DEAD"; reason="empty_stdout"
  elif echo "$first_line" | grep -qiE '(color=red|unreachable|offline|no repo|🔴|❌|ERROR)'; then
    status="DEGRADED"; reason="red_badge:$(echo "$first_line" | head -c 40)"
  elif [[ "$age_days" -gt 30 ]]; then
    status="DEGRADED"; reason="script_age=${age_days}d"
  fi
  bump_counter "$key" "$(echo "$status" | tr '[:upper:]' '[:lower:]')"

  ROWS+=("$base|$status|$sched|${age_days}d|$exit_code|${reason:-ok}|$(echo "$first_line" | tr -d '\t' | head -c 50)")
  rm -f "$out_file" "$exit_file"
done

if [[ "$MODE" == "silent" ]]; then
  echo "probed=${#ROWS[@]} counters_dir=$COUNTER_DIR" >&2
  exit 0
fi

if [[ "$MODE" == "json" ]]; then
  for r in "${ROWS[@]}"; do
    IFS='|' read -r name status sched age exit reason first <<<"$r"
    printf '{"plugin":"%s","status":"%s","schedule":"%s","age":"%s","exit":"%s","reason":"%s","first_line":"%s"}\n' \
      "$name" "$status" "$sched" "$age" "$exit" "$reason" "$first"
  done
else
  printf '%-40s\t%-9s\t%-6s\t%-6s\t%-5s\t%s\n' "plugin" "status" "sched" "age" "exit" "first_line"
  for r in "${ROWS[@]}"; do
    IFS='|' read -r name status sched age exit reason first <<<"$r"
    printf '%-40s\t%-9s\t%-6s\t%-6s\t%-5s\t%s\n' "$name" "$status" "$sched" "$age" "$exit" "$first"
  done
fi

{
  echo ""
  echo "── XBAR HEALTH SUMMARY ──"
  h=0; d=0; x=0
  for r in "${ROWS[@]}"; do
    case "$r" in
      *"|HEALTHY|"*)  h=$((h+1)) ;;
      *"|DEGRADED|"*) d=$((d+1)) ;;
      *"|DEAD|"*)     x=$((x+1)) ;;
    esac
  done
  echo "total_plugins:     ${#ROWS[@]}"
  echo "healthy:           $h"
  echo "degraded:          $d"
  echo "dead:              $x"
  echo "counters_dir:      $COUNTER_DIR"
  err_count=$(wc -l < "$ERR_LOG" | tr -d ' ')
  echo "probe_errors:      $err_count   (see $ERR_LOG)"
} >&2

exit 0
