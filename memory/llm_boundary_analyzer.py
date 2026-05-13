#!/usr/bin/env python3
"""
LLM BOUNDARY ANALYZER - Semantic Project Detection using Local LLM

Uses local LLM (Ollama/MLX) for semantic analysis of prompts to detect
which project the work is about. This is one of the 5 input signals for
Project Boundary Intelligence.

ARCHITECTURE:
- Uses Ollama API for inference (or MLX on Apple Silicon)
- Prompt engineering for project detection
- Confidence scoring based on LLM response quality
- Async support via Celery task queue

Purpose:
The LLM provides semantic understanding that keyword matching cannot:
- "fix the webhook injection" -> Context DNA (even without explicit keyword)
- "update the voice pipeline" -> ersim-voice-stack
- "add authentication" -> ambiguous (needs other signals)

Usage:
    from memory.llm_boundary_analyzer import get_llm_boundary_analyzer

    analyzer = get_llm_boundary_analyzer()

    # Analyze a prompt
    signals = analyzer.analyze_prompt(
        "fix the A/B testing feedback loop for boundary detection",
        known_projects=["context-dna", "ersim-voice-stack", "backend"]
    )
    # Returns: [ProjectSignal(project="context-dna", confidence=0.85, ...)]
"""

import os
import json
import logging
import re
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger('contextdna.llm_boundary')

# =============================================================================
# CONFIGURATION
# =============================================================================

OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5:3b')

# LLM analysis timeout
LLM_TIMEOUT = int(os.environ.get('LLM_TIMEOUT', '30'))

# System prompt for project detection
PROJECT_DETECTION_SYSTEM_PROMPT = """You are a project boundary detector for a software development workspace.
Given a user's task description, determine which project they are most likely working on.

Consider whatever seems relevant to you:

- **What's the semantic meaning?** What is this task actually about?
- **What key technical terms indicate a specific project?** What domain-specific keywords appear?
- **Which projects could this relate to?** What are the most likely candidates?
- **How confident are you?** What would make you more or less confident in your assessment?
- **Are there alternative interpretations?** Could this task relate to multiple projects?
- **What's ambiguous?** What additional information would help resolve uncertainty?

Share your analysis in whatever way makes sense to you. Both JSON-formatted and natural language analyses are equally useful.
Your assessment will help calibrate the project boundary detection system."""


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class ProjectSignal:
    """A signal indicating a specific project (matches boundary_intelligence.py)."""
    project: str
    source: str = "llm"
    confidence: float = 0.5
    keywords: List[str] = None
    weight: float = 0.9  # LLM signals have high weight when confident

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []

    def to_dict(self) -> Dict:
        return asdict(self)


# =============================================================================
# LLM BOUNDARY ANALYZER
# =============================================================================

class LLMBoundaryAnalyzer:
    """
    Use local LLM for semantic project detection.

    Provides semantic understanding beyond simple keyword matching.
    """

    def __init__(self, ollama_url: str = None, model: str = None):
        """
        Initialize LLM analyzer.

        Args:
            ollama_url: Ollama API URL (defaults to OLLAMA_URL env var)
            model: Model to use (defaults to OLLAMA_MODEL env var)
        """
        self.ollama_url = ollama_url or OLLAMA_URL
        self.model = model or OLLAMA_MODEL
        self._healthy = None  # Cached health status

    # =========================================================================
    # CORE ANALYSIS
    # =========================================================================

    def analyze_prompt(
        self,
        prompt: str,
        known_projects: List[str] = None,
        file_context: str = None
    ) -> List[ProjectSignal]:
        """
        Analyze a prompt to detect which project it relates to.

        Args:
            prompt: The user's task/prompt
            known_projects: List of known project names to consider
            file_context: Optional file path for additional context

        Returns:
            List of ProjectSignal objects with confidence scores
        """
        if not self.is_healthy():
            logger.debug("LLM not healthy, skipping analysis")
            return []

        # Build the analysis prompt
        analysis_prompt = self._build_analysis_prompt(prompt, known_projects, file_context)

        try:
            response = self._call_ollama(analysis_prompt)
            if response:
                return self._parse_response(response)
        except Exception as e:
            logger.warning(f"LLM analysis failed: {e}")

        return []

    def _build_analysis_prompt(
        self,
        prompt: str,
        known_projects: List[str] = None,
        file_context: str = None
    ) -> str:
        """Build the prompt for LLM analysis."""
        parts = [f"Task description: {prompt}"]

        if known_projects:
            parts.append(f"\nKnown projects in this workspace: {', '.join(known_projects)}")

        if file_context:
            parts.append(f"\nCurrent file context: {file_context}")

        parts.append("\nAnalyze this task and determine which project it belongs to.")

        return "\n".join(parts)

    def _call_ollama(self, prompt: str) -> Optional[str]:
        """Call Ollama API for analysis."""
        try:
            import httpx

            response = httpx.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "system": PROJECT_DETECTION_SYSTEM_PROMPT,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,  # Lower temp for more deterministic output
                        "num_predict": 256   # Short response needed
                    }
                },
                timeout=LLM_TIMEOUT
            )

            if response.status_code == 200:
                return response.json().get("response", "")
            else:
                logger.warning(f"Ollama returned status {response.status_code}")
                return None

        except ImportError:
            logger.warning("httpx not available for LLM calls")
            return None
        except Exception as e:
            logger.warning(f"Ollama call failed: {e}")
            return None

    def _parse_response(self, response: str) -> List[ProjectSignal]:
        """Parse LLM response into ProjectSignals."""
        signals = []

        try:
            # Try to extract JSON from response
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())

                # Primary project
                primary = data.get("primary_project")
                confidence = float(data.get("confidence", 0.5))
                keywords = data.get("keywords_detected", [])

                if primary and confidence > 0.3:
                    signals.append(ProjectSignal(
                        project=primary.lower(),
                        confidence=confidence,
                        keywords=keywords,
                        weight=0.9 * confidence  # Scale weight by confidence
                    ))

                # Alternative projects
                for alt in data.get("alternative_projects", []):
                    alt_project = alt.get("project")
                    alt_confidence = float(alt.get("confidence", 0.3))
                    if alt_project and alt_confidence > 0.3:
                        signals.append(ProjectSignal(
                            project=alt_project.lower(),
                            confidence=alt_confidence,
                            keywords=[],
                            weight=0.9 * alt_confidence
                        ))

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug(f"Failed to parse LLM response: {e}")

            # Fallback: try to extract project name from text
            signals.extend(self._fallback_extract(response))

        return signals

    def _fallback_extract(self, response: str) -> List[ProjectSignal]:
        """Fallback extraction when JSON parsing fails."""
        signals = []
        response_lower = response.lower()

        # Look for common project indicators
        project_patterns = [
            (r'context[- ]?dna', 'context-dna'),
            (r'ersim[- ]?voice[- ]?stack', 'ersim-voice-stack'),
            (r'voice[- ]?stack', 'ersim-voice-stack'),
            (r'webhook', 'context-dna'),
            (r'injection', 'context-dna'),
            (r'backend', 'backend'),
            (r'frontend', 'frontend'),
            (r'infra', 'infra'),
            (r'memory', 'memory'),
        ]

        for pattern, project in project_patterns:
            if re.search(pattern, response_lower):
                signals.append(ProjectSignal(
                    project=project,
                    confidence=0.5,  # Lower confidence for fallback
                    keywords=[],
                    weight=0.5
                ))
                break  # Only take first match in fallback

        return signals

    # =========================================================================
    # HEALTH CHECK
    # =========================================================================

    def is_healthy(self) -> bool:
        """Check if Ollama is available and responsive."""
        # Use cached result if available
        if self._healthy is not None:
            return self._healthy

        try:
            import httpx
            response = httpx.get(f"{self.ollama_url}/api/tags", timeout=5)
            self._healthy = response.status_code == 200
            return self._healthy
        except Exception:
            self._healthy = False
            return False

    def get_status(self) -> Dict:
        """Get detailed LLM status."""
        status = {
            "available": self.is_healthy(),
            "url": self.ollama_url,
            "model": self.model
        }

        if self.is_healthy():
            try:
                import httpx
                response = httpx.get(f"{self.ollama_url}/api/tags", timeout=5)
                if response.status_code == 200:
                    models = response.json().get("models", [])
                    status["models_available"] = [m.get("name") for m in models]
                    status["target_model_loaded"] = any(
                        self.model in m.get("name", "")
                        for m in models
                    )
            except Exception as e:
                status["error"] = str(e)

        return status

    # =========================================================================
    # ASYNC ANALYSIS (via Celery)
    # =========================================================================

    def analyze_async(
        self,
        prompt: str,
        known_projects: List[str] = None,
        callback_task: str = None
    ) -> str:
        """
        Queue LLM analysis as a Celery task.

        Args:
            prompt: The prompt to analyze
            known_projects: Known project names
            callback_task: Optional task name to call with results

        Returns:
            Task ID
        """
        try:
            from memory.celery_tasks import llm_analyze
            task = llm_analyze.delay(
                text=self._build_analysis_prompt(prompt, known_projects),
                analysis_type="detect_project"
            )
            return task.id
        except ImportError:
            logger.warning("Celery not available for async analysis")
            return None


# =============================================================================
# SINGLETON
# =============================================================================

_instance: Optional[LLMBoundaryAnalyzer] = None


def get_llm_boundary_analyzer() -> LLMBoundaryAnalyzer:
    """Get the singleton LLM boundary analyzer instance."""
    global _instance
    if _instance is None:
        _instance = LLMBoundaryAnalyzer()
    return _instance


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    analyzer = get_llm_boundary_analyzer()

    if len(sys.argv) < 2:
        print("LLM Boundary Analyzer")
        print("=" * 50)
        status = analyzer.get_status()
        print(f"Available: {status['available']}")
        print(f"URL: {status['url']}")
        print(f"Model: {status['model']}")
        if status.get('models_available'):
            print(f"Models: {', '.join(status['models_available'][:5])}")
        print()
        print("Commands:")
        print("  python llm_boundary_analyzer.py analyze <prompt>")
        print("  python llm_boundary_analyzer.py status")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "analyze":
        if len(sys.argv) < 3:
            print("Usage: python llm_boundary_analyzer.py analyze <prompt>")
            sys.exit(1)

        prompt = " ".join(sys.argv[2:])
        print(f"Analyzing: {prompt}\n")

        # Get known projects from hierarchy
        try:
            from memory.boundary_intelligence import get_hierarchy_projects
            known = get_hierarchy_projects()
        except Exception:
            known = ["context-dna", "ersim-voice-stack", "backend", "memory", "infra"]

        signals = analyzer.analyze_prompt(prompt, known_projects=known)

        if signals:
            print("Project Signals:")
            for signal in signals:
                print(f"  {signal.project}: {signal.confidence:.1%} (weight: {signal.weight:.2f})")
                if signal.keywords:
                    print(f"    Keywords: {', '.join(signal.keywords)}")
        else:
            print("No project signals detected (LLM may be unavailable)")

    elif cmd == "status":
        status = analyzer.get_status()
        print("LLM Status:")
        for key, value in status.items():
            print(f"  {key}: {value}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
