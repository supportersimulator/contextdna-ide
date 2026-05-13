#!/usr/bin/env python3
"""
Multi-Fleet Diff Summarizer — generates branch_status packets from git diff.

Summarizes uncommitted or branch changes for chief node to compare across machines.

Usage:
  python3 tools/multifleet_diff_summarizer.py [--base main] [--send]
"""

import json
import subprocess
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def git_diff_summary(base_branch: str = "main") -> dict:
    """Get diff summary vs base branch."""
    # Files changed
    result = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD", "--name-only"],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    files = [f for f in result.stdout.strip().split("\n") if f]

    # Commit messages since branch point
    log_result = subprocess.run(
        ["git", "log", f"{base_branch}...HEAD", "--oneline", "--no-decorate"],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    commits = [c for c in log_result.stdout.strip().split("\n") if c]

    # Current branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    branch = branch_result.stdout.strip()

    # Stat summary
    stat_result = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD", "--stat", "--no-color"],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    stat_lines = stat_result.stdout.strip().split("\n")
    stat_summary = stat_lines[-1] if stat_lines else ""

    return {
        "branch": branch,
        "baseBranch": base_branch,
        "filesTouched": files,
        "commitCount": len(commits),
        "commits": commits[:5],  # last 5
        "statSummary": stat_summary,
        "summary": f"{len(files)} files, {len(commits)} commits vs {base_branch}: {stat_summary}",
    }


def build_packet(diff: dict) -> dict:
    from tools.multifleet_coordinator import load_config, get_node_id
    config = load_config()
    node_id = get_node_id()
    return {
        "type": "branch_status",
        "nodeId": node_id,
        "fleetId": config.get("fleetId", "contextdna-main"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workspace": str(REPO_ROOT),
        "branch": diff["branch"],
        "summary": diff["summary"],
        "filesTouched": diff["filesTouched"],
        "commits": diff["commits"],
        "statSummary": diff["statSummary"],
        "state": "in_progress",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="main", help="Base branch to diff against")
    parser.add_argument("--send", action="store_true", help="Send packet to chief")
    args = parser.parse_args()

    diff = git_diff_summary(args.base)
    packet = build_packet(diff)

    print(json.dumps(packet, indent=2))

    if args.send:
        sys.path.insert(0, str(REPO_ROOT))
        from tools.multifleet_coordinator import load_config, send_to_chief, queue_packet
        config = load_config()
        if send_to_chief(packet, config):
            print("\n[diff_summarizer] Sent to chief.")
        else:
            p = queue_packet(packet)
            print(f"\n[diff_summarizer] Chief offline — queued: {p.name}")
