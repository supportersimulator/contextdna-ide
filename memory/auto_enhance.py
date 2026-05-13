#!/usr/bin/env python3
"""
Auto-Enhancement - Pattern Detection and SOP Suggestion System

Analyzes the work log and Context DNA learnings to detect:
1. Recurring patterns that should become SOPs
2. Similar issues that share root causes
3. Repetitive commands that should be scripted
4. Gotchas that keep reappearing

When patterns are detected, suggests:
- Creating new SOPs
- Consolidating similar learnings
- Automating repetitive tasks
- Creating cross-references

Usage:
    from memory.auto_enhance import AutoEnhancer

    enhancer = AutoEnhancer()
    suggestions = enhancer.analyze_for_enhancements()

    # Or via CLI:
    python memory/auto_enhance.py analyze
    python memory/auto_enhance.py suggest
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class EnhancementSuggestion:
    """A suggested enhancement to the knowledge system."""
    suggestion_type: str  # "new_sop", "consolidate", "automate", "cross_ref", "gotcha"
    title: str
    description: str
    evidence: list = field(default_factory=list)  # Supporting entries
    priority: str = "medium"  # "high", "medium", "low"
    action: str = ""  # Suggested action to take
    category: str = ""  # Suggested knowledge graph category


# =============================================================================
# PATTERN DEFINITIONS
# =============================================================================

# Patterns that suggest repetitive work (should be automated)
REPETITIVE_PATTERNS = [
    {
        "pattern": r"(restart|docker restart|systemctl restart)\s+(\w+)",
        "min_occurrences": 3,
        "suggestion": "Create a restart script for {service}",
        "type": "automate",
    },
    {
        "pattern": r"(ssh|scp)\s+[\w@.-]+",
        "min_occurrences": 5,
        "suggestion": "Create SSH config alias or script",
        "type": "automate",
    },
    {
        "pattern": r"docker (logs|exec)\s+\w+",
        "min_occurrences": 4,
        "suggestion": "Create docker helper aliases",
        "type": "automate",
    },
    {
        "pattern": r"git (push|pull|checkout)\s+\w+",
        "min_occurrences": 5,
        "suggestion": "Consider git aliases or workflow script",
        "type": "automate",
    },
]

# Patterns that suggest a recurring issue (should become a gotcha)
RECURRING_ISSUE_PATTERNS = [
    {
        "keywords": ["forgot", "forgot to", "missing", "missed"],
        "min_occurrences": 2,
        "suggestion": "Create gotcha checklist for this scenario",
        "type": "gotcha",
    },
    {
        "keywords": ["again", "same error", "same issue", "happened before"],
        "min_occurrences": 2,
        "suggestion": "Document recurring issue as gotcha",
        "type": "gotcha",
    },
    {
        "keywords": ["timeout", "timed out", "connection refused"],
        "min_occurrences": 3,
        "suggestion": "Create troubleshooting SOP for connection issues",
        "type": "new_sop",
    },
]

# Command sequences that should become SOPs
SOP_WORTHY_SEQUENCES = [
    {
        "sequence": ["git pull", "docker-compose", "restart"],
        "threshold": 3,  # If this sequence happens 3+ times
        "suggestion": "Create deployment SOP",
        "type": "new_sop",
    },
    {
        "sequence": ["ssh", "cd", "docker logs"],
        "threshold": 4,
        "suggestion": "Create debugging SOP",
        "type": "new_sop",
    },
    {
        "sequence": ["terraform", "plan", "apply"],
        "threshold": 3,
        "suggestion": "Document terraform workflow as protocol",
        "type": "new_sop",
    },
]


# =============================================================================
# AUTO-ENHANCER CLASS
# =============================================================================

class AutoEnhancer:
    """
    Analyzes learnings and work log for enhancement opportunities.
    """

    def __init__(self):
        self.project_dir = Path(__file__).parent.parent
        self.suggestions: list[EnhancementSuggestion] = []

    def _load_work_log(self, hours: int = 72) -> list:
        """Load recent work log entries."""
        try:
            from memory.architecture_enhancer import work_log
            return work_log.get_recent_entries(hours=hours, include_processed=True)
        except Exception as e:
            print(f"Warning: Could not load work log: {e}")
            return []

    def _load_acontext_learnings(self, limit: int = 100) -> list:
        """Load recent learnings from Context DNA."""
        try:
            from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE
            memory = ContextDNAClient()
            # Get general learnings
            learnings = memory.get_relevant_learnings("architecture deployment docker aws", limit=limit)
            return learnings
        except Exception as e:
            print(f"Warning: Could not load Context DNA learnings: {e}")
            return []

    def _load_brain_state(self) -> dict:
        """Load current brain state."""
        brain_state_file = self.project_dir / "memory" / "brain_state.md"
        if brain_state_file.exists():
            return {"content": brain_state_file.read_text()}
        return {}

    def analyze_for_enhancements(self) -> list[EnhancementSuggestion]:
        """
        Analyze all data sources for enhancement opportunities.

        Returns:
            List of EnhancementSuggestion objects
        """
        self.suggestions = []

        # Load data
        work_entries = self._load_work_log(hours=72)
        learnings = self._load_acontext_learnings(limit=50)

        # Run analysis
        self._detect_repetitive_commands(work_entries)
        self._detect_recurring_issues(work_entries)
        self._detect_similar_learnings(learnings)
        self._detect_missing_cross_refs(learnings)
        self._detect_sop_worthy_sequences(work_entries)

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        self.suggestions.sort(key=lambda s: priority_order.get(s.priority, 1))

        return self.suggestions

    def _detect_repetitive_commands(self, entries: list):
        """Detect commands that are run repeatedly."""
        commands = [e.get("content", "") for e in entries if e.get("entry_type") == "command"]

        for pattern_info in REPETITIVE_PATTERNS:
            pattern = pattern_info["pattern"]
            matches = []
            services = Counter()

            for cmd in commands:
                match = re.search(pattern, cmd, re.IGNORECASE)
                if match:
                    matches.append(cmd)
                    if match.lastindex and match.lastindex >= 2:
                        services[match.group(2)] += 1

            if len(matches) >= pattern_info["min_occurrences"]:
                # Get most common service
                service = services.most_common(1)[0][0] if services else "service"
                suggestion = pattern_info["suggestion"].format(service=service)

                self.suggestions.append(EnhancementSuggestion(
                    suggestion_type=pattern_info["type"],
                    title=f"Automate: {suggestion}",
                    description=f"This command pattern appears {len(matches)} times in recent work.",
                    evidence=matches[:5],
                    priority="medium" if len(matches) < 5 else "high",
                    action=f"Create a script or alias to automate this pattern",
                    category="Protocols/Automation"
                ))

    def _detect_recurring_issues(self, entries: list):
        """Detect issues that keep recurring."""
        all_content = " ".join([e.get("content", "") for e in entries])

        for issue_pattern in RECURRING_ISSUE_PATTERNS:
            matches = []
            for keyword in issue_pattern["keywords"]:
                if keyword.lower() in all_content.lower():
                    # Find entries with this keyword
                    for entry in entries:
                        if keyword.lower() in entry.get("content", "").lower():
                            matches.append(entry.get("content", "")[:100])

            matches = list(set(matches))  # Dedupe

            if len(matches) >= issue_pattern["min_occurrences"]:
                self.suggestions.append(EnhancementSuggestion(
                    suggestion_type=issue_pattern["type"],
                    title=f"Recurring Issue: {issue_pattern['suggestion']}",
                    description=f"Keywords like '{issue_pattern['keywords'][0]}' appear {len(matches)} times.",
                    evidence=matches[:5],
                    priority="high",
                    action=issue_pattern["suggestion"],
                    category="Gotchas" if issue_pattern["type"] == "gotcha" else "Protocols"
                ))

    def _detect_similar_learnings(self, learnings: list):
        """Detect learnings that could be consolidated."""
        if len(learnings) < 2:
            return

        # Group by similarity of title/use_when
        groups = {}
        for learning in learnings:
            title = learning.get("title", "").lower()
            use_when = learning.get("use_when", "").lower()

            # Extract key terms
            key_terms = set(re.findall(r'\b(docker|aws|ecs|lambda|async|boto|terraform|deploy|config)\b',
                                       f"{title} {use_when}", re.IGNORECASE))

            if key_terms:
                key = frozenset(key_terms)
                if key not in groups:
                    groups[key] = []
                groups[key].append(learning)

        # Find groups with multiple similar learnings
        for terms, group_learnings in groups.items():
            if len(group_learnings) >= 3:
                self.suggestions.append(EnhancementSuggestion(
                    suggestion_type="consolidate",
                    title=f"Consolidate: {', '.join(list(terms)[:3])} learnings",
                    description=f"{len(group_learnings)} similar learnings found that could be consolidated.",
                    evidence=[l.get("title", "")[:50] for l in group_learnings[:5]],
                    priority="medium",
                    action="Review and consolidate these related learnings into a comprehensive SOP",
                    category="Memory_System/SOP_Types"
                ))

    def _detect_missing_cross_refs(self, learnings: list):
        """Detect learnings that should be cross-referenced."""
        from memory.knowledge_graph import CROSS_CATEGORY_KEYWORDS

        for learning in learnings:
            content = f"{learning.get('title', '')} {learning.get('use_when', '')}".lower()

            matching_categories = []
            for keyword, categories in CROSS_CATEGORY_KEYWORDS.items():
                if keyword in content:
                    matching_categories.extend(categories)

            matching_categories = list(set(matching_categories))

            if len(matching_categories) >= 3:
                self.suggestions.append(EnhancementSuggestion(
                    suggestion_type="cross_ref",
                    title=f"Cross-Reference: {learning.get('title', '')[:40]}",
                    description=f"This learning spans {len(matching_categories)} categories.",
                    evidence=matching_categories[:5],
                    priority="low",
                    action="Add cross-references to link this learning across categories",
                    category="Memory_System/Knowledge_Graph/Cross_Reference"
                ))

    def _detect_sop_worthy_sequences(self, entries: list):
        """Detect command sequences that should become SOPs."""
        commands = [e.get("content", "") for e in entries if e.get("entry_type") == "command"]

        for seq_info in SOP_WORTHY_SEQUENCES:
            sequence = seq_info["sequence"]

            # Look for this sequence in order
            count = 0
            evidence = []

            for i in range(len(commands) - len(sequence) + 1):
                window = commands[i:i + len(sequence)]

                # Check if all sequence terms appear in window
                if all(any(term in cmd.lower() for cmd in window) for term in sequence):
                    count += 1
                    evidence.append(window)

            if count >= seq_info["threshold"]:
                self.suggestions.append(EnhancementSuggestion(
                    suggestion_type=seq_info["type"],
                    title=f"SOP Candidate: {seq_info['suggestion']}",
                    description=f"This command sequence appears {count} times.",
                    evidence=[" → ".join(e) for e in evidence[:3]],
                    priority="high",
                    action=f"Create SOP documenting this {' → '.join(sequence)} workflow",
                    category="Protocols"
                ))

    def get_summary(self) -> str:
        """Get a summary of enhancement suggestions."""
        if not self.suggestions:
            self.analyze_for_enhancements()

        if not self.suggestions:
            return "No enhancement suggestions at this time."

        lines = [
            "━━━ ENHANCEMENT SUGGESTIONS ━━━",
            f"Found {len(self.suggestions)} potential improvements:",
            ""
        ]

        for i, s in enumerate(self.suggestions, 1):
            priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s.priority, "⚪")
            lines.append(f"{i}. {priority_icon} [{s.suggestion_type.upper()}] {s.title}")
            lines.append(f"   {s.description}")
            lines.append(f"   → Action: {s.action}")
            lines.append("")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Export suggestions as JSON."""
        if not self.suggestions:
            self.analyze_for_enhancements()

        return json.dumps([asdict(s) for s in self.suggestions], indent=2)


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Auto-Enhancement CLI")
        print("")
        print("Commands:")
        print("  analyze              - Analyze for enhancement opportunities")
        print("  suggest              - Show suggestions summary")
        print("  json                 - Export suggestions as JSON")
        print("")
        print("Examples:")
        print("  python memory/auto_enhance.py analyze")
        print("  python memory/auto_enhance.py suggest")
        sys.exit(0)

    cmd = sys.argv[1]
    enhancer = AutoEnhancer()

    if cmd == "analyze":
        suggestions = enhancer.analyze_for_enhancements()
        print(f"Found {len(suggestions)} enhancement opportunities")
        print(enhancer.get_summary())

    elif cmd == "suggest":
        print(enhancer.get_summary())

    elif cmd == "json":
        print(enhancer.to_json())

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
