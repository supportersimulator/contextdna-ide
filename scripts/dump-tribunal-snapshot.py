#!/usr/bin/env python3
"""Dump a JSON snapshot of Validation Tribunal cases for the IDE.

Z3 scaffold (2026-05-07).

Mirrors the pattern of ``scripts/dump-race-events-snapshot.py`` and
``scripts/dump-evidence-ledger-summary.py`` — the Next.js
``/api/tribunal/cases`` route reads this snapshot and renders the
CampaignTheater tribunal strip.

For the v0 scaffold, tribunal cases live in-memory inside the Python module
(no durable store yet — write side ships next). The dumper supports two
input modes:

  1. ``--from-file <path>`` — read a JSON list of case+verdict entries the
     caller (test harness, the brainstorm escalate hook, the next-wave
     write side) has already produced. Useful for stitching outputs from
     ``scripts/3s-answer-brainstorm.py`` opened cases into the IDE view.
  2. No input — produce an empty (`{cases: []}`) snapshot so the IDE
     renders a graceful empty state CTA. ZSF: not an error.

Output path: ``dashboard_exports/tribunal_cases_snapshot.json``
(override with ``--out`` or ``TRIBUNAL_CASES_SNAPSHOT_JSON`` env var).

Snapshot shape::

  {
    "schema_version": "tribunal_cases_snapshot/v1",
    "generated_at": "2026-05-07T12:34:11Z",
    "cases": [                       # newest-first by opened_at
      {
        "case": {                    # TribunalCase.to_dict()
          "case_id": "...",
          "race_id_or_evidence_id": "...",
          "dispute_reason": "...",
          "panelists": [...],
          "opened_at": "...",
          "status": "open" | "decided" | "archived"
        },
        "verdict": {                 # optional — present when decided
          "case_id": "...",
          "verdict": "APPROVE|OVERTURN|REMAND|DISMISS|UNRESOLVED",
          "majority_opinion": "...",
          "dissent_opinions": [...],
          "decided_at": "...",
          "panelist_opinions": {...},
          "evidence_record_id": "..."
        }
      }
    ],
    "counters": { ... }              # multifleet.validation_tribunal counters
  }

ZSF: every error path bumps a tribunal counter (or surfaces to stderr) AND
exits non-zero if the write fails. The IDE route degrades gracefully when
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
if str(_REPO_ROOT / "multi-fleet") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "multi-fleet"))

SCHEMA_VERSION = "tribunal_cases_snapshot/v1"
DEFAULT_SNAPSHOT = (
    _REPO_ROOT / "dashboard_exports" / "tribunal_cases_snapshot.json"
)


def _resolve_snapshot_path(out: str | None) -> pathlib.Path:
    if out:
        return pathlib.Path(out).resolve()
    env = os.environ.get("TRIBUNAL_CASES_SNAPSHOT_JSON")
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
    """Read a JSON list of {case, verdict?} entries.

    Accepts either a top-level list OR a dict with a `cases` key (the
    snapshot shape itself, for re-publication / testing).
    """
    raw = p.read_text()
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("cases"), list):
        return parsed["cases"]
    raise ValueError(
        f"input file {p} must be a list or {{cases: [...]}} dict; got {type(parsed).__name__}"
    )


def _validate_entry(entry: dict[str, Any], idx: int) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"cases[{idx}] is not a dict")
    case = entry.get("case")
    if not isinstance(case, dict):
        raise ValueError(f"cases[{idx}].case missing or not a dict")
    for required in ("case_id", "race_id_or_evidence_id", "opened_at", "status"):
        if not case.get(required):
            raise ValueError(f"cases[{idx}].case.{required} missing")
    verdict = entry.get("verdict")
    if verdict is not None and not isinstance(verdict, dict):
        raise ValueError(f"cases[{idx}].verdict present but not a dict")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-file",
        default=None,
        help="Read a JSON file (list or {cases: [...]}) of tribunal entries.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Snapshot path (default: dashboard_exports/tribunal_cases_snapshot.json "
             "or TRIBUNAL_CASES_SNAPSHOT_JSON env).",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_path = _resolve_snapshot_path(args.out)

    cases: list[dict[str, Any]] = []
    if args.from_file:
        in_path = pathlib.Path(args.from_file).resolve()
        if not in_path.is_file():
            sys.stderr.write(
                f"dump-tribunal-snapshot: input file not found: {in_path}\n"
            )
            return 2
        try:
            cases = _load_input(in_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            sys.stderr.write(
                f"dump-tribunal-snapshot: failed to read {in_path}: {exc}\n"
            )
            return 2
        for i, entry in enumerate(cases):
            try:
                _validate_entry(entry, i)
            except ValueError as exc:
                sys.stderr.write(
                    f"dump-tribunal-snapshot: invalid entry: {exc}\n"
                )
                return 2

    # Sort newest-first by opened_at so the IDE strip leads with fresh cases.
    cases.sort(
        key=lambda e: (e.get("case") or {}).get("opened_at") or "",
        reverse=True,
    )

    # Snapshot the live counters from the tribunal module.
    try:
        from multifleet.validation_tribunal import counters_snapshot  # type: ignore
        counters = counters_snapshot()
    except ImportError as exc:
        # Module not importable from this checkout — surface, then continue
        # with empty counters so the IDE strip still renders cases.
        sys.stderr.write(
            f"dump-tribunal-snapshot: counters unavailable ({exc}); continuing\n"
        )
        counters = {}

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "cases": cases,
        "counters": counters,
    }

    try:
        _atomic_write(out_path, snapshot)
    except OSError as exc:
        sys.stderr.write(f"dump-tribunal-snapshot: write failed: {exc}\n")
        return 3

    if not args.quiet:
        print(json.dumps({
            "snapshot_path": str(out_path),
            "cases_in_snapshot": len(cases),
            "open_cases": sum(
                1 for e in cases
                if (e.get("case") or {}).get("status") == "open"
            ),
            "counters": counters,
        }, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())
