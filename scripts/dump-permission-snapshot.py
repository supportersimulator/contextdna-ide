#!/usr/bin/env python3
"""Dump the latest PermissionGovernor snapshot as JSON for the IDE.

Z2 SCAFFOLD bridge (2026-05-07).

Reads the most recent row from ``memory/permission_governor.db`` (table
``permission_map_snapshots``) and prints the snapshot JSON to stdout.

Usage
-----
    python3 scripts/dump-permission-snapshot.py             # latest snapshot
    python3 scripts/dump-permission-snapshot.py --pretty    # pretty-print
    python3 scripts/dump-permission-snapshot.py --db /path  # override DB

Wire shape
----------
Output is the canonical PermissionGovernor wire shape::

    {
      "schema_version": "v1",
      "generated_at":  "2026-05-07T...",
      "entries":       [PermissionEntry, ...],
      "hash":          "<sha256>",
      "source":        "snapshot" | "no-snapshot"
    }

If no snapshot exists, prints
``{"schema_version": 0, "generated_at": null, "entries": [], "source": "no-snapshot"}``
and exits 0.

ZSF: every error path bumps a counter visible via
``multifleet.permission_governor.flat_counters()`` (logged to stderr on exit).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

# Make the multifleet package importable regardless of cwd.
_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
for p in (str(_REPO_ROOT / "multi-fleet"), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("PERMISSION_GOVERNOR_DB"),
        help="Path to permission_governor.db (default: memory/permission_governor.db).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args(argv)

    # Lazy import — keeps --help fast even if the package fails to import.
    try:
        from multifleet.permission_governor import (  # type: ignore
            PermissionGovernor,
            PermissionGovernorError,
            flat_counters,
        )
    except ImportError as exc:
        sys.stderr.write(
            f"dump-permission-snapshot: import failed: {exc}\n"
        )
        # Emit graceful empty payload so the route never 500s on import.
        print(json.dumps(
            {
                "schema_version": 0,
                "generated_at": None,
                "entries": [],
                "source": "import-error",
                "error": str(exc)[:200],
            },
            sort_keys=True,
        ))
        return 0

    governor = PermissionGovernor(db_path=args.db) if args.db else PermissionGovernor()

    try:
        latest = governor.latest_snapshot()
    except PermissionGovernorError as exc:
        sys.stderr.write(
            f"dump-permission-snapshot: latest_snapshot failed: {exc}\n"
        )
        sys.stderr.write(f"counters: {flat_counters()!r}\n")
        # Graceful: route should not 500. Output an error payload.
        print(json.dumps(
            {
                "schema_version": 0,
                "generated_at": None,
                "entries": [],
                "source": "error",
                "error": str(exc)[:200],
            },
            sort_keys=True,
        ))
        return 0

    if latest is None:
        payload: dict[str, Any] = {
            "schema_version": 0,
            "generated_at": None,
            "entries": [],
            "source": "no-snapshot",
        }
    else:
        payload = governor.serialize(latest)
        payload["source"] = "snapshot"

    indent = 2 if args.pretty else None
    print(json.dumps(payload, sort_keys=True, indent=indent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
