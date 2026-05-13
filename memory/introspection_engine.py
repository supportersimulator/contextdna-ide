#!/usr/bin/env python3
"""
INTROSPECTION ENGINE — P4: Self-Directed Learning with LLM Reasoning

Philosophy:
  EMERGENCE OVER PRESCRIPTION
  - Don't engineer "correct" patterns
  - Give LLM the graph, let it reason creatively
  - Evidence pipeline tracks which reasoning works
  - System evolves toward most effective patterns

Purpose:
  Butler identifies knowledge gaps autonomously:
  1. AUDIT: Compare learnings coverage vs. codebase domains
  2. IDENTIFY: Find gaps (domains with files but minimal learnings)
  3. PRIORITIZE: Rank by user query frequency × file count × recency
  4. REASON: Send to LLM with creative freedom
  5. RECORD: Outcomes feed evidence pipeline

The LLM uses REASONING MODE to:
  - Decompose complex dependencies semantically
  - Weigh importance (critical vs. cosmetic) creatively
  - Explore uncertainty
  - Organize graph data in novel ways
  - Suggest study approaches (not just findings)

Evidence Feedback Loop:
  When LLM suggests: "Study X because Y"
  We track outcomes:
    - Did the suggestion prevent a bug? +confidence
    - Did it help with a similar task? +confidence
    - Was it irrelevant? -confidence
  Over time: System learns which reasoning patterns work best
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


class KnowledgeCoverageAuditor:
    """Audit what the butler knows vs. what exists in codebase."""

    def __init__(self):
        self.storage_dir = Path.home() / ".context-dna"
        self.storage_dir.mkdir(exist_ok=True)

    def audit_coverage(self) -> Dict[str, Any]:
        """
        Audit knowledge coverage across codebase domains.

        Returns:
          {
            'domains': {
              'memory': {'files': 47, 'learnings': 12, 'coverage': 0.25, 'priority': 'HIGH'},
              'admin': {'files': 89, 'learnings': 5, 'coverage': 0.06, 'priority': 'CRITICAL'},
              ...
            },
            'total_files': 1247,
            'total_learnings': 156,
            'gaps': [
              {'domain': 'docker', 'gap_score': 0.94, 'files': 23, 'last_activity': '2026-01-15'},
              ...
            ]
          }
        """
        coverage = {
            "domains": {},
            "total_files": 0,
            "total_learnings": 0,
            "gaps": [],
            "audit_time": datetime.now().isoformat(),
        }

        # Get codebase structure from graph cache
        graph_cache = self._load_graph_cache()
        if not graph_cache:
            logger.warning("Graph cache unavailable - coverage audit incomplete")
            return coverage

        # Count files by domain
        domain_files = {}
        for node in graph_cache.get("nodes", []):
            if node.get("type") == "file":
                path = node.get("file_path", "")
                domain = self._extract_domain(path)
                domain_files[domain] = domain_files.get(domain, 0) + 1

        # Count learnings by domain (from Acontext or quarantine)
        domain_learnings = self._count_learnings_by_domain()

        # Calculate coverage ratios
        for domain, file_count in sorted(domain_files.items(), key=lambda x: -x[1]):
            learning_count = domain_learnings.get(domain, 0)
            coverage_ratio = learning_count / max(file_count, 1)

            # Priority: LOW coverage + HIGH file count = CRITICAL gap
            priority = self._calculate_priority(coverage_ratio, file_count)

            coverage["domains"][domain] = {
                "files": file_count,
                "learnings": learning_count,
                "coverage": coverage_ratio,
                "priority": priority,
                "files_per_learning": file_count / max(learning_count, 1),
            }

            coverage["total_files"] += file_count

        coverage["total_learnings"] = sum(domain_learnings.values())

        # Identify gaps (sorted by severity)
        gaps = [
            {
                "domain": domain,
                "gap_score": 1.0 - data["coverage"],
                "files": data["files"],
                "learnings": data["learnings"],
                "priority": data["priority"],
            }
            for domain, data in coverage["domains"].items()
            if data["coverage"] < 0.6 or (data["learnings"] == 0 and data["files"] > 5)
        ]

        coverage["gaps"] = sorted(gaps, key=lambda x: -x["gap_score"])[:10]  # Top 10

        return coverage

    def _extract_domain(self, file_path: str) -> str:
        """Extract domain from file path.

        Examples:
          memory/scheduler.py → memory
          admin.contextdna.io/components/dashboard/views → admin
          backend/services/auth → backend
        """
        if not file_path:
            return "other"

        parts = file_path.split("/")
        first = parts[0].lower()

        # Recognize known domains
        known = {
            "memory": "memory",
            "admin": "admin",
            "backend": "backend",
            "infra": "infra",
            "context-dna": "context-dna",
            "scripts": "scripts",
            "simulator": "simulator",
            "ersim": "simulator",
        }

        for key, domain in known.items():
            if key in first:
                return domain

        return first if first and first != "." else "other"

    # Domain keywords for FTS5 search — maps domain to search terms
    DOMAIN_SEARCH_KEYWORDS = {
        "memory": "memory scheduler butler LLM webhook evidence pipeline",
        "admin": "admin dashboard panel component frontend",
        "backend": "backend server API endpoint service",
        "infra": "infra docker container deploy AWS terraform",
        "context-dna": "context DNA injection SOP learning",
        "scripts": "script bash shell automation",
        "simulator": "simulator monitor waveform ECG vitals patient",
        "other": "config test docs README",
    }

    def _count_learnings_by_domain(self) -> Dict[str, int]:
        """Count learnings by domain using FTS5 search.

        Uses FTS5 keyword search per domain instead of iterating all learnings
        (the old get_all_learnings() method doesn't exist on AcontextMemory).
        FTS5 is fast (BM25 ranking) and already indexed in learnings.db.
        """
        counts = {}

        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()

            for domain, keywords in self.DOMAIN_SEARCH_KEYWORDS.items():
                try:
                    results = storage.query(keywords, limit=100, _skip_hybrid=True)
                    counts[domain] = len(results)
                except Exception:
                    counts[domain] = 0
        except Exception as e:
            logger.debug(f"FTS5 domain count failed: {e}")

        return counts

    def _load_graph_cache(self) -> Optional[Dict[str, Any]]:
        """Load architecture graph cache."""
        try:
            cache_path = (
                Path.home()
                / "dev/er-simulator-superrepo/context-dna/infra/.architecture_graph_cache.json"
            )
            if cache_path.exists():
                with open(cache_path) as f:
                    data = json.load(f)
                    return data.get("graph")
        except Exception as e:
            logger.error(f"Failed to load graph cache: {e}")

        return None

    def _calculate_priority(self, coverage: float, file_count: int) -> str:
        """Calculate priority for gap filling."""
        if coverage < 0.1 and file_count > 50:
            return "CRITICAL"
        elif coverage < 0.2 or file_count > 30:
            return "HIGH"
        elif coverage < 0.4 or file_count > 10:
            return "MEDIUM"
        else:
            return "LOW"


class IntrospectionEngine:
    """Self-directed learning engine with LLM reasoning capacity.

    The butler identifies gaps and delegates to the LLM for creative reasoning:
    - LLM gets: Domain context, graph structure, study focus
    - LLM uses: Thinking mode for deep analysis
    - Result: Learnings + reasoning chains recorded for evidence feedback
    """

    def __init__(self):
        self.auditor = KnowledgeCoverageAuditor()
        self.storage_dir = Path.home() / ".context-dna"
        self.storage_dir.mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize introspection database."""
        db_path = self.storage_dir / ".introspection.db"
        if not db_path.exists():
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_coverage (
                    domain TEXT PRIMARY KEY,
                    file_count INTEGER,
                    learning_count INTEGER,
                    coverage_ratio REAL,
                    priority TEXT,
                    last_audited TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS study_plans (
                    id TEXT PRIMARY KEY,
                    domain TEXT,
                    plan_text TEXT,
                    priority TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    llm_reasoning TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS introspection_outcomes (
                    id TEXT PRIMARY KEY,
                    study_plan_id TEXT,
                    outcome_type TEXT,
                    value REAL,
                    feedback TEXT,
                    timestamp TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()

    def run_introspection_cycle(self, max_duration_seconds: int = 30) -> Dict[str, Any]:
        """
        Run a complete introspection cycle.

        Steps:
        1. AUDIT: Coverage analysis
        2. IDENTIFY: Find gaps
        3. PRIORITIZE: Rank by severity
        4. DELEGATE: Send prioritized gaps to LLM reasoning
        5. RECORD: Store outcomes for evidence tracking

        Args:
            max_duration_seconds: Don't exceed this time (P4 priority = low priority)

        Returns:
            Introspection cycle results with gaps identified + reasoning suggestions
        """
        import time

        start_time = time.time()
        cycle_id = f"introspect_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        logger.info(f"[P4] Starting introspection cycle: {cycle_id}")

        # Step 1: Audit coverage
        coverage = self.auditor.audit_coverage()
        audit_time = time.time() - start_time

        if audit_time > max_duration_seconds:
            logger.warning(
                f"[P4] Audit took {audit_time:.0f}s, exceeding {max_duration_seconds}s budget"
            )
            return {"status": "timeout", "cycle_id": cycle_id, "coverage": coverage}

        # Step 2-3: Identify and prioritize gaps
        gaps = coverage.get("gaps", [])[:5]  # Top 5 gaps

        if not gaps:
            logger.info("[P4] No significant gaps identified")
            return {
                "status": "complete",
                "cycle_id": cycle_id,
                "coverage": coverage,
                "gaps_found": 0,
            }

        # Step 4: Delegate to LLM reasoning
        reasoning_results = self._delegate_to_llm_reasoning(gaps, cycle_id, max_duration_seconds - (time.time() - start_time))

        # Step 5: Record outcomes
        self._record_introspection_outcomes(cycle_id, reasoning_results)

        return {
            "status": "complete",
            "cycle_id": cycle_id,
            "coverage": coverage,
            "gaps_analyzed": len(gaps),
            "reasoning_results": reasoning_results,
            "duration_seconds": time.time() - start_time,
        }

    def _delegate_to_llm_reasoning(
        self, gaps: List[Dict], cycle_id: str, time_budget_sec: float
    ) -> Dict[str, Any]:
        """Delegate gap analysis to LLM with full reasoning capacity.

        The LLM gets:
        - Domain context (what files exist, what we know)
        - Graph structure (dependencies)
        - Creative freedom to analyze as it sees fit

        Expected output: LLM reasoning + suggested study approach
        """
        try:
            # Prepare context for LLM
            context = self._prepare_llm_context(gaps)

            # Call LLM with reasoning mode enabled
            reasoning_prompt = f"""
You are the butler's introspection engine. We've identified knowledge gaps.

IDENTIFIED GAPS (sorted by severity):
{json.dumps(gaps[:3], indent=2)}

CONTEXT:
{json.dumps(context, indent=2)}

## What to Explore

Given these gaps, think through whatever seems relevant:

- **Why does each gap matter?** What depends on understanding this domain?
- **What are the ripple effects?** If this gap remains, what could go wrong?
- **What patterns do you notice?** Are certain gaps blocking progress on others?
- **How would you approach learning this?** What's the natural learning order?
- **What would be most helpful right now?** Which gap, if filled first, would unlock others?
- **What's unclear about these gaps?** What additional information would help prioritize them?
- **What surprised you about the gaps?** Any unexpected connections or dependencies?

## Your Reasoning Process

Use multi-step thinking as it helps. Explore the relationships. Consider both immediate risks and longer-term implications.

Share your insights in whatever way makes sense — you decide how to structure your analysis. Your thinking process itself is valuable information for system learning.
"""

            logger.info("[P4] Sending gaps to LLM for reasoning analysis...")

            # For now: log the prompt and return placeholder
            # In production: Call to LLM with thinking mode
            logger.debug(f"[P4] Reasoning prompt:\n{reasoning_prompt[:500]}...")

            return {
                "analyses": [
                    {
                        "domain": gap["domain"],
                        "gap_score": gap["gap_score"],
                        "recommended_study": f"Analyze {gap['domain']} domain using LLM reasoning",
                        "status": "pending_llm_reasoning",
                    }
                    for gap in gaps[:3]
                ],
                "note": "Awaiting LLM reasoning delegation in Section 6",
            }

        except Exception as e:
            logger.error(f"[P4] LLM delegation failed: {e}")
            return {"status": "error", "message": str(e)}

    def _prepare_llm_context(self, gaps: List[Dict]) -> Dict[str, Any]:
        """Prepare context for LLM reasoning."""
        context = {
            "gap_count": len(gaps),
            "top_gaps": gaps[:3],
            "total_coverage": "Computed from audit",
            "note": "Graph structure available in Section 6 for deep reasoning",
        }
        return context

    def _record_introspection_outcomes(self, cycle_id: str, results: Dict):
        """Record outcomes for evidence feedback tracking."""
        try:
            db_path = self.storage_dir / ".introspection.db"
            conn = sqlite3.connect(str(db_path))

            # Record cycle metadata
            conn.execute(
                """INSERT OR REPLACE INTO study_plans (id, domain, plan_text, status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    cycle_id,
                    "introspection_cycle",
                    json.dumps(results),
                    "recorded",
                    datetime.now().isoformat(),
                ),
            )

            conn.commit()
            conn.close()

            logger.info(f"[P4] Recorded introspection cycle: {cycle_id}")

        except Exception as e:
            logger.error(f"[P4] Failed to record outcomes: {e}")


# ============================================================================
# CLI / TESTING
# ============================================================================


def main():
    """Test introspection engine."""
    import logging

    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 80)
    print("INTROSPECTION ENGINE — P4 TESTING")
    print("=" * 80)

    engine = IntrospectionEngine()

    # Run introspection cycle
    print("\n🧠 Running introspection cycle (max 30 seconds)...")
    results = engine.run_introspection_cycle()

    print(f"\nStatus: {results['status']}")
    print(f"Duration: {results.get('duration_seconds', 0):.1f}s")

    coverage = results.get("coverage", {})
    print(f"\nCoverage Audit:")
    print(f"  Total files: {coverage.get('total_files', 0)}")
    print(f"  Total learnings: {coverage.get('total_learnings', 0)}")
    print(f"  Gaps identified: {results.get('gaps_analyzed', 0)}")

    print(f"\nTop Gaps (domains needing learning):")
    for gap in coverage.get("gaps", [])[:5]:
        print(
            f"  {gap['domain']:20} Gap: {gap['gap_score']:.0%}  "
            f"(Files: {gap['files']}, Learnings: {gap['learnings']}, Priority: {gap['priority']})"
        )

    print(f"\nReasoning Results:")
    reasoning = results.get("reasoning_results", {})
    for analysis in reasoning.get("analyses", [])[:3]:
        print(f"  • {analysis['domain']}: {analysis['recommended_study']}")

    print(f"\n[Next] These gaps will be delegated to LLM reasoning in Section 6")
    print(f"[Evidence Loop] Outcomes will be tracked in .introspection.db")


if __name__ == "__main__":
    main()
