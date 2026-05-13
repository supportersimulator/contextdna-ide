#!/usr/bin/env python3
"""Dump a JSON snapshot of recent race events for the IDE.

Y1 Race Theater backend (2026-05-07).

This is the cross-fleet aggregation lever — the publisher
(``tools/fleet_race_publisher.py``) ALREADY writes the snapshot directly
on the publishing node. This dumper exists for two narrow cases:

  1. Aggregating snapshots from peers when the publishing node is not the
     same as the IDE host — subscribes to ``race.event.>`` for ``--seconds``
     and merges everything seen into the snapshot.
  2. Re-building the snapshot from a corpus of loop-result JSON files
     when the live NATS bus is unavailable.

Architecture choice (audit ``2026-05-07-Y1-race-theater-backend.md``):

  Snapshot bridge JSON (NOT SQLite, NOT subscribe-on-request from the route)
  is the simplest path that keeps the Next.js process stdlib-only — same
  pattern as ``dump-evidence-ledger-summary.py``. The publisher writes
  on every event; this script is the ``catch up`` lever for cross-node
  visibility.

Output path: ``dashboard_exports/race_events_snapshot.json``.

ZSF: every error path bumps a counter in
``multifleet.race_events.counters_snapshot()``. The snapshot file always
includes the latest counters dict so dashboards can plot publish health
inline with the race list.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
if str(_REPO_ROOT / "multi-fleet") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "multi-fleet"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from multifleet.race_events import (  # noqa: E402  — sys.path side effect
    _bump,
    counters_snapshot,
)
from tools.fleet_race_publisher import (  # noqa: E402  — sys.path side effect
    DEFAULT_SNAPSHOT,
    SCHEMA_VERSION,
    _atomic_write,
    _load_snapshot,
    _merge_snapshot,
    _resolve_snapshot_path,
)


async def _subscribe_for(seconds: float, subject: str = "race.event.>") -> List[Dict[str, Any]]:
    """Subscribe for ``seconds`` and return decoded payloads.

    Returns [] on transport-missing (counter bumped). Never raises.
    """
    try:
        import nats  # type: ignore
    except ImportError:
        _bump("race_events_publish_errors_total")
        sys.stderr.write("dump-race-events-snapshot: nats-py not installed — empty\n")
        return []
    nats_url = os.environ.get("NATS_URL") or "nats://127.0.0.1:4222"
    try:
        client = await asyncio.wait_for(
            nats.connect(nats_url, name="dump-race-events-snapshot"),
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        _bump("race_events_publish_errors_total")
        sys.stderr.write(
            f"dump-race-events-snapshot: connect failed ({type(exc).__name__}): {exc}\n"
        )
        return []

    collected: List[Dict[str, Any]] = []

    async def _handle(msg) -> None:  # noqa: ANN001 — nats msg
        try:
            payload = json.loads(msg.data.decode("utf-8"))
            if isinstance(payload, dict):
                collected.append(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _bump("race_event_serialize_errors_total")
            sys.stderr.write(
                f"dump-race-events-snapshot: bad payload on {msg.subject}: {exc}\n"
            )

    try:
        sub = await client.subscribe(subject, cb=_handle)
        await asyncio.sleep(max(0.1, seconds))
        await sub.unsubscribe()
    except Exception as exc:  # noqa: BLE001
        _bump("race_events_publish_errors_total")
        sys.stderr.write(
            f"dump-race-events-snapshot: subscribe failed ({type(exc).__name__}): {exc}\n"
        )
    finally:
        try:
            await asyncio.wait_for(client.drain(), timeout=2.0)
        except Exception:  # noqa: BLE001 — drain best-effort
            pass

    return collected


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=float, default=30.0,
        help="Subscribe to race.event.> for this many seconds (default 30).",
    )
    parser.add_argument(
        "--out", default=None,
        help="Snapshot path (default: dashboard_exports/race_events_snapshot.json "
             "or RACE_EVENTS_SNAPSHOT_JSON env).",
    )
    parser.add_argument(
        "--subject", default="race.event.>",
        help="NATS subject pattern (default race.event.>).",
    )
    parser.add_argument(
        "--no-merge", action="store_true",
        help="Replace snapshot rather than merging into existing.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
    )
    args = parser.parse_args(argv)

    out_path = _resolve_snapshot_path(args.out)

    payloads = asyncio.run(_subscribe_for(args.seconds, args.subject))

    if args.no_merge:
        snapshot: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "races": [],
        }
    else:
        snapshot = _load_snapshot(out_path)

    for p in payloads:
        # publisher payload is the entry dict directly + a `_publisher_node`
        # bookkeeping field. Strip the bookkeeping before persisting.
        entry = {k: v for k, v in p.items() if k != "_publisher_node"}
        if not entry.get("race_id"):
            _bump("race_event_serialize_errors_total")
            continue
        snapshot = _merge_snapshot(snapshot, entry)

    snapshot["counters"] = counters_snapshot()
    snapshot["generated_at"] = _now_iso()

    try:
        _atomic_write(out_path, snapshot)
    except OSError as exc:
        _bump("race_event_serialize_errors_total")
        sys.stderr.write(f"dump-race-events-snapshot: write failed: {exc}\n")
        return 3

    if not args.quiet:
        print(json.dumps({
            "snapshot_path": str(out_path),
            "races_in_snapshot": len(snapshot.get("races") or []),
            "events_received": len(payloads),
            "counters": snapshot["counters"],
        }, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())
