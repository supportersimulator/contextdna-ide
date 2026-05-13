#!/usr/bin/env python3
"""
Cleanup zombie JetStream consumers (RR3).

Background
----------
QQ5 found stale durable pull consumers on the FLEET_MESSAGES stream
(``FLEET_MESSAGES_mac1`` / ``mac2`` / ``mac3``) accumulating 95k–215k
unprocessed messages each, last delivery 18h+ ago. The live consumer is
``FLEET_MESSAGES_cloud_inbox_listener``. The stale ones are leftover
durables from previous mac-side daemon runs that no longer pull — pure
storage leak.

Reversibility
-------------
Safe to delete: ``tools/fleet_nerve_nats.py::_ensure_durable_consumers``
calls ``multifleet.jetstream.create_durable_consumer`` on every daemon
start, which uses ``js.pull_subscribe`` — that creates the consumer
when missing and reuses it when present. So a deleted consumer
recreates the next time that node's daemon connects, with a fresh
"deliver=new" cursor.

Default behaviour
-----------------
``--dry-run`` lists candidates with name, num_pending,
last_active_age_h, will_delete bool. Nothing is deleted without
``--apply``.

Whitelist (never deleted, even with --apply):
  * ``cloud_inbox_listener`` (live consumer per QQ5)
  * any consumer whose ``last_active_age_h < 1`` (recently active)

Selection criteria (must satisfy ALL to be a candidate):
  * name matches ``--name-regex`` (default ``^FLEET_MESSAGES_mac[123]$``)
  * ``num_pending >= --pending-min`` (default 1000)
  * ``last_active_age_h >= --idle-min-hours`` (default 12)
                       OR no last_active timestamp at all (never delivered)

ZSF: every per-consumer failure (info read, delete) is captured in
``api_failures`` and a counter — never silently swallowed. The script
continues on failure rather than aborting the batch.

Idempotent: running twice is safe — second run finds nothing to do
(consumers either gone or freshly recreated and recently active).

Usage
-----
    # safe default
    python3 scripts/cleanup-zombie-jetstream-consumers.py

    # actually delete
    python3 scripts/cleanup-zombie-jetstream-consumers.py --apply

    # broaden criteria (e.g. include FLEET_MESSAGES_demo-st)
    python3 scripts/cleanup-zombie-jetstream-consumers.py \\
        --name-regex '^FLEET_MESSAGES_(mac[123]|demo-.*)$'

Exit codes
----------
  0  no candidates / dry-run completed cleanly
  1  candidates found in dry-run (caller can decide to --apply)
  2  unable to query (NATS unreachable, JetStream disabled, etc.)
  3  --apply finished with at least one delete failure
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Mirror jetstream-quorum-check.py path setup so this works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "multi-fleet"))
sys.path.insert(0, str(_REPO_ROOT))


# Hard-coded whitelist — these names are NEVER deleted regardless of stats.
# QQ5 identified cloud_inbox_listener as the live consumer; keep both the
# bare name and the FLEET_MESSAGES_-prefixed durable form to be safe.
WHITELIST: frozenset[str] = frozenset(
    {
        "cloud_inbox_listener",
        "FLEET_MESSAGES_cloud_inbox_listener",
    }
)

# Recently-active guard: any consumer with last_active within this many
# hours is protected even if it matches the regex/pending criteria.
RECENT_ACTIVE_GUARD_H: float = 1.0


def _parse_iso8601_z(s: Optional[str]) -> Optional[datetime]:
    """Parse an RFC3339/ISO-8601 timestamp like nats-py emits.

    Returns None on missing/unparseable input. Always returns a UTC-aware
    datetime when it returns one.
    """
    if not s:
        return None
    try:
        # nats-py uses trailing 'Z' — fromisoformat handles it from py3.11+
        # but normalise for older runtimes.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _hours_since(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    delta = datetime.now(timezone.utc) - dt
    return round(delta.total_seconds() / 3600.0, 2)


def _consumer_to_row(info: Any) -> dict[str, Any]:
    """Extract the fields we need from a ConsumerInfo object.

    nats-py's ConsumerInfo exposes nested ``delivered`` and ``ack_floor``
    SequenceInfo-style objects; ``last_active`` is the moment of the last
    delivery attempt (None if the consumer has never delivered).
    """
    name = getattr(info, "name", None) or ""
    num_pending = int(getattr(info, "num_pending", 0) or 0)
    num_redelivered = int(getattr(info, "num_redelivered", 0) or 0)

    delivered = getattr(info, "delivered", None)
    last_active_raw = getattr(delivered, "last_active", None) if delivered else None
    # nats-py may surface this as datetime already, or as ISO string —
    # normalise both.
    if isinstance(last_active_raw, datetime):
        last_active_dt = last_active_raw.astimezone(timezone.utc)
    else:
        last_active_dt = _parse_iso8601_z(
            str(last_active_raw) if last_active_raw is not None else None
        )

    return {
        "name": name,
        "num_pending": num_pending,
        "num_redelivered": num_redelivered,
        "last_active": last_active_dt.isoformat() if last_active_dt else None,
        "last_active_age_h": _hours_since(last_active_dt),
    }


def _is_candidate(
    row: dict[str, Any],
    *,
    name_re: re.Pattern[str],
    pending_min: int,
    idle_min_hours: float,
) -> tuple[bool, str]:
    """Return (is_candidate, reason_for_skip_if_any).

    Whitelist + recency guard always win over the regex/pending filters —
    they're hard-coded protections to keep the script safe by default.
    """
    name = row["name"]
    if name in WHITELIST:
        return False, "whitelisted"
    if not name_re.match(name):
        return False, "name_regex_no_match"
    age = row.get("last_active_age_h")
    if age is not None and age < RECENT_ACTIVE_GUARD_H:
        return False, f"recently_active({age}h<{RECENT_ACTIVE_GUARD_H}h)"
    if row["num_pending"] < pending_min:
        return False, f"pending<{pending_min}"
    # Idle gate: never-delivered (age is None) is treated as "infinitely
    # idle" — matches the QQ5 finding where mac2's last_active was None.
    if age is not None and age < idle_min_hours:
        return False, f"idle<{idle_min_hours}h"
    return True, ""


async def _run(
    *,
    nats_url: str,
    stream: str,
    name_re: re.Pattern[str],
    pending_min: int,
    idle_min_hours: float,
    apply: bool,
) -> dict[str, Any]:
    api_failures: list[str] = []
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []

    try:
        import nats  # type: ignore
    except Exception as e:
        return {
            "status": "error",
            "error": f"nats-py not installed: {e}",
            "api_failures": [str(e)],
            "candidates": [],
            "skipped": [],
            "deletions": [],
            "delete_failures": 0,
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
            "candidates": [],
            "skipped": [],
            "deletions": [],
            "delete_failures": 0,
        }

    delete_failures = 0
    try:
        try:
            js = nc.jetstream()
        except Exception as e:
            api_failures.append(f"jetstream(): {e}")
            return {
                "status": "error",
                "error": f"jetstream unavailable: {e}",
                "api_failures": api_failures,
                "candidates": [],
                "skipped": [],
                "deletions": [],
                "delete_failures": 0,
            }

        # Enumerate all consumers on the target stream.
        try:
            infos = await js.consumers_info(stream)
        except Exception as e:
            api_failures.append(f"consumers_info({stream}): {e}")
            return {
                "status": "error",
                "error": f"consumers_info failed: {e}",
                "api_failures": api_failures,
                "candidates": [],
                "skipped": [],
                "deletions": [],
                "delete_failures": 0,
            }

        for info in infos or []:
            try:
                row = _consumer_to_row(info)
            except Exception as e:
                api_failures.append(f"parse_consumer: {e}")
                continue

            is_cand, reason = _is_candidate(
                row,
                name_re=name_re,
                pending_min=pending_min,
                idle_min_hours=idle_min_hours,
            )
            row_out = dict(row)
            row_out["will_delete"] = bool(is_cand)
            if is_cand:
                candidates.append(row_out)
            else:
                row_out["skip_reason"] = reason
                skipped.append(row_out)

        if apply and candidates:
            for cand in candidates:
                name = cand["name"]
                # Belt-and-braces: re-check whitelist right before delete
                # in case a future caller passes a hostile regex.
                if name in WHITELIST:
                    deletions.append({"name": name, "deleted": False, "reason": "whitelisted_at_apply"})
                    continue
                try:
                    ok = await js.delete_consumer(stream, name)
                    deletions.append({"name": name, "deleted": bool(ok), "reason": ""})
                    if not ok:
                        delete_failures += 1
                        api_failures.append(f"delete_consumer({name}): returned falsy")
                except Exception as e:
                    delete_failures += 1
                    api_failures.append(f"delete_consumer({name}): {e}")
                    deletions.append({"name": name, "deleted": False, "reason": str(e)})
    finally:
        try:
            if nc is not None:
                await nc.close()
        except Exception as e:
            api_failures.append(f"close: {e}")

    if apply:
        status = "deleted_with_failures" if delete_failures else "deleted"
    else:
        status = "candidates_found" if candidates else "clean"

    return {
        "status": status,
        "stream": stream,
        "applied": apply,
        "criteria": {
            "name_regex": name_re.pattern,
            "pending_min": pending_min,
            "idle_min_hours": idle_min_hours,
            "recent_active_guard_h": RECENT_ACTIVE_GUARD_H,
            "whitelist": sorted(WHITELIST),
        },
        "candidates": candidates,
        "skipped": skipped,
        "deletions": deletions,
        "delete_failures": delete_failures,
        "api_failures": api_failures,
    }


def _render_table(report: dict[str, Any]) -> str:
    """Compact human-readable summary. Verbatim-friendly for audits."""
    lines: list[str] = []
    lines.append(f"# zombie-consumer cleanup — stream={report.get('stream')}")
    lines.append(f"status: {report.get('status')}  applied: {report.get('applied')}")
    crit = report.get("criteria", {})
    lines.append(
        "criteria: regex=%s pending_min=%s idle_min_hours=%s recent_guard=%sh"
        % (
            crit.get("name_regex"),
            crit.get("pending_min"),
            crit.get("idle_min_hours"),
            crit.get("recent_active_guard_h"),
        )
    )
    lines.append(f"whitelist: {', '.join(crit.get('whitelist', []))}")
    lines.append("")

    cands = report.get("candidates") or []
    lines.append(f"## candidates ({len(cands)})")
    if not cands:
        lines.append("  (none)")
    else:
        lines.append(
            "  %-44s %12s %16s %12s"
            % ("name", "num_pending", "last_active_age_h", "will_delete")
        )
        for c in cands:
            lines.append(
                "  %-44s %12d %16s %12s"
                % (
                    c["name"],
                    c["num_pending"],
                    str(c["last_active_age_h"]),
                    str(c["will_delete"]),
                )
            )

    skipped = report.get("skipped") or []
    lines.append("")
    lines.append(f"## skipped ({len(skipped)})")
    for s in skipped:
        lines.append(
            "  %-44s pending=%d age_h=%s reason=%s"
            % (
                s["name"],
                s["num_pending"],
                str(s["last_active_age_h"]),
                s.get("skip_reason", ""),
            )
        )

    dels = report.get("deletions") or []
    if dels:
        lines.append("")
        lines.append(f"## deletions ({len(dels)})")
        for d in dels:
            lines.append(
                "  %-44s deleted=%s reason=%s"
                % (d["name"], str(d["deleted"]), d.get("reason", ""))
            )

    fails = report.get("api_failures") or []
    if fails:
        lines.append("")
        lines.append(f"## api_failures ({len(fails)})")
        for f in fails:
            lines.append(f"  - {f}")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up zombie JetStream consumers (RR3 / QQ5 follow-up)"
    )
    parser.add_argument(
        "--nats-url",
        default=os.environ.get("NATS_URL", "nats://127.0.0.1:4222"),
    )
    parser.add_argument(
        "--stream",
        default="FLEET_MESSAGES",
        help="Stream to clean up (default: FLEET_MESSAGES)",
    )
    parser.add_argument(
        "--name-regex",
        default=r"^FLEET_MESSAGES_mac[123]$",
        help=(
            "Regex matching consumer names eligible for cleanup. "
            "Default targets only the mac1/mac2/mac3 zombies QQ5 found."
        ),
    )
    parser.add_argument(
        "--pending-min",
        type=int,
        default=1000,
        help="Minimum num_pending to be considered a candidate (default 1000)",
    )
    parser.add_argument(
        "--idle-min-hours",
        type=float,
        default=12.0,
        help="Minimum hours since last_active to be considered idle (default 12)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="List candidates without deleting (DEFAULT, kept for clarity)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the candidates. Without this flag, runs read-only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable table",
    )
    args = parser.parse_args()

    try:
        name_re = re.compile(args.name_regex)
    except re.error as e:
        print(f"ERROR: invalid --name-regex: {e}", file=sys.stderr)
        return 2

    apply = bool(args.apply)
    # --dry-run is the default; --apply explicitly opts out. We never
    # silently combine them — --apply wins so the user gets the deletion
    # they asked for.
    report = asyncio.run(
        _run(
            nats_url=args.nats_url,
            stream=args.stream,
            name_re=name_re,
            pending_min=args.pending_min,
            idle_min_hours=args.idle_min_hours,
            apply=apply,
        )
    )

    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_render_table(report))

    status = report.get("status")
    if status == "error":
        return 2
    if status == "deleted_with_failures":
        return 3
    if status == "candidates_found":
        # dry-run found work to do — non-zero so callers can branch
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
