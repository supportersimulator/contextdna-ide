#!/usr/bin/env python3
"""
LLM-Powered Semantic Success Analyzer

This module provides semantic understanding of success indicators using
local LLMs (Ollama) or cloud LLMs (OpenAI/Anthropic).

The DNA principle: Understanding context is key. "Great, another error"
is sarcasm, not success. "Finally got it" implies previous failures.
LLM semantic analysis catches what regex cannot.

ADDITIVE to existing regex patterns - runs AFTER regex detection
to validate/boost confidence of potential successes and catch missed ones.

Uses existing local_llm_analyzer.py infrastructure (Ollama client).

Usage:
    from memory.llm_success_analyzer import LLMSuccessAnalyzer

    analyzer = LLMSuccessAnalyzer()

    # Analyze context around a potential success
    modifier = analyzer.analyze_context(entries, potential_success)

    # Find successes that regex missed
    implicit_successes = analyzer.detect_implicit_successes(entries)
"""

import json
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

# Import the existing LLM client
try:
    from memory.local_llm_analyzer import llm_client, LocalLLMClient
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False
    llm_client = None


@dataclass
class SemanticAnalysisResult:
    """Result of semantic success analysis."""
    is_success: bool
    confidence_modifier: float  # -0.3 to +0.3
    reasoning: str
    detected_sentiment: str  # 'positive', 'negative', 'neutral', 'sarcastic'


@dataclass
class ImplicitSuccess:
    """A success detected via semantic analysis that regex missed."""
    task: str
    evidence: str
    confidence: float
    timestamp: str
    entry_index: int


# Analysis prompts
SUCCESS_CONTEXT_PROMPT = """Analyze this conversation context to determine if it represents a genuine success.

Context (entries leading up to potential success):
{context}

Potential success indicator:
"{success_text}"

## What to Consider

Think through whatever seems relevant to you:

- **Genuine or sarcasm?** Does this sound like real satisfaction, or is there frustration/sarcasm underneath?
- **Context support?** Does the conversation history before this moment support it being a real success?
- **Problem resolution?** Were there errors or problems mentioned before that now appear resolved?
- **Evidence of completion?** Are there confirmations, exit codes, logs, or other proof the task finished?
- **Sentiment?** What's the overall emotional tone - positive, negative, neutral, or mixed?
- **Confidence?** How confident are you that this is genuine success? What would make you more confident?
- **Uncertainty?** What details are unclear or ambiguous about this success claim?

## Your Analysis

Share your thinking in whatever way makes sense to you. Both JSON-formatted and natural language analyses are useful.
(Your analysis will be tracked for system learning about success patterns.)
"""

IMPLICIT_SUCCESS_PROMPT = """Analyze this development work log and identify any IMPLICIT successes
that may not have explicit success keywords but indicate tasks were completed successfully.

Work Log:
{work_log}

## What to Look For

Consider patterns that suggest completion:

- **Resolution sequences**: "Finally got X working" indicates previous failures now resolved
- **Task transitions**: Moving to the next task implies the previous one succeeded
- **Verification actions**: Testing/verifying behavior suggests implementation is complete
- **Commitment indicators**: Committing/pushing changes means code was deemed ready
- **Confidence indicators**: "That should do it" or "Let's see" followed by silence suggests success
- **Success implications**: What actions or statements could only happen after success?
- **Context clues**: What work log entries suggest preceding tasks were accomplished?

## Your Analysis

Identify implicit successes you detect. For each one, explain:
1. What task was accomplished
2. The specific text or pattern that indicates it
3. Your confidence level (high/medium/low)
4. Why you believe this represents genuine completion

Share your findings in whatever format makes sense to you.
(Patterns you identify will help calibrate implicit success detection for future analysis.)
"""


class LLMSuccessAnalyzer:
    """
    Semantic analysis layer for success detection.

    Uses LLM to understand context and sentiment that regex cannot capture.
    """

    def __init__(self, client: 'LocalLLMClient' = None):
        """
        Initialize analyzer.

        Args:
            client: LLM client to use. If None, uses global llm_client.
        """
        self.client = client or llm_client
        self.available = LLM_AVAILABLE and self.client and self.client.available

    def analyze_context(
        self,
        entries: List[Dict],
        success_text: str,
        success_index: int = -1,
        context_window: int = 5
    ) -> SemanticAnalysisResult:
        """
        Analyze context around a potential success.

        Uses LLM to determine if the success indicator is genuine
        or sarcasm/frustration.

        Args:
            entries: All work log entries
            success_text: The text that triggered success detection
            success_index: Index of the success entry (-1 for last)
            context_window: Number of entries before/after to include

        Returns:
            SemanticAnalysisResult with confidence modifier
        """
        if not self.available:
            # Fallback: assume genuine with no modifier
            return SemanticAnalysisResult(
                is_success=True,
                confidence_modifier=0.0,
                reasoning="LLM not available - defaulting to regex result",
                detected_sentiment="unknown"
            )

        # Build context window
        if success_index < 0:
            success_index = len(entries) + success_index

        start_idx = max(0, success_index - context_window)
        end_idx = min(len(entries), success_index + context_window + 1)
        context_entries = entries[start_idx:end_idx]

        # Format context for LLM
        context_text = self._format_entries(context_entries)

        prompt = SUCCESS_CONTEXT_PROMPT.format(
            context=context_text,
            success_text=success_text
        )

        # Get LLM response
        response = self.client.generate(prompt, max_tokens=500)

        if not response:
            return SemanticAnalysisResult(
                is_success=True,
                confidence_modifier=0.0,
                reasoning="LLM generation failed - defaulting to regex result",
                detected_sentiment="unknown"
            )

        # Parse response (handles both JSON and natural language)
        try:
            # Try JSON parsing first
            result = self._extract_json(response)
            if result:  # Successfully parsed JSON
                return SemanticAnalysisResult(
                    is_success=result.get("is_genuine_success", True),
                    confidence_modifier=float(result.get("confidence_modifier", 0.0)),
                    reasoning=result.get("reasoning", "No reasoning provided"),
                    detected_sentiment=result.get("sentiment", "unknown")
                )
        except Exception as e:
            pass  # Fall through to natural language extraction

        # Fallback: extract insights from natural language response
        try:
            return self._parse_context_analysis_natural_language(response)
        except Exception as e:
            return SemanticAnalysisResult(
                is_success=True,
                confidence_modifier=0.0,
                reasoning=f"LLM response could not be parsed: {str(e)[:100]}",
                detected_sentiment="unknown"
            )

    def detect_implicit_successes(
        self,
        entries: List[Dict],
        min_confidence: float = 0.5
    ) -> List[ImplicitSuccess]:
        """
        Find successes that regex patterns missed.

        Uses LLM to understand implicit success indicators.

        Args:
            entries: Work log entries to analyze
            min_confidence: Minimum confidence to include

        Returns:
            List of implicit successes detected
        """
        if not self.available:
            return []

        if not entries:
            return []

        # Format entries for LLM
        work_log_text = self._format_entries(entries)

        prompt = IMPLICIT_SUCCESS_PROMPT.format(work_log=work_log_text)

        response = self.client.generate(prompt, max_tokens=1000)

        if not response:
            return []

        try:
            # Try JSON parsing first
            result = self._extract_json(response)
            if result and "implicit_successes" in result:
                successes = result.get("implicit_successes", [])
                return [
                    ImplicitSuccess(
                        task=s.get("task", "Unknown task"),
                        evidence=s.get("evidence", ""),
                        confidence=float(s.get("confidence", 0.5)),
                        timestamp=self._get_entry_timestamp(entries, s.get("entry_index", 0)),
                        entry_index=s.get("entry_index", 0)
                    )
                    for s in successes
                    if float(s.get("confidence", 0)) >= min_confidence
                ]
        except Exception:
            pass  # Fall through to natural language extraction

        # Fallback: extract successes from natural language response
        try:
            return self._parse_implicit_successes_natural_language(response, entries, min_confidence)
        except Exception:
            return []

    def quick_sentiment_check(self, text: str) -> str:
        """
        Quick sentiment check without full context.

        Returns: 'positive', 'negative', 'neutral', or 'sarcastic'
        """
        # Simple heuristic patterns (fast, no LLM needed)
        text_lower = text.lower()

        # Sarcasm indicators
        sarcasm_patterns = [
            r"great[,.].*another",
            r"perfect[,.].*just what",
            r"wonderful[,.].*more",
            r"oh.*great",
            r"just.*what.*needed",
        ]
        for pattern in sarcasm_patterns:
            if re.search(pattern, text_lower):
                return "sarcastic"

        # Negative indicators
        if re.search(r"\b(frustrat|annoy|hate|ugh|damn|wtf|crap)\b", text_lower):
            return "negative"

        # Positive indicators
        if re.search(r"\b(finally|awesome|great|perfect|excellent|yes!)\b", text_lower):
            return "positive"

        return "neutral"

    def _parse_context_analysis_natural_language(self, response: str) -> SemanticAnalysisResult:
        """
        Extract context analysis from natural language response when JSON parsing fails.

        Looks for key indicators in the LLM's natural language reasoning.
        """
        response_lower = response.lower()

        # Detect sentiment indicators
        sentiment = "neutral"
        if re.search(r"\b(sarcas|fake|not.*genuine|joking|mocking)\b", response_lower):
            sentiment = "sarcastic"
        elif re.search(r"\b(genuine|real|authentic|confirm|valid|success)\b", response_lower):
            sentiment = "positive"
        elif re.search(r"\b(doubt|uncertain|unclear|suspicious|fake)\b", response_lower):
            sentiment = "negative"

        # Detect if LLM thinks this is genuine success
        is_success = not re.search(r"\b(not.*success|fail|sarcas|false|fake)\b", response_lower)

        # Extract confidence modifier from language
        confidence_modifier = 0.0
        if re.search(r"\b(definitely|certainly|clearly|obviously)\b", response_lower):
            confidence_modifier = 0.2
        elif re.search(r"\b(likely|probably|seems|appears)\b", response_lower):
            confidence_modifier = 0.1
        elif re.search(r"\b(uncertain|unclear|ambiguous|suspicious)\b", response_lower):
            confidence_modifier = -0.1
        elif re.search(r"\b(definitely.*not|clearly.*fail|false.*success)\b", response_lower):
            confidence_modifier = -0.2

        # Use first 300 chars as reasoning summary
        reasoning = response[:300] if response else "Natural language analysis completed"

        return SemanticAnalysisResult(
            is_success=is_success,
            confidence_modifier=confidence_modifier,
            reasoning=reasoning,
            detected_sentiment=sentiment
        )

    def _parse_implicit_successes_natural_language(
        self, response: str, entries: List[Dict], min_confidence: float
    ) -> List[ImplicitSuccess]:
        """
        Extract implicit successes from natural language response when JSON parsing fails.

        Looks for task descriptions and confidence indicators in the response.
        """
        successes = []

        # Simple heuristic: look for task-like patterns
        # Lines starting with "- " or "* " are likely task descriptions
        lines = response.split("\n")
        current_task = None

        for line in lines:
            stripped = line.strip()

            # Potential task line
            if stripped.startswith("- ") or stripped.startswith("* "):
                task_desc = stripped[2:].strip()

                # Estimate confidence based on language
                confidence = 0.6  # Default medium confidence
                if any(word in task_desc.lower() for word in ["definitely", "clearly", "obviously"]):
                    confidence = 0.85
                elif any(word in task_desc.lower() for word in ["likely", "probably", "seems"]):
                    confidence = 0.7
                elif any(word in task_desc.lower() for word in ["maybe", "possibly", "uncertain"]):
                    confidence = 0.5

                if confidence >= min_confidence:
                    successes.append(ImplicitSuccess(
                        task=task_desc[:100],
                        evidence=task_desc[:150],
                        confidence=confidence,
                        timestamp=datetime.now().isoformat(),
                        entry_index=0
                    ))

        return successes

    def _format_entries(self, entries: List[Dict]) -> str:
        """Format entries for LLM consumption."""
        lines = []
        for i, entry in enumerate(entries):
            timestamp = entry.get("timestamp", "")[:19]
            entry_type = entry.get("entry_type", "unknown")
            source = entry.get("source", "")
            content = entry.get("content", "")

            line = f"[{i}] [{timestamp}] [{entry_type}]"
            if source:
                line += f" ({source})"
            line += f": {content[:300]}"
            lines.append(line)

        return "\n".join(lines)

    def _extract_json(self, text: str) -> Dict:
        """Extract JSON from LLM response (may have surrounding text)."""
        # Try direct parse first
        try:
            return json.loads(text)
        except Exception as e:
            print(f"[WARN] Direct JSON parse failed: {e}")

        # Find JSON in response
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except Exception as e:
                print(f"[WARN] Simple JSON extraction failed: {e}")

        # Find JSON with arrays
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except Exception as e:
                print(f"[WARN] Complex JSON extraction failed: {e}")

        return {}

    def _get_entry_timestamp(self, entries: List[Dict], index: int) -> str:
        """Get timestamp from entry at index."""
        if 0 <= index < len(entries):
            return entries[index].get("timestamp", datetime.now().isoformat())
        return datetime.now().isoformat()

    def get_status(self) -> Dict:
        """Get analyzer status."""
        return {
            "available": self.available,
            "llm_endpoint": (
                self.client.endpoint.name if self.client and self.client.endpoint else None
            ),
            "quick_sentiment_available": True,  # Always available (heuristic)
        }


def analyze_success_context(
    entries: List[Dict],
    success_text: str,
    success_index: int = -1
) -> Tuple[bool, float]:
    """
    Convenience function to analyze success context.

    Returns:
        (is_genuine, confidence_modifier) tuple
    """
    analyzer = LLMSuccessAnalyzer()
    result = analyzer.analyze_context(entries, success_text, success_index)
    return result.is_success, result.confidence_modifier


# CLI interface
if __name__ == "__main__":
    import sys

    analyzer = LLMSuccessAnalyzer()

    if len(sys.argv) < 2:
        print("LLM Success Analyzer - Semantic success detection")
        print("")
        print("Commands:")
        print("  status                  - Check LLM availability")
        print("  sentiment <text>        - Quick sentiment check")
        print("  analyze                 - Analyze recent work log for implicit successes")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        status = analyzer.get_status()
        print("LLM Success Analyzer Status:")
        print(f"  Available: {status['available']}")
        print(f"  Endpoint: {status['llm_endpoint'] or 'None'}")
        print(f"  Quick sentiment: {status['quick_sentiment_available']}")

    elif cmd == "sentiment":
        if len(sys.argv) < 3:
            print("Usage: sentiment <text>")
            sys.exit(1)
        text = " ".join(sys.argv[2:])
        sentiment = analyzer.quick_sentiment_check(text)
        print(f"Text: {text}")
        print(f"Sentiment: {sentiment}")

    elif cmd == "analyze":
        # Try to load recent work log
        try:
            from memory.architecture_enhancer import work_log
            entries = work_log.get_recent_entries(hours=4)
            print(f"Analyzing {len(entries)} recent entries...")

            successes = analyzer.detect_implicit_successes(entries)
            if successes:
                print(f"\nFound {len(successes)} implicit success(es):")
                for s in successes:
                    print(f"  [{s.confidence:.2f}] {s.task}")
                    print(f"        Evidence: {s.evidence[:80]}...")
            else:
                print("No implicit successes detected")
        except ImportError:
            print("Work log not available")
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
