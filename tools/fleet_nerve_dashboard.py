#!/usr/bin/env python3
"""
Fleet Nerve Dashboard — Theatrical live view matching 3-Surgeons visual quality.

Design principles (from 3-Surgeons + Theatrical Director persona):
1. Clarity over brevity — always show phase, node, metric
2. Progressive disclosure — summary first, detail on demand
3. Latency + cost transparency — every operation shows timing
4. Node identity preservation — each machine has distinct voice
5. Disagreements highlighted — convergence mentioned, divergence emphasized
6. Evidence grading explicit — claims have stated evidence standards
7. Structural consistency — phases, checks, gates follow templates
8. Human-in-loop affordances — decision points, not automation

Additive to 3-Surgeons: the fleet dashboard wraps AROUND surgical output,
showing the multi-machine coordination layer on top of local surgery.
"""

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Dynamic: chief gets 🏛️, workers get ⚡. No hardcoded node names.
DEFAULT_ICON_CHIEF = "🏛️"
DEFAULT_ICON_WORKER = "⚡"
DEFAULT_ROLE_CHIEF = "Chief / Synthesis"
DEFAULT_ROLE_WORKER = "Worker / Execution"

def _node_icon(node_id: str, role: str = "worker") -> str:
    return DEFAULT_ICON_CHIEF if role == "chief" else DEFAULT_ICON_WORKER

def _node_role(node_id: str, role: str = "worker") -> str:
    return DEFAULT_ROLE_CHIEF if role == "chief" else DEFAULT_ROLE_WORKER
SURGEON_ICONS = {"cardiologist": "❤️", "neurologist": "🔮", "atlas": "🧠"}


def _fetch(url: str, timeout: int = 3):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception:
        return None


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _elapsed(start_time: float) -> str:
    secs = int(time.time() - start_time)
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _confidence_bar(value: float, width: int = 10) -> str:
    """Visual confidence bar: ▓▓▓▓▓▓░░░░ 60%"""
    filled = int(value * width)
    return "▓" * filled + "░" * (width - filled) + f" {int(value * 100)}%"


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    """Progress bar: █████░░░░░ 5/10"""
    if total == 0:
        return "░" * width + " 0/0"
    filled = int((done / total) * width)
    return "█" * filled + "░" * (width - filled) + f" {done}/{total}"


def _git_behind_count() -> int | None:
    """Return how many commits we're behind origin, or None on error."""
    try:
        subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=str(REPO_ROOT), capture_output=True, timeout=5,
        )
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{u}"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def render_dashboard(node_id: str, peers: dict, port: int = 8855) -> str:
    """Generate theatrical dashboard — shows only when there's substance."""

    health = _fetch(f"http://127.0.0.1:{port}/health", timeout=2)

    # Determine role from config or health response
    node_role = "worker"
    if health:
        # Check if we're the chief from peers config
        try:
            from tools.fleet_nerve_config import load_peers
            _, chief = load_peers()
            if chief == node_id:
                node_role = "chief"
        except Exception:
            pass
    icon = _node_icon(node_id, node_role)
    role = _node_role(node_id, node_role)
    tasks = _fetch(f"http://127.0.0.1:{port}/tasks/live")
    work = _fetch(f"http://127.0.0.1:{port}/work")
    work_all = _fetch(f"http://127.0.0.1:{port}/work/all")
    fleet = _fetch(f"http://127.0.0.1:{port}/fleet/live")

    if not health:
        return ""

    git = health.get("git", {})
    surgeons = health.get("surgeons", {})
    sessions = health.get("activeSessions", 0)
    idle_s = health.get("idle_s", 0)
    summary = health.get("sessionSummary", "")

    # Gather work from ALL peers (not just local) for full fleet view
    all_work_items = (work_all or {}).get("items", [])
    # Pull peer work via relay (1s timeout) — never blocks on offline peers
    for name, peer in peers.items():
        if name == node_id:
            continue
        remote_work = _fetch(f"http://127.0.0.1:{port}/relay/{name}/work/all", timeout=1)
        if remote_work and "error" not in remote_work:
            for item in remote_work.get("items", []):
                if not any(w.get("title") == item.get("title") for w in all_work_items):
                    all_work_items.append(item)

    my_tasks_running = [t for t in (tasks or {}).get("tasks", []) if t.get("running")]
    my_tasks_recent = [t for t in (tasks or {}).get("tasks", []) if not t.get("running") and t.get("ageSec", 999) < 300]
    active_work = [i for i in (work or {}).get("items", []) if i.get("node_id") == node_id]
    fleet_active = [n for n in (fleet or {}).get("nodes", []) if n.get("activeTasks", 0) > 0 and n["name"] != node_id]

    has_activity = my_tasks_running or my_tasks_recent or active_work or fleet_active or all_work_items
    if not has_activity and idle_s > 300:
        return ""  # truly idle, nothing to show

    lines = []

    # ── Header ──
    lines.append(f"\n{'─' * 60}")
    lines.append(f"{icon} **{node_id.upper()}** ({role}) — Fleet Nerve Live")
    lines.append(f"`{_ts()}` | {sessions} session(s) | `{git.get('branch', '?')}` @ `{git.get('commit', '?')[:25]}`")
    lines.append(f"{'─' * 60}\n")

    # ── My Current Work (Phase Tree style from 3-Surgeons) ──
    if active_work:
        lines.append("**📋 CURRENT TASK**")
        for w in active_work:
            elapsed = _elapsed(w.get("started_at", time.time()))
            lines.append(f"├─ **P{w.get('priority', '?')}**: {w.get('title', '?')}")
            lines.append(f"├─ Status: ⏳ In progress ({elapsed})")
            lines.append(f"└─ Node: {node_id}")
        lines.append("")

    # ── Task Agents (like Phase Progress from 3-Surgeons) ──
    if my_tasks_running:
        lines.append("**🤖 AGENTS IN THE FIELD**")
        for i, t in enumerate(my_tasks_running):
            prefix = "├─" if i < len(my_tasks_running) - 1 else "└─"
            lines.append(f"{prefix} `{t['taskId']}` ⏳ running ({t.get('sizeBytes', 0)}B output)")
            tail = t.get("tail", "").strip().split("\n")[-1][:90]
            if tail:
                lines.append(f"   > {tail}")
        lines.append("")

    if my_tasks_recent:
        lines.append("**✅ RECENTLY COMPLETED**")
        for t in my_tasks_recent[:3]:
            tail = t.get("tail", "").strip().split("\n")[-1][:80]
            lines.append(f"├─ `{t['taskId']}` done {t['ageSec']}s ago")
            if tail:
                lines.append(f"   > {tail}")
        lines.append("")

    # ── 3-Surgeons Status (matching probe display format) ──
    cardio = surgeons.get("cardiologist", "?")
    neuro = surgeons.get("neurologist", "?")
    key_src = surgeons.get("cardiologist_key_source", "?")
    key_rec = surgeons.get("cardiologist_recommendation", "")
    lines.append("**🏥 3-SURGEONS**")
    lines.append(f"├─ {SURGEON_ICONS['cardiologist']} Cardiologist: {'OK' if cardio in ('ok', 'configured') else 'FAIL'} ({cardio}) [key: {key_src}]")
    lines.append(f"├─ {SURGEON_ICONS['neurologist']} Neurologist: {'OK' if neuro == 'ok' else 'FAIL'} ({neuro})")
    lines.append(f"└─ {SURGEON_ICONS['atlas']} Atlas: always available (this session)")
    if key_rec:
        lines.append(f"   ⚠️  {key_rec}")
    lines.append("")

    # ── Peer Status ──
    peer_data = health.get("peers", {})
    channel_health = health.get("channel_health", {})
    if peer_data:
        lines.append("**📡 PEERS**")
        peer_list = sorted(peer_data.items())
        for i, (name, info) in enumerate(peer_list):
            prefix = "├─" if i < len(peer_list) - 1 else "└─"
            last_seen = info.get("lastSeen", -1)
            transport = info.get("transport", "?")
            sess_count = info.get("sessions", 0)
            if last_seen < 0:
                seen_str = "never"
            elif last_seen < 60:
                seen_str = f"{last_seen}s ago"
            elif last_seen < 3600:
                seen_str = f"{last_seen // 60}m ago"
            else:
                seen_str = f"{last_seen // 3600}h ago"
            online = "🟢" if last_seen < 30 else ("🟡" if last_seen < 90 else "🔴")
            # Channel health score (from self-heal invariance system)
            ch_info = channel_health.get(name, {})
            ch_str = ""
            if ch_info:
                ch_str = f" | channels: {ch_info.get('score', '?')}"
                broken = ch_info.get("broken", [])
                if broken:
                    ch_str += f" [{','.join(broken)} broken]"
            lines.append(f"{prefix} {online} **{name}** | seen {seen_str} | {transport} | {sess_count} session(s){ch_str}")
        lines.append("")

    # ── Worker Status ──
    worker_dir = Path("/tmp/fleet-worker-results")
    if worker_dir.exists():
        worker_files = list(worker_dir.glob("*.json"))
        active_workers = 0
        for wf in worker_files:
            try:
                wd = json.loads(wf.read_text())
                if wd.get("status") == "running":
                    active_workers += 1
            except Exception:
                pass
        lines.append("**⚙️ WORKERS**")
        lines.append(f"└─ {active_workers} active session-driver(s) | {len(worker_files)} result file(s)")
        lines.append("")

    # ── Git Sync ──
    behind = _git_behind_count()
    if behind is not None and behind > 0:
        lines.append("**🔄 GIT SYNC**")
        lines.append(f"└─ ⚠️  {behind} commit(s) behind origin")
        lines.append("")

    # ── Fleet Activity (what others are doing — cross-machine visibility) ──
    if fleet:
        other_nodes = [n for n in fleet.get("nodes", []) if n["name"] != node_id]
        if other_nodes:
            lines.append("**⚡ FLEET**")
            for i, n in enumerate(other_nodes):
                n_icon = _node_icon(n["name"])
                status = n.get("nodeStatus", n.get("status", "?"))
                prefix = "├─" if i < len(other_nodes) - 1 else "└─"

                if status == "offline":
                    lines.append(f"{prefix} {n_icon} **{n['name']}**: 🔴 offline")
                else:
                    active_t = n.get("activeTasks", 0)
                    if active_t > 0:
                        task_info = ""
                        for t in n.get("tasks", []):
                            if t.get("running"):
                                task_info = t.get("lastLine", "")[:50]
                                break
                        lines.append(f"{prefix} {n_icon} **{n['name']}**: ⏳ {active_t} agent(s) | {task_info}")
                    else:
                        lines.append(f"{prefix} {n_icon} **{n['name']}**: {status}")
            lines.append("")

    # ── Fleet-Wide Work In Progress ──
    all_active = [i for i in all_work_items if i.get("status") == "in_progress"]
    if all_active:
        lines.append("**🔨 FLEET WORK IN PROGRESS**")
        for i, w in enumerate(all_active):
            n = w.get("node_id", "?")
            n_icon = _node_icon(n)
            elapsed = _elapsed(w.get("started_at", time.time()))
            prefix = "├─" if i < len(all_active) - 1 else "└─"
            lines.append(f"{prefix} {n_icon} **{n}**: P{w.get('priority', '?')} {w.get('title', '?')[:50]} ({elapsed})")
        lines.append("")

    # ── Work Backlog (progress visualization) ──
    pending = sorted([i for i in all_work_items if i.get("status") == "pending"], key=lambda x: x.get("priority", 9))
    done = [i for i in all_work_items if i.get("status") in ("completed", "failed")]
    total = len(pending) + len(all_active) + len(done)

    if total > 0:
        lines.append("**📊 BACKLOG**")
        lines.append(f"`{_progress_bar(len(done), total)}` — {len(all_active)} active, {len(pending)} pending")
        if pending:
            lines.append(f"Next up:")
            for p in pending[:3]:
                lines.append(f"  P{p.get('priority', '?')}: {p.get('title', '?')[:55]}")
        lines.append("")

    # ── Sports Commentary ──
    lines.append(f"{'─' * 60}")
    commentary = _commentary(node_id, health, fleet, my_tasks_running, my_tasks_recent, all_work_items)
    if commentary:
        lines.append(f"🎙️ *{commentary}*")
    lines.append("")

    return "\n".join(lines)


def _commentary(node_id, health, fleet, running, recent, work_items) -> str:
    """Generate one-liner sports commentator narration. No fluff — factual excitement."""

    if running:
        count = len(running)
        ids = ", ".join(t["taskId"][:6] for t in running[:3])
        return f"{node_id} has {count} agent(s) working: {ids}. Fleet backlog shrinking."

    if recent:
        t = recent[0]
        return f"Task {t['taskId'][:8]} just completed on {node_id}! Results incoming."

    if fleet:
        active_nodes = [n for n in fleet.get("nodes", []) if n.get("activeTasks", 0) > 0]
        if active_nodes:
            names = " + ".join(n["name"] for n in active_nodes)
            total_tasks = sum(n.get("activeTasks", 0) for n in active_nodes)
            return f"Fleet active: {names} running {total_tasks} task(s). {node_id} coordinating."

    pending = [i for i in work_items if i.get("status") == "pending"]
    if pending:
        return f"{len(pending)} items queued. {node_id} ready for the next sprint."

    idle = health.get("idle_s", 0)
    if idle < 120:
        return f"{node_id} just wrapped up. Standing by."
    return ""


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from tools.fleet_nerve_config import detect_node_id, load_peers
    print(render_dashboard(detect_node_id(), load_peers()[0]))
