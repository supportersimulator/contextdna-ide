#!/usr/bin/env python3
"""
Configure Integration Preferences for Context DNA

Sets up user preferences for how Context DNA integrates with their IDE/tools.
Supports multiple integration methods: MCP, HTTP API, WebSocket, Polling, Hooks.

Usage:
    python scripts/configure-integration-preferences.py cursor
    python scripts/configure-integration-preferences.py antigravity
    python scripts/configure-integration-preferences.py --list
"""

import sqlite3
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = Path.home() / ".context-dna" / "context_dna.db"


INTEGRATION_PRESETS = {
    "cursor": {
        "primary_method": "mcp",
        "secondary_method": "polling",
        "enable_background_polling": 1,
        "mcp_resources": ["contextdna://webhook", "contextdna://session-recovery"],
        "api_endpoint": None,
        "websocket_url": None,
        "polling_interval_s": 60,
        "description": "MCP per-message + background polling for ambient awareness"
    },
    "claude-code": {
        "primary_method": "hooks",
        "secondary_method": None,
        "enable_background_polling": 0,
        "mcp_resources": None,
        "api_endpoint": None,
        "websocket_url": None,
        "polling_interval_s": None,
        "description": "UserPromptSubmit hook (real-time, 0ms)"
    },
    "antigravity": {
        "primary_method": "http_api",
        "secondary_method": "websocket",
        "enable_background_polling": 0,
        "mcp_resources": None,
        "api_endpoint": "http://127.0.0.1:8080/contextdna/inject/antigravity",
        "websocket_url": "ws://127.0.0.1:8080/ws/contextdna",
        "polling_interval_s": None,
        "description": "HTTP REST API + WebSocket for real-time dashboard"
    },
    "electron": {
        "primary_method": "hybrid",
        "secondary_method": "websocket",
        "enable_background_polling": 1,
        "mcp_resources": ["contextdna://webhook"],
        "api_endpoint": "http://127.0.0.1:8080/contextdna/inject",
        "websocket_url": "ws://127.0.0.1:8080/ws/contextdna",
        "polling_interval_s": 60,
        "description": "Hybrid: MCP + HTTP + WebSocket (maximum flexibility)"
    },
    "vscode": {
        "primary_method": "extension_api",
        "secondary_method": "http_api",
        "enable_background_polling": 0,
        "mcp_resources": None,
        "api_endpoint": "http://127.0.0.1:8080/consult/unified",
        "websocket_url": None,
        "polling_interval_s": None,
        "description": "VS Code extension with HTTP API backup"
    }
}


def create_integration_preferences_table(conn):
    """Create integration_preferences table if missing."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS integration_preferences (
            destination_id TEXT PRIMARY KEY,
            display_name TEXT,
            primary_method TEXT CHECK (primary_method IN (
                'mcp', 'http_api', 'websocket', 'polling', 'hooks', 
                'extension_api', 'hybrid'
            )),
            secondary_method TEXT,
            enable_background_polling INTEGER DEFAULT 1,
            mcp_resources TEXT,
            api_endpoint TEXT,
            websocket_url TEXT,
            polling_interval_s INTEGER DEFAULT 60,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now') || 'Z'),
            updated_at TEXT DEFAULT (datetime('now') || 'Z')
        )
    """)
    conn.commit()


def configure_integration(destination_id: str):
    """Configure integration for a specific destination."""
    
    if destination_id not in INTEGRATION_PRESETS:
        print(f"❌ Unknown destination: {destination_id}")
        print(f"   Available: {', '.join(INTEGRATION_PRESETS.keys())}")
        return False
    
    preset = INTEGRATION_PRESETS[destination_id]
    
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Create table if needed
        create_integration_preferences_table(conn)
        
        # Check if already configured
        result = conn.execute(
            "SELECT destination_id FROM integration_preferences WHERE destination_id = ?",
            (destination_id,)
        ).fetchone()
        
        now_utc = datetime.now(timezone.utc).isoformat()
        
        if result:
            # Update existing
            conn.execute("""
                UPDATE integration_preferences 
                SET 
                    primary_method = ?,
                    secondary_method = ?,
                    enable_background_polling = ?,
                    mcp_resources = ?,
                    api_endpoint = ?,
                    websocket_url = ?,
                    polling_interval_s = ?,
                    description = ?,
                    updated_at = ?
                WHERE destination_id = ?
            """, (
                preset["primary_method"],
                preset["secondary_method"],
                preset["enable_background_polling"],
                json.dumps(preset["mcp_resources"]) if preset["mcp_resources"] else None,
                preset["api_endpoint"],
                preset["websocket_url"],
                preset["polling_interval_s"],
                preset["description"],
                now_utc,
                destination_id
            ))
            print(f"✅ Updated configuration for {destination_id}")
        else:
            # Insert new
            conn.execute("""
                INSERT INTO integration_preferences (
                    destination_id, display_name, primary_method, secondary_method,
                    enable_background_polling, mcp_resources, api_endpoint,
                    websocket_url, polling_interval_s, description,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                destination_id,
                destination_id.title(),
                preset["primary_method"],
                preset["secondary_method"],
                preset["enable_background_polling"],
                json.dumps(preset["mcp_resources"]) if preset["mcp_resources"] else None,
                preset["api_endpoint"],
                preset["websocket_url"],
                preset["polling_interval_s"],
                preset["description"],
                now_utc,
                now_utc
            ))
            print(f"✅ Created configuration for {destination_id}")
        
        conn.commit()
        
        # Show configuration
        print(f"\n━━━ Configuration for {destination_id} ━━━")
        print(f"Primary Method: {preset['primary_method']}")
        print(f"Secondary Method: {preset['secondary_method']}")
        print(f"Background Polling: {'Enabled' if preset['enable_background_polling'] else 'Disabled'}")
        if preset['mcp_resources']:
            print(f"MCP Resources: {', '.join(preset['mcp_resources'])}")
        if preset['api_endpoint']:
            print(f"API Endpoint: {preset['api_endpoint']}")
        if preset['websocket_url']:
            print(f"WebSocket: {preset['websocket_url']}")
        if preset['polling_interval_s']:
            print(f"Polling Interval: {preset['polling_interval_s']}s")
        print(f"\nDescription: {preset['description']}")
        
        return True
        
    finally:
        conn.close()


def list_configurations():
    """List all configured integrations."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Check if table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='integration_preferences'"
        ).fetchall()
        
        if not tables:
            print("❌ Integration preferences table not created yet")
            print("   Run: python scripts/configure-integration-preferences.py cursor")
            return
        
        results = conn.execute("""
            SELECT destination_id, primary_method, secondary_method, 
                   enable_background_polling, updated_at
            FROM integration_preferences
            ORDER BY updated_at DESC
        """).fetchall()
        
        if not results:
            print("No integrations configured yet")
            return
        
        print(f"\n🧬 Context DNA Integration Configurations\n")
        print(f"{'Destination':<15} {'Primary':<15} {'Secondary':<12} {'Polling':<8} {'Updated'}")
        print("─" * 75)
        
        for dest, primary, secondary, polling, updated in results:
            polling_status = "Yes" if polling else "No"
            updated_date = updated[:10] if updated else "N/A"
            print(f"{dest:<15} {primary:<15} {secondary or 'None':<12} {polling_status:<8} {updated_date}")
        
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Configure Context DNA integration preferences")
    parser.add_argument("destination", nargs="?", help="Destination to configure (cursor, antigravity, electron, etc.)")
    parser.add_argument("--list", action="store_true", help="List all configured integrations")
    
    args = parser.parse_args()
    
    if args.list:
        list_configurations()
    elif args.destination:
        success = configure_integration(args.destination)
        sys.exit(0 if success else 1)
    else:
        print("🧬 Context DNA Integration Preferences\n")
        print("Available destinations:")
        for dest, preset in INTEGRATION_PRESETS.items():
            print(f"  • {dest:<15} - {preset['description']}")
        print("\nUsage:")
        print("  python scripts/configure-integration-preferences.py cursor")
        print("  python scripts/configure-integration-preferences.py --list")
