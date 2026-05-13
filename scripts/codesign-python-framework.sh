#!/bin/bash
# codesign-python-framework.sh
# Ad-hoc codesigns the Homebrew python@3.14 framework for stable TCC identity.
#
# Why: Homebrew ships python unsigned. macOS TCC keys approvals on the binary's
# code signature identity. An unsigned binary gets a fresh, ephemeral identity
# every launch — TCC re-prompts each time and may even create duplicate entries.
# Ad-hoc signing gives the binary a stable signature hash that survives multiple
# instances and patch upgrades within python@3.14.
#
# Forward-compat:
#  - Idempotent: skips binaries already ad-hoc signed (Signature=adhoc)
#  - Auto-detects active python@3.14 cellar via brew --prefix
#  - MUST be re-run after any `brew upgrade python@3.14` (brew unsigns binaries)
#
# Usage: bash scripts/codesign-python-framework.sh [--force]
#   --force: re-sign even if already ad-hoc signed
#
# Aaron-action if popups persist after sign:
#   tccutil reset AppleEvents
#   (then approve once at next prompt — should stick)

set -euo pipefail

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

# Resolve active cellar (forward-compat: works for 3.14.4, 3.14.5, etc.)
PREFIX="$(brew --prefix python@3.14 2>/dev/null || true)"
if [[ -z "$PREFIX" || ! -d "$PREFIX" ]]; then
  echo "ERROR: python@3.14 not installed via brew" >&2
  exit 1
fi

# brew --prefix gives the symlinked /usr/local/opt path; resolve to real cellar
REAL_PREFIX="$(readlink -f "$PREFIX" 2>/dev/null || /usr/bin/python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$PREFIX")"
echo "[codesign-python] active cellar: $REAL_PREFIX"

BINARIES=(
  "$REAL_PREFIX/bin/python3.14"
  "$REAL_PREFIX/Frameworks/Python.framework/Versions/3.14/Python"
  "$REAL_PREFIX/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
)

# Also sign the .app bundle itself (Python.app) so its CFBundleIdentifier signature
# is stable — this is the bundle TCC actually keys on for "access data from other apps"
APP_BUNDLE="$REAL_PREFIX/Frameworks/Python.framework/Versions/3.14/Resources/Python.app"

is_adhoc() {
  # Capture output first to avoid SIGPIPE under `set -o pipefail` when grep -q
  # exits early — that closes the pipe and codesign's broken-pipe SIGPIPE
  # propagates as a non-zero pipeline exit, causing false negatives.
  local target="$1"
  local out
  out="$(codesign -dvv "$target" 2>&1 || true)"
  [[ "$out" == *"Signature=adhoc"* ]]
}

sign_one() {
  local target="$1"
  if [[ ! -e "$target" ]]; then
    echo "[codesign-python] SKIP missing: $target"
    return 0
  fi
  if (( FORCE == 0 )) && is_adhoc "$target"; then
    echo "[codesign-python] OK already adhoc: $target"
    return 0
  fi
  echo "[codesign-python] SIGN $target"
  codesign --force --sign - --deep "$target" 2>&1 | sed 's/^/  /'
  # Log post-sign hash
  local hash
  hash="$(codesign -dvv "$target" 2>&1 | grep -E "CDHash" | head -1 || echo "CDHash=unknown")"
  echo "  $hash"
}

echo "[codesign-python] === pre-sign state ==="
for b in "${BINARIES[@]}" "$APP_BUNDLE"; do
  echo "--- $b"
  codesign -dvv "$b" 2>&1 | head -4 | sed 's/^/  /' || true
done

echo ""
echo "[codesign-python] === signing ==="
# Strategy: sign the bundle first only if its inner binary is unsigned.
# (Signing the .app bundle re-signs the inner MacOS/Python, so checking the
# inner binary is the canonical idempotency probe.)
INNER_APP_BIN="$APP_BUNDLE/Contents/MacOS/Python"
if (( FORCE == 1 )) || ! is_adhoc "$INNER_APP_BIN"; then
  sign_one "$APP_BUNDLE"
else
  echo "[codesign-python] OK already adhoc (bundle): $APP_BUNDLE"
fi
# Then explicitly sign the standalone binaries
for b in "${BINARIES[@]}"; do
  sign_one "$b"
done

echo ""
echo "[codesign-python] === post-sign state ==="
for b in "${BINARIES[@]}" "$APP_BUNDLE"; do
  echo "--- $b"
  codesign -dvv "$b" 2>&1 | head -6 | sed 's/^/  /' || true
done

echo ""
echo "[codesign-python] DONE. If TCC popups persist, run:"
echo "  tccutil reset AppleEvents"
echo "  (then approve once at next prompt)"
