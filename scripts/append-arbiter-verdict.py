#!/usr/bin/env python3
"""Append an arbiter verdict via IDEHumanArbiter.record_verdict.

BB2 wave (2026-05-07).

Thin CLI wrapper invoked by ``app/api/arbiter/verdict/route.ts`` to
record Aaron's verdict in the EvidenceLedger. Stays subprocess-only so
the Next.js side stays stdlib + node:fs only — no Python embedding.

Input contract
--------------
JSON via stdin::

    {
      "case_id":  "<arb-...>",
      "verdict":  "APPROVE|OVERTURN|REMAND|DISMISS|DEFER",
      "reason":   "<aaron's reason>",
      "case_evidence_record_id":     "<sha256>?",
      "tribunal_evidence_record_id": "<sha256>?",
      "source":    "tribunal|race|evidence|manual",
      "source_id": "<artifact id>"
    }

Output contract
---------------
JSON on stdout::

    {
      "ok":                true,
      "evidence_record_id": "<sha256>",
      "verdict":            "<APPROVE|...>",
      "case_id":            "<arb-...>",
      "decided_at":         "<iso>",
      "parent_record_id":   "<sha256>?"
    }

On failure::

    {
      "ok":    false,
      "error": "<short message>"
    }

Exit codes:
  0 — verdict recorded
  2 — bad input shape (validation)
  3 — recording failed (ledger / runtime error)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
for p in (str(_REPO_ROOT / "multi-fleet"), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _fail(message: str, code: int) -> int:
    _emit({"ok": False, "error": message})
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="-",
        help="Path to JSON input (default: '-' = stdin).",
    )
    args = parser.parse_args(argv)

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = pathlib.Path(args.input).read_text()
        except OSError as exc:
            return _fail(f"input read failure: {exc}", 2)

    if not raw or not raw.strip():
        return _fail("empty input", 2)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON: {exc}", 2)
    if not isinstance(payload, dict):
        return _fail("input must be a JSON object", 2)

    case_id = payload.get("case_id")
    verdict = payload.get("verdict")
    reason = payload.get("reason") or ""
    if not isinstance(case_id, str) or not case_id.strip():
        return _fail("case_id required (non-empty string)", 2)
    if not isinstance(verdict, str) or not verdict.strip():
        return _fail("verdict required (non-empty string)", 2)
    if not isinstance(reason, str):
        return _fail("reason must be a string", 2)
    if not reason.strip():
        # Default reason when caller omits one — Aaron's button-only path.
        reason = f"Aaron verdict: {verdict.upper()}"

    try:
        from memory.evidence_ledger import EvidenceLedger
        from multifleet.ide_human_arbiter import IDEHumanArbiter
    except ImportError as exc:
        return _fail(f"import failure: {exc}", 3)

    ledger = EvidenceLedger()
    arbiter = IDEHumanArbiter(evidence_ledger=ledger)

    try:
        record = arbiter.record_verdict(
            case_id=case_id,
            verdict=verdict,
            reason=reason,
            case_evidence_record_id=payload.get("case_evidence_record_id"),
            source=payload.get("source"),
            source_id=payload.get("source_id"),
            tribunal_evidence_record_id=payload.get(
                "tribunal_evidence_record_id"
            ),
        )
    except (ValueError, TypeError) as exc:
        return _fail(f"validation: {exc}", 2)
    except Exception as exc:  # noqa: BLE001 — surface every error
        return _fail(f"record_verdict failed: {exc}", 3)

    _emit({
        "ok": True,
        "evidence_record_id": record.evidence_record_id,
        "verdict": record.verdict,
        "case_id": record.case_id,
        "decided_at": record.decided_at,
        "parent_record_id": record.parent_record_id,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
