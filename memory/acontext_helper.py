"""
DEPRECATED: Use context_dna_client.py instead

This file is kept for backwards compatibility.
All functionality has been moved to context_dna_client.py

Import aliases:
    from memory.acontext_helper import AcontextMemory  # Old way (still works)
    from memory.context_dna_client import ContextDNAClient  # New way (preferred)
"""

# Re-export from new module for backwards compatibility
try:
    from memory.context_dna_client import (
        ContextDNAClient,
        CONTEXT_DNA_AVAILABLE,
        CONTEXT_DNA_BASE_URL,
        CONTEXT_DNA_API_KEY,
        CONTEXT_DNA_SPACE_ID,
    )

    # Backwards-compatible aliases
    AcontextMemory = ContextDNAClient
    ACONTEXT_AVAILABLE = CONTEXT_DNA_AVAILABLE
    ACONTEXT_BASE_URL = CONTEXT_DNA_BASE_URL
    ACONTEXT_API_KEY = CONTEXT_DNA_API_KEY
    ACONTEXT_SPACE_ID = CONTEXT_DNA_SPACE_ID

except ImportError:
    # Fallback if context_dna_client not available
    import os

    ACONTEXT_AVAILABLE = False
    CONTEXT_DNA_AVAILABLE = False
    ACONTEXT_BASE_URL = os.environ.get("ACONTEXT_BASE_URL", "http://localhost:8029/api/v1")
    ACONTEXT_API_KEY = os.environ.get("ACONTEXT_API_KEY", "sk-ac-your-root-api-bearer-token")
    ACONTEXT_SPACE_ID = os.environ.get("ACONTEXT_SPACE_ID", "")

    # Aliases
    CONTEXT_DNA_BASE_URL = ACONTEXT_BASE_URL
    CONTEXT_DNA_API_KEY = ACONTEXT_API_KEY
    CONTEXT_DNA_SPACE_ID = ACONTEXT_SPACE_ID

    class AcontextMemory:
        """Stub class when context_dna_client not available"""
        def __init__(self):
            print("Warning: context_dna_client.py not found, using stub")

        def record_bug_fix(self, *args, **kwargs):
            pass

        def record_architecture_decision(self, *args, **kwargs):
            pass

        def record_performance_lesson(self, *args, **kwargs):
            pass

        def get_relevant_learnings(self, *args, **kwargs):
            return []

        def get_prompt_context(self, *args, **kwargs):
            return ""

    ContextDNAClient = AcontextMemory

# For backwards compatibility, both names work
__all__ = [
    "AcontextMemory",
    "ContextDNAClient",
    "ACONTEXT_AVAILABLE",
    "ACONTEXT_BASE_URL",
    "ACONTEXT_API_KEY",
    "ACONTEXT_SPACE_ID",
    "CONTEXT_DNA_AVAILABLE",
    "CONTEXT_DNA_BASE_URL",
    "CONTEXT_DNA_API_KEY",
    "CONTEXT_DNA_SPACE_ID",
]
