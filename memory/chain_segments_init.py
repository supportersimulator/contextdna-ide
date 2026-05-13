"""Auto-import all segment modules to trigger @segment registration.

Import this module to register all available chain segments.
"""
# Core segments: pre-flight, verify, gains-gate
import memory.chain_segments_core  # noqa: F401

# Analysis segments: risk-scan, contradiction-scan
import memory.chain_segments_analysis  # noqa: F401

# Audit segments: audit-discover, audit-read, audit-extract, audit-crosscheck, audit-report
import memory.chain_segments_audit  # noqa: F401

# Review segments: plan-review, pre-impl
import memory.chain_segments_review  # noqa: F401
