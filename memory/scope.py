"""Canonical MemoryScope constants — mirrors context-dna/engine/types/memory.ts"""

CORE = 'core'
PROFILE = 'profile'
WORKSPACE = 'workspace'
SESSION = 'session'
TASK = 'task'

CANONICAL = frozenset({CORE, PROFILE, WORKSPACE, SESSION, TASK})
LEGACY_MAP = {'global': CORE, 'project': WORKSPACE}


def normalize(scope: str) -> str | None:
    """Normalize any scope (canonical or legacy) to canonical form. Returns None for invalid."""
    if scope in CANONICAL:
        return scope
    return LEGACY_MAP.get(scope)
