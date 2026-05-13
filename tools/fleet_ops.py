#!/usr/bin/env python3
"""
fleet-ops — Consolidated fleet operations tool.

Replaces the repeated bash patterns from Multi-Fleet development into
reusable Python functions with CLI interface.

Usage:
  python3 tools/fleet_ops.py audit          # Full daemon + process audit on all machines
  python3 tools/fleet_ops.py sync           # Sync all machines to latest commit
  python3 tools/fleet_ops.py readiness      # Multi-Fleet readiness assessment
  python3 tools/fleet_ops.py cleanup        # Remove obsolete daemons from all machines
  python3 tools/fleet_ops.py deploy         # Pull + restart daemons on all machines
  python3 tools/fleet_ops.py historian      # Fleet-wide session historian (fast/full/cleanup)
  python3 tools/fleet_ops.py wal            # WAL status across fleet
  python3 tools/fleet_ops.py work           # Work backlog status
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.fleet_nerve_config import detect_node_id, load_peers

NODE_ID = detect_node_id()
PEERS, CHIEF = load_peers()
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]

# Remote repo path — override via FLEET_REPO_DIR env var on each node
_REMOTE_REPO = os.environ.get("FLEET_REPO_DIR", "$HOME/dev/er-simulator-superrepo")


def _ssh(ip: str, cmd: str, timeout: int = 15) -> str:
    """Run command on remote machine via SSH."""
    user = os.environ.get("FLEET_SSH_USER", os.environ.get("USER", ""))
    repo_dir = os.environ.get("FLEET_REPO_DIR", "$HOME/dev/er-simulator-superrepo")
    full_cmd = (
        f'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; '
        f'source ~/.zshrc 2>/dev/null; '
        f'cd {repo_dir} && {cmd}'
    )
    try:
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"{user}@{ip}", full_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def _local(cmd: str) -> str:
    """Run command locally."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=15, cwd=str(REPO_ROOT),
        )
        return result.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def _for_each_peer(fn):
    """Run a function for each peer, handling local vs SSH."""
    for name, peer in PEERS.items():
        ip = peer["ip"]
        if name == NODE_ID:
            ip = "127.0.0.1"
        yield name, ip, peer


def cmd_audit():
    """Full daemon + process audit on all machines."""
    print("Fleet Daemon Audit")
    print("=" * 60)
    for name, ip, peer in _for_each_peer(None):
        print(f"\n--- {name} ---")
        if name == NODE_ID:
            # Local
            agents = _local("ls ~/Library/LaunchAgents/io.contextdna.* ~/Library/LaunchAgents/com.contextdna.* 2>/dev/null | wc -l")
            procs = _local("ps aux | grep -i '[Pp]ython' | grep -v grep | wc -l")
            print(f"  LaunchAgents: {agents.strip()}")
            print(f"  Python procs: {procs.strip()}")
            ports = _local("lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -E '8855|5045|8844' | awk '{print $1, $2, $9}'")
            if ports:
                print(f"  Ports: {ports}")
        else:
            out = _ssh(ip, "echo \"agents=$(ls ~/Library/LaunchAgents/io.contextdna.* ~/Library/LaunchAgents/com.contextdna.* 2>/dev/null | wc -l | tr -d ' ')\" && echo \"procs=$(ps aux | grep -i '[Pp]ython' | grep -v grep | wc -l | tr -d ' ')\"")
            print(f"  {out}")


def cmd_sync():
    """Sync all machines to latest commit."""
    print("Syncing fleet to latest...")
    for name, ip, peer in _for_each_peer(None):
        if name == NODE_ID:
            continue
        print(f"  {name}: ", end="", flush=True)
        out = _ssh(ip, "git fetch origin main 2>&1 | tail -1 && git reset --hard origin/main 2>&1 | tail -1")
        print(out)
    print("  Restart daemons...")
    for name, ip, peer in _for_each_peer(None):
        if name == NODE_ID:
            continue
        _ssh(ip, "launchctl unload ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist 2>/dev/null; sleep 1; launchctl load ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist")
    print("Done.")


def cmd_deploy():
    """Pull + restart daemons on all machines."""
    cmd_sync()


def cmd_readiness():
    """Multi-Fleet readiness assessment."""
    import urllib.request
    print("Multi-Fleet Readiness")
    print("=" * 60)
    for name, ip, peer in _for_each_peer(None):
        check_ip = "127.0.0.1" if name == NODE_ID else ip
        print(f"\n--- {name} ---")
        try:
            resp = urllib.request.urlopen(f"http://{check_ip}:{peer.get('port', 8855)}/health", timeout=3)
            d = json.loads(resp.read())
            print(f"  Status: UP ({d.get('uptime_s', '?')}s)")
            print(f"  Sessions: {d.get('activeSessions', 0)}")
            print(f"  Idle: {d.get('idle_s', 0)}s (sessionIdle: {d.get('sessionIdle', '?')})")
            print(f"  Git: {d.get('git', {}).get('branch', '?')} @ {d.get('git', {}).get('commit', '?')[:40]}")
            print(f"  Surgeons: {d.get('surgeons', {})}")
            summary = d.get("sessionSummary", "")
            if summary:
                print(f"  Working on: {summary[:100]}")
        except Exception:
            print(f"  Status: DOWN / UNREACHABLE")


def cmd_wal():
    """WAL status across fleet."""
    print("WAL Status")
    print("=" * 60)
    for name, ip, peer in _for_each_peer(None):
        print(f"\n--- {name} ---")
        if name == NODE_ID:
            out = _local('python3 -c "from tools.fleet_nerve_store import FleetNerveStore; import sqlite3; s=FleetNerveStore(); c=sqlite3.connect(s.db_path); print(f\'  wal={c.execute(\\\"SELECT COUNT(*) FROM wal\\\").fetchone()[0]}\'); print(f\'  messages={c.execute(\\\"SELECT COUNT(*) FROM messages\\\").fetchone()[0]}\')"')
        else:
            out = _ssh(ip, 'python3 -c "from tools.fleet_nerve_store import FleetNerveStore; import sqlite3; s=FleetNerveStore(); c=sqlite3.connect(s.db_path); print(f\'  wal={c.execute(\\\"SELECT COUNT(*) FROM wal\\\").fetchone()[0]}\'); print(f\'  messages={c.execute(\\\"SELECT COUNT(*) FROM messages\\\").fetchone()[0]}\')"')
        print(out)


def cmd_work():
    """Work backlog status."""
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:8855/work/all", timeout=3)
        d = json.loads(resp.read())
        items = d.get("items", [])
        pending = [i for i in items if i.get("status") == "pending"]
        active = [i for i in items if i.get("status") == "in_progress"]
        done = [i for i in items if i.get("status") in ("completed", "failed")]

        print(f"Work Backlog: {len(pending)} pending, {len(active)} active, {len(done)} done")
        if active:
            print("\nActive:")
            for i in active:
                print(f"  P{i.get('priority', '?')} [{i.get('node_id', '?')}] {i.get('title', '?')[:60]}")
        if pending:
            print("\nPending:")
            for i in pending[:10]:
                print(f"  P{i.get('priority', '?')} {i.get('title', '?')[:60]}")
    except Exception as e:
        print(f"Error: {e}")


def cmd_historian():
    """Fleet-wide session historian."""
    import urllib.request
    mode = sys.argv[2] if len(sys.argv) > 2 else "fast"
    print(f"Fleet historian ({mode}):")
    for name, ip, peer in _for_each_peer(None):
        check_ip = "127.0.0.1" if name == NODE_ID else ip
        try:
            resp = urllib.request.urlopen(
                f"http://{check_ip}:{peer.get('port', 8855)}/historian/{mode}", timeout=120)
            d = json.loads(resp.read())
            print(f"  {name}: extracted={d.get('extracted', 0)}, reclaimed={d.get('reclaimed_mb', 0):.1f}MB")
        except Exception as e:
            print(f"  {name}: {e}")


def cmd_cleanup():
    """Remove obsolete daemons from all machines."""
    print("This would remove obsolete ClaudeExchange daemons.")
    print("Run with --confirm to execute.")


COMMANDS = {
    "audit": cmd_audit,
    "sync": cmd_sync,
    "deploy": cmd_deploy,
    "readiness": cmd_readiness,
    "wal": cmd_wal,
    "work": cmd_work,
    "historian": cmd_historian,
    "cleanup": cmd_cleanup,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python3 tools/fleet_ops.py <command>")
        print(f"Commands: {', '.join(COMMANDS.keys())}")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
