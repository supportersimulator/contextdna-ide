#!/usr/bin/env python3
"""unify-cluster-urls.py — Patch the local NATS-cluster launchd plist so its
``--routes`` value matches the canonical fleet config.

Why: QQ5 (`.fleet/audits/2026-05-08-QQ5-nats-cluster-live-probe.md`) found
mac1↔mac2 had no direct NATS route because mac1's plist used raw LAN IPs
while mac2/mac3 used `.local` mDNS hostnames. mDNS resolution drifts
across machines, so the cluster auto-heals via the mac3 hub — a single
point of failure if mac3 reboots.

This script normalises every node's plist routes to **raw lan_ip**
(deterministic, no DNS dependency) sourced from `.multifleet/config.json`.

Modes:
    --dry-run   (default) print unified diff, no writes.
    --apply     write `<plist>.bak` then update plist; print kickstart hint.
    --revert    restore plist from `<plist>.bak` (latest).

ZSF: any read/parse error → exit 1, verbatim error, no silent drift.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# ── paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / ".multifleet" / "config.json"
DEFAULT_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / "io.contextdna.nats-server.plist"
)

# Cluster port — kept in sync with multifleet.constants.NATS_CLUSTER_PORT.
# Hardcoded here rather than imported so the script runs even when the
# multifleet venv is not active (read-only convenience).
NATS_CLUSTER_PORT = int(os.environ.get("NATS_CLUSTER_PORT", "6222"))

# Match the `<string>nats://...:<port>,nats://...:<port></string>` element
# that immediately follows a `<string>--routes</string>` element in
# `ProgramArguments`. ProgramArguments is an XML <array> of <string>s, and
# nats-server takes the routes as the next argument after `--routes`.
_ROUTES_RE = re.compile(
    r"(<string>--routes</string>\s*\n?\s*<string>)([^<]*)(</string>)",
    re.MULTILINE,
)


# ── canonical config ───────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"canonical config missing: {CONFIG_PATH}")
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"failed to parse {CONFIG_PATH}: {e}")


def detect_node_id(cfg: dict, override: str | None = None) -> str:
    """Detect this node's id using the same precedence as fleet-node-id.sh."""
    if override:
        return override
    env_id = os.environ.get("MULTIFLEET_NODE_ID")
    if env_id:
        return env_id
    # IP match against config
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        my_ip = s.getsockname()[0]
        s.close()
    except OSError:
        my_ip = ""
    for nid, node in (cfg.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get("host") == my_ip or node.get("lan_ip") == my_ip:
            return nid
    # hostname fallback
    host = socket.gethostname().split(".")[0].lower()
    if host in (cfg.get("nodes") or {}):
        return host
    raise SystemExit(
        f"could not detect node_id (env MULTIFLEET_NODE_ID unset, "
        f"local IP {my_ip!r} not in config, hostname {host!r} not in nodes). "
        "Pass --node-id explicitly."
    )


def canonical_routes(cfg: dict, node_id: str) -> str:
    """Return canonical `--routes` value for `node_id`, sorted for determinism.

    Format: `nats://<peer1_lan_ip>:6222,nats://<peer2_lan_ip>:6222,...`
    Excludes self. Uses `lan_ip` if present, else falls back to `host`.
    """
    nodes = cfg.get("nodes") or {}
    if node_id not in nodes:
        raise SystemExit(
            f"node_id {node_id!r} not in config; known: {sorted(nodes)}"
        )
    peers: list[tuple[str, str]] = []
    for nid, node in nodes.items():
        if nid == node_id:
            continue
        ip = (node or {}).get("lan_ip") or (node or {}).get("host")
        if not ip:
            raise SystemExit(
                f"node {nid!r} missing both lan_ip and host in {CONFIG_PATH}"
            )
        peers.append((nid, ip))
    peers.sort(key=lambda p: p[0])  # alphabetical by node id → deterministic
    return ",".join(f"nats://{ip}:{NATS_CLUSTER_PORT}" for _, ip in peers)


# ── plist ops ──────────────────────────────────────────────────────────────
def read_plist(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"plist not found: {path}")
    try:
        return path.read_text()
    except OSError as e:
        raise SystemExit(f"failed to read {path}: {e}")


def extract_routes(plist_text: str) -> str:
    m = _ROUTES_RE.search(plist_text)
    if not m:
        raise SystemExit(
            "plist does not contain a <string>--routes</string> element "
            "followed by a routes <string>. Refusing to patch a plist that "
            "doesn't have a clustered nats-server invocation."
        )
    return m.group(2)


def patch_plist(plist_text: str, new_routes: str) -> str:
    new_text, n = _ROUTES_RE.subn(
        lambda m: m.group(1) + new_routes + m.group(3),
        plist_text,
        count=1,
    )
    if n != 1:
        raise SystemExit(
            f"expected exactly 1 --routes substitution, made {n}. "
            "Plist may have an unexpected shape."
        )
    return new_text


def unified_diff(old: str, new: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{label} (current)",
            tofile=f"{label} (canonical)",
            n=2,
        )
    )


# ── modes ──────────────────────────────────────────────────────────────────
def cmd_dry_run(plist: Path, node_id: str, cfg: dict) -> int:
    current = read_plist(plist)
    current_routes = extract_routes(current)
    target_routes = canonical_routes(cfg, node_id)

    print(f"node_id:         {node_id}")
    print(f"plist:           {plist}")
    print(f"canonical src:   {CONFIG_PATH}")
    print(f"current routes:  {current_routes}")
    print(f"canonical routes: {target_routes}")
    print()

    if current_routes == target_routes:
        print("no changes needed — plist already matches canonical config.")
        return 0

    new_text = patch_plist(current, target_routes)
    diff = unified_diff(current, new_text, plist.name)
    if diff:
        print("--- diff (dry-run, no writes) ---")
        print(diff)
    print("re-run with --apply to write changes.")
    return 0


def cmd_apply(plist: Path, node_id: str, cfg: dict) -> int:
    current = read_plist(plist)
    current_routes = extract_routes(current)
    target_routes = canonical_routes(cfg, node_id)

    if current_routes == target_routes:
        print(f"no changes needed for {plist.name} — already canonical.")
        return 0

    bak = plist.with_suffix(plist.suffix + ".bak")
    try:
        shutil.copy2(plist, bak)
    except OSError as e:
        raise SystemExit(f"failed to write backup {bak}: {e}")

    new_text = patch_plist(current, target_routes)
    try:
        plist.write_text(new_text)
    except OSError as e:
        raise SystemExit(f"failed to write {plist}: {e}")

    print(f"backup:  {bak}")
    print(f"updated: {plist}")
    print(f"new --routes: {target_routes}")
    print()
    print("Aaron must run the following to take effect:")
    print(f"  launchctl kickstart -k gui/$(id -u)/io.contextdna.nats-server")
    print()
    print("Verify after kickstart:")
    print("  curl -s http://127.0.0.1:8222/routez | python3 -m json.tool | head -40")
    return 0


def cmd_revert(plist: Path) -> int:
    bak = plist.with_suffix(plist.suffix + ".bak")
    if not bak.exists():
        raise SystemExit(f"no backup found at {bak}")
    try:
        shutil.copy2(bak, plist)
    except OSError as e:
        raise SystemExit(f"failed to restore {plist} from {bak}: {e}")
    print(f"restored {plist} from {bak}")
    print("kickstart to apply: launchctl kickstart -k gui/$(id -u)/io.contextdna.nats-server")
    return 0


# ── cli ────────────────────────────────────────────────────────────────────
def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Unify NATS cluster --routes in the local launchd plist to "
            "match canonical .multifleet/config.json (raw lan_ip)."
        )
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print diff only, no writes (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="write .bak then update plist (kickstart still required)",
    )
    mode.add_argument(
        "--revert",
        action="store_true",
        help="restore plist from latest .bak",
    )
    p.add_argument(
        "--plist",
        type=Path,
        default=DEFAULT_PLIST,
        help=f"plist path (default: {DEFAULT_PLIST})",
    )
    p.add_argument(
        "--node-id",
        help="override node id detection (one of mac1, mac2, mac3, ...)",
    )
    return p.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.revert:
        return cmd_revert(args.plist)
    cfg = load_config()
    node_id = detect_node_id(cfg, args.node_id)
    if args.apply:
        return cmd_apply(args.plist, node_id, cfg)
    # default = dry-run (also when --dry-run flag explicit)
    return cmd_dry_run(args.plist, node_id, cfg)


if __name__ == "__main__":
    sys.exit(main())
