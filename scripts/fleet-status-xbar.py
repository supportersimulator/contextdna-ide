#!/usr/bin/env python3
# <xbar.title>Fleet Status</xbar.title>
# <xbar.version>v2.0.0</xbar.version>
# <xbar.author>Context DNA Fleet</xbar.author>
# <xbar.desc>Multi-fleet node status — peers, channels, surgeons, sessions</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <xbar.var>string(DAEMON_URL="http://127.0.0.1:8855"): Fleet daemon URL</xbar.var>
import json
import os
import subprocess
from urllib.request import urlopen, Request

DAEMON_URL = os.environ.get("DAEMON_URL", "http://127.0.0.1:8855")
TIMEOUT = 4

# Auto-detect repo path
REPO = None
for candidate in [
    os.path.expanduser("~/dev/er-simulator-superrepo"),
    os.path.expanduser("~/Documents/er-simulator-superrepo"),
    os.path.expanduser("~/er-simulator-superrepo"),
]:
    if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "tools/fleet_nerve_nats.py")):
        REPO = candidate
        break
if not REPO:
    REPO = os.path.expanduser("~/dev/er-simulator-superrepo")


def fetch(endpoint):
    try:
        with urlopen(Request(f"{DAEMON_URL}{endpoint}", headers={"Accept": "application/json"}), timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def fmt_secs(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fmt_uptime(s):
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {sec}s"


def peer_icon(p):
    try:
        ls = int(p.get("lastSeen", 9999))
    except (TypeError, ValueError):
        return "🔴"
    if ls < 30:
        return "🟢"
    if ls < 300:
        return "🟡"
    return "🔴"


def surgeon_icon(status):
    if status in ("ok", "healthy", "running"):
        return "🟢"
    if status in ("down", "offline", "error"):
        return "🔴"
    return "🟡"


def channel_color(score_str):
    try:
        num, denom = score_str.split("/")
        ratio = int(num) / int(denom)
    except (ValueError, ZeroDivisionError):
        return "gray"
    if ratio >= 0.7:
        return "green"
    if ratio >= 0.4:
        return "orange"
    return "red"


def main():
    data = fetch("/health")

    if data is None:
        # Daemon offline — show minimal info
        print("F:? | color=red")
        print("---")
        print("Fleet daemon offline | color=red")
        print(f"Endpoint: {DAEMON_URL} | color=gray size=11")

        # Try to show git info even without daemon
        try:
            branch = subprocess.run(
                ["git", "-C", REPO, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=3
            ).stdout.strip()
            if branch:
                print(f"Branch: {branch} | color=gray size=11")
        except Exception:
            pass

        print("---")
        print(f"Start Daemon | bash=/bin/bash param1=-c param2='cd {REPO} && NATS_URL=nats://127.0.0.1:4222 python3 tools/fleet_nerve_nats.py serve' terminal=true")
        print("Refresh | refresh=true")
        return

    node_id = data.get("nodeId", data.get("node_id", "?"))
    peers = data.get("peers", {})
    uptime = data.get("uptime_s")
    status = data.get("status", "unknown")
    active = data.get("activeSessions", 0)
    transport = data.get("transport", "?")
    stats = data.get("stats", {})
    git_info = data.get("git", {})
    surgeons = data.get("surgeons", {})
    channels = data.get("channel_health", {})
    sessions = data.get("sessions", [])

    # Count online peers (seen in last 30s)
    online = sum(1 for p in peers.values() if isinstance(p, dict) and int(p.get("lastSeen", 9999)) < 120)
    fleet_size = 1 + len(peers)
    # Extract node number (e.g. "mac2" -> "2")
    node_num = "".join(c for c in node_id if c.isdigit()) or "?"

    # Menu bar line — F:N/T where N=node number, T=fleet size
    if status in ("ok", "healthy", "running") and (online == len(peers) or not peers):
        color = "green"
    elif online > 0:
        color = "orange"
    else:
        color = "red" if peers else "green"

    print(f"F:{node_num}/{fleet_size} | color={color}")
    print("---")

    # === Header ===
    ok_color = "green" if status in ("ok", "healthy", "running") else "red"
    print(f"Fleet — {node_id} | size=14 color={ok_color}")
    print(f"Transport: {transport} | Uptime: {fmt_uptime(uptime)} | size=11 color=gray")
    if git_info:
        branch = git_info.get("branch", "?")
        commit = git_info.get("commit", "?")[:50]
        print(f"Branch: {branch} — {commit} | size=11 color=gray")

    # === Sessions ===
    print("---")
    if sessions:
        print(f"Sessions ({active} active) | size=12")
        for s in sessions[:5]:
            sid = s.get("sessionId", "?")[:11]
            kind = s.get("kind", "?")
            idle = fmt_secs(s.get("idle_s", 0))
            entry = s.get("entrypoint", "")
            print(f"  {sid} {kind} ({entry}) idle {idle} | size=11 font=Menlo")
    else:
        print(f"Sessions: {active} active | size=12")

    # === Peers ===
    print("---")
    if peers:
        print(f"Peers ({online}/{len(peers)} online) | size=12")
        for name, p in sorted(peers.items()):
            if not isinstance(p, dict):
                continue
            icon = peer_icon(p)
            seen = fmt_secs(p.get("lastSeen"))
            sess = p.get("sessions", 0)
            ch = channels.get(name, {})
            ch_score = ch.get("score", "?")
            ch_color = channel_color(ch_score) if ch_score != "?" else "gray"
            print(f"{icon} {name} — {sess} sess, seen {seen} ago | size=13")
            print(f"  Channels: {ch_score} | size=11 color={ch_color}")
            broken = ch.get("broken", [])
            if broken:
                print(f"  Broken: {', '.join(broken)} | size=10 color=red")
    else:
        print("No peers | color=gray")

    # === Self channel health ===
    self_ch = channels.get(node_id, {})
    if self_ch:
        self_score = self_ch.get("score", "?")
        self_color = channel_color(self_score) if self_score != "?" else "gray"
        print(f"Self channels: {self_score} | size=11 color={self_color}")
        self_broken = self_ch.get("broken", [])
        if self_broken:
            print(f"  Broken: {', '.join(self_broken)} | size=10 color=red")

    # === 3-Surgeons ===
    if surgeons:
        print("---")
        print("3-Surgeons | size=12")
        neuro = surgeons.get("neurologist", "unknown")
        cardio = surgeons.get("cardiologist", "unknown")
        cardio_key = surgeons.get("cardiologist_key_source", "")
        print(f"  {surgeon_icon(neuro)} Neurologist: {neuro} | size=11")
        cardio_label = f"{cardio} ({cardio_key})" if cardio_key else cardio
        print(f"  {surgeon_icon(cardio)} Cardiologist: {cardio_label} | size=11")
        print(f"  🧠 Atlas: this session | size=11")

    # === Stats ===
    if stats:
        print("---")
        sent = stats.get("sent", 0)
        recv = stats.get("received", 0)
        errs = stats.get("errors", 0)
        print(f"Messages: {sent} sent / {recv} recv | size=11")
        err_color = "red" if errs > 0 else "gray"
        print(f"Errors: {errs} | size=11 color={err_color}")

    # === Probes ===
    probes = fetch("/probes")
    if probes:
        total_probes = probes.get("total_probes", 0)
        total_findings = probes.get("total_findings", 0)
        summary = probes.get("summary", {})
        has_blocking = summary.get("has_blocking", False)
        intensity = summary.get("surgeon_intensity", "?")
        if total_probes > 0:
            print("---")
            pcolor = "red" if has_blocking else ("orange" if total_findings > 5 else "green")
            print(f"Probes: {total_probes} run, {total_findings} findings | size=11 color={pcolor}")
            int_color = "red" if intensity == "full" else ("orange" if intensity == "standard" else "green")
            print(f"Intensity: {intensity} | size=11 color={int_color}")

    # === Actions ===
    print("---")
    print(f"Run Fleet Check | bash=/bin/bash param1=-c param2='cd {REPO} && bash scripts/fleet-check.sh' terminal=true")
    print(f"Test Fallback | bash=/bin/bash param1=-c param2='cd {REPO} && bash scripts/fleet-test-fallback.sh all' terminal=true")
    print(f"Open Arbiter View | bash=/usr/bin/open param1={DAEMON_URL}/arbiter terminal=false")
    print(f"Open Dashboard | bash=/usr/bin/open param1={DAEMON_URL}/dashboard terminal=false")
    print("---")
    print(f"Restart Daemon | bash=/bin/bash param1=-c param2='cd {REPO} && pkill -f fleet_nerve_nats; sleep 1; NATS_URL=nats://127.0.0.1:4222 python3 tools/fleet_nerve_nats.py serve' terminal=true")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
