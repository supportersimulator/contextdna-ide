"""
Health Barge-In System for Context DNA

Implements Synaptic's proactive remediation philosophy:
'Attempt expert-level fixes FIRST before alerting'

This module provides a clean interface for the barge-in system
that wraps synaptic_health_monitor.py's functionality.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


class RemediationAction(Enum):
    """Types of remediation actions."""
    START_SERVICE = "start_service"
    RESTART_SERVICE = "restart_service"
    REBUILD_SERVICE = "rebuild_service"
    WAIT = "wait"
    MANUAL_REQUIRED = "manual_required"


@dataclass
class HealthIssue:
    """A detected health issue."""
    component: str
    description: str
    severity: str  # "critical", "warning", "info"
    suggested_action: RemediationAction
    details: Optional[Dict] = None


@dataclass
class RemediationResult:
    """Result of a remediation attempt."""
    attempted: bool
    success: bool = False
    reason: str = ""
    needs_human: bool = False
    suggested_action: Optional[str] = None
    what_was_tried: Optional[str] = None
    why_failed: Optional[str] = None
    message: str = ""
    error: Optional[str] = None


@dataclass
class BargeInDecision:
    """Decision about whether to barge in and what to say."""
    should_barge_in: bool
    message: Optional[str] = None
    issues_found: List[HealthIssue] = field(default_factory=list)
    issues_fixed: int = 0
    issues_remaining: int = 0
    remediation_attempted: bool = False


class HealthBargeIn:
    """
    Implements Synaptic's proactive remediation philosophy:
    'Attempt expert-level fixes FIRST before alerting'

    This class provides a unified interface for health checking and
    proactive remediation, delegating to synaptic_health_monitor.py
    for the actual implementation.
    """

    # Safe operations that Synaptic can perform autonomously
    SAFE_OPERATIONS = {
        "start_service": "docker compose up -d {service}",
        "restart_service": "docker restart {container}",
        "rebuild_service": "docker compose up -d --build {service}",
    }

    # Unsafe operations that require human intervention
    UNSAFE_OPERATIONS = [
        "stop_service",       # Could interrupt work
        "remove_container",   # Data loss risk
        "delete_volume",      # Irreversible
        "modify_config",      # Could break system
        "prune_images",       # Might remove needed images
        "drop_database",      # Data loss
    ]

    def __init__(self):
        """Initialize the health barge-in system."""
        self._monitor = None

    def _get_monitor(self):
        """Lazy-load the synaptic health monitor."""
        if self._monitor is None:
            try:
                from memory.synaptic_health_monitor import SynapticHealthMonitor
                self._monitor = SynapticHealthMonitor()
            except ImportError:
                return None
        return self._monitor

    def check_and_remediate(self) -> BargeInDecision:
        """
        Main entry point: Check health and attempt remediation.

        Returns a BargeInDecision indicating whether to alert the user.
        """
        monitor = self._get_monitor()
        if not monitor:
            return BargeInDecision(
                should_barge_in=False,
                message=None,
                issues_found=[],
            )

        # Run health check with auto-fix enabled
        report = monitor.check_system_health(auto_fix=True)

        # Convert to BargeInDecision
        issues_found = []

        # Add critical issues
        for issue in report.critical_issues:
            issues_found.append(HealthIssue(
                component="infrastructure",
                description=issue,
                severity="critical",
                suggested_action=RemediationAction.MANUAL_REQUIRED
            ))

        # Add warnings
        for warning in report.warnings:
            issues_found.append(HealthIssue(
                component="infrastructure",
                description=warning,
                severity="warning",
                suggested_action=RemediationAction.RESTART_SERVICE
            ))

        # Get message if needed
        message = None
        if report.should_barge_in:
            message = monitor.to_barge_in_message(report)
        elif report.issues_fixed > 0:
            message = monitor.to_success_message(report)

        return BargeInDecision(
            should_barge_in=report.should_barge_in,
            message=message,
            issues_found=issues_found,
            issues_fixed=report.issues_fixed,
            issues_remaining=report.issues_remaining,
            remediation_attempted=report.remediation_attempted
        )

    def quick_health_check(self) -> Dict[str, Any]:
        """
        Quick health check without remediation.

        Useful for dashboard status or quick queries.
        """
        monitor = self._get_monitor()
        if not monitor:
            return {
                "healthy": True,
                "message": "Health monitor not available",
                "services": []
            }

        report = monitor.check_system_health(auto_fix=False)

        return {
            "healthy": report.overall_status.value == "healthy",
            "status": report.overall_status.value,
            "message": monitor.get_health_summary(),
            "critical_issues": report.critical_issues,
            "warnings": report.warnings,
            "services": [
                {
                    "name": s.name,
                    "running": s.running,
                    "healthy": s.healthy,
                    "importance": s.importance
                }
                for s in report.services
            ]
        }

    def should_alert_user(self) -> bool:
        """
        Quick check: Should we alert the user about health issues?

        This is a fast path for code that just needs to know yes/no.
        """
        decision = self.check_and_remediate()
        return decision.should_barge_in

    def get_barge_in_message(self) -> Optional[str]:
        """
        Get the barge-in message if there are issues.

        Returns None if everything is healthy or was fixed.
        """
        decision = self.check_and_remediate()
        return decision.message


# Module-level convenience functions

_barge_in = None


def get_barge_in() -> HealthBargeIn:
    """Get or create the global HealthBargeIn instance."""
    global _barge_in
    if _barge_in is None:
        _barge_in = HealthBargeIn()
    return _barge_in


def check_and_remediate() -> BargeInDecision:
    """Check health and attempt remediation."""
    return get_barge_in().check_and_remediate()


def quick_check() -> Dict[str, Any]:
    """Quick health check without remediation."""
    return get_barge_in().quick_health_check()


def should_alert() -> bool:
    """Quick check if we should alert the user."""
    return get_barge_in().should_alert_user()


def get_message() -> Optional[str]:
    """Get barge-in message if needed."""
    return get_barge_in().get_barge_in_message()


# CLI interface
if __name__ == "__main__":
    import json

    barge_in = HealthBargeIn()

    if len(sys.argv) > 1:
        if sys.argv[1] == "--quick":
            result = barge_in.quick_health_check()
            print(json.dumps(result, indent=2))
        elif sys.argv[1] == "--json":
            decision = barge_in.check_and_remediate()
            print(json.dumps({
                "should_barge_in": decision.should_barge_in,
                "issues_fixed": decision.issues_fixed,
                "issues_remaining": decision.issues_remaining,
                "remediation_attempted": decision.remediation_attempted,
                "issues": [
                    {
                        "component": i.component,
                        "description": i.description,
                        "severity": i.severity
                    }
                    for i in decision.issues_found
                ]
            }, indent=2))
        elif sys.argv[1] == "--alert-check":
            if barge_in.should_alert_user():
                print("ALERT: Health issues detected")
                sys.exit(1)
            else:
                print("OK: No issues requiring alert")
                sys.exit(0)
    else:
        # Default: Run full check and print message if any
        decision = barge_in.check_and_remediate()

        if decision.message:
            print(decision.message)
        else:
            print("✅ All systems healthy - no barge-in needed")

        print()
        print(f"Issues found: {len(decision.issues_found)}")
        print(f"Issues fixed: {decision.issues_fixed}")
        print(f"Issues remaining: {decision.issues_remaining}")
