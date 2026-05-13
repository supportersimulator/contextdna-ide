#!/bin/bash
# ============================================================================
# Competition Submission Governance Gate — pre-publish verification
# ============================================================================
# Submissions go through this gate BEFORE they leave the fleet (publish to
# Kaggle, write to submissions/, broadcast over NATS). Pattern cloned from
# scripts/gains-gate.sh — same severity vocabulary, same audit log shape.
#
# 8 named checks (see scripts/competition_submission_gate.py for the full
# definitions):
#   1. artifact-exists           critical
#   2. metadata-schema           critical (S1 governed_packet, fallback OK)
#   3. determinism               critical (replay) / warning (static-only)
#   4. constitutional-signoff    critical (3-Surgeon decision exists)
#   5. evidence-ledger           critical (S2 write, fallback OK)
#   6. leaderboard-guard         critical (S4 verdict, fallback OK)
#   7. no-secrets                critical (regex scan, mf secret_redact)
#   8. reversibility-path        critical (artifact under submissions/)
#
# Usage:
#   scripts/competition-submission-gate.sh \
#       --artifact <path> --metadata <metadata.json> [--repo <root>] [--json]
#
# Exit:
#   0 — all critical checks PASS
#   1 — at least one CRITICAL failure (gate blocks)
#   2 — setup error (bad arguments / unparseable metadata)
#
# Audit:
#   .fleet/audits/<YYYY-MM-DD>-submission-gate.log  (JSON-line per run)
#
# Amplification surfaces:
#   * Reuses the audit pipeline directory convention from
#     multi-fleet/multifleet/audit_log.py.
#   * Reuses the secret-shape patterns from
#     multi-fleet/multifleet/secret_redact.py.
#   * S1 governed_packet / S2 EvidenceLedger / S4 leaderboard_guard are all
#     called via graceful fallback so the gate ships independent of those
#     modules — failures degrade to warnings, not silence.
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
HELPER="$SCRIPT_DIR/competition_submission_gate.py"

if [[ ! -f "$HELPER" ]]; then
    echo "[submission-gate] FAIL: helper missing: $HELPER" >&2
    exit 2
fi

PY="$REPO_DIR/.venv/bin/python3"
[[ ! -x "$PY" ]] && PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
    echo "[submission-gate] FAIL: python3 not available" >&2
    exit 2
fi

# Pass through all arguments — Python helper owns argparse.
exec "$PY" "$HELPER" --repo "$REPO_DIR" "$@"
