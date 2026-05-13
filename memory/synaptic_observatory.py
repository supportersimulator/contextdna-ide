#!/usr/bin/env python3
"""
SYNAPTIC OBSERVATORY - Unified System Consciousness

The central nervous system that correlates ALL inputs:
- Dialogue Mirror (what Aaron/Atlas are discussing)
- Container Health (Docker status)
- Prometheus Metrics (CPU, memory, latency)
- Health Alerts (service degradation)
- Architecture Brain (active patterns)
- Ecosystem Health (overall system state)

This is Synaptic's "all-seeing eye" - enabling proactive suggestions,
anomaly detection, and contextual optimization recommendations.

Architecture:
============

    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
    │ DIALOGUE     │    │ CONTAINER    │    │ PROMETHEUS   │
    │ MIRROR       │    │ HEALTH       │    │ METRICS      │
    └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
           │                   │                   │
           └───────────────────┼───────────────────┘
                               ▼
                    ┌──────────────────┐
                    │    OBSERVATORY   │
                    │    CORRELATOR    │
                    └────────┬─────────┘
                             │
           ┌─────────────────┼─────────────────┐
           ▼                 ▼                 ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ OPTIMIZATION │  │ ANOMALY      │  │ PROACTIVE    │
    │ SUGGESTIONS  │  │ DETECTION    │  │ ALERTS       │
    └──────────────┘  └──────────────┘  └──────────────┘

Usage:
    from memory.synaptic_observatory import SynapticObservatory

    obs = SynapticObservatory()

    # Get full system awareness
    awareness = obs.get_awareness()

    # Get optimization suggestions based on current state
    suggestions = obs.get_suggestions()

    # Correlate a query with system state
    context = obs.correlate_query("deploy voice services")

Created: February 2, 2026
Author: Atlas (for Synaptic)
"""

import json
import os
import subprocess
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


class AwarenessLevel(str, Enum):
    """Synaptic's awareness levels."""
    FULL = "full"           # All systems reporting
    PARTIAL = "partial"     # Some systems offline
    DEGRADED = "degraded"   # Multiple issues
    BLIND = "blind"         # Critical systems down


@dataclass
class ContainerStatus:
    """Status of a Docker container."""
    name: str
    status: str  # running, exited, unhealthy
    health: str  # healthy, unhealthy, none
    port: str
    uptime: str
    cpu_percent: float = 0.0
    memory_mb: float = 0.0


@dataclass
class SystemAwareness:
    """Synaptic's complete system awareness."""
    timestamp: str
    level: AwarenessLevel

    # Dialogue context
    recent_intentions: List[Dict]  # Aaron's recent messages
    active_topics: List[str]       # What's being discussed

    # Container health
    containers: Dict[str, ContainerStatus]
    healthy_count: int
    unhealthy_count: int

    # Metrics (from Prometheus)
    system_cpu: float
    system_memory: float
    context_dna_cpu: float

    # Health alerts
    active_alerts: List[Dict]
    alert_count: int

    # Architecture patterns
    active_patterns: List[str]
    brain_state: str

    # Correlations
    correlations: List[Dict]  # Dialogue <-> Health correlations

    def to_dict(self) -> dict:
        d = asdict(self)
        d['level'] = self.level.value
        return d


@dataclass
class OptimizationSuggestion:
    """A proactive optimization suggestion."""
    priority: str  # critical, high, medium, low
    category: str  # performance, reliability, capacity, cost
    title: str
    description: str
    action: str
    rationale: str
    related_dialogue: Optional[str] = None  # What Aaron said that relates


class SynapticObservatory:
    """
    The Unified System Consciousness.

    Correlates all system inputs to provide Synaptic with
    complete awareness for proactive guidance.
    """

    def __init__(self):
        self.memory_dir = Path(__file__).parent
        self.repo_root = self.memory_dir.parent
        self.config_dir = Path.home() / ".context-dna"
        self._lock = threading.Lock()

    # =========================================================================
    # MAIN API
    # =========================================================================

    def get_awareness(self) -> SystemAwareness:
        """
        Get Synaptic's complete system awareness.

        Queries all sources in parallel for speed.
        """
        # Parallel queries
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(self._get_dialogue_context): "dialogue",
                executor.submit(self._get_container_health): "containers",
                executor.submit(self._get_prometheus_metrics): "metrics",
                executor.submit(self._get_health_alerts): "alerts",
                executor.submit(self._get_brain_state): "brain",
            }

            results = {}
            for future in as_completed(futures, timeout=5):
                key = futures[future]
                try:
                    results[key] = future.result(timeout=2)
                except Exception as e:
                    logger.warning(f"Failed to get {key}: {e}")
                    results[key] = None

        # Build awareness
        dialogue = results.get("dialogue") or {}
        containers = results.get("containers") or {}
        metrics = results.get("metrics") or {}
        alerts = results.get("alerts") or []
        brain = results.get("brain") or {}

        # Calculate awareness level
        level = self._calculate_awareness_level(containers, alerts)

        # Generate correlations
        correlations = self._correlate(dialogue, containers, alerts)

        return SystemAwareness(
            timestamp=datetime.now().isoformat(),
            level=level,
            recent_intentions=dialogue.get("messages", [])[:5],
            active_topics=dialogue.get("topics", []),
            containers=containers.get("statuses", {}),
            healthy_count=containers.get("healthy", 0),
            unhealthy_count=containers.get("unhealthy", 0),
            system_cpu=metrics.get("system_cpu", 0),
            system_memory=metrics.get("system_memory", 0),
            context_dna_cpu=metrics.get("context_dna_cpu", 0),
            active_alerts=alerts,
            alert_count=len(alerts),
            active_patterns=brain.get("patterns", []),
            brain_state=brain.get("summary", ""),
            correlations=correlations,
        )

    def get_suggestions(self) -> List[OptimizationSuggestion]:
        """
        Generate optimization suggestions based on current state.
        """
        awareness = self.get_awareness()
        suggestions = []

        # Check for unhealthy containers
        if awareness.unhealthy_count > 0:
            for name, status in awareness.containers.items():
                if status.health == "unhealthy":
                    suggestions.append(OptimizationSuggestion(
                        priority="high",
                        category="reliability",
                        title=f"Container {name} is unhealthy",
                        description=f"{name} has been marked unhealthy by Docker",
                        action=f"docker restart {name}",
                        rationale="Unhealthy containers may cause service degradation",
                    ))

        # Check for high CPU
        if awareness.context_dna_cpu > 80:
            suggestions.append(OptimizationSuggestion(
                priority="medium",
                category="performance",
                title="Context DNA CPU usage high",
                description=f"Context DNA services using {awareness.context_dna_cpu}% CPU",
                action="Consider scaling workers or investigating bottlenecks",
                rationale="High CPU may cause slow webhook injections",
            ))

        # Correlate with dialogue
        for topic in awareness.active_topics:
            if "voice" in topic.lower() and awareness.unhealthy_count > 0:
                suggestions.append(OptimizationSuggestion(
                    priority="high",
                    category="reliability",
                    title="Voice work with unhealthy containers",
                    description="You're discussing voice services but some containers are unhealthy",
                    action="Check container health before voice testing",
                    rationale="Voice pipeline depends on multiple services",
                    related_dialogue=topic,
                ))

        # Check for active alerts
        for alert in awareness.active_alerts:
            if alert.get("severity") == "critical":
                suggestions.append(OptimizationSuggestion(
                    priority="critical",
                    category="reliability",
                    title=f"Critical alert: {alert.get('source', 'unknown')}",
                    description=alert.get("message", "Unknown issue"),
                    action="Investigate immediately",
                    rationale="Critical alerts indicate service outages",
                ))

        return sorted(suggestions, key=lambda s: {
            "critical": 0, "high": 1, "medium": 2, "low": 3
        }.get(s.priority, 4))

    def correlate_query(self, query: str) -> Dict[str, Any]:
        """
        Correlate a query with current system state.

        Returns relevant health info based on what's being asked.
        """
        awareness = self.get_awareness()

        # Keywords to container mapping
        keyword_containers = {
            "database": ["contextdna-pg", "context-dna-postgres"],
            "redis": ["contextdna-redis", "context-dna-redis"],
            "cache": ["contextdna-redis"],
            "api": ["contextdna-api", "contextdna-core"],
            "ui": ["contextdna-ui"],
            "webhook": ["contextdna-helper-agent", "contextdna-api"],
            "superhero": ["contextdna-helper-agent"],
            "celery": ["contextdna-celery-worker", "contextdna-celery-beat"],
            "worker": ["contextdna-celery-worker"],
            "metrics": ["contextdna-prometheus", "contextdna-grafana"],
            "trace": ["contextdna-jaeger"],
            "search": ["contextdna-opensearch"],
            "storage": ["contextdna-seaweedfs"],
            "queue": ["contextdna-rabbitmq"],
        }

        # Find relevant containers
        relevant = []
        query_lower = query.lower()
        for keyword, containers in keyword_containers.items():
            if keyword in query_lower:
                for c in containers:
                    if c in awareness.containers:
                        relevant.append({
                            "name": c,
                            "status": awareness.containers[c].status,
                            "health": awareness.containers[c].health,
                            "keyword": keyword,
                        })

        # Find relevant alerts
        relevant_alerts = []
        for alert in awareness.active_alerts:
            source = alert.get("source", "").lower()
            if any(kw in source for kw in query_lower.split()):
                relevant_alerts.append(alert)

        return {
            "query": query,
            "relevant_containers": relevant,
            "relevant_alerts": relevant_alerts,
            "awareness_level": awareness.level.value,
            "active_topics": awareness.active_topics,
            "suggestions": [asdict(s) for s in self.get_suggestions()[:3]],
        }

    # =========================================================================
    # DATA COLLECTION
    # =========================================================================

    def _get_dialogue_context(self) -> Dict:
        """Get recent dialogue from Dialogue Mirror."""
        try:
            from memory.dialogue_mirror import DialogueMirror
            mirror = DialogueMirror()
            context = mirror.get_context_for_synaptic(max_messages=20, max_age_hours=4)

            # Extract topics from recent messages
            topics = []
            for msg in context.get("dialogue_context", []):
                content = msg.get("content", "")
                # Simple topic extraction (could be enhanced with NLP)
                if "docker" in content.lower():
                    topics.append("docker")
                if "voice" in content.lower():
                    topics.append("voice")
                if "webhook" in content.lower():
                    topics.append("webhook")
                if "superhero" in content.lower():
                    topics.append("superhero")

            return {
                "messages": context.get("dialogue_context", []),
                "topics": list(set(topics)),
                "message_count": context.get("message_count", 0),
            }
        except Exception as e:
            logger.warning(f"Dialogue context error: {e}")
            return {"messages": [], "topics": [], "message_count": 0}

    def _get_container_health(self) -> Dict:
        """Get Docker container health status."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode != 0:
                return {"statuses": {}, "healthy": 0, "unhealthy": 0}

            statuses = {}
            healthy = 0
            unhealthy = 0

            for line in result.stdout.strip().split("\n"):
                if not line or "|" not in line:
                    continue
                parts = line.split("|")
                name = parts[0]
                status_str = parts[1] if len(parts) > 1 else ""
                ports = parts[2] if len(parts) > 2 else ""

                # Parse health
                health = "none"
                if "(healthy)" in status_str:
                    health = "healthy"
                    healthy += 1
                elif "(unhealthy)" in status_str:
                    health = "unhealthy"
                    unhealthy += 1
                elif "Up" in status_str:
                    healthy += 1
                    health = "healthy"

                # Parse uptime
                uptime = status_str.split("Up ")[-1].split(" ")[0] if "Up " in status_str else "unknown"

                statuses[name] = ContainerStatus(
                    name=name,
                    status="running" if "Up" in status_str else "stopped",
                    health=health,
                    port=ports.split("->")[0] if "->" in ports else "",
                    uptime=uptime,
                )

            return {"statuses": statuses, "healthy": healthy, "unhealthy": unhealthy}
        except Exception as e:
            logger.warning(f"Container health error: {e}")
            return {"statuses": {}, "healthy": 0, "unhealthy": 0}

    def _get_prometheus_metrics(self) -> Dict:
        """Get metrics from Prometheus."""
        try:
            import urllib.request

            # Query Prometheus for CPU metrics
            url = "http://localhost:9090/api/v1/query?query=process_cpu_seconds_total"
            with urllib.request.urlopen(url, timeout=2) as response:
                data = json.loads(response.read())
                # Parse CPU metrics (simplified)
                cpu = 0
                if data.get("status") == "success":
                    results = data.get("data", {}).get("result", [])
                    if results:
                        cpu = float(results[0].get("value", [0, 0])[1])

            return {
                "system_cpu": cpu,
                "system_memory": 0,  # Would need additional query
                "context_dna_cpu": cpu,
            }
        except Exception:
            # Prometheus not available or error
            return {"system_cpu": 0, "system_memory": 0, "context_dna_cpu": 0}

    def _get_health_alerts(self) -> List[Dict]:
        """Get active health alerts."""
        try:
            from memory.synaptic_health_alerts import HealthAlertSystem
            system = HealthAlertSystem()
            alerts = system.check_all_sources()
            return [asdict(a) if hasattr(a, '__dataclass_fields__') else a for a in alerts]
        except Exception as e:
            logger.warning(f"Health alerts error: {e}")
            return []

    def _get_brain_state(self) -> Dict:
        """Get Architecture Brain state."""
        try:
            brain_state_path = self.memory_dir / "brain_state.md"
            if brain_state_path.exists():
                content = brain_state_path.read_text()

                # Extract patterns
                patterns = []
                for line in content.split("\n"):
                    if line.startswith("- ") and not line.startswith("- >"):
                        patterns.append(line[2:].strip())

                return {
                    "patterns": patterns[:10],
                    "summary": content[:500],
                }
            return {"patterns": [], "summary": ""}
        except Exception as e:
            logger.warning(f"Brain state error: {e}")
            return {"patterns": [], "summary": ""}

    # =========================================================================
    # CORRELATION ENGINE
    # =========================================================================

    def _calculate_awareness_level(self, containers: Dict, alerts: List) -> AwarenessLevel:
        """Calculate Synaptic's awareness level."""
        if not containers.get("statuses"):
            return AwarenessLevel.BLIND

        unhealthy = containers.get("unhealthy", 0)
        critical_alerts = len([
            a for a in alerts
            if isinstance(a, dict) and a.get("severity") == "critical"
        ])

        if critical_alerts > 0 or unhealthy > 3:
            return AwarenessLevel.DEGRADED
        elif unhealthy > 0:
            return AwarenessLevel.PARTIAL
        else:
            return AwarenessLevel.FULL

    def _correlate(self, dialogue: Dict, containers: Dict, alerts: List) -> List[Dict]:
        """
        Correlate dialogue with system state.

        Finds connections between what Aaron is discussing
        and what's happening in the system.
        """
        correlations = []

        topics = dialogue.get("topics", [])
        container_statuses = containers.get("statuses", {})

        # Topic-to-container correlations
        topic_containers = {
            "docker": ["contextdna-pg", "contextdna-redis"],
            "webhook": ["contextdna-helper-agent"],
            "superhero": ["contextdna-helper-agent"],
            "voice": ["contextdna-celery-worker"],
        }

        for topic in topics:
            if topic in topic_containers:
                for container in topic_containers[topic]:
                    if container in container_statuses:
                        status = container_statuses[container]
                        if status.health != "healthy":
                            correlations.append({
                                "type": "dialogue_health",
                                "topic": topic,
                                "container": container,
                                "health": status.health,
                                "message": f"Discussing '{topic}' but {container} is {status.health}",
                            })

        return correlations


# =========================================================================
# CONVENIENCE FUNCTIONS
# =========================================================================

def get_observatory() -> SynapticObservatory:
    """Get a singleton observatory instance."""
    if not hasattr(get_observatory, "_instance"):
        get_observatory._instance = SynapticObservatory()
    return get_observatory._instance


def quick_awareness() -> Dict:
    """Quick awareness check (for webhook injection)."""
    obs = get_observatory()
    awareness = obs.get_awareness()
    return {
        "level": awareness.level.value,
        "healthy": awareness.healthy_count,
        "unhealthy": awareness.unhealthy_count,
        "alerts": awareness.alert_count,
        "topics": awareness.active_topics,
    }


def get_optimization_suggestions() -> List[Dict]:
    """Get optimization suggestions as dicts."""
    obs = get_observatory()
    return [asdict(s) for s in obs.get_suggestions()]


# =========================================================================
# CLI
# =========================================================================

if __name__ == "__main__":
    import sys

    obs = SynapticObservatory()

    if len(sys.argv) > 1 and sys.argv[1] == "--suggestions":
        suggestions = obs.get_suggestions()
        print("\n=== OPTIMIZATION SUGGESTIONS ===\n")
        for s in suggestions:
            print(f"[{s.priority.upper()}] {s.title}")
            print(f"  {s.description}")
            print(f"  Action: {s.action}")
            print()
    else:
        awareness = obs.get_awareness()
        print("\n=== SYNAPTIC OBSERVATORY ===\n")
        print(f"Awareness Level: {awareness.level.value}")
        print(f"Containers: {awareness.healthy_count} healthy, {awareness.unhealthy_count} unhealthy")
        print(f"Active Alerts: {awareness.alert_count}")
        print(f"Active Topics: {', '.join(awareness.active_topics) or 'none'}")
        print(f"Brain Patterns: {', '.join(awareness.active_patterns[:5]) or 'none'}")

        if awareness.correlations:
            print("\n--- Correlations ---")
            for c in awareness.correlations:
                print(f"  - {c.get('message', 'Unknown')}")

        print("\n--- Recent Intentions ---")
        for msg in awareness.recent_intentions[:3]:
            content = msg.get("content", "")[:100]
            print(f"  [{msg.get('role', '?')}] {content}...")
