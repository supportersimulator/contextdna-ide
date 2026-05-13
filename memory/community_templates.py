#!/usr/bin/env python3
"""
Community Template Library - Crowdsourced IDE Integration Configs

Enables users to:
1. Share working IDE integration configs (anonymized)
2. Download configs contributed by others
3. Vote on template quality
4. Auto-select best template for their IDE/OS

Templates are stored in SQLite and optionally synced to GitHub for community sharing.

Usage:
    from memory.community_templates import CommunityTemplates
    
    templates = CommunityTemplates()
    
    # Get best template for an IDE
    template = templates.get_best_template("cursor", "macos")
    
    # Contribute your working config
    templates.contribute_template(
        ide_family="cursor",
        os_type="macos",
        config_data={...},
        notes="Works perfectly on M3 MacBook"
    )
    
    # Sync with GitHub (pull community templates)
    templates.sync_from_github()
"""

import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent
TEMPLATES_DB = MEMORY_DIR / ".community_templates.db"
GITHUB_TEMPLATES_URL = "https://raw.githubusercontent.com/supportersimulator/contextdna-templates/main/templates.json"


@dataclass
class IntegrationTemplate:
    """Community-contributed integration template."""
    template_id: str              # SHA256 hash of content
    ide_family: str               # "cursor", "vscode", "jetbrains", "vim"
    os_type: str                  # "macos", "linux", "windows"
    ide_version: Optional[str]    # "1.5.2" - Specific version this was tested on
    min_version: Optional[str]    # "1.5.0" - Minimum compatible version
    max_version: Optional[str]    # "1.6.0" - Maximum known compatible version
    last_known_working: str       # "1.5.2" - Last version verified working
    
    # Configuration
    config_format: str            # "json", "yaml", "toml", "lua"
    config_content: str           # Actual config (paths sanitized)
    hook_scripts: Dict[str, str]  # {"beforeSubmit": "script content", ...}
    
    # Metadata
    contributed_by: str           # "anonymous", "username", "verified_maintainer"
    contributed_at: str
    success_count: int            # How many users report this works
    failure_count: int            # How many users report this fails
    confidence_score: float       # success / (success + failure)
    
    # Community
    upvotes: int
    downvotes: int
    notes: str                    # "Works on M3 MacBook", "Requires Cursor 1.5+"
    
    # Verification
    is_verified: bool             # Verified by maintainers
    verified_by: Optional[str]
    verified_at: Optional[str]


@dataclass
class LLMConfigTemplate:
    """Community-contributed LLM configuration (vLLM-MLX, Ollama, etc.)."""
    config_id: str                # SHA256 hash
    backend_type: str             # "vllm-mlx", "ollama", "lmstudio", "openai"
    model_name: str               # "Qwen2.5-Coder-14B-Instruct-4bit"
    hardware_type: str            # "apple_m1", "apple_m3", "nvidia_rtx4090", "cpu_only"
    
    # Configuration (micro-detail)
    model_params: Dict[str, Any]  # All model parameters
    server_config: Dict[str, Any] # Server startup config
    performance_config: Dict[str, Any]  # Performance tuning
    
    # Hardware constraints
    min_ram_gb: int
    min_vram_gb: Optional[int]
    recommended_ram_gb: int
    
    # Performance metrics
    avg_tokens_per_sec: float
    avg_first_token_ms: int
    context_window: int
    
    # Metadata
    contributed_by: str
    contributed_at: str
    success_count: int
    confidence_score: float
    
    # Community
    upvotes: int
    downvotes: int
    notes: str                    # "Perfect for M3 Max 64GB", "Fast on RTX 4090"
    
    # Verification
    is_verified: bool
    verified_by: Optional[str]
    
    # Moderation
    is_flagged: bool              # Flagged for review
    flagged_reason: Optional[str]
    is_approved: bool             # Admin approved
    approved_by: Optional[str]


class CommunityTemplates:
    """Manage community-contributed IDE integration templates."""
    
    def __init__(self, db_path: Path = TEMPLATES_DB):
        self.db_path = db_path
        self._ensure_schema()
    
    def _ensure_schema(self):
        """Create community templates schema."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS integration_templates (
                    template_id TEXT PRIMARY KEY,
                    ide_family TEXT NOT NULL,
                    os_type TEXT NOT NULL,
                    ide_version TEXT,
                    min_version TEXT,
                    max_version TEXT,
                    last_known_working TEXT,
                    
                    config_format TEXT NOT NULL,
                    config_content TEXT NOT NULL,
                    hook_scripts_json TEXT,
                    
                    contributed_by TEXT DEFAULT 'anonymous',
                    contributed_at TEXT NOT NULL,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    confidence_score REAL DEFAULT 0.0,
                    
                    upvotes INTEGER DEFAULT 0,
                    downvotes INTEGER DEFAULT 0,
                    notes TEXT,
                    
                    is_verified BOOLEAN DEFAULT 0,
                    verified_by TEXT,
                    verified_at TEXT,
                    
                    UNIQUE(ide_family, os_type, ide_version, config_content)
                )
            """)
            
            # Version compatibility tracking (which versions work with which templates)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS version_compatibility (
                    compat_id TEXT PRIMARY KEY,
                    template_id TEXT NOT NULL,
                    ide_version TEXT NOT NULL,
                    works BOOLEAN,
                    tested_at TEXT NOT NULL,
                    tested_by TEXT,
                    notes TEXT,
                    
                    FOREIGN KEY (template_id) REFERENCES integration_templates(template_id)
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_version_compat 
                ON version_compatibility(template_id, ide_version, works DESC)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_template_lookup 
                ON integration_templates(ide_family, os_type, confidence_score DESC)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_template_verified 
                ON integration_templates(is_verified, confidence_score DESC)
            """)
            
            # User feedback table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS template_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    template_id TEXT NOT NULL,
                    user_id TEXT,
                    success BOOLEAN,
                    feedback_text TEXT,
                    created_at TEXT NOT NULL,
                    
                    FOREIGN KEY (template_id) REFERENCES integration_templates(template_id)
                )
            """)
            
            conn.commit()
    
    def get_best_template(
        self,
        ide_family: str,
        os_type: str,
        ide_version: Optional[str] = None
    ) -> Optional[IntegrationTemplate]:
        """
        Get the best community template for an IDE/OS combination.
        
        Selection criteria (in order):
        1. Verified templates (by maintainers)
        2. Highest confidence score (success rate)
        3. Most upvotes
        4. Most recent
        
        Args:
            ide_family: "cursor", "vscode", etc.
            os_type: "macos", "linux", "windows"
            ide_version: Optional version constraint
        
        Returns:
            Best matching template or None
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Query with fallback strategy
            # 1. Try exact version match if provided
            # 2. Try version-agnostic templates
            # 3. Try verified templates only
            
            queries = []
            
            if ide_version:
                # Exact version
                queries.append((
                    "WHERE ide_family = ? AND os_type = ? AND ide_version = ?",
                    (ide_family, os_type, ide_version)
                ))
                # Compatible version (version-agnostic)
                queries.append((
                    "WHERE ide_family = ? AND os_type = ? AND (ide_version IS NULL OR ide_version = '')",
                    (ide_family, os_type)
                ))
            else:
                # Version-agnostic
                queries.append((
                    "WHERE ide_family = ? AND os_type = ?",
                    (ide_family, os_type)
                ))
            
            for where_clause, params in queries:
                cursor.execute(f"""
                    SELECT * FROM integration_templates
                    {where_clause}
                    ORDER BY 
                        is_verified DESC,
                        confidence_score DESC,
                        upvotes DESC,
                        contributed_at DESC
                    LIMIT 1
                """, params)
                
                row = cursor.fetchone()
                if row:
                    template_dict = dict(row)
                    # Parse hook_scripts JSON
                    hook_scripts_json = template_dict.get('hook_scripts_json', '{}')
                    template_dict['hook_scripts'] = json.loads(hook_scripts_json) if hook_scripts_json else {}
                    
                    return IntegrationTemplate(**template_dict)
        
        return None
    
    def contribute_template(
        self,
        ide_family: str,
        os_type: str,
        config_content: str,
        hook_scripts: Dict[str, str],
        notes: str = "",
        ide_version: Optional[str] = None,
        contributed_by: str = "anonymous"
    ) -> str:
        """
        Contribute a working template to the community.
        
        Automatically sanitizes paths and secrets before storing.
        
        Returns:
            template_id if successful
        """
        # Sanitize config (remove user-specific paths and secrets)
        sanitized_config = self._sanitize_config(config_content)
        sanitized_scripts = {
            name: self._sanitize_config(script)
            for name, script in hook_scripts.items()
        }
        
        # Generate template ID (hash of sanitized content)
        template_id = hashlib.sha256(
            f"{ide_family}:{os_type}:{sanitized_config}".encode()
        ).hexdigest()[:16]
        
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO integration_templates (
                        template_id, ide_family, os_type, ide_version,
                        config_format, config_content, hook_scripts_json,
                        contributed_by, contributed_at,
                        success_count, failure_count, confidence_score,
                        upvotes, downvotes, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    template_id, ide_family, os_type, ide_version,
                    "json", sanitized_config, json.dumps(sanitized_scripts),
                    contributed_by, datetime.now(timezone.utc).isoformat(),
                    1, 0, 1.0,  # Start with 1 success (contributor's)
                    1, 0, notes  # 1 upvote from contributor
                ))
                
                conn.commit()
            
            logger.info(f"Template contributed: {template_id}")
            return template_id
        
        except Exception as e:
            logger.error(f"Failed to contribute template: {e}")
            raise
    
    def _sanitize_config(self, content: str) -> str:
        """Remove user-specific paths and secrets."""
        sanitized = content
        
        # Replace home directory
        home = str(Path.home())
        sanitized = sanitized.replace(home, "${HOME}")
        
        # Replace username
        username = os.getenv("USER") or "user"
        sanitized = re.sub(rf'/Users/{username}', '${HOME}', sanitized)
        sanitized = re.sub(rf'/home/{username}', '${HOME}', sanitized)
        sanitized = re.sub(rf'C:\\Users\\{username}', '${HOME}', sanitized, flags=re.IGNORECASE)
        
        # Replace common secret patterns
        sanitized = re.sub(r'sk-[a-zA-Z0-9]{32,}', '${Context_DNA_OPENAI}', sanitized)
        sanitized = re.sub(r'ghp_[a-zA-Z0-9]{36}', '${GITHUB_TOKEN}', sanitized)
        
        return sanitized
    
    def record_feedback(
        self,
        template_id: str,
        success: bool,
        feedback_text: str = ""
    ) -> bool:
        """Record user feedback on a template."""
        try:
            feedback_id = f"fb_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            
            with sqlite3.connect(str(self.db_path)) as conn:
                # Record feedback
                conn.execute("""
                    INSERT INTO template_feedback (
                        feedback_id, template_id, user_id, success,
                        feedback_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    feedback_id, template_id, "anonymous",
                    success, feedback_text,
                    datetime.now(timezone.utc).isoformat()
                ))
                
                # Update template stats
                if success:
                    conn.execute("""
                        UPDATE integration_templates 
                        SET success_count = success_count + 1,
                            confidence_score = CAST(success_count + 1 AS REAL) / (success_count + failure_count + 1)
                        WHERE template_id = ?
                    """, (template_id,))
                else:
                    conn.execute("""
                        UPDATE integration_templates 
                        SET failure_count = failure_count + 1,
                            confidence_score = CAST(success_count AS REAL) / (success_count + failure_count + 1)
                        WHERE template_id = ?
                    """, (template_id,))
                
                conn.commit()
            
            return True
        
        except Exception as e:
            logger.error(f"Failed to record feedback: {e}")
            return False
    
    def sync_from_github(self, force: bool = False) -> int:
        """
        Pull community templates from GitHub repository.
        
        Returns:
            Number of templates imported
        """
        try:
            import requests
            
            resp = requests.get(GITHUB_TEMPLATES_URL, timeout=10)
            if not resp.ok:
                logger.warning(f"Could not fetch community templates: HTTP {resp.status_code}")
                return 0
            
            templates_data = resp.json()
            imported = 0
            
            for template_data in templates_data.get("templates", []):
                try:
                    # Import each template
                    template_id = self.contribute_template(
                        ide_family=template_data["ide_family"],
                        os_type=template_data["os_type"],
                        config_content=template_data["config_content"],
                        hook_scripts=template_data.get("hook_scripts", {}),
                        notes=template_data.get("notes", ""),
                        ide_version=template_data.get("ide_version"),
                        contributed_by=template_data.get("contributed_by", "community")
                    )
                    
                    # Mark as verified if from official repo
                    if template_data.get("is_verified"):
                        with sqlite3.connect(str(self.db_path)) as conn:
                            conn.execute("""
                                UPDATE integration_templates 
                                SET is_verified = 1,
                                    verified_by = 'contextdna_maintainers',
                                    verified_at = ?
                                WHERE template_id = ?
                            """, (datetime.now(timezone.utc).isoformat(), template_id))
                            conn.commit()
                    
                    imported += 1
                
                except Exception as e:
                    logger.warning(f"Failed to import template: {e}")
            
            logger.info(f"Imported {imported} community templates")
            return imported
        
        except Exception as e:
            logger.error(f"GitHub sync failed: {e}")
            return 0


def seed_initial_templates():
    """Seed database with known working templates."""
    templates = CommunityTemplates()
    
    # Claude Code (VS Code) - macOS
    claude_code_macos = {
        "hooks": {
            "UserPromptSubmit": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "${HOME}/dev/er-simulator-superrepo/scripts/auto-memory-query.sh"
                }]
            }],
            "PostToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "${HOME}/dev/er-simulator-superrepo/scripts/auto-capture-results.sh"
                }]
            }]
        }
    }
    
    templates.contribute_template(
        ide_family="vscode",
        os_type="macos",
        config_content=json.dumps(claude_code_macos, indent=2),
        hook_scripts={
            "UserPromptSubmit": "auto-memory-query.sh",
            "PostToolUse": "auto-capture-results.sh"
        },
        notes="Claude Code on macOS - verified working",
        contributed_by="contextdna_maintainers"
    )
    
    # Cursor - macOS
    cursor_macos = {
        "beforeSubmitPrompt": [{
            "command": "${HOME}/dev/er-simulator-superrepo/scripts/auto-memory-query-cursor.sh"
        }],
        "afterFileEdit": [{
            "command": "${HOME}/dev/er-simulator-superrepo/scripts/auto-capture-results-cursor.sh"
        }]
    }
    
    templates.contribute_template(
        ide_family="cursor",
        os_type="macos",
        config_content=json.dumps(cursor_macos, indent=2),
        hook_scripts={
            "beforeSubmitPrompt": "auto-memory-query-cursor.sh",
            "afterFileEdit": "auto-capture-results-cursor.sh"
        },
        notes="Cursor IDE on macOS - verified working",
        contributed_by="contextdna_maintainers"
    )
    
    # Mark as verified
    with sqlite3.connect(str(templates.db_path)) as conn:
        conn.execute("""
            UPDATE integration_templates 
            SET is_verified = 1,
                verified_by = 'contextdna_maintainers',
                verified_at = ?
        """, (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
    
    print("✅ Seeded 2 verified templates (Claude Code, Cursor)")


if __name__ == "__main__":
    print("Initializing Community Template Library...")
    seed_initial_templates()
    
    # Show available templates
    templates = CommunityTemplates()
    
    print("\n📚 Available Templates:")
    with sqlite3.connect(str(templates.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ide_family, os_type, confidence_score, upvotes, is_verified, notes
            FROM integration_templates
            ORDER BY is_verified DESC, confidence_score DESC
        """)
        
        for row in cursor.fetchall():
            verified = "✅ Verified" if row['is_verified'] else "👥 Community"
            print(f"\n{verified}: {row['ide_family']} ({row['os_type']})")
            print(f"  Confidence: {row['confidence_score']:.0%}")
            print(f"  Upvotes: {row['upvotes']}")
            print(f"  Notes: {row['notes']}")
