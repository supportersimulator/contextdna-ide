#!/usr/bin/env python3
"""
UNIFIED CONTEXT INJECTION - Single Source of Truth for All Integration Methods

This module provides a SINGLE entry point for context injection across:
- Claude Code (VS Code hooks)
- Synaptic Chat (local :8888)
- Synaptic Chat (phone via tunnel)
- Cursor, JetBrains, Vim, etc.
- Any future integration

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                    unified_injection.py                         │
│                   (Single Source of Truth)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │ get_injection() │  │ get_injection() │  │ get_injection() │ │
│  │   (Python)      │  │   (HTTP API)    │  │   (CLI)         │ │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘ │
│           │                    │                    │           │
│           └────────────────────┼────────────────────┘           │
│                                ▼                                │
│              ┌──────────────────────────────────┐               │
│              │  persistent_hook_structure.py    │               │
│              │  (9-Section Payload Engine)      │               │
│              └──────────────────────────────────┘               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

Key Features:
- Single payload generation logic (DRY principle)
- Auto-versioning for cache invalidation
- Preset configurations for different use cases
- Graceful fallback chain

Usage:
    # Python (recommended for same-process)
    from memory.unified_injection import get_injection, InjectionPreset
    result = get_injection("deploy to production", preset=InjectionPreset.FULL)

    # HTTP (for remote/phone)
    curl -X POST http://localhost:8080/contextdna/unified-inject \\
      -H "Content-Type: application/json" \\
      -d '{"prompt": "deploy to production", "preset": "full"}'

    # CLI (for shell scripts)
    python memory/unified_injection.py "deploy to production" --preset full
"""

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

# Base paths
MEMORY_DIR = Path(__file__).parent
PROJECT_ROOT = MEMORY_DIR.parent

# Version tracking for auto-sync
INJECTION_VERSION = "1.0.2"  # Bumped for SafeMode lock + Replay harness
PAYLOAD_SCHEMA_HASH = None  # Computed at module load

# =============================================================================
# SAFE MODE SYSTEM - Deterministic fallback when dependencies fail
# =============================================================================

# Latency budgets (ms) - increased for quality with Redis pre-loading
LATENCY_BUDGET_LITE = 250   # SQLite + remote LLMs (with Redis cache)
LATENCY_BUDGET_HEAVY = 500  # PostgreSQL + pgvector + Redis + local LLMs (full context)

# Explicit Safe Mode Lock — can be toggled manually via API or config
_SAFE_MODE_LOCKED = False
_SAFE_MODE_LOCK_REASON: Optional[str] = None


class SafeModeReason(Enum):
    """Reason codes for safe mode activation."""
    # 4xx - Client/Environment Issues
    RC_401 = "RC-401: Primary injection engine unavailable"
    RC_402 = "RC-402: SynapticVoice fallback failed"
    RC_403 = "RC-403: All memory systems unreachable"
    RC_404 = "RC-404: Configuration missing or invalid"
    RC_405 = "RC-405: Latency budget exceeded"
    RC_406 = "RC-406: Determinism guarantee violated"

    # 5xx - Internal System Issues
    RC_501 = "RC-501: Database connection failed"
    RC_502 = "RC-502: Redis cache unreachable"
    RC_503 = "RC-503: Vector store query timeout"
    RC_504 = "RC-504: Payload assembly error"
    RC_505 = "RC-505: Schema validation failed"


@dataclass
class SafeModeResult:
    """Result when safe mode is activated."""
    payload: str                          # Minimal safe payload
    reason: SafeModeReason                # Why safe mode activated
    original_error: str                   # Original exception message
    latency_ms: float                     # How long before failure
    injection_id: str                     # Still track for outcome linking
    payload_sha256: str                   # Hash of safe payload
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_unified_result(self) -> 'UnifiedInjectionResult':
        """Convert SafeModeResult to UnifiedInjectionResult for API compatibility."""
        return UnifiedInjectionResult(
            payload=self.payload,
            sections={"safe_mode": self.payload},
            metadata={
                "safe_mode": True,
                "safe_mode_reason": self.reason.value,
                "original_error": self.original_error,
                "latency_ms": round(self.latency_ms, 1),
                "schema_hash": PAYLOAD_SCHEMA_HASH,
                "timestamp": datetime.now().isoformat(),
            },
            preset_used="safe_mode",
            version=INJECTION_VERSION,
            injection_id=self.injection_id,
            payload_sha256=self.payload_sha256,
        )


def generate_safe_payload(reason: SafeModeReason, context_hint: str = "") -> str:
    """
    Generate minimal, deterministic, side-effect-free safe payload.

    Philosophy: When uncertain, degrade capability - don't simulate authority.
    """
    base_payload = f"""[SAFE MODE ACTIVE]
Reason: {reason.value}

ATLAS OPERATING IN DEGRADED MODE
================================
Context injection systems temporarily unavailable.
Operating with minimal safety constraints only.

CONSTRAINTS (Always Apply):
- Preserve determinism
- No side effects
- Reversible actions only
- Yield to evidence over confidence

{f"Context hint: {context_hint[:200]}" if context_hint else "No additional context available."}

When systems recover, full context injection will resume automatically.
"""
    return base_payload


def activate_safe_mode(
    reason: SafeModeReason,
    original_error: str,
    latency_ms: float,
    context_hint: str = ""
) -> SafeModeResult:
    """
    Activate safe mode with reason code and tracking.

    This is THE function to call when any injection path fails.
    Provides deterministic output regardless of failure mode.
    """
    payload = generate_safe_payload(reason, context_hint)
    payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
    injection_id = f"safe_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{payload_sha256[:8]}"

    return SafeModeResult(
        payload=payload,
        reason=reason,
        original_error=original_error[:500],  # Truncate long errors
        latency_ms=latency_ms,
        injection_id=injection_id,
        payload_sha256=payload_sha256,
        metadata={
            "safe_mode_version": "1.0.0",
            "context_hint": context_hint[:100] if context_hint else None,
        }
    )


class InjectionPreset(Enum):
    """Preset configurations for different integration types."""

    FULL = "full"              # Claude Code parity - all 9 sections
    CHAT = "chat"              # Synaptic Chat - lighter, faster
    PHONE = "phone"            # Phone access - optimized for bandwidth
    MINIMAL = "minimal"        # Safety + Foundation only
    TTS = "tts"                # TTS-optimized - voice delivery mode
    SYNAPTIC = "synaptic"      # 7B local model - focused context, real reasoning
    FAST = "fast"              # SPEED: Simple questions, minimal context, fast response
    CUSTOM = "custom"          # User-provided config


@dataclass
class PresetConfig:
    """Configuration for each preset."""
    include_safety: bool = True          # Section 0
    include_foundation: bool = True      # Section 1
    include_wisdom: bool = True          # Section 2
    include_awareness: bool = True       # Section 3
    include_deep_context: bool = True    # Section 4
    include_protocol: bool = True        # Section 5
    include_holistic: bool = True        # Section 6
    include_full_library: bool = False   # Section 7 (escalation only)
    include_8th_intelligence: bool = True  # Section 8
    max_tokens: int = 8000
    suppress_low_confidence: bool = True


# Preset configurations
PRESET_CONFIGS = {
    InjectionPreset.FULL: PresetConfig(
        include_safety=True,
        include_foundation=True,
        include_wisdom=True,
        include_awareness=True,
        include_deep_context=True,
        include_protocol=True,
        include_holistic=True,
        include_full_library=False,
        include_8th_intelligence=True,
        max_tokens=8000,
    ),
    InjectionPreset.CHAT: PresetConfig(
        include_safety=True,
        include_foundation=False,  # SOPs not needed for chat, S1 DB queries timeout
        include_wisdom=True,
        include_awareness=False,  # Skip for speed
        include_deep_context=False,  # Skip for speed
        include_protocol=True,
        include_holistic=False,  # S6 Synaptic→Atlas redundant when we ARE Synaptic
        include_full_library=False,
        include_8th_intelligence=True,
        max_tokens=4000,
    ),
    InjectionPreset.PHONE: PresetConfig(
        include_safety=True,
        include_foundation=True,
        include_wisdom=True,
        include_awareness=False,  # Skip for bandwidth
        include_deep_context=False,  # Skip for bandwidth
        include_protocol=False,  # Skip for bandwidth
        include_holistic=True,
        include_full_library=False,
        include_8th_intelligence=True,
        max_tokens=2000,  # Smaller for phone
    ),
    InjectionPreset.MINIMAL: PresetConfig(
        include_safety=True,
        include_foundation=True,
        include_wisdom=False,
        include_awareness=False,
        include_deep_context=False,
        include_protocol=False,
        include_holistic=False,
        include_full_library=False,
        include_8th_intelligence=False,
        max_tokens=1000,
    ),
    InjectionPreset.TTS: PresetConfig(
        # TTS-optimized preset for voice delivery mode
        # Minimal context for fast response, focused on conversation
        include_safety=True,           # Always include safety
        include_foundation=True,       # Core identity
        include_wisdom=False,          # Skip for latency
        include_awareness=False,       # Skip for latency
        include_deep_context=False,    # Skip for latency
        include_protocol=False,        # Skip for latency
        include_holistic=True,         # Synaptic guidance
        include_full_library=False,    # Never for TTS
        include_8th_intelligence=True, # Subconscious always on
        max_tokens=3000,               # Moderate - balance quality/speed
        suppress_low_confidence=True,  # Only confident responses
    ),
    InjectionPreset.FAST: PresetConfig(
        # SMART+FAST: Keep intelligence, minimize overhead
        # Full context stays (smart), but response is short (fast)
        include_safety=True,           # Always include safety
        include_foundation=True,       # Core identity
        include_wisdom=True,           # Professor wisdom - makes it SMART
        include_awareness=False,       # Skip for speed
        include_deep_context=False,    # Skip for speed
        include_protocol=False,        # Skip for speed
        include_holistic=True,         # Synaptic guidance - makes it SMART
        include_full_library=False,    # Never
        include_8th_intelligence=True, # Subconscious - makes it SMART
        max_tokens=2000,               # Moderate context (profile controls response length)
        suppress_low_confidence=False, # Return whatever
    ),
}


@dataclass
class UnifiedInjectionResult:
    """Result from unified injection."""
    payload: str                      # The full context string
    sections: Dict[str, str]          # Individual sections
    metadata: Dict[str, Any]          # Timing, version, etc.
    preset_used: str                  # Which preset was applied
    version: str = INJECTION_VERSION  # For cache sync
    injection_id: Optional[str] = None  # Unique ID for outcome linking
    payload_sha256: Optional[str] = None  # Hash for determinism verification


def compute_schema_hash() -> str:
    """Compute hash of payload schema for version tracking."""
    schema_elements = [
        str(PRESET_CONFIGS),
        INJECTION_VERSION,
        str(list(InjectionPreset)),
    ]
    return hashlib.md5("".join(schema_elements).encode()).hexdigest()[:8]


# Compute on module load
PAYLOAD_SCHEMA_HASH = compute_schema_hash()


def get_injection(
    prompt: str,
    preset: InjectionPreset = InjectionPreset.FULL,
    session_id: Optional[str] = None,
    custom_config: Optional[PresetConfig] = None,
    ab_variant: Optional[str] = None,
    active_file_path: Optional[str] = None,
    use_boundary_intelligence: bool = True,
) -> UnifiedInjectionResult:
    """
    Get context injection using unified single-source-of-truth.

    This is THE function all integrations should call.

    Args:
        prompt: User's prompt/message
        preset: Which preset to use (FULL, CHAT, PHONE, MINIMAL, CUSTOM)
        session_id: Optional session ID for A/B tracking
        custom_config: Required if preset is CUSTOM
        ab_variant: Force specific A/B variant
        active_file_path: Current file path for boundary intelligence
        use_boundary_intelligence: Whether to apply project boundary filtering

    Returns:
        UnifiedInjectionResult with payload, sections, and metadata
    """
    start_time = time.time()

    # ===========================================================================
    # SAFE MODE LOCK CHECK - If locked, bypass everything
    # ===========================================================================
    if _SAFE_MODE_LOCKED:
        return activate_safe_mode(
            reason=SafeModeReason.RC_404,
            original_error=f"Safe mode locked: {_SAFE_MODE_LOCK_REASON}",
            latency_ms=(time.time() - start_time) * 1000,
            context_hint="System locked via enable_safe_mode_lock()"
        ).to_unified_result()

    # ===========================================================================
    # BOUNDARY INTELLIGENCE - Project context filtering
    # ===========================================================================
    boundary_decision = None
    if use_boundary_intelligence:
        try:
            from memory.boundary_intelligence import (
                get_boundary_intelligence,
                BoundaryContext,
            )

            bi = get_boundary_intelligence(use_llm=False)  # Fast mode for injection
            boundary_context = BoundaryContext(
                user_prompt=prompt,
                active_file_path=active_file_path,
                session_id=session_id or "",
            )

            boundary_decision = bi.analyze_and_decide(boundary_context)

            # If clarification needed but we're in injection (no user interaction),
            # proceed with broad context rather than blocking
            if boundary_decision.should_clarify:
                # Log for future learning, but don't block injection
                pass

        except ImportError:
            # BoundaryIntelligence not available - proceed without filtering
            pass
        except Exception as e:
            # Non-fatal - log and continue
            import logging
            logging.getLogger(__name__).warning(f"BoundaryIntelligence failed: {e}")

    # Get config for preset
    if preset == InjectionPreset.CUSTOM:
        if custom_config is None:
            raise ValueError("custom_config required when preset is CUSTOM")
        config = custom_config
    else:
        config = PRESET_CONFIGS.get(preset, PRESET_CONFIGS[InjectionPreset.FULL])

    # Build InjectionConfig for persistent_hook_structure
    try:
        from memory.persistent_hook_structure import (
            generate_context_injection,
            InjectionConfig,
            InjectionMode,
        )

        # Map PresetConfig to InjectionConfig (different attribute names)
        injection_config = InjectionConfig(
            mode=InjectionMode.HYBRID,
            section_0_enabled=config.include_safety,      # SAFETY
            section_1_enabled=config.include_foundation,  # FOUNDATION
            section_2_enabled=config.include_wisdom,      # WISDOM
            section_3_enabled=config.include_awareness,   # AWARENESS
            section_4_enabled=config.include_deep_context,  # DEEP CONTEXT
            section_5_enabled=config.include_protocol,    # PROTOCOL
            section_6_enabled=config.include_holistic,   # HOLISTIC (Synaptic→Atlas)
            section_10_enabled=config.include_deep_context,  # VISION (requires LLM)
            sop_count=3 if config.include_foundation else 0,
            professor_depth="full" if config.include_wisdom else "one_thing_only",
            awareness_depth="full" if config.include_awareness else "none",
            verbose_protocol=not config.suppress_low_confidence,
            # Chat mode: skip slow BI LLM call + allow short prompts
            skip_boundary_intelligence=not use_boundary_intelligence,
            skip_short_prompt_bypass=not use_boundary_intelligence,
        )

        # Generate injection using canonical engine
        result = generate_context_injection(
            prompt=prompt,
            mode="hybrid",
            session_id=session_id or f"unified-{datetime.now().strftime('%H%M%S')}",
            config=injection_config,
            ab_variant=ab_variant,
        )

        elapsed_ms = (time.time() - start_time) * 1000

        # InjectionResult uses 'content' not 'full_payload'
        payload = result.content if hasattr(result, 'content') else str(result)
        sections_included = result.sections_included if hasattr(result, 'sections_included') else []
        risk_level = result.risk_level.value if hasattr(result, 'risk_level') and hasattr(result.risk_level, 'value') else str(result.risk_level) if hasattr(result, 'risk_level') else "unknown"

        # Build sections dict from sections_included list
        sections_dict = {s: "included" for s in sections_included}

        # Compute payload hash for determinism verification
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()

        # Generate unique injection ID for outcome linking
        injection_id = f"inj_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{payload_sha256[:8]}"

        # Build metadata with boundary intelligence
        metadata = {
            "latency_ms": round(elapsed_ms, 1),
            "token_estimate": len(payload) // 4,
            "risk_level": risk_level,
            "ab_variant": result.ab_variant if hasattr(result, 'ab_variant') else ab_variant,
            "first_try_likelihood": result.first_try_likelihood if hasattr(result, 'first_try_likelihood') else "unknown",
            "volume_tier": result.volume_tier if hasattr(result, 'volume_tier') else 1,
            "schema_hash": PAYLOAD_SCHEMA_HASH,
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id,
        }

        # Add per-section latency if available (actual measured, not estimated)
        section_timings = getattr(result, 'section_timings', None)
        if section_timings:
            metadata["section_timings"] = section_timings

        # Add boundary intelligence metadata if available
        if boundary_decision:
            metadata["boundary_intelligence"] = {
                "primary_project": boundary_decision.primary_project,
                "confidence": round(boundary_decision.confidence, 3),
                "confidence_level": boundary_decision.confidence_level.value,
                "action": boundary_decision.action.value,
                "filter_note": boundary_decision.filter_note,
                "decision_id": boundary_decision.decision_id,
            }

        # === TELEMETRY BRIDGES: Fire-and-forget (never block injection return) ===
        # These write to .observability.db which can lock for 20-30s under contention.
        # All three bridges are best-effort — run in background thread.
        def _fire_telemetry():
            try:
                from memory.observability_store import get_observability_store
                prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
                get_observability_store().record_injection_event(
                    injection_id=injection_id,
                    payload_sha256=payload_sha256,
                    total_latency_ms=int(elapsed_ms),
                    total_tokens=metadata.get("token_estimate", 0),
                    session_id=session_id,
                    task_type="context_injection",
                    entrypoint="unified_injection",
                    user_prompt_sha256=prompt_sha,
                    experiment_id=metadata.get("ab_variant"),
                    variant_id=metadata.get("ab_variant"),
                )
            except Exception:
                pass
            try:
                from memory.observability_store import get_observability_store
                get_observability_store().record_injection_sections(
                    injection_id=injection_id,
                    sections_included=sections_included,
                    payload=payload,
                    total_latency_ms=int(elapsed_ms),
                    section_timings=section_timings,
                )
            except Exception:
                pass
            try:
                from memory.webhook_section_notifications import (
                    record_section_health,
                    store_injection_payload,
                    get_notifier,
                )
                _LABEL_TO_SEC_ID = {
                    "safety": 0, "foundation": 1, "wisdom": 2, "awareness": 3,
                    "deep_context": 4, "protocol": 5, "synaptic_to_atlas": 6,
                    "acontext_library": 7, "synaptic_8th_intelligence": 8,
                    "vision_contextual_awareness": 10,
                }
                for label in sections_included:
                    sec_id = _LABEL_TO_SEC_ID.get(label)
                    if sec_id is not None:
                        record_section_health(
                            destination="vs_code_claude_code",
                            section_id=sec_id,
                            healthy=True,
                            phase="pre_message",
                        )
                store_injection_payload(
                    payload=payload,
                    destination="vs_code_claude_code",
                )
                notifier = get_notifier()
                notifier._store.update_destination_stats("vs_code_claude_code", success=True)
            except Exception:
                pass

        import threading
        threading.Thread(target=_fire_telemetry, daemon=True).start()

        return UnifiedInjectionResult(
            payload=payload,
            sections=sections_dict,
            metadata=metadata,
            preset_used=preset.value,
            version=INJECTION_VERSION,
            injection_id=injection_id,
            payload_sha256=payload_sha256,
        )

    except ImportError as e:
        # Fallback: Use lightweight SynapticVoice if persistent_hook_structure unavailable
        return _fallback_injection(prompt, preset, start_time, str(e), SafeModeReason.RC_401)
    except Exception as e:
        # Check if latency budget exceeded
        elapsed_ms = (time.time() - start_time) * 1000
        if elapsed_ms > LATENCY_BUDGET_HEAVY:
            safe_result = activate_safe_mode(
                reason=SafeModeReason.RC_405,
                original_error=str(e),
                latency_ms=elapsed_ms,
                context_hint=prompt[:100]
            )
            return safe_result.to_unified_result()

        # Graceful degradation with proper reason code
        return _fallback_injection(prompt, preset, start_time, str(e), SafeModeReason.RC_504)


def _fallback_injection(
    prompt: str,
    preset: InjectionPreset,
    start_time: float,
    error: str,
    primary_reason: SafeModeReason = SafeModeReason.RC_401
) -> UnifiedInjectionResult:
    """
    Fallback to lightweight SynapticVoice if main engine fails.

    Uses SafeMode system for ultimate fallback with proper reason codes.
    """
    try:
        from memory.synaptic_voice import get_voice

        # Check latency budget before attempting fallback
        elapsed_ms = (time.time() - start_time) * 1000
        if elapsed_ms > LATENCY_BUDGET_LITE:
            # Already exceeded lite budget - go directly to safe mode
            safe_result = activate_safe_mode(
                reason=SafeModeReason.RC_405,
                original_error=f"Latency {elapsed_ms:.0f}ms exceeded budget {LATENCY_BUDGET_LITE}ms",
                latency_ms=elapsed_ms,
                context_hint=prompt[:100]
            )
            return safe_result.to_unified_result()

        synaptic = get_voice()
        response = synaptic.consult(prompt)

        context_parts = []
        if response.relevant_patterns:
            context_parts.append("PATTERNS:\n" + "\n".join(
                f"  - {p[:100]}" for p in response.relevant_patterns[:3]
            ))
        if response.relevant_learnings:
            context_parts.append("LEARNINGS:\n" + "\n".join(
                f"  - {l.get('title', str(l))[:80]}" if isinstance(l, dict) else f"  - {str(l)[:80]}"
                for l in response.relevant_learnings[:3]
            ))
        if response.synaptic_perspective:
            context_parts.append(f"PERSPECTIVE:\n{response.synaptic_perspective[:300]}")

        payload = "\n\n".join(context_parts) if context_parts else "[Fallback: Limited context available]"

        elapsed_ms = (time.time() - start_time) * 1000

        # Compute payload hash for determinism verification
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        injection_id = f"inj_fallback_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{payload_sha256[:8]}"

        # === TELEMETRY BRIDGE: Persist fallback injection event (best-effort) ===
        try:
            from memory.observability_store import get_observability_store
            prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
            get_observability_store().record_injection_event(
                injection_id=injection_id,
                payload_sha256=payload_sha256,
                total_latency_ms=int(elapsed_ms),
                total_tokens=len(payload) // 4,
                task_type="fallback",
                entrypoint="unified_injection_fallback",
                user_prompt_sha256=prompt_sha,
            )
        except Exception as e:
            logging.getLogger('context_dna').debug(f'Fallback telemetry bridge failed (best-effort): {e}')

        return UnifiedInjectionResult(
            payload=payload,
            sections={"fallback": payload},
            metadata={
                "latency_ms": round(elapsed_ms, 1),
                "fallback": True,
                "fallback_reason": error,
                "primary_reason_code": primary_reason.value,
                "schema_hash": PAYLOAD_SCHEMA_HASH,
                "timestamp": datetime.now().isoformat(),
            },
            preset_used=preset.value,
            version=INJECTION_VERSION,
            injection_id=injection_id,
            payload_sha256=payload_sha256,
        )

    except Exception as e2:
        # Ultimate fallback - use SafeMode system
        elapsed_ms = (time.time() - start_time) * 1000

        # Determine appropriate reason code
        if "SynapticVoice" in str(e2) or "synaptic" in str(e2).lower():
            reason = SafeModeReason.RC_402
        elif "database" in str(e2).lower() or "sqlite" in str(e2).lower():
            reason = SafeModeReason.RC_501
        elif "redis" in str(e2).lower() or "cache" in str(e2).lower():
            reason = SafeModeReason.RC_502
        elif "timeout" in str(e2).lower() or "timed out" in str(e2).lower():
            reason = SafeModeReason.RC_503
        else:
            reason = SafeModeReason.RC_403  # All systems unreachable

        safe_result = activate_safe_mode(
            reason=reason,
            original_error=f"{error}; then {str(e2)}",
            latency_ms=elapsed_ms,
            context_hint=prompt[:100]
        )
        return safe_result.to_unified_result()


# =============================================================================
# SAFE MODE UTILITIES
# =============================================================================

def check_injection_health() -> Dict[str, Any]:
    """
    Check health of injection systems for proactive safe mode prevention.

    Returns dict with health status and recommendations.
    """
    health = {
        "healthy": True,
        "checks": {},
        "recommendations": [],
        "latency_budget": {
            "lite_ms": LATENCY_BUDGET_LITE,
            "heavy_ms": LATENCY_BUDGET_HEAVY,
        }
    }

    # Check persistent_hook_structure availability
    try:
        from memory.persistent_hook_structure import generate_context_injection
        health["checks"]["primary_engine"] = "available"
    except ImportError as e:
        health["checks"]["primary_engine"] = f"unavailable: {e}"
        health["healthy"] = False
        health["recommendations"].append("Install/fix persistent_hook_structure.py")

    # Check SynapticVoice fallback
    try:
        from memory.synaptic_voice import get_voice
        sv = get_voice()
        health["checks"]["synaptic_voice"] = "available"
    except Exception as e:
        health["checks"]["synaptic_voice"] = f"unavailable: {e}"
        health["recommendations"].append("Check SynapticVoice initialization")

    # Check database connectivity (if heavy mode)
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()
        # Quick connection test
        health["checks"]["observability_store"] = "available"
    except Exception as e:
        health["checks"]["observability_store"] = f"unavailable: {e}"
        health["recommendations"].append("Check database connectivity")

    return health


def force_safe_mode(reason: str = "Manual activation") -> SafeModeResult:
    """
    Manually force safe mode (for testing or emergency lockdown).

    Use sparingly - this bypasses all normal injection paths.
    """
    return activate_safe_mode(
        reason=SafeModeReason.RC_404,  # Configuration/manual trigger
        original_error=reason,
        latency_ms=0.0,
        context_hint="Manually activated safe mode"
    )


def enable_safe_mode_lock(reason: str = "Manual lock") -> dict:
    """
    Lock the system into safe mode. ALL injections return safe payloads
    until explicitly unlocked. Use for maintenance or crisis.

    Returns:
        Status dict with lock state
    """
    global _SAFE_MODE_LOCKED, _SAFE_MODE_LOCK_REASON
    _SAFE_MODE_LOCKED = True
    _SAFE_MODE_LOCK_REASON = reason
    return {
        "status": "locked",
        "reason": reason,
        "message": "Safe mode LOCKED. All injections will return safe payloads.",
    }


def disable_safe_mode_lock() -> dict:
    """
    Unlock safe mode. Normal injection resumes.

    Returns:
        Status dict with lock state
    """
    global _SAFE_MODE_LOCKED, _SAFE_MODE_LOCK_REASON
    prev_reason = _SAFE_MODE_LOCK_REASON
    _SAFE_MODE_LOCKED = False
    _SAFE_MODE_LOCK_REASON = None
    return {
        "status": "unlocked",
        "previous_reason": prev_reason,
        "message": "Safe mode UNLOCKED. Normal injection restored.",
    }


def get_safe_mode_status() -> dict:
    """Get current safe mode lock status."""
    return {
        "locked": _SAFE_MODE_LOCKED,
        "reason": _SAFE_MODE_LOCK_REASON,
        "latency_budget_lite_ms": LATENCY_BUDGET_LITE,
        "latency_budget_heavy_ms": LATENCY_BUDGET_HEAVY,
    }


# =============================================================================
# DETERMINISM VALIDATION SYSTEM
# =============================================================================

@dataclass
class InjectionSnapshot:
    """
    Snapshot of inputs for determinism validation.

    Captures all inputs that affect payload generation.
    """
    prompt: str
    preset: str
    session_id: Optional[str]
    ab_variant: Optional[str]
    active_file_path: Optional[str]
    use_boundary_intelligence: bool
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_hash_input(self) -> str:
        """Create deterministic string for hashing."""
        # Include only factors that SHOULD affect output
        # Exclude timestamp (non-deterministic)
        components = [
            f"prompt:{self.prompt}",
            f"preset:{self.preset}",
            f"session_id:{self.session_id or 'none'}",
            f"ab_variant:{self.ab_variant or 'none'}",
            f"file_path:{self.active_file_path or 'none'}",
            f"boundary_intel:{self.use_boundary_intelligence}",
            f"version:{INJECTION_VERSION}",
            f"schema:{PAYLOAD_SCHEMA_HASH}",
        ]
        return "|".join(components)

    def input_hash(self) -> str:
        """Generate hash of inputs for comparison."""
        return hashlib.sha256(self.to_hash_input().encode()).hexdigest()


@dataclass
class DeterminismValidationResult:
    """Result of determinism validation."""
    is_deterministic: bool
    input_hash: str
    payload_hash_1: str
    payload_hash_2: str
    match: bool
    elapsed_ms: float
    error: Optional[str] = None


class DeterminismValidator:
    """
    Validates determinism of injection pipeline.

    Philosophy: Same inputs MUST produce same payload hash.
    Violations indicate non-deterministic behavior that breaks
    outcome attribution and A/B testing integrity.
    """

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._validation_log: List[DeterminismValidationResult] = []

    def validate(
        self,
        prompt: str,
        preset: InjectionPreset = InjectionPreset.FULL,
        session_id: Optional[str] = None,
        ab_variant: Optional[str] = None,
        active_file_path: Optional[str] = None,
    ) -> DeterminismValidationResult:
        """
        Validate determinism by generating injection twice.

        Args:
            prompt: Test prompt
            preset: Injection preset
            session_id: Session ID (use fixed value for tests)
            ab_variant: A/B variant (use fixed value for tests)
            active_file_path: File path context

        Returns:
            DeterminismValidationResult with comparison details
        """
        start_time = time.time()

        # Create snapshot
        snapshot = InjectionSnapshot(
            prompt=prompt,
            preset=preset.value,
            session_id=session_id or "determinism_test",
            ab_variant=ab_variant,
            active_file_path=active_file_path,
            use_boundary_intelligence=False,  # Disable for determinism tests
        )

        try:
            # Generate first injection
            result1 = get_injection(
                prompt=prompt,
                preset=preset,
                session_id=snapshot.session_id,
                ab_variant=ab_variant,
                active_file_path=active_file_path,
                use_boundary_intelligence=False,
            )

            # Generate second injection with same inputs
            result2 = get_injection(
                prompt=prompt,
                preset=preset,
                session_id=snapshot.session_id,
                ab_variant=ab_variant,
                active_file_path=active_file_path,
                use_boundary_intelligence=False,
            )

            elapsed_ms = (time.time() - start_time) * 1000

            # Compare payload hashes
            match = result1.payload_sha256 == result2.payload_sha256

            validation_result = DeterminismValidationResult(
                is_deterministic=match,
                input_hash=snapshot.input_hash(),
                payload_hash_1=result1.payload_sha256 or "",
                payload_hash_2=result2.payload_sha256 or "",
                match=match,
                elapsed_ms=elapsed_ms,
            )

            self._validation_log.append(validation_result)

            # Log violation to observability store if not deterministic
            if not match:
                self._log_violation(snapshot, result1, result2)

            return validation_result

        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            return DeterminismValidationResult(
                is_deterministic=False,
                input_hash=snapshot.input_hash(),
                payload_hash_1="",
                payload_hash_2="",
                match=False,
                elapsed_ms=elapsed_ms,
                error=str(e),
            )

    def _log_violation(
        self,
        snapshot: InjectionSnapshot,
        result1: UnifiedInjectionResult,
        result2: UnifiedInjectionResult
    ):
        """Log determinism violation to observability store."""
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()

            store.record_determinism_violation(
                input_hash=snapshot.input_hash(),
                prompt_preview=snapshot.prompt[:100],
                preset=snapshot.preset,
                hash_1=result1.payload_sha256,
                hash_2=result2.payload_sha256,
            )
        except Exception:
            pass  # Non-fatal - don't break validation for logging failure

    def get_validation_log(self) -> List[DeterminismValidationResult]:
        """Get history of validation results."""
        return self._validation_log.copy()

    def get_success_rate(self) -> float:
        """Get percentage of deterministic validations."""
        if not self._validation_log:
            return 1.0
        successes = sum(1 for r in self._validation_log if r.is_deterministic)
        return successes / len(self._validation_log)


def replay_injection(
    snapshot: InjectionSnapshot
) -> UnifiedInjectionResult:
    """
    Replay an injection from a stored snapshot.

    Used for reproducing issues and validating determinism.

    Args:
        snapshot: InjectionSnapshot with original inputs

    Returns:
        UnifiedInjectionResult from replayed injection
    """
    return get_injection(
        prompt=snapshot.prompt,
        preset=InjectionPreset(snapshot.preset),
        session_id=snapshot.session_id,
        ab_variant=snapshot.ab_variant,
        active_file_path=snapshot.active_file_path,
        use_boundary_intelligence=snapshot.use_boundary_intelligence,
    )


def validate_determinism_quick(prompt: str = "test determinism") -> bool:
    """
    Quick determinism check for health monitoring.

    Returns True if injection is deterministic, False otherwise.
    """
    validator = DeterminismValidator()
    result = validator.validate(prompt)
    return result.is_deterministic


# =============================================================================
# HTTP API ENDPOINT (for agent_service.py to mount)
# =============================================================================

def create_injection_router():
    """Create FastAPI router for HTTP API access."""
    from fastapi import APIRouter
    from pydantic import BaseModel

    router = APIRouter(prefix="/contextdna", tags=["unified-injection"])

    class InjectionRequest(BaseModel):
        prompt: str
        preset: str = "full"
        session_id: Optional[str] = None
        ab_variant: Optional[str] = None

    class InjectionResponse(BaseModel):
        status: str
        payload: str
        sections: Dict[str, str]
        metadata: Dict[str, Any]
        preset_used: str
        version: str

    @router.post("/unified-inject", response_model=InjectionResponse)
    async def unified_inject(request: InjectionRequest):
        """Unified injection endpoint for all integrations."""
        try:
            preset = InjectionPreset(request.preset)
        except ValueError:
            preset = InjectionPreset.FULL

        result = get_injection(
            prompt=request.prompt,
            preset=preset,
            session_id=request.session_id,
            ab_variant=request.ab_variant,
        )

        return InjectionResponse(
            status="success",
            payload=result.payload,
            sections=result.sections,
            metadata=result.metadata,
            preset_used=result.preset_used,
            version=result.version,
        )

    @router.get("/unified-inject/version")
    async def get_version():
        """Get current injection version and schema hash."""
        return {
            "version": INJECTION_VERSION,
            "schema_hash": PAYLOAD_SCHEMA_HASH,
            "presets": [p.value for p in InjectionPreset],
        }

    @router.get("/unified-inject/presets")
    async def get_presets():
        """Get available presets and their configurations."""
        return {
            preset.value: asdict(config)
            for preset, config in PRESET_CONFIGS.items()
        }

    @router.get("/unified-inject/health")
    async def injection_health():
        """Check health of injection systems."""
        return check_injection_health()

    @router.get("/unified-inject/safe-mode/reasons")
    async def safe_mode_reasons():
        """List all safe mode reason codes."""
        return {
            reason.name: reason.value
            for reason in SafeModeReason
        }

    @router.post("/unified-inject/safe-mode/test")
    async def test_safe_mode():
        """
        Test safe mode activation (for debugging/validation).

        Returns what a safe mode response would look like.
        """
        test_result = force_safe_mode("API test activation")
        return {
            "status": "safe_mode_test",
            "payload": test_result.payload,
            "reason": test_result.reason.value,
            "injection_id": test_result.injection_id,
            "payload_sha256": test_result.payload_sha256,
        }

    @router.get("/unified-inject/latency-budgets")
    async def latency_budgets():
        """Get latency budget thresholds for lite/heavy modes."""
        return {
            "lite_mode_ms": LATENCY_BUDGET_LITE,
            "heavy_mode_ms": LATENCY_BUDGET_HEAVY,
            "description": {
                "lite": "SQLite + remote LLMs (≤16GB RAM)",
                "heavy": "PostgreSQL + pgvector + Redis + local LLMs (≥24GB RAM)",
            }
        }

    @router.post("/unified-inject/determinism/validate")
    async def validate_determinism(prompt: str = "test determinism"):
        """
        Validate injection determinism by generating twice and comparing.

        Returns validation result with hash comparison.
        """
        validator = DeterminismValidator()
        result = validator.validate(prompt)
        return {
            "is_deterministic": result.is_deterministic,
            "input_hash": result.input_hash,
            "payload_hash_1": result.payload_hash_1,
            "payload_hash_2": result.payload_hash_2,
            "match": result.match,
            "elapsed_ms": result.elapsed_ms,
            "error": result.error,
        }

    @router.get("/unified-inject/determinism/quick-check")
    async def determinism_quick_check():
        """Quick determinism health check."""
        is_deterministic = validate_determinism_quick()
        return {
            "is_deterministic": is_deterministic,
            "status": "healthy" if is_deterministic else "VIOLATION",
        }

    # ===================================================================
    # SAFE MODE LOCK ENDPOINTS
    # ===================================================================

    @router.get("/unified-inject/safe-mode/status")
    async def safe_mode_status():
        """Get current safe mode lock status."""
        return get_safe_mode_status()

    @router.post("/unified-inject/safe-mode/enable")
    async def safe_mode_enable(reason: str = "Manual API lock"):
        """Lock system into safe mode. All injections return safe payloads."""
        return enable_safe_mode_lock(reason)

    @router.post("/unified-inject/safe-mode/disable")
    async def safe_mode_disable():
        """Unlock safe mode. Normal injection resumes."""
        return disable_safe_mode_lock()

    # ===================================================================
    # REPLAY HARNESS ENDPOINTS
    # ===================================================================

    class ReplayRequest(BaseModel):
        prompt: str
        preset: str = "full"
        session_id: Optional[str] = None
        ab_variant: Optional[str] = None
        active_file_path: Optional[str] = None

    @router.post("/unified-inject/replay")
    async def replay_inject(request: ReplayRequest):
        """
        Replay an injection with specified inputs.

        Used for reproducing issues, debugging non-determinism,
        and validating that fixes produce expected payloads.

        Returns both the result and a determinism comparison
        against a second run with identical inputs.
        """
        snapshot = InjectionSnapshot(
            prompt=request.prompt,
            preset=request.preset,
            session_id=request.session_id or "",
            ab_variant=request.ab_variant or "control",
            active_file_path=request.active_file_path or "",
            use_boundary_intelligence=True,
        )

        # Run the replay
        result = replay_injection(snapshot)

        # Run determinism check on same inputs
        validator = DeterminismValidator()
        det_result = validator.validate(request.prompt)

        return {
            "replay": {
                "status": "success" if result.payload else "empty",
                "payload_sha256": result.payload_sha256,
                "preset_used": result.preset_used,
                "injection_id": result.injection_id,
                "sections": list(result.sections.keys()),
                "payload_length": len(result.payload) if result.payload else 0,
            },
            "determinism": {
                "is_deterministic": det_result.is_deterministic,
                "hash_1": det_result.payload_hash_1,
                "hash_2": det_result.payload_hash_2,
                "match": det_result.match,
            },
            "snapshot": {
                "input_hash": snapshot.input_hash(),
                "prompt_preview": request.prompt[:100],
                "preset": request.preset,
            },
        }

    return router


# =============================================================================
# CLI INTERFACE
# =============================================================================

def main():
    """CLI interface for unified injection."""
    import argparse

    parser = argparse.ArgumentParser(description="Unified Context Injection")
    parser.add_argument("prompt", help="The prompt to inject context for")
    parser.add_argument(
        "--preset", "-p",
        choices=["full", "chat", "phone", "minimal"],
        default="full",
        help="Injection preset (default: full)"
    )
    parser.add_argument(
        "--session-id", "-s",
        help="Session ID for A/B tracking"
    )
    parser.add_argument(
        "--format", "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )
    parser.add_argument(
        "--version", "-v",
        action="store_true",
        help="Show version and exit"
    )

    args = parser.parse_args()

    if args.version:
        print(f"Unified Injection v{INJECTION_VERSION}")
        print(f"Schema Hash: {PAYLOAD_SCHEMA_HASH}")
        sys.exit(0)

    preset = InjectionPreset(args.preset)
    result = get_injection(
        prompt=args.prompt,
        preset=preset,
        session_id=args.session_id,
    )

    if args.format == "json":
        output = {
            "payload": result.payload,
            "sections": result.sections,
            "metadata": result.metadata,
            "preset_used": result.preset_used,
            "version": result.version,
        }
        print(json.dumps(output, indent=2))
    else:
        print(result.payload)


if __name__ == "__main__":
    main()
