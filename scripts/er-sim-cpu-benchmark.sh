#!/usr/bin/env bash
# er-sim-cpu-benchmark.sh
#
# CCC4 — wrapper for the ER simulator audio-engine CPU benchmark.
#
# Drives the AdaptiveSalienceEngine through a sustained worst-case scenario
# (vfib + hypoxia + ducking) at a 60 Hz tick rate, samples %CPU at 100 ms
# intervals, and reports p50/p95/p99/max with a PASS/FAIL verdict against the
# CLAUDE.md "<1% CPU" budget.
#
# This is the FIRST real measurement. Prior to this the budget was gated only
# by the MIN_SOUND_INTERVAL >= 2000ms proxy. Failure logs a warning but does
# not break the build (no historical baseline exists yet).
#
# Usage:
#   ./scripts/er-sim-cpu-benchmark.sh              # default 60s run
#   DURATION_MS=30000 ./scripts/er-sim-cpu-benchmark.sh
#   TICK_HZ=120 ./scripts/er-sim-cpu-benchmark.sh
#
# ZSF: missing node or missing engine file produce explicit FAIL output, not
# silent skip.

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/simulator-core/er-sim-monitor/scripts/benchmarkAudioEngine.mjs"

if ! command -v node >/dev/null 2>&1; then
  echo "[FAIL] node not on PATH — cannot run benchmark." >&2
  exit 1
fi

if [[ ! -f "$SCRIPT" ]]; then
  echo "[FAIL] benchmark script missing: $SCRIPT" >&2
  exit 1
fi

# Forward env knobs (DURATION_MS, TICK_HZ).
exec node "$SCRIPT"
