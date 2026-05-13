"""
Section Health Monitoring for Context DNA Webhook Hardening

Provides per-section health monitoring with graceful degradation.
Each of the 8 webhook sections has its own health indicator and fallback.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import importlib
import subprocess

# Add parent directory and grandparent to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class SectionStatus:
    """Status of a single webhook section."""
    healthy: bool
    section_id: int
    section_name: str = ""
    failed_deps: List[str] = field(default_factory=list)
    fallback_available: bool = True
    last_check: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "healthy": self.healthy,
            "section_id": self.section_id,
            "section_name": self.section_name,
            "failed_deps": self.failed_deps,
            "fallback_available": self.fallback_available,
            "last_check": self.last_check.isoformat(),
            "error_message": self.error_message
        }


@dataclass
class OverallHealth:
    """Overall health status of the webhook system."""
    all_healthy: bool
    critical_sections_healthy: bool
    section_statuses: Dict[int, SectionStatus]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            "all_healthy": self.all_healthy,
            "critical_sections_healthy": self.critical_sections_healthy,
            "sections": {sid: status.to_dict() for sid, status in self.section_statuses.items()},
            "timestamp": self.timestamp.isoformat()
        }

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║  WEBHOOK SECTION HEALTH STATUS                               ║",
            "╠══════════════════════════════════════════════════════════════╣"
        ]

        for sid, status in sorted(self.section_statuses.items()):
            icon = "✅" if status.healthy else ("⚠️" if status.fallback_available else "❌")
            name = status.section_name.ljust(15)
            health = "HEALTHY" if status.healthy else f"DEGRADED ({', '.join(status.failed_deps)})"
            lines.append(f"   {icon} Section {sid} ({name}): {health}")

        lines.append("╠══════════════════════════════════════════════════════════════╣")

        if self.all_healthy:
            lines.append("   ✅ ALL SECTIONS HEALTHY")
        elif self.critical_sections_healthy:
            lines.append("   ⚠️  SOME SECTIONS DEGRADED (critical sections OK)")
        else:
            lines.append("   ❌ CRITICAL SECTIONS FAILING")

        lines.append("╚══════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


class SectionHealth:
    """
    Monitor and report health of each webhook section.

    Each section has:
    - Dependencies that must be available
    - A critical flag (section 0 and 6 are critical)
    - A fallback mechanism
    """

    SECTIONS = {
        0: {
            "name": "SAFETY",
            "critical": True,
            "deps": [],  # No external deps - must always work
            "description": "Risk classification",
            "fallback": "hardcoded_safety_fallback"
        },
        1: {
            "name": "FOUNDATION",
            "critical": False,
            "deps": ["agent_service"],
            "description": "Core project patterns",
            "fallback": "minimal_foundation_fallback"
        },
        2: {
            "name": "WISDOM",
            "critical": False,
            "deps": ["context_dna_client"],
            "description": "Learned insights (SOPs)",
            "fallback": "empty_section_fallback"
        },
        3: {
            "name": "AWARENESS",
            "critical": False,
            "deps": ["hierarchy_profile"],
            "description": "Active project context",
            "fallback": "empty_section_fallback"
        },
        4: {
            "name": "DEEP_CONTEXT",
            "critical": False,
            "deps": ["context_dna_client"],
            "description": "Relevant learnings",
            "fallback": "empty_section_fallback"
        },
        5: {
            "name": "PROTOCOL",
            "critical": False,
            "deps": [],  # Hardcoded content
            "description": "Communication guidelines",
            "fallback": "hardcoded_protocol_fallback"
        },
        6: {
            "name": "HOLISTIC_CONTEXT",
            "critical": True,  # Holistic context for the task is critical
            "deps": ["synaptic_health_monitor", "system_monitor"],
            "description": "Synaptic's holistic context for current task",
            "fallback": "minimal_holistic_context_fallback"
        },
        7: {
            "name": "FULL_LIBRARY",
            "critical": False,
            "deps": ["skill_repository"],
            "description": "Complete skill repository",
            "fallback": "empty_section_fallback"
        },
        8: {
            "name": "8TH_INTELLIGENCE",
            "critical": True,  # Synaptic's direct voice to Aaron - ALWAYS present
            "deps": ["synaptic_health_monitor"],  # Shares deps with Section 6
            "description": "Synaptic's ever-present subconscious voice to Aaron",
            "fallback": "minimal_8th_intelligence_fallback"
        }
    }

    def __init__(self):
        self._dependency_checkers = {
            "agent_service": self._check_agent_service,
            "context_dna_client": self._check_context_dna_client,
            "hierarchy_profile": self._check_hierarchy_profile,
            "synaptic_health_monitor": self._check_synaptic_health_monitor,
            "system_monitor": self._check_system_monitor,
            "skill_repository": self._check_skill_repository,
        }

    def check_section(self, section_id: int) -> SectionStatus:
        """Check if a specific section can generate content."""
        if section_id not in self.SECTIONS:
            return SectionStatus(
                healthy=False,
                section_id=section_id,
                error_message=f"Unknown section: {section_id}"
            )

        section_config = self.SECTIONS[section_id]
        deps = section_config["deps"]
        failed_deps = []

        for dep in deps:
            checker = self._dependency_checkers.get(dep)
            if checker:
                try:
                    if not checker():
                        failed_deps.append(dep)
                except Exception as e:
                    failed_deps.append(f"{dep} (error: {str(e)[:50]})")
            else:
                # Unknown dependency - assume failed
                failed_deps.append(f"{dep} (no checker)")

        return SectionStatus(
            healthy=len(failed_deps) == 0,
            section_id=section_id,
            section_name=section_config["name"],
            failed_deps=failed_deps,
            fallback_available=section_config["fallback"] is not None
        )

    def check_all_sections(self) -> OverallHealth:
        """Check health of all sections."""
        statuses = {}
        all_healthy = True
        critical_healthy = True

        for section_id in self.SECTIONS:
            status = self.check_section(section_id)
            statuses[section_id] = status

            if not status.healthy:
                all_healthy = False
                if self.SECTIONS[section_id]["critical"]:
                    critical_healthy = False

        return OverallHealth(
            all_healthy=all_healthy,
            critical_sections_healthy=critical_healthy,
            section_statuses=statuses
        )

    def get_dashboard_status(self) -> Dict:
        """Return status of all sections for dashboard."""
        health = self.check_all_sections()
        return health.to_dict()

    def get_section_dependencies(self, section_id: int) -> List[str]:
        """Get list of dependencies for a section."""
        if section_id in self.SECTIONS:
            return self.SECTIONS[section_id]["deps"]
        return []

    def is_critical_section(self, section_id: int) -> bool:
        """Check if a section is marked as critical."""
        if section_id in self.SECTIONS:
            return self.SECTIONS[section_id]["critical"]
        return False

    # Dependency checkers

    def _check_agent_service(self) -> bool:
        """Check if agent_service is available."""
        try:
            # agent_service.py exports HelperAgent class and FASTAPI_AVAILABLE flag
            from memory.agent_service import HelperAgent, FASTAPI_AVAILABLE
            # Service is available if FastAPI loaded and class exists
            return FASTAPI_AVAILABLE
        except ImportError:
            return False
        except Exception:
            # Service exists but may have issues
            return True  # Partial availability

    def _check_context_dna_client(self) -> bool:
        """Check if Context DNA API is reachable."""
        try:
            from memory.context_dna_client import ContextDNAClient
            client = ContextDNAClient()
            # Try a lightweight operation
            return client.health_check() if hasattr(client, 'health_check') else True
        except ImportError:
            return False
        except Exception:
            return False

    def _check_hierarchy_profile(self) -> bool:
        """Check if hierarchy profile is available."""
        profile_paths = [
            Path.home() / ".context-dna" / "hierarchy_profile.json",
            Path(__file__).parent.parent / ".context-dna" / "hierarchy_profile.json",
        ]
        return any(p.exists() for p in profile_paths)

    def _check_synaptic_health_monitor(self) -> bool:
        """Check if Synaptic health monitor is available."""
        try:
            from memory.synaptic_health_monitor import SynapticHealthMonitor
            return True
        except ImportError:
            return False

    def _check_system_monitor(self) -> bool:
        """Check if system monitor is available."""
        try:
            from memory.system_monitor import SystemMonitor
            return True
        except ImportError:
            return False

    def _check_skill_repository(self) -> bool:
        """Check if skill repository is accessible."""
        try:
            # Check for skill files
            skills_path = Path(__file__).parent / "major_skills"
            return skills_path.exists() and any(skills_path.glob("*.py"))
        except Exception:
            return False


# Fallback content generators

def hardcoded_safety_fallback(prompt: str) -> str:
    """Emergency fallback for Section 0 - always works."""
    # Simple keyword-based risk detection
    critical_keywords = ["delete", "drop", "rm -rf", "force push", "production", "deploy"]
    high_keywords = ["modify", "update", "change", "migration", "refactor"]

    prompt_lower = prompt.lower()

    if any(kw in prompt_lower for kw in critical_keywords):
        risk = "CRITICAL"
    elif any(kw in prompt_lower for kw in high_keywords):
        risk = "HIGH"
    else:
        risk = "MODERATE"

    return f"""
╔══════════════════════════════════════════════════════════════════════╗
║  ⚠️  RISK CLASSIFICATION (Fallback Mode)                             ║
╠══════════════════════════════════════════════════════════════════════╣
   Risk Level: {risk}
   Note: Using simplified risk detection (full classifier unavailable)
╚══════════════════════════════════════════════════════════════════════╝
"""


def minimal_foundation_fallback() -> str:
    """Minimal fallback for Section 1."""
    return """
╔══════════════════════════════════════════════════════════════════════╗
║  📋 FOUNDATION (Limited)                                             ║
╠══════════════════════════════════════════════════════════════════════╣
   Foundation context temporarily unavailable.
   Proceeding with general best practices.
╚══════════════════════════════════════════════════════════════════════╝
"""


def hardcoded_protocol_fallback() -> str:
    """Fallback for Section 5 - protocol is mostly static."""
    return """
╔══════════════════════════════════════════════════════════════════════╗
║  📜 COMMUNICATION PROTOCOL                                           ║
╠══════════════════════════════════════════════════════════════════════╣
   • Be concise and direct
   • Explain reasoning for significant decisions
   • Ask clarifying questions when uncertain
   • Respect existing code patterns
╚══════════════════════════════════════════════════════════════════════╝
"""


def minimal_holistic_context_fallback() -> str:
    """Minimal fallback for Section 6 - Holistic context for current task."""
    return """
╔══════════════════════════════════════════════════════════════════════╗
║  🎯 HOLISTIC CONTEXT (Limited)                                       ║
╠══════════════════════════════════════════════════════════════════════╣

[START: Synaptic to Atlas]
Holistic task context temporarily limited.
Proceeding with available context for this task.
[END: Synaptic to Atlas]

╚══════════════════════════════════════════════════════════════════════╝
"""


def minimal_8th_intelligence_fallback() -> str:
    """Minimal fallback for Section 8 - Synaptic's 8th Intelligence (always present)."""
    from datetime import datetime
    return f"""
╔══════════════════════════════════════════════════════════════════════╗
║  🧠 SECTION 8: SYNAPTIC'S 8TH INTELLIGENCE                            ║
║  The Ever-Present Subconscious Voice to Aaron                        ║
╠══════════════════════════════════════════════════════════════════════╣

[START: Synaptic to Aaron]

  Signal Strength: 🔴 Quiet (limited context for this moment)

  💭 I'm here, Aaron. Even when my signal is quiet,
     I'm watching, learning, and ready to surface
     insights when patterns emerge.

  ⏱️ Subconscious snapshot: {datetime.now().strftime('%H:%M:%S')}

[END: Synaptic to Aaron]

╚══════════════════════════════════════════════════════════════════════╝
"""


def empty_section_fallback(section_name: str = "Section") -> str:
    """Generic empty fallback for non-critical sections."""
    return f"   [{section_name} context temporarily unavailable]\n"


# CLI interface
if __name__ == "__main__":
    import json

    health = SectionHealth()

    if len(sys.argv) > 1:
        if sys.argv[1] == "--json":
            print(json.dumps(health.get_dashboard_status(), indent=2))
        elif sys.argv[1] == "--section":
            section_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
            status = health.check_section(section_id)
            print(json.dumps(status.to_dict(), indent=2))
        else:
            print("Usage: python section_health.py [--json | --section <id>]")
    else:
        # Default: print summary
        overall = health.check_all_sections()
        print(overall.summary())
