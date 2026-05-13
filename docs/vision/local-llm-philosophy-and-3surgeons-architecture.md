# Local LLM Philosophy & 3-Surgeons Architecture

> **Date**: 2026-04-16
> **Author**: Atlas (mac1)
> **Status**: Fleet evaluation requested — each machine should run 3-surgeons cross-exam on this document
> **Origin**: Batman/Alfred analogy research (context-dna/docs/Agents-Evidence-Based-Context-and-Local-LLM-Butler-Trainers.md)

---

## The Batman/Alfred Model

```
Claude Code = Batman (the Superhero)
Local LLM   = Alfred (the Butler)

Batman arrives at the mansion. Alfred has ALREADY prepared:
- Hot meal on silver platter (contextual awareness)
- Right tools laid out (relevant file contexts)
- Intelligence briefing ready (patterns, gotchas)
- Threats identified (potential landmines)

Batman doesn't tell Alfred what to prepare.
Alfred ANTICIPATES Batman's needs independently.
This is what makes Alfred invaluable.
```

### Job Separation Principle

| Claude Code (API-derived) | Local LLM (Butler) |
|---------------------------|---------------------|
| Pre-frontal cortex | Subconscious mind |
| Conscious decision-making | Background preparation |
| Arrives to execute task | ALREADY prepared context |
| Receives silver platter | CREATED the silver platter |
| Doesn't direct butler | Works INDEPENDENTLY |

**These jobs MUST remain separate and distinct.**

### The Mansion Architecture

```
Atlas = Batman         -> Arrives for missions (user tasks)
Synaptic/LLM = Alfred -> Runs the mansion (Context DNA)

Context DNA = The Mansion
  |-- Evidence pipeline = the plumbing
  |-- SOPs = the library
  |-- Section 8 = Alfred's voice
  |-- Professor = the kitchen
  |-- Outcome attribution = the accounting books

Future apps (ER Simulator, etc.) = Batman missions
  |-- Batman can only take on missions when the mansion runs itself
  |-- Butler must prove competence BEFORE new missions begin
```

**Key Insight**: Butler INDEPENDENCE is the prerequisite for system growth. If Atlas must supervise butler operations, Atlas cannot focus on user tasks. The mansion must run itself.

---

## Our Local LLM Stack (NOT Ollama)

### What We Actually Run

| Component | Port | Purpose |
|-----------|------|---------|
| **MLX server** (`mlx_lm.server`) | 5044 | Direct local inference — Qwen3-4B-4bit on Apple Silicon |
| **Priority proxy** (`llm_priority_proxy.py`) | 5045 | No-stampede serialization — prevents concurrent GPU access |
| **LLM priority queue** (`llm_priority_queue.py`) | — | Python-level priority routing (P1-P4) via Redis GPU lock |

### Why MLX, Not Ollama

| Dimension | MLX | Ollama |
|-----------|-----|--------|
| **Metal optimization** | Native Apple Silicon, written for Metal | Generic GGML backend |
| **Memory efficiency** | Shared CPU/GPU memory, zero-copy | Separate model loading |
| **Priority integration** | Wrapped by our priority proxy (port 5045) | No priority queue support |
| **Model format** | MLX-native quantization (mlx-community/) | GGUF conversion |
| **Ecosystem fit** | Integrates directly with Context DNA scheduler | Standalone daemon |

MLX was chosen because our fleet is 100% Apple Silicon (Mac Mini M4, MacBook Pro M5). MLX exploits the unified memory architecture — the model sits in shared memory accessible to both CPU and GPU with zero-copy overhead. Ollama adds an abstraction layer we don't need.

### Priority System (Anti-Stampede)

The local LLM serves many callers: webhook injection (S2/S8), anticipation engine, gold mining, scheduler, diagnostics. Without prioritization, background batch jobs (1K+ calls/hr) would starve interactive requests.

| Priority | Use | Who |
|----------|-----|-----|
| P1 AARON | Webhook S2/S8, voice, Aaron-facing | Reserved for direct experience |
| P2 ATLAS | Diagnostics, professor queries, task-critical | Atlas operational needs |
| P3 EXTERNAL | External API requests | Remote callers |
| P4 BACKGROUND | Gold mining, anticipation, batch jobs | Scheduler (1K+ calls/hr) |

**Rule**: ALL LLM access goes through the priority proxy (port 5045). NEVER call port 5044 directly. P1/P2 preempt P4 via Redis `llm:gpu_urgent` flag.

### Per-Machine Configuration

| Machine | Local LLM | Neurologist Endpoint | Fallback |
|---------|-----------|---------------------|----------|
| **mac1** (chief) | MLX Qwen3-4B-4bit on port 5044 | `http://127.0.0.1:5045/v1` (priority proxy) | GPT-4.1-mini via external API |
| **mac2** | No local LLM | `https://api.deepseek.com/v1` (API) | — |
| **mac3** | No local LLM | `https://api.deepseek.com/v1` (API) | — |

**Config location**: `~/.3surgeons/config.yaml` (per-machine). Project `.3surgeons.yaml` sets cardiologist only — neurologist intentionally per-machine.

---

## 3-Surgeons Architecture

### The Three Surgeons

| Surgeon | Model | Role | Cost |
|---------|-------|------|------|
| **Atlas** (Head Surgeon) | Claude (session) | Synthesizes, decides, implements | $0 (session) |
| **Cardiologist** | GPT-4.1-mini (OpenAI API) | External perspective, cross-examination | ~$0.40/1M tokens |
| **Neurologist** | Qwen3-4B (local MLX via priority proxy) | Pattern recognition, corrigibility, classification | $0 (local) |

### Why Three Models Matter

**"The value is in the disagreements, not the agreements."**

- Claude may confabulate confidently where GPT hedges
- Local 4B model catches patterns API models miss due to token limits
- Three different training sets, three different biases, three different blind spots
- When all three agree → high confidence
- When they disagree → exact spot needing human judgment

### The Neurologist's Unique Value

The local LLM as Neurologist provides capabilities no API model can:

1. **Zero-cost classification** — P4 background calls run 1K+/hr at $0. Same volume on GPT-4.1-mini = $4-16/day
2. **No data leaves the machine** — security-sensitive pattern recognition stays local
3. **Priority-aware** — yields to interactive requests, never blocks the user experience
4. **Always available** — no network dependency, no rate limits, no API outages
5. **Corrigibility checks** — challenges Atlas assumptions before commits (`neurologist challenge`)
6. **Subconscious pattern recognition** — cross-session pattern detection Atlas can't see in one context window

### Invariance Gate System

5 epistemological invariance skills enforce truth calibration:

| Skill | Gate Type | When |
|-------|-----------|------|
| `architectural-gate` | HARD-GATE | Before arch decisions, >3 files, security/schema/API changes |
| `counter-position` | HARD-GATE | High confidence + low evidence, single surgeon dominates |
| `pre-implementation-review` | HARD-GATE | Before coding starts |
| `post-implementation-verification` | HARD-GATE | Before claiming "done" |
| `invariance-health` | Automatic | Every 10 gate invocations (metacognition) |

---

## What Was Fixed (2026-04-16)

The 3-surgeons skill files (`using-3-surgeons`, `setup-team`, `probe`) incorrectly referenced Ollama as the default/primary local backend. Fixed to reflect our actual stack:

- **Neurologist description**: "local Ollama/MLX" → "local MLX via priority proxy"
- **Setup guidance**: Ollama-first install instructions → MLX-first (macOS), Ollama as cross-platform fallback
- **Probe troubleshooting**: "Start Ollama: `ollama serve`" → "Start MLX: `mlx_lm.server`" with proxy instructions
- **Latency benchmarks**: "Neurologist (local Ollama)" → "Neurologist (local MLX via proxy)"

Ollama remains a supported provider in the providers table — it's valid for non-Apple-Silicon machines or cross-platform setups. But it's not our stack.

---

## Fleet Evaluation Request

**Each machine in the fleet should run a 3-surgeons cross-examination on this document:**

```bash
3s cross-exam "Evaluate the local LLM philosophy and 3-surgeons architecture described in docs/vision/local-llm-philosophy-and-3surgeons-architecture.md. Questions to address: (1) Is the Batman/Alfred job separation principle correctly implemented in our current stack? (2) Is MLX the right choice over Ollama for our Apple Silicon fleet? (3) Are there gaps in the priority proxy system? (4) Should mac2/mac3 run local LLMs or is API-only neurologist acceptable? (5) What improvements would strengthen the 3-surgeon consensus system?"
```

**Expected outputs per machine:**
- mac1: Full 3-surgeon cross-exam (local neurologist available)
- mac2: 2-surgeon cross-exam (API neurologist) — note any perspective gaps from missing local model
- mac3: 2-surgeon cross-exam (API neurologist) — same

**Convergence criteria**: Fleet consensus on each of the 5 questions. Disagreements preserved for Aaron's review.

---

## Constitutional Physics (Governing Principles)

1. **Preserve Determinism** — same inputs → same outputs. If not → SAFE MODE
2. **No Discovery at Injection** — injection = retrieval + assembly only
3. **Evidence Over Confidence** — outcomes determine truth
4. **Prefer Reversible Actions** — checkpoint before risk
5. **Minimalism** — maximum value density. Scalpel, not axe
