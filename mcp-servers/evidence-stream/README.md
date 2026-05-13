# evidence-stream

MCP server + HTTP side-car that exposes the 3-Surgeons evidence DB as a live stream.

## Endpoints (HTTP, port 8878)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | `{"ok": true, "counters": {...}}` |
| GET | `/evidence/latest?n=10` | JSON array of last N evidence rows |
| GET | `/evidence/stream` | SSE feed — pushes new rows every 5 s |

## MCP tools

| Tool | Args | Description |
|------|------|-------------|
| `get_evidence_latest` | `n` (int, default 10) | Last N entries from evidence DB |
| `search_evidence` | `query` (str), `limit` (int, default 5) | FTS5 search (LIKE fallback) |

## Running

```bash
# HTTP only (for testing panel.html)
python3 mcp-servers/evidence-stream/server.py --http-only

# MCP stdio + HTTP side-car
python3 mcp-servers/evidence-stream/server.py
```

## MCP registration snippet (.mcp.json)

```json
{
  "mcpServers": {
    "evidence-stream": {
      "command": "python3",
      "args": ["mcp-servers/evidence-stream/server.py"],
      "env": {}
    }
  }
}
```

## Panel

Open `mcp-servers/evidence-stream/panel.html` in a browser while the server runs.
The panel:
- Loads last 50 entries via `/evidence/latest` on boot
- Subscribes to `/evidence/stream` (SSE) for live updates
- Displays per-entry sha256 hash and `prev_hash` chain integrity check
- Auto-reconnects on SSE disconnect (ZSF)

## Evidence DB

Expected path: `~/.3surgeons/evidence.db`  
Table: `evidence`  
Schema (auto-detected at runtime — any columns work):

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | primary key, used for SSE cursor |
| created_at | TEXT | ISO timestamp |
| type | TEXT | e.g. `probe`, `audit`, `gold` |
| content | TEXT | evidence body (FTS5 indexed) |
| session_id | TEXT | originating session |
| prev_hash | TEXT | sha256 of previous row (for chain verify) |

If the DB is absent, all calls return empty results (ZSF).

## ZSF counters

- `evidence_get_total` / `evidence_get_errors`
- `evidence_search_total` / `evidence_search_errors`
- `evidence_sse_connections_total`

Visible at `GET /health`.

## Known gaps

- Hash chain is display-only: `prev_hash` must be written at DB insert time for real cryptographic verification. Currently only verified if the field exists; entries without it show "no prev_hash".
- FTS5 requires the DB to have an `evidence_fts` virtual table; falls back to LIKE search automatically.
- SSE does not authenticate — intended for localhost use only.
- No write endpoint; this is a read-only viewer.
