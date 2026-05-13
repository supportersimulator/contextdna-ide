#!/usr/bin/env python3
"""
Fleet Repair Guard — cascading failure prevention for self-healing loops.

Hardens against:
1. Cycle detection (A repairs B repairs A)
2. Global repair rate limit (5/10min fleet-wide)
3. Repair agent spawn guard (PID file, max 2 concurrent, stale kill)
4. Exponential backoff on repeated failures (60s→120s→300s→600s)
5. Emergency circuit breaker (>50% fail rate → 15min halt)
6. Audit log with rotation (/tmp/fleet-repair-audit.log, max 1000 lines)

Usage:
    from tools.fleet_repair_guard import RepairGuard
    guard = RepairGuard()

    # Before any repair action:
    if not guard.allow_repair(target, action, source):
        return  # blocked by guard

    # After repair completes:
    guard.record_result(target, action, success=True)

    # Before spawning claude -p repair agent:
    if not guard.allow_agent_spawn():
        return  # too many agents or circuit breaker active

    # After agent finishes:
    guard.release_agent_slot(pid)
"""

import logging
import os
import signal
import time
from pathlib import Path
from threading import Lock
from typing import Optional

logger = logging.getLogger("fleet_repair_guard")

# ── ZSF — Zero Silent Failures invariant ────────────────────────────────────
# Every formerly-silent ``except: pass`` site bumps a named counter here,
# surfaced via ``RepairGuard.get_status()['zsf_swallows']``. Goal: when a
# subtle failure mode emerges, ops can grep the counter rather than wading
# through a "bowl of rotten spaghetti" of invisible swallows. EEE4 sweep.
_ZSF_COUNTERS: dict[str, int] = {}


def _zsf_swallow(site: str, exc: BaseException) -> None:
    """Record a deliberately-tolerated exception. ZSF invariant: never silent.

    Bumps ``_ZSF_COUNTERS[site]`` AND logs at debug (no stack trace — these
    are tolerated failures, not crashes). The counter is surfaced via
    :meth:`RepairGuard.get_status` so /health endpoints can observe trends.
    """
    _ZSF_COUNTERS[site] = _ZSF_COUNTERS.get(site, 0) + 1
    logger.debug("zsf-swallow %s: %s: %s", site, type(exc).__name__, exc)

# ── Constants ──
REPAIR_AUDIT_LOG = Path("/tmp/fleet-repair-audit.log")
REPAIR_AGENT_PID_DIR = Path("/tmp")
REPAIR_AGENT_PID_PREFIX = "fleet-repair-agent"
MAX_AUDIT_LINES = 1000

# Rate limits
GLOBAL_RATE_WINDOW_S = 600       # 10 minutes
GLOBAL_RATE_MAX = 5              # max 5 repairs per window

# Cycle detection
MAX_CHAIN_DEPTH = 3

# Backoff
BACKOFF_SCHEDULE = [60, 120, 300, 600]  # seconds

# Circuit breaker
CIRCUIT_BREAKER_WINDOW_S = 600   # 10 min
CIRCUIT_BREAKER_FAIL_RATIO = 0.5 # >50% failures
CIRCUIT_BREAKER_MIN_SAMPLES = 4  # need at least 4 attempts to trigger
CIRCUIT_BREAKER_COOLDOWN_S = 900 # 15 min halt

# Agent spawn
MAX_CONCURRENT_AGENTS = 2
STALE_AGENT_TIMEOUT_S = 600      # 10 min


class RepairGuard:
    """Centralized repair hardening — prevents cascading failures."""

    def __init__(self):
        self._lock = Lock()

        # 1. Cycle detection: {target: {action: [source_chain]}}
        self._active_repair_chains: dict[str, dict[str, list[str]]] = {}

        # 2. Global rate limit: list of timestamps
        self._global_repair_times: list[float] = []

        # 4. Backoff tracking: "target:action" -> {fail_count, next_allowed_at}
        self._backoff_state: dict[str, dict] = {}

        # 5. Circuit breaker: recent results
        self._recent_results: list[dict] = []  # [{ts, success}]
        self._circuit_open_until: float = 0.0

        # 6. Agent PIDs being tracked
        self._tracked_agent_pids: list[int] = []

    # ── 1. Cycle Detection ──

    def _check_cycle(self, target: str, action: str, source: str) -> bool:
        """Returns True if this repair would create a cycle. Blocks if depth > MAX_CHAIN_DEPTH."""
        chain_key = f"{target}:{action}"

        if target not in self._active_repair_chains:
            self._active_repair_chains[target] = {}

        if action not in self._active_repair_chains[target]:
            self._active_repair_chains[target][action] = []

        chain = self._active_repair_chains[target][action]

        # Cycle: source is already in the chain (A->B->A)
        if source in chain:
            logger.warning(
                f"CYCLE DETECTED: {source} -> {target}:{action}, chain={chain}. Breaking cycle.")
            self._audit("CYCLE_BLOCKED", target, action, f"source={source}, chain={chain}")
            return True

        # Depth exceeded
        if len(chain) >= MAX_CHAIN_DEPTH:
            logger.warning(
                f"CHAIN DEPTH EXCEEDED: {target}:{action}, depth={len(chain)}/{MAX_CHAIN_DEPTH}")
            self._audit("DEPTH_BLOCKED", target, action, f"depth={len(chain)}")
            return True

        return False

    def _push_chain(self, target: str, action: str, source: str):
        """Record that source initiated a repair of target:action."""
        if target not in self._active_repair_chains:
            self._active_repair_chains[target] = {}
        if action not in self._active_repair_chains[target]:
            self._active_repair_chains[target][action] = []
        self._active_repair_chains[target][action].append(source)

    def _pop_chain(self, target: str, action: str, source: str):
        """Remove source from the repair chain after completion."""
        try:
            chain = self._active_repair_chains.get(target, {}).get(action, [])
            if source in chain:
                chain.remove(source)
            # Clean up empty entries
            if not chain and target in self._active_repair_chains:
                self._active_repair_chains[target].pop(action, None)
            if target in self._active_repair_chains and not self._active_repair_chains[target]:
                del self._active_repair_chains[target]
        except Exception as _zsf_e:
            _zsf_swallow("pop_chain", _zsf_e)

    # ── 2. Global Rate Limit ──

    def _check_global_rate(self) -> bool:
        """Returns True if rate limit exceeded (should block)."""
        now = time.time()
        cutoff = now - GLOBAL_RATE_WINDOW_S
        self._global_repair_times = [t for t in self._global_repair_times if t > cutoff]
        if len(self._global_repair_times) >= GLOBAL_RATE_MAX:
            logger.warning(
                f"GLOBAL RATE LIMIT: {len(self._global_repair_times)}/{GLOBAL_RATE_MAX} "
                f"repairs in {GLOBAL_RATE_WINDOW_S}s window")
            self._audit("RATE_LIMITED", "fleet", "global",
                        f"{len(self._global_repair_times)} repairs in window")
            return True
        return False

    def _record_global_repair(self):
        """Record a repair action for rate limiting."""
        self._global_repair_times.append(time.time())

    # ── 3. Repair Agent Spawn Guard ──

    def allow_agent_spawn(self) -> bool:
        """Check if we can spawn another repair agent. Cleans stale PIDs."""
        with self._lock:
            if self._is_circuit_open():
                logger.warning("CIRCUIT BREAKER OPEN: agent spawn blocked")
                return False

            self._clean_stale_agents()
            alive = self._count_alive_agents()

            if alive >= MAX_CONCURRENT_AGENTS:
                logger.warning(
                    f"AGENT LIMIT: {alive}/{MAX_CONCURRENT_AGENTS} repair agents running")
                self._audit("AGENT_BLOCKED", "local", "spawn",
                            f"{alive} agents running")
                return False
            return True

    def register_agent(self, pid: int):
        """Track a newly spawned repair agent PID."""
        with self._lock:
            # Write PID file
            pid_file = REPAIR_AGENT_PID_DIR / f"{REPAIR_AGENT_PID_PREFIX}-{pid}.pid"
            try:
                pid_file.write_text(f"{pid}\n{time.time()}\n")
            except Exception as e:
                logger.warning(f"Failed to write PID file {pid_file}: {e}")
            if pid not in self._tracked_agent_pids:
                self._tracked_agent_pids.append(pid)
            self._audit("AGENT_SPAWNED", "local", "spawn", f"pid={pid}")

    def release_agent_slot(self, pid: int):
        """Clean up after a repair agent finishes."""
        with self._lock:
            if pid in self._tracked_agent_pids:
                self._tracked_agent_pids.remove(pid)
            pid_file = REPAIR_AGENT_PID_DIR / f"{REPAIR_AGENT_PID_PREFIX}-{pid}.pid"
            try:
                pid_file.unlink(missing_ok=True)
            except Exception as _zsf_e:
                _zsf_swallow("release_agent_slot_unlink", _zsf_e)

    def _clean_stale_agents(self):
        """Kill agents running > STALE_AGENT_TIMEOUT_S and remove dead PIDs."""
        now = time.time()
        still_alive = []

        for pid in list(self._tracked_agent_pids):
            if not self._pid_alive(pid):
                self._cleanup_pid(pid)
                continue

            # Check age from PID file
            pid_file = REPAIR_AGENT_PID_DIR / f"{REPAIR_AGENT_PID_PREFIX}-{pid}.pid"
            started_at = self._read_pid_start_time(pid_file)
            if started_at and (now - started_at) > STALE_AGENT_TIMEOUT_S:
                logger.warning(f"STALE AGENT: pid={pid}, age={int(now - started_at)}s — killing")
                self._audit("AGENT_KILLED_STALE", "local", "cleanup", f"pid={pid}")
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError) as _zsf_e:
                    # Already dead or no permission — record so a flood of
                    # stale-kill failures becomes visible.
                    _zsf_swallow("stale_agent_sigterm", _zsf_e)
                self._cleanup_pid(pid)
                continue

            still_alive.append(pid)

        self._tracked_agent_pids = still_alive

        # Also scan for orphaned PID files
        try:
            for f in REPAIR_AGENT_PID_DIR.glob(f"{REPAIR_AGENT_PID_PREFIX}-*.pid"):
                try:
                    pid = int(f.stem.split("-")[-1])
                    if not self._pid_alive(pid):
                        f.unlink(missing_ok=True)
                    elif pid not in self._tracked_agent_pids:
                        # Orphaned but alive — check if stale
                        started_at = self._read_pid_start_time(f)
                        if started_at and (now - started_at) > STALE_AGENT_TIMEOUT_S:
                            logger.warning(f"ORPHANED STALE AGENT: pid={pid} — killing")
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except (ProcessLookupError, PermissionError) as _zsf_e:
                                _zsf_swallow("orphan_agent_sigterm", _zsf_e)
                            f.unlink(missing_ok=True)
                        else:
                            self._tracked_agent_pids.append(pid)
                except (ValueError, IndexError) as _zsf_e:
                    # Malformed PID filename (someone created a non-int).
                    _zsf_swallow("orphan_pidfile_parse", _zsf_e)
        except Exception as _zsf_e:
            _zsf_swallow("orphan_pidfile_glob", _zsf_e)

    def _count_alive_agents(self) -> int:
        """Count currently alive repair agents."""
        return sum(1 for pid in self._tracked_agent_pids if self._pid_alive(pid))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    @staticmethod
    def _read_pid_start_time(pid_file: Path) -> Optional[float]:
        try:
            lines = pid_file.read_text().strip().split("\n")
            if len(lines) >= 2:
                return float(lines[1])
        except Exception as _zsf_e:
            _zsf_swallow("read_pid_start_time", _zsf_e)
        return None

    def _cleanup_pid(self, pid: int):
        if pid in self._tracked_agent_pids:
            self._tracked_agent_pids.remove(pid)
        pid_file = REPAIR_AGENT_PID_DIR / f"{REPAIR_AGENT_PID_PREFIX}-{pid}.pid"
        try:
            pid_file.unlink(missing_ok=True)
        except Exception as _zsf_e:
            _zsf_swallow("cleanup_pid_unlink", _zsf_e)

    # ── 4. Exponential Backoff ──

    def _check_backoff(self, target: str, action: str) -> bool:
        """Returns True if we should wait (backoff active). False = proceed."""
        key = f"{target}:{action}"
        state = self._backoff_state.get(key)
        if not state:
            return False
        now = time.time()
        if now < state["next_allowed_at"]:
            remaining = int(state["next_allowed_at"] - now)
            logger.debug(
                f"BACKOFF ACTIVE: {key}, {remaining}s remaining "
                f"(fail_count={state['fail_count']})")
            return True
        return False

    def _record_backoff_failure(self, target: str, action: str):
        """Record a failure and compute next backoff delay."""
        key = f"{target}:{action}"
        state = self._backoff_state.get(key, {"fail_count": 0, "next_allowed_at": 0})
        state["fail_count"] = min(state["fail_count"] + 1, len(BACKOFF_SCHEDULE))
        idx = min(state["fail_count"] - 1, len(BACKOFF_SCHEDULE) - 1)
        delay = BACKOFF_SCHEDULE[idx]
        state["next_allowed_at"] = time.time() + delay
        self._backoff_state[key] = state
        logger.info(f"BACKOFF SET: {key}, fail_count={state['fail_count']}, delay={delay}s")

    def _reset_backoff(self, target: str, action: str):
        """Reset backoff on success."""
        key = f"{target}:{action}"
        if key in self._backoff_state:
            logger.info(f"BACKOFF RESET: {key} (success)")
            del self._backoff_state[key]

    # ── 5. Emergency Circuit Breaker ──

    def _is_circuit_open(self) -> bool:
        """Returns True if circuit breaker is open (all repairs halted)."""
        now = time.time()
        if now < self._circuit_open_until:
            remaining = int(self._circuit_open_until - now)
            logger.warning(f"CIRCUIT BREAKER OPEN: {remaining}s remaining")
            return True
        return False

    def _evaluate_circuit_breaker(self):
        """Check if >50% of recent repairs failed — if so, open circuit breaker."""
        now = time.time()
        cutoff = now - CIRCUIT_BREAKER_WINDOW_S
        recent = [r for r in self._recent_results if r["ts"] > cutoff]
        self._recent_results = recent  # prune old entries

        if len(recent) < CIRCUIT_BREAKER_MIN_SAMPLES:
            return

        failures = sum(1 for r in recent if not r["success"])
        ratio = failures / len(recent)

        if ratio > CIRCUIT_BREAKER_FAIL_RATIO:
            self._circuit_open_until = now + CIRCUIT_BREAKER_COOLDOWN_S
            logger.error(
                f"EMERGENCY CIRCUIT BREAKER OPEN: {failures}/{len(recent)} repairs failed "
                f"({ratio:.0%}). All self-healing STOPPED for {CIRCUIT_BREAKER_COOLDOWN_S // 60}min.")
            self._audit("CIRCUIT_BREAKER_OPEN", "fleet", "emergency",
                        f"{failures}/{len(recent)} failed ({ratio:.0%})")
            # Alert user
            self._alert_user(
                f"Fleet repair circuit breaker OPEN: {failures}/{len(recent)} repairs failed "
                f"in last {CIRCUIT_BREAKER_WINDOW_S // 60}min. Self-healing paused for "
                f"{CIRCUIT_BREAKER_COOLDOWN_S // 60}min. Check /tmp/fleet-repair-audit.log")

    def _alert_user(self, message: str):
        """Best-effort macOS notification to the user."""
        try:
            import subprocess
            # Sanitize for AppleScript — remove quotes
            safe_msg = message.replace('"', "'").replace("\\", "")[:200]
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe_msg}" with title "Fleet Repair Guard" sound name "Basso"'],
                capture_output=True, timeout=3,
            )
        except Exception as _zsf_e:
            # Best-effort UI alert — not having osascript (CI/Linux) is fine
            # but a flood of failures means notifications are broken.
            _zsf_swallow("alert_user_osascript", _zsf_e)

    # ── 6. Audit Log ──

    def _audit(self, event: str, target: str, action: str, detail: str = ""):
        """Write to audit log. Never logs secrets. Rotates at MAX_AUDIT_LINES."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        # Sanitize: strip anything that looks like a key/token/secret
        safe_detail = detail
        for word in detail.split():
            if len(word) > 20 and any(c in word for c in "=/:@"):
                safe_detail = safe_detail.replace(word, "<REDACTED>")

        line = f"[{ts}] {event} target={target} action={action} {safe_detail}\n"
        try:
            with open(REPAIR_AUDIT_LOG, "a") as f:
                f.write(line)
            self._rotate_audit_log()
        except Exception as e:
            logger.debug(f"Audit log write failed: {e}")

    def _rotate_audit_log(self):
        """Keep audit log under MAX_AUDIT_LINES."""
        try:
            lines = REPAIR_AUDIT_LOG.read_text().splitlines(keepends=True)
            if len(lines) > MAX_AUDIT_LINES:
                # Keep the most recent half
                keep = lines[len(lines) - (MAX_AUDIT_LINES // 2):]
                REPAIR_AUDIT_LOG.write_text("".join(keep))
        except Exception as _zsf_e:
            _zsf_swallow("rotate_audit_log", _zsf_e)

    # ── Public API ──

    def allow_repair(self, target: str, action: str, source: str) -> bool:
        """Central gate: returns True if repair is allowed, False if blocked.

        Checks (in order):
        1. Circuit breaker (emergency halt)
        2. Global rate limit (5/10min)
        3. Cycle detection (A->B->A)
        4. Exponential backoff (repeated failures)
        """
        with self._lock:
            # 5. Circuit breaker
            if self._is_circuit_open():
                return False

            # 2. Global rate limit
            if self._check_global_rate():
                return False

            # 1. Cycle detection
            if self._check_cycle(target, action, source):
                return False

            # 4. Backoff
            if self._check_backoff(target, action):
                self._audit("BACKOFF_BLOCKED", target, action,
                            f"source={source}")
                return False

            # All checks passed — record and allow
            self._record_global_repair()
            self._push_chain(target, action, source)
            self._audit("REPAIR_ALLOWED", target, action, f"source={source}")
            return True

    def record_result(self, target: str, action: str, source: str = "",
                      success: bool = True):
        """Record repair outcome. Updates backoff, circuit breaker, chain tracking."""
        with self._lock:
            self._recent_results.append({"ts": time.time(), "success": success})

            if success:
                self._reset_backoff(target, action)
                self._audit("REPAIR_SUCCESS", target, action, f"source={source}")
            else:
                self._record_backoff_failure(target, action)
                self._audit("REPAIR_FAILED", target, action, f"source={source}")

            # Clean up chain
            if source:
                self._pop_chain(target, action, source)

            # Evaluate circuit breaker after every result
            self._evaluate_circuit_breaker()

    def get_status(self) -> dict:
        """Return guard status for health endpoints."""
        with self._lock:
            now = time.time()
            cutoff = now - GLOBAL_RATE_WINDOW_S
            recent_repairs = len([t for t in self._global_repair_times if t > cutoff])
            alive_agents = self._count_alive_agents()
            circuit_open = now < self._circuit_open_until

            recent = [r for r in self._recent_results if r["ts"] > cutoff]
            failures = sum(1 for r in recent if not r["success"])

            return {
                "circuit_breaker_open": circuit_open,
                "circuit_breaker_remaining_s": max(0, int(self._circuit_open_until - now)) if circuit_open else 0,
                "repairs_in_window": recent_repairs,
                "repair_limit": GLOBAL_RATE_MAX,
                "window_s": GLOBAL_RATE_WINDOW_S,
                "active_chains": {t: {a: len(c) for a, c in acts.items()}
                                  for t, acts in self._active_repair_chains.items() if acts},
                "backoff_active": {k: {"fail_count": v["fail_count"],
                                       "remaining_s": max(0, int(v["next_allowed_at"] - now))}
                                  for k, v in self._backoff_state.items()
                                  if now < v["next_allowed_at"]},
                "alive_repair_agents": alive_agents,
                "max_repair_agents": MAX_CONCURRENT_AGENTS,
                "recent_fail_ratio": f"{failures}/{len(recent)}" if recent else "0/0",
                # EEE4 — ZSF invariant: surface previously-silent swallows so
                # /health consumers see them without grepping logs. Always
                # present even when empty so dashboards can rely on the key.
                "zsf_swallows": dict(_ZSF_COUNTERS),
                "zsf_swallows_total": sum(_ZSF_COUNTERS.values()),
            }


# ── Singleton ──
_guard_instance: Optional[RepairGuard] = None
_guard_lock = Lock()


def get_repair_guard() -> RepairGuard:
    """Get or create the singleton RepairGuard instance."""
    global _guard_instance
    if _guard_instance is None:
        with _guard_lock:
            if _guard_instance is None:
                _guard_instance = RepairGuard()
    return _guard_instance
