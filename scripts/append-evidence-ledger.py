#!/usr/bin/env python3
"""W1.a — EvidenceLedger admin WRITE helper.

Phase-3 plan: ``.fleet/audits/2026-05-04-U4-phase3-plan.md`` (W1).
Read-side scaffolding: ``scripts/dump-evidence-ledger-summary.py`` +
``app/api/competition/status/route.ts`` (T1).

This helper is the canonical write path the Next.js admin route shells
out to. It lifts the redaction concern into Python (so the same logic
runs from CLI, IDE, or future cron-triggered evidence appenders) and
keeps the Next.js process stdlib-only.

CLI
---

    python3 scripts/append-evidence-ledger.py \
        --event-type audit \
        --subject "submission gate ACCEPT for synthetic-001" \
        --actor "operator:aaron" \
        --payload-json '{"score": 0.92, "secret_token": "tok_abc"}' \
        [--parent-record-id <sha256> ...]

Output (stdout, single JSON line on success):

    {"ok": true, "record_id": "<sha256>", "sha256": "<sha256>",
     "kind": "audit", "redacted_count": 1,
     "audit_line": "<iso> kind=audit subject=... record_id=<short>"}

Failures emit a JSON line on stdout AND exit non-zero:

    {"ok": false, "error_kind": "validation_error", "message": "..."}

Exit codes:
    0  success
    2  validation_error  (missing field, invalid JSON, unknown event_type)
    3  parent_not_found  (parent_record_id supplied, no row in ledger)
    4  exec_error        (sqlite write failure, unexpected exception)

ZSF (Zero Silent Failures)
--------------------------
Persistent counters file: ``memory/.evidence_ledger_append_counters.json``

    {
      "ledger_append_ok_total":          int,
      "ledger_append_errors_total":      int,
      "ledger_append_redactions_total":  int,
      "ledger_append_validation_errors_total": int,
      "ledger_append_parent_not_found_total":  int,
      "ledger_append_exec_errors_total": int,
    }

Counters survive process restart so the Next.js wrapper + cardio sentinel
can detect "writes silently broken" without scraping stderr. Every error
path bumps a counter AND prints to stderr. No bare ``except: pass``.

Redaction
---------
Default patterns (regex, case-insensitive):

  * api[_-]?key   ->  recursively masked at any nesting depth
  * password
  * passwd
  * secret
  * token
  * email-like values (``\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b``)

Override with env ``EVIDENCE_REDACTION_PATTERNS`` (comma-separated regex).
Each redaction increments ``ledger_append_redactions_total`` once per
distinct path under the payload tree. The original value is replaced
with the literal string ``"<redacted>"``; a sibling ``__redacted_keys``
list is appended to each dict that had a redaction so downstream tools
can audit redaction lineage without seeing the original value.

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
import re
import sys
import traceback
from typing import Any

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent  # scripts/.. -> superrepo

# Make ``memory`` importable from any cwd (mirrors test_evidence_ledger.py).
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Lazy-import the ledger so --help works even if memory/ is broken.
def _import_ledger():
    from memory import evidence_ledger as el  # type: ignore
    return el


COUNTERS_PATH = pathlib.Path(
    os.environ.get(
        "EVIDENCE_LEDGER_APPEND_COUNTERS",
        str(_REPO_ROOT / "memory" / ".evidence_ledger_append_counters.json"),
    )
)

COUNTER_KEYS = (
    "ledger_append_ok_total",
    "ledger_append_errors_total",
    "ledger_append_redactions_total",
    "ledger_append_validation_errors_total",
    "ledger_append_parent_not_found_total",
    "ledger_append_exec_errors_total",
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
            f"append-evidence-ledger: counters read failed ({e}); resetting\n"
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
        # Counter persistence failure is itself a ZSF violation surface —
        # we surface to stderr but DO NOT mask the underlying success/fail
        # of the actual ledger write that the caller cares about.
        sys.stderr.write(f"append-evidence-ledger: counters write failed: {e}\n")


def _bump(c: dict[str, int], key: str, n: int = 1) -> None:
    c[key] = c.get(key, 0) + n


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

# Map admin-friendly event_type -> EvidenceKind. The set is intentionally
# narrow so the IDE cannot inject arbitrary kinds. Extend deliberately.
EVENT_TYPE_TO_KIND = {
    "experiment": "experiment",
    "competition": "competition",
    "trial": "trial",
    "decision": "decision",
    "audit": "audit",
    "outcome": "outcome",
}


class ValidationError(Exception):
    pass


def _validate_event_type(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("event_type required")
    v = value.strip().lower()
    if v not in EVENT_TYPE_TO_KIND:
        raise ValidationError(
            f"unknown event_type {value!r}; "
            f"valid: {sorted(EVENT_TYPE_TO_KIND.keys())}"
        )
    return v


def _validate_text(name: str, value: str | None, max_len: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} required (non-empty string)")
    if len(value) > max_len:
        raise ValidationError(
            f"{name} too long (got {len(value)} chars, max {max_len})"
        )
    return value.strip()


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        raise ValidationError("payload must be a JSON string")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValidationError(f"payload-json invalid: {e}") from e
    if not isinstance(obj, dict):
        raise ValidationError(
            f"payload must decode to a JSON object, got {type(obj).__name__}"
        )
    return obj


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

DEFAULT_REDACTION_PATTERNS = (
    r"api[_-]?key",
    r"passwords?",
    r"passwd",
    r"secret",
    r"token",
)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _compile_patterns() -> list[re.Pattern[str]]:
    raw_env = os.environ.get("EVIDENCE_REDACTION_PATTERNS", "").strip()
    if raw_env:
        items = [p.strip() for p in raw_env.split(",") if p.strip()]
    else:
        items = list(DEFAULT_REDACTION_PATTERNS)
    out: list[re.Pattern[str]] = []
    for it in items:
        try:
            out.append(re.compile(it, re.IGNORECASE))
        except re.error as e:
            sys.stderr.write(
                f"append-evidence-ledger: redaction pattern {it!r} invalid: {e}\n"
            )
    return out


def _redact_value(value: Any) -> tuple[Any, int]:
    """Redact email-like substrings inside string values.

    Dict / list traversal happens in ``_redact_payload``; this is the
    leaf-level masker.
    """
    if isinstance(value, str):
        masked, n = EMAIL_RE.subn("<redacted-email>", value)
        return masked, n
    return value, 0


def _redact_payload(
    payload: Any, patterns: list[re.Pattern[str]]
) -> tuple[Any, int]:
    """Recursively redact a JSON-shaped tree.

    Returns (new_tree, redactions_applied). Redactions counted:

      * dict KEY matches a pattern -> value replaced with ``"<redacted>"``
      * leaf string values containing email-like substrings (anywhere in
        the tree) -> substring replaced with ``"<redacted-email>"``
        (each match counts as 1 redaction)

    Lists / tuples preserved as lists. Non-JSON-native values stringified.
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        redacted_keys: list[str] = []
        n = 0
        for k, v in payload.items():
            key_str = str(k)
            if any(p.search(key_str) for p in patterns):
                out[key_str] = "<redacted>"
                redacted_keys.append(key_str)
                n += 1
                continue
            new_v, sub_n = _redact_payload(v, patterns)
            out[key_str] = new_v
            n += sub_n
        if redacted_keys:
            # Append a non-secret audit field so downstream tools can see
            # which keys were redacted without seeing the values.
            out.setdefault("__redacted_keys", sorted(redacted_keys))
        return out, n
    if isinstance(payload, list):
        new_list: list[Any] = []
        n = 0
        for item in payload:
            new_item, sub_n = _redact_payload(item, patterns)
            new_list.append(new_item)
            n += sub_n
        return new_list, n
    if isinstance(payload, tuple):
        new_list2 = []
        n2 = 0
        for item in payload:
            new_item, sub_n = _redact_payload(item, patterns)
            new_list2.append(new_item)
            n2 += sub_n
        return new_list2, n2
    return _redact_value(payload)


# --------------------------------------------------------------------------
# Core append
# --------------------------------------------------------------------------

def _emit(payload: dict[str, Any]) -> None:
    """Single JSON line on stdout — keeps the Next.js wrapper trivial to parse."""
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _emit_error(
    counters: dict[str, int], error_kind: str, message: str, exit_code: int
) -> int:
    _bump(counters, "ledger_append_errors_total")
    if error_kind == "validation_error":
        _bump(counters, "ledger_append_validation_errors_total")
    elif error_kind == "parent_not_found":
        _bump(counters, "ledger_append_parent_not_found_total")
    elif error_kind == "exec_error":
        _bump(counters, "ledger_append_exec_errors_total")
    _save_counters(counters)
    sys.stderr.write(
        f"append-evidence-ledger: {error_kind}: {message}\n"
    )
    _emit({"ok": False, "error_kind": error_kind, "message": message})
    return exit_code


def append_evidence(
    *,
    event_type: str,
    subject: str,
    actor: str,
    payload: dict[str, Any],
    parent_record_id: list[str] | None,
    counters: dict[str, int],
) -> tuple[dict[str, Any], int]:
    """Validate, redact, and append. Returns (response_dict, exit_code).

    Caller is responsible for persisting counters and emitting the response.
    """
    # 1. Validate inputs (pure — counters bumped on failure by caller).
    try:
        kind_str = _validate_event_type(event_type)
        subject_norm = _validate_text("subject", subject, max_len=512)
        actor_norm = _validate_text("actor", actor, max_len=256)
    except ValidationError as e:
        return (
            {"ok": False, "error_kind": "validation_error", "message": str(e)},
            2,
        )

    if not isinstance(payload, dict):
        return (
            {
                "ok": False,
                "error_kind": "validation_error",
                "message": "payload must be a dict",
            },
            2,
        )

    # 2. Redact.
    patterns = _compile_patterns()
    redacted_payload, redaction_count = _redact_payload(payload, patterns)
    if redaction_count > 0:
        _bump(counters, "ledger_append_redactions_total", redaction_count)

    # 3. Build content dict for the underlying ledger.
    content = {
        "event_type": kind_str,
        "subject": subject_norm,
        "actor": actor_norm,
        "payload": redacted_payload,
    }

    # 4. Verify parent_record_ids exist (if provided).
    el = _import_ledger()
    ledger = el.EvidenceLedger()
    parents_clean: list[str] = []
    if parent_record_id:
        for pid in parent_record_id:
            pid_norm = pid.strip() if isinstance(pid, str) else ""
            if not pid_norm:
                continue
            try:
                row = ledger.get(pid_norm)
            except Exception as e:  # ledger surfaces sqlite errors here
                return (
                    {
                        "ok": False,
                        "error_kind": "exec_error",
                        "message": f"parent lookup failed: {e}",
                    },
                    4,
                )
            if row is None:
                return (
                    {
                        "ok": False,
                        "error_kind": "parent_not_found",
                        "message": f"parent_record_id {pid_norm!r} not in ledger",
                    },
                    3,
                )
            parents_clean.append(pid_norm)

    # 5. Write through the canonical ledger API.
    try:
        record = ledger.record(
            content=content,
            kind=kind_str,
            parent_ids=parents_clean or None,
        )
    except (TypeError, ValueError) as e:
        return (
            {
                "ok": False,
                "error_kind": "validation_error",
                "message": f"ledger rejected content: {e}",
            },
            2,
        )
    except Exception as e:  # sqlite + anything unexpected
        return (
            {
                "ok": False,
                "error_kind": "exec_error",
                "message": f"ledger write failed: {type(e).__name__}: {e}",
            },
            4,
        )

    audit_line = (
        f"{record.created_at} kind={record.kind} "
        f"subject={subject_norm!r} record_id={record.record_id[:16]}"
    )

    _bump(counters, "ledger_append_ok_total")

    return (
        {
            "ok": True,
            "record_id": record.record_id,
            "sha256": record.record_id,  # alias — record_id is sha256 per S2 design
            "kind": record.kind,
            "redacted_count": int(redaction_count),
            "parent_count": len(parents_clean),
            "audit_line": audit_line,
            "created_at": record.created_at,
        },
        0,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--event-type", required=True, help="One of: experiment, competition, trial, decision, audit, outcome")
    p.add_argument("--subject", required=True, help="Short human-readable subject (≤512 chars)")
    p.add_argument("--actor", required=True, help="Who/what is recording (≤256 chars)")
    p.add_argument(
        "--payload-json",
        default="{}",
        help="JSON object (string). Subject to redaction.",
    )
    p.add_argument(
        "--parent-record-id",
        action="append",
        default=None,
        help="Optional parent record_id (sha256). May repeat. Each must exist.",
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
        sys.stderr.write(f"counters[before]={json.dumps(counters, sort_keys=True)}\n")

    # Parse payload (validation_error if malformed) — separate from main
    # validation block so the counter bump is correct.
    try:
        payload = _parse_payload(args.payload_json)
    except ValidationError as e:
        rc = _emit_error(counters, "validation_error", str(e), 2)
        if args.print_counters:
            sys.stderr.write(f"counters[after]={json.dumps(counters, sort_keys=True)}\n")
        return rc

    try:
        response, exit_code = append_evidence(
            event_type=args.event_type,
            subject=args.subject,
            actor=args.actor,
            payload=payload,
            parent_record_id=args.parent_record_id,
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
            sys.stderr.write(f"counters[after]={json.dumps(counters, sort_keys=True)}\n")
        return rc

    if not response.get("ok"):
        rc = _emit_error(
            counters,
            response.get("error_kind", "exec_error"),
            response.get("message", "unknown failure"),
            exit_code,
        )
        if args.print_counters:
            sys.stderr.write(f"counters[after]={json.dumps(counters, sort_keys=True)}\n")
        return rc

    _save_counters(counters)
    _emit(response)
    if args.print_counters:
        sys.stderr.write(f"counters[after]={json.dumps(counters, sort_keys=True)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
