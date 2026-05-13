#!/usr/bin/env python3
"""Dump a JSON summary of the EvidenceLedger SQLite store for the IDE.

Reads ``memory/evidence_ledger.db`` (stdlib ``sqlite3`` only — no extra
deps) and writes a compact JSON snapshot that the Next.js
``/api/competition/status`` route can fold into the CampaignTheater panel
without spawning Python or pulling ``better-sqlite3``.

Output path: ``dashboard_exports/evidence_ledger_summary.json``
(override with ``--out`` or ``EVIDENCE_LEDGER_SUMMARY_JSON``).

Snapshot shape (kept intentionally small — the IDE renders defensively):

  {
    "schema_version": "evidence_ledger_summary/v1",
    "generated_at": "2026-05-04T20:34:11Z",
    "db_path": "memory/evidence_ledger.db",
    "ok": true,
    "total_records": 2,
    "by_kind": {"experiment": 1, "audit": 1},
    "records": [                    # newest-first, capped by --limit (default 25)
      {
        "record_id": "abcd1234...",
        "kind": "experiment",
        "created_at": "2026-05-04T19:00:00Z",
        "schema_version": "v1",
        "git_rev": "deadbeef",
        "summary": "...",            # content.summary OR content.title OR
                                     #   first 120 chars of canonical content JSON
        "parent_count": 0
      },
      ...
    ]
  }

ZSF (Zero Silent Failures)
--------------------------
* DB missing -> exit 0, write a snapshot with ``ok: false`` and ``reason``
  so the IDE can surface "ledger empty / not initialised" without crashing.
* SQLite errors -> exit 2, write a snapshot with ``ok: false`` and the
  error string. The IDE falls back to the existing audit-only path.
* Errors are also printed to stderr so cron/launchd surfaces them.

Reversibility
-------------
Pure read + atomic write of ONE file. ``rm`` the snapshot to revert.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import traceback
from typing import Any

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent  # scripts/.. -> superrepo

DEFAULT_DB = _REPO_ROOT / "memory" / "evidence_ledger.db"
DEFAULT_OUT = _REPO_ROOT / "dashboard_exports" / "evidence_ledger_summary.json"
SCHEMA_VERSION = "evidence_ledger_summary/v1"

# Cap content-derived strings so the JSON stays small even for fat records.
SUMMARY_MAX = 240


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically (tmpfile in same dir + os.replace).

    Avoids the IDE seeing a half-written file mid-poll.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".evidence_ledger_summary.", suffix=".json.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        # Clean up tmp file before re-raising so we don't leave litter.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _summary_from_content(content_json: str) -> str:
    """Extract a short human-readable summary from a record's content JSON."""
    try:
        data = json.loads(content_json) if content_json else {}
    except (TypeError, ValueError):
        return content_json[:SUMMARY_MAX] if isinstance(content_json, str) else ""
    if not isinstance(data, dict):
        return str(data)[:SUMMARY_MAX]
    for key in ("summary", "title", "name", "decision", "experiment_id"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v[:SUMMARY_MAX]
    # Fall back to a compact JSON head.
    try:
        return json.dumps(data, sort_keys=True)[:SUMMARY_MAX]
    except (TypeError, ValueError):
        return str(data)[:SUMMARY_MAX]


def _read_records(
    db_path: pathlib.Path, limit: int
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """Read records from the ledger DB.

    Returns (records, by_kind, total_records). Caller handles missing DB
    and SQLite errors.
    """
    # Read-only mode so we never accidentally write or create the file.
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        # Total + per-kind counts (cheap aggregates).
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_records"
        ).fetchone()["n"]
        by_kind: dict[str, int] = {
            r["kind"]: r["n"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM evidence_records GROUP BY kind"
            ).fetchall()
        }

        rows = conn.execute(
            "SELECT record_id, content_json, kind, created_at, "
            "schema_version, git_rev "
            "FROM evidence_records "
            "ORDER BY created_at DESC, record_id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()

        records: list[dict[str, Any]] = []
        for r in rows:
            rec_id = r["record_id"]
            parent_count = conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_parents WHERE child_id = ?",
                (rec_id,),
            ).fetchone()["n"]
            records.append(
                {
                    "record_id": rec_id,
                    "kind": r["kind"],
                    "created_at": r["created_at"],
                    "schema_version": r["schema_version"],
                    "git_rev": r["git_rev"],
                    "summary": _summary_from_content(r["content_json"] or ""),
                    "parent_count": int(parent_count),
                }
            )
        return records, by_kind, int(total)
    finally:
        conn.close()


def build_snapshot(
    db_path: pathlib.Path, limit: int
) -> tuple[dict[str, Any], int]:
    """Return (snapshot_payload, exit_code).

    exit_code is the value the CLI should return — 0 on healthy or
    "DB missing" (still writes a fallback snapshot), 2 on read error.
    """
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "db_path": str(db_path.relative_to(_REPO_ROOT))
        if db_path.is_relative_to(_REPO_ROOT)
        else str(db_path),
    }

    if not db_path.exists():
        return (
            {
                **base,
                "ok": False,
                "reason": "db_missing",
                "total_records": 0,
                "by_kind": {},
                "records": [],
            },
            0,
        )

    try:
        records, by_kind, total = _read_records(db_path, limit)
    except sqlite3.Error as e:
        return (
            {
                **base,
                "ok": False,
                "reason": "sqlite_error",
                "error": str(e),
                "total_records": 0,
                "by_kind": {},
                "records": [],
            },
            2,
        )

    return (
        {
            **base,
            "ok": True,
            "total_records": total,
            "by_kind": by_kind,
            "records": records,
        },
        0,
    )


def _resolve_out(arg_out: str | None) -> pathlib.Path:
    if arg_out:
        return pathlib.Path(arg_out).resolve()
    env_out = os.environ.get("EVIDENCE_LEDGER_SUMMARY_JSON")
    if env_out:
        return pathlib.Path(env_out).resolve()
    return DEFAULT_OUT


def _resolve_db(arg_db: str | None) -> pathlib.Path:
    if arg_db:
        return pathlib.Path(arg_db).resolve()
    env_db = os.environ.get("EVIDENCE_LEDGER_DB")
    if env_db:
        return pathlib.Path(env_db).resolve()
    return DEFAULT_DB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        help="Path to evidence_ledger.db (default: memory/evidence_ledger.db).",
    )
    parser.add_argument(
        "--out",
        help="Output JSON path (default: dashboard_exports/evidence_ledger_summary.json).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Max records to include (default 25, newest-first).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print snapshot JSON to stdout instead of writing a file.",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db(args.db)
    out_path = _resolve_out(args.out)

    try:
        snapshot, exit_code = build_snapshot(db_path, max(1, int(args.limit)))
    except Exception as e:  # noqa: BLE001 — last-resort ZSF guard
        # Never crash the cron/launchd job silently; emit a fallback snapshot.
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "db_path": str(db_path),
            "ok": False,
            "reason": "unhandled_exception",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(limit=3),
            "total_records": 0,
            "by_kind": {},
            "records": [],
        }
        exit_code = 2

    if args.stdout:
        json.dump(snapshot, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return exit_code

    try:
        _atomic_write(out_path, snapshot)
    except OSError as e:
        sys.stderr.write(f"dump-evidence-ledger-summary: write failed: {e}\n")
        return 3

    if not snapshot.get("ok"):
        # Surface the reason on stderr so cron logs catch it; still exit 0
        # for "db_missing" so the wrapper job doesn't alarm during bootstrap.
        sys.stderr.write(
            f"dump-evidence-ledger-summary: ok=false reason="
            f"{snapshot.get('reason')!r}\n"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
