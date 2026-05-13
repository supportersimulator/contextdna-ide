"""Context DNA Local API Server.

This server provides a unified REST API that all visualization clients consume:
- Next.js Dashboard (core UI)
- xbar (macOS menu bar)
- VS Code Extension
- Raycast Extension
- Any other client

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    Context DNA Server                             │
    │                  (localhost:3456)                                │
    │                                                                  │
    │  ┌──────────────────────────────────────────────────────────┐  │
    │  │                    REST API                               │  │
    │  │  GET  /api/stats      - Quick stats (for menu bars)      │  │
    │  │  GET  /api/learnings  - List learnings (paginated)       │  │
    │  │  POST /api/win        - Record a win                      │  │
    │  │  POST /api/fix        - Record a fix                      │  │
    │  │  POST /api/query      - Semantic search                   │  │
    │  │  POST /api/consult    - Get context for task              │  │
    │  │  GET  /api/recent     - Recent activity                   │  │
    │  │  WS   /ws/updates     - Real-time updates                 │  │
    │  └──────────────────────────────────────────────────────────┘  │
    │                           │                                      │
    │  ┌──────────────────────────────────────────────────────────┐  │
    │  │                    Brain (Core)                           │  │
    │  │  - LLM Providers (Ollama, OpenAI, Anthropic)             │  │
    │  │  - Storage (SQLite/PostgreSQL + pgvector)                │  │
    │  │  - Extraction (SOP, patterns)                             │  │
    │  └──────────────────────────────────────────────────────────┘  │
    └─────────────────────────────────────────────────────────────────┘
                                │
            ┌───────────────────┼───────────────────┐
            │                   │                   │
            ▼                   ▼                   ▼
    ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
    │  Next.js UI   │   │    xbar       │   │  VS Code      │
    │  (Dashboard)  │   │  (Menu Bar)   │   │  (Extension)  │
    │  Full-featured│   │  Quick stats  │   │  Sidebar      │
    │  CRUD + viz   │   │  + quick add  │   │  + commands   │
    └───────────────┘   └───────────────┘   └───────────────┘

All clients consume the SAME API - update once, reflected everywhere.
"""

from .api import create_app, run_server

__all__ = ["create_app", "run_server"]
