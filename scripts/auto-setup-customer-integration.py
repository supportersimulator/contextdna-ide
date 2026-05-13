#!/usr/bin/env python3
"""
Automatic Customer Integration Setup

ONE-COMMAND setup for Context DNA webhook integration.
Detects installed IDEs, configures hooks, registers destinations.

Usage:
    # Interactive mode (detects and prompts)
    python scripts/auto-setup-customer-integration.py
    
    # Auto mode (sets up everything automatically)
    python scripts/auto-setup-customer-integration.py --auto
    
    # Specific IDE only
    python scripts/auto-setup-customer-integration.py --ide cursor
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memory.destination_registry import DestinationRegistry
from memory.community_templates import CommunityTemplates


class IDEDetector:
    """Detects installed IDEs and their configuration locations."""
    
    @staticmethod
    def detect_all() -> List[Dict[str, str]]:
        """Detect all installed IDEs."""
        detected = []
        
        # Claude Code (VS Code extension)
        if IDEDetector._is_claude_code_installed():
            detected.append({
                'id': 'vs_code_claude_code',
                'name': 'VS Code (Claude Code)',
                'family': 'vscode',
                'config_path': str(Path.home() / '.claude' / 'settings.local.json'),
                'hook_script': 'auto-memory-query.sh',
                'hook_name': 'UserPromptSubmit',
                'capture_hook': 'PostToolUse',
                'capture_script': 'auto-capture-results.sh'
            })
        
        # Cursor
        if IDEDetector._is_cursor_installed():
            detected.append({
                'id': 'cursor_ide',
                'name': 'Cursor',
                'family': 'cursor',
                'config_path': str(Path.home() / '.cursor' / 'hooks.json'),
                'hook_script': 'auto-memory-query-cursor.sh',
                'hook_name': 'beforeSubmitPrompt',
                'capture_hook': 'afterFileEdit',
                'capture_script': 'auto-capture-results-cursor.sh'
            })
        
        # Windsurf
        if IDEDetector._is_windsurf_installed():
            detected.append({
                'id': 'windsurf_ide',
                'name': 'Windsurf',
                'family': 'windsurf',
                'config_path': str(Path.home() / '.windsurf' / 'settings.json'),
                'hook_script': 'auto-memory-query-windsurf.sh',
                'hook_name': 'UserPromptSubmit',  # Assuming similar to VS Code
                'capture_hook': 'PostToolUse',
                'capture_script': 'auto-capture-results-windsurf.sh'
            })
        
        return detected
    
    @staticmethod
    def _is_claude_code_installed() -> bool:
        """Check if Claude Code extension is installed."""
        claude_dir = Path.home() / '.claude'
        vscode_dir = Path.home() / 'Library' / 'Application Support' / 'Code' / 'User' / 'globalStorage'
        
        return claude_dir.exists() or (vscode_dir.exists() and 
            any(vscode_dir.glob('**/claude*')))
    
    @staticmethod
    def _is_cursor_installed() -> bool:
        """Check if Cursor is installed."""
        cursor_dir = Path.home() / '.cursor'
        cursor_app = Path('/Applications/Cursor.app')
        
        return cursor_dir.exists() or cursor_app.exists()
    
    @staticmethod
    def _is_windsurf_installed() -> bool:
        """Check if Windsurf is installed."""
        windsurf_dir = Path.home() / '.windsurf'
        return windsurf_dir.exists()


class CustomerIntegrationSetup:
    """Automated setup system for customers."""
    
    def __init__(self, repo_root: Path = REPO_ROOT):
        self.repo_root = repo_root
        self.registry = DestinationRegistry()
        self.setup_log = []
    
    def log(self, message: str):
        """Log setup steps."""
        print(message)
        self.setup_log.append(message)
    
    def setup_ide(self, ide_info: Dict[str, str], auto_approve: bool = False) -> bool:
        """
        Setup Context DNA integration for a specific IDE.
        
        Steps:
        1. Backup existing config
        2. Create/update config file with hooks
        3. Ensure hook scripts exist and are executable
        4. Register in destination registry
        5. Register in webhook_destination (observability)
        6. Test integration
        
        Args:
            ide_info: IDE detection info
            auto_approve: Skip confirmation prompts
        
        Returns:
            True if setup successful
        """
        ide_name = ide_info['name']
        
        self.log(f"\n{'='*70}")
        self.log(f"Setting up: {ide_name}")
        self.log(f"{'='*70}")
        
        # Confirm with user (unless auto mode)
        if not auto_approve:
            response = input(f"\nSetup {ide_name}? (y/N): ")
            if response.lower() != 'y':
                self.log(f"⏭️  Skipped {ide_name}")
                return False
        
        # Step 1: Backup existing config
        config_path = Path(ide_info['config_path'])
        if config_path.exists():
            backup_path = f"{config_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(config_path, backup_path)
            self.log(f"✅ Backed up config to: {backup_path}")
        
        # Step 2: Create/update hooks config
        success = self._configure_hooks(ide_info)
        if not success:
            self.log(f"❌ Failed to configure hooks for {ide_name}")
            return False
        
        # Step 3: Ensure scripts exist and are executable
        success = self._ensure_scripts(ide_info)
        if not success:
            self.log(f"❌ Failed to setup scripts for {ide_name}")
            return False
        
        # Step 4: Register in destination registry
        success = self._register_destination(ide_info)
        if not success:
            self.log(f"❌ Failed to register {ide_name}")
            return False
        
        # Step 5: Register in webhook_destination (observability)
        self._register_in_observability(ide_info)
        
        # Step 6: Test integration
        success = self._test_integration(ide_info)
        if success:
            self.log(f"✅ {ide_name} setup complete!")
            return True
        else:
            self.log(f"⚠️  {ide_name} setup complete but test failed (may need IDE restart)")
            return True
    
    def _configure_hooks(self, ide_info: Dict[str, str]) -> bool:
        """Create or update IDE hooks configuration."""
        config_path = Path(ide_info['config_path'])
        hook_script = str(self.repo_root / 'scripts' / ide_info['hook_script'])
        capture_script = str(self.repo_root / 'scripts' / ide_info['capture_script'])
        
        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # IDE-specific config format
        if ide_info['family'] == 'vscode':
            # Claude Code format
            config = {
                "hooks": {
                    ide_info['hook_name']: [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": hook_script
                                }
                            ]
                        }
                    ],
                    ide_info['capture_hook']: [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": capture_script
                                }
                            ]
                        }
                    ]
                }
            }
        
        elif ide_info['family'] == 'cursor':
            # Cursor format
            config = {
                ide_info['hook_name']: [
                    {
                        "command": hook_script
                    }
                ],
                ide_info['capture_hook']: [
                    {
                        "command": capture_script
                    }
                ]
            }
        
        else:
            self.log(f"⚠️  Unknown IDE family: {ide_info['family']}")
            return False
        
        # Write config
        try:
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            self.log(f"✅ Created {config_path}")
            return True
        
        except Exception as e:
            self.log(f"❌ Failed to write config: {e}")
            return False
    
    def _ensure_scripts(self, ide_info: Dict[str, str]) -> bool:
        """Ensure hook scripts exist and are executable."""
        scripts_to_check = [
            ide_info['hook_script'],
            ide_info['capture_script']
        ]
        
        all_good = True
        
        for script_name in scripts_to_check:
            script_path = self.repo_root / 'scripts' / script_name
            
            if not script_path.exists():
                self.log(f"❌ Script missing: {script_path}")
                all_good = False
                continue
            
            # Make executable
            os.chmod(script_path, 0o755)
            self.log(f"✅ Script ready: {script_name}")
        
        return all_good
    
    def _register_destination(self, ide_info: Dict[str, str]) -> bool:
        """Register in destination registry."""
        try:
            success, msg = self.registry.register_destination(
                destination_id=ide_info['id'],
                friendly_name=ide_info['name'],
                ide_family=ide_info['family'],
                delivery_method='hook',
                config_path=ide_info['config_path'],
                namespace=ide_info['family'].upper(),
                delivery_endpoint=str(self.repo_root / 'scripts' / ide_info['hook_script']),
                os_type=platform.system().lower().replace('darwin', 'macos'),
                registered_by='auto_setup'
            )
            
            if success or "Already registered" in msg:
                self.log(f"✅ Registered in destination registry")
                return True
            else:
                self.log(f"❌ Registration failed: {msg}")
                return False
        
        except Exception as e:
            self.log(f"❌ Registration error: {e}")
            return False
    
    def _register_in_observability(self, ide_info: Dict[str, str]):
        """Register in webhook_destination table (observability.db)."""
        try:
            from datetime import datetime, timezone
            from memory.db_utils import safe_conn
            
            obs_db = self.repo_root / 'memory' / '.observability.db'
            if not obs_db.exists():
                return
            
            with safe_conn(obs_db) as conn:
                # Insert or update
                conn.execute("""
                    INSERT OR REPLACE INTO webhook_destination (
                        destination_id, destination_name, destination_type,
                        endpoint_url, is_active, created_at,
                        total_deliveries, successful_deliveries, failed_deliveries,
                        config_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ide_info['id'],
                    ide_info['name'],
                    ide_info['family'],
                    str(self.repo_root / 'scripts' / ide_info['hook_script']),
                    1,  # is_active
                    datetime.now(timezone.utc).isoformat(),
                    0, 0, 0,  # Delivery counts
                    json.dumps(ide_info)
                ))
            
            self.log(f"✅ Registered in observability database")
        
        except Exception as e:
            self.log(f"⚠️  Could not register in observability: {e}")
    
    def _test_integration(self, ide_info: Dict[str, str]) -> bool:
        """Test that hook script executes properly."""
        hook_script = self.repo_root / 'scripts' / ide_info['hook_script']
        
        try:
            result = subprocess.run(
                [str(hook_script), "integration test"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self.repo_root)
            )
            
            if result.returncode == 0:
                self.log(f"✅ Hook test successful")
                return True
            else:
                self.log(f"⚠️  Hook test failed (exit {result.returncode})")
                return False
        
        except Exception as e:
            self.log(f"⚠️  Hook test error: {e}")
            return False


def setup_local_llm() -> bool:
    """Setup local LLM (mlx_lm.server) for Apple Silicon users."""
    # Check if Apple Silicon
    if platform.machine() != 'arm64':
        print("ℹ️  Not Apple Silicon - skipping local LLM (will use remote fallback)")
        return False

    print("\n🧠 Setting up local LLM (mlx_lm.server)...")

    # Check if already running
    try:
        import requests
        resp = requests.get("http://127.0.0.1:5044/v1/models", timeout=2)
        if resp.ok:
            data = resp.json()
            model = data.get("data", [{}])[0].get("id", "unknown")
            print(f"✅ Local LLM already running: {model}")
            return True
    except:
        pass

    # Check if start script exists
    start_script = REPO_ROOT / "scripts" / "start-llm.sh"
    if not start_script.exists():
        print("⚠️  start-llm.sh not found - local LLM will be unavailable")
        print("   (Webhook will use templates for Section 2, skip Section 8)")
        return False

    # Start local LLM
    try:
        print("   Starting mlx_lm.server (Qwen3-4B-4bit)...")
        print("   This may take 10-15 seconds for model load...")

        subprocess.run(
            [str(start_script)],
            cwd=str(REPO_ROOT),
            timeout=30
        )

        # Verify it started
        time.sleep(5)
        resp = requests.get("http://127.0.0.1:5044/v1/models", timeout=3)
        if resp.ok:
            print("✅ Local LLM started successfully!")
            return True
        else:
            print("⚠️  Local LLM started but not responding yet (may still be loading)")
            return False

    except Exception as e:
        print(f"⚠️  Local LLM startup error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Auto-setup Context DNA for IDEs")
    parser.add_argument('--auto', action='store_true', help='Auto-approve all setup steps')
    parser.add_argument('--ide', type=str, help='Setup specific IDE only (cursor, claude-code)')
    parser.add_argument('--skip-llm', action='store_true', help='Skip local LLM setup')
    parser.add_argument('--use-community', action='store_true', help='Pull templates from community repo')
    
    args = parser.parse_args()
    
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║  🧬 CONTEXT DNA - AUTOMATIC CUSTOMER INTEGRATION SETUP               ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()
    
    # Step 0: Pull community templates (if requested)
    if args.use_community:
        print("📚 Syncing community templates from GitHub...")
        templates = CommunityTemplates()
        count = templates.sync_from_github()
        print(f"✅ Imported {count} community templates\n")
    
    # Step 1: Setup local LLM (if not skipped)
    if not args.skip_llm:
        setup_local_llm()
    else:
        print("⏭️  Skipping local LLM setup (--skip-llm flag)")
    
    # Detect installed IDEs
    print("🔍 Detecting installed IDEs...")
    detector = IDEDetector()
    detected_ides = detector.detect_all()
    
    if not detected_ides:
        print("❌ No supported IDEs detected")
        print("\nSupported IDEs:")
        print("  • VS Code with Claude Code extension")
        print("  • Cursor IDE")
        print("  • Windsurf")
        sys.exit(1)
    
    print(f"✅ Found {len(detected_ides)} IDE(s):")
    for ide in detected_ides:
        print(f"   • {ide['name']}")
    print()
    
    # Filter if specific IDE requested
    if args.ide:
        detected_ides = [ide for ide in detected_ides if args.ide.lower() in ide['id'].lower()]
        if not detected_ides:
            print(f"❌ IDE not found: {args.ide}")
            sys.exit(1)
    
    # Setup each IDE
    setup = CustomerIntegrationSetup()
    success_count = 0
    
    for ide in detected_ides:
        if setup.setup_ide(ide, auto_approve=args.auto):
            success_count += 1
    
    # Summary
    print(f"\n{'='*70}")
    print(f"Setup Summary: {success_count}/{len(detected_ides)} successful")
    print(f"{'='*70}")
    
    if success_count > 0:
        print("\n✅ Context DNA webhook integration is ready!")
        print("\n📋 Next steps:")
        print("  1. Restart your IDE(s) to load new configuration")
        print("  2. Send a test message")
        print("  3. Verify context injection (check logs)")
        print("\n📊 Monitor:")
        print("  • Cursor: tail -f /tmp/context-dna-cursor-hook.log")
        print("  • Claude Code: tail -f /tmp/context-dna-hook.log")
    
    sys.exit(0 if success_count == len(detected_ides) else 1)


if __name__ == "__main__":
    main()
