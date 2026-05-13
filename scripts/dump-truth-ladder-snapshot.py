#!/usr/bin/env python3
"""Dump the latest IDE Truth Ladder snapshot as JSON for the IDE.

BB2 wave (2026-05-07).

Mirrors ``scripts/dump-permission-snapshot.py`` and
``scripts/dump-tribunal-snapshot.py`` — the Next.js
``/api/truth-ladder/snapshot`` route reads this JSON and renders the
IDE TruthLadder.tsx panel.

Behaviour
---------
1. Compute a fresh snapshot from the EvidenceLedger using
   ``multifleet.ide_truth_ladder.TruthLadder.compute(...)``.
2. Persist the snapshot to ``memory/ide_truth_ladder.db`` (idempotent).
3. Write the snapshot JSON to ``dashboard_exports/truth_ladder_snapshot.json``
   (override with ``--out`` or the ``TRUTH_LADDER_SNAPSHOT_JSON`` env var).

Flags
-----
``--no-compute``    Skip recomputation; emit the latest persisted snapshot.
                    Useful when the IDE just needs a re-publish of the
                    last good payload (and the producer is offline).
``--db <path>``     Override the snapshot DB path.
``--lookback-s N``  Lookback window in seconds (default 86400 = 24h).
``--quiet``         Suppress the summary line on stdout.

Output snapshot shape::

    {
      "schema_version": "ide_truth_ladder/v1",
      "generated_at":   "2026-05-07T...",
      "rungs":          [TruthRung, ...],
      "hash":           "<sha256>"
    }

ZSF: every error path bumps a counter visible via
``multifleet.ide_truth_ladder.counters_snapshot()`` (logged to stderr on
exit).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

# Make the multifleet package + memory module importable regardless of cwd.
_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
for p in (str(_REPO_ROOT / "multi-fleet"), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


_DEFAULT_OUT = _REPO_ROOT / "dashboard_exports" / "truth_ladder_snapshot.json"


def _resolve_out(out: str | None) -> pathlib.Path:
    if out:
        return pathlib.Path(out).resolve()
    env = os.environ.get("TRUTH_LADDER_SNAPSHOT_JSON")
    if env:
        return pathlib.Path(env).resolve()
    return _DEFAULT_OUT


def _atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("IDE_TRUTH_LADDER_DB"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--lookback-s", type=int, default=86400)
    parser.add_argument(
        "--no-compute",
        action="store_true",
        help="Skip recomputation; emit latest persisted snapshot.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_path = _resolve_out(args.out)

    try:
        from multifleet.ide_truth_ladder import TruthLadder, counters_snapshot
    except ImportError as exc:
        sys.stderr.write(
            f"dump-truth-ladder-snapshot: import failed: {exc}\n"
        )
        # Graceful empty payload so the route never 500s on import.
        _atomic_write(out_path, {
            "schema_version": "ide_truth_ladder/v1",
            "generated_at": None,
            "rungs": [],
            "source": "import-error",
            "error": str(exc)[:200],
        })
        return 0

    ladder = TruthLadder(db_path=args.db) if args.db else TruthLadder()

    snapshot = None
    if not args.no_compute:
        try:
            from memory.evidence_ledger import EvidenceLedger
        except ImportError as exc:
            sys.stderr.write(
                f"dump-truth-ladder-snapshot: EvidenceLedger import failed: {exc}\n"
            )
            EvidenceLedger = None  # type: ignore

        if EvidenceLedger is not None:
            try:
                ledger = EvidenceLedger()
                snapshot = ladder.compute(
                    evidence_ledger=ledger,
                    lookback_window_s=args.lookback_s,
                )
                ladder.write_snapshot(snapshot)
            except Exception as exc:  # noqa: BLE001 — surface every failure
                sys.stderr.write(
                    f"dump-truth-ladder-snapshot: compute/write failed: {exc}\n"
                )
                snapshot = None

    if snapshot is None:
        # Fall back to whatever the DB has.
        try:
            snapshot = ladder.latest_snapshot()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"dump-truth-ladder-snapshot: latest_snapshot failed: {exc}\n"
            )
            sys.stderr.write(f"counters: {counters_snapshot()!r}\n")
            _atomic_write(out_path, {
                "schema_version": "ide_truth_ladder/v1",
                "generated_at": None,
                "rungs": [],
                "source": "error",
                "error": str(exc)[:200],
            })
            return 0

    if snapshot is None:
        payload: dict[str, Any] = {
            "schema_version": "ide_truth_ladder/v1",
            "generated_at": None,
            "rungs": [],
            "source": "no-snapshot",
        }
    else:
        payload = TruthLadder.serialize(snapshot)
        payload["source"] = "snapshot"

    try:
        _atomic_write(out_path, payload)
    except OSError as exc:
        sys.stderr.write(
            f"dump-truth-ladder-snapshot: write failed: {exc}\n"
        )
        return 3

    if not args.quiet:
        rungs = payload.get("rungs") or []
        per_rung = {r.get("label"): r.get("item_count", 0) for r in rungs}
        print(json.dumps({
            "snapshot_path": str(out_path),
            "rungs": per_rung,
            "counters": counters_snapshot(),
        }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
