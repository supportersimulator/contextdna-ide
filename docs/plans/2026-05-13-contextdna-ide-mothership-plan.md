# ContextDNA IDE — Mothership Superrepo Plan

**Date:** 2026-05-13
**Status:** Draft — awaiting Aaron's decisions on §4
**Goal:** Make `github.com/supportersimulator/contextdna-ide` a self-contained mothership Aaron can re-clone to a fresh laptop and reconstitute his full persistent-memory ecosystem (heavy + lite modes, bidirectional sync, OS-wide payload injection future).

---

## 1. Distinction from the existing OSS-extraction plan

The earlier `docs/plans/contextdna-ide-open-source-plan.md` targets a **public-facing pip-installable single repo**. This mothership plan is **different**:

| Dimension | OSS extraction (existing) | Mothership (this plan) |
|-----------|---------------------------|------------------------|
| Primary purpose | Public distribution | Aaron's personal continuity + universal coding cockpit |
| Scope | Pip-installable Python core | Full superrepo: Docker stack + IDE shell + panel substrate |
| Modes | One profile | Heavy + Lite + bidirectional sync |
| Integration surface | CLI + MCP | DockView panel system — every tool/brain/domain plugs in |
| Coding domains | Web (IDE-bound) | Universal: web, mobile, robotics, HF, Node-RED, marketing, embedded |
| Marketing thesis | "AI memory" | **"Local LLM subconscious × powerhouse speaker = 10x via better context, not bigger windows"** |
| Future | Mature OSS project | OS-wide payload injection layer |
| Sanitization | Aggressive (no PII) | Moderate (decisions locked) |

**Vision amplification (Aaron 2026-05-13):** the IDE-shell panel substrate is **not an afterthought** — it's the integration surface that lets the mothership grow indefinitely. Every coding pursuit (mobile, robotics, ML, marketing automation, embedded) becomes a panel. Each panel contributes events to the subconscious. The subconscious assembles a *silver platter* of relevant context for the powerhouse model on every prompt. Result: small-context-window powerhouse models outperform large-context-window models that have to discover relevance from scratch.

Both can coexist. The mothership repo *is* `supportersimulator/contextdna-ide`; an OSS-public subset can be re-extracted from it later.

---

## 2. Current state of `supportersimulator/contextdna-ide`

- Single bulk commit `865f4a8` ("Initial open-source release")
- 5 dirs already pushed: `contextdna/fleet/`, `engine/`, `mcp-servers/`, `memory/`, `scripts/`
- Repo size 2.6 MB — snapshot, not living mothership

**Gaps vs. the superrepo's CORE today** (must close to reach mothership status):

| Missing | Size | Why critical |
|---------|------|--------------|
| `tools/` | 4.4M | Contains `fleet_nerve_nats.py` (615KB) — the daemon. Without it, mothership can't run multi-fleet coordination or webhook delivery. |
| Full `context-dna/` engine | 583M | Snapshot has `engine/` core only; full bundles/, migrations/, clients/ are absent. |
| `infra/` | 244K | AWS/Terraform/Lambda. Needed for cloud-deployed heavy nodes. |
| `system/` | 16K | Kubernetes manifests, Docker orchestration. |
| `.github/workflows/` | — | CI: gains-gate as GitHub Action. |
| Heavy/Lite profile specs | — | Don't exist yet. New design work. |
| Bidirectional sync docs | — | Implementation partially exists (NATS leaf-node) but no architecture doc. |
| OS-wide injection architecture | — | Future direction — design doc needed. |

---

## 3. Mothership target structure

```
contextdna-ide/                          # the mothership repo
├── README.md                            # mothership manifesto (rewrite using mac3-style polish)
├── ARCHITECTURE.md                      # heavy/lite/sync architecture
├── BOOTSTRAP.md                         # "clone → docker compose → restored" guide
├── LICENSE / NOTICE
├── pyproject.toml                       # context-dna-ide-mothership
├── docker-compose.heavy.yml             # NEW — full stack
├── docker-compose.lite.yml              # NEW — minimum-viable subset
├── .env.example                         # all envs documented
│
├── contextdna/                          # current — fleet runtime
├── memory/                              # 98M — webhook ecosystem (CORE)
├── mcp-servers/                         # 164K — 8 MCP servers (CORE)
├── tools/                               # 4.4M — fleet daemon + admin (CORE, MISSING)
├── scripts/                             # 3.8M — gains-gate, cardio, auto-memory-query
├── infra/                               # 244K — Terraform/AWS/K8s (MISSING)
├── system/                              # 16K — orchestration (MISSING)
│
├── engines/
│   ├── context-dna/                     # SUBMODULE — TS engine (own repo)
│   ├── 3-surgeons/                      # SUBMODULE — cross-exam (own repo)
│   └── multi-fleet/                     # SUBMODULE — transport (own repo)
│
├── docs/
│   ├── vision/                          # Architecture vision
│   ├── dao/                             # Constitutional docs
│   ├── plans/                           # Implementation plans
│   ├── bootstrap/                       # Bootstrap guides per machine class
│   └── sync/                            # Bidirectional sync architecture
│
├── profiles/
│   ├── heavy.yaml                       # 25-service Docker stack
│   ├── lite.yaml                        # 7-service minimal stack
│   └── leaf.yaml                        # NATS leaf-node sync config
│
└── .github/workflows/
    ├── gains-gate.yml
    ├── zsf-bare-except.yml
    └── docker-build.yml
```

---

## 4. Decisions needed from Aaron

### D1 — Submodules or embed?
**Options:**
- **A. Submodule** `3-surgeons/`, `multi-fleet/`, `context-dna/` (own repos preserved; clean separation of concerns)
- **B. Embed** copies in `engines/` (single-repo simplicity; mothership self-contained)

**Recommendation:** A (submodule) — they're already independent OSS repos. Mac3 owns multi-fleet polish. Submodule keeps OSS work flowing without contamination.

### D2 — `admin.contextdna.io` (the Next.js IDE shell, 1.3GB)
**Options:**
- **A.** Ship as `apps/ide-shell/` submodule (Aaron's IDE is part of the mothership)
- **B.** Keep as separate Vercel-deployed product, reference via URL only
- **C.** Ship as Docker service in the heavy profile (containerized Next.js)

**Recommendation:** A — submodule. The mothership is about reconstitution; the IDE is core to that.

### D3 — `ContextDNASupervisor` (Swift/Xcode) + `web-app/` + `tests/`
These need a quick classification — which product owns each? Reply with 1 line each.

### D4 — Heavy vs. Lite split (proposed by research, your call)
**Heavy** (Mac Studio / home server / cloud, 6GB+): full Acontext stack, Postgres, RabbitMQ, Celery workers, Traefik, Jaeger/Prometheus/Grafana, Ollama, NATS chief, fleet NATS cluster.
**Lite** (laptop, 2-3GB): Postgres (small), Redis, Helper Agent, MCP Webhook Server, Event Bridge, NATS leaf, LLM proxy (Qwen3 via MLX).

Sync via existing NATS leaf-node + JetStream KV `fleet_roster` + Redis pub/sub. Bidirectional already partially shipped (leaf-node plan: `docs/plans/2026-04-23-nats-leaf-node-plan.md`).

**Question:** confirm the split, or shift services?

### D5 — Sanitization level
**Options:**
- **A.** Aggressive (sanitize all PII, real-keys → placeholders, drop personal docs) — repo can be flipped public anytime
- **B.** Moderate (drop secrets only, keep Aaron-specific paths and personal docs) — assumes repo stays private OR Aaron toggles later
- **C.** Minimal (drop only secrets) — fastest mothership; PII sanitization deferred

**Recommendation:** B — repo stays private until Aaron flips it. Moderate gives mothership-immediately + flip-public-later via a sanitize script.

---

## 5. Migration sequence (post-decisions)

1. **Phase 1 — Skeleton** (~2h): rewrite README/ARCHITECTURE in mac3-style, create `docker-compose.heavy.yml` + `lite.yml`, add `profiles/` configs, write `BOOTSTRAP.md`.
2. **Phase 2 — Pull CORE** (~3h): port `tools/`, full `context-dna/`, `infra/`, `system/`, `.github/workflows/` from superrepo to mothership; verify gains-gate runs.
3. **Phase 3 — Submodules** (~1h): wire `engines/3-surgeons`, `engines/multi-fleet`, `engines/context-dna`, `apps/ide-shell` as submodules.
4. **Phase 4 — Sync architecture** (~2h): write `docs/sync/bidirectional.md` documenting NATS leaf-node sync mechanism that already partially exists; identify gaps; add lite-mode boot script.
5. **Phase 5 — Bootstrap test** (~2h): clone fresh, `docker compose -f docker-compose.lite.yml up` from scratch, verify webhook fires + 9-section payload assembles. This is the "credibility moment" ChatGPT identified.
6. **Phase 6 — OS-wide injection sketch** (~1h, design only): write `docs/sync/os-wide-injection.md` — long-term direction, sketch interface for non-IDE apps to subscribe.

Total: ~11h of work, parallelizable across mac1 + mac3 (mac3 owns README + branding; mac1 owns code migration + sync).

---

## 6. Risks + mitigations

| Risk | Mitigation |
|------|------------|
| Submodule churn fatigue | Pin to commit hashes; lock submodule refs at mothership tag boundaries. |
| Secrets in git history | Run `git secrets --scan-history` + `gitleaks` on the migration branch before pushing. |
| Heavy stack breaks on lite machines | Profile-based docker-compose; mode selected at boot via env var. |
| Sync conflicts (heavy ↔ lite write same key) | NATS JetStream is already last-write-wins on KV; add evidence-ledger conflict-resolution doc. |
| Reconstitution doesn't actually work | Phase 5 mandatory — gate every commit on `bootstrap-from-scratch.sh` passing in CI. |

---

## 7. Open questions (lower priority — can be answered during build)

- PyPI vs. GitHub-only distribution? (separate from this plan — affects long-term OSS strategy)
- Should the mothership ship its own banner/branding (separate from MF + 3S OSS banners)?
- LaunchAgent vs. Docker for native macOS services (e.g., MLX requires native macOS — can't containerize)?
