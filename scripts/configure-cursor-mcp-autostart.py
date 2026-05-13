#!/usr/bin/env python3
"""
Configure Cursor MCP Auto-Start Integration

This script:
1. Marks Cursor as configured in ide_configurations table
2. Registers MCP server in webhook_destination table
3. Triggers bidirectional sync to PostgreSQL
4. Verifies the configuration works
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(REPO_ROOT))


def main():
    print("🔧 Configuring Cursor MCP Auto-Start Integration")
    print("=" * 70)
    print()
    
    # 1. Update ide_configurations in context_dna.db
    print("Step 1: Updating IDE configuration...")
    try:
        db_path = Path.home() / ".context-dna" / "context_dna.db"
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        
        cursor = db.execute(
            "SELECT * FROM ide_configurations WHERE ide_type = 'cursor'"
        )
        existing = cursor.fetchone()
        
        if existing:
            print(f"  Found existing Cursor config:")
            print(f"    Installed: {bool(existing['is_installed'])}")
            print(f"    Configured: {bool(existing['is_configured'])}")
            print(f"    Hook version: {existing['hook_version']}")
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Update to mark as configured
        db.execute("""
            UPDATE ide_configurations
            SET is_configured = 1,
                hook_installed_at = ?,
                last_hook_activity = ?,
                config_path = '~/.cursor/mcp.json',
                injection_style = 'mcp',
                enable_injections = 1,
                updated_at = ?
            WHERE ide_type = 'cursor'
        """, (now, now, now))
        db.commit()
        
        print("  ✅ Cursor marked as configured (MCP auto-start)")
        db.close()
    
    except Exception as e:
        print(f"  ⚠️ IDE configuration update failed: {e}")
    
    print()
    
    # 2. Register MCP server in webhook_destination (observability.db)
    print("Step 2: Registering MCP server in webhook destinations...")
    try:
        from memory.db_utils import safe_conn
        db_path = REPO_ROOT / "memory" / ".observability.db"
        
        now = datetime.now(timezone.utc).isoformat()
        
        with safe_conn(db_path) as db:
            # Check if already exists
            cursor = db.execute(
                "SELECT * FROM webhook_destination WHERE destination_id = 'cursor_mcp'"
            )
            existing = cursor.fetchone()
            
            if existing:
                print("  Found existing MCP destination - updating...")
                db.execute("""
                    UPDATE webhook_destination
                    SET endpoint_url = ?,
                        config_json = ?,
                        notes = ?
                    WHERE destination_id = 'cursor_mcp'
                """, (
                    'mcp://contextdna-webhook',
                    json.dumps({
                        "protocol": "mcp",
                        "version": "2.0.0",
                        "auto_start": True,
                        "wrapper_script": "mcp-startup-wrapper.sh",
                        "health_check": "ensure-context-dna-running.sh"
                    }),
                    "MCP server with auto-start wrapper - ensures Context DNA is running"
                ))
            else:
                print("  Creating new MCP destination...")
                db.execute("""
                    INSERT INTO webhook_destination
                    (destination_id, destination_name, destination_type, endpoint_url, 
                     is_active, created_at, config_json, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    'cursor_mcp',
                    'Cursor MCP Server',
                    'ide',
                    'mcp://contextdna-webhook',
                    1,
                    now,
                    json.dumps({
                        "protocol": "mcp",
                        "version": "2.0.0",
                        "auto_start": True,
                        "wrapper_script": "mcp-startup-wrapper.sh",
                        "health_check": "ensure-context-dna-running.sh"
                    }),
                    "MCP server with auto-start wrapper - ensures Context DNA is running"
                ))
        
        print("  ✅ MCP destination registered")
    
    except Exception as e:
        print(f"  ⚠️ Destination registration failed: {e}")
    
    print()
    
    # 3. Trigger bidirectional sync
    print("Step 3: Triggering bidirectional sync to PostgreSQL...")
    try:
        from memory.unified_sync import get_sync_engine
        
        engine = get_sync_engine()
        report = engine.sync_all(caller="cursor_mcp_setup")
        
        if report.total_pushed > 0 or report.total_pulled > 0:
            print(f"  ✅ Synced: {report.total_pushed} pushed, {report.total_pulled} pulled")
        else:
            print("  ✓ Sync complete (no changes needed)")
    
    except Exception as e:
        print(f"  ⚠️ Sync skipped: {e}")
        print("     (This is OK if PostgreSQL is offline)")
    
    print()
    
    # 4. Verification
    print("Step 4: Verifying configuration...")
    errors = []
    
    # Check MCP configs exist
    mcp_configs = [
        Path.home() / ".cursor" / "mcp.json",
        REPO_ROOT / ".mcp.json"
    ]
    
    for config_path in mcp_configs:
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
                if "contextdna-webhook" in config.get("mcpServers", {}):
                    server_config = config["mcpServers"]["contextdna-webhook"]
                    if "mcp-startup-wrapper.sh" in server_config.get("command", ""):
                        print(f"  ✅ {config_path.name} uses auto-start wrapper")
                    else:
                        errors.append(f"{config_path.name} not using wrapper script")
                else:
                    errors.append(f"{config_path.name} missing contextdna-webhook server")
        else:
            errors.append(f"{config_path} not found")
    
    # Check scripts exist and are executable
    scripts = [
        REPO_ROOT / "mcp-servers" / "mcp-startup-wrapper.sh",
        REPO_ROOT / "scripts" / "ensure-context-dna-running.sh",
    ]
    
    for script in scripts:
        if script.exists():
            import os
            if os.access(script, os.X_OK):
                print(f"  ✅ {script.name} is executable")
            else:
                errors.append(f"{script.name} not executable")
        else:
            errors.append(f"{script.name} not found")
    
    print()
    
    if errors:
        print("⚠️ Issues found:")
        for error in errors:
            print(f"  - {error}")
        print()
        return 1
    else:
        print("=" * 70)
        print("✅ AUTO-START CONFIGURATION COMPLETE!")
        print("=" * 70)
        print()
        print("What happens now:")
        print("  1. When you start a new Cursor chat")
        print("  2. Cursor loads MCP server via wrapper script")
        print("  3. Wrapper checks if Context DNA is running")
        print("  4. If not, auto-starts Context DNA")
        print("  5. MCP server registers session in database")
        print("  6. Webhook payload is generated fresh for each message")
        print()
        print("To test:")
        print("  1. Restart Cursor (to reload MCP configuration)")
        print("  2. Open a new chat")
        print("  3. Ask: 'Can you verify you received the webhook payload?'")
        print()
        return 0


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
