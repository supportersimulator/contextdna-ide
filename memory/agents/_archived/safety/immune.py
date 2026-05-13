"""
Immune Agent - Integrity & Drift Monitor

The Immune system protects Synaptic from drift and corruption -
monitoring for configuration changes, detecting anomalies, and
maintaining system integrity.

Anatomical Label: Immune System (Integrity & Drift Monitor)
"""

from __future__ import annotations
import os
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..base import Agent, AgentCategory, AgentState


class ImmuneAgent(Agent):
    """
    Immune Agent - System integrity and drift detection.

    Responsibilities:
    - Monitor configuration integrity
    - Detect behavioral drift
    - Alert on anomalies
    - Maintain system health records
    """

    NAME = "immune"
    CATEGORY = AgentCategory.SAFETY
    DESCRIPTION = "System integrity monitoring and drift detection"
    ANATOMICAL_LABEL = "Immune System (Integrity & Drift Monitor)"
    IS_VITAL = True

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._baseline: Dict[str, str] = {}  # file -> hash
        self._anomalies: List[Dict[str, Any]] = []
        self._drift_metrics: Dict[str, float] = {}
        self._watched_files: List[Path] = []

    def _on_start(self):
        """Initialize immune system."""
        self._setup_watchlist()
        self._establish_baseline()

    def _on_stop(self):
        """Shutdown immune system."""
        pass

    def _setup_watchlist(self):
        """Set up list of files to watch for integrity."""
        memory_dir = Path(__file__).parent.parent.parent
        project_root = memory_dir.parent

        critical_files = [
            memory_dir / "persistent_hook_structure.py",
            memory_dir / "celery_config.py",
            memory_dir / "celery_tasks.py",
            project_root / "CLAUDE.md",
            project_root / ".env"
        ]

        self._watched_files = [f for f in critical_files if f.exists()]

    def _establish_baseline(self):
        """Establish baseline hashes for watched files."""
        for file_path in self._watched_files:
            try:
                content = file_path.read_bytes()
                self._baseline[str(file_path)] = hashlib.sha256(content).hexdigest()
            except Exception as e:
                print(f"[WARN] Failed to hash baseline file {file_path}: {e}")

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check immune system health."""
        integrity_check = self.check_integrity()
        anomaly_count = len(self._anomalies)

        score = 1.0
        if integrity_check.get("violations"):
            score -= 0.2 * len(integrity_check["violations"])
        if anomaly_count > 5:
            score -= 0.1

        return {
            "healthy": score > 0.6,
            "score": max(0.0, score),
            "message": f"Monitoring {len(self._watched_files)} files, {anomaly_count} anomalies",
            "metrics": {
                "watched_files": len(self._watched_files),
                "anomalies": anomaly_count,
                "integrity_violations": len(integrity_check.get("violations", []))
            }
        }

    def process(self, input_data: Any) -> Any:
        """Process immune operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "scan")
            if op == "scan":
                return self.full_scan()
            elif op == "check_integrity":
                return self.check_integrity()
            elif op == "detect_drift":
                return self.detect_drift()
            elif op == "report_anomaly":
                return self.report_anomaly(input_data)
        return self.full_scan()

    def full_scan(self) -> Dict[str, Any]:
        """Run a full system scan."""
        integrity = self.check_integrity()
        drift = self.detect_drift()

        self._last_active = datetime.utcnow()

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "integrity": integrity,
            "drift": drift,
            "anomalies": self._anomalies[-10:],
            "overall_health": "healthy" if not integrity.get("violations") else "compromised"
        }

    def check_integrity(self) -> Dict[str, Any]:
        """Check file integrity against baseline."""
        violations = []

        for file_path in self._watched_files:
            path_str = str(file_path)
            try:
                if not file_path.exists():
                    violations.append({
                        "file": path_str,
                        "type": "missing",
                        "message": "File no longer exists"
                    })
                    continue

                current_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                baseline_hash = self._baseline.get(path_str)

                if baseline_hash and current_hash != baseline_hash:
                    violations.append({
                        "file": path_str,
                        "type": "modified",
                        "message": "File has been modified since baseline"
                    })

            except Exception as e:
                violations.append({
                    "file": path_str,
                    "type": "error",
                    "message": str(e)
                })

        return {
            "checked": len(self._watched_files),
            "violations": violations,
            "passed": len(self._watched_files) - len(violations)
        }

    def detect_drift(self) -> Dict[str, Any]:
        """Detect behavioral drift in the system."""
        drift_indicators = []

        # Check injection success rate drift
        try:
            from memory.injection_store import get_injection_stats
            stats = get_injection_stats()
            success_rate = stats.get("success_rate", 1.0)

            if success_rate < 0.8:
                drift_indicators.append({
                    "metric": "injection_success_rate",
                    "current": success_rate,
                    "expected": 0.8,
                    "severity": "warning" if success_rate > 0.5 else "critical"
                })
        except ImportError as e:
            print(f"[WARN] Injection health monitor not available: {e}")

        # Check for error rate spikes
        error_rate = len([a for a in self._anomalies if a.get("type") == "error"]) / max(len(self._anomalies), 1)
        if error_rate > 0.3:
            drift_indicators.append({
                "metric": "error_rate",
                "current": error_rate,
                "expected": 0.1,
                "severity": "warning"
            })

        return {
            "indicators": drift_indicators,
            "drift_detected": len(drift_indicators) > 0
        }

    def report_anomaly(self, anomaly: Dict[str, Any]) -> bool:
        """Report an anomaly for tracking."""
        anomaly["timestamp"] = datetime.utcnow().isoformat()
        self._anomalies.append(anomaly)

        # Keep only recent anomalies
        if len(self._anomalies) > 100:
            self._anomalies = self._anomalies[-50:]

        # Notify peripheral nerves for broadcast
        self.send_message("peripheral_nerves", "anomaly", anomaly)

        return True

    def update_baseline(self) -> bool:
        """Update baseline to current state."""
        self._establish_baseline()
        return True

    def get_anomaly_summary(self) -> Dict[str, Any]:
        """Get summary of recent anomalies."""
        if not self._anomalies:
            return {"count": 0, "types": {}}

        types = {}
        for a in self._anomalies:
            t = a.get("type", "unknown")
            types[t] = types.get(t, 0) + 1

        return {
            "count": len(self._anomalies),
            "types": types,
            "recent": self._anomalies[-5:]
        }
