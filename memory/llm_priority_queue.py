#!/usr/bin/env python3
"""
LLM PRIORITY QUEUE — Ensures Aaron > Atlas > External > Background

The local LLM (mlx_lm.server on port 5044) serves ONE request at a time.
This queue ensures priority callers don't wait behind background butler tasks.

Priority Levels:
  P1 (AARON):      Aaron's direct queries via Synaptic chat
  P2 (ATLAS):      Atlas webhook/injection LLM calls
  P3 (EXTERNAL):   External integrations (phone bridge, etc.)
  P4 (BACKGROUND): Butler background tasks (hindsight, failure analysis, etc.)

Architecture:
  - Thread-safe priority queue with asyncio support
  - Higher priority requests preempt lower priority waiting requests
  - Requests carry a priority level and get served in order
  - Background tasks yield immediately if a higher-priority request arrives

Usage:
    from memory.llm_priority_queue import llm_generate, Priority

    # Aaron's query (highest priority)
    result = llm_generate("system prompt", "user prompt", priority=Priority.AARON)

    # Butler background task (lowest priority)
    result = llm_generate("system prompt", "analyze this", priority=Priority.BACKGROUND)

Created: February 6, 2026
Purpose: Fair LLM access with Aaron-first priority
"""

import enum
import hashlib
import json
import logging
import os
import re
import sys as _sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from queue import PriorityQueue
from typing import Optional, Tuple

import requests

logger = logging.getLogger("context_dna.llm_queue")


# =============================================================================
# ZERO SILENT FAILURES (INVARIANT)
# =============================================================================
# Per CLAUDE.md: every exception must be observable. The block below tracks
# every previously-silent except-pass site as a named counter, mirrors them to
# stderr in a grep-friendly format, and writes one-shot module-availability
# warnings (e.g. "redis not installed in this python") so daemon /metrics +
# /health can surface the failure instead of silently dropping telemetry.
#
# Counter naming convention:
#   queue_<func>_module_missing  — `import <pkg>` raised ModuleNotFoundError
#   queue_<func>_errors          — any other exception in <func>
#
# Counters live in `_zsf_counters`; daemon's _build_prometheus_metrics scrapes
# them via `get_zsf_counters()` (whitelisted in tools/fleet_nerve_nats.py).
# =============================================================================

_zsf_lock = threading.Lock()
_zsf_counters: dict = {
    # Publish path (was bare-pass at 1263, swallowed ModuleNotFoundError: redis)
    "queue_publish_errors": 0,
    "queue_publish_module_missing": 0,
    # Health publisher (was bare-pass at 94)
    "queue_health_publish_errors": 0,
    "queue_health_publish_module_missing": 0,
    # Misc telemetry / cache / lock helpers
    "queue_lock_telemetry_errors": 0,
    "queue_lock_release_errors": 0,
    "queue_file_lock_errors": 0,
    "queue_external_cache_errors": 0,
    "queue_external_cost_errors": 0,
    "queue_fallback_event_errors": 0,
    "queue_dotenv_errors": 0,
    "queue_hybrid_mode_check_errors": 0,
    "queue_urgent_yield_errors": 0,
    "queue_worker_get_timeouts": 0,  # benign queue.Empty after 30s — kept for shape
    "queue_redis_avail_check_errors": 0,
    "queue_redis_health_check_errors": 0,
    "queue_publish_module_warned": 0,  # bool-as-int: 1 once we've warned
    "queue_health_module_warned": 0,
}

# One-shot guard so we only emit "[ZSF] redis module unavailable" once per
# process regardless of how many publish attempts get rejected.
_redis_publish_available: Optional[bool] = None  # None = unknown, True/False after probe
_redis_publish_warn_lock = threading.Lock()

_ZSF_LOG_PATH = "/tmp/llm-queue-errors.log"


def _zsf_record(counter: str, func: str, exc: BaseException,
                critical: bool = False) -> None:
    """Record a previously-silent failure: bump counter + stderr line + (optional) log file.

    Args:
        counter: Counter name in `_zsf_counters` to increment.
        func: Calling function name (for the [ZSF] log line).
        exc: The exception we caught.
        critical: When True, also append a timestamped line to /tmp/llm-queue-errors.log.
                  Reserve for missing-module failures that take a whole feature down
                  (e.g. redis missing → all queue stats unwritten).
    """
    try:
        with _zsf_lock:
            _zsf_counters[counter] = _zsf_counters.get(counter, 0) + 1
    except Exception:
        # Counter bookkeeping itself must never raise — fall through to stderr.
        pass

    msg = f"[ZSF] llm_priority_queue.{func}: {type(exc).__name__}: {str(exc)[:200]}"
    try:
        print(msg, file=_sys.stderr, flush=True)
    except Exception:
        pass

    if critical:
        try:
            ts = datetime.utcnow().isoformat()
            with open(_ZSF_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"{ts}Z {msg}\n")
        except Exception:
            # Last-resort: bump a meta-counter so even log-write failures are visible
            try:
                with _zsf_lock:
                    _zsf_counters["queue_zsf_log_write_errors"] = (
                        _zsf_counters.get("queue_zsf_log_write_errors", 0) + 1
                    )
            except Exception:
                pass


def _redis_publish_probe() -> bool:
    """Probe (once per process) whether `import redis` works for the publish path.

    Sets _redis_publish_available + emits a one-shot stderr warning when
    redis is missing. Returns True iff publishing should be attempted.
    Subsequent calls are cache-fast (no re-import).
    """
    global _redis_publish_available
    if _redis_publish_available is not None:
        return _redis_publish_available
    with _redis_publish_warn_lock:
        if _redis_publish_available is not None:
            return _redis_publish_available
        try:
            import redis  # noqa: F401
            _redis_publish_available = True
        except ModuleNotFoundError as e:
            _redis_publish_available = False
            with _zsf_lock:
                _zsf_counters["queue_publish_module_missing"] = (
                    _zsf_counters.get("queue_publish_module_missing", 0) + 1
                )
                _zsf_counters["queue_health_publish_module_missing"] = (
                    _zsf_counters.get("queue_health_publish_module_missing", 0) + 1
                )
                _zsf_counters["queue_publish_module_warned"] = 1
                _zsf_counters["queue_health_module_warned"] = 1
            warn = (
                f"[ZSF] redis module unavailable in {_sys.executable}; "
                f"queue_stats publishing disabled ({type(e).__name__}: {e})"
            )
            try:
                print(warn, file=_sys.stderr, flush=True)
            except Exception:
                pass
            try:
                ts = datetime.utcnow().isoformat()
                with open(_ZSF_LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(f"{ts}Z {warn}\n")
            except Exception:
                pass
        except Exception as e:
            # ImportError subclass other than ModuleNotFoundError, or any
            # surprising side-effect during import. Treat as unavailable
            # but keep distinct from "module missing".
            _redis_publish_available = False
            _zsf_record("queue_publish_errors", "_redis_publish_probe", e,
                        critical=True)
        return _redis_publish_available


def get_zsf_counters() -> dict:
    """Expose ZSF observability counters for daemon /metrics scraping.

    Returns a snapshot copy — caller cannot mutate the live dict. Daemon's
    `_build_prometheus_metrics` overlays these onto its own `_stats` view so
    every previously-silent llm_priority_queue failure shows up in Prometheus.
    """
    with _zsf_lock:
        snap = dict(_zsf_counters)
    snap["queue_redis_publish_available"] = (
        1 if _redis_publish_available else 0
    )
    return snap

MLX_SERVER_URL = os.environ.get("MLX_SERVER_URL", "http://127.0.0.1:5044")
DEFAULT_MODEL = os.environ.get("MLX_DEFAULT_MODEL", "mlx-community/Qwen3-4B-4bit")

# Qwen3-4B: 32K native context. At ~4 chars/token, hard limit input to prevent GPU OOM.
# Reserve 4K tokens for output + system overhead → max ~28K tokens input ≈ 112K chars.
MAX_INPUT_CHARS = 112000

# Qwen3 native thinking mode: responses may contain <think>...</think> blocks
_THINK_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL)
# Unclosed <think> (token budget ran out before </think>)
_THINK_UNCLOSED = re.compile(r"<think>.*", re.DOTALL)


# Redis-cached LLM health — updated on every queue request result.
# All health checks MUST read from Redis, never hit 5044 directly.
_HEALTH_KEY = "llm:health"
_HEALTH_TTL = 300  # seconds — persists through scheduler cycles (3min gold mining, priority queue backlog)


def check_llm_health() -> bool:
    """Check if LLM is healthy via Redis cache (NO direct HTTP to 5044).

    Returns True if the queue has successfully communicated with the LLM
    within the last 5 minutes. This is the ONLY approved way to check
    LLM health — all callers must use this instead of GET requests to 5044.
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        status = r.get(_HEALTH_KEY)
        return status == "ok"
    except ModuleNotFoundError as e:
        _redis_publish_probe()  # one-shot module-missing warning + counter
        _zsf_record("queue_redis_health_check_errors", "check_llm_health", e)
        return False
    except Exception as e:
        _zsf_record("queue_redis_health_check_errors", "check_llm_health", e)
        return False


def _update_health_status(healthy: bool):
    """Publish LLM health to Redis — called by _execute_llm_request.

    ZSF: previously bare-passed any failure. Now categorises ModuleNotFoundError
    (redis missing — surfaces once via _redis_publish_probe) versus
    connection/runtime errors (counted per-attempt for /metrics scraping).
    """
    if not _redis_publish_probe():
        # Module missing — already warned once, count attempts for ops visibility.
        with _zsf_lock:
            _zsf_counters["queue_health_publish_module_missing"] = (
                _zsf_counters.get("queue_health_publish_module_missing", 0) + 1
            )
        return
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        if healthy:
            r.setex(_HEALTH_KEY, _HEALTH_TTL, "ok")
        else:
            r.setex(_HEALTH_KEY, _HEALTH_TTL, "down")
    except Exception as e:
        _zsf_record("queue_health_publish_errors", "_update_health_status", e)


class Priority(enum.IntEnum):
    """LLM access priority levels. Lower number = higher priority."""
    AARON = 1       # Aaron's direct queries
    ATLAS = 2       # Atlas webhook/injection calls
    EXTERNAL = 3    # External integrations
    BACKGROUND = 4  # Butler background tasks


@dataclass(order=True)
class LLMRequest:
    """A queued LLM request with priority ordering."""
    priority: int
    timestamp: float = field(compare=True)
    system_prompt: str = field(compare=False)
    user_prompt: str = field(compare=False)
    profile: str = field(compare=False, default="coding")
    result: Optional[str] = field(compare=False, default=None, repr=False)
    thinking: Optional[str] = field(compare=False, default=None, repr=False)
    error: Optional[str] = field(compare=False, default=None, repr=False)
    done: threading.Event = field(compare=False, default_factory=threading.Event, repr=False)
    caller: str = field(compare=False, default="unknown")
    enable_thinking: bool = field(compare=False, default=False)
    cancelled: threading.Event = field(compare=False, default_factory=threading.Event, repr=False)


# Module-level singleton queue
_queue: PriorityQueue = PriorityQueue()
_worker_started = False
_worker_lock = threading.Lock()
_active_priority: Optional[int] = None  # Priority of MOST RECENT request (legacy field; see _inflight_by_priority)
_active_request: Optional[LLMRequest] = None  # Currently executing request (for preemption)
_pending_requests: list = []  # Track queued requests for preemption signaling
_pending_lock = threading.Lock()

# Per-priority parallelism caps. Local MLX (BACKGROUND-only path) is single-threaded
# at the Metal driver layer — concurrent requests cause abort(). The cross-process
# GPU lock (Redis llm:gpu_lock) enforces that for any local fall-through. AARON/ATLAS
# default-route to DeepSeek (HTTPS API, parallel-safe per cutover 2026-04-18); EXTERNAL
# is HTTPS; only BACKGROUND is hard-pinned local. Caps below are conservative — DeepSeek
# tolerates ~60 RPS, OpenAI plenty more, but we don't want webhook bursts to torch a
# rate-limit budget either. Tune via _PRIORITY_CAPS once telemetry shows headroom.
_PRIORITY_CAPS = {
    Priority.AARON.value: 4,       # P1 — webhook concurrency target
    Priority.ATLAS.value: 2,       # P2 — webhook injection LLM calls
    Priority.EXTERNAL.value: 4,    # P3 — phone bridge etc., HTTPS
    Priority.BACKGROUND.value: 1,  # P4 — local MLX, single-threaded by Metal
}
# Total worker thread count = sum of caps. Each worker is a generic dispatcher that
# pulls from _queue in priority order, then takes the per-priority semaphore.
_WORKER_COUNT = sum(_PRIORITY_CAPS.values())  # 4+2+4+1 = 11

# Per-priority semaphores enforce the caps above. Acquired by worker before
# _execute_llm_request, released in finally — ZERO SILENT FAILURES.
_priority_semaphores = {p: threading.BoundedSemaphore(cap) for p, cap in _PRIORITY_CAPS.items()}

# In-flight tracking: how many requests are currently executing per priority.
# Used both for the high-water mark and for the new max-inflight counters.
_inflight_lock = threading.Lock()
_inflight_by_priority: dict = {p: 0 for p in _PRIORITY_CAPS}
_inflight_max_seen: dict = {p: 0 for p in _PRIORITY_CAPS}

_stats = {
    "total_requests": 0,
    "by_priority": {1: 0, 2: 0, 3: 0, 4: 0},
    "preemptions": 0,
    "avg_wait_ms": 0.0,
    # New: max concurrent in-flight ever observed per priority (data-driven cap tuning)
    "queue_concurrent_inflight_max": dict(_inflight_max_seen),
}
QUEUE_DEPTH_WARN = 5   # Log warning when queue backs up
QUEUE_DEPTH_CRIT = 10  # Log error — likely GPU lock or LLM stall
PREEMPT_PRIORITY_DELTA = 2  # P1 can preempt P3+, P2 can preempt P4


class LLMPreemptedError(Exception):
    """Raised when an LLM request was preempted by a higher-priority request.

    Callers can catch this to retry after the high-priority request finishes,
    rather than silently losing work. Use raise_on_preempt=True in llm_generate().
    """
    pass


def extract_thinking(text: str) -> Tuple[str, Optional[str]]:
    """Extract thinking chain from Qwen3 response.

    Handles both closed <think>...</think> and unclosed <think>...
    (when token budget runs out before closing tag).

    Returns (response_text, thinking_text).
    If no <think> tags found, returns (text, None).
    """
    if not text:
        return (text, None)
    # Case 1: Properly closed <think>...</think>
    match = _THINK_PATTERN.search(text)
    if match:
        thinking = match.group(1).strip()
        response = _THINK_PATTERN.sub("", text).strip()
        return (response, thinking)
    # Case 2: Unclosed <think> (token budget exhausted)
    if "<think>" in text:
        # Everything after <think> is thinking, everything before is response
        parts = text.split("<think>", 1)
        before = parts[0].strip()
        thinking = parts[1].strip() if len(parts) > 1 else ""
        # If nothing before <think>, the LLM went straight to thinking
        # Return thinking content as response (best effort)
        if not before and thinking:
            return (thinking, thinking)
        return (before if before else thinking, thinking)
    return (text, None)


def _get_generation_params(profile: str = "coding") -> dict:
    """Get generation parameters by profile name."""
    profiles = {
        # === Fast profiles (4B optimized: focused output, minimal tokens) ===
        "classify": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 64,
                     "frequency_penalty": 0.1, "presence_penalty": 0.1, "repetition_penalty": 1.05},
        "extract": {"temperature": 0.3, "top_p": 0.9, "max_tokens": 1200,
                    "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
        "extract_deep": {"temperature": 0.4, "top_p": 0.9, "max_tokens": 1024,
                         "frequency_penalty": 0.15, "presence_penalty": 0.1, "repetition_penalty": 1.05},
        # === Standard profiles ===
        "coding": {"temperature": 0.3, "top_p": 0.9, "max_tokens": 1024,
                    "frequency_penalty": 0.3, "presence_penalty": 0.2, "repetition_penalty": 1.15},
        "explore": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 1024,
                    "frequency_penalty": 0.3, "presence_penalty": 0.2, "repetition_penalty": 1.15},
        "voice": {"temperature": 0.6, "top_p": 0.85, "max_tokens": 512,
                  "frequency_penalty": 0.4, "presence_penalty": 0.3, "repetition_penalty": 1.2},
        "deep": {"temperature": 0.5, "top_p": 0.95, "max_tokens": 2048,
                 "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
        "reasoning": {"temperature": 0.6, "top_p": 0.95, "max_tokens": 1024,
                      "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
        # === Markdown Memory Layer: concise doc summaries ===
        "summarize": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 512,
                      "frequency_penalty": 0.3, "presence_penalty": 0.2, "repetition_penalty": 1.15},
        # === Webhook S2/S8 profiles (match persistent_hook_structure.py specs) ===
        "s2_professor": {"temperature": 0.6, "top_p": 0.9, "max_tokens": 1200,
                         "frequency_penalty": 0.3, "presence_penalty": 0.2, "repetition_penalty": 1.15},
        "s2_professor_brief": {"temperature": 0.6, "top_p": 0.9, "max_tokens": 700,
                               "frequency_penalty": 0.3, "presence_penalty": 0.2, "repetition_penalty": 1.15},
        "s8_synaptic": {"temperature": 0.7, "top_p": 0.95, "max_tokens": 1500,
                        "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
        # === Synaptic chat (Aaron's direct queries, full reasoning) ===
        "synaptic_chat": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 1024,
                          "frequency_penalty": 0.3, "presence_penalty": 0.2, "repetition_penalty": 1.15},
        # === Post-work analysis (background, thinking mode) ===
        "post_analysis": {"temperature": 0.5, "top_p": 0.9, "max_tokens": 1500,
                          "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
        # === Reasoning chain profiles (multi-step 4B-optimized) ===
        "chain_narrow": {"temperature": 0.35, "top_p": 0.9, "max_tokens": 384,
                         "frequency_penalty": 0.1, "presence_penalty": 0.1, "repetition_penalty": 1.05},
        "chain_extract": {"temperature": 0.3, "top_p": 0.9, "max_tokens": 768,
                          "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
        "chain_creative": {"temperature": 0.65, "top_p": 0.95, "max_tokens": 768,
                           "frequency_penalty": 0.25, "presence_penalty": 0.15, "repetition_penalty": 1.1},
        # === Chain thinking profile (creative + thinking budget) ===
        "chain_thinking": {"temperature": 0.6, "top_p": 0.95, "max_tokens": 1024,
                           "frequency_penalty": 0.2, "presence_penalty": 0.1, "repetition_penalty": 1.1},
    }
    return profiles.get(profile, profiles["coding"])


def _is_pid_alive(pid_str: str) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(int(pid_str), 0)
        return True
    except (OSError, ValueError):
        return False


def _redis_available() -> bool:
    """Quick Redis availability check (PING). Cached for 5s to avoid hammering."""
    now = time.monotonic()
    if hasattr(_redis_available, "_cache") and now - _redis_available._cache[1] < 5.0:
        return _redis_available._cache[0]
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        result = r.ping()
        _redis_available._cache = (result, now)
        return result
    except ModuleNotFoundError as e:
        _redis_publish_probe()
        _zsf_record("queue_redis_avail_check_errors", "_redis_available", e)
        _redis_available._cache = (False, now)
        return False
    except Exception as e:
        _zsf_record("queue_redis_avail_check_errors", "_redis_available", e)
        _redis_available._cache = (False, now)
        return False


# File-based fallback lock when Redis is unavailable
_FILE_LOCK_PATH = "/tmp/contextdna_gpu.lock"


def _acquire_file_lock(timeout: float = 30.0) -> bool:
    """File-based GPU lock fallback when Redis is unavailable."""
    import fcntl
    deadline = time.monotonic() + min(timeout, 30)
    backoff = 0.1  # Exponential: 100ms → 150ms → 225ms → ... → 2s max
    while time.monotonic() < deadline:
        try:
            fd = os.open(_FILE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID for debugging
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            _acquire_file_lock._fd = fd  # Keep fd alive
            return True
        except (OSError, BlockingIOError):
            try:
                os.close(fd)
            except Exception as e:
                _zsf_record("queue_file_lock_errors", "_acquire_file_lock.close", e)
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 2.0)
    return False


def _release_file_lock():
    """Release file-based GPU lock."""
    import fcntl
    fd = getattr(_acquire_file_lock, "_fd", None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except Exception as e:
            _zsf_record("queue_file_lock_errors", "_release_file_lock", e)
        _acquire_file_lock._fd = None


def _record_lock_telemetry(r, wait_ms: float = None, steal: bool = False, p4_yield: bool = False):
    """Record GPU lock telemetry to Redis. Best-effort, never raises."""
    try:
        if wait_ms is not None:
            r.lpush("llm:lock_wait_ms", f"{wait_ms:.1f}")
            r.ltrim("llm:lock_wait_ms", 0, 99)  # Keep last 100
            r.expire("llm:lock_wait_ms", 86400)  # 24h TTL
        if steal:
            r.incr("llm:lock_steals")
            r.expire("llm:lock_steals", 86400)
        if p4_yield:
            r.incr("llm:lock_p4_yields")
            r.expire("llm:lock_p4_yields", 86400)
    except Exception as e:
        # ZSF: never block lock operations on telemetry, but count failures
        # so silent metric loss is observable via /metrics + stderr.
        _zsf_record("queue_lock_telemetry_errors", "_record_lock_telemetry", e)


def _acquire_gpu_lock(timeout: float = 120.0, priority: int = 4) -> bool:
    """Acquire cross-process GPU lock via Redis. Prevents concurrent Metal operations.

    Includes:
    - Redis availability pre-check (avoids silent concurrent access)
    - File-based fallback lock when Redis is down
    - Stale lock detection: if lock holder PID is dead, steal immediately
    - Priority-aware yielding: low-priority callers yield to urgent waiters
    - Telemetry: wait time, steal count, P4 yield count
    """
    # Pre-check: is Redis available?
    if not _redis_available():
        logger.warning("Redis unavailable for GPU lock — using file-based fallback")
        return _acquire_file_lock(timeout)

    t_start = time.monotonic()

    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        ttl = min(int(timeout) + 5, 65)  # Cap TTL at 65s (was timeout+10 ~130s)
        # SET NX with TTL = request timeout (auto-release on crash)
        acquired = r.set("llm:gpu_lock", str(os.getpid()), nx=True, ex=ttl)
        if acquired:
            # Clear urgent flag if we set it
            if priority <= 2:
                r.delete("llm:gpu_urgent")
            wait_ms = (time.monotonic() - t_start) * 1000
            _record_lock_telemetry(r, wait_ms=wait_ms)
            return True

        # Not immediately available — signal urgency if high priority (AARON/ATLAS)
        if priority <= 2:
            # Set urgent flag so low-priority holders yield after their current request
            r.set("llm:gpu_urgent", str(priority), ex=30)

        # Check if lock holder is still alive — steal if dead
        holder = r.get("llm:gpu_lock")
        if holder and not _is_pid_alive(holder):
            logger.warning(f"GPU lock held by dead PID {holder}, stealing")
            r.delete("llm:gpu_lock")
            if r.set("llm:gpu_lock", str(os.getpid()), nx=True, ex=ttl):
                if priority <= 2:
                    r.delete("llm:gpu_urgent")
                wait_ms = (time.monotonic() - t_start) * 1000
                _record_lock_telemetry(r, wait_ms=wait_ms, steal=True)
                return True

        # Wait for lock release with exponential backoff
        # High priority (P1/P2): 100ms → 200ms → 400ms → 800ms cap (responsive)
        # Low priority (P3/P4): 200ms → 400ms → 800ms → 2s cap (yields resources)
        deadline = time.monotonic() + min(timeout, 60)  # Cap wait at 60s
        liveness_check = time.monotonic()
        backoff = 0.1 if priority <= 2 else 0.2
        backoff_cap = 0.8 if priority <= 2 else 2.0
        while time.monotonic() < deadline:
            poll_interval = backoff
            # Low-priority callers back off further when urgent waiters exist
            if priority >= 3:
                urgent = r.get("llm:gpu_urgent")
                if urgent:
                    try:
                        if int(urgent) < priority:
                            poll_interval = max(backoff, 0.5)
                            _record_lock_telemetry(r, p4_yield=True)
                    except (ValueError, TypeError) as e:
                        # Garbage in llm:gpu_urgent — count so dashboards
                        # can spot a publisher writing bad values.
                        _zsf_record(
                            "queue_urgent_yield_errors",
                            "_acquire_gpu_lock.urgent_parse",
                            e,
                        )

            time.sleep(poll_interval)
            backoff = min(backoff * 1.5, backoff_cap)

            if r.set("llm:gpu_lock", str(os.getpid()), nx=True, ex=ttl):
                if priority <= 2:
                    r.delete("llm:gpu_urgent")
                wait_ms = (time.monotonic() - t_start) * 1000
                _record_lock_telemetry(r, wait_ms=wait_ms)
                return True
            # Periodic liveness check on holder
            if time.monotonic() - liveness_check > 2.0:
                liveness_check = time.monotonic()
                holder = r.get("llm:gpu_lock")
                if holder and not _is_pid_alive(holder):
                    logger.warning(f"GPU lock held by dead PID {holder}, stealing")
                    r.delete("llm:gpu_lock")
                    _record_lock_telemetry(r, steal=True)
                    backoff = 0.1  # Reset backoff after steal attempt
        return False
    except Exception as e:
        logger.warning(f"Redis GPU lock failed ({e}) — falling back to file lock")
        return _acquire_file_lock(timeout)


def _release_gpu_lock():
    """Release cross-process GPU lock (Redis primary, file fallback).

    Priority-aware: if urgent waiters exist with higher priority than the
    just-completed request, yields briefly to give them first shot at the lock.
    """
    # Always try to release file lock (no-op if not held)
    _release_file_lock()
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        # Only release if we own it
        if r.get("llm:gpu_lock") == str(os.getpid()):
            r.delete("llm:gpu_lock")
            # Yield to urgent waiters if our priority was lower (higher number)
            urgent = r.get("llm:gpu_urgent")
            if urgent:
                try:
                    urgent_p = int(urgent)
                    current_p = _active_priority or 4
                    if urgent_p < current_p:
                        _stats["preemptions"] += 1
                        logger.debug(
                            f"GPU lock: yielding to P{urgent_p} waiter (was P{current_p})"
                        )
                        time.sleep(0.3)  # Brief yield for higher-priority process
                except (ValueError, TypeError) as e:
                    _zsf_record(
                        "queue_urgent_yield_errors",
                        "_release_gpu_lock.urgent_parse",
                        e,
                    )
    except ModuleNotFoundError as e:
        # Critical — every release that can't reach Redis means the cross-
        # process GPU lock leaks until TTL expires. Log to file so ops
        # notices even if stderr is swallowed by the parent.
        _redis_publish_probe()
        _zsf_record("queue_lock_release_errors", "_release_gpu_lock", e,
                    critical=True)
    except Exception as e:
        _zsf_record("queue_lock_release_errors", "_release_gpu_lock", e)


# =============================================================================
# HYBRID ROUTING — External LLM fallback (Surgery Team of 3 architecture)
# =============================================================================

# Profiles eligible for external fallback when local LLM fails.
# Cost-controlled: background gold mining (1K+ calls/hr) stays $0.
_EXTERNAL_ELIGIBLE_PROFILES = {
    "s2_professor", "s2_professor_brief", "s8_synaptic",
    "deep", "reasoning", "extract_deep", "synaptic_chat",
    "post_analysis", "strategic_analyst",
}

# Profiles that NEVER go external (cost control for high-volume background)
_LOCAL_ONLY_PROFILES = {
    "classify", "extract", "summarize", "voice",
}

# Daily budget gate (USD). When exceeded, degrades to local-only.
_DAILY_BUDGET_USD = float(os.environ.get("LLM_DAILY_BUDGET_USD", "5.0"))


def _should_use_external(profile: str, priority: int) -> bool:
    """Decide if this request should attempt external fallback on local failure.

    Routing logic (joint consensus from Surgery Team of 3 cross-model consultation):
    - P1 (AARON) + P2 (ATLAS): eligible profiles → TRY external
    - P3 (EXTERNAL): eligible profiles → TRY external
    - P4 (BACKGROUND): LOCAL ONLY (cost control for gold mining)
    - Redis llm:hybrid_mode overrides: on/fallback-only(default)/off
    """
    # Background priority = never external (cost control)
    if priority >= Priority.BACKGROUND:
        return False

    # Profile must be eligible
    if profile in _LOCAL_ONLY_PROFILES:
        return False
    if profile not in _EXTERNAL_ELIGIBLE_PROFILES:
        # Unknown profile — allow external for P1/P2, deny for P3+
        if priority > Priority.ATLAS:
            return False

    # Check Redis hybrid mode override
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        mode = r.get("llm:hybrid_mode") or "fallback-only"
        if mode == "off":
            return False
        # "on" = always try external (even without local failure — future use)
        # "fallback-only" = default, try external only on local failure (current path)

        # Budget gate: check daily spend
        from datetime import date
        today = date.today().isoformat()
        daily_cost = float(r.get(f"llm:costs:{today}") or 0)
        if daily_cost >= _DAILY_BUDGET_USD:
            logger.warning(f"Hybrid routing: daily budget ${_DAILY_BUDGET_USD} exhausted "
                          f"(${daily_cost:.2f} spent). Local-only mode.")
            return False
    except ModuleNotFoundError as e:
        _redis_publish_probe()
        _zsf_record("queue_hybrid_mode_check_errors", "_should_use_external", e)
    except Exception as e:
        # Redis down — fall through to allow external (better than total
        # failure) but record so ops can see budget gate is degraded.
        _zsf_record("queue_hybrid_mode_check_errors", "_should_use_external", e)

    return True


def _external_cache_key(system_prompt: str, user_prompt: str, profile: str) -> str:
    """Generate Redis cache key from prompt hash. Deduplicates identical external calls."""
    h = hashlib.sha256(f"{profile}:{system_prompt}:{user_prompt}".encode()).hexdigest()[:16]
    return f"llm:ext_cache:{h}"


# =============================================================================
# EXTERNAL PROVIDER SELECTION
# =============================================================================
# Default: try DeepSeek first (cheap, $0.28/1M), then OpenAI as secondary fallback.
# Override via LLM_EXTERNAL_PROVIDER env var:
#   deepseek                       — DeepSeek only, no secondary fallback
#   openai                         — OpenAI only (legacy behaviour)
#   deepseek-first-openai-fallback — DeepSeek primary, OpenAI if DS fails with 5xx/connect (DEFAULT)
_EXTERNAL_PROVIDER_ENV = "LLM_EXTERNAL_PROVIDER"
_EXTERNAL_PROVIDER_DEFAULT = "deepseek-first-openai-fallback"

# Secret Manager placeholder for DeepSeek key (wire up to AWS SM when value is filled)
_DS_SM_ID = "/ersim/prod/backend/DEEPSEEK_API_KEY"
_OAI_SM_ID = "/ersim/prod/backend/OPENAI_API_KEY"


def _resolve_external_provider() -> str:
    """Return the active external-provider mode (normalized lower-case)."""
    mode = os.environ.get(_EXTERNAL_PROVIDER_ENV, _EXTERNAL_PROVIDER_DEFAULT).strip().lower()
    if mode not in {"deepseek", "openai", "deepseek-first-openai-fallback"}:
        logger.warning(
            f"Unknown {_EXTERNAL_PROVIDER_ENV}={mode!r} — defaulting to "
            f"{_EXTERNAL_PROVIDER_DEFAULT!r}"
        )
        mode = _EXTERNAL_PROVIDER_DEFAULT
    return mode


def _load_secret_chain(env_names, keychain_service_account_pairs, sm_id):
    """Resolve a secret using env → macOS Keychain → AWS Secrets Manager.

    Args:
        env_names: Iterable of env var names to try in order.
        keychain_service_account_pairs: Iterable of ``(service, account)``
            tuples passed to ``security find-generic-password``. Use ``None``
            for the account to omit ``-a`` entirely.
        sm_id: AWS Secrets Manager secret id (str or ``None``).

    Returns:
        The secret string, or ``None`` if every source was empty/unavailable.
    """
    # 1. Env vars
    for name in env_names:
        val = os.environ.get(name)
        if val:
            return val.strip()

    # 2. macOS Keychain (subprocess — stdlib only, no Keychain python bindings)
    import subprocess  # local import keeps cold-start lean
    for service, account in keychain_service_account_pairs:
        if account:
            cmd = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
        else:
            cmd = ["security", "find-generic-password", "-s", service, "-w"]
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            # security binary missing, timeout, etc. — fall through to next
            # source. Counter so ops sees keychain misses (vs silent path).
            _zsf_record(
                "queue_secret_keychain_errors",
                "_load_secret_chain.keychain",
                e,
            )
            continue

    # 3. AWS Secrets Manager via subprocess (stdlib only, avoids boto3 import cost)
    if sm_id:
        try:
            out = subprocess.run(
                [
                    "aws", "secretsmanager", "get-secret-value",
                    "--secret-id", sm_id,
                    "--query", "SecretString",
                    "--output", "text",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0:
                secret = out.stdout.strip()
                # Skip placeholder values (common during bootstrap)
                if secret and not secret.lower().startswith("placeholder"):
                    return secret
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            _zsf_record(
                "queue_secret_aws_errors",
                "_load_secret_chain.aws_sm",
                e,
            )

    return None


def _load_deepseek_api_key() -> Optional[str]:
    """Load DeepSeek API key: env → macOS Keychain → AWS Secrets Manager.

    Sources (first match wins):
      - env: DEEPSEEK_API_KEY, Context_DNA_Deepseek, Context_DNA_Deep_Seek
      - Keychain: Context_DNA_Deep_Seek, Context_DNA_Deepseek, DEEPSEEK_API_KEY,
                  fleet-nerve/account=DEEPSEEK_API_KEY
      - AWS SM: /ersim/prod/backend/DEEPSEEK_API_KEY
    """
    return _load_secret_chain(
        env_names=("DEEPSEEK_API_KEY", "Context_DNA_Deepseek", "Context_DNA_Deep_Seek"),
        keychain_service_account_pairs=(
            ("Context_DNA_Deep_Seek", None),
            ("Context_DNA_Deepseek", None),
            ("DEEPSEEK_API_KEY", None),
            ("fleet-nerve", "DEEPSEEK_API_KEY"),
        ),
        sm_id=_DS_SM_ID,
    )


def _load_openai_api_key() -> Optional[str]:
    """Load OpenAI API key: env → Keychain → AWS SM (secondary fallback only)."""
    return _load_secret_chain(
        env_names=("OPENAI_API_KEY", "Context_DNA_OPENAI"),
        keychain_service_account_pairs=(
            ("Context_DNA_OPENAI", None),
            ("fleet-nerve", "OPENAI_API_KEY"),
        ),
        sm_id=_OAI_SM_ID,
    )


# GPT model-name → DeepSeek-equivalent mapping (for callers that pass explicit models).
_GPT_TO_DEEPSEEK = {
    "gpt-4.1-mini": "deepseek-chat",
    "gpt-4o-mini": "deepseek-chat",
    "gpt-4o": "deepseek-chat",
    "gpt-4-turbo": "deepseek-chat",
    "gpt-4.1": "deepseek-chat",
    "o1": "deepseek-reasoner",
    "o1-mini": "deepseek-reasoner",
    "o3": "deepseek-reasoner",
    "o3-pro": "deepseek-reasoner",
}

# Profiles that benefit from chain-of-thought → route to deepseek-reasoner.
_DS_REASONER_PROFILES = {
    "s2_professor", "s2_professor_brief", "s8_synaptic",
    "deep", "reasoning", "post_analysis", "chain_thinking",
    "strategic_analyst",
}


def _map_to_deepseek_model(profile: str, gpt_model_hint: Optional[str] = None) -> str:
    """Choose DeepSeek model from either a GPT model hint or a generation profile."""
    if gpt_model_hint:
        mapped = _GPT_TO_DEEPSEEK.get(gpt_model_hint.strip().lower())
        if mapped:
            return mapped
    if profile in _DS_REASONER_PROFILES:
        return "deepseek-reasoner"
    return "deepseek-chat"


def _call_deepseek(system_prompt: str, user_prompt: str, profile: str,
                   caller: str) -> Optional[dict]:
    """Synchronous wrapper around DeepSeekProvider.generate.

    Returns a dict with keys {content, cost_estimate, latency_ms, model}, or
    None on failure. Handles its own event loop creation.
    """
    api_key = _load_deepseek_api_key()
    if not api_key:
        logger.warning(
            "DeepSeek API key unavailable (env/Keychain/AWS SM all empty) — "
            "cannot route external fallback"
        )
        return None

    params = _get_generation_params(profile)
    max_tokens = params.get("max_tokens", 1024)
    temperature = params.get("temperature", 0.7)
    model = _map_to_deepseek_model(profile)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    import asyncio
    from memory.providers.deepseek_provider import DeepSeekProvider

    async def _run():
        provider = DeepSeekProvider(api_key=api_key)
        try:
            return await provider.generate(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        finally:
            await provider.close()

    # Always use a fresh loop — the priority-queue worker runs in a thread and
    # may not own an event loop. asyncio.run handles both cases safely here.
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In-loop fallback: spin a dedicated loop on this thread
                new_loop = asyncio.new_event_loop()
                try:
                    return new_loop.run_until_complete(_run())
                finally:
                    new_loop.close()
        except RuntimeError:
            pass
        return asyncio.run(_run())
    except Exception as e:
        # Re-raise so _external_fallback can distinguish 5xx vs other failures
        raise


def _openai_prompt_cache_key(system_prompt: str, profile: str) -> str:
    """Build a stable OpenAI prompt_cache_key from the system prompt + profile.

    OpenAI groups requests with the same ``prompt_cache_key`` into a single
    cache bucket (2024+ API). Passing a hash of the *system* text means every
    repeated call with the same system prompt hits the cache — the user prompt
    varies but the (much larger, 2KB+) system block gets a 50% input-token
    discount on cache hits.

    Profile is appended so different profiles (e.g. ``classify`` vs ``deep``)
    get independent buckets — they have different max_tokens/temperature and
    shouldn't share a cache entry.

    DeepSeek silently ignores this key (its auto-caching works on prefix match,
    not explicit keys), so the header is safe to emit unconditionally on any
    OpenAI-compatible endpoint.
    """
    material = f"{profile}|{system_prompt}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:16]


def _call_openai_fallback(system_prompt: str, user_prompt: str, profile: str,
                          caller: str) -> Optional[dict]:
    """Secondary fallback: call OpenAI via the legacy LLMGateway path.

    Kept as a safety net so a DeepSeek outage does not take down all external
    routing. Only invoked when LLM_EXTERNAL_PROVIDER=openai or when
    deepseek-first-openai-fallback experiences a DS failure.
    """
    try:
        # Import gateway from context-dna package (same path as legacy code)
        import sys
        cdna_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context-dna")
        if cdna_path not in sys.path:
            sys.path.insert(0, cdna_path)

        env_path = os.path.join(cdna_path, ".env")
        if os.path.exists(env_path):
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path, override=True)
            except Exception as e:
                # dotenv optional but record for visibility
                _zsf_record(
                    "queue_dotenv_errors",
                    "_call_openai_fallback.dotenv",
                    e,
                )

        from local_llm.llm_gateway import get_gateway
        gateway = get_gateway()

        params = _get_generation_params(profile)
        max_tokens = params.get("max_tokens", 1024)
        temperature = params.get("temperature", 0.7)

        # Anthropic prompt cache uses cache_control:{"type":"ephemeral"} in message
        # blocks; OpenAI (2024+ API) uses a flat prompt_cache_key param that groups
        # requests for automatic server-side caching (50% discount on cached input).
        # DeepSeek is OpenAI-compatible but does not honour prompt_cache_key — it
        # auto-caches on prefix-match, so this header is a no-op there (harmless).
        # Derive a stable 16-char key from the system prompt so repeated calls with
        # the same system text route to the same OpenAI cache bucket.
        cache_key = _openai_prompt_cache_key(system_prompt, profile)

        response = gateway.infer(
            prompt=user_prompt,
            system=system_prompt,
            provider="openai",
            max_tokens=max_tokens,
            temperature=temperature,
            session_id=f"hybrid_{caller}",
            prompt_cache_key=cache_key,
        )

        if response.error or not response.content:
            logger.warning(f"OpenAI secondary fallback failed: {response.error or 'empty'}")
            return None
        if response.content == gateway.config.static_fallback_message:
            return None

        return {
            "content": response.content,
            "cost_estimate": response.cost_usd,
            "latency_ms": response.latency_ms,
            "model": response.model_id,
            "provider": "openai",
        }
    except Exception as e:
        logger.error(f"OpenAI secondary fallback exception: {type(e).__name__}: {str(e)[:200]}")
        return None


def _external_fallback(system_prompt: str, user_prompt: str, profile: str,
                       caller: str = "unknown") -> Optional[str]:
    """Attempt external LLM fallback — DeepSeek primary, OpenAI secondary.

    Provider selection controlled by ``LLM_EXTERNAL_PROVIDER`` env var:
      - ``deepseek``                        → DeepSeek only
      - ``openai``                          → OpenAI only (legacy)
      - ``deepseek-first-openai-fallback``  → DS primary, OpenAI if DS 5xx/timeout (default)

    Called when local LLM fails. GPU lock already released before this runs.
    Returns content string on success, None on failure.
    Caches responses in Redis (300s TTL) to prevent duplicate API calls.
    """
    # Check Redis cache first — prevents duplicate S8+S10 calls in same window
    cache_key = _external_cache_key(system_prompt, user_prompt, profile)
    try:
        import redis as _redis
        _r = _redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        cached = _r.get(cache_key)
        if cached:
            logger.info(f"Hybrid cache HIT: {caller} [{profile}] (saved API call)")
            return cached
    except ModuleNotFoundError as e:
        _redis_publish_probe()
        _zsf_record("queue_external_cache_errors",
                    "_external_fallback.cache_get", e)
    except Exception as e:
        # Redis down — proceed to actual call (correct fallback) but
        # record so cache miss-rate spikes are explainable.
        _zsf_record("queue_external_cache_errors",
                    "_external_fallback.cache_get", e)

    mode = _resolve_external_provider()
    params = _get_generation_params(profile)
    logger.info(
        f"Hybrid fallback: {caller} [{profile}] → external "
        f"(mode={mode}, max_tokens={params.get('max_tokens', 1024)})"
    )

    result: Optional[dict] = None
    provider_used: Optional[str] = None

    # Primary attempt
    if mode in ("deepseek", "deepseek-first-openai-fallback"):
        try:
            ds_result = _call_deepseek(system_prompt, user_prompt, profile, caller)
            if ds_result and ds_result.get("content"):
                result = {**ds_result, "provider": "deepseek"}
                provider_used = "deepseek"
        except Exception as e:
            # Inspect whether this is a retryable class of failure that warrants OpenAI fallback
            err_msg = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning(f"DeepSeek primary failed: {err_msg}")
            is_5xx_or_connect = (
                "500" in str(e) or "502" in str(e) or "503" in str(e) or "504" in str(e)
                or "ConnectError" in type(e).__name__
                or "Timeout" in type(e).__name__
                or "RuntimeError" in type(e).__name__  # DeepSeekProvider wraps retries-exhausted as RuntimeError
            )
            if mode == "deepseek-first-openai-fallback" and is_5xx_or_connect:
                logger.info("Falling through to OpenAI secondary fallback")
                result = _call_openai_fallback(system_prompt, user_prompt, profile, caller)
                provider_used = "openai" if result else None
    elif mode == "openai":
        result = _call_openai_fallback(system_prompt, user_prompt, profile, caller)
        provider_used = "openai" if result else None

    if not result or not result.get("content"):
        logger.warning(f"Hybrid fallback exhausted (mode={mode}): no content")
        return None

    content = result["content"]
    cost_usd = float(result.get("cost_estimate", 0.0))
    latency_ms = int(result.get("latency_ms", 0))
    model_id = result.get("model", "unknown")

    # Track cost in Redis (reuses existing telemetry path)
    _track_external_cost(cost_usd, provider_used, model_id, profile)

    # Log fallback event for S8 awareness
    _log_fallback_event(profile, caller, provider_used, model_id, latency_ms, cost_usd)

    logger.info(
        f"Hybrid fallback SUCCESS: {provider_used}/{model_id} "
        f"{latency_ms}ms ${cost_usd:.6f}"
    )

    # Cache successful response (300s TTL)
    try:
        _r = _redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        _r.setex(cache_key, 300, content)
    except Exception as e:
        _zsf_record(
            "queue_external_cache_errors",
            "_external_fallback.cache_set",
            e,
        )

    return content


def _track_external_cost(cost_usd: float, provider: str, model: str, profile: str):
    """Track external LLM cost in Redis for budget gate and telemetry."""
    try:
        import redis
        from datetime import date
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        today = date.today().isoformat()

        # Daily total
        r.incrbyfloat(f"llm:costs:{today}", cost_usd)
        r.expire(f"llm:costs:{today}", 86400 * 2)  # 2-day TTL

        # Per-provider breakdown
        r.incrbyfloat(f"llm:costs:{today}:{provider}", cost_usd)
        r.expire(f"llm:costs:{today}:{provider}", 86400 * 2)

        # Provider stats hash
        r.hincrby("llm:provider_stats", f"{provider}:calls", 1)
        r.hincrbyfloat("llm:provider_stats", f"{provider}:total_cost", cost_usd)
    except ModuleNotFoundError as e:
        _redis_publish_probe()
        _zsf_record("queue_external_cost_errors", "_track_external_cost", e)
    except Exception as e:
        _zsf_record("queue_external_cost_errors", "_track_external_cost", e)


def _log_fallback_event(profile: str, caller: str, provider: str, model: str,
                        latency_ms: int, cost_usd: float):
    """Log fallback event to Redis for S8 Synaptic awareness and monitoring."""
    try:
        import redis
        import json
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)

        event = json.dumps({
            "ts": time.time(),
            "from": "local",
            "to": provider,
            "model": model,
            "profile": profile,
            "caller": caller,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
        })
        r.lpush("llm:fallback_events", event)
        r.ltrim("llm:fallback_events", 0, 99)  # Keep last 100 events
        r.expire("llm:fallback_events", 86400)  # 24h TTL
    except ModuleNotFoundError as e:
        _redis_publish_probe()
        _zsf_record("queue_fallback_event_errors", "_log_fallback_event", e)
    except Exception as e:
        _zsf_record("queue_fallback_event_errors", "_log_fallback_event", e)


def _execute_llm_request(req: LLMRequest) -> None:
    """Execute a single LLM request against local LLM (mlx_lm.server)."""
    global _active_priority
    _active_priority = req.priority

    # Cross-process GPU lock — prevents concurrent Metal operations that cause abort()
    lock_timeout = req.done_timeout if hasattr(req, 'done_timeout') else 120.0
    if not _acquire_gpu_lock(timeout=lock_timeout, priority=req.priority):
        req.error = "GPU lock timeout — another process is using the LLM"
        req.done.set()
        return

    params = _get_generation_params(req.profile)

    # Guard: prevent GPU OOM from oversized prompts
    total_input = len(req.system_prompt or "") + len(req.user_prompt or "")
    if total_input > MAX_INPUT_CHARS:
        logger.warning(f"LLM input too large: {total_input} chars (max {MAX_INPUT_CHARS}), "
                      f"caller={req.caller}, segmenting")
        # Segment user prompt into profile-appropriate chunks
        budget = MAX_INPUT_CHARS - len(req.system_prompt or "") - 200  # 200 chars margin
        if budget > 0:
            user = req.user_prompt
            # Try to segment at natural boundaries (double newlines, section markers)
            # Keep the instruction header (first 20% of budget) + most relevant tail (80%)
            header_budget = budget // 5
            tail_budget = budget - header_budget
            # Find a clean break point near the header boundary
            header_end = user.rfind("\n\n", 0, header_budget + 500)
            if header_end < header_budget // 2:
                header_end = header_budget  # No good break found, hard cut
            trimmed_chars = len(user) - budget
            req.user_prompt = user[:header_end] + \
                f"\n\n[... {trimmed_chars} chars segmented out — context exceeds LLM capacity ...]\n\n" + \
                user[-(tail_budget):]
        else:
            req.error = f"Prompt too large: {total_input} chars exceeds {MAX_INPUT_CHARS} limit"
            req.done.set()
            return

    # EMERGENCE-ALIGNED REASONING (trial: 2026-02-21)
    # Philosophy: Let LLM decide when thinking helps. Don't prescribe.
    # Only force /no_think on truly tiny budgets (classify=64tok).
    # Everything else: model chooses naturally. System prompt hints availability.
    enable_think = req.enable_thinking

    try:
        user_prompt = req.user_prompt
        system_prompt = req.system_prompt

        if req.profile == "classify":
            # 64 tok budget — thinking would consume it all. Must suppress.
            user_prompt = "/no_think\n" + user_prompt
        elif enable_think:
            # Caller explicitly requested thinking — honor it
            user_prompt = "/think\n" + user_prompt
        else:
            # EMERGENCE: Don't force /think or /no_think.
            # Add availability hint to system prompt so model can choose.
            system_prompt = system_prompt + \
                "\n\n---\nYou may use /think for detailed reasoning when helpful, " \
                "or respond directly when straightforward. You decide."

        payload = {
            "model": DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": params.get("temperature", 0.7),
            "top_p": params.get("top_p", 0.9),
            "max_tokens": params.get("max_tokens", 1024),
            "frequency_penalty": params.get("frequency_penalty", 0.3),
            "presence_penalty": params.get("presence_penalty", 0.2),
            "repetition_penalty": params.get("repetition_penalty", 1.15),
            "stop": ["<|im_start|>", "<|im_end|>", "<|endoftext|>"],
            "stream": False,
        }

        # Don't force /think. Let LLM decide naturally based on the optional suggestion above.

        resp = requests.post(
            f"{MLX_SERVER_URL}/v1/chat/completions",
            json=payload,
            timeout=120
        )
        if resp.ok:
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                raw_content = choices[0].get("message", {}).get("content", "")
                if enable_think:
                    req.result, req.thinking = extract_thinking(raw_content)
                else:
                    # Always strip <think> tags — Qwen3 emits them spontaneously
                    cleaned, _ = extract_thinking(raw_content)
                    req.result = cleaned if cleaned else raw_content
            else:
                req.error = "No choices in response"
        else:
            req.error = f"HTTP {resp.status_code}: {resp.text[:100]}"
    except requests.exceptions.ConnectionError:
        _update_health_status(False)
        # Release GPU lock BEFORE external fallback — don't block Metal queue
        _release_gpu_lock()
        if _should_use_external(req.profile, req.priority):
            fallback_content = _external_fallback(
                req.system_prompt, req.user_prompt, req.profile, req.caller
            )
            if fallback_content:
                req.result = fallback_content
                req.error = None
                logger.info(f"Hybrid: {req.caller} [{req.profile}] served via external fallback")
            else:
                req.error = "local LLM down + external fallback failed (start: ./scripts/start-llm.sh)"
        else:
            req.error = "local LLM not running on port 5044 (start: ./scripts/start-llm.sh)"
    except Exception as e:
        _update_health_status(False)
        # Release GPU lock BEFORE external fallback
        _release_gpu_lock()
        if _should_use_external(req.profile, req.priority):
            fallback_content = _external_fallback(
                req.system_prompt, req.user_prompt, req.profile, req.caller
            )
            if fallback_content:
                req.result = fallback_content
                req.error = None
                logger.info(f"Hybrid: {req.caller} [{req.profile}] served via external fallback")
            else:
                req.error = f"{type(e).__name__}: {str(e)[:100]} (external fallback also failed)"
        else:
            req.error = f"{type(e).__name__}: {str(e)[:100]}"
    else:
        # Success path — update health cache
        _update_health_status(req.error is None)
    finally:
        _release_gpu_lock()  # No-op if already released in except blocks
        _active_priority = None
        req.done.set()


def _worker_loop():
    """Background worker that processes LLM requests in priority order.

    Multiple worker threads run this loop concurrently (see _WORKER_COUNT). Each
    pulls from the shared priority queue, then acquires the per-priority semaphore
    that caps parallelism for that level. Per-priority caps live in _PRIORITY_CAPS.
    """
    global _active_request, _active_priority
    while True:
        try:
            req = _queue.get(timeout=30)
        except Exception as e:
            # Benign queue.Empty after 30s idle — count for shape but
            # don't log unless it's not Empty (real failure mode).
            if type(e).__name__ != "Empty":
                _zsf_record(
                    "queue_worker_get_timeouts",
                    "_worker_loop.queue_get",
                    e,
                )
            else:
                with _zsf_lock:
                    _zsf_counters["queue_worker_get_timeouts"] = (
                        _zsf_counters.get("queue_worker_get_timeouts", 0) + 1
                    )
            continue

        sem_acquired = False
        inflight_incremented = False
        try:
            # Check if request was cancelled while waiting in queue
            if req.cancelled.is_set():
                req.error = "preempted_in_queue"
                req.done.set()
                _stats["preemptions"] += 1
                logger.info(f"LLM P{req.priority} [{req.caller}] preempted before execution")
                continue

            depth = _queue.qsize()
            wait_ms = (time.monotonic() - req.timestamp) * 1000
            if depth >= QUEUE_DEPTH_CRIT:
                logger.error(f"LLM queue depth CRITICAL: {depth} waiting | processing P{req.priority} [{req.caller}] (waited {wait_ms:.0f}ms)")
            elif depth >= QUEUE_DEPTH_WARN:
                logger.warning(f"LLM queue depth HIGH: {depth} waiting | processing P{req.priority} [{req.caller}] (waited {wait_ms:.0f}ms)")
            else:
                logger.debug(
                    f"LLM P{req.priority} [{req.caller}] processing (waited {wait_ms:.0f}ms, depth {depth})"
                )

            # Per-priority concurrency cap. Falls back to BACKGROUND cap (1) for
            # any unknown priority value — ZSF, never silently un-capped.
            sem = _priority_semaphores.get(
                req.priority, _priority_semaphores[Priority.BACKGROUND.value]
            )
            sem.acquire()
            sem_acquired = True

            # Bump in-flight counters under lock; record max for cap tuning telemetry.
            with _inflight_lock:
                _inflight_by_priority[req.priority] = _inflight_by_priority.get(req.priority, 0) + 1
                cur = _inflight_by_priority[req.priority]
                if cur > _inflight_max_seen.get(req.priority, 0):
                    _inflight_max_seen[req.priority] = cur
                    _stats["queue_concurrent_inflight_max"][req.priority] = cur
                inflight_incremented = True

            _active_request = req
            _active_priority = req.priority
            _execute_llm_request(req)

            _active_request = None

            # Update stats (best-effort; not strictly atomic across workers but
            # close enough for monitoring — exact counters live in Redis hash)
            _stats["total_requests"] += 1
            _stats["by_priority"][req.priority] = _stats["by_priority"].get(req.priority, 0) + 1
            n = _stats["total_requests"]
            _stats["avg_wait_ms"] = (_stats["avg_wait_ms"] * (n - 1) + wait_ms) / n

            # Publish stats to Redis for monitoring
            _publish_queue_stats()

            # Cross-process yield: after releasing GPU lock, check if external
            # processes are waiting. Without this gap, back-to-back in-process
            # jobs starve cross-process callers (agent_service, CLI tools).
            try:
                import redis as _r
                _rc = _r.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
                if _rc.exists("llm:gpu_urgent"):
                    time.sleep(0.5)  # Yield to cross-process urgent waiter
            except ModuleNotFoundError as e:
                _redis_publish_probe()
                _zsf_record(
                    "queue_urgent_yield_errors",
                    "_worker_loop.cross_process_yield",
                    e,
                )
            except Exception as e:
                _zsf_record(
                    "queue_urgent_yield_errors",
                    "_worker_loop.cross_process_yield",
                    e,
                )

        except Exception as e:
            _active_request = None
            req.error = str(e)
            req.done.set()
        finally:
            # ZSF: always decrement in-flight + release semaphore + mark task done,
            # even if execute_llm_request raised. Order matters: counter first (so
            # next worker sees accurate inflight), then sem (so next worker can
            # actually start), then task_done.
            if inflight_incremented:
                with _inflight_lock:
                    _inflight_by_priority[req.priority] = max(
                        0, _inflight_by_priority.get(req.priority, 1) - 1
                    )
            if sem_acquired:
                try:
                    _priority_semaphores[req.priority].release()
                except (ValueError, KeyError):
                    pass  # BoundedSemaphore raises ValueError on over-release; defensive
            _queue.task_done()


def _publish_queue_stats():
    """Publish queue stats to Redis for monitoring/alerting.

    Includes per-priority in-flight counters and high-water marks so cap tuning
    can be data-driven. Keys:
      queue_concurrent_inflight_<priority>      (live)
      queue_concurrent_inflight_max_<priority>  (max-ever observed)

    ZSF (Cycle 7 G2): originally bare-passed every failure. Daemon ran on a
    python interpreter without ``redis`` installed → publish silently failed
    forever and ``llm:queue_stats`` hash was never written, leaving Atlas
    blind to queue depth. Now categorises ModuleNotFoundError vs runtime
    errors so /metrics + stderr surface the failure mode immediately.
    """
    if not _redis_publish_probe():
        with _zsf_lock:
            _zsf_counters["queue_publish_module_missing"] = (
                _zsf_counters.get("queue_publish_module_missing", 0) + 1
            )
        return

    try:
        import redis as _r
        rc = _r.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        with _inflight_lock:
            inflight_snapshot = dict(_inflight_by_priority)
            max_snapshot = dict(_inflight_max_seen)
        stats_data = {
            "total_requests": _stats["total_requests"],
            "preemptions": _stats["preemptions"],
            "avg_wait_ms": round(_stats["avg_wait_ms"], 1),
            "queue_depth": _queue.qsize(),
            "active_priority": _active_priority,
            "by_priority": json.dumps(_stats["by_priority"]),
            "priority_caps": json.dumps(_PRIORITY_CAPS),
            "inflight": json.dumps(inflight_snapshot),
            "inflight_max": json.dumps(max_snapshot),
            "updated_at": datetime.utcnow().isoformat(),
        }
        # Per-priority flat keys for quick Prometheus-style scraping
        for p, n in inflight_snapshot.items():
            stats_data[f"queue_concurrent_inflight_{p}"] = n
        for p, n in max_snapshot.items():
            stats_data[f"queue_concurrent_inflight_max_{p}"] = n
        rc.hset("llm:queue_stats", mapping={k: str(v) for k, v in stats_data.items()})
        rc.expire("llm:queue_stats", 600)  # 10 min TTL
    except Exception as e:
        # Critical telemetry path: log to file + counter + stderr so the
        # operator can correlate dashboard staleness with redis failures.
        _zsf_record(
            "queue_publish_errors",
            "_publish_queue_stats",
            e,
            critical=True,
        )


def get_queue_stats() -> dict:
    """Get current queue stats (for health endpoints / dashboards)."""
    with _inflight_lock:
        inflight_snapshot = dict(_inflight_by_priority)
        max_snapshot = dict(_inflight_max_seen)
    return {
        **_stats,
        "queue_depth": _queue.qsize(),
        "active_priority": _active_priority,
        "priority_caps": dict(_PRIORITY_CAPS),
        "inflight": inflight_snapshot,
        "inflight_max": max_snapshot,
    }


def _ensure_worker():
    """Start the worker thread pool if not already running.

    Spawns _WORKER_COUNT generic workers; per-priority caps are enforced by
    _priority_semaphores inside _worker_loop. This is what unlocks parallel
    AARON/ATLAS/EXTERNAL execution while keeping BACKGROUND single-threaded
    against the local MLX server.
    """
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        for i in range(_WORKER_COUNT):
            t = threading.Thread(
                target=_worker_loop, daemon=True, name=f"llm-priority-queue-{i}"
            )
            t.start()
        _worker_started = True
        logger.info(
            f"LLM priority queue: {_WORKER_COUNT} workers started, caps={_PRIORITY_CAPS}"
        )


def llm_generate(
    system_prompt: str,
    user_prompt: str,
    priority: Priority = Priority.BACKGROUND,
    profile: str = "coding",
    caller: str = "unknown",
    timeout_s: float = 120.0,
    enable_thinking: bool = False,
    raise_on_preempt: bool = False,
) -> Optional[str]:
    """
    Submit an LLM request with priority ordering.

    Args:
        system_prompt: System message for the LLM
        user_prompt: User message for the LLM
        priority: Priority level (AARON=1, ATLAS=2, EXTERNAL=3, BACKGROUND=4)
        profile: Generation profile (coding, explore, voice, deep, reasoning)
        caller: Identifier for logging/debugging
        timeout_s: Max wait time in seconds
        enable_thinking: Force Qwen3 thinking mode (auto-enabled for 'reasoning' profile)
        raise_on_preempt: If True, raise LLMPreemptedError instead of returning None on preemption.
                          Allows callers to retry after the high-priority request finishes.

    Returns:
        LLM response text, or None if failed/timeout

    Raises:
        LLMPreemptedError: If raise_on_preempt=True and request was preempted.
    """
    _ensure_worker()

    req = LLMRequest(
        priority=priority.value,
        timestamp=time.monotonic(),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        profile=profile,
        caller=caller,
        enable_thinking=enable_thinking,
    )

    # Preemption: if this is a high-priority request, cancel queued low-priority ones
    if priority.value <= Priority.ATLAS.value:
        with _pending_lock:
            preempted = 0
            for pending in _pending_requests:
                if (pending.priority - priority.value >= PREEMPT_PRIORITY_DELTA
                        and not pending.done.is_set()):
                    pending.cancelled.set()
                    preempted += 1
            if preempted:
                _stats["preemptions"] += preempted
                logger.info(
                    f"LLM P{priority.value} [{caller}] preempted {preempted} "
                    f"low-priority requests in queue"
                )

    # Track pending request
    with _pending_lock:
        _pending_requests.append(req)

    _queue.put(req)

    # Wait for completion
    completed = req.done.wait(timeout=timeout_s)

    # Cleanup tracking
    with _pending_lock:
        try:
            _pending_requests.remove(req)
        except ValueError:
            pass

    if completed:
        if req.error:
            if req.error == "preempted_in_queue":
                if raise_on_preempt:
                    raise LLMPreemptedError(
                        f"P{priority.value} [{caller}] preempted by higher-priority request"
                    )
                logger.info(f"LLM P{priority.value} [{caller}] was preempted")
            else:
                logger.warning(f"LLM P{priority.value} [{caller}] error: {req.error}")
            return None
        return req.result
    else:
        logger.warning(f"LLM P{priority.value} [{caller}] timed out after {timeout_s}s")
        return None


def llm_generate_with_thinking(
    system_prompt: str,
    user_prompt: str,
    priority: Priority = Priority.BACKGROUND,
    profile: str = "reasoning",
    caller: str = "unknown",
    timeout_s: float = 120.0,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Submit an LLM request with thinking mode enabled.

    Returns:
        (response_text, thinking_chain) — thinking_chain may be None
    """
    _ensure_worker()

    req = LLMRequest(
        priority=priority.value,
        timestamp=time.monotonic(),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        profile=profile,
        caller=caller,
        enable_thinking=True,
    )

    _queue.put(req)

    if req.done.wait(timeout=timeout_s):
        if req.error:
            logger.warning(f"LLM P{priority.value} [{caller}] thinking error: {req.error}")
            return (None, None)
        return (req.result, req.thinking)
    else:
        logger.warning(f"LLM P{priority.value} [{caller}] thinking timed out after {timeout_s}s")
        return (None, None)


def get_queue_depth() -> int:
    """Get current number of waiting requests."""
    return _queue.qsize()


# Convenience wrappers for common callers
def aaron_query(system_prompt: str, user_prompt: str, profile: str = "voice") -> Optional[str]:
    """Aaron's direct query — highest priority."""
    return llm_generate(system_prompt, user_prompt, Priority.AARON, profile, "aaron")


def atlas_query(system_prompt: str, user_prompt: str, profile: str = "coding") -> Optional[str]:
    """Atlas webhook/injection query — P2."""
    return llm_generate(system_prompt, user_prompt, Priority.ATLAS, profile, "atlas")


def webhook_query(system_prompt: str, user_prompt: str, profile: str = "coding") -> Optional[str]:
    """Webhook pre-computation query — P2, outranks background work.

    Use for any LLM call that feeds webhook injection sections (S2 wisdom,
    S6 holistic, S7 librarian reranking).  Processed right after current
    task finishes, before any background gold-mining / batch jobs.
    """
    return llm_generate(system_prompt, user_prompt, Priority.ATLAS, profile, "webhook")


def s2_professor_query(system_prompt: str, user_prompt: str, brief: bool = False) -> Optional[str]:
    """S2 Professor wisdom query — P2 ATLAS, model decides thinking naturally.

    Used by webhook injection for Section 2 (Professor Wisdom).
    brief=True for one_thing_only depth (700 tokens).
    """
    profile = "s2_professor_brief" if brief else "s2_professor"
    return llm_generate(system_prompt, user_prompt, Priority.ATLAS, profile, "s2_professor")


def s8_synaptic_query(system_prompt: str, user_prompt: str) -> Tuple[Optional[str], Optional[str]]:
    """S8 Synaptic voice query — P2 ATLAS, model decides thinking naturally.

    Used by webhook injection for Section 8 (8th Intelligence).
    Returns (response_text, thinking_chain). Budget: 1500 tok.
    """
    return llm_generate_with_thinking(
        system_prompt, user_prompt,
        priority=Priority.ATLAS,
        profile="s8_synaptic",
        caller="s8_synaptic",
    )


def synaptic_chat_query(system_prompt: str, user_prompt: str, profile: str = "synaptic_chat") -> Optional[str]:
    """Aaron's Synaptic chat query — P1 AARON, highest priority.

    Aaron's direct conversation with Synaptic. Preempts all other LLM work.
    """
    return llm_generate(system_prompt, user_prompt, Priority.AARON, profile, "synaptic_chat",
                        timeout_s=180.0)


def butler_query(system_prompt: str, user_prompt: str, profile: str = "explore") -> Optional[str]:
    """Butler background task query — lowest priority."""
    return llm_generate(system_prompt, user_prompt, Priority.BACKGROUND, profile, "butler")


def butler_deep_query(
    system_prompt: str,
    user_prompt: str,
    context_summary: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """Butler reasoning query with full context + thinking mode.

    Prepends context_summary to user prompt for grounded multi-step reasoning.
    Returns (response, thinking_chain).
    """
    enriched_prompt = user_prompt
    if context_summary:
        enriched_prompt = f"Context:\n{context_summary}\n\nTask:\n{user_prompt}"
    return llm_generate_with_thinking(
        system_prompt, enriched_prompt,
        priority=Priority.BACKGROUND,
        profile="reasoning",
        caller="butler_deep",
    )


# =============================================================================
# GG4 — MavKa-inspired typed annotations (PURELY ADDITIVE, no behavior change)
# =============================================================================
# Reference: .fleet/audits/2026-05-07-FF3-multi-provider-abstraction-diff.md
# Reference: .fleet/audits/2026-05-07-GG4-cost-tier-role-class-context-limit.md
#
# Extension contract (DO NOT BREAK):
#   - Existing call sites of `llm_generate`, `Priority`, profile dispatch, and
#     external provider routing are UNTOUCHED. New symbols below are opt-in.
#   - New enums use string values so adding members is non-breaking. NEVER
#     write an exhaustive `match`/`if/elif` ladder over these enums in core
#     dispatch — always treat unknown members as "default tier / pass-through".
#   - `llm_generate_typed(...)` is the typed wrapper; it delegates to the
#     existing `llm_generate(...)` after annotation lookup + cap enforcement.
#     Kwargs not in the typed signature are forwarded verbatim.
#   - `LLM_MAX_COST_TIER` env var (LOW|MID|HIGH; empty = no cap). Requests
#     above the cap are DOWNGRADED, never dropped — counter +1 + stderr WARN.
#   - ZSF: every downgrade is observable via `cost_tier_downgrade_total`
#     counter (exposed through `get_zsf_counters()` once daemon scrapes it).
# =============================================================================


class CostTier(enum.Enum):
    """Per-call cost ceiling. Orthogonal to ``Priority`` (concurrency) and
    ``RoleClass`` (capability).

    - ``LOW``  — local MLX (Qwen3-4B, $0) and DeepSeek-cheap paths.
    - ``MID``  — DeepSeek primary (deepseek-chat / deepseek-reasoner).
    - ``HIGH`` — Claude / GPT-4.1-class models.

    String values so future tiers (e.g. ``"FREE"``, ``"ULTRA"``) can be
    added without renumbering. Always compare via member identity, never
    via raw string, to keep callers extension-safe.
    """
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class RoleClass(enum.Enum):
    """Capability/role axis (MavKa-style). Pure metadata for now — no
    routing impact in this commit. Future iterations may use this to
    escalate ``GENERALIST`` → ``SPECIALIST`` on hard reasoning failures
    or to route ``MEMORY`` calls to a long-context provider.

    - ``GENERALIST`` — default for everyday calls (deepseek-chat, mlx).
    - ``SPECIALIST`` — surgery / hard reasoning (deepseek-reasoner, Claude).
    - ``JUDGE``      — adversarial reviewer (3-Surgeons cardio/neuro).
    - ``MEMORY``     — long-context retrieval / summarization.
    """
    GENERALIST = "generalist"
    SPECIALIST = "specialist"
    JUDGE = "judge"
    MEMORY = "memory"


# Profile → default CostTier annotation. Used by `llm_generate_typed` and
# any future cap-aware router. NOT consulted by existing `llm_generate`.
# Profiles absent from this map default to MID — same behavior as today.
PROFILE_DEFAULT_COST_TIER: dict = {
    # Fast / local-eligible profiles (no external by default → LOW).
    "classify": CostTier.LOW,
    "extract": CostTier.LOW,
    "summarize": CostTier.LOW,
    "voice": CostTier.LOW,
    # Standard profiles → MID (DeepSeek primary).
    "coding": CostTier.MID,
    "explore": CostTier.MID,
    "extract_deep": CostTier.MID,
    "reasoning": CostTier.MID,
    "deep": CostTier.MID,
    "synaptic_chat": CostTier.MID,
    "post_analysis": CostTier.MID,
    "s2_professor": CostTier.MID,
    "s2_professor_brief": CostTier.MID,
    "s8_synaptic": CostTier.MID,
    # Chain reasoning profiles → MID.
    "chain_narrow": CostTier.MID,
    "chain_extract": CostTier.MID,
    "chain_creative": CostTier.MID,
    "chain_thinking": CostTier.MID,
}


# ZSF counter for downgrade events (additive — register once on import).
with _zsf_lock:
    _zsf_counters.setdefault("cost_tier_downgrade_total", 0)


def _cost_tier_rank(tier: "CostTier") -> int:
    """Rank tiers low→high for comparison. Unknown tiers rank as MID (1)
    so future additions don't accidentally bypass caps."""
    return {CostTier.LOW: 0, CostTier.MID: 1, CostTier.HIGH: 2}.get(tier, 1)


def _resolve_max_cost_tier_env() -> Optional["CostTier"]:
    """Read ``LLM_MAX_COST_TIER`` env var; return None if unset/invalid.

    ZSF: invalid values log a one-shot WARN to stderr but never raise —
    same philosophy as `_zsf_record`. Empty string == no cap (default).
    """
    raw = os.environ.get("LLM_MAX_COST_TIER", "").strip().lower()
    if not raw:
        return None
    for member in CostTier:
        if member.value == raw:
            return member
    # Unrecognized → log once, treat as no-cap to stay fail-open.
    print(
        f"[ZSF] LLM_MAX_COST_TIER={raw!r} not in "
        f"{{low,mid,high}} — ignoring (treating as no cap)",
        file=_sys.stderr,
        flush=True,
    )
    return None


def apply_cost_tier_cap(
    requested: "CostTier",
    cap: Optional["CostTier"] = None,
    *,
    caller: str = "unknown",
    profile: str = "unknown",
) -> "CostTier":
    """Return ``requested`` clamped to ``cap`` (or env default).

    If ``requested`` exceeds ``cap``, increment the
    ``cost_tier_downgrade_total`` counter and emit a structured stderr
    WARN. Never raises — fail-open is the contract.

    Pure function (modulo counter increment). Safe to call without the
    queue worker running. Useful in tests.
    """
    if cap is None:
        cap = _resolve_max_cost_tier_env()
    if cap is None:
        return requested
    if _cost_tier_rank(requested) <= _cost_tier_rank(cap):
        return requested
    # Downgrade — observable.
    try:
        with _zsf_lock:
            _zsf_counters["cost_tier_downgrade_total"] = (
                _zsf_counters.get("cost_tier_downgrade_total", 0) + 1
            )
    except Exception:
        pass
    print(
        f"[ZSF] cost_tier_downgrade caller={caller} profile={profile} "
        f"requested={requested.value} cap={cap.value} → using {cap.value}",
        file=_sys.stderr,
        flush=True,
    )
    return cap


def llm_generate_typed(
    system_prompt: str,
    user_prompt: str,
    priority: "Priority" = Priority.BACKGROUND,
    profile: str = "coding",
    caller: str = "unknown",
    timeout_s: float = 120.0,
    enable_thinking: bool = False,
    raise_on_preempt: bool = False,
    *,
    cost_tier: Optional["CostTier"] = None,
    role: Optional["RoleClass"] = None,
    max_cost_tier: Optional["CostTier"] = None,
) -> Optional[str]:
    """Typed wrapper around ``llm_generate``. Opt-in extras:

    - ``cost_tier``     — explicit per-call ceiling. Defaults to the
                          ``PROFILE_DEFAULT_COST_TIER`` lookup (or MID).
    - ``role``          — ``RoleClass`` metadata (currently unused for
                          routing; recorded for future escalation).
    - ``max_cost_tier`` — per-call override of ``LLM_MAX_COST_TIER`` env
                          var. None → env wins; ``CostTier.X`` → that.

    Behavior:
    1. Resolve ``cost_tier`` (arg → profile-default → MID).
    2. Apply cap (``max_cost_tier`` arg → env var → no cap), downgrading
       and incrementing the ZSF counter if needed.
    3. Delegate to ``llm_generate(...)`` with the original kwargs. The
       resolved cost_tier/role are NOT passed through (existing API is
       untouched) — they only gate dispatch here.

    Existing callers of ``llm_generate`` are unaffected.
    """
    requested = (
        cost_tier
        if cost_tier is not None
        else PROFILE_DEFAULT_COST_TIER.get(profile, CostTier.MID)
    )
    _ = apply_cost_tier_cap(
        requested,
        cap=max_cost_tier,
        caller=caller,
        profile=profile,
    )
    # role is metadata-only in this commit — record-and-forget.
    if role is not None:
        logger.debug(
            "llm_generate_typed role=%s caller=%s profile=%s",
            role.value, caller, profile,
        )
    return llm_generate(
        system_prompt,
        user_prompt,
        priority=priority,
        profile=profile,
        caller=caller,
        timeout_s=timeout_s,
        enable_thinking=enable_thinking,
        raise_on_preempt=raise_on_preempt,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(f"Queue stats: {get_queue_stats()}")
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Testing LLM priority queue...")
        result = llm_generate(
            "You are a helpful assistant.",
            "Say hello in exactly 5 words.",
            priority=Priority.ATLAS,
            caller="test",
            timeout_s=30,
        )
        print(f"Result: {result}")
        print(f"Stats: {get_queue_stats()}")
    else:
        print("Usage: python llm_priority_queue.py [stats|test]")
