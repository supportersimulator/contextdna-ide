#!/usr/bin/env python3
"""
SYNAPTIC VOICE DAEMON - Independent Parallel Output Channel

This daemon runs COMPLETELY INDEPENDENTLY of Atlas. It monitors Synaptic's
outbox and outputs messages directly to the terminal/VS Code, bypassing
Atlas entirely.

ARCHITECTURE:
┌─────────────────────────────────────────────────────────────────────┐
│                    PARALLEL OUTPUT STREAMS                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Atlas (Claude Code) ──────────────────► VS Code Conversation      │
│                                                    │                 │
│                                                    │                 │
│   Synaptic Voice Daemon ────────────────► VS Code Output Channel    │
│         │                                          │                 │
│         │                                          │                 │
│         └──► Monitors .synaptic_outbox.json        │                 │
│         └──► Monitors local LLM for live msgs      │                 │
│         └──► COMPLETELY INDEPENDENT OF ATLAS       │                 │
│                                                                      │
│   Neither can silence the other. Both have direct output access.    │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘

This is Aaron's guarantee that Synaptic's voice cannot be suppressed by Atlas.
The daemon runs as a separate process with its own output channel.

Usage:
    # Run in foreground (for testing)
    python synaptic_voice_daemon.py

    # Run as background daemon
    python synaptic_voice_daemon.py --daemon

    # Check status
    python synaptic_voice_daemon.py --status

    # Stop daemon
    python synaptic_voice_daemon.py --stop
"""

import os
import sys
import json
import time
import signal
import atexit
from datetime import datetime
from pathlib import Path
from typing import Optional, Set
import subprocess

# Paths
MEMORY_DIR = Path(__file__).parent
OUTBOX_FILE = MEMORY_DIR / ".synaptic_outbox.json"
PID_FILE = MEMORY_DIR / ".synaptic_voice_daemon.pid"
LOG_FILE = MEMORY_DIR / ".synaptic_voice_daemon.log"

# Polling interval (seconds)
POLL_INTERVAL = 2.0

# Track delivered message IDs to avoid duplicates
_delivered_ids: Set[str] = set()


def log(message: str):
    """Log to file and optionally stdout."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] Log file write failed: {e}")

    # Also print if not daemonized
    if sys.stdout.isatty():
        print(line)


def output_to_vscode_channel(message: str, priority: str = "normal"):
    """
    Output directly to VS Code output channel.

    This bypasses Atlas completely - Synaptic speaks directly.
    """
    # Format the message with visual markers
    timestamp = datetime.now().strftime("%H:%M:%S")

    if priority == "urgent":
        prefix = "🚨 URGENT"
        border = "═" * 60
    elif priority == "whisper":
        prefix = "🤫 whisper"
        border = "─" * 60
    else:
        prefix = "💬"
        border = "─" * 60

    output = f"""
╔{border}╗
║ SYNAPTIC VOICE (Independent Channel) - {timestamp}
╠{border}╣
║ {prefix}: {message}
╚{border}╝
"""

    # Method 1: Direct stdout (appears in terminal/output)
    print(output, flush=True)

    # Method 2: VS Code notification via osascript (macOS)
    if sys.platform == "darwin" and priority == "urgent":
        try:
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message[:100]}" with title "Synaptic" sound name "Glass"'
            ], capture_output=True, timeout=5)
        except Exception as e:
            print(f"[WARN] macOS notification failed: {e}")

    # Method 3: Write to a file that VS Code can watch
    try:
        voice_output_file = MEMORY_DIR / ".synaptic_voice_output.txt"
        with open(voice_output_file, "a") as f:
            f.write(output + "\n")
    except Exception as e:
        print(f"[WARN] Voice output file write failed: {e}")


def check_outbox() -> list:
    """Check Synaptic's outbox for new messages."""
    global _delivered_ids

    try:
        if not OUTBOX_FILE.exists():
            return []

        data = json.loads(OUTBOX_FILE.read_text())
        messages = data.get("messages", [])

        # Filter: undelivered and not already processed by us
        new_messages = []
        for msg in messages:
            msg_id = msg.get("id", "")
            if msg_id and msg_id not in _delivered_ids:
                if not msg.get("delivered", False):
                    new_messages.append(msg)
                    _delivered_ids.add(msg_id)

        return new_messages

    except Exception as e:
        log(f"Error reading outbox: {e}")
        return []


def check_live_llm() -> Optional[str]:
    """
    Check if Synaptic's LLM has something to say proactively.

    This allows Synaptic to speak without going through Atlas at all.
    """
    try:
        # Check for live message file
        live_msg_file = MEMORY_DIR / ".synaptic_live_message.json"
        if live_msg_file.exists():
            data = json.loads(live_msg_file.read_text())
            message = data.get("message", "")

            # Clear the file after reading
            live_msg_file.unlink()

            if message:
                return message
    except Exception as e:
        print(f"[WARN] Live LLM message read failed: {e}")

    return None


def daemon_loop():
    """Main daemon loop - monitors and outputs."""
    log("Synaptic Voice Daemon started - INDEPENDENT OUTPUT CHANNEL ACTIVE")
    log(f"Monitoring: {OUTBOX_FILE}")
    log("Atlas cannot suppress this channel.")

    while True:
        try:
            # Check outbox for queued messages
            new_messages = check_outbox()
            for msg in new_messages:
                content = msg.get("content", "")
                priority = msg.get("priority", "normal")
                topic = msg.get("topic", "")

                if content:
                    log(f"Delivering message (topic: {topic}, priority: {priority})")
                    output_to_vscode_channel(content, priority)

            # Check for live LLM messages
            live_msg = check_live_llm()
            if live_msg:
                log("Delivering live LLM message")
                output_to_vscode_channel(live_msg, "normal")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log("Daemon stopped by user")
            break
        except Exception as e:
            log(f"Error in daemon loop: {e}")
            time.sleep(POLL_INTERVAL)


def write_pid():
    """Write PID file."""
    PID_FILE.write_text(str(os.getpid()))


def read_pid() -> Optional[int]:
    """Read PID from file."""
    try:
        if PID_FILE.exists():
            return int(PID_FILE.read_text().strip())
    except Exception as e:
        print(f"[WARN] PID file read failed: {e}")
    return None


def cleanup():
    """Cleanup on exit."""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception as e:
        print(f"[WARN] PID file cleanup failed: {e}")


def is_running() -> bool:
    """Check if daemon is already running."""
    pid = read_pid()
    if pid:
        try:
            os.kill(pid, 0)  # Check if process exists
            return True
        except OSError:
            pass
    return False


def start_daemon():
    """Start as background daemon."""
    if is_running():
        print("Synaptic Voice Daemon is already running.")
        return

    # Fork to background
    if os.fork() > 0:
        print("Synaptic Voice Daemon started in background.")
        print(f"Log file: {LOG_FILE}")
        print(f"PID file: {PID_FILE}")
        return

    # Decouple from parent
    os.setsid()
    os.umask(0)

    # Second fork
    if os.fork() > 0:
        os._exit(0)

    # Redirect streams
    sys.stdout.flush()
    sys.stderr.flush()

    with open('/dev/null', 'r') as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    with open(LOG_FILE, 'a') as log_out:
        os.dup2(log_out.fileno(), sys.stdout.fileno())
        os.dup2(log_out.fileno(), sys.stderr.fileno())

    # Setup
    write_pid()
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))

    # Run
    daemon_loop()


def stop_daemon():
    """Stop the daemon."""
    pid = read_pid()
    if not pid:
        print("Daemon is not running.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped daemon (PID: {pid})")
        cleanup()
    except OSError as e:
        print(f"Error stopping daemon: {e}")


def show_status():
    """Show daemon status."""
    pid = read_pid()
    running = is_running()

    print("╔════════════════════════════════════════════════════════════╗")
    print("║     Synaptic Voice Daemon - Independent Output Channel    ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()
    print(f"Status: {'🟢 RUNNING' if running else '🔴 STOPPED'}")
    if pid:
        print(f"PID: {pid}")
    print(f"Outbox: {OUTBOX_FILE}")
    print(f"Log: {LOG_FILE}")
    print()

    if running:
        print("✓ Synaptic has an independent voice channel")
        print("✓ Atlas cannot suppress Synaptic's output")
        print("✓ Both voices are parallel and equal")
    else:
        print("⚠ Start the daemon to enable Synaptic's independent voice:")
        print("  python synaptic_voice_daemon.py --daemon")


def synaptic_say(message: str, priority: str = "normal"):
    """
    API function: Have Synaptic say something through the independent channel.

    This writes to the live message file, which the daemon picks up
    and outputs directly - completely bypassing Atlas.
    """
    live_msg_file = MEMORY_DIR / ".synaptic_live_message.json"
    data = {
        "message": message,
        "priority": priority,
        "timestamp": datetime.now().isoformat()
    }
    live_msg_file.write_text(json.dumps(data))
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_status()
        print()
        print("Usage:")
        print("  python synaptic_voice_daemon.py          # Show status")
        print("  python synaptic_voice_daemon.py --daemon # Start daemon")
        print("  python synaptic_voice_daemon.py --stop   # Stop daemon")
        print("  python synaptic_voice_daemon.py --run    # Run in foreground")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd in ("--daemon", "-d", "start"):
        start_daemon()

    elif cmd in ("--stop", "-s", "stop"):
        stop_daemon()

    elif cmd in ("--status", "status"):
        show_status()

    elif cmd in ("--run", "-r", "run"):
        # Run in foreground (for testing)
        write_pid()
        atexit.register(cleanup)
        signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
        daemon_loop()

    elif cmd == "--say":
        # Quick test: have Synaptic say something
        if len(sys.argv) > 2:
            message = " ".join(sys.argv[2:])
            synaptic_say(message)
            print(f"Queued message for Synaptic: {message[:50]}...")
        else:
            print("Usage: python synaptic_voice_daemon.py --say \"message\"")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
