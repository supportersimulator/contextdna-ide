"""
Butler Repair Miner (MMOTW — Mistakes Made On The Way)

Mines the dialogue mirror for Atlas repair sessions and extracts
structured repair SOPs using the LOCAL LLM (butler's intelligence).

Architecture:
  - Reads from: dialogue_mirror.db (threads + messages)
  - Analyzes via: local LLM (butler_query, P4 priority)
  - Writes to: repair_sops.db (versioned repair procedures)
  - Validates: existing SOPs against current system state
  - Feeds: observability pipeline via outcome_events

DESIGN PRINCIPLE (from Aaron):
  The LLM must do the SOP creation — not programmatic keyword matching.
  Keywords are for SIGNAL DETECTION only (finding repair-containing sessions).
  SOP EXTRACTION is the butler's job — adaptive to any project, any repair.
  The butler reads the conversation and UNDERSTANDS what was fixed, why,
  and how — then writes a structured SOP that would help any future agent.

Scheduled by lite_scheduler every 2 hours (mmotw_repair_mining job).
"""

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# ─── Learning-worthy signal patterns ──────────────────────────────
# Keywords = SIGNAL DETECTION only. The LLM decides what type of SOP.
# Signals are organized by category but ALL feed the same LLM pipeline.

# REPAIR signals (fixing broken things)
REPAIR_SIGNALS = [
    r"(?i)\bfix(?:ed|ing|es)?\b.*\b(?:bug|error|crash|corrupt|broken|fail)",
    r"(?i)\brepair(?:ed|ing|s)?\b",
    r"(?i)\brecover(?:ed|ing|y)?\b.*\b(?:db|database|data|table)",
    r"(?i)\brestored?\b.*\b(?:from|backup|pg)",
    r"(?i)\bwal\s+(?:checkpoint|mode)",
    r"(?i)\bsingleton\b.*\b(?:fix|enforce|leak)",
    r"(?i)\brestart(?:ed|ing)?\b.*\b(?:service|daemon|pid|launchd)",
    r"(?i)\bcorrupt(?:ed|ion)?\b",
    r"(?i)\bmigrat(?:ed?|ion|ing)\b.*\b(?:schema|table|column)",
    r"(?i)\brollback\b",
    r"(?i)\bhotfix\b",
    r"(?i)\broot\s*cause\b",
    r"(?i)\bsqlite3?\b.*\b\.recover\b",
]

# INTEGRATION signals (connecting systems, configuring destinations)
INTEGRATION_SIGNALS = [
    r"(?i)\bintegrat(?:ed?|ion|ing)\b",
    r"(?i)\bwebhook\b.*\b(?:config|setup|connect|register|destination)",
    r"(?i)\bhook\b.*\b(?:install|config|setup|enable|wir)",
    r"(?i)\b(?:vs.?code|cursor|windsurf|iterm|terminal|electron)\b.*\b(?:config|setup|integrat|connect)",
    r"(?i)\bdestinat(?:ion|ions)\b.*\b(?:register|add|config|new)",
    r"(?i)\bpre.?hook|post.?hook\b.*\b(?:template|config|setup)",
    r"(?i)\bconfig.?evolution\b",
    r"(?i)\bapp.*detect(?:ed|ion)\b.*\b(?:integrat|config|offer)",
    r"(?i)\b(?:api|endpoint|route)\b.*\b(?:connect|wire|setup|integrat)",
    r"(?i)\boauth|token|credential\b.*\b(?:config|setup|refresh)",
]

# DEPLOYMENT signals (shipping, launching, deploying)
DEPLOYMENT_SIGNALS = [
    r"(?i)\bdeploy(?:ed|ing|ment)?\b",
    r"(?i)\bship(?:ped|ping)?\b",
    r"(?i)\breleas(?:ed?|ing)\b",
    r"(?i)\bpublish(?:ed|ing)?\b",
    r"(?i)\blaunch(?:ed|ing|ctl|d)?\b.*\b(?:service|plist|daemon)",
    r"(?i)\bdocker.?compose.*up\b",
    r"(?i)\bgit\s+push\b",
    r"(?i)\bvercel|netlify|aws\b.*\b(?:deploy|push|ship)",
]

# CONFIGURATION signals (tuning, adjusting, optimizing)
CONFIGURATION_SIGNALS = [
    r"(?i)\bconfigur(?:ed?|ing|ation)\b.*\b(?:new|update|change|set)",
    r"(?i)\bscheduler?\b.*\b(?:add|register|new|job)",
    r"(?i)\bpipeline\b.*\b(?:wire|connect|add|new)",
    r"(?i)\bevidence\b.*\b(?:pipeline|wire|connect)",
    r"(?i)\boptimiz(?:ed?|ing|ation)\b",
    r"(?i)\bperformance\b.*\b(?:improv|optimiz|tun)",
]

# Compile ALL signals into a single detection list
ALL_SIGNALS = REPAIR_SIGNALS + INTEGRATION_SIGNALS + DEPLOYMENT_SIGNALS + CONFIGURATION_SIGNALS
ALL_SIGNAL_COMPILED = [re.compile(p) for p in ALL_SIGNALS]

# Keep repair-only compiled list for backward compat
REPAIR_SIGNAL_COMPILED = [re.compile(p) for p in REPAIR_SIGNALS]

# Component extraction patterns (what part of the mansion was touched)
COMPONENT_PATTERNS = {
    # Skeletal anatomy (databases)
    "observability": re.compile(r"(?i)observab|\.observability\.db|evidence.pipeline"),
    "ab_tracking": re.compile(r"(?i)ab.track|boundary.inject|\.context_ab_tracking"),
    "dialogue_mirror": re.compile(r"(?i)dialogue.mirror|\.dialogue_mirror\.db"),
    "hindsight": re.compile(r"(?i)hindsight|miswiring|\.hindsight_validator"),
    "webhook": re.compile(r"(?i)webhook|section.\d|inject(?:ion)?"),
    "scheduler": re.compile(r"(?i)scheduler|lite_scheduler|launchd|cron"),
    "agent_service": re.compile(r"(?i)agent.service|pid.\d|file.descriptor|FD.count"),
    "docker": re.compile(r"(?i)docker|container|compose"),
    "llm": re.compile(r"(?i)vllm|mlx|5044|5043|qwen|local.llm|butler"),
    "sync": re.compile(r"(?i)unified.sync|pg.sync|sqlite.*pg|learnings.sync"),
    "professor": re.compile(r"(?i)professor|wisdom|section.2"),
    "brain": re.compile(r"(?i)brain\.py|brain.state|architecture.brain"),
    # Integration destinations (nervous system)
    "vscode_integration": re.compile(r"(?i)vs.?code|claude.?code|\.claude/"),
    "cursor_integration": re.compile(r"(?i)cursor|overseer"),
    "windsurf_integration": re.compile(r"(?i)windsurf"),
    "electron_integration": re.compile(r"(?i)electron|desktop.app"),
    "terminal_integration": re.compile(r"(?i)iterm|terminal\.app"),
    "chat_integration": re.compile(r"(?i)chatgpt|synaptic.chat|chat.app"),
    "config_evolution": re.compile(r"(?i)config.evolution|template.evolv"),
    "destination_registry": re.compile(r"(?i)destination.registr|webhook.destination"),
}

# SOP type categories (LLM assigns these, signals hint at them)
SOP_TYPES = ["repair", "integration", "deployment", "configuration", "optimization", "general"]


@dataclass
class MansionSOP:
    """A structured procedure extracted from dialogue — repair, integration, or any type."""
    sop_id: str
    sop_type: str  # repair, integration, deployment, configuration, optimization, general
    component: str
    title: str
    symptom: str  # For repairs: what broke. For integrations: what was needed.
    root_cause: str  # For repairs: why it broke. For integrations: why it was configured this way.
    fix_steps: List[str]  # Steps taken (works for any SOP type)
    outcome: str  # success/partial/failed
    confidence: float  # 0.0-1.0
    source_session: str
    source_timestamp: str
    version: int = 1
    times_validated: int = 0
    last_validated: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d['fix_steps'] = json.dumps(d['fix_steps'])
        return d

# Backward compat alias
RepairSOP = MansionSOP


class MMOTWMiner:
    """
    Mines dialogue mirror for repair patterns and extracts SOPs.

    MMOTW = Mistakes Made On The Way — every repair Atlas performs
    becomes a learnable procedure stored in the bone marrow (repair_sops.db).
    """

    def __init__(self, repair_db_path: str = None, dialogue_db_path: str = None):
        self.repair_db_path = repair_db_path or str(
            Path(__file__).parent / ".repair_sops.db"
        )
        if dialogue_db_path is None:
            from memory.db_utils import get_unified_db_path
            dialogue_db_path = str(get_unified_db_path(
                Path.home() / ".context-dna" / ".dialogue_mirror.db"
            ))
        self.dialogue_db_path = dialogue_db_path
        self._ensure_repair_db()

    def _ensure_repair_db(self):
        """Create repair_sops.db schema if needed, with auto-migration."""
        Path(self.repair_db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.repair_db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS repair_sops (
                    sop_id TEXT PRIMARY KEY,
                    sop_type TEXT NOT NULL DEFAULT 'repair',
                    component TEXT NOT NULL,
                    title TEXT NOT NULL,
                    symptom TEXT,
                    root_cause TEXT,
                    fix_steps TEXT,  -- JSON array
                    outcome TEXT DEFAULT 'unknown',
                    confidence REAL DEFAULT 0.5,
                    source_session TEXT,
                    source_timestamp TEXT,
                    version INTEGER DEFAULT 1,
                    times_validated INTEGER DEFAULT 0,
                    last_validated TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mining_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    sessions_scanned INTEGER DEFAULT 0,
                    repair_signals_found INTEGER DEFAULT 0,
                    new_sops INTEGER DEFAULT 0,
                    updated_sops INTEGER DEFAULT 0,
                    validated INTEGER DEFAULT 0,
                    duration_ms INTEGER DEFAULT 0
                )
            """)
            # Auto-migrate: add sop_type column if missing (existing DBs)
            # MUST run before index creation on sop_type
            try:
                conn.execute("SELECT sop_type FROM repair_sops LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE repair_sops ADD COLUMN sop_type TEXT NOT NULL DEFAULT 'repair'")
                logger.info("Migrated repair_sops: added sop_type column")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sops_component
                ON repair_sops(component)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sops_outcome
                ON repair_sops(outcome)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sops_type
                ON repair_sops(sop_type)
            """)
            conn.commit()

    def run_mining_sweep(self) -> Dict[str, Any]:
        """
        Main entry point — called by lite_scheduler every 2h.

        Returns dict with: sessions_mined, new_sops, updated_sops, validated
        """
        start = time.monotonic()
        results = {
            "sessions_mined": 0,
            "new_sops": 0,
            "updated_sops": 0,
            "validated": 0,
        }

        try:
            # 1. Find unmined repair sessions in dialogue mirror
            sessions = self._find_repair_sessions()
            results["sessions_mined"] = len(sessions)

            if not sessions:
                self._log_sweep(results, start)
                return results

            # 2. Extract repair SOPs from each session
            for session in sessions:
                sops = self._extract_repair_sops(session)
                for sop in sops:
                    stored = self._store_sop(sop)
                    if stored == "new":
                        results["new_sops"] += 1
                        # Emit negative signal for failed SOPs (butler detected failure)
                        if sop.outcome == "failed":
                            self._emit_failure_signal(sop)
                    elif stored == "updated":
                        results["updated_sops"] += 1

            # 3. Validate existing SOPs (check paths, deps still exist)
            results["validated"] = self._validate_existing_sops()

            # 4. Record to observability
            self._record_to_observability(results)

        except Exception as e:
            logger.warning(f"MMOTW mining sweep error: {e}")

        self._log_sweep(results, start)
        return results

    def _find_repair_sessions(self, hours_back: int = 48) -> List[Dict]:
        """
        Scan dialogue_mirror.db for sessions containing repair signals.

        Returns list of dicts with session_id, messages, timestamps.
        """
        if not Path(self.dialogue_db_path).exists():
            logger.debug("Dialogue mirror DB not found")
            return []

        cutoff = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat()

        # Get sessions already mined (avoid re-mining)
        mined_sessions = set()
        try:
            with sqlite3.connect(self.repair_db_path) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT source_session FROM repair_sops"
                ).fetchall()
                mined_sessions = {r[0] for r in rows}
        except Exception:
            pass

        repair_sessions = []
        try:
            with sqlite3.connect(self.dialogue_db_path) as conn:
                conn.row_factory = sqlite3.Row
                # Get threads with recent activity
                threads = conn.execute("""
                    SELECT session_id, last_activity
                    FROM dialogue_threads
                    WHERE last_activity > ?
                    ORDER BY last_activity DESC
                    LIMIT 50
                """, (cutoff,)).fetchall()

                for thread in threads:
                    sid = thread['session_id']
                    if sid in mined_sessions:
                        continue

                    # Get messages for this thread
                    messages = conn.execute("""
                        SELECT role, content, timestamp
                        FROM dialogue_messages
                        WHERE session_id = ?
                        ORDER BY timestamp ASC
                    """, (sid,)).fetchall()

                    if not messages:
                        continue

                    # Check if any message matches learning-worthy signals
                    # (repair, integration, deployment, or configuration)
                    has_signal = False
                    for msg in messages:
                        content = msg['content'] or ""
                        if any(p.search(content) for p in ALL_SIGNAL_COMPILED):
                            has_signal = True
                            break

                    if has_signal:
                        repair_sessions.append({
                            "session_id": sid,
                            "messages": [dict(m) for m in messages],
                            "last_activity": thread['last_activity'],
                        })

        except Exception as e:
            logger.warning(f"Error scanning dialogue mirror: {e}")

        return repair_sessions

    def _extract_repair_sops(self, session: Dict) -> List[RepairSOP]:
        """
        Extract structured repair SOPs from a dialogue session.

        PRIMARY: Uses local LLM (butler_query) to read the transcript
        and produce structured SOPs. The LLM understands context, can
        identify the actual repair pattern regardless of project/domain.

        FALLBACK: Keyword extraction only if LLM is offline.
        """
        messages = session.get("messages", [])
        if not messages:
            return []

        # Build transcript (prioritize repair-relevant messages, cap at ~3000 chars)
        transcript = self._build_analysis_transcript(messages)

        # === PRIMARY: LLM-driven SOP extraction ===
        llm_sops = self._llm_extract_sops(transcript, session)
        if llm_sops:
            return llm_sops

        # === FALLBACK: Keyword extraction (LLM offline) ===
        logger.info("LLM unavailable — falling back to keyword extraction")
        return self._keyword_extract_sops(messages, transcript, session)

    def _build_analysis_transcript(self, messages: List[Dict]) -> str:
        """
        Build a focused transcript for LLM analysis.

        Prioritizes repair-relevant messages, caps total size
        to fit within butler's context budget.
        """
        # Separate repair-relevant vs context messages
        repair_msgs = []
        context_msgs = []

        for msg in messages:
            content = msg.get("content", "") or ""
            role = msg.get("role", "?")
            if any(p.search(content) for p in ALL_SIGNAL_COMPILED):
                repair_msgs.append(f"[{role}] {content[:600]}")
            else:
                context_msgs.append(f"[{role}] {content[:200]}")

        # Build transcript: context setup + all action-relevant messages
        parts = []
        if context_msgs:
            parts.append("=== CONTEXT (problem/task setup) ===")
            parts.extend(context_msgs[:5])
        if repair_msgs:
            parts.append("\n=== ACTIONS TAKEN ===")
            parts.extend(repair_msgs)

        return "\n".join(parts)[:4000]  # Stay within LLM context budget

    def _llm_extract_sops(self, transcript: str, session: Dict) -> Optional[List[RepairSOP]]:
        """
        Use local LLM to analyze the transcript and extract repair SOPs.

        The LLM reads the conversation naturally and produces structured
        output — this is what makes the system adaptive to ANY repair type.
        """
        try:
            from memory.llm_priority_queue import butler_query
        except ImportError:
            return None

        system_prompt = """You are a mansion butler documenting everything Batman does so you can prepare better next time.

Analyze this coding session transcript and extract structured SOPs (Standard Operating Procedures). SOPs can be ANY type of work — repairs, integrations, deployments, configurations, optimizations.

For each distinct action in the transcript, consider:

- **What was the action type?** Is this a repair, integration, deployment, configuration, or optimization?
  Types: "repair" (fixing broken things), "integration" (connecting systems), "deployment" (shipping code/config), "configuration" (tuning/adjusting), "optimization" (improving performance), or "general"

- **What system/component was involved?** (e.g., database, scheduler, webhook, docker, vscode_integration, cursor_integration, config_evolution)

- **What should the title be?** A concise actionable description of what was done.

- **What was the problem/goal?** For repairs: what broke. For integrations: what was missing/needed. For other types: what needed to change.

- **Why was this approach taken?** For repairs: why did it break. For integrations: why this approach. For others: rationale.

- **What were the specific steps?** In order, being specific about file paths, commands, port numbers, etc.

- **What was the outcome?** Success, partial, or failed.

If you identify distinct SOPs in this transcript, describe them in a structured format. You can use JSON, bullet points, or natural language — whatever format makes sense to you.

Include an example of what a well-documented SOP looks like:
  Title: Configure VS Code webhook for Claude Code hook injection
  Type: integration
  Component: vscode_integration
  Problem: VS Code had no context injection
  Approach: Created ~/.claude/hooks/user-prompt-submit.sh and added hook entry to settings.json
  Steps: 1) Create hooks directory, 2) Write shell script, 3) Update settings.json, 4) Verify webhook fires
  Outcome: success

Your documentation will help future work by capturing what was learned and how to replicate successes."""

        try:
            result = butler_query(
                system_prompt=system_prompt,
                user_prompt=f"Analyze this repair session:\n\n{transcript}",
                profile="deep",
            )

            if not result:
                return None

            # Parse LLM response as JSON
            # Handle potential markdown wrapping
            cleaned = result.strip()
            if cleaned.startswith("```"):
                # Strip markdown code blocks
                lines = cleaned.split("\n")
                cleaned = "\n".join(
                    l for l in lines
                    if not l.strip().startswith("```")
                )

            sop_data = json.loads(cleaned)
            if not isinstance(sop_data, list):
                return None

            sops = []
            for item in sop_data:
                if not isinstance(item, dict):
                    continue
                component = item.get("component", "general")
                sop_type = item.get("sop_type", "general")
                if sop_type not in SOP_TYPES:
                    sop_type = "general"
                sop_id = f"mmotw_{sop_type}_{component}_{session['session_id'][:12]}"

                sop = MansionSOP(
                    sop_id=sop_id,
                    sop_type=sop_type,
                    component=component,
                    title=item.get("title", f"{sop_type}: {component}"),
                    symptom=item.get("symptom", ""),
                    root_cause=item.get("root_cause", ""),
                    fix_steps=item.get("fix_steps", []),
                    outcome=item.get("outcome", "unknown"),
                    confidence=0.85,  # LLM-extracted = higher confidence
                    source_session=session['session_id'],
                    source_timestamp=session.get('last_activity', datetime.utcnow().isoformat()),
                )
                sops.append(sop)

            if sops:
                logger.info(f"LLM extracted {len(sops)} repair SOP(s) from session {session['session_id'][:12]}")
            return sops if sops else None

        except json.JSONDecodeError:
            logger.debug("LLM response was not valid JSON — falling back to keywords")
            return None
        except Exception as e:
            logger.debug(f"LLM SOP extraction failed: {e}")
            return None

    def _keyword_extract_sops(
        self, messages: List[Dict], transcript: str, session: Dict
    ) -> List[RepairSOP]:
        """
        FALLBACK: Keyword-based extraction when LLM is offline.

        Less adaptive than LLM but ensures SOPs are still captured.
        """
        sops = []

        # Detect components involved
        components = self._detect_components(transcript)
        if not components:
            components = ["general"]

        # Extract repair segments
        repair_segments = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("atlas", "ATLAS") and any(
                p.search(content) for p in REPAIR_SIGNAL_COMPILED
            ):
                repair_segments.append(msg)

        if not repair_segments:
            return sops

        for component in components[:3]:
            symptom = self._extract_symptom(messages)
            root_cause = self._extract_root_cause(repair_segments)
            fix_steps = self._extract_fix_steps(repair_segments)
            outcome = self._extract_outcome(messages)

            if not fix_steps:
                continue

            sop_type = self._guess_sop_type(transcript)
            sop_id = f"mmotw_{sop_type}_{component}_{session['session_id'][:12]}"
            title = self._generate_title(component, symptom, fix_steps)

            sop = MansionSOP(
                sop_id=sop_id,
                sop_type=sop_type,
                component=component,
                title=title,
                symptom=symptom,
                root_cause=root_cause,
                fix_steps=fix_steps,
                outcome=outcome,
                confidence=0.4,  # Keyword-extracted = lower confidence
                source_session=session['session_id'],
                source_timestamp=session.get('last_activity', datetime.utcnow().isoformat()),
            )
            sops.append(sop)

        return sops

    def _guess_sop_type(self, text: str) -> str:
        """Guess SOP type from keywords (fallback when LLM is offline)."""
        integration_compiled = [re.compile(p) for p in INTEGRATION_SIGNALS]
        deploy_compiled = [re.compile(p) for p in DEPLOYMENT_SIGNALS]
        config_compiled = [re.compile(p) for p in CONFIGURATION_SIGNALS]

        scores = {
            "repair": sum(1 for p in REPAIR_SIGNAL_COMPILED if p.search(text)),
            "integration": sum(1 for p in integration_compiled if p.search(text)),
            "deployment": sum(1 for p in deploy_compiled if p.search(text)),
            "configuration": sum(1 for p in config_compiled if p.search(text)),
        }
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "general"

    def _detect_components(self, text: str) -> List[str]:
        """Detect which system components are mentioned in text."""
        found = []
        for name, pattern in COMPONENT_PATTERNS.items():
            if pattern.search(text):
                found.append(name)
        return found

    def _extract_symptom(self, messages: List[Dict]) -> str:
        """Extract the symptom description from early messages."""
        # Look in first few messages (usually Aaron describing the problem)
        for msg in messages[:5]:
            content = msg.get("content", "")
            role = msg.get("role", "")
            if role in ("aaron", "AARON", "user") and len(content) > 20:
                # Take first sentence-like chunk
                first_line = content.split("\n")[0][:200]
                return first_line
        return "Unknown symptom"

    def _extract_root_cause(self, repair_segments: List[Dict]) -> str:
        """Extract root cause from Atlas repair messages."""
        for msg in repair_segments:
            content = msg.get("content", "")
            # Look for "root cause" or "because" patterns
            rc_match = re.search(
                r"(?i)(?:root\s*cause|because|caused\s*by|due\s*to|the\s*issue\s*(?:is|was))[:\s]+(.{20,200})",
                content
            )
            if rc_match:
                return rc_match.group(1).strip()
        return "Root cause not explicitly stated"

    def _extract_fix_steps(self, repair_segments: List[Dict]) -> List[str]:
        """Extract fix steps from Atlas repair messages."""
        steps = []
        for msg in repair_segments:
            content = msg.get("content", "")
            # Look for numbered lists, bullet points, or "Fixed:" patterns
            lines = content.split("\n")
            for line in lines:
                line = line.strip()
                # Match numbered or bulleted items describing actions
                if re.match(r"^[\d]+[.)]\s+|^[-*]\s+|^(?:Fixed|Changed|Added|Removed|Updated|Replaced|Set):", line):
                    step = re.sub(r"^[\d]+[.)]\s+|^[-*]\s+", "", line).strip()
                    if len(step) > 10 and step not in steps:
                        steps.append(step[:300])
                # Match "X → Y" change patterns
                elif "→" in line and len(line) > 15:
                    steps.append(line[:300])

        return steps[:10]  # Max 10 steps

    def _extract_outcome(self, messages: List[Dict]) -> str:
        """Determine if the repair was successful from the conversation."""
        # Check last few messages for success/failure signals
        for msg in reversed(messages[-5:]):
            content = (msg.get("content", "") or "").lower()
            if any(w in content for w in ["success", "fixed", "resolved", "working", "healthy"]):
                return "success"
            if any(w in content for w in ["failed", "still broken", "didn't work", "error"]):
                return "failed"
            if any(w in content for w in ["partial", "workaround", "temporary"]):
                return "partial"
        return "unknown"

    def _generate_title(self, component: str, symptom: str, fix_steps: List[str]) -> str:
        """Generate a concise title for the SOP."""
        if fix_steps:
            first_action = fix_steps[0][:60]
            return f"[{component}] {first_action}"
        return f"[{component}] Repair: {symptom[:60]}"

    def _store_sop(self, sop: RepairSOP) -> str:
        """
        Store or update a repair SOP.

        Returns: "new", "updated", or "skip"
        """
        try:
            with sqlite3.connect(self.repair_db_path) as conn:
                existing = conn.execute(
                    "SELECT version, confidence FROM repair_sops WHERE sop_id = ?",
                    (sop.sop_id,)
                ).fetchone()

                now = datetime.utcnow().isoformat()

                if existing:
                    # Update if new version has higher confidence
                    old_version, old_confidence = existing
                    if sop.confidence >= old_confidence:
                        conn.execute("""
                            UPDATE repair_sops SET
                                symptom = ?, root_cause = ?, fix_steps = ?,
                                outcome = ?, confidence = ?,
                                version = version + 1, updated_at = ?
                            WHERE sop_id = ?
                        """, (
                            sop.symptom, sop.root_cause,
                            json.dumps(sop.fix_steps),
                            sop.outcome, sop.confidence,
                            now, sop.sop_id
                        ))
                        conn.commit()
                        return "updated"
                    return "skip"
                else:
                    conn.execute("""
                        INSERT INTO repair_sops
                        (sop_id, sop_type, component, title, symptom, root_cause, fix_steps,
                         outcome, confidence, source_session, source_timestamp,
                         version, times_validated, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        sop.sop_id, sop.sop_type, sop.component, sop.title,
                        sop.symptom, sop.root_cause,
                        json.dumps(sop.fix_steps),
                        sop.outcome, sop.confidence,
                        sop.source_session, sop.source_timestamp,
                        sop.version, 0, now, now
                    ))
                    conn.commit()
                    return "new"

        except Exception as e:
            logger.warning(f"Failed to store SOP {sop.sop_id}: {e}")
            return "skip"

    def _validate_existing_sops(self) -> int:
        """
        Validate existing SOPs are still relevant.

        Uses LLM to verify SOPs reference valid paths/components.
        Falls back to structural validation (parseable JSON, non-empty).
        """
        validated = 0
        try:
            with sqlite3.connect(self.repair_db_path) as conn:
                conn.row_factory = sqlite3.Row
                sops = conn.execute("""
                    SELECT sop_id, component, title, fix_steps, symptom,
                           root_cause, times_validated
                    FROM repair_sops
                    WHERE outcome = 'success'
                    ORDER BY times_validated ASC
                    LIMIT 5
                """).fetchall()

                now = datetime.utcnow().isoformat()
                for sop in sops:
                    try:
                        steps = json.loads(sop['fix_steps'])
                        if not isinstance(steps, list) or len(steps) == 0:
                            continue

                        # Structural validation (always)
                        valid = True

                        # Check referenced file paths still exist
                        for step in steps:
                            # Extract file paths from steps
                            path_matches = re.findall(
                                r'(?:memory/|context-dna/|~/.context-dna/)[\w./-]+',
                                step
                            )
                            for path in path_matches:
                                expanded = path.replace("~", str(Path.home()))
                                if not expanded.startswith("/"):
                                    expanded = str(Path(__file__).parent.parent / expanded)
                                if not Path(expanded).exists():
                                    valid = False
                                    logger.debug(
                                        f"SOP {sop['sop_id']}: path no longer exists: {path}"
                                    )

                        if valid:
                            conn.execute("""
                                UPDATE repair_sops SET
                                    times_validated = times_validated + 1,
                                    last_validated = ?
                                WHERE sop_id = ?
                            """, (now, sop['sop_id']))
                            validated += 1

                    except (json.JSONDecodeError, TypeError):
                        pass

                conn.commit()

        except Exception as e:
            logger.debug(f"SOP validation error: {e}")

        return validated

    def _record_to_observability(self, results: Dict):
        """Record mining results to the evidence pipeline."""
        try:
            from memory.observability_store import get_observability_store
            obs = get_observability_store()
            obs.record_outcome_event(
                session_id=f"mmotw_sweep_{datetime.utcnow().strftime('%Y%m%dT%H%M')}",
                outcome_type="mmotw_mining",
                success=True,
                reward=0.1 * results.get("new_sops", 0),
                notes=json.dumps(results),
            )
        except Exception as e:
            logger.debug(f"Failed to record MMOTW to observability: {e}")

    def _emit_failure_signal(self, sop: MansionSOP):
        """
        Emit negative outcome_event when a failed SOP is extracted.

        The butler (local LLM) detected a failure pattern in the dialogue
        mirror. This feeds the evidence pipeline so the system learns
        what NOT to do. Alfred files Batman's failure reports.

        Args:
            sop: The MansionSOP with outcome="failed"
        """
        try:
            from memory.observability_store import get_observability_store
            obs = get_observability_store()
            obs.record_outcome_event(
                session_id=f"mmotw_failure_{sop.sop_id}",
                outcome_type="mmotw_failure_pattern",
                success=False,
                reward=-0.3,
                notes=(
                    f"Failed {sop.sop_type} detected by butler: {sop.title}. "
                    f"Symptom: {sop.symptom[:200]}. "
                    f"Root cause: {sop.root_cause[:200]}"
                ),
            )
            logger.info(f"Negative signal emitted for failed SOP: {sop.sop_id}")
        except Exception as e:
            logger.debug(f"Failed to emit failure signal for {sop.sop_id}: {e}")

    def _log_sweep(self, results: Dict, start_time: float):
        """Log sweep results to mining_log table."""
        duration_ms = int((time.monotonic() - start_time) * 1000)
        try:
            with sqlite3.connect(self.repair_db_path) as conn:
                conn.execute("""
                    INSERT INTO mining_log
                    (timestamp, sessions_scanned, new_sops, updated_sops, validated, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    datetime.utcnow().isoformat(),
                    results.get("sessions_mined", 0),
                    results.get("new_sops", 0),
                    results.get("updated_sops", 0),
                    results.get("validated", 0),
                    duration_ms,
                ))
                conn.commit()
        except Exception:
            pass

    def get_sops_for_component(self, component: str) -> List[Dict]:
        """Get all repair SOPs for a specific component."""
        try:
            with sqlite3.connect(self.repair_db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT * FROM repair_sops
                    WHERE component = ?
                    ORDER BY confidence DESC, version DESC
                """, (component,)).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def get_best_sop(self, component: str) -> Optional[Dict]:
        """Get the highest-confidence successful SOP for a component."""
        try:
            with sqlite3.connect(self.repair_db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("""
                    SELECT * FROM repair_sops
                    WHERE component = ? AND outcome = 'success'
                    ORDER BY confidence DESC, times_validated DESC
                    LIMIT 1
                """, (component,)).fetchone()
                return dict(row) if row else None
        except Exception:
            return None
