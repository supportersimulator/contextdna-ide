# Context DNA Memory System

Sub-project: **context-dna** (memory/, context-dna/, acontext/)

This is the autonomous architecture brain — the mansion that Atlas launches missions from.

## Rehydration

When starting work here, get project-scoped context:
```bash
PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate --project context-dna
```

## Key Architecture
- 9-section webhook injection (persistent_hook_structure.py)
- Evidence pipeline: claim → quarantine → wisdom promotion
- Butler jobs: lite_scheduler.py (24 jobs, 2-10min intervals)
- Session historian: session_historian.py (live extraction + LLM analysis)
- Local LLM: Qwen3-4B-4bit on port 5044 (mlx_lm.server, start: ./scripts/start-llm.sh)
- Storage: SQLite (local) + PostgreSQL (Docker) + Redis (caching)

## Constraints
- SQLiteStorage() → ALWAYS use get_sqlite_storage() singleton
- localhost → 127.0.0.1 (IPv6 resolution issues on macOS)
- reward=-0.3 is codebase convention for negative outcomes
- Python sqlite3 `with connect() as conn:` does NOT close — use try/finally
