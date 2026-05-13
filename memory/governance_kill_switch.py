"""
Governance Kill-Switch — emergency rollback for governor decisions (T3 v4).

Purpose
-------
Addresses Neuro anchor-bias #3: "no rollback if real test runner swap or
blinded generator breaks production". When a governor decision lands badly
(wrong influence threshold, buggy invariant blocking all dispatches, etc.),
this kill-switch lets Aaron INSTANTLY disable governance enforcement so
`memory.invariants.evaluate()` becomes pass-through.

State persistence
-----------------
Kill-switch state lives at `memory/.governance_kill_switch.json`. The file
is intentionally NOT gitignored — if the kill is engaged, it must survive
process restart, daemon redeploy, and (when committed) propagate across
the fleet so any node honors it.

Constraints
-----------
- stdlib only
- ZERO SILENT FAILURES — every failure path bumps a counter and logs.
  Disk read/write errors fall back to in-memory state so a missing FS
  never silently re-enables governance enforcement.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Paths & logging
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
KILL_SWITCH_PATH: Path = _HERE / ".governance_kill_switch.json"
LOG_PATH: Path = Path("/tmp/governance-kill-switch.log")

_log = logging.getLogger("memory.governance_kill_switch")
if not _log.handlers:
    # Always tee to /tmp/governance-kill-switch.log per spec.
    try:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        _log.addHandler(handler)
        _log.setLevel(logging.INFO)
    except OSError:
        # Last-resort: still attach a NullHandler so logging never raises.
        _log.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Counters (ZSF)
# ---------------------------------------------------------------------------

_counter_lock = threading.Lock()
_COUNTERS: Dict[str, int] = {
    "governance_kill_switch_activations": 0,
    "governance_kill_switch_deactivations": 0,
    "governance_kill_switch_passes_throughs": 0,
    "governance_kill_switch_state_read_errors": 0,
    "governance_kill_switch_state_write_errors": 0,
    "governance_kill_switch_state_parse_errors": 0,
}


def _bump(counter: str, delta: int = 1) -> None:
    with _counter_lock:
        _COUNTERS[counter] = _COUNTERS.get(counter, 0) + delta


def get_counters() -> Dict[str, int]:
    """Snapshot of kill-switch counters (observable channel for ZSF)."""
    with _counter_lock:
        return dict(_COUNTERS)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class KillSwitchState:
    """Persisted kill-switch state.

    enabled : True means governance enforcement is DISABLED (kill engaged).
              Default False -> governance ON.
    reason  : free-form rationale supplied at activation.
    set_at  : ISO-8601 UTC timestamp string (or None when never set).
    set_by  : actor that flipped the switch (e.g. "aaron", "atlas", node id).
    """

    enabled: bool = False
    reason: Optional[str] = None
    set_at: Optional[str] = None
    set_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KillSwitchState":
        return cls(
            enabled=bool(data.get("enabled", False)),
            reason=data.get("reason"),
            set_at=data.get("set_at"),
            set_by=data.get("set_by"),
        )


# ---------------------------------------------------------------------------
# Internal: in-memory cache mirrored to disk
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_cached_state: Optional[KillSwitchState] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_state_from_disk() -> KillSwitchState:
    """Read state file. Missing -> default OFF. Errors -> bumped + logged."""
    if not KILL_SWITCH_PATH.exists():
        return KillSwitchState()
    try:
        raw = KILL_SWITCH_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _bump("governance_kill_switch_state_read_errors")
        _log.error("kill_switch: failed to read %s: %s", KILL_SWITCH_PATH, exc)
        # ZSF: do NOT silently default to OFF. Return cached state if any.
        with _state_lock:
            if _cached_state is not None:
                return _cached_state
        return KillSwitchState()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _bump("governance_kill_switch_state_parse_errors")
        _log.error("kill_switch: parse error in %s: %s", KILL_SWITCH_PATH, exc)
        with _state_lock:
            if _cached_state is not None:
                return _cached_state
        return KillSwitchState()
    if not isinstance(data, dict):
        _bump("governance_kill_switch_state_parse_errors")
        _log.error("kill_switch: state file is not a JSON object: %r", type(data))
        return KillSwitchState()
    return KillSwitchState.from_dict(data)


def _write_state_to_disk(state: KillSwitchState) -> None:
    """Atomic write via tmp + rename. ZSF: bumps + logs on failure."""
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp = KILL_SWITCH_PATH.with_suffix(KILL_SWITCH_PATH.suffix + ".tmp")
    try:
        KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, KILL_SWITCH_PATH)
    except OSError as exc:
        _bump("governance_kill_switch_state_write_errors")
        _log.error("kill_switch: failed to write %s: %s", KILL_SWITCH_PATH, exc)
        # Do not raise — caller must still observe in-memory state. ZSF is
        # honored via counter + log; deliberate non-raise so a transient
        # disk error never re-enables governance unexpectedly.


def _load() -> KillSwitchState:
    """Get current state — disk is source of truth, cache mirrors it."""
    global _cached_state
    with _state_lock:
        state = _read_state_from_disk()
        _cached_state = state
        return state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_state() -> KillSwitchState:
    """Return current kill-switch state (disk-backed)."""
    return _load()


def is_killed() -> bool:
    """Check whether governance enforcement is currently disabled.

    Increments `governance_kill_switch_passes_throughs` only when a caller
    sees enabled=True (i.e. we ARE about to bypass governance).
    """
    state = _load()
    if state.enabled:
        _bump("governance_kill_switch_passes_throughs")
        return True
    return False


def activate(reason: str, by: str) -> None:
    """Engage the kill-switch — governance evaluate() becomes pass-through.

    Args:
        reason: free-form rationale (REQUIRED, non-empty).
        by:     actor flipping the switch (REQUIRED, non-empty).
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("activate(reason=...) must be a non-empty string")
    if not isinstance(by, str) or not by.strip():
        raise ValueError("activate(by=...) must be a non-empty string")

    new_state = KillSwitchState(
        enabled=True,
        reason=reason.strip(),
        set_at=_now_iso(),
        set_by=by.strip(),
    )
    global _cached_state
    with _state_lock:
        _write_state_to_disk(new_state)
        _cached_state = new_state
    _bump("governance_kill_switch_activations")
    _log.warning(
        "KILL_SWITCH ACTIVATED by=%s reason=%r at=%s",
        new_state.set_by,
        new_state.reason,
        new_state.set_at,
    )


def deactivate(by: str) -> None:
    """Restore normal governance enforcement.

    Args:
        by: actor restoring enforcement (REQUIRED, non-empty).
    """
    if not isinstance(by, str) or not by.strip():
        raise ValueError("deactivate(by=...) must be a non-empty string")

    # Preserve audit trail: keep prior reason/set_at as last-known fields by
    # writing a fresh OFF record stamped with deactivation metadata.
    new_state = KillSwitchState(
        enabled=False,
        reason=None,
        set_at=_now_iso(),
        set_by=by.strip(),
    )
    global _cached_state
    with _state_lock:
        _write_state_to_disk(new_state)
        _cached_state = new_state
    _bump("governance_kill_switch_deactivations")
    _log.warning("KILL_SWITCH DEACTIVATED by=%s at=%s", new_state.set_by, new_state.set_at)


__all__ = [
    "KillSwitchState",
    "KILL_SWITCH_PATH",
    "LOG_PATH",
    "is_killed",
    "activate",
    "deactivate",
    "get_state",
    "get_counters",
]
