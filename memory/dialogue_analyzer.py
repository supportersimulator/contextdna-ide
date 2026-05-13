#!/usr/bin/env python3
"""
DIALOGUE ANALYZER - Extract project signals from mirrored conversation

Analyzes conversation history to detect which project the work is about.
This is one of the 5 input signals for Project Boundary Intelligence.

ARCHITECTURE:
- Scans messages for file paths, project names, technical domains
- Extracts patterns from user prompts and assistant responses
- Weighs recent messages more heavily
- Provides signal confidence based on pattern strength

Purpose:
The dialogue provides implicit context that other signals might miss:
- User mentions "the webhook" → Context DNA (from conversation flow)
- Assistant worked on "voice pipeline" → ersim-voice-stack (from context)
- Discussion of "Django admin" → backend (from technical domain)

Usage:
    from memory.dialogue_analyzer import get_dialogue_analyzer

    analyzer = get_dialogue_analyzer()

    # Analyze conversation history
    signals = analyzer.analyze(messages=[
        {"role": "user", "content": "fix the webhook injection bug"},
        {"role": "assistant", "content": "Looking at memory/injection_store.py..."}
    ], known_projects=["context-dna", "backend"])
    # Returns: [ProjectSignal(project="context-dna", confidence=0.75, ...)]
"""

import re
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from collections import Counter

logger = logging.getLogger('contextdna.dialogue')

# =============================================================================
# CONFIGURATION
# =============================================================================

# Weight for message recency (recent messages count more)
RECENCY_DECAY = 0.9  # Each older message is 0.9x the previous

# Minimum confidence to report a signal
MIN_CONFIDENCE = 0.3

# Maximum messages to analyze (to limit processing)
MAX_MESSAGES = 50

# =============================================================================
# PROJECT PATTERNS
# =============================================================================

# Pattern definitions: (regex, project, weight)
# Higher weight = stronger signal
PROJECT_PATTERNS = [
    # Context DNA patterns
    (r'context[- ]?dna', 'context-dna', 1.0),
    (r'webhook\s*inject', 'context-dna', 0.9),
    (r'injection[_ ]?store', 'context-dna', 0.9),
    (r'hook[_ ]?evolution', 'context-dna', 0.8),
    (r'pattern[_ ]?(evolution|manager)', 'context-dna', 0.8),
    (r'boundary[_ ]?(intelligence|feedback)', 'context-dna', 0.9),
    (r'acontext', 'context-dna', 0.8),
    (r'sop[_ ]?(enhancer|types)', 'context-dna', 0.7),
    (r'celery[_ ]?tasks?\.py', 'context-dna', 0.6),
    (r'brain\.py', 'context-dna', 0.7),

    # Voice stack patterns
    (r'ersim[- ]?voice[- ]?stack', 'ersim-voice-stack', 1.0),
    (r'voice[- ]?stack', 'ersim-voice-stack', 0.9),
    (r'voice[_ ]?pipeline', 'ersim-voice-stack', 0.9),
    (r'livekit', 'ersim-voice-stack', 0.9),
    (r'stt\s*service', 'ersim-voice-stack', 0.8),
    (r'tts\s*service', 'ersim-voice-stack', 0.8),
    (r'whisper', 'ersim-voice-stack', 0.7),
    (r'elevenlabs', 'ersim-voice-stack', 0.8),
    (r'webrtc', 'ersim-voice-stack', 0.7),

    # Backend patterns
    (r'django', 'backend', 0.8),
    (r'backend/', 'backend', 0.9),
    (r'gunicorn', 'backend', 0.8),
    (r'ersim[_ ]?backend', 'backend', 1.0),
    (r'django\s*admin', 'backend', 0.7),
    (r'users/views\.py', 'backend', 0.9),

    # Memory/local patterns
    (r'\bmemory/', 'memory', 0.8),
    (r'memory/[a-z_]+\.py', 'memory', 0.9),
    (r'local[_ ]?llm', 'memory', 0.7),
    (r'professor\.py', 'memory', 0.8),
    (r'query\.py', 'memory', 0.7),

    # Infrastructure patterns
    (r'\binfra/', 'infra', 0.9),
    (r'terraform', 'infra', 0.9),
    (r'docker[- ]?compose', 'infra', 0.7),
    (r'aws/', 'infra', 0.8),
    (r'ec2|ecs|lambda', 'infra', 0.7),

    # Frontend patterns
    (r'landing[- ]?page', 'landing-page', 0.9),
    (r'sim[- ]?frontend', 'sim-frontend', 0.9),
    (r'admin\.ersimulator', 'admin.ersimulator.com', 0.9),
    (r'admin\.contextdna', 'admin.contextdna.io', 0.9),

    # Simulator core patterns
    (r'simulator[- ]?core', 'simulator-core', 0.9),
    (r'er[- ]?sim[- ]?monitor', 'simulator-core', 0.9),
    (r'vitals', 'simulator-core', 0.6),
    (r'waveform', 'simulator-core', 0.6),
]

# File path patterns (for extracting project from paths)
FILE_PATH_PATTERNS = [
    (r'context-dna/', 'context-dna'),
    (r'ersim-voice-stack/', 'ersim-voice-stack'),
    (r'backend/', 'backend'),
    (r'memory/', 'memory'),
    (r'infra/', 'infra'),
    (r'landing-page/', 'landing-page'),
    (r'sim-frontend/', 'sim-frontend'),
    (r'simulator-core/', 'simulator-core'),
    (r'admin\.ersimulator\.com/', 'admin.ersimulator.com'),
    (r'admin\.contextdna\.io/', 'admin.contextdna.io'),
]

# Technical domain keywords (weaker signals)
DOMAIN_KEYWORDS = {
    'context-dna': ['hook', 'injection', 'sop', 'pattern', 'brain', 'celery', 'redis', 'boundary'],
    'ersim-voice-stack': ['voice', 'audio', 'speech', 'transcript', 'streaming', 'realtime'],
    'backend': ['api', 'endpoint', 'model', 'migration', 'admin', 'auth'],
    'infra': ['deploy', 'server', 'instance', 'container', 'network', 'ssl'],
    'simulator-core': ['monitor', 'ecg', 'patient', 'simulation', 'scenario'],
}


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class ProjectSignal:
    """A signal indicating a specific project (matches boundary_intelligence.py)."""
    project: str
    source: str = "dialogue"
    confidence: float = 0.5
    keywords: List[str] = None
    weight: float = 0.5  # Dialogue signals have moderate weight

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class DialogueContext:
    """Extracted context from conversation."""
    file_paths: List[str]
    project_mentions: List[Tuple[str, float]]  # (project, confidence)
    domain_keywords: List[str]
    message_count: int
    analyzed_at: str


# =============================================================================
# DIALOGUE ANALYZER
# =============================================================================

class DialogueAnalyzer:
    """
    Analyze conversation history for project context.

    Extracts signals from:
    - File paths mentioned in messages
    - Project names referenced directly
    - Technical domain keywords
    - Patterns in recent work discussion
    """

    def __init__(self, known_projects: List[str] = None):
        """
        Initialize dialogue analyzer.

        Args:
            known_projects: List of known project names (for fuzzy matching)
        """
        self.known_projects = known_projects or []
        self._compiled_patterns = self._compile_patterns()

    def _compile_patterns(self) -> List[Tuple[re.Pattern, str, float]]:
        """Pre-compile regex patterns for performance."""
        compiled = []
        for pattern, project, weight in PROJECT_PATTERNS:
            try:
                compiled.append((re.compile(pattern, re.IGNORECASE), project, weight))
            except re.error as e:
                logger.warning(f"Invalid pattern '{pattern}': {e}")
        return compiled

    # =========================================================================
    # CORE ANALYSIS
    # =========================================================================

    def analyze(
        self,
        messages: List[Dict],
        known_projects: List[str] = None
    ) -> List[ProjectSignal]:
        """
        Analyze conversation messages for project signals.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            known_projects: Optional list of known project names

        Returns:
            List of ProjectSignal objects with confidence scores
        """
        if not messages:
            return []

        # Update known projects if provided
        if known_projects:
            self.known_projects = known_projects

        # Limit to recent messages
        recent_messages = messages[-MAX_MESSAGES:]

        # Aggregate signals across messages
        project_scores: Dict[str, float] = {}
        project_keywords: Dict[str, List[str]] = {}

        # Process messages with recency weighting
        for i, msg in enumerate(reversed(recent_messages)):
            content = msg.get('content', '')
            if not content:
                continue

            # Calculate recency weight (most recent = 1.0, decays for older)
            recency_weight = RECENCY_DECAY ** i

            # Extract signals from this message
            msg_signals = self._analyze_message(content, recency_weight)

            # Aggregate into project scores
            for project, score, keywords in msg_signals:
                if project not in project_scores:
                    project_scores[project] = 0
                    project_keywords[project] = []
                project_scores[project] += score
                project_keywords[project].extend(keywords)

        # Normalize scores and create signals
        signals = []
        if project_scores:
            max_score = max(project_scores.values())

            for project, score in project_scores.items():
                # Normalize to 0-1 range
                confidence = min(1.0, score / max(max_score, 1.0))

                if confidence >= MIN_CONFIDENCE:
                    # Dedupe keywords
                    unique_keywords = list(set(project_keywords[project]))[:10]

                    signals.append(ProjectSignal(
                        project=project.lower(),
                        confidence=confidence,
                        keywords=unique_keywords,
                        weight=0.5 * confidence  # Scale weight by confidence
                    ))

        # Sort by confidence descending
        signals.sort(key=lambda s: -s.confidence)

        return signals

    def _analyze_message(
        self,
        content: str,
        recency_weight: float = 1.0
    ) -> List[Tuple[str, float, List[str]]]:
        """
        Analyze a single message for project signals.

        Args:
            content: Message content
            recency_weight: Weight based on message recency

        Returns:
            List of (project, score, keywords) tuples
        """
        signals = []
        content_lower = content.lower()

        # 1. Check for file paths
        file_signals = self._extract_file_paths(content)
        for project, paths in file_signals.items():
            score = len(paths) * 0.8 * recency_weight
            signals.append((project, score, paths))

        # 2. Check for project patterns
        for pattern, project, weight in self._compiled_patterns:
            matches = pattern.findall(content)
            if matches:
                score = len(matches) * weight * recency_weight
                signals.append((project, score, matches[:5]))

        # 3. Check for domain keywords
        for project, keywords in DOMAIN_KEYWORDS.items():
            found_keywords = []
            for kw in keywords:
                if kw in content_lower:
                    found_keywords.append(kw)
            if found_keywords:
                # Domain keywords are weaker signals
                score = len(found_keywords) * 0.3 * recency_weight
                signals.append((project, score, found_keywords))

        # 4. Check against known projects (exact/fuzzy match)
        for known in self.known_projects:
            if known.lower() in content_lower:
                score = 0.9 * recency_weight
                signals.append((known, score, [known]))

        return signals

    def _extract_file_paths(self, content: str) -> Dict[str, List[str]]:
        """Extract file paths and map to projects."""
        project_paths: Dict[str, List[str]] = {}

        # Find potential file paths
        path_pattern = r'[\w\-\.]+/[\w\-\./]+\.(?:py|js|ts|tsx|yaml|yml|json|md|sh)'
        paths = re.findall(path_pattern, content)

        for path in paths:
            for pattern, project in FILE_PATH_PATTERNS:
                if re.search(pattern, path, re.IGNORECASE):
                    if project not in project_paths:
                        project_paths[project] = []
                    project_paths[project].append(path)
                    break

        return project_paths

    # =========================================================================
    # CONTEXT EXTRACTION
    # =========================================================================

    def extract_context(self, messages: List[Dict]) -> DialogueContext:
        """
        Extract full context from conversation (for debugging/analysis).

        Args:
            messages: List of message dicts

        Returns:
            DialogueContext with all extracted information
        """
        all_paths = []
        all_mentions = []
        all_keywords = []

        for msg in messages[-MAX_MESSAGES:]:
            content = msg.get('content', '')
            if not content:
                continue

            # Extract paths
            paths = self._extract_file_paths(content)
            for project_paths in paths.values():
                all_paths.extend(project_paths)

            # Extract mentions
            signals = self._analyze_message(content)
            for project, score, keywords in signals:
                all_mentions.append((project, score))
                all_keywords.extend(keywords)

        # Dedupe and sort
        unique_paths = list(set(all_paths))
        unique_keywords = list(set(all_keywords))

        # Aggregate mentions by project
        mention_scores: Dict[str, float] = {}
        for project, score in all_mentions:
            if project not in mention_scores:
                mention_scores[project] = 0
            mention_scores[project] += score

        sorted_mentions = sorted(mention_scores.items(), key=lambda x: -x[1])

        return DialogueContext(
            file_paths=unique_paths[:20],
            project_mentions=sorted_mentions[:10],
            domain_keywords=unique_keywords[:20],
            message_count=len(messages),
            analyzed_at=datetime.utcnow().isoformat()
        )

    # =========================================================================
    # UTILITIES
    # =========================================================================

    def get_dominant_project(
        self,
        messages: List[Dict],
        known_projects: List[str] = None
    ) -> Optional[Tuple[str, float]]:
        """
        Get the single most likely project from conversation.

        Args:
            messages: Conversation messages
            known_projects: Optional known project list

        Returns:
            Tuple of (project_name, confidence) or None
        """
        signals = self.analyze(messages, known_projects)
        if signals:
            top = signals[0]
            return (top.project, top.confidence)
        return None


# =============================================================================
# SINGLETON
# =============================================================================

_instance: Optional[DialogueAnalyzer] = None


def get_dialogue_analyzer(known_projects: List[str] = None) -> DialogueAnalyzer:
    """Get the singleton dialogue analyzer instance."""
    global _instance
    if _instance is None:
        _instance = DialogueAnalyzer(known_projects)
    elif known_projects:
        _instance.known_projects = known_projects
    return _instance


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys
    import json

    analyzer = get_dialogue_analyzer()

    if len(sys.argv) < 2:
        print("Dialogue Analyzer")
        print("=" * 50)
        print()
        print("Usage:")
        print("  python dialogue_analyzer.py analyze <json_messages>")
        print("  python dialogue_analyzer.py test")
        print()
        print("Example:")
        print('  python dialogue_analyzer.py analyze \'[{"role":"user","content":"fix webhook"}]\'')
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "analyze":
        if len(sys.argv) < 3:
            print("Usage: python dialogue_analyzer.py analyze <json_messages>")
            sys.exit(1)

        try:
            messages = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
            sys.exit(1)

        # Get known projects from hierarchy if available
        try:
            from memory.boundary_intelligence import get_hierarchy_projects
            known = get_hierarchy_projects()
        except Exception:
            known = ["context-dna", "ersim-voice-stack", "backend", "memory", "infra"]

        signals = analyzer.analyze(messages, known_projects=known)

        print("Project Signals from Dialogue:")
        if signals:
            for signal in signals:
                print(f"  {signal.project}: {signal.confidence:.1%} (weight: {signal.weight:.2f})")
                if signal.keywords:
                    print(f"    Keywords: {', '.join(signal.keywords[:5])}")
        else:
            print("  No signals detected")

    elif cmd == "test":
        # Test with sample messages
        test_messages = [
            {"role": "user", "content": "fix the webhook injection boundary detection"},
            {"role": "assistant", "content": "Looking at memory/injection_store.py and the boundary_intelligence.py file..."},
            {"role": "user", "content": "also check the celery tasks"},
            {"role": "assistant", "content": "Found the issue in memory/celery_tasks.py at line 234."}
        ]

        print("Test Messages:")
        for msg in test_messages:
            print(f"  [{msg['role']}]: {msg['content'][:60]}...")

        print("\nAnalysis:")
        signals = analyzer.analyze(test_messages)
        for signal in signals:
            print(f"  {signal.project}: {signal.confidence:.1%}")
            if signal.keywords:
                print(f"    Keywords: {', '.join(signal.keywords[:5])}")

        print("\nFull Context:")
        ctx = analyzer.extract_context(test_messages)
        print(f"  File paths: {ctx.file_paths}")
        print(f"  Mentions: {ctx.project_mentions[:5]}")
        print(f"  Keywords: {ctx.domain_keywords[:10]}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
