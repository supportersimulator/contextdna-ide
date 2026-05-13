#!/usr/bin/env python3
"""
HINDSIGHT VALIDATOR - Delayed Win Verification System

Validates "wins" over time to detect false positives that seemed successful
but later caused failures, misconfigurations, or dependency breaks.

The Core Problem:
- Wins are captured at the moment of perceived success
- No verification that the win didn't cause downstream issues
- False wins pollute the learning system with bad patterns

The Solution:
- Track wins with "pending_verification" status
- After 24-48 hours, check dialogue mirror for related complaints/errors
- Cross-correlate wins with subsequent failures
- Reclassify false wins as "miswiring" learnings
- Demote confidence on patterns that led to false wins

Usage:
    from memory.hindsight_validator import HindsightValidator

    validator = HindsightValidator()

    # Record a win with pending verification
    validator.record_pending_win(
        win_id="win_123",
        task="Configured database port",
        approach="Set port to 5432",
        expected_outcome="Database connects",
        related_patterns=["database_pattern"]
    )

    # Run hindsight check (called by daemon or cron)
    results = validator.run_hindsight_check()

    # Manually validate a specific win
    validator.validate_win("win_123", is_valid=True, notes="Still working")

    # Reclassify a false win
    validator.reclassify_as_miswiring(
        win_id="win_123",
        actual_problem="Port was pointing to wrong database",
        correct_approach="Should use context-dna-postgres on port 5432"
    )

Created: February 2, 2026
Author: Atlas (for Synaptic)
Purpose: Give Synaptic the ability to validate wins in hindsight
"""

import json
import sqlite3
import logging
from memory.db_utils import safe_conn
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field
from enum import Enum

logger = logging.getLogger(__name__)


class VerificationStatus(str, Enum):
    """Status of win verification."""
    PENDING = "pending"           # Awaiting hindsight check
    VERIFIED = "verified"         # Confirmed as valid win
    SUSPECT = "suspect"           # Potential false positive
    MISWIRING = "miswiring"       # Confirmed false win - caused issues
    EXPIRED = "expired"           # No data to verify, assumed valid


class SeverityLevel(str, Enum):
    """Severity of a miswiring."""
    CRITICAL = "critical"   # Caused data loss or production outage
    HIGH = "high"          # Caused significant downstream failures
    MEDIUM = "medium"      # Caused some issues but recoverable
    LOW = "low"            # Minor confusion or inefficiency


@dataclass
class PendingWin:
    """A win awaiting hindsight verification."""
    win_id: str
    task: str
    approach: str
    expected_outcome: str
    recorded_at: str
    verification_deadline: str
    status: VerificationStatus = VerificationStatus.PENDING
    related_patterns: List[str] = field(default_factory=list)
    related_file_paths: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    original_confidence: float = 0.8

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d


@dataclass
class HindsightResult:
    """Result of hindsight validation."""
    win_id: str
    verified_at: str
    status: VerificationStatus
    confidence_adjustment: float  # Negative if demoted
    related_errors_found: int
    evidence: Dict
    notes: str

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d


@dataclass
class MiswiringLearning:
    """A learning generated from a false win."""
    original_win_id: str
    title: str
    symptom: str
    root_cause: str
    correct_approach: str
    severity: SeverityLevel
    created_at: str
    patterns_demoted: List[str]
    confidence_penalty: float

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['severity'] = self.severity.value
        return d


def _t_hind(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".hindsight_validator.db", name)


class HindsightValidator:
    """
    Validates wins in hindsight by correlating with subsequent failures.

    Uses dialogue mirror to detect complaints/errors after a win was recorded,
    then reclassifies false wins and demotes related pattern confidence.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.memory_dir = Path(__file__).parent
        if db_path is None:
            from memory.db_utils import get_unified_db_path
            db_path = get_unified_db_path(self.memory_dir / ".hindsight_validator.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database for hindsight tracking."""
        with safe_conn(self.db_path) as conn:
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS {_t_hind('pending_wins')} (
                    win_id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    approach TEXT NOT NULL,
                    expected_outcome TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    verification_deadline TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    related_patterns_json TEXT,
                    related_file_paths_json TEXT,
                    keywords_json TEXT,
                    original_confidence REAL DEFAULT 0.8
                );

                CREATE TABLE IF NOT EXISTS {_t_hind('hindsight_results')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    win_id TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence_adjustment REAL,
                    related_errors_found INTEGER,
                    evidence_json TEXT,
                    notes TEXT,
                    FOREIGN KEY (win_id) REFERENCES pending_wins(win_id)
                );

                CREATE TABLE IF NOT EXISTS {_t_hind('miswiring_learnings')} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_win_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    symptom TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    correct_approach TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    patterns_demoted_json TEXT,
                    confidence_penalty REAL,
                    FOREIGN KEY (original_win_id) REFERENCES pending_wins(win_id)
                );

                CREATE TABLE IF NOT EXISTS {_t_hind('cross_session_tracking')} (
                    win_id TEXT PRIMARY KEY,
                    original_session_id TEXT,
                    recorded_at TEXT NOT NULL,
                    next_session_verified INTEGER DEFAULT 0,
                    next_session_id TEXT,
                    verification_result TEXT,
                    verified_at TEXT,
                    llm_analysis TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_pending_status
                    ON {_t_hind('pending_wins')}(status);
                CREATE INDEX IF NOT EXISTS idx_pending_deadline
                    ON {_t_hind('pending_wins')}(verification_deadline);
                CREATE INDEX IF NOT EXISTS idx_results_win_id
                    ON {_t_hind('hindsight_results')}(win_id);
                CREATE INDEX IF NOT EXISTS idx_cross_session_unverified
                    ON {_t_hind('cross_session_tracking')}(next_session_verified)
                    WHERE next_session_verified = 0;
            """)

    # =========================================================================
    # RECORDING WINS
    # =========================================================================

    def record_pending_win(
        self,
        win_id: str,
        task: str,
        approach: str,
        expected_outcome: str,
        related_patterns: Optional[List[str]] = None,
        related_file_paths: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        verification_hours: int = 24,
        confidence: float = 0.8
    ) -> PendingWin:
        """
        Record a win as pending verification.

        Args:
            win_id: Unique identifier for the win
            task: What was accomplished
            approach: How it was done
            expected_outcome: What we expect to remain true
            related_patterns: Pattern IDs that contributed to this win
            related_file_paths: Files touched by this win
            keywords: Keywords for dialogue mirror correlation
            verification_hours: Hours before hindsight check (default 24)
            confidence: Initial confidence level

        Returns:
            PendingWin object
        """
        now = datetime.now()
        deadline = now + timedelta(hours=verification_hours)

        win = PendingWin(
            win_id=win_id,
            task=task,
            approach=approach,
            expected_outcome=expected_outcome,
            recorded_at=now.isoformat(),
            verification_deadline=deadline.isoformat(),
            related_patterns=related_patterns or [],
            related_file_paths=related_file_paths or [],
            keywords=keywords or self._extract_keywords(task, approach),
            original_confidence=confidence
        )

        with safe_conn(self.db_path) as conn:
            conn.execute(f"""
                INSERT OR REPLACE INTO {_t_hind('pending_wins')}
                (win_id, task, approach, expected_outcome, recorded_at,
                 verification_deadline, status, related_patterns_json,
                 related_file_paths_json, keywords_json, original_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                win.win_id, win.task, win.approach, win.expected_outcome,
                win.recorded_at, win.verification_deadline, win.status.value,
                json.dumps(win.related_patterns),
                json.dumps(win.related_file_paths),
                json.dumps(win.keywords),
                win.original_confidence
            ))

        logger.info(f"Recorded pending win: {win_id} (verify by {deadline})")
        return win

    def _extract_keywords(self, task: str, approach: str) -> List[str]:
        """Extract keywords from task and approach for correlation."""
        text = f"{task} {approach}".lower()
        # Simple keyword extraction
        skip_words = {"the", "a", "an", "to", "for", "with", "and", "or", "in", "on"}
        words = text.replace("-", " ").replace("_", " ").split()
        keywords = [w for w in words if len(w) > 3 and w not in skip_words]
        return list(set(keywords))[:10]  # Max 10 keywords

    # =========================================================================
    # HINDSIGHT VERIFICATION
    # =========================================================================

    def run_hindsight_check(self) -> List[HindsightResult]:
        """
        Run hindsight verification on all pending wins past their deadline.

        This is the main entry point for the verification daemon.

        Returns:
            List of HindsightResult objects
        """
        results = []
        pending = self._get_wins_ready_for_verification()

        for win in pending:
            result = self._verify_win(win)
            results.append(result)
            self._record_result(result)
            self._update_win_status(win.win_id, result.status)

            # If miswiring detected, create learning and demote patterns
            if result.status == VerificationStatus.MISWIRING:
                self._handle_miswiring(win, result)
            elif result.status == VerificationStatus.SUSPECT:
                # Lighter negative signal for suspects (not confirmed miswiring)
                self._emit_negative_signal(
                    win_id=win.win_id,
                    status=VerificationStatus.SUSPECT,
                    notes=result.notes,
                    errors_found=result.related_errors_found,
                    reward=-0.1,
                )

        logger.info(f"Hindsight check complete: {len(results)} wins verified")
        return results

    def _get_wins_ready_for_verification(self) -> List[PendingWin]:
        """Get wins that have passed their verification deadline."""
        now = datetime.now().isoformat()

        with safe_conn(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(f"""
                SELECT * FROM {_t_hind('pending_wins')}
                WHERE status = 'pending'
                AND verification_deadline <= ?
            """, (now,))

            wins = []
            for row in cursor.fetchall():
                win = PendingWin(
                    win_id=row['win_id'],
                    task=row['task'],
                    approach=row['approach'],
                    expected_outcome=row['expected_outcome'],
                    recorded_at=row['recorded_at'],
                    verification_deadline=row['verification_deadline'],
                    status=VerificationStatus(row['status']),
                    related_patterns=json.loads(row['related_patterns_json'] or '[]'),
                    related_file_paths=json.loads(row['related_file_paths_json'] or '[]'),
                    keywords=json.loads(row['keywords_json'] or '[]'),
                    original_confidence=row['original_confidence']
                )
                wins.append(win)

            return wins

    def _verify_win(self, win: PendingWin) -> HindsightResult:
        """
        Verify a single win via LLM causal analysis.

        Flow: keyword pre-filter (fast) → LLM causal classification (64-token)
              → weighted scoring → verdict
        """
        errors = self._find_related_errors(win)
        complaints = self._find_related_complaints(win)
        all_issues = errors + complaints

        evidence = {
            "errors_found": errors,
            "complaints_found": complaints,
            "keywords_searched": win.keywords,
            "time_window": {"from": win.recorded_at, "to": datetime.now().isoformat()},
            "llm_verdicts": [],
        }

        if not all_issues:
            return HindsightResult(
                win_id=win.win_id, verified_at=datetime.now().isoformat(),
                status=VerificationStatus.VERIFIED, confidence_adjustment=0.05,
                related_errors_found=0, evidence=evidence,
                notes="No related errors found - win verified"
            )

        # LLM causal classification per matched error (cap 5)
        causal_score = 0.0
        llm_verdicts = []
        for issue in all_issues[:5]:
            verdict = self._llm_classify_causality(win, issue)
            llm_verdicts.append(verdict)
            if verdict["classification"] == "CAUSAL":
                causal_score += 1.0
            elif verdict["classification"] == "RELATED":
                causal_score += 0.3

        evidence["llm_verdicts"] = llm_verdicts
        evidence["causal_score"] = causal_score

        # Weighted verdict
        if causal_score < 0.3:
            status = VerificationStatus.VERIFIED
            confidence_adjustment = 0.05
            notes = f"LLM: {len(all_issues)} keyword hits but causally unrelated (score={causal_score:.1f})"
        elif causal_score < 1.5:
            status = VerificationStatus.SUSPECT
            confidence_adjustment = -0.1
            notes = f"LLM: possible causal link (score={causal_score:.1f})"
        else:
            status = VerificationStatus.MISWIRING
            confidence_adjustment = -0.3
            notes = f"LLM: strong causal link (score={causal_score:.1f})"

        return HindsightResult(
            win_id=win.win_id, verified_at=datetime.now().isoformat(),
            status=status, confidence_adjustment=confidence_adjustment,
            related_errors_found=len(all_issues), evidence=evidence, notes=notes
        )

    def _llm_classify_causality(self, win: PendingWin, issue: Dict) -> Dict:
        """LLM causal classification: CAUSAL/RELATED/UNRELATED (64-token profile)."""
        issue_content = issue.get("content", "")[:400]
        try:
            from memory.llm_priority_queue import butler_query
            prompt = (
                f"WIN: {win.task} via {win.approach}\n"
                f"ERROR after: {issue_content}\n\n"
                "Reply ONE word: CAUSAL, RELATED, or UNRELATED. Then one sentence why."
            )
            result = butler_query(
                "Classify causal relationship between a code change and a subsequent error.",
                prompt, profile="classify"
            )
            if result:
                up = result.strip().upper()
                if up.startswith("CAUSAL"):
                    cls = "CAUSAL"
                elif up.startswith("RELATED"):
                    cls = "RELATED"
                else:
                    cls = "UNRELATED"
                return {"classification": cls, "reasoning": result.strip()[:200], "source": "llm"}
        except Exception as e:
            logger.debug(f"LLM causality check failed: {e}")

        # Fallback: keyword heuristic
        n = len(issue.get("matched_keywords", []))
        return {
            "classification": "RELATED" if n >= 3 else "UNRELATED",
            "reasoning": f"LLM unavailable, keyword fallback ({n} matches)",
            "source": "keyword_fallback",
        }

    def _find_related_errors(self, win: PendingWin) -> List[Dict]:
        """Find errors in dialogue mirror related to win keywords. Full text, no truncation."""
        try:
            from memory.dialogue_mirror import DialogueMirror
            mirror = DialogueMirror()

            win_time = datetime.fromisoformat(win.recorded_at)
            hours_since = (datetime.now() - win_time).total_seconds() / 3600

            context = mirror.get_context_for_synaptic(
                max_messages=200,
                max_age_hours=max(hours_since, 48)
            )

            messages = context.get("dialogue_context", [])
            errors = []
            error_indicators = ["error", "fail", "broke", "wrong", "issue", "bug", "crash", "traceback"]

            for msg in messages:
                content = msg.get("content", "").lower()
                has_error = any(ind in content for ind in error_indicators)
                has_keyword = any(kw in content for kw in win.keywords)

                if has_error and has_keyword:
                    errors.append({
                        "content": content,  # Full text for LLM causal analysis
                        "timestamp": msg.get("timestamp", ""),
                        "matched_keywords": [kw for kw in win.keywords if kw in content]
                    })

            return errors[:10]

        except Exception as e:
            logger.warning(f"Could not query dialogue mirror: {e}")
            return []

    def _find_related_complaints(self, win: PendingWin) -> List[Dict]:
        """Find complaints in dialogue mirror related to win files/patterns."""
        try:
            from memory.dialogue_mirror import DialogueMirror
            mirror = DialogueMirror()

            win_time = datetime.fromisoformat(win.recorded_at)
            hours_since = (datetime.now() - win_time).total_seconds() / 3600

            context = mirror.get_context_for_synaptic(
                max_messages=200,
                max_age_hours=max(hours_since, 48)
            )

            messages = context.get("dialogue_context", [])
            complaints = []
            complaint_indicators = ["revert", "undo", "rollback", "broken", "regression", "still", "again"]

            for msg in messages:
                content = msg.get("content", "").lower()
                ts = msg.get("timestamp", "")
                if ts and ts > win.recorded_at:
                    has_complaint = any(ind in content for ind in complaint_indicators)
                    # Check file path mentions
                    has_file = any(
                        Path(fp).name.lower() in content
                        for fp in win.related_file_paths
                    ) if win.related_file_paths else False

                    if has_complaint and (has_file or any(kw in content for kw in win.keywords)):
                        complaints.append({
                            "content": content,
                            "timestamp": ts,
                            "matched_keywords": [kw for kw in win.keywords if kw in content]
                        })

            return complaints[:10]

        except Exception as e:
            logger.warning(f"Could not scan dialogue mirror for complaints: {e}")
            return []

    def _record_result(self, result: HindsightResult):
        """Record verification result to database."""
        with safe_conn(self.db_path) as conn:
            conn.execute(f"""
                INSERT INTO {_t_hind('hindsight_results')}
                (win_id, verified_at, status, confidence_adjustment,
                 related_errors_found, evidence_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                result.win_id, result.verified_at, result.status.value,
                result.confidence_adjustment, result.related_errors_found,
                json.dumps(result.evidence), result.notes
            ))

    def _update_win_status(self, win_id: str, status: VerificationStatus):
        """Update win status in database."""
        with safe_conn(self.db_path) as conn:
            conn.execute(f"""
                UPDATE {_t_hind('pending_wins')} SET status = ? WHERE win_id = ?
            """, (status.value, win_id))

    def _emit_negative_signal(
        self,
        win_id: str,
        status: VerificationStatus,
        notes: str,
        errors_found: int = 0,
        reward: float = -0.3,
    ):
        """
        Emit negative outcome_event to the evidence pipeline.

        This is the AUTHORITATIVE negative signal emitter. The hindsight
        validator owns negative signal emission — not Claude Code, not
        the scheduler. Alfred files Batman's failure reports.

        Args:
            win_id: The win being flagged
            status: MISWIRING or SUSPECT
            notes: Human-readable description
            errors_found: Number of related errors found
            reward: Negative reward signal (-0.3 miswiring, -0.1 suspect)
        """
        try:
            from memory.observability_store import get_observability_store
            obs = get_observability_store()
            outcome_type = (
                "hindsight_miswiring" if status == VerificationStatus.MISWIRING
                else "hindsight_suspect"
            )
            obs.record_outcome_event(
                session_id=f"hindsight_{win_id}",
                outcome_type=outcome_type,
                success=False,
                reward=reward,
                notes=f"{status.value}: {notes}. Errors found: {errors_found}",
            )
            # Also record direct_claim_outcome for quarantine evaluation
            # This bridges hindsight → evidence pipeline (Gap 1 fix)
            try:
                obs.record_direct_claim_outcome(
                    claim_id=win_id,
                    success=False,
                    reward=reward,
                    source="hindsight_validator",
                    notes=f"{status.value}: {notes}",
                )
            except Exception as dco_err:
                logger.debug(f"direct_claim_outcome write failed (non-critical): {dco_err}")
            logger.info(f"Negative signal emitted: {outcome_type} for {win_id} (reward={reward})")
        except Exception as e:
            logger.warning(f"Failed to emit negative signal for {win_id}: {e}")

    def _handle_miswiring(self, win: PendingWin, result: HindsightResult):
        """Handle a detected miswiring - create learning and demote patterns."""
        # Emit negative signal to evidence pipeline (authoritative emitter)
        self._emit_negative_signal(
            win_id=win.win_id,
            status=VerificationStatus.MISWIRING,
            notes=result.notes,
            errors_found=result.related_errors_found,
            reward=-0.3,
        )

        # Use LLM to analyze root cause
        llm_analysis = self._analyze_miswiring_with_llm(win, result)

        # Create miswiring learning with LLM-enhanced understanding
        miswiring = MiswiringLearning(
            original_win_id=win.win_id,
            title=f"Miswiring: {win.task}",
            symptom=f"After '{win.approach}', {result.related_errors_found} errors detected",
            root_cause=llm_analysis.get("root_cause", "Approach caused downstream issues (detected via hindsight)"),
            correct_approach=llm_analysis.get("correct_approach", "[TO BE FILLED BY HUMAN REVIEW]"),
            severity=self._assess_severity(result),
            created_at=datetime.now().isoformat(),
            patterns_demoted=win.related_patterns,
            confidence_penalty=result.confidence_adjustment
        )

        # Save miswiring learning
        with safe_conn(self.db_path) as conn:
            conn.execute(f"""
                INSERT INTO {_t_hind('miswiring_learnings')}
                (original_win_id, title, symptom, root_cause, correct_approach,
                 severity, created_at, patterns_demoted_json, confidence_penalty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                miswiring.original_win_id, miswiring.title, miswiring.symptom,
                miswiring.root_cause, miswiring.correct_approach,
                miswiring.severity.value, miswiring.created_at,
                json.dumps(miswiring.patterns_demoted), miswiring.confidence_penalty
            ))

        # Demote related patterns
        self._demote_patterns(win.related_patterns, result.confidence_adjustment)

        logger.warning(f"Miswiring detected for win {win.win_id}: {miswiring.symptom}")

    def _assess_severity(self, result: HindsightResult) -> SeverityLevel:
        """Assess severity based on number and type of errors."""
        errors = result.related_errors_found
        if errors >= 5:
            return SeverityLevel.CRITICAL
        elif errors >= 3:
            return SeverityLevel.HIGH
        elif errors >= 2:
            return SeverityLevel.MEDIUM
        else:
            return SeverityLevel.LOW

    def _analyze_miswiring_with_llm(
        self,
        win: PendingWin,
        result: HindsightResult
    ) -> Dict[str, str]:
        """
        Use local LLM to analyze WHY a win became a miswiring.

        This provides semantic understanding of:
        - What the original approach missed
        - Why downstream failures occurred
        - What the correct approach should have been

        Falls back to generic response if LLM unavailable.
        """
        try:
            import requests

            # Build context from the win and errors
            errors = result.evidence.get("errors_found", [])
            complaints = result.evidence.get("complaints_found", [])

            error_text = "\n".join([
                f"- {e.get('content', '')[:100]}" for e in errors[:5]
            ]) if errors else "No specific errors recorded"

            complaint_text = "\n".join([
                f"- {c.get('content', '')[:100]}" for c in complaints[:5]
            ]) if complaints else "No specific complaints recorded"

            prompt = f"""Analyze this failed "win" to understand what went wrong:

ORIGINAL TASK: {win.task}
APPROACH USED: {win.approach}
EXPECTED OUTCOME: {win.expected_outcome}

ERRORS DETECTED AFTER:
{error_text}

COMPLAINTS DETECTED AFTER:
{complaint_text}

TIME ELAPSED: {win.recorded_at} to {result.verified_at}

## What To Explore

Given this pattern, think through whatever seems relevant:

- **What did the original approach miss?** What conditions or edge cases weren't considered?
- **Why did errors occur?** What was the causal chain from the approach to the failures?
- **What should have happened?** What would have prevented these downstream issues?
- **What patterns do you notice?** Are there similar failures elsewhere?
- **How confident are you?** What's unclear or ambiguous about the failure?

## Your Analysis

Share your insights in whatever way makes sense to you. Your thinking about why this failed is valuable for learning.
(Your analysis will be used to improve future approaches.)"""

            # ALL LLM access routes through priority queue — NO direct HTTP to port 5044
            from memory.llm_priority_queue import butler_query
            content = butler_query(
                "You analyze failed approaches to understand what went wrong and why.",
                prompt,
                profile="explore"
            )

            if content:
                # Try to parse as JSON if present; otherwise use natural response
                analysis = {}
                try:
                    # Clean potential markdown
                    clean_content = content
                    if clean_content.startswith("```"):
                        clean_content = clean_content.split("```")[1]
                        if clean_content.startswith("json"):
                            clean_content = clean_content[4:]

                    analysis = json.loads(clean_content.strip())
                except (json.JSONDecodeError, ValueError):
                    # Not JSON — extract key insights from natural language response
                    analysis = {
                        "root_cause": content[:150] if content else "Analysis inconclusive",
                        "correct_approach": "[Natural language analysis provided]",
                        "confidence": 0.5,  # Lower confidence when parsing natural language
                        "insights": [content[i:i+100] for i in range(0, len(content), 100)][:3] if content else []
                    }

                # Log successful analysis
                logger.info(f"LLM analyzed miswiring for {win.win_id}: {analysis.get('root_cause', '')[:50]}...")

                return {
                    "root_cause": analysis.get("root_cause", "Approach caused downstream issues"),
                    "correct_approach": analysis.get("correct_approach", "[NEEDS HUMAN REVIEW]"),
                    "confidence": analysis.get("confidence", 0.5),
                    "insights": analysis.get("insights", []),
                }
            else:
                logger.warning("LLM returned empty content for miswiring analysis")

        except Exception as e:
            logger.warning(f"LLM miswiring analysis failed: {e}")

        # Fallback: Return generic analysis
        return {
            "root_cause": "Approach caused downstream issues (detected via hindsight correlation)",
            "correct_approach": "[TO BE FILLED BY HUMAN REVIEW]",
            "confidence": 0.3,
            "insights": [],
        }

    def _demote_patterns(self, pattern_ids: List[str], penalty: float):
        """Demote confidence for patterns that led to miswiring."""
        try:
            pattern_db = Path.home() / ".context-dna" / ".pattern_evolution.db"
            if not pattern_db.exists():
                return

            with safe_conn(pattern_db) as conn:
                for pattern_id in pattern_ids:
                    conn.execute("""
                        INSERT INTO pattern_feedback
                        (pattern_id, outcome_id, adjustment, reason, timestamp)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        pattern_id,
                        f"hindsight_{datetime.now().strftime('%Y%m%d')}",
                        penalty,
                        "Hindsight validation detected miswiring",
                        datetime.now().isoformat()
                    ))

            logger.info(f"Demoted {len(pattern_ids)} patterns by {penalty}")

        except Exception as e:
            logger.warning(f"Could not demote patterns: {e}")

    # =========================================================================
    # END-OF-SESSION RE-VERIFICATION
    # =========================================================================

    def run_session_end_verification(self, session_id: str = "") -> List[HindsightResult]:
        """
        Accelerated hindsight check at session end — don't wait 24h.

        Called by meta_analysis when session-end gap detected (30min inactivity).
        Re-examines ALL pending wins from the current session immediately.
        """
        results = []
        now = datetime.now()

        with safe_conn(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Get ALL pending wins (not just past-deadline)
            cursor = conn.execute(
                f"SELECT * FROM {_t_hind('pending_wins')} WHERE status = 'pending'"
            )
            wins = []
            for row in cursor.fetchall():
                wins.append(PendingWin(
                    win_id=row['win_id'], task=row['task'],
                    approach=row['approach'], expected_outcome=row['expected_outcome'],
                    recorded_at=row['recorded_at'],
                    verification_deadline=row['verification_deadline'],
                    status=VerificationStatus(row['status']),
                    related_patterns=json.loads(row['related_patterns_json'] or '[]'),
                    related_file_paths=json.loads(row['related_file_paths_json'] or '[]'),
                    keywords=json.loads(row['keywords_json'] or '[]'),
                    original_confidence=row['original_confidence']
                ))

        logger.info(f"Session-end verification: {len(wins)} pending wins to re-examine")

        for win in wins:
            result = self._verify_win(win)
            results.append(result)
            self._record_result(result)
            self._update_win_status(win.win_id, result.status)

            if result.status == VerificationStatus.MISWIRING:
                self._handle_miswiring(win, result)
            elif result.status == VerificationStatus.SUSPECT:
                self._emit_negative_signal(
                    win_id=win.win_id, status=VerificationStatus.SUSPECT,
                    notes=f"session-end: {result.notes}",
                    errors_found=result.related_errors_found, reward=-0.1,
                )

            # Track for cross-session verification
            self._track_for_cross_session(win, session_id)

        logger.info(f"Session-end verification complete: {len(results)} wins checked")
        return results

    # =========================================================================
    # CROSS-SESSION WIN TRACKING
    # =========================================================================

    def _track_for_cross_session(self, win: PendingWin, session_id: str = ""):
        """Mark a verified/suspect win for re-verification in next session."""
        try:
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    INSERT OR IGNORE INTO {_t_hind('cross_session_tracking')}
                    (win_id, original_session_id, recorded_at)
                    VALUES (?, ?, ?)
                """, (win.win_id, session_id, win.recorded_at))
        except Exception as e:
            logger.debug(f"Cross-session tracking failed: {e}")

    def run_cross_session_verification(self, current_session_id: str = "") -> List[Dict]:
        """
        Re-verify wins from prior sessions — "did the fix stick?"

        Called at session start. Uses LLM to compare claimed fix
        against current dialogue for regressions.
        """
        results = []

        with safe_conn(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(f"""
                SELECT cst.*, pw.task, pw.approach, pw.expected_outcome,
                       pw.keywords_json, pw.related_file_paths_json
                FROM {_t_hind('cross_session_tracking')} cst
                JOIN pending_wins pw ON pw.win_id = cst.win_id
                WHERE cst.next_session_verified = 0
                ORDER BY cst.recorded_at DESC
                LIMIT 10
            """)
            unverified = cursor.fetchall()

        if not unverified:
            return []

        logger.info(f"Cross-session: {len(unverified)} prior wins to re-verify")

        for row in unverified:
            win_id = row['win_id']
            task = row['task']
            approach = row['approach']
            keywords = json.loads(row['keywords_json'] or '[]')

            # Check current dialogue for regressions mentioning the fix
            verdict = self._llm_cross_session_check(task, approach, keywords)

            result = {
                "win_id": win_id,
                "task": task,
                "verdict": verdict["classification"],
                "reasoning": verdict["reasoning"],
                "source": verdict["source"],
            }
            results.append(result)

            # Update tracking
            with safe_conn(self.db_path) as conn:
                conn.execute(f"""
                    UPDATE {_t_hind('cross_session_tracking')}
                    SET next_session_verified = 1,
                        next_session_id = ?,
                        verification_result = ?,
                        verified_at = ?,
                        llm_analysis = ?
                    WHERE win_id = ?
                """, (
                    current_session_id, verdict["classification"],
                    datetime.now().isoformat(), verdict["reasoning"],
                    win_id
                ))

            # Emit signal based on verdict
            if verdict["classification"] == "REGRESSED":
                self._emit_negative_signal(
                    win_id=win_id, status=VerificationStatus.MISWIRING,
                    notes=f"cross-session regression: {verdict['reasoning']}",
                    errors_found=1, reward=-0.3,
                )

            # Gap 7: Record to observability store cross_session_verification table
            try:
                from memory.observability_store import get_observability_store
                obs = get_observability_store()
                obs.record_cross_session_verification(
                    original_win_id=win_id,
                    original_session=row.get("session_id"),
                    verification_session=current_session_id,
                    verdict=verdict["classification"],
                    evidence=verdict.get("reasoning", ""),
                    confidence=0.7 if verdict["source"] == "llm" else 0.4,
                )
            except Exception:
                pass  # Non-critical

        logger.info(f"Cross-session verification: {len(results)} wins checked")
        return results

    def _llm_cross_session_check(self, task: str, approach: str, keywords: List[str]) -> Dict:
        """LLM check: did this fix persist across sessions? (64-token classify)"""
        try:
            from memory.dialogue_mirror import DialogueMirror
            mirror = DialogueMirror()
            context = mirror.get_context_for_synaptic(max_messages=50, max_age_hours=2)
            recent_msgs = context.get("dialogue_context", [])

            # Quick keyword scan first
            relevant_msgs = []
            for msg in recent_msgs:
                content = msg.get("content", "").lower()
                if any(kw in content for kw in keywords):
                    relevant_msgs.append(content[:200])

            if not relevant_msgs:
                return {"classification": "CONFIRMED", "reasoning": "No mentions in current session", "source": "no_data"}

            from memory.llm_priority_queue import butler_query
            recent_text = "\n".join(relevant_msgs[:3])
            prompt = (
                f"PRIOR FIX: {task} via {approach}\n"
                f"CURRENT SESSION mentions:\n{recent_text}\n\n"
                "Did the fix STICK? Reply ONE word: CONFIRMED, REGRESSED, or UNCLEAR. Then one sentence."
            )
            result = butler_query(
                "Classify if a prior code fix persisted or regressed.",
                prompt, profile="classify"
            )
            if result:
                up = result.strip().upper()
                if up.startswith("REGRESSED"):
                    cls = "REGRESSED"
                elif up.startswith("CONFIRMED"):
                    cls = "CONFIRMED"
                else:
                    cls = "UNCLEAR"
                return {"classification": cls, "reasoning": result.strip()[:200], "source": "llm"}

        except Exception as e:
            logger.debug(f"Cross-session LLM check failed: {e}")

        return {"classification": "UNCLEAR", "reasoning": "LLM unavailable", "source": "fallback"}

    # =========================================================================
    # MANUAL OPERATIONS
    # =========================================================================

    def validate_win(self, win_id: str, is_valid: bool, notes: str = ""):
        """
        Manually validate or invalidate a win.

        Args:
            win_id: The win to validate
            is_valid: True if win is confirmed valid
            notes: Optional notes about the validation
        """
        status = VerificationStatus.VERIFIED if is_valid else VerificationStatus.MISWIRING

        result = HindsightResult(
            win_id=win_id,
            verified_at=datetime.now().isoformat(),
            status=status,
            confidence_adjustment=0.1 if is_valid else -0.3,
            related_errors_found=0 if is_valid else 1,
            evidence={"manual_validation": True},
            notes=notes or f"Manually {'verified' if is_valid else 'invalidated'}"
        )

        self._record_result(result)
        self._update_win_status(win_id, status)

        if not is_valid:
            # Get win to handle miswiring
            win = self._get_win(win_id)
            if win:
                self._handle_miswiring(win, result)

    def _get_win(self, win_id: str) -> Optional[PendingWin]:
        """Get a pending win by ID."""
        with safe_conn(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT * FROM {_t_hind('pending_wins')} WHERE win_id = ?",
                (win_id,)
            )
            row = cursor.fetchone()
            if row:
                return PendingWin(
                    win_id=row['win_id'],
                    task=row['task'],
                    approach=row['approach'],
                    expected_outcome=row['expected_outcome'],
                    recorded_at=row['recorded_at'],
                    verification_deadline=row['verification_deadline'],
                    status=VerificationStatus(row['status']),
                    related_patterns=json.loads(row['related_patterns_json'] or '[]'),
                    related_file_paths=json.loads(row['related_file_paths_json'] or '[]'),
                    keywords=json.loads(row['keywords_json'] or '[]'),
                    original_confidence=row['original_confidence']
                )
            return None

    def reclassify_as_miswiring(
        self,
        win_id: str,
        actual_problem: str,
        correct_approach: str,
        severity: SeverityLevel = SeverityLevel.MEDIUM
    ):
        """
        Manually reclassify a win as miswiring with full context.

        Args:
            win_id: The win to reclassify
            actual_problem: What actually went wrong
            correct_approach: What should have been done
            severity: How bad was the miswiring
        """
        win = self._get_win(win_id)
        if not win:
            logger.error(f"Win not found: {win_id}")
            return

        # Create detailed miswiring learning
        miswiring = MiswiringLearning(
            original_win_id=win_id,
            title=f"Miswiring: {win.task}",
            symptom=actual_problem,
            root_cause=f"Original approach '{win.approach}' was incorrect",
            correct_approach=correct_approach,
            severity=severity,
            created_at=datetime.now().isoformat(),
            patterns_demoted=win.related_patterns,
            confidence_penalty=-0.3 if severity in [SeverityLevel.CRITICAL, SeverityLevel.HIGH] else -0.15
        )

        # Save to database
        with safe_conn(self.db_path) as conn:
            conn.execute(f"""
                INSERT INTO {_t_hind('miswiring_learnings')}
                (original_win_id, title, symptom, root_cause, correct_approach,
                 severity, created_at, patterns_demoted_json, confidence_penalty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                miswiring.original_win_id, miswiring.title, miswiring.symptom,
                miswiring.root_cause, miswiring.correct_approach,
                miswiring.severity.value, miswiring.created_at,
                json.dumps(miswiring.patterns_demoted), miswiring.confidence_penalty
            ))

        # Update win status
        self._update_win_status(win_id, VerificationStatus.MISWIRING)

        # Demote patterns
        self._demote_patterns(win.related_patterns, miswiring.confidence_penalty)

        # Emit negative signal to evidence pipeline (authoritative emitter)
        self._emit_negative_signal(
            win_id=win_id,
            status=VerificationStatus.MISWIRING,
            notes=f"Manual reclassification: {actual_problem}",
            errors_found=1,
            reward=miswiring.confidence_penalty,
        )

        # Also record as a learning in the main system
        self._record_as_learning(miswiring)

        logger.info(f"Reclassified win {win_id} as miswiring: {actual_problem}")

    def _record_as_learning(self, miswiring: MiswiringLearning):
        """Record miswiring as a fix learning in the main system."""
        try:
            learning_history = self.memory_dir / ".learning_history.json"
            if learning_history.exists():
                learnings = json.loads(learning_history.read_text())
            else:
                learnings = []

            new_learning = {
                "id": f"miswiring_{miswiring.original_win_id}",
                "type": "fix",
                "title": miswiring.title,
                "content": f"""**Symptom:** {miswiring.symptom}

**Root Cause:** {miswiring.root_cause}

**Correct Approach:** {miswiring.correct_approach}

**Severity:** {miswiring.severity.value}

**Detected via:** Hindsight validation
""",
                "tags": ["miswiring", "hindsight", "auto-detected"],
                "timestamp": miswiring.created_at,
                "source": "hindsight_validator"
            }

            learnings.insert(0, new_learning)
            learning_history.write_text(json.dumps(learnings, indent=2))

            logger.info(f"Recorded miswiring as learning: {miswiring.title}")

        except Exception as e:
            logger.warning(f"Could not record learning: {e}")

    # =========================================================================
    # REPORTING
    # =========================================================================

    def get_pending_count(self) -> int:
        """Get count of wins awaiting verification."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT COUNT(*) FROM {_t_hind('pending_wins')} WHERE status = 'pending'"
            )
            return cursor.fetchone()[0]

    def get_verification_stats(self) -> Dict:
        """Get statistics on verification outcomes."""
        with safe_conn(self.db_path) as conn:
            cursor = conn.execute(f"""
                SELECT status, COUNT(*) as count
                FROM {_t_hind('pending_wins')}
                GROUP BY status
            """)

            stats = {row[0]: row[1] for row in cursor.fetchall()}

        return {
            "pending": stats.get("pending", 0),
            "verified": stats.get("verified", 0),
            "suspect": stats.get("suspect", 0),
            "miswiring": stats.get("miswiring", 0),
            "total": sum(stats.values())
        }

    def get_recent_miswirings(self, limit: int = 10) -> List[Dict]:
        """Get recent miswiring learnings."""
        with safe_conn(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(f"""
                SELECT * FROM {_t_hind('miswiring_learnings')}
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))

            return [dict(row) for row in cursor.fetchall()]


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def get_validator() -> HindsightValidator:
    """Get singleton validator instance."""
    if not hasattr(get_validator, "_instance"):
        get_validator._instance = HindsightValidator()
    return get_validator._instance


def record_win_for_verification(
    task: str,
    approach: str,
    expected_outcome: str,
    **kwargs
) -> str:
    """
    Convenience function to record a win for hindsight verification.

    Returns the win_id for tracking.
    """
    from datetime import datetime
    win_id = f"win_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    validator = get_validator()
    validator.record_pending_win(
        win_id=win_id,
        task=task,
        approach=approach,
        expected_outcome=expected_outcome,
        **kwargs
    )

    return win_id


def run_verification_cycle() -> Dict:
    """Run a full verification cycle and return summary."""
    validator = get_validator()
    results = validator.run_hindsight_check()

    verified = sum(1 for r in results if r.status == VerificationStatus.VERIFIED)
    suspect = sum(1 for r in results if r.status == VerificationStatus.SUSPECT)
    miswiring = sum(1 for r in results if r.status == VerificationStatus.MISWIRING)

    return {
        "total_checked": len(results),
        "verified": verified,
        "suspect": suspect,
        "miswiring": miswiring,
        "pending_remaining": validator.get_pending_count()
    }


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    validator = HindsightValidator()

    if len(sys.argv) < 2:
        print("Usage: python hindsight_validator.py <command>")
        print("Commands: check, stats, pending, miswirings")
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        print("Running hindsight verification cycle...")
        results = validator.run_hindsight_check()
        print(f"\n=== HINDSIGHT CHECK COMPLETE ===")
        print(f"Wins checked: {len(results)}")
        for r in results:
            status_icon = "✅" if r.status == VerificationStatus.VERIFIED else "⚠️" if r.status == VerificationStatus.SUSPECT else "❌"
            print(f"  {status_icon} {r.win_id}: {r.status.value} ({r.related_errors_found} issues)")

    elif command == "stats":
        stats = validator.get_verification_stats()
        print("\n=== HINDSIGHT VERIFICATION STATS ===")
        print(f"Pending:   {stats['pending']}")
        print(f"Verified:  {stats['verified']} ✅")
        print(f"Suspect:   {stats['suspect']} ⚠️")
        print(f"Miswiring: {stats['miswiring']} ❌")
        print(f"Total:     {stats['total']}")

    elif command == "pending":
        count = validator.get_pending_count()
        print(f"\n{count} wins awaiting hindsight verification")

    elif command == "miswirings":
        miswirings = validator.get_recent_miswirings()
        print("\n=== RECENT MISWIRINGS ===")
        for m in miswirings:
            print(f"\n❌ {m['title']}")
            print(f"   Symptom: {m['symptom']}")
            print(f"   Correct: {m['correct_approach']}")
            print(f"   Severity: {m['severity']}")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
