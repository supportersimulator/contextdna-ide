#!/bin/bash
# =============================================================================
# CONTEXT DNA: Development → Product Sync Script
# =============================================================================
#
# This script syncs evolving memory modules from local development (memory/)
# to the Context DNA product package (context-dna/core/src/context_dna/).
#
# DOGFOODING PRINCIPLE:
# You use the product daily → You improve the code → Code syncs to product
# Your data stays LOCAL (in Docker volumes) → Never in the product repo
#
# Usage:
#   ./scripts/sync-memory-to-product.sh           # Sync all modules
#   ./scripts/sync-memory-to-product.sh --dry-run # Preview what would sync
#   ./scripts/sync-memory-to-product.sh --diff    # Show differences
#
# =============================================================================

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMORY_DIR="$REPO_ROOT/memory"
PRODUCT_DIR="$REPO_ROOT/context-dna/core/src/context_dna"

# Modules to sync (core detection + evolution + audit)
SYNC_MODULES=(
    "pattern_registry.py"
    "temporal_validator.py"
    "llm_success_analyzer.py"
    "enhanced_success_detector.py"
    "win_audit.py"
    "objective_success.py"
    "brain.py"
    "architecture_enhancer.py"
    "sop_types.py"
    "pattern_evolution.py"
    "pattern_manager.py"
)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}══════════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  CONTEXT DNA: Development → Product Sync${NC}"
echo -e "${BLUE}══════════════════════════════════════════════════════════════════${NC}"
echo ""

# Parse arguments
DRY_RUN=false
SHOW_DIFF=false
for arg in "$@"; do
    case $arg in
        --dry-run)
            DRY_RUN=true
            echo -e "${YELLOW}⚠ DRY RUN MODE - No files will be modified${NC}"
            ;;
        --diff)
            SHOW_DIFF=true
            ;;
    esac
done

echo -e "Source:      ${GREEN}$MEMORY_DIR${NC}"
echo -e "Destination: ${GREEN}$PRODUCT_DIR${NC}"
echo ""

# Track changes
SYNCED=0
SKIPPED=0
MISSING=0

for module in "${SYNC_MODULES[@]}"; do
    SOURCE="$MEMORY_DIR/$module"
    DEST="$PRODUCT_DIR/$module"

    if [ ! -f "$SOURCE" ]; then
        echo -e "${RED}✗ MISSING:${NC} $module (not found in memory/)"
        ((MISSING++))
        continue
    fi

    if [ -f "$DEST" ]; then
        # Check if different
        if ! diff -q "$SOURCE" "$DEST" > /dev/null 2>&1; then
            if [ "$SHOW_DIFF" = true ]; then
                echo -e "${YELLOW}━━━ DIFF: $module ━━━${NC}"
                diff --color=always "$DEST" "$SOURCE" | head -50 || true
                echo ""
            fi

            if [ "$DRY_RUN" = true ]; then
                echo -e "${YELLOW}○ WOULD SYNC:${NC} $module (modified)"
            else
                cp "$SOURCE" "$DEST"
                echo -e "${GREEN}✓ SYNCED:${NC} $module"
            fi
            ((SYNCED++))
        else
            echo -e "${BLUE}≡ UP-TO-DATE:${NC} $module"
            ((SKIPPED++))
        fi
    else
        # New file
        if [ "$DRY_RUN" = true ]; then
            echo -e "${YELLOW}○ WOULD CREATE:${NC} $module (new)"
        else
            cp "$SOURCE" "$DEST"
            echo -e "${GREEN}✓ CREATED:${NC} $module"
        fi
        ((SYNCED++))
    fi
done

echo ""
echo -e "${BLUE}══════════════════════════════════════════════════════════════════${NC}"
echo -e "  Summary: ${GREEN}$SYNCED synced${NC}, ${BLUE}$SKIPPED up-to-date${NC}, ${RED}$MISSING missing${NC}"
echo -e "${BLUE}══════════════════════════════════════════════════════════════════${NC}"

if [ "$DRY_RUN" = true ] && [ $SYNCED -gt 0 ]; then
    echo ""
    echo -e "${YELLOW}Run without --dry-run to apply changes${NC}"
fi
