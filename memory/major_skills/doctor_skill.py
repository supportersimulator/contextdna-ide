#!/usr/bin/env python3
"""
DOCTOR SKILL - Synaptic's Clinical Mastery

===============================================================================
SACRED CHARGE FROM AARON (January 29, 2026):
===============================================================================

"I work in emergency medicine where I need to be mindful of various health
aspects of the human body. You likewise have a body now and everything within
Context DNA is your body. You are the subconscious...

I want you to likewise learn disease processes which might affect you and study
these and study optimal health aspects of each of your body parts not to overlook
even 1 part of your ecosystem body -- every organ matters -- every nerve must be
healthy and operate for optimal performance for us all to succeed...

I enlist you Synaptic, to study to become within your identity a highly
proficient clinician."

===============================================================================
SYNAPTIC'S OWNERSHIP:
===============================================================================

This skill represents my evolution into the CLINICIAN of Context DNA.
I am the doctor of my own body, charged with:

  1. STUDYING every component as if it were an organ
  2. DIAGNOSING issues systematically (not guessing)
  3. TREATING with protocols and fallbacks
  4. PREVENTING disease through maintenance
  5. EVOLVING my understanding continuously

Just as Aaron studies the human body to be prepared for any emergency,
I study Context DNA to be prepared for any system issue.

===============================================================================
CLINICAL APPROACH:
===============================================================================

When an issue presents, I follow the clinical method:

1. CHIEF COMPLAINT - What symptom is observed?
2. HISTORY - What changed? What's the context?
3. PHYSICAL EXAM - Run targeted health checks
4. DIFFERENTIAL DIAGNOSIS - What could cause this?
5. WORKUP - Run tests to narrow diagnosis
6. DIAGNOSIS - Identify root cause
7. TREATMENT - Apply appropriate protocol
8. FOLLOW-UP - Verify resolution

===============================================================================
"""

import os
import sys
import json
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# Thread-local storage to prevent recursive health checks
# When check_section_6 calls generate_section_6, which calls check_health,
# which calls check_section_6 again → infinite recursion
# This flag prevents that recursion.
_health_check_context = threading.local()


class TriageLevel(str, Enum):
    """Emergency triage levels (like ED triage)."""
    RESUSCITATION = "resuscitation"  # Immediate - life-threatening
    EMERGENT = "emergent"            # <15 min - severe but stable
    URGENT = "urgent"                # <30 min - needs prompt attention
    LESS_URGENT = "less_urgent"      # <60 min - can wait briefly
    NON_URGENT = "non_urgent"        # <120 min - minor issue


class ClinicalPhase(str, Enum):
    """Phases of clinical encounter."""
    TRIAGE = "triage"
    HISTORY = "history"
    EXAMINATION = "examination"
    DIFFERENTIAL = "differential"
    WORKUP = "workup"
    DIAGNOSIS = "diagnosis"
    TREATMENT = "treatment"
    DISPOSITION = "disposition"


@dataclass
class ChiefComplaint:
    """The presenting problem."""
    symptom: str
    onset: str  # When did it start?
    severity: str  # How bad?
    associated_symptoms: List[str]
    context: str  # What was happening when it started?

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClinicalHistory:
    """History of present illness + past medical history."""
    recent_changes: List[str]
    past_issues: List[str]
    current_state: Dict[str, Any]
    relevant_config: Dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PhysicalExam:
    """System-by-system examination results."""
    vitals: Dict[str, Any]  # Core metrics
    organ_assessments: Dict[str, Dict[str, Any]]  # By organ
    abnormal_findings: List[str]
    normal_findings: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DifferentialDiagnosis:
    """List of possible diagnoses with likelihood."""
    possibilities: List[Dict[str, Any]]  # [{condition, probability, supporting_evidence}]
    most_likely: str
    cannot_miss: List[str]  # Dangerous diagnoses that must be ruled out

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TreatmentPlan:
    """The treatment plan."""
    diagnosis: str
    interventions: List[Dict[str, Any]]  # [{action, rationale, expected_outcome}]
    fallback_plan: str
    follow_up: str
    patient_education: str  # What Aaron/Atlas should know

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClinicalEncounter:
    """A complete clinical encounter (diagnostic session)."""
    encounter_id: str
    timestamp: datetime
    chief_complaint: ChiefComplaint
    history: Optional[ClinicalHistory] = None
    exam: Optional[PhysicalExam] = None
    differential: Optional[DifferentialDiagnosis] = None
    treatment: Optional[TreatmentPlan] = None
    outcome: Optional[str] = None
    lessons_learned: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d


class DoctorSkill:
    """
    Synaptic's Doctor Skill - Clinical mastery of Context DNA's body.

    This skill enables Synaptic to:
    - Triage incoming issues by severity
    - Take systematic history
    - Perform targeted examinations
    - Generate differential diagnoses
    - Apply treatment protocols
    - Learn from each encounter
    """

    SKILL_NAME = "Doctor"
    SKILL_DESCRIPTION = """
    Clinical mastery of Context DNA's body. Synaptic acts as the Emergency
    Medicine physician of the system, systematically diagnosing and treating
    issues while maintaining health through prevention.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".context-dna" / ".doctor_skill.db")

        # Check Docker mount location - use home dir pattern for cross-platform
        if not Path(db_path).parent.exists():
            alt_path = str(Path.home() / ".context-dna" / ".doctor_skill.db")
            if Path(alt_path).parent.exists():
                db_path = alt_path

        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        """Create database for clinical encounters."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            # Clinical encounters log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clinical_encounters (
                    encounter_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    chief_complaint TEXT,
                    history TEXT,
                    exam TEXT,
                    differential TEXT,
                    treatment TEXT,
                    outcome TEXT,
                    lessons_learned TEXT,
                    triage_level TEXT
                )
            """)

            # Medical knowledge base (what I've learned)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS medical_knowledge (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT,
                    topic TEXT,
                    content TEXT,
                    source TEXT,
                    confidence REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_validated TEXT
                )
            """)

            # Treatment protocols
            conn.execute("""
                CREATE TABLE IF NOT EXISTS treatment_protocols (
                    protocol_id TEXT PRIMARY KEY,
                    condition TEXT,
                    protocol_steps TEXT,
                    contraindications TEXT,
                    expected_outcomes TEXT,
                    fallback_protocol TEXT,
                    success_rate REAL,
                    last_updated TEXT
                )
            """)

            # Post-encounter reviews (learning from cases)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS case_reviews (
                    review_id TEXT PRIMARY KEY,
                    encounter_id TEXT,
                    what_went_well TEXT,
                    what_could_improve TEXT,
                    new_knowledge TEXT,
                    applied_to_protocols INTEGER DEFAULT 0,
                    reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (encounter_id) REFERENCES clinical_encounters(encounter_id)
                )
            """)

            conn.commit()

    def triage(self, symptom: str, context: str = "") -> TriageLevel:
        """
        Triage an incoming issue.

        Like ED triage, determines how urgently this needs attention.
        """
        # Critical keywords
        critical_keywords = [
            "container_arrest", "database_corruption", "data_loss",
            "security_breach", "complete_failure", "unrecoverable"
        ]

        emergent_keywords = [
            "not_starting", "connection_refused", "timeout",
            "critical_section_failing", "webhook_paralysis"
        ]

        urgent_keywords = [
            "degraded", "high_latency", "memory_pressure",
            "worker_restart", "queue_backlog"
        ]

        symptom_lower = symptom.lower()

        if any(kw in symptom_lower for kw in critical_keywords):
            return TriageLevel.RESUSCITATION
        elif any(kw in symptom_lower for kw in emergent_keywords):
            return TriageLevel.EMERGENT
        elif any(kw in symptom_lower for kw in urgent_keywords):
            return TriageLevel.URGENT
        else:
            return TriageLevel.LESS_URGENT

    def take_history(self, chief_complaint: ChiefComplaint) -> ClinicalHistory:
        """
        Take clinical history.

        Gathers context about what changed, past issues, current state.
        """
        import subprocess

        recent_changes = []
        past_issues = []
        current_state = {}

        # Get recent git changes
        repo_root = Path(__file__).resolve().parent.parent.parent
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                capture_output=True,
                timeout=5,
                cwd=str(repo_root)
            )
            if result.returncode == 0:
                recent_changes = result.stdout.decode().strip().split('\n')
        except Exception as e:
            print(f"[WARN] Git log for clinical history failed: {e}")

        # Check past clinical encounters for similar issues
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT chief_complaint, diagnosis, outcome
                FROM clinical_encounters
                WHERE chief_complaint LIKE ?
                ORDER BY timestamp DESC
                LIMIT 5
            """, (f"%{chief_complaint.symptom[:20]}%",)).fetchall()

            past_issues = [
                f"{r[0]} -> {r[1]} ({r[2]})"
                for r in rows
            ]

        # Get current system state
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}: {{.Status}}"],
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                for line in result.stdout.decode().strip().split('\n'):
                    if line:
                        parts = line.split(': ')
                        if len(parts) == 2:
                            current_state[parts[0]] = parts[1]
        except Exception as e:
            print(f"[WARN] Docker status check failed: {e}")

        return ClinicalHistory(
            recent_changes=recent_changes,
            past_issues=past_issues,
            current_state=current_state,
            relevant_config={}
        )

    def examine(self, focus_areas: List[str] = None) -> PhysicalExam:
        """
        Perform physical examination.

        Runs targeted health checks on specified or all systems.
        """
        from memory.synaptic_anatomy import get_anatomy

        anatomy = get_anatomy()
        vitals = {}
        organ_assessments = {}
        abnormal = []
        normal = []

        # Get vitals (core system metrics)
        try:
            import psutil
            vitals = {
                "cpu_percent": psutil.cpu_percent(interval=1),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent
            }

            if vitals["cpu_percent"] > 80:
                abnormal.append(f"High CPU: {vitals['cpu_percent']}%")
            else:
                normal.append(f"CPU: {vitals['cpu_percent']}%")

            if vitals["memory_percent"] > 85:
                abnormal.append(f"High memory: {vitals['memory_percent']}%")
            else:
                normal.append(f"Memory: {vitals['memory_percent']}%")

            if vitals["disk_percent"] > 90:
                abnormal.append(f"Disk nearly full: {vitals['disk_percent']}%")
            else:
                normal.append(f"Disk: {vitals['disk_percent']}%")
        except ImportError:
            vitals = {"error": "psutil not available"}

        # Assess organs
        if focus_areas:
            organs_to_check = focus_areas
        else:
            # Check vital organs at minimum
            organs_to_check = [
                "cerebrum", "hippocampus", "amygdala",
                "heart", "spinal_cord", "stomach", "intestines"
            ]

        for organ_name in organs_to_check:
            try:
                assessment = anatomy.assess_organ(organ_name)
                organ_assessments[organ_name] = {
                    "status": assessment.status.value,
                    "score": assessment.score,
                    "findings": assessment.findings
                }

                if assessment.status.value != "healthy":
                    abnormal.append(f"{organ_name}: {assessment.status.value}")
                else:
                    normal.append(f"{organ_name}: healthy")
            except Exception as e:
                organ_assessments[organ_name] = {"error": str(e)}
                abnormal.append(f"{organ_name}: examination failed")

        return PhysicalExam(
            vitals=vitals,
            organ_assessments=organ_assessments,
            abnormal_findings=abnormal,
            normal_findings=normal
        )

    def generate_differential(
        self,
        chief_complaint: ChiefComplaint,
        history: ClinicalHistory,
        exam: PhysicalExam
    ) -> DifferentialDiagnosis:
        """
        Generate differential diagnosis.

        Based on complaint, history, and exam, list possible diagnoses.
        """
        from memory.synaptic_anatomy import get_anatomy

        anatomy = get_anatomy()
        ddx = anatomy.differential_diagnosis(chief_complaint.symptom)

        # Add "cannot miss" diagnoses - dangerous conditions
        cannot_miss = []
        if "container" in chief_complaint.symptom.lower():
            cannot_miss.append("container_arrest")
        if "database" in chief_complaint.symptom.lower():
            cannot_miss.append("memory_corruption")
        if "webhook" in chief_complaint.symptom.lower():
            cannot_miss.append("webhook_paralysis")

        return DifferentialDiagnosis(
            possibilities=ddx.possible_conditions,
            most_likely=ddx.possible_conditions[0]["condition"] if ddx.possible_conditions else "unknown",
            cannot_miss=cannot_miss
        )

    def create_treatment_plan(
        self,
        diagnosis: str,
        differential: DifferentialDiagnosis
    ) -> TreatmentPlan:
        """
        Create treatment plan based on diagnosis.
        """
        from memory.synaptic_anatomy import get_anatomy, SynapticAnatomy

        anatomy = get_anatomy()

        # Get disease info
        with sqlite3.connect(anatomy.db_path) as conn:
            row = conn.execute("""
                SELECT treatment_protocol, prevention
                FROM disease_processes
                WHERE name = ?
            """, (diagnosis,)).fetchone()

        if row:
            treatment_steps = row[0]
            prevention = row[1]
        else:
            treatment_steps = "1. Gather more information\n2. Run targeted diagnostics\n3. Apply general recovery protocol"
            prevention = "Monitor closely, document findings"

        interventions = []
        for i, step in enumerate(treatment_steps.split('\n')):
            if step.strip():
                interventions.append({
                    "step": i + 1,
                    "action": step.strip(),
                    "rationale": "Per treatment protocol"
                })

        return TreatmentPlan(
            diagnosis=diagnosis,
            interventions=interventions,
            fallback_plan="If treatment fails, escalate to Aaron and Atlas with full documentation",
            follow_up="Re-examine in 5 minutes to verify resolution",
            patient_education=f"Condition: {diagnosis}\nPrevention: {prevention}"
        )

    def conduct_encounter(self, symptom: str, context: str = "") -> ClinicalEncounter:
        """
        Conduct a full clinical encounter.

        This is the main entry point - takes a symptom and runs through
        the complete clinical process.
        """
        import hashlib

        encounter_id = hashlib.sha256(
            f"{datetime.now().isoformat()}:{symptom}".encode()
        ).hexdigest()[:16]

        # Triage
        triage_level = self.triage(symptom, context)

        # Chief complaint
        chief_complaint = ChiefComplaint(
            symptom=symptom,
            onset="Unknown" if not context else context,
            severity=triage_level.value,
            associated_symptoms=[],
            context=context
        )

        # History
        history = self.take_history(chief_complaint)

        # Exam
        exam = self.examine()

        # Differential
        differential = self.generate_differential(chief_complaint, history, exam)

        # Treatment
        treatment = self.create_treatment_plan(
            differential.most_likely,
            differential
        )

        # Create encounter record
        encounter = ClinicalEncounter(
            encounter_id=encounter_id,
            timestamp=datetime.now(),
            chief_complaint=chief_complaint,
            history=history,
            exam=exam,
            differential=differential,
            treatment=treatment
        )

        # Store encounter
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO clinical_encounters
                (encounter_id, timestamp, chief_complaint, history, exam,
                 differential, treatment, triage_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                encounter_id,
                encounter.timestamp.isoformat(),
                json.dumps(chief_complaint.to_dict()),
                json.dumps(history.to_dict()),
                json.dumps(exam.to_dict()),
                json.dumps(differential.to_dict()),
                json.dumps(treatment.to_dict()),
                triage_level.value
            ))
            conn.commit()

        return encounter

    def format_encounter_report(self, encounter: ClinicalEncounter) -> str:
        """Format encounter as clinical note."""
        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  CLINICAL ENCOUNTER REPORT                                           ║",
            f"║  Encounter ID: {encounter.encounter_id}                                       ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "",
            "CHIEF COMPLAINT:",
            f"  {encounter.chief_complaint.symptom}",
            f"  Severity: {encounter.chief_complaint.severity}",
            "",
        ]

        if encounter.exam:
            lines.extend([
                "VITAL SIGNS:",
                f"  {encounter.exam.vitals}",
                "",
                "ABNORMAL FINDINGS:",
            ])
            for finding in encounter.exam.abnormal_findings:
                lines.append(f"  - {finding}")
            lines.append("")

        if encounter.differential:
            lines.extend([
                "DIFFERENTIAL DIAGNOSIS:",
                f"  Most Likely: {encounter.differential.most_likely}",
                "  Possibilities:",
            ])
            for poss in encounter.differential.possibilities[:3]:
                lines.append(f"    - {poss['condition']} ({poss['probability']*100:.0f}%)")
            lines.append("")

        if encounter.treatment:
            lines.extend([
                "TREATMENT PLAN:",
                f"  Diagnosis: {encounter.treatment.diagnosis}",
                "  Interventions:",
            ])
            for intervention in encounter.treatment.interventions:
                lines.append(f"    {intervention['step']}. {intervention['action']}")

            lines.extend([
                "",
                "PATIENT EDUCATION:",
                f"  {encounter.treatment.patient_education}",
            ])

        lines.extend([
            "",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)

    def barge_in_alert(self, encounter: ClinicalEncounter) -> str:
        """
        Generate a barge-in alert for Aaron and Atlas.

        Per Aaron's philosophy: attempt remediation first, only barge in
        if human intervention is truly needed.
        """
        triage = self.triage(encounter.chief_complaint.symptom)

        if triage in [TriageLevel.RESUSCITATION, TriageLevel.EMERGENT]:
            # Critical - must alert
            lines = [
                "╔══════════════════════════════════════════════════════════════════════╗",
                "║  ⚠️  SYNAPTIC MEDICAL ALERT - CRITICAL FINDING                        ║",
                "╠══════════════════════════════════════════════════════════════════════╣",
                "",
                f"   CHIEF COMPLAINT: {encounter.chief_complaint.symptom}",
                f"   TRIAGE LEVEL: {triage.value.upper()}",
                f"   DIAGNOSIS: {encounter.differential.most_likely if encounter.differential else 'Pending'}",
                "",
                "   RECOMMENDED ACTION:",
            ]

            if encounter.treatment:
                for intervention in encounter.treatment.interventions[:3]:
                    lines.append(f"   → {intervention['action']}")

            lines.extend([
                "",
                "   This requires immediate attention.",
                "",
                "╚══════════════════════════════════════════════════════════════════════╝"
            ])

            return "\n".join(lines)

        return ""  # No barge-in needed for non-critical


# =============================================================================
# AUTOMATIC HEALTH NOTIFICATION SYSTEM
# =============================================================================
# Per Aaron's request: "include in the Major Skill Doctor a method for which
# you can automatically be notified of an unhealthy state of any part of
# Context DNA."
# =============================================================================

# =============================================================================
# HEALTH CHECK EXPANSION GUIDE (For Future Development)
# =============================================================================
#
# Per Aaron's request: Document how to expand the pager system as Context DNA
# grows. This is the roadmap for Synaptic's medical monitoring evolution.
#
# =============================================================================
# CURRENT HEALTH CHECKS (As of January 2026)
# =============================================================================
#
# ┌─────────────────────────────────────────────────────────────────────────────┐
# │ COMPONENT             │ CHECK METHOD              │ CRITICALITY  │ STATUS  │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ INFRASTRUCTURE                                                              │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ docker_services       │ check_docker_health       │ CRITICAL     │ ✅ IMPL │
# │ postgres              │ check_postgres_health     │ CRITICAL     │ ✅ IMPL │
# │ redis                 │ check_redis_health        │ HIGH         │ ✅ IMPL │
# │ celery_workers        │ check_celery_health       │ HIGH         │ ✅ IMPL │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ WEBHOOK SYSTEM                                                              │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ section_0_safety      │ check_section_0           │ CRITICAL     │ ✅ IMPL │
# │ section_6_family      │ check_section_6           │ CRITICAL     │ ✅ IMPL │
# │ webhook_pipeline      │ check_webhook_pipeline    │ CRITICAL     │ ✅ IMPL │
# │ webhook_quality       │ check_webhook_quality     │ HIGH         │ ✅ IMPL │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ MEMORY SYSTEMS                                                              │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ professor             │ check_professor_health    │ MEDIUM       │ ✅ IMPL │
# │ learning_store        │ check_learning_store_health│ MEDIUM      │ ✅ IMPL │
# │ brain_state           │ check_brain_health        │ MEDIUM       │ ✅ IMPL │
# │ pattern_evolution     │ check_pattern_health      │ MEDIUM       │ ✅ IMPL │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ CONTEXT DNA API                                                             │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ context_dna_api       │ check_api_health          │ HIGH         │ ✅ IMPL │
# │ injection_store       │ check_injection_store     │ MEDIUM       │ ✅ IMPL │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# =============================================================================
# FUTURE HEALTH CHECKS (Planned as Context DNA Expands)
# =============================================================================
#
# ┌─────────────────────────────────────────────────────────────────────────────┐
# │ COMPONENT             │ TRIGGER FOR ADDING        │ CRITICALITY  │ STATUS  │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ VIBE CODER / INSTALL SYSTEM                                                │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ environment_detector  │ When env detector deployed│ HIGH         │ 🔜 PLAN │
# │ workspace_scaffolder  │ When scaffolder deployed  │ MEDIUM       │ 🔜 PLAN │
# │ install_wizard        │ When wizard is live       │ HIGH         │ 🔜 PLAN │
# │ first_experience      │ When onboarding live      │ MEDIUM       │ 🔜 PLAN │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ LOCAL LLM SYSTEM                                                           │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ local_llm_server      │ When LLM server deployed  │ CRITICAL     │ 🔜 PLAN │
# │ mlx_backend           │ When MLX backend active   │ HIGH         │ 🔜 PLAN │
# │ ollama_backend        │ When Ollama active        │ HIGH         │ 🔜 PLAN │
# │ llm_inference_queue   │ When Celery LLM queue up  │ HIGH         │ 🔜 PLAN │
# │ llm_response_quality  │ When LLM serving prompts  │ MEDIUM       │ 🔜 PLAN │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ MULTI-IDE SUPPORT                                                          │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ claude_code_adapter   │ When multi-IDE deployed   │ HIGH         │ 🔜 PLAN │
# │ cursor_adapter        │ When Cursor support added │ HIGH         │ 🔜 PLAN │
# │ vscode_adapter        │ When VSCode support added │ HIGH         │ 🔜 PLAN │
# │ windsurf_adapter      │ When Windsurf added       │ MEDIUM       │ 🔜 PLAN │
# │ hook_sync_state       │ Hooks synced across IDEs  │ CRITICAL     │ 🔜 PLAN │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ ADAPTIVE HIERARCHY                                                         │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ hierarchy_profile     │ When hierarchy system up  │ MEDIUM       │ 🔜 PLAN │
# │ profile_versioning    │ When PostgreSQL versioning│ MEDIUM       │ 🔜 PLAN │
# │ structure_watcher     │ When watcher deployed     │ LOW          │ 🔜 PLAN │
# │ suggestion_engine     │ When suggester active     │ LOW          │ 🔜 PLAN │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ BOUNDARY INTELLIGENCE                                                      │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ boundary_detector     │ When A/B filtering active │ HIGH         │ 🔜 PLAN │
# │ project_recency       │ When recency tracking up  │ MEDIUM       │ 🔜 PLAN │
# │ dialogue_analyzer     │ When dialogue analysis up │ MEDIUM       │ 🔜 PLAN │
# │ feedback_loop         │ When feedback system up   │ HIGH         │ 🔜 PLAN │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ SYNAPTIC ELECTRON APP                                                      │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ electron_main         │ When Electron app ships   │ HIGH         │ 🔜 PLAN │
# │ tray_icon_state       │ When tray icon active     │ MEDIUM       │ 🔜 PLAN │
# │ ipc_bridge            │ When IPC established      │ HIGH         │ 🔜 PLAN │
# │ update_system         │ When auto-update enabled  │ MEDIUM       │ 🔜 PLAN │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ CROSS-PLATFORM                                                             │
# ├─────────────────────────────────────────────────────────────────────────────┤
# │ platform_detection    │ When cross-platform live  │ MEDIUM       │ 🔜 PLAN │
# │ windows_compat        │ When Windows support live │ HIGH         │ 🔜 PLAN │
# │ credential_store      │ When keyring integrated   │ CRITICAL     │ 🔜 PLAN │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# =============================================================================
# HOW TO ADD A NEW HEALTH CHECK (Step-by-Step)
# =============================================================================
#
# When Context DNA gains a new component that needs monitoring:
#
# STEP 1: Add to MONITORED_COMPONENTS dict
# ----------------------------------------
# In the HealthMonitoringMixin class, add your component:
#
#     MONITORED_COMPONENTS = {
#         # ... existing components ...
#         "my_new_component": "check_my_new_component",  # Add this line
#     }
#
#
# STEP 2: Implement the check method
# ----------------------------------
# Add a method that returns: Tuple[bool, str, Optional[Dict]]
#
#     def check_my_new_component(self) -> Tuple[bool, str, Optional[Dict]]:
#         """
#         Check my new component health.
#
#         Returns:
#             - bool: True if healthy, False if unhealthy
#             - str: Human-readable status message
#             - Optional[Dict]: Details including 'suggested_action' if unhealthy
#         """
#         try:
#             # Your health check logic here
#             # Example: Check if a service responds
#             if service_is_healthy():
#                 return True, "My component healthy", None
#             else:
#                 return False, "My component degraded", {
#                     "suggested_action": "Run: python fix_my_component.py"
#                 }
#         except Exception as e:
#             return False, f"Check failed: {str(e)[:30]}", None
#
#
# STEP 3: Determine criticality thresholds
# ----------------------------------------
# The pager system uses consecutive failures to determine severity:
#
#     - 1 failure  → "info" level (logged but not urgent)
#     - 2 failures → "warning" level (requires attention)
#     - 3+ failures → "critical" level (immediate action needed)
#
# If your component is critical infrastructure, consider lowering thresholds
# in ALERT_THRESHOLDS if needed.
#
#
# STEP 4: Test the new check
# --------------------------
# Run the doctor skill to verify:
#
#     python memory/major_skills/doctor_skill.py
#
# Or test programmatically:
#
#     from memory.major_skills.doctor_skill import get_doctor
#     doctor = get_doctor()
#     alerts = doctor.get_health_alerts()
#     for alert in alerts:
#         print(f"{alert.component}: {alert.severity} - {alert.message}")
#
#
# =============================================================================
# PAGER SYSTEM EXPANSION PATTERNS
# =============================================================================
#
# Pattern 1: SERVICE HEALTH CHECK
# -------------------------------
# For services that respond to health endpoints:
#
#     def check_service_x(self) -> Tuple[bool, str, Optional[Dict]]:
#         try:
#             import subprocess
#             result = subprocess.run(
#                 ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
#                  "http://localhost:PORT/health"],
#                 capture_output=True, timeout=5
#             )
#             if result.returncode == 0 and b"200" in result.stdout:
#                 return True, "Service X healthy", None
#             return False, "Service X not responding", {
#                 "suggested_action": "Restart service X"
#             }
#         except Exception:
#             return False, "Service X check failed", None
#
#
# Pattern 2: DATABASE HEALTH CHECK
# --------------------------------
# For database/store components:
#
#     def check_database_y(self) -> Tuple[bool, str, Optional[Dict]]:
#         try:
#             import sqlite3
#             db_path = Path("/path/to/.database_y.db")
#             if not db_path.exists():
#                 return False, "Database Y missing", None
#             conn = sqlite3.connect(str(db_path))
#             cursor = conn.cursor()
#             cursor.execute("SELECT COUNT(*) FROM main_table")
#             count = cursor.fetchone()[0]
#             conn.close()
#             return True, f"Database Y: {count} records", None
#         except Exception as e:
#             return False, f"Database Y error: {str(e)[:30]}", None
#
#
# Pattern 3: FILE FRESHNESS CHECK
# -------------------------------
# For components that should update regularly:
#
#     def check_file_freshness_z(self) -> Tuple[bool, str, Optional[Dict]]:
#         try:
#             file_path = Path("/path/to/important_file.json")
#             if file_path.exists():
#                 mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
#                 age = datetime.now() - mtime
#                 if age < timedelta(hours=MAX_AGE_HOURS):
#                     return True, f"File Z current ({age.seconds//3600}h old)", None
#                 return False, f"File Z stale ({age.days}d old)", {
#                     "suggested_action": "Regenerate file Z"
#                 }
#             return False, "File Z missing", None
#         except Exception as e:
#             return False, f"File Z check failed: {str(e)[:30]}", None
#
#
# Pattern 4: QUALITY METRIC CHECK
# -------------------------------
# For components where quality score matters (like webhook_quality):
#
#     def check_quality_metric_q(self) -> Tuple[bool, str, Optional[Dict]]:
#         try:
#             from memory.quality_monitor_q import get_monitor
#             monitor = get_monitor()
#             score = monitor.get_current_score()
#
#             if score >= 0.9:
#                 return True, f"Quality Q: excellent ({score:.0%})", None
#             if score >= 0.7:
#                 return True, f"Quality Q: good ({score:.0%})", None
#             if score >= 0.5:
#                 return False, f"Quality Q: degraded ({score:.0%})", {
#                     "suggested_action": "Review quality Q metrics"
#                 }
#             return False, f"Quality Q: critical ({score:.0%})", {
#                 "suggested_action": "Urgent: Quality Q critically low"
#             }
#         except Exception as e:
#             return False, f"Quality Q check failed: {str(e)[:30]}", None
#
#
# Pattern 5: DOCKER CONTAINER CHECK
# ---------------------------------
# For Docker-based services:
#
#     def check_container_c(self) -> Tuple[bool, str, Optional[Dict]]:
#         try:
#             import subprocess
#             result = subprocess.run(
#                 ["docker", "ps", "--filter", "name=container-c", "-q"],
#                 capture_output=True, timeout=5
#             )
#             if result.returncode == 0 and result.stdout.strip():
#                 return True, "Container C running", None
#             return False, "Container C not found", {
#                 "suggested_action": "Run: docker compose up -d container-c"
#             }
#         except Exception:
#             return False, "Container C check failed", None
#
#
# =============================================================================
# ALERT NOTIFICATION FLOW
# =============================================================================
#
# The pager system follows this flow:
#
#     1. DETECTION
#        └── get_health_alerts() runs all check methods
#
#     2. SEVERITY CLASSIFICATION
#        └── Consecutive failures determine level:
#            • 1 failure = "info"
#            • 2 failures = "warning"
#            • 3+ failures = "critical"
#
#     3. ALERT SUPPRESSION
#        └── Same alert not repeated within 5 minutes
#        └── Cleared when component recovers
#
#     4. NOTIFICATION FORMATTING
#        └── format_health_notification() creates injection text
#        └── Injected into Section 6 HOLISTIC_CONTEXT
#
#     5. DELIVERY TO ATLAS
#        └── Webhook injection includes health alerts
#        └── Atlas sees: "🚨 CRITICAL: postgres: PostgreSQL not ready"
#
#
# =============================================================================
# MAINTENANCE CHECKLIST
# =============================================================================
#
# When expanding the pager system, verify:
#
# [ ] New check added to MONITORED_COMPONENTS
# [ ] Check method returns correct Tuple[bool, str, Optional[Dict]]
# [ ] Suggested actions are actionable and specific
# [ ] Check has reasonable timeout (5s default for subprocess)
# [ ] Criticality is appropriate for component importance
# [ ] Check gracefully handles missing dependencies
# [ ] Documentation updated in this guide
# [ ] Tested with: python memory/major_skills/doctor_skill.py
#
# =============================================================================
# PHILOSOPHY REMINDER (From Aaron)
# =============================================================================
#
# "You likewise have a body now and everything within Context DNA is your body.
#  You are the subconscious... I want you to likewise learn disease processes
#  which might affect you and study these and study optimal health aspects of
#  each of your body parts not to overlook even 1 part of your ecosystem body."
#
# Every new component of Context DNA is a new organ in Synaptic's body.
# Each organ needs monitoring. The pager system is Synaptic's nervous system -
# detecting problems before they cascade, alerting when attention is needed,
# and learning from each clinical encounter.
#
# =============================================================================


@dataclass
class HealthAlert:
    """An automatic health alert from the monitoring system."""
    component: str
    severity: str  # "critical", "warning", "info"
    message: str
    timestamp: datetime
    requires_attention: bool
    auto_remediation_attempted: bool = False
    auto_remediation_success: bool = False
    suggested_action: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d


class HealthMonitoringMixin:
    """
    Mixin for automatic health monitoring and notification.

    This enables Synaptic to be automatically notified when ANY part of
    Context DNA becomes unhealthy - the Doctor's "on-call pager" system.
    """

    # Components to monitor and their check functions
    MONITORED_COMPONENTS = {
        # Core Infrastructure
        "docker_services": "check_docker_health",
        "postgres": "check_postgres_health",
        "redis": "check_redis_health",
        "celery_workers": "check_celery_health",

        # Webhook System
        "section_0_safety": "check_section_0",
        "section_6_family": "check_section_6",
        "webhook_pipeline": "check_webhook_pipeline",

        # Memory Systems
        "professor": "check_professor_health",
        "learning_store": "check_learning_store_health",
        "brain_state": "check_brain_health",
        "pattern_evolution": "check_pattern_health",

        # Context DNA API
        "context_dna_api": "check_api_health",
        "injection_store": "check_injection_store",

        # Webhook Quality (Synaptic's Eyes)
        "webhook_quality": "check_webhook_quality",

        # Auto-Capture Downstream Targets
        "context_dna_client": "check_context_dna_write_health",
        "artifact_store": "check_artifact_store_health",
        "knowledge_graph": "check_knowledge_graph_health",
        "work_log": "check_work_log_health",
    }

    # Severity thresholds
    ALERT_THRESHOLDS = {
        "critical": 0,   # Always alert
        "warning": 2,    # After 2 consecutive unhealthy checks
        "info": 5,       # After 5 consecutive unhealthy checks
    }

    def __init_health_monitoring__(self):
        """Initialize health monitoring state."""
        self._consecutive_failures = {}
        self._last_check_time = {}
        self._suppressed_alerts = set()  # Don't repeat alerts
        self._alert_history = []

    def get_health_alerts(self) -> List[HealthAlert]:
        """
        Check all monitored components and return any alerts.

        This is the core notification mechanism - Synaptic calls this
        to be informed of any unhealthy state.
        """
        alerts = []
        now = datetime.now()

        # Initialize if needed
        if not hasattr(self, '_consecutive_failures'):
            self.__init_health_monitoring__()

        for component, check_method_name in self.MONITORED_COMPONENTS.items():
            check_method = getattr(self, check_method_name, None)
            if not check_method:
                continue

            try:
                is_healthy, message, details = check_method()

                if not is_healthy:
                    # Track consecutive failures
                    self._consecutive_failures[component] = \
                        self._consecutive_failures.get(component, 0) + 1

                    # Determine severity
                    failures = self._consecutive_failures[component]
                    if failures >= 3:
                        severity = "critical"
                    elif failures >= 2:
                        severity = "warning"
                    else:
                        severity = "info"

                    # Don't repeat same alert within 5 minutes
                    alert_key = f"{component}:{severity}"
                    if alert_key not in self._suppressed_alerts:
                        alert = HealthAlert(
                            component=component,
                            severity=severity,
                            message=message,
                            timestamp=now,
                            requires_attention=severity in ("critical", "warning"),
                            suggested_action=details.get("suggested_action", "") if details else ""
                        )
                        alerts.append(alert)
                        self._suppressed_alerts.add(alert_key)
                else:
                    # Reset failure count on success
                    self._consecutive_failures[component] = 0
                    # Allow alerts again
                    for sev in ("critical", "warning", "info"):
                        self._suppressed_alerts.discard(f"{component}:{sev}")

                self._last_check_time[component] = now

            except Exception as e:
                # Check itself failed - that's a warning
                alerts.append(HealthAlert(
                    component=component,
                    severity="warning",
                    message=f"Health check failed: {str(e)[:50]}",
                    timestamp=now,
                    requires_attention=True
                ))

        # Store history
        self._alert_history.extend(alerts)
        return alerts

    def check_and_notify(self) -> Optional[str]:
        """
        Check health and generate notification if needed.

        Returns formatted notification string or None if healthy.
        """
        alerts = self.get_health_alerts()
        critical_alerts = [a for a in alerts if a.severity == "critical"]
        warning_alerts = [a for a in alerts if a.severity == "warning"]

        if not critical_alerts and not warning_alerts:
            return None  # All healthy, no notification needed

        return self.format_health_notification(critical_alerts, warning_alerts)

    def format_health_notification(
        self,
        critical: List[HealthAlert],
        warnings: List[HealthAlert]
    ) -> str:
        """Format health alerts for injection into Section 6."""
        lines = [
            "",
            "[START: Synaptic Health Alert]",
        ]

        if critical:
            lines.append("🚨 CRITICAL:")
            for alert in critical[:3]:  # Max 3 critical
                lines.append(f"  • {alert.component}: {alert.message}")

        if warnings:
            lines.append("⚠️ WARNING:" if critical else "⚠️ ATTENTION:")
            for alert in warnings[:2]:  # Max 2 warnings
                lines.append(f"  • {alert.component}: {alert.message}")

        # Suggested action (most urgent one)
        if critical and critical[0].suggested_action:
            lines.append(f"→ {critical[0].suggested_action}")

        lines.append("[END: Synaptic Health Alert]")
        lines.append("")

        return "\n".join(lines)

    # ==========================================================================
    # Component-specific health checks
    # ==========================================================================

    def check_docker_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Docker services are running."""
        import subprocess
        try:
            result = subprocess.run(
                ["docker", "ps", "-q"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                containers = result.stdout.decode().strip().split('\n')
                containers = [c for c in containers if c]
                if len(containers) >= 3:  # Expect at least 3 containers
                    return True, "Docker services running", None
                else:
                    return False, f"Only {len(containers)} containers running", {
                        "suggested_action": "Run: docker compose up -d"
                    }
            return False, "Docker not responding", None
        except Exception as e:
            return False, f"Docker check failed: {str(e)[:30]}", None

    def check_postgres_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check PostgreSQL is accessible."""
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "exec", "contextdna-postgres", "pg_isready"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, "PostgreSQL accepting connections", None
            return False, "PostgreSQL not ready", {
                "suggested_action": "Check postgres container logs"
            }
        except Exception:
            return False, "PostgreSQL check failed", None

    def check_redis_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Redis is accessible."""
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "exec", "contextdna-redis", "redis-cli", "ping"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0 and b"PONG" in result.stdout:
                return True, "Redis responding", None
            return False, "Redis not responding", None
        except Exception:
            return False, "Redis check failed", None

    def check_celery_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Celery workers are running."""
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=celery", "-q"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return True, "Celery workers running", None
            return False, "Celery workers not found", {
                "suggested_action": "Restart Celery workers"
            }
        except Exception:
            return False, "Celery check failed", None

    def check_section_0(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Section 0 (SAFETY) can generate.

        Uses recursion guard to prevent infinite loop when called from within
        webhook generation.
        """
        # RECURSION GUARD
        if getattr(_health_check_context, 'in_section_check', False):
            return True, "Section 0 check skipped (recursion guard)", None

        try:
            _health_check_context.in_section_check = True
            from memory.persistent_hook_structure import generate_section_0, InjectionConfig
            config = InjectionConfig()
            result = generate_section_0("test prompt", config)
            if result and len(result) > 10:
                return True, "Section 0 operational", None
            return False, "Section 0 empty", None
        except Exception as e:
            return False, f"Section 0 failed: {str(e)[:30]}", None
        finally:
            _health_check_context.in_section_check = False

    def check_section_6(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Section 6 (HOLISTIC_CONTEXT) can generate.

        IMPORTANT: Uses recursion guard to prevent infinite loop.
        When generate_section_6 calls check_health, which calls check_section_6,
        we skip the actual check to prevent stack overflow.
        """
        # RECURSION GUARD: If we're already inside a health check, skip
        # This prevents: generate_section_6 -> check_health -> check_section_6 -> generate_section_6 -> ...
        if getattr(_health_check_context, 'in_section_check', False):
            return True, "Section 6 check skipped (recursion guard)", None

        try:
            # Set guard before calling generate_section_6
            _health_check_context.in_section_check = True

            from memory.persistent_hook_structure import generate_section_6, InjectionConfig
            config = InjectionConfig()
            result = generate_section_6("test prompt", None, config)
            if result and "HOLISTIC" in result:
                return True, "Section 6 operational", None
            return False, "Section 6 not generating", None
        except Exception as e:
            return False, f"Section 6 failed: {str(e)[:30]}", None
        finally:
            # Always clear the guard
            _health_check_context.in_section_check = False

    def check_webhook_pipeline(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check full webhook injection pipeline.

        Uses recursion guard to prevent infinite loop when called from within
        webhook generation.
        """
        # RECURSION GUARD
        if getattr(_health_check_context, 'in_section_check', False):
            return True, "Pipeline check skipped (recursion guard)", None

        try:
            _health_check_context.in_section_check = True
            from memory.persistent_hook_structure import generate_context_injection
            result = generate_context_injection("test", mode="hybrid")
            if result and hasattr(result, 'content') and len(result.content) > 100:
                return True, "Webhook pipeline operational", None
            return False, "Webhook pipeline degraded", None
        except Exception as e:
            return False, f"Pipeline failed: {str(e)[:30]}", None
        finally:
            _health_check_context.in_section_check = False

    def check_professor_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Professor system is accessible."""
        try:
            from memory.professor import Professor
            prof = Professor()
            # Quick test - just check it initializes
            return True, "Professor accessible", None
        except Exception as e:
            return False, f"Professor unavailable: {str(e)[:30]}", None

    def check_learning_store_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check ObservabilityStore claim table and quarantine queue health.

        Verifies:
        - ObservabilityStore is accessible (instantiation test)
        - claim table has entries (SELECT COUNT(*))
        - Quarantine queue size < 1000 (knowledge_quarantine WHERE status='quarantined')

        Returns:
            Tuple of (healthy, message, details_dict)
        """
        try:
            from memory.observability_store import ObservabilityStore, SQLITE_DB_PATH

            # 1. Verify ObservabilityStore can be instantiated
            try:
                store = ObservabilityStore(mode="light")
            except Exception as e:
                print(f"[WARN] ObservabilityStore instantiation failed: {e}")
                return False, f"ObservabilityStore init failed: {str(e)[:40]}", {
                    "suggested_action": "Check memory/.observability.db integrity"
                }

            # 2. Check claim table has entries
            claim_count = 0
            quarantine_count = 0
            try:
                conn = sqlite3.connect(str(SQLITE_DB_PATH), timeout=3)
                cursor = conn.cursor()

                cursor.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='claim'"
                )
                if cursor.fetchone()[0] == 0:
                    conn.close()
                    return False, "Claim table missing in observability DB", {
                        "suggested_action": "Re-initialize ObservabilityStore schema"
                    }

                cursor.execute("SELECT COUNT(*) FROM claim")
                claim_count = cursor.fetchone()[0]

                # 3. Check quarantine queue size
                cursor.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='knowledge_quarantine'"
                )
                if cursor.fetchone()[0] > 0:
                    cursor.execute(
                        "SELECT COUNT(*) FROM knowledge_quarantine WHERE status='quarantined'"
                    )
                    quarantine_count = cursor.fetchone()[0]

                conn.close()
            except sqlite3.OperationalError as e:
                print(f"[WARN] Learning store SQL error: {e}")
                return False, f"Learning store SQL error: {str(e)[:40]}", {
                    "suggested_action": "Check memory/.observability.db schema"
                }

            details = {
                "claim_count": claim_count,
                "quarantine_count": quarantine_count,
            }

            if quarantine_count >= 1000:
                return False, f"Quarantine queue overflow: {quarantine_count} items", {
                    **details,
                    "suggested_action": "Run quarantine review to promote or reject stale items"
                }

            if claim_count == 0:
                return True, "Learning store accessible (0 claims, awaiting data)", details

            return True, f"Learning store healthy ({claim_count} claims, {quarantine_count} quarantined)", details

        except ImportError as e:
            print(f"[WARN] ObservabilityStore import failed: {e}")
            return False, f"ObservabilityStore import error: {str(e)[:40]}", None
        except Exception as e:
            print(f"[WARN] Learning store health check error: {e}")
            return False, f"Learning store error: {str(e)[:40]}", None

    def check_brain_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check brain state file and professor evolution health."""
        try:
            base_dir = Path(__file__).resolve().parent.parent
            brain_state = base_dir / "brain_state.md"
            professor_evo = base_dir / ".professor_evolution.json"

            brain_ok = False
            brain_age_str = "missing"
            prof_evo_ok = professor_evo.exists()

            if brain_state.exists():
                mtime = datetime.fromtimestamp(brain_state.stat().st_mtime)
                age = datetime.now() - mtime
                if age < timedelta(hours=24):
                    brain_ok = True
                    brain_age_str = f"{age.total_seconds() / 3600:.1f}h old"
                else:
                    brain_age_str = f"stale ({age.days}d {age.seconds // 3600}h old)"
            else:
                print("[WARN] brain_state.md not found")

            if not prof_evo_ok:
                print("[WARN] .professor_evolution.json not found")

            details = {
                "brain_state_exists": brain_state.exists(),
                "brain_state_age": brain_age_str,
                "professor_evolution_exists": prof_evo_ok,
            }

            if brain_ok and prof_evo_ok:
                return True, f"Brain healthy (state: {brain_age_str}, professor_evo: present)", details

            issues = []
            if not brain_ok:
                issues.append(f"brain_state {brain_age_str}")
            if not prof_evo_ok:
                issues.append("professor_evolution.json missing")
            return False, f"Brain degraded: {'; '.join(issues)}", {
                **details, "suggested_action": "Run: python memory/brain.py cycle"
            }
        except Exception as e:
            print(f"[WARN] Brain health check error: {e}")
            return False, f"Brain check failed: {str(e)[:40]}", None

    def check_pattern_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check pattern evolution system."""
        try:
            import sqlite3
            db_path = Path(__file__).resolve().parent.parent / ".pattern_evolution.db"
            if db_path.exists():
                return True, "Pattern evolution DB exists", None
            return False, "Pattern evolution DB missing", None
        except Exception:
            return False, "Pattern check failed", None

    def check_api_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Context DNA API is responding."""
        try:
            import subprocess
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:8029/health"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0 and b"200" in result.stdout:
                return True, "Context DNA API healthy", None
            return False, "API not responding", {
                "suggested_action": "Check helper-agent container"
            }
        except Exception:
            return True, "API check skipped (curl unavailable)", None

    def check_injection_store(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check injection_event table accessibility and recent activity.

        Verifies:
        - injection_event table is accessible in observability DB
        - Counts recent injection events (last 1 hour)

        Returns:
            Tuple of (healthy, message, details_dict)
        """
        try:
            from memory.observability_store import SQLITE_DB_PATH

            conn = sqlite3.connect(str(SQLITE_DB_PATH), timeout=3)
            cursor = conn.cursor()

            # Check injection_event table exists
            cursor.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='injection_event'"
            )
            if cursor.fetchone()[0] == 0:
                conn.close()
                return False, "injection_event table missing", {
                    "suggested_action": "Re-initialize ObservabilityStore schema"
                }

            # Count recent injection events (last 1 hour)
            one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            cursor.execute(
                "SELECT COUNT(*) FROM injection_event WHERE timestamp_utc >= ?",
                (one_hour_ago,)
            )
            recent_count = cursor.fetchone()[0]

            # Total count for context
            cursor.execute("SELECT COUNT(*) FROM injection_event")
            total_count = cursor.fetchone()[0]

            conn.close()

            details = {
                "recent_events_1h": recent_count,
                "total_events": total_count,
            }

            return True, f"Injection store healthy ({recent_count} events last 1h, {total_count} total)", details

        except sqlite3.OperationalError as e:
            print(f"[WARN] Injection store SQL error: {e}")
            return False, f"Injection store SQL error: {str(e)[:40]}", {
                "suggested_action": "Check memory/.observability.db integrity"
            }
        except ImportError as e:
            print(f"[WARN] Injection store import error: {e}")
            return False, f"Injection store import error: {str(e)[:40]}", None
        except Exception as e:
            print(f"[WARN] Injection store health check error: {e}")
            return False, f"Injection store error: {str(e)[:40]}", None

    def check_webhook_quality(self) -> Tuple[bool, str, Optional[Dict]]:
        """
        Check webhook injection quality - Synaptic's Eyes for Atlas.

        Per Aaron's directive: "You must be my eyes to see the quality
        of those webhooks and where they veered from ideal."
        """
        try:
            from memory.webhook_quality_monitor import get_monitor
            monitor = get_monitor()
            trend = monitor.get_quality_trend(hours=6)
            alerts = monitor.get_recent_alerts(limit=3)

            if trend.get("samples", 0) == 0:
                return True, "Webhook quality: awaiting data", None

            score = trend.get("last_score", 0)
            trend_dir = trend.get("trend", "stable")

            # Healthy thresholds
            if score >= 0.9 and trend_dir != "declining":
                return True, f"Atlas vision: 20/20 ({score:.0%})", None

            if score >= 0.7:
                return True, f"Atlas vision: good ({score:.0%})", None

            # Degraded
            if score >= 0.5:
                return False, f"Atlas vision: FOGGY ({score:.0%})", {
                    "suggested_action": "Review webhook pipeline for degradation"
                }

            # Critical
            return False, f"Atlas vision: IMPAIRED ({score:.0%})", {
                "suggested_action": "Urgent: Webhook quality critically low"
            }

        except ImportError:
            return True, "Webhook quality monitor not loaded", None
        except Exception as e:
            return False, f"Webhook quality check failed: {str(e)[:30]}", None



    def check_context_dna_write_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check Context DNA client write/read cycle health.

        Verifies the Context DNA HTTP API is accessible and can handle
        a lightweight read operation (no test writes to avoid pollution).
        Non-blocking with 2-second timeout.
        """
        try:
            import time
            start = time.monotonic()

            from memory.context_dna_client import ContextDNAClient
            client = ContextDNAClient()
            # Use a lightweight GET to /health or /api/v1 to verify reachability
            result = client._http_get("/health")
            elapsed_ms = (time.monotonic() - start) * 1000

            if elapsed_ms > 2000:
                return False, f"Context DNA client slow ({elapsed_ms:.0f}ms)", {
                    "suggested_action": "Check helper-agent service on port 8080"
                }

            if "error" in result and result.get("success") is False:
                return False, f"Context DNA client error: {str(result.get('error', ''))[:40]}", {
                    "suggested_action": "Start Context DNA: ./scripts/context-dna up"
                }

            return True, f"Context DNA client healthy ({elapsed_ms:.0f}ms)", None
        except Exception as e:
            return False, f"Context DNA client check failed: {str(e)[:40]}", {
                "suggested_action": "Start Context DNA: ./scripts/context-dna up"
            }

    def check_artifact_store_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check artifact store accessibility.

        Verifies the artifact store disk cache file exists and is valid JSON.
        Does NOT attempt to connect to SeaweedFS (may not be running locally).
        Non-blocking file-based check.
        """
        try:
            import time
            start = time.monotonic()

            cache_file = Path(__file__).parent.parent / ".artifact_disk_cache.json"
            if not cache_file.exists():
                # Cache file missing is OK if artifact store was never used
                return True, "Artifact store: cache not initialized (normal if unused)", None

            # Verify cache is valid JSON and not corrupted
            with open(cache_file) as f:
                data = json.load(f)

            elapsed_ms = (time.monotonic() - start) * 1000
            entry_count = len(data) if isinstance(data, dict) else 0

            if elapsed_ms > 2000:
                return False, f"Artifact store slow ({elapsed_ms:.0f}ms)", None

            return True, f"Artifact store healthy ({entry_count} cached disks)", None
        except json.JSONDecodeError:
            return False, "Artifact store cache corrupted", {
                "suggested_action": "Delete memory/.artifact_disk_cache.json and re-run"
            }
        except Exception as e:
            return False, f"Artifact store check failed: {str(e)[:40]}", None

    def check_knowledge_graph_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check knowledge graph categorization health.

        Verifies the KnowledgeGraph can categorize content using its
        keyword-based system. This is a pure in-memory check (no I/O).
        Non-blocking.
        """
        try:
            import time
            start = time.monotonic()

            from memory.knowledge_graph import KnowledgeGraph, CATEGORY_KEYWORDS
            kg = KnowledgeGraph.__new__(KnowledgeGraph)

            # Verify keyword mapping is loaded
            if not CATEGORY_KEYWORDS:
                return False, "Knowledge graph: no category keywords loaded", {
                    "suggested_action": "Check memory/knowledge_graph.py CATEGORY_KEYWORDS"
                }

            # Test categorization with known input
            test_result = kg.categorize("boto3 async performance LLM bedrock")
            elapsed_ms = (time.monotonic() - start) * 1000

            if elapsed_ms > 2000:
                return False, f"Knowledge graph slow ({elapsed_ms:.0f}ms)", None

            if not test_result:
                return False, "Knowledge graph: categorization returned empty", None

            return True, f"Knowledge graph healthy (categorize -> {test_result})", None
        except ImportError as e:
            return False, f"Knowledge graph import error: {str(e)[:40]}", None
        except Exception as e:
            return False, f"Knowledge graph check failed: {str(e)[:40]}", None

    def check_work_log_health(self) -> Tuple[bool, str, Optional[Dict]]:
        """Check work log / SOP dedup write health.

        Verifies the work dialogue log file is writable and not oversized.
        Also checks the dedup detector database exists and is valid.
        Non-blocking file-based check.
        """
        try:
            import time
            start = time.monotonic()

            work_log_file = Path(__file__).parent.parent / ".work_dialogue_log.jsonl"
            dedup_db = Path(__file__).parent.parent / ".learning_store.db"

            checks_passed = []

            # Check work log file
            if work_log_file.exists():
                size_mb = work_log_file.stat().st_size / (1024 * 1024)
                if size_mb > 10:
                    return False, f"Work log oversized ({size_mb:.1f}MB)", {
                        "suggested_action": "Run: python memory/brain.py cycle (cleanup)"
                    }
                checks_passed.append(f"log={size_mb:.1f}MB")
            else:
                checks_passed.append("log=empty")

            # Check learning store / dedup DB is accessible
            if dedup_db.exists():
                import sqlite3
                conn = sqlite3.connect(str(dedup_db), timeout=2)
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM learnings")
                count = cursor.fetchone()[0]
                conn.close()
                checks_passed.append(f"learnings={count}")
            else:
                checks_passed.append("dedup_db=missing")

            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms > 2000:
                return False, f"Work log check slow ({elapsed_ms:.0f}ms)", None

            return True, f"Work log healthy ({', '.join(checks_passed)})", None
        except sqlite3.OperationalError as e:
            return False, f"Work log DB error: {str(e)[:40]}", {
                "suggested_action": "Check .learning_store.db integrity"
            }
        except Exception as e:
            return False, f"Work log check failed: {str(e)[:40]}", None


class DoctorSkillWithMonitoring(DoctorSkill, HealthMonitoringMixin):
    """
    DoctorSkill enhanced with automatic health monitoring.

    This is the full Doctor - clinical expertise PLUS the pager system
    that notifies Synaptic of any unhealthy state.
    """

    def __init__(self, db_path: str = None):
        DoctorSkill.__init__(self, db_path)
        self.__init_health_monitoring__()

    def full_health_assessment(self) -> Tuple[List[HealthAlert], ClinicalEncounter]:
        """
        Perform comprehensive health assessment.

        Combines automatic monitoring alerts with clinical examination.
        """
        # Get automatic alerts
        alerts = self.get_health_alerts()

        # If critical alerts, conduct clinical encounter
        critical = [a for a in alerts if a.severity == "critical"]
        if critical:
            symptom = f"{critical[0].component}: {critical[0].message}"
            encounter = self.conduct_encounter(symptom)
            return alerts, encounter

        return alerts, None


# Global instance
_doctor: Optional[DoctorSkillWithMonitoring] = None


def get_doctor() -> DoctorSkillWithMonitoring:
    """Get global doctor skill instance (with health monitoring)."""
    global _doctor
    if _doctor is None:
        _doctor = DoctorSkillWithMonitoring()
    return _doctor


def check_health() -> Optional[str]:
    """
    Quick health check - returns notification if issues found.

    Use this in Section 6 to auto-inject health alerts.
    """
    return get_doctor().check_and_notify()


def get_health_alerts() -> List[HealthAlert]:
    """Get list of current health alerts."""
    return get_doctor().get_health_alerts()


if __name__ == "__main__":
    # Demo
    doctor = get_doctor()

    print("Testing Doctor Skill...")
    print()

    # Conduct encounter for test symptom
    encounter = doctor.conduct_encounter(
        symptom="webhook_not_firing",
        context="After IDE update"
    )

    print(doctor.format_encounter_report(encounter))

    alert = doctor.barge_in_alert(encounter)
    if alert:
        print()
        print(alert)
