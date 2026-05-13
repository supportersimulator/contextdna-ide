#!/usr/bin/env python3
"""profile-resolve.py — resolve a layered extensible profile into a single
docker-compose-format YAML document.

Usage:
    python migrate2/scripts/profile-resolve.py <profile-name> [-o output.yml]

Where <profile-name> is the basename (no .yaml suffix) of a file in
migrate2/profiles/. The resolver walks `extends:` to the root, merges
services, applies overrides LIFO (root-first, leaf-last), stitches in
extensions, and prints the result to stdout (or to -o).

See migrate2/profiles/README.md for the full inheritance contract.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

import yaml

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"
MIN_SCHEMA = (1, 0, 0)


def _parse_semver(s: str) -> tuple[int, int, int]:
    parts = s.split(".")
    if len(parts) != 3:
        raise ValueError(f"not semver: {s!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _load(name: str) -> dict[str, Any]:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"profile not found: {path}")
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    sv = _parse_semver(data.get("schema_version", "0.0.0"))
    if sv < MIN_SCHEMA:
        raise ValueError(
            f"{name}.yaml schema_version {sv} < resolver min {MIN_SCHEMA}"
        )
    return data


def _chain(name: str) -> list[dict[str, Any]]:
    """Return profiles from leaf to root."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    cur: str | None = name
    while cur is not None:
        if cur in seen:
            raise ValueError(f"cycle in extends: chain at {cur}")
        seen.add(cur)
        prof = _load(cur)
        out.append(prof)
        cur = prof.get("extends")
    return out


def _deep_merge(base: Any, top: Any) -> Any:
    """Deep merge `top` onto `base`. Maps merge; lists append; scalars replace."""
    if isinstance(base, dict) and isinstance(top, dict):
        out = dict(base)
        for k, v in top.items():
            out[k] = _deep_merge(out.get(k), v) if k in out else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(top, list):
        return list(base) + copy.deepcopy(top)
    return copy.deepcopy(top) if top is not None else copy.deepcopy(base)


def _merge_services(chain_root_first: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge `services:` blocks, then resolve per-service `extends:`."""
    merged: dict[str, Any] = {}
    for prof in chain_root_first:
        svcs = prof.get("services") or {}
        if isinstance(svcs, list):  # tolerate legacy heavy/lite list form
            svcs = {entry["name"]: {k: v for k, v in entry.items() if k != "name"}
                    for entry in svcs}
        for name, body in svcs.items():
            merged[name] = _deep_merge(merged.get(name, {}), body or {})
    # Per-service extends: resolve after global merge so the named base exists.
    resolved: dict[str, Any] = {}
    for name, body in merged.items():
        base_name = body.get("extends")
        if base_name and base_name in merged:
            inherited = copy.deepcopy(merged[base_name])
            body = _deep_merge(inherited, {k: v for k, v in body.items() if k != "extends"})
        resolved[name] = body
    # Drop services explicitly disabled.
    return {n: b for n, b in resolved.items() if b.get("enabled", True)}


def _apply_overrides(services: dict[str, Any], chain_root_first: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(services)
    for prof in chain_root_first:  # root first, leaf last — leaf wins ties
        for ov in prof.get("overrides") or []:
            tgt = ov.get("service")
            if tgt and tgt in out:
                out[tgt] = _deep_merge(out[tgt], ov.get("patch") or {})
    return out


def _stitch_extensions(services: dict[str, Any], chain_root_first: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(services)
    for prof in chain_root_first:
        for name, body in (prof.get("extensions") or {}).items():
            if name in out:
                raise ValueError(f"extension {name!r} collides with inherited service")
            if (body or {}).get("enabled", True):
                out[name] = copy.deepcopy(body)
    return out


def _to_compose(services: dict[str, Any]) -> dict[str, Any]:
    """Project resolved profile services into docker-compose format."""
    compose_services: dict[str, Any] = {}
    for name, body in services.items():
        svc: dict[str, Any] = {}
        if "image" in body:
            svc["image"] = body["image"]
        if "build" in body:
            svc["build"] = body["build"]
        if body.get("env"):
            svc["environment"] = body["env"]
        if body.get("ports"):
            svc["ports"] = [
                f"{p.get('bind', '0.0.0.0')}:{p['host']}:{p.get('container', p['host'])}"
                for p in body["ports"]
            ]
        if body.get("volumes"):
            svc["volumes"] = [
                f"{v['source']}:{v['target']}" for v in body["volumes"]
            ]
        compose_services[name] = svc
    return {"services": compose_services}


def resolve(name: str) -> dict[str, Any]:
    chain_leaf_first = _chain(name)
    chain_root_first = list(reversed(chain_leaf_first))
    services = _merge_services(chain_root_first)
    services = _apply_overrides(services, chain_root_first)
    services = _stitch_extensions(services, chain_root_first)
    return _to_compose(services)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile", help="profile name (no .yaml suffix)")
    ap.add_argument("-o", "--output", help="write to file instead of stdout")
    args = ap.parse_args()
    try:
        compose = resolve(args.profile)
    except (FileNotFoundError, ValueError) as e:
        print(f"profile-resolve: {e}", file=sys.stderr)
        return 2
    out_yaml = yaml.safe_dump(compose, sort_keys=False)
    if args.output:
        Path(args.output).write_text(out_yaml)
    else:
        sys.stdout.write(out_yaml)
    return 0


if __name__ == "__main__":
    sys.exit(main())
