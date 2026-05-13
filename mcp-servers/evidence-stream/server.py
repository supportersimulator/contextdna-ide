#!/usr/bin/env python3
"""
Evidence Stream MCP server — exposes evidence DB tools + HTTP side-car on port 8878.

MCP tools:
  get_evidence_latest(n=10)      — last N entries from ~/.3surgeons/evidence.db
  search_evidence(query, limit=5) — FTS5 search on evidence DB

HTTP endpoints:
  GET /evidence/latest?n=10      → JSON array
  GET /evidence/stream           → SSE (text/event-stream), new rows every 5s
  GET /health                    → {"ok": true, "counters": {...}}

ZSF: DB missing → empty list, never raises through.
5 ZSF counters: evidence_get_total, evidence_get_errors, evidence_search_total,
                evidence_search_errors, evidence_sse_connections_total
"""

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_PORT = 8878
DB_PATH = Path(os.path.expanduser("~/.3surgeons/evidence.db"))
SSE_POLL_INTERVAL = 5  # seconds

# ── ZSF counters ──────────────────────────────────────────────────────────────
_counters: dict[str, int] = {
    "evidence_get_total": 0,
    "evidence_get_errors": 0,
    "evidence_search_total": 0,
    "evidence_search_errors": 0,
    "evidence_sse_connections_total": 0,
}


def _bump(key: str) -> None:
    _counters[key] = _counters.get(key, 0) + 1


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection | None:
    """Return a sqlite3 connection or None if DB is missing."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _detect_columns(conn: sqlite3.Connection) -> list[str]:
    """Return actual column names for the evidence table."""
    try:
        rows = conn.execute("PRAGMA table_info(evidence)").fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


def _row_to_dict(row: sqlite3.Row, cols: list[str]) -> dict:
    """Convert a sqlite3.Row to a plain dict and attach sha256 hash."""
    d: dict[str, Any] = {c: row[c] for c in cols if c in row.keys()}
    # Compute hash over canonical JSON of the row (for chain display)
    payload = json.dumps(d, sort_keys=True, default=str)
    d["_hash"] = hashlib.sha256(payload.encode()).hexdigest()
    return d


def _get_latest(n: int = 10) -> list[dict]:
    """Return last N evidence rows. ZSF: empty list on any error."""
    try:
        conn = _connect()
        if conn is None:
            return []
        with conn:
            cols = _detect_columns(conn)
            if not cols:
                return []
            # Try to order by id DESC if id col exists, else rowid
            order_col = "id" if "id" in cols else "rowid"
            rows = conn.execute(
                f"SELECT * FROM evidence ORDER BY {order_col} DESC LIMIT ?", (n,)
            ).fetchall()
        return [_row_to_dict(r, cols) for r in rows]
    except Exception:
        return []


def _search_evidence(query: str, limit: int = 5) -> list[dict]:
    """FTS5 search or LIKE fallback. ZSF: empty list on any error."""
    try:
        conn = _connect()
        if conn is None:
            return []
        with conn:
            cols = _detect_columns(conn)
            if not cols:
                return []
            # Try FTS5 virtual table first
            try:
                rows = conn.execute(
                    "SELECT * FROM evidence_fts WHERE evidence_fts MATCH ? LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # Fallback: LIKE search on 'content' col if present, else first text col
                text_col = "content" if "content" in cols else cols[0]
                rows = conn.execute(
                    f"SELECT * FROM evidence WHERE {text_col} LIKE ? LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()
        return [_row_to_dict(r, cols) for r in rows]
    except Exception:
        return []


def _get_rows_after(last_id: int, batch: int = 20) -> tuple[list[dict], int]:
    """
    Return rows with id > last_id (for SSE polling).
    Returns (rows, new_last_id).
    """
    try:
        conn = _connect()
        if conn is None:
            return [], last_id
        with conn:
            cols = _detect_columns(conn)
            if not cols:
                return [], last_id
            id_col = "id" if "id" in cols else "rowid"
            rows = conn.execute(
                f"SELECT * FROM evidence WHERE {id_col} > ? ORDER BY {id_col} ASC LIMIT ?",
                (last_id, batch),
            ).fetchall()
        if not rows:
            return [], last_id
        dicts = [_row_to_dict(r, cols) for r in rows]
        id_col = "id" if "id" in dicts[0] else "rowid"
        new_last = dicts[-1].get(id_col, last_id)
        return dicts, new_last
    except Exception:
        return [], last_id


# ── HTTP handler ──────────────────────────────────────────────────────────────

class EvidenceHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")

        try:
            if path == "/health":
                self._send_json({"ok": True, "counters": dict(_counters)})

            elif path == "/evidence/latest":
                _bump("evidence_get_total")
                try:
                    n = int(qs.get("n", ["10"])[0])
                    n = max(1, min(n, 200))
                    rows = _get_latest(n)
                    self._send_json(rows)
                except Exception:
                    _bump("evidence_get_errors")
                    self._send_json([])

            elif path == "/evidence/stream":
                _bump("evidence_sse_connections_total")
                self._handle_sse()

            else:
                self._send_json({"error": "not found"}, 404)

        except Exception:
            _bump("evidence_get_errors")
            try:
                self._send_json({"error": "internal"}, 500)
            except Exception:
                pass

    def _handle_sse(self):
        """SSE: poll DB every SSE_POLL_INTERVAL seconds, push new rows."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            return

        # Seed last_id from current max so we only stream new rows going forward
        last_id = 0
        try:
            conn = _connect()
            if conn:
                cols = _detect_columns(conn)
                id_col = "id" if "id" in cols else "rowid"
                row = conn.execute(
                    f"SELECT MAX({id_col}) FROM evidence"
                ).fetchone()
                if row and row[0] is not None:
                    last_id = row[0]
                conn.close()
        except Exception:
            pass

        # Send initial heartbeat comment
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except Exception:
            return

        while True:
            try:
                time.sleep(SSE_POLL_INTERVAL)
                rows, last_id = _get_rows_after(last_id)
                for row in rows:
                    data = json.dumps(row, default=str)
                    msg = f"data: {data}\n\n"
                    self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                # Client disconnected or write failed — exit loop cleanly
                break


# ── MCP JSON-RPC stdio server ─────────────────────────────────────────────────

def _mcp_respond(result: Any, req_id: Any) -> None:
    resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
    line = json.dumps(resp) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def _mcp_error(msg: str, req_id: Any, code: int = -32000) -> None:
    resp = {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": msg},
    }
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


_TOOLS = [
    {
        "name": "get_evidence_latest",
        "description": "Return last N entries from the 3-Surgeons evidence DB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "default": 10, "description": "Number of entries to return (max 200)"}
            },
        },
    },
    {
        "name": "search_evidence",
        "description": "Full-text search the 3-Surgeons evidence DB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "default": 5, "description": "Max results"},
            },
            "required": ["query"],
        },
    },
]


def _handle_mcp_request(req: dict) -> None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        _mcp_respond(
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "evidence-stream", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
            req_id,
        )

    elif method == "tools/list":
        _mcp_respond({"tools": _TOOLS}, req_id)

    elif method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        if tool_name == "get_evidence_latest":
            _bump("evidence_get_total")
            try:
                n = int(args.get("n", 10))
                n = max(1, min(n, 200))
                rows = _get_latest(n)
                _mcp_respond(
                    {"content": [{"type": "text", "text": json.dumps(rows, indent=2, default=str)}]},
                    req_id,
                )
            except Exception as e:
                _bump("evidence_get_errors")
                _mcp_respond(
                    {"content": [{"type": "text", "text": "[]"}]}, req_id
                )

        elif tool_name == "search_evidence":
            _bump("evidence_search_total")
            try:
                query = str(args.get("query", ""))
                limit = int(args.get("limit", 5))
                limit = max(1, min(limit, 100))
                rows = _search_evidence(query, limit)
                _mcp_respond(
                    {"content": [{"type": "text", "text": json.dumps(rows, indent=2, default=str)}]},
                    req_id,
                )
            except Exception as e:
                _bump("evidence_search_errors")
                _mcp_respond(
                    {"content": [{"type": "text", "text": "[]"}]}, req_id
                )

        else:
            _mcp_error(f"Unknown tool: {tool_name}", req_id)

    elif method == "notifications/initialized":
        pass  # no response needed

    else:
        _mcp_error(f"Method not found: {method}", req_id, -32601)


def _run_mcp_stdio() -> None:
    """Read JSON-RPC lines from stdin, respond on stdout."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
            _handle_mcp_request(req)
        except json.JSONDecodeError:
            _mcp_error("Parse error", None, -32700)
        except Exception:
            pass  # ZSF — never crash the stdio loop


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Evidence Stream MCP + HTTP server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--http-only", action="store_true", help="Run HTTP only (no MCP stdio)")
    args = parser.parse_args()

    # Start HTTP side-car in background thread
    httpd = HTTPServer(("127.0.0.1", args.port), EvidenceHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    if not args.http_only:
        # MCP stdio loop in main thread
        _run_mcp_stdio()
    else:
        # Foreground HTTP mode for testing
        print(f"Evidence Stream HTTP on http://127.0.0.1:{args.port}", flush=True)
        t.join()


if __name__ == "__main__":
    main()
