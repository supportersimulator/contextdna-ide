#!/usr/bin/env python3
"""
Multi-Fleet Merge Adjudicator — decides which node's branch wins when conflict exists.

Uses ContextDNA memory to inform merge decisions:
- Which patterns have succeeded before
- Which combinations caused regressions
- Surgeon consensus scores

Usage:
  python3 contextdna/fleet/merge_adjudicator.py --branches mac1:feature/x mac2:feature/y
"""

import json
import sys
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


def adjudicate(branches: list[dict]) -> dict:
    """
    branches: [{"nodeId": "mac1", "branch": "feature/x", "confidence": 0.8, "risks": [...]}, ...]
    Returns: {"winner": {...}, "loser": {...}, "reason": "...", "required_followups": [...]}
    """
    if not branches:
        return {"error": "No branches to adjudicate"}

    if len(branches) == 1:
        return {
            "winner": branches[0],
            "reason": "Only one branch submitted",
            "required_followups": [],
        }

    # Score each branch: confidence weighted by risk count
    def score(b):
        confidence = b.get("confidence", 0.5)
        risk_penalty = len(b.get("risks", [])) * 0.05
        dissent_penalty = len(b.get("dissent", [])) * 0.1
        return confidence - risk_penalty - dissent_penalty

    scored = sorted(branches, key=score, reverse=True)
    winner = scored[0]
    losers = scored[1:]

    # Collect followups from losers that aren't in winner
    winner_risks = set(winner.get("risks", []))
    followups = []
    for loser in losers:
        for dissent in loser.get("dissent", []):
            if dissent not in winner_risks:
                followups.append(dissent)

    # Query ContextDNA memory for similar past decisions
    memory_context = _query_memory(winner.get("branch", ""), loser_branches=[l.get("branch", "") for l in losers])

    return {
        "winner": winner,
        "losers": losers,
        "winnerScore": score(winner),
        "reason": f"Highest adjusted confidence ({score(winner):.2f}) with {len(winner.get('risks', []))} risks",
        "required_followups": list(set(followups))[:5],
        "memoryContext": memory_context,
    }


def _query_memory(winner_branch: str, loser_branches: list) -> str | None:
    """Query ContextDNA memory for relevant past merge decisions."""
    try:
        from memory.query import query_memory
        result = query_memory(f"merge adjudication {winner_branch} {' '.join(loser_branches)}")
        return result.get("summary", "") if result else None
    except ImportError:
        pass
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--branches", nargs="+", help="node:branch pairs e.g. mac1:feature/x mac2:feature/y")
    args = parser.parse_args()

    if not args.branches:
        print("Usage: merge_adjudicator.py --branches mac1:feature/x mac2:feature/y")
        sys.exit(1)

    branches = []
    for b in args.branches:
        parts = b.split(":", 1)
        branches.append({"nodeId": parts[0], "branch": parts[1] if len(parts) > 1 else "main"})

    result = adjudicate(branches)
    print(json.dumps(result, indent=2))
