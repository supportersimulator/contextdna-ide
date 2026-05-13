#!/usr/bin/env python3
"""
Fleet JetStream provisioner — idempotent stream creation/verification.

X3 (Phase-5 wave, 2026-05-07).

Ensures the canonical fleet streams (FLEET_MESSAGES, FLEET_EVENTS, FLEET_AUDIT)
exist on the local NATS server with the expected subject filters, replica
count, and retention. Safe to run repeatedly: existing streams are
verified for config drift; missing streams are created; nothing is ever
deleted.

Usage::

    .venv/bin/python3 tools/fleet_jetstream_provision.py [--url URL] [--json]

Defaults:
  --url   nats://127.0.0.1:4222

Exit codes:
  0  All target streams in expected state at end of run
  1  At least one stream could not be reconciled (errors printed to stderr)
  2  Could not connect to NATS / JetStream unavailable

ZSF: every failure path increments a counter exposed in the JSON report and
prints to stderr. No silent swallows.

Constraints honored:
  * NEVER deletes a stream (no js.delete_stream call exists in this file).
  * NEVER narrows subjects on existing streams — extra subjects an operator
    added later are preserved.
  * Idempotent: running twice in a row yields all "verified_ok".
  * Forward-compatible with NATS 2.x JetStream + nats-py 2.14 + Python 3.14.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Ensure multi-fleet is importable when run from repo root without -e install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MF_PATH = os.path.join(_REPO_ROOT, "multi-fleet")
if _MF_PATH not in sys.path:
    sys.path.insert(0, _MF_PATH)

import nats  # type: ignore  # noqa: E402

from multifleet.jetstream import (  # noqa: E402
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_BYTES_MB,
    DEFAULT_NUM_REPLICAS,
    STREAM_AUDIT,
    STREAM_EVENTS,
    STREAM_MESSAGES,
    STREAM_PROBES,
    STREAM_RETENTION_OVERRIDES,
    STREAM_SUBJECTS,
)

DEFAULT_NATS_URL = "nats://127.0.0.1:4222"

# ZSF counters — every failure path advances exactly one of these so the
# operator can see what went wrong without re-reading logs.
COUNTERS: Dict[str, int] = {
    "js_streams_created_total": 0,
    "js_streams_verified_ok_total": 0,
    "js_streams_config_drift_total": 0,
    "js_provision_errors_total": 0,
    # EE3 (Phase-12): EVENT_PROBES + FLEET_EVENTS subject overlap is the
    # expected steady-state when both streams are provisioned. NATS rejects
    # the second add with "subjects overlap"; we count and degrade gracefully
    # — probe events fall through the broader FLEET_EVENTS path. ZSF.
    "js_streams_subject_overlap_total": 0,
}


def _expected_config(stream: str) -> Dict[str, Any]:
    """Return the target config for ``stream``.

    Pulls per-stream overrides (e.g. FLEET_AUDIT's tighter caps) from
    ``STREAM_RETENTION_OVERRIDES`` and falls back to the module defaults.
    """
    overrides = STREAM_RETENTION_OVERRIDES.get(stream, {})
    max_age_days = int(overrides.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
    max_bytes_mb = int(overrides.get("max_bytes_mb", DEFAULT_MAX_BYTES_MB))
    max_msgs = overrides.get("max_msgs")  # may be absent → unbounded
    subjects = STREAM_SUBJECTS.get(stream)
    if subjects is None:
        raise ValueError(f"unknown stream {stream!r} (no subjects mapping)")
    return {
        "name": stream,
        "subjects": list(subjects),
        "num_replicas": DEFAULT_NUM_REPLICAS,
        "retention": "limits",
        "storage": "file",
        "max_age": max_age_days * 24 * 3600,
        "max_bytes": max_bytes_mb * 1024 * 1024,
        "max_msgs": int(max_msgs) if max_msgs is not None else None,
    }


def _diff_config(existing_cfg: Any, target: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable drift descriptions.

    Subject drift is reported only when the *target* contains subjects the
    existing stream lacks — extra subjects an operator added later are
    preserved (forward-compat).
    """
    drifts: List[str] = []
    cur_subjects = set(getattr(existing_cfg, "subjects", []) or [])
    target_subjects = set(target["subjects"])
    missing_subjects = target_subjects - cur_subjects
    if missing_subjects:
        drifts.append(
            f"subjects missing {sorted(missing_subjects)} "
            f"(have {sorted(cur_subjects)})"
        )
    cur_replicas = int(getattr(existing_cfg, "num_replicas", 1) or 1)
    if cur_replicas != target["num_replicas"]:
        drifts.append(
            f"replicas {cur_replicas} != target {target['num_replicas']}"
        )
    cur_age = int(getattr(existing_cfg, "max_age", 0) or 0)
    if cur_age != target["max_age"]:
        drifts.append(f"max_age {cur_age}s != target {target['max_age']}s")
    # max_bytes/max_msgs are size *caps* — only flag drift if the existing
    # cap is *smaller* than the target (would prematurely drop data) or if
    # the target is set and the existing is unlimited.
    if target.get("max_bytes"):
        cur_bytes = int(getattr(existing_cfg, "max_bytes", -1) or -1)
        if cur_bytes != -1 and cur_bytes < target["max_bytes"]:
            drifts.append(
                f"max_bytes {cur_bytes} < target {target['max_bytes']}"
            )
    if target.get("max_msgs"):
        cur_msgs = int(getattr(existing_cfg, "max_msgs", -1) or -1)
        if cur_msgs != -1 and cur_msgs < target["max_msgs"]:
            drifts.append(
                f"max_msgs {cur_msgs} < target {target['max_msgs']}"
            )
    return drifts


async def _ensure_one(js: Any, stream: str) -> Dict[str, Any]:
    """Reconcile a single stream. Returns a per-stream report dict."""
    target = _expected_config(stream)
    report: Dict[str, Any] = {
        "stream": stream,
        "subjects": target["subjects"],
        "target_replicas": target["num_replicas"],
        "action": None,
        "drift": [],
        "error": None,
    }
    try:
        info = await js.stream_info(stream)
    except Exception as e:
        # Likely NotFoundError — create it. Any other error still goes
        # through the create path; if create then fails we report it.
        report["info_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        info = None

    if info is None:
        try:
            create_kwargs: Dict[str, Any] = {
                "name": target["name"],
                "subjects": target["subjects"],
                "retention": target["retention"],
                "storage": target["storage"],
                "max_age": target["max_age"],
                "max_bytes": target["max_bytes"],
                "num_replicas": target["num_replicas"],
            }
            if target.get("max_msgs") is not None:
                create_kwargs["max_msgs"] = target["max_msgs"]
            await js.add_stream(**create_kwargs)
            COUNTERS["js_streams_created_total"] += 1
            report["action"] = "created"
        except Exception as e:
            msg = str(e).lower()
            # Race: another node provisioned between info + create — accept.
            if "already" in msg or "in use" in msg or "stream name" in msg:
                COUNTERS["js_streams_verified_ok_total"] += 1
                report["action"] = "verified_ok_race"
            elif "overlap" in msg or "subjects overlap" in msg:
                # EE3: EVENT_PROBES subject `event.probe.>` overlaps with the
                # broader FLEET_EVENTS `event.>`. Expected on first provision
                # if FLEET_EVENTS already exists. Probe events fall through
                # to FLEET_EVENTS — publisher honors this via its own fallback.
                COUNTERS["js_streams_subject_overlap_total"] += 1
                report["action"] = "skipped_subject_overlap"
                report["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            else:
                COUNTERS["js_provision_errors_total"] += 1
                report["action"] = "create_failed"
                report["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return report

    # Stream exists — diff config and reconcile only the safe drifts.
    cfg = info.config
    drifts = _diff_config(cfg, target)
    report["drift"] = drifts
    if not drifts:
        COUNTERS["js_streams_verified_ok_total"] += 1
        report["action"] = "verified_ok"
        return report

    # Drift present — try update_stream. Subject expansion (add missing) +
    # replica/age/byte/msg bumps are safe online ops. We never narrow.
    try:
        merged_subjects = sorted(
            set(getattr(cfg, "subjects", []) or []) | set(target["subjects"])
        )
        cfg.subjects = merged_subjects
        cfg.num_replicas = target["num_replicas"]
        cfg.max_age = target["max_age"]
        # Only widen byte/msg caps; never shrink.
        cur_bytes = int(getattr(cfg, "max_bytes", -1) or -1)
        if target["max_bytes"] and (cur_bytes == -1 or cur_bytes < target["max_bytes"]):
            cfg.max_bytes = target["max_bytes"]
        cur_msgs = int(getattr(cfg, "max_msgs", -1) or -1)
        if target.get("max_msgs") and (
            cur_msgs == -1 or cur_msgs < target["max_msgs"]
        ):
            cfg.max_msgs = target["max_msgs"]
        await js.update_stream(config=cfg)
        COUNTERS["js_streams_config_drift_total"] += 1
        report["action"] = "reconciled"
    except Exception as e:
        COUNTERS["js_provision_errors_total"] += 1
        report["action"] = "update_failed"
        report["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return report


async def provision(url: str, streams: List[str]) -> Dict[str, Any]:
    """Connect, provision each stream, return a structured report."""
    report: Dict[str, Any] = {
        "url": url,
        "streams": {},
        "counters": COUNTERS,
        "status": "ok",
    }
    try:
        nc = await nats.connect(url, connect_timeout=5)
    except Exception as e:
        COUNTERS["js_provision_errors_total"] += 1
        report["status"] = "connect_failed"
        report["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return report

    try:
        try:
            js = nc.jetstream()
        except Exception as e:
            COUNTERS["js_provision_errors_total"] += 1
            report["status"] = "jetstream_unavailable"
            report["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            return report

        for stream in streams:
            report["streams"][stream] = await _ensure_one(js, stream)
    finally:
        try:
            await nc.drain()
        except Exception as _e:  # ZSF: drain is best-effort
            COUNTERS["js_provision_errors_total"] += 1
            report.setdefault("warnings", []).append(
                f"drain: {type(_e).__name__}: {str(_e)[:120]}"
            )

    if COUNTERS["js_provision_errors_total"] > 0:
        report["status"] = "errors"
    elif COUNTERS["js_streams_subject_overlap_total"] > 0:
        # Subject overlap is an *expected* steady-state when EVENT_PROBES
        # cannot be created alongside FLEET_EVENTS. Surface it via a distinct
        # status so caller scripts can branch — but it's not an error.
        report["status"] = "ok_with_overlap"
    return report


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument(
        "--url",
        default=os.environ.get("NATS_URL", DEFAULT_NATS_URL),
        help=f"NATS URL (default: {DEFAULT_NATS_URL})",
    )
    p.add_argument(
        "--json", action="store_true", help="Emit a JSON report on stdout"
    )
    p.add_argument(
        "--streams",
        nargs="+",
        default=[STREAM_MESSAGES, STREAM_EVENTS, STREAM_AUDIT, STREAM_PROBES],
        help="Stream names to provision (default: all 4 canonical streams)",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    report = asyncio.run(provision(args.url, args.streams))

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"NATS: {report['url']}  status={report['status']}")
        for stream, entry in report.get("streams", {}).items():
            line = (
                f"  {stream:<16} action={entry.get('action')}  "
                f"replicas={entry.get('target_replicas')}"
            )
            if entry.get("drift"):
                line += f"  drift={entry['drift']}"
            if entry.get("error"):
                line += f"  error={entry['error']}"
            print(line)
        c = report["counters"]
        print(
            "  counters: "
            f"created={c['js_streams_created_total']}  "
            f"verified_ok={c['js_streams_verified_ok_total']}  "
            f"drift_reconciled={c['js_streams_config_drift_total']}  "
            f"subject_overlap={c['js_streams_subject_overlap_total']}  "
            f"errors={c['js_provision_errors_total']}"
        )
        if report.get("error"):
            print(f"  top-level error: {report['error']}", file=sys.stderr)

    if report["status"] in ("ok", "ok_with_overlap"):
        return 0
    if report["status"] in ("connect_failed", "jetstream_unavailable"):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
