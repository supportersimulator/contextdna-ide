"""HALT path live-fire test — round-9 audit pipeline verification.

The H4 round-8 live-fire (commit b2af760f8) exercised ESCALATE_TO_RED + ACCEPT
decisions end-to-end with a real DeepSeek consult, but DECISION_HALT was
never reached. This script forces a HALT cluster and verifies:

  1. ``chief_audit.process_findings_batch`` writes ``.fleet/HALT`` with timestamp,
     halted_by, and a cluster-summary reason.
  2. ``green_light_runner.claim_next`` refuses claims while HALT is set.
  3. After HALT removal, ``claim_next`` succeeds again.

ISOLATION GUARANTEE
-------------------
The chief loop and the green-light runner are pointed at temporary directories.
The live superrepo's ``.fleet/HALT`` is **never** touched, even on test failure.
A try/finally guarantees cleanup of any HALT artefacts.

DeepSeek consults are real (live-fire). We use synthetic D-04 findings that
strongly signal ELEVATE_TO_CRITICAL so the chief 3s loop converges quickly.

USAGE
-----
    cd <superrepo>
    multi-fleet/venv.nosync/bin/python3 scripts/tests/test_halt_live_fire.py

Exit codes:
  0 — HALT path verified end-to-end
  2 — HALT decision reached but flag write or refusal contract failed
  3 — HALT decision not reached (cost spent, no contract verified)
  4 — DeepSeek key not available (test skipped)
  5 — Other infra / setup failure
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Repo-relative imports ──────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[2]
MULTIFLEET = REPO / "multi-fleet"
sys.path.insert(0, str(MULTIFLEET))

from multifleet.risk_auditor import (  # noqa: E402
    AuditFinding,
    CLASS_DEGRADATION,
    Severity,
)
from multifleet.audit_log import (  # noqa: E402
    clear_halt,
    is_halted,
)
from multifleet.chief_audit import (  # noqa: E402
    DECISION_HALT,
    DECISION_ROLLBACK,
    process_findings_batch,
)
from multifleet.audit_consult import (  # noqa: E402
    consult_finding,
    _resolve_api_key,
)


# ── Cost tracking ──────────────────────────────────────────────────────────

COST_PER_CONSULT_USD = 0.012  # ~$0.024 for cardio+neuro pair, conservative
MAX_COST_USD = 0.50

_consult_count = 0


def _bump_consult_count() -> None:
    global _consult_count
    _consult_count += 1


def estimated_cost_usd() -> float:
    return _consult_count * COST_PER_CONSULT_USD * 2  # cardio + neuro per consult


# ── Synthetic findings ─────────────────────────────────────────────────────


def make_halt_cluster(node_id: str = "test-m3") -> List[AuditFinding]:
    """Build 2 synthetic D-04 webhook-silence findings, both CRITICAL.

    Same (detector, klass) so they cluster into a single C-D-04-degradation
    cluster — the chief loop will run ONCE and decide HALT.
    """
    return [
        AuditFinding.make(
            detector="D-04",
            klass=CLASS_DEGRADATION,
            title=(
                "[SYNTHETIC HALT TEST] webhook events flatlined for 720s — "
                "Atlas blind to Aaron prompts; CORE VALUE DEGRADED"
            ),
            evidence={
                "synthetic": True,
                "test_id": "halt-live-fire-m3",
                "events_recorded_now": 0,
                "events_recorded_prev": 247,
                "silence_duration_s": 720,
                "daemon_health": "up",
                "subscription_count": 0,
                "impact": (
                    "Context DNA webhook is the #1 priority surface. "
                    "Sustained silence > 5min while daemon is up means the "
                    "producer-or-bus is broken; green-light claims must halt "
                    "until restored. ELEVATE_TO_CRITICAL is the correct call."
                ),
            },
            severity=Severity.CRITICAL,
            node_id=node_id,
        ),
        AuditFinding.make(
            detector="D-04",
            klass=CLASS_DEGRADATION,
            title=(
                "[SYNTHETIC HALT TEST] webhook silence latch fired second tick — "
                "still no recovery; halt green-light pool"
            ),
            evidence={
                "synthetic": True,
                "test_id": "halt-live-fire-m3",
                "events_recorded_now": 0,
                "events_recorded_prev": 247,
                "silence_duration_s": 1320,  # 22min — escalating
                "consecutive_ticks_silent": 2,
                "impact": (
                    "Two consecutive D-04 fires confirm the webhook bus is "
                    "down, not a one-shot blip. Halt is required."
                ),
            },
            severity=Severity.CRITICAL,
            node_id=node_id,
        ),
    ]


# ── Real consult fn (live DeepSeek) ────────────────────────────────────────


def make_live_consult_fn(api_key: str, max_consults: int = 8):
    """Build a chief consult fn that calls real DeepSeek per cluster iter.

    The chief loop calls this once per iteration with the (topic, evidence).
    We unwrap the cluster's first finding into a dict and call
    ``consult_finding`` for cardio+neuro.

    Per-cluster cost is bounded by ``max_consults`` to enforce budget.
    """
    def fn(topic: str, evidence: dict) -> dict:
        _bump_consult_count()
        if _consult_count > max_consults:
            raise RuntimeError(
                f"consult budget exhausted ({max_consults}); "
                f"abort to stay under ${MAX_COST_USD}"
            )
        # Pull a representative finding from the evidence to give DeepSeek
        # the most signal. The chief loop already deduped + clustered these,
        # so all findings in this cluster are D-04 + CLASS_DEGRADATION.
        findings = evidence.get("findings", [])
        if not findings:
            return {
                "cardio": "DISMISS_AS_NOISE",
                "neuro": "DISMISS_AS_NOISE",
                "consensus": 0.0,
                "rationale": "no findings in evidence (test bug)",
            }
        rep = findings[0]
        # Augment with cluster context so DeepSeek sees the multi-finding signal.
        finding_dict = {
            "finding_id": rep.get("id"),
            "detector": rep.get("detector"),
            "klass": rep.get("class"),
            "severity": rep.get("severity"),
            "title": (
                f"[CLUSTER OF {len(findings)} D-04 FINDINGS — HALT TEST] "
                + str(rep.get("title", ""))
            ),
            "evidence": {
                **rep.get("evidence", {}),
                "cluster_size": len(findings),
                "all_severities_critical": all(
                    f.get("severity") == "CRITICAL" for f in findings
                ),
                "topic_from_chief": topic[:300],
                "prior_iterations": len(evidence.get("transcript", [])),
            },
        }
        result = consult_finding(finding_dict, api_key=api_key)
        # `consult_finding` already returns the shape the chief loop expects.
        return result
    return fn


# ── Phase A: chief HALT write ──────────────────────────────────────────────


def phase_a_chief_halt_write(scratch: Path, api_key: str) -> Dict[str, Any]:
    """Run process_findings_batch on a synthetic D-04 cluster.

    Returns a dict of evidence: decisions, halt-flag presence + content,
    consensus, iterations, cost.
    """
    print("\n[Phase A] Running chief loop on synthetic D-04 CRITICAL cluster…")
    findings = make_halt_cluster(node_id="m3-halt-test")
    print(f"  Cluster: {len(findings)} findings, all D-04 CRITICAL.")

    consult_fn = make_live_consult_fn(api_key)
    t0 = time.time()
    decisions = process_findings_batch(
        scratch, findings, consult_fn, halted_by="m3-halt-live-fire-test",
    )
    elapsed = time.time() - t0

    halt_path = scratch / ".fleet" / "HALT"
    halt_present = halt_path.is_file()
    halt_content = halt_path.read_text(encoding="utf-8") if halt_present else None

    summary = {
        "elapsed_s": round(elapsed, 2),
        "decisions": [
            {
                "cluster_id": d.cluster_id,
                "decision": d.decision,
                "consensus": round(d.consensus, 3),
                "iterations": d.iterations,
                "rationale_excerpt": d.rationale[:200],
                "finding_ids": d.finding_ids,
            }
            for d in decisions
        ],
        "halt_flag_present": halt_present,
        "halt_flag_content": halt_content,
        "halt_flag_path": str(halt_path),
        "is_halted_helper_returns_true": is_halted(scratch),
        "consults_made": _consult_count,
        "estimated_cost_usd": round(estimated_cost_usd(), 4),
    }

    # Locate the decisions doc the chief just wrote.
    decisions_dir = scratch / ".fleet" / "audits"
    if decisions_dir.is_dir():
        for p in sorted(decisions_dir.glob("*-decisions.md")):
            summary["decisions_doc_path"] = str(p)
            summary["decisions_doc_excerpt"] = (
                p.read_text(encoding="utf-8")[:600]
            )
            break

    print(f"  Elapsed: {summary['elapsed_s']}s")
    for d in summary["decisions"]:
        print(
            f"  Decision: {d['cluster_id']} -> {d['decision']} "
            f"(consensus={d['consensus']}, iters={d['iterations']})"
        )
    print(f"  HALT flag present: {halt_present}")
    print(f"  Cost so far: ${summary['estimated_cost_usd']:.4f}")
    return summary


# ── Phase B: green-light runner refusal ────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=check,
        capture_output=True, text=True,
    )


def _init_pool_repo(parent: Path) -> Path:
    """Build a self-contained git pool repo we can claim from."""
    origin = parent / "origin.git"
    work = parent / "work"
    _git(parent, "init", "--bare", str(origin))
    _git(parent, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "halt-test@local")
    _git(work, "config", "user.name", "halt-test")
    _git(work, "checkout", "-b", "main")
    fleet_dir = work / ".fleet" / "priorities"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "green-light.md").write_text(
        "# Pool\n\n## Pool\n\n"
        "- [ ] [G-0001] halt-live-fire-test :: scope=test :: evidence=halt-test\n",
        encoding="utf-8",
    )
    (fleet_dir / "red-light.md").write_text("# Red\n\n", encoding="utf-8")
    _git(work, "add", ".fleet/priorities/")
    _git(work, "commit", "-m", "seed halt-live-fire pool")
    _git(work, "push", "-u", "origin", "main")
    return work


def phase_b_green_light_refuses(scratch: Path, halt_content: str) -> Dict[str, Any]:
    """Set HALT in a fresh pool repo, verify claim_next refuses.

    We don't reuse Phase A's scratch dir for git ops to keep concerns clean.
    """
    print("\n[Phase B] Verifying green_light_runner refuses claims under HALT…")
    pool_dir = scratch / "pool"
    pool_dir.mkdir()
    work = _init_pool_repo(pool_dir)

    # Mirror the HALT content the chief produced in Phase A.
    halt_path = work / ".fleet" / "HALT"
    halt_path.parent.mkdir(parents=True, exist_ok=True)
    halt_path.write_text(halt_content or "halted_at=0\nby=test\nreason=phase-b-test\n",
                         encoding="utf-8")

    # Stub out the live detector suite so Phase B isolates HALT from CRITICAL findings.
    # We want to prove the refusal is BECAUSE OF .fleet/HALT, not because of an
    # incidental CRITICAL detector hit on the throwaway scratch repo.
    from multifleet import risk_auditor as ra
    original_run = ra.run_detector_suite
    ra.run_detector_suite = lambda *a, **kw: []  # type: ignore[assignment]
    try:
        from multifleet.green_light_runner import claim_next
        result = claim_next(work, node_id="m3-halt-test")
    finally:
        ra.run_detector_suite = original_run  # type: ignore[assignment]

    refusal = {
        "claimed": result.claimed,
        "reason": result.reason,
        "halt_in_reason": "halt" in (result.reason or "").lower(),
        "critical_in_reason": "critical" in (result.reason or "").lower(),
    }
    print(f"  claim_next.claimed = {refusal['claimed']}")
    print(f"  claim_next.reason  = {refusal['reason']!r}")
    print(f"  halt-in-reason     = {refusal['halt_in_reason']}")
    return {"pool_repo": str(work), "refusal": refusal}


# ── Phase C: recovery ──────────────────────────────────────────────────────


def phase_c_recovery(pool_repo: Path) -> Dict[str, Any]:
    """Remove HALT, verify claim succeeds."""
    print("\n[Phase C] Removing HALT, verifying claim_next now succeeds…")
    work = Path(pool_repo)
    cleared = clear_halt(work)
    print(f"  clear_halt() returned: {cleared}")

    from multifleet import risk_auditor as ra
    original_run = ra.run_detector_suite
    ra.run_detector_suite = lambda *a, **kw: []  # type: ignore[assignment]
    try:
        from multifleet.green_light_runner import claim_next
        result = claim_next(work, node_id="m3-halt-test")
    finally:
        ra.run_detector_suite = original_run  # type: ignore[assignment]

    recovery = {
        "halt_cleared": cleared,
        "halt_present_after": (work / ".fleet" / "HALT").is_file(),
        "claim_after_recovery": {
            "claimed": result.claimed,
            "reason": result.reason,
            "item_id": result.item.item_id if result.item else None,
        },
    }
    print(
        f"  claim_next.claimed after recovery = {recovery['claim_after_recovery']['claimed']}"
    )
    return recovery


# ── Live-superrepo guard ───────────────────────────────────────────────────


@contextmanager
def live_repo_halt_guard():
    """Snapshot+restore the live superrepo's HALT state, defensively.

    The test should NEVER write to the live ``.fleet/HALT`` (we use temp dirs),
    but we still take a defensive snapshot in case of bugs.
    """
    live_halt = REPO / ".fleet" / "HALT"
    pre_existed = live_halt.is_file()
    pre_content = live_halt.read_text(encoding="utf-8") if pre_existed else None
    try:
        yield
    finally:
        post_existed = live_halt.is_file()
        if post_existed and not pre_existed:
            live_halt.unlink()
            print(
                "[guard] CRITICAL: live ``.fleet/HALT`` was created during test; removed."
            )
        elif not post_existed and pre_existed:
            live_halt.write_text(pre_content or "", encoding="utf-8")
            print(
                "[guard] CRITICAL: live ``.fleet/HALT`` was deleted during test; restored."
            )


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    # Resolve API key BEFORE building scratch dirs so we fail fast.
    api_key = _resolve_api_key()
    if not api_key:
        print("[skip] No DeepSeek key resolvable — HALT live-fire requires real consults.")
        return 4

    findings_seen: Dict[str, Any] = {}
    halt_content: Optional[str] = None
    pool_work_path: Optional[str] = None

    with live_repo_halt_guard():
        scratch = Path(tempfile.mkdtemp(prefix="halt-live-fire-"))
        try:
            phase_a = phase_a_chief_halt_write(scratch, api_key)
            findings_seen["phase_a"] = phase_a

            halt_decisions = [
                d for d in phase_a["decisions"]
                if d["decision"] in (DECISION_HALT, DECISION_ROLLBACK)
            ]
            if not halt_decisions:
                print(
                    "\n[FAIL] Phase A did not produce HALT/ROLLBACK decision. "
                    "Decisions seen: "
                    + ", ".join(d["decision"] for d in phase_a["decisions"])
                )
                _emit_summary(findings_seen, status="HALT_NOT_REACHED")
                return 3

            if not phase_a["halt_flag_present"]:
                print(
                    "\n[FAIL] HALT decision reached but ``.fleet/HALT`` "
                    "flag was not written. P0 finding."
                )
                _emit_summary(findings_seen, status="FLAG_WRITE_BROKEN")
                return 2

            halt_content = phase_a["halt_flag_content"]
            phase_b = phase_b_green_light_refuses(scratch, halt_content or "")
            findings_seen["phase_b"] = phase_b
            pool_work_path = phase_b["pool_repo"]

            if phase_b["refusal"]["claimed"]:
                print(
                    "\n[FAIL P0] green-light claim_next CLAIMED an item "
                    "while ``.fleet/HALT`` was set. Audit gate is broken."
                )
                _emit_summary(findings_seen, status="GREEN_LIGHT_NOT_BLOCKED")
                return 2
            if not phase_b["refusal"]["halt_in_reason"]:
                print(
                    "\n[FAIL] claim_next refused but reason did not mention "
                    f"'halt': {phase_b['refusal']['reason']!r}"
                )
                _emit_summary(findings_seen, status="REFUSAL_REASON_WRONG")
                return 2

            phase_c = phase_c_recovery(Path(pool_work_path))
            findings_seen["phase_c"] = phase_c
            if not phase_c["claim_after_recovery"]["claimed"]:
                print(
                    "\n[FAIL] After clearing HALT, claim_next still refuses: "
                    f"reason={phase_c['claim_after_recovery']['reason']!r}"
                )
                _emit_summary(findings_seen, status="RECOVERY_BROKEN")
                return 2

            _emit_summary(findings_seen, status="PASS")
            return 0
        finally:
            # Always wipe scratch dir and any stray HALT in pool work.
            if pool_work_path and Path(pool_work_path).is_dir():
                clear_halt(Path(pool_work_path))
            shutil.rmtree(scratch, ignore_errors=True)


def _emit_summary(findings: Dict[str, Any], *, status: str) -> None:
    out = {
        "status": status,
        "estimated_cost_usd": round(estimated_cost_usd(), 4),
        "consult_count": _consult_count,
        "max_cost_usd": MAX_COST_USD,
        "phases": findings,
    }
    print("\n──── HALT-LIVE-FIRE TEST SUMMARY ────")
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
