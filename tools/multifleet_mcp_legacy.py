#!/usr/bin/env python3
"""
DEPRECATED — use tools/fleet_nerve_mcp.py instead.

This file is retained for reference only. All MCP registrations now point
to fleet_nerve_mcp.py which provides a superset of these tools with NATS
integration, 7-priority fallback, repair escalation, and worker dispatch.

Original tools (now superseded):
  - fleet_status          → fleet_status in fleet_nerve_mcp.py
  - fleet_send_packet     → fleet_send in fleet_nerve_mcp.py
  - fleet_node_verdicts   → (removed — use fleet_probe for node state)
  - fleet_probe_surgeons  → fleet_probe in fleet_nerve_mcp.py
  - fleet_send_branch_status → (removed — use fleet_send with type=branch_status)

Renamed from multifleet_mcp.py on 2026-04-05 to resolve CV-DTP sentinel flag
(duplicate tool paths, 4 hits, risk 0.6).
"""

import json
import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def handle_tool_call(tool_name: str, tool_input: dict) -> dict:
    from tools.multifleet_coordinator import (
        load_config, get_node_id, surgeon_status, send_to_chief, queue_packet, build_heartbeat
    )
    config = load_config()

    if tool_name == "fleet_status":
        try:
            from tools.multifleet_packet_store import PacketStore
            store = PacketStore()
            summary = store.fleet_summary()
        except Exception as e:
            summary = {"error": str(e), "note": "PacketStore only available on chief node"}
        node_id = get_node_id()
        chief = config.get("chief", {})
        return {
            "thisNode": node_id,
            "isChief": node_id == chief.get("nodeId", ""),
            "chiefHost": chief.get("host", "unknown"),
            "surgeons": surgeon_status(),
            **summary,
        }

    elif tool_name == "fleet_send_packet":
        packet_type = tool_input.get("type", "local_verdict")
        payload = tool_input.get("payload", {})
        node_id = get_node_id()
        from datetime import datetime, timezone
        packet = {
            "type": packet_type,
            "nodeId": node_id,
            "fleetId": config.get("fleetId", "contextdna-main"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        if send_to_chief(packet, config):
            return {"sent": True, "packet_type": packet_type}
        else:
            p = queue_packet(packet)
            return {"sent": False, "queued": str(p.name), "packet_type": packet_type}

    elif tool_name == "fleet_node_verdicts":
        try:
            from tools.multifleet_packet_store import PacketStore
            store = PacketStore()
            nodes = store.active_nodes()
            verdicts = {}
            for node in nodes:
                v = store.latest_verdict(node["nodeId"])
                if v:
                    verdicts[node["nodeId"]] = v
            return {"verdicts": verdicts, "activeNodes": [n["nodeId"] for n in nodes]}
        except Exception as e:
            return {"error": str(e), "note": "Verdict history only available on chief node"}

    elif tool_name == "fleet_probe_surgeons":
        return {"surgeons": surgeon_status(), "nodeId": get_node_id()}

    elif tool_name == "fleet_send_branch_status":
        base = tool_input.get("base", "main")
        try:
            from tools.multifleet_diff_summarizer import git_diff_summary, build_packet
            diff = git_diff_summary(base)
            packet = build_packet(diff)
            if send_to_chief(packet, config):
                return {"sent": True, "summary": diff["summary"]}
            else:
                p = queue_packet(packet)
                return {"sent": False, "queued": str(p.name), "summary": diff["summary"]}
        except Exception as e:
            return {"error": str(e)}

    else:
        return {"error": f"Unknown tool: {tool_name}"}


# MCP stdio protocol
def main():
    import sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")

        if method == "initialize":
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "multifleet", "version": "1.0.0"},
                }
            }
        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "tools": [
                        {"name": "fleet_status", "description": "Get current multi-fleet node statuses and surgeon availability", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "fleet_send_packet", "description": "Send a multifleet-v1 packet to the chief node", "inputSchema": {"type": "object", "properties": {"type": {"type": "string"}, "payload": {"type": "object"}}}},
                        {"name": "fleet_node_verdicts", "description": "Get latest 3-surgeon verdicts from all fleet nodes (chief only)", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "fleet_probe_surgeons", "description": "Check if cardiologist and neurologist are reachable on this node", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "fleet_send_branch_status", "description": "Send current branch diff summary to chief for cross-node comparison", "inputSchema": {"type": "object", "properties": {"base": {"type": "string", "default": "main"}}}},
                    ]
                }
            }
        elif method == "tools/call":
            tool_name = msg.get("params", {}).get("name", "")
            tool_input = msg.get("params", {}).get("arguments", {})
            result = handle_tool_call(tool_name, tool_input)
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
            }
        elif method == "notifications/initialized":
            continue
        else:
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }

        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
