#!/usr/bin/env bash
# =============================================================================
# refresh-node.sh — M4 per-node refresh orchestrator (Wave A5 fleet auto-heal).
# =============================================================================
#
# Per-node self-heal entry point. When mac3 (or any node — script is symmetric)
# falls out of sync (stale code, broken venv, missing capabilities), Aaron runs
# this on the target node and it composes EXISTING refresh scripts in order.
# Nothing destructive lives here — only orchestration.
#
# Per spec (docs/plans/2026-05-06-fleet-auto-heal-upgrade-proposal.md §3 M4):
#   * DEFAULT --dry-run — surfaces what each step would do, mutates nothing.
#   * Aaron opt-in --apply executes steps 1-3 + daemon-services restart.
#   * --include-cluster-fix gates the RR2 unify-cluster mutation in --apply.
#   * ZSF — each step has its own log + counter; non-strict mode continues
#           past failure so the operator sees the full picture.
#   * IDEMPOTENT — sub-scripts are already idempotent by design.
#   * REVERSIBLE — composes only; introduces no new destructive operations.
#
# Steps (executed in order):
#   1. git fetch + ff-only pull   — surfaces drift; cascade-escalate on conflict.
#   2. venv-rebuild.sh            — M1 idempotent .venv repair.
#   3. sync-node-config.sh        — xbar + plist FD + post-commit hook + plugins.
#   4. patch-neuro-cutover.py     — PP3 env-var flip (only if ENABLE=True).
#   5. unify-cluster-urls.py      — RR2 cluster URL drift (gated, see flags).
#   6. daemon-services-up.sh      — TT3/R1 daemon liveness check + restart.
#   7. constitutional-invariants.sh — 12/12 post-refresh assertion.
#
# Flags:
#   --dry-run                (DEFAULT) every step runs in preview mode.
#   --apply                  run mutations for steps 1-3 + step 6 (--restart-daemons).
#   --include-cluster-fix    in --apply mode, also mutate step 5 (RR2).
#   --restart-daemons        in --apply mode, restart down daemons via step 6.
#   --strict                 abort the chain on first failed step (default = continue).
#   --target-node <id>       label for log/audit (default: $MULTIFLEET_NODE_ID or hostname).
#   -h | --help              show this header.
#
# Output: /tmp/refresh-<node>-<ts>.log  (one log per run, never overwritten)
# Counters: /tmp/refresh-node-counters.txt (ZSF; same node line format as M1).
# Exit codes: 0 = every step PASS, 1 = any step FAIL (or strict-mode abort),
#             2 = usage error.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_ID_DEFAULT="${MULTIFLEET_NODE_ID:-$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo unknown)}"

MODE="dry-run"
INCLUDE_CLUSTER_FIX=0
RESTART_DAEMONS=0
STRICT=0
TARGET_NODE="$NODE_ID_DEFAULT"

usage() { sed -n '3,52p' "$0"; }

for arg in "$@"; do
    case "$arg" in
        --dry-run)            MODE="dry-run" ;;
        --apply)              MODE="apply" ;;
        --include-cluster-fix) INCLUDE_CLUSTER_FIX=1 ;;
        --restart-daemons)    RESTART_DAEMONS=1 ;;
        --strict)             STRICT=1 ;;
        --target-node=*)      TARGET_NODE="${arg#--target-node=}" ;;
        --target-node)        : ;; # value arrives next iteration (handled below)
        -h|--help)            usage; exit 0 ;;
        *)
            # support `--target-node mac3` (two-token form)
            if [ "${prev_arg:-}" = "--target-node" ]; then
                TARGET_NODE="$arg"
                prev_arg=""
                continue
            fi
            echo "[refresh-node] unknown arg: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
    prev_arg="$arg"
done

TS="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${REFRESH_NODE_LOG:-/tmp/refresh-${TARGET_NODE}-${TS}.log}"
COUNTER_FILE="${REFRESH_NODE_COUNTER_FILE:-/tmp/refresh-node-counters.txt}"

# Allow tests to inject mock script paths (override one-or-more sub-scripts).
SCRIPT_VENV_REBUILD="${SCRIPT_VENV_REBUILD:-$REPO_ROOT/scripts/venv-rebuild.sh}"
SCRIPT_SYNC_NODE="${SCRIPT_SYNC_NODE:-$REPO_ROOT/scripts/sync-node-config.sh}"
SCRIPT_NEURO_PATCH="${SCRIPT_NEURO_PATCH:-$REPO_ROOT/scripts/patch-neuro-cutover.py}"
SCRIPT_UNIFY_CLUSTER="${SCRIPT_UNIFY_CLUSTER:-$REPO_ROOT/scripts/unify-cluster-urls.py}"
SCRIPT_DAEMON_SERVICES="${SCRIPT_DAEMON_SERVICES:-$REPO_ROOT/scripts/daemon-services-up.sh}"
SCRIPT_INVARIANTS="${SCRIPT_INVARIANTS:-$REPO_ROOT/scripts/constitutional-invariants.sh}"
SCRIPT_FLEET_GIT_MSG="${SCRIPT_FLEET_GIT_MSG:-$REPO_ROOT/scripts/fleet-git-msg.sh}"

: > "$LOG_FILE" 2>/dev/null || true

_log() {
    local msg="[$(date '+%Y-%m-%dT%H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

_counter_inc() {
    local key="$1"
    local now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s %s=1 ts=%s node=%s\n' "$TS" "$key" "$now" "$TARGET_NODE" \
        >> "$COUNTER_FILE" 2>/dev/null || true
}

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# Tracks whether --strict mode has tripped an abort.
STRICT_ABORT=0

# Run one step. Args: <step_id> <label> <command...>
# Honours --strict (abort on first failure) and ZSF (always logs + counters).
_step() {
    local id="$1"; shift
    local label="$1"; shift
    # If --strict has already tripped, refuse to run further steps.
    if [ "$STRICT_ABORT" -eq 1 ]; then
        _log "[step $id] SKIP — earlier --strict abort"
        SKIP_COUNT=$((SKIP_COUNT + 1))
        _counter_inc "refresh_step_${id}_skip_total"
        return 0
    fi
    _log "----------------------------------------------------------------------"
    _log "[step $id] $label"
    _log "    cmd: $*"
    if [ "$MODE" = "dry-run" ]; then
        _log "    (dry-run) would execute"
    fi
    local rc=0
    # Execute even in dry-run — every sub-script supports its own --dry-run /
    # --check mode, so calling them is safe and informative.
    if "$@" >> "$LOG_FILE" 2>&1; then
        _log "    [step $id] PASS"
        PASS_COUNT=$((PASS_COUNT + 1))
        _counter_inc "refresh_step_${id}_pass_total"
    else
        rc=$?
        _log "    [step $id] FAIL (rc=$rc)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        _counter_inc "refresh_step_${id}_fail_total"
        if [ "$STRICT" -eq 1 ]; then
            _log "    --strict set — chain will abort after this step"
            STRICT_ABORT=1
        fi
    fi
    return 0
}

_skip() {
    local id="$1"; shift
    local reason="$*"
    _log "[step $id] SKIP — $reason"
    SKIP_COUNT=$((SKIP_COUNT + 1))
    _counter_inc "refresh_step_${id}_skip_total"
}

_log "============================================================"
_log "refresh-node.sh  mode=$MODE  target=$TARGET_NODE  strict=$STRICT"
_log "                include_cluster_fix=$INCLUDE_CLUSTER_FIX"
_log "                restart_daemons=$RESTART_DAEMONS"
_log "Repo: $REPO_ROOT"
_log "Log:  $LOG_FILE"
_log "============================================================"

# Verify all composed sub-scripts exist BEFORE running any step. If any is
# missing, the operator gets a clean message instead of a silent skip.
for s in "$SCRIPT_VENV_REBUILD" "$SCRIPT_SYNC_NODE" "$SCRIPT_NEURO_PATCH" \
         "$SCRIPT_UNIFY_CLUSTER" "$SCRIPT_DAEMON_SERVICES" "$SCRIPT_INVARIANTS"; do
    if [ ! -e "$s" ]; then
        _log "ERROR: missing sub-script: $s"
        _counter_inc "refresh_missing_subscript_total"
        echo "ERROR: missing sub-script: $s" >&2
        exit 1
    fi
done

# Helper: dispatch a sub-script.
#   * If the first argument ends in `.py` AND is not executable on the
#     filesystem, prefix with `python3` so unrunnable trackers still launch.
#   * If the first argument ends in `.sh` AND is not executable, prefix
#     with `bash` for the same reason.
#   * Otherwise rely on shebang + +x bit (covers test stubs which are +x).
# This keeps the orchestrator robust against the real-world fact that
# .py scripts in this repo are sometimes tracked without +x.
_run_subscript() {
    local target="$1"; shift
    case "$target" in
        *.py)
            if [ -x "$target" ]; then
                "$target" "$@"
            else
                python3 "$target" "$@"
            fi
            ;;
        *.sh)
            if [ -x "$target" ]; then
                "$target" "$@"
            else
                bash "$target" "$@"
            fi
            ;;
        *)
            "$target" "$@"
            ;;
    esac
}

# ----------------------------------------------------------------------------
# Step 1: git fetch + ff-only pull
# In --dry-run mode we deliberately do NOTHING here — git fetch has side
# effects (.git/FETCH_HEAD update, post-fetch hooks) that violate the "no
# mutation in dry-run" guarantee tests depend on. The log records intent.
# ----------------------------------------------------------------------------
if [ "$MODE" = "apply" ]; then
    _step 1 "git fetch + ff-only pull" \
        bash -c "cd '$REPO_ROOT' && git fetch origin main && git pull --ff-only origin main"
else
    _log "----------------------------------------------------------------------"
    _log "[step 1] git fetch + ff-only pull"
    _log "    (dry-run) would: cd $REPO_ROOT && git fetch origin main && git pull --ff-only origin main"
    _log "    [step 1] PASS (dry-run, no-op)"
    PASS_COUNT=$((PASS_COUNT + 1))
    _counter_inc "refresh_step_1_pass_total"
fi

# ----------------------------------------------------------------------------
# Step 2: venv-rebuild
# ----------------------------------------------------------------------------
if [ "$MODE" = "apply" ]; then
    _step 2 "venv-rebuild --check (apply only on miss)" \
        bash -c "'$SCRIPT_VENV_REBUILD' --check || '$SCRIPT_VENV_REBUILD' --apply"
else
    _step 2 "venv-rebuild --check" _run_subscript "$SCRIPT_VENV_REBUILD" --check
fi

# ----------------------------------------------------------------------------
# Step 3: sync-node-config (xbar, plist, post-commit hook, plugins)
# ----------------------------------------------------------------------------
if [ "$MODE" = "apply" ]; then
    _step 3 "sync-node-config (apply)" _run_subscript "$SCRIPT_SYNC_NODE"
else
    _step 3 "sync-node-config --dry-run" _run_subscript "$SCRIPT_SYNC_NODE" --dry-run
fi

# ----------------------------------------------------------------------------
# Step 4: patch-neuro-cutover. Default ENABLE=False is safe; --dry-run path
# always previews. In --apply mode the script honours its own ENABLE
# constant — so a no-op when Aaron hasn't flipped the switch.
# ----------------------------------------------------------------------------
if [ "$MODE" = "apply" ]; then
    _step 4 "patch-neuro-cutover (honours script ENABLE constant)" \
        _run_subscript "$SCRIPT_NEURO_PATCH"
else
    _step 4 "patch-neuro-cutover --dry-run" \
        _run_subscript "$SCRIPT_NEURO_PATCH" --dry-run
fi

# ----------------------------------------------------------------------------
# Step 5: unify-cluster-urls (RR2). Gated — only mutates with explicit flag.
# ----------------------------------------------------------------------------
if [ "$MODE" = "apply" ] && [ "$INCLUDE_CLUSTER_FIX" -eq 1 ]; then
    _step 5 "unify-cluster-urls --apply (RR2 mutation, gated)" \
        _run_subscript "$SCRIPT_UNIFY_CLUSTER" --apply --node-id "$TARGET_NODE"
else
    _step 5 "unify-cluster-urls --dry-run (RR2 surface only)" \
        _run_subscript "$SCRIPT_UNIFY_CLUSTER" --dry-run --node-id "$TARGET_NODE"
fi

# ----------------------------------------------------------------------------
# Step 6: daemon-services-up. --restart-daemons gates the apply path.
# ----------------------------------------------------------------------------
if [ "$MODE" = "apply" ] && [ "$RESTART_DAEMONS" -eq 1 ]; then
    _step 6 "daemon-services-up --apply --no-prompt" \
        _run_subscript "$SCRIPT_DAEMON_SERVICES" --apply --no-prompt
else
    _step 6 "daemon-services-up --dry-run" \
        _run_subscript "$SCRIPT_DAEMON_SERVICES" --dry-run
fi

# ----------------------------------------------------------------------------
# Step 7: constitutional-invariants — must stay 12/12 after refresh.
# ----------------------------------------------------------------------------
_step 7 "constitutional-invariants (12/12 assertion)" \
    _run_subscript "$SCRIPT_INVARIANTS"

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
_log "============================================================"
_log "refresh-node.sh summary"
_log "  mode:    $MODE"
_log "  node:    $TARGET_NODE"
_log "  pass:    $PASS_COUNT"
_log "  fail:    $FAIL_COUNT"
_log "  skip:    $SKIP_COUNT"
_log "  log:     $LOG_FILE"
_log "  counters: $COUNTER_FILE"
_log "============================================================"

if [ "$FAIL_COUNT" -gt 0 ]; then
    _counter_inc "refresh_run_fail_total"
    exit 1
fi
_counter_inc "refresh_run_pass_total"
exit 0
