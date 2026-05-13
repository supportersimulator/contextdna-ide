# sprint-aaron-actions.sh — partner doc (Cycle 6 F1)

One-shot installer for the 8 Aaron-actions accumulated across cycles 3–5
of the autonomous 10h sprint. Idempotent. Zero silent failures.

## Invocation

```bash
# Preview every action — no side effects:
bash scripts/sprint-aaron-actions.sh --dry-run

# Execute every action:
bash scripts/sprint-aaron-actions.sh --apply

# Skip individual steps (repeatable):
bash scripts/sprint-aaron-actions.sh --apply --skip 1 --skip 7

# Non-interactive (skips the prompt for step 4):
bash scripts/sprint-aaron-actions.sh --apply --no-prompt
```

## Step-by-step reference

Each row: action → idempotency check → revert command.

### 1. `mlx_lm` install → `context-dna/local_llm/.venv-mlx`

- **Why**: frees Neurologist from DeepSeek fallback.
- **Idempotency**: skipped if `.venv-mlx/bin/python3 -c 'import mlx_lm'` succeeds.
- **Revert**: `rm -rf context-dna/local_llm/.venv-mlx`

### 2. `bash scripts/install-launchd-plists.sh llm`

- **Why**: bootstrap LLM launchd plist (or refresh if already loaded).
- **Idempotency**: re-running is a no-op (installer does `unload → load`).
- **Revert**: `bash scripts/install-launchd-plists.sh --uninstall` (filter as needed).

### 3. `BRIDGE_OAUTH_PASSTHROUGH=1` in fleet-nats plist

- **Why**: lets local IDE bridge piggy-back on daemon's OAuth.
- **Idempotency**: skipped if key already in `EnvironmentVariables` dict (read via `plistlib`).
- **Side-effect**: `launchctl bootout` then `bootstrap` to pick up env var.
- **Backup**: `~/Library/LaunchAgents/io.contextdna.fleet-nats.plist.bak.<timestamp>`
- **Revert**:
  ```bash
  cp ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist.bak.<TS> \
     ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist
  launchctl bootout gui/$(id -u)/io.contextdna.fleet-nats
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist
  ```

### 4. `THREE_SURGEONS_VIA_BRIDGE=1` in `~/.zshrc` *(prompted)*

- **Why**: routes 3-Surgeons traffic through the bridge.
- **Idempotency**: skipped if `export THREE_SURGEONS_VIA_BRIDGE=1` already present.
- **Behavior**: prompts `[y/N]` interactively. With `--no-prompt`, reports MANUAL.
- **Revert**: edit `~/.zshrc`, remove the line plus the `# Added by sprint-aaron-actions on …` marker.

### 5. Restart fleet daemon

- **Why**: pick up D2 ratelimit + D5 sub-watchdog counters.
- **Idempotency**: `pkill -f fleet_nerve_nats`; `KeepAlive=true` auto-respawns.
- **Verification**: polls `http://127.0.0.1:8855/health` for up to 5s.
- **Revert**: not applicable — restart is transient. To force-stop:
  ```bash
  launchctl bootout gui/$(id -u)/io.contextdna.fleet-nats
  ```

### 6. Install IDE VSIX

- **Path**: `context-dna/clients/vscode/context-dna-vscode-0.2.0.vsix`
- **Idempotency**: skipped if `code --list-extensions --show-versions` reports `context-dna.context-dna-vscode@0.2.0`.
- **Falls back to MANUAL** if `code` CLI is not on PATH.
- **Revert**: `code --uninstall-extension context-dna.context-dna-vscode`

### 7. Push `admin.contextdna.io` commits *(MANUAL — interactive auth required)*

- **Why**: the admin submodule remote is HTTPS; auth must be entered live.
- **Remote**: `https://github.com/supportersimulator/v0-Context-DNA-1.git`
- **Unpushed commits at session end** (5 — verified Cycle 9 I5):
  - `d779271` feat(admin): fleet status view — Session 3 DEMO-READY
  - `1a433b9` fix(dashboard): TypeScript strict-mode fixes across 5 components
  - `c7c9bd1` feat(dashboard): fleet-status view component
  - `22f66f5` deps: add @modelcontextprotocol/sdk for MCP tool integration
  - `8bd9803` feat(dashboard): add Fleet panel for multi-fleet node status
- **Action prints** (does not execute):
  ```bash
  cd admin.contextdna.io
  git push origin main
  ```
- **SSH alternative** (avoids HTTPS auth prompt on every push):
  ```bash
  cd admin.contextdna.io
  git remote set-url origin git@github.com:supportersimulator/v0-Context-DNA-1.git
  git push origin main
  ```
- **Idempotency**: reports OK if already up to date (`git rev-list --count origin/main..HEAD == 0`).

### 8. Wire `validateERSimInvariants.cjs` into `scripts/gains-gate.sh`

- **Why**: surface ER-sim invariant drift in the standard post-phase gate.
- **Severity**: warning-only (ER-sim has its own CI; gains-gate signals drift, doesn't block).
- **Idempotency**: skipped if `gains-gate.sh` already references `validateERSimInvariants.cjs`.
- **Inserts** a numbered check block before the `# ── Results ──` section.
- **Backup**: `scripts/gains-gate.sh.bak.<timestamp>`
- **Revert**: `cp scripts/gains-gate.sh.bak.<TS> scripts/gains-gate.sh`

### B. *(bonus)* Scrub `ANTHROPIC_AUTH_TOKEN=dummy`

- **Targets**: `~/.zshrc`, `~/.bash_profile`.
- **Behavior**: comments out the assignment (never deletes it). Adds a marker:
  `# Removed by sprint-aaron-actions on YYYY-MM-DD (Cycle 6 F1)`
- **Idempotency**: only matches uncommented lines; commented lines are ignored.
- **Backup**: `<file>.bak.<timestamp>`
- **Revert**: restore the backup, or uncomment the line and delete the marker.

## Status legend

| Status | Meaning |
|---|---|
| `OK` | Action completed (or idempotency check confirmed already done) |
| `SKIP` | User passed `--skip <N>` |
| `MANUAL` | Step requires Aaron (interactive auth, missing CLI, declined prompt) |
| `FAIL` | Action ran but errored — check the inline detail |
| `DRY` | `--dry-run`: would-do preview |

## Exit codes

- `0` — no `FAIL` rows. `MANUAL` and `SKIP` do not block.
- `1` — at least one `FAIL`. Read the per-row detail; revert via the table above.

## Tricky idempotency notes

- Action 3 reloads the daemon, so action 5 may find it already fresh — `pkill` is still
  issued (KeepAlive respawns within ~2s) to validate the watchdog path.
- Action 6's "already installed" check matches both ID *and* version — bumping the VSIX
  version triggers a real install on next run.
- The bonus scrub uses a regex that excludes lines already starting with `#`,
  so re-running won't double-comment or duplicate the marker.

## Cycle 8 H5 additions (2026-05-04, post-Cycle-7 gains-gate audit)

Pre-fix gains-gate (after H5 redis venv fix): 8 PASS / 4 WARN / 6 CRITICAL.
The 6 criticals below are all **services not running** on this node — Aaron
must start them (none are auto-started by H5 because they need a foreground
GPU/Docker session and explicit Aaron consent).

### 9. Start webhook (`agent_service` on :8080)

- **Why**: webhook = #1 priority per CLAUDE.md. While down, Atlas is blind
  to S0–S8 injections and `/health.webhook.events_recorded` cannot advance.
- **Idempotency**: skip if `curl -sf http://127.0.0.1:8080/health` returns 200.
- **Action**: `bash scripts/start-helper-agent.sh` (foreground or via launchd).
- **Verification**: `curl http://127.0.0.1:8080/health | jq .`
- **Revert**: `pkill -f agent_service` (or unload corresponding launchd plist).

### 10. Start MLX LLM (`mlx:5044`)

- **Why**: P1/P2 hot-path classify/extract calls fall through to remote
  DeepSeek when MLX is down — slower and external-dependent. Fleet-gate #9
  (LLM test query P2 classify) currently FAILS.
- **Idempotency**: skip if `curl -sf http://127.0.0.1:5044/v1/models`.
- **Action**: `bash scripts/start-llm.sh`
- **GPU lock**: starting MLX claims `llm:gpu_lock` in Redis. Verify nothing
  else is using the GPU first (`redis-cli get llm:gpu_lock`).

### 11. Start Synaptic doc index (`:8888`)

- **Why**: CLAUDE.md "Context purity" mandates `/markdown/query` first;
  without Synaptic, agents fall back to raw file reads (token explosion).
- **Idempotency**: skip if `curl -sf http://127.0.0.1:8888/markdown/health`.
- **Canonical start** (verified Cycle 10 J2): `./scripts/context-dna-start`
  (handles `synaptic_chat_server.py` on :8888 via uvicorn).
- **Direct invocation**: `PYTHONPATH=. python -m uvicorn memory.synaptic_chat_server:app --host 0.0.0.0 --port 8888`
- **Verification**: indexed doc count > 0 in `/markdown/health`.

### 12. Start scheduler (`memory/.scheduler_coordinator.pid`)

- **Why**: P4 BACKGROUND tasks (gold mining, complexity sentinel,
  cardiologist EKG) don't run without it. Cardio gate auto-trigger is dead
  while scheduler is down.
- **Idempotency**: skip if PID file present AND `kill -0 $(cat …)` succeeds.
  Secondary probe: `pgrep -f scheduler_coordinator` (covers launchd path).
- **Canonical start** (verified Cycle 10 J2): `./scripts/atlas-ops.sh scheduler start`
  (runs `memory/scheduler_coordinator.py`, which embeds `lite_scheduler`).
- **PID file**: written to `memory/.scheduler_coordinator.pid` on startup.
- **Verification**: `./scripts/atlas-ops.sh scheduler status`

### 13. (FIXED in Cycle 8 H5 commit — no Aaron action) `redis` pip install in `.venv`

- The Cycle 7 venv was missing `redis`, causing gains-gate checks #3 (Redis
  ping), #7 (GPU lock), and #11 (Redis key sanity) to silently fail and the
  P1/P2 LLM router to lose its cross-process yield + GPU lock release paths.
- H5 ran `.venv/bin/pip install redis` (`redis 7.4.0`) and committed a
  `requirements-runtime.txt` entry so future venv bootstraps include it.
- **No Aaron action required** — this section is logged only for the
  installer's audit trail.

## Cycle 8 H5 deferred WARNINGs (non-blocking)

- **Redis key sanity (0 keys)**: expected after Redis restart with no
  Atlas activity yet. Will self-heal once webhook + scheduler resume.
- **V12 action registry coverage (no cache)**: re-generate via
  `./scripts/action-registry.sh list`. Single-shot; not a recurring chore.
- **GPU lock (could not check) → resolved by H5 redis pip install** —
  re-run gains-gate post-fix to confirm.

