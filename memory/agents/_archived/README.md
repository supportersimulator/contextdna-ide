# Archived Anatomical Agents

**Archived:** 2026-02-25
**Reason:** CV Sentinel Vector V9 remediation (Dead Code)

## Background

These 20 anatomical agents (action, brain, control, learning, memory, nervous, policy, safety, sensory, services) were part of an experimental agent framework architecture. Per Aaron's directive (MEMORY.md 2026-02-21), these are **shelved** — not actively used in the current system.

## Why Archived

1. **No active imports**: `grep -r "from memory.agents"` shows zero usage in codebase
2. **Code hygiene**: Reduces confusion and maintenance overhead
3. **Complexity reduction**: CV Sentinel flagged as V9 vector (redundant agents dead code)

## Structure

- **action/**: Action planning and execution agents
- **brain/**: Central coordination and decision-making
- **control/**: Control flow and orchestration
- **learning/**: Learning and adaptation mechanisms
- **memory/**: Memory management agents
- **nervous/**: Event propagation and signaling
- **policy/**: Policy enforcement agents
- **safety/**: Safety monitoring and constraints
- **sensory/**: Input processing and perception
- **services/**: Service coordination agents
- **base.py**: Base agent classes and interfaces

## Restoration

If agent framework is needed in future:
1. Review archived code for relevant patterns
2. Assess if modern agent architecture (e.g., LangGraph, Claude agents via Task tool) is better fit
3. Cherry-pick useful patterns rather than wholesale restoration

## 3-Surgeon Consensus

**Cardiologist (GPT-4.1-mini):** "Moderate severity. Dead code can cause confusion and potential security risks but typically does not impact runtime unless accidentally invoked."

**Atlas (Claude Opus):** "Code hygiene, low urgency. Archive with clear documentation for future reference."

**Cost:** 32 Python files, ~17KB base.py, organized into 10 domain-specific subdirectories.
