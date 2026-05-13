#!/usr/bin/env bash
# unseal-recovery-bundle.sh — Decrypt a recovery bundle and (optionally)
# run its embedded AUTO-RESTORE.sh end-to-end.
#
# This is the script you run on a fresh laptop 6 months from now.
# Prerequisite: `age` is installed and the bundle's passphrase is known.
#
# Usage:
#   bash scripts/unseal-recovery-bundle.sh /Volumes/USB/contextdna-recovery-*.age
#   bash scripts/unseal-recovery-bundle.sh --check  /path/to/bundle.age   # peek at manifest, no restore
#   bash scripts/unseal-recovery-bundle.sh --no-auto /path/to/bundle.age  # extract only, no auto-restore
#
# ZSF: every failure is named and exits non-zero.

set -uo pipefail

CHECK_ONLY=false
NO_AUTO=false
BUNDLE=""
for arg in "$@"; do
    case "$arg" in
        --check)   CHECK_ONLY=true ;;
        --no-auto) NO_AUTO=true ;;
        --help|-h) sed -n '2,18p' "$0" | sed 's|^# ||; s|^#||'; exit 0 ;;
        *)         BUNDLE="$arg" ;;
    esac
done

if [ -t 1 ]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1); BLUE=$(tput setaf 4); DIM=$(tput dim); RESET=$(tput sgr0)
else BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; DIM=""; RESET=""; fi
_step() { echo ""; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
_ok()   { echo "  ${GREEN}✓${RESET} $*"; }
_fail() { echo "  ${RED}✗${RESET} $*" >&2; exit 1; }
_info() { echo "  ${DIM}$*${RESET}"; }

[ -n "$BUNDLE" ] || _fail "usage: $0 [--check|--no-auto] <bundle.age>"
[ -f "$BUNDLE" ] || _fail "bundle not found: $BUNDLE"
command -v age >/dev/null || _fail "age not found (brew install age / apt install age)"
command -v tar >/dev/null || _fail "tar not found"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cat <<EOF

${BOLD}ContextDNA Recovery Bundle — Unsealer${RESET}
${DIM}Bundle: $BUNDLE${RESET}

Mode: ${BOLD}$( $CHECK_ONLY && echo check-only || $NO_AUTO && echo extract-only || echo full-auto-restore )${RESET}

EOF

_step "Step 1/3  Decrypt"
STAGE="$(mktemp -d -t ctxdna-unseal.XXXXXX)" || _fail "mktemp failed"
trap 'rm -rf "$STAGE"' EXIT

TAR_FILE="$STAGE/bundle.tar.gz"
# Detect format: .age uses age, .enc uses openssl AES-256-CBC.
# Magic bytes: age files start with "age-encryption.org/v1\n"; openssl files start with "Salted__".
HEAD="$(head -c 24 "$BUNDLE" 2>/dev/null)"
if [[ "$HEAD" == *"age-encryption.org"* ]]; then
    command -v age >/dev/null || _fail "age not found (brew install age)"
    echo "  ${BOLD}Enter passphrase below (age format).${RESET}"
    age --decrypt -o "$TAR_FILE" "$BUNDLE" || _fail "age decrypt failed (wrong passphrase or corrupted bundle)"
    _ok "age decryption succeeded"
elif [[ "$HEAD" == "Salted__"* ]]; then
    command -v openssl >/dev/null || _fail "openssl not found"
    echo "  ${BOLD}Enter passphrase below (openssl AES-256-CBC + PBKDF2).${RESET}"
    # openssl reads passphrase from /dev/tty if available, else stdin
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 250000 -salt \
        -in "$BUNDLE" -out "$TAR_FILE" \
        || _fail "openssl decrypt failed (wrong passphrase or corrupted bundle)"
    _ok "openssl decryption succeeded"
else
    _fail "bundle format not recognised (expected age or openssl AES-256-CBC)"
fi

EXTRACT_DIR="$STAGE/extracted"
mkdir -p "$EXTRACT_DIR"
tar xzf "$TAR_FILE" -C "$EXTRACT_DIR" || _fail "tar extract failed"
_ok "archive extracted ($(find "$EXTRACT_DIR" -type f | wc -l | tr -d ' ') files)"

_step "Step 2/3  Manifest"
MANIFEST="$EXTRACT_DIR/manifest.json"
[ -f "$MANIFEST" ] || _fail "manifest.json missing — not a valid bundle"
cat "$MANIFEST" | python3 -m json.tool 2>/dev/null || cat "$MANIFEST"

if $CHECK_ONLY; then
    _ok "check-only mode: bundle is decryptable and well-formed"
    exit 0
fi

if $NO_AUTO; then
    # Persist the extracted bundle next to the .age file so user can do it manually
    DEST="${BUNDLE%.age}.unsealed"
    rm -rf "$DEST"
    mv "$EXTRACT_DIR" "$DEST"
    trap - EXIT
    _ok "extracted to $DEST"
    _info "to finish manually: cd $DEST && bash AUTO-RESTORE.sh"
    exit 0
fi

_step "Step 3/3  Run embedded AUTO-RESTORE.sh"
[ -x "$EXTRACT_DIR/AUTO-RESTORE.sh" ] || _fail "AUTO-RESTORE.sh missing or not executable in bundle"

# Pass through REPO_DIR if user set it; otherwise let AUTO-RESTORE auto-detect this checkout
export REPO_DIR="${REPO_DIR:-$REPO_ROOT}"
_info "using REPO_DIR=$REPO_DIR (override with REPO_DIR=… bash …)"

bash "$EXTRACT_DIR/AUTO-RESTORE.sh"
