#!/usr/bin/env python3
"""
BUTLER REASONING CHAIN — Multi-Step Causal Inference for Synaptic

Purpose:
  Transform butler context (from butler_context_summary) into multi-step
  reasoning chains optimized for the local 14B LLM to maximize first-try success.

Philosophy:
  Alfred doesn't just hand Batman the files. Alfred says:
  "Sir, modifying this file will affect these downstream systems. The last 3 times
  you changed this, you forgot to restart the service. Here's the ripple chain.
  Here's what worked before. Here's what failed. Shall I prepare the restart?"

  This module generates THAT reasoning chain.

Output Format:
  {
    "reasoning_steps": [
      { "step": 1, "action": "...", "reasoning": "...", "confidence": 0.9 },
      { "step": 2, "action": "...", "reasoning": "...", "confidence": 0.85 },
      ...
    ],
    "ripple_warnings": [...],
    "known_traps": [...],
    "precedent": {...},
    "recommendation": "...",
    "confidence_overall": 0.88
  }

Integration:
  butler_deep_query() in persistent_hook_structure.py calls this to generate
  Section 6 (Synaptic -> Atlas) guidance with multi-step reasoning.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.butler_context_summary import ButlerContextSummary

logger = logging.getLogger(__name__)


class ButlerReasoningChain:
    """Generate multi-step reasoning for task execution."""

    def __init__(self, context_tier: str = "HEAVY"):
        """
        Initialize reasoning chain generator.

        Args:
          context_tier: "LITE" (150ms), "HEAVY" (250ms), or "REASONING" (500ms)
        """
        self.context_tier = context_tier
        self.butler = ButlerContextSummary(tiers=context_tier)

    def generate(self, task: str, context_hint: str = "") -> Dict[str, Any]:
        """
        Generate multi-step reasoning chain for a task.

        Args:
          task: User's request or file being modified
          context_hint: "modifying" | "creating" | "debugging" | "refactoring"

        Returns:
          Reasoning chain optimized for LLM multi-step inference
        """
        # Get hybrid context
        context = self.butler.summarize(task, context_hint)

        # Generate reasoning steps
        steps = self._generate_steps(task, context, context_hint)

        # Extract warnings and recommendations
        warnings = self._extract_warnings(context)
        traps = self._identify_traps(context)
        precedent = self._find_precedent(context)
        recommendation = self._synthesize_recommendation(task, context, steps)

        # Calculate confidence
        confidence = self._calculate_confidence(steps, context)

        return {
            "task": task,
            "context_hint": context_hint,
            "generated_at": datetime.utcnow().isoformat(),

            "reasoning_steps": steps,
            "ripple_warnings": warnings,
            "known_traps": traps,
            "precedent": precedent,
            "recommendation": recommendation,

            "confidence_overall": confidence,
            "context_tier_used": context["context_tier"],
            "query_time_ms": context.get("query_time_ms", 0),
        }

    # ────────────────────────────────────────────────────────────────────────────
    # STEP GENERATION
    # ────────────────────────────────────────────────────────────────────────────

    def _generate_steps(
        self, task: str, context: Dict, context_hint: str
    ) -> List[Dict]:
        """
        Generate adaptive reasoning insights based on what's actually relevant.

        Philosophy: Don't force a 6-step structure. Instead, assess what
        reasoning would be VALUABLE for this specific task and only generate that.
        Emerge steps from evidence, not from a template.
        """
        steps = []
        step_counter = 1

        # Determine what reasoning is relevant based on actual context

        # INSIGHT 1: Target identification (if we have unambiguous targets)
        ripple = context.get("ripple_analysis", {})
        hubs = ripple.get("critical_hubs", [])

        if hubs:
            target_step = self._step_identify_target(task, context)
            if target_step:
                target_step["step"] = step_counter
                steps.append(target_step)
                step_counter += 1

        # INSIGHT 2: Ripple effects matter when change is broad
        if hubs and len(hubs) > 2:
            ripple_step = self._step_analyze_ripple(task, context)
            if ripple_step:
                ripple_step["step"] = step_counter
                steps.append(ripple_step)
                step_counter += 1

        # INSIGHT 3: Precedent matters when we have prior patterns
        patterns = context.get("cross_session_patterns", [])
        wisdom = context.get("proven_wisdom", [])

        if wisdom or patterns:
            precedent_step = self._step_check_precedent(task, context)
            if precedent_step:
                precedent_step["step"] = step_counter
                steps.append(precedent_step)
                step_counter += 1

        # INSIGHT 4: Landmines matter when we've seen these patterns fail before
        failures = context.get("failure_landscape", {})
        traps = failures.get("high_confidence_traps", [])

        if traps:
            landmine_step = self._step_identify_landmines(task, context)
            if landmine_step:
                landmine_step["step"] = step_counter
                steps.append(landmine_step)
                step_counter += 1

        # INSIGHT 5: Dependencies matter when they're complex
        if hubs and len(hubs) > 3:
            deps_step = self._step_trace_dependencies(task, context)
            if deps_step:
                deps_step["step"] = step_counter
                steps.append(deps_step)
                step_counter += 1

        # INSIGHT 6: Aaron's context always matters (he might have priority)
        aaron = context.get("aaron_context", {})
        if aaron.get("likely_intent"):
            aaron_step = self._step_aaron_priority(task, context)
            if aaron_step:
                aaron_step["step"] = step_counter
                steps.append(aaron_step)
                step_counter += 1

        # If we generated no insights, at least provide target identification
        if not steps and hubs:
            target_step = self._step_identify_target(task, context)
            if target_step:
                target_step["step"] = 1
                steps.append(target_step)

        return steps

    def _step_identify_target(self, task: str, context: Dict) -> Optional[Dict]:
        """Step 1: Identify what file/component is being modified."""
        try:
            import re

            ripple = context.get("ripple_analysis", {})
            hubs = ripple.get("critical_hubs", [])

            target_file = None
            reasoning = ""

            # First: extract file references from the task description itself
            file_refs = re.findall(r'[\w/.-]+\.(?:py|js|ts|sh|yaml|json|md)\b', task)
            if file_refs:
                # Match task-mentioned file against hubs for connection count
                task_file = file_refs[0]
                hub_match = next((h for h in hubs if task_file in h[0]), None)
                if hub_match:
                    target_file = hub_match[0]
                    reasoning = f"Modifying {target_file} (hub: {hub_match[1]} dependencies)"
                else:
                    target_file = task_file
                    reasoning = f"Modifying {target_file} (mentioned in task)"

            # Fallback: use top hub only if no file mentioned in task
            if not target_file and hubs:
                target_file = hubs[0][0]
                reasoning = f"Likely target: {target_file} (hub: {hubs[0][1]} dependencies)"

            if target_file or reasoning:
                return {
                    "step": 1,
                    "action": f"Identify target: {target_file or 'extracted from context'}",
                    "reasoning": reasoning,
                    "confidence": 0.9 if file_refs else 0.6,
                }
        except Exception as e:
            logger.debug(f"Step 1 generation failed: {e}")

        return None

    def _step_analyze_ripple(self, task: str, context: Dict) -> Optional[Dict]:
        """Step 2: Analyze ripple effects (3 levels deep)."""
        try:
            ripple = context.get("ripple_analysis", {})
            hubs = ripple.get("critical_hubs", [])

            if hubs:
                reasoning = f"This change affects {len(hubs)} downstream files:"
                for hub, edge_count in hubs[:3]:
                    reasoning += f"\n  • {hub} ({edge_count} connections)"

                return {
                    "step": 2,
                    "action": f"Trace ripple: {len(hubs)} affected files",
                    "reasoning": reasoning,
                    "confidence": 0.85,
                }
        except Exception as e:
            logger.debug(f"Step 2 generation failed: {e}")

        return None

    def _step_check_precedent(self, task: str, context: Dict) -> Optional[Dict]:
        """Step 3: Check what worked before."""
        try:
            patterns = context.get("cross_session_patterns", [])
            wisdom = context.get("proven_wisdom", [])

            if wisdom:
                top_wisdom = wisdom[0]
                reasoning = f"Precedent: {top_wisdom['title']}"

                return {
                    "step": 3,
                    "action": "Check precedent from past sessions",
                    "reasoning": reasoning,
                    "confidence": float(top_wisdom.get("confidence", 0.5)),
                }
        except Exception as e:
            logger.debug(f"Step 3 generation failed: {e}")

        return None

    def _step_identify_landmines(self, task: str, context: Dict) -> Optional[Dict]:
        """Step 4: Identify failure patterns (landmines)."""
        try:
            failures = context.get("failure_landscape", {})
            traps = failures.get("high_confidence_traps", [])

            if traps:
                reasoning = f"LANDMINES DETECTED: {', '.join(traps[:2])}"

                return {
                    "step": 4,
                    "action": "Identify failure patterns",
                    "reasoning": reasoning,
                    "confidence": 0.8,
                }
        except Exception as e:
            logger.debug(f"Step 4 generation failed: {e}")

        return None

    def _step_trace_dependencies(self, task: str, context: Dict) -> Optional[Dict]:
        """Step 5: Trace full dependency chain."""
        try:
            ripple = context.get("ripple_analysis", {})
            hubs = ripple.get("critical_hubs", [])

            if hubs and len(hubs) > 3:
                reasoning = (
                    f"Full dependency chain: Change propagates through "
                    f"{len(hubs)} files. Most critical: {hubs[0][0]}"
                )

                return {
                    "step": 5,
                    "action": "Trace dependency chain (3+ levels)",
                    "reasoning": reasoning,
                    "confidence": 0.75,
                }
        except Exception as e:
            logger.debug(f"Step 5 generation failed: {e}")

        return None

    def _step_aaron_priority(self, task: str, context: Dict) -> Optional[Dict]:
        """Step 6: Synthesize Aaron's priorities."""
        try:
            aaron = context.get("aaron_context", {})
            intent = aaron.get("likely_intent", "unknown")

            reasoning = f"Aaron's context: This is a {intent} task"

            return {
                "step": 6,
                "action": "Synthesize Aaron's priorities",
                "reasoning": reasoning,
                "confidence": aaron.get("confidence", 0.5),
            }
        except Exception as e:
            logger.debug(f"Step 6 generation failed: {e}")

        return None

    # ────────────────────────────────────────────────────────────────────────────
    # WARNING & TRAP EXTRACTION
    # ────────────────────────────────────────────────────────────────────────────

    def _extract_warnings(self, context: Dict) -> List[str]:
        """Extract ripple-effect warnings."""
        warnings = []

        ripple = context.get("ripple_analysis", {})
        hubs = ripple.get("critical_hubs", [])

        if hubs:
            count = len(hubs)
            warnings.append(
                f"⚠️  Ripple effect: {count} files depend on this change"
            )

        if hubs and hubs[0][1] > 300:
            warnings.append(
                f"🔥 HIGH IMPACT: {hubs[0][0]} has {hubs[0][1]} dependencies"
            )

        return warnings

    def _identify_traps(self, context: Dict) -> List[str]:
        """Identify known failure patterns."""
        traps = []

        failures = context.get("failure_landscape", {})
        high_conf = failures.get("high_confidence_traps", [])

        for trap in high_conf[:3]:
            traps.append(f"🪤 Known trap: {trap}")

        return traps

    def _find_precedent(self, context: Dict) -> Dict[str, Any]:
        """Find what worked before."""
        patterns = context.get("cross_session_patterns", [])
        wisdom = context.get("proven_wisdom", [])

        precedent = {}

        if patterns:
            precedent["pattern"] = patterns[0].get("pattern", "")
            precedent["session"] = patterns[0].get("session", "")

        if wisdom:
            top = wisdom[0]
            precedent["wisdom"] = f"{top['title']} ({top['source']})"
            precedent["confidence"] = top.get("confidence", 0)

        return precedent

    def _synthesize_recommendation(
        self, task: str, context: Dict, steps: List[Dict]
    ) -> str:
        """Synthesize overall recommendation for Atlas."""
        recommendations = []

        # Base recommendation
        if len(steps) > 4:
            recommendations.append(
                "✅ Proceed with caution - multiple dependencies detected"
            )
        elif len(steps) > 2:
            recommendations.append("✅ Proceed - context analyzed")
        else:
            recommendations.append("ℹ️  Limited context available")

        # Add specifics
        failures = context.get("failure_landscape", {})
        if failures.get("high_confidence_traps"):
            recommendations.append(
                f"⚠️  Apply mitigations for known traps: {failures['mitigations']}"
            )

        aaron = context.get("aaron_context", {})
        if aaron.get("likely_intent") == "modifying":
            recommendations.append("📝 Remember to restart services after modify")

        return " | ".join(recommendations)

    # ────────────────────────────────────────────────────────────────────────────
    # CONFIDENCE SCORING
    # ────────────────────────────────────────────────────────────────────────────

    def _calculate_confidence(self, steps: List[Dict], context: Dict) -> float:
        """Calculate overall confidence in reasoning chain."""
        if not steps:
            return 0.3  # Very low if we couldn't generate steps

        # Average step confidences
        step_conf = sum(s.get("confidence", 0.5) for s in steps) / len(steps)

        # Factor in context quality
        context_tier = context.get("context_tier", "LITE")
        tier_weights = {"LITE": 0.7, "HEAVY": 0.85, "REASONING": 0.95}
        tier_conf = tier_weights.get(context_tier, 0.8)

        # Combine: 60% steps, 40% tier
        overall = (step_conf * 0.6) + (tier_conf * 0.4)

        return min(overall, 1.0)

    # ────────────────────────────────────────────────────────────────────────────
    # INTEGRATION: SECTION 6 INJECTION FORMAT
    # ────────────────────────────────────────────────────────────────────────────

    def get_section_6_guidance(self, task: str) -> str:
        """
        Generate Section 6 (HOLISTIC_CONTEXT) guidance using reasoning chains.

        This is what gets injected into the webhook for Claude Code.
        """
        chain = self.generate(task)

        # Format for natural language injection
        guidance = "╔══════════════════════════════════════════════════════════════╗\n"
        guidance += "║  [BUTLER REASONING CHAIN]                                    ║\n"
        guidance += "╚══════════════════════════════════════════════════════════════╝\n\n"

        # Reasoning steps
        guidance += "🧠 MULTI-STEP REASONING:\n"
        for step in chain.get("reasoning_steps", []):
            guidance += (
                f"  Step {step['step']}: {step['action']}\n"
                f"    → {step['reasoning']}\n"
                f"    Confidence: {step['confidence']:.0%}\n\n"
            )

        # Warnings
        if chain.get("ripple_warnings"):
            guidance += "⚠️  RIPPLE EFFECTS:\n"
            for warning in chain["ripple_warnings"]:
                guidance += f"  {warning}\n"
            guidance += "\n"

        # Traps
        if chain.get("known_traps"):
            guidance += "🪤 KNOWN FAILURE PATTERNS:\n"
            for trap in chain["known_traps"]:
                guidance += f"  {trap}\n"
            guidance += "\n"

        # Precedent
        if chain.get("precedent"):
            precedent = chain["precedent"]
            guidance += "📚 PRECEDENT:\n"
            if precedent.get("wisdom"):
                guidance += f"  Wisdom: {precedent['wisdom']}\n"
            if precedent.get("pattern"):
                guidance += f"  Pattern: {precedent['pattern']}\n"
            guidance += "\n"

        # Recommendation
        guidance += f"💡 RECOMMENDATION:\n  {chain['recommendation']}\n"
        guidance += f"\nOverall Confidence: {chain['confidence_overall']:.0%}\n"

        return guidance


# ──────────────────────────────────────────────────────────────────────────────
# CLI / TESTING
# ──────────────────────────────────────────────────────────────────────────────


def main():
    """Test the butler reasoning chain."""
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s"
    )

    tasks = [
        "modifying memory/persistent_hook_structure.py for Section 8 generative",
        "debugging async boto3 calls in lambda functions",
        "refactoring the evidence pipeline for better promotion logic",
    ]

    for task in tasks:
        print(f"\n{'='*70}")
        print(f"TASK: {task}")
        print("=" * 70)

        reasoner = ButlerReasoningChain(context_tier="HEAVY")
        guidance = reasoner.get_section_6_guidance(task)
        print(guidance)


if __name__ == "__main__":
    main()
