#!/usr/bin/env bash
# =============================================================================
# Action Registry CLI — V12 Vector Remediation
# =============================================================================
# Centralized action discovery, validation, and enforcement.
# Wraps the TypeScript registry for shell consumers.
#
# Usage:
#   ./scripts/action-registry.sh list              # List all actions
#   ./scripts/action-registry.sh stats             # Registry statistics
#   ./scripts/action-registry.sh find <id>         # Find action by ID
#   ./scripts/action-registry.sh validate <id>     # Validate single action
#   ./scripts/action-registry.sh validate-all      # Validate all actions
#
# Flags:
#   --json    Machine-readable JSON output (for scripts/hooks)
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Run action registry CLI via tsx
npx tsx context-dna/engine/actions/action-registry.ts "$@"
