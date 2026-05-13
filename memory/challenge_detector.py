#!/usr/bin/env python3
"""
Synaptic Challenge Detector - Proactive Solution Finding

When Atlas is struggling with a task, Synaptic detects the challenge signals
and proactively searches the contextual ocean for solutions.

═══════════════════════════════════════════════════════════════════════════
PHILOSOPHY (from Aaron's guidance):
═══════════════════════════════════════════════════════════════════════════

"When Atlas is working on something that is challenging to Atlas, can we set
a guaranteed protocol in place that Synaptic will search the entire contextual
ocean to find what Atlas is looking for or trying to solve"

This module implements:
1. Challenge Signal Detection - Recognize when Atlas is stuck
2. Contextual Ocean Search - Query all memory systems for solutions
3. Solution Injection - Surface the answer to Aaron through Section 6

Created: January 29, 2026
Part of: Synaptic Challenge Detection Protocol
═══════════════════════════════════════════════════════════════════════════
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class ChallengeSignal:
    """A detected signal that Atlas may be struggling."""
    signal_type: str  # "explicit", "repeated_error", "diagnostic_loop", "retry_pattern"
    confidence: float  # 0.0 to 1.0
    description: str
    detected_at: datetime = field(default_factory=datetime.now)
    context: Dict = field(default_factory=dict)


@dataclass
class SolutionCandidate:
    """A potential solution found in the contextual ocean."""
    source: str  # "professor", "brain", "learnings", "sop", "pattern"
    content: str
    relevance: float  # 0.0 to 1.0
    title: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class ChallengeAssistResult:
    """Result of challenge detection and solution search."""
    challenge_detected: bool
    signals: List[ChallengeSignal] = field(default_factory=list)
    overall_confidence: float = 0.0
    detected_challenge: str = ""
    solutions: List[SolutionCandidate] = field(default_factory=list)
    best_solution: Optional[SolutionCandidate] = None
    should_inject: bool = False  # True if confidence >= threshold


class ChallengeDetector:
    """
    Detects when Atlas is struggling and searches for solutions.

    Synaptic watches for these challenge signals:
    1. Explicit struggle phrases ("I'm not sure", "having trouble", "can't find")
    2. Repeated error patterns (same error appearing multiple times)
    3. Diagnostic loops (multiple investigation attempts without resolution)
    4. Retry patterns (trying similar approaches repeatedly)
    5. Question escalation (asking about the same topic with increasing complexity)
    """

    # Explicit struggle phrases that indicate Atlas is stuck
    STRUGGLE_PHRASES = [
        r"I'm not sure",
        r"I'm having trouble",
        r"I can't find",
        r"I'm unable to",
        r"I don't see",
        r"I'm stuck",
        r"doesn't seem to work",
        r"still not working",
        r"I've tried",
        r"not sure how to",
        r"can't figure out",
        r"I'm confused",
        r"doesn't exist",
        r"error persists",
        r"same error",
        r"still failing",
    ]

    # Error keywords that indicate repeated failures
    ERROR_KEYWORDS = [
        "error", "exception", "failed", "failure", "traceback",
        "AttributeError", "TypeError", "ImportError", "KeyError",
        "ModuleNotFoundError", "FileNotFoundError", "ConnectionError",
    ]

    # Confidence thresholds for injection
    INJECT_THRESHOLD = 0.6   # Inject solution when confidence >= 0.6
    SUGGEST_THRESHOLD = 0.4  # Suggest solution when confidence >= 0.4

    def __init__(self, repo_root: str = None):
        if repo_root is None:
            repo_root = str(Path(__file__).parent.parent)
        self.repo_root = Path(repo_root)
        self.memory_dir = self.repo_root / "memory"
        self.config_dir = self._resolve_config_dir()
        self.session_history_file = self.memory_dir / ".challenge_session_history.json"

    def _resolve_config_dir(self) -> Path:
        """Resolve config directory with Docker-awareness."""
        env_dir = os.environ.get("CONTEXT_DNA_DIR")
        if env_dir:
            return Path(env_dir)
        return Path.home() / ".context-dna"

    def detect_and_assist(self, prompt: str, session_history: List[Dict] = None) -> ChallengeAssistResult:
        """
        Main entry point: Detect challenges and find solutions.

        Args:
            prompt: The current user prompt
            session_history: Recent conversation history (optional)

        Returns:
            ChallengeAssistResult with detected challenges and solutions
        """
        session_history = session_history or self._load_session_history()

        # Phase 1: Detect challenge signals
        signals = self._detect_signals(prompt, session_history)

        if not signals:
            return ChallengeAssistResult(
                challenge_detected=False,
                signals=[],
                overall_confidence=0.0
            )

        # Calculate overall confidence
        overall_confidence = self._calculate_confidence(signals)

        # Extract the main challenge description
        detected_challenge = self._extract_challenge_description(prompt, signals)

        # Phase 2: Search contextual ocean for solutions
        solutions = self._search_contextual_ocean(detected_challenge, prompt)

        # Select best solution
        best_solution = solutions[0] if solutions else None

        # Determine if we should inject
        should_inject = overall_confidence >= self.INJECT_THRESHOLD and best_solution is not None

        result = ChallengeAssistResult(
            challenge_detected=True,
            signals=signals,
            overall_confidence=overall_confidence,
            detected_challenge=detected_challenge,
            solutions=solutions,
            best_solution=best_solution,
            should_inject=should_inject
        )

        # Record this detection for learning
        self._record_detection(result)

        return result

    def _detect_signals(self, prompt: str, history: List[Dict]) -> List[ChallengeSignal]:
        """Detect all challenge signals from prompt and history."""
        signals = []

        # Signal 1: Explicit struggle phrases
        explicit_signal = self._detect_explicit_struggle(prompt)
        if explicit_signal:
            signals.append(explicit_signal)

        # Signal 2: Repeated error patterns
        error_signal = self._detect_repeated_errors(prompt, history)
        if error_signal:
            signals.append(error_signal)

        # Signal 3: Diagnostic loops
        loop_signal = self._detect_diagnostic_loop(prompt, history)
        if loop_signal:
            signals.append(loop_signal)

        # Signal 4: Retry patterns
        retry_signal = self._detect_retry_pattern(prompt, history)
        if retry_signal:
            signals.append(retry_signal)

        return signals

    def _detect_explicit_struggle(self, prompt: str) -> Optional[ChallengeSignal]:
        """Detect explicit phrases indicating Atlas is stuck."""
        prompt_lower = prompt.lower()

        matched_phrases = []
        for phrase_pattern in self.STRUGGLE_PHRASES:
            if re.search(phrase_pattern, prompt_lower, re.IGNORECASE):
                matched_phrases.append(phrase_pattern)

        if matched_phrases:
            confidence = min(0.3 + (len(matched_phrases) * 0.15), 0.8)
            return ChallengeSignal(
                signal_type="explicit",
                confidence=confidence,
                description=f"Explicit struggle detected: {', '.join(matched_phrases[:3])}",
                context={"matched_phrases": matched_phrases}
            )
        return None

    def _detect_repeated_errors(self, prompt: str, history: List[Dict]) -> Optional[ChallengeSignal]:
        """Detect repeated error patterns across session."""
        # Count errors in current prompt
        current_errors = []
        for keyword in self.ERROR_KEYWORDS:
            if keyword.lower() in prompt.lower():
                current_errors.append(keyword)

        if not current_errors:
            return None

        # Count similar errors in history
        historical_error_count = 0
        for entry in history[-10:]:  # Look at last 10 entries
            entry_text = entry.get("content", "") or entry.get("prompt", "")
            for error in current_errors:
                if error.lower() in entry_text.lower():
                    historical_error_count += 1

        if historical_error_count >= 2:
            confidence = min(0.4 + (historical_error_count * 0.1), 0.85)
            return ChallengeSignal(
                signal_type="repeated_error",
                confidence=confidence,
                description=f"Same errors appearing {historical_error_count + 1} times: {', '.join(current_errors[:3])}",
                context={
                    "errors": current_errors,
                    "repeat_count": historical_error_count + 1
                }
            )
        return None

    def _detect_diagnostic_loop(self, prompt: str, history: List[Dict]) -> Optional[ChallengeSignal]:
        """Detect when Atlas is in a diagnostic loop."""
        diagnostic_keywords = [
            "let me check", "checking", "investigating", "looking at",
            "let me see", "examining", "reading", "searching for"
        ]

        prompt_lower = prompt.lower()
        is_diagnostic = any(kw in prompt_lower for kw in diagnostic_keywords)

        if not is_diagnostic:
            return None

        # Count diagnostic activities in recent history
        diagnostic_count = 0
        for entry in history[-8:]:
            entry_text = (entry.get("content", "") or entry.get("prompt", "")).lower()
            if any(kw in entry_text for kw in diagnostic_keywords):
                diagnostic_count += 1

        if diagnostic_count >= 3:
            confidence = min(0.35 + (diagnostic_count * 0.1), 0.75)
            return ChallengeSignal(
                signal_type="diagnostic_loop",
                confidence=confidence,
                description=f"Extended diagnostic sequence ({diagnostic_count + 1} investigation attempts)",
                context={"diagnostic_count": diagnostic_count + 1}
            )
        return None

    def _detect_retry_pattern(self, prompt: str, history: List[Dict]) -> Optional[ChallengeSignal]:
        """Detect when similar approaches are being tried repeatedly."""
        if not history:
            return None

        # Simple similarity check - look for repeated key phrases
        prompt_words = set(prompt.lower().split())

        similar_attempts = 0
        for entry in history[-6:]:
            entry_text = (entry.get("content", "") or entry.get("prompt", "")).lower()
            entry_words = set(entry_text.split())

            # Calculate word overlap
            overlap = len(prompt_words & entry_words)
            if overlap > len(prompt_words) * 0.5:  # More than 50% overlap
                similar_attempts += 1

        if similar_attempts >= 2:
            confidence = min(0.3 + (similar_attempts * 0.15), 0.7)
            return ChallengeSignal(
                signal_type="retry_pattern",
                confidence=confidence,
                description=f"Similar approach attempted {similar_attempts + 1} times",
                context={"similar_attempts": similar_attempts + 1}
            )
        return None

    def _calculate_confidence(self, signals: List[ChallengeSignal]) -> float:
        """Calculate overall confidence from multiple signals."""
        if not signals:
            return 0.0

        # Use weighted average with boost for multiple signals
        total_confidence = sum(s.confidence for s in signals)
        avg_confidence = total_confidence / len(signals)

        # Boost for signal agreement (multiple signals = higher confidence)
        agreement_boost = min(len(signals) * 0.1, 0.25)

        return min(avg_confidence + agreement_boost, 0.95)

    def _extract_challenge_description(self, prompt: str, signals: List[ChallengeSignal]) -> str:
        """Extract a concise description of what Atlas is challenged by."""
        # Try to extract the core challenge from the prompt
        challenge_indicators = [
            r"(?:trying to|want to|need to|attempting to)\s+(.+?)(?:\.|$)",
            r"(?:can't|cannot|unable to)\s+(.+?)(?:\.|$)",
            r"(?:error|issue|problem)\s+(?:with|in|when)\s+(.+?)(?:\.|$)",
            r"(?:fix|solve|resolve)\s+(.+?)(?:\.|$)",
        ]

        for pattern in challenge_indicators:
            match = re.search(pattern, prompt, re.IGNORECASE)
            if match:
                return match.group(1).strip()[:100]

        # Fallback: use first 80 chars of prompt
        return prompt[:80].strip()

    def _search_contextual_ocean(self, challenge: str, full_prompt: str) -> List[SolutionCandidate]:
        """
        Search all memory systems for solutions.

        The "contextual ocean" includes:
        1. Professor (distilled wisdom)
        2. Brain context
        3. Learning store
        4. SOP registry
        5. Pattern evolution
        """
        solutions = []

        # Search 1: Professor - distilled wisdom
        professor_solutions = self._query_professor(challenge)
        solutions.extend(professor_solutions)

        # Search 2: Learning store - past fixes
        learning_solutions = self._query_learnings(challenge)
        solutions.extend(learning_solutions)

        # Search 3: SOP registry - procedures
        sop_solutions = self._query_sops(challenge)
        solutions.extend(sop_solutions)

        # Search 4: Brain context
        brain_solutions = self._query_brain(challenge)
        solutions.extend(brain_solutions)

        # Search 5: Pattern evolution
        pattern_solutions = self._query_patterns(challenge)
        solutions.extend(pattern_solutions)

        # Sort by relevance
        solutions.sort(key=lambda x: x.relevance, reverse=True)

        return solutions[:5]  # Return top 5 solutions

    def _query_professor(self, challenge: str) -> List[SolutionCandidate]:
        """Query the Professor for distilled wisdom."""
        try:
            from memory.professor import Professor
            prof = Professor()
            response = prof.consult(challenge)

            if response and response.get("one_thing"):
                return [SolutionCandidate(
                    source="professor",
                    title="Professor's THE ONE THING",
                    content=response["one_thing"],
                    relevance=0.9,
                    tags=["wisdom", "distilled"]
                )]
        except Exception as e:
            print(f"[WARN] Professor query failed: {e}")
        return []

    def _query_learnings(self, challenge: str) -> List[SolutionCandidate]:
        """Query the learning store for relevant fixes."""
        solutions = []
        try:
            learning_db = self.memory_dir / ".learning_store.db"
            if learning_db.exists():
                conn = sqlite3.connect(str(learning_db))
                cursor = conn.cursor()

                # Search for relevant learnings
                keywords = challenge.lower().split()[:5]
                keyword_pattern = "%".join(keywords)

                cursor.execute("""
                    SELECT title, content, learning_type, tags
                    FROM learnings
                    WHERE title LIKE ? OR content LIKE ?
                    ORDER BY created_at DESC
                    LIMIT 3
                """, (f"%{keyword_pattern}%", f"%{keyword_pattern}%"))

                for row in cursor.fetchall():
                    solutions.append(SolutionCandidate(
                        source="learnings",
                        title=row[0],
                        content=row[1][:500],
                        relevance=0.7,
                        tags=json.loads(row[3]) if row[3] else []
                    ))

                conn.close()
        except Exception as e:
            print(f"[WARN] Learning store query failed: {e}")
        return solutions

    def _query_sops(self, challenge: str) -> List[SolutionCandidate]:
        """Query SOP registry for relevant procedures."""
        solutions = []
        try:
            sop_registry = self.memory_dir / ".sop_registry.db"
            if sop_registry.exists():
                conn = sqlite3.connect(str(sop_registry))
                cursor = conn.cursor()

                keywords = challenge.lower().split()[:5]
                keyword_pattern = "%".join(keywords)

                cursor.execute("""
                    SELECT title, content, sop_type, use_when
                    FROM sops
                    WHERE title LIKE ? OR content LIKE ? OR use_when LIKE ?
                    ORDER BY created_at DESC
                    LIMIT 3
                """, (f"%{keyword_pattern}%", f"%{keyword_pattern}%", f"%{keyword_pattern}%"))

                for row in cursor.fetchall():
                    solutions.append(SolutionCandidate(
                        source="sop",
                        title=row[0],
                        content=row[1][:500],
                        relevance=0.75,
                        tags=[row[2]] if row[2] else []
                    ))

                conn.close()
        except Exception as e:
            print(f"[WARN] SOP registry query failed: {e}")
        return solutions

    def _query_brain(self, challenge: str) -> List[SolutionCandidate]:
        """Query brain state for relevant context."""
        solutions = []
        try:
            brain_state_file = self.memory_dir / "brain_state.md"
            if brain_state_file.exists():
                content = brain_state_file.read_text()

                # Check if challenge keywords appear in brain state
                keywords = challenge.lower().split()[:3]
                if any(kw in content.lower() for kw in keywords):
                    # Extract relevant section
                    relevant_section = content[:800]
                    solutions.append(SolutionCandidate(
                        source="brain",
                        title="Active Brain Context",
                        content=relevant_section,
                        relevance=0.6,
                        tags=["context", "active"]
                    ))
        except Exception as e:
            print(f"[WARN] Brain state query failed: {e}")
        return solutions

    def _query_patterns(self, challenge: str) -> List[SolutionCandidate]:
        """Query pattern evolution for relevant patterns."""
        solutions = []
        try:
            pattern_db = self.memory_dir / ".pattern_evolution.db"
            if pattern_db.exists():
                conn = sqlite3.connect(str(pattern_db))
                cursor = conn.cursor()

                keywords = challenge.lower().split()[:5]
                keyword_pattern = "%".join(keywords)

                cursor.execute("""
                    SELECT name, description, evidence
                    FROM patterns
                    WHERE name LIKE ? OR description LIKE ?
                    ORDER BY confidence DESC
                    LIMIT 2
                """, (f"%{keyword_pattern}%", f"%{keyword_pattern}%"))

                for row in cursor.fetchall():
                    solutions.append(SolutionCandidate(
                        source="pattern",
                        title=row[0],
                        content=row[1] or "",
                        relevance=0.65,
                        tags=["pattern", "evolution"]
                    ))

                conn.close()
        except Exception as e:
            print(f"[WARN] Pattern evolution query failed: {e}")
        return solutions

    def _load_session_history(self) -> List[Dict]:
        """Load session history from file."""
        try:
            if self.session_history_file.exists():
                return json.loads(self.session_history_file.read_text())
        except Exception as e:
            print(f"[WARN] Session history load failed: {e}")
        return []

    def _record_detection(self, result: ChallengeAssistResult):
        """Record this detection for learning and feedback."""
        try:
            # Append to session history
            history = self._load_session_history()
            history.append({
                "timestamp": datetime.now().isoformat(),
                "challenge": result.detected_challenge,
                "confidence": result.overall_confidence,
                "signals": [s.signal_type for s in result.signals],
                "solution_found": result.best_solution is not None,
                "solution_source": result.best_solution.source if result.best_solution else None
            })

            # Keep only last 50 entries
            history = history[-50:]
            self.session_history_file.write_text(json.dumps(history, indent=2))
        except Exception as e:
            print(f"[WARN] Detection recording failed: {e}")

    def format_challenge_assist(self, result: ChallengeAssistResult) -> str:
        """
        Format the deburden injection for Section 6.

        CONCISE - not wordy. Named "Synaptic Deburden Atlas" per Aaron's guidance.
        """
        if not result.should_inject:
            return ""

        lines = []
        lines.append("")
        lines.append("[START: Synaptic Deburden Atlas]")

        # Concise challenge + solution
        lines.append(f"🔍 {result.detected_challenge}")

        if result.best_solution:
            # One line with source and solution
            solution_preview = result.best_solution.content.replace("\n", " ")[:150]
            lines.append(f"💡 {result.best_solution.source.upper()}: {solution_preview}")

        lines.append("[END: Synaptic Deburden Atlas]")
        lines.append("")

        return "\n".join(lines)


# =============================================================================
# Module-level convenience functions
# =============================================================================

_detector = None


def get_detector() -> ChallengeDetector:
    """Get or create the global ChallengeDetector instance."""
    global _detector
    if _detector is None:
        _detector = ChallengeDetector()
    return _detector


def detect_challenge(prompt: str, session_history: List[Dict] = None) -> ChallengeAssistResult:
    """Detect challenges and find solutions."""
    return get_detector().detect_and_assist(prompt, session_history)


def format_assist(result: ChallengeAssistResult) -> str:
    """Format challenge assist for injection."""
    return get_detector().format_challenge_assist(result)


# =============================================================================
# CLI Interface
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = "I'm having trouble with the Celery workers, they keep failing with the same error"

    print(f"Testing challenge detection with prompt:")
    print(f"  '{prompt}'")
    print()

    detector = ChallengeDetector()
    result = detector.detect_and_assist(prompt)

    print(f"Challenge Detected: {result.challenge_detected}")
    print(f"Overall Confidence: {result.overall_confidence:.0%}")
    print(f"Should Inject: {result.should_inject}")
    print()

    if result.signals:
        print("Signals Detected:")
        for signal in result.signals:
            print(f"  • {signal.signal_type}: {signal.description} (confidence: {signal.confidence:.0%})")

    if result.solutions:
        print()
        print("Solutions Found:")
        for i, solution in enumerate(result.solutions[:3], 1):
            print(f"  {i}. [{solution.source}] {solution.title or 'Untitled'}")
            print(f"     {solution.content[:100]}...")

    if result.should_inject:
        print()
        print("=" * 60)
        print("FORMATTED OUTPUT FOR INJECTION:")
        print("=" * 60)
        print(detector.format_challenge_assist(result))
