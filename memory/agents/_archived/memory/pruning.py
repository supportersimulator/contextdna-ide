"""
Pruning Agent - Decay & Relevance Pruner

The Pruning agent implements forgetting - removing stale memories,
decaying unused associations, and keeping the memory system lean.

Anatomical Label: Synaptic Pruning (Decay & Relevance Pruner)
"""

from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class PruningAgent(Agent):
    """
    Pruning Agent - Memory decay and cleanup.

    Responsibilities:
    - Decay unused memories
    - Prune irrelevant entries
    - Maintain memory efficiency
    - Prevent memory bloat
    """

    NAME = "pruning"
    CATEGORY = AgentCategory.MEMORY
    DESCRIPTION = "Memory decay and relevance pruning"
    ANATOMICAL_LABEL = "Synaptic Pruning (Decay & Relevance Pruner)"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._decay_rate = config.get("decay_rate", 0.95) if config else 0.95
        self._prune_threshold = config.get("prune_threshold", 0.1) if config else 0.1
        self._max_age_days = config.get("max_age_days", 90) if config else 90

    def _on_start(self):
        """Initialize pruning agent."""
        pass

    def _on_stop(self):
        """Shutdown pruning agent."""
        pass

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check pruning health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Pruning active (decay: {self._decay_rate}, threshold: {self._prune_threshold})",
            "metrics": {
                "decay_rate": self._decay_rate,
                "prune_threshold": self._prune_threshold,
                "max_age_days": self._max_age_days
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process pruning operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "prune")
            if op == "prune":
                return self.run_pruning_cycle()
            elif op == "decay":
                return self.apply_decay()
            elif op == "cleanup_old":
                return self.cleanup_old_entries(input_data.get("days", 90))
        return self.run_pruning_cycle()

    def run_pruning_cycle(self) -> Dict[str, Any]:
        """Run a complete pruning cycle."""
        results = {
            "decayed": 0,
            "pruned": 0,
            "cleaned_old": 0
        }

        # Apply decay
        decay_result = self.apply_decay()
        results["decayed"] = decay_result.get("affected", 0)

        # Prune low-value entries
        prune_result = self.prune_low_value()
        results["pruned"] = prune_result.get("pruned", 0)

        # Cleanup old entries
        cleanup_result = self.cleanup_old_entries(self._max_age_days)
        results["cleaned_old"] = cleanup_result.get("cleaned", 0)

        self._last_active = datetime.utcnow()
        return results

    def apply_decay(self) -> Dict[str, Any]:
        """Apply decay to memory scores."""
        affected = 0

        # Decay neocortex learnings confidence
        try:
            db_path = Path.home() / ".context-dna" / ".neocortex.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cursor = conn.execute("""
                    UPDATE learnings
                    SET confidence = confidence * ?
                    WHERE confidence > ?
                """, (self._decay_rate, self._prune_threshold))
                affected += cursor.rowcount
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[WARN] Confidence decay on neocortex failed: {e}")

        # Decay boundary associations
        try:
            from memory.boundary_feedback import get_boundary_learner
            learner = get_boundary_learner()
            learner.decay_recency_weights(decay_factor=self._decay_rate)
        except Exception as e:
            print(f"[WARN] Boundary association decay failed: {e}")

        return {"affected": affected}

    def prune_low_value(self) -> Dict[str, Any]:
        """Prune entries below threshold."""
        pruned = 0

        try:
            db_path = Path.home() / ".context-dna" / ".neocortex.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))

                # Prune low-confidence learnings that haven't been used
                cursor = conn.execute("""
                    DELETE FROM learnings
                    WHERE confidence < ?
                    AND usage_count = 0
                    AND created_at < datetime('now', '-7 days')
                """, (self._prune_threshold,))
                pruned += cursor.rowcount

                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[WARN] Low-value pruning failed: {e}")

        return {"pruned": pruned}

    def cleanup_old_entries(self, days: int = 90) -> Dict[str, Any]:
        """Clean up entries older than specified days."""
        cleaned = 0
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        # Cleanup dialogue mirror
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            mirror = get_dialogue_mirror()
            mirror.cleanup_old(days=days)
        except Exception as e:
            print(f"[WARN] Dialogue mirror cleanup failed: {e}")

        # Cleanup hippocampus index
        try:
            db_path = Path.home() / ".context-dna" / ".hippocampus.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cursor = conn.execute("""
                    DELETE FROM context_index
                    WHERE accessed_at < ?
                    AND access_count < 2
                """, (cutoff,))
                cleaned += cursor.rowcount
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[WARN] Hippocampus index cleanup failed: {e}")

        # Cleanup vault old entries
        try:
            db_path = Path.home() / ".context-dna" / ".vault.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cursor = conn.execute("""
                    DELETE FROM vault_entries
                    WHERE accessed_at < ?
                    AND access_count < 2
                    AND category != 'permanent'
                """, (cutoff,))
                cleaned += cursor.rowcount
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[WARN] Vault cleanup failed: {e}")

        return {"cleaned": cleaned}

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get memory usage statistics."""
        stats = {
            "neocortex": {"learnings": 0, "patterns": 0, "sops": 0},
            "hippocampus": {"indexed": 0},
            "vault": {"entries": 0},
            "dialogue": {"messages": 0}
        }

        # Neocortex stats
        try:
            db_path = Path.home() / ".context-dna" / ".neocortex.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                stats["neocortex"]["learnings"] = conn.execute(
                    "SELECT COUNT(*) FROM learnings"
                ).fetchone()[0]
                stats["neocortex"]["patterns"] = conn.execute(
                    "SELECT COUNT(*) FROM patterns"
                ).fetchone()[0]
                stats["neocortex"]["sops"] = conn.execute(
                    "SELECT COUNT(*) FROM sops"
                ).fetchone()[0]
                conn.close()
        except Exception as e:
            print(f"[WARN] Neocortex stats query failed: {e}")

        # Hippocampus stats
        try:
            db_path = Path.home() / ".context-dna" / ".hippocampus.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                stats["hippocampus"]["indexed"] = conn.execute(
                    "SELECT COUNT(*) FROM context_index"
                ).fetchone()[0]
                conn.close()
        except Exception as e:
            print(f"[WARN] Hippocampus stats query failed: {e}")

        # Vault stats
        try:
            db_path = Path.home() / ".context-dna" / ".vault.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                stats["vault"]["entries"] = conn.execute(
                    "SELECT COUNT(*) FROM vault_entries"
                ).fetchone()[0]
                conn.close()
        except Exception as e:
            print(f"[WARN] Vault stats query failed: {e}")

        # Dialogue stats
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            mirror = get_dialogue_mirror()
            context = mirror.get_context_for_synaptic(max_messages=1)
            stats["dialogue"]["messages"] = context.get("message_count", 0)
        except Exception as e:
            print(f"[WARN] Dialogue stats query failed: {e}")

        return stats
