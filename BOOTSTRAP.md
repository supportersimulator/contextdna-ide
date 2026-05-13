# BOOTSTRAP — From Zero To Mothership

**Audience:** anyone (Aaron, a new fleet member, the post-disaster Aaron with a fresh laptop) standing in front of a clean machine and a network connection.
**Goal:** prove, in a single sitting, that the mothership reconstitutes from a `git clone` and nothing else.

---

## The Operational Invariance Promise

If your laptop fell in the ocean today and your AWS account was deleted tomorrow, the mothership would still come back. Clone the repo to any machine that can run Docker. Drop in the secrets you keep backed up offline. Run one command. The brain wakes up with everything it knew yesterday — every evidence-graded learning, every cross-examined verdict, every architectural decision. Nothing in the running system depends on a specific machine being alive.

That is the *Operational Invariance Promise*. This document is its litmus test. Every section below is a recipe you can run end-to-end; the final section is a single script (`bootstrap-verify.sh`) that runs all the recipes for you and prints `BOOTSTRAP-VERIFIED` only when the whole chain holds. If `BOOTSTRAP-VERIFIED` does not print, the promise is not kept. Fix the failing step, then run it again.

---

## TL;DR — Most reproducible path (Nix)

If you want *byte-identical* setup today and 6 years from now, use Nix. Every dependency pinned by hash, every service declared, full system rollback. See [`nix/README.md`](nix/README.md) for the full walkthrough. Three commands on a fresh Mac:

```bash
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
git clone git@github.com:supportersimulator/contextdna-ide.git && cd contextdna-ide
nix run .#restore -- /Volumes/USB/contextdna-recovery-*.age
```

That's it. Enter the passphrase. ~10 minutes later: `BOOTSTRAP-VERIFIED`.

To go further and have your entire macOS declaratively managed (launchd services for NATS + backup schedule + everything) from one flake:

```bash
nix run nix-darwin/master -- switch --flake .#mothership
```

## TL;DR — One-command onboarding

If this is your first time, run the interactive installer. It walks through every step below, generates the age keypair, configures your backup bucket, writes the offline-recovery card, and schedules daily/weekly backups — in about ten minutes:

```bash
git clone git@github.com:supportersimulator/contextdna-ide.git
cd contextdna-ide
bash scripts/setup-mothership.sh
```

The installer is **idempotent** — safe to re-run anytime. To audit your setup without making changes:

```bash
bash scripts/setup-mothership.sh --check
```

### Wire up LLM providers + local LLM + optional services

After (or before) the bootstrap installer, run the service configurator. It **probes every service**, **prompts only for what's missing or broken**, and lets you **swap providers freely**:

```bash
bash scripts/configure-services.sh
```

Specifically it handles:

- **LLM providers** — OpenAI, Anthropic, DeepSeek, Groq, xAI, Mistral, Cohere, Perplexity, Together. Probes each saved key live; if it works, keeps it. If it doesn't, prompts you with the signup URL and accepts a new key.
- **Local LLM** — auto-detects MLX (port 5044), Ollama (11434), LM Studio (1234), or vLLM (8000). If none are running, offers a backend menu (MLX recommended on Apple Silicon, Ollama for cross-platform) and a model picker (Qwen3-4B / Qwen3-8B / Llama-3.3 / custom). Installs a launchd plist so it auto-loads at login.
- **NATS** — probes localhost:4222, offers to install + run via launchd if missing.
- **Docker stack** — checks Docker is running, offers to bring up the lite profile.
- **Network detection** — finds stale `192.168.x.x` / `10.0.x.x` IPs in your `.env` (common when switching networks or moving to a new machine) and offers to update them.
- **Optional services** — ElevenLabs, LiveKit, Stripe, Supabase, Firebase, SendGrid, Sentry, Cloudflare, Google OAuth. Each is opt-in with the signup URL shown.

Probe mode just reports — useful as a quarterly drill or after any environment change:

```bash
bash scripts/configure-services.sh --probe
```

### Install + wire the Claude Code ecosystem

After services are configured, run the ecosystem configurator. It probes + installs + configures the Claude Code plugins and CLIs that ContextDNA depends on, each one opt-in:

```bash
bash scripts/configure-ecosystem.sh
```

Handles:

- **Claude Code CLI** — verifies it's installed; offers `npm install -g @anthropic-ai/claude-code` if missing
- **Multi-Fleet** — clones `supportersimulator/multi-fleet`, `pip install -e .`, registers the Claude Code plugin, writes `MULTIFLEET_NODE_ID` + `NATS_URL` to `.env`, optionally installs the fleet-daemon launchd plist
- **3-Surgeons** — installs the Claude Code plugin from `supportersimulator/3-surgeons-marketplace`, optionally clones the standalone repo + installs the `3s` CLI, generates a starter `~/.3surgeons/config.yaml` that points the Neurologist at your local LLM and the Cardiologist at whichever provider `configure-services.sh` got working
- **Superpowers** — installs the plugin from `obra/superpowers` (override the marketplace if you use a fork)
- **.mcp.json wiring** — validates that the 7 MCP servers (multifleet, synaptic, projectdna, contextdna-engine, contextdna-webhook, race-theater, evidence-stream, event-bridge) are registered, and that their Python files parse cleanly

Probe mode (no install attempts):

```bash
bash scripts/configure-ecosystem.sh --probe
```

The full first-time-setup flow on a fresh laptop becomes:

```bash
bash scripts/setup-mothership.sh        # bootstrap + backup schedule
bash scripts/configure-services.sh      # LLM providers + local LLM + NATS + Docker
bash scripts/configure-ecosystem.sh     # Multi-Fleet + 3-Surgeons + Superpowers + MCP
bash scripts/seal-recovery-bundle.sh    # encrypt everything to a thumb-drive bundle
```

The seal bundle now includes **the ecosystem configs too** — `~/.claude/settings.json`, `~/.3surgeons/config.yaml`, `.mcp.json`, `.multifleet/config.json`, all `io.contextdna.*` launchd plists, and the list of installed Claude Code plugins. So when you unseal on a fresh laptop in 6 months, `AUTO-RESTORE.sh` restores all of that AND runs `configure-ecosystem.sh` to reinstall any plugins that need it.

### Single-file recovery for disaster scenarios

After running the installer, seal every secret into one passphrase-encrypted `.age` file you can save to a thumb drive:

```bash
bash scripts/seal-recovery-bundle.sh
```

This produces `~/Desktop/contextdna-recovery-YYYYMMDD-<host>.age` + a printable README. Copy both to a thumb drive, save your passphrase to 1Password, delete the Desktop copies. The bundle contains your `.env`, age private key, AWS credentials, macOS Keychain entries, and an embedded `AUTO-RESTORE.sh` — everything needed to bring back the mothership on a fresh laptop with zero manual configuration.

To recover on a brand-new machine 6 months from now:

```bash
brew install age git
git clone git@github.com:supportersimulator/contextdna-ide.git
cd contextdna-ide
bash scripts/unseal-recovery-bundle.sh /Volumes/USB/contextdna-recovery-*.age
# Enter passphrase. Wait ~10 minutes. BOOTSTRAP-VERIFIED prints. Done.
```

The rest of this document is the manual recipe behind the installer. Read it if you want to understand what each step does, or if `setup-mothership.sh` can't do something automatically on your platform.

---

## Prerequisites

You need four things and four things only:

| Need | Why | Check |
|------|-----|-------|
| **Docker** (24+) with the `compose` v2 plugin | Runs the stack | `docker compose version` |
| **git** with SSH access to `supportersimulator/contextdna-ide` | Pulls the mothership | `git ls-remote git@github.com:supportersimulator/contextdna-ide.git` |
| **curl** + **jq** | Probes the live webhook in step 5 | `which curl jq` |
| **Disk space** | ~6 GB for lite, ~50 GB for heavy (Postgres + Ollama models dominate) | `df -h` |

You also need a populated `.env` file. The repo ships `.env.example` with every variable documented and safe defaults for the optional ones. The variables you MUST set yourself (no defaults, secrets only):

| Variable | Profile | Why |
|----------|---------|-----|
| `POSTGRES_PASSWORD` | lite + heavy | Evidence ledger database password |
| `REDIS_PASSWORD` | lite + heavy | Redis auth (pub/sub + cache) |
| `NATS_REMOTE_URL` | lite | URL of the heavy chief this leaf node should sync to (e.g. `nats-leaf://heavy.lan:7422`) |
| `NATS_LEAF_PASSWORD` | lite | Credential the leaf uses to authenticate to chief |
| `RABBITMQ_PASSWORD` | heavy | Celery broker password |
| `GRAFANA_PASSWORD` | heavy | Grafana admin login |

Keep a copy of your populated `.env` somewhere offline (1Password, Bitwarden, a USB stick in a fire safe). That file plus `git clone` is your complete recovery key.

---

## Lite mode (laptop)

Lite mode is a single laptop running a slim subset of the stack. It connects back to a heavy chief over a NATS leaf node if one exists; if no chief is reachable, lite still runs standalone and queues sync events for later.

```bash
# 1. Clone the mothership (and all submodules — engines/, apps/ide-shell)
git clone --recurse-submodules git@github.com:supportersimulator/contextdna-ide.git
cd contextdna-ide

# 2. Drop in your secrets. The example file is annotated; fill in the
#    four required vars (POSTGRES_PASSWORD, REDIS_PASSWORD, NATS_REMOTE_URL,
#    NATS_LEAF_PASSWORD) and leave the rest as defaults for a first run.
cp .env.example .env
$EDITOR .env

# 3. Bring up the lite stack (postgres, redis, nats-leaf, mcp-webhook, helper-agent).
docker compose -f docker-compose.lite.yml up -d

# 4. Wait until every container reports healthy. The helper agent takes the
#    longest (30s start_period). `docker compose ps` shows the live state.
docker compose -f docker-compose.lite.yml ps

# 5. Verify the webhook fires and returns a layered context payload.
curl -sS -X POST -G \
    --data-urlencode "prompt=hello mothership" \
    --data-urlencode "risk_level=moderate" \
    http://127.0.0.1:8080/consult | jq .
```

If step 5 prints a JSON object with a `layers` key holding professor/learnings/codebase_hints, the lite stack is fully alive.

**Native macOS sidecars.** Two services do *not* run in containers on macOS — they live as `launchd` agents so they can reach the host filesystem and Metal framework directly:

```bash
cp scripts/launchd/io.contextdna.event-bridge.plist ~/Library/LaunchAgents/
cp scripts/launchd/com.contextdna.llm.plist          ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.contextdna.event-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.contextdna.llm.plist
```

These provide the MLX-LLM proxy on `:5044` and the event bridge socket. Lite degrades gracefully if they are absent — the webhook still serves — but the local LLM subconscious is dark until the MLX agent boots.

---

## Heavy mode (home server / Mac Studio)

Heavy is the full stack: Postgres at full size, RabbitMQ + Celery workers, Ollama, the NATS chief other nodes leaf into, Prometheus + Grafana + Jaeger. Aimed at a home server, a Mac Studio in a closet, or a cloud VM with at least 6 GB RAM and a few sustained CPU cores.

```bash
# 1. Clone with submodules. Same command, different machine class.
git clone --recurse-submodules git@github.com:supportersimulator/contextdna-ide.git
cd contextdna-ide

# 2. Populate .env. Heavy requires the same four secrets as lite PLUS
#    RABBITMQ_PASSWORD and GRAFANA_PASSWORD. NATS_REMOTE_URL is unused
#    on a chief (the chief listens, leaves connect to it).
cp .env.example .env
$EDITOR .env

# 3. Bring up the heavy stack. First boot pulls Ollama models — expect
#    5–15 min on a fast connection.
docker compose -f docker-compose.heavy.yml up -d

# 4. Watch it converge. Healthchecks settle in this order:
#    postgres -> redis -> rabbitmq -> nats -> ollama -> helper-agent -> celery
docker compose -f docker-compose.heavy.yml ps

# 5. Same verification probe as lite — helper agent /consult on :8080.
curl -sS -X POST -G \
    --data-urlencode "prompt=heavy node smoke" \
    --data-urlencode "risk_level=moderate" \
    http://127.0.0.1:8080/consult | jq .
```

The heavy stack also exposes Grafana at `:3001` (login with `GRAFANA_USER` / `GRAFANA_PASSWORD`), Jaeger at `:16686`, and Prometheus at `:9090`. None are required for the webhook to serve; they are visibility tooling for the operator.

---

## Bidirectional sync (heavy + lite together)

A heavy node and any number of lite nodes form a single brain by means of one mechanism: a **NATS leaf-node link** plus **JetStream KV** that replicates the `fleet_roster` and `evidence_ledger` buckets across the link.

On the heavy node, the chief NATS server listens on `:7422` for leaf connections (set in `docker-compose.heavy.yml`). On the lite node, the `nats-leaf` container connects out:

```bash
# On the lite laptop, before `docker compose up`:
$EDITOR .env
# Set:
#   NATS_REMOTE_URL=nats-leaf://heavy.lan:7422
#   NATS_LEAF_USER=leaf
#   NATS_LEAF_PASSWORD=<matches chief's leaf credential>
```

That alone is enough. Once the leaf link is up:

* JetStream KV `fleet_roster` becomes a single keyspace across the leaf and chief — write on either side, read on both.
* Subjects `fleet.heavy.learnings.>` flow downstream to the lite helper agent.
* Subjects `fleet.lite.<node_id>.worklog` flow upstream from each laptop into the chief's evidence ledger.
* Redis pub/sub remains intra-machine — leaf nodes have their own Redis.

If the laptop goes offline (café WiFi dies), the leaf disconnects cleanly, JetStream queues messages locally, and resync happens on reconnect. Last-write-wins on KV; evidence merges by claim_id on the ledger. See `docs/sync/bidirectional.md` for the conflict-resolution invariants.

---

## Verification

The whole chain — clone, env, compose up, healthchecks, webhook probe — is wrapped in a single script. Run it from any directory; it picks a sacrificial `$TARGET` and tears down or leaves the stack running depending on the flag.

```bash
# Lite mode, default target (/tmp/mothership-verify-$(date +%s)), interactive
# prompt for secrets after .env.example is copied.
bash scripts/bootstrap-verify.sh --profile lite

# Lite, automated (no TTY) — pass a pre-populated env file.
bash scripts/bootstrap-verify.sh \
    --profile lite \
    --env-file ~/secrets/contextdna-ide.env \
    --target /tmp/verify-lite

# Heavy, with full teardown when done.
bash scripts/bootstrap-verify.sh \
    --profile heavy \
    --env-file ~/secrets/contextdna-ide.env \
    --teardown

# Re-run step 3+ on an existing checkout (skip clone).
bash scripts/bootstrap-verify.sh --no-clone --target /tmp/verify-lite
```

A successful run ends with `BOOTSTRAP-VERIFIED` printed to STDOUT and a final stats block on STDERR. Anything else is a real failure — the script exits non-zero and names which step broke.

---

## What's preserved across the wipe-and-clone

Three layers of state survive a total laptop loss because they live somewhere other than the laptop:

| Layer | Lives in | Survives because |
|-------|----------|------------------|
| **Evidence ledger** | Postgres on the heavy chief (`evidence_ledger`, `claims`, `outcomes` tables) | Postgres on heavy is the canonical store; lite Postgres is a cache that rebuilds from JetStream on first leaf reconnect. |
| **Pattern library** | `memory/.brain/` files committed to the mothership repo + `memory/*.md` learnings + `gold_passes` table mirrored over JetStream | Committed files are restored by `git clone`. Live SOPs are mirrored over NATS KV `fleet_roster` and replayed when a fresh lite leaf comes online. |
| **Session history** | JetStream KV bucket `fleet_roster` + Postgres `session_archives` on heavy | Heavy retains 90+ days of session archives; lite asks chief on reconnect via `session_historian.get_structured_rehydration`. |

The contract is simple: **anything the AI needs to "remember" is either committed to the repo or replicated to the heavy chief.** Nothing important lives only on a laptop.

---

## What's NOT in the clone

A few things deliberately do not ship in `git clone`. The clone is the *recipe*, not the *kitchen*:

* **Real secrets.** `.env` is in `.gitignore`. You keep your own offline. The repo ships `.env.example` with every variable named, commented, and given safe defaults where possible.
* **Local cache directories.** `~/.context-dna/`, `~/.cache/contextdna/`, and the running container volumes (`postgres_lite_data`, `redis_lite_data`, etc.) are machine-local. They rebuild from the heavy chief on first sync.
* **In-flight work.** Anything written in the last few seconds before a laptop dies and not yet flushed to JetStream is lost. JetStream's flush cadence is configured for "seconds, not minutes" — but it is not zero.
* **The MLX model weights.** Qwen3-4B (~3 GB) is downloaded by `com.contextdna.llm.plist` on first launch. Network and time, not git, deliver the weights.
* **OS-level secrets in Keychain.** Any credential you stash in macOS Keychain stays on the machine that minted it. Treat the offline `.env` backup as the single source for secrets that must survive.

---

## Troubleshooting

A short list, in order of how often each one bites in practice:

* **Docker port conflict on `:5432` / `:6379` / `:8080`.** Another local Postgres / Redis / Python service is already bound. Override via `.env`: `POSTGRES_PORT=15432`, `REDIS_PORT=16379`, `HELPER_AGENT_PORT=18080`. The webhook probe in `bootstrap-verify.sh` reads `WEBHOOK_PORT` env var to follow your override.
* **NATS leaf certificate expired (heavy + lite sync).** Symptom: `nats-leaf` container restarts every 30s, leaf logs show `tls: certificate expired`. Fix: regenerate the leaf creds on the chief (`nsc edit user leaf --expiry 1y`) and copy the new `.creds` file to `nats/leaf.creds` on every lite laptop.
* **Postgres init race on first boot.** Healthcheck flips to healthy before the migrations apply, helper agent tries to connect and gets `relation "claims" does not exist`. Fix: wait 15s after `docker compose up -d`, then `docker compose restart helper-agent`. The retry is enough — second boot finds the migrated schema.
* **`docker compose` not found, only `docker-compose`.** You have the legacy v1 binary. Install the v2 plugin (`brew install docker-compose` on macOS or follow Docker's official guide). The compose files use v2 syntax (no top-level `version:` key).
* **Helper agent stays "unhealthy" forever.** Tail the logs: `docker compose -f docker-compose.lite.yml logs -f helper-agent`. The most common cause is the MLX server on the host not being up — the helper agent retries forever but logs `Connection refused 5044`. Boot the launchd agent (`launchctl kickstart -k gui/$(id -u)/com.contextdna.llm`) and the helper recovers within 30s.
* **Webhook returns 200 but `layers` is empty.** The helper agent is up but the underlying brain/professor modules are not loaded. Almost always a `PYTHONPATH` mismatch in the container — confirm `PYTHONPATH=/app` and that `/app/memory/` is mounted via the compose volume.
* **`git clone` fails on submodules.** SSH key on the machine cannot reach all submodule remotes (often `landing-page` or `3-surgeons`). Test each remote with `git ls-remote`; add deploy keys or use a personal token via HTTPS clone URL as a fallback.

If a failure mode is reproducible and isn't on this list, add it to `docs/sync/bidirectional.md` (sync failures) or this file (bootstrap failures) and open a PR. Every unfixed surprise is a future Aaron paying interest on yesterday's silence.

---

## Why this document is the credibility moment

The mothership is a claim. Claims need evidence. This document and its companion `bootstrap-verify.sh` *are* the evidence — they are the experiment any reader can run to falsify the Operational Invariance Promise. Run it on a fresh Linux VM with no prior contextdna footprint, on a brand-new MacBook out of the box, on a friend's laptop. If `BOOTSTRAP-VERIFIED` prints, the promise holds. If it doesn't, the bug is in the mothership, not in the user, and it gets fixed at the source.

The repo is the manifesto. The script is the proof.
