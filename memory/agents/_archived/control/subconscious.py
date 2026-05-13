"""
Subconscious Agent - Local LLM Reasoner

The Subconscious uses a local LLM (Ollama) for reasoning tasks that
don't need to go to the main Claude model - quick classifications,
summaries, and pattern recognition.

Anatomical Label: Limbic System (Local LLM Reasoner)
"""

from __future__ import annotations
import os
import json
from datetime import datetime
from typing import Dict, Any, Optional

from ..base import Agent, AgentCategory, AgentState


class SubconsciousAgent(Agent):
    """
    Subconscious Agent - Local LLM reasoning.

    Responsibilities:
    - Quick text classification
    - Local summarization
    - Pattern recognition
    - Risk assessment support
    """

    NAME = "subconscious"
    CATEGORY = AgentCategory.CONTROL
    DESCRIPTION = "Local LLM reasoning for fast classifications"
    ANATOMICAL_LABEL = "Limbic System (Local LLM Reasoner)"
    IS_VITAL = False  # System works without local LLM

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
        self._model = os.environ.get('OLLAMA_MODEL', 'qwen2.5:3b')
        self._available = False
        self._last_response_time: Optional[float] = None

    def _on_start(self):
        """Check Ollama availability."""
        self._check_ollama()

    def _on_stop(self):
        """Shutdown subconscious."""
        pass

    def _check_ollama(self) -> bool:
        """Check if Ollama is available."""
        try:
            import httpx
            response = httpx.get(f"{self._ollama_url}/api/tags", timeout=5)
            if response.status_code == 200:
                models = response.json().get("models", [])
                self._available = any(
                    self._model.split(":")[0] in m.get("name", "")
                    for m in models
                )
                return self._available
        except Exception as e:
            print(f"[WARN] Ollama availability check failed: {e}")
        self._available = False
        return False

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check subconscious health."""
        self._check_ollama()
        return {
            "healthy": True,  # Always healthy, LLM is optional
            "score": 1.0 if self._available else 0.5,
            "message": f"Ollama ({self._model}) available" if self._available else "Ollama unavailable",
            "metrics": {
                "ollama_available": self._available,
                "model": self._model,
                "last_response_time": self._last_response_time
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process reasoning requests."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "reason")
            if op == "reason":
                return self.reason(input_data.get("prompt"), input_data.get("context"))
            elif op == "classify":
                return self.classify(input_data.get("text"), input_data.get("categories"))
            elif op == "summarize":
                return self.summarize(input_data.get("text"), input_data.get("max_length", 100))
        return None

    def reason(self, prompt: str, context: str = None) -> Dict[str, Any]:
        """Use local LLM for reasoning."""
        if not self._available:
            return {"status": "unavailable", "message": "Ollama not available"}

        try:
            import httpx
            import time

            full_prompt = prompt
            if context:
                full_prompt = f"Context:\n{context}\n\nQuestion:\n{prompt}"

            start_time = time.time()
            response = httpx.post(
                f"{self._ollama_url}/api/generate",
                json={
                    "model": self._model,
                    "prompt": full_prompt,
                    "stream": False
                },
                timeout=60
            )
            self._last_response_time = time.time() - start_time

            if response.status_code == 200:
                result = response.json().get("response", "")
                self._last_active = datetime.utcnow()
                return {
                    "status": "success",
                    "response": result,
                    "model": self._model,
                    "response_time": self._last_response_time
                }
            else:
                return {"status": "error", "message": f"HTTP {response.status_code}"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    def classify(self, text: str, categories: list) -> Dict[str, Any]:
        """Classify text into categories."""
        prompt = f"""Classify the following text into ONE of these categories: {', '.join(categories)}

Consider whatever classification approach makes sense to you:
- What are the key themes or features in this text?
- Which category best captures the primary meaning?
- Are there borderline cases where multiple categories apply?
- How confident are you in this classification?

You can respond in whatever way makes sense. You could:
- Simply state the category name
- Explain your reasoning and then state the category
- If unsure, describe what makes this classification difficult

Text: {text[:500]}"""

        result = self.reason(prompt)
        if result.get("status") == "success":
            response = result["response"].strip().lower()

            # Try to match category name directly (handles "category_name only" response)
            for cat in categories:
                if cat.lower() in response or response in cat.lower():
                    return {"status": "success", "category": cat}

            # Fallback: extract category from explanation
            # Look for any category name mentioned in the response
            for cat in categories:
                if cat.lower() in response:
                    return {"status": "success", "category": cat}

            # Ultimate fallback: return raw response (user will handle ambiguity)
            return {"status": "success", "category": result["response"].strip()}
        return result

    def summarize(self, text: str, max_length: int = 100) -> Dict[str, Any]:
        """Summarize text."""
        prompt = f"""Summarize the following text in {max_length} words or less:

{text[:2000]}

Summary:"""

        result = self.reason(prompt)
        if result.get("status") == "success":
            return {
                "status": "success",
                "summary": result["response"].strip()[:max_length * 6]  # Rough char limit
            }
        return result

    def assess_risk(self, prompt: str) -> Dict[str, Any]:
        """Quick risk assessment using local LLM."""
        assessment_prompt = f"""Assess the risk level of this coding task on a scale of 1-10:
1-3: Low risk (simple changes, well-understood)
4-6: Moderate risk (some complexity, potential side effects)
7-10: High risk (critical systems, production, irreversible)

Consider whatever assessment approach makes sense to you:
- What's the scope of change? (narrow = lower risk, broad = higher risk)
- How well-understood is the component? (known = lower risk, complex = higher risk)
- What's the blast radius? (isolated = lower risk, cascading effects = higher risk)
- What's the reversibility? (easy to rollback = lower risk, permanent = higher risk)

You can respond in whatever way makes sense. You could:
- Simply state a number 1-10
- Explain your reasoning and then provide the risk score
- Highlight the most critical risk factors

Task: {prompt[:500]}"""

        result = self.reason(assessment_prompt)
        if result.get("status") == "success":
            try:
                response_text = result["response"].strip()

                # Extract first number from response (handles both "5 only" and "5 because...")
                import re
                numbers = re.findall(r'\b\d+\b', response_text)
                if numbers:
                    score = int(numbers[0])
                    # Clamp to valid range
                    score = max(1, min(10, score))
                else:
                    # No number found, use default
                    score = 5

                if score <= 3:
                    level = "low"
                elif score <= 6:
                    level = "moderate"
                else:
                    level = "high"
                return {"status": "success", "score": score, "level": level}
            except Exception as e:
                print(f"[WARN] Risk score parsing failed: {e}")
        return {"status": "fallback", "score": 5, "level": "moderate"}
