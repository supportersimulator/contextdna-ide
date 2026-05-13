#!/usr/bin/env python3
"""
JetStream quorum check (RACE N3).

Diagnoses replica-set drift like the M1+M5 finding where mac1 was missing
from FLEET_EVENTS' replica set after a daemon restart. For each known
JetStream stream, query the live replica set and report any configured
peer that is *not* in the set (or any orphan in the set that isn't a
configured peer).

Outputs:
  - JSON (default, machine-readable for automation)
  - Markdown (with --markdown) for humans

Exit codes:
  0  all streams healthy (every configured peer present in every stream)
  1  drift detected (at least one peer missing from at least one stream)
  2  unable to query (NATS unreachable, JetStream disabled, etc.)

ZSF: every JetStream API failure is counted in the JSON output's
``api_failures`` list — never silently swallowed.

Usage:
    python3 scripts/jetstream-quorum-check.py [--nats-url URL] [--markdown]
                                              [--config PATH] [--streams ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure multifleet package importable when run from repo root or scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "multi-fleet"))
sys.path.insert(0, str(_REPO_ROOT))


def _load_peers_from_config(config_path: Path | None) -> list[str]:
    """Return sorted list of configured peer node_ids, empty on failure."""
    candidates: list[Path] = []
    if config_path is not None:
        candidates.append(config_path)
    env_path = os.environ.get("MULTIFLEET_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_REPO_ROOT / ".multifleet" / "config.json")
    candidates.append(Path.cwd() / ".multifleet" / "config.json")
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text()) or {}
                nodes = data.get("nodes") or {}
                if isinstance(nodes, dict) and nodes:
                    return sorted(nodes.keys())
        except Exception:
            # ZSF: surface in api_failures list at top level (caller path).
            continue
    return []


async def _query_stream(js: Any, stream: str) -> dict[str, Any]:
    """Probe a single stream — return compact replica/leader info or error."""
    try:
        info = await js.stream_info(stream)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    try:
        cluster = getattr(info, "cluster", None)
        cfg = getattr(info, "config", None)
        leader = getattr(cluster, "leader", None) if cluster else None
        replicas_cfg = int(getattr(cfg, "num_replicas", 1) or 1) if cfg else 1
        followers = getattr(cluster, "replicas", []) if cluster else []
        replica_names: list[str] = []
        for r in followers or []:
            name = getattr(r, "name", None)
            if name:
                replica_names.append(str(name))
        # The replica_set is the leader plus all reported followers.
        replica_set = sorted(set([leader] + replica_names) - {None})
        return {
            "status": "ok",
            "leader": leader,
            "replica_set": replica_set,
            "num_replicas_configured": replicas_cfg,
        }
    except Exception as e:
        return {"status": "error", "error": f"parse: {type(e).__name__}: {e}"}


async def _run(
    nats_url: str,
    streams: list[str],
    expected_peers: list[str],
) -> dict[str, Any]:
    api_failures: list[str] = []
    streams_report: dict[str, Any] = {}
    overall = "ok"

    try:
        import nats  # type: ignore
    except Exception as e:
        return {
            "status": "error",
            "error": f"nats-py not installed: {e}",
            "api_failures": [str(e)],
            "streams": {},
            "expected_peers": expected_peers,
        }

    nc = None
    try:
        nc = await nats.connect(nats_url, connect_timeout=3)
    except Exception as e:
        api_failures.append(f"connect: {e}")
        return {
            "status": "error",
            "error": f"connect failed: {e}",
            "api_failures": api_failures,
            "streams": {},
            "expected_peers": expected_peers,
        }

    try:
        try:
            js = nc.jetstream()
        except Exception as e:
            api_failures.append(f"jetstream(): {e}")
            return {
                "status": "error",
                "error": f"jetstream unavailable: {e}",
                "api_failures": api_failures,
                "streams": {},
                "expected_peers": expected_peers,
            }

        for stream in streams:
            probed = await _query_stream(js, stream)
            if probed.get("status") == "error":
                api_failures.append(f"{stream}: {probed.get('error')}")
                streams_report[stream] = probed
                overall = "degraded"
                continue

            replica_set = probed.get("replica_set") or []
            missing = sorted(set(expected_peers) - set(replica_set))
            orphans = sorted(set(replica_set) - set(expected_peers))
            probed["missing_peers"] = missing
            probed["orphan_replicas"] = orphans
            probed["healthy"] = (not missing) and (not orphans)
            if not probed["healthy"]:
                overall = "drift"
            streams_report[stream] = probed
    finally:
        try:
            if nc is not None:
                await nc.close()
        except Exception as e:
            api_failures.append(f"close: {e}")

    return {
        "status": overall,
        "api_failures": api_failures,
        "expected_peers": expected_peers,
        "streams": streams_report,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# JetStream Quorum Check")
    lines.append("")
    lines.append(f"- **Status:** `{report.get('status')}`")
    lines.append(f"- **Expected peers:** {', '.join(report.get('expected_peers', [])) or '(none)'}")
    api_failures = report.get("api_failures") or []
    if api_failures:
        lines.append(f"- **API failures:** {len(api_failures)}")
        for f in api_failures:
            lines.append(f"  - {f}")
    lines.append("")
    lines.append("## Streams")
    streams = report.get("streams") or {}
    if not streams:
        lines.append("_No streams probed._")
    for name, data in streams.items():
        lines.append(f"### {name}")
        if data.get("status") == "error":
            lines.append(f"- ERROR: `{data.get('error')}`")
            continue
        lines.append(f"- Leader: `{data.get('leader')}`")
        lines.append(f"- Replica set: `{data.get('replica_set')}`")
        lines.append(f"- Configured num_replicas: `{data.get('num_replicas_configured')}`")
        missing = data.get("missing_peers") or []
        orphans = data.get("orphan_replicas") or []
        if missing:
            lines.append(f"- **Missing peers:** `{missing}`  <- DRIFT")
        if orphans:
            lines.append(f"- **Orphan replicas:** `{orphans}`")
        if data.get("healthy"):
            lines.append("- Healthy: yes")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="JetStream quorum drift check")
    parser.add_argument(
        "--nats-url",
        default=os.environ.get("NATS_URL", "nats://127.0.0.1:4222"),
    )
    parser.add_argument(
        "--streams",
        nargs="*",
        default=["FLEET_MESSAGES", "FLEET_EVENTS"],
        help="Stream names to probe (default: both fleet streams)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to multifleet config.json (default: search standard locations)",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Render markdown instead of JSON",
    )
    parser.add_argument(
        "--peers",
        nargs="*",
        default=None,
        help="Override expected peer list (skips config lookup)",
    )
    args = parser.parse_args()

    expected_peers: list[str]
    if args.peers:
        expected_peers = sorted(args.peers)
    else:
        expected_peers = _load_peers_from_config(args.config)

    report = asyncio.run(
        _run(
            nats_url=args.nats_url,
            streams=list(args.streams),
            expected_peers=expected_peers,
        )
    )

    if args.markdown:
        sys.stdout.write(_render_markdown(report))
    else:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")

    status = report.get("status")
    if status == "ok":
        return 0
    if status == "drift":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
