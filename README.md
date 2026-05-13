# ContextDNA IDE

> Persistent, evidence-based contextual memory for AI coding sessions.

ContextDNA IDE gives your AI assistant a living memory that persists across sessions,
learns from outcomes, and injects structured context at the right moment — so you stop
re-explaining your codebase and start building at speed.

## What it does

- **Session Memory**: Every coding session is indexed, searchable, and auto-summarized
- **Evidence-Based Context**: Webhook injects 9 sections of structured context per prompt
- **Professor Wisdom**: Pattern library from past sessions surfaces proactively
- **Synaptic Intelligence**: 8th intelligence layer for deep architectural reasoning
- **Multi-Model Consensus**: 3-Surgeons integration catches blind spots before you commit
- **Fleet Coordination**: Multi-Fleet integration for multi-machine AI collaboration

## Architecture

```
User Prompt
     │
     ▼
Claude Code (or Cursor / VS Code)
     │
     ▼  (pre-prompt hook)
ContextDNA Webhook
     │
     ├── S0: Safety Rails
     ├── S1: Foundation (file paths, entry points)
     ├── S2: Professor Wisdom (patterns, landmines)
     ├── S3: Awareness (SOPs, past mistakes)
     ├── S4: Deep Context (architecture twin)
     ├── S5: Protocol (risk-calibrated behavior)
     ├── S6: Holistic (cross-cutting concerns)
     ├── S7: Full Library (evidence ledger)
     └── S8: 8th Intelligence (Synaptic layer)
          │
          ▼
     9-Section Payload injected into prompt
          │
          ▼
     Session Memory  ◄──────────────────────────────┐
          │                                          │
          ▼                                          │
     Evidence DB  ──── auto-learns from outcomes ───┘
```

## Quick Start

```bash
# Install
pip install context-dna-ide

# Initialize in your project
cd your-project
context-dna-ide init

# Install the Claude Code hook (runs before every prompt)
context-dna-ide hooks install claude

# Record your first win
context-dna-ide win "Fixed async bug" "Used asyncio.to_thread() wrapper"

# Query context before a task
context-dna-ide consult "deploy to production"

# Check system health
context-dna-ide health
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXTDNA_LLM_PROVIDER` | `deepseek` | LLM backend (`openai`, `anthropic`, `deepseek`, `ollama`) |
| `CONTEXTDNA_LLM_API_KEY` | — | API key for chosen provider |
| `CONTEXTDNA_INJECTION_MODE` | `hybrid` | Context injection mode (`hybrid`, `greedy`, `layered`, `minimal`) |
| `NATS_URL` | `nats://localhost:4222` | NATS server for fleet coordination (optional) |
| `CONTEXTDNA_NODE_ID` | `local` | Node identifier for multi-machine setups |

## Injection Modes

| Mode | Token Cost | When to Use |
|------|-----------|-------------|
| `greedy` | High | Maximum context, complex tasks |
| `hybrid` | Medium | Default — balanced performance |
| `layered` | Low | Token-constrained environments |
| `minimal` | Minimal | Quick queries only |

## Integrations

### Claude Code (primary)

```bash
context-dna-ide hooks install claude
```

Installs a `userPromptSubmit` hook that fires before every prompt, injecting the 9-section payload.

### Cursor

```bash
context-dna-ide hooks install cursor
```

### Git

```bash
context-dna-ide hooks install git
```

Captures every commit as a learning event.

### 3-Surgeons (multi-model consensus)

Requires the [3-Surgeons CLI](https://github.com/supportersimulator/3-surgeons):

```bash
pip install 3-surgeons
context-dna-ide integrations enable 3-surgeons
```

Enables pre-implementation adversarial review by three independent LLMs before any code is written.

### Multi-Fleet (multi-machine coordination)

Requires NATS running:

```bash
docker run -p 4222:4222 nats:latest
context-dna-ide integrations enable multi-fleet
```

Enables context sharing and task coordination across multiple machines.

## MCP Servers

ContextDNA IDE ships four MCP servers for deep IDE integration:

| Server | Purpose |
|--------|---------|
| `contextdna-engine` | Core memory read/write |
| `contextdna-webhook` | Webhook delivery pipeline |
| `projectdna` | Project-level DNA extraction |
| `synaptic` | 8th Intelligence reasoning layer |

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "contextdna-engine": {
      "command": "python3",
      "args": ["-m", "contextdna_ide.mcp.engine"]
    },
    "projectdna": {
      "command": "python3",
      "args": ["-m", "contextdna_ide.mcp.projectdna"]
    }
  }
}
```

## Health Check

```bash
# 17-point gains gate — all must pass before proceeding
./scripts/gains-gate.sh

# Cardiologist EKG — architectural health
./scripts/cardio-gate.sh
```

## How It Learns

1. **Capture**: Every command, file change, and git commit is observed
2. **Verify**: Only objective successes are recorded (user/system confirmation required — no self-reporting)
3. **Consolidate**: Learnings are pattern-matched against the evidence ledger
4. **Inject**: Relevant patterns surface automatically in the next session

This eliminates the "10 failures for every 1 success" problem — only verified outcomes
enter the knowledge base.

## Zero Silent Failures

ContextDNA IDE enforces observable failures:

- Every exception is routed to an observable channel (health endpoint, log, or gains gate)
- `except Exception: pass` is forbidden in the codebase
- Failed webhook deliveries increment a counter visible at `/health`
- Gains gate catches silent regressions in CI

## License

MIT — see [LICENSE](LICENSE)

## Contributing

PRs welcome. Before submitting:

1. Run `./scripts/gains-gate.sh` — all 17 checks must pass
2. Run `./scripts/cardio-gate.sh` — architectural health check
3. No `except Exception: pass` — all failures must be observable
4. Tests required for new memory or webhook behavior

## Acknowledgments

Built on top of:
- [NATS](https://nats.io) — messaging backbone
- [3-Surgeons](https://github.com/supportersimulator/3-surgeons) — multi-model consensus
- [Multi-Fleet](https://github.com/supportersimulator/multi-fleet) — fleet coordination
