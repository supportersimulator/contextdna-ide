"""
Mission Envelope — typed multi-action autonomy contract above per-action governor.

Pipeline position (this module sits ABOVE permission_governor.py)::

    User Goal
        ↓
    [MissionEnvelope]   ← scope/TTL/rollback/evidence_required (this module)
        ↓
    ActionProposal       ← per-action gate (memory/invariants.py)
        ↓
    PermissionTier       ← per-agent gate (memory/permission_governor.py)
        ↓
    Outcome              ← outcome_governor ledger
        ↓
    Influence/Memory     ← future Q4+ consumers

Origin: MavKa diff doc (~/Downloads/OpenPath_Context-DNA-MavKa.md L1143-1167) +
E-batch convergent finding (E1, E2, E3 worth-adopting) + cross-node 3s green
on mac2 + mac1 chief.

Why this is NOT R5 (governedWriteFile wrappers from rejected-archive):
  - opt-in not wrapper-around-everything (callers explicitly create envelope
    when they want session-scoped delegation)
  - scope-explicit fields (scope_repos, scope_nodes, allowed_actions,
    forbidden_actions) — not a stealth permission grant
  - lives in shipped Python superrepo (NOT new TS scaffold per dao R1)
  - augments permission_governor (NOT replaces) — verdict from cross-node 3s

Why this is NOT R4 (surgeon influence-weighted voting):
  - envelope is a STATIC typed contract created once at session-start
  - surgeons are NOT consulted at action-time inside the envelope check
  - corrigibility-loop algorithm preserved: gate is invariants.evaluate(),
    not surgeon vote

Read-only contract:
  - Envelope state lives in caller; this module stores nothing globally
  - check_action() is pure: (envelope, proposal) -> EnvelopeDecision

ZERO SILENT FAILURES:
  - module-level COMPUTE_ERRORS dict daemon-scrapable
  - check_action() never raises; returns block-decision on internal error
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
from dataclasses import dataclass, field
from typing import List, Literal, Optional


_log = logging.getLogger("memory.mission_envelope")
if not _log.handlers:
    _log.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# ZSF counters
# ---------------------------------------------------------------------------

_counter_lock = threading.Lock()
COMPUTE_ERRORS: dict[str, int] = {
    "envelope_check_internal_error": 0,
    "envelope_expired": 0,
    "envelope_action_blocked": 0,
    "envelope_scope_violation": 0,
    "envelope_evidence_missing": 0,
    "envelope_rollback_missing": 0,
}


def _bump(counter: str) -> None:
    with _counter_lock:
        COMPUTE_ERRORS[counter] = COMPUTE_ERRORS.get(counter, 0) + 1


def get_counters() -> dict[str, int]:
    with _counter_lock:
        return dict(COMPUTE_ERRORS)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


# RiskLane — ORTHOGONAL action-axis (alongside agent-axis PermissionTier).
# Per E3 verdict: Risk Lane and PermissionTier are different dimensions:
#   - PermissionTier = WHO is acting (agent trust derived from outcomes)
#   - RiskLane = WHAT is being done (action's reversibility + blast radius)
# Both gates apply; neither replaces the other. dao locks this orthogonality.
RiskLane = Literal[0, 1, 2, 3, 4]

# Verdict on whether an action proposal is permitted by the envelope.
EnvelopeDecision = Literal[
    "allow",                # in-scope, all gates satisfied
    "allow_with_warning",   # in-scope but soft constraint borderline (e.g. evidence light)
    "block_out_of_scope",   # action outside scope_* lists
    "block_expired",        # envelope past expires_at
    "block_forbidden",      # action in forbidden_actions
    "block_lane_too_high",  # action's risk_lane > envelope.risk_lane_ceiling
    "block_evidence_missing",
    "block_rollback_missing",
    "block_internal_error",
]


@dataclass
class MissionEnvelope:
    """Typed multi-action autonomy contract.

    Constructed once at the start of a delegated session (or batch). Every
    subsequent ActionProposal is checked against this envelope BEFORE per-action
    invariants run. Out-of-scope actions block early without burning invariant
    cycles or surgeon calls.

    Source-pattern fields (originally proposed in MavKa diff doc):
      id              — unique envelope id (uuid4 prefix recommended)
      user_goal       — what the user actually asked for (one sentence)
      source          — channel that started the session (cli/ide/p7/api)
      scope_nodes     — fleet nodes this envelope is allowed to act on
      scope_repos     — repos this envelope is allowed to touch
      allowed_actions — action_class strings the envelope permits
      forbidden_actions — action_class strings the envelope explicitly forbids
                          (forbidden takes precedence over allowed)
      risk_lane_ceiling — max RiskLane permitted; actions above ceiling block
      rollback_required — when True, action must declare reversible=True
                          OR provide evidence of rollback path
      evidence_required — when True, action must have non-empty evidence_refs
      expires_at      — ISO-8601 timestamp; None means no expiry
                        (recommended to set even for long sessions; safety-net)
    """

    id: str
    user_goal: str
    source: str = "cli"
    scope_nodes: List[str] = field(default_factory=list)
    scope_repos: List[str] = field(default_factory=list)
    allowed_actions: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    risk_lane_ceiling: RiskLane = 2
    rollback_required: bool = False
    evidence_required: bool = False
    expires_at: Optional[str] = None  # ISO-8601 UTC

    # Optional metadata — kept open for caller annotations.
    rationale: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class EnvelopeReport:
    """Result of checking an ActionProposal against a MissionEnvelope."""

    decision: EnvelopeDecision
    reason: str = ""
    envelope_id: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _is_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        # Tolerate trailing 'Z' or explicit offset.
        clean = expires_at.replace("Z", "+00:00")
        ts = _dt.datetime.fromisoformat(clean)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return _dt.datetime.now(_dt.timezone.utc) >= ts
    except Exception as exc:
        _log.warning("mission_envelope: bad expires_at=%r: %s", expires_at, exc)
        # Treat unparseable expiry as expired (fail-closed).
        return True


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def check_action(
    envelope: MissionEnvelope,
    *,
    action_class: str,
    risk_lane: RiskLane = 0,
    target_node: Optional[str] = None,
    target_repo: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    reversible: Optional[bool] = None,
    rollback_evidence: Optional[List[str]] = None,
) -> EnvelopeReport:
    """Pure check — does this action fit the envelope's scope/TTL/policy?

    Caller passes ActionProposal-shaped fields. Returns EnvelopeReport.
    NEVER raises — internal errors return block_internal_error.
    """
    try:
        # Expiry first — fastest reject.
        if _is_expired(envelope.expires_at):
            _bump("envelope_expired")
            return EnvelopeReport(
                decision="block_expired",
                reason=f"envelope expired at {envelope.expires_at}",
                envelope_id=envelope.id,
            )

        # Forbidden takes precedence over allowed — explicit deny-list.
        if envelope.forbidden_actions and action_class in envelope.forbidden_actions:
            _bump("envelope_action_blocked")
            return EnvelopeReport(
                decision="block_forbidden",
                reason=f"action_class={action_class!r} in envelope.forbidden_actions",
                envelope_id=envelope.id,
            )

        # Allowed-list check — empty allowed_actions means "no whitelist; rely on
        # forbidden + risk_lane_ceiling alone". This is the common case.
        if envelope.allowed_actions and action_class not in envelope.allowed_actions:
            _bump("envelope_action_blocked")
            return EnvelopeReport(
                decision="block_out_of_scope",
                reason=f"action_class={action_class!r} not in envelope.allowed_actions",
                envelope_id=envelope.id,
            )

        # Risk lane ceiling — orthogonal to PermissionTier per dao.
        if risk_lane > envelope.risk_lane_ceiling:
            _bump("envelope_action_blocked")
            return EnvelopeReport(
                decision="block_lane_too_high",
                reason=f"risk_lane={risk_lane} > ceiling={envelope.risk_lane_ceiling}",
                envelope_id=envelope.id,
            )

        # Scope-node / scope-repo — empty list means "no scope restriction".
        if envelope.scope_nodes and target_node and target_node not in envelope.scope_nodes:
            _bump("envelope_scope_violation")
            return EnvelopeReport(
                decision="block_out_of_scope",
                reason=f"target_node={target_node!r} not in envelope.scope_nodes",
                envelope_id=envelope.id,
            )
        if envelope.scope_repos and target_repo and target_repo not in envelope.scope_repos:
            _bump("envelope_scope_violation")
            return EnvelopeReport(
                decision="block_out_of_scope",
                reason=f"target_repo={target_repo!r} not in envelope.scope_repos",
                envelope_id=envelope.id,
            )

        # Evidence requirement — if envelope.evidence_required, evidence_refs must be non-empty.
        if envelope.evidence_required and not (evidence_refs and len(evidence_refs) > 0):
            _bump("envelope_evidence_missing")
            return EnvelopeReport(
                decision="block_evidence_missing",
                reason="envelope.evidence_required=True but evidence_refs is empty",
                envelope_id=envelope.id,
            )

        # Rollback requirement — either reversible=True OR rollback_evidence non-empty.
        if envelope.rollback_required:
            has_rollback_path = (reversible is True) or bool(rollback_evidence)
            if not has_rollback_path:
                _bump("envelope_rollback_missing")
                return EnvelopeReport(
                    decision="block_rollback_missing",
                    reason="envelope.rollback_required=True but no reversible=True and no rollback_evidence",
                    envelope_id=envelope.id,
                )

        # All gates passed.
        return EnvelopeReport(
            decision="allow",
            reason="",
            envelope_id=envelope.id,
        )

    except Exception as exc:
        _bump("envelope_check_internal_error")
        _log.error("mission_envelope: check_action internal error: %s", exc)
        # Fail-closed.
        return EnvelopeReport(
            decision="block_internal_error",
            reason=f"internal: {exc}",
            envelope_id=envelope.id if envelope else "?",
        )
