#!/bin/bash
# =============================================================================
# Test: backup-context-dna.sh ZSF observability (T4)
# =============================================================================
# Verifies:
#   1. pg_dump failure (non-zero exit) → exit 0 by default, counter incremented,
#      stderr message visible. Other backup steps still run.
#   2. pg_dump failure → exit non-zero with --strict.
#   3. pg_dump silent failure (exit 0 + empty output) → counter incremented,
#      stderr message visible.
#   4. Healthy pg_dump path → no counter increment, success log line.
# =============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_SCRIPT="$REPO_ROOT/scripts/backup-context-dna.sh"

if [ ! -x "$BACKUP_SCRIPT" ]; then
    echo "FAIL: $BACKUP_SCRIPT not executable" >&2
    exit 1
fi

PASS=0
FAIL=0
CASES=0

_run_case() {
    local name="$1"
    shift
    CASES=$((CASES + 1))
    if "$@"; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Build a sandbox dir + fake `docker` shim that simulates each scenario.
SANDBOX="$(mktemp -d -t backup-zsf-XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

BACKUP_DIR="$SANDBOX/backups"
COUNTER_FILE="$SANDBOX/counters.txt"
STDERR_LOG="$SANDBOX/pg_dump.stderr.log"
SHIM_BIN="$SANDBOX/bin"
mkdir -p "$BACKUP_DIR" "$SHIM_BIN"

# Create a docker shim that:
#   - `docker ps` always reports contextdna-pg + contextdna-redis present
#   - `docker exec contextdna-pg pg_dump ...` behavior chosen via $DOCKER_PG_MODE:
#         "fail"      → exit 1 with stderr "permission denied"
#         "empty"     → exit 0 with no output  (silent-failure mode)
#         "healthy"   → exit 0 with realistic SQL dump (~2KB)
#   - `docker exec contextdna-redis redis-cli ... BGSAVE` always succeeds
#   - `docker cp` writes a stub file
cat > "$SHIM_BIN/docker" <<'SHIM_EOF'
#!/bin/bash
# Test shim for `docker`. Logs invocations to $DOCKER_SHIM_LOG.
echo "$@" >> "${DOCKER_SHIM_LOG:-/dev/null}"
case "$1" in
    ps)
        # Always report all containers running.
        echo "contextdna-pg"
        echo "contextdna-redis"
        echo "contextdna-seaweedfs"
        exit 0
        ;;
    exec)
        # docker exec <container> <cmd...>
        container="$2"
        shift 2
        cmd="$1"
        case "$container/$cmd" in
            contextdna-pg/pg_dump)
                case "${DOCKER_PG_MODE:-healthy}" in
                    fail)
                        echo "pg_dump: error: permission denied for database \"acontext\"" >&2
                        exit 1
                        ;;
                    empty)
                        # Exit 0 with no stdout — the actual failure mode
                        # that produced today's 20-byte gzip backup.
                        exit 0
                        ;;
                    healthy)
                        # Emit ~2KB of plausible SQL.
                        for i in $(seq 1 50); do
                            echo "-- table row $i: lorem ipsum dolor sit amet consectetur"
                        done
                        echo "INSERT INTO foo VALUES (1,'a'),(2,'b'),(3,'c');"
                        exit 0
                        ;;
                esac
                ;;
            contextdna-redis/redis-cli)
                exit 0
                ;;
            contextdna-seaweedfs/tar)
                # Emit a tiny but valid tar.
                printf 'fake-seaweedfs-tar'
                exit 0
                ;;
        esac
        exit 0
        ;;
    cp)
        # Write a stub at the destination path.
        dest="${3:-/dev/null}"
        printf 'fake-rdb' > "$dest" 2>/dev/null || true
        exit 0
        ;;
esac
exit 0
SHIM_EOF
chmod +x "$SHIM_BIN/docker"

# Helper: run the script with the shim ahead of PATH.
# Uses `env -i` + a fresh `bash --noprofile --norc` so the shell starts
# with no inherited command cache and no user rc files (which on macOS
# can re-add /usr/local/bin or /opt/homebrew/bin ahead of SHIM_BIN and
# resolve `docker` to the real binary).
_run_backup() {
    local mode="$1"
    shift
    env -i \
        HOME="$SANDBOX" \
        DOCKER_PG_MODE="$mode" \
        DOCKER_SHIM_LOG="$SANDBOX/docker.log" \
        BACKUP_DIR="$BACKUP_DIR" \
        BACKUP_COUNTER_FILE="$COUNTER_FILE" \
        BACKUP_PG_DUMP_STDERR_LOG="$STDERR_LOG" \
        PATH="$SHIM_BIN:/usr/bin:/bin" \
        bash --noprofile --norc "$BACKUP_SCRIPT" "$@" "$BACKUP_DIR"
}

# Reset state between cases.
_reset() {
    rm -f "$COUNTER_FILE" "$STDERR_LOG"
    rm -f "$BACKUP_DIR"/postgres_*.sql.gz
    rm -f "$BACKUP_DIR"/redis_*.rdb
    rm -f "$BACKUP_DIR"/seaweedfs_*.tar.gz
}

case_1_pg_dump_failure_default() {
    _reset
    local out err rc
    out="$(_run_backup fail 2> "$SANDBOX/stderr.txt")"
    rc=$?
    err="$(cat "$SANDBOX/stderr.txt")"

    # Default mode: pipeline-level rc must be 0 (other backups still ran).
    if [ "$rc" -ne 0 ]; then
        echo "  expected rc=0 (default mode), got rc=$rc" >&2
        return 1
    fi
    # Counter file must contain the pg_dump_errors line.
    if ! grep -q "backup_pg_dump_errors_total" "$COUNTER_FILE" 2>/dev/null; then
        echo "  counter file missing backup_pg_dump_errors_total" >&2
        echo "  --- counter file ---" >&2
        cat "$COUNTER_FILE" 2>/dev/null >&2 || echo "(empty)" >&2
        return 1
    fi
    # Stderr must mention the failure (not silent).
    if ! echo "$err" | grep -qi "PostgreSQL backup pipeline failed"; then
        echo "  stderr did not include 'PostgreSQL backup pipeline failed'" >&2
        echo "  --- stderr ---" >&2
        echo "$err" >&2
        return 1
    fi
    return 0
}

case_2_pg_dump_failure_strict() {
    _reset
    local rc
    _run_backup fail --strict > /dev/null 2> "$SANDBOX/stderr.txt"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "  expected non-zero rc with --strict, got rc=0" >&2
        return 1
    fi
    if ! grep -qi "aborting" "$SANDBOX/stderr.txt"; then
        echo "  --strict did not log 'aborting'" >&2
        return 1
    fi
    return 0
}

case_3_pg_dump_silent_empty() {
    _reset
    local rc
    _run_backup empty > /dev/null 2> "$SANDBOX/stderr.txt"
    rc=$?
    # Default mode → exit 0 even though backup is undersized.
    if [ "$rc" -ne 0 ]; then
        echo "  expected rc=0 (default), got rc=$rc" >&2
        return 1
    fi
    if ! grep -q "backup_pg_dump_undersized_total" "$COUNTER_FILE" 2>/dev/null; then
        echo "  expected backup_pg_dump_undersized_total counter increment" >&2
        return 1
    fi
    if ! grep -qi "suspiciously small" "$SANDBOX/stderr.txt"; then
        echo "  expected 'suspiciously small' warning on stderr" >&2
        return 1
    fi
    return 0
}

case_4_pg_dump_healthy() {
    _reset
    local rc
    _run_backup healthy > "$SANDBOX/stdout.txt" 2> "$SANDBOX/stderr.txt"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  healthy backup returned rc=$rc" >&2
        cat "$SANDBOX/stderr.txt" >&2
        return 1
    fi
    # Counter file should exist but should NOT contain any pg_dump error/undersized lines.
    if [ -f "$COUNTER_FILE" ] && grep -qE "backup_pg_dump_(errors|undersized)_total" "$COUNTER_FILE"; then
        echo "  healthy backup unexpectedly incremented error/undersized counter" >&2
        cat "$COUNTER_FILE" >&2
        return 1
    fi
    if ! grep -q "PostgreSQL backup complete" "$SANDBOX/stdout.txt"; then
        echo "  expected 'PostgreSQL backup complete' on stdout" >&2
        return 1
    fi
    return 0
}

echo "Running backup-context-dna.sh ZSF tests..."
_run_case "pg_dump fail (default mode)"  case_1_pg_dump_failure_default
_run_case "pg_dump fail (--strict)"      case_2_pg_dump_failure_strict
_run_case "pg_dump empty (silent)"       case_3_pg_dump_silent_empty
_run_case "pg_dump healthy"              case_4_pg_dump_healthy

echo ""
echo "Result: $PASS/$CASES passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
exit 0
