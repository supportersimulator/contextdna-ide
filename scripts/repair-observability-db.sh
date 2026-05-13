#!/usr/bin/env bash
# repair-observability-db.sh — Rebuild corrupted .observability.db
#
# The .observability.db has recurring corruption (B-tree page depth errors,
# rowid out of order, duplicate page references). This script:
#   1. Checks if the DB is locked by another process
#   2. Recovers data using sqlite3 .recover (more resilient than .dump)
#   3. Imports into a clean DB with WAL mode enabled
#   4. Verifies integrity and row counts
#   5. Swaps old→backup, new→active
#
# Usage: ./scripts/repair-observability-db.sh [--force]
#   --force: Skip the "is the DB locked?" check (use with caution)
#
# Critical #4 fix from critical-findings-verification-2026-02-26.md

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="$REPO_DIR/memory/.observability.db"
RECOVER_SQL="/tmp/observability_recover_$(date +%Y%m%d_%H%M%S).sql"
NEW_DB="/tmp/observability_rebuilt_$(date +%Y%m%d_%H%M%S).db"
BACKUP_SUFFIX=".corrupted-$(date +%Y%m%d-%H%M%S)"

FORCE=false
if [[ "${1:-}" == "--force" ]]; then
  FORCE=true
fi

echo "=== .observability.db Repair Tool ==="
echo "DB: $DB_PATH"
echo ""

# 1. Check if DB exists
if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: DB not found at $DB_PATH"
  exit 1
fi

# 2. Check integrity first
echo "Step 1: Checking current integrity..."
INTEGRITY=$(sqlite3 "$DB_PATH" "PRAGMA integrity_check;" 2>&1 | head -1)
if [[ "$INTEGRITY" == "ok" ]]; then
  echo "  DB passes integrity check. No repair needed."
  exit 0
fi
echo "  Corruption detected: $INTEGRITY"
echo ""

# 3. Check if DB is locked
if [[ "$FORCE" != "true" ]]; then
  echo "Step 2: Checking for active users..."
  USERS=$(lsof "$DB_PATH" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')
  if [[ "$USERS" -gt 0 ]]; then
    echo "  ERROR: $USERS processes have the DB open:"
    lsof "$DB_PATH" 2>/dev/null | head -5
    echo ""
    echo "  Stop the scheduler first: launchctl unload ~/Library/LaunchAgents/com.contextdna.scheduler.plist"
    echo "  Or use --force to proceed anyway (risky if process is writing)"
    exit 1
  fi
  echo "  No active users. Safe to proceed."
else
  echo "Step 2: Skipped (--force mode)"
fi
echo ""

# 4. Recover data
echo "Step 3: Recovering data with .recover..."
sqlite3 "$DB_PATH" ".recover" 2>/dev/null > "$RECOVER_SQL"
LINES=$(wc -l < "$RECOVER_SQL" | tr -d ' ')
echo "  Recovered $LINES lines of SQL"

# Check for clean COMMIT
if ! tail -5 "$RECOVER_SQL" | grep -q "COMMIT;"; then
  echo "  WARNING: Recovery SQL does not end with COMMIT. Data may be incomplete."
fi
echo ""

# 5. Import into clean DB
echo "Step 4: Building clean database..."
sqlite3 "$NEW_DB" < "$RECOVER_SQL" 2>&1 | tail -3
sqlite3 "$NEW_DB" "PRAGMA journal_mode=wal;" > /dev/null
echo "  Clean DB created at $NEW_DB"
echo ""

# 6. Verify integrity
echo "Step 5: Verifying clean DB integrity..."
NEW_INTEGRITY=$(sqlite3 "$NEW_DB" "PRAGMA integrity_check;" 2>&1 | head -1)
if [[ "$NEW_INTEGRITY" != "ok" ]]; then
  echo "  ERROR: Clean DB ALSO fails integrity check: $NEW_INTEGRITY"
  echo "  Recovery failed. Manual intervention needed."
  exit 1
fi
echo "  Integrity: OK"

# 7. Compare key row counts
echo "Step 6: Comparing row counts..."
TABLES="task_run_event butler_code_note dependency_status_event sync_history knowledge_quarantine"
ALL_MATCH=true
for tbl in $TABLES; do
  OLD=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM $tbl;" 2>/dev/null || echo "ERROR")
  NEW=$(sqlite3 "$NEW_DB" "SELECT COUNT(*) FROM $tbl;" 2>/dev/null || echo "ERROR")
  STATUS="OK"
  if [[ "$OLD" != "$NEW" ]]; then
    STATUS="MISMATCH"
    ALL_MATCH=false
  fi
  printf "  %-30s old=%-8s new=%-8s %s\n" "$tbl" "$OLD" "$NEW" "$STATUS"
done

if [[ "$ALL_MATCH" != "true" ]]; then
  echo ""
  echo "  WARNING: Some row counts don't match. Proceeding but review carefully."
fi
echo ""

# 8. Swap
echo "Step 7: Swapping databases..."
OLD_SIZE=$(ls -lh "$DB_PATH" | awk '{print $5}')
NEW_SIZE=$(ls -lh "$NEW_DB" | awk '{print $5}')
echo "  Old: $OLD_SIZE → New: $NEW_SIZE"

# Remove WAL/SHM files first
rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"

# Backup corrupt DB
mv "$DB_PATH" "${DB_PATH}${BACKUP_SUFFIX}"
echo "  Backup: ${DB_PATH}${BACKUP_SUFFIX}"

# Move clean DB in
mv "$NEW_DB" "$DB_PATH"
echo "  Swapped successfully."
echo ""

# Cleanup recovery SQL
rm -f "$RECOVER_SQL"

echo "=== Repair Complete ==="
echo "Old DB backed up to: ${DB_PATH}${BACKUP_SUFFIX}"
echo "New DB: $DB_PATH (WAL mode, integrity OK)"
echo ""
echo "Restart scheduler: launchctl load ~/Library/LaunchAgents/com.contextdna.scheduler.plist"
