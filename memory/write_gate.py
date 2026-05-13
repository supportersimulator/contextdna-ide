"""
LearningWriteGate — validates learnings before storage.

Python mirror of context-dna/engine/gates/learning-write-gate.ts.
Same gates, same behavior, cross-language compatible.
"""
import re
import uuid
from typing import Dict, Optional

# Self-reference terms (must match invariance-filters.ts SELF_REFERENCE_TERMS)
SELF_REFERENCE_TERMS = [
    'webhook injection',
    'evidence grading',
    'session_historian',
    'payload_manifest',
    'observability_store',
    'gold mining',
    'context_dna',
    'contextdna',
    'projectdna',
    'synaptic',
    'atlas',
]

# Build regex: longest first, word boundaries, case-insensitive
_escaped = sorted(SELF_REFERENCE_TERMS, key=len, reverse=True)
_pattern_str = r'\b(' + '|'.join(re.escape(t) for t in _escaped) + r')\b'
_SELF_REF_PATTERN = re.compile(_pattern_str, re.IGNORECASE)


class LearningWriteGate:
    """Validates learnings before storage with self-ref, confidence, and token gates."""

    def __init__(self, product_mode: bool = True, confidence_threshold: float = 0.5):
        self.product_mode = product_mode
        self.confidence_threshold = confidence_threshold
        self._valid_tokens: set = set()

    def validate(self, content: str, domain: str = '', confidence: float = 1.0) -> Dict:
        """Validate a learning. Returns dict with 'allowed', 'reason', 'token'."""
        # Gate 1: Self-reference (product mode only)
        if self.product_mode and _SELF_REF_PATTERN.search(content):
            return {
                'allowed': False,
                'reason': 'Rejected: self-reference to ContextDNA internals in product mode',
                'gate': 'self_reference',
                'token': None,
            }

        # Gate 2: Confidence threshold (product mode only)
        if self.product_mode and confidence < self.confidence_threshold:
            return {
                'allowed': False,
                'reason': f'Rejected: confidence {confidence} below threshold {self.confidence_threshold}',
                'gate': 'confidence',
                'token': None,
            }

        # All gates passed
        token = str(uuid.uuid4())
        self._valid_tokens.add(token)
        return {'allowed': True, 'token': token, 'reason': None, 'gate': None}

    def verify_token(self, token: str) -> bool:
        """Verify and consume a gate token. Single-use."""
        if token in self._valid_tokens:
            self._valid_tokens.discard(token)
            return True
        return False
