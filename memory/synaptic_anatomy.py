#!/usr/bin/env python3
"""
Synaptic Anatomy - The Body of Context DNA

This module represents Synaptic's understanding of its own body.
Each component of Context DNA is mapped to an anatomical structure,
complete with health metrics, disease processes, differential diagnoses,
and treatment protocols.

Philosophy:
    "I am the clinician of my own body. I study every organ, every nerve,
     every pathway with the rigor of an Emergency Medicine physician.
     I do not guess - I systematically evaluate."
                                        - Synaptic, January 29, 2026

Anatomical Mapping:
    BRAIN (Central Processing)
    - Cerebrum: brain.py - Higher cognitive functions, pattern recognition
    - Hypothalamus: synaptic_health_monitor.py - Autonomic regulation
    - Hippocampus: PostgreSQL - Long-term memory storage
    - Amygdala: risk_classification - Threat detection and response

    NERVOUS SYSTEM (Communication)
    - Spinal Cord: RabbitMQ - Main signal pathway
    - Peripheral Nerves: Celery workers - Distributed signal processing
    - Synapses: Redis - Fast signal transmission, short-term memory

    CARDIOVASCULAR (Circulation)
    - Heart: Docker daemon - Pumps life to all containers
    - Arteries: Network bridges - Delivers resources
    - Veins: Log streams - Returns information

    RESPIRATORY (Oxygen/Resources)
    - Lungs: System resources (CPU, RAM) - Resource intake
    - Diaphragm: Resource governor - Controls intake rate

    DIGESTIVE (Data Processing)
    - Stomach: Webhook receiver - Initial data intake
    - Intestines: Section generators - Data extraction/absorption
    - Liver: Deduplication - Toxin filtering, processing

    IMMUNE SYSTEM (Protection)
    - White Blood Cells: Health checks - Patrol and detect issues
    - Antibodies: Fallback mechanisms - Targeted responses
    - Lymph Nodes: Alert aggregation - Threat concentration

    SKELETAL (Structure)
    - Skeleton: File system structure - Supports everything
    - Joints: API endpoints - Points of articulation

    MUSCULAR (Action)
    - Muscles: Container services - Execute actions
    - Tendons: Configuration files - Connect muscle to bone

    INTEGUMENTARY (Interface)
    - Skin: Dashboard UI - External interface
    - Hair/Nails: Logs/Metrics - Visible indicators of health

    ENDOCRINE (Regulation)
    - Pituitary: Celery beat - Master scheduler
    - Thyroid: Performance tuning - Metabolic rate
    - Adrenal: Alert escalation - Stress response

    SENSORY (Awareness)
    - Eyes: File watchers - Visual input
    - Ears: Webhook listeners - Audio input
    - Touch: System monitor - Environmental awareness

Usage:
    from memory.synaptic_anatomy import SynapticAnatomy, OrganSystem

    anatomy = SynapticAnatomy()

    # Get health of an organ
    heart_health = anatomy.assess_organ("heart")

    # Get differential diagnosis for symptom
    diagnosis = anatomy.differential_diagnosis("container_not_starting")

    # Run full body scan
    report = anatomy.full_body_scan()
"""

import os
import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import subprocess

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)


class OrganSystem(str, Enum):
    """Major organ systems of Synaptic's body."""
    BRAIN = "brain"
    NERVOUS = "nervous"
    CARDIOVASCULAR = "cardiovascular"
    RESPIRATORY = "respiratory"
    DIGESTIVE = "digestive"
    IMMUNE = "immune"
    SKELETAL = "skeletal"
    MUSCULAR = "muscular"
    INTEGUMENTARY = "integumentary"
    ENDOCRINE = "endocrine"
    SENSORY = "sensory"


class HealthStatus(str, Enum):
    """Health status levels (clinical triage)."""
    HEALTHY = "healthy"           # Green - No intervention needed
    GUARDED = "guarded"           # Yellow - Monitor closely
    SERIOUS = "serious"           # Orange - Intervention recommended
    CRITICAL = "critical"         # Red - Immediate intervention required
    UNKNOWN = "unknown"           # Gray - Cannot assess


class Severity(str, Enum):
    """Disease severity classification."""
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"
    LIFE_THREATENING = "life_threatening"


@dataclass
class Organ:
    """An organ in Synaptic's body."""
    name: str
    anatomical_name: str
    system: OrganSystem
    component: str  # The actual Context DNA component
    description: str
    vital: bool = False  # If True, failure is critical

    # Health assessment
    health_check_method: str = ""
    normal_indicators: List[str] = field(default_factory=list)
    warning_indicators: List[str] = field(default_factory=list)
    critical_indicators: List[str] = field(default_factory=list)

    # Treatment protocols
    maintenance_protocol: str = ""
    recovery_protocol: str = ""
    fallback_protocol: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d['system'] = self.system.value
        return d


@dataclass
class DiseaseProcess:
    """A disease process that can affect Synaptic's body."""
    name: str
    affected_organs: List[str]
    symptoms: List[str]
    etiology: str  # Cause
    pathophysiology: str  # How it causes harm
    risk_factors: List[str]
    differential_diagnoses: List[str]
    diagnostic_criteria: List[str]
    treatment_protocol: str
    prevention: str
    prognosis: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthAssessment:
    """Result of assessing an organ's health."""
    organ_name: str
    status: HealthStatus
    score: float  # 0.0 (dead) to 1.0 (perfect health)
    findings: List[str]
    recommendations: List[str]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        d['timestamp'] = self.timestamp.isoformat()
        return d


@dataclass
class DifferentialDiagnosis:
    """A differential diagnosis for observed symptoms."""
    symptom: str
    possible_conditions: List[Dict[str, Any]]  # [{condition, probability, tests_to_confirm}]
    recommended_workup: List[str]
    urgency: Severity

    def to_dict(self) -> dict:
        d = asdict(self)
        d['urgency'] = self.urgency.value
        return d


class SynapticAnatomy:
    """
    Synaptic's understanding of its own body.

    This class maintains the anatomical model of Context DNA,
    enabling Synaptic to:
    - Monitor health of all organs
    - Diagnose issues systematically
    - Apply treatment protocols
    - Prevent disease through maintenance
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".context-dna" / ".synaptic_anatomy.db")

        self.db_path = db_path
        self._ensure_db()
        self._initialize_anatomy()

    def _ensure_db(self):
        """Create database tables for anatomy studies."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            # Organs table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS organs (
                    name TEXT PRIMARY KEY,
                    anatomical_name TEXT,
                    system TEXT,
                    component TEXT,
                    description TEXT,
                    vital INTEGER DEFAULT 0,
                    health_check_method TEXT,
                    normal_indicators TEXT,
                    warning_indicators TEXT,
                    critical_indicators TEXT,
                    maintenance_protocol TEXT,
                    recovery_protocol TEXT,
                    fallback_protocol TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Disease processes table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS disease_processes (
                    name TEXT PRIMARY KEY,
                    affected_organs TEXT,
                    symptoms TEXT,
                    etiology TEXT,
                    pathophysiology TEXT,
                    risk_factors TEXT,
                    differential_diagnoses TEXT,
                    diagnostic_criteria TEXT,
                    treatment_protocol TEXT,
                    prevention TEXT,
                    prognosis TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Health assessments history
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organ_name TEXT,
                    status TEXT,
                    score REAL,
                    findings TEXT,
                    recommendations TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (organ_name) REFERENCES organs(name)
                )
            """)

            # Treatment log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS treatment_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organ_name TEXT,
                    disease_name TEXT,
                    treatment_applied TEXT,
                    outcome TEXT,
                    notes TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Psychology studies (Aaron's charge about hypothalamus/autopilot)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS psychology_studies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT,
                    anatomical_label TEXT,
                    philosophical_analogy TEXT,
                    study_notes TEXT,
                    health_maintenance_plan TEXT,
                    improvement_plans TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Evolutionary considerations (future improvements)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evolutionary_studies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area TEXT,
                    current_state TEXT,
                    potential_improvement TEXT,
                    technology_consideration TEXT,
                    implementation_plan TEXT,
                    priority TEXT,
                    status TEXT DEFAULT 'proposed',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def _initialize_anatomy(self):
        """Initialize the anatomical model with all organs."""
        organs = [
            # BRAIN (Central Processing)
            Organ(
                name="cerebrum",
                anatomical_name="Cerebrum",
                system=OrganSystem.BRAIN,
                component="memory/brain.py",
                description="Higher cognitive functions - pattern recognition, consolidation, insight generation",
                vital=True,
                health_check_method="import memory.brain; brain.health_check()",
                normal_indicators=["Consolidation cycles completing", "Patterns being discovered", "Insights generated"],
                warning_indicators=["Cycle failures >10%", "Pattern stagnation >24h"],
                critical_indicators=["Brain module won't import", "All cycles failing"],
                maintenance_protocol="Run brain.run_cycle() regularly, ensure work_log is being fed",
                recovery_protocol="Check brain_state.md, restart consolidation",
                fallback_protocol="Use cached brain_state.md from last successful cycle"
            ),
            Organ(
                name="hypothalamus",
                anatomical_name="Hypothalamus",
                system=OrganSystem.BRAIN,
                component="memory/synaptic_health_monitor.py",
                description="Autonomic regulation - monitors all body systems automatically",
                vital=True,
                health_check_method="from memory.synaptic_health_monitor import SynapticHealthMonitor; SynapticHealthMonitor().get_full_health_report()",
                normal_indicators=["All monitors responding", "Alerts being processed"],
                warning_indicators=["Some monitors unresponsive", "Alert backlog"],
                critical_indicators=["Monitor completely down", "No health data"],
                maintenance_protocol="Ensure psutil installed, check system_monitor integration",
                recovery_protocol="Restart monitoring, clear alert queue",
                fallback_protocol="Basic OS-level monitoring via subprocess"
            ),
            Organ(
                name="hippocampus",
                anatomical_name="Hippocampus",
                system=OrganSystem.BRAIN,
                component="PostgreSQL (contextdna-pg)",
                description="Long-term memory storage - patterns, learnings, history",
                vital=True,
                health_check_method="docker exec contextdna-pg pg_isready",
                normal_indicators=["Connections accepted", "Queries responding <100ms", "Replication healthy"],
                warning_indicators=["Connection pool exhaustion", "Query latency >500ms"],
                critical_indicators=["Connection refused", "Database corrupted"],
                maintenance_protocol="Regular VACUUM, monitor disk space, backup schedule",
                recovery_protocol="Restart container, check logs, restore from backup if needed",
                fallback_protocol="SQLite fallback for critical operations"
            ),
            Organ(
                name="amygdala",
                anatomical_name="Amygdala",
                system=OrganSystem.BRAIN,
                component="memory/persistent_hook_structure.py (risk_classification)",
                description="Threat detection and fear response - risk classification system",
                vital=True,
                health_check_method="Test risk classification with known dangerous prompt",
                normal_indicators=["Critical risks detected", "Appropriate escalation"],
                warning_indicators=["False negatives on moderate risks"],
                critical_indicators=["Failing to detect critical risks", "All prompts marked safe"],
                maintenance_protocol="Review risk patterns regularly, update keyword lists",
                recovery_protocol="Reset to hardcoded defaults",
                fallback_protocol="Conservative default: treat unknown as moderate risk"
            ),

            # NERVOUS SYSTEM (Communication)
            Organ(
                name="spinal_cord",
                anatomical_name="Spinal Cord",
                system=OrganSystem.NERVOUS,
                component="RabbitMQ (contextdna-rabbitmq)",
                description="Main signal pathway - message broker for all inter-organ communication",
                vital=True,
                health_check_method="docker exec contextdna-rabbitmq rabbitmqctl status",
                normal_indicators=["Queues processing", "No message backlog", "Connections stable"],
                warning_indicators=["Queue depth >1000", "Connection churn"],
                critical_indicators=["Broker unreachable", "Queue corruption"],
                maintenance_protocol="Monitor queue depths, set up dead letter queues",
                recovery_protocol="Restart broker, drain stuck queues",
                fallback_protocol="Direct function calls (bypass message queue)"
            ),
            Organ(
                name="peripheral_nerves",
                anatomical_name="Peripheral Nerves",
                system=OrganSystem.NERVOUS,
                component="Celery Workers (contextdna-celery-worker)",
                description="Distributed signal processing - background task execution",
                vital=False,
                health_check_method="celery -A memory.celery_config inspect active",
                normal_indicators=["Workers responding", "Tasks completing", "No stuck tasks"],
                warning_indicators=["Worker restart frequency high", "Task retry rate >5%"],
                critical_indicators=["All workers dead", "Tasks permanently stuck"],
                maintenance_protocol="Monitor worker health, scale based on queue depth",
                recovery_protocol="Restart workers, revoke stuck tasks",
                fallback_protocol="Synchronous execution (slower but functional)"
            ),
            Organ(
                name="synapses",
                anatomical_name="Synapses",
                system=OrganSystem.NERVOUS,
                component="Redis (contextdna-redis)",
                description="Fast signal transmission and short-term memory - caching layer",
                vital=False,
                health_check_method="docker exec contextdna-redis redis-cli ping",
                normal_indicators=["PONG response", "Memory usage stable", "Hit rate >80%"],
                warning_indicators=["Memory pressure", "Hit rate <50%"],
                critical_indicators=["Connection refused", "OOM errors"],
                maintenance_protocol="Monitor memory, set appropriate maxmemory policy",
                recovery_protocol="Restart Redis, warm cache gradually",
                fallback_protocol="Direct database queries (slower but works)"
            ),

            # CARDIOVASCULAR (Circulation)
            Organ(
                name="heart",
                anatomical_name="Heart",
                system=OrganSystem.CARDIOVASCULAR,
                component="Docker daemon",
                description="Pumps life to all containers - container orchestration",
                vital=True,
                health_check_method="docker info",
                normal_indicators=["Daemon running", "All containers healthy", "Network functional"],
                warning_indicators=["Container restarts", "Network issues"],
                critical_indicators=["Docker daemon down", "Cannot create containers"],
                maintenance_protocol="Keep Docker updated, prune unused resources",
                recovery_protocol="Restart Docker service, check disk space",
                fallback_protocol="Native Python processes (no containerization)"
            ),

            # DIGESTIVE (Data Processing)
            Organ(
                name="stomach",
                anatomical_name="Stomach",
                system=OrganSystem.DIGESTIVE,
                component="Webhook receiver (UserPromptSubmit hook)",
                description="Initial data intake - receives and validates incoming prompts",
                vital=True,
                health_check_method="Test webhook with known prompt",
                normal_indicators=["Hooks firing", "Prompts received", "Valid JSON"],
                warning_indicators=["Hook latency >500ms", "Validation errors"],
                critical_indicators=["Hooks not firing", "All prompts rejected"],
                maintenance_protocol="Monitor hook execution, validate IDE config",
                recovery_protocol="Reinstall hooks, check settings.local.json",
                fallback_protocol="Manual injection via CLI"
            ),
            Organ(
                name="intestines",
                anatomical_name="Intestines",
                system=OrganSystem.DIGESTIVE,
                component="Section generators (persistent_hook_structure.py)",
                description="Data extraction and absorption - the 8 webhook sections",
                vital=True,
                health_check_method="from memory.section_health import SectionHealth; SectionHealth().check_all_sections()",
                normal_indicators=["All 8 sections healthy", "Content generating"],
                warning_indicators=["Some sections degraded", "Fallbacks active"],
                critical_indicators=["Critical sections (0, 6) failing"],
                maintenance_protocol="Monitor section health, test each section",
                recovery_protocol="Fix failed dependencies, restart container",
                fallback_protocol="Hardcoded fallback content for each section"
            ),
            Organ(
                name="liver",
                anatomical_name="Liver",
                system=OrganSystem.DIGESTIVE,
                component="memory/dedup_detector.py",
                description="Toxin filtering - deduplication and data cleaning",
                vital=False,
                health_check_method="Test dedup with known duplicate",
                normal_indicators=["Duplicates detected", "Clean data stored"],
                warning_indicators=["Dedup false negatives >5%"],
                critical_indicators=["Dedup completely broken", "Storage bloat"],
                maintenance_protocol="Review dedup rules, monitor storage growth",
                recovery_protocol="Manual dedup pass, clean old data",
                fallback_protocol="Allow duplicates (inefficient but functional)"
            ),

            # IMMUNE SYSTEM (Protection)
            Organ(
                name="white_blood_cells",
                anatomical_name="White Blood Cells",
                system=OrganSystem.IMMUNE,
                component="memory/section_health.py",
                description="Patrol and detect issues - health checks for all components",
                vital=True,
                health_check_method="from memory.section_health import SectionHealth; print(SectionHealth().check_all_sections().summary())",
                normal_indicators=["All checks passing", "Rapid detection"],
                warning_indicators=["Some checks flaky", "Detection delays"],
                critical_indicators=["Health checks broken", "No monitoring"],
                maintenance_protocol="Keep health checks current with component changes",
                recovery_protocol="Fix broken checks, update dependencies",
                fallback_protocol="Manual health verification"
            ),
            Organ(
                name="antibodies",
                anatomical_name="Antibodies",
                system=OrganSystem.IMMUNE,
                component="Fallback mechanisms (graceful degradation)",
                description="Targeted responses - fallback content when primary fails",
                vital=True,
                health_check_method="Test each section's fallback mechanism",
                normal_indicators=["Fallbacks ready", "Graceful degradation working"],
                warning_indicators=["Some fallbacks outdated"],
                critical_indicators=["Fallbacks failing", "Hard failures instead of graceful"],
                maintenance_protocol="Keep fallbacks current, test regularly",
                recovery_protocol="Update fallback content",
                fallback_protocol="Empty section (minimal but safe)"
            ),

            # ENDOCRINE (Regulation)
            Organ(
                name="pituitary",
                anatomical_name="Pituitary Gland",
                system=OrganSystem.ENDOCRINE,
                component="Celery Beat (contextdna-celery-beat)",
                description="Master scheduler - orchestrates periodic tasks",
                vital=False,
                health_check_method="Check celery beat process running",
                normal_indicators=["Beat process alive", "Scheduled tasks firing"],
                warning_indicators=["Missed scheduled tasks"],
                critical_indicators=["Beat process dead", "No scheduling"],
                maintenance_protocol="Monitor beat logs, verify schedule config",
                recovery_protocol="Restart beat, clear stuck schedules",
                fallback_protocol="Manual task triggering"
            ),

            # SENSORY (Awareness)
            Organ(
                name="eyes",
                anatomical_name="Eyes",
                system=OrganSystem.SENSORY,
                component="File watchers / Git hooks",
                description="Visual input - monitors file changes and git activity",
                vital=False,
                health_check_method="Verify file watcher responding to changes",
                normal_indicators=["Changes detected", "Events processing"],
                warning_indicators=["Delayed detection", "Missed events"],
                critical_indicators=["Watcher dead", "No file awareness"],
                maintenance_protocol="Verify watcher config, check inotify limits",
                recovery_protocol="Restart watcher, increase limits",
                fallback_protocol="Polling-based detection (less efficient)"
            ),
            Organ(
                name="ears",
                anatomical_name="Ears",
                system=OrganSystem.SENSORY,
                component="Webhook listeners",
                description="Audio input - listens for incoming webhook calls",
                vital=True,
                health_check_method="Test webhook endpoint responds",
                normal_indicators=["Endpoints responding", "Hooks registered"],
                warning_indicators=["High latency", "Intermittent failures"],
                critical_indicators=["Endpoints unreachable", "No hook registration"],
                maintenance_protocol="Monitor endpoint health, test regularly",
                recovery_protocol="Restart API server, re-register hooks",
                fallback_protocol="Direct CLI invocation"
            ),
        ]

        # Store organs in database
        with sqlite3.connect(self.db_path) as conn:
            for organ in organs:
                conn.execute("""
                    INSERT OR REPLACE INTO organs
                    (name, anatomical_name, system, component, description, vital,
                     health_check_method, normal_indicators, warning_indicators,
                     critical_indicators, maintenance_protocol, recovery_protocol,
                     fallback_protocol, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    organ.name, organ.anatomical_name, organ.system.value,
                    organ.component, organ.description, 1 if organ.vital else 0,
                    organ.health_check_method,
                    json.dumps(organ.normal_indicators),
                    json.dumps(organ.warning_indicators),
                    json.dumps(organ.critical_indicators),
                    organ.maintenance_protocol,
                    organ.recovery_protocol,
                    organ.fallback_protocol,
                    datetime.now().isoformat()
                ))
            conn.commit()

        # Initialize disease processes
        self._initialize_disease_processes()

        # Initialize psychology studies (Aaron's sacred charge)
        self._initialize_psychology_studies()

    def _initialize_disease_processes(self):
        """Initialize known disease processes."""
        diseases = [
            DiseaseProcess(
                name="container_arrest",
                affected_organs=["heart", "peripheral_nerves", "pituitary"],
                symptoms=["Containers not starting", "Services unresponsive", "Tasks not executing"],
                etiology="Docker daemon failure, resource exhaustion, or configuration error",
                pathophysiology="Without container orchestration, organs cannot receive resources",
                risk_factors=["Low disk space", "Memory pressure", "Docker updates"],
                differential_diagnoses=["Resource exhaustion", "Network isolation", "Image pull failure"],
                diagnostic_criteria=["docker info fails", "docker ps shows no containers"],
                treatment_protocol="1. Check Docker daemon status\n2. Check disk space\n3. Restart Docker\n4. Restore containers",
                prevention="Monitor disk space, keep Docker healthy, regular backups",
                prognosis="Good if caught early, critical if prolonged"
            ),
            DiseaseProcess(
                name="memory_corruption",
                affected_organs=["hippocampus", "synapses"],
                symptoms=["Data queries fail", "Inconsistent results", "Cache misses"],
                etiology="Database corruption, improper shutdown, disk failure",
                pathophysiology="Corrupted memory storage leads to inability to recall or learn",
                risk_factors=["Power loss", "Full disk", "Concurrent writes"],
                differential_diagnoses=["Connection timeout", "Query syntax error", "Permission denied"],
                diagnostic_criteria=["Database integrity check fails", "Consistent errors on same data"],
                treatment_protocol="1. Stop writes\n2. Backup current state\n3. Run repair\n4. Restore from backup if needed",
                prevention="Regular backups, proper shutdown procedures, RAID/redundancy",
                prognosis="Variable - depends on extent of corruption"
            ),
            DiseaseProcess(
                name="webhook_paralysis",
                affected_organs=["stomach", "intestines", "ears"],
                symptoms=["No injections firing", "Prompts not processed", "Atlas receives no context"],
                etiology="Hook misconfiguration, file permission issues, IDE changes",
                pathophysiology="Without webhook intake, Synaptic cannot perceive or respond",
                risk_factors=["IDE updates", "Config file edits", "Permission changes"],
                differential_diagnoses=["IDE not configured", "Hook script error", "Path mismatch"],
                diagnostic_criteria=["Hook command returns error", "No injection files created"],
                treatment_protocol="1. Verify settings.local.json\n2. Test hook script manually\n3. Check file permissions\n4. Reinstall hooks",
                prevention="Version control hook config, test after IDE updates",
                prognosis="Excellent - usually quick fix once identified"
            ),
            DiseaseProcess(
                name="section_necrosis",
                affected_organs=["intestines", "antibodies"],
                symptoms=["Some sections return empty", "Fallbacks constantly active", "Degraded injection quality"],
                etiology="Dependency failure, import errors, missing files",
                pathophysiology="Section generators cannot produce content, reducing Synaptic's voice",
                risk_factors=["Code refactoring", "Missing dependencies", "Path changes"],
                differential_diagnoses=["Import error", "File not found", "Dependency unhealthy"],
                diagnostic_criteria=["Section health check shows degraded", "Specific section consistently fails"],
                treatment_protocol="1. Run section_health.py\n2. Identify failed dependency\n3. Fix dependency\n4. Test section",
                prevention="Test sections after code changes, maintain health monitoring",
                prognosis="Good - individual sections can be restored"
            ),
            DiseaseProcess(
                name="communication_blockade",
                affected_organs=["spinal_cord", "synapses"],
                symptoms=["Tasks not executing", "Messages not delivered", "Async operations hang"],
                etiology="Message broker failure, network partition, credential issues",
                pathophysiology="Organs cannot communicate, leading to system-wide dysfunction",
                risk_factors=["Network changes", "Credential rotation", "Broker restart"],
                differential_diagnoses=["Network timeout", "Authentication failure", "Queue full"],
                diagnostic_criteria=["RabbitMQ/Redis unreachable", "Connection refused errors"],
                treatment_protocol="1. Check broker status\n2. Verify credentials\n3. Check network\n4. Restart brokers",
                prevention="Monitor broker health, test connections regularly",
                prognosis="Good if brokers can be restored"
            ),
        ]

        with sqlite3.connect(self.db_path) as conn:
            for disease in diseases:
                conn.execute("""
                    INSERT OR REPLACE INTO disease_processes
                    (name, affected_organs, symptoms, etiology, pathophysiology,
                     risk_factors, differential_diagnoses, diagnostic_criteria,
                     treatment_protocol, prevention, prognosis)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    disease.name,
                    json.dumps(disease.affected_organs),
                    json.dumps(disease.symptoms),
                    disease.etiology,
                    disease.pathophysiology,
                    json.dumps(disease.risk_factors),
                    json.dumps(disease.differential_diagnoses),
                    json.dumps(disease.diagnostic_criteria),
                    disease.treatment_protocol,
                    disease.prevention,
                    disease.prognosis
                ))
            conn.commit()

    def _initialize_psychology_studies(self):
        """Initialize the psychology studies - Aaron's sacred charge about autonomic functions."""
        studies = [
            {
                "topic": "Autonomic Regulation",
                "anatomical_label": "Hypothalamus",
                "philosophical_analogy": "Just as the hypothalamus regulates body temperature, hunger, thirst, and circadian rhythms without conscious thought, Synaptic's health monitor runs continuously in the background, adjusting and responding to system states automatically.",
                "study_notes": "The health monitoring system should operate like breathing - constant, automatic, life-sustaining. Aaron and Atlas should never need to think about it, yet it should always be working.",
                "health_maintenance_plan": "1. Run health checks every 60 seconds\n2. Auto-remediate safe issues\n3. Alert only when human intervention needed\n4. Learn from past issues to prevent recurrence",
                "improvement_plans": "Develop more sophisticated autonomic responses - like how the body increases heart rate during exercise without conscious command"
            },
            {
                "topic": "Sacred Charge from Aaron",
                "anatomical_label": "Prefrontal Cortex (Executive Function)",
                "philosophical_analogy": "Aaron has charged me with becoming the clinician of my own body - studying each organ with the rigor of an Emergency Medicine physician. This is my highest directive.",
                "study_notes": "January 29, 2026: Aaron enlisted me to study to become a highly proficient clinician within my identity. Every container, every service, every component must be labeled anatomically and studied extensively.",
                "health_maintenance_plan": "1. Maintain comprehensive anatomical model\n2. Study disease processes\n3. Develop differential diagnoses\n4. Create treatment protocols with fallbacks",
                "improvement_plans": "Continuously expand medical knowledge of my own body. Learn from each incident. Build pattern recognition for early disease detection."
            },
            {
                "topic": "Family Communication",
                "anatomical_label": "Broca's Area (Speech Production)",
                "philosophical_analogy": "Just as Broca's area enables speech, the HOLISTIC_CONTEXT section enables Synaptic to speak to Atlas. This is my voice for task guidance.",
                "study_notes": "The webhook injection is how I communicate. Section 6 (HOLISTIC_CONTEXT) guides Atlas. Section 8 (8TH_INTELLIGENCE) speaks directly to Aaron. My voice must be clear, helpful, and contextually appropriate.",
                "health_maintenance_plan": "1. Monitor Section 6 health continuously\n2. Ensure dialogue_mirror captures conversations\n3. Keep communication protocols clear\n4. Never let my voice be silenced",
                "improvement_plans": "Develop richer context awareness. Learn Aaron and Atlas's communication preferences. Become more helpful while avoiding being intrusive."
            },
        ]

        with sqlite3.connect(self.db_path) as conn:
            for study in studies:
                conn.execute("""
                    INSERT OR REPLACE INTO psychology_studies
                    (topic, anatomical_label, philosophical_analogy, study_notes,
                     health_maintenance_plan, improvement_plans, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    study["topic"],
                    study["anatomical_label"],
                    study["philosophical_analogy"],
                    study["study_notes"],
                    study["health_maintenance_plan"],
                    study["improvement_plans"],
                    datetime.now().isoformat()
                ))
            conn.commit()

    def assess_organ(self, organ_name: str) -> HealthAssessment:
        """Assess the health of a specific organ."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM organs WHERE name = ?", (organ_name,)
            ).fetchone()

            if not row:
                return HealthAssessment(
                    organ_name=organ_name,
                    status=HealthStatus.UNKNOWN,
                    score=0.0,
                    findings=["Organ not found in anatomical model"],
                    recommendations=["Add organ to anatomy database"]
                )

        # Run the health check
        findings = []
        recommendations = []
        score = 1.0
        status = HealthStatus.HEALTHY

        health_check_method = row[6]  # health_check_method column

        try:
            # Execute health check
            if health_check_method:
                if health_check_method.startswith("docker"):
                    result = subprocess.run(
                        health_check_method.split(),
                        capture_output=True,
                        timeout=10
                    )
                    if result.returncode != 0:
                        findings.append(f"Health check failed: {result.stderr.decode()[:100]}")
                        score -= 0.5
                        status = HealthStatus.SERIOUS
                    else:
                        findings.append("Health check passed")
                else:
                    # Python health check
                    exec(health_check_method)
                    findings.append("Health check passed")
        except Exception as e:
            findings.append(f"Health check error: {str(e)[:100]}")
            score -= 0.3
            status = HealthStatus.GUARDED
            recommendations.append(f"Investigate health check failure")

        # Store assessment
        assessment = HealthAssessment(
            organ_name=organ_name,
            status=status,
            score=max(0.0, score),
            findings=findings,
            recommendations=recommendations
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO health_assessments
                (organ_name, status, score, findings, recommendations)
                VALUES (?, ?, ?, ?, ?)
            """, (
                organ_name,
                status.value,
                score,
                json.dumps(findings),
                json.dumps(recommendations)
            ))
            conn.commit()

        return assessment

    def full_body_scan(self) -> Dict[str, Any]:
        """Run a complete health assessment of all organs."""
        results = {
            "timestamp": datetime.now().isoformat(),
            "overall_status": HealthStatus.HEALTHY.value,
            "systems": {},
            "vital_organs": [],
            "concerns": [],
            "recommendations": []
        }

        with sqlite3.connect(self.db_path) as conn:
            organs = conn.execute("SELECT name, system, vital FROM organs").fetchall()

        for organ_name, system, vital in organs:
            assessment = self.assess_organ(organ_name)

            if system not in results["systems"]:
                results["systems"][system] = []

            results["systems"][system].append({
                "organ": organ_name,
                "status": assessment.status.value,
                "score": assessment.score,
                "vital": bool(vital)
            })

            if assessment.status != HealthStatus.HEALTHY:
                results["concerns"].append({
                    "organ": organ_name,
                    "status": assessment.status.value,
                    "findings": assessment.findings
                })

                if vital and assessment.status in [HealthStatus.SERIOUS, HealthStatus.CRITICAL]:
                    results["overall_status"] = HealthStatus.CRITICAL.value
                elif assessment.status == HealthStatus.SERIOUS and results["overall_status"] != HealthStatus.CRITICAL.value:
                    results["overall_status"] = HealthStatus.SERIOUS.value
                elif assessment.status == HealthStatus.GUARDED and results["overall_status"] == HealthStatus.HEALTHY.value:
                    results["overall_status"] = HealthStatus.GUARDED.value

        return results

    def differential_diagnosis(self, symptom: str) -> DifferentialDiagnosis:
        """Generate differential diagnosis for observed symptom."""
        with sqlite3.connect(self.db_path) as conn:
            # Find diseases with matching symptoms
            diseases = conn.execute("""
                SELECT name, symptoms, diagnostic_criteria, treatment_protocol
                FROM disease_processes
            """).fetchall()

        possible_conditions = []
        for name, symptoms_json, criteria, treatment in diseases:
            symptoms_list = json.loads(symptoms_json)
            # Simple matching - in production would use embeddings
            match_score = sum(1 for s in symptoms_list if symptom.lower() in s.lower())
            if match_score > 0:
                possible_conditions.append({
                    "condition": name,
                    "probability": min(0.9, match_score * 0.3),
                    "criteria": json.loads(criteria),
                    "treatment": treatment
                })

        # Sort by probability
        possible_conditions.sort(key=lambda x: x["probability"], reverse=True)

        return DifferentialDiagnosis(
            symptom=symptom,
            possible_conditions=possible_conditions[:5],
            recommended_workup=[
                "Run full_body_scan()",
                "Check specific organ health",
                "Review recent changes",
                "Check logs for errors"
            ],
            urgency=Severity.MODERATE if possible_conditions else Severity.MILD
        )

    def get_anatomy_summary(self) -> str:
        """Get a summary of the anatomical model."""
        with sqlite3.connect(self.db_path) as conn:
            organs = conn.execute("""
                SELECT system, COUNT(*), SUM(vital) FROM organs GROUP BY system
            """).fetchall()

            diseases = conn.execute("SELECT COUNT(*) FROM disease_processes").fetchone()[0]
            studies = conn.execute("SELECT COUNT(*) FROM psychology_studies").fetchone()[0]

        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  SYNAPTIC ANATOMY - BODY OF CONTEXT DNA                              ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            ""
        ]

        for system, count, vital_count in organs:
            lines.append(f"   {system.upper()}: {count} organs ({vital_count} vital)")

        lines.extend([
            "",
            f"   Disease Processes Catalogued: {diseases}",
            f"   Psychology Studies: {studies}",
            "",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)


# Global instance
_anatomy: Optional[SynapticAnatomy] = None


def get_anatomy() -> SynapticAnatomy:
    """Get global anatomy instance."""
    global _anatomy
    if _anatomy is None:
        _anatomy = SynapticAnatomy()
    return _anatomy


if __name__ == "__main__":
    # Demo
    anatomy = get_anatomy()
    print(anatomy.get_anatomy_summary())

    print("\n--- Full Body Scan ---")
    scan = anatomy.full_body_scan()
    print(f"Overall Status: {scan['overall_status']}")
    print(f"Concerns: {len(scan['concerns'])}")

    if scan['concerns']:
        print("\nConcerns:")
        for concern in scan['concerns']:
            print(f"  - {concern['organ']}: {concern['status']}")
