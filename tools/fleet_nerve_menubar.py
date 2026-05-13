#!/usr/bin/env python3
"""
Fleet Nerve Menu Bar — native macOS status bar indicator for fleet health.

Pattern 4 from ClaudeExchange: persistent menu bar icon showing fleet status.
- Green: all nodes online, no active tasks
- Yellow: tasks running on fleet
- Red: node(s) offline or errors

Click the icon for a dropdown with fleet summary.

Requires: pip install pyobjc-framework-Cocoa
Fallback: if PyObjC unavailable, prints status to terminal.

Usage:
  python3 tools/fleet_nerve_menubar.py
  python3 tools/fleet_nerve_menubar.py --install  # LaunchAgent
"""

import json
import logging
import os
import sys
import time
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.fleet_nerve_config import detect_node_id, load_peers

POLL_INTERVAL = 15  # seconds between status refreshes

# ── Status fetcher (runs in background thread) ──

class FleetStatus:
    def __init__(self):
        self.node_id = detect_node_id()
        self.peers, self.chief = load_peers()
        self.nodes = {}
        self.color = "gray"  # gray=unknown, green=ok, yellow=busy, red=offline
        self.summary = "Starting..."
        self.lock = threading.Lock()

    def refresh(self):
        nodes = {}
        online = 0
        total_tasks = 0

        for name, peer in self.peers.items():
            ip = peer["ip"] if name != self.node_id else "127.0.0.1"
            try:
                resp = urllib.request.urlopen(
                    f"http://{ip}:{peer.get('port', 8855)}/health", timeout=3)
                d = json.loads(resp.read())
                sessions = d.get("activeSessions", 0)
                nodes[name] = {
                    "status": "online", "sessions": sessions,
                    "branch": d.get("git", {}).get("branch", "?"),
                    "idle_s": d.get("idle_s", 0),
                }
                online += 1
                # Check tasks
                try:
                    tresp = urllib.request.urlopen(
                        f"http://{ip}:{peer.get('port', 8855)}/tasks/live", timeout=3)
                    td = json.loads(tresp.read())
                    active = td.get("activeTasks", 0)
                    nodes[name]["tasks"] = active
                    total_tasks += active
                except Exception as e:
                    logger.warning(f"Task probe failed for {name}: {e}")
                    nodes[name]["tasks"] = 0
            except Exception as e:
                logger.warning(f"Health probe failed for {name}: {e}")
                nodes[name] = {"status": "offline"}

        total = len(self.peers)
        if online == 0:
            color = "red"
            summary = "All nodes offline"
        elif online < total:
            color = "red"
            summary = f"{online}/{total} online"
        elif total_tasks > 0:
            color = "yellow"
            summary = f"{total_tasks} task(s) running"
        else:
            color = "green"
            summary = f"{online}/{total} online, idle"

        with self.lock:
            self.nodes = nodes
            self.color = color
            self.summary = summary

    def get_menu_text(self) -> list[str]:
        with self.lock:
            lines = [f"Fleet: {self.summary}"]
            for name, info in self.nodes.items():
                if info["status"] == "offline":
                    lines.append(f"  {name}: offline")
                else:
                    tasks = info.get("tasks", 0)
                    task_str = f" | {tasks} tasks" if tasks > 0 else ""
                    lines.append(f"  {name}: {info['sessions']}s | {info['branch']}{task_str}")
            return lines


# ── Try PyObjC menu bar (macOS native) ──

def run_menubar(status: FleetStatus):
    try:
        import objc
        from AppKit import (
            NSApplication, NSStatusBar, NSVariableStatusItemLength,
            NSMenu, NSMenuItem, NSImage, NSTimer, NSRunLoop, NSDefaultRunLoopMode,
        )
        from PyObjCTools import AppHelper

        app = NSApplication.sharedApplication()

        status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)

        ICONS = {
            "green": "🟢",
            "yellow": "🟡",
            "red": "🔴",
            "gray": "⚪",
        }

        def update_menu(_timer=None):
            status.refresh()
            with status.lock:
                status_item.setTitle_(f"{ICONS.get(status.color, '⚪')} Fleet")

            menu = NSMenu.alloc().init()
            for line in status.get_menu_text():
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    line, None, "")
                menu.addItem_(item)

            # Separator + quit
            menu.addItem_(NSMenuItem.separatorItem())
            quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit Fleet Monitor", "terminate:", "q")
            menu.addItem_(quit_item)

            status_item.setMenu_(menu)

        # Initial update
        update_menu()

        # Timer for periodic refresh
        timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL, app, objc.selector(update_menu, signature=b'v@:@'), None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)

        AppHelper.runEventLoop()

    except ImportError:
        print("PyObjC not available — install with: pip install pyobjc-framework-Cocoa")
        print("Falling back to terminal mode...")
        run_terminal(status)


def run_terminal(status: FleetStatus):
    """Fallback: print fleet status to terminal every POLL_INTERVAL seconds."""
    while True:
        status.refresh()
        os.system("clear")
        icon = {"green": "🟢", "yellow": "🟡", "red": "🔴", "gray": "⚪"}.get(status.color, "⚪")
        print(f"{icon} Fleet Nerve Monitor — {status.summary}")
        print()
        for line in status.get_menu_text():
            print(line)
        print(f"\nRefreshing every {POLL_INTERVAL}s... Ctrl+C to quit")
        time.sleep(POLL_INTERVAL)


def install():
    """Install as LaunchAgent."""
    node_id = detect_node_id()
    plist_path = Path.home() / "Library/LaunchAgents/io.contextdna.fleet-menubar.plist"
    python = sys.executable
    script = Path(__file__).resolve()

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.fleet-menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>{REPO_ROOT}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>MULTIFLEET_NODE_ID</key>
        <string>{node_id}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>"""

    plist_path.write_text(plist)
    os.system(f"launchctl unload '{plist_path}' 2>/dev/null")
    os.system(f"launchctl load '{plist_path}'")
    print(f"Fleet menu bar installed: {plist_path}")


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    else:
        status = FleetStatus()
        run_menubar(status)
