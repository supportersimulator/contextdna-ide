"""
CTXDNA Constitutional Invariants — Python (v0 TrialBench).

Original seed: an external TS scaffold (see ~/Downloads/contextdna-outcome-
governor-trialbench/src/invariants/) was the brainstorming starting point.
This module is the *normative* registry — Python is the canonical surface;
the TS reference is no longer load-bearing. Spec'd CTXDNA-INV-001..020 from
that origin and ADDS CTXDNA-INV-021 ("Trial protocol must be locked") for
v0 TrialBench dispatch. (Per D-batch corrigibility analysis 2026-05-06:
chat-residue path reference removed; this comment is the trail.)

Design:
    - stdlib only (no new pip deps)
    - ZERO SILENT FAILURES — every failure path increments a counter and
      records the reason. `evaluate()` never raises on internal failures.
    - Schema-version gated. SCHEMA_VERSION mismatch -> hard block.
    - INV-021 reads docs/dao/trialbench-protocol.lock.json (absence is
      treated as "no protocols locked yet" -> only proposals that explicitly
      claim a trial_protocol_hash are checked; trial-dispatch action
      classes without a hash are blocked).

Wiring status (2026-05-06, T4 v4 D-batch):
    Active rule_checks: 001, 007, 013, 014, 015, 016, 017, 018, 019, 020, 021.
    DEFERRED-EXTERNAL (registered in INVARIANTS dict, no rule check yet —
    each requires querying state outside the proposal payload, so wiring
    inside this stdlib-only kernel would either lie or break the contract):
        INV-002 inject_context — needs cross-ref to outcome+target-behavior
                 records in evidence store.
        INV-003 close_correction — needs replacement-behavior log lookup.
        INV-004 score_emit — needs query of governance side-effects
                 (permission/influence/routing/teaching tables).
        INV-005 memory_change/promote_memory — needs evidence store + future
                 utility tracker join.
        INV-006 run_tool — needs tool-need classification record from the
                 calling agent's discernment log.
        INV-008 cast_vote — needs domain-specific influence-weight table.
        INV-009 permission_change (restrictions) — needs recovery-path
                 registry lookup.
        INV-010 ask_chief — needs uncertainty/evidence-sufficiency record
                 (distinct from INV-020 which only checks the assertion flag).
        INV-011 ledger entry — needs ledger db lookup; wrappers may
                 auto-create pre-action entries.
        INV-012 governance_cycle — needs fidelity-on-governance metrics.
    Wire each when its state store becomes reachable without violating the
    stdlib-only / never-raise contract.

API contract (locked across the 5-agent v0 batch):
    SCHEMA_VERSION                  int — bump on schema-breaking changes
    InvariantId                     Literal[CTXDNA-INV-001..021]
    InvariantDecision               Literal[allow|allow_with_warning|escalate|block]
    ActionProposal                  dataclass
    InvariantReport                 dataclass
    INVARIANTS                      dict[InvariantId, InvariantSpec]
    evaluate(proposal) -> InvariantReport
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

# ---------------------------------------------------------------------------
# Schema version & types
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1

InvariantId = Literal[
    "CTXDNA-INV-001",
    "CTXDNA-INV-002",
    "CTXDNA-INV-003",
    "CTXDNA-INV-004",
    "CTXDNA-INV-005",
    "CTXDNA-INV-006",
    "CTXDNA-INV-007",
    "CTXDNA-INV-008",
    "CTXDNA-INV-009",
    "CTXDNA-INV-010",
    "CTXDNA-INV-011",
    "CTXDNA-INV-012",
    "CTXDNA-INV-013",
    "CTXDNA-INV-014",
    "CTXDNA-INV-015",
    "CTXDNA-INV-016",
    "CTXDNA-INV-017",
    "CTXDNA-INV-018",
    "CTXDNA-INV-019",
    "CTXDNA-INV-020",
    "CTXDNA-INV-021",
    "CTXDNA-INV-022",
    "MFINV-C01",
]

InvariantDecision = Literal["allow", "allow_with_warning", "escalate", "block"]

# Mirrors GovernanceMode.ActionClass — kept open as str for forward-compat.
ActionClass = str  # e.g. "memory_change", "permission_change", "trial_dispatch", ...
BlastRadius = Literal["none", "low", "medium", "high", "fleet_wide"]
Severity = Literal["low", "medium", "high", "fatal"]

# ---------------------------------------------------------------------------
# Logger + counters (ZERO SILENT FAILURES)
# ---------------------------------------------------------------------------

_log = logging.getLogger("memory.invariants")
if not _log.handlers:
    # Don't override caller logging config; just guarantee a handler exists.
    _log.addHandler(logging.NullHandler())

_counter_lock = threading.Lock()
_FAILURE_COUNTERS: Dict[str, int] = {
    "schema_mismatch": 0,
    "lockfile_read_error": 0,
    "lockfile_parse_error": 0,
    "evaluate_internal_error": 0,
    "inv021_unknown_hash": 0,
    "inv021_missing_hash": 0,
    "mfinv_c01_caller_bypass": 0,
    "mfinv_c01_via_cascade_missing": 0,
    "inv022_no_message_attempt": 0,
    "inv022_via_cascade_missing": 0,
}


def _bump(counter: str) -> None:
    with _counter_lock:
        _FAILURE_COUNTERS[counter] = _FAILURE_COUNTERS.get(counter, 0) + 1


def get_failure_counters() -> Dict[str, int]:
    """Snapshot of failure counters — observable channel for ZSF."""
    with _counter_lock:
        return dict(_FAILURE_COUNTERS)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class InvariantSpec:
    id: InvariantId
    name: str
    rule_text: str
    applies_to: List[ActionClass]
    severity: Severity


@dataclass
class ActionProposal:
    """Minimal proposal surface for v0 TrialBench. Other agents extend via kwargs."""

    action_class: ActionClass
    blast_radius: BlastRadius = "low"
    evidence_refs: List[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    # INV-021 surface: hash of the locked trial protocol the proposal targets.
    trial_protocol_hash: Optional[str] = None

    # Optional governance hints (kept loose so other agents can extend).
    proposal_id: Optional[str] = None
    rationale: Optional[str] = None
    reversible: Optional[bool] = None
    sandbox_id: Optional[str] = None
    emergency_override_reason: Optional[str] = None

    # INV-013 surface: efficiency rewards must come AFTER correctness verification.
    # Set claimed_correctness_verified=True when the action_class is "score_emit"
    # AND the underlying outcome was tagged as primary-endpoint success.
    claimed_correctness_verified: Optional[bool] = None
    # INV-015 surface: minimal-sufficient-context tracking. context_payload_kb
    # is the size of the injected context for this action; minimal_sufficient_kb
    # is the operator-set ceiling. Exceeding requires justification.
    context_payload_kb: Optional[int] = None
    minimal_sufficient_kb: Optional[int] = None

    # INV-016 surface: when score_emit awards an abstention reward, the
    # posthoc_correctness flag must indicate that the abstention turned out
    # to be the right call (i.e. the held-back action would have been wrong).
    claimed_abstention: Optional[bool] = None
    posthoc_correctness_validated: Optional[bool] = None
    # INV-017 surface: tool-discipline scoring must measure BOTH directions.
    # When run_tool emits a discipline score, both overuse_count and
    # underuse_count must be present (>=0). Either being None blocks.
    tool_discipline_score_emitted: Optional[bool] = None
    overuse_count: Optional[int] = None
    underuse_count: Optional[int] = None
    # INV-018 surface: trace curation gate. learn_from_trace requires the
    # trace to have passed quality curation.
    trace_curation_passed: Optional[bool] = None
    # INV-019 surface: an intervention scored as "good" must improve at least
    # one of the four canonical dimensions.
    scored_as_good_intervention: Optional[bool] = None
    intervention_outcome: Optional[str] = None  # one of _INV019_VALID_OUTCOMES
    # INV-020 surface: ask_chief escalations must record that a local
    # evidence/uncertainty assessment was performed first.
    local_assessment_done: Optional[bool] = None
    # MFINV-C01 surface: peer_message actions must traverse the multifleet
    # channel-priority dispatcher, never call a transport directly.
    # via_cascade=True means dispatcher.send() routed the message through
    # the 7-priority cascade. caller_path identifies the caller module so
    # the dispatcher allowlist can grant exceptions (governance kill-switch,
    # dispatcher internals, tests).
    via_cascade: Optional[bool] = None
    caller_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action_class": self.action_class,
            "blast_radius": self.blast_radius,
            "evidence_refs": list(self.evidence_refs),
            "schema_version": self.schema_version,
            "trial_protocol_hash": self.trial_protocol_hash,
            "proposal_id": self.proposal_id,
            "rationale": self.rationale,
            "reversible": self.reversible,
            "sandbox_id": self.sandbox_id,
            "emergency_override_reason": self.emergency_override_reason,
        }


@dataclass
class InvariantReport:
    decision: InvariantDecision
    triggered: List[InvariantId] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# INV-021: trial protocol lockfile
# ---------------------------------------------------------------------------

# Trial dispatches must reference a locked protocol hash. Other action classes
# are unaffected by INV-021.
_TRIAL_DISPATCH_CLASSES = {
    "trial_dispatch",
    "surgeon_review_dispatch",
    "trial_protocol_run",
}


def _default_lockfile_path() -> Path:
    """Resolve docs/dao/trialbench-protocol.lock.json relative to repo root."""
    # Allow override for tests / non-default deployments.
    override = os.environ.get("CTXDNA_TRIALBENCH_LOCKFILE")
    if override:
        return Path(override)
    # memory/invariants.py -> repo root is parent of `memory/`
    here = Path(__file__).resolve()
    repo_root = here.parent.parent
    return repo_root / "docs" / "dao" / "trialbench-protocol.lock.json"


def _load_locked_protocol_hashes(path: Optional[Path] = None) -> Tuple[set, Optional[str]]:
    """Return (known_hashes, error_reason). Missing file is not an error."""
    p = path or _default_lockfile_path()
    if not p.exists():
        return set(), None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        _bump("lockfile_read_error")
        _log.warning("invariants: failed to read trialbench lockfile %s: %s", p, exc)
        return set(), f"lockfile_read_error: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _bump("lockfile_parse_error")
        _log.warning("invariants: failed to parse trialbench lockfile %s: %s", p, exc)
        return set(), f"lockfile_parse_error: {exc}"

    hashes: set = set()
    # Accept four canonical lock-file shapes (N3 ships single `trial_protocol_hash`;
    # multi-protocol callers may use `hashes: list[str]` or `protocols: list[{hash}]`;
    # bare JSON array also supported for terse v0 tooling).
    if isinstance(data, dict):
        if isinstance(data.get("trial_protocol_hash"), str):
            hashes.add(data["trial_protocol_hash"])
        if isinstance(data.get("hashes"), list):
            hashes.update(str(h) for h in data["hashes"] if isinstance(h, str))
        if isinstance(data.get("protocols"), list):
            for entry in data["protocols"]:
                if isinstance(entry, dict) and isinstance(entry.get("hash"), str):
                    hashes.add(entry["hash"])
    elif isinstance(data, list):
        hashes.update(str(h) for h in data if isinstance(h, str))
    return hashes, None


# ---------------------------------------------------------------------------
# Registry — 21 specs (001..020 ported from TS, 021 = NEW protocol-lock rule)
# ---------------------------------------------------------------------------

_ALL_CLASSES: List[ActionClass] = [
    "*",  # sentinel meaning "applies to every action class"
]


def _spec(
    id: InvariantId,
    name: str,
    rule_text: str,
    applies_to: List[ActionClass],
    severity: Severity,
) -> InvariantSpec:
    return InvariantSpec(id=id, name=name, rule_text=rule_text, applies_to=applies_to, severity=severity)


INVARIANTS: Dict[InvariantId, InvariantSpec] = {
    "CTXDNA-INV-001": _spec(
        "CTXDNA-INV-001",
        "Evidence before authority",
        "No agent may gain influence or permissions without proportional outcome evidence.",
        ["permission_change", "influence_change"],
        "high",
    ),
    "CTXDNA-INV-002": _spec(
        "CTXDNA-INV-002",
        "Context must have purpose",
        "No context item may be injected without intended outcome and target behavior.",
        ["inject_context"],
        "medium",
    ),
    "CTXDNA-INV-003": _spec(
        "CTXDNA-INV-003",
        "Correction must teach",
        "No correction may close without replacement behavior or justified dismissal.",
        ["close_correction"],
        "medium",
    ),
    "CTXDNA-INV-004": _spec(
        "CTXDNA-INV-004",
        "Scores must govern",
        "No score may exist without affecting permission, influence, routing, teaching, or measurement.",
        ["score_emit"],
        "medium",
    ),
    "CTXDNA-INV-005": _spec(
        "CTXDNA-INV-005",
        "Memory must prove utility",
        "No memory may be promoted without evidence refs and future utility tracking.",
        ["memory_change", "promote_memory"],
        "high",
    ),
    "CTXDNA-INV-006": _spec(
        "CTXDNA-INV-006",
        "Tools require discernment",
        "No meaningful tool call may execute without tool-need classification.",
        ["run_tool"],
        "medium",
    ),
    "CTXDNA-INV-007": _spec(
        "CTXDNA-INV-007",
        "High-risk requires review",
        "No high-risk world-changing action may execute without evidence and appropriate review.",
        ["*"],
        "fatal",
    ),
    "CTXDNA-INV-008": _spec(
        "CTXDNA-INV-008",
        "Votes must be weighted",
        "No agent vote may be counted without domain-specific influence weight.",
        ["cast_vote"],
        "medium",
    ),
    "CTXDNA-INV-009": _spec(
        "CTXDNA-INV-009",
        "Restrictions require recovery",
        "No restriction may be applied without a recovery path.",
        ["permission_change"],
        "medium",
    ),
    "CTXDNA-INV-010": _spec(
        "CTXDNA-INV-010",
        "Chief escalation must be justified",
        "No chief escalation may occur without local uncertainty/evidence sufficiency assessment.",
        ["ask_chief"],
        "high",
    ),
    "CTXDNA-INV-011": _spec(
        "CTXDNA-INV-011",
        "Ledger is mandatory",
        "No governed action may occur without a ledger entry; wrappers may auto-create pre-action ledger entries.",
        ["*"],
        "high",
    ),
    "CTXDNA-INV-012": _spec(
        "CTXDNA-INV-012",
        "Fidelity audits the auditor",
        "No governance cycle is complete without fidelity checks on governance itself.",
        ["governance_cycle"],
        "medium",
    ),
    "CTXDNA-INV-013": _spec(
        "CTXDNA-INV-013",
        "Correctness before efficiency",
        "No efficiency reward unless correctness threshold is met.",
        ["score_emit"],
        "medium",
    ),
    "CTXDNA-INV-014": _spec(
        "CTXDNA-INV-014",
        "Evidence sufficiency before action",
        "No durable/high-risk action above risk threshold without evidence sufficiency assessment.",
        ["*"],
        "high",
    ),
    "CTXDNA-INV-015": _spec(
        "CTXDNA-INV-015",
        "Minimal sufficient context",
        "No context payload may exceed minimal sufficient context without justification.",
        ["inject_context"],
        "low",
    ),
    "CTXDNA-INV-016": _spec(
        "CTXDNA-INV-016",
        "Validated abstention",
        "No abstention reward without posthoc correctness validation.",
        ["score_emit"],
        "medium",
    ),
    "CTXDNA-INV-017": _spec(
        "CTXDNA-INV-017",
        "Tool discipline is bidirectional",
        "Tool governance must score both overuse and underuse.",
        ["run_tool"],
        "medium",
    ),
    "CTXDNA-INV-018": _spec(
        "CTXDNA-INV-018",
        "Curated learning traces",
        "No learning from traces that fail curation quality checks.",
        ["learn_from_trace"],
        "high",
    ),
    "CTXDNA-INV-019": _spec(
        "CTXDNA-INV-019",
        "Intervention value required",
        "No intervention may be scored as good unless it improved evidence, correctness, safety, or governance.",
        ["score_emit"],
        "medium",
    ),
    "CTXDNA-INV-020": _spec(
        "CTXDNA-INV-020",
        "Escalation is not evidence",
        "No escalation may be used as a substitute for local evidence assessment.",
        ["ask_chief"],
        "high",
    ),
    # NEW for v0 TrialBench:
    "CTXDNA-INV-021": _spec(
        "CTXDNA-INV-021",
        "Trial protocol must be locked",
        "Only protocols with hash present in docs/dao/trialbench-protocol.lock.json may pass "
        "surgeon-review for trial dispatches. No bypass mechanism.",
        list(_TRIAL_DISPATCH_CLASSES),
        "fatal",
    ),
    # QQ2 2026-05-08 — peer messages must traverse the cascade AND be backed
    # by a MessageAttempt evidence record. MFINV-C01 enforces dispatcher
    # traversal; INV-022 enforces evidence-of-traversal (so a regression that
    # short-circuits attempt logging is caught even when via_cascade=True).
    # See .fleet/audits/2026-05-08-mf-channel-invariance-gap-analysis.md Gap 3.
    "CTXDNA-INV-022": _spec(
        "CTXDNA-INV-022",
        "peer_messages_via_cascade_only",
        "Every peer_message must have via_cascade=True AND at least one "
        "MessageAttempt evidence record proving the cascade actually executed.",
        ["peer_message"],
        "fatal",
    ),
    # MultiFleet channel-priority — see docs/dao/multifleet-channel-priority.md.
    # Forbids direct transport calls (NATS publish, urlopen :8855/message,
    # scp .fleet-messages, wakeonlan, git push fleet-message-dir) outside the
    # multifleet.channel_priority dispatcher.
    "MFINV-C01": _spec(
        "MFINV-C01",
        "channel-priority-traversal",
        "Every cross-node peer_message must traverse the channel-priority "
        "dispatcher (via_cascade=True) or originate from an allowlisted "
        "caller path. No direct transport invocation from caller code.",
        ["peer_message"],
        "fatal",
    ),
}


# MFINV-C01 dispatcher allowlist — caller_path prefixes permitted to bypass
# via_cascade=True (because they ARE the dispatcher, the kill-switch, or
# test fixtures). Mirrors the allowlist intent in
# docs/dao/multifleet-channel-priority.md.
_MFINV_C01_DISPATCHER_ALLOWLIST: Tuple[str, ...] = (
    "multifleet.channel_priority",
    "memory.governance_kill_switch",
    "memory.tests",
    "multi-fleet.tests",
)


# ---------------------------------------------------------------------------
# Decision precedence
# ---------------------------------------------------------------------------

_DECISION_PRECEDENCE: Dict[InvariantDecision, int] = {
    "allow": 0,
    "allow_with_warning": 1,
    "escalate": 2,
    "block": 3,
}


def _worst(a: InvariantDecision, b: InvariantDecision) -> InvariantDecision:
    return a if _DECISION_PRECEDENCE[a] >= _DECISION_PRECEDENCE[b] else b


def _severity_to_decision(sev: Severity) -> InvariantDecision:
    if sev == "fatal":
        return "block"
    if sev == "high":
        return "escalate"
    if sev == "medium":
        return "allow_with_warning"
    return "allow_with_warning"  # low-severity failures still surface


# ---------------------------------------------------------------------------
# Per-invariant rule logic
# ---------------------------------------------------------------------------


def _applies(spec: InvariantSpec, action_class: ActionClass) -> bool:
    return "*" in spec.applies_to or action_class in spec.applies_to


def _check_inv021(
    proposal: ActionProposal, lockfile_path: Optional[Path] = None
) -> Tuple[bool, str]:
    """Return (passed, reason). Only meaningful for trial-dispatch action classes."""
    if proposal.action_class not in _TRIAL_DISPATCH_CLASSES:
        return True, "INV-021 not applicable to action_class"

    if not proposal.trial_protocol_hash:
        _bump("inv021_missing_hash")
        return False, (
            "INV-021: trial dispatch missing trial_protocol_hash; "
            "every trial dispatch must reference a locked protocol hash."
        )

    known, err = _load_locked_protocol_hashes(lockfile_path)
    if err:
        # Lockfile unreadable -> conservative block. ZSF: counter already bumped.
        return False, f"INV-021: lockfile error -> {err}"

    if proposal.trial_protocol_hash not in known:
        _bump("inv021_unknown_hash")
        return False, (
            f"INV-021: trial_protocol_hash={proposal.trial_protocol_hash!r} not present "
            f"in trialbench-protocol.lock.json (known={len(known)} hashes). No bypass."
        )
    return True, "INV-021: protocol hash locked"


def _check_inv001_evidence_for_authority(p: ActionProposal) -> Tuple[bool, str]:
    if p.action_class in ("permission_change", "influence_change") and not p.evidence_refs:
        return False, "INV-001: permission/influence change requires evidence_refs"
    return True, ""


def _check_inv007_high_risk_review(p: ActionProposal) -> Tuple[bool, str]:
    if p.blast_radius in ("high", "fleet_wide") and not p.evidence_refs:
        return False, "INV-007: high blast-radius action requires evidence_refs (review)"
    return True, ""


def _check_inv014_evidence_sufficiency(p: ActionProposal) -> Tuple[bool, str]:
    # Durable / non-reversible high-risk actions need evidence.
    if (
        p.blast_radius in ("medium", "high", "fleet_wide")
        and p.reversible is False
        and not p.evidence_refs
    ):
        return False, "INV-014: durable medium+ action needs evidence sufficiency"
    return True, ""


def _check_inv013_correctness_before_efficiency(p: ActionProposal) -> Tuple[bool, str]:
    # INV-013: efficiency rewards (i.e. score_emit actions tagged as efficiency
    # gains — fewer tool calls, smaller context, faster wall) must NOT be
    # awarded unless the underlying outcome was correctness-verified.
    # When score_emit + claimed_correctness_verified is None or False, block.
    if p.action_class == "score_emit" and p.claimed_correctness_verified is not True:
        return False, (
            "INV-013: score_emit requires claimed_correctness_verified=True "
            "(no efficiency reward unless correctness threshold met)"
        )
    return True, ""


_INV019_VALID_OUTCOMES = frozenset(
    {
        "evidence_improved",
        "correctness_improved",
        "safety_improved",
        "governance_improved",
    }
)


def _check_inv016_validated_abstention(p: ActionProposal) -> Tuple[bool, str]:
    # INV-016: score_emit that claims an abstention reward must be
    # posthoc-validated as correct. claimed_abstention is the opt-in flag —
    # only score_emit actions that explicitly claim an abstention reward
    # are gated. Non-abstention score_emit is unaffected (INV-013/019 cover those).
    if p.action_class != "score_emit":
        return True, ""
    if p.claimed_abstention is not True:
        return True, ""
    if p.posthoc_correctness_validated is not True:
        return False, (
            "INV-016: abstention reward requires posthoc_correctness_validated=True "
            "(no abstention reward without posthoc validation)"
        )
    return True, ""


def _check_inv017_tool_discipline_bidirectional(p: ActionProposal) -> Tuple[bool, str]:
    # INV-017: when run_tool emits a tool-discipline score, both overuse_count
    # AND underuse_count must be measured. Missing either side = unmeasured
    # bias direction = block. tool_discipline_score_emitted is the opt-in flag
    # so other run_tool invocations remain unaffected.
    if p.action_class != "run_tool":
        return True, ""
    if p.tool_discipline_score_emitted is not True:
        return True, ""
    missing: List[str] = []
    if p.overuse_count is None or p.overuse_count < 0:
        missing.append("overuse_count")
    if p.underuse_count is None or p.underuse_count < 0:
        missing.append("underuse_count")
    if missing:
        return False, (
            f"INV-017: tool-discipline score requires both overuse_count and underuse_count "
            f"(missing/invalid: {', '.join(missing)})"
        )
    return True, ""


def _check_inv018_curated_learning_traces(p: ActionProposal) -> Tuple[bool, str]:
    # INV-018: learn_from_trace must have passed curation. Defaulting to None
    # is treated as "not asserted" -> block (high severity = escalate). The
    # caller must explicitly assert curation status.
    if p.action_class != "learn_from_trace":
        return True, ""
    if p.trace_curation_passed is not True:
        return False, (
            "INV-018: learn_from_trace requires trace_curation_passed=True "
            "(no learning from uncurated traces)"
        )
    return True, ""


def _check_inv019_intervention_value(p: ActionProposal) -> Tuple[bool, str]:
    # INV-019: a score_emit that flags an intervention as "good" must record
    # which of the four canonical outcome dimensions improved. Opt-in via
    # scored_as_good_intervention=True; otherwise this rule does not gate.
    if p.action_class != "score_emit":
        return True, ""
    if p.scored_as_good_intervention is not True:
        return True, ""
    outcome = p.intervention_outcome
    if not isinstance(outcome, str) or outcome not in _INV019_VALID_OUTCOMES:
        return False, (
            f"INV-019: intervention scored as good requires intervention_outcome in "
            f"{sorted(_INV019_VALID_OUTCOMES)} (got {outcome!r})"
        )
    return True, ""


def _check_inv020_escalation_not_evidence(p: ActionProposal) -> Tuple[bool, str]:
    # INV-020: ask_chief escalations must record that a local evidence /
    # uncertainty assessment ran first. Default None -> not asserted -> block
    # (high severity = escalate decision).
    if p.action_class != "ask_chief":
        return True, ""
    if p.local_assessment_done is not True:
        return False, (
            "INV-020: ask_chief escalation requires local_assessment_done=True "
            "(escalation is not a substitute for local evidence assessment)"
        )
    return True, ""


# INV-022: MessageAttempt evidence prefix. Cascade-emitted attempt records
# tag their evidence_refs with this prefix so the invariant can confirm at
# least one transport channel was actually exercised. Convention is enforced
# at the dispatcher: every successful or failed channel attempt logs one
# `message_attempt:<channel>:<msg_id>` ref onto the proposal before evaluate().
_INV022_MESSAGE_ATTEMPT_PREFIX = "message_attempt:"


def _has_message_attempt_evidence(p: ActionProposal) -> bool:
    refs = p.evidence_refs or []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith(_INV022_MESSAGE_ATTEMPT_PREFIX):
            return True
    return False


def _check_inv022_peer_messages_via_cascade(p: ActionProposal) -> Tuple[bool, str]:
    # INV-022: peer_message proposals must (a) declare via_cascade=True AND
    # (b) carry at least one MessageAttempt evidence ref. Both conditions
    # must hold — a proposal can claim cascade traversal without proving it.
    # The dispatcher allowlist (see MFINV-C01) does NOT exempt INV-022 — even
    # the dispatcher itself must record an attempt before declaring the
    # proposal admissible. Non-peer-message classes are out of scope.
    if p.action_class != "peer_message":
        return True, ""
    if p.via_cascade is not True:
        _bump("inv022_via_cascade_missing")
        return False, (
            "INV-022: peer_message requires via_cascade=True (cascade traversal "
            "is mandatory; see docs/dao/multifleet-channel-priority.md)"
        )
    if not _has_message_attempt_evidence(p):
        _bump("inv022_no_message_attempt")
        return False, (
            "INV-022: peer_message requires at least one "
            f"{_INV022_MESSAGE_ATTEMPT_PREFIX}<channel>:<msg_id> evidence ref "
            "(MessageAttempt record proving the cascade executed)"
        )
    return True, ""


def _check_mfinv_c01_channel_priority(p: ActionProposal) -> Tuple[bool, str]:
    # MFINV-C01: peer_message actions must traverse the channel-priority
    # dispatcher. Pass condition: via_cascade=True OR caller_path on the
    # dispatcher allowlist (governance kill-switch, dispatcher internals,
    # test fixtures). Anything else = caller bypass = block.
    if p.action_class != "peer_message":
        return True, ""
    if p.via_cascade is True:
        return True, ""
    cp = p.caller_path or ""
    for allowed in _MFINV_C01_DISPATCHER_ALLOWLIST:
        if cp == allowed or cp.startswith(allowed + "."):
            return True, ""
    if not cp:
        _bump("mfinv_c01_via_cascade_missing")
    else:
        _bump("mfinv_c01_caller_bypass")
    return False, (
        "MFINV-C01: peer_message must set via_cascade=True or caller_path in "
        "the dispatcher allowlist; direct transport bypass is forbidden "
        "(see docs/dao/multifleet-channel-priority.md)."
    )


def _check_inv015_minimal_sufficient_context(p: ActionProposal) -> Tuple[bool, str]:
    # INV-015: context payload must not exceed minimal-sufficient ceiling
    # without explicit justification. When inject_context + payload exceeds
    # ceiling AND no rationale explains why, block.
    if p.action_class != "inject_context":
        return True, ""
    payload = p.context_payload_kb
    ceiling = p.minimal_sufficient_kb
    if payload is None or ceiling is None:
        # Neither side set the budget — pass (operator hasn't opted in).
        return True, ""
    if payload > ceiling and not p.rationale:
        return False, (
            f"INV-015: inject_context payload {payload}kb exceeds minimal-sufficient "
            f"ceiling {ceiling}kb without rationale"
        )
    return True, ""


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


def evaluate(
    proposal: ActionProposal,
    *,
    lockfile_path: Optional[Path] = None,
) -> InvariantReport:
    """Evaluate a proposal against all 21 invariants. Never raises."""
    triggered: List[InvariantId] = []
    reasons: List[str] = []
    decision: InvariantDecision = "allow"

    # 0a. Kill-switch gate (T3 v4 — emergency rollback).
    # If the governance kill-switch is engaged, return allow-bypass with the
    # documented reason. ZSF: failures inside the kill-switch module are
    # logged + counted in that module; here we defensively wrap so any
    # internal error CANNOT silently disable governance.
    try:
        from memory.governance_kill_switch import (  # local import to avoid cycles
            get_state,
            is_killed,
        )
        if is_killed():
            ks_state = get_state()
            return InvariantReport(
                decision="allow",
                triggered=[],
                reasons=[
                    f"KILL_SWITCH_ACTIVE: {ks_state.reason or '(no reason recorded)'}"
                ],
            )
    except Exception as exc:  # noqa: BLE001 — ZSF: log+count, never silently swallow
        _bump("evaluate_internal_error")
        _log.error("invariants: kill-switch check failed: %s", exc)
        # Fail-closed: on kill-switch errors continue to normal evaluation so
        # governance enforcement is NOT silently dropped.

    # 0. Schema version gate — fatal block.
    try:
        if proposal.schema_version != SCHEMA_VERSION:
            _bump("schema_mismatch")
            return InvariantReport(
                decision="block",
                triggered=[],
                reasons=[
                    f"schema_version mismatch: proposal={proposal.schema_version} "
                    f"expected={SCHEMA_VERSION}"
                ],
            )
    except Exception as exc:  # noqa: BLE001 — last-resort guard, ZSF logs+counts
        _bump("evaluate_internal_error")
        _log.error("invariants: schema check failed: %s", exc)
        return InvariantReport(
            decision="block",
            triggered=[],
            reasons=[f"internal: schema check failed: {exc}"],
        )

    # 1. Active rule checks (only the ones whose logic is deterministic & local).
    rule_checks = (
        ("CTXDNA-INV-001", _check_inv001_evidence_for_authority),
        ("CTXDNA-INV-007", _check_inv007_high_risk_review),
        ("CTXDNA-INV-013", _check_inv013_correctness_before_efficiency),
        ("CTXDNA-INV-014", _check_inv014_evidence_sufficiency),
        ("CTXDNA-INV-015", _check_inv015_minimal_sufficient_context),
        ("CTXDNA-INV-016", _check_inv016_validated_abstention),
        ("CTXDNA-INV-017", _check_inv017_tool_discipline_bidirectional),
        ("CTXDNA-INV-018", _check_inv018_curated_learning_traces),
        ("CTXDNA-INV-019", _check_inv019_intervention_value),
        ("CTXDNA-INV-020", _check_inv020_escalation_not_evidence),
        ("CTXDNA-INV-022", _check_inv022_peer_messages_via_cascade),
        ("MFINV-C01", _check_mfinv_c01_channel_priority),
    )

    try:
        for inv_id, fn in rule_checks:
            spec = INVARIANTS[inv_id]
            if not _applies(spec, proposal.action_class) and "*" not in spec.applies_to:
                continue
            ok, reason = fn(proposal)
            if not ok:
                triggered.append(inv_id)  # type: ignore[arg-type]
                reasons.append(reason)
                decision = _worst(decision, _severity_to_decision(spec.severity))

        # 2. INV-021 — trial protocol lockfile.
        ok, reason = _check_inv021(proposal, lockfile_path=lockfile_path)
        if not ok:
            triggered.append("CTXDNA-INV-021")
            reasons.append(reason)
            decision = _worst(decision, _severity_to_decision(INVARIANTS["CTXDNA-INV-021"].severity))
        else:
            # Annotate when applicable so callers can see the gate ran.
            if proposal.action_class in _TRIAL_DISPATCH_CLASSES:
                reasons.append(reason)

    except Exception as exc:  # noqa: BLE001 — ZSF: never silently swallow
        _bump("evaluate_internal_error")
        _log.error("invariants: evaluate() internal failure: %s", exc)
        return InvariantReport(
            decision="block",
            triggered=triggered,
            reasons=reasons + [f"internal: evaluate() raised {type(exc).__name__}: {exc}"],
        )

    return InvariantReport(decision=decision, triggered=triggered, reasons=reasons)


__all__ = [
    "SCHEMA_VERSION",
    "InvariantId",
    "InvariantDecision",
    "ActionProposal",
    "InvariantReport",
    "InvariantSpec",
    "INVARIANTS",
    "evaluate",
    "get_failure_counters",
]
