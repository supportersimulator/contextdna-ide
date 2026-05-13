#!/usr/bin/env python3
"""
fleet-send — CLI tool to send messages to Fleet Nerve peers.

Usage:
  python3 tools/fleet_nerve_send.py mac1 "Branch review complete"
  python3 tools/fleet_nerve_send.py --type task mac2 "Run 3s cross-exam"
  python3 tools/fleet_nerve_send.py --type task --wait 120 mac1 "Do X and reply"
  python3 tools/fleet_nerve_send.py --broadcast "All machines: pull main"
  python3 tools/fleet_nerve_send.py --wake mac3
  python3 tools/fleet_nerve_send.py --check          # check for pending replies
  python3 tools/fleet_nerve_send.py --historian fast  # fleet-wide cache hygiene
  python3 tools/fleet_nerve_send.py --status          # fleet health summary
"""

import argparse
import json
import os
import socket
import sys
import time
import uuid
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.fleet_nerve_config import detect_node_id, load_peers


LOCAL_PORT = int(os.environ.get("FLEET_NERVE_PORT", "8855"))


def _get_fleet_id() -> str:
    """Read fleet ID from config, not hardcoded."""
    try:
        cfg_path = Path(__file__).parent.parent / ".multifleet" / "config.json"
        if cfg_path.exists():
            return json.loads(cfg_path.read_text()).get("fleetId", "default-fleet")
    except Exception:
        pass
    return os.environ.get("MULTIFLEET_FLEET_ID", "default-fleet")


def send_message(target_ip: str, port: int, msg: dict, target_node: str = None) -> dict:
    """Send message to peer. Tries relay through local daemon first (sandbox-safe),
    falls back to direct HTTP (for LaunchAgent/unsandboxed processes)."""
    data = json.dumps(msg).encode()

    # Try relay first (works inside Claude Code sandbox)
    if target_node:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{LOCAL_PORT}/relay/{target_node}/message",
                data=data, headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return json.loads(resp.read())
        except Exception:
            pass  # relay failed, try direct

    # Direct HTTP (works outside sandbox — LaunchAgents, SSH)
    try:
        req = urllib.request.Request(
            f"http://{target_ip}:{port}/message",
            data=data, headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def send_smart(target_node: str, msg: dict) -> dict:
    """Send via local daemon's /send-smart endpoint (session-aware routing).

    The local daemon probes the target, checks session state, and routes
    the message to the best injection point.
    """
    payload = {**msg, "target": target_node}
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{LOCAL_PORT}/send-smart",
            data=data, headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)  # longer timeout for probe + deliver
        return json.loads(resp.read())
    except Exception as e:
        return {"status": "failed", "error": str(e), "delivered": False}


def check_replies(node_id: str):
    """Check for pending fleet replies in the seed file."""
    seed_path = Path(f"/tmp/fleet-seed-{node_id}.md")
    if not seed_path.exists() or seed_path.stat().st_size == 0:
        print("No pending fleet messages.")
        return

    content = seed_path.read_text()
    msg_count = content.count("## [")
    print(f"[FLEET: {msg_count} pending message(s)]\n")
    print(content)

    # Also check other common node ID patterns
    for f in Path("/tmp").glob("fleet-seed-*.md"):
        if f != seed_path and f.stat().st_size > 0:
            alt_content = f.read_text()
            alt_count = alt_content.count("## [")
            print(f"\n[Also found: {f.name} with {alt_count} message(s)]")
            print(alt_content)


def wait_for_reply(node_id: str, timeout_s: int, msg_id: str):
    """Poll seed file for a reply referencing our message ID."""
    print(f"Waiting for reply (timeout: {timeout_s}s)...", flush=True)
    seed_patterns = [
        Path(f"/tmp/fleet-seed-{node_id}.md"),
    ]
    # Also check hostname-based patterns
    hostname = socket.gethostname().lower()
    seed_patterns.append(Path(f"/tmp/fleet-seed-{hostname}.md"))

    start = time.time()
    last_size = {str(p): p.stat().st_size if p.exists() else 0 for p in seed_patterns}

    while time.time() - start < timeout_s:
        for seed_path in seed_patterns:
            if not seed_path.exists():
                continue
            current_size = seed_path.stat().st_size
            sp = str(seed_path)
            if current_size > last_size.get(sp, 0):
                # New content — check if it's a reply
                content = seed_path.read_text()
                if "REPLY" in content or msg_id[:8] in content:
                    elapsed = int(time.time() - start)
                    print(f"\nReply received ({elapsed}s):\n")
                    # Show only new content
                    new_part = content[last_size.get(sp, 0):]
                    print(new_part)
                    return True
                last_size[sp] = current_size
        time.sleep(3)
        print(".", end="", flush=True)

    print(f"\nTimeout after {timeout_s}s. Check later with: python3 tools/fleet_nerve_send.py --check")
    return False


def wake_node(peers: dict, target: str):
    """Multi-strategy wake for sleeping machines.

    Strategy (tried in order):
    1. Health check — already awake? Done.
    2. SSH ping — machine awake but daemon not running? Start daemon.
    3. WoL magic packet — machine sleeping? Send wake packet.
    4. caffeinate — once awake, prevent re-sleep during fleet ops.
    """
    import subprocess

    peer = peers.get(target)
    if not peer:
        print(f"Unknown peer: {target}. Known: {list(peers.keys())}")
        return

    ip = peer["ip"]
    user = peer.get("user", os.environ.get("FLEET_SSH_USER", os.environ.get("USER", "")))

    # Strategy 1: Health check — already awake?
    try:
        resp = urllib.request.urlopen(f"http://{ip}:{peer.get('port', 8855)}/health", timeout=3)
        data = json.loads(resp.read())
        print(f"  {target}: already awake (up {data.get('uptime_s', '?')}s, {data.get('activeSessions', '?')} sessions)")
        return
    except Exception:
        pass

    print(f"  {target}: Fleet Nerve not responding. Trying wake strategies...")

    # Strategy 2: SSH ping — machine awake but daemon stopped?
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             f"{user}@{ip}", "echo ALIVE"],
            capture_output=True, text=True, timeout=8,
        )
        if "ALIVE" in result.stdout:
            print(f"  {target}: SSH reachable — machine is awake, daemon may be down")
            print(f"  Starting Fleet Nerve daemon via SSH...")
            subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                 f"{user}@{ip}",
                 "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; source ~/.zshrc 2>/dev/null; "
                 "cd ${FLEET_REPO_DIR:-$HOME/dev/er-simulator-superrepo} && bash scripts/fleet-nerve-setup.sh 2>&1 | tail -2"],
                capture_output=True, text=True, timeout=15,
            )
            # Also caffeinate to prevent re-sleep during fleet ops (1 hour)
            subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", f"{user}@{ip}",
                 "caffeinate -dims -t 10800 &"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(3)
            # Verify daemon came up
            try:
                resp = urllib.request.urlopen(f"http://{ip}:{peer.get('port', 8855)}/health", timeout=3)
                data = json.loads(resp.read())
                print(f"  {target}: AWAKE — daemon started ({data.get('activeSessions', 0)} sessions, caffeinate 3hr)")
                return
            except Exception:
                print(f"  {target}: SSH works but daemon didn't start. Check logs: ssh {user}@{ip} 'tail /tmp/fleet-nerve.log'")
                return
    except Exception:
        pass

    # Strategy 3: WoL magic packet — machine is actually sleeping
    mac_addr = peer.get("mac_address")
    if not mac_addr:
        # Try ARP cache as fallback
        try:
            arp = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5)
            for line in arp.stdout.split("\n"):
                if ip in line:
                    parts = line.split()
                    for p in parts:
                        if ":" in p and len(p) >= 11:
                            mac_addr = p
                            break
        except Exception:
            pass

    if mac_addr:
        print(f"  Sending WoL magic packet to {mac_addr}...")
        print(f"  (Note: WoL over Wi-Fi is unreliable on macOS — Ethernet recommended)")
        try:
            mac_bytes = bytes.fromhex(mac_addr.replace(":", "").replace("-", ""))
            magic = b'\xff' * 6 + mac_bytes * 16
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, ('<broadcast>', 9))
            sock.close()
        except Exception as e:
            print(f"  WoL send failed: {e}")

    # Poll for wake (works for both SSH-started and WoL-woken)
    print(f"  Waiting for {target}...", end="", flush=True)
    for i in range(12):
        time.sleep(5)
        try:
            resp = urllib.request.urlopen(f"http://{ip}:{peer.get('port', 8855)}/health", timeout=3)
            data = json.loads(resp.read())
            print(f"\n  {target}: AWAKE ({data.get('activeSessions', 0)} sessions)")
            # Caffeinate to prevent re-sleep
            subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", f"{user}@{ip}", "caffeinate -dims -t 10800 &"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            print(".", end="", flush=True)

    print(f"\n  {target}: did not respond after 60s.")
    print(f"  May need physical wake. Once awake, run: bash scripts/fleet-nerve-setup.sh")


def fleet_status(peers: dict, node_id: str):
    """Quick fleet health summary."""
    print("Fleet Status:")
    for name, peer in peers.items():
        ip = peer["ip"] if name != node_id else "127.0.0.1"
        try:
            resp = urllib.request.urlopen(f"http://{ip}:{peer.get('port', 8855)}/health", timeout=3)
            d = json.loads(resp.read())
            sessions = d.get("activeSessions", 0)
            git = d.get("git", {})
            branch = git.get("branch", "?")
            commit = git.get("commit", "?")[:30]
            idle = d.get("idle_s", 0)
            chief = " [CHIEF]" if name == peers.get("chief", d.get("chief")) else ""
            print(f"  {name}: UP | {sessions} sessions | {branch} @ {commit} | idle {idle}s{chief}")
        except Exception:
            print(f"  {name}: DOWN / SLEEPING")


def main():
    parser = argparse.ArgumentParser(description="Send Fleet Nerve message")
    parser.add_argument("target", nargs="?", help="Target node (e.g., mac1)")
    parser.add_argument("message", nargs="?", help="Message body")
    parser.add_argument("--type", default="context", choices=["context", "task", "reply", "alert", "sync", "broadcast"])
    parser.add_argument("--subject", default="")
    parser.add_argument("--ref", default=None, help="Reference message ID (for replies)")
    parser.add_argument("--broadcast", action="store_true", help="Send to all peers")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="Wait for a reply after sending (polls seed file)")
    parser.add_argument("--check", action="store_true", help="Check for pending fleet replies")
    parser.add_argument("--wake", metavar="NODE", help="Wake a sleeping machine")
    parser.add_argument("--wake-fleet", action="store_true", help="Wake ALL peers — bring entire fleet online")
    parser.add_argument("--status", action="store_true", help="Fleet health summary")
    parser.add_argument("--tasks", action="store_true", help="Show active task agents across fleet")
    parser.add_argument("--historian", choices=["fast", "full", "cleanup"],
                        help="Trigger session historian on all nodes")
    parser.add_argument("--smart", action="store_true",
                        help="Use send_smart (session-aware routing) instead of direct send")
    args = parser.parse_args()

    node_id = detect_node_id()
    peers, chief = load_peers()

    # --check: read pending replies
    if args.check:
        check_replies(node_id)
        sys.exit(0)

    # --wake: Wake single node
    if args.wake:
        wake_node(peers, args.wake)
        sys.exit(0)

    # --wake-fleet: Wake ALL peers — bring entire fleet online
    if args.wake_fleet:
        print("Waking entire fleet...")
        for name in peers:
            if name != node_id:
                print(f"\n--- {name} ---")
                wake_node(peers, name)
        print(f"\nFleet wake complete.")
        fleet_status(peers, node_id)
        sys.exit(0)

    # --status: fleet health
    if args.status:
        fleet_status(peers, node_id)
        sys.exit(0)

    # --tasks: show active task agents across fleet
    if args.tasks:
        print("Fleet Task Agents:")
        for name, peer in peers.items():
            ip = peer["ip"] if name != node_id else "127.0.0.1"
            try:
                resp = urllib.request.urlopen(f"http://{ip}:{peer.get('port', 8855)}/tasks/live", timeout=5)
                d = json.loads(resp.read())
                active = d.get("activeTasks", 0)
                recent = d.get("recentTasks", 0)
                print(f"\n  {name}: {active} running, {recent} recent")
                for t in d.get("tasks", []):
                    status = "RUNNING" if t["running"] else f"done ({t['ageSec']}s ago)"
                    print(f"    [{t['taskId']}] {status} ({t['sizeBytes']}B)")
                    # Show last 200 chars of tail
                    tail = t.get("tail", "")
                    if tail:
                        last_lines = tail.strip().split("\n")[-3:]
                        for line in last_lines:
                            print(f"      > {line[:120]}")
            except Exception as e:
                print(f"\n  {name}: UNREACHABLE ({e})")
        sys.exit(0)

    # --historian: fleet-wide session historian
    if args.historian:
        mode = args.historian
        print(f"Fleet-wide session historian ({mode}):")
        for name, peer in peers.items():
            ip = peer["ip"] if name != node_id else "127.0.0.1"
            try:
                resp = urllib.request.urlopen(
                    f"http://{ip}:{peer.get('port', 8855)}/historian/{mode}", timeout=120)
                data = json.loads(resp.read())
                extracted = data.get("extracted", 0)
                reclaimed = data.get("reclaimed_mb", 0)
                protected = data.get("protected_sessions", 0)
                status = f"extracted={extracted}, reclaimed={reclaimed:.1f}MB, protected={protected}"
                print(f"  {name}: {status}")
            except Exception as e:
                print(f"  {name}: FAILED ({e})")
        sys.exit(0)

    if not args.broadcast and not args.message:
        parser.error("message is required (or use --broadcast, --check, --wake, --status)")

    msg_base = {
        "type": args.type,
        "from": node_id,
        "ref": args.ref,
        "seq": int(time.time()) % 100000,
        "fleetId": _get_fleet_id(),
        "priority": 2,
        "payload": {
            "subject": args.subject or (args.message[:50] if args.message else ""),
            "body": args.message or "",
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl_hours": 24,
        "requires_ack": False,
    }

    sent_ids = []

    if args.broadcast:
        msg_base["type"] = "broadcast"
        msg_base["payload"]["body"] = args.message or args.target or ""
        for name, peer in peers.items():
            if name != node_id:
                msg = {**msg_base, "to": name, "id": str(uuid.uuid4())}
                if getattr(args, 'smart', False):
                    result = send_smart(name, msg)
                    delivered = result.get("delivered", False)
                    routing = result.get("routing", "")
                    print(f"  {name}: {'delivered' if delivered else 'queued'}"
                          f"{f' ({routing})' if routing else ''}")
                else:
                    result = send_message(peer["ip"], peer.get("port", 8855), msg, target_node=name)
                    print(f"  {name}: {result.get('status', 'unknown')}")
                sent_ids.append(msg["id"])
    else:
        if args.target not in peers:
            print(f"Error: unknown peer '{args.target}'. Known: {list(peers.keys())}")
            sys.exit(1)
        peer = peers[args.target]
        msg_base["to"] = args.target
        msg_base["id"] = str(uuid.uuid4())
        if getattr(args, 'smart', False):
            result = send_smart(args.target, msg_base)
            delivered = result.get("delivered", False)
            routing = result.get("routing", "")
            channel = result.get("channel", "")
            print(f"  {args.target}: {'delivered' if delivered else 'queued'}"
                  f" via {channel}{f' ({routing})' if routing else ''}")
        else:
            result = send_message(peer["ip"], peer.get("port", 8855), msg_base, target_node=args.target)
            print(f"  {args.target}: {result.get('status', 'unknown')}")
        sent_ids.append(msg_base["id"])

    # --wait: poll for reply
    if args.wait > 0 and sent_ids:
        wait_for_reply(node_id, args.wait, sent_ids[0])


if __name__ == "__main__":
    main()
