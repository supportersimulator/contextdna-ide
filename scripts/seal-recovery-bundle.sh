#!/usr/bin/env bash
# seal-recovery-bundle.sh — Package every secret needed for full recovery into
# a SINGLE passphrase-encrypted file you can save to a thumb drive.
#
# What goes in the bundle (whatever exists on this machine):
#   - .env                                  (current secrets in the repo)
#   - $BACKUP_AGE_KEYFILE                   (the age private key for S3 backups)
#   - ~/.aws/credentials + ~/.aws/config    (AWS access)
#   - macOS Keychain: contextdna-* items    (bucket config, etc.)
#   - ~/.ssh/id_ed25519* (optional)         (only if user opts in)
#   - manifest.json                         (what's inside, when, where it came from)
#   - AUTO-RESTORE.sh                       (the self-contained restore script)
#
# Output:
#   ~/Desktop/contextdna-recovery-<date>-<hostname>.age
#   ~/Desktop/contextdna-recovery-README.txt   (printable instructions)
#
# Usage:
#   bash scripts/seal-recovery-bundle.sh                # interactive, prompts passphrase
#   bash scripts/seal-recovery-bundle.sh --output PATH  # custom output path
#   bash scripts/seal-recovery-bundle.sh --include-ssh  # also bundle ~/.ssh/id_ed25519
#
# Recovery (6 months from now, fresh laptop):
#   brew install age
#   git clone git@github.com:supportersimulator/contextdna-ide.git
#   cd contextdna-ide
#   bash scripts/unseal-recovery-bundle.sh ~/path/to/bundle.age
#
# ZSF: every failure path is named and exits non-zero.

set -uo pipefail

INCLUDE_SSH=false
OUTPUT_PATH=""
PASSPHRASE_SOURCE="tty"   # tty | stdin | clipboard | env:NAME
for arg in "$@"; do
    case "$arg" in
        --include-ssh) INCLUDE_SSH=true ;;
        --output) OUTPUT_PATH="next" ;;
        --passphrase-stdin)     PASSPHRASE_SOURCE="stdin" ;;
        --passphrase-clipboard) PASSPHRASE_SOURCE="clipboard" ;;
        --passphrase-env)       PASSPHRASE_SOURCE="env" ;;
        --help|-h)
            sed -n '2,30p' "$0" | sed 's|^# ||; s|^#||'
            exit 0 ;;
        *)
            if [ "$OUTPUT_PATH" = "next" ]; then OUTPUT_PATH="$arg"
            elif [[ "$PASSPHRASE_SOURCE" = "env" && "$arg" =~ ^[A-Z_]+$ ]]; then PASSPHRASE_SOURCE="env:$arg"
            else echo "unknown arg: $arg" >&2; exit 2
            fi ;;
    esac
done

# ── Colors ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1); BLUE=$(tput setaf 4); DIM=$(tput dim); RESET=$(tput sgr0)
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; DIM=""; RESET=""
fi
_step() { echo ""; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
_ok()   { echo "  ${GREEN}✓${RESET} $*"; }
_warn() { echo "  ${YELLOW}⚠${RESET} $*"; }
_fail() { echo "  ${RED}✗ FAIL:${RESET} $*" >&2; exit 1; }
_info() { echo "  ${DIM}$*${RESET}"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname)"
DATE_TAG="$(date +%Y%m%d)"
DEFAULT_OUT="$HOME/Desktop/contextdna-recovery-${DATE_TAG}-${HOSTNAME_SHORT}.age"
[ -z "$OUTPUT_PATH" ] && OUTPUT_PATH="$DEFAULT_OUT"

# ── Preflight ────────────────────────────────────────────────────────────────
command -v age >/dev/null || _fail "age not found (brew install age)"
command -v tar >/dev/null || _fail "tar not found"

cat <<EOF

${BOLD}ContextDNA Recovery Bundle Sealer${RESET}
${DIM}Packages every secret needed for full recovery into one encrypted file.${RESET}

Output: ${BOLD}$OUTPUT_PATH${RESET}

You'll be asked for a ${BOLD}passphrase${RESET}. Pick one you can remember in 6 months —
or store it in 1Password and call this the "ContextDNA recovery passphrase".
Without it, the bundle is unrecoverable. The passphrase is the ONLY thing
that protects every secret in this archive.

EOF

# ── Stage 1: collect into a temp tree ────────────────────────────────────────
_step "Step 1/4  Collecting secrets"

STAGE="$(mktemp -d -t ctxdna-seal.XXXXXX)" || _fail "mktemp failed"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/secrets" "$STAGE/repo" "$STAGE/aws" "$STAGE/ssh" "$STAGE/ecosystem"

# .env (the master config)
if [ -f "$REPO_ROOT/.env" ]; then
    cp "$REPO_ROOT/.env" "$STAGE/repo/.env"
    chmod 600 "$STAGE/repo/.env"
    _ok ".env captured ($(wc -l < "$REPO_ROOT/.env" | tr -d ' ') lines)"
else
    _warn ".env not found (skipping — make sure setup-mothership.sh ran first)"
fi

# age private key (parse from .env first, fall back to default location)
AGE_KEYFILE_HINT="$(grep '^BACKUP_AGE_KEYFILE=' "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- | tr -d '"')"
AGE_KEYFILE="${AGE_KEYFILE_HINT:-$HOME/.ssh/contextdna-backup.age.key}"
AGE_KEYFILE="${AGE_KEYFILE/#\$HOME/$HOME}"
AGE_KEYFILE="${AGE_KEYFILE/#\~/$HOME}"
if [ -f "$AGE_KEYFILE" ]; then
    cp "$AGE_KEYFILE" "$STAGE/secrets/contextdna-backup.age.key"
    chmod 600 "$STAGE/secrets/contextdna-backup.age.key"
    _ok "age private key captured ($(basename "$AGE_KEYFILE"))"
else
    _warn "age private key not found at $AGE_KEYFILE — backups will be unrecoverable without it"
fi

# AWS credentials
if [ -f "$HOME/.aws/credentials" ]; then
    cp "$HOME/.aws/credentials" "$STAGE/aws/credentials"
    chmod 600 "$STAGE/aws/credentials"
    _ok "~/.aws/credentials captured"
fi
if [ -f "$HOME/.aws/config" ]; then
    cp "$HOME/.aws/config" "$STAGE/aws/config"
    _ok "~/.aws/config captured"
fi
[ ! -f "$STAGE/aws/credentials" ] && _warn "~/.aws/credentials not found (you'll need AWS keys at restore time)"

# macOS Keychain entries (contextdna-*)
if [ "$(uname -s)" = "Darwin" ]; then
    KC_DUMP="$STAGE/secrets/keychain-contextdna.txt"
    echo "# macOS Keychain dump (contextdna-* services) — restored verbatim via security add-generic-password" > "$KC_DUMP"
    chmod 600 "$KC_DUMP"
    FOUND=0
    for service in backup_bucket backup_s3_endpoint; do
        val="$(security find-generic-password -a "$USER" -s "contextdna-$service" -w 2>/dev/null || true)"
        if [ -n "$val" ]; then
            echo "${service}=${val}" >> "$KC_DUMP"
            FOUND=$((FOUND + 1))
        fi
    done
    _ok "Keychain: $FOUND contextdna-* entries captured"
fi

# SSH key (opt-in)
if $INCLUDE_SSH; then
    for keyname in id_ed25519 id_ed25519.pub id_rsa id_rsa.pub; do
        if [ -f "$HOME/.ssh/$keyname" ]; then
            cp "$HOME/.ssh/$keyname" "$STAGE/ssh/$keyname"
            chmod 600 "$STAGE/ssh/$keyname"
        fi
    done
    _ok "SSH keys captured (you opted in via --include-ssh)"
else
    _info "SSH keys NOT included (re-run with --include-ssh if you want GitHub auth bundled)"
    _info "without them, you'll need to re-issue SSH keys + add to GitHub on the recovery machine"
fi

# Ecosystem configs — Claude Code, 3-Surgeons, Multi-Fleet, MCP wiring
if [ -f "$HOME/.claude/settings.json" ]; then
    cp "$HOME/.claude/settings.json" "$STAGE/ecosystem/claude-settings.json"
    chmod 600 "$STAGE/ecosystem/claude-settings.json"
    _ok "~/.claude/settings.json captured"
fi
if [ -f "$HOME/.claude/CLAUDE.md" ]; then
    cp "$HOME/.claude/CLAUDE.md" "$STAGE/ecosystem/claude-CLAUDE.md"
    _ok "~/.claude/CLAUDE.md captured (user instructions)"
fi
if [ -f "$HOME/.3surgeons/config.yaml" ]; then
    cp "$HOME/.3surgeons/config.yaml" "$STAGE/ecosystem/3surgeons-config.yaml"
    chmod 600 "$STAGE/ecosystem/3surgeons-config.yaml"
    _ok "~/.3surgeons/config.yaml captured"
fi
if [ -f "$REPO_ROOT/.mcp.json" ]; then
    cp "$REPO_ROOT/.mcp.json" "$STAGE/ecosystem/mcp.json"
    _ok ".mcp.json snapshot captured"
fi
if [ -f "$REPO_ROOT/.multifleet/config.json" ]; then
    cp "$REPO_ROOT/.multifleet/config.json" "$STAGE/ecosystem/multifleet-config.json"
    _ok ".multifleet/config.json captured"
fi
# Snapshot the list of installed Claude Code plugins (names only, useful for re-install)
if command -v claude >/dev/null 2>&1; then
    claude plugin list 2>/dev/null > "$STAGE/ecosystem/claude-plugins-installed.txt" || true
    [ -s "$STAGE/ecosystem/claude-plugins-installed.txt" ] && _ok "Claude Code plugin list captured ($(wc -l < "$STAGE/ecosystem/claude-plugins-installed.txt" | tr -d ' ') entries)"
fi
# launchd plists (so model + daemon configs survive)
if [ "$(uname -s)" = "Darwin" ]; then
    mkdir -p "$STAGE/ecosystem/launchd"
    for plist in ~/Library/LaunchAgents/io.contextdna.*.plist ~/Library/LaunchAgents/com.contextdna.*.plist; do
        [ -f "$plist" ] && cp "$plist" "$STAGE/ecosystem/launchd/"
    done
    plist_count=$(ls "$STAGE/ecosystem/launchd/" 2>/dev/null | wc -l | tr -d ' ')
    [ "$plist_count" -gt 0 ] && _ok "$plist_count launchd plist(s) captured"
fi

# Repo identity (so restore knows which repo to clone)
GIT_REMOTE="$(cd "$REPO_ROOT" && git remote get-url origin 2>/dev/null || echo "git@github.com:supportersimulator/contextdna-ide.git")"
GIT_COMMIT="$(cd "$REPO_ROOT" && git rev-parse HEAD 2>/dev/null || echo "unknown")"

# ── Stage 2: manifest + AUTO-RESTORE.sh ──────────────────────────────────────
_step "Step 2/4  Generating manifest + bundled restore script"

cat > "$STAGE/manifest.json" <<EOF
{
  "format_version": "1",
  "sealed_at":      "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "sealed_on_host": "$HOSTNAME_SHORT",
  "sealed_on_user": "$USER",
  "platform":       "$(uname -s)",
  "git_remote":     "$GIT_REMOTE",
  "git_commit":     "$GIT_COMMIT",
  "contents": {
    "env":             $( [ -f "$STAGE/repo/.env" ] && echo true || echo false ),
    "age_key":         $( [ -f "$STAGE/secrets/contextdna-backup.age.key" ] && echo true || echo false ),
    "aws_creds":       $( [ -f "$STAGE/aws/credentials" ] && echo true || echo false ),
    "keychain":        $( [ -f "$STAGE/secrets/keychain-contextdna.txt" ] && echo true || echo false ),
    "ssh_keys":        $( ls "$STAGE/ssh/" 2>/dev/null | grep -q . && echo true || echo false ),
    "claude_settings": $( [ -f "$STAGE/ecosystem/claude-settings.json" ] && echo true || echo false ),
    "claude_md":       $( [ -f "$STAGE/ecosystem/claude-CLAUDE.md" ] && echo true || echo false ),
    "3surgeons_cfg":   $( [ -f "$STAGE/ecosystem/3surgeons-config.yaml" ] && echo true || echo false ),
    "mcp_json":        $( [ -f "$STAGE/ecosystem/mcp.json" ] && echo true || echo false ),
    "multifleet_cfg":  $( [ -f "$STAGE/ecosystem/multifleet-config.json" ] && echo true || echo false ),
    "claude_plugins":  $( [ -f "$STAGE/ecosystem/claude-plugins-installed.txt" ] && echo true || echo false ),
    "launchd_plists":  $( ls "$STAGE/ecosystem/launchd/" 2>/dev/null | grep -q . && echo true || echo false )
  }
}
EOF
_ok "manifest.json written"

# Bundled AUTO-RESTORE.sh: re-emits files to the right places + invokes the
# public-repo's bootstrap chain. It is intentionally self-contained — assumes
# only `bash`, `tar`, `cp`, `mkdir`, `chmod`, `security` (macOS), and that
# the user has `cd`'d into the unsealed directory.
cat > "$STAGE/AUTO-RESTORE.sh" <<'AUTORESTOREEOF'
#!/usr/bin/env bash
# AUTO-RESTORE.sh — restores secrets from this bundle, then triggers the
# repo's bootstrap chain. Run this from the directory where you unsealed.
#
# Pre-requisite: you have already cloned supportersimulator/contextdna-ide
# (the public repo) somewhere AND you know that path. We default to a
# sibling directory or prompt for it.
set -uo pipefail

if [ -t 1 ]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1); BLUE=$(tput setaf 4); RESET=$(tput sgr0)
else BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""; fi
_ok() { echo "  ${GREEN}✓${RESET} $*"; }
_fail() { echo "  ${RED}✗${RESET} $*" >&2; exit 1; }
_warn() { echo "  ${YELLOW}⚠${RESET} $*"; }
_step() { echo ""; echo "${BOLD}${BLUE}▶ $*${RESET}"; }

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$BUNDLE_DIR/manifest.json" ] || _fail "manifest.json missing — not a valid bundle dir"

REPO_DIR="${REPO_DIR:-}"
if [ -z "$REPO_DIR" ]; then
    # Auto-detect a sibling contextdna-ide checkout
    for cand in "$BUNDLE_DIR/../contextdna-ide" "$HOME/dev/contextdna-ide" "$HOME/contextdna-ide" "$(pwd)/contextdna-ide"; do
        if [ -d "$cand/.git" ] && [ -f "$cand/scripts/setup-mothership.sh" ]; then
            REPO_DIR="$cand"; break
        fi
    done
fi

if [ -z "$REPO_DIR" ] || [ ! -d "$REPO_DIR/.git" ]; then
    GIT_REMOTE="$(python3 -c "import json; print(json.load(open('$BUNDLE_DIR/manifest.json'))['git_remote'])" 2>/dev/null || echo "git@github.com:supportersimulator/contextdna-ide.git")"
    echo ""
    echo "No checkout found. Clone the public repo first, then re-run with REPO_DIR set:"
    echo ""
    echo "    git clone $GIT_REMOTE ~/dev/contextdna-ide"
    echo "    REPO_DIR=~/dev/contextdna-ide bash AUTO-RESTORE.sh"
    echo ""
    exit 2
fi

REPO_DIR="$(cd "$REPO_DIR" && pwd)"
_step "Restoring into $REPO_DIR"

# 1. .env
if [ -f "$BUNDLE_DIR/repo/.env" ]; then
    cp "$BUNDLE_DIR/repo/.env" "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    _ok ".env restored"
fi

# 2. age private key
if [ -f "$BUNDLE_DIR/secrets/contextdna-backup.age.key" ]; then
    AGE_KEY_DEST="${BACKUP_AGE_KEYFILE:-$HOME/.ssh/contextdna-backup.age.key}"
    AGE_KEY_DEST="${AGE_KEY_DEST/#\$HOME/$HOME}"; AGE_KEY_DEST="${AGE_KEY_DEST/#\~/$HOME}"
    mkdir -p "$(dirname "$AGE_KEY_DEST")"; chmod 700 "$(dirname "$AGE_KEY_DEST")"
    cp "$BUNDLE_DIR/secrets/contextdna-backup.age.key" "$AGE_KEY_DEST"
    chmod 600 "$AGE_KEY_DEST"
    _ok "age private key restored to $AGE_KEY_DEST"
fi

# 3. AWS creds
if [ -f "$BUNDLE_DIR/aws/credentials" ]; then
    mkdir -p "$HOME/.aws"; chmod 700 "$HOME/.aws"
    cp "$BUNDLE_DIR/aws/credentials" "$HOME/.aws/credentials"
    chmod 600 "$HOME/.aws/credentials"
    [ -f "$BUNDLE_DIR/aws/config" ] && cp "$BUNDLE_DIR/aws/config" "$HOME/.aws/config"
    _ok "AWS credentials restored to ~/.aws/"
fi

# 4. Keychain (macOS)
if [ "$(uname -s)" = "Darwin" ] && [ -f "$BUNDLE_DIR/secrets/keychain-contextdna.txt" ]; then
    while IFS='=' read -r key val; do
        [ -z "$key" ] || [[ "$key" =~ ^# ]] && continue
        security delete-generic-password -a "$USER" -s "contextdna-$key" >/dev/null 2>&1 || true
        security add-generic-password -a "$USER" -s "contextdna-$key" -w "$val" 2>/dev/null && \
            _ok "Keychain: contextdna-$key restored"
    done < "$BUNDLE_DIR/secrets/keychain-contextdna.txt"
fi

# 5. SSH keys (if bundled)
if [ -d "$BUNDLE_DIR/ssh" ] && ls "$BUNDLE_DIR/ssh"/* >/dev/null 2>&1; then
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
    for k in "$BUNDLE_DIR/ssh"/*; do
        name="$(basename "$k")"
        cp "$k" "$HOME/.ssh/$name"
        [[ "$name" == *.pub ]] && chmod 644 "$HOME/.ssh/$name" || chmod 600 "$HOME/.ssh/$name"
        _ok "SSH key restored: ~/.ssh/$name"
    done
fi

# 5b. Ecosystem configs (Claude Code settings, 3-Surgeons config, launchd plists)
if [ -d "$BUNDLE_DIR/ecosystem" ]; then
    if [ -f "$BUNDLE_DIR/ecosystem/claude-settings.json" ]; then
        mkdir -p "$HOME/.claude"
        cp "$BUNDLE_DIR/ecosystem/claude-settings.json" "$HOME/.claude/settings.json"
        chmod 600 "$HOME/.claude/settings.json"
        _ok "~/.claude/settings.json restored"
    fi
    if [ -f "$BUNDLE_DIR/ecosystem/claude-CLAUDE.md" ]; then
        cp "$BUNDLE_DIR/ecosystem/claude-CLAUDE.md" "$HOME/.claude/CLAUDE.md"
        _ok "~/.claude/CLAUDE.md restored (user instructions)"
    fi
    if [ -f "$BUNDLE_DIR/ecosystem/3surgeons-config.yaml" ]; then
        mkdir -p "$HOME/.3surgeons"; chmod 700 "$HOME/.3surgeons"
        cp "$BUNDLE_DIR/ecosystem/3surgeons-config.yaml" "$HOME/.3surgeons/config.yaml"
        chmod 600 "$HOME/.3surgeons/config.yaml"
        _ok "~/.3surgeons/config.yaml restored"
    fi
    if [ -f "$BUNDLE_DIR/ecosystem/mcp.json" ] && [ ! -f "$REPO_DIR/.mcp.json" ]; then
        cp "$BUNDLE_DIR/ecosystem/mcp.json" "$REPO_DIR/.mcp.json"
        _ok ".mcp.json restored to repo"
    fi
    if [ -f "$BUNDLE_DIR/ecosystem/multifleet-config.json" ]; then
        mkdir -p "$REPO_DIR/.multifleet"
        cp "$BUNDLE_DIR/ecosystem/multifleet-config.json" "$REPO_DIR/.multifleet/config.json"
        _ok ".multifleet/config.json restored"
    fi
    if [ -d "$BUNDLE_DIR/ecosystem/launchd" ] && [ "$(uname -s)" = "Darwin" ]; then
        mkdir -p "$HOME/Library/LaunchAgents"
        for plist in "$BUNDLE_DIR/ecosystem/launchd"/*.plist; do
            [ -f "$plist" ] || continue
            name="$(basename "$plist")"
            cp "$plist" "$HOME/Library/LaunchAgents/$name"
            launchctl unload "$HOME/Library/LaunchAgents/$name" 2>/dev/null || true
            launchctl load -w "$HOME/Library/LaunchAgents/$name" 2>/dev/null \
                && _ok "launchd: $name loaded" \
                || _warn "launchd: $name load failed"
        done
    fi
    if [ -f "$BUNDLE_DIR/ecosystem/claude-plugins-installed.txt" ]; then
        _ok "Claude Code plugin list available (will be reinstalled by configure-ecosystem.sh):"
        sed 's/^/      /' "$BUNDLE_DIR/ecosystem/claude-plugins-installed.txt"
    fi
fi

# 6. Probe + reconfigure services (this is the big one — handles rotated
#    API keys, different LLM providers, IP changes, missing optional services)
_step "Probing services + reconfiguring anything that's changed"
if [ -x "$REPO_DIR/scripts/configure-services.sh" ]; then
    # Probe first to see what works as-is
    bash "$REPO_DIR/scripts/configure-services.sh" --probe 2>&1 | tail -30
    echo ""
    read -r -p "  Run full interactive configurator now (recommended)? [Y/n]: " ans
    if [[ ! "$ans" =~ ^[Nn] ]]; then
        bash "$REPO_DIR/scripts/configure-services.sh" || _warn "configure-services had issues — re-run manually"
    fi
fi

# 6b. Ecosystem tools (Multi-Fleet, 3-Surgeons, Superpowers, MCP wiring)
_step "Probing + installing ecosystem tools"
if [ -x "$REPO_DIR/scripts/configure-ecosystem.sh" ]; then
    bash "$REPO_DIR/scripts/configure-ecosystem.sh" --probe 2>&1 | tail -30
    echo ""
    read -r -p "  Run full ecosystem configurator (install missing plugins)? [Y/n]: " ans
    if [[ ! "$ans" =~ ^[Nn] ]]; then
        bash "$REPO_DIR/scripts/configure-ecosystem.sh" || _warn "configure-ecosystem had issues — re-run manually"
    fi
fi

# 6b. Run setup-mothership.sh --check to verify backup config
_step "Verifying setup"
bash "$REPO_DIR/scripts/setup-mothership.sh" --check 2>&1 | tail -20 || true
_ok "setup check complete"

# 7. Pull latest backup from S3 (if AWS creds + age key both present)
if [ -f "$HOME/.aws/credentials" ] && [ -f "${AGE_KEY_DEST:-$HOME/.ssh/contextdna-backup.age.key}" ]; then
    _step "Restoring data from latest S3 snapshot"
    set -a; . "$REPO_DIR/.env"; set +a
    if bash "$REPO_DIR/infra/backup/restore.sh" --list 2>&1 | head -10; then
        echo ""
        read -r -p "  Run restore --kind all --latest now? [Y/n]: " ans
        if [[ ! "$ans" =~ ^[Nn] ]]; then
            bash "$REPO_DIR/infra/backup/restore.sh" --kind all --latest \
                && _ok "data restored from S3" \
                || _warn "S3 restore had issues — check output above"
        fi
    fi
fi

# 8. Bring up the stack
if command -v docker >/dev/null && [ -f "$REPO_DIR/docker-compose.lite.yml" ]; then
    _step "Starting docker stack"
    (cd "$REPO_DIR" && docker compose -f docker-compose.lite.yml up -d) \
        && _ok "docker stack up" \
        || _warn "docker compose failed — start manually"
fi

# 9. Install launchd / cron schedule
_step "Installing backup schedule"
(cd "$REPO_DIR" && bash scripts/setup-mothership.sh --non-interactive 2>&1 | grep -E 'scheduled|cron|FAIL' | head -5)

echo ""
echo "${BOLD}${GREEN}━━━ Auto-restore complete ━━━${RESET}"
echo "  Verify with:"
echo "    curl -sf http://localhost:8855/health | jq"
echo "    bash $REPO_DIR/scripts/bootstrap-verify.sh"
echo ""
AUTORESTOREEOF

chmod +x "$STAGE/AUTO-RESTORE.sh"
_ok "AUTO-RESTORE.sh embedded ($(wc -l < "$STAGE/AUTO-RESTORE.sh" | tr -d ' ') lines)"

# ── Stage 3: tar + age-encrypt with passphrase ───────────────────────────────
_step "Step 3/4  Encrypting bundle"

TAR_FILE="$(mktemp -t ctxdna-tar.XXXXXX).tar.gz"
(cd "$STAGE" && tar czf "$TAR_FILE" .) || _fail "tar failed"
TAR_SIZE="$(wc -c < "$TAR_FILE" | tr -d ' ')"
_ok "archive packed ($TAR_SIZE bytes)"

# Source the passphrase
case "$PASSPHRASE_SOURCE" in
    tty)
        echo ""
        echo "  ${BOLD}Choose a passphrase you can remember in 6 months.${RESET}"
        echo "  ${YELLOW}Write it down. Save it in 1Password. Without it, this bundle is gone.${RESET}"
        echo ""
        { command -v rage >/dev/null && rage --passphrase -o "$OUTPUT_PATH" "$TAR_FILE"; } || age --passphrase -o "$OUTPUT_PATH" "$TAR_FILE" || _fail "age encrypt failed"
        ;;
    stdin)
        command -v openssl >/dev/null || _fail "openssl not found"
        _info "encrypting with openssl AES-256-CBC + PBKDF2 (passphrase from stdin)"
        OUTPUT_PATH="${OUTPUT_PATH%.age}.enc"
        openssl enc -aes-256-cbc -pbkdf2 -iter 250000 -salt \
            -pass stdin -in "$TAR_FILE" -out "$OUTPUT_PATH" 2>&1 | tail -5 \
            || _fail "openssl encrypt failed"
        ;;
    clipboard)
        command -v pbpaste >/dev/null || _fail "--passphrase-clipboard requires pbpaste (macOS)"
        command -v openssl >/dev/null || _fail "openssl not found"
        _info "encrypting with openssl AES-256-CBC + PBKDF2 (passphrase from pbpaste)"
        # openssl reads passphrase from stdin via '-pass stdin' — works in any environment.
        # Output extension switched to .enc to signal openssl format (not age).
        OUTPUT_PATH="${OUTPUT_PATH%.age}.enc"
        pbpaste | openssl enc -aes-256-cbc -pbkdf2 -iter 250000 -salt \
            -pass stdin -in "$TAR_FILE" -out "$OUTPUT_PATH" 2>&1 | tail -5 \
            || _fail "openssl encrypt failed"
        ;;
    env:*)
        varname="${PASSPHRASE_SOURCE#env:}"
        passval="${!varname:-}"
        [ -n "$passval" ] || _fail "env var $varname is empty"
        command -v openssl >/dev/null || _fail "openssl not found"
        _info "encrypting with openssl AES-256-CBC + PBKDF2 (passphrase from env $varname)"
        OUTPUT_PATH="${OUTPUT_PATH%.age}.enc"
        printf '%s' "$passval" | openssl enc -aes-256-cbc -pbkdf2 -iter 250000 -salt \
            -pass stdin -in "$TAR_FILE" -out "$OUTPUT_PATH" 2>&1 | tail -5 \
            || _fail "openssl encrypt failed"
        unset passval
        ;;
esac
rm -f "$TAR_FILE"
chmod 600 "$OUTPUT_PATH"
OUT_SIZE="$(wc -c < "$OUTPUT_PATH" | tr -d ' ')"
_ok "encrypted bundle written ($OUT_SIZE bytes)"

# ── Stage 4: printable README ────────────────────────────────────────────────
_step "Step 4/4  Printable recovery instructions"

README_PATH="$HOME/Desktop/contextdna-recovery-README-${DATE_TAG}.txt"
cat > "$README_PATH" <<EOF
================================================================================
ContextDNA Mothership — Single-File Recovery Bundle
================================================================================

WHAT THIS IS
  A passphrase-encrypted .age file that contains every secret needed to
  rebuild your mothership from scratch.

  Inside (encrypted): .env, age private key, AWS credentials, macOS
  Keychain entries, optionally SSH keys, and AUTO-RESTORE.sh.

  Without the passphrase, the file is gone. Keep the passphrase in
  1Password / a hardware key / written on paper in a fireproof box.

FILES
  Bundle:     $OUTPUT_PATH
  This card:  $README_PATH

WHAT TO DO RIGHT NOW
  1. Copy $(basename "$OUTPUT_PATH") to a thumb drive
     (any USB stick, any encrypted external drive, any safe-deposit box)
  2. Copy this README to the same thumb drive
  3. Verify the thumb drive plugs in cleanly and reads back
  4. Delete the Desktop copies (this file AND the .age file)
  5. Verify your offline-backup passphrase is in 1Password

QUARTERLY DRILL (do this once every 3 months on a throwaway VM)
  bash scripts/unseal-recovery-bundle.sh <path-to-bundle>.age
  # If BOOTSTRAP-VERIFIED prints at the end, your drill passed.

================================================================================
RECOVERY — 6 MONTHS FROM NOW, FRESH LAPTOP, NOTHING ELSE
================================================================================

  STEP 1 — Install the only tools you need
    brew install age git
    # (or apt/dnf install age git)

  STEP 2 — Clone the public repo
    git clone $GIT_REMOTE ~/dev/contextdna-ide
    cd ~/dev/contextdna-ide

  STEP 3 — Plug in your thumb drive and run unseal
    bash scripts/unseal-recovery-bundle.sh /Volumes/<DRIVE>/$(basename "$OUTPUT_PATH")
    # Or copy the .age file anywhere first; just pass its path.
    # You'll be prompted for your passphrase.

  STEP 4 — Wait
    The unseal script auto-installs everything else (docker, aws, nats…),
    restores .env + keys + Keychain + AWS creds, pulls the latest data
    snapshot from S3, brings up the docker stack, installs the backup
    schedule, and verifies the bootstrap. ~10 minutes total.

  STEP 5 — Final check
    Expected last line of output:
        BOOTSTRAP-VERIFIED
    If you see it: the mothership is back.
    If you don't: read docs/operational-invariance.md §6 (troubleshooting).

================================================================================
SEALED:    $(date)
HOST:      $HOSTNAME_SHORT
USER:      $USER
COMMIT:    $GIT_COMMIT
PLATFORM:  $(uname -s)
GIT REMOTE: $GIT_REMOTE
================================================================================

REMEMBER: this README is safe to share. The .age bundle is NOT — it
contains every secret. Treat it like cash.
EOF
chmod 600 "$README_PATH"
_ok "README written to $README_PATH"

echo ""
echo "${BOLD}${GREEN}━━━ Bundle sealed ━━━${RESET}"
echo ""
echo "  Bundle: ${BOLD}$OUTPUT_PATH${RESET}"
echo "  Card:   ${BOLD}$README_PATH${RESET}"
echo ""
echo "  ${BOLD}${YELLOW}DO THIS NEXT (5 minutes):${RESET}"
echo "    1. Copy both files to a thumb drive"
echo "    2. Test-read them on a different computer"
echo "    3. Save your passphrase to 1Password"
echo "    4. Delete the Desktop copies"
echo "    5. Schedule a quarterly drill (open Calendar, set reminder)"
echo ""
echo "  To verify the bundle without restoring: ${BLUE}bash scripts/unseal-recovery-bundle.sh --check $OUTPUT_PATH${RESET}"
echo ""
