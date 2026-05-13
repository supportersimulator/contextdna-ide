#!/usr/bin/env python3
"""
DOCKER MCP - Machine Control Plane for Container Introspection

Implements Logos's MCP architecture for Docker introspection:
- Typed tools, no arbitrary shell
- Read-only first; writes are gated
- Every tool call returns telemetry for Synaptic logging
- Permission tiers: read_only → safe_write → destructive

Usage:
    from memory.docker_mcp import DockerMCP, get_health_snapshot

    mcp = DockerMCP()

    # Read-only operations (Tier 0)
    containers = mcp.list_containers()
    stats = mcp.container_stats("contextdna-pg")
    logs = mcp.container_logs("contextdna-helper-agent", tail=100)
    snapshot = mcp.health_snapshot(project="contextdna")

    # Analysis
    analysis = mcp.analyze_resource_usage()
    orphans = mcp.find_orphaned_containers()

Created: February 2, 2026
Author: Atlas (SUPERHERO MODE) with Logos architecture spec
Verifier: Synaptic
"""

import json
import subprocess
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum
import uuid

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================

class RiskTier(str, Enum):
    """Permission tiers for Docker operations."""
    READ_ONLY = "read_only"       # Tier 0: Always allowed
    SAFE_WRITE = "safe_write"     # Tier 1: Synaptic approval
    DESTRUCTIVE = "destructive"   # Tier 2: Human approval required


class ContainerState(str, Enum):
    """Container states."""
    RUNNING = "running"
    CREATED = "created"
    EXITED = "exited"
    PAUSED = "paused"
    RESTARTING = "restarting"
    DEAD = "dead"


@dataclass
class Telemetry:
    """Telemetry envelope for every MCP operation."""
    tool_call_id: str
    tool_name: str
    requested_by: str  # Atlas, Synaptic, SpawnedAgent
    intent: str
    risk_tier: RiskTier
    timestamp: str
    duration_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['risk_tier'] = self.risk_tier.value
        return d


@dataclass
class ContainerInfo:
    """Container information."""
    id: str
    name: str
    image: str
    state: str
    status: str
    created_at: str
    ports: str
    labels: Dict[str, str] = field(default_factory=dict)
    health: str = "unknown"

    def is_healthy(self) -> bool:
        return "healthy" in self.status.lower()

    def is_running(self) -> bool:
        return "Up" in self.status


@dataclass
class ContainerStats:
    """Container resource stats."""
    name: str
    cpu_percent: float
    memory_usage_mb: float
    memory_limit_mb: float
    memory_percent: float
    net_io_rx_mb: float
    net_io_tx_mb: float
    block_io_read_mb: float
    block_io_write_mb: float
    pids: int = 0


@dataclass
class HealthSnapshot:
    """Complete health snapshot of a Docker project."""
    timestamp: str
    project: str
    total_containers: int
    running: int
    healthy: int
    unhealthy: int
    stopped: int
    orphaned: int
    containers: List[ContainerInfo]
    stats: List[ContainerStats]
    top_cpu: List[Dict]
    top_memory: List[Dict]
    alerts: List[str]
    telemetry: Telemetry


@dataclass
class ResourceAnalysis:
    """Resource usage analysis."""
    timestamp: str
    total_memory_mb: float
    total_cpu_percent: float
    potential_waste_mb: float
    recommendations: List[str]
    by_category: Dict[str, Dict]


# =============================================================================
# DOCKER MCP CLASS
# =============================================================================

class DockerMCP:
    """
    Docker Machine Control Plane.

    Provides typed, governed access to Docker introspection.
    All operations return telemetry for Synaptic logging.
    """

    VERSION = "0.1.0"

    def __init__(self, requester: str = "Atlas"):
        self.requester = requester
        self._telemetry_log: List[Telemetry] = []

    # =========================================================================
    # TIER 0: READ-ONLY OPERATIONS (Always Allowed)
    # =========================================================================

    def list_containers(self, all: bool = True, filters: Optional[Dict] = None) -> Tuple[List[ContainerInfo], Telemetry]:
        """
        List containers with status and metadata.

        Args:
            all: Include stopped containers
            filters: Docker filter dict (e.g., {"name": "contextdna"})

        Returns:
            Tuple of (containers list, telemetry)
        """
        import time
        start = time.time()

        telemetry = Telemetry(
            tool_call_id=str(uuid.uuid4())[:8],
            tool_name="docker.list_containers",
            requested_by=self.requester,
            intent="Enumerate containers for health assessment",
            risk_tier=RiskTier.READ_ONLY,
            timestamp=datetime.now().isoformat(),
        )

        try:
            cmd = ["docker", "ps", "--format", "{{.ID}}|{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}|{{.CreatedAt}}|{{.Ports}}"]
            if all:
                cmd.insert(2, "-a")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            containers = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) >= 6:
                    containers.append(ContainerInfo(
                        id=parts[0],
                        name=parts[1],
                        image=parts[2],
                        state=parts[3],
                        status=parts[4],
                        created_at=parts[5],
                        ports=parts[6] if len(parts) > 6 else "",
                    ))

            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return containers, telemetry

        except Exception as e:
            telemetry.success = False
            telemetry.error = str(e)
            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return [], telemetry

    def container_stats(self, container_id: str, sample_seconds: float = 1.0) -> Tuple[Optional[ContainerStats], Telemetry]:
        """
        Get live resource stats for a container.

        Args:
            container_id: Container name or ID
            sample_seconds: Sampling duration

        Returns:
            Tuple of (stats, telemetry)
        """
        import time
        start = time.time()

        telemetry = Telemetry(
            tool_call_id=str(uuid.uuid4())[:8],
            tool_name="docker.container_stats",
            requested_by=self.requester,
            intent=f"Get resource stats for {container_id}",
            risk_tier=RiskTier.READ_ONLY,
            timestamp=datetime.now().isoformat(),
        )

        try:
            cmd = ["docker", "stats", "--no-stream", "--format",
                   "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}",
                   container_id]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                telemetry.success = False
                telemetry.error = result.stderr
                telemetry.duration_ms = (time.time() - start) * 1000
                self._telemetry_log.append(telemetry)
                return None, telemetry

            line = result.stdout.strip()
            if not line:
                return None, telemetry

            parts = line.split('|')
            if len(parts) >= 6:
                # Parse memory usage (e.g., "88.82MiB / 512MiB")
                mem_parts = parts[2].split(' / ')
                mem_usage = self._parse_size(mem_parts[0]) if mem_parts else 0
                mem_limit = self._parse_size(mem_parts[1]) if len(mem_parts) > 1 else 0

                # Parse network I/O (e.g., "115kB / 250kB")
                net_parts = parts[4].split(' / ')
                net_rx = self._parse_size(net_parts[0]) if net_parts else 0
                net_tx = self._parse_size(net_parts[1]) if len(net_parts) > 1 else 0

                # Parse block I/O
                block_parts = parts[5].split(' / ')
                block_read = self._parse_size(block_parts[0]) if block_parts else 0
                block_write = self._parse_size(block_parts[1]) if len(block_parts) > 1 else 0

                stats = ContainerStats(
                    name=parts[0],
                    cpu_percent=float(parts[1].replace('%', '')) if parts[1] else 0,
                    memory_usage_mb=mem_usage,
                    memory_limit_mb=mem_limit,
                    memory_percent=float(parts[3].replace('%', '')) if parts[3] else 0,
                    net_io_rx_mb=net_rx,
                    net_io_tx_mb=net_tx,
                    block_io_read_mb=block_read,
                    block_io_write_mb=block_write,
                    pids=int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0,
                )

                telemetry.duration_ms = (time.time() - start) * 1000
                self._telemetry_log.append(telemetry)
                return stats, telemetry

            return None, telemetry

        except Exception as e:
            telemetry.success = False
            telemetry.error = str(e)
            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return None, telemetry

    def container_logs(self, container_id: str, tail: int = 200,
                       since_seconds: Optional[int] = None,
                       timestamps: bool = True) -> Tuple[List[str], Telemetry]:
        """
        Fetch recent logs for a container.

        Args:
            container_id: Container name or ID
            tail: Number of lines from end
            since_seconds: Only logs newer than this
            timestamps: Include timestamps

        Returns:
            Tuple of (log lines, telemetry)
        """
        import time
        start = time.time()

        telemetry = Telemetry(
            tool_call_id=str(uuid.uuid4())[:8],
            tool_name="docker.container_logs",
            requested_by=self.requester,
            intent=f"Fetch logs for {container_id}",
            risk_tier=RiskTier.READ_ONLY,
            timestamp=datetime.now().isoformat(),
        )

        try:
            cmd = ["docker", "logs", "--tail", str(tail)]
            if timestamps:
                cmd.append("--timestamps")
            if since_seconds:
                cmd.extend(["--since", f"{since_seconds}s"])
            cmd.append(container_id)

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            # Docker logs go to stderr for some containers
            output = result.stdout or result.stderr
            lines = output.strip().split('\n') if output else []

            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return lines, telemetry

        except Exception as e:
            telemetry.success = False
            telemetry.error = str(e)
            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return [], telemetry

    def compose_ps(self, project: str) -> Tuple[List[Dict], Telemetry]:
        """
        List compose services for a project.

        Args:
            project: Compose project name

        Returns:
            Tuple of (services list, telemetry)
        """
        import time
        start = time.time()

        telemetry = Telemetry(
            tool_call_id=str(uuid.uuid4())[:8],
            tool_name="docker.compose_ps",
            requested_by=self.requester,
            intent=f"List services for project {project}",
            risk_tier=RiskTier.READ_ONLY,
            timestamp=datetime.now().isoformat(),
        )

        try:
            cmd = ["docker", "compose", "-p", project, "ps", "--format", "json"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            services = []
            if result.stdout:
                for line in result.stdout.strip().split('\n'):
                    if line:
                        try:
                            services.append(json.loads(line))
                        except json.JSONDecodeError as e:
                            print(f"[WARN] Docker service JSON parse failed: {e}")

            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return services, telemetry

        except Exception as e:
            telemetry.success = False
            telemetry.error = str(e)
            telemetry.duration_ms = (time.time() - start) * 1000
            self._telemetry_log.append(telemetry)
            return [], telemetry

    def health_snapshot(self, project: str = "contextdna", log_tail: int = 50) -> HealthSnapshot:
        """
        Complete health snapshot: containers + health + stats + key logs.

        Args:
            project: Compose project name
            log_tail: Lines of logs to include per container

        Returns:
            HealthSnapshot with full system state
        """
        import time
        start = time.time()

        telemetry = Telemetry(
            tool_call_id=str(uuid.uuid4())[:8],
            tool_name="docker.health_snapshot",
            requested_by=self.requester,
            intent=f"Full health snapshot for {project}",
            risk_tier=RiskTier.READ_ONLY,
            timestamp=datetime.now().isoformat(),
        )

        # Get all containers
        containers, _ = self.list_containers(all=True)

        # Categorize
        running = [c for c in containers if c.is_running()]
        healthy = [c for c in running if c.is_healthy()]
        unhealthy = [c for c in running if not c.is_healthy()]
        stopped = [c for c in containers if not c.is_running() and c.state != "created"]
        orphaned = [c for c in containers if c.state == "created"]

        # Get stats for running containers
        stats = []
        for c in running[:20]:  # Limit to avoid timeout
            s, _ = self.container_stats(c.name)
            if s:
                stats.append(s)

        # Top consumers
        top_cpu = sorted(
            [{"name": s.name, "cpu": s.cpu_percent} for s in stats],
            key=lambda x: x["cpu"],
            reverse=True
        )[:5]

        top_memory = sorted(
            [{"name": s.name, "memory_mb": s.memory_usage_mb} for s in stats],
            key=lambda x: x["memory_mb"],
            reverse=True
        )[:5]

        # Generate alerts
        alerts = []
        for c in unhealthy:
            alerts.append(f"UNHEALTHY: {c.name} ({c.status})")
        for c in orphaned:
            alerts.append(f"ORPHANED: {c.name} (created but never started)")
        for s in stats:
            if s.memory_percent > 90:
                alerts.append(f"HIGH MEMORY: {s.name} at {s.memory_percent}%")
            if s.cpu_percent > 80:
                alerts.append(f"HIGH CPU: {s.name} at {s.cpu_percent}%")

        telemetry.duration_ms = (time.time() - start) * 1000
        self._telemetry_log.append(telemetry)

        return HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            project=project,
            total_containers=len(containers),
            running=len(running),
            healthy=len(healthy),
            unhealthy=len(unhealthy),
            stopped=len(stopped),
            orphaned=len(orphaned),
            containers=containers,
            stats=stats,
            top_cpu=top_cpu,
            top_memory=top_memory,
            alerts=alerts,
            telemetry=telemetry,
        )

    # =========================================================================
    # ANALYSIS METHODS
    # =========================================================================

    def analyze_resource_usage(self) -> ResourceAnalysis:
        """
        Analyze resource usage and identify potential waste.

        Returns:
            ResourceAnalysis with recommendations
        """
        containers, _ = self.list_containers(all=True)

        running = [c for c in containers if c.is_running()]
        orphaned = [c for c in containers if c.state == "created"]
        stopped = [c for c in containers if not c.is_running() and c.state != "created"]

        # Get stats
        stats = []
        for c in running:
            s, _ = self.container_stats(c.name)
            if s:
                stats.append(s)

        total_memory = sum(s.memory_usage_mb for s in stats)
        total_cpu = sum(s.cpu_percent for s in stats)

        # Categorize by purpose
        by_category = {
            "canonical_contextdna": {"count": 0, "memory_mb": 0, "containers": []},
            "parallel_unused": {"count": 0, "memory_mb": 0, "containers": []},
            "monitoring": {"count": 0, "memory_mb": 0, "containers": []},
            "orphaned": {"count": len(orphaned), "memory_mb": 0, "containers": [c.name for c in orphaned]},
            "stopped": {"count": len(stopped), "memory_mb": 0, "containers": [c.name for c in stopped]},
        }

        for s in stats:
            if "contextdna-" in s.name and "context-dna-" not in s.name:
                if any(m in s.name for m in ["prometheus", "grafana", "exporter", "jaeger"]):
                    by_category["monitoring"]["count"] += 1
                    by_category["monitoring"]["memory_mb"] += s.memory_usage_mb
                    by_category["monitoring"]["containers"].append(s.name)
                else:
                    by_category["canonical_contextdna"]["count"] += 1
                    by_category["canonical_contextdna"]["memory_mb"] += s.memory_usage_mb
                    by_category["canonical_contextdna"]["containers"].append(s.name)
            elif "context-dna-" in s.name:
                by_category["parallel_unused"]["count"] += 1
                by_category["parallel_unused"]["memory_mb"] += s.memory_usage_mb
                by_category["parallel_unused"]["containers"].append(s.name)

        # Calculate potential waste
        potential_waste = by_category["parallel_unused"]["memory_mb"]

        # Generate recommendations
        recommendations = []

        if len(orphaned) > 0:
            recommendations.append(f"Remove {len(orphaned)} orphaned containers: docker rm {' '.join(c.name for c in orphaned)}")

        if by_category["parallel_unused"]["count"] > 0:
            recommendations.append(f"Consider stopping {by_category['parallel_unused']['count']} parallel unused containers (saves {by_category['parallel_unused']['memory_mb']:.1f}MB)")

        if by_category["monitoring"]["memory_mb"] > 400:
            recommendations.append(f"Monitoring stack using {by_category['monitoring']['memory_mb']:.1f}MB - consider if all exporters are needed")

        # Check for high-memory containers
        high_mem = [s for s in stats if s.memory_percent > 80]
        if high_mem:
            for s in high_mem:
                recommendations.append(f"High memory: {s.name} at {s.memory_percent}% - may need limit increase or optimization")

        return ResourceAnalysis(
            timestamp=datetime.now().isoformat(),
            total_memory_mb=total_memory,
            total_cpu_percent=total_cpu,
            potential_waste_mb=potential_waste,
            recommendations=recommendations,
            by_category=by_category,
        )

    def find_orphaned_containers(self) -> List[ContainerInfo]:
        """Find containers in 'Created' state that never started."""
        containers, _ = self.list_containers(all=True)
        return [c for c in containers if c.state == "created"]

    def find_parallel_unused(self) -> List[ContainerInfo]:
        """Find parallel/unused containers (context-dna-* vs contextdna-*)."""
        containers, _ = self.list_containers(all=True)
        return [c for c in containers if "context-dna-" in c.name and "contextdna-" not in c.name]

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _parse_size(self, size_str: str) -> float:
        """Parse Docker size string to MB."""
        size_str = size_str.strip()
        multipliers = {
            'B': 1 / (1024 * 1024),
            'kB': 1 / 1024,
            'KB': 1 / 1024,
            'KiB': 1 / 1024,
            'MB': 1,
            'MiB': 1,
            'GB': 1024,
            'GiB': 1024,
        }

        for suffix, mult in multipliers.items():
            if size_str.endswith(suffix):
                try:
                    return float(size_str[:-len(suffix)].strip()) * mult
                except ValueError:
                    return 0

        try:
            return float(size_str)
        except ValueError:
            return 0

    def get_telemetry_log(self) -> List[Dict]:
        """Get all telemetry entries."""
        return [t.to_dict() for t in self._telemetry_log]

    def clear_telemetry_log(self):
        """Clear telemetry log."""
        self._telemetry_log = []


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def get_mcp() -> DockerMCP:
    """Get singleton MCP instance."""
    if not hasattr(get_mcp, "_instance"):
        get_mcp._instance = DockerMCP()
    return get_mcp._instance


def quick_health() -> Dict:
    """Quick health check for webhook injection."""
    mcp = get_mcp()
    snapshot = mcp.health_snapshot()
    return {
        "timestamp": snapshot.timestamp,
        "total": snapshot.total_containers,
        "running": snapshot.running,
        "healthy": snapshot.healthy,
        "unhealthy": snapshot.unhealthy,
        "orphaned": snapshot.orphaned,
        "alerts": snapshot.alerts[:5],
        "top_memory": snapshot.top_memory[:3],
    }


def get_health_snapshot(project: str = "contextdna") -> HealthSnapshot:
    """Get full health snapshot."""
    mcp = get_mcp()
    return mcp.health_snapshot(project=project)


def analyze_waste() -> ResourceAnalysis:
    """Analyze resource usage and waste."""
    mcp = get_mcp()
    return mcp.analyze_resource_usage()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    mcp = DockerMCP()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "health":
            snapshot = mcp.health_snapshot()
            print(f"\n{'='*60}")
            print(f"DOCKER MCP HEALTH SNAPSHOT")
            print(f"{'='*60}")
            print(f"Timestamp: {snapshot.timestamp}")
            print(f"Total: {snapshot.total_containers} | Running: {snapshot.running} | Healthy: {snapshot.healthy}")
            print(f"Unhealthy: {snapshot.unhealthy} | Stopped: {snapshot.stopped} | Orphaned: {snapshot.orphaned}")

            if snapshot.alerts:
                print(f"\n⚠️  ALERTS:")
                for alert in snapshot.alerts:
                    print(f"  - {alert}")

            print(f"\n📊 TOP CPU:")
            for c in snapshot.top_cpu:
                print(f"  {c['name']}: {c['cpu']}%")

            print(f"\n📊 TOP MEMORY:")
            for c in snapshot.top_memory:
                print(f"  {c['name']}: {c['memory_mb']:.1f}MB")

        elif cmd == "analyze":
            analysis = mcp.analyze_resource_usage()
            print(f"\n{'='*60}")
            print(f"DOCKER MCP RESOURCE ANALYSIS")
            print(f"{'='*60}")
            print(f"Total Memory: {analysis.total_memory_mb:.1f}MB")
            print(f"Total CPU: {analysis.total_cpu_percent:.1f}%")
            print(f"Potential Waste: {analysis.potential_waste_mb:.1f}MB")

            print(f"\n📊 BY CATEGORY:")
            for cat, data in analysis.by_category.items():
                if data["count"] > 0:
                    print(f"  {cat}: {data['count']} containers, {data['memory_mb']:.1f}MB")

            if analysis.recommendations:
                print(f"\n💡 RECOMMENDATIONS:")
                for rec in analysis.recommendations:
                    print(f"  - {rec}")

        elif cmd == "orphans":
            orphans = mcp.find_orphaned_containers()
            print(f"\n🔴 ORPHANED CONTAINERS ({len(orphans)}):")
            for c in orphans:
                print(f"  - {c.name} ({c.image})")
            if orphans:
                print(f"\nRemove with: docker rm {' '.join(c.name for c in orphans)}")

        elif cmd == "logs":
            if len(sys.argv) > 2:
                container = sys.argv[2]
                tail = int(sys.argv[3]) if len(sys.argv) > 3 else 50
                logs, _ = mcp.container_logs(container, tail=tail)
                print(f"\n📜 LOGS for {container} (last {tail} lines):\n")
                for line in logs:
                    print(line)

        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python docker_mcp.py [health|analyze|orphans|logs <container> [tail]]")

    else:
        # Default: health snapshot
        snapshot = mcp.health_snapshot()
        print(json.dumps({
            "total": snapshot.total_containers,
            "running": snapshot.running,
            "healthy": snapshot.healthy,
            "alerts": snapshot.alerts,
        }, indent=2))
