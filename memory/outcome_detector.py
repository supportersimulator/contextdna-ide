#!/usr/bin/env python3
"""
OUTCOME DETECTOR - Detects Success/Failure from User Messages

This module analyzes user messages to detect whether the previous injection
resulted in a successful task completion or a failure that needs retry.

The outcome loop:
    1. generate_context_injection() fires → records hook firing
    2. Agent works on task
    3. User responds
    4. detect_outcome(user_message) → determines success/failure
    5. HookEvolutionEngine.record_outcome() → feeds A/B testing

SUCCESS SIGNALS:
    - User confirmation: "that worked", "perfect", "success!", "nice"
    - Continuation phrases: "next", "now do", "great, also"
    - System success markers from user quotes: "200 OK", "deployed"

FAILURE SIGNALS:
    - Explicit failure: "that didn't work", "still broken", "error"
    - Retry requests: "try again", "can you fix", "that's wrong"
    - Frustration markers: "why", "again?!", "not what I asked"

Usage:
    from memory.outcome_detector import detect_outcome, OutcomeResult

    result = detect_outcome("perfect, that fixed the issue!")
    # result.outcome_type = "positive"
    # result.confidence = 0.9
    # result.signals = ["user_confirmed_perfect"]
"""

import re
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from enum import Enum


class OutcomeType(Enum):
    """Possible outcomes detected from user message."""
    POSITIVE = "positive"      # Clear success signal
    NEGATIVE = "negative"      # Clear failure signal
    NEUTRAL = "neutral"        # No clear signal either way
    UNKNOWN = "unknown"        # Can't determine


@dataclass
class OutcomeResult:
    """Result of outcome detection."""
    outcome_type: str
    confidence: float          # 0.0 to 1.0
    signals: List[str]         # What signals triggered this detection
    raw_matches: List[str] = field(default_factory=list)  # Actual text that matched


# =============================================================================
# SUCCESS PATTERNS
# =============================================================================
# Format: (pattern, confidence, signal_name)

SUCCESS_SIGNALS = [
    # User confirmation (STRONG)
    (r"\bsuccess[!.]*\b", 0.95, "user_confirmed_success"),
    (r"\bthat worked[!.]*\b", 0.95, "user_confirmed_worked"),
    (r"\bit works[!.]*\b", 0.9, "user_confirmed_works"),
    (r"\bperfect[!.]*\b", 0.9, "user_confirmed_perfect"),
    (r"\bexcellent[!.]*\b", 0.9, "user_confirmed_excellent"),
    (r"\bexactly[!.]*\b", 0.85, "user_confirmed_exactly"),
    (r"\bbeautiful[!.]*\b", 0.85, "user_confirmed_beautiful"),
    (r"\bbrilliant[!.]*\b", 0.85, "user_confirmed_brilliant"),
    (r"\bawesome[!.]*\b", 0.85, "user_confirmed_awesome"),
    (r"\bgood job[!.]*\b", 0.85, "user_confirmed_good_job"),
    (r"\bwell done[!.]*\b", 0.85, "user_confirmed_well_done"),
    (r"\bnailed it[!.]*\b", 0.9, "user_confirmed_nailed"),
    (r"\bspot on[!.]*\b", 0.85, "user_confirmed_spot_on"),
    (r"\bnice[!.]*\b", 0.8, "user_confirmed_nice"),
    (r"\bgreat[!.]*\b", 0.75, "user_confirmed_great"),
    (r"\bcool[!.]*\b", 0.7, "user_confirmed_cool"),
    (r"\blgtm\b", 0.9, "user_confirmed_lgtm"),
    (r"\blooks good\b", 0.8, "user_confirmed_looks_good"),
    (r"\bgood to go\b", 0.85, "user_confirmed_good_to_go"),
    (r"\bship it\b", 0.85, "user_confirmed_ship"),

    # Continuation (implies previous task succeeded)
    (r"\bnow (do|can you|let's|we can)\b", 0.75, "continuation_now"),
    (r"\bnext[,:]?\s+(can you|let's|do|we)\b", 0.75, "continuation_next"),
    (r"\balso[,:]?\s+(can you|do|add)\b", 0.7, "continuation_also"),
    (r"\bmoving on\b", 0.75, "continuation_moving_on"),
    (r"\blet's move to\b", 0.75, "continuation_move_to"),

    # Acknowledgment (moderate confidence)
    (r"\bthanks[!.]*\b", 0.65, "user_acknowledged_thanks"),
    (r"\bthank you[!.]*\b", 0.65, "user_acknowledged_thank_you"),
    (r"\byes[!.]*\b(?!\s*(but|however|wait))", 0.6, "user_acknowledged_yes"),
    (r"\bok[!.]*\b(?!\s*(but|however|wait|so|,))", 0.5, "user_acknowledged_ok"),
    (r"\bgot it\b", 0.6, "user_acknowledged_got_it"),

    # System success in user message (quoting output)
    (r"\b200\s*OK\b", 0.7, "system_200_ok"),
    (r"\bdeployed\b", 0.7, "system_deployed"),
    (r"\bhealthy\b", 0.65, "system_healthy"),
    (r"\bpassed\b", 0.7, "system_passed"),
    (r"\bgreen\b", 0.6, "system_green"),
]

# =============================================================================
# FAILURE PATTERNS
# =============================================================================

FAILURE_SIGNALS = [
    # Explicit failure (STRONG)
    (r"\bthat didn'?t work\b", 0.95, "failure_didnt_work"),
    (r"\bstill (broken|not working|failing|wrong)\b", 0.95, "failure_still_broken"),
    (r"\bit('s| is)? (not working|broken|failing)\b", 0.9, "failure_not_working"),
    (r"\bthat'?s (wrong|incorrect|not right)\b", 0.9, "failure_wrong"),
    (r"\bthat'?s not (what I|correct|right)\b", 0.9, "failure_not_correct"),
    (r"\bfailed\b", 0.85, "failure_explicit"),
    (r"\berror\b", 0.75, "failure_error"),
    (r"\bbug\b", 0.6, "failure_bug"),
    (r"\bbroken\b", 0.8, "failure_broken"),
    (r"\bdoesn'?t work\b", 0.9, "failure_doesnt_work"),

    # Retry/correction requests
    (r"\btry again\b", 0.85, "retry_try_again"),
    (r"\bcan you fix\b", 0.85, "retry_fix_request"),
    (r"\bfix (this|that|it)\b", 0.8, "retry_fix_this"),
    (r"\bundo\b", 0.9, "retry_undo"),
    (r"\brevert\b", 0.9, "retry_revert"),
    (r"\brollback\b", 0.9, "retry_rollback"),
    (r"\bactually[,:]?\s+(no|wait|that)\b", 0.8, "correction_actually"),
    (r"\bno[,:]?\s*wait\b", 0.85, "correction_no_wait"),
    (r"\bwait[,:]?\s+(no|that)\b", 0.8, "correction_wait"),

    # Frustration markers
    (r"\bwhy (did|is|does|doesn)\b", 0.5, "frustration_why"),
    (r"\bagain\?[!]?\b", 0.7, "frustration_again"),
    (r"[!?]{2,}", 0.6, "frustration_punctuation"),  # Multiple !? indicates frustration
    (r"\bugh\b", 0.7, "frustration_ugh"),
    (r"\bcome on\b", 0.7, "frustration_come_on"),
    (r"\bwhat happened\b", 0.6, "frustration_what_happened"),
    (r"\bthis is (wrong|broken)\b", 0.85, "frustration_this_is_wrong"),

    # Partial failure
    (r"\balmost\b", 0.4, "partial_almost"),
    (r"\bclose but\b", 0.5, "partial_close_but"),
    (r"\bnot quite\b", 0.6, "partial_not_quite"),
    (r"\bmissing\b", 0.5, "partial_missing"),
]


def detect_outcome(user_message: str) -> OutcomeResult:
    """
    Detect outcome type from a user message.

    Args:
        user_message: The user's response after an injection

    Returns:
        OutcomeResult with outcome_type, confidence, and signals
    """
    if not user_message or not user_message.strip():
        return OutcomeResult(
            outcome_type=OutcomeType.UNKNOWN.value,
            confidence=0.0,
            signals=["empty_message"]
        )

    # Normalize message for matching
    message_lower = user_message.lower().strip()

    success_score = 0.0
    failure_score = 0.0
    success_signals = []
    failure_signals = []
    success_matches = []
    failure_matches = []

    # Check success patterns
    for pattern, confidence, signal_name in SUCCESS_SIGNALS:
        match = re.search(pattern, message_lower, re.IGNORECASE)
        if match:
            success_score = max(success_score, confidence)
            success_signals.append(signal_name)
            success_matches.append(match.group())

    # Check failure patterns
    for pattern, confidence, signal_name in FAILURE_SIGNALS:
        match = re.search(pattern, message_lower, re.IGNORECASE)
        if match:
            failure_score = max(failure_score, confidence)
            failure_signals.append(signal_name)
            failure_matches.append(match.group())

    # Determine outcome
    # Failure signals take precedence (be conservative)
    if failure_score > 0.7 and failure_score > success_score:
        return OutcomeResult(
            outcome_type=OutcomeType.NEGATIVE.value,
            confidence=failure_score,
            signals=failure_signals,
            raw_matches=failure_matches
        )

    if success_score > 0.6 and success_score > failure_score:
        return OutcomeResult(
            outcome_type=OutcomeType.POSITIVE.value,
            confidence=success_score,
            signals=success_signals,
            raw_matches=success_matches
        )

    # Mixed signals or low confidence
    if success_score > 0 or failure_score > 0:
        if success_score > failure_score:
            return OutcomeResult(
                outcome_type=OutcomeType.NEUTRAL.value,
                confidence=success_score,
                signals=success_signals + ["low_confidence"],
                raw_matches=success_matches
            )
        else:
            return OutcomeResult(
                outcome_type=OutcomeType.NEUTRAL.value,
                confidence=failure_score,
                signals=failure_signals + ["low_confidence"],
                raw_matches=failure_matches
            )

    # No signals detected
    return OutcomeResult(
        outcome_type=OutcomeType.UNKNOWN.value,
        confidence=0.0,
        signals=["no_signals_detected"]
    )


def record_outcome_from_message(
    user_message: str,
    session_id: str,
    variant_id: str = None,
    trigger_context: str = "",
    risk_level: str = "",
    area: str = ""
) -> bool:
    """
    Convenience function to detect outcome and record it to HookEvolutionEngine.

    Args:
        user_message: User's response message
        session_id: Session ID for tracking
        variant_id: Optional variant ID (if known)
        trigger_context: What triggered the original injection
        risk_level: Risk level of the original task
        area: Area/domain of the task

    Returns:
        True if outcome was recorded successfully
    """
    result = detect_outcome(user_message)

    # Only record if we have some confidence
    if result.outcome_type == OutcomeType.UNKNOWN.value or result.confidence < 0.4:
        return False

    try:
        from memory.hook_evolution import get_hook_evolution_engine
        engine = get_hook_evolution_engine()

        # If no variant_id provided, get the default UserPromptSubmit variant
        if not variant_id:
            variant, _ = engine.get_active_variant("UserPromptSubmit", session_id)
            variant_id = variant.variant_id if variant else "userpromptsubmit_default"

        return engine.record_outcome(
            variant_id=variant_id,
            session_id=session_id,
            outcome=result.outcome_type,
            signals=result.signals,
            task_completed=(result.outcome_type == OutcomeType.POSITIVE.value),
            confidence=result.confidence,
            trigger_context=trigger_context,
            risk_level=risk_level,
            area=area
        )
    except ImportError:
        return False
    except Exception:
        return False


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python outcome_detector.py <user_message>")
        print("")
        print("Examples:")
        print('  python outcome_detector.py "perfect, that worked!"')
        print('  python outcome_detector.py "that didn\'t work, try again"')
        print('  python outcome_detector.py "ok"')
        sys.exit(1)

    message = " ".join(sys.argv[1:])
    result = detect_outcome(message)

    print(f"Message: {message}")
    print(f"Outcome: {result.outcome_type}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Signals: {', '.join(result.signals)}")
    if result.raw_matches:
        print(f"Matches: {result.raw_matches}")
