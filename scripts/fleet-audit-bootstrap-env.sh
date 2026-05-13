#!/bin/bash
# Bootstrap DEEPSEEK_API_KEY env for the audit-tick pipeline (E3 fix).
#
# WHY THIS EXISTS
# ---------------
# multi-fleet/multifleet/audit_consult.py:_resolve_api_key() checks:
#   1. $DEEPSEEK_API_KEY env (preferred, fast path)
#   2. macOS keychain `security find-generic-password -s fleet-nerve
#      -a Context_DNA_DEEPSEEK -w` (does NOT exist on this node — rc=44)
#
# When the audit-tick is launched by launchd it inherits a minimal env
# (no DEEPSEEK_API_KEY). The keychain fallback then fails silently and
# every consult is short-circuited to DISMISS_AS_NOISE. Chief never
# escalates → audit_consult.py is reverted → we cannot patch the python.
#
# This wrapper sources DEEPSEEK_API_KEY from the brain broker
# (scripts/brain-secret.sh), then `exec "$@"` the real audit-tick.
#
# ZSF: any failure to obtain the key is logged to stderr and the launchd
# log captures it. We DO NOT exit non-zero on broker miss because that
# would cause launchd to back-off the entire audit pipeline; instead we
# fall through and let audit_consult.py emit its existing no_api_key
# warning so the regression remains observable in the same channel.

set -u

LOG_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_err() { printf '%s fleet-audit-bootstrap-env: %s\n' "$LOG_TS" "$*" >&2; }

# Brain broker requires xbar gate; launchd is headless so skip explicitly.
# This is the standard pattern documented in scripts/brain-secret.sh.
export BRAIN_BROKER_SKIP_XBAR_CHECK=1
export BRAIN_SECRET_CALLER="${BRAIN_SECRET_CALLER:-fleet-audit-bootstrap-env}"

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    here_dir="$(cd "$(dirname "$0")" && pwd)"
    broker="$here_dir/brain-secret.sh"
    if [ ! -x "$broker" ]; then
        log_err "ERROR brain broker not executable at $broker (audit will hit no_api_key)"
    else
        if key="$("$broker" DEEPSEEK_API_KEY 2>/tmp/fleet-audit-bootstrap-env.err)"; then
            if [ -n "$key" ]; then
                export DEEPSEEK_API_KEY="$key"
            else
                log_err "ERROR brain broker returned empty key"
            fi
        else
            broker_rc=$?
            log_err "ERROR brain broker rc=$broker_rc (see /tmp/fleet-audit-bootstrap-env.err)"
        fi
    fi
fi

if [ $# -eq 0 ]; then
    log_err "ERROR no command supplied to wrapper"
    exit 64
fi

exec "$@"
