"""Extraction Pipeline for Context DNA.

Automatically extracts:
- SOPs (Standard Operating Procedures) from work sessions
- Patterns from code commits
- Gotchas from error resolutions
"""

from context_dna.extraction.sop_extractor import SOPExtractor
from context_dna.extraction.git_analyzer import GitAnalyzer

__all__ = ["SOPExtractor", "GitAnalyzer"]
