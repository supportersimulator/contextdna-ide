#!/usr/bin/env python3
"""channel-smoketest.py — Weekly exercise of every fleet channel (P1-P7).

WHY: In normal traffic the cascade short-circuits on the first success
(almost always P1 NATS). The lower channels (P4 seed / P5 ssh / P6 WoL /
P7 git) are NEVER exercised until something is already broken — by which
time we don't know if the fallback itself still works. WW7-E mission.

This script directly probes each channel for each peer with INERT
payloads, then:
  1. Appends per-probe results to `.fleet/channel-smoketest/<date>.json`
  2. POSTs a success/failure summary to the local daemon `/message`
     endpoint addressed to `all` — this lets the daemon's natural
     `_record_channel_attempt` ledger update via the normal cascade
     so `channel_reliability` on /health advances for the smoketest's
     own P1/P2 path.

Inert markers used (so observers can filter):
  - subject prefix: `[SMOKE]`
  - payload field: `"smoketest": true`
  - seed filename prefix: `smoketest-`
  - ssh remote tag: `# fleet-smoketest probe`

Channels exercised (per peer):
  P1 NATS    — POST /message via local daemon, subject `[SMOKE] P1 probe`
  P2 HTTP    — direct GET http://<peer-host>:8855/health (5s timeout)
  P2T Tail   — direct GET http://<tailscale_ip>:8855/health (skip if empty)
  P4 seed    — scp inert file to ~/.fleet-messages/<peer>/smoketest-*.md
  P5 SSH     — ssh peer 'echo fleet-smoketest probe rc=$?'
  P6 WoL     — DRY-RUN: print the wakeonlan command we'd run (no packet)
  P7 git     — `git ls-remote origin HEAD` (verifies origin reachable)

Acceptance criteria after one run:
  - .fleet/channel-smoketest/<date>.json exists with one row per
    (peer, channel) and a status of ok|fail|skip|dry-run
  - For peers that are alive: P1 + P2 + P5 + P7 should be `ok`
  - Daemon /health `channel_reliability.<peer>.P1_nats.ok` advances
    by at least 1 (because the smoketest's P1 probe goes through the
    real cascade and updates the ledger)

ZSF: every channel attempt records a row. Exceptions are caught and
recorded as `fail` with the exception class name in `reason`. The
script's exit code is 0 if the ledger write succeeded — individual
channel failures are surfaced in the ledger, not as exit codes.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".multifleet" / "config.json"
LEDGER_DIR = REPO_ROOT / ".fleet" / "channel-smoketest"
DAEMON_URL = os.environ.get("FLEET_DAEMON_URL", "http://127.0.0.1:8855")
PROBE_TIMEOUT_S = int(os.environ.get("SMOKETEST_TIMEOUT_S", "8"))

SMOKE_SUBJECT = "[SMOKE] channel smoketest probe"
SMOKE_BODY_TEMPLATE = (
    "Inert weekly channel-smoketest probe. "
    "Source: scripts/channel-smoketest.py. "
    "Run: {run_id}. "
    "smoketest=true. "
    "If you receive this, the channel WORKS — no action required."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _self_node_id() -> str:
    out = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "fleet-node-id.sh")],
        capture_output=True, text=True, timeout=5,
    )
    return out.stdout.strip() or "unknown"


def _load_config() -> dict:
    with open(CONFIG_PATH, "r") as fh:
        return json.load(fh)


def _peers(cfg: dict, self_id: str) -> list[tuple[str, dict]]:
    return [(nid, nval) for nid, nval in cfg.get("nodes", {}).items()
            if nid != self_id]


def _record(rows: list[dict], peer: str, channel: str, status: str,
            latency_ms: int, reason: str = "") -> None:
    rows.append({
        "ts": _now_iso(),
        "peer": peer,
        "channel": channel,
        "status": status,            # ok | fail | skip | dry-run
        "latency_ms": latency_ms,
        "reason": reason,
    })


# ── Channel probes ─────────────────────────────────────────────────


def probe_p1_nats(peer: str, run_id: str) -> tuple[str, int, str]:
    """POST to local daemon /message — daemon delivers via P1 NATS."""
    payload = {
        "type": "context",
        "from": _self_node_id(),
        "to": peer,
        "payload": {
            "subject": SMOKE_SUBJECT + " (P1)",
            "body": SMOKE_BODY_TEMPLATE.format(run_id=run_id),
            "priority": "low",
            "smoketest": True,
        },
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{DAEMON_URL}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            body = resp.read().decode(errors="replace")
        ms = int((time.time() - t0) * 1000)
        # daemon returns JSON; just record HTTP-level success.
        if "delivered" in body or resp.status == 200:
            return ("ok", ms, "")
        return ("fail", ms, f"daemon response: {body[:120]}")
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        return ("fail", ms, f"{type(exc).__name__}: {exc}")


def probe_http(host: str, label: str) -> tuple[str, int, str]:
    """Direct GET on http://<host>:8855/health (P2 LAN or P2T Tailscale)."""
    if not host:
        return ("skip", 0, f"no {label} host in config")
    url = f"http://{host}:8855/health"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_S) as resp:
            resp.read(64)  # tiny read; we just need a response
        ms = int((time.time() - t0) * 1000)
        return ("ok", ms, "")
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        return ("fail", ms, f"{type(exc).__name__}: {exc}")


def probe_p4_seed(peer: str, peer_cfg: dict, run_id: str) -> tuple[str, int, str]:
    """scp an inert file to ~/.fleet-messages/<peer>/smoketest-*.md on peer."""
    host = peer_cfg.get("host") or peer_cfg.get("lan_ip")
    user = peer_cfg.get("user") or os.environ.get("USER", "")
    if not host or not user:
        return ("skip", 0, "no host/user in config")
    local_tmp = Path(f"/tmp/smoketest-{run_id}-{peer}.md")
    local_tmp.write_text(
        f"# Channel smoketest seed (inert)\n\n"
        f"run_id: {run_id}\n"
        f"channel: P4_seed\n"
        f"from: {_self_node_id()}\n"
        f"to: {peer}\n"
        f"smoketest: true\n"
    )
    remote_dir = f".fleet-messages/{peer}"
    remote = f"{user}@{host}:{remote_dir}/{local_tmp.name}"
    t0 = time.time()
    try:
        # Ensure remote dir exists (peer may have never received a seed).
        # This mirrors what real P4 flow does — the daemon creates the
        # destination directory before scp; smoketest must do the same.
        mk = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             f"{user}@{host}", f"mkdir -p ~/{remote_dir}"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
        if mk.returncode != 0:
            ms = int((time.time() - t0) * 1000)
            return ("fail", ms, f"mkdir rc={mk.returncode}: {mk.stderr.strip()[:120]}")
        proc = subprocess.run(
            ["scp", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             str(local_tmp), remote],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 4,
        )
        ms = int((time.time() - t0) * 1000)
        if proc.returncode == 0:
            return ("ok", ms, "")
        err = (proc.stderr or proc.stdout).strip().splitlines()[-1:]
        return ("fail", ms, f"scp rc={proc.returncode}: {err}")
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        return ("fail", ms, f"{type(exc).__name__}: {exc}")


def probe_p5_ssh(peer: str, peer_cfg: dict) -> tuple[str, int, str]:
    host = peer_cfg.get("host") or peer_cfg.get("lan_ip")
    user = peer_cfg.get("user") or os.environ.get("USER", "")
    if not host or not user:
        return ("skip", 0, "no host/user in config")
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             f"{user}@{host}", "true  # fleet-smoketest probe"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 4,
        )
        ms = int((time.time() - t0) * 1000)
        if proc.returncode == 0:
            return ("ok", ms, "")
        return ("fail", ms, f"ssh rc={proc.returncode}")
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        return ("fail", ms, f"{type(exc).__name__}: {exc}")


def probe_p6_wol(peer: str, peer_cfg: dict) -> tuple[str, int, str]:
    """DRY-RUN — never actually wake a peer. Verifies the MAC + tool exist."""
    mac = peer_cfg.get("mac_address", "")
    if not mac:
        return ("skip", 0, "no mac_address in config")
    have_wol = subprocess.run(["which", "wakeonlan"],
                              capture_output=True).returncode == 0
    if not have_wol:
        return ("fail", 0, "wakeonlan binary not installed")
    return ("dry-run", 0, f"would: wakeonlan {mac}")


def probe_p7_git(peer: str) -> tuple[str, int, str]:
    """git ls-remote origin HEAD — verifies the git fallback path works."""
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", "HEAD"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 4,
            cwd=str(REPO_ROOT),
        )
        ms = int((time.time() - t0) * 1000)
        if proc.returncode == 0:
            return ("ok", ms, "")
        return ("fail", ms, f"git ls-remote rc={proc.returncode}")
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        return ("fail", ms, f"{type(exc).__name__}: {exc}")


# ── Main orchestration ────────────────────────────────────────────


def run_smoketest(dry_run: bool = False) -> int:
    self_id = _self_node_id()
    cfg = _load_config()
    peers = _peers(cfg, self_id)
    run_id = f"{int(time.time())}-{socket.gethostname()}"
    rows: list[dict] = []

    print(f"[smoketest] run_id={run_id} self={self_id} peers={[p[0] for p in peers]}",
          file=sys.stderr)

    for peer, peer_cfg in peers:
        print(f"[smoketest] peer={peer}", file=sys.stderr)
        # P1 NATS (via daemon)
        status, ms, reason = probe_p1_nats(peer, run_id)
        _record(rows, peer, "P1_nats", status, ms, reason)
        # P2 HTTP LAN
        host = peer_cfg.get("host") or peer_cfg.get("lan_ip", "")
        status, ms, reason = probe_http(host, "P2 LAN")
        _record(rows, peer, "P2_http", status, ms, reason)
        # P2T Tailscale
        tail = peer_cfg.get("tailscale_ip", "")
        status, ms, reason = probe_http(tail, "P2T tailscale")
        _record(rows, peer, "P2t_tailscale", status, ms, reason)
        # P4 seed
        status, ms, reason = probe_p4_seed(peer, peer_cfg, run_id)
        _record(rows, peer, "P4_seed", status, ms, reason)
        # P5 ssh
        status, ms, reason = probe_p5_ssh(peer, peer_cfg)
        _record(rows, peer, "P5_ssh", status, ms, reason)
        # P6 WoL (dry-run)
        status, ms, reason = probe_p6_wol(peer, peer_cfg)
        _record(rows, peer, "P6_wol", status, ms, reason)
        # P7 git (origin-level; same for every peer but recorded per-peer
        # so we can see if mirrors drift in future)
        status, ms, reason = probe_p7_git(peer)
        _record(rows, peer, "P7_git", status, ms, reason)

    # Write ledger
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ledger_file = LEDGER_DIR / f"{today}.json"
    # Append-style: load existing rows, extend, rewrite atomically
    existing: list[dict] = []
    if ledger_file.exists():
        try:
            existing = json.loads(ledger_file.read_text())
            if not isinstance(existing, list):
                existing = []
        except Exception as exc:
            print(f"[smoketest] WARN existing ledger unreadable ({exc}) — "
                  f"rotating to .corrupt", file=sys.stderr)
            ledger_file.rename(ledger_file.with_suffix(".corrupt.json"))
            existing = []
    all_rows = existing + rows
    tmp = ledger_file.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(all_rows, indent=2) + "\n")
    tmp.replace(ledger_file)

    # Quick console summary
    ok = sum(1 for r in rows if r["status"] == "ok")
    fail = sum(1 for r in rows if r["status"] == "fail")
    skip = sum(1 for r in rows if r["status"] == "skip")
    dry = sum(1 for r in rows if r["status"] == "dry-run")
    print(f"[smoketest] DONE rows={len(rows)} ok={ok} fail={fail} "
          f"skip={skip} dry-run={dry} ledger={ledger_file}", file=sys.stderr)

    if dry_run:
        return 0
    return 0  # never propagate channel failures as process failure


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="run probes but don't propagate failures")
    args = ap.parse_args()
    return run_smoketest(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
