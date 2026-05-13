#!/bin/bash
# sync-node-config.sh — apply git-tracked node config to local node.
#
# Trigger-based cross-node symmetry pattern:
#   1. fleet-git-msg.sh auto-pulls origin/main every 60s on each peer
#   2. After pull, this script applies any new node-config to local Library
#   3. Idempotent — re-running is safe; logs each apply with before/after
#   4. Symmetric — same script runs on all peers; works regardless of which
#      canonical plist label they use (fleet-nats vs fleet-nerve)
#
# Sources of truth (git-tracked):
#   scripts/xbar/                — xbar plugins (synced to ~/Library/Application Support/xbar/plugins/)
#   scripts/raise-fd-limit.py     — plist FD-limit patcher (auto-finds canonical label)
#
# Usage:
#   bash scripts/sync-node-config.sh             # check + apply
#   bash scripts/sync-node-config.sh --dry-run   # preview, no changes
#   bash scripts/sync-node-config.sh --restart   # also restart daemon if plist changed
#
# Designed to be safely cron-triggered (e.g. fleet-inbox-watcher event).

set -uo pipefail

DRY_RUN=0
DO_RESTART=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --restart) DO_RESTART=1; shift ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
XBAR_DIR="$HOME/Library/Application Support/xbar/plugins"
LOG="/tmp/sync-node-config-$(date +%Y%m%d).log"
ERR_COUNTER="/tmp/sync-node-config-errors.count"
PLIST_CHANGED=0
ERR_COUNT=0

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
say() { log ">>> $*"; }
err() {
    log "ERROR: $*"
    ERR_COUNT=$((ERR_COUNT + 1))
    # ZSF: persist counter so external watchers (xbar / health) can read.
    local prev=0
    [[ -f "$ERR_COUNTER" ]] && prev=$(cat "$ERR_COUNTER" 2>/dev/null || echo 0)
    [[ "$prev" =~ ^[0-9]+$ ]] || prev=0
    echo $((prev + 1)) > "$ERR_COUNTER" 2>/dev/null || true
}

# Recursive content hash of a directory tree (sha256, sorted by path so order-stable).
tree_hash() {
    local dir="$1"
    [[ -d "$dir" ]] || { echo "MISSING"; return; }
    (cd "$dir" && find . -type f \! -name '.DS_Store' -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 shasum -a 256 2>/dev/null \
        | shasum -a 256 \
        | awk '{print $1}')
}

# ---- xbar plugins sync --------------------------------------------------

sync_xbar() {
    local src="$REPO_ROOT/scripts/xbar"
    [[ -d "$src" ]] || { log "xbar: src dir missing $src — skip"; return 0; }
    [[ -d "$XBAR_DIR" ]] || { log "xbar: ~/Library/.../xbar/plugins missing — skip"; return 0; }
    local count_changed=0
    local f base dest src_hash dest_hash
    for f in "$src"/*.sh; do
        [[ -f "$f" ]] || continue
        base=$(basename "$f")
        dest="$XBAR_DIR/$base"
        src_hash=$(shasum -a 256 "$f" | awk '{print $1}')
        dest_hash=$(shasum -a 256 "$dest" 2>/dev/null | awk '{print $1}')
        if [[ "$src_hash" != "$dest_hash" ]]; then
            if [[ $DRY_RUN -eq 1 ]]; then
                log "xbar DRY-RUN: would update $base"
            else
                cp -p "$f" "$dest"
                chmod +x "$dest"
                log "xbar: updated $base (src=${src_hash:0:8} → dest)"
            fi
            count_changed=$((count_changed + 1))
        fi
    done
    log "xbar: $count_changed plugin(s) updated"
}

# ---- plist FD-limit patch -----------------------------------------------

patch_plist() {
    local patcher="$REPO_ROOT/scripts/raise-fd-limit.py"
    [[ -f "$patcher" ]] || { log "fd-patcher: missing $patcher — skip"; return 0; }
    local out
    if [[ $DRY_RUN -eq 1 ]]; then
        log "plist DRY-RUN: would run $patcher"
        out=$(python3 "$patcher" --dry-run 2>&1 || true)
    else
        out=$(python3 "$patcher" 2>&1 || true)
    fi
    log "plist: $out"
    if echo "$out" | grep -q "^PATCHED:"; then
        PLIST_CHANGED=1
    fi
}

# ---- post-commit hook for fleet.capability KV publisher --------------------
# (mac3 finding 2026-05-07: bucket had 0 keys because hook wasn't installed
# on mac1+mac2. Wire on every peer via canonical scripts/git-hooks/post-commit.)

install_post_commit_hook() {
    local hook_src="$REPO_ROOT/scripts/git-hooks/post-commit"
    local hook_dst="$REPO_ROOT/.git/hooks/post-commit"
    [[ -f "$hook_src" ]] || { log "hook: src missing $hook_src — skip"; return 0; }
    [[ -d "$REPO_ROOT/.git/hooks" ]] || { log "hook: .git/hooks missing — not in a git repo?"; return 0; }
    # Idempotent: if dst is already a symlink pointing at our src (relative path), no-op.
    if [[ -L "$hook_dst" ]] && [[ "$(readlink "$hook_dst")" == "../../scripts/git-hooks/post-commit" ]]; then
        log "hook: post-commit already installed (symlink)"
        return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        log "hook DRY-RUN: would install symlink ../../scripts/git-hooks/post-commit"
        return 0
    fi
    # Preserve existing non-symlink hook as .bak before overwrite.
    if [[ -e "$hook_dst" && ! -L "$hook_dst" ]]; then
        cp "$hook_dst" "${hook_dst}.bak"
        log "hook: preserved existing non-symlink hook to ${hook_dst}.bak"
    fi
    ln -sf ../../scripts/git-hooks/post-commit "$hook_dst"
    log "hook: installed post-commit symlink — fleet.capability publisher will fire on next commit"
    # One-time backfill so capability KV doesn't have to wait until next commit.
    # Best-effort; ZSF — failures logged, script continues.
    if [[ -f "$REPO_ROOT/tools/fleet_capability_publish.py" ]]; then
        local out
        out=$(cd "$REPO_ROOT" && PYTHONPATH=. python3 tools/fleet_capability_publish.py 2>&1 | tail -1)
        log "hook: backfill — $out"
    fi
}

# ---- multifleet Claude Code plugin install ------------------------------
# JJ2 (2026-05-08): II4 shipped multi-fleet/multifleet-plugin/ but it must
# land at ~/.claude/plugins/cache/<marketplace>/<plugin>/<ver>/ before the
# PreToolUse hook (MFINV-C01) can fire. This step copies the in-repo plugin
# tree to the canonical Claude Code cache path on each peer.
#
# Idempotent: tree_hash() compares src vs dest; no-op when equal.
# Backup: any pre-existing non-symlink dest is preserved as .bak.<TS>.
# ZSF: copy/version-read failures increment ERR_COUNT and log; never silent.

install_multifleet_plugin() {
    local plugin_src="$REPO_ROOT/multi-fleet/multifleet-plugin"
    if [[ ! -d "$plugin_src" ]]; then
        log "plugin: src dir missing $plugin_src — skip"
        return 0
    fi
    # Canonical Claude Code manifest path is .claude-plugin/plugin.json
    # (Claude Code's plugin loader REJECTS root-level plugin.json — LL3 fix).
    local manifest="$plugin_src/.claude-plugin/plugin.json"
    if [[ ! -f "$manifest" ]]; then
        err "plugin: manifest missing $manifest"
        return 0
    fi
    # Read version + name from canonical manifest (no jq dependency).
    local version plugin_name
    version=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$manifest" 2>/dev/null || true)
    plugin_name=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('name',''))" "$manifest" 2>/dev/null || true)
    if [[ -z "$version" || -z "$plugin_name" ]]; then
        err "plugin: could not read version/name from $manifest"
        return 0
    fi
    # Plugin lives under multi-fleet-marketplace (registered in
    # multi-fleet/.claude-plugin/marketplace.json) — match the marketplace
    # Claude Code already knows about, otherwise the plugin loader won't
    # see it even if files are on disk.
    local marketplace="multi-fleet-marketplace"
    local cache_root="$HOME/.claude/plugins/cache/$marketplace/$plugin_name"
    local dest="$cache_root/$version"

    local src_hash
    src_hash=$(tree_hash "$plugin_src")
    local dest_hash
    dest_hash=$(tree_hash "$dest")

    if [[ -L "$dest" ]]; then
        # Existing symlink — log canonical state, do not overwrite.
        log "plugin: ALREADY-CANONICAL — $dest is a symlink → $(readlink "$dest")"
        return 0
    fi

    if [[ "$src_hash" == "$dest_hash" && "$src_hash" != "MISSING" ]]; then
        log "plugin: unchanged (sha=${src_hash:0:8}) — $dest"
        return 0
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        log "plugin DRY-RUN: would install $plugin_src → $dest (src_hash=${src_hash:0:8}, dest_hash=${dest_hash:0:8})"
        return 0
    fi

    # Backup any pre-existing non-symlink directory before overwrite.
    if [[ -e "$dest" && ! -L "$dest" ]]; then
        local ts
        ts=$(date +%Y%m%dT%H%M%S)
        if mv "$dest" "${dest}.bak.${ts}" 2>>"$LOG"; then
            log "plugin: backed up existing dest → ${dest}.bak.${ts}"
        else
            err "plugin: failed to back up existing dest $dest"
            return 0
        fi
    fi

    # Ensure cache root exists, then copy.
    if ! mkdir -p "$cache_root" 2>>"$LOG"; then
        err "plugin: mkdir -p $cache_root failed"
        return 0
    fi
    # Copy contents into dest (preserve perms/timestamps, exclude .DS_Store noise).
    if ! cp -Rp "$plugin_src" "$dest" 2>>"$LOG"; then
        err "plugin: cp -Rp $plugin_src → $dest failed"
        return 0
    fi
    # Strip macOS metadata that would skew tree_hash on next run.
    find "$dest" -name '.DS_Store' -delete 2>/dev/null || true

    local new_hash
    new_hash=$(tree_hash "$dest")
    if [[ "$new_hash" != "$src_hash" ]]; then
        err "plugin: post-copy hash mismatch (src=${src_hash:0:8}, dest=${new_hash:0:8})"
        return 0
    fi
    log "plugin: installed $plugin_name@$version → $dest (sha=${src_hash:0:8})"
}

# ---- multifleet jetstream num_replicas pin (LL2 2026-05-08) ----------------
# JJ3 root cause: daemon `_handle_heartbeat` actively narrows JetStream
# replicas on peer churn. Cure: pin `jetstream.num_replicas=3` in each node's
# local .multifleet/config.json so `compute_target_replicas` returns the
# operator value verbatim (override path) instead of falling through to the
# `min(peer_count, 5)` heuristic.
#
# Idempotent — runs every cron tick:
#   - If `.multifleet/config.json` already has `jetstream.num_replicas` set
#     (any value), no-op (don't clobber lab values like R=1).
#   - Else, additive merge `{"jetstream": {"num_replicas": 3}}` and back up
#     the original to `.bak.<TS>`.
# ZSF: any failure increments ERR_COUNT + logs; never silent.

ensure_jetstream_num_replicas() {
    local cfg_path="$REPO_ROOT/.multifleet/config.json"
    if [[ ! -f "$cfg_path" ]]; then
        log "jetstream-pin: $cfg_path missing — skip (daemon will load template defaults)"
        return 0
    fi
    # Use python3 (always present, no jq dep) to read + merge atomically.
    # Output: ALREADY_SET | UPDATED | ERROR:<reason>
    local result
    result=$(python3 - "$cfg_path" <<'PY' 2>&1
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    cfg = json.loads(p.read_text() or "{}")
except Exception as e:
    print(f"ERROR:read_failed:{e}")
    sys.exit(2)
js = cfg.get("jetstream")
if isinstance(js, dict) and "num_replicas" in js:
    print(f"ALREADY_SET:{js.get('num_replicas')}")
    sys.exit(0)
if not isinstance(js, dict):
    js = {}
js["num_replicas"] = 3
# Preserve enabled if previously set; if absent, add enabled=true since pin
# only matters when JS is on. Operators can still flip enabled=false later.
if "enabled" not in js:
    js["enabled"] = True
cfg["jetstream"] = js
try:
    p.write_text(json.dumps(cfg, indent=2) + "\n")
except Exception as e:
    print(f"ERROR:write_failed:{e}")
    sys.exit(3)
print("UPDATED")
PY
    )
    local rc=$?
    case "$result" in
        ALREADY_SET:*)
            log "jetstream-pin: $cfg_path — $result (idempotent no-op)"
            ;;
        UPDATED)
            if [[ $rc -ne 0 ]]; then
                err "jetstream-pin: python merge exited $rc but printed UPDATED — investigate"
                return 0
            fi
            log "jetstream-pin: $cfg_path — added jetstream.num_replicas=3"
            ;;
        ERROR:*)
            err "jetstream-pin: $cfg_path merge failed — $result"
            return 0
            ;;
        *)
            err "jetstream-pin: unexpected python output: $result (rc=$rc)"
            return 0
            ;;
    esac
}

# Pre-flight backup helper: copies the file to .bak.<TS> before mutation.
backup_before_pin() {
    local cfg_path="$REPO_ROOT/.multifleet/config.json"
    [[ -f "$cfg_path" ]] || return 0
    # Skip backup if pin already present (no mutation will happen).
    if python3 -c "import json,sys; cfg=json.load(open(sys.argv[1])); js=cfg.get('jetstream') or {}; sys.exit(0 if 'num_replicas' in js else 1)" "$cfg_path" 2>/dev/null; then
        return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        log "jetstream-pin DRY-RUN: would back up $cfg_path"
        return 0
    fi
    local ts
    ts=$(date +%Y%m%dT%H%M%S)
    if cp "$cfg_path" "${cfg_path}.bak.${ts}" 2>>"$LOG"; then
        log "jetstream-pin: backed up $cfg_path → ${cfg_path}.bak.${ts}"
    else
        err "jetstream-pin: failed to back up $cfg_path"
    fi
}

apply_jetstream_pin() {
    if [[ $DRY_RUN -eq 1 ]]; then
        local cfg_path="$REPO_ROOT/.multifleet/config.json"
        if [[ -f "$cfg_path" ]] && python3 -c "import json,sys; cfg=json.load(open(sys.argv[1])); js=cfg.get('jetstream') or {}; sys.exit(0 if 'num_replicas' in js else 1)" "$cfg_path" 2>/dev/null; then
            log "jetstream-pin DRY-RUN: $cfg_path already has jetstream.num_replicas — no-op"
        else
            log "jetstream-pin DRY-RUN: would add jetstream.num_replicas=3 to $cfg_path"
        fi
        return 0
    fi
    backup_before_pin
    ensure_jetstream_num_replicas
}

# ---- multifleet marketplace + plugin registration (MM3 2026-05-08) -------
# LL3 distribution gap: step 4 (install_multifleet_plugin) lays files into
# ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/ on each peer, but
# Claude Code's plugin loader ALSO needs the marketplace to be registered
# (`claude plugin marketplace add`) and the plugin to be installed/enabled
# (`claude plugin install <name>@<marketplace>`). Without those, mac2/mac3/cloud
# have files on disk but the loader never picks them up.
#
# This step is ADDITIVE — it runs after install_multifleet_plugin and only
# wires the marketplace/plugin registration. Step 4 is unchanged.
#
# Idempotency:
#   - If `claude plugin marketplace list` already includes "multi-fleet-marketplace",
#     skip add (logs ALREADY-INSTALLED:marketplace).
#   - If `claude plugin list` already includes "multifleet-channel-priority"
#     with Status enabled, skip install (logs ALREADY-INSTALLED:plugin).
#
# ZSF (missing CLI):
#   - If `claude` is not on PATH (e.g. running under launchd with stripped env),
#     log WARN, increment ERR_COUNT (so xbar/health surfaces it), and return 0.
#     Sync must NOT fail just because Claude Code CLI isn't reachable in this
#     execution context — it's a deferred-registration condition, not a fatal.
#
# ZSF (CLI failure):
#   - `claude plugin install` non-zero → err() (counter bump + non-zero exit).

register_multifleet_marketplace_and_install() {
    local marketplace="multi-fleet-marketplace"
    local marketplace_manifest="$REPO_ROOT/multi-fleet/.claude-plugin/marketplace.json"
    local plugin_name="multifleet-channel-priority"

    # ZSF: detect missing claude CLI (PATH-stripped launchd context, headless
    # cron, etc.). Don't blow up the whole sync — WARN + counter, return 0.
    if ! command -v claude >/dev/null 2>&1; then
        log "plugin-register: WARN — 'claude' CLI not on PATH (launchd/cron context?). Skipping marketplace + install registration. Files on disk; registration deferred to next interactive sync."
        # Best-effort counter bump so xbar/health watcher can surface the warn.
        local prev=0
        [[ -f "$ERR_COUNTER" ]] && prev=$(cat "$ERR_COUNTER" 2>/dev/null || echo 0)
        [[ "$prev" =~ ^[0-9]+$ ]] || prev=0
        echo $((prev + 1)) > "$ERR_COUNTER" 2>/dev/null || true
        return 0
    fi

    if [[ ! -f "$marketplace_manifest" ]]; then
        err "plugin-register: marketplace manifest missing $marketplace_manifest"
        return 0
    fi

    # ---- 5a. Marketplace registration (idempotent) ----
    local mp_listing
    mp_listing=$(claude plugin marketplace list 2>&1 || true)
    if echo "$mp_listing" | grep -q "$marketplace"; then
        log "plugin-register: ALREADY-INSTALLED:marketplace ($marketplace)"
    else
        if [[ $DRY_RUN -eq 1 ]]; then
            log "plugin-register DRY-RUN: would 'claude plugin marketplace add $REPO_ROOT/multi-fleet'"
        else
            local add_out
            # Add by directory (per `claude plugin marketplace add --help` — accepts path).
            if add_out=$(claude plugin marketplace add "$REPO_ROOT/multi-fleet" 2>&1); then
                log "plugin-register: marketplace added — $marketplace (output: $(echo "$add_out" | tr '\n' ' ' | head -c 200))"
            else
                err "plugin-register: 'claude plugin marketplace add' failed — $(echo "$add_out" | tr '\n' ' ' | head -c 300)"
                return 0
            fi
        fi
    fi

    # ---- 5b. Plugin install/enable (idempotent) ----
    local plugin_listing
    plugin_listing=$(claude plugin list 2>&1 || true)
    # Match a 4-line block: "❯ <name>@<marketplace>" header + Status: enabled within
    # the next 4 lines. grep -A 3 keeps logic simple and resilient to format jitter.
    if echo "$plugin_listing" | grep -A 3 "$plugin_name@$marketplace" | grep -q "enabled"; then
        log "plugin-register: ALREADY-INSTALLED:plugin ($plugin_name@$marketplace, enabled)"
        return 0
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        log "plugin-register DRY-RUN: would 'claude plugin install $plugin_name@$marketplace'"
        return 0
    fi

    local install_out
    if install_out=$(claude plugin install "$plugin_name@$marketplace" 2>&1); then
        log "plugin-register: installed $plugin_name@$marketplace (output: $(echo "$install_out" | tr '\n' ' ' | head -c 200))"
    else
        err "plugin-register: 'claude plugin install $plugin_name@$marketplace' failed — $(echo "$install_out" | tr '\n' ' ' | head -c 300)"
        return 0
    fi
}

# ---- neuro-cutover env var (PP3 2026-05-08) ------------------------------
# Pre-staged scaffolding for the 3-surgeons neurologist DeepSeek cutover.
# Default behavior on every node = NO-OP (ENABLE=False in the patcher).
# Aaron flips by editing one constant — see scripts/patch-neuro-cutover.py
# and docs/runbooks/neuro-cutover-flip.md.
#
# This step runs the patcher in default mode every cron tick. Until Aaron
# flips ENABLE, the patcher prints "ALREADY-SET: ... (no
# CONTEXT_DNA_NEURO_PROVIDER present)" — pure idempotent read. After the
# flip, every peer self-heals to CONTEXT_DNA_NEURO_PROVIDER=deepseek on
# the next sync tick.
#
# ZSF: patcher exits non-zero on I/O errors → err() bumps counter.
apply_neuro_cutover() {
    local patcher="$REPO_ROOT/scripts/patch-neuro-cutover.py"
    if [[ ! -f "$patcher" ]]; then
        log "neuro-cutover: missing $patcher — skip"
        return 0
    fi
    local out rc
    if [[ $DRY_RUN -eq 1 ]]; then
        out=$(python3 "$patcher" --dry-run 2>&1); rc=$?
    else
        out=$(python3 "$patcher" 2>&1); rc=$?
    fi
    # Patcher emits one parseable line per target — log each line separately
    # so xbar / tail can grep cleanly.
    while IFS= read -r line; do
        [[ -n "$line" ]] && log "neuro-cutover: $line"
    done <<< "$out"
    if [[ $rc -ne 0 ]]; then
        err "neuro-cutover: patcher exited $rc"
    fi
    # If the patcher reported PATCHED on the fleet-nats / fleet-nerve plist,
    # mark PLIST_CHANGED so an optional --restart picks up the env var.
    if echo "$out" | grep -qE "^PATCHED: .*(fleet-nats|fleet-nerve)\.plist"; then
        PLIST_CHANGED=1
    fi
}

restart_daemon_if_plist_changed() {
    [[ $PLIST_CHANGED -eq 1 ]] || { log "restart: plist unchanged — skip"; return 0; }
    [[ $DO_RESTART -eq 1 ]] || { log "restart: --restart not given — plist patched but daemon not restarted"; return 0; }
    if [[ -x "$REPO_ROOT/scripts/fleet-daemon.sh" ]]; then
        say "restarting daemon to pick up plist changes"
        bash "$REPO_ROOT/scripts/fleet-daemon.sh" restart 2>&1 | tee -a "$LOG"
    else
        log "restart: scripts/fleet-daemon.sh missing — manual restart needed"
    fi
}

# ---- main ---------------------------------------------------------------

say "sync-node-config start (dry-run=$DRY_RUN, restart=$DO_RESTART)"
say "node: $(hostname -s)"
sync_xbar
patch_plist
install_post_commit_hook
install_multifleet_plugin
register_multifleet_marketplace_and_install
apply_jetstream_pin
apply_neuro_cutover
restart_daemon_if_plist_changed
say "sync-node-config done — full log: $LOG (errors=$ERR_COUNT)"
# ZSF: non-zero exit when any step recorded an error so callers/cron see it.
[[ $ERR_COUNT -eq 0 ]] || exit 1
exit 0
