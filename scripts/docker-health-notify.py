#!/usr/bin/env python3
"""One-shot Docker health notifier — called by xbar watchdog on state changes.

Posts to Discord webhook + commits to git status log.
No daemon, no process to keep alive. xbar calls, this runs, exits.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

WEBHOOK_FILE = os.path.expanduser("~/.config/fleet/discord-ops-webhook")
GIT_STATUS_LOG = ".fleet-status/chief-docker.log"


def post_discord(state: str, runtime: str, version: str, healed: str, prev: str) -> bool:
    """Post state change to Discord ops channel via webhook."""
    webhook_url = ""
    if os.path.isfile(WEBHOOK_FILE):
        with open(WEBHOOK_FILE) as f:
            webhook_url = f.read().strip()

    if not webhook_url:
        print("no discord webhook configured, skipping", file=sys.stderr)
        return False

    color_map = {"running": 0x2ECC71, "starting": 0xF39C12, "stopped": 0xE74C3C, "dead": 0xE74C3C}
    color = color_map.get(state, 0x95A5A6)

    emoji = {"running": "🟢", "starting": "🟡", "stopped": "🔴", "dead": "💀"}.get(state, "❓")
    heal_text = "yes — restart attempted" if healed == "true" else "no"

    embed = {
        "title": f"{emoji} Docker {state.upper()} on chief",
        "color": color,
        "fields": [
            {"name": "Runtime", "value": runtime, "inline": True},
            {"name": "Version", "value": version, "inline": True},
            {"name": "Previous", "value": prev, "inline": True},
            {"name": "Self-heal", "value": heal_text, "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "fleet-docker-watchdog"},
    }

    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "fleet-docker-watchdog/1.0"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"discord post failed: {e}", file=sys.stderr)
        return False


def commit_git(state: str, runtime: str, version: str, healed: str, prev: str) -> bool:
    """Append to git status log and push (P7 channel)."""
    repo = None
    for d in [
        os.path.expanduser("~/dev/er-simulator-superrepo"),
        os.path.expanduser("~/Documents/er-simulator-superrepo"),
    ]:
        if os.path.isdir(d):
            repo = d
            break
    if not repo:
        return False

    log_path = os.path.join(repo, GIT_STATUS_LOG)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"{ts} state={state} prev={prev} runtime={runtime} version={version} healed={healed}\n"

    with open(log_path, "a") as f:
        f.write(entry)

    try:
        subprocess.run(["git", "add", GIT_STATUS_LOG], cwd=repo, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", f"fleet-docker: {prev} -> {state}"],
            cwd=repo,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(["git", "push"], cwd=repo, capture_output=True, timeout=30)
        return True
    except Exception as e:
        print(f"git commit/push failed: {e}", file=sys.stderr)
        return False


RATE_LIMIT_FILE = "/tmp/fleet-docker-notify-last"
RATE_LIMIT_S = 60  # Max 1 notification per 60s


def _rate_limited() -> bool:
    """Prevent notification spam — max 1 per RATE_LIMIT_S seconds."""
    try:
        if os.path.isfile(RATE_LIMIT_FILE):
            last = float(open(RATE_LIMIT_FILE).read().strip())
            if (datetime.now(timezone.utc).timestamp() - last) < RATE_LIMIT_S:
                return True
    except (ValueError, OSError):
        pass
    try:
        with open(RATE_LIMIT_FILE, "w") as f:
            f.write(str(datetime.now(timezone.utc).timestamp()))
    except OSError:
        pass
    return False


def main():
    parser = argparse.ArgumentParser(description="Docker health state change notifier")
    parser.add_argument("--state", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--version", default="unknown")
    parser.add_argument("--healed", default="false")
    parser.add_argument("--prev", default="unknown")
    args = parser.parse_args()

    if _rate_limited():
        print("rate limited, skipping notification", file=sys.stderr)
        sys.exit(0)

    discord_ok = post_discord(args.state, args.runtime, args.version, args.healed, args.prev)
    git_ok = commit_git(args.state, args.runtime, args.version, args.healed, args.prev)

    if discord_ok or git_ok:
        print(f"notified: discord={discord_ok} git={git_ok}")
    else:
        print("no notifications sent (webhook not configured, git failed)", file=sys.stderr)


if __name__ == "__main__":
    main()
