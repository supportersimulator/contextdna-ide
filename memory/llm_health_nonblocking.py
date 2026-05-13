#!/usr/bin/env python3
"""
Non-Blocking Local LLM Health Monitor (mlx_lm.server)

Zero-disruption health checking using:
1. Process check (pgrep - no network)
2. Log tail monitoring (zero HTTP)
3. Heartbeat file (if server writes one)

NO HTTP requests to LLM during normal operation.
History: Was vllm-mlx (crash-looped). Now mlx_lm.server (stable).
"""

import subprocess
import time
from pathlib import Path
from typing import Tuple, Optional
import re

LLM_LOG_PATH = Path(__file__).parent.parent / "logs" / "llm_server.log"
LLM_HEARTBEAT_FILE = Path("/tmp/mlx-lm-heartbeat.txt")
LOG_FRESHNESS_THRESHOLD = 300  # 5 minutes
CACHE_TTL = 30  # Cache result for 30 seconds

# Global cache
_health_cache = {"status": None, "timestamp": 0, "reason": ""}

# Backwards compatibility alias
check_vllm_health_nonblocking = None  # Set below after function def


def check_llm_health_nonblocking() -> Tuple[bool, str]:
    """
    Check local LLM (mlx_lm.server) health without making HTTP requests.

    Returns:
        (is_healthy, reason_string)
    """
    now = time.time()

    # Return cached result if still valid
    if _health_cache["timestamp"] + CACHE_TTL > now:
        return (_health_cache["status"], _health_cache["reason"])

    # METHOD 1: Check if process is running (fastest, zero disruption)
    try:
        result = subprocess.run(
            ["pgrep", "-f", "mlx_lm"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if not result.stdout.strip():
            reason = "mlx_lm process not running"
            _update_cache(False, reason)
            return (False, reason)
    except Exception as e:
        reason = f"Process check failed: {e}"
        _update_cache(False, reason)
        return (False, reason)

    # METHOD 2: Check heartbeat file
    if LLM_HEARTBEAT_FILE.exists():
        try:
            mtime = LLM_HEARTBEAT_FILE.stat().st_mtime
            age = now - mtime
            if age < 60:
                reason = f"Heartbeat file fresh ({age:.0f}s ago)"
                _update_cache(True, reason)
                return (True, reason)
        except Exception:
            pass

    # METHOD 3: Check log file for recent activity (zero network disruption)
    if LLM_LOG_PATH.exists():
        try:
            result = subprocess.run(
                ["tail", "-50", str(LLM_LOG_PATH)],
                capture_output=True,
                text=True,
                timeout=2
            )

            log_lines = result.stdout.strip().split('\n')

            for line in reversed(log_lines):
                if "/v1/chat/completions" in line or "Chat completion:" in line:
                    reason = "Recent generation activity in logs"
                    _update_cache(True, reason)
                    return (True, reason)

            log_mtime = LLM_LOG_PATH.stat().st_mtime
            log_age = now - log_mtime

            if log_age < LOG_FRESHNESS_THRESHOLD:
                reason = f"Log file active ({log_age/60:.1f}min ago)"
                _update_cache(True, reason)
                return (True, reason)
            else:
                reason = f"Log stale ({log_age/60:.1f}min, no activity)"
                _update_cache(False, reason)
                return (False, reason)

        except Exception as e:
            reason = f"Process running (log check failed: {e})"
            _update_cache(True, reason)
            return (True, reason)

    # METHOD 4: Process is running but no logs - assume starting up
    reason = "Process running (no logs yet, likely starting)"
    _update_cache(True, reason)
    return (True, reason)


# Backwards compatibility alias (old name)
check_vllm_health_nonblocking = check_llm_health_nonblocking


def _update_cache(status: bool, reason: str):
    """Update health check cache."""
    _health_cache["status"] = status
    _health_cache["timestamp"] = time.time()
    _health_cache["reason"] = reason


def get_llm_stats() -> Optional[dict]:
    """
    Extract LLM performance stats from recent logs (zero disruption).

    Returns dict with last_generation_tokens, speed, avg_speed_recent, etc.
    """
    if not LLM_LOG_PATH.exists():
        return None

    try:
        result = subprocess.run(
            ["tail", "-100", str(LLM_LOG_PATH)],
            capture_output=True,
            text=True,
            timeout=2
        )

        log_lines = result.stdout.strip().split('\n')
        generations = []

        pattern = r"Chat completion: (\d+) tokens in ([\d.]+)s \(([\d.]+) tok/s\)"

        for line in reversed(log_lines):
            match = re.search(pattern, line)
            if match:
                generations.append({
                    "tokens": int(match.group(1)),
                    "time": float(match.group(2)),
                    "speed": float(match.group(3))
                })

        if not generations:
            return None

        recent = generations[0]
        avg_speed = sum(g["speed"] for g in generations[:5]) / min(len(generations), 5)

        return {
            "last_generation_tokens": recent["tokens"],
            "last_generation_time": recent["time"],
            "last_generation_speed": recent["speed"],
            "avg_speed_recent": avg_speed,
            "sample_count": len(generations)
        }

    except Exception:
        return None


# Backwards compatibility alias
get_vllm_stats = get_llm_stats


if __name__ == "__main__":
    is_healthy, reason = check_llm_health_nonblocking()
    print(f"LLM Health: {'Healthy' if is_healthy else 'Unhealthy'}")
    print(f"Reason: {reason}")

    stats = get_llm_stats()
    if stats:
        print(f"\nRecent Performance:")
        print(f"  Last: {stats['last_generation_tokens']} tokens in {stats['last_generation_time']:.1f}s")
        print(f"  Speed: {stats['last_generation_speed']:.1f} tok/s")
        print(f"  Average (last {stats['sample_count']}): {stats['avg_speed_recent']:.1f} tok/s")
