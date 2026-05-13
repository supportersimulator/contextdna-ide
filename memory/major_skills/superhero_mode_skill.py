#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  SUPERHERO MODE - Atlas + Synaptic Collaborative Skill              ║
║  Maximum Agent Parallelization with Synaptic Omnipresence           ║
╚══════════════════════════════════════════════════════════════════════╝

SKILL OWNERS: Atlas (execution) + Synaptic (context provision)

PURPOSE:
  Enable maximum parallelization for complex tasks by spawning multiple
  agents that ALL have access to Synaptic's 8th Intelligence mid-task.

PROTOCOL (Assign → Compact → Results):

  WAVE 1 — EXECUTION (up to 10 agents):
  1. Atlas assigns all 10 agents with SPECIFIC subtasks while context is rich
     - Each agent gets a distinct aspect/location/question
     - Agents work independently with CLAUDE-agent.md (~350 tokens)
     - All agents curl 8th-intelligence mid-task for patterns + prior findings
     - All agents record findings to WAL: POST /superhero/finding
     - All agents write full results to /tmp/atlas-agent-results/AGENT_ID.md
  2. Atlas runs /compact — frees delegation overhead from context
  3. Atlas rehydrates: session_historian.py rehydrate
  4. Atlas calls GET /contextdna/superhero/debrief — one curl, full picture
  5. Atlas selectively reads /tmp files ONLY for high-severity findings

  WAVE 2 — VERIFICATION (up to 5 agents):
  6. Atlas spawns verification agents based on Wave 1 findings
  7. Repeat: assign → compact → debrief

  ALL AGENTS MUST:
  - curl 127.0.0.1:8080/contextdna/8th-intelligence mid-task
  - POST findings to /contextdna/superhero/finding with severity
  - Write results to /tmp/atlas-agent-results/AGENT_ID.md
  - Use /contextdna/agent-doc/append for durable research

AGENT CURL PATTERNS:
```bash
# Mid-task intelligence (patterns, gotchas, prior agent findings)
curl -s -X POST http://127.0.0.1:8080/contextdna/8th-intelligence \\
  -H "Content-Type: application/json" \\
  -d '{"subtask":"what you are doing","agent_id":"YOUR_ID"}'

# Record finding to WAL
curl -s -X POST http://127.0.0.1:8080/contextdna/superhero/finding \\
  -H "Content-Type: application/json" \\
  -d '{"agent_id":"YOUR_ID","finding":"text","finding_type":"gotcha","severity":"high"}'

# Write to designated agent doc (durable)
curl -s -X POST http://127.0.0.1:8080/contextdna/agent-doc/append \\
  -H "Content-Type: application/json" \\
  -d '{"agent_id":"YOUR_ID","doc_path":"context-dna/docs/YOUR-DOC.md","content":"research"}'
```

DEBRIEF (Atlas calls after /compact + rehydrate):
```bash
curl -s http://127.0.0.1:8080/contextdna/superhero/debrief
# Returns: WAL summary, criticals, result files, agent doc changes
```

Created: January 31, 2026
Updated: February 16, 2026 — v3: Assign→Compact→Results protocol
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
import json


@dataclass
class SuperheroModeConfig:
    """Configuration for SUPERHERO MODE execution.

    v3: Assign→Compact→Results protocol.
    Wave 1: up to 10 execution agents. Wave 2: up to 5 verification agents.
    Anticipation engine pre-computes 4 LLM artifacts cached in Redis.
    Agents receive enriched context via 8th-intelligence endpoint (~3ms).
    """
    execution_agents: int = 10  # Wave 1: parallelize task execution
    verification_agents: int = 5  # Wave 2: test and verify findings
    max_agents_per_batch: int = 10  # Hard limit per wave
    synaptic_endpoint: str = "http://127.0.0.1:8080/contextdna/8th-intelligence"
    finding_endpoint: str = "http://127.0.0.1:8080/contextdna/superhero/finding"
    debrief_endpoint: str = "http://127.0.0.1:8080/contextdna/superhero/debrief"
    doc_append_endpoint: str = "http://127.0.0.1:8080/contextdna/agent-doc/append"
    deburden_enabled: bool = True
    max_concurrent_queries: int = 10
    anticipation_enabled: bool = True


@dataclass
class AgentTask:
    """Task assignment for a spawned agent."""
    agent_id: str
    subtask: str
    likelihood_rank: int  # 1 = most likely, higher = less likely
    search_location: Optional[str] = None
    verification_target: Optional[str] = None


class SuperheroModeSkill:
    """
    SUPERHERO MODE: Maximum parallelization with Synaptic omnipresence.

    Protocol: Assign → Compact → Debrief → (optional Wave 2)
    """

    SKILL_NAME = "SUPERHERO MODE"
    SKILL_ID = "superhero_mode"
    OWNERS = ["Atlas", "Synaptic"]

    def __init__(self):
        self.config = SuperheroModeConfig()
        self.active_agents: List[AgentTask] = []
        self.completed_agents: List[Dict] = []

    def generate_execution_plan(self, task: str, locations: List[str]) -> List[AgentTask]:
        """Generate Wave 1 execution plan (up to 10 agents)."""
        agents = []
        count = min(self.config.execution_agents, self.config.max_agents_per_batch)
        for i in range(count):
            location_idx = i % len(locations) if locations else 0
            agents.append(AgentTask(
                agent_id=f"exec-agent-{i+1:02d}",
                subtask=f"Search {locations[location_idx] if locations else 'codebase'} for: {task}",
                likelihood_rank=location_idx + 1,
                search_location=locations[location_idx] if locations else None
            ))
        return agents

    def generate_verification_plan(self, findings: List[str]) -> List[AgentTask]:
        """Generate Wave 2 verification plan (up to 5 agents)."""
        agents = []
        count = min(self.config.verification_agents, len(findings) if findings else 3)
        for i in range(count):
            finding_idx = i % len(findings) if findings else 0
            agents.append(AgentTask(
                agent_id=f"verify-agent-{i+1:02d}",
                subtask=f"Verify and test: {findings[finding_idx] if findings else 'solution'}",
                likelihood_rank=1,
                verification_target=findings[finding_idx] if findings else None
            ))
        return agents

    def get_agent_curl_command(self, agent: AgentTask) -> str:
        """Generate the curl command for an agent to query Synaptic."""
        payload = {
            "subtask": agent.subtask,
            "agent_id": agent.agent_id,
            "context": f"likelihood_rank: {agent.likelihood_rank}"
        }
        return f'''curl -s -X POST {self.config.synaptic_endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{json.dumps(payload)}'
'''

    def format_skill_instructions(self) -> str:
        """Format SUPERHERO MODE instructions for Atlas."""
        return f"""
╔══════════════════════════════════════════════════════════════════════╗
║  SUPERHERO MODE ACTIVATED (v3: Assign→Compact→Results)              ║
╠══════════════════════════════════════════════════════════════════════╣

WAVE 1 — ASSIGN {self.config.execution_agents} EXECUTION AGENTS:
  1. Assign all {self.config.execution_agents} agents with SPECIFIC subtasks (context is rich now)
  2. Run /compact — free delegation overhead
  3. Rehydrate: .venv/bin/python3 memory/session_historian.py rehydrate
  4. Debrief: curl -s {self.config.debrief_endpoint}
  5. Selectively read /tmp/atlas-agent-results/ for high-severity only

WAVE 2 — VERIFY (up to {self.config.verification_agents} agents):
  6. Spawn verification agents based on Wave 1 criticals
  7. Repeat: assign → compact → debrief

ALL AGENTS MUST:
  curl {self.config.synaptic_endpoint} mid-task
  POST findings to {self.config.finding_endpoint}
  Write results to /tmp/atlas-agent-results/AGENT_ID.md

SYNAPTIC PROVIDES:
  patterns | gotchas | intuitions | major_skills | stop_signals
  + LLM-enriched superhero artifacts (mission, gotchas, architecture, failures)
  + Prior agent findings from WAL (agent-to-agent visibility)

CONTEXT BUDGET: ~350 tokens per agent (CLAUDE-agent.md)
DEBURDEN: {self.config.deburden_enabled} (~3ms response)

╚══════════════════════════════════════════════════════════════════════╝
"""


# Skill registration for major_skills system
SKILL_METADATA = {
    "name": "SUPERHERO MODE",
    "id": "superhero_mode",
    "description": (
        "Maximum agent parallelization with Synaptic omnipresence. "
        "v3: Assign→Compact→Results protocol. Wave 1: 10 execution agents. "
        "Wave 2: 5 verification agents. All agents curl Synaptic mid-task. "
        "Atlas compacts after delegation, rehydrates, then debriefs via single endpoint."
    ),
    "owners": ["Atlas", "Synaptic"],
    "created": "2026-01-31",
    "updated": "2026-02-16",
    "category": "collaboration",
    "requirements": {
        "execution_agents": 10,
        "verification_agents": 5,
        "max_per_batch": 10,
        "synaptic_endpoint": "http://127.0.0.1:8080/contextdna/8th-intelligence",
        "debrief_endpoint": "http://127.0.0.1:8080/contextdna/superhero/debrief",
        "deburden": True,
    },
    "protocol": "assign_all → /compact → rehydrate → debrief → selective_read",
}


if __name__ == "__main__":
    skill = SuperheroModeSkill()
    print(skill.format_skill_instructions())

    # Example: Generate plan for a task
    task = "Find all Synaptic communication disconnects"
    locations = [
        "memory/synaptic_voice.py",
        "memory/synaptic_chat_server.py",
        "memory/agent_service.py",
        "mcp-servers/synaptic_mcp.py",
        "memory/persistent_hook_structure.py",
        "context-dna/local_llm/api_server.py"
    ]

    print("\nWave 1 — Execution Plan:")
    for agent in skill.generate_execution_plan(task, locations):
        print(f"  {agent.agent_id}: {agent.subtask[:60]}...")
