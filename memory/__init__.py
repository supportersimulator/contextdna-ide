"""contextdna-ide-oss migrate3 memory package.

Hosts the restored modules that are progressively migrated from the legacy
`memory/` tree into the OSS distribution.  Currently includes:

- ``anticipation_engine``       — real-time MLX probe, predictive layer
- ``failure_pattern_analyzer``  — learns from anticipation misses
- ``observability_store``       — session/workflow audit trail (Synaptic critical)
- ``learning_store``            — long-term learning history
- ``sqlite_storage``            — lightweight key/value backing store
- ``synaptic_voice``            — voice rendering for S8 8th Intelligence
"""

__all__ = [
    "anticipation_engine",
    "failure_pattern_analyzer",
    "learning_store",
    "observability_store",
    "sqlite_storage",
    "synaptic_voice",
]
