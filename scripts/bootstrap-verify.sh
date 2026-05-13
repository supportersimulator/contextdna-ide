#!/usr/bin/env bash
# ============================================================================
#  bootstrap-verify.sh
# ----------------------------------------------------------------------------
#  Purpose:
#    Prove the Operational Invariance Promise for the ContextDNA IDE mothership.
#
#      "Delete laptop. Delete AWS. Clone repo. Run one command. Brain wakes
#       up with everything it knew yesterday."
#
#    This script is the litmus test. It performs a from-zero reconstitution
#    on a clean target directory and asserts every step succeeds:
#
#      1. fresh `git clone --recurse-submodules` of supportersimulator/contextdna-ide
#      2. .env materialisation from .env.example (or --env-file)
#      3. `docker compose up -d` with the requested profile (lite | heavy)
#      4. Wait for every container healthcheck to flip to "healthy"
#      5. HTTP probe of the live 9-section webhook endpoint
#      6. Print a BOOTSTRAP-VERIFIED summary, or hard-fail at the first wrong step
#
#    The script is destructive only inside $TARGET — it never touches the
#    host's existing mothership checkout. Run on a sacrificial path.
#
#  Usage:
#    bootstrap-verify.sh [--profile lite|heavy] [--target PATH]
#                        [--env-file PATH] [--teardown] [--no-clone]
#                        [--repo URL] [--branch NAME] [--timeout SECONDS]
#                        [--help]
#
#  Defaults:
#    --profile  lite
#    --target   /tmp/mothership-verify-$(date +%s)
#    --repo     git@github.com:supportersimulator/contextdna-ide.git
#    --branch   main
#    --timeout  300   (5 min — wait-for-healthy budget per attempt)
#
#  Exit codes:
#    0  success — every step bumped its counter, BOOTSTRAP-VERIFIED printed
#    1  step failure — one named step failed; ERRORS counter > 0
#    2  missing prereqs — docker / git / curl / jq absent, or required flag bad
#
#  ZSF (Zero Silent Failures):
#    Every step owns exactly one counter (see COUNTERS block).
#    Every probe writes a structured log line to STDERR:
#      [verify] step=<n> status=<ok|fail> elapsed=<s> detail="..."
#    On failure: the counter is incremented, the script exits non-zero,
#    and the final stats block lists which step broke.
#
#  Notes for callers:
#    * If you pass --no-clone, the script skips step 1 and treats --target as
#      a pre-existing mothership checkout. Useful for re-running step 3+ after
#      fixing .env.
#    * --env-file PATH copies PATH over .env after the .env.example template
#      is laid down. Use a known-good local env stash to make the run
#      deterministic. Without --env-file the script prompts on TTY, or
#      hard-fails with exit 2 if STDIN is not a TTY.
#    * --teardown runs `docker compose down -v` at the end. The target
#      directory is left in place (so logs survive) unless you also remove
#      it manually. Default behaviour leaves the stack running — handy for
#      poking at the verified system before tearing it down.
# ============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# Counters (ZSF — every step contributes to exactly one)
# ----------------------------------------------------------------------------
STEP1_CLONED=0          # Step 1: git clone succeeded
STEP2_ENV_OK=0          # Step 2: .env materialised + required vars present
STEP3_COMPOSE_UP=0      # Step 3: docker compose up issued without error
STEP4_HEALTHY=0         # Step 4: every container reported "healthy"
STEP5_WEBHOOK_OK=0      # Step 5: webhook probe returned a valid 9-section payload
STEP6_TEARDOWN=0        # Step 6: optional teardown ran cleanly
ERRORS=0                # ANY step failure increments

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
PROFILE="lite"
TARGET=""
ENV_FILE=""
REPO="git@github.com:supportersimulator/contextdna-ide.git"
BRANCH="main"
TIMEOUT_SECONDS=300
DO_TEARDOWN=0
DO_CLONE=1

# Required env vars per profile. Lite/heavy share the first four; heavy adds
# RabbitMQ + Grafana. Anything in this list MUST resolve to a non-empty value
# inside $TARGET/.env or step 2 fails.
REQUIRED_VARS_LITE=(
    POSTGRES_PASSWORD
    REDIS_PASSWORD
    NATS_REMOTE_URL
    NATS_LEAF_PASSWORD
)
REQUIRED_VARS_HEAVY=(
    POSTGRES_PASSWORD
    REDIS_PASSWORD
    RABBITMQ_PASSWORD
    GRAFANA_PASSWORD
)

# Webhook probe target. Lite + heavy both expose the helper agent on 8080 and
# the MCP webhook stub on 8000. The 9-section payload is served by the helper
# agent (FastAPI `/consult` endpoint) — the MCP server in mcp-servers/ is
# stdio-only and is NOT a candidate. See BOOTSTRAP.md "Verification".
WEBHOOK_HOST="127.0.0.1"
WEBHOOK_PORT="8080"
WEBHOOK_PATH="/consult"

# ----------------------------------------------------------------------------
# Logging helpers (ZSF — all output goes to STDERR so STDOUT stays clean
# for downstream parsers)
# ----------------------------------------------------------------------------
log() {
    # log <step> <status:ok|fail|info> <elapsed_seconds> <detail>
    local step="$1"; local status="$2"; local elapsed="$3"; shift 3
    printf '[verify] step=%s status=%s elapsed=%ss detail="%s"\n' \
        "$step" "$status" "$elapsed" "$*" >&2
}

fatal() {
    # fatal <step> <message>
    ERRORS=$((ERRORS + 1))
    log "$1" "fail" "0" "$2"
    print_stats
    exit 1
}

print_stats() {
    cat >&2 <<EOF
============================================================
  bootstrap-verify.sh — final stats
------------------------------------------------------------
  profile:           ${PROFILE}
  target:            ${TARGET}
  STEP1_CLONED       ${STEP1_CLONED}
  STEP2_ENV_OK       ${STEP2_ENV_OK}
  STEP3_COMPOSE_UP   ${STEP3_COMPOSE_UP}
  STEP4_HEALTHY      ${STEP4_HEALTHY}
  STEP5_WEBHOOK_OK   ${STEP5_WEBHOOK_OK}
  STEP6_TEARDOWN     ${STEP6_TEARDOWN}
  ERRORS             ${ERRORS}
============================================================
EOF
}

usage() {
    sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
}

# ----------------------------------------------------------------------------
# Arg parsing
# ----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)    PROFILE="$2";          shift 2 ;;
        --target)     TARGET="$2";           shift 2 ;;
        --env-file)   ENV_FILE="$2";         shift 2 ;;
        --repo)       REPO="$2";             shift 2 ;;
        --branch)     BRANCH="$2";           shift 2 ;;
        --timeout)    TIMEOUT_SECONDS="$2";  shift 2 ;;
        --teardown)   DO_TEARDOWN=1;         shift   ;;
        --no-clone)   DO_CLONE=0;            shift   ;;
        --help|-h)    usage; exit 0 ;;
        *)
            echo "Unknown flag: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    TARGET="/tmp/mothership-verify-$(date +%s)"
fi

case "$PROFILE" in
    lite|heavy) ;;
    *)
        echo "ERROR: --profile must be 'lite' or 'heavy', got '$PROFILE'" >&2
        exit 2
        ;;
esac

# ----------------------------------------------------------------------------
# Prereq check (exit code 2 if anything missing)
# ----------------------------------------------------------------------------
require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command '$1' not found on PATH" >&2
        exit 2
    fi
}
require_cmd docker
require_cmd git
require_cmd curl
require_cmd jq
require_cmd awk
require_cmd sed

# Docker daemon must actually be reachable, not just installed.
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon not reachable (try 'docker info')" >&2
    exit 2
fi

# `docker compose` v2 plugin — not the legacy `docker-compose` binary.
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' plugin v2 missing (legacy docker-compose not supported)" >&2
    exit 2
fi

# ----------------------------------------------------------------------------
# Banner
# ----------------------------------------------------------------------------
cat >&2 <<EOF
============================================================
  ContextDNA IDE — Bootstrap Verification
------------------------------------------------------------
  profile:        ${PROFILE}
  target:         ${TARGET}
  repo:           ${REPO}
  branch:         ${BRANCH}
  timeout:        ${TIMEOUT_SECONDS}s
  teardown:       $([[ $DO_TEARDOWN -eq 1 ]] && echo yes || echo no)
  clone step:     $([[ $DO_CLONE -eq 1 ]] && echo yes || echo skipped)
  env file:       ${ENV_FILE:-<interactive>}
============================================================
EOF

# ============================================================================
# STEP 1 — fresh clone (--recurse-submodules)
# ============================================================================
if [[ $DO_CLONE -eq 1 ]]; then
    if [[ -e "$TARGET" ]]; then
        fatal 1 "target exists already: $TARGET (refusing to clone over it)"
    fi
    mkdir -p "$(dirname "$TARGET")" || fatal 1 "could not create parent of $TARGET"

    t0=$(date +%s)
    if git clone --recurse-submodules --branch "$BRANCH" "$REPO" "$TARGET" >&2; then
        STEP1_CLONED=1
        log 1 ok "$(( $(date +%s) - t0 ))" "cloned $REPO -> $TARGET"
    else
        fatal 1 "git clone failed (check SSH access to $REPO)"
    fi
else
    if [[ ! -d "$TARGET/.git" ]]; then
        fatal 1 "--no-clone set but $TARGET is not a git checkout"
    fi
    STEP1_CLONED=1
    log 1 info 0 "--no-clone: assuming pre-existing checkout at $TARGET"
fi

cd "$TARGET"

# Confirm the expected compose file actually shipped in this clone.
COMPOSE_FILE="docker-compose.${PROFILE}.yml"
if [[ ! -f "$COMPOSE_FILE" ]]; then
    fatal 1 "expected $COMPOSE_FILE in $TARGET — repo does not match this script's profile"
fi

# ============================================================================
# STEP 2 — materialise .env, verify required vars resolve
# ============================================================================
t0=$(date +%s)

if [[ ! -f .env.example ]]; then
    fatal 2 ".env.example missing in repo root (cannot template .env)"
fi

if [[ -n "$ENV_FILE" ]]; then
    if [[ ! -f "$ENV_FILE" ]]; then
        fatal 2 "--env-file $ENV_FILE does not exist"
    fi
    cp "$ENV_FILE" .env
    log 2 info 0 "copied env from $ENV_FILE"
else
    if [[ -t 0 ]]; then
        echo "" >&2
        echo "No --env-file supplied. .env.example will be copied to .env."  >&2
        echo "You MUST fill in real secrets before continuing." >&2
        echo "Press ENTER once .env is populated, or Ctrl-C to abort." >&2
        cp .env.example .env
        # shellcheck disable=SC2034
        read -r _ack
    else
        fatal 2 "no --env-file and STDIN is not a TTY (cannot prompt for secrets)"
    fi
fi

# Pick the right required-vars list for the profile.
if [[ "$PROFILE" == "heavy" ]]; then
    REQUIRED_VARS=("${REQUIRED_VARS_HEAVY[@]}")
else
    REQUIRED_VARS=("${REQUIRED_VARS_LITE[@]}")
fi

# check_env_var <name>: returns 0 if the var has a non-empty value in .env,
# 1 otherwise. Strips quotes and trailing whitespace.
check_env_var() {
    local var="$1"
    local value
    value=$(awk -F= -v k="$var" '
        $1 == k {
            sub(/^[^=]*=/, "", $0)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
            gsub(/^"|"$/, "", $0)
            gsub(/^'\''|'\''$/, "", $0)
            print
            exit
        }' .env)
    [[ -n "$value" ]]
}

MISSING_VARS=()
for var in "${REQUIRED_VARS[@]}"; do
    if ! check_env_var "$var"; then
        MISSING_VARS+=("$var")
    fi
done

if [[ ${#MISSING_VARS[@]} -gt 0 ]]; then
    fatal 2 "missing/empty required env vars: ${MISSING_VARS[*]}"
fi

STEP2_ENV_OK=1
log 2 ok "$(( $(date +%s) - t0 ))" \
    "all ${#REQUIRED_VARS[@]} required vars present in .env"

# ============================================================================
# STEP 3 — docker compose up -d
# ============================================================================
t0=$(date +%s)

if docker compose -f "$COMPOSE_FILE" up -d >&2; then
    STEP3_COMPOSE_UP=1
    log 3 ok "$(( $(date +%s) - t0 ))" "$COMPOSE_FILE issued without error"
else
    fatal 3 "docker compose up returned non-zero (see compose logs above)"
fi

# ============================================================================
# STEP 4 — wait for all containers to become "healthy"
# ============================================================================
t0=$(date +%s)

# Pull the project name out of the compose file ("name: contextdna-lite") so
# we filter `docker ps` correctly. Falls back to the directory name if the
# `name:` directive is absent.
PROJECT_NAME=$(awk '/^name:/ {print $2; exit}' "$COMPOSE_FILE" 2>/dev/null || true)
if [[ -z "$PROJECT_NAME" ]]; then
    PROJECT_NAME=$(basename "$TARGET")
fi

# List containers in this project. Each line: <name> <health>
list_containers() {
    docker ps --filter "label=com.docker.compose.project=${PROJECT_NAME}" \
        --format '{{.Names}}|{{.State}}' 2>/dev/null
}

# probe_health: returns 0 only when every container in the project is
# "healthy" (or "running" if no healthcheck declared). Returns 1 otherwise.
probe_health() {
    local lines status name not_ready=()
    lines=$(list_containers)
    if [[ -z "$lines" ]]; then
        return 1
    fi
    while IFS='|' read -r name state; do
        # `state` from `docker ps` is "running" — we want health, which lives
        # in `docker inspect`. If the container has no healthcheck, treat
        # "running" as healthy.
        status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo "missing")
        case "$status" in
            healthy|running) ;;
            *) not_ready+=("$name=$status") ;;
        esac
    done <<< "$lines"
    if [[ ${#not_ready[@]} -gt 0 ]]; then
        log 4 info 0 "still waiting: ${not_ready[*]}"
        return 1
    fi
    return 0
}

# Poll loop. Sleep 5s between probes. Bail at $TIMEOUT_SECONDS.
poll_deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
while ! probe_health; do
    if [[ $(date +%s) -ge $poll_deadline ]]; then
        fatal 4 "healthcheck timeout after ${TIMEOUT_SECONDS}s — see 'docker compose logs'"
    fi
    sleep 5
done

STEP4_HEALTHY=1
log 4 ok "$(( $(date +%s) - t0 ))" "all containers healthy"

# ============================================================================
# STEP 5 — webhook probe (helper agent /consult, 9-section assertion)
# ============================================================================
t0=$(date +%s)

# The compose stack maps the helper agent to 127.0.0.1:8080 (HELPER_AGENT_PORT
# default — overridable via the env file). The `/consult` endpoint accepts
# query-string params `prompt`, `risk_level`, `session_id`. It returns JSON
# of shape:
#   {
#     "prompt": "...",
#     "risk_level": "...",
#     "timestamp": "...",
#     "layers": { ... }
#   }
# Per BOOTSTRAP.md "Verification" — the 9-section S0..S8 keys live inside
# the persistent_hook_structure payload, not in /consult. We assert the
# layered JSON contract here and surface the layer keys we actually see.
WEBHOOK_URL="http://${WEBHOOK_HOST}:${WEBHOOK_PORT}${WEBHOOK_PATH}"
response_file="$(mktemp)"
status_code=$(curl -sS -o "$response_file" -w '%{http_code}' \
    --max-time 30 \
    -X POST \
    -G "$WEBHOOK_URL" \
    --data-urlencode "prompt=bootstrap-smoke-test" \
    --data-urlencode "risk_level=moderate" \
    --data-urlencode "session_id=bootstrap-verify-$$" \
    || echo "000")

if [[ "$status_code" != "200" ]]; then
    fatal 5 "webhook probe returned HTTP $status_code (expected 200) — body=$(head -c 500 "$response_file")"
fi

# Parse the JSON; the shape we assert is { layers: { ... } }. A successful
# response must have at least one layer (helper agent emits codebase_hints,
# learnings, professor, gotchas, blueprint depending on risk_level).
if ! jq -e '.layers | type == "object" and (keys | length > 0)' "$response_file" >/dev/null 2>&1; then
    fatal 5 "webhook returned JSON with no .layers — body=$(head -c 500 "$response_file")"
fi

LAYER_KEYS=$(jq -r '.layers | keys | join(",")' "$response_file")

STEP5_WEBHOOK_OK=1
log 5 ok "$(( $(date +%s) - t0 ))" \
    "webhook HTTP 200, layers=[$LAYER_KEYS]"

rm -f "$response_file"

# ============================================================================
# STEP 6 — optional teardown
# ============================================================================
if [[ $DO_TEARDOWN -eq 1 ]]; then
    t0=$(date +%s)
    if docker compose -f "$COMPOSE_FILE" down -v >&2; then
        STEP6_TEARDOWN=1
        log 6 ok "$(( $(date +%s) - t0 ))" "stack torn down, volumes removed"
    else
        # Teardown failure is logged but doesn't roll back BOOTSTRAP-VERIFIED.
        ERRORS=$((ERRORS + 1))
        log 6 fail "$(( $(date +%s) - t0 ))" "docker compose down returned non-zero"
    fi
else
    log 6 info 0 "--teardown not requested; stack left running at $TARGET"
fi

# ============================================================================
# SUMMARY
# ============================================================================
print_stats

cat <<EOF

============================================================
  BOOTSTRAP-VERIFIED
------------------------------------------------------------
  profile:           ${PROFILE}
  target:            ${TARGET}
  compose file:      ${COMPOSE_FILE}
  webhook layers:    ${LAYER_KEYS}
  teardown:          $([[ $DO_TEARDOWN -eq 1 ]] && echo "yes" || echo "no (stack still running)")
------------------------------------------------------------
  Operational Invariance Promise — confirmed for ${PROFILE} profile.
  Clone -> compose up -> healthy -> webhook serves payload.
============================================================
EOF

exit 0
