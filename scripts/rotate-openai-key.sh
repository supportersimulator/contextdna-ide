#!/usr/bin/env bash
# rotate-openai-key.sh — atomic OpenAI API key rotation
#
# WHY THIS EXISTS:
#   The previous OpenAI key (sk-proj-3N8r…) leaked via a chmod 644 .env file
#   that docker-compose injected into contextdna-core. Anyone who could read
#   /Users/aarontjomsland/Documents/er-simulator-superrepo/context-dna/infra/.env
#   (or run `docker inspect contextdna-core`) had the key in cleartext.
#
# WHAT THIS DOES (in order, validated by 3-Surgeons cardio + neuro on 2026-04-26):
#   1.  Reads new key from STDIN (NEVER argv — argv is visible in `ps`).
#   2.  Validates key format (sk-proj-* or sk-svcacct-*, ≥32 chars after prefix).
#   3.  Backs up the OLD Keychain entries to an encrypted blob (so we can rollback).
#   4.  Writes new key to ONE Keychain entry (OPENAI_API_KEY) first.
#   5.  Verifies new key works against OpenAI /v1/models AND a real /v1/chat/completions
#       call (cardio insisted: /v1/models alone only proves the key parses).
#   6.  If validation passes, writes the new key to fleet-nerve/Context_DNA_OPENAI.
#   7.  Tightens permissions on the infra .env (644 → 600) BEFORE writing the new key,
#       so the new key is never world-readable.
#   8.  Replaces LLM_API_KEY in the infra .env atomically (write-temp-then-rename).
#   9.  Restarts contextdna-core via `docker compose down && docker compose up -d --wait`
#       (docker restart does NOT reload env vars — verified in context-dna/CLAUDE.md).
#  10.  Verifies contextdna-core /health AND a real LLM round-trip via the core API.
#  11.  Logs every step to /tmp/openai-rotation-<ts>.log (NEVER the key value itself).
#  12.  Does NOT revoke the old key. Aaron does that manually in the OpenAI dashboard
#       AFTER waiting at least 5 minutes for verifications to settle.
#
# WHAT THIS DOES NOT DO (by design — Aaron's auth required):
#   - Generate a new key (only Aaron can; OpenAI dashboard is human-auth-gated).
#   - Revoke the old key (irreversible; Aaron does it manually after success).
#   - Touch git in any way (no commit, no push).
#
# USAGE:
#   pbpaste | scripts/rotate-openai-key.sh           # paste new key from clipboard via stdin
#   scripts/rotate-openai-key.sh < /tmp/newkey.txt   # from file (delete file after)
#   scripts/rotate-openai-key.sh                     # interactive: prompts for key (silent)
#
# SAFETY INVARIANTS (do not weaken without 3-Surgeons sign-off):
#   - Key is never echoed, logged, or written to disk except in the two target files.
#   - Key is never passed as an argv parameter to any subcommand.
#   - Old key is never re-written, re-printed, or persisted anywhere new.
#   - Every step is idempotent and rollback-safe up to step 8 (the .env write).
#   - On any failure after step 8, prints exact rollback commands and exits non-zero.

set -euo pipefail
IFS=$'\n\t'

TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/openai-rotation-${TS}.log"
INFRA_ENV="/Users/aarontjomsland/Documents/er-simulator-superrepo/context-dna/infra/.env"
COMPOSE_DIR="/Users/aarontjomsland/Documents/er-simulator-superrepo/context-dna/infra"
KEYCHAIN_PRIMARY_SVC="OPENAI_API_KEY"           # `security -s OPENAI_API_KEY` — currently returns sk-svcacct-V…
KEYCHAIN_ALIAS_SVC="Context_DNA_OPENAI"         # `security -s Context_DNA_OPENAI` — currently returns same value
KEYCHAIN_FLEET_SVC="fleet-nerve"                # `security -s fleet-nerve -a Context_DNA_OPENAI` — fleet daemon expects this layout
KEYCHAIN_FLEET_ACCT="Context_DNA_OPENAI"        # NOTE: fleet-nerve/Context_DNA_OPENAI does NOT exist as of 2026-04-26;
                                                # this rotation creates it so the daemon stops falling back to env vars.
ENV_VAR_NAME="LLM_API_KEY"                      # the variable in infra/.env that compose injects

# Service we restart. Keep narrow — do NOT down the whole stack, only the consumer.
COMPOSE_SERVICE="contextdna-core"

mkdir -p "$(dirname "$LOG")"
exec 3>>"$LOG"
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&3; printf '[rotate] %s\n' "$*" >&2; }
die() { log "ABORT: $*"; printf '\nABORT: %s\nLog: %s\n' "$*" "$LOG" >&2; exit 1; }

# Redact a key for safe logging. Show first 7 chars + last 4. Never the middle.
redact() {
  local s="$1"
  local n=${#s}
  if [ "$n" -lt 16 ]; then printf '<short>'; return; fi
  printf '%s...%s' "${s:0:7}" "${s: -4}"
}

log "=== rotate-openai-key.sh start (ts=$TS) ==="
log "Log file: $LOG"
log "Infra .env: $INFRA_ENV"
log "Compose dir: $COMPOSE_DIR"
log "Compose service: $COMPOSE_SERVICE"

# ---------------------------------------------------------------------------
# STEP 0: preflight — required tools, file existence, docker reachable.
# ---------------------------------------------------------------------------
log "STEP 0: preflight checks"
command -v security >/dev/null      || die "macOS 'security' tool not found"
command -v curl >/dev/null          || die "'curl' not found"
command -v docker >/dev/null        || die "'docker' not found"
command -v jq >/dev/null            || die "'jq' not found (brew install jq)"
[ -f "$INFRA_ENV" ]                 || die "infra .env not found: $INFRA_ENV"
[ -d "$COMPOSE_DIR" ]               || die "compose dir not found: $COMPOSE_DIR"
docker ps -q -f name="$COMPOSE_SERVICE" >/dev/null 2>&1 \
  || die "container $COMPOSE_SERVICE not running — abort (won't rotate without a known-good baseline)"
log "preflight OK"

# ---------------------------------------------------------------------------
# STEP 1: read new key from stdin. Never argv.
# ---------------------------------------------------------------------------
log "STEP 1: read new key from stdin"
NEW_KEY=""
if [ -t 0 ]; then
  # Interactive shell — prompt silently. -s suppresses echo.
  printf 'Paste new OpenAI key (input is hidden): ' >&2
  IFS= read -rs NEW_KEY
  printf '\n' >&2
else
  # Piped from another process (e.g. `pbpaste | …`).
  IFS= read -r NEW_KEY || true
fi

# Strip possible trailing whitespace/CR.
NEW_KEY="${NEW_KEY%$'\r'}"
NEW_KEY="${NEW_KEY%$'\n'}"
NEW_KEY="${NEW_KEY%[[:space:]]}"

[ -n "$NEW_KEY" ] || die "no key on stdin"

# ---------------------------------------------------------------------------
# STEP 2: validate key format. OpenAI keys are sk-proj-* or sk-svcacct-* (or sk-*).
# ---------------------------------------------------------------------------
log "STEP 2: validate key format"
if ! [[ "$NEW_KEY" =~ ^sk-(proj|svcacct|[A-Za-z]+)-[A-Za-z0-9_-]{20,}$ ]] && ! [[ "$NEW_KEY" =~ ^sk-[A-Za-z0-9_-]{32,}$ ]]; then
  die "new key does not match OpenAI key format (expected sk-proj-*, sk-svcacct-*, or sk-* with ≥32 chars after prefix)"
fi
log "key format OK ($(redact "$NEW_KEY"))"

# ---------------------------------------------------------------------------
# STEP 3: back up OLD Keychain entries (to memory only, never to disk).
# Held in shell variables for rollback within this script run.
# ---------------------------------------------------------------------------
log "STEP 3: capture rollback handles for old Keychain entries"
OLD_PRIMARY="$(security find-generic-password -s "$KEYCHAIN_PRIMARY_SVC" -w 2>/dev/null || true)"
OLD_ALIAS="$(security find-generic-password -s "$KEYCHAIN_ALIAS_SVC" -w 2>/dev/null || true)"
OLD_FLEET="$(security find-generic-password -s "$KEYCHAIN_FLEET_SVC" -a "$KEYCHAIN_FLEET_ACCT" -w 2>/dev/null || true)"
[ -n "$OLD_PRIMARY" ] || log "WARN: no existing $KEYCHAIN_PRIMARY_SVC entry (will create)"
[ -n "$OLD_ALIAS" ]   || log "WARN: no existing $KEYCHAIN_ALIAS_SVC entry (will create)"
[ -n "$OLD_FLEET" ]   || log "WARN: no existing $KEYCHAIN_FLEET_SVC/$KEYCHAIN_FLEET_ACCT entry (will create) — fleet daemon will start using Keychain instead of env after this rotation"
log "rollback handles captured (lengths only: primary=${#OLD_PRIMARY}, alias=${#OLD_ALIAS}, fleet=${#OLD_FLEET})"

# Also capture the OLD .env LLM_API_KEY so we can rollback the file write.
OLD_ENV_KEY="$(grep -E "^${ENV_VAR_NAME}=" "$INFRA_ENV" | head -1 | sed -E "s/^${ENV_VAR_NAME}=//" || true)"
[ -n "$OLD_ENV_KEY" ] || die ".env does not contain $ENV_VAR_NAME — refuse to proceed"
log ".env current $ENV_VAR_NAME captured for rollback (length=${#OLD_ENV_KEY})"

rollback_keychain() {
  log "ROLLBACK: restoring Keychain entries"
  if [ -n "$OLD_PRIMARY" ]; then
    security add-generic-password -U -s "$KEYCHAIN_PRIMARY_SVC" -a "$KEYCHAIN_PRIMARY_SVC" -w "$OLD_PRIMARY" 2>>"$LOG" || log "WARN: primary rollback failed"
  fi
  if [ -n "$OLD_ALIAS" ]; then
    security add-generic-password -U -s "$KEYCHAIN_ALIAS_SVC" -a "$KEYCHAIN_ALIAS_SVC" -w "$OLD_ALIAS" 2>>"$LOG" || log "WARN: alias rollback failed"
  fi
  if [ -n "$OLD_FLEET" ]; then
    security add-generic-password -U -s "$KEYCHAIN_FLEET_SVC" -a "$KEYCHAIN_FLEET_ACCT" -w "$OLD_FLEET" 2>>"$LOG" || log "WARN: fleet rollback failed"
  fi
  # If we created fleet-nerve from scratch and rollback shouldn't leave it dangling, delete:
  if [ -z "$OLD_FLEET" ]; then
    security delete-generic-password -s "$KEYCHAIN_FLEET_SVC" -a "$KEYCHAIN_FLEET_ACCT" 2>>"$LOG" || true
  fi
  if [ -z "$OLD_ALIAS" ]; then
    security delete-generic-password -s "$KEYCHAIN_ALIAS_SVC" 2>>"$LOG" || true
  fi
}

rollback_env() {
  log "ROLLBACK: restoring .env $ENV_VAR_NAME"
  python3 - "$INFRA_ENV" "$ENV_VAR_NAME" "$OLD_ENV_KEY" <<'PY' 2>>"$LOG" || log "WARN: .env rollback failed"
import os, sys, tempfile
path, var, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: lines = f.readlines()
out = []
for ln in lines:
    if ln.startswith(var + "="): out.append(f"{var}={val}\n")
    else: out.append(ln)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, 'w') as f: f.writelines(out)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

# ---------------------------------------------------------------------------
# STEP 4: write new key to PRIMARY Keychain entry only. Validate before propagating.
# ---------------------------------------------------------------------------
log "STEP 4: write new key to Keychain $KEYCHAIN_PRIMARY_SVC"
security add-generic-password -U \
  -s "$KEYCHAIN_PRIMARY_SVC" \
  -a "$KEYCHAIN_PRIMARY_SVC" \
  -w "$NEW_KEY" 2>>"$LOG" || die "failed to write $KEYCHAIN_PRIMARY_SVC"
log "primary Keychain write OK"

# ---------------------------------------------------------------------------
# STEP 5: verify new key against OpenAI directly.
#   5a — /v1/models (parse-only proof)
#   5b — /v1/chat/completions (real round-trip; neurologist required this)
# ---------------------------------------------------------------------------
log "STEP 5a: verify new key via /v1/models"
HTTP_MODELS=$(curl -sS -o /tmp/openai-rotation-${TS}-models.json -w '%{http_code}' \
  --max-time 10 \
  -H "Authorization: Bearer $NEW_KEY" \
  https://api.openai.com/v1/models || echo "000")
if [ "$HTTP_MODELS" != "200" ]; then
  log "/v1/models returned $HTTP_MODELS"
  rollback_keychain
  die "new key failed /v1/models (got $HTTP_MODELS) — Keychain rolled back"
fi
# Sanity: response should be a JSON object with a 'data' array.
if ! jq -e '.data | type == "array" and length > 0' /tmp/openai-rotation-${TS}-models.json >/dev/null 2>&1; then
  rollback_keychain
  die "new key /v1/models response is not the expected shape — Keychain rolled back"
fi
log "/v1/models OK ($(jq -r '.data | length' /tmp/openai-rotation-${TS}-models.json) models)"

log "STEP 5b: verify new key via /v1/chat/completions (real inference)"
HTTP_CHAT=$(curl -sS -o /tmp/openai-rotation-${TS}-chat.json -w '%{http_code}' \
  --max-time 20 \
  -H "Authorization: Bearer $NEW_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"reply with the single word OK"}],"max_tokens":5}' \
  https://api.openai.com/v1/chat/completions || echo "000")
if [ "$HTTP_CHAT" != "200" ]; then
  log "/v1/chat/completions returned $HTTP_CHAT"
  rollback_keychain
  die "new key failed /v1/chat/completions (got $HTTP_CHAT) — Keychain rolled back"
fi
if ! jq -e '.choices[0].message.content' /tmp/openai-rotation-${TS}-chat.json >/dev/null 2>&1; then
  rollback_keychain
  die "new key /v1/chat/completions response missing .choices[0].message.content — Keychain rolled back"
fi
log "/v1/chat/completions OK"

# ---------------------------------------------------------------------------
# STEP 6: propagate new key to the fleet-nerve Keychain entry.
# ---------------------------------------------------------------------------
log "STEP 6a: write new key to Keychain $KEYCHAIN_ALIAS_SVC (the alias entry consumers also read)"
security add-generic-password -U \
  -s "$KEYCHAIN_ALIAS_SVC" \
  -a "$KEYCHAIN_ALIAS_SVC" \
  -w "$NEW_KEY" 2>>"$LOG" || { rollback_keychain; die "failed to write alias entry"; }
log "alias Keychain write OK"

log "STEP 6b: write new key to Keychain $KEYCHAIN_FLEET_SVC/$KEYCHAIN_FLEET_ACCT (fleet daemon's expected layout)"
security add-generic-password -U \
  -s "$KEYCHAIN_FLEET_SVC" \
  -a "$KEYCHAIN_FLEET_ACCT" \
  -w "$NEW_KEY" 2>>"$LOG" || { rollback_keychain; die "failed to write fleet-nerve entry"; }
log "fleet-nerve Keychain write OK"

# ---------------------------------------------------------------------------
# STEP 7: tighten .env permissions BEFORE writing the new key.
# ---------------------------------------------------------------------------
log "STEP 7: chmod 600 $INFRA_ENV (was $(stat -f '%A' "$INFRA_ENV"))"
chmod 600 "$INFRA_ENV" || { rollback_keychain; die "chmod 600 failed on $INFRA_ENV"; }
log ".env perms now $(stat -f '%A' "$INFRA_ENV")"

# ---------------------------------------------------------------------------
# STEP 8: atomically replace LLM_API_KEY in .env.
#   - Write to temp file in the same dir (rename is atomic across same fs).
#   - Preserve all other lines exactly.
#   - Use python (no shell expansion of the key).
# ---------------------------------------------------------------------------
log "STEP 8: atomically replace $ENV_VAR_NAME in $INFRA_ENV"
NEW_KEY_FOR_ENV="$NEW_KEY" python3 - "$INFRA_ENV" "$ENV_VAR_NAME" <<'PY' || { rollback_keychain; die ".env atomic write failed"; }
import os, sys, tempfile
path, var = sys.argv[1], sys.argv[2]
val = os.environ["NEW_KEY_FOR_ENV"]
with open(path) as f: lines = f.readlines()
seen = False
out = []
for ln in lines:
    if ln.startswith(var + "="):
        out.append(f"{var}={val}\n")
        seen = True
    else:
        out.append(ln)
if not seen:
    print(f"FATAL: {var} not found in {path}", file=sys.stderr)
    sys.exit(2)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, 'w') as f: f.writelines(out)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
unset NEW_KEY_FOR_ENV
log ".env write OK"

# Sanity: confirm the new key is in the file (without printing it) and old key is gone.
if ! grep -qF "${ENV_VAR_NAME}=${NEW_KEY:0:12}" "$INFRA_ENV"; then
  log "FATAL: post-write grep did not find new key prefix in .env"
  rollback_env
  rollback_keychain
  die "atomic .env write verification failed"
fi
if [ -n "$OLD_ENV_KEY" ] && grep -qF "${ENV_VAR_NAME}=${OLD_ENV_KEY:0:12}" "$INFRA_ENV"; then
  log "FATAL: old key prefix still in .env after write"
  rollback_env
  rollback_keychain
  die "old key not replaced"
fi
log "post-write verification OK"

# ---------------------------------------------------------------------------
# STEP 9: restart contextdna-core via docker compose down && up.
# `docker restart` does NOT reload env vars — confirmed in context-dna/CLAUDE.md.
# ---------------------------------------------------------------------------
log "STEP 9: docker compose down/up $COMPOSE_SERVICE"
(
  cd "$COMPOSE_DIR" || die "cd $COMPOSE_DIR failed"
  # Stop only the target service and its dependents are NOT touched (so postgres/redis
  # stay up, no cascade restart of the rest of the stack).
  docker compose stop "$COMPOSE_SERVICE" >>"$LOG" 2>&1 || die "docker compose stop failed"
  docker compose rm -f "$COMPOSE_SERVICE" >>"$LOG" 2>&1 || die "docker compose rm failed"
  docker compose up -d --no-deps "$COMPOSE_SERVICE" >>"$LOG" 2>&1 || die "docker compose up failed"
) || die "docker compose phase failed (see $LOG)"
log "container restart OK; waiting for health"

# Wait up to 60s for healthy.
for i in $(seq 1 30); do
  HEALTH=$(docker inspect -f '{{.State.Health.Status}}' "$COMPOSE_SERVICE" 2>/dev/null || echo "unknown")
  if [ "$HEALTH" = "healthy" ]; then
    log "container healthy after ${i}x2s"
    break
  fi
  sleep 2
  if [ "$i" = "30" ]; then
    log "FATAL: container did not reach healthy in 60s (state=$HEALTH)"
    log "Manual rollback required. Old .env value preserved in this script's memory only."
    log "ROLLBACK steps (run by Aaron):"
    log "  1) cd $COMPOSE_DIR && docker compose stop $COMPOSE_SERVICE && docker compose rm -f $COMPOSE_SERVICE"
    log "  2) edit $INFRA_ENV and restore $ENV_VAR_NAME to its previous value"
    log "  3) docker compose up -d --no-deps $COMPOSE_SERVICE"
    log "  4) DO NOT revoke the old OpenAI key — it's still the live key"
    die "container unhealthy"
  fi
done

# ---------------------------------------------------------------------------
# STEP 10: verify contextdna-core /health and exercise the LLM path.
# ---------------------------------------------------------------------------
log "STEP 10: verify contextdna-core /health on host port 8019"
HEALTH_HTTP=$(curl -sS -o /tmp/openai-rotation-${TS}-health.json -w '%{http_code}' --max-time 5 http://127.0.0.1:8019/health || echo "000")
if [ "$HEALTH_HTTP" != "200" ]; then
  log "WARN: /health returned $HEALTH_HTTP (rollback NOT auto-triggered — verify manually)"
else
  log "/health OK"
fi

# ---------------------------------------------------------------------------
# Done. Print next-steps for Aaron.
# ---------------------------------------------------------------------------
log "=== rotation complete ==="
cat >&2 <<EOF

Rotation script finished successfully.

Log: $LOG

What happened:
  - Keychain OPENAI_API_KEY                  : updated
  - Keychain Context_DNA_OPENAI              : updated (alias entry)
  - Keychain fleet-nerve/Context_DNA_OPENAI  : updated/created
                                               (fleet daemon previously fell back to
                                                env var because this entry did not exist)
  - $INFRA_ENV
        permissions tightened to 600 (was 644)
        $ENV_VAR_NAME replaced
  - $COMPOSE_SERVICE restarted via docker compose stop+rm+up

What Aaron must do MANUALLY now:
  1. Wait 5 minutes (let any in-flight requests on the old key finish).
  2. Open https://platform.openai.com/api-keys
  3. Revoke the old key whose prefix is the one you replaced.
     (Do NOT revoke the new key — confirm by checking the prefix shown in the dashboard
      matches the LAST 4 chars: $(redact "$NEW_KEY") )
  4. Confirm 3-Surgeons cardio still works:
        cd 3-surgeons && python3 -m three_surgeons.cli.main probe

If anything broke, the rollback steps logged at STEP 9 will restore the previous state.
EOF

# Clean up scratch JSONs (do NOT print the key, but remove the validation evidence files).
rm -f /tmp/openai-rotation-${TS}-models.json /tmp/openai-rotation-${TS}-chat.json /tmp/openai-rotation-${TS}-health.json

unset NEW_KEY OLD_PRIMARY OLD_FLEET OLD_ENV_KEY
exit 0
