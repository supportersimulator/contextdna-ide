"""
Neocortex Agent - Consolidated Context Store

The Neocortex holds long-term memories - consolidated learnings,
patterns, and insights that have proven valuable over time.

Anatomical Label: Neocortex (Long-term Memory Store)
"""

from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class NeocortexAgent(Agent):
    """
    Neocortex Agent - Long-term memory storage.

    Responsibilities:
    - Store consolidated learnings
    - Maintain pattern library
    - Hold proven SOPs
    - Preserve valuable insights
    """

    NAME = "neocortex"
    CATEGORY = AgentCategory.MEMORY
    DESCRIPTION = "Long-term consolidated memory storage"
    ANATOMICAL_LABEL = "Neocortex (Long-term Memory Store)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._db_path = Path.home() / ".context-dna" / ".neocortex.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _on_start(self):
        """Initialize long-term storage."""
        self._ensure_db()
        # One-time migration of learnings to unified FTS5 store
        sync_flag = self._db_path.parent / ".neocortex_synced"
        if not sync_flag.exists():
            self._sync_to_main_store()
            try:
                sync_flag.touch()
            except Exception:
                pass

    def _on_stop(self):
        """Shutdown storage."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_db(self):
        """Create neocortex database."""
        self._conn = sqlite3.connect(str(self._db_path))

        # Learnings table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT,
                tags TEXT,
                confidence REAL DEFAULT 0.5,
                usage_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            )
        """)

        # Patterns table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                trigger_conditions TEXT,
                recommended_actions TEXT,
                success_rate REAL DEFAULT 0.5,
                occurrence_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # SOPs table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sops (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                sop_type TEXT,
                applicable_contexts TEXT,
                effectiveness_score REAL DEFAULT 0.5,
                usage_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._conn.commit()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check neocortex health."""
        try:
            if not self._conn:
                return {"healthy": False, "message": "Not connected", "score": 0.0}

            learnings = self._conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
            patterns = self._conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
            sops = self._conn.execute("SELECT COUNT(*) FROM sops").fetchone()[0]

            return {
                "healthy": True,
                "score": 1.0,
                "message": f"Memory intact: {learnings} learnings, {patterns} patterns, {sops} SOPs",
                "metrics": {
                    "learnings": learnings,
                    "patterns": patterns,
                    "sops": sops
                }
            }
        except Exception as e:
            return {"healthy": False, "message": str(e), "score": 0.0}

    def process(self, input_data: Any) -> Any:
        """Process memory operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "recall")
            if op == "store_learning":
                return self.store_learning(input_data)
            elif op == "store_pattern":
                return self.store_pattern(input_data)
            elif op == "store_sop":
                return self.store_sop(input_data)
            elif op == "recall":
                return self.recall(input_data.get("query"), input_data.get("type"))
        return None

    def store_learning(self, learning: Dict[str, Any]) -> str:
        """Store a learning in long-term memory."""
        try:
            if not self._conn:
                self._ensure_db()

            import hashlib
            learning_id = f"lrn_{hashlib.sha256(learning['title'].encode()).hexdigest()[:12]}"

            self._conn.execute("""
                INSERT OR REPLACE INTO learnings
                (id, title, content, category, tags, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                learning_id,
                learning['title'],
                learning['content'],
                learning.get('category', 'general'),
                ','.join(learning.get('tags', [])),
                learning.get('confidence', 0.5)
            ))
            self._conn.commit()
            self._last_active = datetime.utcnow()

            return learning_id
        except Exception as e:
            self._handle_error(e)
            return ""

    def store_pattern(self, pattern: Dict[str, Any]) -> str:
        """Store a pattern in long-term memory."""
        try:
            if not self._conn:
                self._ensure_db()

            import hashlib
            pattern_id = f"pat_{hashlib.sha256(pattern['name'].encode()).hexdigest()[:12]}"

            self._conn.execute("""
                INSERT OR REPLACE INTO patterns
                (id, name, description, trigger_conditions, recommended_actions, success_rate)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                pattern_id,
                pattern['name'],
                pattern.get('description', ''),
                json.dumps(pattern.get('trigger_conditions', [])),
                json.dumps(pattern.get('recommended_actions', [])),
                pattern.get('success_rate', 0.5)
            ))
            self._conn.commit()
            self._last_active = datetime.utcnow()

            return pattern_id
        except Exception as e:
            self._handle_error(e)
            return ""

    def store_sop(self, sop: Dict[str, Any]) -> str:
        """Store an SOP in long-term memory."""
        try:
            if not self._conn:
                self._ensure_db()

            import hashlib
            sop_id = f"sop_{hashlib.sha256(sop['title'].encode()).hexdigest()[:12]}"

            self._conn.execute("""
                INSERT OR REPLACE INTO sops
                (id, title, content, sop_type, applicable_contexts, effectiveness_score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                sop_id,
                sop['title'],
                sop['content'],
                sop.get('sop_type', 'general'),
                json.dumps(sop.get('applicable_contexts', [])),
                sop.get('effectiveness_score', 0.5)
            ))
            self._conn.commit()
            self._last_active = datetime.utcnow()

            return sop_id
        except Exception as e:
            self._handle_error(e)
            return ""

    def recall(self, query: str, memory_type: str = None) -> List[Dict[str, Any]]:
        """Recall memories — delegates learnings search to unified FTS5 + semantic system."""
        results = []

        # Learnings: use unified search (FTS5 + semantic rescue)
        if memory_type in (None, "learnings", "learning"):
            try:
                from memory.sqlite_storage import get_sqlite_storage
                store = get_sqlite_storage()
                unified_results = store.query(query, limit=10)
                for r in unified_results:
                    results.append({
                        "type": "learning",
                        "id": r.get("id", ""),
                        "title": r.get("title", ""),
                        "content": r.get("content", ""),
                        "source": "unified_fts5"
                    })
            except Exception:
                # Fallback to legacy LIKE search on neocortex DB
                results.extend(self._recall_legacy_learnings(query))

        # Patterns: keep using neocortex DB (different schema)
        if memory_type in (None, "patterns", "pattern"):
            results.extend(self._recall_patterns(query))

        # SOPs: keep using neocortex DB (different schema)
        if memory_type in (None, "sops", "sop"):
            results.extend(self._recall_sops(query))

        return results

    # ------------------------------------------------------------------
    # Legacy / neocortex-DB helpers
    # ------------------------------------------------------------------

    def _recall_legacy(self, query: str, memory_type: str = None) -> List[Dict[str, Any]]:
        """Legacy recall using LIKE queries against the neocortex DB."""
        results = []
        if memory_type in (None, "learnings", "learning"):
            results.extend(self._recall_legacy_learnings(query))
        if memory_type in (None, "patterns", "pattern"):
            results.extend(self._recall_patterns(query))
        if memory_type in (None, "sops", "sop"):
            results.extend(self._recall_sops(query))
        return results

    def _recall_legacy_learnings(self, query: str) -> List[Dict[str, Any]]:
        """Recall learnings from neocortex DB using LIKE queries (fallback)."""
        try:
            if not self._conn:
                self._ensure_db()
            cursor = self._conn.execute("""
                SELECT id, title, content, category, confidence
                FROM learnings
                WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
                ORDER BY confidence DESC, usage_count DESC
                LIMIT 10
            """, (f"%{query}%", f"%{query}%", f"%{query}%"))
            return [{
                "type": "learning",
                "id": row[0],
                "title": row[1],
                "content": row[2],
                "category": row[3],
                "confidence": row[4]
            } for row in cursor.fetchall()]
        except Exception as e:
            self._handle_error(e)
            return []

    def _recall_patterns(self, query: str) -> List[Dict[str, Any]]:
        """Recall patterns from neocortex DB using LIKE queries."""
        try:
            if not self._conn:
                self._ensure_db()
            cursor = self._conn.execute("""
                SELECT id, name, description, success_rate
                FROM patterns
                WHERE name LIKE ? OR description LIKE ?
                ORDER BY success_rate DESC
                LIMIT 10
            """, (f"%{query}%", f"%{query}%"))
            return [{
                "type": "pattern",
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "success_rate": row[3]
            } for row in cursor.fetchall()]
        except Exception as e:
            self._handle_error(e)
            return []

    def _recall_sops(self, query: str) -> List[Dict[str, Any]]:
        """Recall SOPs from neocortex DB using LIKE queries."""
        try:
            if not self._conn:
                self._ensure_db()
            cursor = self._conn.execute("""
                SELECT id, title, content, sop_type, effectiveness_score
                FROM sops
                WHERE title LIKE ? OR content LIKE ?
                ORDER BY effectiveness_score DESC
                LIMIT 10
            """, (f"%{query}%", f"%{query}%"))
            return [{
                "type": "sop",
                "id": row[0],
                "title": row[1],
                "content": row[2],
                "sop_type": row[3],
                "effectiveness": row[4]
            } for row in cursor.fetchall()]
        except Exception as e:
            self._handle_error(e)
            return []

    def _sync_to_main_store(self):
        """One-time migration: copy neocortex learnings to main SQLite storage."""
        try:
            if not self._conn:
                return
            from memory.sqlite_storage import get_sqlite_storage
            store = get_sqlite_storage()
            cursor = self._conn.execute(
                "SELECT id, title, content, category, tags FROM learnings"
            )
            synced = 0
            for row in cursor.fetchall():
                try:
                    store.store_learning({
                        "id": f"neo_{row[0]}",
                        "title": row[1],
                        "content": row[2],
                        "type": "learning",
                        "tags": [t.strip() for t in (row[4] or "").split(",") if t.strip()],
                        "source": "neocortex_migration"
                    }, skip_dedup=False)
                    synced += 1
                except Exception:
                    pass
            if synced > 0:
                import logging
                logging.getLogger(__name__).info(
                    f"Synced {synced} learnings from neocortex to main store"
                )
        except Exception:
            pass

    def mark_used(self, memory_id: str, was_successful: bool = True):
        """Mark a memory as used and record outcome."""
        try:
            if not self._conn:
                self._ensure_db()

            now = datetime.utcnow().isoformat()

            # Determine table from ID prefix
            if memory_id.startswith("lrn_"):
                table = "learnings"
            elif memory_id.startswith("pat_"):
                table = "patterns"
            elif memory_id.startswith("sop_"):
                table = "sops"
            else:
                return

            self._conn.execute(f"""
                UPDATE {table}
                SET usage_count = usage_count + 1,
                    last_used_at = ?
                WHERE id = ?
            """, (now, memory_id))

            if was_successful and table == "learnings":
                self._conn.execute("""
                    UPDATE learnings
                    SET success_count = success_count + 1
                    WHERE id = ?
                """, (memory_id,))

            self._conn.commit()
        except Exception as e:
            self._handle_error(e)
