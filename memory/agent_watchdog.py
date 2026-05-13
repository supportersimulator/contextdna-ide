"""
Agent Watchdog - Synaptic's Autonomous Process Monitor

Monitors spawned agents and background tasks for:
1. Runaway processes (running too long)
2. Zombie processes (stuck, not producing output)
3. Resource hogs (excessive CPU/memory)

PROTECTED PROCESSES (never kill):
- MLX local LLM (Synaptic's brain)
- Context DNA core services
- Helper agent container processes
- Redis, PostgreSQL, RabbitMQ

This module integrates with Synaptic's health monitoring system.
"""

import os
import psutil
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

# Maximum runtime for agent tasks (default: 10 minutes)
DEFAULT_TIMEOUT_SECONDS = 600

# Check interval for monitoring
CHECK_INTERVAL_SECONDS = 30

# CPU threshold for "runaway" detection (percentage)
CPU_THRESHOLD_PERCENT = 50

# Memory threshold for warnings (MB)
MEMORY_THRESHOLD_MB = 500

# Protected process patterns - NEVER KILL THESE
PROTECTED_PATTERNS = [
    "mlx",                    # MLX local LLM (Synaptic's brain)
    "ollama",                 # Ollama backend
    "contextdna",             # Context DNA services
    "helper-agent",           # Helper agent container
    "redis",                  # Redis
    "postgres",               # PostgreSQL
    "rabbitmq",               # RabbitMQ
    "celery",                 # Celery workers
    "docker",                 # Docker processes
    "api_server",             # API servers
    "uvicorn",                # ASGI server
    "gunicorn",               # WSGI server
]

# Patterns that indicate an AGENT task (safe to kill if stuck)
AGENT_PATTERNS = [
    "generate_section",       # Section generation tests
    "persistent_hook",        # Hook structure tests
    "query_learnings",        # Learning queries
    "ask_synaptic",           # Synaptic queries
    "test_section",           # Test harness
]


@dataclass
class AgentProcess:
    """Represents a monitored agent process."""
    pid: int
    name: str
    cmdline: str
    start_time: datetime
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    last_check: datetime = field(default_factory=datetime.now)
    warnings: List[str] = field(default_factory=list)
    is_protected: bool = False


@dataclass
class WatchdogStatus:
    """Current watchdog status for Synaptic health reporting."""
    active: bool = True
    last_check: Optional[datetime] = None
    agents_monitored: int = 0
    agents_killed: int = 0
    warnings: List[str] = field(default_factory=list)
    protected_count: int = 0


class AgentWatchdog:
    """
    Autonomous agent process monitor for Synaptic.

    Integrates with Synaptic's health monitoring to provide:
    - Real-time agent status
    - Automatic cleanup of stuck processes
    - Resource usage alerts
    """

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds
        self.monitored_agents: Dict[int, AgentProcess] = {}
        self.kill_log: List[Tuple[datetime, int, str]] = []
        self.status = WatchdogStatus()
        self._last_scan = None

    def is_protected(self, cmdline: str) -> bool:
        """Check if a process is protected from killing."""
        cmdline_lower = cmdline.lower()
        return any(pattern in cmdline_lower for pattern in PROTECTED_PATTERNS)

    def is_agent_process(self, cmdline: str) -> bool:
        """Check if a process looks like an agent task."""
        cmdline_lower = cmdline.lower()
        return any(pattern in cmdline_lower for pattern in AGENT_PATTERNS)

    def scan_processes(self) -> List[AgentProcess]:
        """Scan for agent-like processes."""
        agents = []
        protected_count = 0

        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'cpu_percent', 'memory_info']):
            try:
                info = proc.info
                cmdline = ' '.join(info.get('cmdline') or [])

                # Skip if no cmdline
                if not cmdline:
                    continue

                # Check if this is a protected process
                if self.is_protected(cmdline):
                    protected_count += 1
                    continue

                # Check if this looks like an agent process
                if self.is_agent_process(cmdline):
                    start_time = datetime.fromtimestamp(info.get('create_time', time.time()))
                    memory_mb = (info.get('memory_info') or psutil._common.pmem(0, 0, 0, 0, 0, 0, 0)).rss / (1024 * 1024)

                    agent = AgentProcess(
                        pid=info['pid'],
                        name=info.get('name', 'unknown'),
                        cmdline=cmdline[:200],  # Truncate for readability
                        start_time=start_time,
                        cpu_percent=info.get('cpu_percent', 0.0) or 0.0,
                        memory_mb=memory_mb,
                        is_protected=False
                    )
                    agents.append(agent)

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        self.status.protected_count = protected_count
        self._last_scan = datetime.now()
        return agents

    def check_for_runaways(self) -> List[AgentProcess]:
        """Identify runaway processes that exceed timeout."""
        runaways = []
        now = datetime.now()

        agents = self.scan_processes()

        for agent in agents:
            runtime = (now - agent.start_time).total_seconds()

            # Check timeout
            if runtime > self.timeout_seconds:
                agent.warnings.append(f"TIMEOUT: Running for {runtime/60:.1f} minutes (limit: {self.timeout_seconds/60:.0f})")
                runaways.append(agent)

            # Check CPU usage
            elif agent.cpu_percent > CPU_THRESHOLD_PERCENT:
                agent.warnings.append(f"HIGH CPU: {agent.cpu_percent:.1f}%")
                # Don't add to runaways yet - just warn

            # Check memory
            elif agent.memory_mb > MEMORY_THRESHOLD_MB:
                agent.warnings.append(f"HIGH MEMORY: {agent.memory_mb:.1f}MB")
                # Don't add to runaways yet - just warn

        self.status.agents_monitored = len(agents)
        self.status.last_check = now

        return runaways

    def kill_runaway(self, agent: AgentProcess, reason: str = "timeout") -> bool:
        """Kill a runaway agent process."""
        try:
            proc = psutil.Process(agent.pid)

            # Double-check it's not protected
            cmdline = ' '.join(proc.cmdline())
            if self.is_protected(cmdline):
                logger.warning(f"Refusing to kill protected process {agent.pid}: {cmdline[:50]}")
                return False

            # Kill it
            proc.kill()

            # Log the kill
            self.kill_log.append((datetime.now(), agent.pid, reason))
            self.status.agents_killed += 1

            logger.info(f"Killed runaway agent PID {agent.pid}: {reason}")
            return True

        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning(f"Could not kill PID {agent.pid}: {e}")
            return False

    def cleanup_runaways(self, dry_run: bool = False) -> List[Tuple[int, str, bool]]:
        """
        Find and kill all runaway processes.

        Args:
            dry_run: If True, report but don't kill

        Returns:
            List of (pid, reason, killed) tuples
        """
        results = []
        runaways = self.check_for_runaways()

        for agent in runaways:
            reason = '; '.join(agent.warnings)

            if dry_run:
                results.append((agent.pid, reason, False))
                logger.info(f"[DRY RUN] Would kill PID {agent.pid}: {reason}")
            else:
                killed = self.kill_runaway(agent, reason)
                results.append((agent.pid, reason, killed))

        return results

    def get_health_status(self) -> Dict:
        """
        Get health status for Synaptic's monitoring.

        Returns dict suitable for inclusion in Section 8 or health endpoints.
        """
        runaways = self.check_for_runaways()

        return {
            "watchdog_active": self.status.active,
            "last_check": self.status.last_check.isoformat() if self.status.last_check else None,
            "agents_monitored": self.status.agents_monitored,
            "agents_killed_session": self.status.agents_killed,
            "protected_processes": self.status.protected_count,
            "current_runaways": len(runaways),
            "runaway_details": [
                {
                    "pid": a.pid,
                    "runtime_minutes": (datetime.now() - a.start_time).total_seconds() / 60,
                    "warnings": a.warnings
                }
                for a in runaways[:5]  # Limit to 5 for readability
            ],
            "timeout_minutes": self.timeout_seconds / 60,
            "kill_log_recent": [
                {"time": t.isoformat(), "pid": p, "reason": r}
                for t, p, r in self.kill_log[-5:]  # Last 5 kills
            ]
        }

    def get_synaptic_message(self) -> str:
        """
        Generate a message for Synaptic to include in health reports.
        """
        runaways = self.check_for_runaways()

        if not runaways:
            return "🟢 Agent Watchdog: All clear. No runaway processes detected."

        lines = [f"⚠️ Agent Watchdog: {len(runaways)} runaway process(es) detected:"]
        for agent in runaways[:3]:
            runtime = (datetime.now() - agent.start_time).total_seconds() / 60
            lines.append(f"   PID {agent.pid}: Running {runtime:.1f}min - {agent.warnings[0] if agent.warnings else 'timeout'}")

        if len(runaways) > 3:
            lines.append(f"   ... and {len(runaways) - 3} more")

        return '\n'.join(lines)


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_watchdog_instance: Optional[AgentWatchdog] = None


def get_watchdog() -> AgentWatchdog:
    """Get the singleton watchdog instance."""
    global _watchdog_instance
    if _watchdog_instance is None:
        _watchdog_instance = AgentWatchdog()
    return _watchdog_instance


def check_agents() -> Dict:
    """Quick check for agent status - for health endpoints."""
    return get_watchdog().get_health_status()


def cleanup_stuck_agents(dry_run: bool = False) -> List[Tuple[int, str, bool]]:
    """Clean up stuck agents - callable from CLI or health monitor."""
    return get_watchdog().cleanup_runaways(dry_run=dry_run)


def get_synaptic_watchdog_message() -> str:
    """Get watchdog status for Synaptic's voice."""
    return get_watchdog().get_synaptic_message()


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent Watchdog - Monitor and clean up stuck agents")
    parser.add_argument("--check", action="store_true", help="Check for runaway agents")
    parser.add_argument("--cleanup", action="store_true", help="Kill runaway agents")
    parser.add_argument("--dry-run", action="store_true", help="Report but don't kill")
    parser.add_argument("--status", action="store_true", help="Show full status")

    args = parser.parse_args()

    watchdog = get_watchdog()

    if args.status:
        import json
        print(json.dumps(watchdog.get_health_status(), indent=2, default=str))

    elif args.check:
        runaways = watchdog.check_for_runaways()
        if runaways:
            print(f"⚠️ Found {len(runaways)} runaway agent(s):")
            for agent in runaways:
                runtime = (datetime.now() - agent.start_time).total_seconds() / 60
                print(f"  PID {agent.pid}: {runtime:.1f} min - {agent.cmdline[:60]}...")
        else:
            print("✅ No runaway agents detected")

    elif args.cleanup:
        results = watchdog.cleanup_runaways(dry_run=args.dry_run)
        if results:
            for pid, reason, killed in results:
                status = "KILLED" if killed else "WOULD KILL" if args.dry_run else "FAILED"
                print(f"  [{status}] PID {pid}: {reason}")
        else:
            print("✅ No agents to clean up")

    else:
        # Default: show Synaptic message
        print(watchdog.get_synaptic_message())
