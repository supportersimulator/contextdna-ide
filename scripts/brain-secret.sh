#!/bin/bash
# Brain Secrets Broker — fetch a fleet-nerve keychain item, but only when
# the ContextDNA brain plugin is installed and xbar is running on this
# node. This makes the brain a precondition for fleet secret access:
#
#   1. Each node must visibly have the 🧠 brain in its menu bar.
#   2. mf (and any other consumer) calls this wrapper instead of running
#      `security find-generic-password` directly.
#   3. A node without the brain plugin file cannot satisfy secret requests
#      → mf operates in no-secrets mode (DeepSeek/cloud calls fail loudly).
#   4. Every read appends to /tmp/brain-secret-audit.log so we know who
#      asked for what (audit trail).
#
# Usage:
#   scripts/brain-secret.sh Context_DNA_DEEPSEEK
#   scripts/brain-secret.sh Context_DNA_OPENAI
#
# Exit codes:
#   0 = success, secret on stdout (no trailing newline beyond keychain default)
#   1 = brain plugin not installed
#   2 = xbar not running
#   3 = keychain read failed
#   4 = bad usage (no key name)

set -e

KEY_NAME="${1:-}"
SERVICE="${BRAIN_SECRET_SERVICE:-fleet-nerve}"
PLUGIN_PATH="${BRAIN_PLUGIN_PATH:-$HOME/Library/Application Support/xbar/plugins/context-dna.1m.py}"
AUDIT_LOG="${BRAIN_SECRET_AUDIT_LOG:-/tmp/brain-secret-audit.log}"

if [ -z "$KEY_NAME" ]; then
    echo "usage: brain-secret.sh <key-name>" >&2
    echo "       (key-name = keychain account on service '$SERVICE')" >&2
    exit 4
fi

# Whitelist: only allow alphanumeric + underscore + dash. Prevents shell
# injection through the keychain name argument.
if ! [[ "$KEY_NAME" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "ERR: invalid key name (alphanumeric/_/- only)" >&2
    exit 4
fi

# Gate 1: brain plugin file must exist on this node. If a fleet machine
# hasn't been set up with the brain, it cannot grant secrets.
if [ ! -f "$PLUGIN_PATH" ]; then
    echo "ERR: brain plugin not installed at $PLUGIN_PATH" >&2
    echo "     install xbar + ContextDNA brain plugin to enable secrets" >&2
    exit 1
fi

# Gate 2: xbar process must be running. If the user has not started xbar,
# the brain is not visibly present in the menu bar — fail closed.
# Skippable via BRAIN_BROKER_SKIP_XBAR_CHECK=1 for headless test runs.
# 3s K5 tightening: a skip is itself a security-weakening event — record
# it so the audit trail flags any brain-gate bypass.
if [ -n "${BRAIN_BROKER_SKIP_XBAR_CHECK:-}" ]; then
    skip_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    skip_caller="${BRAIN_SECRET_CALLER:-${PPID:-?}}"
    skip_node="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
    echo "$skip_ts node=$skip_node caller=$skip_caller key=$KEY_NAME warn=xbar_check_skipped" \
        >> "$AUDIT_LOG" 2>/dev/null || true
elif ! pgrep -qx xbar 2>/dev/null && ! pgrep -qf "/Applications/xbar.app" 2>/dev/null; then
    echo "ERR: xbar not running — start xbar to activate brain broker" >&2
    exit 2
fi

# Gate 3: read the keychain. Items may be stored either as
#   a) service-only:        security find-generic-password -s <name> -w
#   b) service+account:     security find-generic-password -s <SERVICE> -a <name> -w
# We try (a) first because most fleet items live there (DEEPSEEK_API_KEY,
# Context_DNA_OPENAI, Context_DNA_Deep_Seek). Fall back to (b) for items
# stored under the configured umbrella SERVICE.
secret=""
if s=$(security find-generic-password -s "$KEY_NAME" -w 2>/dev/null); then
    secret="$s"
elif s=$(security find-generic-password -s "$SERVICE" -a "$KEY_NAME" -w 2>/dev/null); then
    secret="$s"
else
    echo "ERR: keychain item not found (tried -s $KEY_NAME, then -s $SERVICE -a $KEY_NAME)" >&2
    exit 3
fi
if [ -z "$secret" ]; then
    echo "ERR: keychain item resolved but empty: $KEY_NAME" >&2
    exit 3
fi

# Audit log: who, when, what (NOT the secret itself). Append-only, keeps
# the last 1000 lines via tail-truncate.
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
caller="${BRAIN_SECRET_CALLER:-${PPID:-?}}"
node="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
audit_line="$ts node=$node caller=$caller key=$KEY_NAME ok"

# Volatile log (/tmp — fast, but wiped on reboot).
echo "$audit_line" >> "$AUDIT_LOG" 2>/dev/null || true
if [ -f "$AUDIT_LOG" ] && [ "$(wc -l < "$AUDIT_LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 1000 "$AUDIT_LOG" > "${AUDIT_LOG}.tmp" && mv "${AUDIT_LOG}.tmp" "$AUDIT_LOG"
fi

# Durable log (.fleet/audits/brain-secret-audit.log — survives reboot, can
# be committed for fleet-wide forensic trail). 3s post-exec verification
# Q4 finding: /tmp alone is too volatile for the only secret-access trail.
DURABLE_LOG="${BRAIN_SECRET_DURABLE_LOG:-}"
if [ -z "$DURABLE_LOG" ]; then
    here_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo_root="$(cd "$here_dir/.." && pwd)"
    if [ -d "$repo_root/.fleet" ] || [ -d "$repo_root/.git" ]; then
        DURABLE_LOG="$repo_root/.fleet/audits/brain-secret-audit.log"
    fi
fi
if [ -n "$DURABLE_LOG" ]; then
    mkdir -p "$(dirname "$DURABLE_LOG")" 2>/dev/null || true
    echo "$audit_line" >> "$DURABLE_LOG" 2>/dev/null || true
    # Same tail-truncate semantics: cap at 2000 lines.
    if [ -f "$DURABLE_LOG" ] && [ "$(wc -l < "$DURABLE_LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
        tail -n 1000 "$DURABLE_LOG" > "${DURABLE_LOG}.tmp" && mv "${DURABLE_LOG}.tmp" "$DURABLE_LOG"
    fi
fi

printf '%s' "$secret"
