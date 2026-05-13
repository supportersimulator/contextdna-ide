#!/usr/bin/env python3
"""Dump a cluster-wide status snapshot as JSON for the IDE Status Overview.

CC4 Phase-10 — Status Overview panel (2026-05-07).

Mirrors the pattern of ``scripts/dump-permission-snapshot.py`` and
``scripts/dump-truth-ladder-snapshot.py`` — the Next.js
``/api/cluster/status`` route reads the JSON written here and the
``StatusOverview.tsx`` pill bar renders a 6-pill summary at the top of
the IDE dashboard.

Behaviour
---------
1.  Probe ``http://127.0.0.1:8855/health`` (with retry guard) and pluck
    the cluster-health-relevant fields (NATS subs count, JS streams ok,
    webhook last-event age, surgeon backend statuses).
2.  Read ``FLEET_PUSH_FREEZE`` env (default ``"1"`` = freeze active).
3.  Count commits ahead of ``origin/main`` for the superrepo and the
    ``multi-fleet`` + ``admin.contextdna.io`` submodules via
    ``git rev-list --count origin/main..HEAD``.
4.  Locate the most recent constitutional-invariants log (in
    ``.fleet/audits/``) and parse "N/M PASS" out of it; if none is
    found, fall back to running ``constitutional-invariants.sh`` is
    *not* attempted here (that would be expensive — instead the
    snapshot reports ``unknown`` and the pill renders ``?/12``).
5.  Resolve the active phase from ``.fleet/active-phase`` (single-line
    file) or from a ``FLEET_ACTIVE_PHASE`` env override.
6.  Write JSON to ``dashboard_exports/cluster_status_snapshot.json``
    (override with ``--out`` or ``CLUSTER_STATUS_SNAPSHOT_JSON``).

Snapshot shape::

    {
      "schema_version":   "cluster_status/v1",
      "generated_at":     "2026-05-07T...",
      "active_phase":     "Phase-10 closeout",
      "cluster_health":   {
          "state":            "ok"|"degraded"|"down"|"unknown",
          "nats_subs":        123,
          "js_streams_ok":    true,
          "webhook_last_age_s": 14.2,
          "surgeons":         {"cardio": "ok", "neuro": "ok"}
      },
      "push_freeze":      {"active": true, "source": "env"},
      "commits_ahead":    {"super": 50, "mf": 95, "admin": 18, "total": 163},
      "invariants":       {"passed": 12, "total": 12, "last_run": "..."},
      "panels_live":      8,
      "source":           "snapshot"
    }

ZSF
---
Every error path bumps ``CLUSTER_STATUS_DUMP_ERRORS`` (a process-local
counter dict, mirroring ``flat_counters()`` from the permission /
tribunal / race scripts) and writes the counter snapshot to stderr at
exit. The script never raises uncaught — graceful empty payload always
written so the route never 500s.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent

_DEFAULT_OUT = _REPO_ROOT / "dashboard_exports" / "cluster_status_snapshot.json"
_HEALTH_URL = os.environ.get("FLEET_HEALTH_URL", "http://127.0.0.1:8855/health")
_HEALTH_TIMEOUT_S = float(os.environ.get("FLEET_HEALTH_TIMEOUT_S", "1.5"))
_HEALTH_RETRIES = int(os.environ.get("FLEET_HEALTH_RETRIES", "2"))
_PANELS_LIVE_DEFAULT = 8

# ZSF: monotonic, process-local. Logged to stderr on exit.
CLUSTER_STATUS_DUMP_ERRORS: dict[str, int] = {
    "health_probe": 0,
    "health_parse": 0,
    "git_super": 0,
    "git_mf": 0,
    "git_admin": 0,
    "invariants_log": 0,
    "phase_read": 0,
    "write": 0,
}


def _bump(stage: str) -> None:
    CLUSTER_STATUS_DUMP_ERRORS[stage] = CLUSTER_STATUS_DUMP_ERRORS.get(stage, 0) + 1


def _resolve_out(out: str | None) -> pathlib.Path:
    if out:
        return pathlib.Path(out).resolve()
    env = os.environ.get("CLUSTER_STATUS_SNAPSHOT_JSON")
    if env:
        return pathlib.Path(env).resolve()
    return _DEFAULT_OUT


def _atomic_write(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def _probe_health() -> dict[str, Any] | None:
    """Probe the fleet daemon /health endpoint with a small retry guard.

    Returns the decoded JSON payload, or None on every failure path.
    """
    last_err: str | None = None
    for attempt in range(_HEALTH_RETRIES + 1):
        try:
            req = urllib.request.Request(
                _HEALTH_URL, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT_S) as r:
                raw = r.read()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                _bump("health_parse")
                last_err = f"parse: {exc}"
                continue
            if isinstance(data, dict):
                return data
            _bump("health_parse")
            last_err = "shape"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _bump("health_probe")
            last_err = str(exc)
        if attempt < _HEALTH_RETRIES:
            time.sleep(0.15)
    if last_err is not None:
        sys.stderr.write(
            f"dump-cluster-status: /health probe failed: {last_err}\n"
        )
    return None


def _summarise_health(health: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(health, dict):
        return {
            "state": "unknown",
            "nats_subs": None,
            "js_streams_ok": None,
            "webhook_last_age_s": None,
            "surgeons": {"cardio": "unknown", "neuro": "unknown"},
        }

    nats = health.get("nats") if isinstance(health.get("nats"), dict) else {}
    nats_subs = nats.get("subscription_count") or nats.get("subs") or health.get(
        "nats_subscription_count"
    )
    if not isinstance(nats_subs, (int, float)):
        nats_subs = None

    js = health.get("jetstream") if isinstance(health.get("jetstream"), dict) else {}
    js_ok = js.get("ok")
    if js_ok is None:
        js_ok = js.get("streams_ok")
    if not isinstance(js_ok, bool):
        js_ok = None

    webhook = (
        health.get("webhook") if isinstance(health.get("webhook"), dict) else {}
    )
    webhook_age = (
        webhook.get("last_event_age_s")
        or webhook.get("last_age_s")
        or webhook.get("events_last_age_s")
    )
    if not isinstance(webhook_age, (int, float)):
        webhook_age = None

    surgeons_raw = (
        health.get("surgeons")
        if isinstance(health.get("surgeons"), dict)
        else {}
    )

    def _surgeon_status(node: Any) -> str:
        if isinstance(node, dict):
            s = node.get("status") or node.get("state")
            if isinstance(s, str) and s:
                return s
        if isinstance(node, str) and node:
            return node
        return "unknown"

    cardio = _surgeon_status(surgeons_raw.get("cardio"))
    neuro = _surgeon_status(surgeons_raw.get("neuro"))

    # Roll-up rule:
    #   ok        — every signal we observed reports ok
    #   degraded  — any signal degraded but daemon reachable
    #   down      — daemon unreachable (handled above, but also if every
    #               sub-signal is missing/unknown)
    #   unknown   — partial visibility
    if (
        nats_subs is None
        and js_ok is None
        and webhook_age is None
        and cardio == "unknown"
        and neuro == "unknown"
    ):
        state = "unknown"
    else:
        bad = (
            (isinstance(nats_subs, (int, float)) and nats_subs == 0)
            or js_ok is False
            or (
                isinstance(webhook_age, (int, float))
                and webhook_age is not None
                and webhook_age > 600
            )
            or cardio not in ("ok", "ready", "healthy", "unknown")
            or neuro not in ("ok", "ready", "healthy", "unknown")
        )
        state = "degraded" if bad else "ok"

    return {
        "state": state,
        "nats_subs": nats_subs,
        "js_streams_ok": js_ok,
        "webhook_last_age_s": webhook_age,
        "surgeons": {"cardio": cardio, "neuro": neuro},
    }


def _git_ahead(cwd: pathlib.Path, stage: str) -> int | None:
    """Return commits ahead of origin/main, or None on failure."""
    if not cwd.exists():
        _bump(stage)
        return None
    try:
        out = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _bump(stage)
        sys.stderr.write(f"dump-cluster-status: git_ahead {stage} failed: {exc}\n")
        return None
    if out.returncode != 0:
        _bump(stage)
        return None
    raw = (out.stdout or "").strip()
    try:
        return int(raw)
    except ValueError:
        _bump(stage)
        return None


_INVARIANT_PATTERN = re.compile(r"Constitutional invariants:\s+(\d+)\s*/\s*(\d+)\s+PASS")


def _read_invariants() -> dict[str, Any]:
    """Find the most recent constitutional-invariants log/audit and parse PASS counts.

    Searches ``.fleet/audits/*constitutional-invariants*`` (.log or .md) and
    picks the freshest by mtime. If nothing is found, returns ``unknown``.
    """
    audits_dir = _REPO_ROOT / ".fleet" / "audits"
    if not audits_dir.exists():
        _bump("invariants_log")
        return {"passed": None, "total": 12, "last_run": None}
    candidates = sorted(
        (
            p
            for p in audits_dir.iterdir()
            if p.is_file()
            and "constitutional-invariants" in p.name
            and p.suffix in (".log", ".md")
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        # Try BB5 style closeouts that cite invariants in body.
        candidates = sorted(
            (
                p
                for p in audits_dir.iterdir()
                if p.is_file()
                and p.suffix == ".md"
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
    for p in candidates:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            _bump("invariants_log")
            continue
        m = _INVARIANT_PATTERN.search(text)
        if m:
            try:
                passed = int(m.group(1))
                total = int(m.group(2))
            except ValueError:
                continue
            mtime = datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            return {"passed": passed, "total": total, "last_run": mtime}
    _bump("invariants_log")
    return {"passed": None, "total": 12, "last_run": None}


def _read_active_phase() -> str | None:
    env = os.environ.get("FLEET_ACTIVE_PHASE")
    if env and env.strip():
        return env.strip()
    phase_file = _REPO_ROOT / ".fleet" / "active-phase"
    if not phase_file.exists():
        return None
    try:
        line = phase_file.read_text().strip().splitlines()
        if line:
            return line[0].strip() or None
    except OSError as exc:
        _bump("phase_read")
        sys.stderr.write(f"dump-cluster-status: active-phase read failed: {exc}\n")
    return None


def build_snapshot() -> dict[str, Any]:
    health = _probe_health()
    cluster_health = _summarise_health(health)

    push_freeze_env = os.environ.get("FLEET_PUSH_FREEZE", "1").strip()
    push_freeze_active = push_freeze_env not in ("0", "false", "False", "")

    super_ahead = _git_ahead(_REPO_ROOT, "git_super")
    mf_ahead = _git_ahead(_REPO_ROOT / "multi-fleet", "git_mf")
    admin_ahead = _git_ahead(_REPO_ROOT / "admin.contextdna.io", "git_admin")
    total_ahead = sum(v for v in (super_ahead, mf_ahead, admin_ahead) if isinstance(v, int))

    invariants = _read_invariants()
    active_phase = _read_active_phase()

    payload: dict[str, Any] = {
        "schema_version": "cluster_status/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_phase": active_phase,
        "cluster_health": cluster_health,
        "push_freeze": {
            "active": push_freeze_active,
            "source": "env",
        },
        "commits_ahead": {
            "super": super_ahead,
            "mf": mf_ahead,
            "admin": admin_ahead,
            "total": total_ahead,
        },
        "invariants": invariants,
        "panels_live": _PANELS_LIVE_DEFAULT,
        "source": "snapshot" if health is not None else "snapshot-degraded",
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_path = _resolve_out(args.out)

    try:
        payload = build_snapshot()
    except Exception as exc:  # noqa: BLE001 — observable + graceful
        _bump("write")
        sys.stderr.write(f"dump-cluster-status: build failed: {exc}\n")
        payload = {
            "schema_version": "cluster_status/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "active_phase": None,
            "cluster_health": {
                "state": "unknown",
                "nats_subs": None,
                "js_streams_ok": None,
                "webhook_last_age_s": None,
                "surgeons": {"cardio": "unknown", "neuro": "unknown"},
            },
            "push_freeze": {"active": True, "source": "env"},
            "commits_ahead": {
                "super": None,
                "mf": None,
                "admin": None,
                "total": 0,
            },
            "invariants": {"passed": None, "total": 12, "last_run": None},
            "panels_live": _PANELS_LIVE_DEFAULT,
            "source": "error",
            "error": str(exc)[:200],
        }

    try:
        _atomic_write(out_path, payload)
    except OSError as exc:
        _bump("write")
        sys.stderr.write(f"dump-cluster-status: write failed: {exc}\n")
        sys.stderr.write(f"counters: {CLUSTER_STATUS_DUMP_ERRORS!r}\n")
        return 3

    if not args.quiet:
        print(json.dumps({
            "snapshot_path": str(out_path),
            "payload": payload,
            "counters": CLUSTER_STATUS_DUMP_ERRORS,
        }, indent=2, sort_keys=True))
    sys.stderr.write(f"counters: {CLUSTER_STATUS_DUMP_ERRORS!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
