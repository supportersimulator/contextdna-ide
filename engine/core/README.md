# Context DNA 🧬

> Autonomous learning system for developers. Captures wins, fixes, and patterns automatically.

## ⚠️ Boundary Status (2026-03-03)

**This directory (`core/`) is FROZEN.** It represents the aspirational "what customers install"
boundary — the distributable pip package, separate from project infrastructure in `../src/`.

- **`core/src/`** = curated product subset (58 files, last synced ~Jan 27 2026)
- **`../src/`** = active development tree (82+ files, all current work)
- **`../engine/`** = TypeScript kernel (`@contextdna/engine` npm package)

**Decision:** The product boundary concept is correct (V3 complexity vector: tool-that-builds-itself),
but resurrection requires a dedicated workstream to forward-port fixes and establish sync.
Until then, `../src/` is the single source of truth for Python. Do not develop in `core/`.

**When to resurrect:** Before PyPI distribution. Workstream: forward-port, curate, separate pyproject.toml.
See `docs/plans/2026-03-03-doc-gap-analysis.md` for full context.

Context DNA is your personal knowledge base that learns as you work. It captures what works, what doesn't, and why - so you never repeat mistakes and always have context when you need it.

## ✨ Features

- **🎯 Automatic Learning Capture** - Git commits, IDE hooks, manual entries
- **🔍 Semantic Search** - Find related learnings using AI-powered search
- **🤖 Multi-Provider LLM** - OpenAI, Anthropic, Ollama (local), or LM Studio
- **📝 SOP Extraction** - Automatically extract procedures from your work
- **🔌 IDE Integration** - Claude Code, Cursor, VS Code, or any terminal
- **💾 Zero Cloud Lock-in** - Run entirely local with Ollama (Pro tier)

## 🚀 Quick Start

```bash
# Install Context DNA
pip install context-dna

# Start infrastructure (requires Docker)
context-dna setup

# Initialize in your project
cd your-project
context-dna init

# Install IDE hooks
context-dna hooks install claude  # or: cursor, git

# Start learning!
context-dna win "Fixed async bug" "Used asyncio.to_thread() wrapper"
context-dna query "async"
context-dna consult "deploy to production"
```

## 📦 Installation

### Basic (SQLite backend)

```bash
pip install context-dna
context-dna init
```

### Full (Docker + PostgreSQL + pgvector)

```bash
pip install context-dna[full]
context-dna setup
context-dna init --backend pgvector
```

### One-Line Setup

```bash
curl -sSL https://context-dna.dev/setup.sh | bash
```

## 💡 Usage

### Recording Learnings

```bash
# Record a win (something that worked)
context-dna win "Fixed async bug" "Used asyncio.to_thread() for blocking calls"

# Record a fix/gotcha (problem + solution)
context-dna fix "Docker restart doesn't reload env" "Must recreate container with docker-compose up -d"

# Record a pattern (reusable code pattern)
context-dna pattern "Async wrapper" "Wrap blocking calls" --example "await asyncio.to_thread(func)"
```

### Searching

```bash
# Full-text search
context-dna query "async boto3"

# Search by type
context-dna query "docker" --type fix

# Get recent learnings
context-dna recent --hours 24
```

### Consulting (The Professor)

```bash
# Get context before starting work
context-dna consult "implement WebSocket reconnection"

# The professor returns:
# - Relevant gotchas to avoid
# - What worked before
# - Related patterns
```

### Status

```bash
context-dna status
# Context DNA Status
# =====================================
# Project:     my-project
# Backend:     pgvector
# Healthy:     Yes
#
# Total:       47 learnings
# Today:       3
# Last:        2024-01-15 14:32
#
# By Type:
#   win: 28
#   fix: 15
#   pattern: 4
```

## 🔌 IDE Integration

### Claude Code

```bash
context-dna hooks install claude
# Creates .claude/settings.local.json with hooks:
# - UserPromptSubmit: Consults memory before each task
# - PostToolUse: Captures successful bash commands
```

### Cursor

```bash
context-dna hooks install cursor
# Appends to .cursorrules with Context DNA integration instructions
```

### Git

```bash
context-dna hooks install git
# Creates .git/hooks/post-commit that auto-captures commits
```

## 🤖 LLM Providers

Context DNA supports multiple LLM providers with automatic selection:

| Provider | Cost | Embeddings | Best For |
|----------|------|------------|----------|
| **Ollama** | Free | ✅ | Local, offline, privacy |
| **OpenAI** | $0.00015/1K | ✅ | Fast, reliable |
| **Anthropic** | $0.003/1K | ❌ | Advanced reasoning |
| **LM Studio** | Free | ❌ | Local, custom models |

### Provider Priority

1. Local providers first (Ollama, LM Studio) - zero cost
2. Cloud providers as fallback (OpenAI, Anthropic)

```bash
# Check available providers
context-dna providers

# Force specific provider
export CONTEXT_DNA_LLM_PROVIDER=openai
```

## ⭐ Pro Tier

Unlock local LLM inference with optimized quantized models:

```bash
context-dna upgrade
# One-time payment: $29

# Pro features:
# ✓ Llama 3.1 8B Instruct (Q4_K_M quantized) - 4.7GB
# ✓ Nomic Embed Text (embeddings) - 274MB
# ✓ Auto-configured for your hardware
# ✓ Zero API costs after upgrade
# ✓ Works 100% offline
```

### Activate with License Key

```bash
context-dna upgrade --license MDNA-XXXX-XXXX-XXXX-XXXX
```

## 🐳 Docker Infrastructure

Context DNA uses Docker for its storage layer:

```yaml
services:
  postgres:     # pgvector for semantic search
  redis:        # Caching
  seaweedfs:    # Artifact storage
  ollama:       # Local LLM (optional)
```

### Start Infrastructure

```bash
context-dna setup                  # Core services
context-dna setup --with-ollama    # Include local LLM
```

### Manual Control

```bash
cd ~/.context-dna
docker-compose up -d              # Start all
docker-compose logs postgres      # View logs
docker-compose down               # Stop all
```

## 📁 Project Structure

```
.context-dna/
├── config.json       # Project configuration
├── learnings.db      # SQLite database (basic mode)
└── brain_state.md    # Current brain state

~/.context-dna/
├── license.json      # Pro license
├── pro_config.json   # Optimized LLM config
└── models/           # Local model cache
```

## 🔧 Configuration

### Environment Variables

```bash
# LLM API Keys (optional if using Ollama)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Database (defaults work for Docker)
CONTEXT_DNA_POSTGRES_URL=postgresql://context_dna:context_dna_dev@localhost:5432/context_dna
CONTEXT_DNA_REDIS_URL=redis://localhost:6379

# Override provider selection
CONTEXT_DNA_LLM_PROVIDER=ollama
CONTEXT_DNA_EMBEDDING_PROVIDER=ollama
```

### Project Config (.context-dna/config.json)

```json
{
  "version": "1.0.0",
  "project": "my-project",
  "storage": {
    "backend": "pgvector",
    "path": "postgresql://..."
  },
  "hooks": {
    "claude": true,
    "cursor": true,
    "git": true
  },
  "capture": {
    "git_commits": true,
    "test_results": true,
    "deployments": true
  }
}
```

## 🧪 Python API

```python
from context_dna import brain

# Initialize
brain.init()

# Record learnings
brain.win("Fixed bug", "Used async wrapper")
brain.fix("Docker env issue", "Recreate container")
brain.pattern("Retry pattern", "Exponential backoff", example="...")

# Search
results = brain.query("async")
for r in results:
    print(f"{r.title}: {r.content}")

# Get wisdom
context = brain.consult("deploy to production")
print(context)
```

## 📊 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER INTERFACE                           │
│  CLI: context-dna | Python API | IDE Hooks                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         BRAIN                                    │
│  Orchestrates learning lifecycle                                 │
│  Routes to LLM providers                                         │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│     Ollama      │  │     OpenAI      │  │   Anthropic     │
│ (Local, Free)   │  │ (Cloud, Fast)   │  │ (Cloud, Smart)  │
└─────────────────┘  └─────────────────┘  └─────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         STORAGE                                  │
│  PostgreSQL + pgvector | Redis | SeaweedFS                       │
└─────────────────────────────────────────────────────────────────┘
```

## 🤝 Contributing

Contributions welcome! Please read our [Contributing Guide](CONTRIBUTING.md).

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

## 🔗 Links

- **Website**: https://context-dna.dev
- **Documentation**: https://context-dna.dev/docs
- **GitHub**: https://github.com/context-dna/context-dna
- **Discord**: https://discord.gg/context-dna

---

**Made with ❤️ by developers, for developers.**
