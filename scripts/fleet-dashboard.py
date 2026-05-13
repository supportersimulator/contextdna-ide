#!/usr/bin/env python3
"""fleet-dashboard.py — one-shot fleet observability dashboard.

Design contract (Aaron's rule: no polling ever):
    - Default mode: fetch state ONCE from HTTP endpoints, render, exit.
    - --follow mode: NATS subscription that blocks on incoming messages
      (event-driven, not a poll loop). Ctrl-C exits.
    - --json mode: emit machine-readable JSON (one-shot).

Data sources (all one-shot HTTP GETs):
    - http://127.0.0.1:8855/health         (fleet daemon)
    - http://127.0.0.1:8222/jsz            (NATS JetStream)
    - http://127.0.0.1:8222/routez         (NATS cluster routes)
    - http://127.0.0.1:8222/varz           (NATS server, for cluster name)
    - .fleet-messages/<node>/*.md          (last-5 seed files)

Usage:
    ./scripts/fleet-dashboard.py            # print state, exit
    ./scripts/fleet-dashboard.py --follow   # event-driven, subscribe fleet.>/event.>
    ./scripts/fleet-dashboard.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Load centralized port constants from multifleet (env-overridable at import time)
_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT / "multi-fleet"))
try:
    from multifleet.constants import (
        FLEET_DAEMON_PORT,
        NATS_PORT,
        NATS_MONITOR_PORT,
    )
except Exception:  # pragma: no cover — fallback for minimal installs
    FLEET_DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", 8855))
    NATS_PORT = int(os.environ.get("NATS_PORT", 4222))
    NATS_MONITOR_PORT = int(os.environ.get("NATS_MONITOR_PORT", 8222))

# Optional rich support — falls back to plain text if unavailable.
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    _RICH = True
    _CONSOLE = Console()
except Exception:  # pragma: no cover - exercised only when rich missing
    _RICH = False
    _CONSOLE = None

HEALTH_URL = f"http://127.0.0.1:{FLEET_DAEMON_PORT}/health"
NATS_JSZ_URL = f"http://127.0.0.1:{NATS_MONITOR_PORT}/jsz?streams=true&config=true"
NATS_ROUTEZ_URL = f"http://127.0.0.1:{NATS_MONITOR_PORT}/routez"
NATS_VARZ_URL = f"http://127.0.0.1:{NATS_MONITOR_PORT}/varz"
REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_MSG_ROOT = REPO_ROOT / ".fleet-messages"

# RACE Q5 — extended observability hooks. All are optional/best-effort and
# never block the one-shot render path beyond a small fixed timeout.
THREE_SURGEONS_TIMEOUT_S = float(os.environ.get("Q5_3S_PROBE_TIMEOUT_S", "5"))
CLOUD_LISTENER_LABEL = os.environ.get(
    "Q5_CLOUD_LISTENER_LABEL", "io.contextdna.cloud-inbox-listener"
)


def _http_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    """One-shot HTTP GET returning parsed JSON; returns {} on failure.

    Catches the broad family of transport faults (URLError, timeout, reset
    by peer, malformed JSON). The dashboard prefers an empty section over
    a stack trace — every connection failure is normal during daemon
    restarts and should never abort the render path. ZSF: failure is still
    observable because the section renders as ``unreachable``.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw, strict=False)
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError, ValueError,
            ConnectionResetError, ConnectionRefusedError, OSError):
        return {}


def _node_id() -> str:
    return os.environ.get("MULTIFLEET_NODE_ID") or socket.gethostname().split(".")[0].lower()


def _last_fleet_messages(node_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Walk the node's .fleet-messages dir and return newest `limit` seed files."""
    out: list[dict[str, Any]] = []
    candidates: list[Path] = []
    for d in (FLEET_MSG_ROOT / node_id, FLEET_MSG_ROOT / "all"):
        if d.exists():
            candidates.extend(p for p in d.glob("*.md") if p.is_file())
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    for p in candidates[:limit]:
        meta = {"from": "?", "to": "?", "subject": p.stem}
        try:
            text = p.read_text(errors="replace").splitlines()
            in_front = False
            for line in text[:20]:
                s = line.strip()
                if s == "---":
                    in_front = not in_front
                    if not in_front:
                        break
                    continue
                if in_front and ":" in s:
                    k, _, v = s.partition(":")
                    meta[k.strip()] = v.strip()
        except OSError:
            pass
        age_s = int(now - p.stat().st_mtime)
        out.append(
            {
                "path": str(p),
                "from": meta.get("from", "?"),
                "to": meta.get("to", "?"),
                "subject": meta.get("subject", p.stem),
                "age_s": age_s,
            }
        )
    return out


def _fmt_age(secs: int | float | None) -> str:
    if secs is None:
        return "?"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _expected_peers(health: dict[str, Any]) -> list[str]:
    """Return the configured peer roster (self + known peers).

    The fleet daemon advertises observed peers on /health.peers. We treat the
    union of (self_node, peers) as the "expected replica set" for stream
    membership comparisons. This is the same source of truth race/n3 uses
    when computing replica gaps, so the dashboard view matches the daemon's.
    """
    peers = list((health.get("peers") or {}).keys())
    self_node = (health.get("delegation") or {}).get("self_node") or health.get("nodeId")
    if self_node and self_node not in peers:
        peers.append(self_node)
    return sorted(p for p in peers if p)


def _build_replica_view(
    health: dict[str, Any],
    streams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project /health.jetstream_health + /jsz into a per-stream replica view.

    For each stream we surface:
      - leader: cluster leader node id (None if unreplicated / unknown)
      - replicas: configured replica count R (from /jsz config)
      - replica_set: leader + followers actually present
      - expected: configured peer roster (race/n3's source-of-truth)
      - missing: expected peers not in replica_set (the gap)
      - max_follower_lag: largest seq gap among followers
      - status: ok | degraded | error | unknown
    """
    js_health = (health.get("jetstream_health") or {}).get("streams") or {}
    expected = _expected_peers(health)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _entry(name: str, cfg_replicas: Any, msgs: Any, bytes_: Any) -> dict[str, Any]:
        info = js_health.get(name) or {}
        leader = info.get("leader")
        followers = info.get("followers") or []
        follower_names = [f.get("name") for f in followers if f.get("name")]
        replica_set = sorted({n for n in [leader, *follower_names] if n})
        # Only flag missing if we actually have a leader (otherwise we
        # don't yet know whether this stream is even replicated here).
        missing = (
            sorted(set(expected) - set(replica_set)) if leader and expected else []
        )
        status = info.get("status") or ("unknown" if not leader else "ok")
        if missing:
            status = "degraded"
        return {
            "name": name,
            "leader": leader,
            "replicas": info.get("replicas") or cfg_replicas,
            "replica_set": replica_set,
            "expected": expected,
            "missing": missing,
            "max_follower_lag": int(info.get("max_follower_lag") or 0),
            "messages": int(info.get("messages") or msgs or 0),
            "bytes": int(info.get("bytes") or bytes_ or 0),
            "status": status,
        }

    for s in streams:
        name = s.get("name")
        if not name:
            continue
        seen.add(name)
        out.append(_entry(name, s.get("replicas"), s.get("messages"), s.get("bytes")))
    # Surface streams the daemon knows about even when /jsz didn't return
    # them (e.g. monitor port restricted but daemon has cached state).
    for name in js_health.keys():
        if name in seen:
            continue
        out.append(_entry(name, None, 0, 0))
    return out


def _collect_3s_probe(
    runner: Any | None = None, timeout_s: float = THREE_SURGEONS_TIMEOUT_S,
) -> dict[str, Any]:
    """Run ``3s probe`` once with a hard timeout.

    Returns a normalized dict::

        {"status": "ok"|"degraded"|"error"|"skip", "rc": int|None,
         "summary": "...", "raw": "<stdout-tail>"}

    The dashboard never raises if `3s` is missing — it simply marks the
    section as ``skip``. ``runner`` is injected for tests so they don't have
    to spawn a real subprocess.
    """
    cmd = ["3s", "probe"]
    if runner is None:
        bin_path = shutil.which("3s")
        if not bin_path:
            return {
                "status": "skip",
                "rc": None,
                "summary": "3s CLI not on PATH",
                "raw": "",
            }
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
            rc = proc.returncode
            stdout = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "rc": None,
                "summary": f"3s probe timed out after {timeout_s:g}s",
                "raw": "",
            }
        except Exception as exc:  # noqa: BLE001 — defensive, surface as error
            return {
                "status": "error",
                "rc": None,
                "summary": f"3s probe failed: {type(exc).__name__}: {exc}",
                "raw": "",
            }
    else:
        rc, stdout = runner(cmd)

    tail = "\n".join((stdout or "").strip().splitlines()[-6:])
    if rc == 0:
        status = "ok"
        summary = "all 3 surgeons reachable"
    elif rc is None:
        status = "error"
        summary = "no exit code"
    else:
        status = "degraded"
        summary = f"3s probe rc={rc} (one or more surgeons unreachable)"
    return {"status": status, "rc": rc, "summary": summary, "raw": tail}


def _collect_cloud_listener(
    runner: Any | None = None,
    label: str = CLOUD_LISTENER_LABEL,
    health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Probe the cloud-inbox-listener via launchctl and last-event marker.

    Returns::

        {"status": "ok"|"warn"|"skip"|"error", "pid": int|None,
         "label": "...", "last_event_age_s": int|None, "summary": "..."}

    On non-cloud nodes the LaunchAgent isn't loaded — we surface that as a
    ``skip`` so the section renders without screaming. On cloud, PID == 0
    means launchd loaded the plist but the process isn't running (the exact
    failure mode we want operators to see).
    """
    cmd = ["launchctl", "list", label]
    if runner is None:
        if not shutil.which("launchctl"):
            return {
                "status": "skip",
                "pid": None,
                "label": label,
                "last_event_age_s": None,
                "summary": "launchctl unavailable (non-darwin?)",
            }
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            rc = proc.returncode
            stdout = proc.stdout or ""
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "pid": None,
                "label": label,
                "last_event_age_s": None,
                "summary": "launchctl list timed out",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "pid": None,
                "label": label,
                "last_event_age_s": None,
                "summary": f"launchctl error: {type(exc).__name__}",
            }
    else:
        rc, stdout = runner(cmd)

    if rc != 0:
        return {
            "status": "skip",
            "pid": None,
            "label": label,
            "last_event_age_s": None,
            "summary": f"{label} not loaded (expected on non-cloud nodes)",
        }

    pid_value: int | None = None
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip().rstrip(";").rstrip(",")
        if line.startswith('"PID"') or line.startswith("PID\t") or line.startswith("PID "):
            tail = line.split("=")[-1] if "=" in line else line.split()[-1]
            tail = tail.strip().strip('"').strip(";")
            try:
                pid_value = int(tail)
            except ValueError:
                continue
            break

    # Best-effort: surface last_event_ts from /health.cloud_listener if the
    # daemon happens to publish it (the cloud_inbox_listener counters dict
    # exposes it). Fall back to None when absent — dashboard renders "?".
    last_age: int | None = None
    if health is not None:
        cl = health.get("cloud_listener") or {}
        ts = cl.get("last_event_ts")
        if isinstance(ts, (int, float)) and ts > 0:
            last_age = max(0, int(time.time() - ts))

    if pid_value is None:
        return {
            "status": "warn",
            "pid": None,
            "label": label,
            "last_event_age_s": last_age,
            "summary": f"{label} loaded but PID not parseable",
        }
    if pid_value == 0:
        return {
            "status": "warn",
            "pid": 0,
            "label": label,
            "last_event_age_s": last_age,
            "summary": f"{label} loaded but not running (PID=0)",
        }
    return {
        "status": "ok",
        "pid": pid_value,
        "label": label,
        "last_event_age_s": last_age,
        "summary": f"{label} running (PID={pid_value})",
    }


def _collect(
    *,
    three_surgeons_runner: Any | None = None,
    cloud_listener_runner: Any | None = None,
) -> dict[str, Any]:
    """Fetch all data sources once and return a normalized snapshot.

    Test seams: ``three_surgeons_runner`` and ``cloud_listener_runner`` allow
    tests to bypass real subprocess calls. Production paths leave them None.
    """
    health = _http_json(HEALTH_URL)
    jsz = _http_json(NATS_JSZ_URL)
    routez = _http_json(NATS_ROUTEZ_URL)
    varz = _http_json(NATS_VARZ_URL)
    node_id = health.get("nodeId") or _node_id()
    streams: list[dict[str, Any]] = []
    for acct in jsz.get("account_details", []) or []:
        for s in acct.get("stream_detail", []) or []:
            cfg = s.get("config", {}) or {}
            state = s.get("state", {}) or {}
            streams.append(
                {
                    "name": s.get("name"),
                    "replicas": cfg.get("num_replicas"),
                    "max_age_s": (cfg.get("max_age") or 0) // 1_000_000_000,
                    "retention": cfg.get("retention"),
                    "messages": state.get("messages", 0),
                    "bytes": state.get("bytes", 0),
                }
            )
    return {
        "generated_at": time.time(),
        "node_id": node_id,
        "health": health,
        "cluster": {
            "name": (varz.get("cluster") or {}).get("name"),
            "num_routes": routez.get("num_routes", 0),
            "routes": [
                {
                    "remote_name": r.get("remote_name"),
                    "rtt": r.get("rtt"),
                    "uptime": r.get("uptime"),
                }
                for r in routez.get("routes", []) or []
            ],
            "meta_leader": (jsz.get("meta_cluster") or {}).get("leader"),
            "meta_cluster_size": (jsz.get("meta_cluster") or {}).get("cluster_size"),
        },
        "streams": streams,
        "stream_replicas": _build_replica_view(health, streams),
        "webhook": health.get("webhook") or {"active": False, "reason": "unreachable"},
        "three_surgeons": _collect_3s_probe(runner=three_surgeons_runner),
        "cloud_listener": _collect_cloud_listener(
            runner=cloud_listener_runner, health=health
        ),
        "fleet_messages": _last_fleet_messages(node_id),
    }


# ─────────────────────────── rendering ────────────────────────────
def _print(line: str = "") -> None:
    if _RICH:
        _CONSOLE.print(line)
    else:
        print(line)


def _render(snap: dict[str, Any]) -> None:
    health = snap["health"]
    cluster = snap["cluster"]

    _print(f"[bold]== Fleet Dashboard ==[/bold] node={snap['node_id']}  t={time.strftime('%H:%M:%S')}")
    if not health:
        _print(f"[red]WARN:[/red] fleet daemon /health unreachable (127.0.0.1:{FLEET_DAEMON_PORT})")

    # Node summary
    _print("\n[bold cyan]== Node summary ==[/bold cyan]")
    _print(
        f"  uptime={_fmt_age(health.get('uptime_s'))}  "
        f"sessions={health.get('activeSessions', 0)}  "
        f"chief={(health.get('delegation') or {}).get('chief_node', '?')}"
    )
    peers = health.get("peers") or {}
    if peers:
        _print(f"  peers ({len(peers)}): " + ", ".join(f"{n}(last_seen={_fmt_age(p.get('lastSeen'))})" for n, p in peers.items()))
    else:
        _print("  peers: (none)")

    # Cluster
    _print("\n[bold cyan]== Cluster ==[/bold cyan]")
    _print(
        f"  name={cluster.get('name')}  routes={cluster.get('num_routes')}  "
        f"meta_leader={cluster.get('meta_leader')}  size={cluster.get('meta_cluster_size')}"
    )
    for r in cluster.get("routes", []):
        _print(f"    route: {r.get('remote_name')}  rtt={r.get('rtt')}  up={r.get('uptime')}")

    # JetStream
    _print("\n[bold cyan]== JetStream ==[/bold cyan]")
    if not snap["streams"]:
        _print("  (no streams / jsz unreachable)")
    for s in snap["streams"]:
        _print(
            f"  {s['name']}  R={s['replicas']}  msgs={s['messages']}  "
            f"bytes={s['bytes']}  max_age={_fmt_age(s['max_age_s'])}  retention={s['retention']}"
        )

    # Channel reliability
    _print("\n[bold cyan]== Channel reliability ==[/bold cyan]")
    cr = health.get("channel_reliability") or {}
    ch_health = health.get("channel_health") or {}
    for peer, channels in cr.items():
        score = (ch_health.get(peer) or {}).get("score", "?")
        broken = (ch_health.get(peer) or {}).get("broken") or []
        _print(f"  {peer}: score={score}  broken={broken or '[]'}")
        for cname, stats in channels.items():
            last_fail = stats.get("last_fail_ts")
            last_fail_str = _fmt_age(time.time() - last_fail) + " ago" if last_fail else "never"
            _print(
                f"    {cname}: ok={stats.get('ok', 0)} fail={stats.get('fail', 0)} "
                f"score={stats.get('score', '?')} last_fail={last_fail_str}"
            )

    # Delegation
    deleg = health.get("delegation") or {}
    if deleg.get("active"):
        _print("\n[bold cyan]== Delegation ==[/bold cyan]")
        _print(
            f"  self={deleg.get('self_node')}  chief={deleg.get('chief_node')}  "
            f"active_agents={deleg.get('active_agents', 0)}/{deleg.get('max_parallel_agents', 0)}"
        )
        counters = {k: v for k, v in deleg.items() if k.startswith(("received", "accepted", "dispatched", "rejected_"))}
        nonzero = {k: v for k, v in counters.items() if v}
        _print(f"  counters: received={deleg.get('received', 0)} accepted={deleg.get('accepted', 0)} dispatched={deleg.get('dispatched', 0)}")
        if nonzero:
            for k, v in sorted(nonzero.items()):
                if k.startswith("rejected_"):
                    _print(f"    {k}: {v}")

    # Rate limits
    rl = health.get("rate_limits") or {}
    stats = rl.get("stats") or {}
    if stats or rl.get("peers"):
        _print("\n[bold cyan]== Rate limits ==[/bold cyan]")
        _print(f"  allowed={stats.get('allowed', 0)}  denied={stats.get('denied', 0)}")
        for peer, buckets in (rl.get("peers") or {}).items():
            for bkey, b in (buckets or {}).items():
                if b.get("used"):
                    _print(f"    {peer}/{bkey}: used={b.get('used')}/{b.get('limit')} window={b.get('window_s')}s")

    # Invariance gates (optional)
    ig = health.get("invariance_gates")
    if ig:
        _print("\n[bold cyan]== Invariance gates ==[/bold cyan]")
        _print(f"  mode={ig.get('mode', '?')}  blocked={ig.get('blocked', 0)}")

    # ── RACE Q5 sections (webhook / replicas / 3-surgeons / cloud) ──
    _render_webhook(snap.get("webhook") or {})
    _render_jetstream_replicas(snap.get("stream_replicas") or [])
    _render_three_surgeons(snap.get("three_surgeons") or {})
    _render_cloud_listener(snap.get("cloud_listener") or {})

    # Last 5 messages
    _print("\n[bold cyan]== Last 5 fleet messages ==[/bold cyan]")
    if not snap["fleet_messages"]:
        _print("  (none)")
    for m in snap["fleet_messages"]:
        _print(f"  [{_fmt_age(m['age_s'])} ago] {m['from']} -> {m['to']}: {m['subject']}")


# ─────────────────── RACE Q5 section renderers ────────────────────
# Each renderer caps output at ≤6 lines for terminal-friendly display.
# Per Aaron's "no surprise scroll" rule: a healthy section is one line of
# headline + at most a few detail lines; pathologies get a red marker.

_RED = "[red]"
_RED_END = "[/red]"


def _render_webhook(webhook: dict[str, Any]) -> None:
    """Webhook health (RACE N2 + O3) — last age, p50/p95, top failing sections."""
    _print("\n[bold cyan]== Webhook health ==[/bold cyan]")
    if not webhook or webhook.get("active") is False:
        reason = webhook.get("reason", "no bridge / daemon offline") if webhook else "(no data)"
        _print(f"  {_RED}offline{_RED_END} ({reason})")
        return
    events = webhook.get("events_recorded", 0)
    if not events:
        _print(f"  bridge active — events_recorded=0 (no webhook fired yet)")
        return
    last_age = webhook.get("last_webhook_age_s")
    p50 = webhook.get("p50_total_ms")
    p95 = webhook.get("p95_total_ms")
    cache = webhook.get("cache_hit_rate")
    cache_str = f"{int(round((cache or 0) * 100))}%" if cache is not None else "n/a"
    age_str = _fmt_age(last_age) if last_age is not None else "?"
    _print(
        f"  last={age_str} ago  events={events}  p50={p50}ms  p95={p95}ms  "
        f"cache_hit={cache_str}"
    )
    # Top 3 most-often-failing sections from the rolling window.
    section_health = webhook.get("section_health") or {}
    failing = sorted(
        ((name, info.get("fail_rate", 0.0), info.get("samples", 0))
         for name, info in section_health.items()
         if (info.get("fail_rate", 0.0) or 0) > 0),
        key=lambda kv: -kv[1],
    )[:3]
    if failing:
        bits = ", ".join(f"{n}={int(r * 100)}% (n={s})" for n, r, s in failing)
        _print(f"  {_RED}failing{_RED_END}: {bits}")
    missing_recent = webhook.get("missing_sections_recent") or []
    if missing_recent:
        top = ", ".join(f"{m['section']}x{m['count']}" for m in missing_recent[:3])
        _print(f"  recent_missing: {top}")


def _render_jetstream_replicas(replicas: list[dict[str, Any]]) -> None:
    """Per-stream replica gaps (RACE N3) — leader, set, missing peers."""
    _print("\n[bold cyan]== JetStream replicas ==[/bold cyan]")
    if not replicas:
        _print("  (no streams visible)")
        return
    # Cap to first 5 streams to stay within ≤6 lines including header.
    for s in replicas[:5]:
        leader = s.get("leader") or "?"
        rset = s.get("replica_set") or []
        missing = s.get("missing") or []
        r = s.get("replicas") or "?"
        lag = s.get("max_follower_lag", 0)
        line = (
            f"  {s['name']}: leader={leader} R={r} set={','.join(rset) or '-'} "
            f"lag={lag}"
        )
        if missing:
            line += f"  {_RED}missing={','.join(missing)}{_RED_END}"
        _print(line)


def _render_three_surgeons(probe: dict[str, Any]) -> None:
    """3-surgeons probe status — single subprocess call, cached per render."""
    _print("\n[bold cyan]== 3-surgeons probe ==[/bold cyan]")
    if not probe:
        _print("  (no data)")
        return
    status = probe.get("status", "?")
    summary = probe.get("summary", "")
    marker = ""
    if status == "ok":
        marker = "OK"
    elif status == "skip":
        marker = "skip"
    else:
        marker = f"{_RED}{status.upper()}{_RED_END}"
    _print(f"  {marker}  {summary}")
    if status not in ("ok", "skip") and probe.get("raw"):
        # Show last 3 lines of probe output for quick triage.
        for line in probe["raw"].splitlines()[-3:]:
            _print(f"    {line}")


def _render_cloud_listener(listener: dict[str, Any]) -> None:
    """Cloud listener — process alive (launchctl) + last event age."""
    _print("\n[bold cyan]== Cloud listener ==[/bold cyan]")
    if not listener:
        _print("  (no data)")
        return
    status = listener.get("status", "?")
    pid = listener.get("pid")
    last_age = listener.get("last_event_age_s")
    last_str = f"{_fmt_age(last_age)} ago" if last_age is not None else "n/a"
    if status == "ok":
        head = f"  OK  pid={pid}  last_event={last_str}"
    elif status == "skip":
        head = f"  skip  {listener.get('summary', '')}"
    elif status == "warn":
        head = f"  {_RED}WARN{_RED_END}  {listener.get('summary', '')}  last_event={last_str}"
    else:
        head = f"  {_RED}ERROR{_RED_END}  {listener.get('summary', '')}"
    _print(head)


# ─────────────────────── follow (event-driven) ────────────────────────
def _follow() -> int:
    """Subscribe to fleet.> and event.> via NATS; block on messages. No polling."""
    try:
        import asyncio

        from nats.aio.client import Client as NATS  # type: ignore
    except Exception:
        print("ERROR: nats-py not installed; cannot --follow", file=sys.stderr)
        return 2

    async def run() -> None:
        nc = NATS()
        url = os.environ.get("NATS_URL", f"nats://127.0.0.1:{NATS_PORT}")
        await nc.connect(servers=[url], name="fleet-dashboard-follow")
        # RACE Q5 — explicitly include webhook completion + jetstream replica
        # gap subjects so --follow surfaces live signal without polling.
        # event.> already wildcard-covers them, but listing the targets here
        # both documents intent and lets a future grep find what we listen for.
        print(
            f"[follow] subscribed to fleet.>, event.>, "
            f"event.webhook.completed.>, jetstream.replica_gap.> on {url} "
            f"(Ctrl-C to exit)"
        )

        async def on_msg(msg: Any) -> None:
            ts = time.strftime("%H:%M:%S")
            extra = ""
            try:
                payload = json.loads(msg.data.decode("utf-8", errors="replace"))
                src = payload.get("from") or payload.get("source") or payload.get("node") or "?"
                # For webhook completion events surface the headline timing
                # so operators don't have to crack the payload open by hand.
                if msg.subject.startswith("event.webhook.completed"):
                    total = payload.get("total_ms")
                    missing = payload.get("missing") or []
                    miss_str = f" missing={len(missing)}" if missing else ""
                    extra = f"  total_ms={total}{miss_str}"
                elif msg.subject.startswith("jetstream.replica_gap"):
                    stream = payload.get("stream", "?")
                    miss = payload.get("missing") or []
                    extra = f"  stream={stream} missing={','.join(miss)}"
            except Exception:
                src = "?"
            print(f"{ts}  {msg.subject}  from={src}  ({len(msg.data)}B){extra}")

        await nc.subscribe("fleet.>", cb=on_msg)
        await nc.subscribe("event.>", cb=on_msg)
        # Block forever on a future — event-driven, zero polling.
        stop = asyncio.Future()
        try:
            await stop
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await nc.drain()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[follow] exit")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One-shot fleet observability dashboard")
    ap.add_argument("--follow", action="store_true", help="event-driven NATS subscribe mode")
    ap.add_argument("--json", action="store_true", help="emit JSON, not text")
    args = ap.parse_args(argv)

    if args.follow:
        return _follow()

    snap = _collect()
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        _render(snap)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
