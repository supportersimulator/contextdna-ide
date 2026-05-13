"""North Star — non-decaying vision priorities.

Aaron's locked sequence: Multi-Fleet → 3-Surgeons → ContextDNA IDE →
full local ops → ER Simulator. Each entry is a persistent (non-decaying)
truth that future TBG/alignment scoring can reference. Seeded once and
queryable thereafter.

Per docs/plans/2026-04-25-tbg-north-star-architecture.md (commit 4224d908):
this is the smallest demonstrative first commit — Aaron immediately sees
his locked sequence in queryable form, addressing his stated pain that
"many aspects are forgotten that used to be very big plans."

Storage: synaptic_north_star_priorities table on the consolidated DB
(memory/contextdna.db) via memory/consolidated_db.synaptic_db().

CLI:
    python3 -m memory.north_star list           # show all priorities, ranked
    python3 -m memory.north_star get <id>       # one entry detail
    python3 -m memory.north_star reviewed <id>  # mark reviewed (resets cycle)
    python3 -m memory.north_star status         # which are due for review
    python3 -m memory.north_star seed --force   # re-seed (idempotent)
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from typing import Iterable

from memory.consolidated_db import synaptic_db, DOMAIN_PREFIXES

_TABLE_SUFFIX = "north_star_priorities"
_TABLE = f"{DOMAIN_PREFIXES['synaptic']}{_TABLE_SUFFIX}"

# Aaron's locked sequence as of 2026-04-25. Order matters: each item depends
# on its predecessor reaching working state first. None of these decay.
_SEED: tuple[dict, ...] = (
    {
        "id": "multi-fleet",
        "rank": 1,
        "name": "Multi-Fleet — coordination foundation",
        "description": (
            "Multi-machine fleet daemon with NATS pub/sub, 7-channel cascade, "
            "self-healing, multi-chief Raft. Must be stable + observable before "
            "anything sits on top of it."
        ),
        "depends_on": [],
    },
    {
        "id": "3-surgeons",
        "rank": 2,
        "name": "3-Surgeons — multi-LLM consensus",
        "description": (
            "Atlas + Cardiologist + Neurologist cross-examination protocol. "
            "Disagreements are the feature. Must be optimal before IDE layer."
        ),
        "depends_on": ["multi-fleet"],
    },
    {
        "id": "context-dna-ide",
        "rank": 3,
        "name": "ContextDNA IDE — visible intelligence",
        "description": (
            "VS Code panels + theatrical dashboard + admin.contextdna.io view. "
            "AI thinking is visible, corrigible, collaborative. Sits on top of "
            "stable Multi-Fleet + optimal 3-Surgeons."
        ),
        "depends_on": ["3-surgeons"],
    },
    {
        "id": "full-local-ops",
        "rank": 4,
        "name": "Full local operations — ContextDNA core",
        "description": (
            "Memory historian, evidence ledger, gold mining, Synaptic, "
            "9-section webhook injection — all running locally, persistent, "
            "self-improving."
        ),
        "depends_on": ["context-dna-ide"],
    },
    {
        "id": "er-simulator",
        "rank": 5,
        "name": "ER Simulator — application built on the platform",
        "description": (
            "Event-driven medical training audio. Demonstrates the platform "
            "in production. Resumes only after the four foundations work end-to-end."
        ),
        "depends_on": ["full-local-ops"],
    },
)


def _ensure_schema(cur) -> None:
    """Create the priorities table if missing (idempotent)."""
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id                  TEXT PRIMARY KEY,
            rank                INTEGER NOT NULL,
            name                TEXT NOT NULL,
            description         TEXT,
            depends_on          TEXT,
            non_decay           INTEGER NOT NULL DEFAULT 1,
            review_cycle_days   INTEGER NOT NULL DEFAULT 7,
            alignment_weight    REAL NOT NULL DEFAULT 1.0,
            created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_reviewed_at    TEXT
        )
        """
    )


def seed(force: bool = False) -> int:
    """Insert the locked sequence. Idempotent unless force=True replaces rows."""
    inserted = 0
    with synaptic_db() as cur:
        _ensure_schema(cur)
        for entry in _SEED:
            depends_json = json.dumps(entry["depends_on"])
            if force:
                cur.execute(f"DELETE FROM {_TABLE} WHERE id = ?", (entry["id"],))
            row = cur.execute(
                f"SELECT id FROM {_TABLE} WHERE id = ?", (entry["id"],)
            ).fetchone()
            if row is not None:
                continue
            cur.execute(
                f"INSERT INTO {_TABLE} (id, rank, name, description, depends_on) "
                f"VALUES (?, ?, ?, ?, ?)",
                (entry["id"], entry["rank"], entry["name"], entry["description"], depends_json),
            )
            inserted += 1
    return inserted


def list_all() -> list[dict]:
    """Return all priorities ranked ascending (1 = first)."""
    with synaptic_db() as cur:
        _ensure_schema(cur)
        rows = cur.execute(f"SELECT * FROM {_TABLE} ORDER BY rank ASC").fetchall()
    return [dict(r) for r in rows]


def get(priority_id: str) -> dict | None:
    """Single entry by id, or None if missing."""
    with synaptic_db() as cur:
        _ensure_schema(cur)
        row = cur.execute(
            f"SELECT * FROM {_TABLE} WHERE id = ?", (priority_id,)
        ).fetchone()
    return dict(row) if row else None


def mark_reviewed(priority_id: str) -> bool:
    """Stamp last_reviewed_at = now. Returns True if updated."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with synaptic_db() as cur:
        _ensure_schema(cur)
        result = cur.execute(
            f"UPDATE {_TABLE} SET last_reviewed_at = ? WHERE id = ?",
            (now, priority_id),
        )
        return result.rowcount > 0


def overdue() -> list[dict]:
    """Priorities whose last review exceeds their review_cycle_days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    out: list[dict] = []
    for entry in list_all():
        last = entry.get("last_reviewed_at") or entry.get("created_at")
        try:
            last_dt = datetime.datetime.fromisoformat(last)
        except (TypeError, ValueError):
            continue
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
        age_days = (now - last_dt).total_seconds() / 86400
        if age_days >= entry["review_cycle_days"]:
            entry["age_days"] = round(age_days, 1)
            out.append(entry)
    return out


def _format_entry(e: dict) -> str:
    deps = json.loads(e.get("depends_on") or "[]")
    deps_str = " ← " + ", ".join(deps) if deps else " (foundation)"
    return f"  {e['rank']}. {e['name']}{deps_str}"


def _cmd_list() -> int:
    rows = list_all()
    if not rows:
        print("North Star is empty. Run: python3 -m memory.north_star seed")
        return 1
    print(f"North Star — {len(rows)} priorities (locked sequence):")
    for e in rows:
        print(_format_entry(e))
    return 0


def _cmd_get(priority_id: str) -> int:
    e = get(priority_id)
    if not e:
        print(f"Not found: {priority_id}", file=sys.stderr)
        return 2
    print(json.dumps(e, indent=2, default=str))
    return 0


def _cmd_status() -> int:
    rows = overdue()
    if not rows:
        print("All priorities reviewed within cycle. Nothing overdue.")
        return 0
    print(f"Overdue review ({len(rows)}):")
    for e in rows:
        print(f"  {e['id']}: {e['age_days']}d old (cycle={e['review_cycle_days']}d)")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="north_star", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show all priorities ranked")
    g = sub.add_parser("get", help="show one entry by id")
    g.add_argument("id")
    r = sub.add_parser("reviewed", help="mark a priority as reviewed")
    r.add_argument("id")
    sub.add_parser("status", help="which priorities are overdue for review")
    s = sub.add_parser("seed", help="insert locked sequence (idempotent)")
    s.add_argument("--force", action="store_true", help="re-seed by replacing rows")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "get":
        return _cmd_get(args.id)
    if args.cmd == "reviewed":
        ok = mark_reviewed(args.id)
        print("ok" if ok else f"not found: {args.id}", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 2
    if args.cmd == "status":
        return _cmd_status()
    if args.cmd == "seed":
        n = seed(force=args.force)
        print(f"seeded {n} priorit{'y' if n == 1 else 'ies'}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
