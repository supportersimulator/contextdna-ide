# ContextDNA IDE — Open Source Extraction Plan

**Date**: 2026-05-13  
**Status**: Draft  
**Goal**: Extract ContextDNA IDE as a standalone, installable public repo for open-source release.

---

## What ContextDNA IDE Is

A persistent, evidence-based contextual memory system for AI coding agents. It gives AI assistants (Claude Code, Cursor, VS Code) a living memory that persists across sessions, learns from outcomes, and injects structured context at exactly the right moment — eliminating the "re-explain your codebase" tax.

Core value proposition: **Webhook injects 9 sections of structured context per prompt. Session memory persists. Evidence accumulates. First-try success rate lifts from ~60% to ~90%+.**

---

## Target Public Repo

`supportersimulator/contextdna-ide` (or `context-dna/contextdna-ide`)

Scaffold lives at: `contextdna-ide-oss/` within this superrepo until extraction.

---

## What Gets Included

### Python Core (`memory/`)
| File/Dir | Purpose |
|----------|---------|
| `brain.py` | Master context controller — auto-capture, verify, consolidate, inject |
| `session_historian.py` | Session indexing, rehydration, summarization |
| `anticipatory_butler.py` | Proactive context surface (butler protocol) |
| `professor.py` | Pattern library, landmine surface, wisdom injection |
| `webhook_health_publisher.py` | Observable channel for all webhook events |
| `webhook_section_notifications.py` | 9-section payload builder |
| `unified_injection.py` | Injection mode orchestration (hybrid/greedy/layered/minimal) |
| `unified_storage.py` | Evidence DB, session storage |
| `architecture.py` | Architecture twin + blueprint generation |
| `llm_priority_queue.py` | Multi-provider LLM with priority lanes |
| `agent_service.py` | Core agent orchestration |
| `anticipation_engine.py` | Deep pattern anticipation |
| `providers/` | OpenAI, Anthropic, DeepSeek, Ollama provider adapters |
| `ide_adapters/` | Claude Code, Cursor, VS Code hook wiring |
| `tests/` | Full test suite |

### MCP Servers (`mcp-servers/`)
| File/Dir | Purpose |
|----------|---------|
| `contextdna_engine_mcp.py` | Core ContextDNA MCP server |
| `contextdna_webhook_mcp.py` | Webhook delivery MCP server |
| `projectdna_mcp.py` | Project-level DNA extraction |
| `synaptic_mcp.py` | 8th Intelligence MCP server |
| `event-bridge/` | Event routing between fleet nodes |
| `evidence-stream/` | Live evidence ingestion stream |
| `race-theater/` | Race condition test theater |
| `Dockerfile.mcp` | Container build for MCP stack |

### TypeScript Engine (`context-dna/`)
Full standalone TypeScript package (already pip-installable as `context-dna`).
Includes: `core/`, `engine/`, `clients/`, `src/`, `bundles/`, `infra/`, `migrations/`, `tests/`

### Scripts
| Script | Purpose |
|--------|---------|
| `scripts/auto-memory-query.sh` | Main webhook query pipeline (layered + hybrid branches) |
| `scripts/gains-gate.sh` | 17-check health gate, <30s |
| `scripts/cardio-gate.sh` | Cardiologist EKG gate |
| `scripts/fleet-check.sh` | Fleet health dashboard |

### Integrations (as submodule references, not embedded)
- `3-surgeons/` — Multi-model consensus (referenced, not forked)
- `multi-fleet/` — Multi-machine coordination (referenced, not forked)

### Config/Docs
- `CLAUDE.md` (sanitized — no personal email/phone/customer data)
- `ARCHITECTURE.md`
- `docs/` (vision/, dao/ only — not reflect/ or inbox/)
- `.mcp.json` (template with placeholders, no real keys)

---

## What Gets EXCLUDED

### Data & Secrets
- `memory/*.db`, `memory/*.sqlite`, `memory/*.db-shm`, `memory/*.db-wal` — local runtime data
- `memory/.*.json` — runtime state files (ab_testing_log, active_session_injections, etc.)
- `.env` files with real values (template `.env.example` included instead)
- `memory/family_wisdom/` — personal pattern library
- Any hardcoded Aaron email (`your@email.com`), phone, or customer PII

### Personal/Private Dirs
- `google-drive-code/` — personal Google Drive sync
- `docs/reflect/` — personal session journals
- `docs/inbox/` — personal inbox
- `submissions/` — runtime data
- `logs/`, `artifacts/`, `dashboard_exports/` — runtime artifacts
- `admin.ersimulator.com/`, `admin.contextdna.io/` — product admin UIs (separate repos)
- `er-simulator/`, `ersim-voice-stack/` — ER Simulator product (separate repo)
- `backend/` — ER Simulator backend

### ER Simulator Specifics
- Any file importing ER Simulator domain logic
- `AGENT_*.md` files with fleet-specific topology
- `.fleet-messages/` — inter-fleet comms (runtime)

---

## Sanitization Rules

1. Replace all occurrences of `your@email.com` with `your@email.com` placeholder
2. Replace `aarontjomsland` paths with `$HOME` or generic user paths in docs
3. Strip `NATS_URL` real values from any checked-in config — replace with `nats://localhost:4222`
4. Strip any OpenAI/Anthropic/DeepSeek API keys — replace with `YOUR_API_KEY_HERE`
5. `.env.example` ships with all vars documented, no real values

---

## Repository Structure (Target)

```
contextdna-ide/
├── README.md                    # This is the face of the product
├── ARCHITECTURE.md              # System architecture
├── LICENSE                      # MIT
├── NOTICE                       # Attribution
├── pyproject.toml               # pip install context-dna-ide
├── .env.example                 # All env vars documented, no real values
├── .gitignore                   # Excludes all runtime/data files
├── memory/                      # Python core
├── mcp-servers/                 # MCP server stack
├── context-dna/                 # TypeScript engine (pip-installable)
├── scripts/                     # Automation scripts
│   ├── auto-memory-query.sh
│   ├── gains-gate.sh
│   ├── cardio-gate.sh
│   └── fleet-check.sh
├── docs/
│   ├── vision/                  # Architecture vision docs
│   ├── dao/                     # Constitutional docs
│   └── plans/                   # Implementation plans
└── tests/                       # Integration test suite
```

---

## Extraction Steps

1. Create `contextdna-ide-oss/` scaffold in this superrepo (DONE — this plan + README)
2. Write sanitization script (`scripts/sanitize-for-oss.sh`) that strips PII and runtime data
3. Run sanitization on a branch, verify no secrets remain (`git secrets --scan`)
4. Create public GitHub repo `supportersimulator/contextdna-ide`
5. Push sanitized branch
6. Wire CI: `gains-gate.sh` as GitHub Action health check
7. Publish to PyPI as `context-dna-ide` (separate from existing `context-dna`)
8. Update `context-dna/README.md` to cross-link

---

## Success Criteria

- [ ] `pip install context-dna-ide` works from scratch on a clean machine
- [ ] `context-dna-ide init` creates working webhook hook in Claude Code
- [ ] Gains gate (17 checks) passes in CI
- [ ] Zero secrets in git history (`git secrets --scan-history` clean)
- [ ] README gives a working Quick Start in under 5 minutes
