#!/usr/bin/env python3
"""
Cursor Activity Watcher - Dynamic Context Injection for Cursor IDE

Monitors Cursor activity and automatically updates .cursorrules with fresh
Context DNA webhook payloads. This provides near-real-time context injection
without requiring Cursor's native hook system (which doesn't exist).

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │             CURSOR ACTIVITY WATCHER                         │
    ├─────────────────────────────────────────────────────────────┤
    │                                                             │
    │  ┌──────────────────────┐                                  │
    │  │  Activity Detection  │                                  │
    │  │  (every 60s)         │                                  │
    │  │  ├─ Cursor running?  │                                  │
    │  │  ├─ Files modified?  │                                  │
    │  │  └─ Session active?  │                                  │
    │  └──────────┬───────────┘                                  │
    │             │ IF ACTIVE                                     │
    │             ▼                                               │
    │  ┌──────────────────────┐                                  │
    │  │  Context Generation  │                                  │
    │  │  Helper Agent        │                                  │
    │  │  POST /inject/cursor │                                  │
    │  └──────────┬───────────┘                                  │
    │             │                                               │
    │             ▼                                               │
    │  ┌──────────────────────┐                                  │
    │  │  Rules File Update   │                                  │
    │  │  .cursorrules        │                                  │
    │  │  (Context DNA section)│                                 │
    │  └──────────────────────┘                                  │
    │                                                             │
    └─────────────────────────────────────────────────────────────┘

Usage:
    # Run as standalone daemon
    python memory/cursor_activity_watcher.py --daemon
    
    # Manual refresh
    python memory/cursor_activity_watcher.py --refresh
    
    # Check status
    python memory/cursor_activity_watcher.py --status
"""

import os
import re
import subprocess
import time
import logging
import requests
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Configuration
REPO_ROOT = Path(__file__).parent.parent
CURSORRULES_PATH = REPO_ROOT / ".cursorrules"
HELPER_AGENT_URL = "http://127.0.0.1:8080"
REFRESH_INTERVAL_SECONDS = 60  # Check every 60 seconds
ACTIVITY_LOG = REPO_ROOT / "memory" / ".cursor_activity_watcher.log"

# Context DNA markers in .cursorrules
CONTEXT_DNA_START = "# === CONTEXT DNA DYNAMIC INJECTION ==="
CONTEXT_DNA_END = "# === END CONTEXT DNA DYNAMIC INJECTION ==="


def is_cursor_running() -> bool:
    """Check if Cursor.app is currently running."""
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=2
        )
        return "Cursor.app" in result.stdout or "cursor" in result.stdout.lower()
    except Exception as e:
        logger.warning(f"Could not check Cursor process: {e}")
        return False


def get_last_file_activity() -> Optional[float]:
    """Get timestamp of last file modification in workspace."""
    try:
        # Check for recent modifications in key directories
        recent_time = None
        
        for pattern in ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.md"]:
            for file in REPO_ROOT.glob(pattern):
                # Skip hidden, cache, and build directories
                if any(p in file.parts for p in ['.git', 'node_modules', '.venv', '__pycache__']):
                    continue
                
                mtime = file.stat().st_mtime
                if recent_time is None or mtime > recent_time:
                    recent_time = mtime
        
        return recent_time
    except Exception as e:
        logger.warning(f"Could not check file activity: {e}")
        return None


def should_refresh() -> Tuple[bool, str]:
    """
    Determine if context should be refreshed.
    
    Returns:
        (should_refresh: bool, reason: str)
    """
    # Check if Cursor is running
    if not is_cursor_running():
        return False, "Cursor not running"
    
    # Check file activity
    last_activity = get_last_file_activity()
    if last_activity is None:
        return False, "Could not determine file activity"
    
    # If files modified in last 5 minutes, refresh
    age_seconds = time.time() - last_activity
    if age_seconds < 300:  # 5 minutes
        return True, f"Files modified {int(age_seconds)}s ago"
    
    return False, "No recent activity"


def fetch_cursor_context(prompt: str = "general workspace context") -> Optional[str]:
    """
    Fetch context injection from helper agent.
    
    Args:
        prompt: Prompt to generate context for (default: general)
    
    Returns:
        Formatted context string or None if failed
    """
    try:
        response = requests.post(
            f"{HELPER_AGENT_URL}/contextdna/inject/cursor",
            json={
                "prompt": prompt,
                "workspace": str(REPO_ROOT),
                "session_id": f"cursor-auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            },
            timeout=5
        )
        
        if response.ok:
            data = response.json()
            return data.get("payload", "")
        else:
            logger.warning(f"Helper agent returned {response.status_code}")
            return None
            
    except requests.exceptions.ConnectionError:
        logger.warning("Helper agent not reachable (port 8080)")
        return None
    except Exception as e:
        logger.warning(f"Failed to fetch context: {e}")
        return None


def update_cursorrules_context(context: str) -> bool:
    """
    Update the Context DNA section in .cursorrules.
    
    Args:
        context: The formatted context to inject
    
    Returns:
        True if updated successfully
    """
    if not CURSORRULES_PATH.exists():
        logger.error(f".cursorrules not found: {CURSORRULES_PATH}")
        return False
    
    try:
        # Read current content
        with open(CURSORRULES_PATH, 'r') as f:
            content = f.read()
        
        # Prepare new context section
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        new_section = f"""{CONTEXT_DNA_START}
# Auto-generated: {timestamp}
# Source: Helper Agent (port 8080) via cursor_activity_watcher.py
# Refresh: Every 60s when Cursor is active

{context}

{CONTEXT_DNA_END}"""
        
        # Check if section already exists
        if CONTEXT_DNA_START in content:
            # Replace existing section
            pattern = f"{re.escape(CONTEXT_DNA_START)}.*?{re.escape(CONTEXT_DNA_END)}"
            updated = re.sub(pattern, new_section, content, flags=re.DOTALL)
        else:
            # Append new section (after session recovery, before first ##)
            # Find position after session recovery section
            lines = content.split('\n')
            insert_pos = 0
            
            # Find first ## after the session recovery section
            in_recovery = False
            for i, line in enumerate(lines):
                if 'SESSION CRASH RECOVERY' in line:
                    in_recovery = True
                elif in_recovery and line.startswith('##') and 'SESSION' not in line:
                    insert_pos = i
                    break
            
            if insert_pos == 0:
                # Fallback: add after session recovery protocol (around line 50)
                insert_pos = min(60, len(lines))
            
            lines.insert(insert_pos, new_section)
            updated = '\n'.join(lines)
        
        # Write back
        with open(CURSORRULES_PATH, 'w') as f:
            f.write(updated)
        
        logger.info(f"Updated .cursorrules with fresh context ({len(context)} chars)")
        return True
        
    except Exception as e:
        logger.error(f"Failed to update .cursorrules: {e}")
        return False


def refresh_if_active() -> dict:
    """
    Refresh context if Cursor is active (called by scheduler).
    
    Returns:
        Dict with status information
    """
    should_refresh_flag, reason = should_refresh()
    
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cursor_running": is_cursor_running(),
        "should_refresh": should_refresh_flag,
        "reason": reason,
        "refreshed": False
    }
    
    if should_refresh_flag:
        # Fetch fresh context
        context = fetch_cursor_context()
        
        if context:
            # Update rules file
            if update_cursorrules_context(context):
                result["refreshed"] = True
                result["context_size"] = len(context)
                logger.info(f"Cursor context refreshed: {reason}")
            else:
                result["error"] = "Failed to update .cursorrules"
        else:
            result["error"] = "Failed to fetch context from helper agent"
    
    # Log activity
    try:
        with open(ACTIVITY_LOG, 'a') as f:
            f.write(f"{result['timestamp']} - {result}\n")
    except Exception:
        pass
    
    return result


def daemon_loop(interval: int = REFRESH_INTERVAL_SECONDS):
    """
    Run as daemon, refreshing context periodically.
    
    Args:
        interval: Seconds between refresh checks
    """
    logger.info(f"Starting Cursor activity watcher daemon (interval: {interval}s)")
    print(f"🧬 Cursor Activity Watcher Daemon Started")
    print(f"   Interval: {interval}s")
    print(f"   Log: {ACTIVITY_LOG}")
    print(f"   Helper Agent: {HELPER_AGENT_URL}")
    print("")
    
    while True:
        try:
            result = refresh_if_active()
            
            if result["refreshed"]:
                print(f"✅ [{datetime.now().strftime('%H:%M:%S')}] Context refreshed: {result['reason']}")
            elif result["should_refresh"]:
                print(f"⚠️ [{datetime.now().strftime('%H:%M:%S')}] Refresh failed: {result.get('error', 'Unknown')}")
            else:
                # Silent when no refresh needed
                pass
            
        except KeyboardInterrupt:
            print("\n\n🛑 Cursor activity watcher stopped")
            break
        except Exception as e:
            logger.error(f"Daemon loop error: {e}")
            print(f"❌ Error: {e}")
        
        time.sleep(interval)


def manual_refresh() -> bool:
    """Manually trigger a context refresh."""
    print("🔄 Manually refreshing Cursor context...")
    
    context = fetch_cursor_context()
    if not context:
        print("❌ Failed to fetch context from helper agent")
        print("   Is helper agent running? curl http://localhost:8080/health")
        return False
    
    if update_cursorrules_context(context):
        print(f"✅ Context updated ({len(context)} chars)")
        print(f"   File: {CURSORRULES_PATH}")
        return True
    else:
        print("❌ Failed to update .cursorrules")
        return False


def show_status():
    """Show current watcher status."""
    print("🧬 Cursor Activity Watcher Status")
    print("=" * 60)
    print(f"Cursor Running: {is_cursor_running()}")
    print(f"Rules File: {CURSORRULES_PATH}")
    print(f"Rules Exists: {CURSORRULES_PATH.exists()}")
    
    if CURSORRULES_PATH.exists():
        stat = CURSORRULES_PATH.stat()
        print(f"Rules Size: {stat.st_size} bytes")
        print(f"Last Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Check for Context DNA section
        with open(CURSORRULES_PATH, 'r') as f:
            content = f.read()
        has_section = CONTEXT_DNA_START in content
        print(f"Has Context DNA Section: {has_section}")
    
    print(f"\nHelper Agent: {HELPER_AGENT_URL}")
    try:
        response = requests.get(f"{HELPER_AGENT_URL}/health", timeout=2)
        print(f"Helper Status: {'✅ Online' if response.ok else '❌ Offline'}")
    except Exception:
        print(f"Helper Status: ❌ Offline")
    
    print(f"\nActivity Log: {ACTIVITY_LOG}")
    if ACTIVITY_LOG.exists():
        stat = ACTIVITY_LOG.stat()
        print(f"Log Size: {stat.st_size} bytes")
        print(f"Last Entry: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("Log: Not created yet")


if __name__ == "__main__":
    import sys
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
    )
    
    parser = argparse.ArgumentParser(description="Cursor Activity Watcher")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon")
    parser.add_argument("--refresh", action="store_true", help="Manual refresh")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--interval", type=int, default=REFRESH_INTERVAL_SECONDS,
                       help="Daemon interval in seconds")
    
    args = parser.parse_args()
    
    if args.daemon:
        daemon_loop(interval=args.interval)
    elif args.refresh:
        success = manual_refresh()
        sys.exit(0 if success else 1)
    elif args.status:
        show_status()
    else:
        # Default: check and refresh once
        result = refresh_if_active()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("refreshed") else 1)
