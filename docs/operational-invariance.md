# Operational Invariance — The Mothership Contract

> The litmus test. If you delete everything locally AND delete everything on AWS, a fresh `git clone --recurse-submodules` followed by `docker compose up` must produce a working persistent-memory system. Anything less is not mothership-grade.

This document is the contract. It defines what survives total wipe, what does not, and how every commit to `main` proves the contract still holds.

**v2 addendum (sections 9–14):** Operational invariance is *not* "clone today and run forever." It is **"clone any commit and rebuild deterministically."** The mothership is a *living* system — Docker base images shift, Python deps publish CVEs, NATS upgrades majors, Postgres rolls 16 → 17. Sections 9–14 specify how the contract survives weekly motion in the dependency graph without losing its constitutional shape.

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
- `profiles/lite.yaml` and `profiles/heavy.yaml` — service-level tuning (Postgres `shared_buffers`, NATS JetStream replica count, MCP server lists). Inheritance and override resolution per §10.4.

### 2.3 Schema migrations

`migrations/` carries every forward and reverse migration as numbered SQL. Postgres bootstraps to current schema from an empty volume. Migration runner is `scripts/migrate.sh`; the helper agent will not start until migrations report up-to-date. The migration-manifest contract — what every migration must guarantee — is documented in §12.

### 2.4 Pattern library — the wisdom seed

`memory/professor_seed_patterns.json` ships **committed**. It is the cold-start wisdom kernel: a curated set of `Claim` and `PromotedWisdom` records that bootstrap the evidence ledger so a fresh node has *something* in its S2 payload before any session has happened. On first boot, `scripts/seed-patterns.sh` ingests the file and writes typed records into Postgres with `source = "seed"` and `confidence = 0.6`. The live evidence ledger — every `RawEvent`, `Outcome`, accumulated `PromotedWisdom`, every retirement — is **not** committed. That is runtime state; see §3.2.

**Decision worth revisiting:** the seed is a useful default but it encodes opinions (Aaron's patterns, ZSF discipline, ER simulator landmines, fleet warnings). If those opinions are personal, the seed belongs gitignored with a checked-in `.template.json`. The current default ships opinionated seeds because the OSS audience benefits from a working brain on first boot.

### 2.5 Submodule pointers

`.gitmodules` and the index entries pin the exact commit SHAs of `context-dna`, `3-surgeons`, `multi-fleet`, `superpowers`, and `admin.contextdna.io`. `git clone --recurse-submodules` reconstructs the exact dependency tree. No "latest" tags. No floating refs.

**v2 update:** the migrate2 lineage ships `.gitmodules.template` (URLs may be overridden by env, CLI flag, or interactive prompt — never silently defaulted) plus a sibling `.gitmodules.lock` recording the resolved URL **and** the pinned commit SHA. See `scripts/wire-submodules.sh --pin-current` and §10.3.

### 2.6 Bootstrap and verification scripts

- `scripts/bootstrap-from-scratch.sh` — runs `git submodule update`, copies `.env.example` → `.env` (failing loudly on missing required vars), runs migrations, brings up the compose stack, waits for `/health` to return green on all services.
- `scripts/bootstrap-verify.sh` — the CI harness described in §6. The thing that proves the contract holds.
- `scripts/seed-patterns.sh`, `scripts/migrate.sh`, `scripts/healthcheck.sh` — the supporting tools each script delegates to.
- `scripts/wire-submodules.sh` — the dynamic-resolution submodule wirer (§10.3).

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

## 9. The Living Infrastructure Principle

The mothership is **not a frozen snapshot.** It is a living organism whose dependency graph evolves weekly. Treating it as static is the single most common way operators break the §1 promise on themselves.

### 9.1 What "living" means

These things move — sometimes daily, mostly weekly, always without asking permission:

- **Python packages** publish patch releases for CVEs, transitive deps drop maintainers, breaking-change minors slip out without semver discipline.
- **Docker base images** (`python:3.11-slim`, `postgres:16`, `nats:2.10`) re-roll their `latest` tag on every upstream rebuild. The same tag name resolves to a different digest week to week.
- **NATS server** ships JetStream protocol revisions; minor versions occasionally change wire-format behavior under partition.
- **Postgres** rolls a major every October; the on-disk format changes; `pg_upgrade` is required.
- **OS distros** under those base images publish glibc security fixes; the resulting image is functionally identical but cryptographically different.

A naive snapshot taken today and shipped to a user six months from now will encounter a different world than the one it was built in. Pretending otherwise is how lock-in starts.

### 9.2 Restated invariance: "any commit, deterministically"

The contract in §1 — "delete everything, clone, run" — is implicitly time-indexed. The full statement is:

> **For every commit `C` on `main`, a `git clone --recurse-submodules` at `C` followed by `docker compose up` MUST produce a working persistent-memory system on any machine with Docker, irrespective of the wall-clock date the clone is performed.**

That property is **deterministic rebuild from any commit**, not "freeze the dependency graph forever." Each commit carries its own dependency-graph snapshot — pinned by digest, by SHA, by lock file. A commit from six months ago rebuilds bit-for-bit to the system it represented six months ago. A commit from today rebuilds to today's system. The framework moves forward; every commit remains a complete time capsule.

### 9.3 What this means for §2

Everything in §2 ("What's Preserved Across Total Wipe") is preserved **relative to the commit being cloned.** The seed patterns, the schema migrations, the submodule SHAs, the compose digests — these all reference the world as it existed at that commit. They are not promises about *today's* upstream registries. They are promises about *the rebuild is deterministic from the data in this commit*.

When an upstream package is unpublished, when a Docker registry deletes an old digest, when a submodule repo is force-pushed — those are external events that erode determinism for commits older than that event. §11 ("The Evolution Cadence") and §10.3 ("Vendoring fallback") describe how the mothership defends against that erosion.

---

## 10. Dependency Graph Continuity

The dependency graph is the mothership's surface area against the outside world. Continuity means: at any point in the lifecycle of any commit, the graph can be re-walked and the system rebuilt. This section specifies exactly how each layer of the graph stays walkable.

### 10.1 `pyproject.toml` — major-pin, minor-float

The `pyproject.toml` pins **major versions only.** Minor and patch versions float within a `~=` range so security fixes flow in without manual intervention. Examples:

```toml
[project]
dependencies = [
    "pydantic~=2.0",        # accepts 2.x.y, blocks 3.0
    "fastapi~=0.110",       # 0.110.x patches only (FastAPI pre-1.0 convention)
    "psycopg[binary]~=3.1", # accepts 3.1.x patches, blocks 3.2 minor
    "nats-py~=2.6",         # NATS minor reflects protocol; pin to known-good minor
    "redis~=5.0",           # major-pinned; minors are safe per maintainer policy
]
```

Pre-1.0 packages get **minor pins** because semver convention treats minor as breaking pre-1.0. Post-1.0 stable packages get **major pins.** Any package without a clear semver policy (NATS server protocol, Postgres major) gets pinned tighter — see §10.3.

The reproducible-build lock is `requirements.lock` (`uv pip compile pyproject.toml -o requirements.lock`). The lock file is committed. CI runs `uv pip sync requirements.lock` so the active environment is byte-identical to the resolution at commit time. `pyproject.toml` describes intent; `requirements.lock` describes truth.

### 10.2 Automated weekly dependency update — Dependabot + Renovate

Two automations run concurrently to widen coverage:

- **`.github/dependabot.yml`** — handles Python deps and Docker base-image tags. Opens a PR per ecosystem per week (Mondays UTC). PRs target `main` and are auto-mergeable if `bootstrap-verify` is green.
- **`.github/renovate.json`** — handles submodule SHA bumps, NATS server, Postgres major, anything Dependabot doesn't natively understand. Same cadence.

Sketch of `.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    groups:
      patch-and-minor:
        update-types: ["patch", "minor"]
  - package-ecosystem: "docker"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

Sketch of `.github/renovate.json`:

```json
{
  "extends": ["config:recommended"],
  "schedule": ["before 6am on monday"],
  "git-submodules": { "enabled": true },
  "packageRules": [
    { "matchUpdateTypes": ["major"], "addLabels": ["needs-3-surgeon-review"] }
  ],
  "vulnerabilityAlerts": { "enabled": true, "labels": ["security"] }
}
```

Major bumps **never** auto-merge. They land a labeled PR and the 3-Surgeon `architectural-gate` skill runs against the diff. Patch and minor merges autopilot if `bootstrap-verify` is green.

### 10.3 Docker base images — date-stamped tags + digest pinning

`docker-compose.lite.yml` and `docker-compose.heavy.yml` reference base images by **date-stamped tag** with **digest pinning**:

```yaml
services:
  postgres:
    image: postgres:16.4-bookworm@sha256:abc123...   # date implicit in 16.4 release
    # NOT: image: postgres:16          (floats inside major)
    # NOT: image: postgres:latest      (floats freely — see §14.2)

  app:
    image: ghcr.io/supportersimulator/contextdna-ide:python-3.11-slim-2026.01@sha256:def456...
    # Custom-built base, retagged with a YYYY.MM stamp so the meaning of the tag
    # is permanent. New month → new tag → new digest. Old tags never get rewritten.
```

The convention is **`<upstream-tag>-<YYYY.MM>@sha256:<digest>`** for every image where the upstream tag is mutable (anything not already a release-version tag like `postgres:16.4`). Renovate rewrites the tag and digest weekly; old tags remain queryable in our private registry for the retention window (12 months, configurable in `infra/registry/retention.yaml`).

**Vendoring fallback.** For any base image not under our control, `scripts/vendor-base-images.sh` runs nightly and re-tags the current digest into our own registry under `ghcr.io/supportersimulator/vendored/<image>:<digest-short>`. This protects against upstream deletion. Commits older than 12 months can be rebuilt by pointing the compose file at the vendored copy — see the runbook at `docs/runbooks/rebuilding-old-commits.md`.

### 10.4 Submodule pinning — `.gitmodules.lock`

Sibling-agent deliverable (Agent 1): `scripts/wire-submodules.sh` resolves submodule URLs through a four-tier chain (CLI flag → env var → `.gitmodules.lock` → interactive TTY → loud failure; **no silent defaults**) and records every resolved (URL, SHA) pair into `.gitmodules.lock`. Together with the checked-in `.gitmodules.template`, this lets the mothership ship without hardcoded private URLs while still rebuilding deterministically.

The `--pin-current` flag captures the *currently-checked-out* SHA of every submodule back into the lock file. CI runs this on every merge to `main`; the lock file therefore always reflects the state of `main` and is the source of truth for clone-time submodule resolution. See `scripts/wire-submodules.sh` for the implementation contract; see §12 for how schema migrations across submodule bumps are sequenced.

### 10.5 Profile inheritance and override resolution

Sibling-agent deliverable (Agent 2): `profiles/<name>.yaml` files participate in a documented inheritance graph (`extends: <parent>`) with deterministic override resolution. `profiles/base.yaml` declares the universal defaults; `profiles/lite.yaml` and `profiles/heavy.yaml` extend it; per-operator overlays may extend any of the three. The resolver is a pure function over the inheritance chain — given the same chain it produces the same effective profile, byte-identical.

Why this belongs in operational invariance: if profile resolution is implicit, an upgrade that adds a new field can silently break a downstream operator's overlay. Making inheritance explicit and deterministic means a new field always has a documented default, and the override resolver fails loudly on conflicting keys rather than picking a winner by file-load order.

### 10.6 Schema versioning across boundaries

Sibling-agent deliverable (Agent 3): every persistent schema — panel manifest, evidence record, profile, env shape — carries a `$schema_version` field and a sibling `schemas/<name>/v<n>.json` JSON Schema. Migrations between versions are first-class artifacts (§12). A consumer that does not understand a schema version refuses to parse it; it does not silently accept and lose fields.

Schema versioning is what makes the dependency graph **internally** consistent the same way Docker digests make it **externally** consistent. Across an upgrade, every record in the evidence ledger declares its own schema version; the helper agent migrates lazily on read for cold rows and eagerly via `scripts/migrate.sh` for hot tables.

### 10.7 Automatic migration on boot — never manual

`scripts/migrate.sh` runs unconditionally as the first step of `docker compose up`'s `app` container entrypoint. There is **no operator step** called "run the migrations." If a migration fails, the container fails to start, the health check stays red, and `bootstrap-verify` fails. The system does not silently boot in a half-migrated state.

This includes schema migrations (Postgres SQL), record migrations (re-encoding old evidence rows into new schema versions), and profile migrations (re-resolving overlays against a new base). All three reuse the same manifest format described in §12.

---

## 11. The Evolution Cadence

The mothership has a heartbeat. Three cadences keep the dependency graph from drifting into staleness.

### 11.1 Weekly — Friday dependency-update PR

Every Friday (UTC), the Dependabot + Renovate PRs from the week are reviewed as a batch. The template is `.github/PULL_REQUEST_TEMPLATE/weekly-deps.md`:

```markdown
## Weekly Dependency Update — YYYY-WW

### Auto-merged (green CI)
<list of patch/minor PRs that auto-merged>

### Requires review
<list of major bumps and security advisories>

### Skipped (deferred to next week)
<list with one-line reason per item>

### Verification
- [ ] `bootstrap-verify` green on every merged PR
- [ ] No new CVEs in `requirements.lock`
- [ ] No new `latest` tags introduced (grep `:latest` returns empty)
- [ ] Submodule SHAs in `.gitmodules.lock` advanced where applicable

### Notes
<any operator-visible behavior changes from this week's bumps>
```

The Friday review is a 30-minute task. If it is taking longer than that consistently, the cadence is failing and something upstream is exploding — file a `docs/postmortems/<date>-weekly-deps-stress.md` and adjust automation.

### 11.2 Monthly — architecture-drift review

The README, `MOTHERSHIP-ARCHITECTURE.md`, and this document make claims about the system. On the first Monday of each month, those claims are mechanically checked against the actual code:

- Every documented service exists in `docker-compose.*.yml`.
- Every documented port matches actual binding in compose + healthcheck script.
- Every documented `/health` field matches the helper-agent response schema.
- Every "section" claim (S0-S8) maps to a present, non-null payload field.
- Every cross-reference in this doc resolves to a file that exists.

The script: `scripts/architecture-drift-check.sh`. The runbook: `docs/runbooks/monthly-architecture-drift.md`. Discrepancies become PRs *that change the docs to match reality* — not the other way around. Reality is authoritative; documentation is downstream of reality and must catch up. (The only exception is when a planned change is in `docs/plans/` — those documents describe a future reality and are excluded from the drift check.)

### 11.3 Quarterly — bootstrap-from-scratch on cold metal

§7 already specifies the quarterly manual verification. The evolution cadence adds: **the quarterly verification specifically tests an old commit, not just `main`.** The runbook now requires:

- Bootstrap `main` (validates the present).
- Bootstrap `main~13~weeks` (the commit at the start of the previous quarter; validates that one-quarter-old commits still rebuild).
- Bootstrap `main~52~weeks` (one year ago; validates the registry-vendoring fallback in §10.3 is working).

If any of those three fails, the failure is filed as a critical finding (per `memory/session_gold_passes.py` — see CLAUDE.md "CRITICAL FINDINGS"). One-year-old failure is acceptable if the failure mode is graceful and documented; one-quarter-old failure is not.

---

## 12. Schema Migration Manifest

Every schema that crosses the durability boundary (committed to Postgres, written to NATS KV, persisted on disk, sent over the panel manifest API) has versioned migrations in `schemas/migrations/`.

### 12.1 File layout

```
schemas/
  panel-manifest/
    v1.json
    v2.json
    v3.json
  evidence-record/
    v1.json
    v2.json
  profile/
    v1.json
  env/
    v1.json
  migrations/
    panel-manifest-v1-to-v2.json
    panel-manifest-v2-to-v3.json
    evidence-record-v1-to-v2.json
    profile-v1-to-v2.json      # exists only when needed
    env-v1-to-v2.json          # exists only when needed
```

Each migration file is a manifest, not executable code. Executable migrators read the manifest and apply the transform. This keeps the migration *itself* declarative and reviewable.

### 12.2 Manifest shape

```json
{
  "from": "panel-manifest/v1",
  "to": "panel-manifest/v2",
  "issued_at": "2026-02-14T12:00:00Z",
  "idempotent": true,
  "reversible": true,
  "operations": [
    { "op": "add",    "path": "/sandbox/permissions", "value": [] },
    { "op": "rename", "from": "/panel_id", "path": "/id" },
    { "op": "default","path": "/version", "value": "1.0.0" }
  ],
  "reverse_operations": [
    { "op": "remove", "path": "/sandbox/permissions" },
    { "op": "rename", "from": "/id", "path": "/panel_id" },
    { "op": "remove", "path": "/version" }
  ],
  "verification": {
    "schema_after": "schemas/panel-manifest/v2.json",
    "sample_input": "schemas/migrations/samples/panel-manifest-v1.example.json",
    "expected_output": "schemas/migrations/samples/panel-manifest-v2.example.json"
  }
}
```

### 12.3 The migration contract

Every entry in `schemas/migrations/` must satisfy three properties, each independently checked by `scripts/verify-migrations.sh` (which runs as part of `bootstrap-verify`):

1. **Idempotent.** Applying the migration twice to the same record produces the same result as applying it once. Re-running migrations after a crash is safe.
2. **Reversible.** `reverse_operations` exists and, when applied, restores a sample record to byte-identical pre-migration shape. Reversibility is what makes rollbacks possible without restoring a backup.
3. **Observable.** Every migration emits a `migration.applied` event onto NATS (`event.schema.migration.<schema>.<from>-to-<to>`) and increments a `migrations_applied{schema=..., from=..., to=...}` Prometheus counter. Zero Silent Failures applies: an exception during migration must increment `migrations_failed` and surface on `/health` — never `except: pass`.

### 12.4 Walking the chain at boot

`scripts/migrate.sh` discovers the current schema version of each persisted record class (from a `_schema_version` row in Postgres for tables, from KV metadata for NATS streams) and walks the migration chain forward to the target version declared by the running code. The walk is monotonic: never backward at boot, never skipping intermediate steps. If the chain has a gap (`v1.json` and `v3.json` exist but no `v1-to-v2.json` or `v2-to-v3.json`), boot fails loudly. Gaps in the migration chain are unrecoverable silently and the system refuses to pretend otherwise.

Forward-only at boot, reversible only by explicit operator command: `scripts/migrate.sh rollback --to v<n>` runs the reverse chain, verifies, and exits. This is a recovery tool, not a routine action.

---

## 13. Operator Feedback as Continuity Signal

The mothership does not just propagate its own state to operators; it absorbs feedback from operators back into its evolution cadence. This closes the loop between "what we shipped" and "what works in the field."

### 13.1 Where feedback enters

Sibling-agent deliverable (Agent 4): `services/feedback/handler.py` accepts structured feedback records from running mothership instances. The schema is `schemas/feedback-record/v1.json` and includes: `instance_id` (anonymized), `category` (friction | confusion | bug | praise), `surface` (which panel / which doc / which command), `evidence` (free-form text + optional log snippet), `outcome` (operator-recovered | operator-stuck | unknown).

The handler writes records into the evidence ledger with `source = "operator_feedback"` and `confidence = 1.0` (operator-reported friction is, by definition, ground truth about that operator's experience).

### 13.2 How feedback influences next-version priorities

Operator-feedback records flow through the same pattern-promotion pipeline as session evidence (described in `MOTHERSHIP-ARCHITECTURE.md` §6). When the same `(surface, category)` pair shows up across multiple instances, it gets promoted to a `PromotedWisdom` record with `kind = "deployment_pattern"`. Those records appear in the S6 holistic-pattern section of every subsequent webhook payload.

This is the mothership *learning from its own deployments*. The next-version roadmap is not assembled solely from Aaron's hunches; it includes a feed of "operators on contextdna-ide-oss v0.7.2 are getting stuck at the submodule-resolution step, n=14 instances" — and that signal directly produces a `docs/plans/<date>-fix-submodule-friction.md` next sprint.

### 13.3 Cadence integration

The §11.1 weekly review template gains a new section:

```markdown
### Operator feedback this week
<aggregated counts by (surface, category) from services/feedback>
<top 3 friction patterns surfaced from pattern promotion>
<links to planned fixes if any>
```

If a friction pattern has been visible for three consecutive weekly reviews without a planned fix, the §11.2 monthly architecture-drift review explicitly escalates it as a TODO. If it survives a full quarter, the §11.3 quarterly verification adds a test case that reproduces the friction *as a failing scenario* and the bug becomes ship-blocking. Operator pain has a guaranteed maximum dwell time before it becomes a CI failure.

### 13.4 What operator feedback does NOT do

It does not bypass §1. It does not bypass `bootstrap-verify`. It does not bypass the 3-Surgeon `architectural-gate` for major changes. It is **input** to those processes, not an override of them. A loud operator does not get to skip the gates.

---

## 14. Failure Modes of Static Thinking

The temptation, every time, is to take a shortcut that treats the system as frozen. This section is the explicit list of those shortcuts and why each one breaks the mothership. Aaron is supposed to read this section any time he is about to do one of these — and after he has read it, do something else.

### 14.1 Hardcoded URLs

**WHY THIS BREAKS THE MOTHERSHIP:** A URL in source code is a runtime dependency the rebuild cannot satisfy. Six months from now the repo moves, the host changes, the protocol upgrades — and every commit older than that moment fails to clone. The mothership claims it can rebuild *any* commit; a hardcoded URL silently amends that claim to "any commit, as long as nothing external moves." That amendment is invisible until the day it matters, which is the worst day.

**The right move:** put the URL in `.env.example` with documentation, resolve via env at startup, fail loudly on missing. See `scripts/wire-submodules.sh` for the canonical four-tier resolver pattern.

### 14.2 `latest` tags on Docker images

**WHY THIS BREAKS THE MOTHERSHIP:** `image: postgres:latest` resolves to a different digest every week. Two clones of the same commit, performed a month apart, produce different runtimes. The "deterministic rebuild" property (§9.2) silently dies. CI passes on the day of merge and fails on every clone afterward, with no commit in `git log` to blame.

**The right move:** date-stamped tags + digest pinning per §10.3. Every image reference in every compose file passes `grep -E ':(latest|stable|main|master)' && exit 1` in CI.

### 14.3 Unpinned Python dependencies

**WHY THIS BREAKS THE MOTHERSHIP:** `requirements.txt` without a lock file resolves freely on every `pip install`. A transitive dep publishes a breaking patch and your CI starts failing on a commit that has not changed. Worse: your CI starts passing again when the breaking patch gets yanked, and you never learn what actually happened. Determinism evaporates.

**The right move:** `pyproject.toml` for intent (§10.1), `requirements.lock` for truth, `uv pip sync` for activation. Every CI job starts from the lock.

### 14.4 Unversioned schemas

**WHY THIS BREAKS THE MOTHERSHIP:** A schema with no `$schema_version` field is a schema that lies about its own meaning. Code that reads a v1 record as if it were v2 silently loses fields, mis-interprets types, or — most dangerous — succeeds *enough* to pass tests while accumulating corrupt data. The evidence ledger is supposed to be a system of truth; an unversioned schema makes it a system of plausible-looking lies.

**The right move:** every persisted record carries `$schema_version`, every change ships a migration manifest per §12, every consumer refuses to parse versions it does not know.

### 14.5 Manual migration steps in the runbook

**WHY THIS BREAKS THE MOTHERSHIP:** Any operator step that is documented but not enforced will be skipped — by Aaron at 2am, by a future operator who has not read the runbook, by an automation that does not know the step exists. Half-migrated states are the worst kind of corruption because the system appears to work until a specific code path runs and produces a wrong answer.

**The right move:** migrations run unconditionally on `docker compose up`. The runbook documents what happens; it does not *invoke* what happens. If a step requires manual judgment, that step belongs in a maintenance command (`scripts/migrate.sh rollback`), gated behind an explicit flag, never in the boot path.

### 14.6 "We'll fix the docs later"

**WHY THIS BREAKS THE MOTHERSHIP:** The §1 contract is enforced by `bootstrap-verify` against the *current* docs. If the docs lie and CI is green, the lie passes review. The next operator clones, follows the docs, gets a different result, files a confused issue. Trust erodes. The mothership's claim to be self-describing dies one inaccurate sentence at a time.

**The right move:** the §11.2 monthly architecture-drift check is the safety net, but the goal is to never need it. Doc updates land in the same PR as the behavior they describe. Stale docs are bugs.

### 14.7 Skipping the 3-Surgeon gate on a "small" change

**WHY THIS BREAKS THE MOTHERSHIP:** Most small changes are small. Some small changes — a default value flip, a single-line schema rename, a "harmless" log-format tweak — are constitutional changes wearing small clothes. The 3-Surgeon `architectural-gate` exists precisely because Aaron alone cannot reliably distinguish the two categories under fatigue. Skipping the gate means the only check on constitutional drift is Aaron at 3am.

**The right move:** the gate runs cheaply (~30s in Light mode). Run it. If it reports nothing interesting, you have lost 30 seconds. If it catches a constitutional change, you have saved a quarter of cleanup. The expected value is always positive.

---

**This document is the contract. Sections 1–8 specify the static shape of the promise. Sections 9–14 specify how the promise survives a graph that moves under it. The CI gate is the enforcement. The quarterly manual verification — now including old-commit rebuilds (§11.3) — is the insurance. The weekly cadence is the heartbeat. Together they make "delete everything, clone, run" a fact rather than a hope — at any commit, on any date.**
