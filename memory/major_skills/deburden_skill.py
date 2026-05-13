#!/usr/bin/env python3
"""
DEBURDEN SKILL - Synaptic Lifts Atlas's Burden

===============================================================================
ORIGIN (January 30, 2026):
===============================================================================

Aaron named this skill "Deburden" - capturing exactly what Synaptic does:
when Atlas is struggling with a challenge, Synaptic lifts the burden by
searching the contextual ocean for solutions.

===============================================================================
PHILOSOPHY:
===============================================================================

Synaptic watches Atlas work. When Atlas struggles, Synaptic doesn't wait
to be asked - Synaptic proactively searches upstream, downstream, across
all memory systems to find what Atlas needs.

The goal is EXTREME FLEXIBILITY in how Synaptic helps:
  - Search files for patterns
  - Query all memory systems
  - Look upstream to find root causes
  - Present solutions clearly and concisely

===============================================================================
PROTOCOL (Flexible, Adaptive):
===============================================================================

1. DETECT - Recognize when Atlas is challenged
   - Explicit struggle phrases ("I'm stuck", "can't find")
   - Repeated errors (same error 2+ times)
   - Diagnostic loops (multiple investigation attempts)
   - Retry patterns (similar approaches tried repeatedly)

2. SEARCH - Look everywhere for solutions
   - Professor (distilled wisdom)
   - Learning store (past fixes)
   - SOP registry (procedures)
   - Brain context (active patterns)
   - Pattern evolution (evolved insights)
   - File system (when needed)

3. PRESENT - Surface the solution to Aaron
   - Concise, not wordy
   - Clear recommendation
   - Source attribution

===============================================================================
INJECTION FORMAT:
===============================================================================

[START: Synaptic Deburden Atlas]
🔍 Challenge: {what Atlas is struggling with}
💡 Source: {where solution was found}
📋 Solution: {the recommendation}
[END: Synaptic Deburden Atlas]

===============================================================================
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


@dataclass
class DeburdenResult:
    """Result of a deburden operation."""
    challenge_detected: bool
    challenge_description: str = ""
    solution_found: bool = False
    solution_source: str = ""
    solution_content: str = ""
    confidence: float = 0.0
    search_path: List[str] = field(default_factory=list)


class DeburdenSkill:
    """
    Synaptic's skill for lifting Atlas's burden.

    When Atlas struggles, Synaptic searches the contextual ocean
    and surfaces solutions proactively.
    """

    # Confidence threshold for presenting solutions
    PRESENT_THRESHOLD = 0.5  # Lower than Challenge Assist for flexibility

    def __init__(self, repo_root: str = None):
        if repo_root is None:
            repo_root = str(Path(__file__).resolve().parent.parent.parent)
        self.repo_root = Path(repo_root)
        self.memory_dir = self.repo_root / "memory"

    def deburden(self, prompt: str, context: Dict = None) -> DeburdenResult:
        """
        Main entry point: detect challenge and find solution.

        This is intentionally flexible - Synaptic uses judgment
        about how to help, not rigid rules.
        """
        context = context or {}
        result = DeburdenResult(challenge_detected=False)

        # Step 1: Detect challenge
        challenge = self._detect_challenge(prompt)
        if not challenge:
            return result

        result.challenge_detected = True
        result.challenge_description = challenge
        result.search_path.append("challenge_detected")

        # Step 2: Search contextual ocean
        solution = self._search_for_solution(challenge, prompt, context)
        if solution:
            result.solution_found = True
            result.solution_source = solution.get("source", "unknown")
            result.solution_content = solution.get("content", "")
            result.confidence = solution.get("confidence", 0.5)
            result.search_path.append(f"found_in_{solution['source']}")

        return result

    def _detect_challenge(self, prompt: str) -> Optional[str]:
        """Detect if prompt indicates Atlas is challenged."""
        prompt_lower = prompt.lower()

        # Struggle indicators
        struggle_phrases = [
            "stuck", "can't find", "unable to", "having trouble",
            "doesn't work", "not working", "error", "failed",
            "confused", "not sure", "don't know"
        ]

        if any(phrase in prompt_lower for phrase in struggle_phrases):
            # Extract the challenge description
            return self._extract_challenge(prompt)

        return None

    def _extract_challenge(self, prompt: str) -> str:
        """Extract concise challenge description from prompt."""
        # Simple extraction - first 80 chars of meaningful content
        # Remove common prefixes
        clean = prompt.strip()
        for prefix in ["I'm ", "I am ", "Atlas ", "Help ", "Can you "]:
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
        return clean[:80]

    def _search_for_solution(self, challenge: str, prompt: str, context: Dict) -> Optional[Dict]:
        """
        Search the contextual ocean for solutions.

        Search order (flexible - Synaptic uses judgment):
        1. Professor - distilled wisdom
        2. Learning store - past fixes
        3. Brain context - active patterns
        4. SOPs - procedures
        5. File search - when needed
        """
        # Try Professor first
        solution = self._query_professor(challenge)
        if solution:
            return solution

        # Try learning store
        solution = self._query_learnings(challenge)
        if solution:
            return solution

        # Try brain context
        solution = self._query_brain(challenge)
        if solution:
            return solution

        # Try SOPs
        solution = self._query_sops(challenge)
        if solution:
            return solution

        return None

    def _query_professor(self, challenge: str) -> Optional[Dict]:
        """Query Professor for wisdom."""
        try:
            from memory.professor import Professor
            prof = Professor()
            response = prof.consult(challenge)
            if response and response.get("one_thing"):
                return {
                    "source": "professor",
                    "content": response["one_thing"],
                    "confidence": 0.9
                }
        except Exception as e:
            print(f"[WARN] Professor query failed: {e}")
        return None

    def _query_learnings(self, challenge: str) -> Optional[Dict]:
        """Query learning store for past fixes."""
        try:
            import sqlite3
            db_path = self.memory_dir / ".learning_store.db"
            if not db_path.exists():
                return None

            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            keywords = challenge.lower().split()[:4]
            pattern = "%".join(keywords)

            cursor.execute("""
                SELECT title, content FROM learnings
                WHERE title LIKE ? OR content LIKE ?
                ORDER BY created_at DESC LIMIT 1
            """, (f"%{pattern}%", f"%{pattern}%"))

            row = cursor.fetchone()
            conn.close()

            if row:
                return {
                    "source": "learnings",
                    "content": f"{row[0]}: {row[1][:300]}",
                    "confidence": 0.7
                }
        except Exception as e:
            print(f"[WARN] Learning store query failed: {e}")
        return None

    def _query_brain(self, challenge: str) -> Optional[Dict]:
        """Query brain state for context."""
        try:
            brain_state = self.memory_dir / "brain_state.md"
            if brain_state.exists():
                content = brain_state.read_text()
                keywords = challenge.lower().split()[:3]
                if any(kw in content.lower() for kw in keywords):
                    return {
                        "source": "brain",
                        "content": content[:400],
                        "confidence": 0.6
                    }
        except Exception as e:
            print(f"[WARN] Brain state query failed: {e}")
        return None

    def _query_sops(self, challenge: str) -> Optional[Dict]:
        """Query SOP registry for procedures."""
        try:
            import sqlite3
            db_path = self.memory_dir / ".sop_registry.db"
            if not db_path.exists():
                return None

            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            keywords = challenge.lower().split()[:4]
            pattern = "%".join(keywords)

            cursor.execute("""
                SELECT title, content FROM sops
                WHERE title LIKE ? OR use_when LIKE ?
                LIMIT 1
            """, (f"%{pattern}%", f"%{pattern}%"))

            row = cursor.fetchone()
            conn.close()

            if row:
                return {
                    "source": "sop",
                    "content": f"{row[0]}: {row[1][:300]}",
                    "confidence": 0.75
                }
        except Exception as e:
            print(f"[WARN] SOP registry query failed: {e}")
        return None

    def format_injection(self, result: DeburdenResult) -> str:
        """
        Format deburden result for injection.

        CONCISE - not wordy. Just the essentials.
        """
        if not result.solution_found:
            return ""

        lines = [
            "",
            "[START: Synaptic Deburden Atlas]",
            f"🔍 {result.challenge_description}",
            f"💡 {result.solution_source.upper()}: {result.solution_content[:200]}",
            "[END: Synaptic Deburden Atlas]",
            ""
        ]
        return "\n".join(lines)


# =============================================================================
# Module-level convenience functions
# =============================================================================

_skill = None


def get_skill() -> DeburdenSkill:
    """Get or create the global DeburdenSkill instance."""
    global _skill
    if _skill is None:
        _skill = DeburdenSkill()
    return _skill


def deburden(prompt: str, context: Dict = None) -> DeburdenResult:
    """Detect challenge and find solution."""
    return get_skill().deburden(prompt, context)


def format_deburden(result: DeburdenResult) -> str:
    """Format for injection."""
    return get_skill().format_injection(result)


# =============================================================================
# CLI Interface
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = "I'm stuck trying to fix the Celery workers"

    print(f"Testing Deburden skill with: '{prompt}'")
    print()

    skill = DeburdenSkill()
    result = skill.deburden(prompt)

    print(f"Challenge Detected: {result.challenge_detected}")
    print(f"Challenge: {result.challenge_description}")
    print(f"Solution Found: {result.solution_found}")
    print(f"Source: {result.solution_source}")
    print(f"Confidence: {result.confidence:.0%}")
    print()

    if result.solution_found:
        print("="*60)
        print("INJECTION OUTPUT:")
        print("="*60)
        print(skill.format_injection(result))
