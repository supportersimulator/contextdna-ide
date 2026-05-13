#!/usr/bin/env python3
"""
Event Bridge — SSE keystone endpoint for the ER Simulator IDE.

Exposes:
  GET /api/v1/events          → SSE stream (text/event-stream, CORS *)
  GET /api/v1/events/replay   → last N events from ring buffer (?since=<iso>)
  GET /health                 → {"ok": true, "sources": {...}, "counters": {...}}

Event types emitted:
  fleet.heartbeat  — daemon health from http://127.0.0.1:8855/health
  fleet.evidence   — latest row from ~/.3surgeons/evidence.db
  fleet.race       — race status from http://127.0.0.1:8877/race-status
  fleet.webhook    — webhook health from session_historian + /tmp/webhook-publish.err

ZSF: source failure → emit source_error event, never crash. All exceptions counted.
Ring buffer: 500 events max (deque).
Poll interval: 5s. Push only on change (MD5 hash of last payload per source).
"""

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ── ZSF counters ─────────────────────────────────────────────────────────────
_counters: dict[str, int] = {
    "heartbeat_errors": 0,
    "evidence_errors": 0,
    "race_errors": 0,
    "webhook_errors": 0,
    "sse_client_errors": 0,
    "ring_buffer_errors": 0,
}

def _bump(key: str) -> None:
    _counters[key] = _counters.get(key, 0) + 1


# ── Ring buffer ───────────────────────────────────────────────────────────────
_RING_CAP = 500
_ring: deque[dict] = deque(maxlen=_RING_CAP)
_ring_lock = threading.Lock()

def _ring_append(event_type: str, data: dict) -> None:
    try:
        with _ring_lock:
            _ring.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event_type,
                "data": data,
            })
    except Exception as exc:
        _bump("ring_buffer_errors")
        logging.warning("ring_append failed: %s", exc)


# ── SSE client registry ───────────────────────────────────────────────────────
_clients: list[Any] = []
_clients_lock = threading.Lock()

def _broadcast(event_type: str, data: dict) -> None:
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()
    _ring_append(event_type, data)
    dead: list[Any] = []
    with _clients_lock:
        for q in list(_clients):
            try:
                q.append(payload)
            except Exception:
                dead.append(q)
    if dead:
        with _clients_lock:
            for q in dead:
                try:
                    _clients.remove(q)
                except ValueError:
                    pass


# ── Source: fleet.heartbeat ───────────────────────────────────────────────────
def _fetch_heartbeat() -> dict:
    url = "http://127.0.0.1:8855/health"
    with urllib.request.urlopen(url, timeout=4) as r:
        return json.loads(r.read())


# ── Source: fleet.evidence ────────────────────────────────────────────────────
def _fetch_evidence() -> dict:
    db_path = Path.home() / ".3surgeons" / "evidence.db"
    if not db_path.exists():
        return {"available": False}
    conn = sqlite3.connect(str(db_path), timeout=3)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, created_at, source, summary FROM evidence ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return {"available": True, "row": None}
        return {
            "available": True,
            "id": row[0],
            "created_at": row[1],
            "source": row[2],
            "summary": row[3],
        }
    finally:
        conn.close()


# ── Source: fleet.race ────────────────────────────────────────────────────────
def _fetch_race() -> dict:
    url = "http://127.0.0.1:8877/race-status"
    with urllib.request.urlopen(url, timeout=4) as r:
        return json.loads(r.read())


# ── Source: fleet.webhook ─────────────────────────────────────────────────────
def _fetch_webhook() -> dict:
    result: dict = {}
    # Try session_historian
    try:
        import subprocess
        out = subprocess.check_output(
            [sys.executable, "memory/session_historian.py", "status"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            timeout=6,
            stderr=subprocess.DEVNULL,
        )
        result["historian"] = out.decode("utf-8", errors="replace").strip()[:500]
    except Exception as exc:
        result["historian_error"] = str(exc)[:200]

    # Check /tmp/webhook-publish.err
    err_path = Path("/tmp/webhook-publish.err")
    if err_path.exists():
        try:
            result["publish_err"] = err_path.read_text(errors="replace").strip()[-300:]
        except Exception as exc:
            result["publish_err_read_error"] = str(exc)[:200]
    else:
        result["publish_err"] = None

    return result


# ── Poller thread ─────────────────────────────────────────────────────────────
_POLL_INTERVAL = 5  # seconds

_sources = {
    "fleet.heartbeat": (_fetch_heartbeat, "heartbeat_errors"),
    "fleet.evidence":  (_fetch_evidence,  "evidence_errors"),
    "fleet.race":      (_fetch_race,      "race_errors"),
    "fleet.webhook":   (_fetch_webhook,   "webhook_errors"),
}

_last_hash: dict[str, str] = {}
_source_status: dict[str, str] = {k: "unknown" for k in _sources}


def _md5(obj: Any) -> str:
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _poller_loop() -> None:
    while True:
        for event_type, (fetch_fn, err_key) in _sources.items():
            try:
                data = fetch_fn()
                h = _md5(data)
                if _last_hash.get(event_type) != h:
                    _last_hash[event_type] = h
                    _broadcast(event_type, data)
                _source_status[event_type] = "ok"
            except Exception as exc:
                _bump(err_key)
                _source_status[event_type] = f"error: {exc}"
                _broadcast("source_error", {"source": event_type, "error": str(exc)[:300]})
        time.sleep(_POLL_INTERVAL)


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        pass

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/v1/events":
            self._handle_sse()
        elif path == "/api/v1/events/replay":
            self._handle_replay()
        elif path == "/health":
            self._handle_health()
        elif path == "/panel.html" or path == "/":
            self._handle_panel()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.send_header("Content-Length", "16")
            self.end_headers()
            self.wfile.write(b'{"error":"404"}\n')

    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # Send initial ping
        try:
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
        except Exception:
            return

        # Register client queue
        q: deque[bytes] = deque()
        with _clients_lock:
            _clients.append(q)

        try:
            while True:
                if q:
                    chunk = q.popleft()
                    self.wfile.write(chunk)
                    self.wfile.flush()
                else:
                    # Keepalive comment every 15s
                    time.sleep(0.2)
        except Exception:
            _bump("sse_client_errors")
        finally:
            with _clients_lock:
                try:
                    _clients.remove(q)
                except ValueError:
                    pass

    def _handle_replay(self) -> None:
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        since_iso: str | None = None
        for part in qs.split("&"):
            if part.startswith("since="):
                since_iso = part[6:]
        try:
            with _ring_lock:
                events = list(_ring)
            if since_iso:
                try:
                    since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
                    events = [
                        e for e in events
                        if datetime.fromisoformat(e["ts"]) >= since_dt
                    ]
                except Exception:
                    _bump("date_filter_errors")  # zsf-allow: date parse fail — events returned unfiltered
            body = json.dumps({"events": events, "count": len(events)}).encode()
        except Exception as exc:
            _bump("ring_buffer_errors")
            body = json.dumps({"error": str(exc)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self) -> None:
        payload = {
            "ok": True,
            "sources": _source_status,
            "counters": dict(_counters),
            "clients": len(_clients),
            "ring_size": len(_ring),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_panel(self) -> None:
        panel_path = Path(__file__).parent / "panel.html"
        try:
            html = panel_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        except Exception as exc:
            body = f"panel.html not found: {exc}".encode()
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Event Bridge SSE server")
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [event-bridge] %(levelname)s %(message)s",
    )

    # Start poller in background daemon thread
    t = threading.Thread(target=_poller_loop, daemon=True, name="poller")
    t.start()
    logging.info("Poller started (interval=%ds)", _POLL_INTERVAL)

    server = HTTPServer((args.host, args.port), Handler)
    logging.info("Event Bridge listening on http://%s:%d", args.host, args.port)
    logging.info("  SSE:    http://%s:%d/api/v1/events", args.host, args.port)
    logging.info("  Replay: http://%s:%d/api/v1/events/replay", args.host, args.port)
    logging.info("  Health: http://%s:%d/health", args.host, args.port)
    logging.info("  Panel:  http://%s:%d/panel.html", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down.")


if __name__ == "__main__":
    main()
