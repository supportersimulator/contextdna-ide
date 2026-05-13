#!/usr/bin/env python3
"""
AUTO REVIEWER - Synaptic's Automated Task Review System

This module provides automated review of Atlas's completed work for the
Phone → Synaptic → Atlas cognitive control architecture (Logos Priority 2).

Review Process:
    1. Atlas completes task → POST /atlas/terminal with outcome=completed
    2. AutoReviewer evaluates output against original intent
    3. Review outcome: pass | needs_revision | fail
    4. If needs_revision: emit revision directive via Section 6
    5. Phone receives review result via /report endpoint

Review Methods (in priority order):
    1. LLM Review: Uses Claude/GPT to evaluate task completion
    2. Heuristic Fallback: Rules-based validation when LLM unavailable

Heuristic Checks:
    - Output length vs intent complexity
    - Keyword coverage from original intent
    - Files changed vs scope of task
    - Explicit error/failure patterns

Usage:
    from memory.auto_reviewer import get_auto_reviewer, trigger_review

    # Automatic (called from /atlas/terminal endpoint)
    review = await trigger_review(task_id)

    # Manual review
    reviewer = get_auto_reviewer()
    review = await reviewer.review_task(task_id)
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

from memory.task_persistence import get_task_store, TaskStore

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    """Result of Synaptic's automated review."""
    outcome: str  # pass | needs_revision | fail
    confidence: float  # 0.0 - 1.0
    notes: str
    followups: List[str]
    review_method: str  # llm | heuristic
    reviewed_at: str = None

    def __post_init__(self):
        if self.reviewed_at is None:
            self.reviewed_at = datetime.now(timezone.utc).isoformat()


class AutoReviewer:
    """
    Automated review system for task completion.

    Reviews Atlas's work using:
    1. LLM evaluation (when available)
    2. Heuristic fallback (always available)
    """

    def __init__(self, store: TaskStore = None):
        self.store = store or get_task_store()
        self.llm_available = self._check_llm_availability()

    def _check_llm_availability(self) -> bool:
        """Check if LLM review is available via Redis health cache (no direct HTTP to 5044)."""
        try:
            from memory.llm_priority_queue import check_llm_health
            available = check_llm_health()
            if available:
                logger.info("LLM review available via priority queue health cache")
                return True
        except Exception as e:
            logger.debug(f"LLM health check failed: {e}")
        logger.info("LLM review unavailable - using heuristic fallback")
        return False

    async def review_task(self, task_id: str) -> ReviewResult:
        """
        Review a completed task.

        Args:
            task_id: The task to review

        Returns:
            ReviewResult with outcome, confidence, notes
        """
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # Get terminal event (completion details)
        terminal_event = task.get("terminal_event")
        if not terminal_event:
            # No terminal event - task might not be complete
            return ReviewResult(
                outcome="fail",
                confidence=1.0,
                notes="No terminal event found - task did not report completion properly",
                followups=["Ensure Atlas uses POST /atlas/terminal to report completion"],
                review_method="heuristic"
            )

        # Check terminal outcome
        if terminal_event.get("outcome") == "failed":
            return ReviewResult(
                outcome="fail",
                confidence=1.0,
                notes=f"Task failed: {terminal_event.get('summary', 'No reason provided')}",
                followups=[],
                review_method="heuristic"
            )

        if terminal_event.get("outcome") == "blocked":
            return ReviewResult(
                outcome="needs_revision",
                confidence=1.0,
                notes=f"Task blocked: {terminal_event.get('blocker_reason', 'No reason provided')}",
                followups=["Address the blocker and retry"],
                review_method="heuristic"
            )

        # Task reported as completed - evaluate quality
        if self.llm_available:
            return await self._llm_review(task, terminal_event)
        else:
            return self._heuristic_review(task, terminal_event)

    async def _llm_review(self, task: Dict, terminal_event: Dict) -> ReviewResult:
        """
        Review task using LLM evaluation via priority queue.

        ALL LLM access routes through llm_priority_queue — NO direct HTTP to port 5044.
        """
        from memory.llm_priority_queue import butler_query

        intent = task.get("intent", "")
        summary = terminal_event.get("summary", "")
        files_changed = terminal_event.get("files_changed", [])

        system_prompt = """You are Synaptic, reviewing Atlas's completed work.
Evaluate whether the task was completed successfully based on the original intent.

Return JSON:
{
  "outcome": "pass" | "needs_revision" | "fail",
  "confidence": 0.0-1.0,
  "notes": "Your assessment",
  "followups": ["suggested", "next", "steps"]
}"""

        user_prompt = f"""Review this completed task:

ORIGINAL INTENT:
{intent}

COMPLETION SUMMARY:
{summary}

FILES CHANGED:
{', '.join(files_changed) if files_changed else 'None reported'}

Provide your review assessment as JSON."""

        try:
            # Route through priority queue (P4 BACKGROUND via butler_query)
            llm_response = butler_query(
                system_prompt,
                user_prompt,
                profile="extract"
            )

            if llm_response:
                # Strip thinking tags
                llm_response = re.sub(r'<think>.*?</think>', '', llm_response, flags=re.DOTALL).strip()

                # Parse JSON from LLM response
                try:
                    json_match = re.search(r'\{[^{}]*\}', llm_response, re.DOTALL)
                    if json_match:
                        review_data = json.loads(json_match.group())
                        return ReviewResult(
                            outcome=review_data.get("outcome", "pass"),
                            confidence=float(review_data.get("confidence", 0.7)),
                            notes=review_data.get("notes", "LLM review completed"),
                            followups=review_data.get("followups", []),
                            review_method="llm"
                        )
                except json.JSONDecodeError:
                    pass

                # Fallback: extract from natural language response
                outcome = "pass"
                if re.search(r'\b(fail|failed|doesn\'t|did not|incomplete|miss|short)\b', llm_response, re.IGNORECASE):
                    outcome = "fail"
                elif re.search(r'\b(partial|partially|needs.*work|revision|issue|problem)\b', llm_response, re.IGNORECASE):
                    outcome = "needs_revision"

                confidence = 0.6
                if re.search(r'\b(clearly|definitely|certainly|obviously)\b', llm_response, re.IGNORECASE):
                    confidence = 0.8
                elif re.search(r'\b(uncertain|unclear|ambiguous)\b', llm_response, re.IGNORECASE):
                    confidence = 0.4

                return ReviewResult(
                    outcome=outcome,
                    confidence=confidence,
                    notes=llm_response[:200],
                    followups=[],
                    review_method="llm"
                )

        except Exception as e:
            logger.warning(f"LLM review failed: {e}")

        # Fall back to heuristic if LLM fails
        logger.info("Falling back to heuristic review")
        return self._heuristic_review(task, terminal_event)

    def _heuristic_review(self, task: Dict, terminal_event: Dict) -> ReviewResult:
        """
        Review task using rule-based heuristics.

        Checks:
        1. Output summary exists and is meaningful
        2. Keywords from intent appear in summary
        3. Files changed (if any) aligns with scope
        4. No explicit error patterns
        """
        intent = task.get("intent", "")
        summary = terminal_event.get("summary", "")
        files_changed = terminal_event.get("files_changed", [])

        # Initialize scores
        issues = []
        confidence_factors = []

        # Check 1: Summary exists and has substance
        summary_score = self._check_summary_quality(summary, intent)
        confidence_factors.append(summary_score)
        if summary_score < 0.5:
            issues.append("Summary seems too brief or lacks detail")

        # Check 2: Keyword coverage
        keyword_score = self._check_keyword_coverage(intent, summary)
        confidence_factors.append(keyword_score)
        if keyword_score < 0.3:
            issues.append("Summary doesn't address key aspects of the intent")

        # Check 3: Files changed alignment (if applicable)
        if self._intent_suggests_code_changes(intent):
            if not files_changed:
                issues.append("Task suggested code changes but no files were reported")
                confidence_factors.append(0.3)
            else:
                confidence_factors.append(0.8)

        # Check 4: Error patterns
        error_patterns = self._check_error_patterns(summary)
        if error_patterns:
            issues.extend(error_patterns)
            confidence_factors.append(0.2)

        # Calculate overall confidence
        avg_confidence = sum(confidence_factors) / len(confidence_factors) if confidence_factors else 0.5

        # Determine outcome
        if avg_confidence >= 0.7 and len(issues) == 0:
            return ReviewResult(
                outcome="pass",
                confidence=avg_confidence,
                notes="Task completed successfully",
                followups=[],
                review_method="heuristic"
            )
        elif avg_confidence >= 0.4 or len(issues) <= 1:
            return ReviewResult(
                outcome="pass",
                confidence=avg_confidence,
                notes=f"Task completed with minor concerns: {'; '.join(issues)}" if issues else "Task completed",
                followups=issues,
                review_method="heuristic"
            )
        else:
            return ReviewResult(
                outcome="needs_revision",
                confidence=avg_confidence,
                notes=f"Task needs revision: {'; '.join(issues)}",
                followups=issues,
                review_method="heuristic"
            )

    def _check_summary_quality(self, summary: str, intent: str) -> float:
        """Check if summary is substantial relative to intent."""
        if not summary:
            return 0.0

        # Rough heuristic: summary should be at least 10% of intent length
        # but minimum 20 characters
        min_length = max(20, len(intent) * 0.1)
        if len(summary) < min_length:
            return len(summary) / min_length

        # Good length
        return min(1.0, len(summary) / (min_length * 2))

    def _check_keyword_coverage(self, intent: str, summary: str) -> float:
        """Check if key words from intent appear in summary."""
        # Extract significant words (>3 chars, not common words)
        common_words = {"the", "and", "for", "with", "that", "this", "from", "have", "will", "should"}
        intent_words = set(w.lower() for w in re.findall(r'\b\w{4,}\b', intent)) - common_words
        summary_words = set(w.lower() for w in re.findall(r'\b\w{4,}\b', summary))

        if not intent_words:
            return 0.5  # Can't evaluate

        # Calculate overlap
        overlap = len(intent_words & summary_words)
        return min(1.0, overlap / (len(intent_words) * 0.5))

    def _intent_suggests_code_changes(self, intent: str) -> bool:
        """Check if intent suggests code should be modified."""
        code_words = [
            "implement", "add", "create", "modify", "update", "fix",
            "refactor", "change", "write", "delete", "remove", "edit"
        ]
        intent_lower = intent.lower()
        return any(word in intent_lower for word in code_words)

    def _check_error_patterns(self, summary: str) -> List[str]:
        """Check for explicit error/failure patterns in summary."""
        issues = []
        summary_lower = summary.lower()

        error_patterns = [
            (r'\berror\b', "Summary mentions an error"),
            (r'\bfailed\b', "Summary mentions failure"),
            (r'\bcould not\b', "Summary indicates inability to complete"),
            (r'\bunable to\b', "Summary indicates inability to complete"),
            (r'\bexception\b', "Summary mentions an exception"),
        ]

        for pattern, message in error_patterns:
            if re.search(pattern, summary_lower):
                issues.append(message)

        return issues

    def apply_review(self, task_id: str, review: ReviewResult) -> Dict[str, Any]:
        """
        Apply the review result to the task.

        Args:
            task_id: The task to update
            review: The review result

        Returns:
            Updated task
        """
        return self.store.add_synaptic_review(
            task_id,
            {
                "outcome": review.outcome,
                "confidence": review.confidence,
                "notes": review.notes,
                "followups": review.followups
            }
        )


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_reviewer: Optional[AutoReviewer] = None


def get_auto_reviewer() -> AutoReviewer:
    """Get the global auto reviewer instance."""
    global _reviewer
    if _reviewer is None:
        _reviewer = AutoReviewer()
    return _reviewer


async def trigger_review(task_id: str) -> ReviewResult:
    """
    Trigger automated review for a task.

    This is the primary entry point, called from /atlas/terminal endpoint.

    Args:
        task_id: The task to review

    Returns:
        ReviewResult with outcome and details
    """
    reviewer = get_auto_reviewer()
    review = await reviewer.review_task(task_id)
    reviewer.apply_review(task_id, review)

    # If needs revision, emit revision directive
    if review.outcome == "needs_revision":
        from memory.task_directives import emit_revision_directive
        from memory.task_persistence import get_task_store

        store = get_task_store()
        task = store.get_task(task_id)
        if task:
            emit_revision_directive(task, review.notes)
            logger.info(f"Emitted revision directive for {task_id}: {review.notes}")

    return review


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════════╗")
        print("║  AUTO REVIEWER - Synaptic's Automated Task Review                ║")
        print("║  Logos Priority 2: Auto-review on completion                     ║")
        print("╚══════════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python auto_reviewer.py review <task_id>    # Review a task")
        print("  python auto_reviewer.py status              # Check reviewer status")
        print()
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "review" and len(sys.argv) >= 3:
        task_id = sys.argv[2]
        review = asyncio.run(trigger_review(task_id))
        print(f"Review complete for {task_id}:")
        print(json.dumps(asdict(review), indent=2))

    elif cmd == "status":
        reviewer = get_auto_reviewer()
        print(f"LLM Available: {reviewer.llm_available}")
        print(f"Review Method: {'llm' if reviewer.llm_available else 'heuristic'}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
