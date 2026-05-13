#!/usr/bin/env python3
"""
Temporal Validator - Ensures Success Persists Without Reversal

This module validates that detected successes weren't followed by
reversals, retries, or errors within a time window.

The DNA principle: A success is only real if it survives the test of time.
A "that worked!" followed by "wait, that broke something" is not a success.

ADDITIVE to existing detection - runs AFTER regex/LLM detection to validate.

Usage:
    from memory.temporal_validator import TemporalValidator

    validator = TemporalValidator(window_seconds=300)  # 5 minutes

    # Validate a detected success against subsequent entries
    is_valid, modifier = validator.validate_persistence(
        success_timestamp="2024-01-15T10:00:00",
        success_task="Deployed Django",
        entries_after=subsequent_entries
    )

    if is_valid:
        adjusted_confidence = original_confidence + modifier
"""

import re
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


@dataclass
class ValidationResult:
    """Result of temporal validation."""
    is_valid: bool
    confidence_modifier: float  # -0.5 to +0.2
    reason: str
    reversal_evidence: Optional[str] = None


class TemporalValidator:
    """
    Validates success persistence over time.

    Philosophy: The mirrored text (DNA) records everything - including
    retries, errors, and corrections. A true success should not be
    followed by these reversal signals within the validation window.
    """

    # Patterns that indicate the previous success was NOT real
    REVERSAL_PATTERNS = [
        # Explicit reversals
        (r"\b(no|wait|actually|oops)\b.{0,20}(that|it).{0,10}(didn'?t|not|wrong)", "explicit_reversal"),
        (r"\bthat (broke|failed|didn'?t work|isn'?t right)", "explicit_failure"),
        (r"\b(reverting|rollback|undo|revert)", "rollback"),

        # Retry indicators
        (r"\btry(ing)? again\b", "retry"),
        (r"\blet me (fix|redo|try)", "retry"),
        (r"\bone more (time|attempt|try)", "retry"),
        (r"\bstill (not|broken|failing)", "still_failing"),

        # Error indicators
        (r"\berror\b.{0,30}(same|again|still)", "recurring_error"),
        (r"\bfailed\b.{0,20}(again|still)", "recurring_failure"),
        (r"\b(crash|crashed|crashing)\b", "crash"),

        # Correction indicators
        (r"\bactually.{0,20}(need|should|have to)", "correction"),
        (r"\bwait.{0,10}(I|we|that)", "correction"),
        (r"\bmissed.{0,20}(something|a step|one thing)", "missed_step"),
    ]

    # Patterns that REINFORCE the success (positive modifiers)
    CONFIRMATION_PATTERNS = [
        (r"\bstill (working|works|good|running)", "sustained_success"),
        (r"\b(confirmed|verified|double[- ]?checked)", "confirmed"),
        (r"\ball (good|set|done|working)", "all_good"),
        (r"\bno (issues?|problems?|errors?)\b", "no_issues"),
    ]

    def __init__(self, window_seconds: int = 300):
        """
        Initialize validator.

        Args:
            window_seconds: Time window to check for reversals (default 5 min)
        """
        self.window = timedelta(seconds=window_seconds)

    def validate_persistence(
        self,
        success_timestamp: str,
        success_task: str,
        entries_after: List[Dict]
    ) -> ValidationResult:
        """
        Validate that a success persists without reversal.

        Args:
            success_timestamp: ISO timestamp of the detected success
            success_task: Description of the successful task
            entries_after: Work log entries AFTER the success

        Returns:
            ValidationResult with is_valid, confidence_modifier, and reason
        """
        try:
            success_time = datetime.fromisoformat(success_timestamp)
        except (ValueError, TypeError):
            # Can't parse timestamp, assume valid
            return ValidationResult(
                is_valid=True,
                confidence_modifier=0.0,
                reason="timestamp_unparseable"
            )

        # Get entries within the validation window
        window_end = success_time + self.window
        entries_in_window = self._get_entries_in_window(
            entries_after, success_time, window_end
        )

        if not entries_in_window:
            # No subsequent entries - cautiously valid
            return ValidationResult(
                is_valid=True,
                confidence_modifier=0.0,
                reason="no_subsequent_entries"
            )

        # Check for reversal patterns
        for entry in entries_in_window:
            content = entry.get("content", "").lower()

            for pattern, reversal_type in self.REVERSAL_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    return ValidationResult(
                        is_valid=False,
                        confidence_modifier=-0.5,
                        reason=f"reversal_detected:{reversal_type}",
                        reversal_evidence=content[:100]
                    )

        # Check for confirmation patterns (positive reinforcement)
        confirmations = 0
        for entry in entries_in_window:
            content = entry.get("content", "").lower()
            for pattern, _ in self.CONFIRMATION_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    confirmations += 1
                    break

        # Calculate confidence modifier
        if confirmations >= 2:
            modifier = 0.15  # Strong confirmation
        elif confirmations == 1:
            modifier = 0.1  # Some confirmation
        else:
            modifier = 0.0  # No additional confirmation

        return ValidationResult(
            is_valid=True,
            confidence_modifier=modifier,
            reason=f"validated_with_{confirmations}_confirmations"
        )

    def _get_entries_in_window(
        self,
        entries: List[Dict],
        after: datetime,
        before: datetime
    ) -> List[Dict]:
        """Get entries within a time window."""
        result = []
        for entry in entries:
            try:
                entry_time = datetime.fromisoformat(entry.get("timestamp", ""))
                if after < entry_time <= before:
                    result.append(entry)
            except (ValueError, TypeError):
                continue
        return result

    def check_for_same_task_retry(
        self,
        success_task: str,
        entries_after: List[Dict]
    ) -> bool:
        """
        Check if the same task was retried after the success.

        This indicates the "success" was actually a failure.
        """
        # Extract key terms from the task
        task_terms = set(
            word.lower() for word in re.findall(r'\w+', success_task)
            if len(word) > 3
        )

        for entry in entries_after:
            content = entry.get("content", "").lower()
            entry_type = entry.get("entry_type", "")

            # Only check commands and dialogues
            if entry_type not in ("command", "dialogue"):
                continue

            # Check if same task terms appear with retry indicators
            content_terms = set(re.findall(r'\w+', content))
            overlap = task_terms & content_terms

            # If significant overlap AND retry indicators
            if len(overlap) >= 2:
                for pattern, _ in self.REVERSAL_PATTERNS:
                    if re.search(pattern, content):
                        return True

        return False


def validate_success(
    success_timestamp: str,
    success_task: str,
    entries_after: List[Dict],
    window_seconds: int = 300
) -> Tuple[bool, float]:
    """
    Convenience function to validate a success.

    Returns:
        (is_valid, confidence_modifier) tuple
    """
    validator = TemporalValidator(window_seconds)
    result = validator.validate_persistence(
        success_timestamp, success_task, entries_after
    )
    return result.is_valid, result.confidence_modifier


# CLI interface
if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Temporal Validator - Validate success persistence")
        print("")
        print("Commands:")
        print("  test <timestamp> <task>  - Test validation with sample entries")
        print("  patterns                 - Show all reversal patterns")
        print("")
        print("Example:")
        print("  python temporal_validator.py test '2024-01-15T10:00:00' 'Deploy Django'")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "patterns":
        validator = TemporalValidator()
        print("Reversal Patterns (indicate success was NOT real):")
        for pattern, reversal_type in validator.REVERSAL_PATTERNS:
            print(f"  [{reversal_type}]: {pattern}")

        print("\nConfirmation Patterns (reinforce success):")
        for pattern, conf_type in validator.CONFIRMATION_PATTERNS:
            print(f"  [{conf_type}]: {pattern}")

    elif cmd == "test":
        if len(sys.argv) < 4:
            print("Usage: test <timestamp> <task>")
            sys.exit(1)

        timestamp = sys.argv[2]
        task = sys.argv[3]

        # Create sample entries for testing
        sample_entries = [
            {
                "timestamp": datetime.fromisoformat(timestamp).replace(
                    second=datetime.fromisoformat(timestamp).second + 30
                ).isoformat(),
                "entry_type": "dialogue",
                "content": "still working, no issues",
                "source": "user"
            },
            {
                "timestamp": datetime.fromisoformat(timestamp).replace(
                    minute=datetime.fromisoformat(timestamp).minute + 2
                ).isoformat(),
                "entry_type": "dialogue",
                "content": "confirmed everything looks good",
                "source": "user"
            }
        ]

        validator = TemporalValidator()
        result = validator.validate_persistence(timestamp, task, sample_entries)

        print(f"Validation Result:")
        print(f"  Is Valid: {result.is_valid}")
        print(f"  Confidence Modifier: {result.confidence_modifier:+.2f}")
        print(f"  Reason: {result.reason}")
        if result.reversal_evidence:
            print(f"  Evidence: {result.reversal_evidence}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
