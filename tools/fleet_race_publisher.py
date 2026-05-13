"""Race-event publisher — Y1 Race Theater backend (2026-05-07).

Given a 3-surgeon brainstorm result (JSON file produced by
``scripts/3s-answer-brainstorm.py``), this module:

  1. Converts it to a :class:`multifleet.race_events.RaceEntry`.
  2. Persists / merges it into the dashboard snapshot
     ``dashboard_exports/race_events_snapshot.json``. Idempotent on
     ``race_id`` — re-publishing the same race overwrites its prior entry.
  3. Publishes a ``race.event.<race_id>.<phase>`` NATS message via the
     existing ``WebhookHealthPublisher`` transport pattern (lazy NATS
     connection on a daemon thread, fire-and-forget, ZSF-counted).

Used both:

* As a CLI: ``python3 -m tools.fleet_race_publisher publish --in <file>``
  (or ``--stdin``) — opt-in hook called from
  ``scripts/3s-answer-brainstorm.py`` when ``RACE_PUBLISH=1``.
* As a library: ``publish_loop_result(loop_result, race_id)`` from any
  Python caller.

ZSF posture
-----------
* Every failure path increments a counter in
  :func:`multifleet.race_events.counters_snapshot`.
* The CLI exits 0 on dispatch (snapshot written), 2 on transport-missing
  (snapshot still written so the IDE keeps rendering), 1 on argparse /
  read error.
* Snapshot writes are atomic (tmpfile + os.replace).

Reversibility
-------------
``rm dashboard_exports/race_events_snapshot.json`` restores the empty
state. The route already handles missing snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

# Allow running both as a module and as a direct script.
_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent  # tools/.. -> superrepo
if str(_REPO_ROOT / "multi-fleet") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "multi-fleet"))

from multifleet.race_events import (  # noqa: E402  — sys.path side effect above
    RaceEntry,
    _bump,
    counters_snapshot,
    from_3s_loop_result,
    race_subject_for,
    serialize_race_entry,
    serialize_race_status_response,
)

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT = _REPO_ROOT / "dashboard_exports" / "race_events_snapshot.json"
SCHEMA_VERSION = "race_events_snapshot/v1"
SNAPSHOT_RETAIN = 30  # cap entries so the file never grows unbounded


# ── Snapshot I/O (mirrors dump-evidence-ledger-summary.py) ────────────────


def _resolve_snapshot_path(arg: Optional[str]) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg).resolve()
    env = os.environ.get("RACE_EVENTS_SNAPSHOT_JSON")
    if env:
        return pathlib.Path(env).resolve()
    return DEFAULT_SNAPSHOT


def _atomic_write(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".race_events_snapshot.", suffix=".json.tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_snapshot(path: pathlib.Path) -> Dict[str, Any]:
    """Load existing snapshot or return a fresh skeleton.

    ZSF: parse failures count + return skeleton (don't crash).
    """
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "races": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "races" not in data:
            _bump("race_event_serialize_errors_total")
            return {"schema_version": SCHEMA_VERSION, "races": []}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        _bump("race_event_serialize_errors_total")
        logger.warning("fleet_race_publisher: snapshot read failed (%s): %s", path, exc)
        return {"schema_version": SCHEMA_VERSION, "races": []}


def _merge_snapshot(snapshot: Dict[str, Any], new_entry_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `new_entry_dict` into `snapshot["races"]`, idempotent on race_id.

    The most-recent occurrence of a race_id wins; older copies are dropped.
    Newest-first ordering. List capped at SNAPSHOT_RETAIN.
    """
    races: List[Dict[str, Any]] = list(snapshot.get("races") or [])
    rid = new_entry_dict.get("race_id")
    if rid:
        races = [r for r in races if r.get("race_id") != rid]
    races.insert(0, new_entry_dict)
    if len(races) > SNAPSHOT_RETAIN:
        races = races[:SNAPSHOT_RETAIN]
    snapshot["races"] = races
    snapshot["schema_version"] = SCHEMA_VERSION
    snapshot["generated_at"] = new_entry_dict.get("updated_at") or _now_iso()
    snapshot["counters"] = counters_snapshot()
    return snapshot


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── NATS publish (single-shot, ZSF) ───────────────────────────────────────


_loop_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None


def _resolve_nats_url() -> str:
    return os.environ.get("NATS_URL") or "nats://127.0.0.1:4222"


def _resolve_node_id() -> str:
    env = os.environ.get("MULTIFLEET_NODE_ID")
    if env:
        return env
    try:
        from multifleet.fleet_config import get_node_id  # type: ignore

        nid = get_node_id()
        if nid:
            return nid
    except Exception as exc:  # noqa: BLE001
        logger.debug("fleet_race_publisher: get_node_id failed: %s", exc)
    return socket.gethostname().split(".")[0] or "unknown"


def _ensure_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Spin up a single asyncio loop on a daemon thread (or reuse)."""
    global _loop, _loop_thread
    if _loop is not None and not _loop.is_closed():
        return _loop
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        ready = threading.Event()
        holder: Dict[str, Any] = {}

        def _run() -> None:
            try:
                lp = asyncio.new_event_loop()
                holder["loop"] = lp
                asyncio.set_event_loop(lp)
                ready.set()
                lp.run_forever()
            except Exception as exc:  # noqa: BLE001
                logger.warning("fleet_race_publisher: loop crashed: %s", exc)
                ready.set()

        t = threading.Thread(target=_run, name="fleet-race-publisher-loop",
                             daemon=True)
        t.start()
        ready.wait(timeout=2.0)
        _loop_thread = t
        _loop = holder.get("loop")
        return _loop


# Module-level last-error slot — Atlas/IDE can surface the most recent
# transport failure reason without scraping logs. ZSF: every error path
# both bumps a reason-tagged counter AND updates this string under lock.
_last_error_lock = threading.Lock()
_last_publish_error: Optional[str] = None


def _record_publish_error(reason: str, exc: Optional[BaseException] = None) -> None:
    """Bump umbrella + reason counters, set last-error string, log at WARNING.

    Reasons are short stable tokens (``nats_py_missing``, ``connect``,
    ``publish``, ``loop``, ``result_wait``) so dashboards / gains-gate can
    pivot on them without parsing free-form messages.
    """
    global _last_publish_error
    _bump("race_events_publish_errors_total")
    _bump(f"race_events_publish_errors_{reason}_total")
    if exc is not None:
        msg = f"{reason}: {type(exc).__name__}: {exc}"
    else:
        msg = reason
    with _last_error_lock:
        _last_publish_error = msg
    # Lift visibility to WARNING — Y1 had these at DEBUG, which meant the
    # *reason* a publish failed was invisible by default (counter bumped
    # but no message reached stderr). ZSF: observable, not silent.
    logger.warning("fleet_race_publisher: publish error (%s)", msg)


def _last_publish_error_snapshot() -> Optional[str]:
    """Return the most recent publish-error message (None if no errors)."""
    with _last_error_lock:
        return _last_publish_error


async def _publish_async(nats_url: str, subject: str, data: bytes) -> bool:
    """Connect (lazy) and publish once. Returns True on send."""
    try:
        import nats  # type: ignore
    except ImportError as exc:
        _record_publish_error("nats_py_missing", exc)
        return False
    client = None
    try:
        client = await asyncio.wait_for(
            nats.connect(nats_url, name="fleet-race-publisher"),
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        _record_publish_error("connect", exc)
        return False
    try:
        await client.publish(subject, data)
        await client.flush(timeout=2.0)
        _bump("race_events_published_total")
        return True
    except Exception as exc:  # noqa: BLE001
        _record_publish_error("publish", exc)
        return False
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.drain(), timeout=2.0)
            except Exception:  # noqa: BLE001 — drain best-effort
                pass


def publish_to_nats(entry: RaceEntry, *, wait_s: float = 3.0) -> bool:
    """Fire-and-forget publish for one race entry.

    Schedules a one-shot connect+publish on the background loop. Blocks up
    to ``wait_s`` seconds so the dispatch completes before the caller exits
    (keeps the CLI deterministic).

    Default ``wait_s`` (3.0s) is intentionally larger than the inner
    connect (2.0s) + flush (2.0s) timeouts so a true connect-refused path
    surfaces ``connect`` as the reason rather than being shadowed by the
    outer ``result_wait`` future-timeout. ZSF: keep the reason specific.
    """
    loop = _ensure_loop()
    if loop is None or loop.is_closed():
        _record_publish_error("loop")
        return False

    # Determine phase from race status (publish granularity = race lifecycle).
    phase = entry.status or "update"
    subject = race_subject_for(entry.race_id, phase)

    payload = serialize_race_entry(entry)
    payload["_publisher_node"] = _resolve_node_id()
    try:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _bump("race_event_serialize_errors_total")
        logger.warning("fleet_race_publisher: payload encode failed: %s", exc)
        return False

    fut = asyncio.run_coroutine_threadsafe(
        _publish_async(_resolve_nats_url(), subject, data),
        loop,
    )
    try:
        return bool(fut.result(timeout=max(0.5, wait_s)))
    except Exception as exc:  # noqa: BLE001 — record + count, never raise
        _record_publish_error("result_wait", exc)
        return False


# ── High-level helpers ────────────────────────────────────────────────────


def publish_loop_result(
    loop_result: Dict[str, Any],
    race_id: Optional[str] = None,
    snapshot_path: Optional[pathlib.Path] = None,
    skip_nats: bool = False,
) -> Dict[str, Any]:
    """Convert + persist + (optionally) publish.

    Returns a dict with::

        {
          "race_id": "...",
          "snapshot_path": "...",
          "snapshot_written": True,
          "nats_dispatched": True/False,
          "counters": {...},
        }

    ZSF: snapshot write failures still produce a result dict (with
    ``snapshot_written=False``) and bump
    ``race_event_serialize_errors_total``.
    """
    rid = race_id or loop_result.get("race_id") or _derive_race_id(loop_result)

    try:
        entry = from_3s_loop_result(loop_result, rid)
    except Exception as exc:  # noqa: BLE001 — surface via counter
        _bump("race_event_from_loop_errors_total")
        logger.warning("fleet_race_publisher: convert failed: %s", exc)
        return {
            "race_id": rid,
            "snapshot_path": None,
            "snapshot_written": False,
            "nats_dispatched": False,
            "counters": counters_snapshot(),
            "error": f"convert: {exc}",
        }

    entry_dict = serialize_race_entry(entry)
    path = snapshot_path or _resolve_snapshot_path(None)

    # ZSF: publish FIRST so the counter snapshot we persist reflects the
    # publish outcome (success bumps ``race_events_published_total``,
    # failure bumps the reason-tagged counter). Y1's original order
    # captured counters BEFORE publish, so the on-disk ``counters`` block
    # always read 0 even on success — observability gap fixed.
    nats_ok = False
    if not skip_nats:
        nats_ok = publish_to_nats(entry)

    snapshot = _load_snapshot(path)
    snapshot = _merge_snapshot(snapshot, entry_dict)

    snapshot_written = False
    try:
        _atomic_write(path, snapshot)
        snapshot_written = True
    except OSError as exc:
        _bump("race_event_serialize_errors_total")
        logger.warning("fleet_race_publisher: snapshot write failed: %s", exc)

    result: Dict[str, Any] = {
        "race_id": rid,
        "snapshot_path": str(path),
        "snapshot_written": snapshot_written,
        "nats_dispatched": nats_ok,
        "counters": counters_snapshot(),
    }
    # ZSF: when NATS dispatch failed, include the human-readable reason so
    # callers (3s brainstorm hook, IDE) can surface why instead of seeing
    # a bare ``nats_dispatched=false``. Only present on failure path —
    # forward-compat additive field.
    if not skip_nats and not nats_ok:
        last = _last_publish_error_snapshot()
        if last:
            result["last_publish_error"] = last
    return result


def _derive_race_id(loop_result: Dict[str, Any]) -> str:
    """Synthesize a stable race_id from the loop result.

    Format: ``3s-<started_utc>``. Stable across re-runs of the same loop,
    so re-publishing is idempotent.
    """
    started = loop_result.get("started_utc") or "unknown"
    return f"3s-{started}"


# ── CLI entry point ───────────────────────────────────────────────────────


def _cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.fleet_race_publisher",
        description="Convert a 3s brainstorm loop result to a Race Theater "
                    "snapshot entry + publish race.event.<race_id>.<phase> via NATS.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="Publish a race entry from a 3s loop result.")
    p_pub.add_argument(
        "--in",
        dest="input",
        help="Path to 3s-answer-brainstorm-*.json. Use '-' or omit + --stdin "
             "for stdin.",
    )
    p_pub.add_argument(
        "--stdin", action="store_true",
        help="Read JSON from stdin even when --in is unset.",
    )
    p_pub.add_argument("--race-id", default=None,
                       help="Override race_id (default: '3s-<started_utc>').")
    p_pub.add_argument("--snapshot", default=None,
                       help="Override snapshot path (default: "
                            "dashboard_exports/race_events_snapshot.json).")
    p_pub.add_argument("--skip-nats", action="store_true",
                       help="Snapshot only; don't publish to NATS.")
    p_pub.add_argument("--wait-s", type=float, default=3.0,
                       help="Block this long for NATS dispatch (default 3.0s — "
                            "must exceed inner connect/flush timeouts so the "
                            "specific failure reason surfaces).")
    p_pub.add_argument("--quiet", action="store_true")

    args = parser.parse_args(argv)

    raw: str
    if args.input and args.input != "-":
        try:
            raw = pathlib.Path(args.input).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: read --in failed: {exc}", file=sys.stderr)
            return 1
    elif args.stdin or args.input == "-":
        raw = sys.stdin.read()
    else:
        print("ERROR: pass --in <path> or --stdin", file=sys.stderr)
        return 1

    try:
        loop_result = json.loads(raw)
    except json.JSONDecodeError as exc:
        _bump("race_event_serialize_errors_total")
        print(f"ERROR: input not valid JSON: {exc}", file=sys.stderr)
        return 1

    snapshot_path = (
        pathlib.Path(args.snapshot).resolve() if args.snapshot
        else _resolve_snapshot_path(None)
    )

    # When --skip-nats, don't bother spinning up the asyncio loop.
    if args.skip_nats:
        result = publish_loop_result(
            loop_result, race_id=args.race_id,
            snapshot_path=snapshot_path, skip_nats=True,
        )
    else:
        # Brief settle so the daemon loop is up before we publish.
        time.sleep(0.05)
        result = publish_loop_result(
            loop_result, race_id=args.race_id,
            snapshot_path=snapshot_path, skip_nats=False,
        )
        # Give NATS drain headroom.
        if args.wait_s > 0:
            time.sleep(min(0.5, args.wait_s))

    if not args.quiet:
        print(json.dumps(result, indent=2, sort_keys=True))

    if not result.get("snapshot_written"):
        return 1
    if not args.skip_nats and not result.get("nats_dispatched"):
        # ZSF: distinct exit so callers can tell snapshot-only from full publish.
        return 2
    return 0


__all__ = [
    "DEFAULT_SNAPSHOT",
    "SCHEMA_VERSION",
    "publish_loop_result",
    "publish_to_nats",
]


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(_cli_main(sys.argv[1:]))
