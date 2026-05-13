#!/usr/bin/env python3
"""W1.b — EvidenceLedger admin REDACT helper (post-hoc tombstone).

Phase-5 plan: ``.fleet/audits/2026-05-04-U4-phase3-plan.md`` (W1.redact).
Companion to ``scripts/append-evidence-ledger.py`` (V1, W1.a).

V1's W1.a redaction was *preventive* (write-time secret scrub). This helper
is the *post-hoc* surgical removal of an existing record's payload while
preserving its sha256 (and therefore the cryptographic chain).

Behaviour mirrors :meth:`memory.evidence_ledger.EvidenceLedger.redact_record`.
On success a NEW ``kind="redaction"`` record (the tombstone) is written and
the target's ``content_json`` is replaced with the literal marker string.
The target's ``record_id`` (= sha256 of the original canonical content) is
*never* mutated — chain integrity preserved.

CLI
---

    python3 scripts/redact-evidence-ledger.py \
        --record-id <sha256> \
        --reason "manual" \
        --actor "atlas-ui" \
        [--marker "[REDACTED]"]

Output (stdout, single JSON line on success):

    {"ok": true,
     "tombstone_record_id": "<sha256>",
     "redacted_target":     "<sha256>",
     "redacted_at":         "<iso>",
     "already_redacted":    false}

Failures emit a JSON line on stdout AND exit non-zero:

    {"ok": false, "error_kind": "validation_error", "message": "..."}

Exit codes:
    0  success
    2  validation_error  (missing/empty field, invalid marker)
    3  target_not_found  (record_id supplied, no row in ledger)
    4  exec_error        (sqlite write failure, unexpected exception)

ZSF (Zero Silent Failures)
--------------------------
Persistent counters file: ``memory/.evidence_ledger_redact_counters.json``

    {
      "ledger_redact_ok_total":             int,
      "ledger_redact_errors_total":         int,
      "ledger_redact_target_missing_total": int,
      "ledger_redact_validation_errors_total": int,
      "ledger_redact_exec_errors_total":    int,
    }

Counters survive process restart so the Next.js wrapper + cardio sentinel
can detect "redacts silently broken" without scraping stderr. Every error
path bumps a counter AND prints to stderr. No bare ``except: pass``.

Forward-compat
--------------
Python 3.14 ready (no walrus-in-comprehension, no PEP 695 generics).
Helper is invoked by Next.js via ``child_process.spawn`` so the admin
submodule never imports ``memory.evidence_ledger`` directly.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import traceback
from typing import Any

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent  # scripts/.. -> superrepo

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _import_ledger():
    from memory import evidence_ledger as el  # type: ignore
    return el


COUNTERS_PATH = pathlib.Path(
    os.environ.get(
        "EVIDENCE_LEDGER_REDACT_COUNTERS",
        str(_REPO_ROOT / "memory" / ".evidence_ledger_redact_counters.json"),
    )
)

COUNTER_KEYS = (
    "ledger_redact_ok_total",
    "ledger_redact_errors_total",
    "ledger_redact_target_missing_total",
    "ledger_redact_validation_errors_total",
    "ledger_redact_exec_errors_total",
)


def _load_counters() -> dict[str, int]:
    if not COUNTERS_PATH.is_file():
        return {k: 0 for k in COUNTER_KEYS}
    try:
        with COUNTERS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {k: 0 for k in COUNTER_KEYS}
        return {k: int(data.get(k, 0)) for k in COUNTER_KEYS}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        sys.stderr.write(
            f"redact-evidence-ledger: counters read failed ({e}); resetting\n"
        )
        return {k: 0 for k in COUNTER_KEYS}


def _save_counters(c: dict[str, int]) -> None:
    try:
        COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = COUNTERS_PATH.with_suffix(COUNTERS_PATH.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(c, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp, COUNTERS_PATH)
    except OSError as e:
        # Counter persistence failure is itself a ZSF surface, but it MUST
        # NOT mask the actual operation result. Just surface to stderr.
        sys.stderr.write(f"redact-evidence-ledger: counters write failed: {e}\n")


def _bump(c: dict[str, int], key: str, n: int = 1) -> None:
    c[key] = c.get(key, 0) + n


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _validate_text(name: str, value: str | None, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} required (non-empty string)")
    if len(value) > max_len:
        raise ValidationError(
            f"{name} too long (got {len(value)} chars, max {max_len})"
        )
    return value.strip()


def _validate_record_id(value: str | None) -> str:
    rid = _validate_text("record-id", value, max_len=128)
    # Defensive — record_ids are sha256 hex (64 chars). We accept anything
    # non-empty so the helper still works against a future schema without
    # forcing length here, but we do strip whitespace.
    return rid


# --------------------------------------------------------------------------
# Core redact
# --------------------------------------------------------------------------

def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _emit_error(
    counters: dict[str, int], error_kind: str, message: str, exit_code: int
) -> int:
    _bump(counters, "ledger_redact_errors_total")
    if error_kind == "validation_error":
        _bump(counters, "ledger_redact_validation_errors_total")
    elif error_kind == "target_not_found":
        _bump(counters, "ledger_redact_target_missing_total")
    elif error_kind == "exec_error":
        _bump(counters, "ledger_redact_exec_errors_total")
    _save_counters(counters)
    sys.stderr.write(f"redact-evidence-ledger: {error_kind}: {message}\n")
    _emit({"ok": False, "error_kind": error_kind, "message": message})
    return exit_code


def redact_evidence(
    *,
    record_id: str,
    reason: str,
    actor: str,
    marker: str,
    counters: dict[str, int],
) -> tuple[dict[str, Any], int]:
    """Validate inputs and call EvidenceLedger.redact_record. Returns
    (response_dict, exit_code).
    """
    try:
        rid = _validate_record_id(record_id)
        reason_norm = _validate_text("reason", reason, max_len=1000)
        actor_norm = _validate_text("actor", actor, max_len=256)
        marker_norm = _validate_text("marker", marker, max_len=256)
    except ValidationError as e:
        return (
            {"ok": False, "error_kind": "validation_error", "message": str(e)},
            2,
        )

    el = _import_ledger()
    ledger = el.EvidenceLedger()

    try:
        result = ledger.redact_record(
            record_id=rid,
            reason=reason_norm,
            actor=actor_norm,
            redacted_marker=marker_norm,
        )
    except el.EvidenceLedgerError as e:
        # Either target missing OR marker invalid — distinguish via message
        # (target_missing counter was already bumped in the ledger module
        # if the target was missing, and we bump it again here so the CLI
        # counters file mirrors the in-process module counters).
        msg = str(e)
        if "not in ledger" in msg:
            return (
                {
                    "ok": False,
                    "error_kind": "target_not_found",
                    "message": msg,
                },
                3,
            )
        return (
            {"ok": False, "error_kind": "validation_error", "message": msg},
            2,
        )
    except (TypeError, ValueError) as e:
        return (
            {
                "ok": False,
                "error_kind": "validation_error",
                "message": f"ledger rejected redact: {e}",
            },
            2,
        )
    except Exception as e:  # sqlite + anything unexpected
        return (
            {
                "ok": False,
                "error_kind": "exec_error",
                "message": f"redact failed: {type(e).__name__}: {e}",
            },
            4,
        )

    _bump(counters, "ledger_redact_ok_total")

    return (
        {
            "ok": True,
            "tombstone_record_id": result["tombstone_record_id"],
            "redacted_target": result["redacted_target"],
            "redacted_at": result["redacted_at"],
            "already_redacted": bool(result.get("already_redacted", False)),
        },
        0,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--record-id",
        required=True,
        help="sha256 record_id of the target row to redact",
    )
    p.add_argument(
        "--reason",
        required=True,
        help="Why the redaction is happening (non-empty, ≤1000 chars). Stored in tombstone.",
    )
    p.add_argument(
        "--actor",
        required=True,
        help="Who is performing the redact (non-empty, ≤256 chars). Stored in tombstone.",
    )
    p.add_argument(
        "--marker",
        default="[REDACTED]",
        help="Literal string to overwrite the target payload (default: [REDACTED])",
    )
    p.add_argument(
        "--print-counters",
        action="store_true",
        help="Print persisted counters to stderr before/after for debug.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    counters = _load_counters()

    if args.print_counters:
        sys.stderr.write(
            f"counters[before]={json.dumps(counters, sort_keys=True)}\n"
        )

    try:
        response, exit_code = redact_evidence(
            record_id=args.record_id,
            reason=args.reason,
            actor=args.actor,
            marker=args.marker,
            counters=counters,
        )
    except Exception as e:  # final ZSF guard — never let the helper crash silent
        tb = traceback.format_exc(limit=4)
        rc = _emit_error(
            counters,
            "exec_error",
            f"unhandled {type(e).__name__}: {e}\n{tb}",
            4,
        )
        if args.print_counters:
            sys.stderr.write(
                f"counters[after]={json.dumps(counters, sort_keys=True)}\n"
            )
        return rc

    if not response.get("ok"):
        rc = _emit_error(
            counters,
            response.get("error_kind", "exec_error"),
            response.get("message", "unknown failure"),
            exit_code,
        )
        if args.print_counters:
            sys.stderr.write(
                f"counters[after]={json.dumps(counters, sort_keys=True)}\n"
            )
        return rc

    _save_counters(counters)
    _emit(response)
    if args.print_counters:
        sys.stderr.write(
            f"counters[after]={json.dumps(counters, sort_keys=True)}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
