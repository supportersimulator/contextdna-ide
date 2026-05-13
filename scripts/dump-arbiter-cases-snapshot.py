#!/usr/bin/env python3
"""Dump arbiter cases as JSON for the IDE HumanArbiter panel.

BB2 wave (2026-05-07).

Mirrors ``scripts/dump-tribunal-snapshot.py`` — the Next.js
``/api/arbiter/cases`` route reads the JSON this script writes and renders
the IDE HumanArbiter.tsx panel.

The IDEHumanArbiter does not maintain its own durable case list. The
durable record is the EvidenceLedger (audit / arbiter_case_opened +
audit / arbiter_verdict_recorded records). For the IDE snapshot bridge
we walk the ledger and project the open + decided cases into the wire
shape the route expects.

Behaviour
---------
1. Default mode — query EvidenceLedger for ``kind=audit`` records;
   filter to ``event_type in {arbiter_case_opened, arbiter_verdict_recorded}``;
   stitch case + verdict pairs by ``case_id``.
2. ``--from-file <path>`` — for tests / fleet seeders, read a JSON list
   of {case, verdict?} entries (the snapshot shape itself, mirror of
   dump-tribunal-snapshot).
3. Atomic write to ``dashboard_exports/arbiter_cases_snapshot.json``
   (override with ``--out`` or ``ARBITER_CASES_SNAPSHOT_JSON``).

Wire shape (matches lib/ide/human-arbiter-types.ts ArbiterCasesResponse)::

    {
      "schema_version": "arbiter_cases_snapshot/v1",
      "generated_at":   "2026-05-07T...",
      "cases":          [ArbiterCase, ...],
      "open_count":     N,
      "decided_count":  N,
      "counters":       { ... }
    }

ZSF: every error path bumps an arbiter counter (or surfaces to stderr) AND
exits non-zero on write failure. The IDE route degrades gracefully when
the snapshot is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
for p in (str(_REPO_ROOT / "multi-fleet"), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

SCHEMA_VERSION = "arbiter_cases_snapshot/v1"
DEFAULT_SNAPSHOT = (
    _REPO_ROOT / "dashboard_exports" / "arbiter_cases_snapshot.json"
)


def _resolve_out(out: str | None) -> pathlib.Path:
    if out:
        return pathlib.Path(out).resolve()
    env = os.environ.get("ARBITER_CASES_SNAPSHOT_JSON")
    if env:
        return pathlib.Path(env).resolve()
    return DEFAULT_SNAPSHOT


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def _load_input(p: pathlib.Path) -> list[dict[str, Any]]:
    raw = p.read_text()
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("cases"), list):
        return parsed["cases"]
    raise ValueError(
        f"input file {p} must be a list or {{cases: [...]}} dict; "
        f"got {type(parsed).__name__}"
    )


def _project_from_ledger(lookback_s: int) -> list[dict[str, Any]]:
    """Walk the ledger, project arbiter cases for the snapshot shape."""
    try:
        from memory.evidence_ledger import EvidenceLedger
    except ImportError as exc:
        sys.stderr.write(
            f"dump-arbiter-cases-snapshot: EvidenceLedger import failed: {exc}\n"
        )
        return []

    ledger = EvidenceLedger()
    # We want everything that mentions an arbiter event in the lookback window.
    # The Q3 ledger query() filter is by kind / since; cheapest path is a
    # "kind=audit" query, then filter on event_type in Python. Production
    # arbiter events are <100/day so this is fine for v0.
    anchor = datetime.now(timezone.utc).timestamp()
    since = datetime.fromtimestamp(
        anchor - max(int(lookback_s), 1), tz=timezone.utc,
    ).isoformat(timespec="seconds")

    try:
        records = ledger.query(kind="audit", since=since)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"dump-arbiter-cases-snapshot: ledger.query failed: {exc}\n"
        )
        return []

    cases_by_id: dict[str, dict[str, Any]] = {}
    verdicts_by_case: dict[str, dict[str, Any]] = {}

    for rec in records:
        content = getattr(rec, "content", None) or {}
        if not isinstance(content, dict):
            continue
        et = content.get("event_type")
        cid = content.get("case_id")
        if not isinstance(cid, str) or not cid:
            continue
        if et == "arbiter_case_opened":
            cases_by_id[cid] = {
                "case_id": cid,
                "opened_at": content.get("opened_at"),
                "source": content.get("source"),
                "source_id": content.get("source_id"),
                "dispute_summary": content.get("dispute_summary"),
                "status": "open",
                "aaron_verdict": None,
                "decided_at": None,
                "reason": None,
                "case_evidence_record_id": getattr(rec, "record_id", None),
            }
        elif et == "arbiter_verdict_recorded":
            verdicts_by_case[cid] = {
                "verdict": content.get("verdict"),
                "decided_at": content.get("decided_at"),
                "reason": content.get("reason"),
                "evidence_record_id": getattr(rec, "record_id", None),
            }

    out: list[dict[str, Any]] = []
    for cid, case in cases_by_id.items():
        v = verdicts_by_case.get(cid)
        if v is not None:
            case = dict(case)
            case["status"] = "decided"
            case["aaron_verdict"] = v.get("verdict")
            case["decided_at"] = v.get("decided_at")
            case["reason"] = v.get("reason")
            case["verdict_evidence_record_id"] = v.get("evidence_record_id")
        out.append(case)
    # Newest-first by opened_at.
    out.sort(key=lambda c: c.get("opened_at") or "", reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-file", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--lookback-s", type=int, default=86400 * 7)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_path = _resolve_out(args.out)

    cases: list[dict[str, Any]] = []
    if args.from_file:
        in_path = pathlib.Path(args.from_file).resolve()
        if not in_path.is_file():
            sys.stderr.write(
                f"dump-arbiter-cases-snapshot: input file not found: {in_path}\n"
            )
            return 2
        try:
            cases = _load_input(in_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            sys.stderr.write(
                f"dump-arbiter-cases-snapshot: failed to read {in_path}: {exc}\n"
            )
            return 2
    else:
        cases = _project_from_ledger(args.lookback_s)

    open_count = sum(1 for c in cases if c.get("status") == "open")
    decided_count = sum(1 for c in cases if c.get("status") != "open")

    counters: dict[str, int] = {}
    try:
        from multifleet.ide_human_arbiter import counters_snapshot
        counters = counters_snapshot()
    except ImportError as exc:
        sys.stderr.write(
            f"dump-arbiter-cases-snapshot: counters unavailable ({exc})\n"
        )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "cases": cases,
        "open_count": open_count,
        "decided_count": decided_count,
        "counters": counters,
    }
    try:
        _atomic_write(out_path, snapshot)
    except OSError as exc:
        sys.stderr.write(f"dump-arbiter-cases-snapshot: write failed: {exc}\n")
        return 3

    if not args.quiet:
        print(json.dumps({
            "snapshot_path": str(out_path),
            "open_count": open_count,
            "decided_count": decided_count,
            "counters": counters,
        }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
