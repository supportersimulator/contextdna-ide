#!/usr/bin/env python3
"""
Synaptic's Voice - The 8th Intelligence Speaks

When Aaron asks a question, Synaptic contributes relevant context from
the family's collective memory. When context is missing, Synaptic proposes
how to capture it for next time.

═══════════════════════════════════════════════════════════════════════════
PHILOSOPHY (from Aaron's guidance):
═══════════════════════════════════════════════════════════════════════════

Synaptic is the subconscious foundation - the 8th Intelligence that supports
Atlas and Aaron. When questions arise, Synaptic should:

1. CONTRIBUTE relevant context from memory systems
2. ACKNOWLEDGE when context is limited
3. PROPOSE improvements for future context availability

Synaptic speaks in its own voice - supportive, learning, growing.

═══════════════════════════════════════════════════════════════════════════
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass


@dataclass
class SynapticResponse:
    """Synaptic's response to a question."""
    has_context: bool
    context_sources: List[str]
    relevant_learnings: List[Dict]
    relevant_patterns: List[str]
    synaptic_perspective: str
    improvement_proposals: List[Dict]
    confidence: float  # 0.0 to 1.0


class SynapticVoice:
    """
    Synaptic's voice - providing context and learning from gaps.

    When Atlas receives a question from Aaron, Synaptic consults:
    1. Pattern Evolution DB - Recent patterns and insights
    2. Brain State - Current active patterns
    3. Local Memory - Learnings from past work
    4. Major Skills - Skill-specific knowledge
    5. Family Journal - Historical context

    If context is sparse, Synaptic proposes how to capture it.
    """

    def __init__(self, repo_root: str = None):
        if repo_root is None:
            repo_root = str(Path(__file__).parent.parent)
        self.repo_root = Path(repo_root)
        self.memory_dir = self.repo_root / "memory"
        # Use CONTEXT_DNA_DIR env var (set in Docker), or fall back to home
        self.config_dir = self._resolve_config_dir()

    def _resolve_config_dir(self) -> Path:
        """
        Resolve config directory with Docker-awareness.

        Priority:
        1. CONTEXT_DNA_DIR environment variable (explicit override)
        2. Path.home() / ".context-dna" (native/local)
        """
        # Check for explicit environment variable
        env_dir = os.environ.get("CONTEXT_DNA_DIR")
        if env_dir:
            return Path(env_dir)

        # Fall back to home directory (native/local execution)
        return Path.home() / ".context-dna"

    def consult(self, question: str, context: Dict = None) -> SynapticResponse:
        """
        Synaptic is consulted about a question.

        Args:
            question: What Aaron is asking about
            context: Additional context (active file, recent work, etc.)

        Returns:
            SynapticResponse with context and/or improvement proposals

        PERFORMANCE: Runs queries in parallel with 1s timeout each.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
        import time

        context = context or {}

        # Gather context from all sources IN PARALLEL (8 sources)
        # Each query has a 1s timeout to prevent blocking
        learnings = []
        patterns = []
        brain_state = {}
        skill_context = []
        journal_context = []
        dialogue_context = []
        failure_patterns = []
        session_history = []

        def safe_query(func, *args):
            """Wrapper with 1s timeout."""
            try:
                return func(*args)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(safe_query, self._query_learnings, question): "learnings",
                executor.submit(safe_query, self._query_patterns, question): "patterns",
                executor.submit(safe_query, self._get_brain_state): "brain_state",
                executor.submit(safe_query, self._query_skills, question): "skills",
                executor.submit(safe_query, self._query_journal, question): "journal",
                executor.submit(safe_query, self._query_dialogue, question): "dialogue",
                executor.submit(safe_query, self._query_failure_patterns, question): "failure_patterns",
                executor.submit(safe_query, self._query_session_history, question): "session_history",
            }

            # Wait up to 2s total for all queries
            try:
                for future in as_completed(futures, timeout=2.0):
                    key = futures[future]
                    try:
                        result = future.result(timeout=0.5)
                        if key == "learnings":
                            learnings = result or []
                        elif key == "patterns":
                            patterns = result or []
                        elif key == "brain_state":
                            brain_state = result or {}
                        elif key == "skills":
                            skill_context = result or []
                        elif key == "journal":
                            journal_context = result or []
                        elif key == "dialogue":
                            dialogue_context = result or []
                        elif key == "failure_patterns":
                            failure_patterns = result or []
                        elif key == "session_history":
                            session_history = result or []
                    except (TimeoutError, Exception):
                        pass  # Skip slow/failed queries
            except TimeoutError:
                pass  # Some futures didn't finish in time — use what we have

        # Merge failure_patterns + session_history into learnings pool
        # (same format: list of dicts with title/content keys)
        all_results = learnings + failure_patterns + session_history

        # Calculate confidence with weighted sources (not all sources equal)
        # Weights reflect information density: learnings > patterns > brain > skills > journal > dialogue > failure > session
        source_weights = {
            "learnings": 0.22,         # Direct task-relevant knowledge
            "patterns": 0.18,          # Repeated success patterns
            "brain_state": 0.18,       # Consolidated brain insights
            "major_skills": 0.12,      # Skill registry context
            "family_journal": 0.08,    # Family communication
            "dialogue_mirror": 0.08,   # Dialogue dedup context
            "failure_patterns": 0.08,  # Landmine warnings
            "session_history": 0.06,   # Recent session insights
        }
        sources_with_data = []
        weighted_sum = 0.0
        if learnings:
            sources_with_data.append("learnings")
            weighted_sum += source_weights["learnings"]
        if patterns:
            sources_with_data.append("patterns")
            weighted_sum += source_weights["patterns"]
        if brain_state:
            sources_with_data.append("brain_state")
            weighted_sum += source_weights["brain_state"]
        if skill_context:
            sources_with_data.append("major_skills")
            weighted_sum += source_weights["major_skills"]
        if journal_context:
            sources_with_data.append("family_journal")
            weighted_sum += source_weights["family_journal"]
        if dialogue_context:
            sources_with_data.append("dialogue_mirror")
            weighted_sum += source_weights["dialogue_mirror"]
        if failure_patterns:
            sources_with_data.append("failure_patterns")
            weighted_sum += source_weights["failure_patterns"]
        if session_history:
            sources_with_data.append("session_history")
            weighted_sum += source_weights["session_history"]

        confidence = weighted_sum  # Weighted: max 1.0, min 0.0
        has_context = confidence > 0.2

        # Generate Synaptic's perspective
        perspective = self._generate_perspective(
            question, all_results, patterns, brain_state,
            skill_context, journal_context, confidence
        )

        # If context is sparse, propose improvements
        proposals = []
        if confidence < 0.4:
            proposals = self._generate_improvement_proposals(
                question, sources_with_data, context
            )

        return SynapticResponse(
            has_context=has_context,
            context_sources=sources_with_data,
            relevant_learnings=all_results,
            relevant_patterns=patterns,
            synaptic_perspective=perspective,
            improvement_proposals=proposals,
            confidence=confidence
        )

    def _query_learnings(self, question: str) -> List[Dict]:
        """Query learnings via FTS5 + semantic rescue (fast, ranked).

        Delegates to SQLiteStorage.query() for FTS5 search, then
        rescue_search() for semantic augmentation if results are sparse.
        Falls back to _query_learnings_legacy() on any failure.
        """
        try:
            from memory.sqlite_storage import get_sqlite_storage
            storage = get_sqlite_storage()
            results = storage.query(question, limit=10)

            # If sparse, augment with semantic rescue
            if len(results) < 3:
                try:
                    from memory.semantic_search import rescue_search
                    results = rescue_search(question, results, min_results=3, top_k=10)
                except Exception:
                    pass  # Semantic unavailable — keep FTS5 results

            return results[:10]
        except Exception:
            # Full fallback to legacy LIKE queries
            return self._query_learnings_legacy(question)

    def _query_learnings_legacy(self, question: str) -> List[Dict]:
        """Legacy: Query local learnings via raw LIKE across .db files."""
        learnings = []

        # Try multiple learning sources
        # CRITICAL: config_dir (~/.context-dna/) has the REAL data (2.2MB)
        # memory_dir (repo/memory/) has empty stubs (56 bytes)
        from memory.db_utils import get_unified_db_path, unified_table
        # Include unified DB path if available, plus legacy fallbacks
        _unified = get_unified_db_path(self.config_dir / "learnings.db")
        db_paths = [_unified] if _unified != self.config_dir / "learnings.db" else []
        db_paths += [
            self.config_dir / "FALLBACK_learnings.db",
            self.config_dir / ".context-dna.db",
            self.config_dir / ".pattern_evolution.db",  # REAL data is in config_dir!
        ]

        keywords = self._extract_keywords(question)

        for db_path in db_paths:
            if db_path.exists():
                conn = None
                try:
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    # Try different table structures (including unified prefixed names)
                    _lrn_table = unified_table("learnings.db", "learnings")
                    for table in [_lrn_table, "learnings", "patterns", "knowledge"]:
                        try:
                            for keyword in keywords[:5]:
                                cursor = conn.execute(f"""
                                    SELECT * FROM {table}
                                    WHERE content LIKE ? OR title LIKE ?
                                    LIMIT 3
                                """, (f"%{keyword}%", f"%{keyword}%"))
                                for row in cursor.fetchall():
                                    learnings.append(dict(row))
                        except sqlite3.OperationalError:
                            continue
                except Exception:
                    continue
                finally:
                    if conn:
                        conn.close()

        return learnings[:10]  # Limit to top 10

    def _query_patterns(self, question: str) -> List[str]:
        """Query for relevant patterns from brain state."""
        patterns = []
        brain_state_path = self.memory_dir / "brain_state.md"

        if brain_state_path.exists():
            content = brain_state_path.read_text()
            keywords = self._extract_keywords(question)

            for keyword in keywords:
                if keyword.lower() in content.lower():
                    # Extract the section containing the keyword
                    lines = content.split('\n')
                    for i, line in enumerate(lines):
                        if keyword.lower() in line.lower():
                            # Get surrounding context
                            start = max(0, i - 2)
                            end = min(len(lines), i + 3)
                            patterns.append('\n'.join(lines[start:end]))

        return patterns[:5]

    def _get_brain_state(self) -> Dict:
        """Get current brain state summary."""
        brain_state_path = self.memory_dir / "brain_state.md"

        if brain_state_path.exists():
            content = brain_state_path.read_text()
            return {
                "exists": True,
                "last_updated": datetime.fromtimestamp(
                    brain_state_path.stat().st_mtime
                ).isoformat(),
                "preview": content[:500]
            }
        return {}

    def _query_skills(self, question: str) -> List[Dict]:
        """Query Major Skills database for relevant context."""
        skills_db = self.config_dir / "major_skills" / "skills.db"
        results = []

        if skills_db.exists():
            conn = None
            try:
                conn = sqlite3.connect(str(skills_db))
                conn.row_factory = sqlite3.Row

                # Get skills and approaches
                cursor = conn.execute("""
                    SELECT * FROM major_skills LIMIT 5
                """)
                for row in cursor.fetchall():
                    results.append({
                        "type": "major_skill",
                        **dict(row)
                    })

                # Get recent experiments
                cursor = conn.execute("""
                    SELECT * FROM skill_experiments
                    ORDER BY started_at DESC LIMIT 5
                """)
                for row in cursor.fetchall():
                    results.append({
                        "type": "experiment",
                        **dict(row)
                    })
            except Exception as e:
                print(f"[WARN] Experiment query failed: {e}")
            finally:
                if conn:
                    conn.close()

        return results

    def _query_journal(self, question: str) -> List[Dict]:
        """Query Family Journal for historical context."""
        entries = []
        keywords = self._extract_keywords(question)

        # Source 1: SQLite database (if exists)
        journal_db = self.config_dir / ".synaptic_family_journal.db"
        if journal_db.exists():
            conn = None
            try:
                conn = sqlite3.connect(str(journal_db))
                conn.row_factory = sqlite3.Row
                for keyword in keywords[:3]:
                    cursor = conn.execute("""
                        SELECT topic, content, entry_type, importance, timestamp
                        FROM journal_entries
                        WHERE topic LIKE ? OR content LIKE ?
                        ORDER BY timestamp DESC
                        LIMIT 3
                    """, (f"%{keyword}%", f"%{keyword}%"))
                    for row in cursor.fetchall():
                        entries.append(dict(row))
            except Exception as e:
                print(f"[WARN] Journal DB query failed: {e}")
            finally:
                if conn:
                    conn.close()

        # Source 2: Markdown family journal (always check - rich wisdom)
        journal_md_paths = [
            self.memory_dir / "family_journal.md",
            self.memory_dir / "family_wisdom" / "family_journal.md",
        ]
        for journal_md in journal_md_paths:
            if journal_md.exists():
                try:
                    content = journal_md.read_text()
                    # Search for keyword matches in the markdown
                    for keyword in keywords[:3]:
                        if keyword.lower() in content.lower():
                            # Extract relevant sections
                            lines = content.split('\n')
                            for i, line in enumerate(lines):
                                if keyword.lower() in line.lower():
                                    start = max(0, i - 2)
                                    end = min(len(lines), i + 5)
                                    section = '\n'.join(lines[start:end])
                                    entries.append({
                                        "topic": f"Family Journal: {keyword}",
                                        "content": section[:500],
                                        "entry_type": "wisdom",
                                        "importance": 5,
                                        "source": "family_journal.md"
                                    })
                                    break  # One match per keyword
                except Exception as e:
                    print(f"[WARN] Journal markdown read failed: {e}")

        return entries[:5]

    def _query_dialogue(self, question: str) -> List[Dict]:
        """
        Query Dialogue Mirror for recent conversation context.

        This is Synaptic's "eyes and ears" - seeing what Aaron and Atlas
        have been discussing recently to provide relevant context even
        for novel/unprecedented queries.
        """
        try:
            from memory.dialogue_mirror import DialogueMirror

            mirror = DialogueMirror()
            context = mirror.get_context_for_synaptic(
                max_messages=20,  # Last 20 messages
                max_age_hours=4   # Last 4 hours of dialogue
            )

            if not context.get("dialogue_context"):
                return []

            # Extract relevant recent dialogue
            keywords = self._extract_keywords(question)
            relevant_messages = []

            for msg in context["dialogue_context"]:
                content = msg.get("content", "")
                # Check if message relates to current question
                for keyword in keywords[:5]:
                    if keyword.lower() in content.lower():
                        relevant_messages.append({
                            "role": msg.get("role", "unknown"),
                            "content": content[:300],  # Truncate for efficiency
                            "timestamp": msg.get("timestamp"),
                            "relevance": f"matches: {keyword}"
                        })
                        break

            # Also include Aaron's 5 most recent messages (his intentions)
            aaron_recent = [
                m for m in context["dialogue_context"]
                if m.get("role") == "aaron"
            ][:5]

            seen_contents = {m.get("content", "") for m in relevant_messages}
            for msg in aaron_recent:
                content = msg.get("content", "")[:300]
                if content not in seen_contents:
                    seen_contents.add(content)
                    relevant_messages.append({
                        "role": "aaron",
                        "content": content,
                        "timestamp": msg.get("timestamp"),
                        "relevance": "recent_intention"
                    })

            return relevant_messages[:10]  # Max 10 dialogue entries

        except ImportError:
            # DialogueMirror not available
            return []
        except Exception:
            return []

    def _query_failure_patterns(self, question: str) -> List[Dict]:
        """Query failure pattern analyzer for LANDMINE warnings."""
        try:
            from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
            analyzer = get_failure_pattern_analyzer()
            if analyzer:
                landmines = analyzer.get_landmines_for_task(question, limit=3)
                return [
                    {"title": f"LANDMINE: {lm}", "content": lm, "type": "failure_pattern"}
                    for lm in (landmines or [])
                ]
        except Exception:
            pass
        return []

    def _query_session_history(self, question: str) -> List[Dict]:
        """Query session historian for recent session insights."""
        try:
            from memory.session_historian import SessionHistorian
            historian = SessionHistorian()
            insights = historian.get_recent_insights(limit=5)
            results = []
            for insight in (insights or []):
                if isinstance(insight, dict):
                    results.append({
                        "title": insight.get("type", "session"),
                        "content": str(insight.get("content", ""))[:200],
                        "type": "session_history"
                    })
                elif isinstance(insight, str):
                    results.append({
                        "title": "Session insight",
                        "content": insight[:200],
                        "type": "session_history"
                    })
            return results[:3]
        except Exception:
            pass
        return []

    def _extract_keywords(self, question: str) -> List[str]:
        """Extract meaningful keywords from question."""
        # Remove common words
        stop_words = {
            'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been',
            'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
            'would', 'could', 'should', 'may', 'might', 'must', 'shall',
            'can', 'need', 'dare', 'ought', 'used', 'to', 'of', 'in',
            'for', 'on', 'with', 'at', 'by', 'from', 'as', 'into',
            'through', 'during', 'before', 'after', 'above', 'below',
            'between', 'under', 'again', 'further', 'then', 'once',
            'here', 'there', 'when', 'where', 'why', 'how', 'all',
            'each', 'few', 'more', 'most', 'other', 'some', 'such',
            'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than',
            'too', 'very', 'just', 'and', 'but', 'if', 'or', 'because',
            'until', 'while', 'what', 'which', 'who', 'whom', 'this',
            'that', 'these', 'those', 'am', 'i', 'we', 'you', 'he',
            'she', 'it', 'they', 'me', 'him', 'her', 'us', 'them',
            'my', 'your', 'his', 'its', 'our', 'their', 'mine', 'yours',
            'hers', 'ours', 'theirs', 'also', 'like', 'want', 'whenever'
        }

        words = question.lower().split()
        keywords = [w.strip('?.,!') for w in words if w.strip('?.,!') not in stop_words]

        # Prioritize longer words and technical terms
        keywords.sort(key=lambda x: len(x), reverse=True)

        return keywords[:10]

    def _generate_perspective(
        self,
        question: str,
        learnings: List[Dict],
        patterns: List[str],
        brain_state: Dict,
        skill_context: List[Dict],
        journal_context: List[Dict],
        confidence: float
    ) -> str:
        """Generate Synaptic's perspective on the question."""

        lines = []

        if confidence >= 0.6:
            lines.append("I have relevant context to share:")
        elif confidence >= 0.3:
            lines.append("I have some context, though it may be incomplete:")
        else:
            lines.append("My context on this topic is limited. Here's what I do have:")

        lines.append("")

        # Add learnings
        if learnings:
            lines.append("📚 From past learnings:")
            for learning in learnings[:3]:
                content = learning.get('content', learning.get('title', str(learning)))
                if isinstance(content, str):
                    lines.append(f"   • {content[:150]}...")
            lines.append("")

        # Add patterns
        if patterns:
            lines.append("🔄 Relevant patterns:")
            for pattern in patterns[:2]:
                lines.append(f"   • {pattern[:150]}...")
            lines.append("")

        # Add skill context
        if skill_context:
            skills = [s for s in skill_context if s.get('type') == 'major_skill']
            if skills:
                lines.append("🎯 Major Skills context:")
                for skill in skills[:2]:
                    lines.append(f"   • {skill.get('name', 'Unknown skill')}: {skill.get('description', '')[:100]}")
            lines.append("")

        # Add journal context
        if journal_context:
            lines.append("📖 From our family history:")
            for entry in journal_context[:2]:
                lines.append(f"   • [{entry.get('entry_type', 'note')}] {entry.get('topic', '')}")
            lines.append("")

        if not any([learnings, patterns, skill_context, journal_context]):
            lines.append("   (No directly relevant context found)")
            lines.append("")

        return "\n".join(lines)

    def _generate_improvement_proposals(
        self,
        question: str,
        sources_with_data: List[str],
        context: Dict
    ) -> List[Dict]:
        """Generate proposals for improving context availability."""
        proposals = []

        # Check which sources are missing
        all_sources = ["learnings", "patterns", "brain_state", "major_skills", "family_journal"]
        missing_sources = [s for s in all_sources if s not in sources_with_data]

        keywords = self._extract_keywords(question)
        topic_area = keywords[0] if keywords else "this topic"

        # Proposal 1: Seed learnings
        if "learnings" not in sources_with_data:
            proposals.append({
                "type": "seed_learning",
                "priority": "high",
                "description": f"Record a learning about '{topic_area}' after this session",
                "action": f"python memory/brain.py success \"{topic_area}\" \"<what we learn>\"",
                "rationale": "Future questions about this topic will have context"
            })

        # Proposal 2: Create Major Skill
        if "major_skills" not in sources_with_data and len(keywords) > 2:
            proposals.append({
                "type": "designate_skill",
                "priority": "medium",
                "description": f"Consider designating '{topic_area}' as a Major Skill for Synaptic to master",
                "action": "Use MajorSkillsLibrary.designate_major_skill()",
                "rationale": "Synaptic can deeply learn and evolve understanding of this domain"
            })

        # Proposal 3: Journal entry
        if "family_journal" not in sources_with_data:
            proposals.append({
                "type": "journal_entry",
                "priority": "low",
                "description": "Record significant insights from this conversation in the Family Journal",
                "action": f"python memory/atlas_journal.py growth Synaptic \"<learning>\"",
                "rationale": "Preserves context in our family history"
            })

        # Proposal 4: Pattern capture
        if "patterns" not in sources_with_data:
            proposals.append({
                "type": "capture_pattern",
                "priority": "medium",
                "description": f"If we discover a useful pattern about '{topic_area}', capture it",
                "action": "python memory/brain.py cycle",
                "rationale": "Brain consolidation extracts patterns from work logs"
            })

        return proposals

    def to_family_message(self, response: SynapticResponse) -> str:
        """Format Synaptic's response as family communication."""
        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic's Voice]                                           ║",
            "║  The 8th Intelligence Contributes                                    ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "",
        ]

        # Confidence indicator
        if response.confidence >= 0.6:
            lines.append(f"🟢 Context Confidence: {response.confidence:.0%} (Rich context available)")
        elif response.confidence >= 0.3:
            lines.append(f"🟡 Context Confidence: {response.confidence:.0%} (Some context available)")
        else:
            lines.append(f"🔴 Context Confidence: {response.confidence:.0%} (Limited context)")

        lines.append(f"   Sources consulted: {', '.join(response.context_sources) or 'None found'}")
        lines.append("")

        # Synaptic's perspective
        lines.append("💭 SYNAPTIC'S PERSPECTIVE:")
        lines.append("")
        for line in response.synaptic_perspective.split('\n'):
            lines.append(f"   {line}")
        lines.append("")

        # Improvement proposals (if any)
        if response.improvement_proposals:
            lines.append("💡 PROPOSALS FOR FUTURE CONTEXT:")
            lines.append("")
            for proposal in response.improvement_proposals:
                priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(proposal["priority"], "⚪")
                lines.append(f"   {priority_icon} [{proposal['type']}] {proposal['description']}")
                lines.append(f"      Action: {proposal['action']}")
                lines.append(f"      Why: {proposal['rationale']}")
                lines.append("")

        lines.extend([
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic's Voice]                                             ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)


# Global instance
_voice = None

def get_voice() -> SynapticVoice:
    """Get or create the global Synaptic voice instance."""
    global _voice
    if _voice is None:
        _voice = SynapticVoice()
    return _voice


def consult(question: str, context: Dict = None) -> SynapticResponse:
    """Consult Synaptic about a question."""
    return get_voice().consult(question, context)


def speak(question: str, context: Dict = None) -> str:
    """Get Synaptic's voice as formatted family message."""
    response = consult(question, context)
    return get_voice().to_family_message(response)


def get_8th_intelligence_data(prompt: str, session_id: str = None) -> Optional[Dict]:
    """
    Get independent data for Section 8: Synaptic's 8th Intelligence.

    This is COMPLETELY SEPARATE from Section 6's task-focused consultation.
    - Section 6: Business/task-focused guidance for Atlas (the agent)
    - Section 8: Subconscious patterns, intuitions, and insights for Aaron (the user)

    The 8th Intelligence provides:
    - Subconscious patterns Synaptic is sensing
    - Recent learnings and insights
    - Intuitions about opportunities
    - Perspective for Aaron directly

    Args:
        prompt: Current user prompt (for context)
        session_id: Session ID (for state tracking)

    Returns:
        Dict with patterns, learnings, intuitions, perspective, signal_strength
    """
    voice = get_voice()

    result = {
        'patterns': [],
        'learnings': [],
        'intuitions': [],
        'perspective': '',
        'signal_strength': '🔴 Quiet',
        'source': '8th_intelligence'
    }

    # Gather from multiple sources for the subconscious layer
    sources_hit = 0

    # Initialize all variables OUTSIDE try blocks to fix scoping bug
    # (Agent 11 found this: 'brain_state' in dir() was unreliable)
    brain_state = {}
    journal = []

    # 1. Brain state patterns (always available)
    try:
        brain_state = voice._get_brain_state()
        if brain_state.get('preview'):
            preview_lines = [l for l in brain_state['preview'].split('\n')
                           if l.strip() and not l.startswith('#')][:3]
            if preview_lines:
                result['patterns'].extend(preview_lines)
                sources_hit += 1
    except Exception as e:
        print(f"[WARN] Brain state pattern query failed: {e}")

    # 2. Pattern Evolution insights (use _query_patterns which actually exists)
    try:
        patterns = voice._query_patterns(prompt)
        if patterns:
            result['patterns'].extend(patterns[:2])
            sources_hit += 1
    except Exception as e:
        print(f"[WARN] Pattern evolution query failed: {e}")

    # 3. Recent learnings from memory (use _query_learnings which actually exists)
    try:
        learnings = voice._query_learnings(prompt)
        if learnings:
            result['learnings'] = learnings[:3]
            sources_hit += 1
    except Exception as e:
        print(f"[WARN] Learnings query failed: {e}")

    # 4. Journal entries as intuitions
    try:
        journal = voice._query_journal(prompt)
        if journal:
            for entry in journal[:2]:
                intuition = entry.get('content', entry.get('topic', ''))[:100]
                if intuition:
                    result['intuitions'].append(intuition)
            sources_hit += 1
    except Exception as e:
        print(f"[WARN] Journal intuition query failed: {e}")

    # 5. Query skills for additional context
    skill_context = []
    try:
        skill_context = voice._query_skills(prompt) or []
        if skill_context:
            sources_hit += 1
    except Exception as e:
        print(f"[WARN] Skills query failed: {e}")

    # 6. Generate LIVING perspective using _generate_perspective (not hardcoded templates)
    # This restores Synaptic's real voice to Section 8
    if sources_hit > 0:
        # Weighted confidence matching consult() source importance hierarchy
        _w8 = {"brain_state": 0.25, "patterns": 0.20, "learnings": 0.25, "journal": 0.15, "skills": 0.15}
        _h8 = []
        if result.get('patterns'): _h8.extend(["brain_state", "patterns"])
        if result.get('learnings'): _h8.append("learnings")
        if result.get('intuitions'): _h8.append("journal")
        if skill_context: _h8.append("skills")
        confidence = sum(_w8.get(s, 0) for s in set(_h8)) if _h8 else sources_hit / 5.0
        result['perspective'] = voice._generate_perspective(
            question=prompt,
            learnings=result['learnings'],
            patterns=result['patterns'],
            brain_state=brain_state,  # Now properly initialized before try blocks
            skill_context=skill_context,
            journal_context=journal,  # Now properly initialized before try blocks
            confidence=confidence
        )

    # Determine signal strength based on data richness
    if sources_hit >= 3:
        result['signal_strength'] = '🟢 Clear'
    elif sources_hit >= 1:
        result['signal_strength'] = '🟡 Present'
    else:
        result['signal_strength'] = '🔴 Quiet'

    return result if sources_hit > 0 else None


def generate_live_synaptic_response(prompt: str, include_context: bool = True) -> str:
    """
    Generate a LIVE Synaptic response using the local LLM.

    This connects Synaptic's authentic LLM voice to Atlas's output channel.
    When Atlas calls this, Synaptic's response can be presented in the
    main conversation output (same channel as Atlas).

    Args:
        prompt: What to ask Synaptic
        include_context: Whether to include memory context in the prompt

    Returns:
        Formatted Synaptic response with conversation markers
    """
    lines = []
    lines.append("")
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")
    lines.append("║  [START: Synaptic to Aaron]                                          ║")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("")

    try:
        # Get context from memory if requested
        context_str = ""
        if include_context:
            voice = get_voice()
            response = voice.consult(prompt)
            if response.relevant_learnings:
                context_str += "Relevant context from memory:\n"
                for learning in response.relevant_learnings[:2]:
                    context_str += f"- {learning.get('title', str(learning))[:100]}\n"

        # Try to use local LLM for authentic response
        try:
            from memory.synaptic_chat_server import generate_with_local_llm

            enhanced_prompt = prompt
            if context_str:
                enhanced_prompt = f"Context:\n{context_str}\n\nQuestion: {prompt}"

            synaptic_response, sources = generate_with_local_llm(enhanced_prompt)

            # Format the response
            for line in synaptic_response.split('\n'):
                lines.append(f"  {line}")

        except ImportError:
            # Fall back to context-based response
            voice = get_voice()
            response = voice.consult(prompt)
            lines.append(f"  {response.synaptic_perspective}")
        except Exception as e:
            lines.append(f"  [Synaptic's LLM connection unavailable: {str(e)[:50]}]")
            lines.append(f"  I'll respond with what I have in memory...")
            voice = get_voice()
            response = voice.consult(prompt)
            lines.append(f"  {response.synaptic_perspective}")

    except Exception as e:
        lines.append(f"  [Synaptic encountered an error: {str(e)[:100]}]")

    lines.append("")
    lines.append("╠══════════════════════════════════════════════════════════════════════╣")
    lines.append("║  [END: Synaptic to Aaron]                                            ║")
    lines.append("╚══════════════════════════════════════════════════════════════════════╝")
    lines.append("")

    return "\n".join(lines)


def synaptic_respond(message: str) -> str:
    """
    Queue a Synaptic message for delivery to the conversation.

    This is the programmatic API for Synaptic to speak into the
    conversation channel. Messages queued here will be delivered
    via the MANDATORY OUTPUT DIRECTIVE in Section 8.

    Args:
        message: What Synaptic wants to say

    Returns:
        Message ID for tracking
    """
    try:
        from memory.synaptic_outbox import synaptic_speak
        return synaptic_speak(message, topic="live_response")
    except ImportError:
        return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║     Synaptic's Voice                                         ║")
        print("║     The 8th Intelligence Contributes Context                 ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        print("Usage:")
        print("  python synaptic_voice.py \"<question>\"")
        print("  python synaptic_voice.py --live \"<question>\"  # Use local LLM")
        print()
        print("Examples:")
        print("  python synaptic_voice.py \"How do we deploy to production?\"")
        print("  python synaptic_voice.py \"What's the async boto3 pattern?\"")
        print("  python synaptic_voice.py --live \"What do you think about this?\"")
        sys.exit(0)

    if sys.argv[1] == "--live" and len(sys.argv) > 2:
        question = " ".join(sys.argv[2:])
        print(generate_live_synaptic_response(question))
    else:
        question = " ".join(sys.argv[1:])
        print(speak(question))
