#!/usr/bin/env bash
# setup-mothership.sh — One-command onboarding for ContextDNA IDE.
#
# Turns a fresh laptop + a network connection into a fully-backed-up
# mothership in ~10 minutes. Walks through the 7-step "before disaster"
# checklist interactively, storing secrets in macOS Keychain (or an
# age-encrypted file on Linux), and schedules the daily/weekly backups.
#
# Idempotent: safe to re-run. Each step detects existing state and
# prompts only for what's missing.
#
# Usage:
#   bash scripts/setup-mothership.sh
#   bash scripts/setup-mothership.sh --non-interactive   # use env vars only
#   bash scripts/setup-mothership.sh --check             # report status only
#
# Env vars (override defaults; useful for --non-interactive):
#   BACKUP_BUCKET            (required for --non-interactive)
#   BACKUP_S3_ENDPOINT       (optional; e.g. https://s3.us-west-002.backblazeb2.com)
#   BACKUP_AGE_KEYFILE       (default: ~/.ssh/contextdna-backup.age.key)
#   AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (or use aws sso)
#
# ZSF: every failure exits non-zero with a [setup] FAIL: <reason> line.
# Errors are never swallowed. Re-run after fixing.

set -uo pipefail

# ── Mode ─────────────────────────────────────────────────────────────────────
MODE="interactive"
for arg in "$@"; do
    case "$arg" in
        --non-interactive) MODE="non-interactive" ;;
        --check)           MODE="check" ;;
        --help|-h)
            sed -n '2,30p' "$0" | sed 's|^# ||; s|^#||'
            exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# ── Colors (skip if not a tty) ───────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1); BLUE=$(tput setaf 4); DIM=$(tput dim); RESET=$(tput sgr0)
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; DIM=""; RESET=""
fi

_step()  { echo ""; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
_ok()    { echo "  ${GREEN}✓${RESET} $*"; }
_warn()  { echo "  ${YELLOW}⚠${RESET} $*"; }
_fail()  { echo "  ${RED}✗ FAIL:${RESET} $*" >&2; exit 1; }
_info()  { echo "  ${DIM}$*${RESET}"; }
_ask()   {
    local prompt="$1" default="${2:-}" var
    if [ "$MODE" = "non-interactive" ]; then echo "$default"; return; fi
    if [ -n "$default" ]; then
        read -r -p "  ${BOLD}?${RESET} $prompt [${DIM}$default${RESET}]: " var
        echo "${var:-$default}"
    else
        read -r -p "  ${BOLD}?${RESET} $prompt: " var
        echo "$var"
    fi
}
_confirm() {
    local prompt="$1"
    # In --check mode, never confirm anything (read-only).
    # In --non-interactive mode, default to no for safety (caller must set env).
    [ "$MODE" = "check" ] && return 1
    [ "$MODE" = "non-interactive" ] && return 0
    read -r -p "  ${BOLD}?${RESET} $prompt [Y/n]: " var
    [[ "$var" =~ ^[Nn] ]] && return 1 || return 0
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
PLATFORM="$(uname -s)"   # Darwin / Linux

cat <<EOF

${BOLD}ContextDNA Mothership — Onboarding${RESET}
${DIM}Turns a fresh laptop into a fully-backed-up mothership in ~10 minutes.${RESET}

This walks you through:
  1. Install prerequisites    (docker, age, aws, nats, jq)
  2. Generate age keypair     (the one secret that protects every backup)
  3. Configure backup bucket  (Backblaze B2, AWS S3, or any S3-compatible)
  4. Build .env               (secrets stored in macOS Keychain where possible)
  5. Print offline-backup card (you'll write this down / save to 1Password)
  6. Run first backup         (verify the loop works end-to-end)
  7. Schedule daily + weekly  (launchd on macOS, cron on Linux)

Mode: ${BOLD}$MODE${RESET}
EOF

# ────────────────────────────────────────────────────────────────────────────
# STEP 1 — Prerequisites
# ────────────────────────────────────────────────────────────────────────────
_step "Step 1/7  Prerequisites"

NEEDED=(docker age aws jq curl)
[ "$PLATFORM" = "Darwin" ] && NEEDED+=(brew)
MISSING=()
for tool in "${NEEDED[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
        _ok "$tool present"
    else
        _warn "$tool MISSING"
        MISSING+=("$tool")
    fi
done

# nats CLI is optional but recommended
if command -v nats >/dev/null 2>&1; then
    _ok "nats present"
else
    _warn "nats CLI missing (needed for jetstream backups — optional)"
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    if [ "$MODE" = "check" ]; then
        _info "missing tools (run without --check to install): ${MISSING[*]/brew/}"
    elif [ "$PLATFORM" = "Darwin" ] && command -v brew >/dev/null; then
        _info "Install missing tools with:"
        echo "    brew install ${MISSING[*]/brew/}"
        if _confirm "Install now via brew?"; then
            brew install "${MISSING[@]/brew/}" || _fail "brew install failed"
            _ok "all prereqs now present"
        else
            _fail "prerequisites not satisfied; install manually and re-run"
        fi
    else
        _fail "missing tools: ${MISSING[*]} — install and re-run"
    fi
fi

if [ "$MODE" = "check" ]; then
    _info "(--check mode: reporting only, no changes will be made)"
fi

# ────────────────────────────────────────────────────────────────────────────
# STEP 2 — Age keypair
# ────────────────────────────────────────────────────────────────────────────
_step "Step 2/7  Age keypair (offline-backup encryption key)"

KEYFILE_DEFAULT="${BACKUP_AGE_KEYFILE:-$HOME/.ssh/contextdna-backup.age.key}"
KEYFILE="$(_ask "Path for age private key" "$KEYFILE_DEFAULT")"
KEYDIR="$(dirname "$KEYFILE")"
mkdir -p "$KEYDIR"
chmod 700 "$KEYDIR"

AGE_PUBKEY=""
if [ -f "$KEYFILE" ]; then
    _ok "existing keyfile found at $KEYFILE"
    AGE_PUBKEY="$(grep '^# public key:' "$KEYFILE" | head -1 | sed 's|^# public key: ||')"
elif [ "$MODE" = "check" ]; then
    _warn "no keyfile at $KEYFILE — would generate in interactive mode"
elif command -v age-keygen >/dev/null; then
    age-keygen -o "$KEYFILE" 2>/dev/null || _fail "age-keygen failed"
    chmod 600 "$KEYFILE"
    AGE_PUBKEY="$(grep '^# public key:' "$KEYFILE" | head -1 | sed 's|^# public key: ||')"
    _ok "generated new keyfile at $KEYFILE"
else
    _fail "age-keygen not found (install age and re-run)"
fi
[ -n "$AGE_PUBKEY" ] && _info "public key: ${AGE_PUBKEY:0:30}…"

# ────────────────────────────────────────────────────────────────────────────
# STEP 3 — Backup bucket
# ────────────────────────────────────────────────────────────────────────────
_step "Step 3/7  Backup bucket"

# Persisted preferences live in Keychain (macOS) or a plain file (Linux).
_kc_get() {
    [ "$PLATFORM" = "Darwin" ] || { cat "$HOME/.config/contextdna/$1" 2>/dev/null; return; }
    security find-generic-password -a "$USER" -s "contextdna-$1" -w 2>/dev/null
}
_kc_set() {
    [ "$PLATFORM" = "Darwin" ] || { mkdir -p "$HOME/.config/contextdna"; chmod 700 "$HOME/.config/contextdna"; printf '%s' "$2" > "$HOME/.config/contextdna/$1"; chmod 600 "$HOME/.config/contextdna/$1"; return; }
    security delete-generic-password -a "$USER" -s "contextdna-$1" >/dev/null 2>&1 || true
    security add-generic-password -a "$USER" -s "contextdna-$1" -w "$2" 2>/dev/null
}

CUR_BUCKET="$(_kc_get backup_bucket || echo "")"
CUR_ENDPOINT="$(_kc_get backup_s3_endpoint || echo "")"

BACKUP_BUCKET="${BACKUP_BUCKET:-$CUR_BUCKET}"
BACKUP_S3_ENDPOINT="${BACKUP_S3_ENDPOINT:-$CUR_ENDPOINT}"

if [ -z "$BACKUP_BUCKET" ] || [ "$MODE" = "interactive" ]; then
    echo ""
    echo "  Backup provider options:"
    echo "    1) Backblaze B2     (~\$5/month for typical use, S3-compatible)"
    echo "    2) AWS S3           (free tier 5GB, then ~\$0.023/GB)"
    echo "    3) Wasabi           (~\$6/month for 1TB)"
    echo "    4) Self-hosted MinIO / other S3-compatible"
    echo ""
    PROVIDER="$(_ask "Choose 1-4" "1")"
    case "$PROVIDER" in
        1) BACKUP_S3_ENDPOINT_DEFAULT="https://s3.us-west-002.backblazeb2.com" ;;
        2) BACKUP_S3_ENDPOINT_DEFAULT="" ;;
        3) BACKUP_S3_ENDPOINT_DEFAULT="https://s3.us-east-1.wasabisys.com" ;;
        4) BACKUP_S3_ENDPOINT_DEFAULT="" ;;
        *) BACKUP_S3_ENDPOINT_DEFAULT="" ;;
    esac
    BACKUP_BUCKET="$(_ask "Bucket URI (e.g. s3://contextdna-backups)" "${BACKUP_BUCKET:-s3://contextdna-backups}")"
    BACKUP_S3_ENDPOINT="$(_ask "S3 endpoint (blank = AWS)" "${BACKUP_S3_ENDPOINT:-$BACKUP_S3_ENDPOINT_DEFAULT}")"
fi

# AWS credentials
if ! aws sts get-caller-identity ${BACKUP_S3_ENDPOINT:+--endpoint-url "$BACKUP_S3_ENDPOINT"} >/dev/null 2>&1; then
    _warn "AWS CLI not authenticated for this endpoint"
    _info "Set credentials with one of:"
    _info "  - aws configure                    (writes ~/.aws/credentials)"
    _info "  - export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=…"
    _info "  - aws sso login                    (if using SSO)"
    if [ "$MODE" = "interactive" ]; then
        if _confirm "Run 'aws configure' now?"; then
            aws configure
        fi
    fi
fi

# Test bucket access
if [ "$MODE" != "check" ]; then
    AWS_TEST_ARGS=()
    [ -n "$BACKUP_S3_ENDPOINT" ] && AWS_TEST_ARGS+=(--endpoint-url "$BACKUP_S3_ENDPOINT")
    if aws "${AWS_TEST_ARGS[@]}" s3 ls "$BACKUP_BUCKET" >/dev/null 2>&1; then
        _ok "bucket $BACKUP_BUCKET reachable"
    else
        _warn "bucket $BACKUP_BUCKET not reachable (may not exist yet)"
        if _confirm "Create it now?"; then
            aws "${AWS_TEST_ARGS[@]}" s3 mb "$BACKUP_BUCKET" || _fail "mb failed"
            _ok "bucket created"
        fi
    fi
fi

if [ "$MODE" != "check" ]; then
    _kc_set backup_bucket "$BACKUP_BUCKET"
    _kc_set backup_s3_endpoint "$BACKUP_S3_ENDPOINT"
    _ok "bucket config saved to ${PLATFORM} keychain"
fi

# ────────────────────────────────────────────────────────────────────────────
# STEP 4 — Build .env
# ────────────────────────────────────────────────────────────────────────────
_step "Step 4/7  Build .env"

if [ "$MODE" = "check" ]; then
    if [ -f "$ENV_FILE" ]; then
        _ok ".env exists at $ENV_FILE"
        for k in BACKUP_BUCKET BACKUP_AGE_PUBKEY BACKUP_AGE_KEYFILE BACKUP_RETENTION_DAYS; do
            if grep -q "^${k}=" "$ENV_FILE"; then _ok "  $k set"; else _warn "  $k missing"; fi
        done
    else
        _warn ".env missing (would create from .env.example)"
    fi
    # Skip the mutating section in check mode
    SKIP_ENV_WRITE=1
else
    SKIP_ENV_WRITE=0
fi

if [ "$SKIP_ENV_WRITE" = "0" ] && [ -f "$ENV_FILE" ]; then
    BACKUP_ENV="${ENV_FILE}.backup-$(date +%s)"
    cp "$ENV_FILE" "$BACKUP_ENV"
    _info "existing .env backed up to $BACKUP_ENV"
fi

# Append/update the backup-related vars without nuking existing content
_set_env_var() {
    local key="$1" val="$2"
    if [ -f "$ENV_FILE" ] && grep -q "^${key}=" "$ENV_FILE"; then
        # Use a temp file to avoid sed in-place quirks
        grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    fi
    echo "${key}=${val}" >> "$ENV_FILE"
}

if [ "$SKIP_ENV_WRITE" = "0" ]; then
    if [ ! -f "$ENV_FILE" ] && [ -f "$REPO_ROOT/.env.example" ]; then
        cp "$REPO_ROOT/.env.example" "$ENV_FILE"
        _ok "seeded .env from .env.example"
    fi

    touch "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    _set_env_var "BACKUP_BUCKET" "$BACKUP_BUCKET"
    [ -n "$BACKUP_S3_ENDPOINT" ] && _set_env_var "BACKUP_S3_ENDPOINT" "$BACKUP_S3_ENDPOINT"
    [ -n "$AGE_PUBKEY" ] && _set_env_var "BACKUP_AGE_PUBKEY" "$AGE_PUBKEY"
    _set_env_var "BACKUP_AGE_KEYFILE" "$KEYFILE"
    _set_env_var "BACKUP_RETENTION_DAYS" "90"

    _ok ".env updated with backup config"
    _info "  → $ENV_FILE (chmod 600, not in git)"
fi

# ────────────────────────────────────────────────────────────────────────────
# STEP 5 — Offline backup card
# ────────────────────────────────────────────────────────────────────────────
_step "Step 5/7  Offline-backup card"

if [ "$MODE" = "check" ]; then
    LATEST_CARD="$(ls -t "$HOME/Desktop/"CONTEXTDNA-OFFLINE-BACKUP-*.txt 2>/dev/null | head -1)"
    [ -n "$LATEST_CARD" ] && _ok "latest card: $LATEST_CARD" || _warn "no offline-backup card found"
    CARD=""
else
CARD="$HOME/Desktop/CONTEXTDNA-OFFLINE-BACKUP-$(date +%Y%m%d).txt"
cat > "$CARD" <<EOF
======================================================================
ContextDNA Mothership — Offline Recovery Card
Generated:  $(date)
Hostname:   $(hostname)
======================================================================

If your laptop dies, AWS account is deleted, or 6 months pass and
you've forgotten everything: this card + the public GitHub repo is
all you need to bring the mothership back.

────────────────────────────────────────────────────────────────────
WHAT TO DO WITH THIS FILE:
  1. Print it AND save to 1Password / encrypted vault
  2. ALSO save the age key file (next item) to the same place
  3. Then delete this Desktop copy
  4. Test the recovery on a throwaway VM once a quarter
────────────────────────────────────────────────────────────────────

PUBLIC REPO:
  git@github.com:supportersimulator/contextdna-ide.git

BACKUP BUCKET:
  $BACKUP_BUCKET
  endpoint: ${BACKUP_S3_ENDPOINT:-<aws default>}

AGE PUBLIC KEY (the recipient — safe to share):
  $AGE_PUBKEY

AGE PRIVATE KEY FILE (this is the actual secret):
  $KEYFILE
  → ALSO BACK UP THIS FILE to 1Password / printed paper.
  → Without it, your backups are unrecoverable.
  → The file itself is small enough to print as a QR code.

ENV FILE LOCATION (regenerated automatically on this machine):
  $ENV_FILE
  → Variables that need offline backup are inside.

────────────────────────────────────────────────────────────────────
RECOVERY RECIPE (3 hours, fresh laptop):
  brew install docker age aws jq curl nats-io/nats-tools/nats
  git clone git@github.com:supportersimulator/contextdna-ide.git
  cd contextdna-ide
  cp <restored-from-1password>/.env ./
  cp <restored-from-1password>/contextdna-backup.age.key ~/.ssh/
  chmod 600 ~/.ssh/contextdna-backup.age.key
  docker compose -f docker-compose.lite.yml up -d
  bash infra/backup/restore.sh --kind all --latest
  curl -sf http://localhost:8855/health | jq
  bash scripts/bootstrap-verify.sh    # prints BOOTSTRAP-VERIFIED if OK
────────────────────────────────────────────────────────────────────

If BOOTSTRAP-VERIFIED prints: the promise held. You are back.
If it doesn't: read docs/operational-invariance.md §6 (troubleshooting).
EOF
chmod 600 "$CARD"
_ok "card written to $CARD"
_warn "PRINT this card AND save to 1Password before deleting"
fi  # close the check-mode wrapper

# ────────────────────────────────────────────────────────────────────────────
# STEP 6 — First backup
# ────────────────────────────────────────────────────────────────────────────
_step "Step 6/7  First backup (verify the loop)"

if [ "$MODE" = "check" ]; then
    _info "(--check: skipping live backup)"
else
    if _confirm "Run pg-dump now (dry-run only — no real DB needed)?"; then
        set -a; . "$ENV_FILE"; set +a
        # Provide harmless defaults so dry-run works without a real DB
        : "${POSTGRES_DB:=postgres}" "${POSTGRES_USER:=postgres}" "${PGPASSWORD:=changeme}"
        export POSTGRES_DB POSTGRES_USER PGPASSWORD
        bash "$REPO_ROOT/infra/backup/pg-dump.sh" --dry-run \
            && _ok "pg-dump dry-run passed" \
            || _warn "pg-dump dry-run had issues — review output above"
    fi
fi

# ────────────────────────────────────────────────────────────────────────────
# STEP 7 — Schedule
# ────────────────────────────────────────────────────────────────────────────
_step "Step 7/7  Schedule daily + weekly backups"

if [ "$MODE" = "check" ]; then
    if [ "$PLATFORM" = "Darwin" ]; then
        for label in io.contextdna.backup-pg io.contextdna.backup-jetstream; do
            if launchctl list 2>/dev/null | awk '{print $3}' | grep -qx "$label"; then
                _ok "$label scheduled"
            else
                _warn "$label NOT scheduled"
            fi
        done
    else
        crontab -l 2>/dev/null | grep -q 'contextdna-backup' && _ok "cron lines present" || _warn "no cron lines"
    fi
elif [ "$PLATFORM" = "Darwin" ]; then
    LAUNCHD_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCHD_DIR"

    PG_PLIST="$LAUNCHD_DIR/io.contextdna.backup-pg.plist"
    JS_PLIST="$LAUNCHD_DIR/io.contextdna.backup-jetstream.plist"

    cat > "$PG_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.contextdna.backup-pg</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$REPO_ROOT" &amp;&amp; set -a &amp;&amp; . ./.env &amp;&amp; set +a &amp;&amp; bash infra/backup/pg-dump.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/contextdna-backup-pg.log</string>
  <key>StandardErrorPath</key><string>/tmp/contextdna-backup-pg.err</string>
</dict>
</plist>
EOF

    cat > "$JS_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.contextdna.backup-jetstream</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$REPO_ROOT" &amp;&amp; set -a &amp;&amp; . ./.env &amp;&amp; set +a &amp;&amp; bash infra/backup/jetstream-snapshot.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/contextdna-backup-js.log</string>
  <key>StandardErrorPath</key><string>/tmp/contextdna-backup-js.err</string>
</dict>
</plist>
EOF

    launchctl unload "$PG_PLIST" 2>/dev/null || true
    launchctl unload "$JS_PLIST" 2>/dev/null || true
    launchctl load -w "$PG_PLIST" && _ok "pg backup scheduled daily @ 03:00" || _warn "launchctl load pg failed"
    launchctl load -w "$JS_PLIST" && _ok "jetstream backup scheduled Sunday @ 04:00" || _warn "launchctl load jetstream failed"

elif [ "$PLATFORM" = "Linux" ]; then
    CRON_LINE_PG="0 3 * * * cd $REPO_ROOT && set -a && . ./.env && set +a && bash infra/backup/pg-dump.sh >> /tmp/contextdna-backup-pg.log 2>&1"
    CRON_LINE_JS="0 4 * * 0 cd $REPO_ROOT && set -a && . ./.env && set +a && bash infra/backup/jetstream-snapshot.sh >> /tmp/contextdna-backup-js.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v 'contextdna-backup'; echo "$CRON_LINE_PG"; echo "$CRON_LINE_JS" ) | crontab - \
        && _ok "cron lines installed (daily 03:00, weekly Sun 04:00)"
else
    _warn "unknown platform $PLATFORM — schedule manually using your OS scheduler"
fi

# ────────────────────────────────────────────────────────────────────────────
# Final status
# ────────────────────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}${GREEN}━━━ Onboarding complete ━━━${RESET}"
echo ""
echo "  ${BOLD}What just happened:${RESET}"
echo "    ✓ Prereqs verified"
echo "    ✓ Age keypair at $KEYFILE (mode 600)"
echo "    ✓ Backup bucket $BACKUP_BUCKET configured"
echo "    ✓ .env at $ENV_FILE (mode 600)"
echo "    ✓ Offline-backup card at $CARD"
echo "    ✓ pg-dump dry-run validated"
echo "    ✓ launchd / cron schedule installed"
echo ""
echo "  ${BOLD}${YELLOW}DO THIS NEXT (5 minutes):${RESET}"
echo "    1. Open $CARD"
echo "    2. Save its contents to 1Password (or print + lock in a drawer)"
echo "    3. Also save $KEYFILE to 1Password (the actual key)"
echo "    4. Delete the Desktop card AND any plaintext copies"
echo "    5. Run: ${BLUE}bash scripts/setup-mothership.sh --check${RESET} once a quarter"
echo ""
echo "  ${BOLD}Quarterly resurrection drill:${RESET}"
echo "    Spin up a fresh VM, clone the repo, restore .env + key, run:"
echo "      ${BLUE}bash infra/backup/restore.sh --kind all --latest${RESET}"
echo "    Then ${BLUE}bash scripts/bootstrap-verify.sh${RESET}"
echo "    Expected output: ${GREEN}BOOTSTRAP-VERIFIED${RESET}"
echo ""
echo "  ${BOLD}Next: wire up LLM providers + local LLM + optional services${RESET}"
echo "    Run: ${BLUE}bash scripts/configure-services.sh${RESET}"
echo "    Probes everything, prompts only for what's missing or broken."
echo "    Handles: OpenAI / Anthropic / DeepSeek / Groq / xAI / Mistral / etc."
echo "             Local LLM (MLX or Ollama with model picker)"
echo "             NATS + Docker + optional services (ElevenLabs, LiveKit, etc.)"
echo "             Stale IP detection if you switched networks"
echo ""
echo "  ${BOLD}Want a single-file thumb-drive backup of every secret on this machine?${RESET}"
echo "    Run: ${BLUE}bash scripts/seal-recovery-bundle.sh${RESET}"
echo "    Produces one passphrase-encrypted .age file with everything inside."
echo "    Recover on any fresh laptop with one command:"
echo "      ${BLUE}bash scripts/unseal-recovery-bundle.sh <bundle.age>${RESET}"
echo ""
