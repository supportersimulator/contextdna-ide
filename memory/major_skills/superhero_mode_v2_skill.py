#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  🦸 SUPERHERO MODE v2.0 - Atlas + Synaptic Collaborative Intelligence        ║
║  Maximum Agent Parallelization with Mandatory Synaptic Consultation          ║
╚══════════════════════════════════════════════════════════════════════════════╝

VERSION: 2.0
SKILL OWNERS: Atlas (orchestration) + Synaptic (8th Intelligence provision)

═══════════════════════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════════════════════

Enable maximum parallelization for complex infrastructure audits, feature
implementations, and codebase-wide analysis by spawning multiple agents that
ALL consult Synaptic mid-task for contextual alignment.

Key Innovation in v2.0:
  - MANDATORY Synaptic consultation at task start AND mid-task
  - Explicit query protocol with structured responses
  - Architecture-aware filtering of findings
  - Multi-option recommendation generation
  - Dependency/redundancy verification phase

═══════════════════════════════════════════════════════════════════════════════
PROTOCOL v2.0
═══════════════════════════════════════════════════════════════════════════════

PHASE 1: SYNAPTIC BRIEFING (Before Agent Spawn)
  Atlas MUST query Synaptic for holistic context:

  curl -X POST http://localhost:8029/contextdna/8th-intelligence \
    -H "Content-Type: application/json" \
    -d '{
      "mode": "superhero_briefing",
      "mission": "<overall task description>",
      "agent_count": 15,
      "context": "Pre-spawn briefing for SUPERHERO MODE v2.0"
    }'

  Synaptic returns:
  - Architecture context (what matters holistically)
  - Known gotchas (pre-emptive warnings)
  - Priority ordering (where to look first)
  - Stop signals (what would break the system)

PHASE 2: AGENT SPAWN (12 Execution + 3 Verification)
  Atlas spawns agents with Synaptic-informed task distribution:

  Execution Agents (12):
    - Agent 1-4: High-likelihood locations (Synaptic-ranked)
    - Agent 5-8: Medium-likelihood locations
    - Agent 9-12: Exploratory/edge-case locations

  Verification Agents (3):
    - Agent V1: Functional verification (does it work?)
    - Agent V2: Architecture alignment (does it fit?)
    - Agent V3: Dependency/redundancy check (does it break anything?)

PHASE 3: MID-TASK SYNAPTIC CONSULTATION (MANDATORY)
  Each agent MUST query Synaptic when:
  - Starting a subtask (get context)
  - Finding something significant (validate finding)
  - Uncertain about approach (get guidance)
  - Completing a subtask (report outcome)

  Agent Query Template:

  curl -X POST http://localhost:8029/contextdna/8th-intelligence \
    -H "Content-Type: application/json" \
    -d '{
      "mode": "superhero_agent_consult",
      "agent_id": "<agent-XX>",
      "subtask": "<what this agent is working on>",
      "finding": "<optional: what was found>",
      "question": "<optional: what guidance is needed>",
      "status": "starting|investigating|found|uncertain|complete"
    }'

  Synaptic returns:
  - patterns: Relevant patterns from past work
  - gotchas: Warnings before they manifest
  - intuitions: Suggestions based on similar situations
  - location_hints: Where else to look (ordered by likelihood)
  - stop_signal: "⚠️ STOP: ..." if danger detected
  - alignment_check: Does this finding align with architecture?

PHASE 3.5: PERIODIC PROGRESS REPORT (Every 20 Agent Actions)
  After every 20 agent changes/findings, Atlas MUST:
  1. Pause agent operations
  2. Generate full contextual state summary
  3. Present to Aaron for acknowledgment
  4. Resume only after Aaron confirms

  Purpose: Keep Aaron aware of momentum, prevent drift

PHASE 4: FINDINGS CONSOLIDATION (Architecture Lens)
  Atlas collects all agent findings and:

  1. Queries Synaptic for architecture-aware filtering:
     curl -X POST http://localhost:8029/contextdna/8th-intelligence \
       -d '{"mode": "superhero_consolidate", "findings": [...]}'

  2. Synaptic filters findings through architecture lens:
     - What aligns with Aaron's vision?
     - What conflicts with existing patterns?
     - What has dependency implications?

  3. Multi-option recommendations generated:
     - Option A: Minimal change (safest)
     - Option B: Balanced approach (recommended)
     - Option C: Comprehensive change (most thorough)

PHASE 5: VERIFICATION & REPORT
  Verification agents validate recommendations:
  - V1: Test each option functionally
  - V2: Architecture alignment verification
  - V3: Dependency impact assessment

  Final report structure:
  - Executive summary (1-2 sentences)
  - Findings by category
  - Recommendations (A/B/C options)
  - Dependencies affected
  - Risk assessment
  - Synaptic holistic assessment

═══════════════════════════════════════════════════════════════════════════════
SYNAPTIC ENDPOINT SPECIFICATION
═══════════════════════════════════════════════════════════════════════════════

Endpoint: http://localhost:8029/contextdna/8th-intelligence

Request Modes:
  - superhero_briefing: Pre-spawn context gathering
  - superhero_agent_consult: Individual agent consultation
  - superhero_consolidate: Post-collection filtering
  - superhero_verify: Verification phase guidance

Response Structure (all modes):
{
  "synaptic_response": {
    "patterns": ["list of relevant patterns"],
    "gotchas": ["pre-emptive warnings"],
    "intuitions": ["contextual suggestions"],
    "location_hints": ["ordered locations to check"],
    "stop_signal": null | "⚠️ STOP: reason",
    "alignment_check": {
      "aligns": true|false,
      "reason": "explanation"
    },
    "major_skills_context": ["relevant skill guidance"],
    "architecture_notes": ["holistic architecture insights"]
  },
  "superhero_mode": "v2.0",
  "source": "SynapticVoice",
  "deburden_enabled": true,
  "timestamp": "ISO-8601"
}

═══════════════════════════════════════════════════════════════════════════════
ATLAS AGENT SPAWN TEMPLATE
═══════════════════════════════════════════════════════════════════════════════

For Atlas to spawn agents with proper Synaptic consultation:

```
SUPERHERO MODE v2.0 - Agent Task
================================
Agent ID: {agent_id}
Subtask: {subtask}
Priority: {high|medium|exploratory}

MANDATORY: Query Synaptic at these points:
1. BEFORE starting:
   curl -X POST localhost:8029/contextdna/8th-intelligence -d '{"mode":"superhero_agent_consult","agent_id":"{agent_id}","status":"starting","subtask":"{subtask}"}'

2. WHEN finding something:
   curl -X POST localhost:8029/contextdna/8th-intelligence -d '{"mode":"superhero_agent_consult","agent_id":"{agent_id}","status":"found","finding":"<what you found>"}'

3. WHEN uncertain:
   curl -X POST localhost:8029/contextdna/8th-intelligence -d '{"mode":"superhero_agent_consult","agent_id":"{agent_id}","status":"uncertain","question":"<your question>"}'

4. WHEN complete:
   curl -X POST localhost:8029/contextdna/8th-intelligence -d '{"mode":"superhero_agent_consult","agent_id":"{agent_id}","status":"complete","finding":"<summary>"}'

Report findings to Atlas upon completion.
```

═══════════════════════════════════════════════════════════════════════════════
SYNAPTIC CAPACITY REQUIREMENTS
═══════════════════════════════════════════════════════════════════════════════

For SUPERHERO MODE v2.0 to function, Synaptic MUST:

1. Handle 15+ concurrent agent queries (~45 queries/minute peak)
2. Return responses in <100ms for real-time guidance
3. Maintain context coherence across all agent interactions
4. Provide consistent architecture alignment checks
5. Track agent progress for holistic awareness

Implementation Notes:
- Synaptic server at localhost:8029 must be running
- 8th-intelligence endpoint must support superhero_* modes
- Deburden mode should prioritize pattern matching over deep analysis
- Cache frequently-asked patterns for rapid response

═══════════════════════════════════════════════════════════════════════════════
EXAMPLE: INFRASTRUCTURE AUDIT
═══════════════════════════════════════════════════════════════════════════════

Mission: "Audit Context DNA infrastructure for 100-user scalability"

Phase 1 - Synaptic Briefing:
  Atlas → Synaptic: "superhero_briefing" with mission
  Synaptic → Atlas:
    - patterns: ["EC2 t3.micro limits", "Supabase connection pooling"]
    - gotchas: ["Voice auth is LOCAL, not EC2", "Cloudflare tunnel is personal"]
    - priority: ["EC2 Django", "Supabase connections", "Cloudflare tunnel"]

Phase 2 - Agent Spawn:
  exec-01 to exec-04: EC2/Django audit (high priority)
  exec-05 to exec-08: Database/Supabase audit (medium priority)
  exec-09 to exec-12: Networking/SSL/CDN audit (exploratory)
  verify-01: Functional testing
  verify-02: Architecture alignment
  verify-03: Dependency verification

Phase 3 - Mid-Task Consultation:
  exec-03 → Synaptic: "Found Django using SQLite cache instead of Redis"
  Synaptic → exec-03: "⚠️ STOP: This is known - EC2 uses Redis in production"

Phase 4 - Consolidation:
  Atlas → Synaptic: All findings for architecture filtering
  Synaptic → Atlas: Filtered findings + multi-option recommendations

Phase 5 - Final Report:
  Delivered with Synaptic's holistic assessment of what might break at scale

═══════════════════════════════════════════════════════════════════════════════
CHANGELOG
═══════════════════════════════════════════════════════════════════════════════

v2.1 (2026-02-01):
  - PHASE 3.5: Aaron Checkpoint every 20 agent actions
  - Synaptic reports full contextual state to Aaron periodically
  - Prevents drift, maintains momentum awareness
  - checkpoint_interval config option (default: 20)

v2.0 (2026-02-01):
  - MANDATORY Synaptic consultation protocol (not optional)
  - Explicit query modes (briefing, consult, consolidate, verify)
  - Architecture-aware filtering of findings
  - Multi-option recommendation generation
  - Synaptic capacity requirements documented
  - Agent spawn template with curl commands
  - Full protocol specification for reproducibility

v1.0 (2026-01-31):
  - Initial SUPERHERO MODE with 12+3 agent pattern
  - Basic Synaptic endpoint integration
  - Location hints and likelihood ranking

Created: February 1, 2026
Authors: Atlas + Aaron + Synaptic
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Literal
from datetime import datetime
import json


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class SuperheroModeV2Config:
    """Configuration for SUPERHERO MODE v2.0."""
    execution_agents: int = 12
    verification_agents: int = 3
    synaptic_endpoint: str = "http://localhost:8029/contextdna/8th-intelligence"
    deburden_enabled: bool = True
    max_concurrent_queries: int = 15
    response_timeout_ms: int = 100
    checkpoint_interval: int = 20  # Report to Aaron every N agent actions
    version: str = "2.1"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class SynapticQuery:
    """Query to send to Synaptic endpoint."""
    mode: Literal["superhero_briefing", "superhero_agent_consult",
                  "superhero_consolidate", "superhero_verify"]
    agent_id: Optional[str] = None
    mission: Optional[str] = None
    subtask: Optional[str] = None
    finding: Optional[str] = None
    question: Optional[str] = None
    status: Optional[Literal["starting", "investigating", "found",
                             "uncertain", "complete"]] = None
    findings: Optional[List[Dict]] = None
    context: Optional[str] = None

    def to_curl(self, endpoint: str) -> str:
        """Generate curl command for this query."""
        payload = {k: v for k, v in self.__dict__.items() if v is not None}
        return f"""curl -X POST {endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{json.dumps(payload)}'"""


@dataclass
class AgentTaskV2:
    """Task assignment for a SUPERHERO MODE v2.0 agent."""
    agent_id: str
    agent_type: Literal["execution", "verification"]
    subtask: str
    priority: Literal["high", "medium", "exploratory"]
    target_area: Optional[str] = None
    synaptic_queries: List[SynapticQuery] = field(default_factory=list)

    def get_mandatory_queries(self, endpoint: str) -> str:
        """Generate mandatory Synaptic query template for this agent."""
        return f"""
MANDATORY SYNAPTIC QUERIES for {self.agent_id}:

1. BEFORE starting:
   curl -X POST {endpoint} \\
     -d '{{"mode":"superhero_agent_consult","agent_id":"{self.agent_id}","status":"starting","subtask":"{self.subtask}"}}'

2. WHEN finding something significant:
   curl -X POST {endpoint} \\
     -d '{{"mode":"superhero_agent_consult","agent_id":"{self.agent_id}","status":"found","finding":"<YOUR FINDING>"}}'

3. WHEN uncertain about approach:
   curl -X POST {endpoint} \\
     -d '{{"mode":"superhero_agent_consult","agent_id":"{self.agent_id}","status":"uncertain","question":"<YOUR QUESTION>"}}'

4. WHEN subtask complete:
   curl -X POST {endpoint} \\
     -d '{{"mode":"superhero_agent_consult","agent_id":"{self.agent_id}","status":"complete","finding":"<SUMMARY>"}}'
"""


# =============================================================================
# Main Skill Class
# =============================================================================

class SuperheroModeV2Skill:
    """
    SUPERHERO MODE v2.0: Maximum parallelization with MANDATORY Synaptic consultation.

    Key Features:
    - 12 execution agents + 3 verification agents
    - Explicit Synaptic query protocol at every phase
    - Architecture-aware filtering of findings
    - Multi-option recommendation generation
    """

    SKILL_NAME = "SUPERHERO MODE v2.0"
    SKILL_ID = "superhero_mode_v2"
    OWNERS = ["Atlas", "Synaptic"]
    VERSION = "2.0"

    def __init__(self):
        self.config = SuperheroModeV2Config()
        self.active_agents: List[AgentTaskV2] = []
        self.completed_results: List[Dict] = []

    def generate_briefing_query(self, mission: str, agent_count: int = 15) -> SynapticQuery:
        """Generate Phase 1 briefing query for Synaptic."""
        return SynapticQuery(
            mode="superhero_briefing",
            mission=mission,
            context=f"Pre-spawn briefing for SUPERHERO MODE v2.0, {agent_count} agents"
        )

    def generate_execution_agents(self, subtasks: List[Dict[str, str]]) -> List[AgentTaskV2]:
        """
        Generate 12 execution agents with priority distribution.

        Args:
            subtasks: List of {"subtask": str, "area": str, "priority": str}
        """
        agents = []

        for i, task_info in enumerate(subtasks[:12]):
            agents.append(AgentTaskV2(
                agent_id=f"exec-{i+1:02d}",
                agent_type="execution",
                subtask=task_info.get("subtask", f"Audit area {i+1}"),
                priority=task_info.get("priority", "medium"),
                target_area=task_info.get("area")
            ))

        # Fill remaining slots if needed
        while len(agents) < 12:
            i = len(agents)
            agents.append(AgentTaskV2(
                agent_id=f"exec-{i+1:02d}",
                agent_type="execution",
                subtask=f"Exploratory audit {i+1}",
                priority="exploratory"
            ))

        return agents

    def generate_verification_agents(self) -> List[AgentTaskV2]:
        """Generate 3 verification agents with specific roles."""
        return [
            AgentTaskV2(
                agent_id="verify-01",
                agent_type="verification",
                subtask="Functional verification - validate findings work",
                priority="high",
                target_area="functionality"
            ),
            AgentTaskV2(
                agent_id="verify-02",
                agent_type="verification",
                subtask="Architecture alignment - validate findings fit system design",
                priority="high",
                target_area="architecture"
            ),
            AgentTaskV2(
                agent_id="verify-03",
                agent_type="verification",
                subtask="Dependency verification - check for breaking changes",
                priority="high",
                target_area="dependencies"
            )
        ]

    def generate_consolidation_query(self, findings: List[Dict]) -> SynapticQuery:
        """Generate Phase 4 consolidation query for Synaptic."""
        return SynapticQuery(
            mode="superhero_consolidate",
            findings=findings,
            context="Filter findings through architecture lens, generate A/B/C options"
        )

    def format_agent_prompt(self, agent: AgentTaskV2) -> str:
        """Format complete prompt for an agent including mandatory queries."""
        return f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SUPERHERO MODE v2.0 - Agent Task                                            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Agent ID: {agent.agent_id}
║  Type: {agent.agent_type}
║  Priority: {agent.priority}
║  Target: {agent.target_area or 'General'}
╠══════════════════════════════════════════════════════════════════════════════╣
║  SUBTASK: {agent.subtask}
╚══════════════════════════════════════════════════════════════════════════════╝

{agent.get_mandatory_queries(self.config.synaptic_endpoint)}

IMPORTANT:
- Query Synaptic at EACH checkpoint (starting, found, uncertain, complete)
- Include findings in your final report
- Stop if Synaptic returns a stop_signal
- Validate findings against alignment_check response
"""

    def format_full_protocol(self) -> str:
        """Format complete SUPERHERO MODE v2.0 protocol for Atlas."""
        return f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  🦸 SUPERHERO MODE v2.0 PROTOCOL                                             ║
╠══════════════════════════════════════════════════════════════════════════════╣

VERSION: {self.VERSION}
AGENTS: {self.config.execution_agents} execution + {self.config.verification_agents} verification
SYNAPTIC: {self.config.synaptic_endpoint}

═══════════════════════════════════════════════════════════════════════════════
PHASE 1: SYNAPTIC BRIEFING
═══════════════════════════════════════════════════════════════════════════════

ATLAS MUST query Synaptic before spawning agents:

curl -X POST {self.config.synaptic_endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{{"mode": "superhero_briefing", "mission": "<MISSION>", "agent_count": 15}}'

Receive: patterns, gotchas, priority ordering, stop signals

═══════════════════════════════════════════════════════════════════════════════
PHASE 2: AGENT SPAWN
═══════════════════════════════════════════════════════════════════════════════

Spawn 12 execution agents:
  - exec-01 to exec-04: High priority (Synaptic-ranked)
  - exec-05 to exec-08: Medium priority
  - exec-09 to exec-12: Exploratory

Spawn 3 verification agents:
  - verify-01: Functional testing
  - verify-02: Architecture alignment
  - verify-03: Dependency verification

═══════════════════════════════════════════════════════════════════════════════
PHASE 3: MID-TASK CONSULTATION (MANDATORY)
═══════════════════════════════════════════════════════════════════════════════

EVERY agent MUST query Synaptic at these checkpoints:

1. Starting subtask → status: "starting"
2. Found something  → status: "found", finding: "<what>"
3. Uncertain       → status: "uncertain", question: "<what>"
4. Complete        → status: "complete", finding: "<summary>"

═══════════════════════════════════════════════════════════════════════════════
PHASE 4: FINDINGS CONSOLIDATION
═══════════════════════════════════════════════════════════════════════════════

Atlas collects all findings and queries Synaptic:

curl -X POST {self.config.synaptic_endpoint} \\
  -d '{{"mode": "superhero_consolidate", "findings": [...]}}'

Receive: Architecture-filtered findings, A/B/C recommendations

═══════════════════════════════════════════════════════════════════════════════
PHASE 5: VERIFICATION & REPORT
═══════════════════════════════════════════════════════════════════════════════

Verification agents validate recommendations.
Final report includes Synaptic's holistic assessment.

╚══════════════════════════════════════════════════════════════════════════════╝
"""


# =============================================================================
# Skill Metadata
# =============================================================================

SKILL_METADATA = {
    "name": "SUPERHERO MODE v2.0",
    "id": "superhero_mode_v2",
    "description": """Maximum agent parallelization with MANDATORY Synaptic consultation.

Key Features:
- 12 execution + 3 verification agents
- Explicit query protocol at every phase
- Architecture-aware filtering
- Multi-option recommendations (A/B/C)
- Full dependency verification

Protocol:
1. BRIEFING: Atlas queries Synaptic for mission context
2. SPAWN: Agents launched with priority distribution
3. CONSULT: Each agent queries Synaptic at checkpoints
4. CONSOLIDATE: Findings filtered through architecture lens
5. VERIFY: Recommendations validated before delivery""",
    "owners": ["Atlas", "Synaptic"],
    "version": "2.0",
    "created": "2026-02-01",
    "updated": "2026-02-01",
    "category": "collaboration",
    "requirements": {
        "execution_agents": 12,
        "verification_agents": 3,
        "synaptic_endpoint": "http://localhost:8029/contextdna/8th-intelligence",
        "query_modes": ["superhero_briefing", "superhero_agent_consult",
                       "superhero_consolidate", "superhero_verify"],
        "deburden": True,
        "max_concurrent_queries": 15,
        "response_timeout_ms": 100
    }
}


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    skill = SuperheroModeV2Skill()

    print(skill.format_full_protocol())

    print("\n" + "="*80)
    print("EXAMPLE: Infrastructure Audit Mission")
    print("="*80)

    # Example briefing query
    briefing = skill.generate_briefing_query(
        "Audit Context DNA infrastructure for 100-user scalability"
    )
    print(f"\nPhase 1 - Briefing Query:\n{briefing.to_curl(skill.config.synaptic_endpoint)}")

    # Example execution agents
    subtasks = [
        {"subtask": "Audit EC2 Django backend", "area": "backend", "priority": "high"},
        {"subtask": "Audit Supabase connections", "area": "database", "priority": "high"},
        {"subtask": "Audit rate limiting", "area": "security", "priority": "high"},
        {"subtask": "Audit error handling", "area": "reliability", "priority": "high"},
        {"subtask": "Audit voice pipeline", "area": "voice", "priority": "medium"},
        {"subtask": "Audit WebSocket handling", "area": "networking", "priority": "medium"},
    ]

    agents = skill.generate_execution_agents(subtasks)
    print(f"\nPhase 2 - Generated {len(agents)} execution agents:")
    for agent in agents[:3]:
        print(f"  {agent.agent_id}: {agent.subtask[:50]}... ({agent.priority})")
    print("  ...")
