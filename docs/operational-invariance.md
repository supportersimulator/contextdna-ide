# Operational Invariance — The Mothership Contract

> The litmus test. If you delete everything locally AND delete everything on AWS, a fresh `git clone --recurse-submodules` followed by `docker compose up` must produce a working persistent-memory system. Anything less is not mothership-grade.

This document is the contract. It defines what survives total wipe, what does not, and how every commit to `main` proves the contract still holds.

---

## 1. The Promise

The mothership is not "another AI tool." It is a **personal continuity layer** — an evidence-backed memory ledger, a quality-cross-examination protocol, and a cross-machine transport, all welded into one repo that boots from nothing. The promise is operational, not aspirational: the framework — schema, services, wiring, verification — is fully recoverable from `git clone` alone. Wisdom accumulated through use is recoverable from any surviving node (laptop, home server, S3 snapshot). Lose all of those simultaneously and you lose accumulated wisdom; you do not lose the *means of accumulating wisdom*.

That distinction is what separates this from a chat app whose servers can go dark, a SaaS whose vendor can pivot, a notebook whose file can corrupt. The mothership defines invariance the way a constitutional system does: not "this will never happen," but "if everything burns, here is what reconstitutes itself from text." Every architectural choice in this repo serves that property. When you deploy the mothership you are not trusting Anthropic, OpenAI, AWS, or me — you are trusting your own ability to `git clone` and run `docker compose up`. That trust is auditable. It is verified in CI on every commit.

---

## 2. What's Preserved Across Total Wipe

These artifacts survive `rm -rf /` on the laptop and full AWS account deletion. They live in git, which lives in GitHub (and any clone you have), and reconstitute the framework end-to-end.

### 2.1 Source code

Everything under `src/`, `memory/`, `apps/ide-shell/`, the panel substrate, the webhook host, the helper-agent skills. Committed to `github.com/supportersimulator/contextdna-ide` on `main`. No source is held in private mirrors, encrypted enclaves, or vendor-only registries.

### 2.2 Configuration templates

- `.env.example` — every required variable, documented inline with what it does and where to acquire the secret. The shape of the runtime is fully specified; only the values are absent.
- `docker-compose.lite.yml` and `docker-compose.heavy.yml` — declarative service graphs. Image tags pinned by digest, not floating tags. Reproducing the runtime is `docker compose up`, never "run these 14 manual commands first."
- `profiles/lite.yaml` and `profiles/heavy.yaml` — service-level tuning (Postgres `shared_buffers`, NATS JetStream replica count, MCP server lists).

### 2.3 Schema migrations

`migrations/` carries every forward and reverse migration as numbered SQL. Postgres bootstraps to current schema from an empty volume. Migration runner is `scripts/migrate.sh`; the helper agent will not start until migrations report up-to-date.

### 2.4 Pattern library — the wisdom seed

`memory/professor_seed_patterns.json` ships **committed**. It is the cold-start wisdom kernel: a curated set of `Claim` and `PromotedWisdom` records that bootstrap the evidence ledger so a fresh node has *something* in its S2 payload before any session has happened. On first boot, `scripts/seed-patterns.sh` ingests the file and writes typed records into Postgres with `source = "seed"` and `confidence = 0.6`. The live evidence ledger — every `RawEvent`, `Outcome`, accumulated `PromotedWisdom`, every retirement — is **not** committed. That is runtime state; see §3.2.

**Decision worth revisiting:** the seed is a useful default but it encodes opinions (Aaron's patterns, ZSF discipline, ER simulator landmines, fleet warnings). If those opinions are personal, the seed belongs gitignored with a checked-in `.template.json`. The current default ships opinionated seeds because the OSS audience benefits from a working brain on first boot.

### 2.5 Submodule pointers

`.gitmodules` and the index entries pin the exact commit SHAs of `context-dna`, `3-surgeons`, `multi-fleet`, `superpowers`, and `admin.contextdna.io`. `git clone --recurse-submodules` reconstructs the exact dependency tree. No "latest" tags. No floating refs.

### 2.6 Bootstrap and verification scripts

- `scripts/bootstrap-from-scratch.sh` — runs `git submodule update`, copies `.env.example` → `.env` (failing loudly on missing required vars), runs migrations, brings up the compose stack, waits for `/health` to return green on all services.
- `scripts/bootstrap-verify.sh` — the CI harness described in §6. The thing that proves the contract holds.
- `scripts/seed-patterns.sh`, `scripts/migrate.sh`, `scripts/healthcheck.sh` — the supporting tools each script delegates to.

### 2.7 Documentation

- `MOTHERSHIP-README.md` — the manifesto.
- `MOTHERSHIP-ARCHITECTURE.md` — the schematic.
- `docs/panels/manifest-spec.md` — the universal panel manifest schema.
- `docs/sync/bidirectional.md` — Lite ↔ Heavy reconciliation.
- `docs/operational-invariance.md` — this document, which is itself part of the contract it describes.

---

## 3. What's NOT Preserved

These do not live in git, do not live in the repo, and must be re-acquired or restored from offline backup. Shipping any of these in the repo would defeat the security model.

### 3.1 Real secrets

`.env` is gitignored. Aaron keeps an encrypted backup offline — choose any one of:

- **1Password vault** (recommended; cross-device sync, recovery key)
- **iCloud Keychain** with a long passphrase
- **`age`-encrypted file** on a USB key in a fire-safe (`age --encrypt -r ssh-ed25519...`)

The mothership cannot ship secrets. It can only ship the *shape* of secrets (`.env.example`). On bootstrap-from-zero, the operator must restore `.env` from one of the offline channels above.

### 3.2 Runtime state — the live evidence ledger

The Postgres rows in `evidence_ledger`, `claims`, `outcomes`, `promoted_wisdom`, `retired_wisdom`, the Redis cache, NATS in-flight messages, the Prometheus time-series, the Jaeger traces — none of these are in the repo. They are *accumulated through use*. The seed patterns (§2.4) give you a non-empty starting point but the *learned* wisdom only exists on machines that have done sessions.

This is intentional. Committing accumulated wisdom would (a) leak personal context, (b) bloat the repo, (c) confuse "framework" with "instance." Backups of accumulated wisdom are runtime backups — see §5 and `infra/backup/`.

### 3.3 API keys to powerhouse providers

OpenAI, Anthropic, DeepSeek, Mistral, any other model API keys. Aaron re-acquires from the respective dashboards. The mothership uses these keys; it does not own them. The `.env.example` documents which keys are required for which providers and which features degrade if a given provider is unavailable (e.g. 3-Surgeons falls back to single-surgeon mode if only one provider is reachable).

### 3.4 TLS certificates

Let's Encrypt certs are auto-renewed by Traefik on first boot. The `acme.json` cert store is gitignored and Docker-volume-mounted; on a fresh node Traefik re-acquires certs from Let's Encrypt automatically. No manual cert restoration required.

---

## 4. Recovery Scenarios

These are worked examples of the contract in action. Each one has been walked through end-to-end.

### 4.1 Total laptop loss

> Scenario: laptop stolen, drowned, melted. New MacBook arrives.

1. `git clone --recurse-submodules https://github.com/supportersimulator/contextdna-ide ~/dev/contextdna-ide`
2. Restore `.env` from offline backup (1Password / iCloud / `age` file)
3. `cd ~/dev/contextdna-ide && docker compose -f docker-compose.lite.yml up -d`
4. `scripts/healthcheck.sh` — waits for all services to report green
5. ~5 minutes later: evidence ledger reinitialized from seed patterns, lite mode operational, MCP webhook serving 9-section payloads, helper agent on `/health` returns `ready`.

If a Heavy node (home server) is reachable, the lite node opens a NATS leaf-node link and backfills accumulated evidence from the heavy ledger. The laptop is back to *yesterday's wisdom*, not just *seed wisdom*. The leaf-node mechanism is documented in `docs/plans/2026-04-23-nats-leaf-node-plan.md`.

### 4.2 AWS account loss

> Scenario: AWS account suspended, deleted, or compromised.

Heavy mode does not have to run on AWS. If it runs locally (Mac Studio, home server), AWS loss is irrelevant. If heavy mode *did* run on AWS:

1. `cd infra/aws && terraform apply` — redeploys the heavy-mode topology (Postgres RDS, NATS cluster, MCP host, monitoring stack) into a new AWS account.
2. Restore the latest S3 evidence-ledger snapshot from the backup bucket (per §5) or from a local heavy node if one survived.
3. Bring up the redeployed compose stack; the helper agent boots, NATS resyncs from JetStream KV, and the heavy node rejoins the fleet.

The mothership infrastructure is **declarative**. Terraform manifests live in `infra/aws/`. Nothing about the heavy topology requires clicking through the AWS console. Anything that does is a bug in the manifests.

### 4.3 Both lost simultaneously

> Scenario: laptop wipe AND AWS account deletion in the same incident.

Worst case. Aaron loses *accumulated* wisdom — the patterns the system promoted over months of sessions, the retired claims, the per-codebase landmines. He does not lose:

- The framework (source code, schema, services, wiring)
- The seed patterns (the cold-start wisdom kernel)
- The architecture (every design decision, every invariant)
- The means of re-accumulating wisdom (every session henceforth re-fills the ledger)

Recovery: clone the repo on any machine, restore `.env`, `docker compose up`. The system is *operationally* whole within minutes; *epistemically* it has lost months of learned distinctions. Pattern promotion restarts from the seed. The framework is intact. The mothership claim is intact. What is gone is the personal history — which is exactly what should be gone if a personal history is what was deleted.

If §5 backups are in place, even the personal history is recoverable.

---

## 5. Bidirectional Sync as Continuity Insurance

Running Lite (laptop) and Heavy (home server) **concurrently** is the mothership's first line of defense against accumulated-wisdom loss. NATS JetStream replicates three streams across both nodes:

- `fleet_roster` (KV) — node liveness, capabilities, last-seen
- `evidence_ledger` (stream) — every `RawEvent`, `Claim`, `Evidence`, `Outcome`, promotion, retirement
- `injection_signal` (KV + subject) — "new InjectionCandidate available for session X"

Losing the laptop is recoverable from the home server. Losing the home server is recoverable from the laptop. The leaf-node link does the reconciliation automatically when the lost node returns.

Losing **both** requires explicit backup. The mothership ships:

- `infra/backup/pg-dump.sh` — daily `pg_dump` of the evidence ledger to S3 (or B2, or any S3-compatible target — configured in `.env`). Encrypted at rest with a key in `.env`, retention 90 days.
- `infra/backup/jetstream-snapshot.sh` — weekly JetStream KV snapshot to the same bucket.
- `infra/backup/restore.sh` — symmetric restore from a chosen snapshot timestamp.

If `infra/backup/` is properly configured, the §4.3 worst case becomes: clone repo, restore `.env`, restore latest S3 snapshot, `docker compose up`. Accumulated wisdom returns alongside the framework.

---

## 6. CI Gate: `bootstrap-verify`

Every commit to `main` runs `scripts/bootstrap-verify.sh` inside a clean Docker-in-Docker environment. The workflow at `.github/workflows/bootstrap-verify.yml` enforces this. The workflow is non-skippable on `main`.

**The gate's contract:**

1. Spin up a fresh Ubuntu-latest runner.
2. Install Docker, Docker Compose, and Python 3.12 — nothing else.
3. `git clone --recurse-submodules` the commit under test into a clean working directory (no caches, no volumes, no `.env`).
4. Copy `.env.example` → `.env`, substitute fixture values for required secrets (test-only keys with no real-provider access).
5. Run `scripts/bootstrap-from-scratch.sh`.
6. Run `scripts/bootstrap-verify.sh` — the assertion harness described in §8.
7. Tear down. Report pass/fail.

**Failure = the mothership is no longer mothership-grade and must not merge.** There is no override. There is no "fix in follow-up PR." A failed `bootstrap-verify` means the contract in §1 no longer holds, which means the README's central claim — "delete everything, clone, run" — is a lie. The fix is at the root, in the same PR that broke it.

---

## 7. Manual Quarterly Verification

CI proves the contract on every commit but CI runs on Ubuntu in a VM. Aaron runs a **quarterly manual verification** on a fresh machine that has never seen the mothership: a borrowed laptop, a clean cloud VM, a wiped Mac.

**The habit:** every 3 months, on a calendar reminder, perform the §4.1 recovery from cold metal — no shell aliases, no `~/.zshrc` shortcuts, no cached Docker images. Verify the end-to-end path works exactly as documented. Note every place where reality and documentation diverge. Fix in the same session.

This is captured in a runbook at `docs/runbooks/quarterly-bootstrap-verification.md` (to be created with the first quarterly run). The runbook records: hardware used, exact commands run, time-to-green, any deviations from documented behavior, any documentation patches required.

The reason CI alone is not enough: CI tests the path the test author *knew about*. Quarterly manual verification tests the path the operator *actually walks* on a worst day, which always reveals gaps the test author missed.

---

## 8. What the Test Suite Covers

`scripts/bootstrap-verify.sh` is the executable form of the §1 promise. It asserts:

### 8.1 Container health

All services in the active compose profile reach `healthy` status within `BOOTSTRAP_VERIFY_TIMEOUT_S` (default 300 s). The assertion uses `docker compose ps --format json` and filters on `Health.Status`. Any container in `unhealthy` or `starting` past the timeout fails the gate.

### 8.2 MCP webhook returns the 9-section payload

`curl -X POST http://127.0.0.1:8888/webhook` with a fixture prompt returns a `ContextPayload` (the typed schema from `MOTHERSHIP-ARCHITECTURE.md` §3) where all of `s0_safety_rails` through `s8_eighth_intelligence` are present and non-null. The fixture prompt exceeds the ≤5-word gate. Each section's `meta.actual_tokens > 0`. The harness validates against the Pydantic schema; an invalid payload fails the gate.

### 8.3 Postgres schema migrations applied

`scripts/migrate.sh status` returns exit 0 and reports `up-to-date`. The harness also runs a direct SQL probe: `SELECT count(*) FROM claims` and `SELECT count(*) FROM promoted_wisdom` both succeed (return values are not asserted — the assertion is *the tables exist and are queryable*). Seed-pattern row count matches the source file's row count (no silent ingestion loss).

### 8.4 NATS reachable

`nats-cli rtt` against `nats://127.0.0.1:4222` returns under 100 ms. JetStream is enabled (`nats stream ls` returns at least the three core streams: `fleet_roster`, `evidence_ledger`, `injection_signal`). If the active profile is `heavy`, replica count is 3.

### 8.5 Helper agent reports `ready` on `/health`

`curl -sf http://127.0.0.1:8855/health` returns HTTP 200 with a JSON body containing `{"status": "ready"}` and all subsystem fields (`webhook`, `nats`, `postgres`, `mcp_servers`) reporting `ok`. Any field reporting `degraded` fails the gate; the gate distinguishes `degraded` from `down` only for non-required subsystems documented in `profiles/<profile>.yaml`.

### 8.6 Counters move

The harness submits a second fixture prompt and asserts at least one counter in `counters_snapshot` advances between the two payloads. This catches the "service is up but evidence flow is broken" silent failure — Zero Silent Failures applies to the verification harness itself.

---

**This document is the contract. The CI gate is its enforcement. The quarterly manual verification is its insurance. Together they make `delete everything, clone, run` a fact rather than a hope.**
