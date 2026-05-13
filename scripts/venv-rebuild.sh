#!/usr/bin/env bash
# =============================================================================
# venv-rebuild.sh — M1 idempotent .venv repair for Fleet Auto-Heal (Wave A5).
# =============================================================================
#
# Detects whether `.venv/` exists and whether the essential core packages
# (uvicorn, nats-py, httpx, click, pydantic, fastapi, redis, requests, pyyaml)
# are importable. If any are missing, installs them via the existing
# `memory/requirements-agent.txt` manifest. Logs to
# /tmp/venv-rebuild-{NODE_ID}.log so that mac1, mac2, mac3 each get their own
# trail (cross-node-aware per the 2026-05-06 proposal).
#
# Per spec (docs/plans/2026-05-06-fleet-auto-heal-upgrade-proposal.md §3 M1):
#   * IDEMPOTENT — running twice has no side effects when venv is healthy.
#   * REVERSIBLE — --force will rm -rf the venv before rebuilding; default
#     mode never destroys.
#   * ZERO SILENT FAILURES — each install failure increments an observable
#     counter at /tmp/venv-rebuild-counters.txt and writes to the log file.
#   * Exit codes:
#         0 — venv healthy after the run (or already healthy)
#         1 — rebuild needed but pip install failed (counter incremented)
#         2 — usage error
#
# Modes:
#   --check       (default in absence of other flag) Probe only; never mutate.
#   --dry-run     Print the install commands that would run; never mutate.
#   --apply       Run pip install for any missing packages.
#   --force       rm -rf the venv and rebuild from scratch (then --apply).
#   -h | --help   Show this header.
#
# Wired into sprint-aaron-actions.sh (§9-§12 spec) as a pre-flight that
# daemon-services-up.sh can call: `bash scripts/venv-rebuild.sh --check`.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"
REQ_FILE="${REQ_FILE:-$REPO_ROOT/memory/requirements-agent.txt}"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo unknown)}"
LOG_FILE="${VENV_REBUILD_LOG:-/tmp/venv-rebuild-${NODE_ID}.log}"
COUNTER_FILE="${VENV_REBUILD_COUNTER_FILE:-/tmp/venv-rebuild-counters.txt}"

# Essential package import names — these are what daemon code actually
# imports. Keep this list small and stable; if you add a new daemon dep,
# add it here AND to memory/requirements-agent.txt.
ESSENTIAL_PKGS=(uvicorn fastapi nats httpx click pydantic redis requests yaml)

MODE="check"

usage() {
    sed -n '3,33p' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --check)   MODE="check" ;;
        --dry-run) MODE="dry-run" ;;
        --apply)   MODE="apply" ;;
        --force)   MODE="force" ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[venv-rebuild] unknown arg: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# Best-effort fresh-log per run (one node, one trail).
: > "$LOG_FILE" 2>/dev/null || true

_log() {
    local msg="[$(date '+%Y-%m-%dT%H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# ZSF counter — append-only k=v lines per backup-context-dna.sh convention.
_counter_inc() {
    local key="$1"
    local now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s %s=1 ts=%s node=%s\n' "$(date +%Y%m%d_%H%M%S)" "$key" "$now" "$NODE_ID" \
        >> "$COUNTER_FILE" 2>/dev/null || true
}

_py_exe() {
    # Returns the venv python if usable, else empty.
    if [ -x "$VENV_DIR/bin/python" ]; then
        echo "$VENV_DIR/bin/python"
    elif [ -x "$VENV_DIR/bin/python3" ]; then
        echo "$VENV_DIR/bin/python3"
    else
        echo ""
    fi
}

_missing_pkgs() {
    # Echoes a space-separated list of missing import names.
    # Empty output means all healthy.
    local py
    py="$(_py_exe)"
    [ -z "$py" ] && { echo "${ESSENTIAL_PKGS[*]}"; return; }
    local missing=""
    for pkg in "${ESSENTIAL_PKGS[@]}"; do
        if ! "$py" -c "import $pkg" >/dev/null 2>&1; then
            missing="$missing $pkg"
        fi
    done
    # Trim leading space
    echo "${missing# }"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

_log "==========================================================================="
_log "venv-rebuild.sh — mode=$MODE  node=$NODE_ID"
_log "Repo:      $REPO_ROOT"
_log "Venv:      $VENV_DIR"
_log "Req file:  $REQ_FILE"
_log "Log:       $LOG_FILE"
_log "Counters:  $COUNTER_FILE"
_log "==========================================================================="

# --force: destroy the venv first (then fall through to apply).
if [ "$MODE" = "force" ]; then
    if [ -d "$VENV_DIR" ]; then
        _log "force: removing existing venv at $VENV_DIR"
        rm -rf "$VENV_DIR" || {
            _log "FAIL: rm -rf $VENV_DIR failed"
            _counter_inc "venv_rebuild_rm_errors_total"
            exit 1
        }
    fi
    MODE="apply"
fi

# Ensure venv exists for apply/dry-run modes; check mode just reports.
if [ ! -d "$VENV_DIR" ]; then
    if [ "$MODE" = "check" ]; then
        _log "CHECK: venv directory missing at $VENV_DIR"
        _counter_inc "venv_rebuild_missing_total"
        exit 1
    fi
    _log "venv missing — creating with: python3 -m venv $VENV_DIR"
    if [ "$MODE" = "dry-run" ]; then
        _log "DRY-RUN: would run python3 -m venv $VENV_DIR"
    else
        if ! python3 -m venv "$VENV_DIR" 2>>"$LOG_FILE"; then
            _log "FAIL: python3 -m venv $VENV_DIR failed"
            _counter_inc "venv_rebuild_create_errors_total"
            exit 1
        fi
    fi
fi

MISSING="$(_missing_pkgs)"
if [ -z "$MISSING" ]; then
    _log "OK: all essential packages importable (${ESSENTIAL_PKGS[*]})"
    exit 0
fi

_log "MISSING: $MISSING"
_counter_inc "venv_rebuild_missing_packages_total"

# Build the install command. Prefer the canonical requirements file because
# it pins versions that other daemons rely on; fall back to the bare package
# list if the requirements file is unreachable.
PIP="$VENV_DIR/bin/pip"
if [ ! -x "$PIP" ]; then
    # If the venv exists but lacks pip (rare), surface clearly.
    _log "FAIL: pip not found at $PIP (venv broken)"
    _counter_inc "venv_rebuild_pip_missing_total"
    exit 1
fi

if [ -f "$REQ_FILE" ]; then
    INSTALL_CMD="$PIP install -r $REQ_FILE"
else
    # Map the import names back to canonical pip distribution names where
    # they differ (yaml -> pyyaml, nats -> nats-py).
    declare -a PIP_PKGS=()
    for pkg in $MISSING; do
        case "$pkg" in
            yaml) PIP_PKGS+=("pyyaml") ;;
            nats) PIP_PKGS+=("nats-py") ;;
            *)    PIP_PKGS+=("$pkg") ;;
        esac
    done
    INSTALL_CMD="$PIP install ${PIP_PKGS[*]}"
fi

case "$MODE" in
    check)
        _log "CHECK: would run: $INSTALL_CMD"
        # Exit 1: caller (e.g. daemon-services-up.sh pre-flight) knows a
        # rebuild is needed but we have not been authorized to mutate.
        exit 1
        ;;
    dry-run)
        _log "DRY-RUN: would run: $INSTALL_CMD"
        exit 0
        ;;
    apply)
        _log "APPLY: running $INSTALL_CMD"
        # shellcheck disable=SC2086
        if ! $INSTALL_CMD >>"$LOG_FILE" 2>&1; then
            _log "FAIL: pip install returned non-zero (see $LOG_FILE)"
            _counter_inc "venv_rebuild_install_errors_total"
            exit 1
        fi
        # Re-probe after install — confirm we actually closed the gap.
        STILL_MISSING="$(_missing_pkgs)"
        if [ -n "$STILL_MISSING" ]; then
            _log "FAIL: still missing after install: $STILL_MISSING"
            _counter_inc "venv_rebuild_install_incomplete_total"
            exit 1
        fi
        _log "OK: venv rebuilt; all essential packages importable"
        _counter_inc "venv_rebuild_success_total"
        exit 0
        ;;
esac

# Should not reach here.
_log "FAIL: unreachable code path"
exit 1
