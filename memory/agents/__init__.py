"""
Context DNA Agents - The 20 Agents of Synaptic's Nervous System

Based on the Epistemic Sustainability Architecture (Logos Design):

BRAIN (Central Processing):
  - vault: Encrypted persistent storage (Brain)

SENSORY (Perception):
  - eyes: Code observers (Visual cortex)
  - ears: Runtime & console observers (Auditory cortex)
  - touch: Interaction sensors (Somatosensory cortex)

NERVOUS (Communication):
  - ans: Celery task fabric (Autonomic nervous system)
  - peripheral_nerves: Signal emitters (Peripheral nerves)

MEMORY (Storage & Recall):
  - hippocampus: Context indexer (Memory formation)
  - neocortex: Consolidated context store (Long-term memory)
  - pruning: Decay & relevance pruner (Forgetting)

COGNITION (Reasoning):
  - subconscious: Local LLM reasoner (Limbic system)

CONTROL (Filtering & Orchestration):
  - thalamus: Relevance filter/gate (Sensory relay)
  - prefrontal: Injection orchestrator (Executive function)

ACTION (Execution):
  - motor: Conscious coding agent (Motor cortex)

LEARNING (Improvement):
  - cerebellum: Outcome evaluator (Motor learning)

SAFETY (Protection):
  - immune: Integrity & drift monitor (Immune system)

POLICY (Identity):
  - self: ContextDNA identity model (Sense of self)

SERVICES (High-level Operations):
  - curator: Data curation pipeline
  - indexer: Content indexing service
  - observer: Event observation service
  - injector: Context injection service

Total: 20 Agents
"""

from .base import Agent, AgentState, AgentRegistry, get_registry
from .brain.vault import VaultAgent
from .sensory.eyes import EyesAgent
from .sensory.ears import EarsAgent
from .sensory.touch import TouchAgent
from .nervous.ans import ANSAgent
from .nervous.peripheral_nerves import PeripheralNervesAgent
from .memory.hippocampus import HippocampusAgent
from .memory.neocortex import NeocortexAgent
from .memory.pruning import PruningAgent
from .control.thalamus import ThalamusAgent
from .control.prefrontal import PrefrontalAgent
from .control.subconscious import SubconsciousAgent
from .action.motor import MotorAgent
from .learning.cerebellum import CerebellumAgent
from .safety.immune import ImmuneAgent
from .policy.self_agent import SelfAgent
from .services.curator import CuratorAgent
from .services.indexer import IndexerAgent
from .services.observer import ObserverAgent
from .services.injector import InjectorAgent

__all__ = [
    # Base classes
    'Agent', 'AgentState', 'AgentRegistry', 'get_registry',
    # All 20 agents
    'VaultAgent',           # 1. Brain storage
    'EyesAgent',            # 2. Code observers
    'EarsAgent',            # 3. Runtime observers
    'TouchAgent',           # 4. Interaction sensors
    'ANSAgent',             # 5. Celery fabric
    'PeripheralNervesAgent',# 6. Signal emitters
    'HippocampusAgent',     # 7. Context indexer
    'NeocortexAgent',       # 8. Long-term memory
    'PruningAgent',         # 9. Decay/pruner
    'SubconsciousAgent',    # 10. Local LLM
    'ThalamusAgent',        # 11. Relevance filter
    'PrefrontalAgent',      # 12. Injection orchestrator
    'MotorAgent',           # 13. Coding agent
    'CerebellumAgent',      # 14. Outcome evaluator
    'ImmuneAgent',          # 15. Integrity monitor
    'SelfAgent',            # 16. Identity model
    'CuratorAgent',         # 17. Data curation
    'IndexerAgent',         # 18. Content indexing
    'ObserverAgent',        # 19. Event observation
    'InjectorAgent',        # 20. Context injection
]
