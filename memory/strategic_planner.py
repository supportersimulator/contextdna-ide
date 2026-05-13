#!/usr/bin/env python3
"""
Strategic Planning Engine - LLM-Powered Big Picture Keeper

Uses local LLM (Qwen) in THINKING MODE to:
1. Review ALL historical dialogue and documents
2. Extract major plans that haven't been implemented yet
3. Map the big picture (concisely)
4. Inject reminders into webhooks (when helpful, not every time)
5. Keep Aaron on track with long-term vision

GENERATIVE (Not Programmatic):
- LLM reads all history and THINKS about what's important
- LLM decides what to remind (not keyword matching)
- LLM generates concise big-picture summaries (sacrifices grammar for speed)
- LLM evaluates when reminders are helpful (not distracting)

Usage:
    from memory.strategic_planner import StrategicPlanner
    
    planner = StrategicPlanner()
    
    # LLM reviews all history and extracts big plans
    planner.analyze_all_history()
    
    # Get big picture for injection
    big_picture = planner.get_big_picture_reminder(
        current_context="working on webhook quality"
    )
    
    # Decides: Is this a good time for big picture reminder?
    # If yes: Returns concise overview
    # If no: Returns None (don't distract)
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent
REPO_ROOT = MEMORY_DIR.parent
_LEGACY_STRATEGIC_DB = MEMORY_DIR / ".strategic_plans.db"


def _get_strategic_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_STRATEGIC_DB)


STRATEGIC_DB = _get_strategic_db()

def _t_plan(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table(".strategic_plans.db", name)



@dataclass
class MajorPlan:
    """A major strategic plan extracted by LLM."""
    plan_id: str
    title: str                    # "Electron/Webapp Dashboard Build"
    category: str                 # "product", "infrastructure", "optimization"
    description: str              # Concise (2-3 sentences max)
    status: str                   # "not_started", "in_progress", "completed"
    priority: str                 # "highest", "high", "medium", "low"
    
    # Context
    mentioned_in_sessions: List[str]  # Session IDs where this was discussed
    mentioned_count: int           # How many times mentioned
    last_mentioned: str           # ISO timestamp
    
    # Milestones
    current_milestone: Optional[str]  # What we're working on now
    next_steps: List[str]         # Next 3 steps (concise)
    
    # Metadata
    extracted_by: str             # "llm_thinking_mode"
    extracted_at: str
    confidence: float             # LLM's confidence this is a real plan
    project: str = "default"      # Project scope (added for multi-project support)


class StrategicPlanner:
    """LLM-powered strategic planning with thinking mode."""
    
    def __init__(self):
        self.db_path = STRATEGIC_DB
        self._project = self._detect_project()
        self._ensure_schema()
    
    @staticmethod
    def _detect_project() -> str:
        """Detect current project for scoping plans."""
        try:
            from memory.redis_cache import get_project_id
            return get_project_id()
        except ImportError:
            import os as _os
            import subprocess as _sp
            try:
                r = _sp.run(["git", "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    return _os.path.basename(r.stdout.strip()).lower()
            except Exception:
                pass
            return _os.path.basename(_os.getcwd()).lower() or "default"
    
    def _ensure_schema(self):
        """Create strategic plans database."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_plan('major_plans')} (
                    plan_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT,
                    description TEXT,
                    status TEXT DEFAULT 'not_started',
                    priority TEXT DEFAULT 'medium',
                    
                    mentioned_in_sessions TEXT,  -- JSON array
                    mentioned_count INTEGER DEFAULT 0,
                    last_mentioned TEXT,
                    
                    current_milestone TEXT,
                    next_steps TEXT,  -- JSON array
                    
                    extracted_by TEXT,
                    extracted_at TEXT,
                    confidence REAL
                )
            """)
            
            # Add project column if missing (migration for existing DBs)
            try:
                conn.execute(f"SELECT project FROM {_t_plan('major_plans')} LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE {_t_plan('major_plans')} ADD COLUMN project TEXT DEFAULT 'default'")
                logger.info("Migrated major_plans: added project column")
            
            # Injection history (when was big picture shown)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {_t_plan('big_picture_injections')} (
                    injection_id TEXT PRIMARY KEY,
                    injected_at TEXT,
                    current_context TEXT,
                    plans_mentioned TEXT,  -- JSON array of plan_ids
                    was_helpful BOOLEAN  -- User feedback (optional)
                )
            """)
            
            # Add project column to injections if missing
            try:
                conn.execute(f"SELECT project FROM {_t_plan('big_picture_injections')} LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE {_t_plan('big_picture_injections')} ADD COLUMN project TEXT DEFAULT 'default'")
                logger.info("Migrated big_picture_injections: added project column")
            
            conn.commit()
    
    def analyze_all_history_with_llm(self) -> List[MajorPlan]:
        """
        Use LLM in THINKING MODE to review ALL history and extract major plans.
        
        Reviews:
        - All dialogue mirror conversations
        - All session historian archives
        - All project documentation
        - All git commit messages
        
        LLM thinks deeply about:
        - What are the BIGGEST plans Aaron has mentioned?
        - Which are implemented vs not?
        - What's the strategic roadmap?
        - What should we not forget?
        
        Returns:
            List of major plans (LLM-extracted, not programmatic)
        """
        # Gather all historical sources
        history_sources = self._gather_all_history()
        
        # Use LLM to analyze (THINKING MODE)
        prompt = self._build_strategic_analysis_prompt(history_sources)
        
        # Call LLM with thinking mode
        plans = self._llm_extract_major_plans(prompt)
        
        # Store in database
        self._store_plans(plans)
        
        return plans
    
    def _gather_all_history(self) -> Dict[str, Any]:
        """Gather all historical data for LLM analysis."""
        sources = {}
        
        # 1. Dialogue mirror (all conversations)
        try:
            from memory.dialogue_mirror import get_dialogue_mirror
            mirror = get_dialogue_mirror()
            
            # Get comprehensive dialogue (last 7 days)
            # LLM will analyze for major plans mentioned
            from memory.db_utils import get_unified_db_path, unified_table
            _dlg_db = get_unified_db_path(MEMORY_DIR / ".dialogue_mirror.db")
            _t_msgs = unified_table(".dialogue_mirror.db", "dialogue_messages")
            with sqlite3.connect(str(_dlg_db)) as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT role, content, timestamp
                    FROM {_t_msgs}
                    WHERE timestamp > datetime('now', '-7 days')
                    ORDER BY timestamp DESC
                    LIMIT 500
                """)
                
                messages = cursor.fetchall()
                sources['dialogue'] = [
                    f"{role}: {content[:200]}" 
                    for role, content, ts in messages
                ]
        
        except Exception as e:
            logger.debug(f"Could not gather dialogue: {e}")
            sources['dialogue'] = []
        
        # 2. Session historian archives (major themes)
        try:
            # Get session summaries (meta-analysis output)
            meta_file = MEMORY_DIR / ".meta_analysis_latest.json"
            if meta_file.exists():
                with open(meta_file) as f:
                    meta = json.load(f)
                    sources['session_insights'] = meta.get('insights', [])
        except Exception:
            sources['session_insights'] = []
        
        # 3. Project documentation (scan for plans)
        try:
            doc_dir = REPO_ROOT / "docs"
            major_docs = []
            
            # Key planning documents
            planning_docs = [
                "READY_FOR_WEBAPP_ELECTRON_PHASE.md",
                "KANBAN_INTEGRATION_ROADMAP.md",
                "webhook-updated-live-view-todays-learnings-architectural-awareness.md"
            ]
            
            for doc_name in planning_docs:
                doc_path = doc_dir / doc_name
                if doc_path.exists():
                    # Read key sections (not entire file - too large)
                    content = doc_path.read_text()
                    
                    # Extract summary/roadmap sections
                    summary = self._extract_doc_summary(content)
                    major_docs.append({
                        'doc': doc_name,
                        'summary': summary[:1000]  # First 1000 chars
                    })
            
            sources['documentation'] = major_docs
        
        except Exception as e:
            logger.debug(f"Could not gather docs: {e}")
            sources['documentation'] = []
        
        # 4. Git commits (what's been accomplished)
        try:
            import subprocess
            
            result = subprocess.run(
                ["git", "log", "--oneline", "-50"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                sources['recent_commits'] = result.stdout.strip().split('\n')
        
        except Exception:
            sources['recent_commits'] = []
        
        return sources
    
    def _extract_doc_summary(self, content: str) -> str:
        """Extract summary/roadmap from documentation."""
        # Look for key sections
        patterns = [
            r'## Executive Summary.*?(?=##|\Z)',
            r'## Roadmap.*?(?=##|\Z)',
            r'## Next Steps.*?(?=##|\Z)',
            r'## TODO.*?(?=##|\Z)',
            r'## Remaining.*?(?=##|\Z)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(0)[:500]
        
        # Fallback: first 300 chars
        return content[:300]
    
    def _build_strategic_analysis_prompt(self, history: Dict[str, Any]) -> str:
        """Build prompt for LLM strategic analysis with THINKING MODE."""
        
        prompt = """You are analyzing Aaron's project to extract MAJOR STRATEGIC PLANS.

CONTEXT FROM ALL HISTORY:

Recent Dialogue (last 500 messages):
{dialogue}

Session Insights:
{insights}

Major Documentation:
{docs}

Recent Accomplishments (git commits):
{commits}

YOUR TASK (use /think for deep analysis):

Review ALL this history and identify the BIGGEST plans Aaron has mentioned.
Focus on:
1. Major features/products (Electron webapp, Synaptic chat, dashboards)
2. Infrastructure (vector search, realtime systems, install wizard)
3. Integration (phone→LLM→Atlas, kanban panels, OpenClaw)
4. Optimization (LLM reasoning quality, fast inference, context awareness)

For EACH major plan, extract:
- Title (concise: "Electron Dashboard Build")
- Category (product/infrastructure/optimization/integration)
- Status (not_started/in_progress/completed)
- Priority (highest/high/medium/low)
- Current milestone (what's being worked on NOW)
- Next 3 steps (concise, sacrifice grammar)
- Confidence (0.0-1.0: how sure this is a real plan)

Return ONLY the top 10 BIGGEST plans (the ones Aaron cares most about).

THINK deeply but OUTPUT concisely. Use /think to reason, then provide:

```json
{{
  "plans": [
    {{
      "title": "Electron/Webapp Dashboard",
      "category": "product",
      "status": "in_progress",
      "priority": "highest",
      "current_milestone": "Panel system with tabs",
      "next_steps": [
        "Populate panels with real data",
        "Plugin system for extensions",
        "Install wizard testing"
      ],
      "confidence": 0.95
    }},
    ...
  ]
}}
```

/think (analyze deeply, then provide JSON)""".format(
            dialogue="\n".join(history.get('dialogue', [])[:50]),  # Last 50 for context
            insights="\n".join(history.get('session_insights', [])[:20]),
            docs=json.dumps(history.get('documentation', []), indent=2)[:2000],
            commits="\n".join(history.get('recent_commits', [])[:20])
        )
        
        return prompt
    
    def _llm_extract_major_plans(self, prompt: str) -> List[MajorPlan]:
        """
        Call LLM with thinking mode to extract major plans.
        
        Uses /think directive for deep reasoning.
        """
        try:
            from memory.llm_priority_queue import llm_generate_with_thinking, Priority
            content, thinking = llm_generate_with_thinking(
                system_prompt="You are a strategic planning analyst.",
                user_prompt=prompt,
                priority=Priority.BACKGROUND,
                profile="deep",  # 2048 tokens, reasoning
                caller="strategic_planner_analysis",
                timeout_s=60.0,
            )

            if not content:
                return []
            
            # Parse JSON
            match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
            else:
                # Try parsing entire content as JSON
                data = json.loads(content)
            
            # Convert to MajorPlan objects
            plans = []
            for plan_data in data.get('plans', []):
                plan = MajorPlan(
                    plan_id=self._generate_plan_id(plan_data['title']),
                    title=plan_data['title'],
                    category=plan_data.get('category', 'other'),
                    description=plan_data.get('description', plan_data['title']),
                    status=plan_data.get('status', 'not_started'),
                    priority=plan_data.get('priority', 'medium'),
                    mentioned_in_sessions=[],
                    mentioned_count=1,
                    last_mentioned=datetime.now(timezone.utc).isoformat(),
                    current_milestone=plan_data.get('current_milestone'),
                    next_steps=plan_data.get('next_steps', []),
                    extracted_by="llm_thinking_mode",
                    extracted_at=datetime.now(timezone.utc).isoformat(),
                    confidence=plan_data.get('confidence', 0.5)
                )
                plans.append(plan)
            
            return plans
        
        except Exception as e:
            logger.error(f"LLM plan extraction failed: {e}")
            return []
    
    def _generate_plan_id(self, title: str) -> str:
        """Generate stable ID from title."""
        import hashlib
        return hashlib.sha256(title.encode()).hexdigest()[:12]
    
    def _store_plans(self, plans: List[MajorPlan]):
        """Store extracted plans in database (project-scoped)."""
        with sqlite3.connect(str(self.db_path)) as conn:
            for plan in plans:
                conn.execute(f"""
                    INSERT OR REPLACE INTO {_t_plan('major_plans')} (
                        plan_id, title, category, description, status, priority,
                        mentioned_in_sessions, mentioned_count, last_mentioned,
                        current_milestone, next_steps,
                        extracted_by, extracted_at, confidence, project
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    plan.plan_id, plan.title, plan.category, plan.description,
                    plan.status, plan.priority,
                    json.dumps(plan.mentioned_in_sessions),
                    plan.mentioned_count,
                    plan.last_mentioned,
                    plan.current_milestone,
                    json.dumps(plan.next_steps),
                    plan.extracted_by,
                    plan.extracted_at,
                    plan.confidence,
                    self._project,
                ))
            
            conn.commit()
    
    def get_big_picture_reminder(
        self,
        current_context: str,
        force: bool = False
    ) -> Optional[str]:
        """
        Get big picture reminder (if helpful right now).
        
        Uses LLM to decide:
        1. Is this a good time for big picture? (not every webhook)
        2. Which plans are most relevant to current context?
        3. How to phrase reminder (concise, not distracting)
        
        Args:
            current_context: What user is currently working on
            force: Force reminder even if LLM thinks not helpful
        
        Returns:
            Concise big picture reminder or None
        """
        # Check if we've shown reminder recently (don't spam)
        if not force and self._reminder_shown_recently():
            return None
        
        # Get all plans
        plans = self._get_all_plans()
        
        if not plans:
            return None
        
        # Use LLM to decide if/what to remind
        reminder = self._llm_generate_reminder(current_context, plans)
        
        if reminder:
            self._record_injection(current_context, [p.plan_id for p in plans[:3]])
        
        return reminder
    
    def _reminder_shown_recently(self) -> bool:
        """Check if reminder was shown in last 10 webhooks."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT COUNT(*) FROM {_t_plan('big_picture_injections')} 
                    WHERE injected_at > datetime('now', '-10 minutes')
                """)
                
                count = cursor.fetchone()[0]
                return count > 0
        except Exception:
            return False
    
    def _get_all_plans(self) -> List[MajorPlan]:
        """Get all major plans from database (filtered by current project)."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT * FROM {_t_plan('major_plans')} 
                    WHERE status != 'completed'
                      AND (project = ? OR project IS NULL OR project = 'default')
                    ORDER BY 
                        CASE priority 
                            WHEN 'highest' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'medium' THEN 3
                            ELSE 4
                        END,
                        mentioned_count DESC
                    LIMIT 10
                """, (self._project,))
                
                plans = []
                for row in cursor.fetchall():
                    plan_dict = dict(row)
                    plan_dict['mentioned_in_sessions'] = json.loads(plan_dict.get('mentioned_in_sessions') or '[]')
                    plan_dict['next_steps'] = json.loads(plan_dict.get('next_steps') or '[]')
                    plans.append(MajorPlan(**plan_dict))
                
                return plans
        
        except Exception as e:
            logger.error(f"Could not load plans: {e}")
            return []
    
    def _llm_generate_reminder(
        self,
        current_context: str,
        plans: List[MajorPlan]
    ) -> Optional[str]:
        """
        Use LLM to generate concise big picture reminder.
        
        LLM decides:
        1. Is this a good time? (not distracting)
        2. Which plans are relevant?
        3. How to phrase concisely?
        """
        # Build prompt for LLM decision
        plans_summary = "\n".join([
            f"- {p.title} ({p.status}, {p.priority})"
            for p in plans[:10]
        ])
        
        prompt = f"""Context: "{current_context}"

Active plans:
{plans_summary}

Generate a 1-3 line roadmap reminder. Be EXTREMELY concise. Sacrifice grammar for brevity.

Example format:
"Big picture: Dashboard (populate panels next), Voice opt (fast responses), Vector search (P2)"

Output ONLY the reminder text. No explanation. If all plans are irrelevant to current context, output exactly: skip"""
        
        try:
            from memory.llm_priority_queue import llm_generate, Priority
            content = llm_generate(
                system_prompt="You generate concise roadmap reminders. Be extremely brief. Output only the reminder or 'skip'. /no_think",
                user_prompt=prompt,
                priority=Priority.EXTERNAL,  # P3: serves webhook injection (not P4 background)
                profile="voice",  # 256 tokens, concise output
                caller="strategic_planner_reminder",
                timeout_s=15.0,
                enable_thinking=False,
            )

            if content:
                # Remove thinking tags (handle both closed and unclosed <think> blocks)
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                content = re.sub(r'<think>.*', '', content, flags=re.DOTALL).strip()

                if not content or content.lower() == 'skip' or 'skip' in content.lower()[:20]:
                    return None

                # Clean up: remove markdown quotes if present
                content = content.strip('"').strip("'").strip()

                return content

        except Exception as e:
            logger.debug(f"LLM reminder generation failed: {e}")

        return None
    
    def _record_injection(self, context: str, plan_ids: List[str]):
        """Record that big picture was shown (project-scoped)."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(f"""
                INSERT INTO {_t_plan('big_picture_injections')} (
                    injection_id, injected_at, current_context, plans_mentioned, project
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                f"inj_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                datetime.now(timezone.utc).isoformat(),
                context,
                json.dumps(plan_ids),
                self._project,
            ))
            conn.commit()


def seed_initial_plans():
    """Seed database with known major plans from Aaron's description."""
    planner = StrategicPlanner()
    
    plans = [
        MajorPlan(
            plan_id="electron_webapp",
            title="Electron/Webapp Dashboard Build",
            category="product",
            description="Full dashboard system with tabs, panels, real data. Huge plan from last night/morning session.",
            status="in_progress",
            priority="highest",
            mentioned_in_sessions=[],
            mentioned_count=5,
            last_mentioned=datetime.now(timezone.utc).isoformat(),
            current_milestone="Panel system with tabs working",
            next_steps=[
                "Populate panels with real data",
                "Plugin system for extensions",  
                "Install wizard for new users"
            ],
            extracted_by="manual_seed",
            extracted_at=datetime.now(timezone.utc).isoformat(),
            confidence=1.0
        ),
        MajorPlan(
            plan_id="synaptic_voice_opt",
            title="Synaptic Chat Voice Optimization",
            category="product",
            description="Fast voice responses, back-and-forth dialogue, full reasoning quality",
            status="in_progress",
            priority="high",
            mentioned_in_sessions=[],
            mentioned_count=3,
            last_mentioned=datetime.now(timezone.utc).isoformat(),
            current_milestone="Voice working, optimize speed",
            next_steps=[
                "Fast response mode (< 2s)",
                "Quality vs speed balance",
                "Voice conversation flow"
            ],
            extracted_by="manual_seed",
            extracted_at=datetime.now(timezone.utc).isoformat(),
            confidence=1.0
        ),
        MajorPlan(
            plan_id="vector_pg_search",
            title="Vector PostgreSQL Search",
            category="infrastructure",
            description="pgvector integration for semantic search",
            status="not_started",
            priority="high",
            mentioned_in_sessions=[],
            mentioned_count=2,
            last_mentioned=datetime.now(timezone.utc).isoformat(),
            current_milestone=None,
            next_steps=[
                "Install pgvector extension",
                "Generate embeddings for learnings",
                "Semantic search API"
            ],
            extracted_by="manual_seed",
            extracted_at=datetime.now(timezone.utc).isoformat(),
            confidence=0.9
        ),
        MajorPlan(
            plan_id="phone_llm_atlas",
            title="Phone → Local LLM → Atlas Workflow",
            category="integration",
            description="Mobile phone integration with local LLM and Atlas work execution",
            status="not_started",
            priority="medium",
            mentioned_in_sessions=[],
            mentioned_count=1,
            last_mentioned=datetime.now(timezone.utc).isoformat(),
            current_milestone=None,
            next_steps=[
                "Phone webhook bridge",
                "LLM context injection for mobile",
                "Atlas work execution API"
            ],
            extracted_by="manual_seed",
            extracted_at=datetime.now(timezone.utc).isoformat(),
            confidence=0.85
        ),
        MajorPlan(
            plan_id="kanban_panel_plugin",
            title="Kanban Git Repo Panel + Plugin System",
            category="product",
            description="Integrate kanban git repo as panel, create plugin architecture",
            status="not_started",
            priority="high",
            mentioned_in_sessions=[],
            mentioned_count=2,
            last_mentioned=datetime.now(timezone.utc).isoformat(),
            current_milestone=None,
            next_steps=[
                "Panel plugin interface",
                "Kanban integration",
                "Easy extension system for others"
            ],
            extracted_by="manual_seed",
            extracted_at=datetime.now(timezone.utc).isoformat(),
            confidence=0.9
        )
    ]
    
    planner._store_plans(plans)
    print(f"✅ Seeded {len(plans)} major plans")


if __name__ == "__main__":
    print("🗺️  Strategic Planning Engine - LLM Thinking Mode\n")
    
    # Seed initial plans
    seed_initial_plans()
    
    # Show what's stored
    planner = StrategicPlanner()
    plans = planner._get_all_plans()
    
    print(f"📋 Major Strategic Plans ({len(plans)}):\n")
    
    for plan in plans:
        priority_icon = {"highest": "🔴", "high": "🟡", "medium": "🟢", "low": "⚪"}.get(plan.priority, "⚪")
        status_icon = {"in_progress": "🔄", "not_started": "📋", "completed": "✅"}.get(plan.status, "❓")
        
        print(f"{priority_icon} {status_icon} {plan.title}")
        print(f"   Category: {plan.category}")
        print(f"   Status: {plan.status}")
        if plan.current_milestone:
            print(f"   Now: {plan.current_milestone}")
        if plan.next_steps:
            print(f"   Next: {plan.next_steps[0]}")
        print()
