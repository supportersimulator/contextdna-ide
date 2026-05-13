#!/usr/bin/env python3
"""
Multi-Fleet Chief Ingest Server — HTTP server that receives packets from worker nodes.

Runs on the chief machine (mac1 / chief).
Listens on port 8844 for inbound packets.

Usage:
  python3 contextdna/fleet/chief_ingest.py [--port 8844]

Endpoints:
  POST /packets        — ingest a multifleet-v1 packet
  POST /message        — send a message to one or more nodes
  GET  /inbox?node=X   — fetch unread messages for node X (marks them read)
  GET  /status         — fleet status summary
  GET  /nodes          — active node list
  GET  /verdicts       — latest verdicts per node
"""

import json
import sys
import argparse
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.multifleet_packet_store import PacketStore

store = PacketStore()


class FleetHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Use structured log
        print(f"[chief_ingest] {self.address_string()} {format % args}")

    def send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path == "/packets":
            try:
                packet = json.loads(raw)
                packet_type = packet.get("type", "unknown")
                node_id = packet.get("nodeId", "unknown")
                row_id = store.ingest(packet)
                print(f"[chief_ingest] Packet #{row_id}: {packet_type} from {node_id}")
                if packet_type == "local_verdict":
                    self._trigger_synthesis(packet)
                self.send_json(200, {"ok": True, "id": row_id, "type": packet_type})
            except Exception as e:
                self.send_json(400, {"error": str(e)})

        elif self.path == "/message":
            try:
                msg = json.loads(raw)
                from_node = msg.get("from", "unknown")
                to = msg.get("to", [])
                if isinstance(to, str):
                    to = [to]
                subject = msg.get("subject", "(no subject)")
                body = msg.get("body", "")
                priority = msg.get("priority", "normal")
                ids = []
                for node in to:
                    mid = store.send_message(from_node, node, subject, body, priority)
                    ids.append({"node": node, "id": mid})
                    print(f"[chief_ingest] Message #{mid}: {from_node} → {node} | {subject!r}")
                self.send_json(200, {"ok": True, "delivered": ids})
            except Exception as e:
                self.send_json(400, {"error": str(e)})

        else:
            self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        if self.path.startswith("/inbox"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            node = qs.get("node", [None])[0]
            if not node:
                self.send_json(400, {"error": "?node= required"})
                return
            messages = store.get_inbox(node, unread_only=True)
            if messages:
                store.mark_read([m["id"] for m in messages])
                print(f"[chief_ingest] Inbox poll: {node} — {len(messages)} unread")
            self.send_json(200, {"node": node, "messages": messages, "count": len(messages)})

        elif self.path == "/status":
            self.send_json(200, store.fleet_summary())
        elif self.path == "/nodes":
            self.send_json(200, {"nodes": store.active_nodes()})
        elif self.path == "/verdicts":
            nodes = store.active_nodes()
            verdicts = {}
            for n in nodes:
                v = store.latest_verdict(n["nodeId"])
                if v:
                    verdicts[n["nodeId"]] = v
            self.send_json(200, {"verdicts": verdicts})
        elif self.path == "/health":
            self.send_json(200, {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()})
        else:
            self.send_json(404, {"error": "Not found"})

    def _trigger_synthesis(self, verdict_packet: dict):
        """Non-blocking: trigger synthesis when new verdict arrives."""
        try:
            from contextdna.fleet.chief_synthesis import synthesize_async
            synthesize_async(verdict_packet)
        except ImportError:
            pass  # synthesis module not yet built
        except Exception as e:
            print(f"[chief_ingest] Synthesis trigger failed (non-critical): {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8844, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    print(f"[chief_ingest] Starting on {args.host}:{args.port}")
    print(f"[chief_ingest] PacketStore: {store.db_path}")

    server = HTTPServer((args.host, args.port), FleetHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[chief_ingest] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
