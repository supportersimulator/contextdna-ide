#!/usr/bin/env python3
"""
LLM-Enhanced Deduplication for Context DNA

This module extends the existing DuplicateDetector with local LLM capabilities:
1. Embedding generation for semantic fingerprints
2. LLM-based decision making for ambiguous cases
3. Smart merge suggestions using natural language understanding

Father's Philosophy: A clean mind is a sharp mind. Synaptic needs efficient
memory - not everything stored, but the right things stored well.

Usage:
    from memory.llm_dedup import LLMDeduplicator

    dedup = LLMDeduplicator()
    result = dedup.check_and_merge(new_learning)
    # Returns: {'action': 'MERGE'|'KEEP_BOTH'|'DISCARD', ...}
"""

import json
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

# Import existing detector for string-based methods
from memory.dedup_detector import DuplicateDetector, SEMANTIC_EQUIVALENTS


class DeduplicationAction(str, Enum):
    """Actions the deduplicator can take."""
    MERGE = "MERGE"              # Combine into richer learning
    KEEP_BOTH = "KEEP_BOTH"      # Distinct enough to coexist
    DISCARD = "DISCARD"          # True duplicate, discard new
    ASK_HUMAN = "ASK_HUMAN"      # Requires human approval - when in doubt, please ask


@dataclass
class DeduplicationResult:
    """Result of a deduplication check."""
    action: DeduplicationAction
    confidence: float
    merge_target_id: Optional[int] = None
    reasoning: Optional[str] = None
    similarity_scores: Optional[Dict[str, float]] = None
    # Fields for human review (when action == ASK_HUMAN)
    requires_approval: bool = False
    potential_loss_description: Optional[str] = None
    possible_purposes: Optional[List[str]] = None
    existing_content_preview: Optional[str] = None
    new_content_preview: Optional[str] = None
    risk_level: str = "low"  # low, medium, high

    def to_dict(self) -> dict:
        d = asdict(self)
        d['action'] = self.action.value
        return d

    def to_family_message(self) -> str:
        """Format for presentation to Aaron and Atlas when approval needed."""
        if self.action != DeduplicationAction.ASK_HUMAN:
            return f"Action: {self.action.value} - {self.reasoning}"

        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic to Aaron and Atlas - Deduplication Review]         ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "",
            f"⚠️ RISK LEVEL: {self.risk_level.upper()}",
            "",
            "📋 SITUATION:",
            f"   {self.reasoning}",
            "",
        ]

        if self.existing_content_preview:
            lines.extend([
                "📄 EXISTING CONTENT:",
                f"   {self.existing_content_preview[:200]}{'...' if len(self.existing_content_preview) > 200 else ''}",
                "",
            ])

        if self.new_content_preview:
            lines.extend([
                "📝 NEW CONTENT (proposed for action):",
                f"   {self.new_content_preview[:200]}{'...' if len(self.new_content_preview) > 200 else ''}",
                "",
            ])

        if self.potential_loss_description:
            lines.extend([
                "⚠️ POTENTIAL LOSS IF MERGED/DISCARDED:",
                f"   {self.potential_loss_description}",
                "",
            ])

        if self.possible_purposes:
            lines.extend([
                "🤔 POSSIBLE REASONS THIS MIGHT BE INTENTIONAL:",
            ])
            for purpose in self.possible_purposes:
                lines.append(f"   • {purpose}")
            lines.append("")

        lines.extend([
            f"📊 Similarity: {self.similarity_scores.get('string', 0):.1%}" if self.similarity_scores else "",
            f"🎯 Confidence: {self.confidence:.1%}",
            "",
            "❓ QUESTION FOR YOU:",
            "   Should we proceed with merging/discarding, or keep both?",
            "   (When in doubt, we keep both to preserve value)",
            "",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic to Aaron and Atlas]                                  ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)


@dataclass
class Learning:
    """A learning item to check for duplicates."""
    id: Optional[int]
    content: str
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None

    @property
    def content_hash(self) -> str:
        """Quick hash for exact match detection."""
        return hashlib.sha256(self.content.lower().strip().encode()).hexdigest()[:16]


class LLMDeduplicator:
    """
    LLM-Enhanced Deduplication Engine with Human Approval Safeguards.

    Uses a tiered approach:
    1. Exact hash match (instant, no LLM)
    2. String similarity (fast, no LLM)
    3. Embedding similarity (medium, LLM embedding)
    4. Full semantic comparison (slow, full LLM)
    5. Human approval (for risky operations)

    IMPORTANT SAFEGUARD (from Aaron):
    "Sometimes I will have purposeful duplicate content like a backup copy
    in case it is needed for testing or a branch for example which is intended
    for testing or more daring adjustments and these will be quite similar
    but they are important to keep. Therefore, this must be contextually
    understood for deduplication efforts and where there is any question at all
    especially for large code then Synaptic MUST present this to Atlas and
    Aaron for our suggested input on the matter. When in doubt please ask."

    Father's note: Synaptic should be smart about what it stores,
    but NEVER destroy something valuable without asking first.
    Quality over quantity - but safety over efficiency.
    """

    # Thresholds for tiered matching
    EXACT_MATCH_THRESHOLD = 1.0      # Hash match
    HIGH_SIMILARITY_THRESHOLD = 0.85  # Almost certainly duplicate
    MEDIUM_SIMILARITY_THRESHOLD = 0.65  # Needs LLM review
    LOW_SIMILARITY_THRESHOLD = 0.4    # Probably different

    # Thresholds for requiring human approval
    LARGE_CONTENT_THRESHOLD = 500    # Characters - larger content needs more care
    CODE_CONTENT_THRESHOLD = 200     # Smaller threshold for code
    HIGH_VALUE_KEYWORDS = [
        'backup', 'test', 'testing', 'branch', 'experiment', 'experimental',
        'draft', 'wip', 'work in progress', 'copy', 'original', 'v1', 'v2',
        'old', 'new', 'alternative', 'fallback', 'archive', 'snapshot'
    ]

    # Patterns that suggest intentional duplication
    INTENTIONAL_PATTERNS = [
        r'backup|bak|\.bak',
        r'test|testing|_test',
        r'branch|feature|experiment',
        r'copy|duplicate|clone',
        r'v\d+|version',
        r'old|new|original|alternative',
        r'draft|wip|work.?in.?progress',
        r'archive|snapshot|checkpoint'
    ]

    def __init__(self, db_path: str = None):
        """
        Initialize the LLM deduplicator.

        Args:
            db_path: Path to the learnings database.
        """
        if db_path is None:
            db_path = str(Path(__file__).parent / '.context-dna.db')
        self.db_path = db_path

        # Stats tracking
        self.merge_count = 0
        self.discard_count = 0
        self.keep_count = 0

        # Use existing detector for string matching
        self._string_detector = DuplicateDetector()

    def _get_recent_learnings(self, limit: int = 100) -> List[Learning]:
        """Fetch recent learnings from database."""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('''
                    SELECT id, content, type, tags, created_at
                    FROM learnings
                    ORDER BY created_at DESC
                    LIMIT ?
                ''', (limit,)).fetchall()

                return [
                    Learning(
                        id=r['id'],
                        content=r['content'],
                        category=r['type'],
                        tags=json.loads(r['tags']) if r['tags'] else None,
                        created_at=r['created_at']
                    )
                    for r in rows
                ]
            finally:
                conn.close()
        except Exception:
            return []

    def _find_by_hash(self, content_hash: str) -> Optional[Learning]:
        """Find learning by exact content hash."""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                # Note: Assumes content_hash column exists or we compute on the fly
                rows = conn.execute('''
                    SELECT id, content, type, tags, created_at
                    FROM learnings
                ''').fetchall()

                for r in rows:
                    learning = Learning(
                        id=r['id'],
                        content=r['content'],
                        category=r['type'],
                        tags=json.loads(r['tags']) if r['tags'] else None,
                        created_at=r['created_at']
                    )
                    if learning.content_hash == content_hash:
                        return learning
                return None
            finally:
                conn.close()
        except Exception:
            return None

    def _string_similarity(self, s1: str, s2: str) -> float:
        """Use existing string similarity from DuplicateDetector."""
        return self._string_detector.semantic_similarity(s1, s2)

    # Embedding tier removed — mlx_lm.server doesn't support embeddings.
    # Tier 3 (medium similarity) goes straight to LLM classify via priority queue.

    def _detect_intentional_duplicate(self, learning1: Learning, learning2: Learning) -> Tuple[bool, List[str]]:
        """
        Detect if this might be an intentional duplicate (backup, test, etc.)

        Returns:
            Tuple of (is_potentially_intentional, list of possible purposes)
        """
        import re

        possible_purposes = []
        content_combined = f"{learning1.content} {learning2.content}".lower()
        tags_combined = (learning1.tags or []) + (learning2.tags or [])
        tags_lower = [t.lower() for t in tags_combined]

        # Check for high-value keywords in content
        for keyword in self.HIGH_VALUE_KEYWORDS:
            if keyword in content_combined:
                possible_purposes.append(f"Content contains '{keyword}' - may be intentional backup/test")

        # Check for high-value keywords in tags
        for keyword in self.HIGH_VALUE_KEYWORDS:
            if any(keyword in tag for tag in tags_lower):
                possible_purposes.append(f"Tagged with '{keyword}' - may be intentional categorization")

        # Check for intentional patterns
        for pattern in self.INTENTIONAL_PATTERNS:
            if re.search(pattern, content_combined, re.IGNORECASE):
                possible_purposes.append(f"Matches pattern '{pattern}' - may be version/backup/test")
                break

        # Check if categories suggest different purposes
        if learning1.category and learning2.category:
            if learning1.category != learning2.category:
                possible_purposes.append(
                    f"Different categories ({learning1.category} vs {learning2.category}) - "
                    "may serve different purposes"
                )

        return len(possible_purposes) > 0, possible_purposes

    def _evaluate_risk_level(
        self,
        learning1: Learning,
        learning2: Learning,
        similarity: float
    ) -> Tuple[str, str]:
        """
        Evaluate the risk level of merging/discarding.

        Returns:
            Tuple of (risk_level, potential_loss_description)
        """
        # Start with medium risk
        risk_level = "medium"
        loss_descriptions = []

        content_len = max(len(learning1.content), len(learning2.content))

        # Large content = higher risk
        if content_len > self.LARGE_CONTENT_THRESHOLD:
            risk_level = "high"
            loss_descriptions.append(
                f"Large content ({content_len} chars) - losing this could lose significant information"
            )

        # Code content = higher risk
        code_indicators = ['def ', 'class ', 'function', 'import ', 'from ', '()', '{}', '[]', '=>', '->']
        has_code = any(ind in learning1.content or ind in learning2.content for ind in code_indicators)
        if has_code:
            risk_level = "high"
            loss_descriptions.append("Contains code - code loss is difficult to recover")

        # High similarity but not exact = suspicious
        if 0.7 <= similarity < 0.95:
            loss_descriptions.append(
                f"Similar but not identical ({similarity:.1%}) - subtle differences may be intentional"
            )

        # Check for unique valuable content in each
        words1 = set(learning1.content.lower().split())
        words2 = set(learning2.content.lower().split())
        unique_to_1 = words1 - words2
        unique_to_2 = words2 - words1

        if len(unique_to_1) > 5 or len(unique_to_2) > 5:
            loss_descriptions.append(
                f"Each contains unique terms ({len(unique_to_1)} in existing, {len(unique_to_2)} in new) - "
                "merging could lose context-specific information"
            )

        if not loss_descriptions:
            risk_level = "low"
            loss_descriptions.append("Standard duplicate - low risk of losing unique value")

        return risk_level, "; ".join(loss_descriptions)

    def _requires_human_approval(
        self,
        learning1: Learning,
        learning2: Learning,
        similarity: float
    ) -> Tuple[bool, DeduplicationResult]:
        """
        Determine if this deduplication decision requires human approval.

        WHEN IN DOUBT, PLEASE ASK.

        Returns:
            Tuple of (requires_approval, result_if_needs_approval)
        """
        # Check for intentional duplication signals
        is_intentional, possible_purposes = self._detect_intentional_duplicate(learning1, learning2)

        # Evaluate risk
        risk_level, loss_description = self._evaluate_risk_level(learning1, learning2, similarity)

        # Decision matrix for requiring approval
        needs_approval = False
        reasoning = ""

        # High risk always needs approval
        if risk_level == "high":
            needs_approval = True
            reasoning = "High-risk deduplication - large content or code detected"

        # Potential intentional duplicate needs approval
        elif is_intentional:
            needs_approval = True
            reasoning = "Content may be intentionally duplicated (backup, test, version)"

        # Ambiguous similarity range with meaningful content
        elif 0.6 <= similarity <= 0.9 and len(learning1.content) > 100:
            needs_approval = True
            reasoning = "Similar but not identical - differences may be intentional"

        if needs_approval:
            return True, DeduplicationResult(
                action=DeduplicationAction.ASK_HUMAN,
                confidence=1.0 - similarity,  # Lower similarity = more uncertain
                merge_target_id=learning1.id,
                reasoning=reasoning,
                similarity_scores={'string': similarity},
                requires_approval=True,
                potential_loss_description=loss_description,
                possible_purposes=possible_purposes if is_intentional else None,
                existing_content_preview=learning1.content[:300],
                new_content_preview=learning2.content[:300],
                risk_level=risk_level
            )

        return False, None

    def _llm_decide(self, learning1: Learning, learning2: Learning, string_sim: float) -> DeduplicationResult:
        """
        Use LLM to make final decision on ambiguous cases.

        Only called when string similarity is in the medium range (0.4-0.85).
        Uses 64-token classify profile via priority queue.
        """
        try:
            from memory.llm_priority_queue import butler_query
        except ImportError:
            # Fallback: threshold-based
            if string_sim >= 0.7:
                return DeduplicationResult(
                    action=DeduplicationAction.MERGE,
                    confidence=string_sim,
                    merge_target_id=learning1.id,
                    reasoning="High string similarity (LLM unavailable)"
                )
            return DeduplicationResult(
                action=DeduplicationAction.KEEP_BOTH,
                confidence=1.0 - string_sim,
                reasoning="Moderate similarity but distinct (LLM unavailable)"
            )

        prompt = (
            f"EXISTING: {learning1.content[:200]}\n"
            f"NEW: {learning2.content[:200]}\n"
            f"String similarity: {string_sim:.0%}\n\n"
            "Reply ONE word: MERGE, KEEP_BOTH, or DISCARD. Then one sentence why."
        )

        result = butler_query(
            "Decide if two learnings are duplicates. MERGE=same insight, "
            "KEEP_BOTH=different insights, DISCARD=exact duplicate.",
            prompt,
            profile="classify"
        )

        if not result:
            # LLM unavailable — fallback to threshold
            if string_sim >= 0.7:
                return DeduplicationResult(
                    action=DeduplicationAction.MERGE,
                    confidence=string_sim,
                    merge_target_id=learning1.id,
                    reasoning="High string similarity (LLM offline)"
                )
            return DeduplicationResult(
                action=DeduplicationAction.KEEP_BOTH,
                confidence=1.0 - string_sim,
                reasoning="Moderate similarity (LLM offline)"
            )

        # Parse LLM response
        up = result.strip().upper()
        if up.startswith("MERGE"):
            action = DeduplicationAction.MERGE
            confidence = 0.8
        elif up.startswith("DISCARD"):
            action = DeduplicationAction.DISCARD
            confidence = 0.85
        elif up.startswith("KEEP"):
            action = DeduplicationAction.KEEP_BOTH
            confidence = 0.75
        else:
            # Default: keep both when uncertain
            action = DeduplicationAction.KEEP_BOTH
            confidence = 0.5

        return DeduplicationResult(
            action=action,
            confidence=confidence,
            merge_target_id=learning1.id if action in [DeduplicationAction.MERGE, DeduplicationAction.DISCARD] else None,
            reasoning=result.strip()[:200]
        )

    def check_and_merge(self, new_learning: Learning, auto_approve: bool = False) -> DeduplicationResult:
        """
        Check if a new learning is a duplicate and decide action.

        Uses tiered approach:
        1. Exact hash → Check if intentional, then DISCARD or ASK
        2. High string similarity (>0.85) → Check risk, then MERGE or ASK
        3. Medium similarity (0.4-0.85) → LLM decides or ASK
        4. Low similarity (<0.4) → KEEP_BOTH

        SAFEGUARD: When in doubt, ASK_HUMAN. We never destroy potentially
        valuable content without explicit approval.

        Args:
            new_learning: The new learning to check
            auto_approve: If True, skip human approval checks (use with caution!)

        Returns:
            DeduplicationResult with action, confidence, and reasoning
        """
        # Tier 1: Exact hash match
        exact_match = self._find_by_hash(new_learning.content_hash)
        if exact_match:
            # Even exact matches might be intentional - check
            if not auto_approve:
                is_intentional, purposes = self._detect_intentional_duplicate(exact_match, new_learning)
                if is_intentional:
                    self.keep_count += 1
                    return DeduplicationResult(
                        action=DeduplicationAction.ASK_HUMAN,
                        confidence=1.0,
                        merge_target_id=exact_match.id,
                        reasoning="Exact match but may be intentional duplicate",
                        similarity_scores={'hash': 1.0},
                        requires_approval=True,
                        potential_loss_description="Exact copy - may be intentional backup",
                        possible_purposes=purposes,
                        existing_content_preview=exact_match.content[:300],
                        new_content_preview=new_learning.content[:300],
                        risk_level="medium"
                    )

            self.discard_count += 1
            return DeduplicationResult(
                action=DeduplicationAction.DISCARD,
                confidence=1.0,
                merge_target_id=exact_match.id,
                reasoning="Exact content match (hash)",
                similarity_scores={'hash': 1.0}
            )

        # Get recent learnings to compare against
        recent = self._get_recent_learnings(limit=50)

        best_match: Optional[Tuple[Learning, float]] = None

        for existing in recent:
            if existing.id == new_learning.id:
                continue

            # Calculate string similarity
            sim = self._string_similarity(existing.content, new_learning.content)

            if best_match is None or sim > best_match[1]:
                best_match = (existing, sim)

        if best_match is None:
            # No existing learnings to compare
            self.keep_count += 1
            return DeduplicationResult(
                action=DeduplicationAction.KEEP_BOTH,
                confidence=1.0,
                reasoning="No existing learnings to compare"
            )

        existing, similarity = best_match

        # CHECK FOR HUMAN APPROVAL REQUIREMENT BEFORE ANY DESTRUCTIVE ACTION
        if not auto_approve:
            needs_approval, approval_result = self._requires_human_approval(
                existing, new_learning, similarity
            )
            if needs_approval:
                self.keep_count += 1  # Treat as keep until approved
                return approval_result

        # Tier 2: High similarity → MERGE (after approval check passed)
        if similarity >= self.HIGH_SIMILARITY_THRESHOLD:
            self.merge_count += 1
            return DeduplicationResult(
                action=DeduplicationAction.MERGE,
                confidence=similarity,
                merge_target_id=existing.id,
                reasoning=f"High string similarity ({similarity:.2%})",
                similarity_scores={'string': similarity}
            )

        # Tier 3: Medium similarity → LLM decides
        if similarity >= self.LOW_SIMILARITY_THRESHOLD:
            result = self._llm_decide(existing, new_learning, similarity)

            if result.action == DeduplicationAction.MERGE:
                self.merge_count += 1
            elif result.action == DeduplicationAction.DISCARD:
                self.discard_count += 1
            else:
                self.keep_count += 1

            result.similarity_scores = {'string': similarity}
            return result

        # Tier 4: Low similarity → KEEP_BOTH
        self.keep_count += 1
        return DeduplicationResult(
            action=DeduplicationAction.KEEP_BOTH,
            confidence=1.0 - similarity,
            reasoning=f"Low similarity ({similarity:.2%}), distinct learning",
            similarity_scores={'string': similarity}
        )

    def batch_deduplicate(self, learnings: List[Learning], auto_approve: bool = False) -> Dict[str, Any]:
        """
        Process a batch of learnings for deduplication.

        Used by the Celery task for periodic cleanup.

        IMPORTANT: By default (auto_approve=False), items that might be
        intentional duplicates or high-risk will be flagged for human review
        rather than automatically merged/discarded.

        Args:
            learnings: List of learnings to process
            auto_approve: If True, skip human approval (use with caution!)

        Returns:
            Summary of actions taken, including items needing approval
        """
        results = {
            'processed': 0,
            'merged': 0,
            'discarded': 0,
            'kept': 0,
            'needs_approval': 0,
            'details': [],
            'pending_approval': []  # Items that need human review
        }

        for learning in learnings:
            result = self.check_and_merge(learning, auto_approve=auto_approve)
            results['processed'] += 1

            if result.action == DeduplicationAction.MERGE:
                results['merged'] += 1
            elif result.action == DeduplicationAction.DISCARD:
                results['discarded'] += 1
            elif result.action == DeduplicationAction.ASK_HUMAN:
                results['needs_approval'] += 1
                results['pending_approval'].append({
                    'learning_id': learning.id,
                    'content_preview': learning.content[:100],
                    'result': result.to_dict(),
                    'family_message': result.to_family_message()
                })
            else:
                results['kept'] += 1

            results['details'].append({
                'learning_id': learning.id,
                'action': result.action.value,
                'confidence': result.confidence,
                'reasoning': result.reasoning
            })

        # If there are items needing approval, prepare family communication
        if results['pending_approval']:
            results['family_summary'] = self._generate_approval_summary(results['pending_approval'])

        return results

    def _generate_approval_summary(self, pending_items: List[Dict]) -> str:
        """Generate a summary message for items needing approval."""
        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic to Aaron and Atlas - Deduplication Review Queue]   ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "",
            f"📋 {len(pending_items)} item(s) need your review before deduplication.",
            "",
            "I've identified potential duplicates but I'm not certain these",
            "should be merged or discarded. They may be intentional backups,",
            "test versions, or serve different purposes.",
            "",
            "WHEN IN DOUBT, I ASK. Here's what needs your attention:",
            "",
        ]

        for i, item in enumerate(pending_items, 1):
            result = item['result']
            lines.append(f"─── Item {i} ───")
            lines.append(f"Risk: {result.get('risk_level', 'unknown').upper()}")
            lines.append(f"Preview: {item['content_preview']}...")
            lines.append(f"Reason: {result.get('reasoning', 'Unknown')}")
            if result.get('possible_purposes'):
                lines.append("Possible purposes: " + ", ".join(result['possible_purposes'][:2]))
            lines.append("")

        lines.extend([
            "Please review each item and let me know:",
            "  • APPROVE - Proceed with merge/discard",
            "  • KEEP - Keep both versions (safe choice)",
            "  • EXPLAIN - Tell me why these should be kept separate",
            "",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic to Aaron and Atlas]                                  ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)

    def get_stats(self) -> Dict[str, int]:
        """Get deduplication statistics."""
        return {
            'total_processed': self.merge_count + self.discard_count + self.keep_count,
            'merged': self.merge_count,
            'discarded': self.discard_count,
            'kept': self.keep_count
        }


# ================================================================
# WISDOM REFINEMENT: LLM refines generic learnings into specific ones
# ================================================================
# RULE: Don't BLOCK generic claims. REFINE them using LLM + context.
# "Tests passed" → "47/47 pytest tests passed after fixing Redis port"
# "Build succeeded" → "Next.js build completed, 0 warnings, bundle 1.2MB"
# ================================================================

# Patterns that indicate generic, unrefined learnings
_GENERIC_PATTERNS = [
    'tests passed', 'test passed', 'all tests', 'tests green',
    'build succeeded', 'build completed', 'compilation succeeded',
    'commit succeeded', 'pushed to', 'deploy completed',
    'container healthy', 'containers healthy', 'docker healthy',
    'service running', 'service started', 'service up',
    'exit 0', '200 ok', 'health check passed',
    'deployed successfully', 'deployment completed',
]


def refine_generic_learnings(hours: int = 24) -> Dict[str, Any]:
    """
    Find generic learnings and refine them using LLM + dialogue context.

    Instead of blocking "Tests passed", the LLM sees the surrounding dialogue
    to extract specifics: what tests, how many, what was being fixed, etc.

    Called by lite_scheduler job: wisdom_refinement
    """
    import logging
    _logger = logging.getLogger("context_dna.wisdom_refine")

    try:
        from memory.db_utils import get_unified_db_path, unified_table
        db_path = get_unified_db_path(Path.home() / ".context-dna" / "learnings.db")
        if not db_path.exists():
            return {"processed": 0, "refined": 0, "message": "no learnings.db"}

        t_lrn = unified_table("learnings.db", "learnings")
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f'''
                SELECT id, content, type, tags, created_at
                FROM {t_lrn}
                WHERE created_at > datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT 50
            ''', (f'-{hours} hours',)).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"processed": 0, "refined": 0, "message": "no recent learnings"}

        # Find generic ones
        generic_rows = []
        for row in rows:
            content_lower = (row["content"] or "").lower()
            title_check = content_lower[:120]
            for pattern in _GENERIC_PATTERNS:
                if pattern in title_check:
                    generic_rows.append(row)
                    break

        if not generic_rows:
            return {"processed": len(rows), "refined": 0, "message": "no generic learnings found"}

        # Get recent dialogue context for refinement
        dialogue_context = ""
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            dm = get_dialogue_mirror()
            recent = dm.get_recent_messages(count=10)
            if recent:
                dialogue_context = "\n".join(
                    f"[{m.get('role', '?')}] {(m.get('content', '') or '')[:300]}"
                    for m in recent[-10:]
                )
        except Exception:
            pass

        if not dialogue_context:
            # Try reading from session files as fallback
            try:
                session_dir = Path.home() / ".context-dna" / "sessions"
                if session_dir.exists():
                    session_files = sorted(session_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
                    if session_files:
                        lines = session_files[0].read_text().strip().split("\n")[-20:]
                        dialogue_context = "\n".join(lines[:2000])
            except Exception:
                pass

        # Refine each generic learning using LLM
        refined = 0
        for row in generic_rows[:5]:  # Max 5 per cycle to be gentle on LLM
            try:
                from memory.llm_priority_queue import butler_query
                result = butler_query(
                    system_prompt=(
                        "You are a wisdom refinement engine. Given a GENERIC learning like "
                        "'Tests passed' and the recent conversation context, extract the SPECIFIC "
                        "details that make this learning actionable. What specifically was tested? "
                        "How many tests? What was being fixed? What tool/command was used?\n\n"
                        "Output ONLY the refined learning text (1-2 sentences). "
                        "If you cannot determine specifics from context, output the original unchanged."
                    ),
                    user_prompt=(
                        f"GENERIC LEARNING: {row['content']}\n\n"
                        f"RECENT CONTEXT:\n{dialogue_context[:4000]}\n\n"
                        f"Refined learning:"
                    ),
                    profile="extract",  # 512 tokens, fast
                )

                if result and result.strip() and result.strip().lower() != row["content"].lower():
                    # Update the learning with refined version
                    conn = sqlite3.connect(str(db_path))
                    try:
                        conn.execute(
                            "UPDATE learnings SET content = ? WHERE id = ?",
                            (result.strip(), row["id"])
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    refined += 1
                    _logger.info(f"Refined learning {row['id']}: '{row['content'][:40]}' → '{result.strip()[:40]}'")

            except Exception as e:
                _logger.debug(f"Refinement failed for {row['id']}: {e}")
                continue

        return {
            "processed": len(rows),
            "generic_found": len(generic_rows),
            "refined": refined,
        }

    except Exception as e:
        return {"error": str(e), "processed": 0, "refined": 0}


# Convenience function for scheduler
def deduplicate_recent_learnings(hours: int = 1) -> Dict[str, Any]:
    """
    Deduplicate learnings from the last N hours.

    Called by lite_scheduler job: dedup_learnings

    Args:
        hours: Number of hours to look back

    Returns:
        Summary of deduplication actions
    """
    dedup = LLMDeduplicator()

    # Get learnings from the time window
    try:
        conn = sqlite3.connect(dedup.db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT id, content, type, tags, created_at
                FROM learnings
                WHERE created_at > datetime('now', ?)
                ORDER BY created_at DESC
            ''', (f'-{hours} hours',)).fetchall()

            learnings = [
                Learning(
                    id=r['id'],
                    content=r['content'],
                    category=r['type'],
                    tags=json.loads(r['tags']) if r['tags'] else None,
                    created_at=r['created_at']
                )
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as e:
        return {'error': str(e), 'processed': 0}

    if not learnings:
        return {'processed': 0, 'message': 'No recent learnings to process'}

    return dedup.batch_deduplicate(learnings)


if __name__ == "__main__":
    # Demo
    print("LLM Deduplication Demo")
    print("=" * 50)

    dedup = LLMDeduplicator()

    # Test with sample learnings
    test_learnings = [
        Learning(id=1, content="Use asyncio.to_thread() for blocking I/O calls in async code"),
        Learning(id=None, content="Wrap blocking I/O with asyncio.to_thread() in async functions"),
        Learning(id=None, content="Docker container restart doesn't reload environment variables"),
    ]

    print("\nTesting deduplication:")
    for learning in test_learnings[1:]:
        print(f"\nChecking: {learning.content[:50]}...")
        result = dedup.check_and_merge(learning)
        print(f"  Action: {result.action.value}")
        print(f"  Confidence: {result.confidence:.2%}")
        print(f"  Reasoning: {result.reasoning}")

    print(f"\nStats: {dedup.get_stats()}")
