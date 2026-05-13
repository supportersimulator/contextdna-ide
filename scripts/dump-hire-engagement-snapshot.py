#!/usr/bin/env python3
"""Dump a JSON snapshot of a Hire Panel engagement for the IDE.

EE1 Phase-12 scaffold (2026-05-07). Mirrors the pattern of:
  - scripts/dump-tribunal-snapshot.py (Z3)
  - scripts/dump-permission-snapshot.py (Z2)
  - scripts/dump-evidence-ledger-summary.py
  - scripts/dump-truth-ladder-snapshot.py (BB2)

The Next.js route at ``app/api/hire/[engagement_id]/route.ts`` reads this
snapshot and serves it (already redacted) to the client-facing
``app/hire/[engagement_id]/page.tsx`` page.

For the v0 scaffold an engagement can be supplied EITHER via:

  1. ``--from-file <path>`` — read a JSON dict matching
     ``HireEngagement.to_dict()`` (the production path will read the
     EvidenceLedger via ``HirePanel.current_engagement(...)`` once a real
     engagement is open).
  2. No input — produce an empty engagement so the IDE renders a graceful
     "no active engagement" CTA. ZSF: not an error.

Output path: ``dashboard_exports/hire_engagement_<id>_snapshot.json``
(override with ``--out`` or ``HIRE_ENGAGEMENT_SNAPSHOT_JSON`` env var).

Snapshot shape::

    {
      "schema_version": "hire_engagement_snapshot/v1",
      "generated_at": "2026-05-07T12:34:11Z",
      "engagement": { ...redact_for_client output... } | null,
      "counters": { ...multifleet.hire_panel counters... }
    }

ZSF: every failure path increments a ``hire_*`` counter and returns
non-zero on write failure. The IDE route degrades gracefully when the
snapshot is missing.
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

SCHEMA_VERSION = "hire_engagement_snapshot/v1"
DEFAULT_SNAPSHOT_DIR = _REPO_ROOT / "dashboard_exports"
TEMPLATE_PATH = DEFAULT_SNAPSHOT_DIR / "hire_engagement_snapshot.template.json"

# KK4 — fields that are stripped from the persisted snapshot's `engagement`
# block because the EE1 redaction allowlist does not include them. Kept
# alongside the engagement in the EvidenceLedger record for Aaron-side
# auditability, but never surfaced to the client surface.
_INTERNAL_ONLY_FIELDS: tuple[str, ...] = (
    "scope",
    "hours_committed",
    "rate_usd",
    "evidence_packets",
)

# KK4 — additive ledger record kind for engagement creation events.
# Mirrors the GG5 Mementos pattern: a free-form `event_type` string on the
# multifleet.evidence_ledger.EvidenceLedger (the fleet hash-chain ledger),
# NOT to be confused with memory.evidence_ledger.EvidenceKind enum.
ENGAGEMENT_CREATED_EVENT_TYPE = "engagement_created"


def _resolve_snapshot_path(out: str | None, engagement_id: str) -> pathlib.Path:
    if out:
        return pathlib.Path(out).resolve()
    env = os.environ.get("HIRE_ENGAGEMENT_SNAPSHOT_JSON")
    if env:
        return pathlib.Path(env).resolve()
    safe = "".join(c for c in engagement_id if c.isalnum() or c in "-_")[:40] or "engagement"
    return DEFAULT_SNAPSHOT_DIR / f"hire_engagement_{safe}_snapshot.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def _build_engagement_from_file(p: pathlib.Path):
    """Read a HireEngagement-shaped dict and reconstitute the dataclass."""
    from multifleet.hire_panel import (  # type: ignore
        HireEngagement,
        HireMilestone,
        HireStatus,
    )

    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"input file {p} must be a JSON object")

    milestones_in = raw.get("milestones") or []
    milestones = tuple(
        HireMilestone(
            timestamp=str(m.get("timestamp") or ""),
            description=str(m.get("description") or ""),
            evidence_record_id=m.get("evidence_record_id"),
        )
        for m in milestones_in
        if isinstance(m, dict)
    )
    status_raw = str(raw.get("status") or HireStatus.SCOPING.value)
    try:
        status = HireStatus(status_raw)
    except ValueError as exc:
        raise ValueError(f"invalid status {status_raw!r}: {exc}") from exc

    return HireEngagement(
        engagement_id=str(raw.get("engagement_id") or ""),
        client_name=str(raw.get("client_name") or ""),
        started_at=str(raw.get("started_at") or _now_iso()),
        current_task=str(raw.get("current_task") or ""),
        deliverables=tuple(str(d) for d in (raw.get("deliverables") or ())),
        atlas_actor=str(raw.get("atlas_actor") or "Atlas"),
        status=status,
        recent_evidence_record_ids=tuple(
            str(x) for x in (raw.get("recent_evidence_record_ids") or ())
        ),
        milestones=milestones,
        last_updated_at=raw.get("last_updated_at"),
    )


def _load_template() -> dict[str, Any]:
    """Read the committed template. Caller-friendly fallback."""
    try:
        return json.loads(TEMPLATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"dump-hire-engagement-snapshot: template read failed at {TEMPLATE_PATH}: {exc}\n"
        )
        return {}


def _build_engagement_from_cli(args: "argparse.Namespace") -> tuple[Any, dict[str, Any]]:
    """Construct a HireEngagement from KK4 CLI args.

    Returns (HireEngagement, internal_metadata_dict). The internal metadata
    contains the fields not on the EE1 redaction allowlist (scope, rate,
    hours, evidence_packets) so they can be written into the ledger record
    for Aaron-side audit, separate from the redacted client snapshot.
    """
    from multifleet.hire_panel import (  # type: ignore
        HireEngagement,
        HireMilestone,
        HireStatus,
    )

    template = _load_template()
    template_engagement = (template.get("engagement") or {}) if isinstance(template, dict) else {}

    deliverables: tuple[str, ...] = tuple(
        str(d).strip() for d in (args.deliverable or ()) if str(d).strip()
    )
    if not deliverables and template_engagement.get("deliverables"):
        # Fall back to the template list ONLY if every entry looks like a
        # placeholder (starts with "<"). Otherwise leave empty — we never
        # want a real deliverables list overwritten by template guidance.
        sample = template_engagement.get("deliverables") or []
        if all(isinstance(x, str) and x.startswith("<") for x in sample):
            deliverables = tuple()

    milestones: tuple[Any, ...] = tuple(
        HireMilestone(
            timestamp=_now_iso(),
            description=f"Engagement scoped: {args.scope[:120]}",
            evidence_record_id=None,
        )
        for _ in (1,)
    )

    try:
        status = HireStatus(args.status)
    except ValueError as exc:
        raise SystemExit(
            f"dump-hire-engagement-snapshot: invalid --status {args.status!r}: {exc}"
        )

    started_at = _now_iso()
    engagement = HireEngagement(
        engagement_id=args.engagement_id,
        client_name=args.client_name,
        started_at=started_at,
        current_task=f"Scoping engagement with {args.client_name}",
        deliverables=deliverables,
        atlas_actor="Atlas",
        status=status,
        recent_evidence_record_ids=(),
        milestones=milestones,
        last_updated_at=started_at,
    )

    internal = {
        "scope": args.scope,
        "rate_usd": float(args.rate_usd),
        "hours_committed": float(args.hours),
        "evidence_packets": [],  # populated by Aaron over the engagement lifecycle
    }
    return engagement, internal


def _record_engagement_created(
    engagement_id: str,
    client_name: str,
    redacted: dict[str, Any],
    internal: dict[str, Any],
) -> str | None:
    """Append an `engagement_created` event to the multifleet EvidenceLedger.

    Returns the new entry_id on success or None on failure (failure is
    structured-logged but never raised — the snapshot itself is still
    written so the IDE renders a usable page even if the ledger is
    unreachable). Mirrors the Mementos pattern from GG5: additive new
    record kind, no schema change to existing rows.
    """
    try:
        from multifleet.evidence_ledger import EvidenceLedger  # type: ignore
    except ImportError as exc:
        sys.stderr.write(
            f"dump-hire-engagement-snapshot: EvidenceLedger unavailable ({exc}); "
            "skipping engagement_created record\n"
        )
        return None

    try:
        ledger = EvidenceLedger()
        entry = ledger.record(
            event_type=ENGAGEMENT_CREATED_EVENT_TYPE,
            node_id="hire_panel.dump",
            subject=f"hire:{engagement_id}:created:{client_name[:40]}",
            payload={
                "tags": [f"hire:{engagement_id}", "engagement_created"],
                "engagement_id": engagement_id,
                "client_name": client_name,
                "redacted_snapshot": redacted,  # what the client sees
                "internal": internal,           # scope / rate / hours / packets
            },
        )
        return str(entry.get("entry_id") or "") or None
    except Exception as exc:  # noqa: BLE001 — ZSF: log + return None
        sys.stderr.write(
            f"dump-hire-engagement-snapshot: ledger record failed: {exc!r}\n"
        )
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engagement-id",
        required=True,
        help="Engagement id to snapshot (forms the output filename).",
    )
    parser.add_argument(
        "--from-file",
        default=None,
        help="Read a JSON HireEngagement.to_dict() blob (test/seed path).",
    )
    # KK4 — first-paying-client onboarding mode: build the engagement from
    # CLI args, render via the template, write snapshot, and append an
    # `engagement_created` evidence record. Mutually exclusive with
    # --from-file (we error if both are given to avoid silent override).
    parser.add_argument(
        "--client-name",
        default=None,
        help="(KK4) Human-readable client name.",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="(KK4) Engagement scope description (paragraph).",
    )
    parser.add_argument(
        "--rate-usd",
        type=float,
        default=None,
        help="(KK4) Hourly rate in USD (Atlas-only — not surfaced to client).",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=None,
        help="(KK4) Hours committed (Atlas-only — not surfaced to client).",
    )
    parser.add_argument(
        "--deliverable",
        action="append",
        default=None,
        help="(KK4) Deliverable string. Repeat for multiple. Surfaces to client.",
    )
    parser.add_argument(
        "--status",
        default="scoping",
        help="(KK4) Initial HireStatus (default: scoping).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Snapshot path (default: dashboard_exports/hire_engagement_<id>_snapshot.json "
             "or HIRE_ENGAGEMENT_SNAPSHOT_JSON env).",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cli_mode_fields = (args.client_name, args.scope, args.rate_usd, args.hours)
    cli_mode = any(v is not None for v in cli_mode_fields)
    if cli_mode and args.from_file:
        sys.stderr.write(
            "dump-hire-engagement-snapshot: --from-file is mutually exclusive "
            "with --client-name/--scope/--rate-usd/--hours\n"
        )
        return 2
    if cli_mode and not all(v is not None for v in cli_mode_fields):
        sys.stderr.write(
            "dump-hire-engagement-snapshot: KK4 CLI mode requires "
            "--client-name, --scope, --rate-usd, and --hours together\n"
        )
        return 2

    out_path = _resolve_snapshot_path(args.out, args.engagement_id)

    redacted: dict[str, Any] | None = None
    internal_meta: dict[str, Any] = {}
    ledger_entry_id: str | None = None
    if cli_mode:
        try:
            from multifleet.hire_panel import HirePanel  # type: ignore
        except ImportError as exc:
            sys.stderr.write(
                f"dump-hire-engagement-snapshot: hire_panel unavailable ({exc})\n"
            )
            return 3
        engagement, internal_meta = _build_engagement_from_cli(args)
        panel = HirePanel(evidence_ledger=_NullLedger())
        redacted = panel.redact_for_client(engagement)
        ledger_entry_id = _record_engagement_created(
            engagement_id=args.engagement_id,
            client_name=args.client_name or "",
            redacted=redacted,
            internal=internal_meta,
        )
    elif args.from_file:
        in_path = pathlib.Path(args.from_file).resolve()
        if not in_path.is_file():
            sys.stderr.write(
                f"dump-hire-engagement-snapshot: input file not found: {in_path}\n"
            )
            return 2
        try:
            engagement = _build_engagement_from_file(in_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            sys.stderr.write(
                f"dump-hire-engagement-snapshot: failed to read {in_path}: {exc}\n"
            )
            return 2

        try:
            from multifleet.hire_panel import HirePanel  # type: ignore
        except ImportError as exc:
            sys.stderr.write(
                f"dump-hire-engagement-snapshot: hire_panel unavailable ({exc})\n"
            )
            return 3

        # The dump path doesn't need a live ledger to redact — the
        # redact_for_client method only reads the in-memory dataclass.
        panel = HirePanel(evidence_ledger=_NullLedger())
        redacted = panel.redact_for_client(engagement)

    # Snapshot the live counters from the hire_panel module.
    try:
        from multifleet.hire_panel import counters_snapshot  # type: ignore
        counters = counters_snapshot()
    except ImportError as exc:
        sys.stderr.write(
            f"dump-hire-engagement-snapshot: counters unavailable ({exc}); continuing\n"
        )
        counters = {}

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "engagement": redacted,
        "counters": counters,
    }

    try:
        _atomic_write(out_path, snapshot)
    except OSError as exc:
        sys.stderr.write(f"dump-hire-engagement-snapshot: write failed: {exc}\n")
        return 3

    if not args.quiet:
        print(json.dumps({
            "snapshot_path": str(out_path),
            "engagement_id": args.engagement_id,
            "milestones_in_snapshot": (
                len(redacted["milestones"]) if isinstance(redacted, dict)
                and isinstance(redacted.get("milestones"), list) else 0
            ),
            "status": (
                redacted.get("status") if isinstance(redacted, dict) else None
            ),
            "counters": counters,
            # KK4 — engagement_created ledger record id (None when not in
            # CLI mode or when the ledger write failed; never crashes the
            # snapshot dump).
            "engagement_created_record_id": ledger_entry_id,
        }, indent=2, sort_keys=True))

    return 0


class _NullLedger:
    """Stand-in for an EvidenceLedger when the dump path only needs the
    redactor. Raises if any read/write is attempted, by design.
    """

    def record(self, *args, **kwargs):  # noqa: D401 — explicit denial
        raise RuntimeError("dump-hire-engagement-snapshot: ledger writes disabled")

    def query(self, *args, **kwargs):
        raise RuntimeError("dump-hire-engagement-snapshot: ledger queries disabled")


if __name__ == "__main__":
    sys.exit(main())
