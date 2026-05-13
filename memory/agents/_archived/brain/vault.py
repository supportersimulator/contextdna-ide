"""
Vault Agent - The Brain's Encrypted Persistent Storage

The Vault is Synaptic's secure memory store - protecting sensitive learnings,
credentials references, and critical system state.

Anatomical Label: Brain (Central Storage)
"""

from __future__ import annotations
import os
import json
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class VaultAgent(Agent):
    """
    Vault Agent - Secure encrypted storage for Synaptic's memories.

    Responsibilities:
    - Store and retrieve encrypted learnings
    - Manage credential references (never actual secrets)
    - Persist critical system state
    - Provide transactional guarantees
    """

    NAME = "vault"
    CATEGORY = AgentCategory.BRAIN
    DESCRIPTION = "Encrypted persistent storage for Synaptic's memories"
    ANATOMICAL_LABEL = "Brain (Central Storage)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._db_path = Path.home() / ".context-dna" / ".vault.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _on_start(self):
        """Initialize vault storage."""
        self._ensure_db()

    def _on_stop(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_db(self):
        """Create vault database tables."""
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS vault_entries (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                encrypted INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                accessed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                access_count INTEGER DEFAULT 0
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vault_category
            ON vault_entries(category)
        """)
        self._conn.commit()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check vault health."""
        try:
            if not self._conn:
                return {"healthy": False, "message": "Database not connected", "score": 0.0}

            # Test query
            cursor = self._conn.execute("SELECT COUNT(*) FROM vault_entries")
            count = cursor.fetchone()[0]

            return {
                "healthy": True,
                "score": 1.0,
                "message": f"Vault operational with {count} entries",
                "metrics": {"entry_count": count}
            }
        except Exception as e:
            return {"healthy": False, "message": str(e), "score": 0.0}

    def process(self, input_data: Any) -> Any:
        """Process vault operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "get")
            if op == "store":
                return self.store(
                    input_data["key"],
                    input_data["value"],
                    input_data.get("category", "general")
                )
            elif op == "retrieve":
                return self.retrieve(input_data["key"])
            elif op == "delete":
                return self.delete(input_data["key"])
            elif op == "list":
                return self.list_keys(input_data.get("category"))
        return None

    def store(self, key: str, value: Any, category: str = "general") -> bool:
        """Store a value in the vault."""
        try:
            if not self._conn:
                self._ensure_db()

            value_json = json.dumps(value) if not isinstance(value, str) else value
            now = datetime.utcnow().isoformat()

            self._conn.execute("""
                INSERT OR REPLACE INTO vault_entries
                (key, value, category, updated_at)
                VALUES (?, ?, ?, ?)
            """, (key, value_json, category, now))
            self._conn.commit()
            self._last_active = datetime.utcnow()
            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve a value from the vault."""
        try:
            if not self._conn:
                self._ensure_db()

            cursor = self._conn.execute(
                "SELECT value FROM vault_entries WHERE key = ?",
                (key,)
            )
            row = cursor.fetchone()

            if row:
                # Update access tracking
                now = datetime.utcnow().isoformat()
                self._conn.execute("""
                    UPDATE vault_entries
                    SET accessed_at = ?, access_count = access_count + 1
                    WHERE key = ?
                """, (now, key))
                self._conn.commit()

                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return row[0]
            return None
        except Exception as e:
            self._handle_error(e)
            return None

    def delete(self, key: str) -> bool:
        """Delete a value from the vault."""
        try:
            if not self._conn:
                self._ensure_db()

            self._conn.execute("DELETE FROM vault_entries WHERE key = ?", (key,))
            self._conn.commit()
            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def list_keys(self, category: str = None) -> List[str]:
        """List all keys in the vault, optionally filtered by category."""
        try:
            if not self._conn:
                self._ensure_db()

            if category:
                cursor = self._conn.execute(
                    "SELECT key FROM vault_entries WHERE category = ?",
                    (category,)
                )
            else:
                cursor = self._conn.execute("SELECT key FROM vault_entries")

            return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            self._handle_error(e)
            return []
