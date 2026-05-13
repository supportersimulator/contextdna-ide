#!/usr/bin/env python3
"""Panel-manifest migration runner.

Walks the migration registry under ``schemas/migrations/`` to upgrade a
panel.manifest.json from its declared ``schema_version`` to a target version,
then validates the result against the target JSON Schema.

Migrations are declarative records (see ``schemas/migrations/*.json``) composed
of four operations: ``add_field``, ``rename_field``, ``move_field``,
``remove_field``. Each is idempotent and uses literal dot/bracket path
selectors (no wildcards, no JSONPath). See ``docs/panels/manifest-spec.md``
section 10 for the full contract.

Exit codes:
    0  success (or no-op when already at target)
    2  CLI / usage error
    3  manifest unreadable or invalid JSON
    4  migration chain gap (cannot reach target from source)
    5  malformed migration record (unknown op, missing keys, conflict)
    6  post-migration schema validation failed

Zero Silent Failures: every failure mode surfaces a typed message on stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import jsonschema  # type: ignore
except ImportError:  # pragma: no cover - optional dep, validated below
    jsonschema = None  # noqa: N816 — preserve module name for runtime check

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
OP_KEYS = {
    "add_field": {"path", "default"},
    "rename_field": {"from", "to"},
    "move_field": {"from", "to"},
    "remove_field": {"path"},
}


def parse_semver(v: str) -> tuple[int, int, int]:
    if not SEMVER_RE.match(v):
        raise ValueError(f"invalid semver: {v!r}")
    major, minor, patch = (int(x) for x in v.split("."))
    return major, minor, patch


def split_path(selector: str) -> list[str]:
    """Split a dot-and-bracket path into literal keys. No wildcards."""
    if not selector or selector.startswith(".") or selector.endswith("."):
        raise ValueError(f"invalid path selector: {selector!r}")
    parts = selector.split(".")
    if any(not p for p in parts):
        raise ValueError(f"invalid path selector (empty segment): {selector!r}")
    return parts


def get_path(doc: Any, parts: list[str]) -> tuple[bool, Any]:
    cur = doc
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return False, None
        cur = cur[p]
    return True, cur


def set_path(doc: dict, parts: list[str], value: Any) -> None:
    cur = doc
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def del_path(doc: dict, parts: list[str]) -> bool:
    cur = doc
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    if isinstance(cur, dict) and parts[-1] in cur:
        del cur[parts[-1]]
        return True
    return False


def apply_op(doc: dict, op: dict) -> None:
    kind = op.get("op")
    if kind not in OP_KEYS:
        raise ValueError(f"unknown op: {kind!r}")
    missing = OP_KEYS[kind] - set(op.keys())
    if missing:
        raise ValueError(f"op {kind!r} missing required keys: {sorted(missing)}")

    if kind == "add_field":
        parts = split_path(op["path"])
        present, _ = get_path(doc, parts)
        if not present:
            set_path(doc, parts, op["default"])
    elif kind == "rename_field" or kind == "move_field":
        src = split_path(op["from"])
        dst = split_path(op["to"])
        present, value = get_path(doc, src)
        if not present:
            return  # idempotent: source missing -> no-op
        dst_present, _ = get_path(doc, dst)
        if dst_present:
            raise ValueError(
                f"{kind} conflict: destination {op['to']!r} already exists"
            )
        del_path(doc, src)
        set_path(doc, dst, value)
    elif kind == "remove_field":
        del_path(doc, split_path(op["path"]))


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"PanelManifestUnreadable: {path}: {e}") from e


def discover_migrations(schema_dir: Path) -> dict[tuple[str, str], Path]:
    """Return mapping (from, to) -> migration record path. Skips *.example."""
    out: dict[tuple[str, str], Path] = {}
    mig_dir = schema_dir / "migrations"
    if not mig_dir.is_dir():
        return out
    for p in sorted(mig_dir.glob("*-to-*.json")):
        # Skip examples / templates
        if p.name.endswith(".json.example") or ".example." in p.name:
            continue
        m = re.match(r"^(\d+\.\d+\.\d+)-to-(\d+\.\d+\.\d+)\.json$", p.name)
        if not m:
            continue
        out[(m.group(1), m.group(2))] = p
    return out


def discover_target(schema_dir: Path) -> str | None:
    versions: list[tuple[tuple[int, int, int], str]] = []
    for p in schema_dir.glob("panel-manifest-v*.schema.json"):
        m = re.match(r"^panel-manifest-v(\d+\.\d+\.\d+)\.schema\.json$", p.name)
        if m:
            versions.append((parse_semver(m.group(1)), m.group(1)))
    if not versions:
        return None
    versions.sort()
    return versions[-1][1]


def resolve_chain(
    src: str, dst: str, migrations: dict[tuple[str, str], Path]
) -> list[Path]:
    """Walk linear chain from src to dst. Branches/gaps surface as errors."""
    if src == dst:
        return []
    chain: list[Path] = []
    cur = src
    seen = {src}
    while cur != dst:
        nexts = [(f, t) for (f, t) in migrations if f == cur]
        if not nexts:
            raise SystemExit(
                f"PanelMigrationGap: no migration record from {cur!r} "
                f"(target {dst!r}). Expected file: "
                f"schemas/migrations/{cur}-to-<next>.json"
            )
        if len(nexts) > 1:
            raise SystemExit(
                f"PanelMigrationBranch: multiple migrations leave {cur!r}: "
                f"{sorted(t for _, t in nexts)}. The registry must be linear."
            )
        nxt = nexts[0][1]
        if nxt in seen:
            raise SystemExit(f"PanelMigrationCycle: cycle detected at {nxt!r}")
        seen.add(nxt)
        chain.append(migrations[(cur, nxt)])
        cur = nxt
    return chain


def apply_migration(doc: dict, record_path: Path) -> dict:
    record = load_json(record_path)
    src_v = record.get("schema_version_from")
    dst_v = record.get("schema_version_to")
    ops = record.get("operations", [])
    if not (src_v and dst_v and isinstance(ops, list)):
        raise SystemExit(f"PanelMigrationMalformed: {record_path}")
    if doc.get("schema_version") != src_v:
        raise SystemExit(
            f"PanelMigrationMismatch: record {record_path.name} expects "
            f"schema_version={src_v!r} but manifest is "
            f"{doc.get('schema_version')!r}"
        )
    for op in ops:
        if not isinstance(op, dict):
            raise SystemExit(f"PanelMigrationMalformed: non-object op in {record_path}")
        try:
            apply_op(doc, op)
        except ValueError as e:
            raise SystemExit(f"PanelMigrationMalformed: {record_path.name}: {e}") from e
    doc["schema_version"] = dst_v
    return doc


def validate_against_schema(doc: dict, schema_dir: Path, version: str) -> None:
    schema_path = schema_dir / f"panel-manifest-v{version}.schema.json"
    if not schema_path.is_file():
        raise SystemExit(f"PanelSchemaMissing: {schema_path}")
    if jsonschema is None:
        sys.stderr.write(
            "warning: `jsonschema` not installed; skipping post-migration "
            "validation. Install via `pip install jsonschema` to enforce.\n"
        )
        return
    schema = load_json(schema_path)
    try:
        jsonschema.validate(instance=doc, schema=schema)  # type: ignore[arg-type]
    except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
        raise SystemExit(
            f"PanelManifestValidationFailed: target schema {version}: {e.message}"
        ) from e


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Migrate a panel.manifest.json to a target schema_version."
    )
    ap.add_argument("manifest", type=Path, help="Path to panel.manifest.json")
    ap.add_argument("--target", help="Target schema_version (e.g. 1.1.0). "
                    "Defaults to the latest schema present under --schema-dir.")
    ap.add_argument(
        "--schema-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "schemas",
        help="Directory containing panel-manifest-v*.schema.json and migrations/",
    )
    ap.add_argument("--out", type=Path, help="Write to this path (default: in-place).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the result to stdout; do not write.")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if not args.manifest.is_file():
        raise SystemExit(f"PanelManifestUnreadable: not a file: {args.manifest}")
    if not args.schema_dir.is_dir():
        raise SystemExit(f"PanelSchemaDirMissing: {args.schema_dir}")

    doc = load_json(args.manifest)
    if not isinstance(doc, dict):
        raise SystemExit("PanelManifestUnreadable: root is not an object")
    src_v = doc.get("schema_version")
    if not isinstance(src_v, str) or not SEMVER_RE.match(src_v):
        raise SystemExit(
            "PanelManifestSchemaVersionMissing: manifest must declare "
            "`schema_version` as a semver string (e.g. \"1.0.0\")."
        )

    target = args.target or discover_target(args.schema_dir)
    if not target:
        raise SystemExit(f"PanelSchemaDirEmpty: no schemas in {args.schema_dir}")
    if not SEMVER_RE.match(target):
        raise SystemExit(f"PanelTargetInvalid: {target!r} is not semver")

    migrations = discover_migrations(args.schema_dir)
    chain = resolve_chain(src_v, target, migrations)

    for record_path in chain:
        doc = apply_migration(doc, record_path)

    validate_against_schema(doc, args.schema_dir, target)

    text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
    if args.dry_run:
        sys.stdout.write(text)
    else:
        out_path = args.out or args.manifest
        out_path.write_text(text, encoding="utf-8")
        sys.stderr.write(
            f"migrated {args.manifest} from {src_v} to {target} "
            f"({len(chain)} step{'s' if len(chain) != 1 else ''}) -> {out_path}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
