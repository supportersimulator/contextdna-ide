#!/usr/bin/env python3
"""
Race Theater MCP server — exposes get_race_status MCP tool + HTTP GET /race-status.

Reads:
  - dashboard_exports/race_events_snapshot.json  (3s-loop race data, primary)
  - .fleet/priorities/green-light.md             (per-node done/claimed items)
  - git log                                      (commits today per author)

HTTP:  GET http://127.0.0.1:8877/race-status   → JSON
MCP:   tool get_race_status                    → same JSON as text

ZSF: any read/parse failure → returns empty metrics dict, never crashes.

Usage:
  python3 server.py [--port 8877] [--repo /path/to/repo]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ── Error counters (ZSF) ─────────────────────────────────────────────────────
_counters: dict[str, int] = {
    "snapshot_read_errors": 0,
    "green_light_read_errors": 0,
    "git_log_errors": 0,
    "mcp_tool_errors": 0,
    "http_errors": 0,
}


def _bump(key: str) -> None:
    _counters[key] = _counters.get(key, 0) + 1


# ── Repo root resolution ──────────────────────────────────────────────────────

def _repo_root(hint: str | None = None) -> Path:
    if hint:
        return Path(hint).resolve()
    # Walk up from this file until we find a .git directory
    p = Path(__file__).resolve().parent
    for _ in range(10):
        if (p / ".git").exists():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent.parent  # fallback


# ── Git commits-today per author ──────────────────────────────────────────────

def _commits_today(repo: Path) -> dict[str, int]:
    """Return {author_name: commit_count} for commits made today (local date)."""
    try:
        today = date.today().isoformat()
        out = subprocess.check_output(
            ["git", "log", "--oneline", "--format=%ae", f"--since={today}T00:00:00"],
            cwd=repo,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode("utf-8", errors="replace")
        counts: dict[str, int] = {}
        for line in out.splitlines():
            email = line.strip()
            if email:
                counts[email] = counts.get(email, 0) + 1
        return counts
    except Exception:
        _bump("git_log_errors")
        return {}


# ── Green-light.md parser ─────────────────────────────────────────────────────
# Done line:    - [x] [G-XXXX] title (sha=abc123)
# Claimed line: - [⏳ <node> <epoch>] [G-XXXX] ...
# Unclaimed:    - [ ] [G-XXXX] ...

_DONE_RE = re.compile(r"^\s*-\s+\[x\].*\(sha=([^)]+)\)", re.IGNORECASE)
_CLAIMED_RE = re.compile(r"^\s*-\s+\[⏳\s+(\S+)\s+\d+\]")


def _parse_green_light(repo: Path) -> dict[str, dict[str, int]]:
    """
    Returns {node_id: {items_done: N, items_claimed: M}}.
    Done items are attributed to the git committer whose sha matches;
    claimed items are attributed to the node listed in the bracket.
    """
    gl_path = repo / ".fleet" / "priorities" / "green-light.md"
    node_stats: dict[str, dict[str, int]] = {}

    try:
        text = gl_path.read_text(encoding="utf-8")
    except Exception:
        _bump("green_light_read_errors")
        return node_stats

    # Build sha → node mapping from git log (author email hint not needed;
    # we just count done items globally and attribute claimed items by name).
    done_count = 0
    claimed_by: dict[str, int] = {}

    for line in text.splitlines():
        if _DONE_RE.match(line):
            done_count += 1
        m = _CLAIMED_RE.match(line)
        if m:
            node = m.group(1)
            claimed_by[node] = claimed_by.get(node, 0) + 1

    # We can't reliably attribute done items to nodes without cross-referencing
    # commits, so we store them as a fleet-wide total under "fleet".
    if done_count:
        node_stats["fleet"] = {"items_done": done_count, "items_claimed": 0}

    for node, cnt in claimed_by.items():
        entry = node_stats.setdefault(node, {"items_done": 0, "items_claimed": 0})
        entry["items_claimed"] = cnt

    return node_stats


# ── Race snapshot reader ───────────────────────────────────────────────────────

def _read_race_snapshot(repo: Path) -> list[dict[str, Any]]:
    snap_path = repo / "dashboard_exports" / "race_events_snapshot.json"
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        races = data.get("races", [])
        if not isinstance(races, list):
            raise ValueError("races is not a list")
        return races
    except Exception:
        _bump("snapshot_read_errors")
        return []


# ── Leaderboard builder ────────────────────────────────────────────────────────

# Known fleet nodes — extend as the fleet grows
_FLEET_NODES = ["mac1", "mac2", "mac3", "cloud"]


def _build_leaderboard(repo: Path) -> dict[str, Any]:
    races = _read_race_snapshot(repo)
    commits_by_email = _commits_today(repo)
    green_light_stats = _parse_green_light(repo)

    # Compute per-node metrics from race snapshot
    node_wins: dict[str, int] = {}
    node_races: dict[str, int] = {}
    active_count = 0

    for race in races:
        if not isinstance(race, dict):
            continue
        status = race.get("status", "")
        if status not in ("complete", "active", "racing"):
            continue
        if status in ("active", "racing"):
            active_count += 1
        winner = race.get("winner_node_id")
        if winner:
            node_wins[winner] = node_wins.get(winner, 0) + 1
        for participant in race.get("participants", []):
            if not isinstance(participant, dict):
                continue
            nid = participant.get("node_id", "")
            if nid:
                node_races[nid] = node_races.get(nid, 0) + 1

    # Assemble leaderboard rows (all known nodes + any discovered from data)
    all_nodes = set(_FLEET_NODES) | set(node_wins) | set(node_races)
    rows = []
    for node in sorted(all_nodes):
        gl = green_light_stats.get(node, {})
        rows.append(
            {
                "node": node,
                "races_participated": node_races.get(node, 0),
                "races_won": node_wins.get(node, 0),
                "items_done": gl.get("items_done", 0),
                "items_claimed": gl.get("items_claimed", 0),
                "commits_today": sum(
                    v for k, v in commits_by_email.items() if node in k
                ),
            }
        )

    # Sort: wins desc, then races_participated desc
    rows.sort(key=lambda r: (-r["races_won"], -r["races_participated"]))

    # Fleet-wide done items attributed under "fleet" key
    fleet_gl = green_light_stats.get("fleet", {})

    return {
        "leaderboard": rows,
        "active_race_count": active_count,
        "total_races": len(
            [r for r in races if isinstance(r, dict) and r.get("status") in ("complete", "active", "racing")]
        ),
        "fleet_items_done_total": fleet_gl.get("items_done", 0),
        "counters": dict(_counters),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── MCP tool handler ──────────────────────────────────────────────────────────

def handle_mcp_line(line: str, repo: Path) -> str:
    """Process one MCP JSON-RPC line, return a JSON-RPC response string."""
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        )

    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    # ── initialize ────────────────────────────────────────────────────────────
    if method == "initialize":
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "race-theater", "version": "1.0.0"},
                },
            }
        )

    # ── tools/list ────────────────────────────────────────────────────────────
    if method == "tools/list":
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "get_race_status",
                            "description": (
                                "Returns the current Race Theater leaderboard: "
                                "per-node races won, items done, commits today, "
                                "and active race count."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        }
                    ]
                },
            }
        )

    # ── tools/call ────────────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        if tool_name == "get_race_status":
            try:
                data = _build_leaderboard(repo)
                text = json.dumps(data, indent=2)
            except Exception as exc:
                _bump("mcp_tool_errors")
                text = json.dumps({"error": str(exc), "counters": dict(_counters)})
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            )
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        )

    # ── notifications (no response needed) ───────────────────────────────────
    if "id" not in req:
        return ""  # notification — silent ack

    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
    )


# ── HTTP server ────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    repo: Path = Path(".")

    def log_message(self, fmt: str, *args: Any) -> None:  # suppress default access log
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") not in ("/race-status", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            if self.path.rstrip("/") == "/health":
                body = json.dumps({"status": "ok", "counters": dict(_counters)}).encode()
            else:
                data = _build_leaderboard(self.repo)
                body = json.dumps(data, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            _bump("http_errors")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode())

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


def _run_http(port: int, repo: Path) -> None:
    _Handler.repo = repo
    httpd = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"[race-theater] HTTP  http://127.0.0.1:{port}/race-status", flush=True)
    httpd.serve_forever()


# ── MCP stdio loop ────────────────────────────────────────────────────────────

def _run_mcp_stdio(repo: Path) -> None:
    print("[race-theater] MCP stdio ready", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = handle_mcp_line(line, repo)
        if response:
            print(response, flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Race Theater MCP server")
    parser.add_argument("--port", type=int, default=8877, help="HTTP port (default 8877)")
    parser.add_argument("--repo", default=None, help="Path to superrepo root")
    parser.add_argument(
        "--http-only",
        action="store_true",
        help="Run HTTP server only (no MCP stdio loop)",
    )
    args = parser.parse_args()

    repo = _repo_root(args.repo)
    print(f"[race-theater] repo={repo}", file=sys.stderr, flush=True)

    # Always start the HTTP server in a daemon thread
    t = threading.Thread(target=_run_http, args=(args.port, repo), daemon=True)
    t.start()

    if args.http_only:
        print(f"[race-theater] HTTP-only mode on port {args.port}", flush=True)
        t.join()
    else:
        _run_mcp_stdio(repo)


if __name__ == "__main__":
    main()
