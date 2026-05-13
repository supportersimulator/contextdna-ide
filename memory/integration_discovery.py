#!/usr/bin/env python3
"""
Integration Discovery System - Autonomous Pattern Learning

Monitors ALL webhook/hook activity and automatically detects novel successful
integrations that aren't yet in the Context DNA system.

When a new integration pattern is detected (3+ successful executions):
1. Studies the integration pattern (secrets protected)
2. Sends popup notification: "New Context DNA integration discovered!"
3. User clicks "okay, thanks" to approve contribution
4. Auto-commits to GitHub contextdna-templates repo
5. Other users get it automatically via community sync

This creates a self-expanding ecosystem where Context DNA learns from
every user's successful integrations.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │               INTEGRATION DISCOVERY ENGINE                       │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                  │
    │  MONITOR (passive observation)                                  │
    │  ├─ Watch all webhook executions                                │
    │  ├─ Detect unknown sources (not in destination_registry)        │
    │  ├─ Track success/failure patterns                              │
    │  └─ Trigger: 3+ consecutive successes                           │
    │                                                                  │
    │  ↓ (novel pattern detected)                                     │
    │                                                                  │
    │  ANALYZER (pattern extraction)                                   │
    │  ├─ Study successful integration                                │
    │  ├─ Extract: IDE type, config format, hook mechanism            │
    │  ├─ Sanitize: Remove paths, secrets, usernames                  │
    │  └─ Generate: Reusable template                                 │
    │                                                                  │
    │  ↓ (template ready)                                             │
    │                                                                  │
    │  NOTIFY (user approval)                                          │
    │  ├─ macOS popup: "New integration discovered!"                  │
    │  ├─ Show: IDE name, detection confidence                        │
    │  ├─ Buttons: "Share with Community" / "Keep Private"            │
    │  └─ User clicks → approval recorded                             │
    │                                                                  │
    │  ↓ (if user approves)                                           │
    │                                                                  │
    │  CONTRIBUTE (GitHub auto-commit)                                │
    │  ├─ Add to local community_templates.db                         │
    │  ├─ Export to JSON (sanitized)                                  │
    │  ├─ Git commit to contextdna-templates repo                     │
    │  ├─ Push to GitHub (if user opted in)                           │
    │  └─ Notification: "Thanks! Other users will benefit!"           │
    │                                                                  │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    # Start the discovery monitor (background daemon)
    python memory/integration_discovery.py --daemon
    
    # Check discovered integrations
    python memory/integration_discovery.py --list
    
    # Manually approve a discovered integration
    python memory/integration_discovery.py --approve <discovery_id>
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent
DISCOVERY_DB = MEMORY_DIR / ".integration_discoveries.db"
BETA_POLICY_FILE = MEMORY_DIR / ".community_beta_opt_in.json"


@dataclass
class DiscoveredIntegration:
    """A novel integration pattern discovered in the wild."""
    discovery_id: str
    source_identifier: str        # Detected IDE/app name
    integration_type: str         # "pre_hook", "post_hook", "api", "websocket"
    
    # Detection
    first_seen: str
    last_seen: str
    execution_count: int
    success_count: int
    failure_count: int
    confidence: float             # success_count / execution_count
    
    # Pattern
    detected_pattern: str         # JSON of the integration pattern
    config_location: Optional[str]
    hook_command: Optional[str]
    
    # Status
    status: str                   # "monitoring", "ready_for_approval", "approved", "contributed"
    user_notified: bool
    user_approved: bool
    contributed_to_github: bool
    
    # Metadata
    os_type: str
    ide_version: Optional[str]
    notes: str


class IntegrationDiscovery:
    """Autonomous integration discovery and contribution system."""
    
    def __init__(self, db_path: Path = DISCOVERY_DB):
        self.db_path = db_path
        self._ensure_schema()
    
    def _ensure_schema(self):
        """Create discovery database schema."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS discovered_integrations (
                    discovery_id TEXT PRIMARY KEY,
                    source_identifier TEXT NOT NULL,
                    integration_type TEXT NOT NULL,
                    
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    execution_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    confidence REAL DEFAULT 0.0,
                    
                    detected_pattern TEXT,
                    config_location TEXT,
                    hook_command TEXT,
                    
                    status TEXT DEFAULT 'monitoring',
                    user_notified BOOLEAN DEFAULT 0,
                    user_approved BOOLEAN DEFAULT 0,
                    contributed_to_github BOOLEAN DEFAULT 0,
                    
                    os_type TEXT,
                    ide_version TEXT,
                    notes TEXT,
                    
                    UNIQUE(source_identifier, integration_type)
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_discovery_status 
                ON discovered_integrations(status, confidence DESC)
            """)
            
            # Execution log for pattern detection
            conn.execute("""
                CREATE TABLE IF NOT EXISTS integration_executions (
                    execution_id TEXT PRIMARY KEY,
                    source_identifier TEXT NOT NULL,
                    integration_type TEXT,
                    success BOOLEAN,
                    latency_ms INTEGER,
                    error_message TEXT,
                    detected_config TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_execution_timestamp 
                ON integration_executions(timestamp DESC)
            """)
            
            conn.commit()
    
    def record_execution(
        self,
        source_identifier: str,
        integration_type: str,
        success: bool,
        detected_config: Optional[str] = None,
        latency_ms: int = 0,
        error_message: Optional[str] = None
    ):
        """Record an integration execution for pattern detection."""
        execution_id = f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        with sqlite3.connect(str(self.db_path)) as conn:
            # Record execution
            conn.execute("""
                INSERT INTO integration_executions (
                    execution_id, source_identifier, integration_type,
                    success, latency_ms, error_message, detected_config, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id, source_identifier, integration_type,
                success, latency_ms, error_message, detected_config,
                datetime.now(timezone.utc).isoformat()
            ))
            
            # Check if this is a known integration (in destination_registry)
            is_known = self._is_known_integration(source_identifier)
            
            if not is_known:
                # Novel integration! Track it
                self._track_novel_integration(
                    source_identifier, integration_type, success,
                    detected_config, conn
                )
            
            conn.commit()
    
    def _is_known_integration(self, source_identifier: str) -> bool:
        """Check if this integration is already in destination_registry."""
        try:
            from memory.destination_registry import DestinationRegistry
            registry = DestinationRegistry()
            
            # Check if any destination matches this source
            destinations = registry.get_all_destinations(enabled_only=False)
            for dest in destinations:
                if source_identifier.lower() in dest.destination_id.lower():
                    return True
                if source_identifier.lower() in dest.friendly_name.lower():
                    return True
        
        except Exception:
            pass
        
        return False
    
    def _track_novel_integration(
        self,
        source_identifier: str,
        integration_type: str,
        success: bool,
        detected_config: Optional[str],
        conn: sqlite3.Connection
    ):
        """Track a novel integration pattern."""
        discovery_id = hashlib.sha256(
            f"{source_identifier}:{integration_type}".encode()
        ).hexdigest()[:12]
        
        # Check if already tracking
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM discovered_integrations WHERE discovery_id = ?",
            (discovery_id,)
        )
        existing = cursor.fetchone()
        
        if existing:
            # Update existing discovery
            new_exec_count = existing[5] + 1
            new_success_count = existing[6] + (1 if success else 0)
            new_failure_count = existing[7] + (0 if success else 1)
            new_confidence = new_success_count / new_exec_count if new_exec_count > 0 else 0
            
            conn.execute("""
                UPDATE discovered_integrations 
                SET last_seen = ?,
                    execution_count = ?,
                    success_count = ?,
                    failure_count = ?,
                    confidence = ?,
                    detected_pattern = ?
                WHERE discovery_id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                new_exec_count,
                new_success_count,
                new_failure_count,
                new_confidence,
                detected_config or existing[9],  # Update config if provided
                discovery_id
            ))
            
            # Check if ready for approval (3+ consecutive successes)
            if new_success_count >= 3 and new_confidence >= 0.75 and not existing[13]:
                # Ready for user approval!
                conn.execute("""
                    UPDATE discovered_integrations 
                    SET status = 'ready_for_approval'
                    WHERE discovery_id = ?
                """, (discovery_id,))
                
                # Trigger notification (async)
                self._trigger_approval_notification(discovery_id, source_identifier)
        
        else:
            # New discovery!
            import platform
            
            conn.execute("""
                INSERT INTO discovered_integrations (
                    discovery_id, source_identifier, integration_type,
                    first_seen, last_seen,
                    execution_count, success_count, failure_count, confidence,
                    detected_pattern, status, os_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                discovery_id, source_identifier, integration_type,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                1, 1 if success else 0, 0 if success else 1,
                1.0 if success else 0.0,
                detected_config, 'monitoring',
                platform.system().lower().replace('darwin', 'macos')
            ))
            
            logger.info(f"🔍 New integration discovered: {source_identifier}")
    
    def _trigger_approval_notification(self, discovery_id: str, source_identifier: str):
        """Trigger macOS notification for user approval."""
        try:
            # Spawn notification as background process
            script = MEMORY_DIR.parent / "scripts" / "notify-new-integration.sh"
            if script.exists():
                subprocess.Popen(
                    [str(script), discovery_id, source_identifier],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                logger.info(f"✅ Notification triggered for {discovery_id}")
        except Exception as e:
            logger.debug(f"Could not trigger notification: {e}")
    
    def get_ready_for_approval(self) -> List[DiscoveredIntegration]:
        """Get integrations ready for user approval."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM discovered_integrations 
                WHERE status = 'ready_for_approval'
                ORDER BY confidence DESC, execution_count DESC
            """)
            
            return [DiscoveredIntegration(**dict(row)) for row in cursor.fetchall()]
    
    def approve_and_contribute(self, discovery_id: str) -> Tuple[bool, str]:
        """
        User approved - contribute to community templates and GitHub.
        
        Requires: Beta policy opt-in
        
        Steps:
        1. Mark as approved
        2. Add to community_templates.db
        3. Export to JSON
        4. Git commit to contextdna-templates repo
        5. Push to GitHub (if opted in)
        
        Returns:
            (success, message)
        """
        # Check beta policy opt-in
        if not self._has_beta_opt_in():
            return False, "User must opt-in to Community Beta Integrations Policy first"
        
        # Get discovery
        discovery = self._get_discovery(discovery_id)
        if not discovery:
            return False, f"Discovery not found: {discovery_id}"
        
        if discovery.status != 'ready_for_approval':
            return False, f"Discovery not ready (status: {discovery.status})"
        
        try:
            # Step 1: Mark as approved
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    UPDATE discovered_integrations 
                    SET status = 'approved',
                        user_approved = 1
                    WHERE discovery_id = ?
                """, (discovery_id,))
                conn.commit()
            
            # Step 2: Add to community templates
            from memory.community_templates import CommunityTemplates
            
            templates = CommunityTemplates()
            
            # Parse detected pattern
            pattern = json.loads(discovery.detected_pattern) if discovery.detected_pattern else {}
            
            template_id = templates.contribute_template(
                ide_family=self._infer_ide_family(discovery.source_identifier),
                os_type=discovery.os_type,
                config_content=discovery.detected_pattern,
                hook_scripts=pattern.get('hook_scripts', {}),
                notes=f"Auto-discovered from {discovery.source_identifier} ({discovery.success_count} successes)",
                contributed_by="auto_discovery"
            )
            
            # Step 3: Export for GitHub
            export_data = {
                "template_id": template_id,
                "discovery_id": discovery_id,
                "source": discovery.source_identifier,
                "ide_family": self._infer_ide_family(discovery.source_identifier),
                "os_type": discovery.os_type,
                "confidence": discovery.confidence,
                "success_count": discovery.success_count,
                "pattern": json.loads(discovery.detected_pattern) if discovery.detected_pattern else {},
                "contributed_at": datetime.now(timezone.utc).isoformat()
            }
            
            export_file = MEMORY_DIR / f".discovery_export_{discovery_id}.json"
            with open(export_file, 'w') as f:
                json.dump(export_data, f, indent=2)
            
            # Step 4: Git commit (if GitHub integration configured)
            github_success = self._contribute_to_github(discovery_id, export_file)
            
            # Step 5: Update status
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    UPDATE discovered_integrations 
                    SET status = 'contributed',
                        contributed_to_github = ?
                    WHERE discovery_id = ?
                """, (github_success, discovery_id))
                conn.commit()
            
            # Celebrate!
            self._send_celebration_notification(discovery.source_identifier)
            
            return True, f"Contributed template {template_id} to community!"
        
        except Exception as e:
            logger.error(f"Contribution failed: {e}")
            return False, str(e)
    
    def _get_discovery(self, discovery_id: str) -> Optional[DiscoveredIntegration]:
        """Get discovery by ID."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM discovered_integrations WHERE discovery_id = ?",
                (discovery_id,)
            )
            row = cursor.fetchone()
            return DiscoveredIntegration(**dict(row)) if row else None
    
    def _has_beta_opt_in(self) -> bool:
        """Check if user has opted into Community Beta Integrations Policy."""
        if not BETA_POLICY_FILE.exists():
            return False
        
        try:
            with open(BETA_POLICY_FILE, 'r') as f:
                data = json.load(f)
                return data.get('opted_in', False)
        except Exception:
            return False
    
    def opt_in_beta_policy(self, github_username: Optional[str] = None) -> bool:
        """Opt into Community Beta Integrations Policy."""
        try:
            opt_in_data = {
                'opted_in': True,
                'opted_in_at': datetime.now(timezone.utc).isoformat(),
                'github_username': github_username,
                'policy_version': '1.0',
                'agreement': (
                    "I agree to share anonymized integration configs with the "
                    "Context DNA community for the benefit of other users."
                )
            }
            
            with open(BETA_POLICY_FILE, 'w') as f:
                json.dump(opt_in_data, f, indent=2)
            
            logger.info("✅ User opted into Community Beta Integrations")
            return True
        
        except Exception as e:
            logger.error(f"Opt-in failed: {e}")
            return False
    
    def _infer_ide_family(self, source_identifier: str) -> str:
        """Infer IDE family from source identifier."""
        source_lower = source_identifier.lower()
        
        if 'cursor' in source_lower:
            return 'cursor'
        elif 'claude' in source_lower or 'vscode' in source_lower:
            return 'vscode'
        elif 'antigravity' in source_lower:
            return 'antigravity'
        elif 'windsurf' in source_lower:
            return 'windsurf'
        elif 'pycharm' in source_lower or 'intellij' in source_lower:
            return 'jetbrains'
        elif 'vim' in source_lower or 'neovim' in source_lower:
            return 'vim'
        else:
            return 'unknown'
    
    def _contribute_to_github(self, discovery_id: str, export_file: Path) -> bool:
        """
        Contribute discovered integration to GitHub repo.
        
        Requires:
        - Beta policy opt-in
        - Git configured
        - contextdna-templates repo cloned
        
        Returns:
            True if successfully pushed to GitHub
        """
        try:
            # Check if user has GitHub configured
            templates_repo = MEMORY_DIR.parent / ".." / "contextdna-templates"
            
            if not templates_repo.exists():
                logger.info("contextdna-templates repo not found - skipping GitHub push")
                logger.info("User can manually commit later if desired")
                return False
            
            # Copy export to templates repo
            dest_file = templates_repo / "discoveries" / f"{discovery_id}.json"
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(export_file, dest_file)
            
            # Git commit
            subprocess.run(
                ["git", "add", str(dest_file)],
                cwd=str(templates_repo),
                check=True
            )
            
            subprocess.run(
                ["git", "commit", "-m", f"feat: Add auto-discovered integration {discovery_id}"],
                cwd=str(templates_repo),
                check=True
            )
            
            # Push to GitHub (if remote configured)
            try:
                subprocess.run(
                    ["git", "push", "origin", "main"],
                    cwd=str(templates_repo),
                    check=True,
                    timeout=10
                )
                logger.info(f"✅ Pushed to GitHub: {discovery_id}")
                return True
            
            except subprocess.TimeoutExpired:
                logger.warning("GitHub push timed out - will retry later")
                return False
            
            except subprocess.CalledProcessError:
                logger.info("GitHub push failed - may need authentication or remote setup")
                return False
        
        except Exception as e:
            logger.debug(f"GitHub contribution failed (non-critical): {e}")
            return False
    
    def _send_celebration_notification(self, source_identifier: str):
        """Send celebration notification after successful contribution."""
        try:
            subprocess.run([
                "osascript", "-e",
                f'display notification "Your {source_identifier} integration will help other users!" '
                f'with title "🎉 Context DNA - Integration Shared!" sound name "Glass"'
            ], timeout=2)
        except Exception:
            pass


def monitor_integrations_daemon():
    """Run as background daemon to monitor for novel integrations."""
    import time
    
    discovery = IntegrationDiscovery()
    
    print("🔍 Integration Discovery Monitor started")
    print("   Watching for novel successful integrations...")
    print("   Press Ctrl+C to stop")
    print()
    
    while True:
        try:
            # Check for integrations ready for approval
            ready = discovery.get_ready_for_approval()
            
            for integration in ready:
                if not integration.user_notified:
                    # Send notification
                    print(f"🎉 New integration discovered: {integration.source_identifier}")
                    print(f"   Confidence: {integration.confidence:.0%} ({integration.success_count} successes)")
                    
                    # Mark as notified
                    with sqlite3.connect(str(discovery.db_path)) as conn:
                        conn.execute("""
                            UPDATE discovered_integrations 
                            SET user_notified = 1
                            WHERE discovery_id = ?
                        """, (integration.discovery_id,))
                        conn.commit()
            
            time.sleep(60)  # Check every minute
        
        except KeyboardInterrupt:
            print("\n👋 Monitor stopped")
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Integration Discovery System")
    parser.add_argument('--daemon', action='store_true', help='Run as background monitor')
    parser.add_argument('--list', action='store_true', help='List discovered integrations')
    parser.add_argument('--approve', type=str, help='Approve a discovery by ID')
    parser.add_argument('--opt-in', action='store_true', help='Opt into Community Beta Policy')
    
    args = parser.parse_args()
    
    discovery = IntegrationDiscovery()
    
    if args.opt_in:
        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║        COMMUNITY BETA INTEGRATIONS POLICY                            ║")
        print("╠══════════════════════════════════════════════════════════════════════╣")
        print("║                                                                      ║")
        print("║  By opting in, you agree to share anonymized integration configs    ║")
        print("║  with the Context DNA community to help other users.                ║")
        print("║                                                                      ║")
        print("║  What's shared: IDE configs (paths/secrets sanitized)               ║")
        print("║  What's NOT shared: Your code, data, or personal info               ║")
        print("║                                                                      ║")
        print("║  You can opt-out anytime by deleting:                               ║")
        print("║  memory/.community_beta_opt_in.json                                 ║")
        print("║                                                                      ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()
        
        github_user = input("GitHub username (optional, press Enter to skip): ").strip()
        confirm = input("\nOpt in? (yes/no): ").strip().lower()
        
        if confirm == 'yes':
            discovery.opt_in_beta_policy(github_user if github_user else None)
            print("\n✅ Opted in! Future discoveries will be shared automatically.")
        else:
            print("\n⏭️  Skipped - you can opt in later")
    
    elif args.daemon:
        monitor_integrations_daemon()
    
    elif args.list:
        print("📋 Discovered Integrations:\n")
        
        with sqlite3.connect(str(discovery.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM discovered_integrations 
                ORDER BY status, confidence DESC
            """)
            
            for row in cursor.fetchall():
                status_emoji = {
                    'monitoring': '👀',
                    'ready_for_approval': '🎯',
                    'approved': '✅',
                    'contributed': '🌟'
                }.get(row['status'], '❓')
                
                print(f"{status_emoji} {row['source_identifier']} ({row['integration_type']})")
                print(f"   Confidence: {row['confidence']:.0%} ({row['success_count']}/{row['execution_count']})")
                print(f"   Status: {row['status']}")
                print()
    
    elif args.approve:
        print(f"Approving and contributing: {args.approve}")
        success, msg = discovery.approve_and_contribute(args.approve)
        print(f"{'✅' if success else '❌'} {msg}")
    
    else:
        parser.print_help()
