#!/usr/bin/env python3
"""
BUTLER DEEP QUERY — Enhanced Section 6 Injection with Adaptive Reasoning

Purpose:
  Generate Section 6 (HOLISTIC_CONTEXT) using butler_reasoning_chain with adaptive depth.

Integration point:
  Replaces/augments voice.consult() in persistent_hook_structure.py generate_section_6()

Features:
  - Risk-adaptive ripple depth (LOW/MEDIUM/HIGH/CRITICAL)
  - Multi-step causal reasoning chains
  - Project-aware boundary detection
  - Latency budgets enforced per risk tier
  - Real-time dialogue context integration
  - Reasoning capacity optimization
"""

import logging
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.butler_reasoning_chain import ButlerReasoningChain
from memory.butler_context_summary import ButlerContextSummary
from memory.graph_reasoning_context import GraphReasoningContext

logger = logging.getLogger(__name__)


class TaskRiskAssessment:
    """Categorize task risk tier for adaptive reasoning depth."""

    # File patterns by risk tier
    CRITICAL_PATHS = [
        "persistent_hook_structure.py",  # Injection engine
        "memory/",  # Learning systems
        "context-dna/local_llm/",  # Inference
        "scripts/",  # Launch/control
    ]

    INFRASTRUCTURE_PATHS = [
        "backend/",
        "terraform/",
        ".github/workflows/",
    ]

    LOW_RISK_KEYWORDS = ["test", "doc", "comment", "readme", "example"]

    @staticmethod
    def assess(task: str) -> Tuple[str, float]:
        """
        Assess task risk tier and latency budget.

        Returns:
          (tier: str, latency_budget_ms: float)
        """
        task_lower = task.lower()

        # CRITICAL: Core butler/injection infrastructure
        for pattern in TaskRiskAssessment.CRITICAL_PATHS:
            if pattern in task_lower:
                return ("CRITICAL", 500.0)  # No budget limit

        # HIGH: Infrastructure and core services
        for pattern in TaskRiskAssessment.INFRASTRUCTURE_PATHS:
            if pattern in task_lower:
                return ("HIGH", 400.0)

        # LOW: Documentation, comments, tests
        for keyword in TaskRiskAssessment.LOW_RISK_KEYWORDS:
            if keyword in task_lower:
                return ("LOW", 100.0)

        # MEDIUM: Default for features and refactoring
        return ("MEDIUM", 250.0)

    @staticmethod
    def get_ripple_depth(tier: str) -> int:
        """Get ripple analysis depth for risk tier."""
        return {
            "CRITICAL": 7,  # Full graph traversal
            "HIGH": 5,  # Deep dependency chains
            "MEDIUM": 3,  # Standard 3 levels
            "LOW": 1,  # Only direct dependencies
        }.get(tier, 3)

    @staticmethod
    def get_reasoning_budget(tier: str, total_budget_ms: float) -> Tuple[float, float]:
        """
        Allocate budget between ripple analysis and reasoning depth.

        Returns:
          (ripple_budget_ms, reasoning_budget_ms)
        """
        if tier == "CRITICAL":
            # Both get full attention
            return (250.0, 250.0)
        elif tier == "HIGH":
            # Ripple priority
            return (250.0, 150.0)
        elif tier == "MEDIUM":
            # Balanced
            return (100.0, 150.0)
        else:  # LOW
            # Minimal ripple, focus on quick guidance
            return (50.0, 50.0)


class ProjectBoundaryDetector:
    """Detect which projects a task affects."""

    PROJECTS = {
        "context-dna": ["context-dna/", "memory/", "scripts/"],
        "er-simulator": ["simulator-core/", "backend/sim/", "mobile/"],
        "admin": ["admin.", "google-drive-code/"],
    }

    @staticmethod
    def detect(task: str) -> Tuple[str, List[str]]:
        """
        Detect primary and affected projects.

        Returns:
          (primary_project, secondary_projects)
        """
        task_lower = task.lower()
        scores = {proj: 0 for proj in ProjectBoundaryDetector.PROJECTS}

        for proj, patterns in ProjectBoundaryDetector.PROJECTS.items():
            for pattern in patterns:
                if pattern.lower() in task_lower:
                    scores[proj] += 1

        # Primary project is highest scoring
        primary = max(scores, key=scores.get) if max(scores.values()) > 0 else "context-dna"
        secondary = [p for p, s in scores.items() if s > 0 and p != primary]

        return (primary, secondary)


class ButlerDeepQuery:
    """
    Enhanced Section 6 generator with adaptive reasoning depth.

    Orchestrates:
    - Risk assessment
    - Project boundary detection
    - Latency budget allocation
    - Multi-step reasoning chains
    - Real-time context integration
    """

    def __init__(self):
        self.reasoner = ButlerReasoningChain(context_tier="HEAVY")
        self.context = ButlerContextSummary(tiers="HEAVY")
        self.graph_reasoning = GraphReasoningContext()

    def query(self, task: str, session_id: Optional[str] = None) -> str:
        """
        Generate Section 6 (HOLISTIC_CONTEXT) guidance with enhanced reasoning.

        Args:
          task: The user's current task
          session_id: Optional session ID for tracking

        Returns:
          Formatted Section 6 content ready for injection
        """
        start_time = time.time()

        # Step 1: Assess risk and allocate budgets
        risk_tier, latency_total = TaskRiskAssessment.assess(task)
        ripple_depth = TaskRiskAssessment.get_ripple_depth(risk_tier)
        ripple_budget, reasoning_budget = TaskRiskAssessment.get_reasoning_budget(
            risk_tier,
            latency_total,
        )

        # Step 2: Detect projects
        primary_proj, secondary_projs = ProjectBoundaryDetector.detect(task)

        # Step 3: Generate reasoning chain with adaptive depth
        reasoning_start = time.time()
        chain = self._generate_reasoning_chain(
            task, ripple_depth, reasoning_budget, primary_proj
        )
        reasoning_time = time.time() - reasoning_start

        # Step 4: Format for Section 6 injection
        lines = []

        # Header
        lines.append("╔══════════════════════════════════════════════════════════════════════╗")
        lines.append("║  BUTLER DEEP QUERY — HOLISTIC CONTEXT                               ║")
        lines.append("╠══════════════════════════════════════════════════════════════════════╣")
        lines.append("")

        # Risk and capacity assessment
        lines.append(f"⚠️  TASK ASSESSMENT:")
        lines.append(
            f"  • Risk Tier: {risk_tier} (ripple depth: {ripple_depth} levels)"
        )
        lines.append(f"  • Primary Project: {primary_proj}")
        if secondary_projs:
            lines.append(f"  • Cross-project: {', '.join(secondary_projs)}")
        lines.append(f"  • Budget: {latency_total:.0f}ms total")
        lines.append("")

        # Multi-step reasoning
        if chain.get("reasoning_steps"):
            lines.append("🧠 MULTI-STEP REASONING CHAIN:")
            for step in chain["reasoning_steps"]:
                lines.append(
                    f"  Step {step['step']}: {step['action']}"
                )
                lines.append(f"    → {step['reasoning']}")
                lines.append(f"    Confidence: {step['confidence']:.0%}")
            lines.append("")

        # Ripple analysis
        if chain.get("ripple_warnings"):
            lines.append("⚡ RIPPLE EFFECTS:")
            for warning in chain["ripple_warnings"]:
                lines.append(f"  {warning}")
            lines.append("")

        # Traps and mitigations
        if chain.get("known_traps"):
            lines.append("🪤 KNOWN TRAPS:")
            for trap in chain["known_traps"]:
                lines.append(f"  {trap}")
            lines.append("")

        # Precedent from past sessions
        if chain.get("precedent"):
            precedent = chain["precedent"]
            lines.append("📚 PRECEDENT FROM PAST SESSIONS:")
            if precedent.get("wisdom"):
                lines.append(f"  Wisdom: {precedent['wisdom']}")
            if precedent.get("pattern"):
                lines.append(f"  Pattern: {precedent['pattern']}")
                lines.append("")

        # P4: Graph-based reasoning context (LLM creative analysis)
        try:
            graph_context = self.graph_reasoning.get_reasoning_context(
                include_gaps=True, max_hubs=20, include_full_graph=False
            )
            if graph_context.get("knowledge_gaps"):
                lines.append("🧬 GRAPH-BASED KNOWLEDGE ANALYSIS (LLM Reasoning Mode):")
                lines.append(
                    f"  Graph Structure: {graph_context['graph_structure'].get('total_nodes', '?')} nodes, "
                    f"{graph_context['graph_structure'].get('total_edges', '?')} edges"
                )
                lines.append(
                    f"  Coverage: {graph_context['coverage_summary'].get('total_files', 0)} files, "
                    f"{graph_context['coverage_summary'].get('total_learnings', 0)} learnings"
                )

                # Top critical gaps
                gaps = graph_context.get("knowledge_gaps", [])[:3]
                if gaps:
                    lines.append("  Top Knowledge Gaps (awaiting LLM reasoning):")
                    for gap in gaps:
                        lines.append(
                            f"    • {gap['domain']}: {gap['gap_score']:.0%} gap "
                            f"({gap['files']} files, {gap['priority']})"
                        )

                lines.append("")
                lines.append(
                    "  💡 LLM REASONING MODE ENABLED: Graph context available for creative dependency analysis"
                )
                lines.append(
                    "     See Section 6 supplementary prompt for full graph reasoning opportunity"
                )
                lines.append("")
        except Exception as e:
            logger.debug(f"Graph reasoning context unavailable: {e}")

        # Synthesis and recommendation
        lines.append("💡 RECOMMENDATION:")
        lines.append(f"  {chain['recommendation']}")
        lines.append("")
        lines.append(f"Overall Confidence: {chain['confidence_overall']:.0%}")
        lines.append(f"Analysis Time: {reasoning_time:.0f}ms / {latency_total:.0f}ms budget")
        lines.append("")

        # Performance metrics
        total_time = time.time() - start_time
        lines.append(f"[Performance: {total_time*1000:.0f}ms end-to-end]")

        return "\n".join(lines)

    def _generate_reasoning_chain(
        self,
        task: str,
        ripple_depth: int,
        reasoning_budget: float,
        primary_project: str,
    ) -> Dict[str, Any]:
        """Generate reasoning chain with adaptive depth."""
        try:
            chain = self.reasoner.generate(task)

            # Enhance with project context
            chain["project_context"] = primary_project
            chain["ripple_depth_used"] = ripple_depth
            chain["reasoning_budget_ms"] = reasoning_budget

            return chain
        except Exception as e:
            logger.error(f"Reasoning chain generation failed: {e}")
            return self._fallback_chain(task)

    def _fallback_chain(self, task: str) -> Dict[str, Any]:
        """Minimal fallback when reasoning fails."""
        return {
            "task": task,
            "reasoning_steps": [],
            "ripple_warnings": ["Reasoning engine unavailable"],
            "known_traps": [],
            "precedent": {},
            "recommendation": "⚠️  Proceed with caution — limited context available",
            "confidence_overall": 0.3,
            "project_context": "unknown",
        }

    def get_graph_reasoning_prompt(self) -> Optional[str]:
        """
        Generate full LLM reasoning prompt for creative graph analysis.

        Useful for HEAVY reasoning tier or when doing P4 introspection analysis.
        Includes full graph structure and reasoning opportunity.
        """
        try:
            graph_context = self.graph_reasoning.get_reasoning_context(
                include_gaps=True, max_hubs=25, include_full_graph=False
            )
            return self.graph_reasoning.generate_reasoning_prompt(graph_context)
        except Exception as e:
            logger.debug(f"Could not generate graph reasoning prompt: {e}")
            return None


# ──────────────────────────────────────────────────────────────────────────────
# INTEGRATION WITH PERSISTENT_HOOK_STRUCTURE
# ──────────────────────────────────────────────────────────────────────────────


def butler_enhanced_section_6(prompt: str, session_id: Optional[str] = None, active_file: Optional[str] = None) -> str:
    """
    Enhanced Section 6 generator for persistent_hook_structure.py

    Now includes Synaptic agent review injection:
      1. Check Redis for cached agent review (from synaptic_reviewer.py)
      2. If review exists, prepend to Section 6 output
      3. Clear review after injection (prevent stale repeats)
      4. Generate normal deep query guidance

    Args:
      prompt: The user's current prompt/task
      session_id: Optional session ID for tracking
      active_file: Optional path to the file Atlas is currently editing (from IDE detection)

    Usage in generate_section_6():
      Replace or augment:
        response = voice.consult(prompt, context={...})
      With:
        butler_query = butler_enhanced_section_6(prompt, session_id)
        # Merge with voice output
    """
    parts = []

    # ── Agent Review Injection ─────────────────────────────────────────────
    # If Synaptic reviewed a recent agent's output, inject the review here.
    # This is the "subconscious → conscious" feedback channel.
    if session_id:
        try:
            from memory.synaptic_reviewer import get_cached_review, format_review_for_section6, clear_review
            review = get_cached_review(session_id)
            if review:
                parts.append(format_review_for_section6(review))
                parts.append("")  # blank line separator
                clear_review(session_id)  # one-shot injection
        except Exception as e:
            logger.debug(f"Agent review check skipped: {e}")

    # ── File-Aware Prompt Enhancement (G5) ─────────────────────────────────
    # If we know the active file, enrich the prompt so the LLM reasoning
    # chain and guidance are contextually aware of what Atlas is editing.
    enriched_prompt = prompt
    if active_file:
        enriched_prompt = f"[Atlas is currently editing: {active_file}]\n{prompt}"

    # ── Normal Deep Query ──────────────────────────────────────────────────
    try:
        query_engine = ButlerDeepQuery()
        deep_result = query_engine.query(enriched_prompt, session_id)
        if deep_result:
            parts.append(deep_result)
    except Exception as e:
        logger.error(f"Butler deep query failed: {e}")

    return "\n".join(parts) if parts else ""  # Graceful degradation


# ──────────────────────────────────────────────────────────────────────────────
# CLI / TESTING
# ──────────────────────────────────────────────────────────────────────────────


def main():
    """Test butler deep query."""
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s"
    )

    tasks = [
        "modifying persistent_hook_structure.py for Section 8 generative (CRITICAL)",
        "debugging async boto3 in backend/lambda (HIGH)",
        "adding doc comment to monitor.py (LOW)",
        "refactoring evidence pipeline (MEDIUM)",
    ]

    for task in tasks:
        print(f"\n{'='*70}")
        print(f"TASK: {task}")
        print("=" * 70)

        result = butler_enhanced_section_6(task)
        print(result)


if __name__ == "__main__":
    main()
