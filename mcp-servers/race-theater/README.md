# Race Theater — MCP server + HTML panel

Standalone MCP server that exposes `get_race_status` and an HTTP endpoint
(`GET /race-status`) consumed by `panel.html`.

## What it does

- Reads `dashboard_exports/race_events_snapshot.json` for 3-surgeon race data
  (wins, participants, active count).
- Reads `.fleet/priorities/green-light.md` for per-node claimed/done items.
- Calls `git log --since=<today>` to count commits per node (matched by
  node name appearing in committer email).
- Returns a leaderboard JSON object with columns:
  `node | races_won | races_participated | commits_today | items_done`.
- ZSF: every read/parse failure increments a counter and returns empty metrics
  — the server never crashes on bad input.

## Usage

```bash
# HTTP-only (serve the panel from a browser)
python3 mcp-servers/race-theater/server.py --http-only --port 8877

# MCP stdio mode (for Claude Code MCP integration) + HTTP side-car
python3 mcp-servers/race-theater/server.py --port 8877

# Open the panel
open mcp-servers/race-theater/panel.html
```

The panel auto-refreshes every 10 seconds.

## MCP tool

**`get_race_status`** — no arguments, returns the leaderboard JSON as text.

## .mcp.json registration (optional)

```json
{
  "mcpServers": {
    "race-theater": {
      "command": "python3",
      "args": ["mcp-servers/race-theater/server.py"]
    }
  }
}
```

## Unresolved gaps (from gap analysis)

| Gap | Status | Notes |
|-----|--------|-------|
| Evidence Stream panel — hash-chain verify | Unshipped | `evidence-view.tsx` exists but no chain verification. Needs `/evidence/latest` endpoint + SSE. |
| Evidence Stream — `/evidence/latest` endpoint | Unshipped | Route does not exist in admin.contextdna.io. |
| Evidence Stream — SSE feed | Unshipped | Admin uses polling only; no SSE on either side. |
| Race Theater — NATS subscription (always-on) | Partial | Publisher is wired; no always-on daemon subscriber that updates snapshot from peer nodes. |
| Race Theater — per-node done-item attribution | Partial | Green-light done items are fleet-wide totals; git sha→node mapping needed for per-node breakdown. |
| Truth Ladder panel | Unshipped | No 4-folder pipeline UI in admin (gap analysis item #4). |
| Human Arbiter View | Unshipped | Noted in gap analysis P5; scaffold exists in `.fleet/audits/2026-05-07-AA3`. |

The Race Theater frontend (X5/Y1/Y2 waves) lives in the
`admin.contextdna.io` submodule under
`components/dashboard/RaceTheater.tsx` and is the canonical IDE panel.
This MCP server is a lightweight standalone companion for direct
Claude Code tool access and browser-only preview.
