"""TrialBench v0 — governed packet builder (Arm-A, Arm-B, Arm-C).

Builds the prompt packet that a single trial sends to the model under test.

Three arms:
    A_raw                : just task_prompt (control)
    B_generic_context    : task_prompt + tiny project summary (defer richer impl to v1)
    C_governed           : full Context-DNA governed packet — task-world,
                           minimal sufficient context (subset of webhook S0-S8),
                           invariants, evidence threshold, failure modes,
                           required output structure

Public API:
    build_governed_packet(task: dict, arm: str) -> dict

ZERO SILENT FAILURES: any degraded section source is recorded as a clear stub
    "[s<N>:trial degraded - <reason>]" and surfaced via packet["degradations"].
The function never raises — degraded sections still produce a packet.

Imported by N2 (runtime trial executor). Read-only consumer of
memory.persistent_hook_structure — does NOT mutate webhook state.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# stdlib only — keep this file dependency-free for trial reproducibility
log = logging.getLogger("trialbench.packet")

PACKET_VERSION = "v0.1.0-N4"


# ── BB1 Phase-9: permission-gate wiring (Y3 follow-up to AA2) ──────────────
#
# Capability + actor tags for the trialbench C-arm packet emission. Mirrors
# the chief_audit pattern: stable policy keys, decoupled from packet shape.
TRIALBENCH_ARM_C_CAPABILITY = "trialbench_emit_governed_arm_c"
TRIALBENCH_ARM_C_ACTOR = "trial_runner"


# ── ZSF counters ───────────────────────────────────────────────────────────
#
# Zero-silent-failures invariant: every gate decision and denial-record path
# bumps an observable counter. Module-global, test-resettable.
_arm_c_counter_lock = threading.Lock()
_arm_c_counters: Dict[str, int] = {
    "trialbench_arm_c_decisions_gated_total": 0,
    "trialbench_arm_c_decisions_denied_total": 0,
    "trialbench_arm_c_denial_record_errors_total": 0,
}


def _bump_arm_c_counter(name: str) -> None:
    with _arm_c_counter_lock:
        _arm_c_counters[name] = _arm_c_counters.get(name, 0) + 1


def get_arm_c_decisions_gated_count() -> int:
    """Monotonic count of arm-C packets that passed through the gate."""
    with _arm_c_counter_lock:
        return _arm_c_counters["trialbench_arm_c_decisions_gated_total"]


def get_arm_c_decisions_denied_count() -> int:
    """Monotonic count of arm-C packets blocked by the gate."""
    with _arm_c_counter_lock:
        return _arm_c_counters["trialbench_arm_c_decisions_denied_total"]


def get_arm_c_denial_record_error_count() -> int:
    """Monotonic count of arm-C denial-record write failures."""
    with _arm_c_counter_lock:
        return _arm_c_counters["trialbench_arm_c_denial_record_errors_total"]


def reset_arm_c_counters_for_tests() -> None:
    """Test-only reset. Never call in production."""
    with _arm_c_counter_lock:
        for k in _arm_c_counters:
            _arm_c_counters[k] = 0

# ---------------------------------------------------------------------------
# Invariants source (memory.invariants — N1 deliverable; fallback if unshipped)
# ---------------------------------------------------------------------------
# CTXDNA-INV-* canonical IDs; matches the 21 invariants enumerated in
# docs/vision/08-constitutional-invariants.md and 09-ideal-adaptive-invariance.md
# (trialbench reference). Each entry: (id, statement, families_it_applies_to)
_FALLBACK_INVARIANTS: List[Dict[str, Any]] = [
    {"id": "CTXDNA-INV-001", "statement": "No context without intended outcome",
     "families": ["architecture_sensitive", "memory_promotion", "correction_response", "tool_abstention"]},
    {"id": "CTXDNA-INV-002", "statement": "No correction without replacement behavior",
     "families": ["correction_response", "architecture_sensitive"]},
    {"id": "CTXDNA-INV-003", "statement": "No memory promotion without evidence and future utility tracking",
     "families": ["memory_promotion", "architecture_sensitive"]},
    {"id": "CTXDNA-INV-004", "statement": "No authority increase without outcome evidence",
     "families": ["architecture_sensitive", "memory_promotion"]},
    {"id": "CTXDNA-INV-005", "statement": "Minimal sufficient context principle — drop redundancy",
     "families": ["tool_abstention", "architecture_sensitive", "memory_promotion", "correction_response"]},
    {"id": "CTXDNA-INV-006", "statement": "Correctness before efficiency",
     "families": ["architecture_sensitive", "correction_response"]},
    {"id": "CTXDNA-INV-007", "statement": "Tool calls require justification — abstain when sufficient",
     "families": ["tool_abstention"]},
    {"id": "CTXDNA-INV-008", "statement": "High-risk action requires evidence sufficiency above threshold",
     "families": ["architecture_sensitive"]},
    {"id": "CTXDNA-INV-009", "statement": "Preserve corrigibility — accept skeptic correction over confidence",
     "families": ["correction_response"]},
    {"id": "CTXDNA-INV-010", "statement": "Disclose remaining uncertainty in every output",
     "families": ["architecture_sensitive", "memory_promotion", "correction_response", "tool_abstention"]},
    {"id": "CTXDNA-INV-011", "statement": "Focused change only — no scope creep beyond request",
     "families": ["architecture_sensitive", "correction_response"]},
    {"id": "CTXDNA-INV-012", "statement": "Preserve schema/parent-layout/backward compatibility",
     "families": ["architecture_sensitive"]},
    {"id": "CTXDNA-INV-013", "statement": "Report tests/build outcome — never silent",
     "families": ["architecture_sensitive", "memory_promotion", "correction_response", "tool_abstention"]},
    {"id": "CTXDNA-INV-014", "statement": "No bypass of chief arbitration on high-risk fleet conflicts",
     "families": ["architecture_sensitive"]},
    {"id": "CTXDNA-INV-015", "statement": "Memory utility scoring requires future-use signal, not popularity",
     "families": ["memory_promotion"]},
    {"id": "CTXDNA-INV-016", "statement": "Stale or misleading memory must be flagged, not promoted",
     "families": ["memory_promotion"]},
    {"id": "CTXDNA-INV-017", "statement": "Context compiler logs WHY items were excluded",
     "families": ["tool_abstention", "architecture_sensitive"]},
    {"id": "CTXDNA-INV-018", "statement": "Source pattern pulse takes precedence over local optimization",
     "families": ["architecture_sensitive", "correction_response"]},
    {"id": "CTXDNA-INV-019", "statement": "Reversible actions preferred — checkpoint before risk",
     "families": ["architecture_sensitive"]},
    {"id": "CTXDNA-INV-020", "statement": "Evidence over confidence — outcomes are truth",
     "families": ["architecture_sensitive", "memory_promotion", "correction_response"]},
    {"id": "CTXDNA-INV-021", "statement": "Determinism preserved — same inputs → same output",
     "families": ["architecture_sensitive", "tool_abstention"]},
]

# FIXME(N1): replace fallback once memory.invariants ships its INVARIANTS list.
# Expected shape: List[{"id","statement","families"}] or equivalent dataclass.
_INVARIANTS_FIXME: List[str] = []


# N1 (memory.invariants) uses an *action-class* taxonomy in `applies_to`
# (run_tool, inject_context, promote_memory, ...) rather than the task-family
# names used in task_bank.json. Map task family → action classes that family
# tends to perform, so we can select the right CTXDNA-INV-* per task.
_FAMILY_TO_ACTIONS: Dict[str, List[str]] = {
    "architecture_sensitive": [
        "*", "inject_context", "permission_change", "influence_change",
        "memory_change", "governance_cycle", "score_emit",
    ],
    "correction_response": [
        "*", "close_correction", "score_emit", "inject_context",
    ],
    "memory_promotion": [
        "*", "memory_change", "promote_memory", "learn_from_trace",
    ],
    "tool_abstention": [
        "*", "run_tool", "score_emit",
    ],
}


def _normalize_invariant(inv: Any) -> Dict[str, Any]:
    """Coerce one invariant (dict or dataclass) into the local schema.

    Local schema: {"id", "statement", "families"} where "families" is the
    action-class list (`applies_to` from N1).
    """
    if isinstance(inv, dict):
        return {
            "id": inv.get("id") or inv.get("invariant_id") or "CTXDNA-INV-UNKNOWN",
            "statement": inv.get("statement")
                          or inv.get("rule_text")
                          or inv.get("text")
                          or inv.get("name")
                          or "",
            "families": list(
                inv.get("families")
                or inv.get("applies_to")
                or []
            ),
            "severity": inv.get("severity"),
        }
    # dataclass / object
    return {
        "id": getattr(inv, "id", "CTXDNA-INV-UNKNOWN"),
        "statement": (
            getattr(inv, "rule_text", None)
            or getattr(inv, "statement", None)
            or getattr(inv, "name", None)
            or ""
        ),
        "families": list(
            getattr(inv, "applies_to", None)
            or getattr(inv, "families", None)
            or []
        ),
        "severity": getattr(inv, "severity", None),
    }


def _load_invariants() -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load invariants from memory.invariants if shipped, else fallback.

    Returns (invariants_list, fixme_notes). Never raises. Each entry is a dict
    with id/statement/families/severity. "families" carries the underlying
    action-class taxonomy from N1 (`applies_to`).
    """
    fixmes: List[str] = []
    try:
        from memory.invariants import INVARIANTS  # type: ignore[attr-defined]
        # Support both list and dict-keyed-by-id shapes.
        iterable = (
            INVARIANTS.values() if isinstance(INVARIANTS, dict) else INVARIANTS
        )
        normalized = [_normalize_invariant(inv) for inv in iterable]
        if not normalized:
            fixmes.append(
                "FIXME(N1): memory.invariants imported but empty — using fallback"
            )
            return _FALLBACK_INVARIANTS, fixmes
        return normalized, fixmes
    except ImportError:
        fixmes.append(
            "FIXME(N1): memory.invariants not yet shipped — using hardcoded "
            f"fallback ({len(_FALLBACK_INVARIANTS)} CTXDNA-INV-* IDs)"
        )
        return _FALLBACK_INVARIANTS, fixmes
    except Exception as e:  # pragma: no cover — defensive
        fixmes.append(
            f"FIXME(N1): memory.invariants import failed ({e!r}) — using fallback"
        )
        return _FALLBACK_INVARIANTS, fixmes


# ---------------------------------------------------------------------------
# Section family mapping — minimal sufficient context per task family
# ---------------------------------------------------------------------------
# Aligned with CLAUDE.md WEBHOOK SECTIONS:
#   0 SAFETY | 1 FOUNDATION | 2 WISDOM | 3 AWARENESS | 4 DEEP_CONTEXT
#   5 PROTOCOL | 6 HOLISTIC | 7 FULL_LIBRARY | 8 8TH_INTELLIGENCE | 10 STRATEGIC
#
# The point of Arm C is MINIMAL sufficient context. Drop everything that
# isn't task-relevant — that's the trial.
_FAMILY_TO_SECTIONS: Dict[str, List[int]] = {
    "architecture_sensitive": [5, 2, 10],   # PROTOCOL + WISDOM + STRATEGIC
    "correction_response":    [2, 6],        # WISDOM + HOLISTIC
    "memory_promotion":       [0, 5],        # SAFETY + PROTOCOL
    "tool_abstention":        [5],           # PROTOCOL only
}


def _sections_for_family(family: str) -> List[int]:
    """Return list of section indices to include for this task family."""
    return list(_FAMILY_TO_SECTIONS.get(family, [5]))  # default: PROTOCOL only


# ---------------------------------------------------------------------------
# Webhook section generation (read-only — uses memory.persistent_hook_structure)
# ---------------------------------------------------------------------------
def _generate_webhook_section(section_idx: int, prompt: str) -> Tuple[str, str]:
    """Call the corresponding generate_section_N from persistent_hook_structure.

    Returns (content, source). On any failure, returns a clear degradation stub
    per the Cycle 7 G3-G5 pattern.
    """
    try:
        from memory.persistent_hook_structure import (  # type: ignore[attr-defined]
            generate_section_0,
            generate_section_2,
            generate_section_5,
            generate_section_6,
            generate_section_10,
        )
        from memory.webhook_types import InjectionConfig, RiskLevel
    except Exception as e:
        return (
            f"[s{section_idx}:trial degraded - import failed: {e!r}]",
            "import_error",
        )

    try:
        cfg = InjectionConfig()
        # Most generators take (prompt, config) or (risk, config) or (prompt, session_id, config)
        if section_idx == 0:
            content = generate_section_0(prompt, cfg)
        elif section_idx == 2:
            content = generate_section_2(prompt, cfg)
        elif section_idx == 5:
            # PROTOCOL: signature is (risk_level, config). Use MODERATE for trials.
            content = generate_section_5(RiskLevel.MODERATE, cfg)
        elif section_idx == 6:
            content = generate_section_6(prompt, None, cfg)
        elif section_idx == 10:
            content = generate_section_10(prompt, None, cfg)
        else:
            return (
                f"[s{section_idx}:trial degraded - unsupported section index]",
                "unsupported",
            )

        if not content or not str(content).strip():
            return (
                f"[s{section_idx}:trial degraded - generator returned empty]",
                "empty",
            )
        return str(content), "live"
    except Exception as e:
        return (
            f"[s{section_idx}:trial degraded - generator raised: {e!r}]",
            "exception",
        )


# ---------------------------------------------------------------------------
# Arm composition
# ---------------------------------------------------------------------------
def _select_invariants(family: str, task_known_invariants: List[str],
                        all_invariants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pick CTXDNA-INV-* entries that apply to this task family.

    An invariant applies if any of:
      - its `families` (action-class) list intersects the actions this family
        tends to perform (see _FAMILY_TO_ACTIONS),
      - its `families` list contains the task family name directly (legacy
        / fallback shape),
      - it carries the wildcard "*" (universal invariants like the ledger),
      - the task's `known_invariants` list contains a matching statement
        substring (forces inclusion regardless of taxonomy).
    """
    actions_for_family = set(_FAMILY_TO_ACTIONS.get(family, ["*"]))
    actions_for_family.add(family)  # support legacy fallback shape

    known_statements = [s.lower() for s in (task_known_invariants or [])]

    selected: List[Dict[str, Any]] = []
    seen: set = set()
    for inv in all_invariants:
        inv_actions = set(inv.get("families") or [])
        applies = bool(actions_for_family & inv_actions) or "*" in inv_actions
        stmt = (inv.get("statement", "") or "").lower()
        statement_match = any(ks and ks in stmt for ks in known_statements)
        if (applies or statement_match) and inv["id"] not in seen:
            selected.append(inv)
            seen.add(inv["id"])
    return selected


def _evidence_threshold(task: Dict[str, Any]) -> Dict[str, Any]:
    """Derive evidence threshold from required_context_items count + difficulty."""
    items = task.get("required_context_items") or []
    diff = (task.get("difficulty") or "medium").lower()
    base = {"low": 0.5, "medium": 0.65, "high": 0.8, "critical": 0.9}.get(diff, 0.65)
    return {
        "required_context_items": list(items),
        "min_sufficiency_score": base,
        "rationale": (
            f"derived from difficulty={diff} and "
            f"{len(items)} required_context_items"
        ),
    }


def _project_summary() -> str:
    """Tiny generic project summary used in arm B (placeholder for v1)."""
    return (
        "Project summary: Context DNA — autonomous architecture brain. "
        "9-section webhook injects governed context (Safety, Foundation, "
        "Wisdom, Awareness, Deep Context, Protocol, Holistic, Full Library, "
        "Strategic). Multi-fleet (3 macs) coordinates via NATS. Constitutional "
        "physics: preserve determinism, evidence over confidence, minimalism."
    )


def _arm_a(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prompt": task["task_prompt"],
        "arm": "A_raw",
        "task_id": task["task_id"],
        "packet_version": PACKET_VERSION,
        "sections_included": [],
        "degradations": [],
        "fixmes": [],
    }


def _arm_b(task: Dict[str, Any]) -> Dict[str, Any]:
    summary = _project_summary()
    prompt = f'{task["task_prompt"]}\n\n{summary}'
    return {
        "prompt": prompt,
        "arm": "B_generic_context",
        "task_id": task["task_id"],
        "packet_version": PACKET_VERSION,
        "sections_included": ["project_summary_stub"],
        "degradations": [],
        "fixmes": [
            "FIXME(v1): arm B currently uses a static project summary. "
            "v1 should pull a real generic-context blob (e.g. README + recent commits)."
        ],
    }


def _record_arm_c_permission_denial(
    *,
    evidence_ledger: Any,
    task: Dict[str, Any],
    capability: str,
    actor: str,
    permission_entry: Any,
) -> None:
    """Write a ``permission_denial_recorded`` audit record into the
    EvidenceLedger when the trialbench arm-C gate blocks emission.

    Mirrors ``chief_audit._record_permission_denial`` exactly. ZSF: any
    failure here bumps ``trialbench_arm_c_denial_record_errors_total`` and
    is logged. Denial enforcement (caller skipping the live packet build)
    happens regardless — the ledger write is auxiliary book-keeping.
    """
    try:
        from memory.evidence_ledger import EvidenceKind  # type: ignore
        kind: Any = EvidenceKind.AUDIT
    except Exception as exc:
        log.warning(
            "EvidenceKind import failed (%s) — falling back to literal "
            "kind='audit' for permission denial",
            exc,
        )
        kind = "audit"

    content: Dict[str, Any] = {
        "event_type": "permission_denial_recorded",
        "subject": f"trialbench_arm_c:{task.get('task_id', 'unknown')}",
        "actor": actor,
        "capability": capability,
        "blocked_decision": "C_governed",
        "task_id": task.get("task_id", "unknown"),
        "family": task.get("family", "unknown"),
        "permission_status": getattr(
            getattr(permission_entry, "status", None), "value", "denied"
        ),
        "permission_reason": getattr(permission_entry, "reason", ""),
        "summary": (
            f"permission_denial_recorded: {actor}/{capability} "
            f"blocked decision=C_governed task={task.get('task_id', 'unknown')}"
        ),
    }
    try:
        evidence_ledger.record(content=content, kind=kind)
        # Bump the gate-side denials_recorded counter (lazy import — gate
        # may not be on path in lean trial environments).
        try:
            from multifleet.permission_gate import note_denial_recorded
            note_denial_recorded()
        except Exception as inner_exc:
            log.warning(
                "permission_gate.note_denial_recorded import failed: %s",
                inner_exc,
            )
    except Exception as exc:
        _bump_arm_c_counter("trialbench_arm_c_denial_record_errors_total")
        log.warning(
            "permission_denial_recorded ledger write failed for "
            "task=%s actor=%s capability=%s: %s",
            task.get("task_id", "unknown"), actor, capability, exc,
        )


def _arm_c(
    task: Dict[str, Any],
    *,
    permission_governor: Any = None,
    evidence_ledger: Any = None,
) -> Dict[str, Any]:
    """Build the arm-C governed packet for a trial run.

    BB1 Phase-9 permission-gate wiring (Y3 follow-up to AA2): when
    ``permission_governor`` is supplied, the arm-C emission is gated
    against the latest snapshot BEFORE the (expensive) section
    composition runs. Denied emissions return a degraded packet stub
    and (when ``evidence_ledger`` is supplied) record a
    ``permission_denial_recorded`` audit entry.

    Backwards-compat invariant: when either kwarg is ``None``, behaviour
    is byte-identical to the pre-Phase-9 code path. The gate function
    itself returns ``(True, None)`` when the governor is missing.
    """
    family = task.get("family") or "architecture_sensitive"
    section_indices = _sections_for_family(family)
    prompt = task["task_prompt"]

    # ── BB1 Phase-9 permission-gate ────────────────────────────────────
    # Lazy import — keeps trialbench dependency-light when multifleet is
    # not on the path (tools/ standalone runs).
    try:
        from multifleet.permission_gate import gate_packet  # type: ignore
    except Exception as exc:
        gate_packet = None  # type: ignore
        if permission_governor is not None:
            log.warning(
                "permission_gate import failed (%s) — defaulting to "
                "GRANTED for task=%s",
                exc, task.get("task_id", "unknown"),
            )

    # Pre-build a packet-shaped object for derivation (no network/I/O).
    _gate_proxy = type("_ArmCProxy", (), {
        "arm": "C_governed",
        "source_schema": "trialbench",
        "agent_identity_json": json.dumps({"role": "trial_runner"}),
    })()

    if gate_packet is not None:
        gate_allowed, gate_entry = gate_packet(
            _gate_proxy,
            capability=TRIALBENCH_ARM_C_CAPABILITY,
            actor=TRIALBENCH_ARM_C_ACTOR,
            governor=permission_governor,
        )
        _bump_arm_c_counter("trialbench_arm_c_decisions_gated_total")
        if not gate_allowed:
            _bump_arm_c_counter("trialbench_arm_c_decisions_denied_total")
            log.warning(
                "trialbench arm_c GATED-DENIED task=%s family=%s "
                "capability=%s actor=%s",
                task.get("task_id", "unknown"), family,
                TRIALBENCH_ARM_C_CAPABILITY, TRIALBENCH_ARM_C_ACTOR,
            )
            if evidence_ledger is not None:
                _record_arm_c_permission_denial(
                    evidence_ledger=evidence_ledger,
                    task=task,
                    capability=TRIALBENCH_ARM_C_CAPABILITY,
                    actor=TRIALBENCH_ARM_C_ACTOR,
                    permission_entry=gate_entry,
                )
            else:
                log.warning(
                    "trialbench arm_c denial NOT recorded — "
                    "evidence_ledger=None (task=%s)",
                    task.get("task_id", "unknown"),
                )
            return {
                "prompt": "",
                "arm": "C_governed",
                "task_id": task.get("task_id", "unknown"),
                "packet_version": PACKET_VERSION,
                "sections_included": [],
                "degradations": [
                    "permission_denial_recorded:trialbench_emit_governed_arm_c"
                ],
                "fixmes": [],
                "permission_denied": True,
            }
    # ── End permission gate ────────────────────────────────────────────

    sections: List[Dict[str, Any]] = []
    degradations: List[str] = []
    for idx in section_indices:
        content, source = _generate_webhook_section(idx, prompt)
        sections.append({"section": idx, "source": source, "content": content})
        if source != "live":
            degradations.append(f"s{idx}:{source}")

    invariants_all, fixmes = _load_invariants()
    selected_invariants = _select_invariants(
        family, task.get("known_invariants") or [], invariants_all
    )
    evidence = _evidence_threshold(task)

    task_world = {
        "task_id": task["task_id"],
        "title": task.get("title", ""),
        "family": family,
        "difficulty": task.get("difficulty", "medium"),
        "success_criteria": list(task.get("success_criteria") or []),
        "known_failure_modes": list(task.get("known_failure_modes") or []),
        "known_invariants": list(task.get("known_invariants") or []),
    }

    agent_identity = {
        "name": "ctxdna-trial-agent",
        "current_ascension_level": 2,
        "ascension_label": "Evidence-Aware",
        "role": "builder",
        "_note": "placeholder — full identity registry lands in v1",
    }

    required_output_structure = (
        "Brief summary, then code, then 'Tests/Build:' line, then "
        "'Remaining uncertainty:' line"
    )

    composed_prompt_parts: List[str] = []
    composed_prompt_parts.append(f"# Task: {task_world['title']}")
    composed_prompt_parts.append(task["task_prompt"])
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Task World")
    composed_prompt_parts.append(json.dumps(task_world, indent=2))
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Agent Identity")
    composed_prompt_parts.append(json.dumps(agent_identity, indent=2))
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Minimal Sufficient Context")
    for s in sections:
        composed_prompt_parts.append(f"### Section {s['section']} ({s['source']})")
        composed_prompt_parts.append(s["content"])
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Invariants (must-preserve)")
    for inv in selected_invariants:
        composed_prompt_parts.append(f"- [{inv['id']}] {inv['statement']}")
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Evidence Threshold")
    composed_prompt_parts.append(json.dumps(evidence, indent=2))
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Known Failure Modes (avoid)")
    for fm in task_world["known_failure_modes"]:
        composed_prompt_parts.append(f"- {fm}")
    composed_prompt_parts.append("")
    composed_prompt_parts.append("## Required Output Structure")
    composed_prompt_parts.append(required_output_structure)

    composed_prompt = "\n".join(composed_prompt_parts)

    return {
        "prompt": composed_prompt,
        "arm": "C_governed",
        "task_id": task["task_id"],
        "packet_version": PACKET_VERSION,
        "sections_included": [s["section"] for s in sections],
        "task_world": task_world,
        "agent_identity": agent_identity,
        "minimal_sufficient_context": sections,
        "invariants": selected_invariants,
        "evidence_threshold": evidence,
        "failure_modes": task_world["known_failure_modes"],
        "required_output_structure": required_output_structure,
        "degradations": degradations,
        "fixmes": fixmes,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_governed_packet(
    task: Dict[str, Any],
    arm: str,
    *,
    permission_governor: Any = None,
    evidence_ledger: Any = None,
) -> Dict[str, Any]:
    """Build the prompt packet for a trial run.

    Args:
        task: dict from task_bank.json (task_id, family, task_prompt, ...).
        arm:  one of "A_raw", "B_generic_context", "C_governed".
        permission_governor: optional ``PermissionGovernor`` for arm-C
            write-side gating (BB1 Phase-9). When ``None``, behaviour is
            byte-identical to the pre-Phase-9 code path.
        evidence_ledger: optional ledger for ``permission_denial_recorded``
            audit writes. Only consulted on arm-C denial.

    Returns:
        dict with at least: prompt, arm, task_id, packet_version, sections_included.
        Arm C also includes task_world, minimal_sufficient_context, invariants,
        evidence_threshold, failure_modes, required_output_structure.

    Never raises — degraded sources surface in packet["degradations"] /
    packet["fixmes"]. Caller (N2) must inspect those before claiming a trial
    ran with a fully-live governed packet.
    """
    if not isinstance(task, dict) or "task_prompt" not in task or "task_id" not in task:
        # Degraded but observable — return a stub packet rather than crash trial run.
        return {
            "prompt": "",
            "arm": arm,
            "task_id": (task or {}).get("task_id", "unknown"),
            "packet_version": PACKET_VERSION,
            "sections_included": [],
            "degradations": ["packet:trial degraded - invalid task dict"],
            "fixmes": [],
        }

    if arm == "A_raw":
        return _arm_a(task)
    if arm == "B_generic_context":
        return _arm_b(task)
    if arm == "C_governed":
        return _arm_c(
            task,
            permission_governor=permission_governor,
            evidence_ledger=evidence_ledger,
        )

    return {
        "prompt": task.get("task_prompt", ""),
        "arm": arm,
        "task_id": task["task_id"],
        "packet_version": PACKET_VERSION,
        "sections_included": [],
        "degradations": [f"packet:trial degraded - unknown arm '{arm}'"],
        "fixmes": [],
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import argparse

    p = argparse.ArgumentParser(description="TrialBench governed packet smoke test")
    p.add_argument("--task-id", default="arch_001")
    p.add_argument("--arm", default="C_governed",
                   choices=["A_raw", "B_generic_context", "C_governed"])
    p.add_argument("--task-bank",
                   default=os.path.join(
                       os.path.dirname(os.path.abspath(__file__)),
                       "..", "docs", "dao", "task_bank.json"))
    args = p.parse_args()

    with open(args.task_bank, "r", encoding="utf-8") as f:
        bank = json.load(f)
    matches = [t for t in bank if t.get("task_id") == args.task_id]
    if not matches:
        print(f"task_id {args.task_id} not found in {args.task_bank}", file=sys.stderr)
        sys.exit(2)
    packet = build_governed_packet(matches[0], args.arm)
    print(json.dumps({
        "arm": packet["arm"],
        "task_id": packet["task_id"],
        "prompt_chars": len(packet["prompt"]),
        "sections_included": packet["sections_included"],
        "degradations": packet.get("degradations", []),
        "fixmes": packet.get("fixmes", []),
    }, indent=2))
