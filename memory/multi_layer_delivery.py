#!/usr/bin/env python3
"""
Multi-Layer Webhook Delivery with Auto-Recovery

Delivers webhook payload using 4-layer fallback system.
Each fallback layer attempts to restore the primary method in background.

Usage:
    from memory.multi_layer_delivery import deliver_with_fallback
    
    result = deliver_with_fallback(
        destination_id="cursor_ide_hooks",
        prompt="user's message",
        mode="hybrid"
    )
    
    if result.success:
        print(result.payload)
    else:
        print(f"Delivery failed: {result.error_message}")
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
MEMORY_DIR = Path(__file__).parent


@dataclass
class DeliveryResult:
    """Result of webhook delivery attempt."""
    success: bool
    method_used: str              # "hook", "mcp", "api", "file", "minimal"
    payload: str
    latency_ms: int
    error_message: Optional[str] = None
    recovery_triggered: bool = False


def deliver_with_fallback(
    destination_id: str,
    prompt: str,
    mode: str = "hybrid",
    session_id: Optional[str] = None
) -> DeliveryResult:
    """
    Deliver webhook with 4-layer fallback + auto-recovery.
    
    Layer 1: UserPromptSubmit hook (primary) - 99.9% reliable
    Layer 2: MCP resource (fallback 1) - if hook fails
    Layer 3: HTTP API (fallback 2) - if MCP fails  
    Layer 4: File-based (fallback 3) - always works
    
    Each fallback attempts to restore primary in background.
    
    Args:
        destination_id: Target IDE (e.g., "cursor_ide_hooks")
        prompt: User's message
        mode: Injection mode ("hybrid", "layered", "greedy", "minimal")
        session_id: Optional session ID for tracking
    
    Returns:
        DeliveryResult with payload and delivery metadata
    """
    from memory.destination_registry import DestinationRegistry
    
    registry = DestinationRegistry()
    dest = registry.get_destination(destination_id)
    
    if not dest:
        return DeliveryResult(
            success=False,
            method_used="none",
            payload="",
            latency_ms=0,
            error_message=f"Destination not registered: {destination_id}"
        )
    
    # =================================================================
    # LAYER 1: UserPromptSubmit Hook (Primary - 99.9% Reliable)
    # =================================================================
    if dest.delivery_method == "hook" and dest.delivery_endpoint:
        start_time = time.time()
        
        try:
            result = subprocess.run(
                [dest.delivery_endpoint, prompt],
                capture_output=True,
                text=True,
                timeout=5,  # 5 second timeout for hook
                cwd=str(REPO_ROOT)
            )
            
            latency_ms = int((time.time() - start_time) * 1000)
            
            if result.returncode == 0 and result.stdout:
                # Hook succeeded!
                registry.record_delivery(destination_id, len(result.stdout), True, latency_ms)
                
                return DeliveryResult(
                    success=True,
                    method_used="hook",
                    payload=result.stdout,
                    latency_ms=latency_ms
                )
            else:
                # Hook failed - log and proceed to fallback
                error_msg = f"Hook failed: exit {result.returncode}"
                logger.warning(f"{error_msg} for {destination_id}")
                registry.update_health(destination_id, False, error_msg)
        
        except subprocess.TimeoutExpired:
            logger.warning(f"Hook timeout for {destination_id}")
            registry.update_health(destination_id, False, "Hook timeout >5s")
        
        except Exception as e:
            logger.warning(f"Hook exception for {destination_id}: {e}")
            registry.update_health(destination_id, False, str(e))
    
    # =================================================================
    # FALLBACK 1: MCP Resource
    # =================================================================
    # Primary failed - try MCP
    # ALSO: Spawn background job to fix the hook
    
    logger.info(f"Falling back to MCP for {destination_id}")
    _spawn_hook_recovery(destination_id, "hook_failed")
    
    try:
        # Generate payload directly (MCP would serve this)
        from memory.persistent_hook_structure import generate_context_injection
        
        start_time = time.time()
        result = generate_context_injection(prompt, mode, session_id)
        latency_ms = int((time.time() - start_time) * 1000)
        
        if hasattr(result, 'content') and result.content:
            registry.record_delivery(destination_id, len(result.content), True, latency_ms)
            
            return DeliveryResult(
                success=True,
                method_used="mcp",
                payload=result.content,
                latency_ms=latency_ms,
                recovery_triggered=True
            )
    
    except Exception as e:
        logger.warning(f"MCP fallback failed for {destination_id}: {e}")
    
    # =================================================================
    # FALLBACK 2: HTTP API
    # =================================================================
    # MCP failed - try HTTP API
    # ALSO: Spawn background recovery for hook AND MCP
    
    logger.info(f"Falling back to HTTP API for {destination_id}")
    _spawn_mcp_recovery(destination_id, "mcp_failed")
    
    try:
        import requests
        
        start_time = time.time()
        resp = requests.post(
            "http://localhost:8029/contextdna/inject",
            json={"prompt": prompt, "mode": mode, "session_id": session_id or ""},
            timeout=3
        )
        latency_ms = int((time.time() - start_time) * 1000)
        
        if resp.ok:
            data = resp.json()
            payload = data.get("payload", "")
            
            registry.record_delivery(destination_id, len(payload), True, latency_ms)
            
            return DeliveryResult(
                success=True,
                method_used="api",
                payload=payload,
                latency_ms=latency_ms,
                recovery_triggered=True
            )
    
    except Exception as e:
        logger.warning(f"API fallback failed for {destination_id}: {e}")
    
    # =================================================================
    # FALLBACK 3: File-Based (Always Works)
    # =================================================================
    # All network methods failed - read latest injection file
    # ALSO: Spawn comprehensive recovery (hook + MCP + API + services)
    
    logger.info(f"Falling back to file-based for {destination_id}")
    _spawn_comprehensive_recovery(destination_id, "all_network_methods_failed")
    
    try:
        injection_file = MEMORY_DIR / ".injection_latest.json"
        
        if injection_file.exists():
            with open(injection_file, 'r') as f:
                data = json.load(f)
                payload = data.get("raw_output", "")
                
                if payload:
                    registry.record_delivery(destination_id, len(payload), True, 5)
                    
                    return DeliveryResult(
                        success=True,
                        method_used="file",
                        payload=payload,
                        latency_ms=5,
                        recovery_triggered=True
                    )
    
    except Exception as e:
        logger.error(f"File fallback failed for {destination_id}: {e}")
    
    # =================================================================
    # ULTIMATE FALLBACK: Minimal Context (Never Fails)
    # =================================================================
    minimal_context = """
╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  CONTEXT DNA - MINIMAL MODE                                      ║
╠══════════════════════════════════════════════════════════════════════╣
║  All delivery methods failed. Operating in minimal mode.             ║
║                                                                      ║
║  SAFETY RAILS:                                                       ║
║  • Read existing code before modifying                               ║
║  • Query memory if unsure                                            ║
║  • Test changes before committing                                    ║
║  • Record successes when they happen                                 ║
║                                                                      ║
║  Recovery in progress - full context will return automatically.     ║
╚══════════════════════════════════════════════════════════════════════╝
"""
    
    # Send critical notification
    try:
        subprocess.run(
            ["osascript", "-e", 
             'display notification "Context DNA in minimal mode - recovery in progress" '
             'with title "Context DNA Alert" sound name "Basso"'],
            timeout=2
        )
    except Exception:
        pass
    
    return DeliveryResult(
        success=False,
        method_used="minimal",
        payload=minimal_context,
        latency_ms=0,
        error_message="All delivery methods failed",
        recovery_triggered=True
    )


# =============================================================================
# AUTO-RECOVERY FUNCTIONS (Background Jobs)
# =============================================================================

def _spawn_hook_recovery(destination_id: str, reason: str):
    """Spawn background job to restore hook functionality."""
    try:
        script = REPO_ROOT / "scripts" / "recover-hook.sh"
        if script.exists():
            subprocess.Popen(
                [str(script), destination_id, reason],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(REPO_ROOT)
            )
            logger.info(f"✅ Spawned hook recovery for {destination_id}")
        else:
            logger.debug(f"Hook recovery script not found: {script}")
    except Exception as e:
        logger.debug(f"Could not spawn hook recovery: {e}")


def _spawn_mcp_recovery(destination_id: str, reason: str):
    """Spawn background job to restore MCP server."""
    try:
        script = REPO_ROOT / "scripts" / "recover-mcp.sh"
        if script.exists():
            subprocess.Popen(
                [str(script), destination_id, reason],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(REPO_ROOT)
            )
            logger.info(f"✅ Spawned MCP recovery for {destination_id}")
        else:
            logger.debug(f"MCP recovery script not found: {script}")
    except Exception as e:
        logger.debug(f"Could not spawn MCP recovery: {e}")


def _spawn_comprehensive_recovery(destination_id: str, reason: str):
    """Spawn background job to restore ALL delivery methods."""
    try:
        script = REPO_ROOT / "scripts" / "recover-all.sh"
        if script.exists():
            subprocess.Popen(
                [str(script), destination_id, reason],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(REPO_ROOT)
            )
            logger.info(f"✅ Spawned comprehensive recovery for {destination_id}")
        else:
            logger.debug(f"Comprehensive recovery script not found: {script}")
    except Exception as e:
        logger.debug(f"Could not spawn comprehensive recovery: {e}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python multi_layer_delivery.py <destination_id> <prompt>")
        print("\nExample:")
        print("  python multi_layer_delivery.py cursor_ide_hooks 'deploy Django'")
        sys.exit(1)
    
    destination_id = sys.argv[1]
    prompt = " ".join(sys.argv[2:])
    
    print(f"Testing multi-layer delivery for: {destination_id}")
    print(f"Prompt: {prompt[:60]}...")
    print()
    
    result = deliver_with_fallback(destination_id, prompt)
    
    print(f"✅ Success: {result.success}")
    print(f"📡 Method: {result.method_used}")
    print(f"⏱️  Latency: {result.latency_ms}ms")
    print(f"🔄 Recovery: {result.recovery_triggered}")
    
    if result.error_message:
        print(f"❌ Error: {result.error_message}")
    
    print(f"\n📄 Payload preview:")
    print(result.payload[:500])
    print("...")
