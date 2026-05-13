#!/usr/bin/env bash
# scripts/install.sh — Unified installer for Context DNA IDE.
#
# Brings a fresh checkout from `git clone` to a runnable Context DNA IDE in
# under 10 minutes. Six steps:
#   1. Python venv at .venv/ + `pip install -e multi-fleet/.[full]`
#   2. Run multi-fleet/scripts/install.sh (fleet daemon, --skip-firewall by default)
#   3. context-dna/clients/vscode: npm install && npm run compile
#   4. context-dna/clients/vscode: npx vsce package (produce .vsix)
#   5. (optional, --with-admin) admin.contextdna.io: npm install && npm run build
#   6. Print summary + next-step commands (demo.sh, code --install-extension)
#
# Each step is idempotent: a marker file under .install-state/ is written on
# success and consulted on re-run. Use --force to bypass markers.
#
# Flags:
#   --with-admin     Also build the admin.contextdna.io Next.js app
#   --skip-vsix      Skip vsce package (compile only)
#   --skip-firewall  Pass-through to multi-fleet/scripts/install.sh (default ON)
#   --with-firewall  Disable --skip-firewall, let multi-fleet harden the host
#   --dry-run        Print every command, execute nothing
#   --yes            Auto-confirm destructive prompts (CI)
#   --force          Re-run all steps even if marker says they're done
#   --help|-h        Show this help
#
# Refuses to run on Windows native (cmd/PowerShell). Use WSL2.

set -u  # do not -e: per-step traps emit clearer diagnostics.

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$REPO_ROOT/.install-state"
LOG_FILE="$STATE_DIR/install.log"

WITH_ADMIN=0
SKIP_VSIX=0
SKIP_FIREWALL=1   # default ON: top-level installer defers fleet hardening to the user
DRY_RUN=0
ASSUME_YES=0
FORCE=0

CURRENT_STEP=""
CURRENT_STEP_NAME=""

# Colors (disabled if non-tty)
if [ -t 1 ]; then
  C_RESET="\033[0m"; C_RED="\033[0;31m"; C_GRN="\033[0;32m"
  C_YEL="\033[1;33m"; C_BLU="\033[0;34m"; C_BOLD="\033[1m"
else
  C_RESET=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BOLD=""
fi

log()  { printf "%b[install]%b %s\n" "$C_GRN"  "$C_RESET" "$*"; }
warn() { printf "%b[install]%b %s\n" "$C_YEL"  "$C_RESET" "$*" >&2; }
err()  { printf "%b[install]%b %s\n" "$C_RED"  "$C_RESET" "$*" >&2; }
info() { printf "%b[install]%b %s\n" "$C_BLU"  "$C_RESET" "$*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --with-admin)    WITH_ADMIN=1 ;;
    --skip-vsix)     SKIP_VSIX=1 ;;
    --skip-firewall) SKIP_FIREWALL=1 ;;
    --with-firewall) SKIP_FIREWALL=0 ;;
    --dry-run)       DRY_RUN=1 ;;
    --yes|-y)        ASSUME_YES=1 ;;
    --force)         FORCE=1 ;;
    -h|--help)
      sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) err "unknown flag: $1"; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------
run() {
  # run <description> -- <cmd...>
  local desc="$1"; shift
  if [ "$DRY_RUN" -eq 1 ]; then
    printf "  %b[dry]%b %s\n" "$C_BLU" "$C_RESET" "$desc"
    printf "        $ %s\n" "$*"
    return 0
  fi
  info "$desc"
  printf "        $ %s\n" "$*" >> "$LOG_FILE" 2>/dev/null || true
  "$@"
}

mark_done() {
  local step="$1"
  [ "$DRY_RUN" -eq 1 ] && return 0
  mkdir -p "$STATE_DIR"
  : > "$STATE_DIR/${step}.done"
}

is_done() {
  local step="$1"
  [ "$FORCE" -eq 1 ] && return 1
  [ -f "$STATE_DIR/${step}.done" ]
}

step_start() {
  CURRENT_STEP="$1"
  CURRENT_STEP_NAME="$2"
  printf "\n%b== Step %s: %s ==%b\n" "$C_BOLD" "$1" "$2" "$C_RESET"
}

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ -n "${CURRENT_STEP:-}" ]; then
    err "FAILED at Step $CURRENT_STEP: $CURRENT_STEP_NAME (exit=$rc)"
    err "Log: $LOG_FILE"
    err "Recover: re-run \`./scripts/install.sh\` (idempotent), or pass --force to redo all steps."
    err "If a single step is wedged, delete its marker: rm $STATE_DIR/step-$CURRENT_STEP.done"
  fi
  exit "$rc"
}
trap on_exit EXIT INT TERM

# ---------------------------------------------------------------------------
# Step 0: platform + prereq detection
# ---------------------------------------------------------------------------
step_start 0 "platform detection + prereq check"

UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
case "$UNAME_S" in
  Darwin)               PLATFORM="macOS";   PKG_HINT="brew install" ;;
  Linux)
    if grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
      PLATFORM="WSL2";  PKG_HINT="apt install -y"
    else
      PLATFORM="Linux"; PKG_HINT="apt install -y"
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    err "Windows native is NOT supported. Install WSL2 (Ubuntu) and re-run inside it:"
    err "  https://learn.microsoft.com/windows/wsl/install"
    exit 1
    ;;
  *) PLATFORM="$UNAME_S"; PKG_HINT="(install manually)"; warn "unrecognized platform: $UNAME_S — proceeding best-effort" ;;
esac

log "platform: $PLATFORM"

mkdir -p "$STATE_DIR"
[ "$DRY_RUN" -eq 1 ] || : > "$LOG_FILE"

MISSING=()
need_python_310() {
  command -v python3 >/dev/null 2>&1 || return 1
  local ver
  ver="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo 0.0)"
  case "$ver" in
    3.1[0-9]|3.[2-9][0-9]) return 0 ;;
    *) return 1 ;;
  esac
}
need_node_20() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [ "$major" -ge 20 ] 2>/dev/null
}

need_python_310 || MISSING+=("python3.10+")
need_node_20    || MISSING+=("node20+")
command -v npm >/dev/null 2>&1 || MISSING+=("npm")
command -v nats-server >/dev/null 2>&1 || MISSING+=("nats-server")

if [ "${#MISSING[@]}" -gt 0 ]; then
  err "missing prerequisites: ${MISSING[*]}"
  echo
  case "$PLATFORM" in
    macOS)
      echo "  Install via Homebrew:"
      echo "    brew install python@3.11 node@20 nats-server"
      ;;
    Linux|WSL2)
      echo "  Install via apt (or your distro equivalent):"
      echo "    sudo apt update"
      echo "    sudo apt install -y python3.11 python3.11-venv python3-pip"
      echo "    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
      echo "    sudo apt install -y nodejs"
      echo "    # NATS: https://github.com/nats-io/nats-server/releases"
      ;;
    *)
      echo "  $PKG_HINT python@3.11 node@20 nats-server"
      ;;
  esac
  echo
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "dry-run: continuing despite missing prereqs"
  else
    exit 1
  fi
fi

mark_done step-0

# ---------------------------------------------------------------------------
# Step 1: Python venv + pip install -e multi-fleet[.full]
# ---------------------------------------------------------------------------
step_start 1 "Python venv at .venv/ + multi-fleet editable install"

VENV_DIR="$REPO_ROOT/.venv"
# Resolve dangling symlinks: .venv -> venv.nosync may exist without target.
if [ -L "$VENV_DIR" ] && [ ! -e "$VENV_DIR" ]; then
  warn ".venv is a dangling symlink — removing"
  run "remove dangling .venv symlink" rm -f "$VENV_DIR"
fi

if is_done step-1 && [ -x "$VENV_DIR/bin/pip" ]; then
  log "step-1 already done (marker + pip exists) — skipping"
else
  if [ ! -x "$VENV_DIR/bin/pip" ]; then
    run "create venv" python3 -m venv "$VENV_DIR"
  else
    log "venv already exists — reusing"
  fi
  run "upgrade pip" "$VENV_DIR/bin/pip" install --upgrade pip
  run "install multi-fleet[full] editable" \
      "$VENV_DIR/bin/pip" install -e "$REPO_ROOT/multi-fleet/.[full]"
  mark_done step-1
fi

# ---------------------------------------------------------------------------
# Step 2: multi-fleet daemon installer
# ---------------------------------------------------------------------------
step_start 2 "multi-fleet/scripts/install.sh (fleet daemon)"

SUB_INSTALL="$REPO_ROOT/multi-fleet/scripts/install.sh"
if [ ! -x "$SUB_INSTALL" ]; then
  err "missing $SUB_INSTALL — repo state inconsistent. Did you clone with submodules?"
  exit 1
fi

if is_done step-2; then
  log "step-2 already done — skipping (use --force to redo)"
else
  SUB_FLAGS=()
  [ "$SKIP_FIREWALL" -eq 1 ] && SUB_FLAGS+=(--skip-firewall)
  [ "$ASSUME_YES" -eq 1 ]    && SUB_FLAGS+=(--yes)
  [ "$DRY_RUN" -eq 1 ]       && SUB_FLAGS+=(--dry-run)
  # Always skip TLS + e2e at IDE-install time: those belong to a separate
  # bash multi-fleet/scripts/install.sh invocation by an operator who
  # explicitly wants fleet hardening.
  SUB_FLAGS+=(--skip-tls --skip-e2e)
  run "run sub-installer" bash "$SUB_INSTALL" "${SUB_FLAGS[@]}"
  mark_done step-2
fi

# ---------------------------------------------------------------------------
# Step 3: VS Code extension build
# ---------------------------------------------------------------------------
step_start 3 "context-dna/clients/vscode: npm install + compile"

VSCODE_DIR="$REPO_ROOT/context-dna/clients/vscode"
if [ ! -d "$VSCODE_DIR" ]; then
  err "missing $VSCODE_DIR — VS Code extension source not present"
  exit 1
fi

if is_done step-3 && [ -d "$VSCODE_DIR/out" ]; then
  log "step-3 already done (marker + out/ exists) — skipping"
else
  run "npm install"   bash -c "cd '$VSCODE_DIR' && npm install --no-audit --no-fund"
  run "npm run compile" bash -c "cd '$VSCODE_DIR' && npm run compile"
  mark_done step-3
fi

# ---------------------------------------------------------------------------
# Step 4: Package VSIX
# ---------------------------------------------------------------------------
step_start 4 "context-dna/clients/vscode: vsce package (.vsix)"

if [ "$SKIP_VSIX" -eq 1 ]; then
  log "--skip-vsix set — skipping packaging"
elif is_done step-4 && ls "$VSCODE_DIR"/context-dna-vscode-*.vsix >/dev/null 2>&1; then
  log "step-4 already done (vsix present) — skipping"
else
  run "vsce package" bash -c "cd '$VSCODE_DIR' && npx --yes @vscode/vsce package --out context-dna-vscode-0.2.0.vsix"
  mark_done step-4
fi

# ---------------------------------------------------------------------------
# Step 5: admin.contextdna.io (optional)
# ---------------------------------------------------------------------------
step_start 5 "admin.contextdna.io build (optional, --with-admin)"

if [ "$WITH_ADMIN" -eq 0 ]; then
  log "skipping admin build (pass --with-admin to enable)"
elif is_done step-5; then
  log "step-5 already done — skipping"
else
  ADMIN_DIR="$REPO_ROOT/admin.contextdna.io"
  if [ ! -d "$ADMIN_DIR" ]; then
    err "--with-admin set but $ADMIN_DIR missing"
    exit 1
  fi
  run "admin npm install"   bash -c "cd '$ADMIN_DIR' && npm install --no-audit --no-fund"
  run "admin npm run build" bash -c "cd '$ADMIN_DIR' && npm run build"
  mark_done step-5
fi

# ---------------------------------------------------------------------------
# Step 6: success summary + next steps
# ---------------------------------------------------------------------------
step_start 6 "summary + next steps"

CURRENT_STEP=""  # disable failure trap from here on

# Locate the freshly-built VSIX (best-effort)
VSIX_PATH=""
if [ "$DRY_RUN" -eq 0 ]; then
  VSIX_PATH="$(ls -t "$VSCODE_DIR"/context-dna-vscode-*.vsix 2>/dev/null | head -1 || true)"
fi

printf "\n%b%bContext DNA IDE install complete.%b\n\n" "$C_BOLD" "$C_GRN" "$C_RESET"
echo   "Next steps:"
echo   "  1. Install the VS Code extension:"
if [ -n "$VSIX_PATH" ]; then
  echo "       code --install-extension '$VSIX_PATH'"
else
  echo "       code --install-extension '$VSCODE_DIR/context-dna-vscode-0.2.0.vsix'"
fi
echo   "  2. Run the live demo (boots NATS + fleet daemon, opens the dashboard):"
echo   "       ./scripts/demo.sh"
echo   "  3. Open the dashboard directly:"
echo   "       http://localhost:8855/dashboard"
echo   "  4. Verify everything is healthy:"
echo   "       ./scripts/demo.sh --dry-run    # scenario timeline only, no infra"
echo   "       curl -sf http://127.0.0.1:8855/health"
echo
printf  "Re-run safety: %b./scripts/install.sh%b is idempotent. Pass --force to redo all steps.\n" "$C_BOLD" "$C_RESET"
echo   "Logs: $LOG_FILE"

mark_done step-6
exit 0
