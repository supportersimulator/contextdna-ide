#!/usr/bin/env bash
# ============================================================================
# sprint-aaron-actions.sh — Cycle 6 (F1) one-shot Aaron-actions installer.
#
# Performs the 8 accumulated Aaron-actions from cycles 3–5 of the autonomous
# 10h sprint. Idempotent: re-running is safe; each step detects "already done"
# and skips work. Zero silent failures: every action reports OK / SKIP / FAIL /
# MANUAL with colored output and a one-line reason.
#
# Usage:
#   bash scripts/sprint-aaron-actions.sh --dry-run
#   bash scripts/sprint-aaron-actions.sh --apply
#   bash scripts/sprint-aaron-actions.sh --apply --skip 1 --skip 4
#   bash scripts/sprint-aaron-actions.sh --help
#
# Companion doc: scripts/sprint-aaron-actions.md (per-action revert commands).
#
# 8 + Cycle 8 H5 actions:
#   1. Install mlx_lm into context-dna/local_llm/.venv-mlx
#   2. Bootstrap launchd LLM plist (delegates to install-launchd-plists.sh llm)
#   3. Add BRIDGE_OAUTH_PASSTHROUGH=1 to fleet-nats plist + reload daemon
#   4. (Prompted) Add THREE_SURGEONS_VIA_BRIDGE=1 to ~/.zshrc
#   5. Restart fleet daemon (pickup D2 ratelimit + D5 sub-watchdog counters)
#   6. Install IDE VSIX (context-dna-vscode-0.2.0.vsix)
#   7. Push admin.contextdna.io 5 local commits (interactive — prints commands)
#   8. Wire validateERSimInvariants.cjs into scripts/gains-gate.sh
#   9. Webhook agent_service (:8080)        — probe + print start command
#  10. MLX LLM (:5044)                      — probe + (with --no-prompt) start
#  11. Synaptic doc index (:8888)           — probe + print start command
#  12. Scheduler (memory/.scheduler_coordinator.pid) — probe + print start command
#  13. Audit-only: redis pip install (Cycle 8 H5 — no Aaron action required)
#
# Bonus: scrub `ANTHROPIC_AUTH_TOKEN=dummy` from ~/.zshrc / ~/.bash_profile
# (commented out, never deleted; marker line indicates date of removal).
# ============================================================================
set -uo pipefail

# ── Constants ────────────────────────────────────────────────────────────────
REPO_DIR="${SPRINT_REPO_DIR:-/Users/aarontjomsland/dev/er-simulator-superrepo}"
LOCAL_LLM_DIR="$REPO_DIR/context-dna/local_llm"
MLX_VENV="$LOCAL_LLM_DIR/.venv-mlx"
FLEET_PLIST="$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist"
# VSIX version auto-discovered from latest .vsix on disk (highest version wins).
# Falls back to 0.2.0 if no .vsix exists yet (initial bootstrap).
VSIX_DIR="$REPO_DIR/context-dna/clients/vscode"
VSIX_PATH="$(ls -1 "$VSIX_DIR"/context-dna-vscode-*.vsix 2>/dev/null | sort -V | tail -n 1)"
if [[ -n "$VSIX_PATH" && -f "$VSIX_PATH" ]]; then
    VSIX_VERSION="$(basename "$VSIX_PATH" | sed -E 's/^context-dna-vscode-(.+)\.vsix$/\1/')"
else
    VSIX_PATH="$VSIX_DIR/context-dna-vscode-0.2.0.vsix"
    VSIX_VERSION="0.2.0"
fi
VSIX_ID="context-dna.context-dna-vscode"
ADMIN_DIR="$REPO_DIR/admin.contextdna.io"
GAINS_GATE="$REPO_DIR/scripts/gains-gate.sh"
VALIDATE_INVARIANTS="$REPO_DIR/simulator-core/er-sim-monitor/scripts/validateERSimInvariants.cjs"
SHELL_FILES=("$HOME/.zshrc" "$HOME/.bash_profile")
TODAY="$(date +%Y-%m-%d)"

# Cycle 8 H5 daemon probes
WEBHOOK_PORT=8080
MLX_PORT=5044
SYNAPTIC_PORT=8888
SCHEDULER_PID_FILE="$REPO_DIR/memory/.scheduler_coordinator.pid"
HELPER_AGENT_SCRIPT="$REPO_DIR/scripts/start-helper-agent.sh"
START_LLM_SCRIPT="$REPO_DIR/scripts/start-llm.sh"
ATLAS_OPS_SCRIPT="$REPO_DIR/scripts/atlas-ops.sh"
VENV_DIR="$REPO_DIR/.venv"

# Colors (NO_COLOR=1 disables)
if [[ -n "${NO_COLOR:-}" ]]; then
    RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; NC=""
else
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
fi

# ── Args ─────────────────────────────────────────────────────────────────────
MODE=""
SKIPS=()
PROMPT_OK=1

usage() {
    cat <<EOF
sprint-aaron-actions.sh — Cycle 6 (F1) Aaron-actions one-shot installer.

Usage:
  $(basename "$0") --dry-run               Preview every action (no changes).
  $(basename "$0") --apply                 Execute every action (idempotent).
  $(basename "$0") --apply --skip N        Skip step N (repeatable).
  $(basename "$0") --apply --no-prompt     Skip interactive prompt for step 4.

Steps: 1=mlx_lm  2=launchd_llm  3=bridge_oauth  4=zshrc_3s  5=daemon_restart
       6=vsix    7=admin_push   8=invariants_wire
       9=webhook 10=mlx_llm    11=synaptic     12=scheduler  13=redis_audit

Companion doc: scripts/sprint-aaron-actions.md
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) MODE="dry-run"; shift ;;
        --apply)   MODE="apply"; shift ;;
        --skip)    SKIPS+=("$2"); shift 2 ;;
        --no-prompt) PROMPT_OK=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown flag: $1" >&2; usage; exit 2 ;;
    esac
done

[[ -z "$MODE" ]] && { usage; exit 2; }

is_skipped() {
    local n="$1"
    for s in "${SKIPS[@]:-}"; do
        [[ "$s" == "$n" ]] && return 0
    done
    return 1
}

# ── Reporting ────────────────────────────────────────────────────────────────
declare -a SUMMARY=()
PASS=0; FAIL=0; SKIP=0; MANUAL=0

report() {
    local n="$1" status="$2" name="$3" detail="${4:-}"
    case "$status" in
        OK)     SUMMARY+=("${GREEN}[${n}] OK${NC}      ${name}${detail:+ — $detail}"); PASS=$((PASS+1)) ;;
        SKIP)   SUMMARY+=("${CYAN}[${n}] SKIP${NC}    ${name}${detail:+ — $detail}"); SKIP=$((SKIP+1)) ;;
        FAIL)   SUMMARY+=("${RED}[${n}] FAIL${NC}    ${name}${detail:+ — $detail}"); FAIL=$((FAIL+1)) ;;
        MANUAL) SUMMARY+=("${YELLOW}[${n}] MANUAL${NC}  ${name}${detail:+ — $detail}"); MANUAL=$((MANUAL+1)) ;;
        DRY)    SUMMARY+=("${YELLOW}[${n}] DRY${NC}     ${name}${detail:+ — $detail}") ;;
    esac
}

say() { echo -e "${CYAN}>>>${NC} $*"; }

# ── Action 1: Install mlx_lm into local_llm/.venv-mlx ────────────────────────
action_mlx_lm() {
    local n=1 name="Install mlx_lm into $MLX_VENV"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ -d "$MLX_VENV" ]] && "$MLX_VENV/bin/python3" -c "import mlx_lm" 2>/dev/null; then
        report "$n" OK "$name" "venv exists, mlx_lm importable"
        return
    fi

    # mlx requires Apple Silicon (arm64). Intel Macs cannot install mlx — skip
    # gracefully with MANUAL pointing to mac3 (M1 Max). Verified 2026-05-04
    # on mac2 (Intel Core i7-1060NG7): mlx>=0.31.2 wheel resolution impossible.
    local arch
    arch="$(uname -m 2>/dev/null || echo unknown)"
    if [[ "$arch" != "arm64" ]]; then
        report "$n" MANUAL "$name" "Intel Mac ($arch) — mlx requires Apple Silicon; run on mac3 instead"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would: python3 -m venv $MLX_VENV && pip install mlx_lm"
        return
    fi

    if [[ ! -d "$LOCAL_LLM_DIR" ]]; then
        report "$n" FAIL "$name" "$LOCAL_LLM_DIR missing"
        return
    fi

    say "[$n] Creating venv at $MLX_VENV"
    if ! python3 -m venv "$MLX_VENV" 2>&1; then
        report "$n" FAIL "$name" "python3 -m venv failed"
        return
    fi

    say "[$n] Installing mlx_lm (~80MB, may take 30–60s)"
    if "$MLX_VENV/bin/pip" install --quiet --upgrade pip mlx_lm 2>&1; then
        if "$MLX_VENV/bin/python3" -c "import mlx_lm" 2>/dev/null; then
            report "$n" OK "$name" "installed, importable"
        else
            report "$n" FAIL "$name" "installed but import fails"
        fi
    else
        report "$n" FAIL "$name" "pip install mlx_lm failed (Apple Silicon required)"
    fi
}

# ── Action 2: Bootstrap launchd LLM plist ────────────────────────────────────
action_launchd_llm() {
    local n=2 name="Bootstrap launchd LLM plist"
    local installer="$REPO_DIR/scripts/install-launchd-plists.sh"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ ! -x "$installer" ]] && [[ ! -f "$installer" ]]; then
        report "$n" FAIL "$name" "installer not found: $installer"
        return
    fi

    # Idempotency: check if any *llm* plist is already loaded
    if launchctl list 2>/dev/null | grep -qiE 'contextdna.*llm|llm.*contextdna'; then
        if [[ "$MODE" == "dry-run" ]]; then
            report "$n" DRY "$name" "would re-run installer (already loaded — bash $installer llm)"
            return
        fi
        # Re-running is idempotent in install-launchd-plists.sh (unload→load)
        say "[$n] LLM plist already loaded — re-running installer for idempotency"
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would: bash $installer llm"
        return
    fi

    if bash "$installer" llm 2>&1; then
        report "$n" OK "$name" "installer ran"
    else
        report "$n" FAIL "$name" "installer exited non-zero"
    fi
}

# ── Action 3: Add BRIDGE_OAUTH_PASSTHROUGH=1 to fleet-nats plist + reload ────
action_bridge_oauth() {
    local n=3 name="Add BRIDGE_OAUTH_PASSTHROUGH=1 to fleet-nats plist"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ ! -f "$FLEET_PLIST" ]]; then
        report "$n" FAIL "$name" "plist missing: $FLEET_PLIST"
        return
    fi

    # Idempotency check via plistlib
    local already
    already=$(python3 - "$FLEET_PLIST" <<'PY'
import plistlib, sys
with open(sys.argv[1], "rb") as f:
    plist = plistlib.load(f)
env = plist.get("EnvironmentVariables", {}) or {}
print("YES" if env.get("BRIDGE_OAUTH_PASSTHROUGH") == "1" else "NO")
PY
)

    if [[ "$already" == "YES" ]]; then
        report "$n" OK "$name" "key already present"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would set EnvironmentVariables.BRIDGE_OAUTH_PASSTHROUGH=1 + bootout/bootstrap"
        return
    fi

    # Backup before mutation
    local backup="${FLEET_PLIST}.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$FLEET_PLIST" "$backup" || { report "$n" FAIL "$name" "backup failed"; return; }
    say "[$n] Backup: $backup"

    # Mutate via plistlib (preserves XML format)
    if ! python3 - "$FLEET_PLIST" <<'PY'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as f:
    plist = plistlib.load(f)
env = plist.setdefault("EnvironmentVariables", {})
env["BRIDGE_OAUTH_PASSTHROUGH"] = "1"
with open(path, "wb") as f:
    plistlib.dump(plist, f)
print("ok")
PY
    then
        report "$n" FAIL "$name" "plistlib write failed (backup at $backup)"
        return
    fi

    # Reload daemon to pick up env var
    local uid; uid="$(id -u)"
    say "[$n] Reloading daemon (bootout + bootstrap)"
    launchctl bootout "gui/$uid/io.contextdna.fleet-nats" 2>/dev/null || true
    sleep 1
    if launchctl bootstrap "gui/$uid" "$FLEET_PLIST" 2>&1; then
        report "$n" OK "$name" "key set + daemon reloaded"
    else
        report "$n" FAIL "$name" "bootstrap failed (key set, daemon may need manual restart)"
    fi
}

# ── Action 4: (Prompted) Add THREE_SURGEONS_VIA_BRIDGE=1 to ~/.zshrc ─────────
action_zshrc_3s() {
    local n=4 name="Add THREE_SURGEONS_VIA_BRIDGE=1 to ~/.zshrc"
    local zshrc="$HOME/.zshrc"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ ! -f "$zshrc" ]]; then
        report "$n" SKIP "$name" "~/.zshrc missing"
        return
    fi

    if grep -qE '^[[:space:]]*export[[:space:]]+THREE_SURGEONS_VIA_BRIDGE=1' "$zshrc"; then
        report "$n" OK "$name" "already exported in ~/.zshrc"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would prompt then append: export THREE_SURGEONS_VIA_BRIDGE=1"
        return
    fi

    if [[ "$PROMPT_OK" -eq 0 ]]; then
        report "$n" MANUAL "$name" "skipped via --no-prompt; add manually: echo 'export THREE_SURGEONS_VIA_BRIDGE=1' >> ~/.zshrc"
        return
    fi

    # Interactive prompt (safe: only proceeds on explicit y)
    echo ""
    echo -e "${YELLOW}[4] Optional:${NC} append 'export THREE_SURGEONS_VIA_BRIDGE=1' to ~/.zshrc?"
    echo -n "    [y/N] "
    local ans=""
    if [[ -t 0 ]]; then
        read -r ans
    else
        ans="n"
    fi

    if [[ "$ans" =~ ^[Yy]$ ]]; then
        printf '\n# Added by sprint-aaron-actions on %s (Cycle 6 F1)\nexport THREE_SURGEONS_VIA_BRIDGE=1\n' "$TODAY" >> "$zshrc"
        report "$n" OK "$name" "appended to ~/.zshrc (re-source: source ~/.zshrc)"
    else
        report "$n" MANUAL "$name" "user declined; add manually if desired"
    fi
}

# ── Action 5: Restart fleet daemon (pickup D2 ratelimit + D5 sub-watchdog) ──
action_daemon_restart() {
    local n=5 name="Restart fleet daemon (D2 ratelimit + D5 sub-watchdog pickup)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ ! -f "$FLEET_PLIST" ]]; then
        report "$n" FAIL "$name" "plist missing: $FLEET_PLIST"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would: pkill -f fleet_nerve_nats (KeepAlive auto-respawns)"
        return
    fi

    # If action 3 already bootstrapped, the daemon is fresh — no-op.
    # We pkill regardless; KeepAlive=true respawns within seconds.
    say "[$n] pkill -f fleet_nerve_nats (KeepAlive will respawn)"
    pkill -f fleet_nerve_nats 2>/dev/null || true
    sleep 2

    # Verify it came back via /health
    local ok=0
    for _ in 1 2 3 4 5; do
        if curl -sf --max-time 2 http://127.0.0.1:8855/health >/dev/null 2>&1; then
            ok=1; break
        fi
        sleep 1
    done

    if [[ "$ok" -eq 1 ]]; then
        report "$n" OK "$name" "daemon respawned, /health 200"
    else
        # Try explicit bootstrap as fallback
        local uid; uid="$(id -u)"
        launchctl bootstrap "gui/$uid" "$FLEET_PLIST" 2>/dev/null || true
        sleep 2
        if curl -sf --max-time 2 http://127.0.0.1:8855/health >/dev/null 2>&1; then
            report "$n" OK "$name" "respawned via bootstrap fallback"
        else
            report "$n" FAIL "$name" "daemon did not respawn within 5s"
        fi
    fi
}

# ── Action 6: Install IDE VSIX ───────────────────────────────────────────────
action_vsix() {
    local n=6 name="Install context-dna-vscode VSIX (v$VSIX_VERSION)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ ! -f "$VSIX_PATH" ]]; then
        report "$n" FAIL "$name" "VSIX not found: $VSIX_PATH"
        return
    fi

    if ! command -v code >/dev/null 2>&1; then
        report "$n" MANUAL "$name" "'code' CLI not on PATH; install from VSCode Cmd+Shift+P → 'Shell Command: Install code command in PATH'"
        return
    fi

    # Idempotency: check installed version
    local installed
    installed="$(code --list-extensions --show-versions 2>/dev/null | grep "^${VSIX_ID}@" || true)"
    if [[ "$installed" == "${VSIX_ID}@${VSIX_VERSION}" ]]; then
        report "$n" OK "$name" "already installed: $installed"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would: code --install-extension $VSIX_PATH (currently: ${installed:-none})"
        return
    fi

    if code --install-extension "$VSIX_PATH" --force 2>&1 | tail -5; then
        report "$n" OK "$name" "installed v$VSIX_VERSION"
    else
        report "$n" FAIL "$name" "code --install-extension failed"
    fi
}

# ── Action 7: Push admin.contextdna.io 5 commits (interactive — print only) ─
action_admin_push() {
    local n=7 name="Push admin.contextdna.io commits to origin"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    # admin.contextdna.io is a git submodule — .git can be either a dir or
    # a gitlink file. Accept both.
    if [[ ! -e "$ADMIN_DIR/.git" ]]; then
        report "$n" SKIP "$name" "$ADMIN_DIR not a git repo (no .git)"
        return
    fi

    local ahead
    ahead=$(cd "$ADMIN_DIR" && git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")

    if [[ "$ahead" == "0" ]]; then
        report "$n" OK "$name" "already up to date with origin/main"
        return
    fi

    # Auth-gated push: print, don't execute (per spec)
    report "$n" MANUAL "$name" "$ahead commit(s) ahead — run interactively (see commands below)"
    echo ""
    echo -e "  ${YELLOW}Manual step 7${NC}: push admin.contextdna.io ($ahead commits ahead)"
    echo -e "  ${BOLD}Run interactively (HTTPS auth required):${NC}"
    echo "      cd $ADMIN_DIR"
    echo "      git push origin main"
    echo "  Optional preview first:"
    echo "      cd $ADMIN_DIR && git log origin/main..HEAD --oneline"
    echo ""
}

# ── Action 8: Wire validateERSimInvariants.cjs into gains-gate.sh ────────────
action_invariants_wire() {
    local n=8 name="Wire validateERSimInvariants.cjs into gains-gate.sh"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    if [[ ! -f "$GAINS_GATE" ]]; then
        report "$n" FAIL "$name" "gains-gate.sh missing"
        return
    fi
    if [[ ! -f "$VALIDATE_INVARIANTS" ]]; then
        report "$n" FAIL "$name" "validateERSimInvariants.cjs missing: $VALIDATE_INVARIANTS"
        return
    fi

    # Idempotency: marker comment
    if grep -q "validateERSimInvariants.cjs" "$GAINS_GATE"; then
        report "$n" OK "$name" "already wired in gains-gate.sh"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would insert ER-sim invariants check before '── Results ──' section"
        return
    fi

    # Backup
    local backup="${GAINS_GATE}.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$GAINS_GATE" "$backup" || { report "$n" FAIL "$name" "backup failed"; return; }

    # Build the insertion block to a temp file
    local tmpblock; tmpblock="$(mktemp)"
    cat > "$tmpblock" <<EOF

# 17. ER-sim invariants — validateERSimInvariants.cjs
# Wired by sprint-aaron-actions.sh on $TODAY (Cycle 6 F1).
# Why warning: ER-sim has its own CI; gains-gate signals drift, doesn't block.
ER_INV="\$REPO_DIR/simulator-core/er-sim-monitor/scripts/validateERSimInvariants.cjs"
if [[ -f "\$ER_INV" ]] && command -v node >/dev/null 2>&1; then
    if node "\$ER_INV" >/dev/null 2>&1; then
        check "ER-sim invariants" "warning" 0 "validateERSimInvariants.cjs OK"
    else
        check "ER-sim invariants" "warning" 1 "validateERSimInvariants.cjs failed (run: node \$ER_INV)"
    fi
else
    check "ER-sim invariants" "warning" 0 "skipped (script or node missing)"
fi

EOF

    # Insert before "── Results ──" line. Use python for reliable insertion.
    if ! python3 - "$GAINS_GATE" "$tmpblock" <<'PY'
import sys, pathlib
gate_path = pathlib.Path(sys.argv[1])
block_path = pathlib.Path(sys.argv[2])
gate = gate_path.read_text()
block = block_path.read_text()
marker = "# ── Results ──"
idx = gate.find(marker)
if idx == -1:
    print("ERROR: marker '── Results ──' not found", file=sys.stderr)
    sys.exit(1)
new = gate[:idx] + block + gate[idx:]
gate_path.write_text(new)
print("ok")
PY
    then
        rm -f "$tmpblock"
        report "$n" FAIL "$name" "insertion failed (backup at $backup)"
        return
    fi

    rm -f "$tmpblock"
    report "$n" OK "$name" "inserted; backup at $backup"
}

# ── Bonus: scrub ANTHROPIC_AUTH_TOKEN=dummy ──────────────────────────────────
action_scrub_anthropic() {
    local name="Scrub ANTHROPIC_AUTH_TOKEN=dummy"
    local found=0 changed=0
    local matches=()

    for f in "${SHELL_FILES[@]}"; do
        [[ -f "$f" ]] || continue
        # Match assignments (export X=dummy or X=dummy), already-commented lines ignored
        if grep -nE '^[[:space:]]*(export[[:space:]]+)?ANTHROPIC_AUTH_TOKEN=dummy' "$f" >/dev/null 2>&1; then
            found=1
            matches+=("$f")
        fi
    done

    if [[ "$found" -eq 0 ]]; then
        report "B" OK "$name" "no live assignment found in shell rc files"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "B" DRY "$name" "would comment out in: ${matches[*]}"
        return
    fi

    for f in "${matches[@]}"; do
        local backup="${f}.bak.$(date +%Y%m%d-%H%M%S)"
        cp "$f" "$backup" || { report "B" FAIL "$name" "backup failed for $f"; return; }
        # Comment out + leave marker. Use python for safety (sed -i quirks on macOS).
        python3 - "$f" "$TODAY" <<'PY' || { echo "scrub-python-failed" >&2; exit 1; }
import sys, re, pathlib
path = pathlib.Path(sys.argv[1])
today = sys.argv[2]
text = path.read_text()
pat = re.compile(r'^([\t ]*)((?:export[\t ]+)?ANTHROPIC_AUTH_TOKEN=dummy.*)$', re.MULTILINE)
marker = f"# Removed by sprint-aaron-actions on {today} (Cycle 6 F1)"
def sub(m):
    return f"{m.group(1)}{marker}\n{m.group(1)}# {m.group(2)}"
new = pat.sub(sub, text)
path.write_text(new)
PY
        changed=$((changed+1))
    done
    report "B" OK "$name" "commented out in ${changed} file(s); marker line added"
}

# ── Action 9: Webhook agent_service (:8080) — probe + manual hint ────────────
action_webhook() {
    local n=9 name="Webhook agent_service (:$WEBHOOK_PORT)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    # Idempotency: probe via /health
    if curl -sf --max-time 2 "http://127.0.0.1:$WEBHOOK_PORT/health" >/dev/null 2>&1; then
        report "$n" OK "$name" "already responding on :$WEBHOOK_PORT/health"
        return
    fi

    # Secondary probe: lsof (covers daemons that bind without /health)
    if lsof -iTCP:$WEBHOOK_PORT -sTCP:LISTEN -n -P >/dev/null 2>&1; then
        report "$n" OK "$name" ":$WEBHOOK_PORT bound (no /health 200 — see process list)"
        return
    fi

    if [[ ! -f "$HELPER_AGENT_SCRIPT" ]]; then
        report "$n" FAIL "$name" "start-helper-agent.sh missing: $HELPER_AGENT_SCRIPT"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would print: bash scripts/start-helper-agent.sh"
        return
    fi

    # Per spec: do NOT auto-start daemons without --no-prompt — Aaron consent first.
    # Print the canonical start command; Aaron can run it manually.
    report "$n" MANUAL "$name" "down; start with: bash $HELPER_AGENT_SCRIPT"
    echo ""
    echo -e "  ${YELLOW}Manual step 9${NC}: webhook is the #1 priority (see CLAUDE.md)"
    echo -e "  Start it with: ${BOLD}bash scripts/start-helper-agent.sh${NC}"
    echo "  Verify with:   curl -sf http://127.0.0.1:$WEBHOOK_PORT/health | jq ."
    echo ""
}

# ── Action 10: MLX LLM (:5044) — probe + (--no-prompt) start ────────────────
action_mlx_llm() {
    local n=10 name="MLX LLM (:$MLX_PORT)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    # Idempotency: /v1/models is the OpenAI-compatible probe
    if curl -sf --max-time 2 "http://127.0.0.1:$MLX_PORT/v1/models" >/dev/null 2>&1; then
        report "$n" OK "$name" "already responding on :$MLX_PORT/v1/models"
        return
    fi

    if lsof -iTCP:$MLX_PORT -sTCP:LISTEN -n -P >/dev/null 2>&1; then
        report "$n" OK "$name" ":$MLX_PORT bound (no /v1/models 200 — see process list)"
        return
    fi

    # mlx requires Apple Silicon — Intel Macs cannot run MLX. Skip gracefully.
    local arch
    arch="$(uname -m 2>/dev/null || echo unknown)"
    if [[ "$arch" != "arm64" ]]; then
        report "$n" MANUAL "$name" "Intel Mac ($arch) — mlx requires Apple Silicon; run on mac3 instead"
        return
    fi

    if [[ ! -f "$START_LLM_SCRIPT" ]]; then
        report "$n" FAIL "$name" "start-llm.sh missing: $START_LLM_SCRIPT"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would: bash scripts/start-llm.sh (claims llm:gpu_lock in Redis)"
        return
    fi

    # Apply mode: only auto-start with --no-prompt (Aaron consent rule)
    if [[ "$PROMPT_OK" -eq 0 ]]; then
        say "[$n] Starting MLX via $START_LLM_SCRIPT (--no-prompt set)"
        # nohup so we don't block; redirect to log so we can diagnose silently-failed starts (ZSF)
        local logf="/tmp/start-llm.sprint-$(date +%Y%m%d-%H%M%S).log"
        nohup bash "$START_LLM_SCRIPT" >"$logf" 2>&1 &
        local pid=$!
        # Poll up to 60s for /v1/models
        local ok=0
        for _ in $(seq 1 30); do
            if curl -sf --max-time 2 "http://127.0.0.1:$MLX_PORT/v1/models" >/dev/null 2>&1; then
                ok=1; break
            fi
            sleep 2
        done
        if [[ "$ok" -eq 1 ]]; then
            report "$n" OK "$name" "started, /v1/models 200 (pid $pid, log $logf)"
        else
            report "$n" FAIL "$name" "start did not respond within 60s — see $logf"
        fi
        return
    fi

    report "$n" MANUAL "$name" "down; start with: bash $START_LLM_SCRIPT (or pass --no-prompt to auto-start)"
    echo ""
    echo -e "  ${YELLOW}Manual step 10${NC}: MLX off → P1/P2 hot-path falls through to remote DeepSeek"
    echo -e "  Start it with: ${BOLD}bash scripts/start-llm.sh${NC}"
    echo "  Verify with:   curl -sf http://127.0.0.1:$MLX_PORT/v1/models | jq ."
    echo "  Verify GPU lock free first: redis-cli get llm:gpu_lock"
    echo ""
}

# ── Action 11: Synaptic doc index (:8888) — probe + manual hint ─────────────
action_synaptic() {
    local n=11 name="Synaptic doc index (:$SYNAPTIC_PORT)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    # Idempotency: /markdown/health is the canonical probe (verified in synaptic_chat_server.py:6481)
    if curl -sf --max-time 2 "http://127.0.0.1:$SYNAPTIC_PORT/markdown/health" >/dev/null 2>&1; then
        report "$n" OK "$name" "already responding on :$SYNAPTIC_PORT/markdown/health"
        return
    fi

    if lsof -iTCP:$SYNAPTIC_PORT -sTCP:LISTEN -n -P >/dev/null 2>&1; then
        report "$n" OK "$name" ":$SYNAPTIC_PORT bound (no /markdown/health 200 — see process list)"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would print: ./scripts/context-dna-start (uvicorn memory.synaptic_chat_server:app --port 8888)"
        return
    fi

    # Canonical start lives in scripts/context-dna-start (verified). Don't auto-start.
    report "$n" MANUAL "$name" "down; canonical start: ./scripts/context-dna-start"
    echo ""
    echo -e "  ${YELLOW}Manual step 11${NC}: Synaptic off → agents fall back to raw doc reads (token explosion)"
    echo -e "  Canonical start: ${BOLD}./scripts/context-dna-start${NC} (handles synaptic_chat_server on :8888)"
    echo "  Or directly:   PYTHONPATH=. python -m uvicorn memory.synaptic_chat_server:app --host 0.0.0.0 --port $SYNAPTIC_PORT"
    echo "  Verify with:   curl -sf http://127.0.0.1:$SYNAPTIC_PORT/markdown/health | jq ."
    echo ""
}

# ── Action 12: Scheduler (memory/.scheduler_coordinator.pid) ────────────────
action_scheduler() {
    local n=12 name="Scheduler (scheduler_coordinator)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    # Idempotency: PID file present AND kill -0 succeeds
    if [[ -f "$SCHEDULER_PID_FILE" ]]; then
        local pid
        pid="$(cat "$SCHEDULER_PID_FILE" 2>/dev/null || echo "")"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            report "$n" OK "$name" "running (PID $pid via $SCHEDULER_PID_FILE)"
            return
        fi
    fi

    # Secondary probe: pgrep (PID file may be absent on some launch paths)
    if pgrep -f "scheduler_coordinator" >/dev/null 2>&1; then
        local pid; pid="$(pgrep -f scheduler_coordinator | head -1)"
        report "$n" OK "$name" "running (PID $pid via pgrep — no PID file)"
        return
    fi

    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would print: ./scripts/atlas-ops.sh scheduler start"
        return
    fi

    if [[ ! -f "$ATLAS_OPS_SCRIPT" ]]; then
        report "$n" FAIL "$name" "atlas-ops.sh missing: $ATLAS_OPS_SCRIPT"
        return
    fi

    report "$n" MANUAL "$name" "down; start with: ./scripts/atlas-ops.sh scheduler start"
    echo ""
    echo -e "  ${YELLOW}Manual step 12${NC}: scheduler off → P4 BACKGROUND tasks (gold mining, cardio EKG) dead"
    echo -e "  Start it with: ${BOLD}./scripts/atlas-ops.sh scheduler start${NC}"
    echo "  Verify with:   ./scripts/atlas-ops.sh scheduler status"
    echo ""
}

# ── Action 13: Audit-only — redis pip install (Cycle 8 H5) ──────────────────
action_redis_audit() {
    local n=13 name="Redis pip install audit (Cycle 8 H5)"

    if is_skipped "$n"; then report "$n" SKIP "$name" "user --skip"; return; fi

    # Audit-only: confirm via .venv/bin/python -c 'import redis'
    local pyexe="$VENV_DIR/bin/python"
    [[ -x "$pyexe" ]] || pyexe="$VENV_DIR/bin/python3"

    if [[ ! -x "$pyexe" ]]; then
        report "$n" MANUAL "$name" "no .venv python found at $VENV_DIR — re-run: python3 -m venv $VENV_DIR && pip install redis"
        return
    fi

    local redis_version
    redis_version="$("$pyexe" -c 'import redis, sys; print(redis.__version__)' 2>/dev/null || echo "")"
    if [[ -n "$redis_version" ]]; then
        report "$n" OK "$name" "redis $redis_version importable in .venv (confirmed via $pyexe -c 'import redis')"
        return
    fi

    # Not importable — ZSF: report MANUAL with the fix command
    if [[ "$MODE" == "dry-run" ]]; then
        report "$n" DRY "$name" "would re-install: $VENV_DIR/bin/pip install redis"
        return
    fi

    report "$n" MANUAL "$name" "redis not importable in .venv — run: $VENV_DIR/bin/pip install redis"
}

# ── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}═══ sprint-aaron-actions.sh — Cycle 6 (F1) ═══${NC}"
echo -e "  Mode: ${BOLD}${MODE}${NC}  |  Repo: $REPO_DIR"
[[ "${#SKIPS[@]}" -gt 0 ]] && echo -e "  Skips: ${SKIPS[*]}"
echo ""

# ── Run ──────────────────────────────────────────────────────────────────────
action_mlx_lm
action_launchd_llm
action_bridge_oauth
action_zshrc_3s
action_daemon_restart
action_vsix
action_admin_push
action_invariants_wire
action_webhook
action_mlx_llm
action_synaptic
action_scheduler
action_redis_audit
action_scrub_anthropic

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}───── Summary (${MODE}) ─────${NC}"
for line in "${SUMMARY[@]}"; do
    echo -e "  $line"
done
echo ""
if [[ "$MODE" == "dry-run" ]]; then
    echo -e "  Dry-run complete. Re-run with ${BOLD}--apply${NC} to execute."
else
    echo -e "  ${GREEN}OK${NC}: $PASS  ${CYAN}SKIP${NC}: $SKIP  ${YELLOW}MANUAL${NC}: $MANUAL  ${RED}FAIL${NC}: $FAIL"
fi
echo ""

# Exit non-zero only on real failures (manual/skip are not failures).
[[ "$FAIL" -gt 0 ]] && exit 1
exit 0
