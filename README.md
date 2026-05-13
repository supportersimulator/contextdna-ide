<!-- This file is the canonical README. Drafts live in contextdna-ide-oss/draft/. -->

<div align="center">

<img src="docs/assets/contextdna-mothership-banner.png" alt="ContextDNA IDE — the mothership of persistent AI memory" width="100%" />
<!-- TODO: swap for finalized banner once design lands -->

# ContextDNA IDE — The Mothership

**An AI subconscious mind that delivers the right context, at the right moment, to whichever powerhouse model you're driving — across every coding domain, every panel, every machine.**

Most coding assistants race for bigger context windows. ContextDNA IDE races for **better** context. A local LLM runs as your AI subconscious — silently watching every panel, every commit, every cross-examined verdict — so when you prompt Claude, GPT, or whichever powerhouse model you choose, it arrives at a *silver platter* of exactly the patterns, landmines, and architectural decisions that matter most to **this** prompt. Smaller context windows. Sharper answers. First-try success rates that compound across sessions.

Clone the mothership to a fresh laptop, run one Docker command, and the subconscious wakes up with everything it knew yesterday: every win, every landmine, every architectural decision, every cross-examined verdict. **A living, evidence-backed brain that survives the loss of any single machine.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-compose-2496ed.svg)](https://www.docker.com/)
[![NATS](https://img.shields.io/badge/transport-NATS_JetStream-27aae1.svg)](https://nats.io/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-plugin-7c3aed.svg)](https://claude.com/claude-code)
[![ZSF](https://img.shields.io/badge/Zero_Silent_Failures-invariant-success.svg)](#zero-silent-failures)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[**Quick Start**](#quick-start) · [**Architecture**](ARCHITECTURE.md) · [**Bootstrap**](BOOTSTRAP.md) · [**Plain-English Model**](#the-plain-english-model) · [**Pairs With**](#pairs-with)

</div>

---

## The Mothership Vision

> If your laptop fell in the ocean today, how much of what your AI knows about you would be gone?

For most developers the answer is *all of it*. Chat history. Context. Every "I already told you that" moment, vanished. You'd start over from zero.

ContextDNA IDE is the mothership that answers that question with **none of it.** Clone the repo to a new machine, bring up the stack, and your AI assistant resumes mid-thought — with the same patterns, the same warnings, the same hard-won wisdom from every previous session. The mothership is not a product to install. **It is the operating environment for a persistent intelligence you carry with you.**

The mothership ships three things in one repo:

1. **The brain** — an evidence-based memory ledger (`memory/`) that records what worked, what failed, and what survived cross-examination.
2. **The nervous system** — fleet transport, hooks, and MCP servers that connect the brain to your IDE in real time.
3. **The body** — Docker stacks, bootstrap scripts, and bidirectional sync so the brain runs on a laptop, a home server, or a fleet of both.

Three engines plug into that body, each owning one dimension of the operating environment:

| Engine | Dimension | Repo |
|--------|-----------|------|
| **[Context DNA engine](https://github.com/supportersimulator/context-dna)** | Memory | TypeScript engine for indexing, claim graphs, and evidence flow |
| **[3-Surgeons](https://github.com/supportersimulator/3-surgeons)** | Quality | Three independent LLMs cross-examine every decision |
| **[Multi-Fleet](https://github.com/supportersimulator/multi-fleet)** | Transport | 9-priority cross-machine messaging that always delivers |
| **[IDE Shell](https://github.com/supportersimulator/admin.contextdna.io)** | Panels | DockView-based universal panel substrate — every tool, every brain, every domain plugs in as a panel |

You can use any one of them alone. The mothership is what happens when all four run on the same hardware, share the same evidence ledger, and answer to the same person: you.

---

## The Plain-English Model

What does the mothership actually *do*? Strip away the jargon and it does four things, in this order:

1. **Remember** — every coding session, every commit, every cross-examined verdict is captured as structured evidence. No vector embeddings, no fuzzy similarity — explicit claims with explicit support.
2. **Decide which memories have evidence** — a claim only graduates to wisdom when outcomes confirm it. Failed attempts retire automatically. Confidence is *earned*, not asserted.
3. **Inject relevant context** — when you start a new prompt, the mothership assembles a 9-section payload of *only the wisdom that applies right now*. Your AI starts the conversation already up to speed.
4. **Learn from outcomes** — what the AI did, whether it shipped, whether you had to fix it later — all of that feeds back into the ledger. Tomorrow's payload is sharper because of today's session.

That's the loop. Capture → promote with evidence → inject → learn. Everything else in this repo is plumbing in service of those four steps.

---

## Heavy + Lite + Sync

The mothership is **the same code on every machine** — but it runs in two profiles depending on what the machine is good at.

```
┌────────────────────────────────────────────────────────────────────────┐
│                                                                        │
│   HEAVY mode (Mac Studio / home server / cloud — 6 GB+ RAM)            │
│   ──────────────────────────────────────────────────────────           │
│   Full Acontext stack · Postgres · Redis · RabbitMQ · Celery           │
│   Ollama · NATS chief · Prometheus · Grafana · Jaeger                  │
│   Owns: long-term evidence ledger, heavy LLM jobs, fleet chief          │
│                                                                        │
│                              ▲                                         │
│                              │  bidirectional sync                      │
│                              │  (NATS leaf-node + JetStream KV)         │
│                              ▼                                         │
│                                                                        │
│   LITE mode (laptop / cabin Mac / café — 2–3 GB RAM)                   │
│   ─────────────────────────────────────────────────                    │
│   Postgres (small) · Redis · MCP servers · NATS leaf · MLX proxy       │
│   Owns: live session, hooks, daily injection payload                   │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

**Bidirectional sync** is not eventual-consistency hand-waving. It is a concrete mechanism: every node holds a NATS leaf-node link to a shared hub, JetStream KV (`fleet_roster`, `evidence_ledger`) replicates across the cluster, and Redis pub/sub handles intra-machine notifications. Heavy and Lite can both write; conflicts resolve last-write-wins on KV with an evidence-merge pass for claim updates. See [docs/sync/bidirectional.md](docs/sync/bidirectional.md).

| Scale | Topology | What it unlocks |
|-------|----------|-----------------|
| **1 machine — Lite only** | Laptop with Docker | Persistent memory across IDE sessions on one box. Survives reboots. No fleet. |
| **1 machine — Heavy only** | Mac Studio at home | Full evidence ledger, all telemetry, no roaming. Best for solo home setup. |
| **Laptop + home server** | Lite syncs to Heavy | Code anywhere, ledger stays canonical at home. Laptop in a café still gets fresh injections. |
| **Fleet of 3–5** | Multi-Fleet (NATS cluster + leaf nodes) | Cross-machine background agents. Mac1 codes, Mac2 reviews, Mac3 trains models. All share one ledger. |
| **10+ machines** | Multi-Fleet at scale + cloud hub | A coding swarm. Each machine owns a piece. Evidence compounds across the fleet. |

The mothership *is* the codebase. The scale is a choice of how many machines to clone it onto.

---

## The 9-Section Payload

Every time you submit a prompt, ContextDNA IDE assembles a structured payload and injects it before your message reaches the AI. Nine sections, each answering one question:

| # | Section | Plain-English question it answers |
|---|---------|-----------------------------------|
| **S0** | Safety Rails | What must this AI never do in this codebase? |
| **S1** | Foundation | Where do the relevant files live, and what are the entry points? |
| **S2** | Professor Wisdom | What patterns have I learned that apply right now? |
| **S3** | Awareness | What mistakes did I make last time on something like this? |
| **S4** | Deep Context | What is the actual architecture, not the marketing version? |
| **S5** | Protocol | How careful should the AI be? Light review or full surgical gate? |
| **S6** | Holistic | What cross-cutting concerns intersect this change? |
| **S7** | Full Library | What does the evidence ledger say about claims related to this task? |
| **S8** | 8th Intelligence | What subconscious patterns is the system noticing across sessions? |

The payload is **assembled fresh per prompt**, not pre-rendered. The mothership knows the question you're about to ask before the AI does, and picks only the sections that earn their tokens. See [ARCHITECTURE.md](ARCHITECTURE.md#the-9-section-payload) for the typed schema.

---

## Evidence-Based Memory

Most "AI memory" today is vector similarity over chat logs. It is impressive at first, then it lies confidently when an old half-truth out-ranks a new correction.

ContextDNA IDE rejects that model. **Memory in the mothership is a directed graph of typed evidence, with explicit promotion and retirement.**

```
   RawEvent          (command run, file edit, commit, test result)
       │
       ▼
   SessionSummary    (what happened across one coding session)
       │
       ▼
   Claim             ("asyncio.to_thread is the right wrapper for blocking IO")
       │
       ▼
   Evidence          (outcomes, test runs, fleet verdicts that support the claim)
       │
       ▼
   Outcome           (did the change ship? did it survive?)
       │
       ├──▶  PromotedWisdom        (claim survived → enters S2 payload)
       │
       └──▶  RetiredWisdom         (claim contradicted → leaves circulation)
                                            │
                                            ▼
                                     InjectionCandidate
                                     (what to surface next time)
```

A claim never enters wisdom because the AI was confident about it. It enters wisdom because **outcomes confirmed it across sessions**. A claim retires not because it was deleted, but because **new evidence overturned it**. The full taxonomy lives in [ARCHITECTURE.md](ARCHITECTURE.md#evidence-taxonomy).

This is the principle that separates the mothership from any vector store: confidence is **earned**, not asserted.

---

## The Universal Panel Substrate

Every coding domain has its own tools, its own brains, its own quirks. The mothership ships a **DockView-based panel system** as `apps/ide-shell/` — already 39 panels live today — so any tool, any local brain, any AI system can plug in as a first-class panel with its own persistent-memory slice.

Panels that exist or are wireable today:

| Panel class | Examples |
|-------------|----------|
| **Code Surgery** | SurgeonTheater, TruthLadder, EvidenceLedger, Tribunal, HumanArbiter |
| **Fleet operations** | RaceTheater, CampaignTheater, Permissions, HirePanel |
| **Domain coding** | bring your own — HuggingFace Hub, Node-RED, mobile dev (iOS/Android tooling), robotics (ROS workflows), embedded firmware, ML training |
| **Marketing + ops** | Buffer, Mailchimp, HubSpot, Stripe checkout, Vercel deploys — any tool with an API |
| **External brains** | drop in any third-party persistent-memory system; the mothership routes its events through the same evidence ledger |
| **Full repos as panels** | mount any existing repo as a panel — its files, tests, build logs all flow into the subconscious |

The promise: **whatever you're coding — web, mobile, robots, ML training, marketing automation, embedded firmware — there's a panel for it, and the subconscious learns across all of them.** Cross-domain wisdom is the unlock. A landmine learned in a robotics project surfaces when you touch a mobile auth flow. A pattern proven in a Node-RED automation gets promoted when you write similar code in a Python service.

The panel layer is what turns the mothership from "a memory system for one workflow" into **the operating environment for every coding pursuit you take on**.

---

## The 10x Thesis

Bigger context windows are a brute-force answer to a quality problem. The mothership takes the opposite bet:

> A 32K-window powerhouse model fed *exactly* the right S0–S8 payload will out-code a 1M-window model that has to discover relevance from scratch.

Three things make this work:

1. **The local LLM never sleeps.** It processes panel events, fleet messages, evidence verdicts continuously — distilling what's worth promoting, what's worth retiring.
2. **Promotion is gated by outcomes.** A pattern only enters Professor Wisdom after evidence says it worked. Confidence is *earnable*, not asserted.
3. **Injection is just-in-time.** The 9-section payload is assembled the moment you prompt — no precomputed bundles, no stale slices.

The local LLM is the **subconscious**. The powerhouse model (Claude, Opus, GPT, whichever you're driving) is the **speaker**. Together they outperform either alone — because the subconscious has been learning across every panel, every session, every cross-examined verdict that the speaker never has time to see.

**The 10x isn't a benchmark claim. It's a structural one: context curated by accumulated wisdom beats context discovered from scratch, every time.**

---

## Pairs With

The mothership is the operating environment. It is good at running things. The three engines below are good at *being* things — and each fills one dimension the mothership orchestrates.

### Multi-Fleet — the scale dimension

[**Multi-Fleet**](https://github.com/supportersimulator/multi-fleet) is the transport. Run the mothership on one machine and you get persistent memory. Run it across a Multi-Fleet of N machines and the **evidence ledger replicates fleet-wide via NATS JetStream**. Failures route around dead nodes through a 9-priority channel cascade. Your laptop in a café and your home server stay in sync over the same protocol that survives Wi-Fi outages and NAT traversal.

### 3-Surgeons — the quality dimension

[**3-Surgeons**](https://github.com/supportersimulator/3-surgeons) is the cross-examination. Three independent LLMs review every architectural decision before code is written. Their verdicts feed back into the evidence ledger as `Evidence` records — so the mothership doesn't just remember what *happened*, it remembers what **three independent reviewers thought about it**. Confidence is calibrated, not asserted.

### Superpowers — the process dimension

[**Superpowers**](https://github.com/supportersimulator/superpowers) is the workflow discipline. Skills for TDD, debugging, planning, code review, and verification compose with the mothership: every skill invocation is captured as a `RawEvent`, every verified outcome graduates to `PromotedWisdom`. Process and memory reinforce each other.

**Memory × scale × quality × process** — that is the mothership.

---

## Quick Start

```bash
# 1. Clone the mothership (with submodules)
git clone --recurse-submodules https://github.com/supportersimulator/contextdna-ide
cd contextdna-ide

# 2. Start the lite stack (works on a 2-3 GB laptop)
cp .env.example .env
docker compose -f docker-compose.lite.yml up -d

# 3. Verify everything is up
context-dna-ide health

# 4. Ask the mothership for a 9-section payload
context-dna-ide consult "deploy the landing page"

# 5. Install the Claude Code hook so payloads inject automatically
context-dna-ide hooks install claude
```

After step 5, every prompt you submit to Claude Code (or Cursor, or VS Code, or Codex, or Gemini) is automatically prefixed with the 9-section payload. No manual context-loading. No "let me explain the codebase again." **The mothership remembers for you.**

For the heavy stack:

```bash
docker compose -f docker-compose.heavy.yml up -d
```

Heavy boots Postgres, RabbitMQ, Celery workers, Prometheus, Grafana, Jaeger, Ollama, and the NATS chief node. Use it on a Mac Studio / home server / cloud host with 6 GB+ RAM.

---

## Bootstrap from Scratch

The credibility moment for any "mothership" repo is whether a fresh clone actually reconstitutes the system. The mothership is built around that test.

```bash
git clone --recurse-submodules https://github.com/supportersimulator/contextdna-ide
cd contextdna-ide
./scripts/bootstrap-from-scratch.sh    # exits 0 → mothership is real
```

`bootstrap-from-scratch.sh` is the canonical end-to-end test: clone the repo on an empty machine, bring up the lite stack, fire a webhook, assemble the 9-section payload, and verify the evidence ledger persisted. If it fails in CI, the mothership claim fails with it.

Full per-machine guides (macOS laptop, macOS Mac Studio, Linux home server, cloud VM): see [BOOTSTRAP.md](BOOTSTRAP.md).

---

## Status

**What's shipped:**

- Evidence-based memory ledger with claim graph, promotion, and retirement
- 9-section payload assembly with per-section timeouts and ZSF instrumentation
- 8 MCP servers (`contextdna-engine`, `contextdna-webhook`, `projectdna`, `synaptic`, and four more)
- Webhook injection via `userPromptSubmit` hooks for Claude Code, Cursor, VS Code, Codex, Gemini
- Multi-Fleet integration (NATS JetStream cluster, 9-priority cascade, fleet-roster KV)
- 3-Surgeons integration (cross-examined verdicts flow into evidence ledger)
- Heavy + Lite Docker profiles with bidirectional sync
- 17-check gains-gate and cardiologist EKG running in CI

**What's experimental:**

- OS-wide payload injection — design sketch in [docs/sync/os-wide-injection.md](docs/sync/os-wide-injection.md). Today the mothership injects into IDEs and CLI. The longer-term direction is a system-wide payload bus that *any* application can subscribe to.
- Multi-tenant ledger — one ledger per person today; team and org sharding is on the roadmap.
- Native Windows lite mode — macOS and Linux first-class; Windows works via WSL but isn't first-class yet.

The mothership runs daily on a 4-node fleet (mac1, mac2, mac3, cloud) and has shipped through real network partitions, model outages, JetStream restarts, and intermittent Wi-Fi without dropping a payload.

It is **production-tested at small scale**. We invite you to clone it and test it at yours.

---

## Zero Silent Failures

Every failure path in the mothership bumps a named counter. You can grep the codebase: there is no `except Exception: pass`. The gains-gate CI step enforces this.

```bash
curl -s http://127.0.0.1:8888/health | jq '.counters'
```

```json
{
  "webhook_events_recorded":        1842,
  "webhook_publish_errors":         0,
  "ledger_promotion_total":         317,
  "ledger_retirement_total":        58,
  "cross_exam_verdicts_recorded":   124,
  "fleet_cascade_fallback_total":   14
}
```

If you see a counter climbing, you have a real bug. If all the error counters are zero and the activity counters are climbing, your mothership is healthy.

---

## License

MIT — do anything you want, just keep the copyright notice. See [LICENSE](LICENSE).

---

<div align="center">

**Built for the developer who refuses to start over every Monday.**

⭐ Star this repo if you've ever said *"I already told you that yesterday"* to your AI.

</div>
