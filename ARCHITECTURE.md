# ContextDNA IDE — Mothership Architecture

> The deep dive. What runs where, how data flows, and why every brand term means something concrete.

This document is the canonical architectural reference for the mothership. The [README](MOTHERSHIP-README.md) is the entry point; this is the schematic.

---

## 1. The Three Layers

The mothership is a stack of three independent layers. Each layer has one job, one ownership boundary, and one external repo backing it. You can run any layer alone — and that is the point.

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                      │
   │   QUALITY LAYER         3-Surgeons                                   │
   │   ────────────────      github.com/supportersimulator/3-surgeons     │
   │   Cross-examined        Three independent LLMs. Verdicts feed the    │
   │   verdicts              evidence ledger as typed Evidence records.   │
   │                                                                      │
   │   ─────────────────────────────────────────────────────────────────  │
   │                                                                      │
   │   MEMORY LAYER          Context DNA + ContextDNA IDE                 │
   │   ────────────────      github.com/supportersimulator/context-dna    │
   │                         (this repo orchestrates it)                  │
   │   Evidence-based        RawEvent → Claim → Evidence → PromotedWisdom │
   │   memory ledger         9-section payload assembly at injection time │
   │                                                                      │
   │   ─────────────────────────────────────────────────────────────────  │
   │                                                                      │
   │   TRANSPORT LAYER       Multi-Fleet                                  │
   │   ────────────────      github.com/supportersimulator/multi-fleet    │
   │   9-priority cascade    NATS JetStream + HTTP + SSH + git + WoL.     │
   │   cross-machine bus     Messages always deliver. Counters always     │
   │                         move.                                        │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
```

The mothership is what happens when all three layers run on the same machine (or fleet of machines) and share one evidence ledger. The README's "memory × scale × quality" framing maps directly onto Memory × Transport × Quality.

---

## 2. The Sacred Loop — Capture, Index, Promote, Inject

The mothership runs one loop forever. ChatGPT named it the sacred loop because every other piece of the system exists in service of it.

```
                ┌─────────────────────────────────────────────┐
                │                                             │
                ▼                                             │
        ┌──────────────┐                                      │
        │   CAPTURE    │  Every prompt, edit, commit, test    │
        │              │  becomes a RawEvent in the ledger    │
        └──────┬───────┘                                      │
               │                                              │
               ▼                                              │
        ┌──────────────┐                                      │
        │    INDEX     │  RawEvents roll up into Sessions,    │
        │              │  Sessions distill into Claims        │
        └──────┬───────┘                                      │
               │                                              │
               ▼                                              │
        ┌──────────────┐                                      │
        │   PROMOTE    │  Claims survive (or don't) based on  │
        │              │  Outcomes + 3-Surgeons verdicts      │
        └──────┬───────┘                                      │
               │                                              │
               ▼                                              │
        ┌──────────────┐                                      │
        │    INJECT    │  PromotedWisdom enters the           │
        │              │  9-section payload on next prompt    │
        └──────┬───────┘                                      │
               │                                              │
               └──────────────────────────────────────────────┘
                  (outcomes from new sessions feed CAPTURE)
```

Each transition has a typed event. Each event bumps a counter. Each counter is observable at `/health`. There is no "AI memory" magic — just disciplined bookkeeping over typed records.

---

## 3. The 9-Section Payload — Typed Schema

The payload is the artifact that crosses the boundary from mothership to IDE. It is strongly typed.

```python
from typing import List, Literal, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class SectionMeta(BaseModel):
    """Bookkeeping every section carries."""
    section_id: Literal["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S10"]
    assembled_at: datetime
    token_budget: int                  # max tokens this section may consume
    actual_tokens: int                 # what it actually used
    sources: List[str]                 # evidence IDs that contributed
    confidence: float = Field(ge=0.0, le=1.0)


class SafetyRails(BaseModel):           # S0
    """What this AI must never do in this codebase."""
    meta: SectionMeta
    forbidden_patterns: List[str]       # e.g. "except Exception: pass"
    must_preserve: List[str]            # e.g. "_ecg in audio cues"
    blast_radius_caps: dict             # max files, max LOC per change


class Foundation(BaseModel):            # S1
    """Where the relevant files live."""
    meta: SectionMeta
    entry_points: List[str]
    relevant_paths: List[str]
    recent_edits: List[str]


class ProfessorWisdom(BaseModel):       # S2
    """Patterns earned across prior sessions."""
    meta: SectionMeta
    patterns: List["PromotedWisdom"]    # forward ref to evidence taxonomy
    landmines: List[str]


class Awareness(BaseModel):             # S3
    """Past mistakes adjacent to the current task."""
    meta: SectionMeta
    retired_wisdom: List["RetiredWisdom"]
    sop_warnings: List[str]


class DeepContext(BaseModel):           # S4
    """Architecture twin — the real shape, not the marketing one."""
    meta: SectionMeta
    architecture_summary: str
    dependency_graph_excerpt: dict


class Protocol(BaseModel):              # S5
    """Risk-calibrated behavior."""
    meta: SectionMeta
    risk_level: Literal["light", "standard", "full"]
    required_gates: List[str]           # ["sentinel", "cross-exam", "gains-gate"]
    per_section_timeouts_s: dict        # WEBHOOK_S<n>_TIMEOUT_S overrides


class Holistic(BaseModel):              # S6
    """Cross-cutting concerns intersecting this change."""
    meta: SectionMeta
    intersecting_systems: List[str]
    coupling_warnings: List[str]


class FullLibrary(BaseModel):           # S7
    """Evidence ledger slice relevant to this task."""
    meta: SectionMeta
    claims: List["Claim"]
    open_questions: List[str]


class EighthIntelligence(BaseModel):    # S8
    """Subconscious pattern recognition across sessions."""
    meta: SectionMeta
    cross_session_patterns: List[str]
    synaptic_signals: List[str]


class StrategicLayer(BaseModel):        # S10 (optional)
    """High-stakes architectural context for full-gate prompts."""
    meta: SectionMeta
    architectural_decisions: List[str]
    open_invariants: List[str]


class ContextPayload(BaseModel):
    """The artifact injected before every prompt."""
    schema_version: Literal["mothership-v1"]
    prompt_text: str
    assembled_at: datetime
    assembled_for: str                  # node_id assembling the payload
    target_ide: Literal["claude-code", "cursor", "vscode", "codex", "gemini"]

    s0_safety_rails: SafetyRails
    s1_foundation: Foundation
    s2_professor_wisdom: ProfessorWisdom
    s3_awareness: Awareness
    s4_deep_context: DeepContext
    s5_protocol: Protocol
    s6_holistic: Holistic
    s7_full_library: FullLibrary
    s8_eighth_intelligence: EighthIntelligence
    s10_strategic: Optional[StrategicLayer] = None

    total_token_budget: int
    actual_tokens: int
    counters_snapshot: dict             # ZSF observability per-section
```

The payload is **assembled fresh per prompt** by `memory/webhook_assembly.py`. Sections that exceed budget downgrade gracefully (full → abbreviated). Sections that fail to assemble emit a typed error and bump `webhook_section_errors_total{section=...}` — never silently empty.

---

## 4. Evidence Taxonomy — Typed Classes

The evidence layer is the soul of the mothership. Every brand term in the README is a class here.

```python
class RawEvent(BaseModel):
    """The atom. One thing that happened in one session."""
    event_id: str
    occurred_at: datetime
    session_id: str
    node_id: str                        # which machine in the fleet
    kind: Literal[
        "prompt_submitted", "tool_invocation", "file_edit",
        "command_run", "test_result", "commit", "fleet_message",
        "surgeon_verdict", "user_feedback",
    ]
    payload: dict


class SessionSummary(BaseModel):
    """A coding session distilled. Many RawEvents → one summary."""
    session_id: str
    started_at: datetime
    ended_at: datetime
    node_id: str
    raw_event_ids: List[str]
    narrative: str                      # 200-500 word distillation
    files_touched: List[str]
    outcome_hint: Literal["shipped", "abandoned", "in_progress", "rolled_back"]


class Claim(BaseModel):
    """A testable assertion the system thinks might be wisdom."""
    claim_id: str
    proposed_at: datetime
    proposed_by: str                    # session_id or surgeon_id
    statement: str                      # "asyncio.to_thread wraps blocking IO correctly"
    scope: List[str]                    # paths / modules where the claim applies
    status: Literal["proposed", "supported", "contested", "promoted", "retired"]
    supporting_evidence_ids: List[str]
    contesting_evidence_ids: List[str]


class Evidence(BaseModel):
    """An observation that supports or contests a Claim."""
    evidence_id: str
    recorded_at: datetime
    claim_id: str
    polarity: Literal["supports", "contests"]
    source: Literal[
        "test_result", "outcome", "surgeon_verdict",
        "user_confirmation", "fleet_consensus", "regression_caught",
    ]
    weight: float = Field(ge=0.0, le=1.0)
    detail: str


class Outcome(BaseModel):
    """Did the session's change ship and survive?"""
    outcome_id: str
    session_id: str
    claim_ids_touched: List[str]
    shipped: bool
    rolled_back: bool
    survived_after: Optional[datetime]  # None until enough time has passed
    rollback_reason: Optional[str]


class PromotedWisdom(BaseModel):
    """A Claim that survived. Enters the S2 payload."""
    wisdom_id: str
    promoted_at: datetime
    promoted_from_claim_id: str
    statement: str
    scope: List[str]
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_count: int
    last_reinforced_at: datetime


class RetiredWisdom(BaseModel):
    """A Claim or PromotedWisdom that was overturned. Enters S3 as a warning."""
    retired_id: str
    retired_at: datetime
    retired_from_id: str                # claim_id or wisdom_id
    reason: Literal[
        "contradicted_by_evidence", "superseded_by_better_claim",
        "scope_no_longer_relevant", "user_explicit_rejection",
    ]
    last_statement: str
    contesting_evidence_ids: List[str]


class InjectionCandidate(BaseModel):
    """Synthesizer output. What the next payload should include and why."""
    candidate_id: str
    proposed_at: datetime
    target_section: Literal[
        "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S10",
    ]
    source_id: str                      # promoted_wisdom_id | retired_id | claim_id
    relevance_score: float = Field(ge=0.0, le=1.0)
    token_cost_estimate: int
    rationale: str
```

These nine classes are the entire ontology. Everything in `memory/` reads from and writes to records of these types. The schema is validated by Pydantic at every write — invalid records cannot enter the ledger.

---

## 5. The Panel Substrate

The IDE shell is not a fixed application. It is a **panel substrate** — a DockView-based chrome where every coding domain plugs in as a panel and contributes to the same evidence ledger. The substrate lives at `apps/ide-shell/` (39 panels already live in `admin.contextdna.io`). New domains arrive as third-party panels; the mothership treats them all the same.

### 5.1 Architecture Overview

DockView owns layout (tabs, splits, drag-rearrange, persistence). Each panel is a React component conforming to the `MothershipPanel` interface. The shell never reaches into a panel's internals — it only calls the contract.

```ts
// apps/ide-shell/src/panel/contract.ts

export interface PanelManifest {
  id: string;                         // "huggingface.hub" | "mobile.ios" | ...
  version: string;                    // semver
  domains: string[];                  // ["mobile.ios", "mobile.android"]
  emits: string[];                    // evidence-event types this panel produces
  subscribes: string[];               // evidence-event types this panel consumes
  required_env: string[];             // ["HF_TOKEN", "STRIPE_API_KEY"]
  required_mcp: string[];             // ["huggingface-mcp", "stripe-mcp"]
}

export interface MothershipPanel {
  id: string;                         // matches manifest.id
  version: string;                    // matches manifest.version
  manifest: PanelManifest;

  mount(host: PanelHost): void;       // host gives access to DockView slot,
                                      // event bus, evidence contributor, etc.
  unmount(): void;

  events: EventEmitter;               // panel-local event stream

  // Every panel is an evidence source. The substrate calls this to drain
  // panel observations into RawEvents in the ledger.
  evidence_contributor(): AsyncIterable<RawEvent>;
}
```

The substrate guarantees: panels never share state directly, never write to the ledger by hand, and never know which other panels are mounted. They only emit typed events and `RawEvent`s. The subconscious does the rest.

### 5.2 Universal Panel Manifest

Every panel ships a `panel.manifest.json` at its root. The schema is documented in [docs/panels/manifest-spec.md](docs/panels/manifest-spec.md) and validated on install.

```json
{
  "id": "huggingface.hub",
  "version": "0.3.1",
  "domains": ["ml.training", "ml.inference", "datasets"],
  "emits": [
    "panel.huggingface.model_pulled",
    "panel.huggingface.dataset_explored",
    "panel.huggingface.inference_ran"
  ],
  "subscribes": [
    "evidence.claim.proposed",
    "evidence.wisdom.promoted"
  ],
  "required_env": ["HF_TOKEN"],
  "required_mcp": ["huggingface-mcp"]
}
```

The manifest is the only thing the substrate needs to decide whether a panel is installable, which MCP servers to boot, and which evidence streams to wire up. Missing env or MCP entries surface as a typed install error — never a silent half-mount.

### 5.3 Subconscious–Speaker Dataflow

Every panel feeds the local LLM (subconscious). The local LLM continuously distills, promotes, and retires evidence — without the user prompting. When the user does prompt, the powerhouse model (the speaker) receives the 9-section payload assembled from whatever the subconscious has settled on so far.

```
                    ┌──────────────────────────────┐
   panel events ───▶│  Local LLM (subconscious)    │───▶ Evidence Ledger
                    │   Qwen3-4B continuously       │      (Postgres + claim graph)
                    │   distills/promotes/retires   │
                    └─────────────┬────────────────┘
                                  │
                            (at prompt time)
                                  │
                                  ▼
                  ┌──────────────────────────────────┐
   user prompt ──▶│  9-section S0-S8 payload         │──▶ Powerhouse Model
                  │  assembled from live ledger      │     (Claude / Opus / GPT)
                  └──────────────────────────────────┘
```

This is the structural reason a smaller context window still 10x's performance: the subconscious already pre-digested the panel firehose into a few hundred high-confidence claims. The speaker reads digested wisdom, not raw logs.

### 5.4 Panel Categories

| Category | Example panels | Domain affinity |
|---|---|---|
| **Code Surgery** | SurgeonTheater, EvidenceLedger, TruthLadder, Tribunal, HumanArbiter | quality.review, quality.verdict |
| **Fleet Operations** | RaceTheater, CampaignTheater, Permissions, HirePanel | fleet.ops, fleet.governance |
| **Domain Coding** | HuggingFace Hub, Node-RED, mobile dev iOS/Android, robotics ROS, ML training, embedded firmware | ml.*, mobile.*, robotics.*, embedded.* |
| **Marketing + Ops** | Buffer, Mailchimp, HubSpot, Stripe, Vercel | marketing.*, payments.*, deploy.* |
| **External Brains** | Third-party persistent-memory systems mounted as panels (Mem0, LangMem, custom vendor stacks) | memory.external.* |
| **Full Repos as Panels** | Any external repo mounted whole; its files/tests/build-logs flow into the subconscious | repo.<id> |

Each category is just a convention on `domains`. The substrate does not privilege one category over another — a robotics panel and a Stripe panel are peers, and the subconscious's promotion logic is identical across both.

### 5.5 Third-Party Panel Installation

Panel install is a single command:

```bash
context-dna-ide panel install <git-url>
```

The substrate:

1. Clones the repo into `apps/ide-shell/panels/<id>/`.
2. Validates `panel.manifest.json` against the manifest schema.
3. Boots required MCP servers and checks required env vars.
4. Registers the panel in `apps/ide-shell/panels.lock.json` (the panel equivalent of `package-lock.json`).
5. Hot-mounts the panel in DockView without a shell restart.

Uninstall is `context-dna-ide panel remove <id>` — symmetric, idempotent, and counter-tracked.

### 5.6 Cross-Domain Wisdom Unlock

A landmine learned in a robotics project surfaces when you touch a mobile auth flow, because both panels contribute `RawEvent`s to the same evidence ledger and the subconscious's promotion logic is **domain-blind by default** — a `Claim` like *"async cleanup callbacks must clear timers before releasing handles"* gets promoted on the strength of its evidence, not on which panel emitted it. The Mobile panel's S2 payload sees the robotics-derived `PromotedWisdom` the moment its `scope` overlaps. This is the structural reason a multi-domain developer outperforms a specialist with the same speaker model — the substrate turns every domain into a teacher for every other.

---

## 6. Heavy Mode Topology

Heavy mode runs on a 6 GB+ machine (Mac Studio, home server, cloud VM). It owns the canonical evidence ledger and serves as the fleet chief.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                       HEAVY NODE  (docker-compose.heavy.yml)        │
   │                                                                     │
   │   ┌─────────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
   │   │   Postgres      │   │   Redis         │   │   RabbitMQ       │  │
   │   │   (ledger)      │   │   (cache/pubsub)│   │   (task bus)     │  │
   │   └────────┬────────┘   └────────┬────────┘   └─────────┬────────┘  │
   │            │                     │                      │           │
   │            └─────────────────────┼──────────────────────┘           │
   │                                  │                                  │
   │   ┌─────────────────┐   ┌────────▼─────────┐   ┌──────────────────┐ │
   │   │   Celery        │◀──┤   Memory API     │──▶│   MCP servers    │ │
   │   │   workers       │   │   (FastAPI)      │   │   (8 servers)    │ │
   │   └─────────────────┘   └────────┬─────────┘   └──────────────────┘ │
   │                                  │                                  │
   │   ┌─────────────────┐   ┌────────▼─────────┐   ┌──────────────────┐ │
   │   │   Ollama        │   │  Webhook host    │   │  Fleet daemon    │ │
   │   │   (LLM runtime) │   │  (IDE injection) │   │  (NATS chief)    │ │
   │   └─────────────────┘   └──────────────────┘   └─────────┬────────┘ │
   │                                                          │          │
   │   ┌─────────────────┐   ┌──────────────────┐   ┌─────────▼────────┐ │
   │   │   Prometheus    │   │   Grafana        │   │   NATS server    │ │
   │   │   (metrics)     │   │   (dashboards)   │   │   (JetStream R3) │ │
   │   └─────────────────┘   └──────────────────┘   └─────────┬────────┘ │
   │                                                          │          │
   │   ┌─────────────────┐   ┌──────────────────┐             │          │
   │   │   Jaeger        │   │   Traefik        │             │          │
   │   │   (tracing)     │   │   (TLS + routes) │             │          │
   │   └─────────────────┘   └──────────────────┘             │          │
   │                                                          │          │
   └──────────────────────────────────────────────────────────┼──────────┘
                                                              │
                                       ┌──────────────────────▼──────────┐
                                       │  Public NATS hub (leaf-node     │
                                       │  rendezvous: NGS or Fly.io)     │
                                       └─────────────────────────────────┘
```

Heavy mode boots **roughly 25 services**. It is meant to run permanently. The home-server profile gives you long-term storage, full telemetry, and the ability to run heavy LLM jobs without melting a laptop.

---

## 7. Lite Mode Topology

Lite mode runs on a 2-3 GB laptop. It owns the live session and the daily injection payload. It defers heavy lifting to a Heavy node when one is reachable.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                       LITE NODE  (docker-compose.lite.yml)          │
   │                                                                     │
   │   ┌─────────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
   │   │  Postgres       │   │  Redis          │   │  MCP servers     │  │
   │   │  (small ledger) │   │  (cache/pubsub) │   │  (4 servers)     │  │
   │   └────────┬────────┘   └────────┬────────┘   └─────────┬────────┘  │
   │            │                     │                      │           │
   │            └─────────────────────┼──────────────────────┘           │
   │                                  │                                  │
   │                         ┌────────▼─────────┐                        │
   │                         │  Memory API      │                        │
   │                         │  (FastAPI lite)  │                        │
   │                         └────────┬─────────┘                        │
   │                                  │                                  │
   │   ┌─────────────────┐   ┌────────▼─────────┐   ┌──────────────────┐ │
   │   │   MLX proxy     │◀──┤  Webhook host    │──▶│  Fleet daemon    │ │
   │   │   (native macOS)│   │  (IDE injection) │   │  (NATS leaf)     │ │
   │   └─────────────────┘   └──────────────────┘   └─────────┬────────┘ │
   │                                                          │          │
   └──────────────────────────────────────────────────────────┼──────────┘
                                                              │
                                       ┌──────────────────────▼──────────┐
                                       │  Heavy node (over leaf-node)    │
                                       │  OR public NATS hub             │
                                       └─────────────────────────────────┘
```

Lite mode boots **roughly 7 services**. Postgres is configured small (256 MB shared buffers). The MLX proxy is a native macOS process, not a container — Apple's Metal stack does not virtualize. Everything else fits in Docker.

A laptop running lite mode is fully autonomous if it loses network: writes accumulate locally and replay to Heavy when the leaf link comes back. The leaf-node mechanism is the heart of bidirectional sync.

---

## 8. Bidirectional Sync — How Lite and Heavy Stay Coherent

Sync is not a feature bolted on top. It is the protocol by which the mothership has a single brain across many bodies.

```
   ┌──────────── Lite node (laptop, café) ────────────┐
   │                                                  │
   │   memory/  ──▶ Postgres (lite ledger)            │
   │                       │                          │
   │                       ▼                          │
   │   Memory API ──▶ Redis pub/sub (intra-node)      │
   │                       │                          │
   │                       ▼                          │
   │   Fleet daemon ──▶ NATS leaf link  ──────┐       │
   │                                          │       │
   └──────────────────────────────────────────┼───────┘
                                              │
                                              │  outbound TLS, NAT-traversing
                                              ▼
                              ┌────────────────────────────┐
                              │  Public NATS hub           │
                              │  (Synadia NGS / Fly.io)    │
                              │  JetStream + KV cluster    │
                              └──────────────┬─────────────┘
                                             │
                                             │  outbound TLS
                                             ▼
   ┌──────────── Heavy node (home server) ───────────┐
   │                                                  │
   │   Fleet daemon ──▶ NATS leaf link ◀─────────────┐│
   │                                                 ││
   │   ▼                                             ││
   │   Memory API ──▶ Postgres (canonical ledger)    ││
   │                       │                         ││
   │                       └──▶ JetStream replays    ││
   │                            from KV `evidence_   ││
   │                            ledger` stream       ││
   │                                                 ││
   └─────────────────────────────────────────────────┘│
                                                      │
                                                      ▼
                                  Lite reads JetStream KV updates and
                                  reconciles its local ledger
```

**Three streams carry the sync:**

1. `fleet_roster` (JetStream KV) — who is alive, which capabilities each node has, last-seen timestamps. Lifted directly from Multi-Fleet.
2. `evidence_ledger` (JetStream stream) — every `RawEvent`, `Claim`, `Evidence`, `Outcome`, promotion, and retirement is published. Subscribers replay on reconnect.
3. `injection_signal` (Redis pub/sub, intra-machine + JetStream subject for cross-machine) — "new InjectionCandidate available for session X."

**Conflict resolution:**

- KV writes are last-write-wins with a Lamport clock per record.
- `Claim` updates carry an evidence-merge rule: contesting evidence is *appended*, never overwritten. A claim cannot be "force-promoted" remotely — promotion requires the local promoter to see the supporting evidence in its own ledger first.
- `RetiredWisdom` is monotonic: once retired, the only way back is a new `Claim` with new `Evidence`.

The leaf-node mechanism is documented in detail in [docs/plans/2026-04-23-nats-leaf-node-plan.md](docs/plans/2026-04-23-nats-leaf-node-plan.md). Bidirectional sync layers on top of that transport.

---

## 9. OS-Wide Injection — Design Sketch

Today the mothership injects payloads into IDEs via `userPromptSubmit` hooks. The longer-term direction is **a system-wide payload bus that any application can subscribe to**.

```
   ┌───────────────────────────────────────────────────────────────────┐
   │                    OS-WIDE PAYLOAD BUS  (future)                  │
   │                                                                   │
   │   ┌─────────────────────────────────────────────────────────────┐ │
   │   │  Mothership payload assembler (existing webhook host)       │ │
   │   └────────────────────────────┬────────────────────────────────┘ │
   │                                │                                  │
   │           NATS subject  `injection.payload.v1.<client_id>`        │
   │                                │                                  │
   │   ┌────────────────────────────┼────────────────────────────┐     │
   │   │                            │                            │     │
   │   ▼                            ▼                            ▼     │
   │ IDE plugin                Spotlight-style              Browser     │
   │ (existing)                quick-launcher               extension   │
   │                           (new)                        (new)       │
   │                                                                   │
   │   ▼                            ▼                            ▼     │
   │ Claude Code,               System-wide                Reads page  │
   │ Cursor, VS Code,           Cmd-Space input            context, asks│
   │ Codex, Gemini              injects payload            mothership   │
   │                                                                   │
   └───────────────────────────────────────────────────────────────────┘
```

**Subscription contract** (proposed):

```python
class OSWidePayloadSubscriber(BaseModel):
    client_id: str                    # stable per-app identifier
    capabilities_required: List[str]  # ["S0", "S1", "S2"] or ["all"]
    token_budget_max: int             # client's local limit
    transport: Literal["nats", "unix_socket", "http_sse"]


class OSWidePayloadRequest(BaseModel):
    client_id: str
    prompt_text: str
    context_hints: dict               # app name, document path, etc.


class OSWidePayloadResponse(BaseModel):
    request_id: str
    payload: ContextPayload           # the typed schema from §3
    delivery_channel: Literal["nats", "unix_socket", "http_sse"]
```

The design constraint: **the mothership stays the single source of truth.** OS-wide injection is a *transport* extension, not a data-model extension. Same `ContextPayload`, more subscribers. See [docs/sync/os-wide-injection.md](docs/sync/os-wide-injection.md) for the longer plan.

---

## 10. Related Repositories

The mothership is a superrepo that orchestrates multiple independent OSS projects. Each lives in its own repo and is brought in as a submodule.

| Repo | Role in the mothership | URL |
|------|------------------------|-----|
| `context-dna` | TypeScript memory engine — claim graph, evidence flow, ledger primitives | https://github.com/supportersimulator/context-dna |
| `3-surgeons` | Cross-examination protocol — three independent LLMs return verdicts that feed the ledger as `Evidence` records | https://github.com/supportersimulator/3-surgeons |
| `multi-fleet` | Cross-machine transport — 9-priority cascade, JetStream replication, fleet-roster KV | https://github.com/supportersimulator/multi-fleet |
| `superpowers` | Process discipline — TDD, debugging, verification, code review skills that compose with the mothership loop | https://github.com/supportersimulator/superpowers |
| `admin.contextdna.io` | Next.js IDE shell — the human-facing surface for the mothership (lives at `apps/ide-shell/` as a submodule) | (private; pulled as submodule) |
| `er-simulator-superrepo` | The development superrepo this mothership was extracted from. Contains additional vertical (ER Simulator) on top of the same memory ledger. | (private) |

Adjacent reference plans inside this repo:

- [docs/plans/2026-04-23-nats-leaf-node-plan.md](docs/plans/2026-04-23-nats-leaf-node-plan.md) — NATS leaf-node transport that powers bidirectional sync.
- [docs/plans/2026-05-13-contextdna-ide-mothership-plan.md](docs/plans/2026-05-13-contextdna-ide-mothership-plan.md) — the master plan that produced this architecture document.
- [docs/sync/bidirectional.md](docs/sync/bidirectional.md) — bidirectional sync details (to be authored as part of Phase 4).
- [docs/sync/os-wide-injection.md](docs/sync/os-wide-injection.md) — OS-wide injection direction (to be authored as part of Phase 6).

---

## 11. Invariants

These cannot be relaxed without breaking the mothership claim.

1. **Zero Silent Failures.** Every exception path bumps a named counter. `except Exception: pass` is forbidden. CI gate (`scripts/zsf-bare-except.sh`) enforces this.
2. **Evidence Before Wisdom.** A `Claim` cannot become `PromotedWisdom` without recorded `Evidence` of polarity `supports`. The Pydantic validator on `PromotedWisdom` requires `supporting_evidence_count > 0`.
3. **Retirements are monotonic.** Once a `Claim` or `PromotedWisdom` is retired, the original record cannot be edited or un-retired. New claims must be created if the wisdom should re-enter circulation.
4. **Sync is last-write-wins on KV, append-only on evidence.** Heavy and Lite cannot silently overwrite each other's evidence ledger.
5. **Reconstitution test in CI.** `scripts/bootstrap-from-scratch.sh` must exit 0 on a fresh checkout. If it fails, the mothership claim fails with it.

---

**This document is the schematic. The README is the manifesto. Together they describe the mothership Aaron carries from machine to machine.**
