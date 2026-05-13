#!/usr/bin/env python3
"""
System Health Monitor for Context DNA

Monitors macOS system resources and provides recommendations
for optimal LLM and Docker performance.

Usage:
    python memory/system_health_monitor.py          # Quick check
    python memory/system_health_monitor.py --watch  # Continuous monitoring
    python memory/system_health_monitor.py --json   # JSON output for integration
"""

import subprocess
import json
import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from datetime import datetime
import logging


logger = logging.getLogger(__name__)


@dataclass
class ProcessInfo:
    name: str
    pid: int
    cpu_percent: float
    memory_mb: float
    status: str = "running"


@dataclass
class SystemHealth:
    timestamp: str
    total_memory_gb: float
    used_memory_gb: float
    free_memory_gb: float
    memory_pressure: str  # "normal", "warning", "critical"
    cpu_idle_percent: float
    top_memory_processes: List[ProcessInfo]
    top_cpu_processes: List[ProcessInfo]
    recommendations: List[str]
    llm_ready: bool
    docker_ready: bool


def get_memory_stats() -> Dict:
    """Get memory statistics using vm_stat."""
    try:
        result = subprocess.run(['vm_stat'], capture_output=True, text=True)
        lines = result.stdout.strip().split('\n')

        stats = {}
        for line in lines[1:]:  # Skip header
            if ':' in line:
                key, value = line.split(':')
                # Extract number, remove dots and whitespace
                num = re.search(r'(\d+)', value)
                if num:
                    stats[key.strip()] = int(num.group(1))

        # Page size is typically 4096 bytes on macOS
        page_size = 4096

        free_pages = stats.get('Pages free', 0)
        active_pages = stats.get('Pages active', 0)
        inactive_pages = stats.get('Pages inactive', 0)
        wired_pages = stats.get('Pages wired down', 0)
        compressed_pages = stats.get('Pages occupied by compressor', 0)

        free_gb = (free_pages * page_size) / (1024**3)
        used_gb = ((active_pages + wired_pages + compressed_pages) * page_size) / (1024**3)

        return {
            'free_gb': free_gb,
            'used_gb': used_gb,
            'active_gb': (active_pages * page_size) / (1024**3),
            'inactive_gb': (inactive_pages * page_size) / (1024**3),
            'wired_gb': (wired_pages * page_size) / (1024**3),
        }
    except Exception as e:
        return {'error': str(e)}


def get_total_memory() -> float:
    """Get total system memory in GB."""
    try:
        result = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True)
        return int(result.stdout.strip()) / (1024**3)
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        logger.debug(f"Could not get total memory: {e}")
        return 0


def get_top_processes(sort_by: str = 'mem', limit: int = 10) -> List[ProcessInfo]:
    """Get top processes by memory or CPU."""
    try:
        result = subprocess.run(
            ['ps', '-eo', 'pid,pcpu,rss,comm'],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split('\n')[1:]  # Skip header

        processes = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                pid = int(parts[0])
                cpu = float(parts[1])
                mem_kb = int(parts[2])
                name = ' '.join(parts[3:])

                # Skip kernel processes
                if name.startswith('(') or pid == 0:
                    continue

                processes.append(ProcessInfo(
                    name=name.split('/')[-1][:40],  # Short name
                    pid=pid,
                    cpu_percent=cpu,
                    memory_mb=mem_kb / 1024
                ))

        # Sort by memory or CPU
        if sort_by == 'mem':
            processes.sort(key=lambda p: p.memory_mb, reverse=True)
        else:
            processes.sort(key=lambda p: p.cpu_percent, reverse=True)

        return processes[:limit]
    except Exception as e:
        return []


def get_cpu_idle() -> float:
    """Get CPU idle percentage."""
    try:
        result = subprocess.run(
            ['top', '-l', '1', '-n', '0'],
            capture_output=True, text=True
        )
        for line in result.stdout.split('\n'):
            if 'CPU usage' in line:
                # Parse: CPU usage: 5.40% user, 2.61% sys, 91.99% idle
                match = re.search(r'(\d+\.?\d*)% idle', line)
                if match:
                    return float(match.group(1))
        return 0
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        logger.debug(f"Could not get CPU idle: {e}")
        return 0


def check_docker_status() -> bool:
    """Check if Docker is running and responsive."""
    try:
        result = subprocess.run(
            ['docker', 'ps'],
            capture_output=True, text=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"Docker status check failed: {e}")
        return False


def check_llm_status() -> bool:
    """Check if local LLM is running — via Redis health cache, NO direct HTTP to 5044."""
    try:
        from memory.llm_priority_queue import check_llm_health
        return check_llm_health()
    except Exception as e:
        logger.debug(f"LLM health check failed: {e}")
        return False


def generate_recommendations(
    free_gb: float,
    total_gb: float,
    top_mem: List[ProcessInfo],
    docker_ok: bool,
    llm_ok: bool
) -> List[str]:
    """Generate optimization recommendations."""
    recommendations = []

    # Memory recommendations
    if free_gb < 2:
        recommendations.append("⚠️ CRITICAL: Less than 2GB free - close apps before running LLM")
    elif free_gb < 4:
        recommendations.append("⚠️ WARNING: Low memory - consider closing heavy apps")

    # Check for memory hogs
    for proc in top_mem[:5]:
        if proc.memory_mb > 2000:  # > 2GB
            if 'Code' in proc.name or 'Electron' in proc.name:
                recommendations.append(f"💡 VS Code using {proc.memory_mb/1024:.1f}GB - disable Pylance for more RAM")
            elif 'Chrome' in proc.name or 'Safari' in proc.name:
                recommendations.append(f"💡 Browser using {proc.memory_mb/1024:.1f}GB - close tabs")
            elif 'Spotify' in proc.name:
                recommendations.append(f"💡 Spotify using {proc.memory_mb/1024:.1f}GB - close for demo")

    # LLM readiness
    needed_for_llm = 16  # Qwen3-30B needs ~16GB
    available = free_gb + 2  # Can reclaim some inactive

    if available < needed_for_llm:
        deficit = needed_for_llm - available
        recommendations.append(f"❌ Need {deficit:.1f}GB more RAM for 30B LLM")
        recommendations.append("💡 Consider: smaller model (8B uses ~5GB) OR close apps")
    else:
        recommendations.append(f"✅ Enough RAM for LLM ({available:.1f}GB available)")

    # Docker status
    if not docker_ok:
        recommendations.append("⚠️ Docker not responding - restart Docker Desktop")

    return recommendations


def get_system_health() -> SystemHealth:
    """Get comprehensive system health report."""
    mem_stats = get_memory_stats()
    total_gb = get_total_memory()
    free_gb = mem_stats.get('free_gb', 0) + mem_stats.get('inactive_gb', 0)
    used_gb = mem_stats.get('used_gb', 0)

    # Determine memory pressure
    free_percent = (free_gb / total_gb) * 100 if total_gb > 0 else 0
    if free_percent < 10:
        pressure = "critical"
    elif free_percent < 25:
        pressure = "warning"
    else:
        pressure = "normal"

    top_mem = get_top_processes('mem', 10)
    top_cpu = get_top_processes('cpu', 5)
    cpu_idle = get_cpu_idle()
    docker_ok = check_docker_status()
    llm_ok = check_llm_status()

    recommendations = generate_recommendations(
        free_gb, total_gb, top_mem, docker_ok, llm_ok
    )

    return SystemHealth(
        timestamp=datetime.now().isoformat(),
        total_memory_gb=round(total_gb, 1),
        used_memory_gb=round(used_gb, 1),
        free_memory_gb=round(free_gb, 1),
        memory_pressure=pressure,
        cpu_idle_percent=round(cpu_idle, 1),
        top_memory_processes=top_mem[:5],
        top_cpu_processes=top_cpu,
        recommendations=recommendations,
        llm_ready=llm_ok,
        docker_ready=docker_ok
    )


def print_health_report(health: SystemHealth):
    """Print formatted health report."""
    print("=" * 60)
    print("  CONTEXT DNA SYSTEM HEALTH MONITOR")
    print("=" * 60)
    print(f"\n📊 Memory: {health.used_memory_gb:.1f}GB used / {health.total_memory_gb:.1f}GB total")
    print(f"   Free: {health.free_memory_gb:.1f}GB ({health.memory_pressure.upper()})")
    print(f"   CPU Idle: {health.cpu_idle_percent}%")

    print(f"\n🐳 Docker: {'✅ Running' if health.docker_ready else '❌ Not responding'}")
    print(f"🧠 LLM:    {'✅ Running' if health.llm_ready else '⚪ Not started'}")

    print("\n📈 Top Memory Consumers:")
    for i, proc in enumerate(health.top_memory_processes, 1):
        print(f"   {i}. {proc.name[:30]:30s} {proc.memory_mb/1024:5.1f} GB")

    print("\n💡 Recommendations:")
    for rec in health.recommendations:
        print(f"   {rec}")

    print("\n" + "=" * 60)


def main():
    import sys

    health = get_system_health()

    if '--json' in sys.argv:
        # Convert to JSON-serializable dict
        health_dict = asdict(health)
        health_dict['top_memory_processes'] = [asdict(p) for p in health.top_memory_processes]
        health_dict['top_cpu_processes'] = [asdict(p) for p in health.top_cpu_processes]
        print(json.dumps(health_dict, indent=2))
    elif '--watch' in sys.argv:
        import time
        while True:
            subprocess.run(['clear'])
            health = get_system_health()
            print_health_report(health)
            print("\nRefreshing in 5 seconds... (Ctrl+C to stop)")
            time.sleep(5)
    else:
        print_health_report(health)


if __name__ == '__main__':
    main()
