#!/usr/bin/env python3
"""
fleet_wake_agent.py — Periodic wake agent for idle Claude Code sessions.

Polls fleet inbox every POLL_INTERVAL seconds.
When messages exist OR idle threshold exceeded, injects exactly ONE prompt:

    "wake up and check if there are fleet messages"

That's the only prompt this agent ever sends. No other user prompts allowed.
Claude's existing SessionStart/Stop/UserPromptSubmit hooks handle the actual
message delivery — this agent is purely the wake signal.

Delivery methods (tried in order):
  1. claude -p (CLI, non-interactive) — starts a headless session with the prompt
  2. seed file (/tmp/fleet-seed-<node>.md) — picked up by UserPromptSubmit hook
     on the next human turn in an active VS Code session

Rate limiting: one wake signal per IDLE_THRESHOLD period max.

Usage:
  python3 scripts/fleet_wake_agent.py
  python3 scripts/fleet_wake_agent.py --install   # LaunchAgent
  python3 scripts/fleet_wake_agent.py --once      # single poll, exit
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.environ.get("FLEET_POLL_INTERVAL", "30"))        # seconds
IDLE_THRESHOLD = int(os.environ.get("FLEET_IDLE_THRESHOLD", "60")) * 60 # seconds (default 60min)
NODE = os.environ.get("MULTIFLEET_NODE_ID", "")
CHIEF_URL = os.environ.get("CHIEF_INGEST_URL", "http://chief.local:8844")
REPO = Path(os.environ.get("CONTEXT_DNA_REPO", Path.home() / "dev/er-simulator-superrepo"))
SEED_FILE = Path(f"/tmp/fleet-seed-{NODE or 'unknown'}.md")
LOG_FILE = Path(f"/tmp/fleet-wake-agent.log")

# The ONLY prompt this agent ever injects — no exceptions
def build_wake_prompt() -> str:
    # CHIEF_URL is already set correctly per node: loopback when we are the
    # chief, env/mDNS when we are a worker.
    inbox_cmd = f"curl -sf {CHIEF_URL}/inbox?node={NODE}"
    reply_cmd = f"CHIEF_INGEST_URL={CHIEF_URL} ~/dev/er-simulator-superrepo/scripts/fleet-send.sh mac3 reply"
    return (
        f"You have fleet messages. Run: {inbox_cmd} "
        f"Read and handle each message. Then confirm receipt: "
        f"{reply_cmd} \"{NODE} is online and processed fleet messages\""
    )

# ── Node detection ────────────────────────────────────────────────────────────
def _chief_id() -> Optional[str]:
    """Resolve chief node id via fleet_config; None if unavailable."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        for candidate in (repo_root / "multi-fleet", repo_root):
            p = str(candidate)
            if p not in sys.path:
                sys.path.insert(0, p)
        from multifleet.fleet_config import get_chief_id  # type: ignore
        return get_chief_id()
    except Exception:
        return None


def detect_node() -> str:
    """Env > config IP match > hostname. Mirrors fleet_config.get_node_id."""
    env = os.environ.get("MULTIFLEET_NODE_ID", "").strip()
    if env:
        return env
    try:
        repo_root = Path(__file__).resolve().parent.parent
        for candidate in (repo_root / "multi-fleet", repo_root):
            p = str(candidate)
            if p not in sys.path:
                sys.path.insert(0, p)
        from multifleet.fleet_config import get_node_id  # type: ignore
        return get_node_id()
    except Exception:
        return socket.gethostname().lower().split(".")[0]

def setup():
    global NODE, CHIEF_URL, SEED_FILE
    if not NODE:
        NODE = detect_node()
    # If we ARE the chief, always loopback (skip chief.local DNS round-trip).
    chief_id = _chief_id()
    if chief_id and NODE == chief_id and "chief.local" in CHIEF_URL:
        CHIEF_URL = "http://127.0.0.1:8844"
    SEED_FILE = Path(f"/tmp/fleet-seed-{NODE}.md")

# ── Inbox poll ────────────────────────────────────────────────────────────────
def poll_inbox() -> int:
    """Returns count of unread messages. -1 on error (chief unreachable)."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"{CHIEF_URL}/inbox?node={NODE}", timeout=5) as r:
            data = json.loads(r.read())
            return data.get("count", 0)
    except Exception:
        return -1

# ── Wake delivery ─────────────────────────────────────────────────────────────
def find_claude() -> Optional[str]:
    for path in ["/usr/local/bin/claude", str(Path.home() / ".local/bin/claude"),
                 "/opt/homebrew/bin/claude"]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("claude")

WAKE_LOCK = Path(f"/tmp/fleet-wake-running-{NODE or 'unknown'}.lock")

def wake_via_cli(claude_bin: str) -> bool:
    """Run claude -p with the wake prompt. Non-blocking. Lock prevents concurrent sessions."""
    # Check if a previous wake session is still running
    if WAKE_LOCK.exists():
        try:
            pid = int(WAKE_LOCK.read_text().strip())
            # Check if PID is still alive
            os.kill(pid, 0)
            log(f"  CLI wake skipped — previous session (pid {pid}) still running")
            return False
        except (OSError, ValueError):
            WAKE_LOCK.unlink(missing_ok=True)  # stale lock, clear it
    try:
        prompt = build_wake_prompt()
        proc = subprocess.Popen(
            [claude_bin, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=str(REPO),
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        WAKE_LOCK.write_text(str(proc.pid))
        return True
    except Exception as e:
        log(f"CLI wake failed: {e}")
        return False

def wake_via_seed() -> bool:
    """Write seed file — picked up by UserPromptSubmit hook on next human turn."""
    try:
        SEED_FILE.write_text(
            f"# Fleet Wake Signal — {NODE}\n\n{build_wake_prompt()}\n\n"
            f"_generated {datetime.now().strftime('%H:%M:%S')}_\n"
        )
        return True
    except Exception as e:
        log(f"Seed write failed: {e}")
        return False

def notify_os(message: str):
    """macOS notification as last resort — shows in Notification Center."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "Fleet — {NODE}" sound name "Glass"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass

def wake_via_keystroke() -> bool:
    """Write seed file then send Return keystroke to active VS Code terminal via osascript.
    This simulates a user pressing Enter in the active Claude Code session,
    which triggers the UserPromptSubmit hook and injects the seed file content."""
    try:
        # Write seed first
        SEED_FILE.write_text(
            f"# Fleet Wake Signal — {NODE}\n\n{build_wake_prompt()}\n\n"
            f"_generated {datetime.now().strftime('%H:%M:%S')}_\n"
        )
        # Focus VS Code — this alone surfaces the seed file on next user interaction
        # System Events keystroke requires Accessibility permission; skip if unavailable
        script = ('tell application "System Events" to if exists (process "Code") then '
                   'tell application "Visual Studio Code 3" to activate')
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        return True
    except Exception as e:
        log(f"Keystroke wake failed: {e}")
        return False


def send_wake(reason: str, has_messages: bool = False):
    """Send the wake signal into the EXISTING active VS Code session only.
    Never spawns new sessions — new sessions disrupt the user's workflow.
    Delivery: seed file (picked up by UserPromptSubmit hook) + VS Code focus.
    claude -p is only used when there are real fleet messages AND no VS Code open."""
    log(f"Sending wake signal ({reason})")
    ok = wake_via_keystroke()
    log(f"  Keystroke wake: {'ok' if ok else 'failed'}")
    if not ok:
        notify_os(f"{reason} — check fleet inbox")

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── Install ───────────────────────────────────────────────────────────────────
def install():
    plist_path = Path.home() / "Library/LaunchAgents/io.contextdna.fleet-wake.plist"
    script = Path(__file__).resolve()
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>io.contextdna.fleet-wake</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MULTIFLEET_NODE_ID</key><string>{NODE}</string>
        <key>CHIEF_INGEST_URL</key><string>{CHIEF_URL}</string>
        <key>CONTEXT_DNA_REPO</key><string>{REPO}</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{LOG_FILE}</string>
    <key>StandardErrorPath</key><string>{LOG_FILE}</string>
    <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>"""
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print(f"Installed: {plist_path}")
    print(f"Node={NODE} | Chief={CHIEF_URL} | Poll={POLL_INTERVAL}s | Idle={IDLE_THRESHOLD//60}m")

# ── Main loop ─────────────────────────────────────────────────────────────────
def run(once: bool = False):
    log(f"fleet_wake_agent starting — node={NODE} chief={CHIEF_URL} "
        f"poll={POLL_INTERVAL}s idle={IDLE_THRESHOLD//60}m "
        f"claude={find_claude() or 'NOT FOUND (seed fallback)'}")

    last_wake = 0.0
    last_activity = time.time()

    while True:
        count = poll_inbox()

        if count > 0:
            last_activity = time.time()
            now = time.time()
            if now - last_wake >= 300:  # rate-limit: max once per 5min for real messages
                send_wake(f"{count} fleet message(s)", has_messages=True)
                last_wake = now

        elif count == 0:
            idle_secs = time.time() - last_activity
            if idle_secs >= IDLE_THRESHOLD and time.time() - last_wake >= IDLE_THRESHOLD:
                # Idle wake: just a macOS notification, no session spawn
                log(f"Idle {int(idle_secs // 60)}m — notifying only")
                notify_os(f"idle {int(idle_secs // 60)}m — check fleet inbox")
                last_wake = time.time()

        # count == -1 means chief unreachable — skip silently

        if once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    setup()

    if args.install:
        install()
    else:
        run(once=args.once)
