#!/usr/bin/env python3
"""
Register Cursor IDE in Context DNA Configuration Database

Adds Cursor to the ide_configurations table with full integration support.
This enables tracking of hook activity, injection preferences, and IDE-specific configuration.
"""

import sqlite3
import uuid
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path for imports
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory.ide_detection import detect_active_ide, get_ide_context, IDE


def get_machine_id(conn: sqlite3.Connection) -> str:
    """Get or create machine ID."""
    cursor = conn.cursor()
    
    # Try to get existing machine ID from system_config
    try:
        cursor.execute("SELECT value FROM system_config WHERE key = 'machine_id'")
        result = cursor.fetchone()
        if result:
            return result[0]
    except sqlite3.OperationalError:
        # system_config table doesn't exist - that's okay
        pass
    
    # Try to get from existing IDE configurations
    try:
        cursor.execute("SELECT DISTINCT machine_id FROM ide_configurations LIMIT 1")
        result = cursor.fetchone()
        if result:
            return result[0]
    except sqlite3.OperationalError:
        # ide_configurations table doesn't exist yet - that's okay
        pass
    
    # Generate new machine ID based on hostname
    import socket
    hostname = socket.gethostname()
    machine_id = f"cursor-{hostname}-{str(uuid.uuid4())[:8]}"
    
    return machine_id


def register_cursor():
    """Register Cursor IDE in ide_configurations table."""
    
    # Database path
    db_path = Path.home() / ".context-dna" / "context_dna.db"
    
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        print("   Run Context DNA installation first:")
        print("   ./scripts/install-context-dna.sh")
        return False
    
    # Detect if Cursor is currently active
    ctx = get_ide_context()
    is_cursor_active = ctx.ide == IDE.CURSOR
    
    if is_cursor_active:
        print(f"✅ Cursor detected as active IDE")
        print(f"   Workspace: {ctx.workspace_folder or 'N/A'}")
        print(f"   Active File: {ctx.active_file_path or 'N/A'}")
    else:
        print(f"⚠️ Cursor not currently active (detected: {ctx.ide_name})")
        print("   Proceeding with registration anyway...")
    
    # Connect to database
    conn = sqlite3.connect(str(db_path))
    try:
        # Ensure ide_configurations table exists
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ide_configurations (
                id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                ide_type TEXT NOT NULL CHECK (ide_type IN (
                    'cursor', 'vscode', 'windsurf', 'zed', 'jetbrains',
                    'neovim', 'emacs', 'sublime', 'xcode', 'other'
                )),
                ide_version TEXT,
                ide_path TEXT,
                is_installed INTEGER DEFAULT 0,
                is_configured INTEGER DEFAULT 0,
                is_primary INTEGER DEFAULT 0,
                config_path TEXT,
                hook_version TEXT,
                hook_installed_at TEXT,
                last_hook_activity TEXT,
                enable_injections INTEGER DEFAULT 1,
                injection_style TEXT DEFAULT 'full',
                created_at TEXT DEFAULT (datetime('now') || 'Z'),
                updated_at TEXT DEFAULT (datetime('now') || 'Z'),
                synced_to_postgres INTEGER DEFAULT 0,
                synced_at TEXT,
                postgres_id TEXT,
                UNIQUE(machine_id, ide_type)
            )
        """)
        conn.commit()
        
        # Get or create machine ID
        machine_id = get_machine_id(conn)
        
        # Check if Cursor already registered
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, is_configured, injection_style, hook_version
            FROM ide_configurations 
            WHERE machine_id = ? AND ide_type = 'cursor'
        """, (machine_id,))
        
        existing = cursor.fetchone()
        
        now_utc = datetime.now(timezone.utc).isoformat()
        
        if existing:
            config_id, is_configured, injection_style, hook_version = existing
            print(f"\n✅ Cursor already registered (ID: {config_id[:8]}...)")
            print(f"   Configured: {'Yes' if is_configured else 'No'}")
            print(f"   Injection Style: {injection_style}")
            print(f"   Hook Version: {hook_version or 'N/A'}")
            
            # Update registration
            cursor.execute("""
                UPDATE ide_configurations 
                SET 
                    is_installed = 1,
                    ide_path = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                '/Applications/Cursor.app',
                now_utc,
                config_id
            ))
            conn.commit()
            print(f"\n✅ Registration updated")
            
        else:
            # Create new registration
            config_id = str(uuid.uuid4())
            
            cursor.execute("""
                INSERT INTO ide_configurations (
                    id,
                    machine_id,
                    ide_type,
                    ide_version,
                    ide_path,
                    is_installed,
                    is_configured,
                    is_primary,
                    config_path,
                    hook_version,
                    hook_installed_at,
                    enable_injections,
                    injection_style,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                config_id,
                machine_id,
                'cursor',
                'latest',  # Will be detected dynamically
                '/Applications/Cursor.app',
                1,  # is_installed
                0,  # is_configured (will be set to 1 after webhook setup)
                1,  # is_primary (assume Cursor is primary IDE)
                str(Path.home() / ".cursor"),
                '2.0.0-dynamic',  # hook_version
                now_utc,
                1,  # enable_injections
                'full',  # injection_style: full, minimal, or custom
                now_utc,
                now_utc
            ))
            
            conn.commit()
            
            print(f"\n✅ Cursor registered in Context DNA database")
            print(f"   ID: {config_id}")
            print(f"   Machine ID: {machine_id}")
            print(f"   Config Path: {Path.home() / '.cursor'}")
            print(f"   Injection Style: full")
            print(f"   Hook Version: 2.0.0-dynamic")
        
        # Show next steps
        print(f"\n━━━ NEXT STEPS ━━━")
        print(f"1. Install webhook integration:")
        print(f"   ./scripts/install-cursor-webhook.sh")
        print(f"2. Verify installation:")
        print(f"   ./scripts/verify-cursor-webhook.sh")
        print(f"3. Check helper agent:")
        print(f"   curl http://localhost:8080/health")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Registration failed: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        conn.close()


if __name__ == "__main__":
    success = register_cursor()
    sys.exit(0 if success else 1)
