#!/usr/bin/env python3
"""
Multi-Fleet Chief Synthesis — compares verdicts across nodes and produces chief_decision.

When all active nodes have submitted local_verdicts, the chief:
1. Compares summaries, confidence scores, and dissent
2. Runs 3-surgeon cross-examination if any node has dissent or low confidence
3. Emits a chief_decision packet back to all nodes

Usage:
  python3 contextdna/fleet/chief_synthesis.py
  python3 contextdna/fleet/chief_synthesis.py --force  # synthesize even with incomplete verdicts
"""

import json
import sys
import argparse
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.multifleet_packet_store import PacketStore


def synthesize_async(trigger_packet: dict):
    """Non-blocking synthesis trigger."""
    t = threading.Thread(target=synthesize, kwargs={"trigger": trigger_packet}, daemon=True)
    t.start()


def synthesize(trigger: dict = None, force: bool = False):
    store = PacketStore()
    nodes = store.active_nodes()
    if not nodes:
        print("[synthesis] No active nodes.")
        return None

    verdicts = {}
    for node in nodes:
        v = store.latest_verdict(node["nodeId"])
        if v:
            verdicts[node["nodeId"]] = v

    if not verdicts:
        print("[synthesis] No verdicts yet.")
        return None

    # Check if all active nodes have verdicts
    missing = [n["nodeId"] for n in nodes if n["nodeId"] not in verdicts]
    if missing and not force:
        print(f"[synthesis] Waiting for verdicts from: {missing}")
        return None

    print(f"[synthesis] Synthesizing {len(verdicts)} verdicts from: {list(verdicts.keys())}")

    # Find disagreements
    all_risks = []
    all_dissent = []
    confidences = []
    summaries = []

    for node_id, v in verdicts.items():
        all_risks.extend(v.get("risks", []))
        all_dissent.extend(v.get("dissent", []))
        confidences.append(v.get("confidence", 0.5))
        summaries.append(f"{node_id}: {v.get('summary', '')}")

    avg_confidence = sum(confidences) / len(confidences) if confidences else 0
    has_dissent = len(all_dissent) > 0
    low_confidence = avg_confidence < 0.6

    # Determine if we need 3-surgeon cross-examination
    need_surgeons = has_dissent or low_confidence
    cross_exam_result = None

    if need_surgeons:
        print(f"[synthesis] Cross-exam triggered (confidence={avg_confidence:.2f}, dissent={has_dissent})")
        cross_exam_result = _run_cross_exam(verdicts, all_risks, all_dissent)

    # Build chief decision
    winner_branch = _pick_winner(verdicts)
    decision_text = _make_decision(verdicts, all_risks, all_dissent, cross_exam_result)

    chief_decision = {
        "type": "chief_decision",
        "nodeId": "mac1",
        "fleetId": "contextdna-main",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "winnerBranch": winner_branch,
        "decision": decision_text,
        "avgConfidence": avg_confidence,
        "requiredFollowups": list(set(all_dissent))[:5],
        "memoryReasons": summaries,
        "crossExamResult": cross_exam_result,
    }

    # Store the decision
    store.ingest(chief_decision)
    print(f"[synthesis] Chief decision: {decision_text[:100]}")

    return chief_decision


def _pick_winner(verdicts: dict) -> str:
    """Pick the highest-confidence verdict's branch as winner."""
    best_node = max(verdicts.keys(), key=lambda n: verdicts[n].get("confidence", 0))
    return verdicts[best_node].get("workspace", "main")


def _make_decision(verdicts, risks, dissent, cross_exam=None) -> str:
    if cross_exam:
        return f"Cross-examined: {cross_exam.get('synthesis', {}).get('summary', 'See cross-exam results')}"
    if dissent:
        return f"Proceed with caution — {len(dissent)} dissent points require followup"
    return f"Consensus across {len(verdicts)} nodes — proceed"


def _run_cross_exam(verdicts: dict, risks: list, dissent: list) -> dict | None:
    """Run 3-surgeon cross-examination on the synthesized verdict data."""
    topic = f"Multi-fleet verdict synthesis: {len(verdicts)} nodes, risks={risks[:3]}, dissent={dissent[:3]}"
    try:
        result = subprocess.run(
            ["3s", "cross-exam", topic, "--mode", "single"],
            capture_output=True, text=True, timeout=120, cwd=REPO_ROOT
        )
        if result.returncode == 0:
            return {"raw": result.stdout[:2000], "synthesis": {"summary": result.stdout[:200]}}
        return {"error": result.stderr[:200]}
    except FileNotFoundError:
        # Try surgery-team.py fallback
        try:
            result = subprocess.run(
                [str(REPO_ROOT / ".venv/bin/python3"), "scripts/surgery-team.py", "cross-exam", topic],
                capture_output=True, text=True, timeout=120, cwd=REPO_ROOT
            )
            if result.returncode == 0:
                return {"raw": result.stdout[:2000], "synthesis": {"summary": result.stdout[:200]}}
        except Exception:
            pass
        return None
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Synthesize even with incomplete verdicts")
    args = parser.parse_args()
    result = synthesize(force=args.force)
    if result:
        print(json.dumps(result, indent=2))
