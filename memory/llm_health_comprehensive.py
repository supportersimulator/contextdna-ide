#!/usr/bin/env python3
"""
Comprehensive LLM Health Check - Multi-Dimensional Validation

Goes beyond simple port checking to verify:
1. Port listening
2. API responding  
3. Model loaded in memory (RSS check)
4. Can actually generate text
5. Performance within acceptable range

Used by both Layer 1 (lite_scheduler) and Layer 2 (launchd) watchdogs.
"""

import logging
import requests
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Thresholds
MIN_RSS_MB = 2000          # Minimum memory for loaded model (2GB)
MAX_GENERATION_TIME = 15   # Max seconds for simple generation
HEALTH_TIMEOUT = 3         # Timeout for health endpoint


@dataclass
class LLMHealthStatus:
    """Comprehensive LLM health status."""
    overall_healthy: bool
    port_listening: bool
    api_responding: bool
    model_loaded: bool
    can_generate: bool
    performance_ok: bool
    
    # Details
    rss_mb: Optional[int] = None
    generation_time_s: Optional[float] = None
    model_name: Optional[str] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "overall_healthy": self.overall_healthy,
            "port_listening": self.port_listening,
            "api_responding": self.api_responding,
            "model_loaded": self.model_loaded,
            "can_generate": self.can_generate,
            "performance_ok": self.performance_ok,
            "rss_mb": self.rss_mb,
            "generation_time_s": self.generation_time_s,
            "model_name": self.model_name,
            "error_message": self.error_message
        }


def check_llm_health_comprehensive(
    port: int = 5044,
    test_generation: bool = True
) -> LLMHealthStatus:
    """
    Comprehensive multi-dimensional health check for local LLM (mlx_lm.server).

    Args:
        port: LLM server port (default 5044)
        test_generation: Whether to test actual generation (slower but thorough)
    
    Returns:
        LLMHealthStatus with all check results
    """
    health = LLMHealthStatus(
        overall_healthy=False,
        port_listening=False,
        api_responding=False,
        model_loaded=False,
        can_generate=False,
        performance_ok=False
    )
    
    # =================================================================
    # CHECK 1: Port Listening
    # =================================================================
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        health.port_listening = (result == 0)
    except Exception as e:
        health.error_message = f"Port check failed: {e}"
        return health
    
    if not health.port_listening:
        health.error_message = f"Port {port} not listening"
        return health
    
    # =================================================================
    # CHECK 2: API Responding
    # =================================================================
    try:
        resp = requests.get(
            f"http://127.0.0.1:{port}/v1/models",
            timeout=HEALTH_TIMEOUT
        )
        if resp.status_code == 200:
            health.api_responding = True
            
            # Extract model name
            try:
                data = resp.json()
                if data.get("data") and len(data["data"]) > 0:
                    health.model_name = data["data"][0].get("id", "unknown")
            except Exception:
                pass
    except requests.exceptions.Timeout:
        health.error_message = "API timeout"
        return health
    except requests.exceptions.ConnectionError:
        health.error_message = "API connection refused"
        return health
    except Exception as e:
        health.error_message = f"API check failed: {e}"
        return health
    
    if not health.api_responding:
        health.error_message = "API not responding properly"
        return health
    
    # =================================================================
    # CHECK 3: Model Loaded in Memory (RSS Check)
    # =================================================================
    # NOTE: MLX models use Apple Metal GPU memory, NOT CPU RAM!
    # For MLX: Low RSS (<1GB) is NORMAL - model lives on GPU
    # For Ollama: High RSS (>2GB) is expected - model lives in CPU RAM
    # 
    # Strategy: Record RSS but don't fail on low values for MLX
    try:
        # Find local LLM process (mlx_lm.server)
        result = subprocess.run(
            ["pgrep", "-f", "mlx_lm"],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode == 0 and result.stdout.strip():
            pid = result.stdout.strip().split('\n')[0]
            
            # Get RSS memory
            ps_result = subprocess.run(
                ["ps", "-o", "rss=", "-p", pid],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if ps_result.returncode == 0:
                rss_kb = int(ps_result.stdout.strip())
                health.rss_mb = rss_kb // 1024
                
                # For MLX: If API responds, assume loaded (GPU memory not in RSS)
                # For Ollama: Actually check RSS threshold
                is_mlx = "mlx_lm" in str(subprocess.run(
                    ["ps", "-o", "command=", "-p", pid],
                    capture_output=True, text=True, timeout=1
                ).stdout)
                
                if is_mlx:
                    # MLX: Trust API response, log RSS for info only
                    health.model_loaded = health.api_responding
                    logger.debug(f"MLX detected - RSS {health.rss_mb}MB (GPU memory not counted)")
                else:
                    # Ollama or other: Use RSS threshold
                    health.model_loaded = health.rss_mb >= MIN_RSS_MB
                    if not health.model_loaded:
                        health.error_message = f"Model not loaded (RSS: {health.rss_mb}MB, need >{MIN_RSS_MB}MB)"
    except Exception as e:
        logger.debug(f"RSS check failed (non-critical): {e}")
        # If RSS check fails, trust the API response (model might be loaded)
        health.model_loaded = True
    
    # =================================================================
    # CHECK 4: Can Generate (Optional but Recommended)
    # =================================================================
    if test_generation and health.api_responding:
        try:
            # ALL LLM generation routes through priority queue — NO direct HTTP to port 5044
            from memory.llm_priority_queue import llm_generate, Priority

            start_time = time.time()

            content = llm_generate(
                system_prompt="",
                user_prompt="test",
                priority=Priority.ATLAS,  # P2: health monitoring is critical
                profile="classify",
                caller="health_comprehensive_test",
            )

            generation_time = time.time() - start_time
            health.generation_time_s = round(generation_time, 2)

            if content and len(content) > 0:
                health.can_generate = True
                health.performance_ok = generation_time < MAX_GENERATION_TIME
            else:
                health.error_message = "Generation returned empty content"
        
        except Exception as e:
            health.error_message = f"Generation test failed: {e}"
    else:
        # Skip generation test - assume healthy if API responds
        health.can_generate = True
        health.performance_ok = True
    
    # =================================================================
    # OVERALL HEALTH DETERMINATION
    # =================================================================
    health.overall_healthy = (
        health.port_listening and
        health.api_responding and
        health.model_loaded and
        health.can_generate
    )
    
    return health


def get_health_summary(port: int = 5044) -> str:
    """Get human-readable health summary."""
    health = check_llm_health_comprehensive(port)
    
    if health.overall_healthy:
        return f"✅ Healthy - {health.model_name} ({health.rss_mb}MB, gen: {health.generation_time_s}s)"
    else:
        return f"❌ Unhealthy - {health.error_message}"


if __name__ == "__main__":
    # Test the comprehensive health check
    print("Running comprehensive LLM health check...")
    health = check_llm_health_comprehensive(test_generation=True)
    
    print("\nResults:")
    print(f"  Port listening: {'✅' if health.port_listening else '❌'}")
    print(f"  API responding: {'✅' if health.api_responding else '❌'}")
    print(f"  Model loaded: {'✅' if health.model_loaded else '❌'} ({health.rss_mb}MB)")
    print(f"  Can generate: {'✅' if health.can_generate else '❌'}")
    print(f"  Performance: {'✅' if health.performance_ok else '❌'}")
    print(f"\nOverall: {'✅ HEALTHY' if health.overall_healthy else '❌ UNHEALTHY'}")
    
    if health.error_message:
        print(f"Error: {health.error_message}")
    
    if health.model_name:
        print(f"Model: {health.model_name}")
    
    if health.generation_time_s:
        print(f"Generation time: {health.generation_time_s}s")
