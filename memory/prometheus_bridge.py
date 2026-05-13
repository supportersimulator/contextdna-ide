#!/usr/bin/env python3
"""
PROMETHEUS BRIDGE - Real-Time Metrics for Synaptic

Connects Synaptic to Prometheus (port 9090) for live container metrics:
- CPU usage per container
- Memory usage per container
- Request latency
- Error rates
- Queue depths

This gives Synaptic "peripheral vision" - awareness of system strain
before issues become critical.

Usage:
    from memory.prometheus_bridge import PrometheusBridge, get_container_metrics

    bridge = PrometheusBridge()

    # Get all container metrics
    metrics = bridge.get_all_metrics()

    # Get specific container
    pg_metrics = bridge.get_container_metrics("contextdna-pg")

    # Get system overview
    overview = bridge.get_system_overview()

Created: February 2, 2026
Author: Atlas (for Synaptic)
"""

import json
import urllib.request
import urllib.parse
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

PROMETHEUS_URL = "http://localhost:9090"


@dataclass
class ContainerMetrics:
    """Metrics for a single container."""
    name: str
    cpu_percent: float
    memory_mb: float
    memory_percent: float
    network_rx_bytes: float
    network_tx_bytes: float
    timestamp: str

    def is_stressed(self) -> bool:
        """Check if container is under stress."""
        return self.cpu_percent > 80 or self.memory_percent > 80


@dataclass
class SystemOverview:
    """System-wide metrics overview."""
    timestamp: str
    total_containers: int
    total_cpu_percent: float
    total_memory_mb: float
    stressed_containers: List[str]
    top_cpu: List[Dict]
    top_memory: List[Dict]


class PrometheusBridge:
    """
    Bridge to Prometheus metrics.

    Queries Prometheus API for real-time container metrics.
    """

    def __init__(self, prometheus_url: str = PROMETHEUS_URL):
        self.prometheus_url = prometheus_url
        self._cache = {}
        self._cache_time = None
        self._cache_ttl = 10  # seconds

    def _query(self, query: str) -> Optional[Dict]:
        """Execute a PromQL query."""
        try:
            url = f"{self.prometheus_url}/api/v1/query"
            params = urllib.parse.urlencode({"query": query})
            full_url = f"{url}?{params}"

            with urllib.request.urlopen(full_url, timeout=5) as response:
                data = json.loads(response.read())
                if data.get("status") == "success":
                    return data.get("data", {})
                return None
        except Exception as e:
            logger.debug(f"Prometheus query failed: {e}")
            return None

    def _extract_value(self, result: List) -> float:
        """Extract numeric value from Prometheus result."""
        if result and len(result) > 0:
            value = result[0].get("value", [0, 0])
            if len(value) > 1:
                try:
                    return float(value[1])
                except (ValueError, TypeError) as e:
                    print(f"[WARN] Prometheus value extraction failed: {e}")
        return 0.0

    def is_available(self) -> bool:
        """Check if Prometheus is available."""
        try:
            url = f"{self.prometheus_url}/-/healthy"
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def get_container_metrics(self, container_name: str) -> Optional[ContainerMetrics]:
        """Get metrics for a specific container."""
        try:
            # CPU usage (from cAdvisor or Docker metrics)
            cpu_query = f'rate(container_cpu_usage_seconds_total{{name="{container_name}"}}[1m]) * 100'
            cpu_result = self._query(cpu_query)
            cpu = self._extract_value(cpu_result.get("result", [])) if cpu_result else 0

            # Memory usage
            mem_query = f'container_memory_usage_bytes{{name="{container_name}"}}'
            mem_result = self._query(mem_query)
            memory_bytes = self._extract_value(mem_result.get("result", [])) if mem_result else 0
            memory_mb = memory_bytes / (1024 * 1024)

            # Memory percent (need total memory)
            memory_percent = 0
            total_mem_query = "node_memory_MemTotal_bytes"
            total_result = self._query(total_mem_query)
            if total_result:
                total_mem = self._extract_value(total_result.get("result", []))
                if total_mem > 0:
                    memory_percent = (memory_bytes / total_mem) * 100

            # Network I/O
            rx_query = f'container_network_receive_bytes_total{{name="{container_name}"}}'
            tx_query = f'container_network_transmit_bytes_total{{name="{container_name}"}}'
            rx_result = self._query(rx_query)
            tx_result = self._query(tx_query)

            return ContainerMetrics(
                name=container_name,
                cpu_percent=round(cpu, 2),
                memory_mb=round(memory_mb, 2),
                memory_percent=round(memory_percent, 2),
                network_rx_bytes=self._extract_value(rx_result.get("result", [])) if rx_result else 0,
                network_tx_bytes=self._extract_value(tx_result.get("result", [])) if tx_result else 0,
                timestamp=datetime.now().isoformat(),
            )
        except Exception as e:
            logger.warning(f"Failed to get metrics for {container_name}: {e}")
            return None

    def get_all_metrics(self) -> Dict[str, ContainerMetrics]:
        """Get metrics for all Context DNA containers."""
        containers = [
            "contextdna-helper-agent",
            "contextdna-api",
            "contextdna-core",
            "contextdna-ui",
            "contextdna-pg",
            "contextdna-redis",
            "contextdna-rabbitmq",
            "contextdna-opensearch",
            "contextdna-seaweedfs",
            "contextdna-grafana",
            "contextdna-prometheus",
            "contextdna-traefik",
            "contextdna-jaeger",
            "contextdna-celery-worker",
            "contextdna-celery-beat",
            "context-dna-postgres",
            "context-dna-redis",
        ]

        metrics = {}
        for container in containers:
            m = self.get_container_metrics(container)
            if m:
                metrics[container] = m

        return metrics

    def get_system_overview(self) -> SystemOverview:
        """Get system-wide metrics overview."""
        metrics = self.get_all_metrics()

        total_cpu = sum(m.cpu_percent for m in metrics.values())
        total_memory = sum(m.memory_mb for m in metrics.values())
        stressed = [name for name, m in metrics.items() if m.is_stressed()]

        # Top CPU consumers
        top_cpu = sorted(
            [{"name": name, "cpu": m.cpu_percent} for name, m in metrics.items()],
            key=lambda x: x["cpu"],
            reverse=True
        )[:5]

        # Top memory consumers
        top_memory = sorted(
            [{"name": name, "memory_mb": m.memory_mb} for name, m in metrics.items()],
            key=lambda x: x["memory_mb"],
            reverse=True
        )[:5]

        return SystemOverview(
            timestamp=datetime.now().isoformat(),
            total_containers=len(metrics),
            total_cpu_percent=round(total_cpu, 2),
            total_memory_mb=round(total_memory, 2),
            stressed_containers=stressed,
            top_cpu=top_cpu,
            top_memory=top_memory,
        )

    def get_alert_metrics(self) -> List[Dict]:
        """Get any firing Prometheus alerts."""
        try:
            url = f"{self.prometheus_url}/api/v1/alerts"
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read())
                if data.get("status") == "success":
                    alerts = data.get("data", {}).get("alerts", [])
                    return [
                        {
                            "name": a.get("labels", {}).get("alertname"),
                            "state": a.get("state"),
                            "severity": a.get("labels", {}).get("severity", "unknown"),
                            "message": a.get("annotations", {}).get("summary", ""),
                        }
                        for a in alerts
                        if a.get("state") == "firing"
                    ]
                return []
        except Exception as e:
            logger.debug(f"Failed to get alerts: {e}")
            return []


# =========================================================================
# CONVENIENCE FUNCTIONS
# =========================================================================

def get_bridge() -> PrometheusBridge:
    """Get singleton bridge instance."""
    if not hasattr(get_bridge, "_instance"):
        get_bridge._instance = PrometheusBridge()
    return get_bridge._instance


def quick_metrics() -> Dict:
    """Quick metrics check (for webhook injection)."""
    bridge = get_bridge()
    if not bridge.is_available():
        return {"available": False}

    overview = bridge.get_system_overview()
    return {
        "available": True,
        "total_cpu": overview.total_cpu_percent,
        "total_memory_mb": overview.total_memory_mb,
        "stressed": overview.stressed_containers,
        "alerts": len(bridge.get_alert_metrics()),
    }


def get_container_metrics(name: str) -> Optional[Dict]:
    """Get metrics for a container as dict."""
    bridge = get_bridge()
    m = bridge.get_container_metrics(name)
    return asdict(m) if m else None


# =========================================================================
# CLI
# =========================================================================

if __name__ == "__main__":
    import sys

    bridge = PrometheusBridge()

    if not bridge.is_available():
        print("Prometheus not available at", PROMETHEUS_URL)
        print("Make sure contextdna-prometheus container is running")
        sys.exit(1)

    if len(sys.argv) > 1:
        # Get metrics for specific container
        container = sys.argv[1]
        metrics = bridge.get_container_metrics(container)
        if metrics:
            print(f"\n=== {container} ===")
            print(f"CPU: {metrics.cpu_percent}%")
            print(f"Memory: {metrics.memory_mb} MB ({metrics.memory_percent}%)")
            print(f"Network RX: {metrics.network_rx_bytes / 1024:.1f} KB")
            print(f"Network TX: {metrics.network_tx_bytes / 1024:.1f} KB")
        else:
            print(f"No metrics for {container}")
    else:
        # System overview
        overview = bridge.get_system_overview()
        print("\n=== PROMETHEUS METRICS OVERVIEW ===\n")
        print(f"Containers monitored: {overview.total_containers}")
        print(f"Total CPU: {overview.total_cpu_percent}%")
        print(f"Total Memory: {overview.total_memory_mb:.1f} MB")

        if overview.stressed_containers:
            print(f"\nStressed containers: {', '.join(overview.stressed_containers)}")

        print("\nTop CPU consumers:")
        for c in overview.top_cpu:
            print(f"  {c['name']}: {c['cpu']}%")

        print("\nTop Memory consumers:")
        for c in overview.top_memory:
            print(f"  {c['name']}: {c['memory_mb']:.1f} MB")

        alerts = bridge.get_alert_metrics()
        if alerts:
            print(f"\nFiring alerts: {len(alerts)}")
            for a in alerts:
                print(f"  [{a['severity']}] {a['name']}: {a['message']}")
