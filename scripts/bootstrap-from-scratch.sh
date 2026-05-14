#!/usr/bin/env bash
# bootstrap-from-scratch.sh — README-promised entry point.
#
# This is a thin wrapper around `bootstrap-verify.sh`, which is the actual
# end-to-end reconstitution script (clone → docker compose → daemon → webhook
# probe → BOOTSTRAP-VERIFIED). The README quickstart promises a script with
# this name; rather than break the README contract or rename the test, we
# expose this alias so a fresh cloner can copy-paste from the README and have
# it work.
#
# All flags are forwarded verbatim. See bootstrap-verify.sh for usage details.
#
# Usage:
#   ./scripts/bootstrap-from-scratch.sh [--profile lite|heavy] [--target PATH]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/bootstrap-verify.sh"

if [[ ! -x "${TARGET}" ]]; then
  echo "bootstrap-from-scratch.sh: cannot find ${TARGET}" >&2
  echo "  This wrapper requires bootstrap-verify.sh in the same scripts/ directory." >&2
  exit 1
fi

exec "${TARGET}" "$@"
