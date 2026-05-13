"""SOP Extractor for Context DNA.

Uses LLM to extract Standard Operating Procedures, patterns, and gotchas
from work sessions and learnings.
"""

import json
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from context_dna.llm.manager import ProviderManager
from context_dna.llm.base import LLMResponse


@dataclass
class ExtractedSOP:
    """An extracted Standard Operating Procedure."""

    title: str
    steps: List[str]
    when_to_use: str
    tags: List[str]
    confidence: float  # 0-1


@dataclass
class ExtractedGotcha:
    """An extracted gotcha/pitfall."""

    problem: str
    solution: str
    tags: List[str]
    confidence: float


@dataclass
class ExtractedPattern:
    """An extracted reusable pattern."""

    name: str
    description: str
    example: str
    tags: List[str]
    confidence: float


@dataclass
class ExtractionResult:
    """Result of SOP extraction."""

    sops: List[ExtractedSOP]
    gotchas: List[ExtractedGotcha]
    patterns: List[ExtractedPattern]
    raw_response: str


class SOPExtractor:
    """Extracts SOPs, patterns, and gotchas using LLM analysis.

    Uses a multi-stage extraction process:
    1. Analyze work session content
    2. Extract structured procedures
    3. Identify patterns and gotchas
    4. Validate and score extractions
    """

    EXTRACTION_PROMPT = '''Analyze this work session and extract reusable knowledge.

## Work Session Content:
{session_content}

## Instructions:
Extract the following from the work session:

1. **PROCEDURES (SOPs)**: Step-by-step instructions that could be reused
   - Focus on actionable, repeatable processes
   - Include specific commands, file paths, or code when relevant
   - Describe WHEN to use each procedure

2. **GOTCHAS**: Problems encountered and their solutions
   - Focus on non-obvious issues that could trip someone up
   - Include the root cause if discovered
   - Prioritize issues that took significant time to diagnose

3. **PATTERNS**: Reusable code or configuration patterns
   - Include actual code examples when present
   - Describe the problem the pattern solves
   - Note any prerequisites or constraints

## Output Format (JSON):
{{
    "procedures": [
        {{
            "title": "Brief descriptive title",
            "steps": ["Step 1: ...", "Step 2: ...", "..."],
            "when_to_use": "Describe when this procedure applies",
            "tags": ["relevant", "tags"],
            "confidence": 0.9
        }}
    ],
    "gotchas": [
        {{
            "problem": "What went wrong or was confusing",
            "solution": "How to fix or avoid it",
            "tags": ["relevant", "tags"],
            "confidence": 0.85
        }}
    ],
    "patterns": [
        {{
            "name": "Pattern name",
            "description": "What problem it solves",
            "example": "Code or config example",
            "tags": ["relevant", "tags"],
            "confidence": 0.8
        }}
    ]
}}

## Important:
- Only extract genuinely useful knowledge (confidence > 0.7)
- Be specific - vague procedures are not helpful
- Include actual commands/code when available
- If nothing useful can be extracted, return empty arrays
- Output ONLY valid JSON, no markdown or explanation'''

    VALIDATION_PROMPT = '''Review this extracted knowledge for accuracy and usefulness.

## Extracted Content:
{extracted_content}

## Original Context:
{original_context}

## Instructions:
For each extracted item, evaluate:
1. Is it accurate based on the original content?
2. Is it specific enough to be actionable?
3. Would it be useful for future reference?

Return a JSON object with adjusted confidence scores (0-1):
{{
    "procedures": [{{ "index": 0, "confidence": 0.9, "keep": true, "reason": "..." }}],
    "gotchas": [{{ "index": 0, "confidence": 0.85, "keep": true, "reason": "..." }}],
    "patterns": [{{ "index": 0, "confidence": 0.8, "keep": true, "reason": "..." }}]
}}

Only output valid JSON.'''

    def __init__(
        self,
        provider_manager: Optional[ProviderManager] = None,
        min_confidence: float = 0.7,
    ):
        """Initialize SOP extractor.

        Args:
            provider_manager: LLM provider manager (creates default if None)
            min_confidence: Minimum confidence threshold for keeping extractions
        """
        self._provider_manager = provider_manager or ProviderManager()
        self._min_confidence = min_confidence

    def extract(
        self,
        session_content: str,
        validate: bool = True,
    ) -> ExtractionResult:
        """Extract SOPs, patterns, and gotchas from session content.

        Args:
            session_content: Work session content to analyze
            validate: Whether to run validation pass

        Returns:
            ExtractionResult with extracted items
        """
        # Get LLM provider
        provider = self._provider_manager.get_provider()

        # Run extraction
        extraction_response = provider.generate(
            prompt=self.EXTRACTION_PROMPT.format(session_content=session_content),
            system_prompt="You are an expert at extracting reusable procedures and patterns from work sessions. Output only valid JSON.",
            temperature=0.3,  # Lower temperature for more consistent extraction
            max_tokens=4000,
        )

        # Parse extraction result
        try:
            extracted = self._parse_json_response(extraction_response.content)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            extracted = self._extract_json_from_markdown(extraction_response.content)

        # Validate if requested
        if validate and extracted:
            extracted = self._validate_extractions(
                extracted, session_content, provider
            )

        # Build result
        return self._build_result(extracted, extraction_response.content)

    def extract_from_learnings(
        self,
        learnings: List[Any],  # List[Learning]
    ) -> ExtractionResult:
        """Extract SOPs from a collection of learnings.

        Args:
            learnings: List of Learning objects to analyze

        Returns:
            ExtractionResult with extracted items
        """
        # Combine learnings into session content
        content_parts = []
        for learning in learnings:
            content_parts.append(f"## {learning.type.value.upper()}: {learning.title}")
            content_parts.append(learning.content)
            if learning.tags:
                content_parts.append(f"Tags: {', '.join(learning.tags)}")
            content_parts.append("")

        session_content = "\n".join(content_parts)
        return self.extract(session_content)

    def _parse_json_response(self, content: str) -> Dict[str, Any]:
        """Parse JSON from LLM response."""
        # Try direct parsing
        content = content.strip()
        if content.startswith("{"):
            return json.loads(content)

        # Try to find JSON in response
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])

        raise json.JSONDecodeError("No JSON found", content, 0)

    def _extract_json_from_markdown(self, content: str) -> Dict[str, Any]:
        """Extract JSON from markdown code blocks."""
        import re

        # Look for ```json ... ``` blocks
        pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        matches = re.findall(pattern, content, re.DOTALL)

        for match in matches:
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue

        # Fallback to empty result
        return {"procedures": [], "gotchas": [], "patterns": []}

    def _validate_extractions(
        self,
        extracted: Dict[str, Any],
        original_content: str,
        provider: Any,
    ) -> Dict[str, Any]:
        """Validate extractions with second LLM pass."""
        try:
            validation_response = provider.generate(
                prompt=self.VALIDATION_PROMPT.format(
                    extracted_content=json.dumps(extracted, indent=2),
                    original_context=original_content[:2000],  # Truncate for context
                ),
                system_prompt="You are a quality reviewer. Output only valid JSON.",
                temperature=0.1,
                max_tokens=2000,
            )

            validation = self._parse_json_response(validation_response.content)

            # Apply validation results
            for key in ["procedures", "gotchas", "patterns"]:
                if key in validation and key in extracted:
                    for val in validation[key]:
                        idx = val.get("index", 0)
                        if idx < len(extracted[key]):
                            extracted[key][idx]["confidence"] = val.get(
                                "confidence", extracted[key][idx].get("confidence", 0.5)
                            )
                            if not val.get("keep", True):
                                extracted[key][idx]["confidence"] = 0  # Mark for removal

            # Filter by confidence
            for key in ["procedures", "gotchas", "patterns"]:
                if key in extracted:
                    extracted[key] = [
                        item for item in extracted[key]
                        if item.get("confidence", 0) >= self._min_confidence
                    ]

        except Exception as e:
            # Validation failed, keep original extractions
            print(f"[WARN] SOP extraction validation failed, keeping originals: {e}")

        return extracted

    def _build_result(
        self,
        extracted: Dict[str, Any],
        raw_response: str,
    ) -> ExtractionResult:
        """Build ExtractionResult from parsed data."""
        sops = []
        for item in extracted.get("procedures", []):
            sops.append(ExtractedSOP(
                title=item.get("title", ""),
                steps=item.get("steps", []),
                when_to_use=item.get("when_to_use", ""),
                tags=item.get("tags", []),
                confidence=item.get("confidence", 0.5),
            ))

        gotchas = []
        for item in extracted.get("gotchas", []):
            gotchas.append(ExtractedGotcha(
                problem=item.get("problem", ""),
                solution=item.get("solution", ""),
                tags=item.get("tags", []),
                confidence=item.get("confidence", 0.5),
            ))

        patterns = []
        for item in extracted.get("patterns", []):
            patterns.append(ExtractedPattern(
                name=item.get("name", ""),
                description=item.get("description", ""),
                example=item.get("example", ""),
                tags=item.get("tags", []),
                confidence=item.get("confidence", 0.5),
            ))

        return ExtractionResult(
            sops=sops,
            gotchas=gotchas,
            patterns=patterns,
            raw_response=raw_response,
        )
