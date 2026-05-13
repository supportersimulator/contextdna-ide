#!/bin/bash
# =============================================================================
# Context DNA Rollback Script
# =============================================================================
# Restores Context DNA from a timestamped backup:
# - PostgreSQL database
# - Redis data
# - SeaweedFS artifacts
#
# Usage: ./rollback-context-dna.sh <timestamp>
#   Example: ./rollback-context-dna.sh 20260125_020000
#
# Environment Variables:
#   BACKUP_DIR - Directory containing backups (default: /var/backups/context-dna)
# =============================================================================

set -euo pipefail

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/var/backups/context-dna}"
TIMESTAMP="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${CYAN}[STEP]${NC} $1"; }

# =============================================================================
# Validation
# =============================================================================

if [ -z "$TIMESTAMP" ]; then
    log_error "Usage: $0 <timestamp>"
    echo ""
    echo "Available backups:"
    echo "=================="
    if [ -d "$BACKUP_DIR" ]; then
        ls -la "$BACKUP_DIR"/postgres_*.sql.gz 2>/dev/null | sed 's/.*postgres_/  /; s/.sql.gz//' | head -20
    else
        echo "  No backups found in $BACKUP_DIR"
    fi
    echo ""
    echo "Example: $0 20260125_020000"
    exit 1
fi

# Check backup files exist
POSTGRES_BACKUP="$BACKUP_DIR/postgres_$TIMESTAMP.sql.gz"
REDIS_BACKUP="$BACKUP_DIR/redis_$TIMESTAMP.rdb"
SEAWEEDFS_BACKUP="$BACKUP_DIR/seaweedfs_$TIMESTAMP.tar.gz"

log_info "Checking backup files for timestamp: $TIMESTAMP"

MISSING=0
if [ ! -f "$POSTGRES_BACKUP" ]; then
    log_warn "PostgreSQL backup not found: $POSTGRES_BACKUP"
    MISSING=$((MISSING + 1))
fi
if [ ! -f "$REDIS_BACKUP" ]; then
    log_warn "Redis backup not found: $REDIS_BACKUP"
    MISSING=$((MISSING + 1))
fi
if [ ! -f "$SEAWEEDFS_BACKUP" ]; then
    log_warn "SeaweedFS backup not found: $SEAWEEDFS_BACKUP"
    MISSING=$((MISSING + 1))
fi

if [ $MISSING -eq 3 ]; then
    log_error "No backup files found for timestamp: $TIMESTAMP"
    exit 1
fi

# =============================================================================
# Confirmation
# =============================================================================

echo ""
echo -e "${RED}=== WARNING: DESTRUCTIVE OPERATION ===${NC}"
echo ""
echo "This will restore Context DNA to the state from: $TIMESTAMP"
echo "All current data will be REPLACED with backup data."
echo ""
echo "Backup files to restore:"
[ -f "$POSTGRES_BACKUP" ] && echo "  - PostgreSQL: $(du -h "$POSTGRES_BACKUP" | cut -f1)"
[ -f "$REDIS_BACKUP" ] && echo "  - Redis: $(du -h "$REDIS_BACKUP" | cut -f1)"
[ -f "$SEAWEEDFS_BACKUP" ] && echo "  - SeaweedFS: $(du -h "$SEAWEEDFS_BACKUP" | cut -f1)"
echo ""

read -p "Are you sure you want to proceed? (type 'yes' to confirm): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    log_info "Rollback cancelled."
    exit 0
fi

# =============================================================================
# Stop Services
# =============================================================================

log_step "Stopping Context DNA services..."

cd "$REPO_ROOT/context-dna/infra" 2>/dev/null || cd "$REPO_ROOT/context-dna"

if [ -f "docker-compose.yaml" ]; then
    docker-compose -f docker-compose.yaml stop contextdna-core contextdna-api contextdna-ui contextdna-helper-agent 2>/dev/null || true
elif [ -f "docker-compose.yml" ]; then
    docker-compose -f docker-compose.yml stop 2>/dev/null || true
fi

log_info "Services stopped"

# =============================================================================
# PostgreSQL Restore
# =============================================================================

if [ -f "$POSTGRES_BACKUP" ]; then
    log_step "Restoring PostgreSQL..."

    if docker ps --format '{{.Names}}' | grep -q 'contextdna-pg'; then
        # Drop and recreate database
        docker exec contextdna-pg psql -U acontext -d postgres -c "DROP DATABASE IF EXISTS acontext;" 2>/dev/null || true
        docker exec contextdna-pg psql -U acontext -d postgres -c "CREATE DATABASE acontext;" 2>/dev/null || true

        # Restore from backup
        gunzip -c "$POSTGRES_BACKUP" | docker exec -i contextdna-pg psql -U acontext acontext 2>/dev/null

        log_info "PostgreSQL restored successfully"
    else
        log_error "PostgreSQL container not running"
    fi
fi

# =============================================================================
# Redis Restore
# =============================================================================

if [ -f "$REDIS_BACKUP" ]; then
    log_step "Restoring Redis..."

    if docker ps --format '{{.Names}}' | grep -q 'contextdna-redis'; then
        # Stop Redis, copy RDB, restart
        docker stop contextdna-redis 2>/dev/null || true
        docker cp "$REDIS_BACKUP" contextdna-redis:/data/dump.rdb 2>/dev/null
        docker start contextdna-redis 2>/dev/null || true

        # Wait for Redis to come back
        sleep 3
        docker exec contextdna-redis redis-cli -a "${REDIS_PASSWORD:-INSECURE_DEFAULT_CHANGE_ME}" PING 2>/dev/null && \
            log_info "Redis restored successfully" || \
            log_warn "Redis may need manual verification"
    else
        log_warn "Redis container not found"
    fi
fi

# =============================================================================
# SeaweedFS Restore
# =============================================================================

if [ -f "$SEAWEEDFS_BACKUP" ]; then
    log_step "Restoring SeaweedFS..."

    if docker ps --format '{{.Names}}' | grep -q 'contextdna-seaweedfs'; then
        # Clear existing data and restore
        docker exec contextdna-seaweedfs rm -rf /data/* 2>/dev/null || true
        docker cp "$SEAWEEDFS_BACKUP" contextdna-seaweedfs:/tmp/restore.tar.gz 2>/dev/null
        docker exec contextdna-seaweedfs tar xzf /tmp/restore.tar.gz -C / 2>/dev/null
        docker exec contextdna-seaweedfs rm /tmp/restore.tar.gz 2>/dev/null || true

        log_info "SeaweedFS restored successfully"
    else
        log_warn "SeaweedFS container not found"
    fi
fi

# =============================================================================
# Restart Services
# =============================================================================

log_step "Restarting Context DNA services..."

if [ -f "docker-compose.yaml" ]; then
    docker-compose -f docker-compose.yaml up -d 2>/dev/null
elif [ -f "docker-compose.yml" ]; then
    docker-compose -f docker-compose.yml up -d 2>/dev/null
fi

# Wait for services to start
log_info "Waiting for services to become healthy..."
sleep 10

# =============================================================================
# Verification
# =============================================================================

log_step "Verifying restoration..."

echo ""
if docker ps --format '{{.Names}}' | grep -q 'contextdna-pg'; then
    echo -e "${GREEN}[OK]${NC} PostgreSQL running"
else
    echo -e "${RED}[FAIL]${NC} PostgreSQL not running"
fi

if docker ps --format '{{.Names}}' | grep -q 'contextdna-redis'; then
    echo -e "${GREEN}[OK]${NC} Redis running"
else
    echo -e "${RED}[FAIL]${NC} Redis not running"
fi

if docker ps --format '{{.Names}}' | grep -q 'contextdna-seaweedfs'; then
    echo -e "${GREEN}[OK]${NC} SeaweedFS running"
else
    echo -e "${RED}[FAIL]${NC} SeaweedFS not running"
fi

if docker ps --format '{{.Names}}' | grep -q 'contextdna-api'; then
    echo -e "${GREEN}[OK]${NC} API running"
else
    echo -e "${YELLOW}[WARN]${NC} API not running (may need to start manually)"
fi

echo ""
log_info "=== Rollback Complete ==="
log_info "Restored from timestamp: $TIMESTAMP"
log_info "Please verify data integrity before resuming operations."
