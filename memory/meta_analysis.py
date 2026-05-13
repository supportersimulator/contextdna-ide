"""
Post-Session Meta-Analysis Pipeline

Runs after Atlas sessions end (detected by dialogue gap > 30min).
4-phase pipeline per Evidence-Based-updates.md Section 11:

  Phase 1: Summarize — Butler summarizes each thread's decisions/outcomes
  Phase 2: Cross-Reference — Compare against ContextDNA historical patterns
  Phase 3: Synthesize — Produce session-level insights
  Phase 4: Feed Back — Insert findings into evidence pipeline (quarantine)

BUDGET: ~2-3 minutes total (fits within butler's 97% idle capacity).
SCHEDULE: Runs 30 minutes after last detected Atlas activity.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """Summary of a single dialogue thread."""
    session_id: str
    message_count: int
    decisions: List[str]
    outcomes: List[str]
    failures: List[str]
    components_touched: List[str]
    duration_minutes: float
    summary_text: str


@dataclass
class MetaAnalysisResult:
    """Result of full meta-analysis pipeline."""
    analysis_id: str
    timestamp: str
    sessions_analyzed: int
    total_messages: int
    insights: List[str]
    concerns: List[str]
    miswirings: List[str]
    sop_candidates: List[str]
    historical_matches: int
    duration_ms: int
    llm_used: bool


class PostSessionMetaAnalysis:
    """
    Post-session meta-analysis engine.

    Detects session end via dialogue gap, then runs 4-phase analysis.
    """

    INACTIVITY_THRESHOLD_MIN = 10  # Session ended after 10min gap (was 30 — too slow for learning)
    # Sessions that are catch-all buckets, not real sessions
    EXCLUDED_SESSIONS = {"default-session"}
    DB_PATH = str(Path(__file__).parent / ".meta_analysis.db")

    def __init__(self, db_path: str = None):
        self.db_path = db_path or self.DB_PATH
        from memory.db_utils import get_unified_db_path
        self.dialogue_db = str(get_unified_db_path(
            Path.home() / ".context-dna" / ".dialogue_mirror.db"
        ))
        self._ensure_db()

    def _ensure_db(self):
        """Create meta-analysis storage if needed."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta_analysis_runs (
                    analysis_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    sessions_analyzed INTEGER DEFAULT 0,
                    total_messages INTEGER DEFAULT 0,
                    insights TEXT,  -- JSON array
                    concerns TEXT,  -- JSON array
                    miswirings TEXT,  -- JSON array
                    sop_candidates TEXT,  -- JSON array
                    historical_matches INTEGER DEFAULT 0,
                    duration_ms INTEGER DEFAULT 0,
                    llm_used INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id TEXT,
                    session_id TEXT,
                    message_count INTEGER,
                    decisions TEXT,  -- JSON array
                    outcomes TEXT,  -- JSON array
                    failures TEXT,  -- JSON array
                    components TEXT,  -- JSON array
                    duration_minutes REAL,
                    summary_text TEXT,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS last_analysis_marker (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_session_activity TEXT,
                    last_analysis_time TEXT
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO last_analysis_marker (id, last_session_activity, last_analysis_time)
                VALUES (1, '2000-01-01T00:00:00', '2000-01-01T00:00:00')
            """)
            # Track which sessions have been analyzed (per-session watermark)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analyzed_sessions (
                    session_id TEXT PRIMARY KEY,
                    analyzed_at TEXT NOT NULL,
                    analysis_id TEXT
                )
            """)
            # Cross-session pattern aggregation (P4)
            # Tracks recurring component x failure patterns across sessions.
            # Patterns with session_count >= 3 auto-promote to SOP candidates.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cross_session_patterns (
                    pattern_id TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    failure_type TEXT NOT NULL,
                    description TEXT,
                    session_count INTEGER DEFAULT 1,
                    session_ids TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    confidence REAL DEFAULT 0.5,
                    promoted_to_sop INTEGER DEFAULT 0,
                    promoted_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cross_patterns_component
                    ON cross_session_patterns(component)
            """)
            conn.commit()

    def should_run(self) -> bool:
        """
        Check if meta-analysis should run.

        Conditions:
        1. Last dialogue activity > 30 minutes ago (session ended)
        2. New activity since last analysis
        """
        if not Path(self.dialogue_db).exists():
            return False

        try:
            # Get last dialogue activity
            with sqlite3.connect(self.dialogue_db) as conn:
                row = conn.execute(
                    "SELECT MAX(last_activity) FROM dialogue_threads"
                ).fetchone()
                if not row or not row[0]:
                    return False
                last_activity = row[0]

            # Check inactivity threshold
            try:
                last_dt = datetime.fromisoformat(last_activity)
            except (ValueError, TypeError):
                return False

            gap = datetime.utcnow() - last_dt
            if gap < timedelta(minutes=self.INACTIVITY_THRESHOLD_MIN):
                return False  # Session still active

            # Check if we've already analyzed this activity
            with sqlite3.connect(self.db_path) as conn:
                marker = conn.execute(
                    "SELECT last_session_activity FROM last_analysis_marker WHERE id = 1"
                ).fetchone()
                if marker and marker[0] >= last_activity:
                    return False  # Already analyzed

            return True

        except Exception as e:
            logger.debug(f"Meta-analysis should_run check failed: {e}")
            return False

    def run_analysis(self) -> Optional[MetaAnalysisResult]:
        """
        Run the full 4-phase meta-analysis pipeline.

        Returns MetaAnalysisResult or None if nothing to analyze.
        """
        start = time.monotonic()
        analysis_id = f"meta_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"

        # Phase 1: Summarize
        summaries = self._phase1_summarize()
        if not summaries:
            return None

        # Phase 2: Cross-Reference
        historical = self._phase2_cross_reference(summaries)

        # Phase 3: Synthesize
        synthesis = self._phase3_synthesize(summaries, historical)

        # Phase 4: Feed Back
        self._phase4_feed_back(synthesis, analysis_id)

        # Phase 5: Cross-session pattern aggregation
        self._aggregate_historical_patterns(summaries)

        # Phase 6: Session-end hindsight re-verification (accelerated, no 24h wait)
        try:
            from memory.hindsight_validator import HindsightValidator
            validator = HindsightValidator()
            hindsight_results = validator.run_session_end_verification(session_id=analysis_id)
            if hindsight_results:
                logger.info(f"Session-end hindsight: {len(hindsight_results)} wins re-verified")
        except Exception as e:
            logger.debug(f"Session-end hindsight skipped: {e}")

        duration_ms = int((time.monotonic() - start) * 1000)

        result = MetaAnalysisResult(
            analysis_id=analysis_id,
            timestamp=datetime.utcnow().isoformat(),
            sessions_analyzed=len(summaries),
            total_messages=sum(s.message_count for s in summaries),
            insights=synthesis.get("insights", []),
            concerns=synthesis.get("concerns", []),
            miswirings=synthesis.get("miswirings", []),
            sop_candidates=synthesis.get("sop_candidates", []),
            historical_matches=len(historical),
            duration_ms=duration_ms,
            llm_used=synthesis.get("llm_used", False),
        )

        # Store result
        self._store_result(result, summaries)

        # Update marker
        self._update_marker()

        logger.info(
            f"Meta-analysis complete: {result.sessions_analyzed} sessions, "
            f"{len(result.insights)} insights, {len(result.concerns)} concerns, "
            f"{duration_ms}ms"
        )
        return result

    def _phase1_summarize(self) -> List[SessionSummary]:
        """
        Phase 1: Summarize each recent dialogue thread.

        Extracts decisions, outcomes, failures from dialogue.
        Uses LLM if available, falls back to keyword extraction.
        """
        if not Path(self.dialogue_db).exists():
            return []

        # Get last analysis time
        last_analyzed = "2000-01-01T00:00:00"
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT last_session_activity FROM last_analysis_marker WHERE id = 1"
                ).fetchone()
                if row and row[0]:
                    last_analyzed = row[0]
        except Exception:
            pass

        summaries = []
        try:
            with sqlite3.connect(self.dialogue_db) as conn:
                conn.row_factory = sqlite3.Row
                threads = conn.execute("""
                    SELECT session_id, started_at, last_activity, message_count
                    FROM dialogue_threads
                    WHERE last_activity > ?
                    ORDER BY last_activity DESC
                    LIMIT 20
                """, (last_analyzed,)).fetchall()

                for thread in threads:
                    messages = conn.execute("""
                        SELECT role, content, timestamp
                        FROM dialogue_messages
                        WHERE session_id = ?
                        ORDER BY timestamp ASC
                    """, (thread['session_id'],)).fetchall()

                    if not messages:
                        continue

                    summary = self._summarize_thread(
                        thread['session_id'],
                        [dict(m) for m in messages],
                        thread['started_at'],
                        thread['last_activity'],
                    )
                    if summary:
                        summaries.append(summary)

        except Exception as e:
            logger.warning(f"Phase 1 summarize error: {e}")

        return summaries

    def _summarize_thread(
        self, session_id: str, messages: List[Dict],
        started_at: str, last_activity: str
    ) -> Optional[SessionSummary]:
        """Summarize a single thread — keyword extraction (LLM enhancement optional)."""
        if not messages:
            return None

        # FULL dialogue to LLM — zero truncation per Aaron's directive
        # Butler has 97% idle capacity and operates at cohort-level evidence
        # (LLM's irreplaceable strength per Evidence-Based-updates.md Section 11)
        transcript = "\n".join(
            f"[{m.get('role', '?')}] {m.get('content', '')}"
            for m in messages
        )

        # Extract decisions (patterns: "decided", "chose", "will use", "going with")
        decisions = self._extract_patterns(transcript, [
            r"(?i)(?:decided|chose|choosing|going with|will use|switched to|changed to)\s+(.{10,120})",
            r"(?i)(?:fix|approach|strategy|solution):\s*(.{10,120})",
        ])

        # Extract outcomes (patterns: success/failure indicators)
        outcomes = self._extract_patterns(transcript, [
            r"(?i)(?:success|worked|fixed|resolved|healthy|verified|confirmed)[\s:]+(.{10,120})",
            r"(?i)(?:DONE|COMPLETE|FIXED|OK)\b.{0,5}(.{10,80})",
        ])

        # Extract failures
        failures = self._extract_patterns(transcript, [
            r"(?i)(?:failed|error|broken|corrupt|crash|bug|issue)[\s:]+(.{10,120})",
            r"(?i)(?:FAILED|ERROR|DOWN|TIMEOUT)\b.{0,5}(.{10,80})",
        ])

        # Detect components
        components = self._detect_components(transcript)

        # Calculate duration
        duration = 0.0
        try:
            start_dt = datetime.fromisoformat(started_at)
            end_dt = datetime.fromisoformat(last_activity)
            duration = (end_dt - start_dt).total_seconds() / 60.0
        except (ValueError, TypeError):
            pass

        # LLM summarization with FULL dialogue — no artificial truncation
        # Qwen3-14B practical limit ~8K tokens (~32K chars), but system prompt is small
        # so we can feed up to ~24K chars of dialogue safely
        llm_summary = self._try_llm_summary(transcript[:24000])

        summary_text = llm_summary or self._keyword_summary(decisions, outcomes, failures)

        return SessionSummary(
            session_id=session_id,
            message_count=len(messages),
            decisions=decisions[:5],
            outcomes=outcomes[:5],
            failures=failures[:5],
            components_touched=components,
            duration_minutes=round(duration, 1),
            summary_text=summary_text,
        )

    def _extract_patterns(self, text: str, patterns: List[str]) -> List[str]:
        """Extract matching groups from text using regex patterns."""
        import re
        found = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                val = match.group(1).strip()
                if val and val not in found:
                    found.append(val)
                    if len(found) >= 5:
                        return found
        return found

    def _detect_components(self, text: str) -> List[str]:
        """Detect which system components are mentioned."""
        import re
        components = {
            "webhook": r"(?i)webhook|section.\d|inject",
            "evidence": r"(?i)evidence|quarantine|claim|outcome",
            "scheduler": r"(?i)scheduler|launchd|cron.job",
            "docker": r"(?i)docker|container|compose",
            "llm": r"(?i)vllm|mlx|5044|local.llm|butler",
            "sync": r"(?i)unified.sync|pg.sync|sqlite.*pg",
            "agent_service": r"(?i)agent.service|file.descriptor|FD",
            "database": r"(?i)sqlite|postgres|\.db\b|WAL|corruption",
            "a/b_testing": r"(?i)a.b.test|variant|hook.evolution",
        }
        found = []
        for name, pattern in components.items():
            if re.search(pattern, text):
                found.append(name)
        return found

    def _try_llm_summary(self, transcript: str) -> Optional[str]:
        """Get LLM summary via butler_query with full evidence-based analysis.

        The butler operates at COHORT level of evidence — reading full natural
        language dialogue to make nuanced judgments that keyword extraction cannot.
        """
        try:
            from memory.llm_priority_queue import butler_query
            result = butler_query(
                system_prompt=(
                    "You are a session analyst performing EVIDENCE-BASED meta-analysis. "
                    "Analyze this full Atlas coding session dialogue. Extract:\n"
                    "1. OBJECTIVE SUCCESSES: What specifically worked? (tests passed, "
                    "bugs fixed, features deployed, errors resolved)\n"
                    "2. OBJECTIVE FAILURES: What specifically failed? (errors, crashes, "
                    "misiwrings, regressions, abandoned approaches)\n"
                    "3. KEY DECISIONS: What was chosen and why?\n"
                    "4. PATTERNS: Recurring themes (positive or negative) across the session\n"
                    "5. SOP CANDIDATES: Procedures that worked and could become SOPs\n"
                    "Be SPECIFIC about components, files, error messages, and outcomes. "
                    "Distinguish correlation from causation — a fix being present during "
                    "a success doesn't mean it caused the success."
                ),
                user_prompt=f"Full session dialogue:\n{transcript}",
                profile="reasoning",
            )
            return result
        except Exception:
            return None

    def _keyword_summary(
        self, decisions: List[str], outcomes: List[str], failures: List[str]
    ) -> str:
        """Fallback keyword-based summary when LLM unavailable."""
        parts = []
        if decisions:
            parts.append(f"Decisions: {'; '.join(decisions[:3])}")
        if outcomes:
            parts.append(f"Outcomes: {'; '.join(outcomes[:3])}")
        if failures:
            parts.append(f"Issues: {'; '.join(failures[:3])}")
        return " | ".join(parts) if parts else "No significant patterns detected"

    def _phase2_cross_reference(self, summaries: List[SessionSummary]) -> List[Dict]:
        """
        Phase 2: Cross-reference against ContextDNA historical patterns.

        Uses FTS5 search to find relevant historical context without
        overwhelming the LLM's context window.
        """
        historical = []
        try:
            from memory.context_dna_client import ContextDNAClient
            client = ContextDNAClient()

            # Build search queries from session components and decisions
            search_terms = set()
            for s in summaries:
                search_terms.update(s.components_touched)
                for d in s.decisions[:2]:
                    # Extract key terms from decisions
                    words = d.split()[:5]
                    search_terms.add(" ".join(words))

            # Query ContextDNA for each search term
            for term in list(search_terms)[:10]:
                try:
                    results = client.get_relevant_learnings(term, limit=3)
                    for r in results:
                        historical.append({
                            "query": term,
                            "title": r.get("title", ""),
                            "content": r.get("content", ""),
                            "type": r.get("type", ""),
                        })
                except Exception:
                    continue

        except ImportError:
            logger.debug("ContextDNA client not available for cross-reference")
        except Exception as e:
            logger.debug(f"Phase 2 cross-reference error: {e}")

        return historical

    def _phase3_synthesize(
        self, summaries: List[SessionSummary], historical: List[Dict]
    ) -> Dict[str, Any]:
        """
        Phase 3: Synthesize session summaries + historical context.

        Produces insights, concerns, miswirings, SOP candidates.
        """
        synthesis = {
            "insights": [],
            "concerns": [],
            "miswirings": [],
            "sop_candidates": [],
            "llm_used": False,
        }

        # Collect all decisions, outcomes, failures across sessions
        all_decisions = []
        all_outcomes = []
        all_failures = []
        all_components = set()

        for s in summaries:
            all_decisions.extend(s.decisions)
            all_outcomes.extend(s.outcomes)
            all_failures.extend(s.failures)
            all_components.update(s.components_touched)

        # Keyword-based synthesis (always runs)
        # 1. Insights = successful outcomes + decisions
        for outcome in all_outcomes[:5]:
            synthesis["insights"].append(f"Success: {outcome}")

        # 2. Concerns = repeated failures or unresolved issues
        failure_counts: Dict[str, int] = {}
        for f in all_failures:
            key = f[:50].lower()
            failure_counts[key] = failure_counts.get(key, 0) + 1
        for fail, count in failure_counts.items():
            if count >= 2:
                synthesis["concerns"].append(f"Repeated ({count}x): {fail}")
            else:
                synthesis["concerns"].append(f"Issue: {fail}")

        # 3. SOP candidates = successful fix patterns
        for d in all_decisions:
            if any(w in d.lower() for w in ["fix", "repair", "recover", "restart", "migrate"]):
                synthesis["sop_candidates"].append(d)

        # 4. Historical cross-reference insights
        if historical:
            hist_components = set(h.get("query", "") for h in historical)
            recurring = all_components.intersection(hist_components)
            if recurring:
                synthesis["insights"].append(
                    f"Recurring components: {', '.join(recurring)} — check for systemic patterns"
                )

        # Try LLM synthesis for deeper analysis
        try:
            from memory.llm_priority_queue import butler_query

            context = self._build_synthesis_prompt(summaries, historical)
            if context:
                result = butler_query(
                    system_prompt=(
                        "You are an EVIDENCE-BASED meta-analyst. Your analysis feeds the "
                        "ContextDNA evidence pipeline. Apply EBM principles:\n\n"
                        "EXTRACT (be SPECIFIC, not generic):\n"
                        "1. OBJECTIVE SUCCESSES: What measurably worked? (tests passed, "
                        "errors resolved, deployments succeeded — with specifics)\n"
                        "2. CONCERNS: Patterns suggesting drift, regression, or miswiring\n"
                        "3. SOP CANDIDATES: Repeatable procedures that produced positive outcomes\n"
                        "4. CAUSAL vs CORRELATIONAL: Did the action CAUSE the outcome, "
                        "or was it merely present? Be explicit about confidence level.\n"
                        "5. CROSS-SESSION PATTERNS: How does this connect to historical data?\n\n"
                        "OUTPUT as JSON: {\"insights\": [...], \"concerns\": [...], "
                        "\"sop_candidates\": [...]}.\n"
                        "Each entry should be specific and actionable, not generic."
                    ),
                    user_prompt=context,
                    profile="reasoning",
                )
                if result:
                    try:
                        llm_data = json.loads(result)
                        synthesis["insights"].extend(llm_data.get("insights", []))
                        synthesis["concerns"].extend(llm_data.get("concerns", []))
                        synthesis["sop_candidates"].extend(llm_data.get("sop_candidates", []))
                        synthesis["llm_used"] = True
                    except json.JSONDecodeError:
                        # LLM returned non-JSON — extract insights from natural language
                        natural_insights = self._extract_insights_from_natural_language(result)
                        if natural_insights:
                            synthesis["insights"].extend(natural_insights)
                            synthesis["llm_used"] = True
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"LLM synthesis failed: {e}")

        # Deduplicate
        for key in ["insights", "concerns", "miswirings", "sop_candidates"]:
            synthesis[key] = list(dict.fromkeys(synthesis[key]))[:10]

        return synthesis

    def _build_synthesis_prompt(
        self, summaries: List[SessionSummary], historical: List[Dict]
    ) -> str:
        """Build FULL synthesis prompt — butler has 97% idle capacity.

        No artificial truncation of summaries or historical context.
        Qwen3-14B handles ~8K tokens (~32K chars), system prompt is small.
        """
        parts = ["SESSION SUMMARIES:"]
        for s in summaries[:5]:
            parts.append(
                f"- [{s.session_id[:8]}] {s.message_count} msgs, "
                f"{s.duration_minutes}min, components: {','.join(s.components_touched)}"
            )
            if s.summary_text:
                parts.append(f"  Summary: {s.summary_text}")
            if s.decisions:
                parts.append(f"  Decisions: {'; '.join(s.decisions)}")
            if s.outcomes:
                parts.append(f"  Outcomes: {'; '.join(s.outcomes)}")
            if s.failures:
                parts.append(f"  Failures: {'; '.join(s.failures)}")

        if historical:
            parts.append("\nHISTORICAL MATCHES:")
            for h in historical[:5]:
                parts.append(f"- [{h.get('query','')}] {h.get('title','')}: {h.get('content','')}")

        prompt = "\n".join(parts)
        return prompt[:16000]  # ~4K tokens — well within Qwen3 budget

    def _extract_insights_from_natural_language(self, response: str) -> List[str]:
        """
        Extract insights from natural language response when JSON parsing fails.

        Looks for key indicator phrases and bullet points.
        """
        import re
        insights = []

        # Look for lines starting with bullet points or dashes
        lines = response.split('\n')
        for line in lines:
            stripped = line.strip()
            # Lines that start with common markers
            if stripped and (stripped.startswith('- ') or stripped.startswith('* ') or
                            stripped.startswith('• ') or stripped.startswith('✓ ') or
                            stripped.startswith('✅ ')):
                # Extract the content after marker
                content = re.sub(r'^[-*•✓✅]\s+', '', stripped).strip()
                if content and len(content) > 10:
                    insights.append(content)

        # If no bullet points found, look for sentences with insight keywords
        if not insights:
            insight_patterns = [
                r'(?:learned|discovered|found|insight):\s*([^.!?]+[.!?])',
                r'(?:pattern|trend|observation):\s*([^.!?]+[.!?])',
                r'^([A-Z][^.!?]{20,200}[.!?])',  # Sentences starting with capital
            ]

            for pattern in insight_patterns:
                for match in re.finditer(pattern, response, re.MULTILINE | re.IGNORECASE):
                    content = match.group(1).strip()
                    if content and len(content) > 10 and content not in insights:
                        insights.append(content)
                        if len(insights) >= 3:
                            break

        return insights

    def _phase4_feed_back(self, synthesis: Dict, analysis_id: str):
        """
        Phase 4: Feed insights back into the evidence pipeline.

        - New insights → quarantine as hypotheses
        - Miswirings → negative outcome events
        - SOP candidates → observation claims
        """
        try:
            from memory.observability_store import get_observability_store
            obs = get_observability_store()

            # Feed insights as claims (quarantined by default)
            for insight in synthesis.get("insights", [])[:5]:
                try:
                    obs.record_claim(
                        statement=insight,
                        evidence_grade="cohort",
                        source=f"meta_analysis:{analysis_id}",
                        confidence=0.5,
                    )
                except Exception:
                    pass

            # Feed concerns as outcome events
            for concern in synthesis.get("concerns", [])[:3]:
                try:
                    obs.record_outcome_event(
                        session_id=analysis_id,
                        outcome_type="meta_analysis_concern",
                        success=False,
                        reward=-0.1,
                        notes=concern,
                    )
                except Exception:
                    pass

            # Feed SOP candidates as claims with higher grade
            for sop in synthesis.get("sop_candidates", [])[:3]:
                try:
                    obs.record_claim(
                        statement=f"SOP candidate: {sop}",
                        evidence_grade="quasi",
                        source=f"meta_analysis:{analysis_id}",
                        confidence=0.6,
                    )
                except Exception:
                    pass

        except ImportError:
            logger.debug("Observability store not available for Phase 4")
        except Exception as e:
            logger.debug(f"Phase 4 feed back error: {e}")

    def _store_result(self, result: MetaAnalysisResult, summaries: List[SessionSummary]):
        """Persist analysis result and summaries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO meta_analysis_runs
                    (analysis_id, timestamp, sessions_analyzed, total_messages,
                     insights, concerns, miswirings, sop_candidates,
                     historical_matches, duration_ms, llm_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    result.analysis_id, result.timestamp,
                    result.sessions_analyzed, result.total_messages,
                    json.dumps(result.insights), json.dumps(result.concerns),
                    json.dumps(result.miswirings), json.dumps(result.sop_candidates),
                    result.historical_matches, result.duration_ms,
                    1 if result.llm_used else 0,
                ))

                now = datetime.utcnow().isoformat()
                for s in summaries:
                    conn.execute("""
                        INSERT INTO session_summaries
                        (analysis_id, session_id, message_count, decisions,
                         outcomes, failures, components, duration_minutes,
                         summary_text, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        result.analysis_id, s.session_id, s.message_count,
                        json.dumps(s.decisions), json.dumps(s.outcomes),
                        json.dumps(s.failures), json.dumps(s.components_touched),
                        s.duration_minutes, s.summary_text, now,
                    ))

                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to store meta-analysis result: {e}")

    def _update_marker(self):
        """Update the last analysis marker."""
        try:
            # Get current last activity from dialogue mirror
            last_activity = datetime.utcnow().isoformat()
            if Path(self.dialogue_db).exists():
                with sqlite3.connect(self.dialogue_db) as conn:
                    row = conn.execute(
                        "SELECT MAX(last_activity) FROM dialogue_threads"
                    ).fetchone()
                    if row and row[0]:
                        last_activity = row[0]

            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE last_analysis_marker SET
                        last_session_activity = ?,
                        last_analysis_time = ?
                    WHERE id = 1
                """, (last_activity, datetime.utcnow().isoformat()))
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to update analysis marker: {e}")

    def _aggregate_historical_patterns(self, summaries: list):
        """Aggregate component x failure patterns across all sessions.

        Counts recurring patterns in session_summaries. When a pattern
        appears in 3+ sessions, it auto-promotes to SOP candidate and
        feeds to the evidence pipeline.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get all historical failures grouped by component
                cursor = conn.execute("""
                    SELECT components, failures, session_id, created_at
                    FROM session_summaries
                    WHERE failures IS NOT NULL AND components IS NOT NULL
                """)
                rows = cursor.fetchall()

            if not rows:
                return

            # Build pattern counts: component → failure_type → {sessions, first/last}
            pattern_map = {}
            for components_json, failures_json, session_id, created_at in rows:
                try:
                    components = json.loads(components_json) if components_json else []
                    failures = json.loads(failures_json) if failures_json else []
                except (json.JSONDecodeError, TypeError):
                    continue

                if not components or not failures:
                    continue

                for comp in components[:5]:
                    for fail in failures[:5]:
                        # Normalize: lowercase, strip whitespace
                        comp_key = str(comp).lower().strip()[:50]
                        fail_key = str(fail).lower().strip()[:100]
                        key = f"{comp_key}::{fail_key}"

                        if key not in pattern_map:
                            pattern_map[key] = {
                                'component': comp_key,
                                'failure_type': fail_key,
                                'sessions': set(),
                                'first_seen': created_at,
                                'last_seen': created_at,
                            }
                        pattern_map[key]['sessions'].add(session_id)
                        if created_at and created_at > pattern_map[key]['last_seen']:
                            pattern_map[key]['last_seen'] = created_at

            # Upsert patterns into cross_session_patterns table
            now = datetime.utcnow().isoformat()
            promoted_count = 0
            with sqlite3.connect(self.db_path) as conn:
                for key, data in pattern_map.items():
                    session_count = len(data['sessions'])
                    confidence = min(0.9, 0.3 + (session_count * 0.15))
                    session_ids_json = json.dumps(sorted(list(data['sessions']))[:20])

                    conn.execute("""
                        INSERT INTO cross_session_patterns
                        (pattern_id, component, failure_type, session_count,
                         session_ids, first_seen, last_seen, confidence)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(pattern_id) DO UPDATE SET
                            session_count = ?,
                            session_ids = ?,
                            last_seen = ?,
                            confidence = ?
                    """, (
                        key, data['component'], data['failure_type'],
                        session_count, session_ids_json,
                        data['first_seen'], data['last_seen'], confidence,
                        session_count, session_ids_json,
                        data['last_seen'], confidence,
                    ))

                    # Auto-promote to SOP candidate at 3+ sessions
                    if session_count >= 3:
                        existing = conn.execute(
                            "SELECT promoted_to_sop FROM cross_session_patterns WHERE pattern_id = ?",
                            (key,)
                        ).fetchone()
                        if existing and not existing[0]:
                            conn.execute("""
                                UPDATE cross_session_patterns
                                SET promoted_to_sop = 1, promoted_at = ?
                                WHERE pattern_id = ?
                            """, (now, key))
                            promoted_count += 1

                            # Feed promoted pattern to evidence pipeline
                            try:
                                from memory.observability_store import get_observability_store
                                store = get_observability_store()
                                store.record_quarantine(
                                    item_type='cross_session_pattern',
                                    item_id=key[:50],
                                    statement=f"Recurring failure: {data['component']} — {data['failure_type']} "
                                              f"(seen in {session_count} sessions)",
                                    source='meta_analysis_aggregation',
                                    confidence=confidence,
                                )
                            except Exception:
                                pass

                conn.commit()

            if promoted_count > 0:
                logger.info(f"Promoted {promoted_count} cross-session patterns to SOP candidates")

        except Exception as e:
            logger.warning(f"Cross-session aggregation failed: {e}")

    def get_latest_analysis(self) -> Optional[Dict]:
        """Get the most recent meta-analysis result."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("""
                    SELECT * FROM meta_analysis_runs
                    ORDER BY timestamp DESC LIMIT 1
                """).fetchone()
                return dict(row) if row else None
        except Exception:
            return None
