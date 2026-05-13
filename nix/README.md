# Nix — the Reproducible Path

> If you want to come back in 6 months — or 6 years — and have *byte-identical* infrastructure: use this.

Plain `setup-mothership.sh` + `configure-services.sh` + `configure-ecosystem.sh` work great today. But they rely on `brew install`, `pip install`, etc., which silently track the latest version of every dependency. In 6 months, `age` might be v2.0 with a different CLI. `nats-server` might drop a flag. The bash scripts will mostly still work — but mostly is not always.

The Nix flake locks every dependency by SHA256 hash via `flake.lock`. As long as the GitHub repo (and the Nix store cache) exists, you get the *exact* versions you ran today. **Reproducibility is the feature.**

## Quick start

### 1. Install Nix (one-time, ~5 min)

```bash
# The Determinate Systems installer is the cleanest on macOS:
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
# Close and reopen your terminal so `nix` is on PATH.
```

### 2. Clone the repo

```bash
git clone git@github.com:supportersimulator/contextdna-ide.git ~/dev/contextdna-ide
cd ~/dev/contextdna-ide
```

### 3. Drop into the pinned dev shell

```bash
nix develop
```

You now have a shell with `age`, `awscli2`, `nats-server`, `natscli`, `python312`, `docker`, `docker-compose`, `postgresql_16`, `jq`, `gh`, and friends — all pinned by `flake.lock`. The shell hook prints the resolved versions on entry so you can verify.

### 4. Use the same scripts you already know

Inside `nix develop`:

```bash
bash scripts/setup-mothership.sh
bash scripts/configure-services.sh
bash scripts/configure-ecosystem.sh
bash scripts/seal-recovery-bundle.sh
bash scripts/unseal-recovery-bundle.sh <bundle.age>
```

Or invoke them through Nix apps (same scripts, guaranteed to find every tool):

```bash
nix run .#setup
nix run .#services
nix run .#ecosystem
nix run .#probe       # read-only audit
nix run .#seal
nix run .#restore -- ~/USB/contextdna-recovery-*.age
```

## Full system management (optional but recommended)

For the "I want my whole machine declaratively managed" path:

```bash
nix run nix-darwin/master -- switch --flake .#mothership
```

This activates `nix/darwin-configuration.nix`, which:

- Installs every system tool at pinned versions
- Bridges to Homebrew for Docker Desktop + Claude Code (things Nix doesn't package as well)
- Declares launchd services for NATS + daily pg-backup + weekly JetStream-snapshot
- Wires `home-manager` so `~/.claude/settings.json` and `~/.3surgeons/config.yaml` are managed from `nix/home-manager.nix`
- Sets `nix.gc.automatic = true` so the store stays tidy

After activation, your laptop is *declared* by `flake.nix` + `nix/*.nix`. To change anything: edit a file, run `darwin-rebuild switch --flake .#mothership`. To preview changes: `darwin-rebuild build --flake .#mothership`. To roll back: `darwin-rebuild --rollback`.

## The 6-month recovery path with Nix

This is genuinely *one command* once Nix is installed:

```bash
# Day 1: fresh Mac, nothing installed
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
exit
# (reopen terminal)

# Day 1, hour 1: clone + restore from your thumb drive
git clone git@github.com:supportersimulator/contextdna-ide.git ~/dev/contextdna-ide
cd ~/dev/contextdna-ide
nix run .#restore -- /Volumes/USB/contextdna-recovery-*.age
# Enter passphrase. Wait ~10 minutes.

# Day 1, hour 2: optional — apply the system-level config
nix run nix-darwin/master -- switch --flake .#mothership

# Verify
nix run .#probe
```

Three commands. Identical to what you ran a year ago. **Same versions, same configs, same behavior.**

## Why this beats bash alone

| | bash scripts only | Nix flake + bash |
|---|---|---|
| Dependency versions | Whatever brew/pip serves today | Pinned by hash via `flake.lock` |
| "It worked yesterday" | Sometimes drifts | Never |
| Cross-machine consistency | Best-effort | Byte-identical |
| Rollback | Not built-in | `darwin-rebuild --rollback` |
| Garbage collection | Manual | Automatic |
| Onboarding a new user | Run 3 scripts | `nix run .#setup` |
| System-level config | launchd plists hand-edited | Declarative in `darwin-configuration.nix` |

## Updating pinned versions

```bash
nix flake update      # bump all inputs
git diff flake.lock   # see what moved
nix flake check       # verify nothing broke
git commit -am "flake: update inputs"
```

The lock file is the source of truth. Commit it. Treat it like `package-lock.json`.

## Limitations

- **Nix has a learning curve.** The pure-functional, store-based model is unfamiliar at first. Lean on the existing flake; you don't need to author new modules until you want to.
- **macOS Nix has rough edges** at the system-management level. nix-darwin smooths most of them, but expect occasional friction with paths and Homebrew interop.
- **Disk space.** The Nix store grows. `nix.gc.automatic` keeps it bounded, but if you never gc, expect 10-20 GB.
- **The bash scripts are still the ground truth.** Nix wraps them; it doesn't replace them. If you're debugging, drop into `nix develop` and run the bash directly.

## When to use which path

| Situation | Best tool |
|---|---|
| Solo machine, won't touch infrastructure for a while | Plain bash scripts |
| Want bash UX but pinned tools | `nix develop` + bash |
| Multiple machines, want them identical | `darwinConfigurations.mothership` |
| Coming back after 6+ months | Nix everything |
| Want to onboard a new contributor | `nix run .#setup` |

If in doubt: start with `nix develop`, fall back to plain bash if Nix gets in the way. Both produce a working mothership; only Nix produces a *byte-identical* one.
