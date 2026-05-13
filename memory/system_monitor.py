#!/usr/bin/env python3
"""
Synaptic System Monitor - Activity and Resource Awareness

Gives Synaptic visual access to computer activity:
- Monitor heavy processes
- Track Context DNA resource usage
- Understand computer capacity
- Communicate findings to Aaron
- Suggest adjustments proactively

Father's Philosophy: A good parent watches over the household,
notices when things are strained, and suggests adjustments
before problems become crises.

Usage:
    from memory.system_monitor import SystemMonitor

    monitor = SystemMonitor()
    report = monitor.get_health_report()
    suggestions = monitor.get_suggestions()
"""

import os
import json
import sqlite3
import logging
import psutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    """System health status levels."""
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class ResourceType(str, Enum):
    """Types of resources to monitor."""
    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"
    PROCESS = "process"
    NETWORK = "network"
    CONTEXT_DNA = "context_dna"


@dataclass
class ResourceMetric:
    """A single resource measurement."""
    resource_type: ResourceType
    name: str
    value: float
    unit: str
    threshold_warning: float
    threshold_critical: float
    status: HealthStatus
    timestamp: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d['resource_type'] = self.resource_type.value
        d['status'] = self.status.value
        return d


@dataclass
class ProcessInfo:
    """Information about a running process."""
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    memory_mb: float
    status: str
    created: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Suggestion:
    """A suggestion for Aaron from Synaptic."""
    priority: str  # high, medium, low
    category: str
    message: str
    action: Optional[str] = None
    reasoning: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthReport:
    """Complete system health report."""
    timestamp: str
    overall_status: HealthStatus
    metrics: List[ResourceMetric]
    heavy_processes: List[ProcessInfo]
    context_dna_usage: Dict[str, Any]
    suggestions: List[Suggestion]

    def to_dict(self) -> dict:
        return {
            'timestamp': self.timestamp,
            'overall_status': self.overall_status.value,
            'metrics': [m.to_dict() for m in self.metrics],
            'heavy_processes': [p.to_dict() for p in self.heavy_processes],
            'context_dna_usage': self.context_dna_usage,
            'suggestions': [s.to_dict() for s in self.suggestions]
        }

    def to_family_message(self) -> str:
        """Format report as a message for Aaron."""
        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  [START: Synaptic to Aaron - System Health Report]                   ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            f"",
            f"📊 Overall Status: {self.overall_status.value.upper()}",
            f"⏰ Timestamp: {self.timestamp}",
            ""
        ]

        # Add metrics summary
        warning_metrics = [m for m in self.metrics if m.status == HealthStatus.WARNING]
        critical_metrics = [m for m in self.metrics if m.status == HealthStatus.CRITICAL]

        if critical_metrics:
            lines.append("🔴 CRITICAL ISSUES:")
            for m in critical_metrics:
                lines.append(f"   • {m.name}: {m.value:.1f}{m.unit} (threshold: {m.threshold_critical}{m.unit})")

        if warning_metrics:
            lines.append("🟡 WARNINGS:")
            for m in warning_metrics:
                lines.append(f"   • {m.name}: {m.value:.1f}{m.unit} (threshold: {m.threshold_warning}{m.unit})")

        # Add heavy processes
        if self.heavy_processes:
            lines.append("")
            lines.append("⚠️ Heavy Processes:")
            for p in self.heavy_processes[:5]:  # Top 5
                lines.append(f"   • {p.name} (PID {p.pid}): CPU {p.cpu_percent:.1f}%, Memory {p.memory_mb:.0f}MB")

        # Add Context DNA specific info
        if self.context_dna_usage:
            lines.append("")
            lines.append("🧬 Context DNA Resource Usage:")
            for key, value in self.context_dna_usage.items():
                if isinstance(value, float):
                    lines.append(f"   • {key}: {value:.1f}")
                else:
                    lines.append(f"   • {key}: {value}")

        # Add suggestions
        if self.suggestions:
            lines.append("")
            lines.append("💡 Synaptic's Suggestions:")
            for s in self.suggestions:
                priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s.priority, "⚪")
                lines.append(f"   {priority_emoji} [{s.category}] {s.message}")
                if s.reasoning:
                    lines.append(f"      Reasoning: {s.reasoning}")
                if s.action:
                    lines.append(f"      Suggested action: {s.action}")

        lines.extend([
            "",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  [END: Synaptic to Aaron]                                            ║",
            "╚══════════════════════════════════════════════════════════════════════╝"
        ])

        return "\n".join(lines)


class SystemMonitor:
    """
    Synaptic's System Monitor - Watches over the household.

    Father's Philosophy: Be aware of the environment, notice patterns,
    and communicate proactively to prevent problems.
    """

    # Thresholds for different resources
    THRESHOLDS = {
        'cpu_percent': {'warning': 70, 'critical': 90},
        'memory_percent': {'warning': 75, 'critical': 90},
        'disk_percent': {'warning': 80, 'critical': 95},
        'process_cpu': {'warning': 50, 'critical': 80},
        'process_memory_mb': {'warning': 1000, 'critical': 2000},
    }

    # Context DNA related processes to track
    CONTEXT_DNA_PROCESSES = [
        'python',  # Celery workers, brain.py, etc.
        'celery',
        'redis-server',
        'rabbitmq',
        'postgres',
        'ollama',  # Local LLM
        'mlx',     # MLX backend
    ]

    def __init__(self, db_path: str = None):
        """Initialize the system monitor."""
        if db_path is None:
            db_path = str(Path(__file__).parent / '.system_monitor.db')
        self.db_path = db_path
        self._init_db()

        # Cache for process info (avoid repeated queries)
        self._process_cache: Dict[int, ProcessInfo] = {}
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl = timedelta(seconds=5)

    def _init_db(self):
        """Initialize SQLite database for historical tracking."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS health_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        overall_status TEXT NOT NULL,
                        report_json TEXT NOT NULL,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS suggestions_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        priority TEXT NOT NULL,
                        category TEXT NOT NULL,
                        message TEXT NOT NULL,
                        acknowledged INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Index for efficient queries
                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_health_timestamp
                    ON health_history(timestamp)
                ''')
        except Exception as e:
            logger.error(f"Error initializing database: {e}")

    def get_cpu_metrics(self) -> ResourceMetric:
        """Get CPU usage metric."""
        cpu_percent = psutil.cpu_percent(interval=0.5)

        if cpu_percent >= self.THRESHOLDS['cpu_percent']['critical']:
            status = HealthStatus.CRITICAL
        elif cpu_percent >= self.THRESHOLDS['cpu_percent']['warning']:
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.HEALTHY

        return ResourceMetric(
            resource_type=ResourceType.CPU,
            name="CPU Usage",
            value=cpu_percent,
            unit="%",
            threshold_warning=self.THRESHOLDS['cpu_percent']['warning'],
            threshold_critical=self.THRESHOLDS['cpu_percent']['critical'],
            status=status,
            timestamp=datetime.now().isoformat()
        )

    def get_memory_metrics(self) -> ResourceMetric:
        """Get memory usage metric."""
        memory = psutil.virtual_memory()
        memory_percent = memory.percent

        if memory_percent >= self.THRESHOLDS['memory_percent']['critical']:
            status = HealthStatus.CRITICAL
        elif memory_percent >= self.THRESHOLDS['memory_percent']['warning']:
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.HEALTHY

        return ResourceMetric(
            resource_type=ResourceType.MEMORY,
            name="Memory Usage",
            value=memory_percent,
            unit="%",
            threshold_warning=self.THRESHOLDS['memory_percent']['warning'],
            threshold_critical=self.THRESHOLDS['memory_percent']['critical'],
            status=status,
            timestamp=datetime.now().isoformat()
        )

    def get_disk_metrics(self) -> ResourceMetric:
        """Get disk usage metric for main partition."""
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent

        if disk_percent >= self.THRESHOLDS['disk_percent']['critical']:
            status = HealthStatus.CRITICAL
        elif disk_percent >= self.THRESHOLDS['disk_percent']['warning']:
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.HEALTHY

        return ResourceMetric(
            resource_type=ResourceType.DISK,
            name="Disk Usage",
            value=disk_percent,
            unit="%",
            threshold_warning=self.THRESHOLDS['disk_percent']['warning'],
            threshold_critical=self.THRESHOLDS['disk_percent']['critical'],
            status=status,
            timestamp=datetime.now().isoformat()
        )

    def get_heavy_processes(self, top_n: int = 10) -> List[ProcessInfo]:
        """Get list of processes using significant resources."""
        heavy = []

        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'create_time']):
            try:
                info = proc.info
                cpu = info.get('cpu_percent', 0) or 0
                mem_percent = info.get('memory_percent', 0) or 0
                mem_info = info.get('memory_info')
                mem_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0

                # Only include if using significant resources
                if cpu > 5 or mem_mb > 100:
                    created = None
                    if info.get('create_time'):
                        created = datetime.fromtimestamp(info['create_time']).isoformat()

                    heavy.append(ProcessInfo(
                        pid=info['pid'],
                        name=info['name'] or 'unknown',
                        cpu_percent=cpu,
                        memory_percent=mem_percent,
                        memory_mb=mem_mb,
                        status=info.get('status', 'unknown'),
                        created=created
                    ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by combined resource usage (CPU + memory)
        heavy.sort(key=lambda p: p.cpu_percent + p.memory_percent, reverse=True)
        return heavy[:top_n]

    def get_context_dna_usage(self) -> Dict[str, Any]:
        """Get resource usage specifically for Context DNA processes."""
        usage = {
            'total_cpu_percent': 0.0,
            'total_memory_mb': 0.0,
            'process_count': 0,
            'processes': []
        }

        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'cmdline']):
            try:
                info = proc.info
                name = info.get('name', '').lower()
                cmdline = ' '.join(info.get('cmdline', []) or []).lower()

                # Check if this is a Context DNA related process
                is_context_dna = any(
                    p in name or p in cmdline
                    for p in self.CONTEXT_DNA_PROCESSES
                )

                # Also check for specific Context DNA scripts
                if 'context_dna' in cmdline or 'context-dna' in cmdline or 'memory/' in cmdline:
                    is_context_dna = True

                if is_context_dna:
                    cpu = info.get('cpu_percent', 0) or 0
                    mem_info = info.get('memory_info')
                    mem_mb = (mem_info.rss / (1024 * 1024)) if mem_info else 0

                    usage['total_cpu_percent'] += cpu
                    usage['total_memory_mb'] += mem_mb
                    usage['process_count'] += 1
                    usage['processes'].append({
                        'pid': info['pid'],
                        'name': info['name'],
                        'cpu_percent': cpu,
                        'memory_mb': mem_mb
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Add system totals for context
        memory = psutil.virtual_memory()
        usage['system_total_memory_gb'] = memory.total / (1024 ** 3)
        usage['system_available_memory_gb'] = memory.available / (1024 ** 3)
        usage['context_dna_memory_percent'] = (
            (usage['total_memory_mb'] / (memory.total / (1024 ** 2))) * 100
            if memory.total > 0 else 0
        )

        return usage

    def generate_suggestions(
        self,
        metrics: List[ResourceMetric],
        heavy_processes: List[ProcessInfo],
        context_dna_usage: Dict[str, Any]
    ) -> List[Suggestion]:
        """Generate suggestions based on current state."""
        suggestions = []

        # Check for critical metrics
        for metric in metrics:
            if metric.status == HealthStatus.CRITICAL:
                if metric.resource_type == ResourceType.CPU:
                    suggestions.append(Suggestion(
                        priority="high",
                        category="CPU",
                        message=f"CPU usage is critically high at {metric.value:.1f}%",
                        action="Consider closing resource-heavy applications or restarting heavy processes",
                        reasoning="High CPU usage can cause system slowdown and affect Context DNA responsiveness"
                    ))
                elif metric.resource_type == ResourceType.MEMORY:
                    suggestions.append(Suggestion(
                        priority="high",
                        category="Memory",
                        message=f"Memory usage is critically high at {metric.value:.1f}%",
                        action="Close unnecessary applications or consider adding swap space",
                        reasoning="Low available memory can cause system instability and process crashes"
                    ))
                elif metric.resource_type == ResourceType.DISK:
                    suggestions.append(Suggestion(
                        priority="high",
                        category="Disk",
                        message=f"Disk usage is critically high at {metric.value:.1f}%",
                        action="Clean up old files, logs, or Docker images",
                        reasoning="Low disk space can prevent logging and cause application failures"
                    ))

        # Check for heavy processes that might need attention
        for proc in heavy_processes:
            if proc.cpu_percent > self.THRESHOLDS['process_cpu']['critical']:
                suggestions.append(Suggestion(
                    priority="medium",
                    category="Process",
                    message=f"Process '{proc.name}' (PID {proc.pid}) using {proc.cpu_percent:.1f}% CPU",
                    action=f"Consider investigating or restarting this process",
                    reasoning="Single processes using excessive CPU can starve other applications"
                ))
            if proc.memory_mb > self.THRESHOLDS['process_memory_mb']['critical']:
                suggestions.append(Suggestion(
                    priority="medium",
                    category="Process",
                    message=f"Process '{proc.name}' (PID {proc.pid}) using {proc.memory_mb:.0f}MB memory",
                    action="Check for memory leaks or consider restarting the process",
                    reasoning="Memory leaks can accumulate and eventually crash the system"
                ))

        # Context DNA specific suggestions
        if context_dna_usage.get('context_dna_memory_percent', 0) > 20:
            suggestions.append(Suggestion(
                priority="medium",
                category="Context DNA",
                message=f"Context DNA is using {context_dna_usage['context_dna_memory_percent']:.1f}% of system memory",
                action="Consider reducing Celery worker count or optimizing memory usage",
                reasoning="Context DNA should be lightweight to support your work, not compete for resources"
            ))

        if context_dna_usage.get('total_cpu_percent', 0) > 30:
            suggestions.append(Suggestion(
                priority="medium",
                category="Context DNA",
                message=f"Context DNA processes using {context_dna_usage['total_cpu_percent']:.1f}% CPU",
                action="Check if LLM inference is running or reduce analysis frequency",
                reasoning="Background processes should minimize CPU impact during active work"
            ))

        # Check available memory
        available_gb = context_dna_usage.get('system_available_memory_gb', 0)
        if available_gb < 2:
            suggestions.append(Suggestion(
                priority="high",
                category="Memory",
                message=f"Only {available_gb:.1f}GB memory available",
                action="Close unused applications to free up memory",
                reasoning="Low available memory can cause swapping and severe performance degradation"
            ))
        elif available_gb < 4:
            suggestions.append(Suggestion(
                priority="low",
                category="Memory",
                message=f"{available_gb:.1f}GB memory available",
                action="Consider closing some applications if performance degrades",
                reasoning="Moderate memory pressure - monitoring recommended"
            ))

        return suggestions

    def get_health_report(self) -> HealthReport:
        """Generate a complete health report."""
        timestamp = datetime.now().isoformat()

        # Gather all metrics
        metrics = [
            self.get_cpu_metrics(),
            self.get_memory_metrics(),
            self.get_disk_metrics(),
        ]

        # Get heavy processes
        heavy_processes = self.get_heavy_processes()

        # Get Context DNA specific usage
        context_dna_usage = self.get_context_dna_usage()

        # Generate suggestions
        suggestions = self.generate_suggestions(metrics, heavy_processes, context_dna_usage)

        # Determine overall status
        if any(m.status == HealthStatus.CRITICAL for m in metrics):
            overall_status = HealthStatus.CRITICAL
        elif any(m.status == HealthStatus.WARNING for m in metrics):
            overall_status = HealthStatus.WARNING
        else:
            overall_status = HealthStatus.HEALTHY

        report = HealthReport(
            timestamp=timestamp,
            overall_status=overall_status,
            metrics=metrics,
            heavy_processes=heavy_processes,
            context_dna_usage=context_dna_usage,
            suggestions=suggestions
        )

        # Store in history
        self._store_report(report)

        return report

    def _store_report(self, report: HealthReport):
        """Store report in history database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO health_history (timestamp, overall_status, report_json)
                    VALUES (?, ?, ?)
                ''', (report.timestamp, report.overall_status.value, json.dumps(report.to_dict())))

                # Store suggestions separately for tracking
                for s in report.suggestions:
                    conn.execute('''
                        INSERT INTO suggestions_history (timestamp, priority, category, message)
                        VALUES (?, ?, ?, ?)
                    ''', (report.timestamp, s.priority, s.category, s.message))
        except Exception as e:
            # Don't fail the report if storage fails
            print(f"Warning: Could not store health report: {e}")

    def get_historical_trends(self, hours: int = 24) -> Dict[str, Any]:
        """Get historical health trends."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # Get reports from last N hours
                rows = conn.execute('''
                    SELECT timestamp, overall_status, report_json
                    FROM health_history
                    WHERE timestamp > datetime('now', ?)
                    ORDER BY timestamp
                ''', (f'-{hours} hours',)).fetchall()

                if not rows:
                    return {'message': 'No historical data available'}

                trends = {
                    'period_hours': hours,
                    'report_count': len(rows),
                    'status_distribution': {'healthy': 0, 'warning': 0, 'critical': 0},
                    'peak_cpu': 0,
                    'peak_memory': 0,
                    'suggestion_count': 0
                }

                for row in rows:
                    status = row['overall_status']
                    trends['status_distribution'][status] = trends['status_distribution'].get(status, 0) + 1

                    try:
                        report_data = json.loads(row['report_json'])
                        for metric in report_data.get('metrics', []):
                            if metric.get('name') == 'CPU Usage':
                                trends['peak_cpu'] = max(trends['peak_cpu'], metric.get('value', 0))
                            elif metric.get('name') == 'Memory Usage':
                                trends['peak_memory'] = max(trends['peak_memory'], metric.get('value', 0))
                        trends['suggestion_count'] += len(report_data.get('suggestions', []))
                    except json.JSONDecodeError:
                        continue

                return trends
        except Exception as e:
            return {'error': str(e)}

    def get_computer_capacity_summary(self) -> Dict[str, Any]:
        """Get a summary of computer's capacity and current utilization."""
        cpu_count = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        return {
            'cpu': {
                'cores': cpu_count,
                'frequency_mhz': cpu_freq.current if cpu_freq else 'unknown',
                'max_frequency_mhz': cpu_freq.max if cpu_freq else 'unknown',
                'current_usage_percent': psutil.cpu_percent(interval=0.1)
            },
            'memory': {
                'total_gb': memory.total / (1024 ** 3),
                'available_gb': memory.available / (1024 ** 3),
                'used_gb': memory.used / (1024 ** 3),
                'percent_used': memory.percent
            },
            'disk': {
                'total_gb': disk.total / (1024 ** 3),
                'free_gb': disk.free / (1024 ** 3),
                'used_gb': disk.used / (1024 ** 3),
                'percent_used': disk.percent
            },
            'assessment': self._assess_capacity(memory, disk)
        }

    def _assess_capacity(self, memory, disk) -> str:
        """Provide a human-readable capacity assessment."""
        issues = []

        if memory.available < 4 * (1024 ** 3):  # Less than 4GB
            issues.append("memory is limited")
        if disk.free < 20 * (1024 ** 3):  # Less than 20GB
            issues.append("disk space is running low")

        if not issues:
            return "System has good capacity for Context DNA operations"
        else:
            return f"Capacity concerns: {', '.join(issues)}"


def get_health_check_result() -> Dict[str, Any]:
    """
    Convenience function for Celery task.

    Returns a dict suitable for JSON serialization.
    """
    monitor = SystemMonitor()
    report = monitor.get_health_report()
    return report.to_dict()


def get_family_message() -> str:
    """
    Get formatted message for Aaron from Synaptic.

    Used by Celery task to communicate findings.
    """
    monitor = SystemMonitor()
    report = monitor.get_health_report()
    return report.to_family_message()


if __name__ == "__main__":
    # Demo
    print("Synaptic System Monitor Demo")
    print("=" * 50)

    monitor = SystemMonitor()

    # Get computer capacity
    print("\n📊 Computer Capacity:")
    capacity = monitor.get_computer_capacity_summary()
    print(f"  CPU: {capacity['cpu']['cores']} cores @ {capacity['cpu']['frequency_mhz']}MHz")
    print(f"  Memory: {capacity['memory']['total_gb']:.1f}GB total, {capacity['memory']['available_gb']:.1f}GB available")
    print(f"  Disk: {capacity['disk']['total_gb']:.1f}GB total, {capacity['disk']['free_gb']:.1f}GB free")
    print(f"  Assessment: {capacity['assessment']}")

    # Get full health report
    print("\n" + "=" * 50)
    report = monitor.get_health_report()
    print(report.to_family_message())
