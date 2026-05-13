#!/usr/bin/env python3
"""One-shot fleet-audit tick. Modeled on scripts/docker-health-notify.py.

Trigger sources (NOT a polling loop):
  - xbar plugin: ~/Library/Application Support/xbar/plugins/fleet-audit-watchdog.30s.sh
  - git post-commit hook
  - git post-merge hook
  - daemon NATS subscriber on fleet.audit.finding.* (calls this script)
  - fswatch on .fleet/HALT or .fleet/priorities/*

Each tick:
  1. Run the detector suite from multifleet.risk_auditor
  2. Diff against /tmp/fleet-audit-state-<detector>.json (state-change-only)
  3. For NEW findings, call audit_consult per finding (DeepSeek-direct, P3)
  4. Append to .fleet/audits/<date>-findings.md
  5. If chief AND any cluster wants HALT/ROLLBACK, run chief loop and write
     .fleet/audits/<date>-decisions.md + set .fleet/HALT
  6. Exit. No daemon, no Claude session, no polling.

Usage:
  fleet-audit-tick.py [--node mac1] [--repo /path/to/repo] [--source xbar]
  fleet-audit-tick.py --json              # report findings as JSON, no log writes
  fleet-audit-tick.py --tick-source git-post-commit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

# Allow running from a checkout without install.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "multi-fleet"))

from multifleet.risk_auditor import (  # noqa: E402
    AuditFinding, Severity, run_detector_suite,
    TICK_WALL_CLOCK_S, get_counters, _bump,
)
from multifleet.audit_consult import consult_finding  # noqa: E402
from multifleet.audit_log import append_findings, set_halt  # noqa: E402
from multifleet.chief_audit import (  # noqa: E402
    process_findings_batch,
    DECISION_HALT,
    DECISION_ROLLBACK,
)


# R-0018: hard SIGALRM backstop — fires even if a detector wedges below
# Python (e.g. a urllib3 bug, a getaddrinfo loop, a kqueue). 2x the soft
# wall-clock so the in-suite logic gets first crack at clean shutdown.
TICK_HARD_TIMEOUT_S = max(int(TICK_WALL_CLOCK_S * 2), 30)


class TickTimeout(Exception):
    """Raised by the SIGALRM handler when the tick exceeds its budget."""


def _install_tick_alarm(seconds: int) -> bool:
    """Install a SIGALRM hard timeout. Returns True if installed.

    Returns False on platforms without SIGALRM (Windows) or when the alarm
    cannot be set (we are not on the main thread). The in-suite soft
    wall-clock guard remains active either way.
    """
    if not hasattr(signal, "SIGALRM"):
        return False
    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(seconds)
        return True
    except (ValueError, OSError):
        return False


def _clear_tick_alarm() -> None:
    if hasattr(signal, "SIGALRM"):
        try:
            signal.alarm(0)
        except (ValueError, OSError):
            pass


def _alarm_handler(signum, frame) -> None:  # noqa: ARG001
    raise TickTimeout(
        f"audit-tick exceeded SIGALRM hard timeout {TICK_HARD_TIMEOUT_S}s"
    )


logger = logging.getLogger("fleet.audit_tick")
logging.basicConfig(
    level=os.environ.get("FLEET_AUDIT_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


STATE_DIR = Path("/tmp")
STATE_PREFIX = "fleet-audit-state"


# ── State diff ─────────────────────────────────────────────────────────────


def load_state(node_id: str) -> dict:
    """Load per-detector last-seen state. One file per node so sibling
    nodes on the same machine (rare) don't clobber."""
    p = STATE_DIR / f"{STATE_PREFIX}-{node_id}.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("state load failed: %s — starting fresh", exc)
        return {}


def save_state(node_id: str, state: dict) -> None:
    p = STATE_DIR / f"{STATE_PREFIX}-{node_id}.json"
    try:
        p.write_text(json.dumps(state, default=str), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("state save failed: %s", exc)


def load_seen_findings(node_id: str) -> set:
    """Set of finding_ids we have already reported. Prevents re-emitting on
    every tick when state has not changed."""
    p = STATE_DIR / f"{STATE_PREFIX}-{node_id}-seen.json"
    if not p.is_file():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen_findings(node_id: str, seen: set) -> None:
    p = STATE_DIR / f"{STATE_PREFIX}-{node_id}-seen.json"
    try:
        # Cap the seen-set so it does not grow forever; keep last 5k ids.
        keep = list(seen)[-5000:]
        p.write_text(json.dumps(keep), encoding="utf-8")
    except Exception:
        pass


# ── Chief detection ────────────────────────────────────────────────────────


def is_chief(node_id: str, repo: Path) -> bool:
    """Is this node the chief per .multifleet/config.json?"""
    cfg_path = repo / ".multifleet" / "config.json"
    if not cfg_path.is_file():
        return False
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return str(cfg.get("chief", {}).get("nodeId", "")) == node_id
    except Exception:
        return False


# ── Main ───────────────────────────────────────────────────────────────────


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--node", default=os.environ.get("MULTIFLEET_NODE_ID")
                   or socket.gethostname().split(".")[0])
    p.add_argument("--repo", default=str(REPO_ROOT))
    p.add_argument("--source", default="cli",
                   help="trigger source label (xbar/git-post-commit/cli/...)")
    p.add_argument("--json", action="store_true",
                   help="print findings as JSON only; no log writes, no consults")
    p.add_argument("--no-consult", action="store_true",
                   help="skip per-finding 3s consult (still writes findings log)")
    p.add_argument("--no-chief", action="store_true",
                   help="skip chief aggregator even if this node is chief")
    args = p.parse_args(argv)

    repo = Path(args.repo)
    node_id = args.node
    state = load_state(node_id)
    seen = load_seen_findings(node_id)

    # R-0018: hard SIGALRM backstop guarantees the tick CANNOT wedge
    # forever even if a detector hits a network path that ignores both
    # the per-call timeout and the in-suite wall-clock guard.
    alarm_installed = _install_tick_alarm(TICK_HARD_TIMEOUT_S)
    tick_started = time.monotonic()
    try:
        findings = run_detector_suite(repo, state=state, node_id=node_id)
    except TickTimeout as exc:
        _bump("tick_wall_clock_exceeded_total")
        logger.error("tick: %s", exc)
        # Emit a synthetic META finding so Aaron sees the wedge in the
        # audit log even if state.save below races.
        findings = [AuditFinding.make(
            detector="META", klass="meta",
            title=f"audit-tick SIGALRM hard timeout ({TICK_HARD_TIMEOUT_S}s)",
            evidence={
                "elapsed_s": round(time.monotonic() - tick_started, 2),
                "limit_s": TICK_HARD_TIMEOUT_S,
                "source": args.source,
            },
            severity=Severity.CRITICAL, node_id=node_id,
        )]
    finally:
        if alarm_installed:
            _clear_tick_alarm()
    # Persist counters so the daemon /health surface can pick them up.
    state["_audit_counters"] = get_counters()
    state["_audit_last_tick_s"] = round(time.monotonic() - tick_started, 3)
    save_state(node_id, state)

    new_findings = [f for f in findings if f.finding_id not in seen]
    seen.update(f.finding_id for f in new_findings)
    save_seen_findings(node_id, seen)

    if args.json:
        print(json.dumps([{
            "id": f.finding_id, "detector": f.detector, "class": f.klass,
            "severity": Severity(f.severity).name, "title": f.title,
            "evidence": f.evidence,
        } for f in new_findings], indent=2))
        return 0

    if not new_findings:
        logger.info("tick: no new findings (source=%s, total=%d, seen=%d)",
                    args.source, len(findings), len(seen))
        return 0

    # Per-finding 3s consult (DeepSeek-direct, ~$0.012/finding)
    verdicts: dict = {}
    if not args.no_consult:
        for f in new_findings:
            try:
                v = consult_finding({
                    "finding_id": f.finding_id, "detector": f.detector,
                    "klass": f.klass, "severity": int(f.severity),
                    "title": f.title, "evidence": f.evidence,
                })
                verdicts[f.finding_id] = v
                # Per-node 3s vote can up- or down-grade severity.
                if v["cardio"] == "ELEVATE_TO_CRITICAL" or v["neuro"] == "ELEVATE_TO_CRITICAL":
                    if f.severity < Severity.CRITICAL:
                        f.severity = Severity.CRITICAL
                if v["cardio"] == "DISMISS_AS_NOISE" and v["neuro"] == "DISMISS_AS_NOISE":
                    f.severity = Severity.INFO
            except Exception as exc:  # noqa: BLE001
                logger.warning("consult failed for %s: %s", f.finding_id, exc)
                verdicts[f.finding_id] = {"errors": [f"exception:{exc}"]}

    log_path = append_findings(repo, new_findings, per_node_verdicts=verdicts)
    logger.info("tick: wrote %d new finding(s) to %s (source=%s)",
                len(new_findings), log_path, args.source)

    # Chief node runs the aggregator on findings that look serious.
    if is_chief(node_id, repo) and not args.no_chief:
        actionable = [f for f in new_findings
                      if f.severity >= Severity.WARN and f.klass != "meta"]
        if actionable:
            decisions = process_findings_batch(
                repo, actionable,
                consult_fn=_chief_consult_factory(),
                halted_by=node_id,
            )
            for d in decisions:
                logger.info("chief decision: %s (consensus=%.2f, iters=%d)",
                            d.decision, d.consensus, d.iterations)
            if any(d.decision in (DECISION_HALT, DECISION_ROLLBACK) for d in decisions):
                # Re-emit as ZSF marker; xbar/Discord pickup
                logger.warning("CHIEF HALT/ROLLBACK — see .fleet/audits/")

    return 0


def _chief_consult_factory():
    """Return a chief-side consult fn that hits DeepSeek for both surgeons.

    Builds on audit_consult.consult_finding by passing the cluster as a
    pseudo-finding (the chief loop expects a topic+evidence dict).
    """
    def consult(topic: str, evidence: dict) -> dict:
        finding_dict = {
            "finding_id": "CLUSTER",
            "detector": "CHIEF",
            "klass": "cluster",
            "severity": 1,
            "title": topic[:200],
            "evidence": evidence,
        }
        return consult_finding(finding_dict)
    return consult


if __name__ == "__main__":
    raise SystemExit(main())
