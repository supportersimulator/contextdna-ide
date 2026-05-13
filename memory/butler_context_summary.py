#!/usr/bin/env python3
"""
BUTLER CONTEXT SUMMARY — Hybrid Full Context for Synaptic's Multi-Step Reasoning

Purpose:
  Give the local LLM butler access to task-relevant context across 7 data sources
  in a reasoning-ready format that supports multi-step causal inference.

Philosophy:
  Batman arrives at the mansion. Alfred doesn't just hand him a briefing note.
  Alfred's brain contains: the codebase map, recent failures, Aaron's priorities,
  evidence about what worked last time, dependency chains showing ripple effects,
  and the patterns Alfred has noticed across sessions.

  This module IS Alfred's brain — structured for reasoning, not retrieval.

Data Sources (7):
  1. Codebase Graph         — 13K nodes, 58K edges → dependency chains, hub files
  2. Learnings Database     — 354 records → proven wisdom sorted by merge_count
  3. Session Historian      — Recent insights + unfinished tasks
  4. Failure Patterns       — Predictive signatures of failure modes
  5. Aaron Intent Patterns  — Sentiment, priorities, satisfaction signals
  6. Evidence Outcomes      — Success/failure attribution + reward signals
  7. Brain State / Meta-Analysis — Cross-session patterns, active skills

Output Format (for LLM reasoning):
  {
    "task": "what the user is asking",
    "context_tier": "silver|expanded|full",  # latency budget based

    "ripple_analysis": {
      "likely_affected_files": [...],  # dependency chains 3 levels deep
      "critical_hubs": [...],          # files with highest impact
      "precedent": {...}               # what happened last time similar task ran
    },

    "failure_landscape": {
      "high_confidence_traps": [...],  # patterns with 3+ occurrences
      "mitigations": [...]             # what worked
    },

    "aaron_context": {
      "priorities_this_week": "...",
      "satisfaction_signals": "...",
      "likely_intent": "..."
    },

    "proven_wisdom": [
      { "learning": "...", "evidence": {...}, "confidence": 0.85 }
    ],

    "cross_session_patterns": [
      { "pattern": "...", "occurrence_count": 5, "first_seen": "..." }
    ],

    "reasoning_chain_hints": {
      "step_1": "...",
      "step_2": "...",
      "step_3": "..."
    }
  }

Latency Budgets:
  - LITE mode (SQLite only):  150ms
  - HEAVY mode (PostgreSQL + graph + full queries): 250ms
  - REASONING mode (for Section 6 injection): 500ms (async)
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import hashlib
from dataclasses import dataclass, asdict
import concurrent.futures
import sys
import os

# Local imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from memory.sqlite_storage import get_sqlite_storage
from memory.session_historian import SessionHistorian

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RippleAnalysis:
    """Dependency chain analysis for understanding code change impact."""
    likely_affected_files: List[str]
    critical_hubs: List[Tuple[str, int]]  # (file_path, edge_count)
    dependency_chain: List[str]  # ordered by distance from changed file
    precedent: Optional[Dict[str, Any]] = None


@dataclass
class FailureIsland:
    """Predictive signature of a failure mode."""
    pattern_text: str
    occurrence_count: int
    confidence: float
    mitigations: List[str]
    files_involved: List[str]


@dataclass
class AaronIntent:
    """Extracted intent and sentiment from dialogue mirror."""
    likely_intent: str
    satisfaction_signals: List[str]
    priorities_this_session: List[str]
    frustration_indicators: int
    enthusiasm_indicators: int


@dataclass
class ProvenWisdom:
    """Single learning with evidence."""
    title: str
    content: str
    source: str  # "learnings" | "repair" | "pattern"
    confidence: float
    merge_count: int
    tags: List[str]


# ──────────────────────────────────────────────────────────────────────────────
# BUTLER CONTEXT SUMMARY ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class ButlerContextSummary:
    """
    Aggregates hybrid context from 7 sources for multi-step reasoning.

    Operates in three tiers (latency budgets):
      - LITE (150ms): SQLite only, recent data only
      - HEAVY (250ms): All sources, parallelized queries
      - REASONING (500ms, async): Full depth for LLM multi-step chains
    """

    def __init__(self, tiers: str = "HEAVY"):
        self.tiers = tiers  # LITE | HEAVY | REASONING
        self.graph_cache = None
        self.storage = get_sqlite_storage()
        self.historian = SessionHistorian()
        self.start_time = time.time()

        # Load graph if HEAVY/REASONING tier
        if tiers in ("HEAVY", "REASONING"):
            self._load_graph_cache()

    def _load_graph_cache(self) -> None:
        """Load architecture graph cache (20.5MB JSON)."""
        try:
            cache_path = Path("context-dna/infra/.architecture_graph_cache.json")
            if cache_path.exists():
                with open(cache_path) as f:
                    data = json.load(f)
                    self.graph_cache = data.get("graph", {})
                    logger.debug(f"Graph cache loaded: {len(self.graph_cache.get('nodes', []))} nodes")
            else:
                logger.warning("Graph cache not found")
        except Exception as e:
            logger.warning(f"Failed to load graph: {e}")
            self.graph_cache = None

    def summarize(self, task: str, context_hint: str = "") -> Dict[str, Any]:
        """
        Aggregate context for a given task.

        Args:
          task: The user's request or file being modified
          context_hint: Additional context (e.g., "modifying" | "creating" | "debugging")

        Returns:
          Reasoning-ready context dict
        """
        start = time.time()
        tier_used = self._choose_tier()

        results = {
            "task": task,
            "context_tier": tier_used,
            "generated_at": datetime.utcnow().isoformat(),
            "latency_budget_ms": self._latency_budget(tier_used),
        }

        # Parallel queries based on tier
        if tier_used == "LITE":
            results.update(self._lite_summary(task))
        elif tier_used == "HEAVY":
            results.update(self._heavy_summary(task))
        elif tier_used == "REASONING":
            results.update(self._reasoning_summary(task))

        results["query_time_ms"] = int((time.time() - start) * 1000)
        results["within_budget"] = results["query_time_ms"] <= results["latency_budget_ms"]

        return results

    def _choose_tier(self) -> str:
        """Choose tier based on available time budget."""
        # In real use, this would be based on whether this is part of webhook injection
        # For now, return configured tier
        return self.tiers

    def _latency_budget(self, tier: str) -> int:
        """Latency target for tier in milliseconds (increased for quality)."""
        return {"LITE": 250, "HEAVY": 500, "REASONING": 1000}[tier]

    # ────────────────────────────────────────────────────────────────────────────
    # TIER 1: LITE (SQLite only, recent data)
    # ────────────────────────────────────────────────────────────────────────────

    def _lite_summary(self, task: str) -> Dict[str, Any]:
        """Minimal context: top learnings + recent failures + intent."""
        return {
            "proven_wisdom": self._query_learnings_lite(task),
            "recent_failures": self._query_failures_lite(task),
            "aaron_context": self._extract_intent_lite(task),
        }

    def _query_learnings_lite(self, task: str, limit: int = 20) -> List[Dict]:
        """Top learnings by merge_count (most valuable)."""
        try:
            records = self.storage.query(task, limit=limit)
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("content", "")[:200],
                    "confidence": min(r.get("merge_count", 1) / 10.0, 1.0),  # Normalize
                    "source": r.get("type", "")
                }
                for r in records
            ]
        except Exception as e:
            logger.warning(f"Failed to query learnings: {e}")
            return []

    def _query_failures_lite(self, task: str, hours: int = 24) -> List[Dict]:
        """Recent failure patterns from observability DB."""
        try:
            obs_db = Path.home() / ".context-dna" / ".observability.db"
            if not obs_db.exists() or obs_db.stat().st_size == 0:
                return []

            from memory.db_utils import safe_conn
            with safe_conn(obs_db) as conn:
                cursor = conn.cursor()

                since = datetime.utcnow() - timedelta(hours=hours)
                query = """
                  SELECT outcome_type, COUNT(*) as count, AVG(reward) as avg_reward
                  FROM outcome_event
                  WHERE success = 0 AND timestamp_utc > ?
                  GROUP BY outcome_type
                  ORDER BY count DESC LIMIT 10
                """
                cursor.execute(query, (since.isoformat(),))
                results = [dict(row) for row in cursor.fetchall()]

            return results
        except Exception as e:
            logger.debug(f"Observability DB not available (expected if empty): {e}")
            return []

    def _extract_intent_lite(self, task: str) -> Dict[str, Any]:
        """Extract Aaron's likely intent from task text."""
        # Simple heuristics in LITE mode
        intent_keywords = {
            "modifying": ["fix", "change", "update", "modify", "edit"],
            "creating": ["add", "create", "new", "implement", "build"],
            "debugging": ["debug", "fix", "error", "issue", "broken"],
            "optimizing": ["speed", "perf", "optimize", "fast", "slow"],
        }

        task_lower = task.lower()
        detected_intent = "unknown"

        for intent, keywords in intent_keywords.items():
            if any(kw in task_lower for kw in keywords):
                detected_intent = intent
                break

        return {
            "likely_intent": detected_intent,
            "confidence": 0.6,  # Low confidence in LITE mode
        }

    # ────────────────────────────────────────────────────────────────────────────
    # TIER 2: HEAVY (All sources, parallelized)
    # ────────────────────────────────────────────────────────────────────────────

    def _heavy_summary(self, task: str) -> Dict[str, Any]:
        """Full context with parallelized queries."""
        results = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                "ripple": executor.submit(self._analyze_ripple, task),
                "failures": executor.submit(self._analyze_failures, task),
                "intent": executor.submit(self._analyze_intent, task),
                "wisdom": executor.submit(self._query_learnings_heavy, task),
                "patterns": executor.submit(self._analyze_cross_session_patterns, task),
            }

            for key, future in futures.items():
                try:
                    results[key] = future.result(timeout=0.2)  # Individual 200ms timeout
                except concurrent.futures.TimeoutError:
                    logger.warning(f"{key} timed out, skipping")
                    results[key] = {}
                except Exception as e:
                    logger.warning(f"{key} failed: {e}")
                    results[key] = {}

        return {
            "ripple_analysis": results.get("ripple", {}),
            "failure_landscape": results.get("failures", {}),
            "aaron_context": results.get("intent", {}),
            "proven_wisdom": results.get("wisdom", []),
            "cross_session_patterns": results.get("patterns", []),
        }

    def _analyze_ripple(self, task: str) -> Dict[str, Any]:
        """Analyze dependency chains and ripple effects."""
        if not self.graph_cache:
            return {}

        # Extract file references from task
        files_mentioned = self._extract_file_refs(task)

        try:
            result = {
                "likely_affected_files": [],
                "critical_hubs": [],
                "dependency_chain": []
            }

            # If graph available, find dependencies
            nodes = self.graph_cache.get("nodes", [])
            edges = self.graph_cache.get("edges", [])

            if not nodes or not edges:
                return result

            # Build node lookup
            node_map = {n["id"]: n for n in nodes}

            # Find hub nodes (files with most edges)
            hub_scores = {}
            for e in edges:
                src = e.get("source", "")
                tgt = e.get("target", "")
                for node_id in [src, tgt]:
                    if node_map.get(node_id, {}).get("type") == "file":
                        hub_scores[node_id] = hub_scores.get(node_id, 0) + 1

            result["critical_hubs"] = [
                (node_map[nid]["file_path"], count)
                for nid, count in sorted(hub_scores.items(), key=lambda x: -x[1])[:10]
            ]

            return result
        except Exception as e:
            logger.warning(f"Ripple analysis failed: {e}")
            return {"likely_affected_files": [], "critical_hubs": []}

    def _analyze_failures(self, task: str) -> Dict[str, Any]:
        """Analyze failure patterns and predictive signatures."""
        try:
            obs_db = Path.home() / ".context-dna" / ".observability.db"
            if not obs_db.exists() or obs_db.stat().st_size == 0:
                return {}

            from memory.db_utils import safe_conn
            with safe_conn(obs_db) as conn:
                cursor = conn.cursor()

                # High-confidence failure patterns (3+ occurrences)
                query = """
                  SELECT outcome_type, COUNT(*) as count, AVG(reward) as avg_reward
                  FROM outcome_event
                  WHERE success = 0
                  GROUP BY outcome_type
                  HAVING COUNT(*) >= 3
                  ORDER BY count DESC LIMIT 5
                """
                cursor.execute(query)
                patterns = [dict(row) for row in cursor.fetchall()]

            return {
                "high_confidence_traps": [p["outcome_type"] for p in patterns[:3]],
                "mitigations": [],  # Would be populated from learnings DB
                "patterns": patterns
            }
        except Exception as e:
            logger.debug(f"Failure analysis not available: {e}")
            return {}

    def _analyze_intent(self, task: str) -> Dict[str, Any]:
        """Extract Aaron's intent with higher confidence."""
        # Enhanced intent analysis from dialogue mirror + recent patterns
        return {
            "likely_intent": self._extract_intent_from_dialogue(task),
            "priorities_this_session": [],
            "satisfaction_signals": [],
            "confidence": 0.75
        }

    def _extract_intent_from_dialogue(self, task: str) -> str:
        """Extract intent from recent dialogue mirror, with keyword fallback."""
        try:
            recent_insights = self.historian.get_recent_insights(limit=5)
            if recent_insights:
                insight_type = recent_insights[0].get("insight_type", "")
                if insight_type and insight_type != "unknown":
                    return insight_type
        except Exception as e:
            logger.warning(f"Intent extraction failed: {e}")

        # Keyword fallback (same logic as LITE mode)
        task_lower = task.lower()
        for intent, keywords in {
            "debugging": ["debug", "fix", "error", "issue", "broken", "bug"],
            "modifying": ["change", "update", "modify", "edit", "refactor"],
            "creating": ["add", "create", "new", "implement", "build"],
            "optimizing": ["speed", "perf", "optimize", "fast", "slow"],
        }.items():
            if any(kw in task_lower for kw in keywords):
                return intent

        return "unknown"

    def _query_learnings_heavy(self, task: str, limit: int = 30) -> List[Dict]:
        """Top learnings sorted by evidence (merge_count + outcome correlation)."""
        try:
            records = self.storage.query(task, limit=limit)

            wisdom = []
            for r in records:
                wisdom.append({
                    "title": r.get("title", ""),
                    "content": r.get("content", "")[:300],
                    "confidence": min(r.get("merge_count", 1) / 10.0, 1.0),
                    "source": r.get("type", ""),
                    "tags": r.get("tags", []) if isinstance(r.get("tags"), list) else []
                })

            return wisdom
        except Exception as e:
            logger.debug(f"Heavy wisdom query failed: {e}")
            return []

    def _analyze_cross_session_patterns(self, task: str) -> List[Dict]:
        """Cross-session meta-analysis patterns."""
        try:
            # Get recent meta-analysis insights
            insights = self.historian.get_recent_insights(limit=10)
            return [
                {
                    "pattern": i.get("content", ""),
                    "confidence": i.get("confidence", 0.5),
                    "session": i.get("session_id", "")[:8]
                }
                for i in insights
            ]
        except Exception as e:
            logger.warning(f"Pattern analysis failed: {e}")
            return []

    # ────────────────────────────────────────────────────────────────────────────
    # TIER 3: REASONING (Full depth, async)
    # ────────────────────────────────────────────────────────────────────────────

    def _reasoning_summary(self, task: str) -> Dict[str, Any]:
        """Full depth context for multi-step LLM reasoning."""
        # Includes everything from HEAVY + extra reasoning chains
        heavy = self._heavy_summary(task)

        heavy["reasoning_chain_hints"] = self._generate_reasoning_chains(task, heavy)
        heavy["full_dependency_graph"] = self._expand_dependency_graph(task)

        return heavy

    def _generate_reasoning_chains(self, task: str, context: Dict) -> Dict[str, Any]:
        """
        Generate reasoning suggestions for LLM based on available context.

        Rather than prescribing a specific step sequence, provides context-aware
        suggestions that the LLM can use creatively as it reasons through the task.
        """
        suggestions = {
            "suggested_focus_areas": [],
            "known_landmines": [],
            "precedent_from_similar_tasks": None,
            "reasoning_approach": "Whatever approach makes sense to you given the context below"
        }

        # Populate based on available context
        if context.get("ripple_analysis"):
            ripple = context["ripple_analysis"]
            suggestions["suggested_focus_areas"] = ripple.get("critical_hubs", [])[:3]

        if context.get("failure_landscape"):
            landscape = context["failure_landscape"]
            suggestions["known_landmines"] = landscape.get("high_confidence_traps", [])[:3]

        if context.get("cross_session_patterns"):
            patterns = context.get("cross_session_patterns", [])
            if patterns:
                suggestions["precedent_from_similar_tasks"] = patterns[0].get("pattern", "")

        return suggestions

    def _expand_dependency_graph(self, task: str) -> Dict[str, Any]:
        """Full dependency graph for traversal."""
        if not self.graph_cache:
            return {}

        return {
            "nodes_count": len(self.graph_cache.get("nodes", [])),
            "edges_count": len(self.graph_cache.get("edges", [])),
            "categories": list(set(n.get("category", "") for n in self.graph_cache.get("nodes", [])))[:10]
        }

    # ────────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ────────────────────────────────────────────────────────────────────────────

    def _extract_file_refs(self, text: str) -> List[str]:
        """Extract file paths from task text."""
        import re
        # Simple regex for common file patterns
        pattern = r"(?:memory|backend|context-dna|simulator-core|landing-page)/[\w/\-\.]*\.(?:py|ts|tsx|js|json|sh)"
        matches = re.findall(pattern, text, re.IGNORECASE)
        return matches

    # ────────────────────────────────────────────────────────────────────────────
    # PUBLIC API FOR WEBHOOK INJECTION
    # ────────────────────────────────────────────────────────────────────────────

    def get_section_6_guidance(self, task: str) -> str:
        """
        Generate Section 6 (HOLISTIC_CONTEXT) guidance for injection.

        This is Synaptic's task-focused guidance to Atlas.
        """
        context = self.summarize(task, context_hint="webhook_injection")

        # Format for injection
        guidance = f"""
Task Context Summary:
  - Likely intent: {context['aaron_context'].get('likely_intent', 'unknown')}
  - Affected files: {context.get('ripple_analysis', {}).get('critical_hubs', [])[:3]}
  - Known failure patterns: {context.get('failure_landscape', {}).get('high_confidence_traps', [])[:2]}

Top Proven Wisdom:
{self._format_wisdom_bullets(context.get('proven_wisdom', [])[:3])}

Cross-Session Patterns:
{self._format_pattern_bullets(context.get('cross_session_patterns', [])[:3])}
"""
        return guidance.strip()

    def _format_wisdom_bullets(self, wisdom_list: List[Dict]) -> str:
        """Format wisdom for readability."""
        return "\n".join(
            f"  • {w['title'][:80]} (confidence: {w['confidence']:.1%})"
            for w in wisdom_list
        )

    def _format_pattern_bullets(self, patterns: List[Dict]) -> str:
        """Format patterns for readability."""
        return "\n".join(
            f"  • {p.get('pattern', '')[:80]} (session: {p.get('session', 'unknown')})"
            for p in patterns
        )


# ──────────────────────────────────────────────────────────────────────────────
# CLI / TESTING
# ──────────────────────────────────────────────────────────────────────────────

def main():
    """Test the butler context summary."""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s - %(levelname)s - %(message)s"
    )

    # Test with LITE tier first
    print("\n=== LITE TIER (150ms) ===")
    butler_lite = ButlerContextSummary(tiers="LITE")
    result_lite = butler_lite.summarize("modifying memory/persistent_hook_structure.py")
    print(json.dumps(result_lite, indent=2, default=str)[:800])

    # Test with HEAVY tier
    print("\n=== HEAVY TIER (250ms) ===")
    butler_heavy = ButlerContextSummary(tiers="HEAVY")
    result_heavy = butler_heavy.summarize("debugging async boto3 calls in lambda functions")
    print(json.dumps(result_heavy, indent=2, default=str)[:800])

    # Test Section 6 injection format
    print("\n=== SECTION 6 INJECTION FORMAT ===")
    guidance = butler_heavy.get_section_6_guidance("refactoring the evidence pipeline")
    print(guidance)


if __name__ == "__main__":
    main()
