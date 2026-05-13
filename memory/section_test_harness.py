"""
Section Test Harness for Context DNA Webhooks

Interactive CLI tool to test individual webhook sections with optional dependency mocking.
Enables isolated testing of each of the 8 webhook sections.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Optional, List, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime
from unittest.mock import patch, MagicMock
from contextlib import contextmanager
from enum import Enum

# Add parent directory and grandparent to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import InjectionConfig and RiskLevel for generator calls
try:
    from memory.persistent_hook_structure import InjectionConfig, RiskLevel
except ImportError:
    # Fallback definitions if import fails
    class RiskLevel(Enum):
        CRITICAL = "critical"
        HIGH = "high"
        MODERATE = "moderate"
        LOW = "low"

    @dataclass
    class InjectionConfig:
        section_0_enabled: bool = True
        section_1_enabled: bool = True
        section_2_enabled: bool = True
        section_3_enabled: bool = True
        section_4_enabled: bool = True
        section_5_enabled: bool = True


@dataclass
class TestResult:
    """Result of testing a single section."""
    section_id: int
    section_name: str
    prompt: str
    output: str
    success: bool
    length: int
    has_expected_markers: bool
    execution_time_ms: float
    error: Optional[str] = None
    mocked_deps: bool = False

    def to_dict(self) -> Dict:
        return {
            "section_id": self.section_id,
            "section_name": self.section_name,
            "prompt": self.prompt,
            "success": self.success,
            "length": self.length,
            "has_expected_markers": self.has_expected_markers,
            "execution_time_ms": self.execution_time_ms,
            "error": self.error,
            "mocked_deps": self.mocked_deps,
            "output_preview": self.output[:200] + "..." if len(self.output) > 200 else self.output
        }

    def summary(self) -> str:
        icon = "✅" if self.success else "❌"
        markers = "✓" if self.has_expected_markers else "✗"
        mock_note = " (mocked)" if self.mocked_deps else ""
        return f"{icon} Section {self.section_id} ({self.section_name}){mock_note}: {self.length} chars, markers: {markers}, {self.execution_time_ms:.1f}ms"


@dataclass
class HarnessReport:
    """Complete test harness report."""
    results: Dict[int, TestResult]
    timestamp: datetime = field(default_factory=datetime.now)
    prompt: str = ""

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "prompt": self.prompt,
            "total_sections": len(self.results),
            "passed_sections": sum(1 for r in self.results.values() if r.success),
            "results": {sid: r.to_dict() for sid, r in self.results.items()}
        }

    def summary(self) -> str:
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║  SECTION TEST HARNESS RESULTS                                ║",
            "╠══════════════════════════════════════════════════════════════╣",
            f"   Prompt: \"{self.prompt[:50]}...\"" if len(self.prompt) > 50 else f"   Prompt: \"{self.prompt}\"",
            "──────────────────────────────────────────────────────────────"
        ]

        for sid, result in sorted(self.results.items()):
            lines.append(f"   {result.summary()}")

        passed = sum(1 for r in self.results.values() if r.success)
        total = len(self.results)

        lines.append("╠══════════════════════════════════════════════════════════════╣")
        lines.append(f"   Results: {passed}/{total} sections passed")
        lines.append("╚══════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


class SectionTestHarness:
    """Interactive test harness for webhook sections."""

    # Section metadata with expected output markers and call signatures
    # Signature types: "prompt_config", "prompt_risk_config", "risk_config", "prompt_optional"
    SECTIONS = {
        0: {"name": "SAFETY", "generator": "generate_section_0", "markers": ["SAFETY", "NEVER"], "sig": "prompt_config"},
        1: {"name": "FOUNDATION", "generator": "generate_section_1", "markers": ["FOUNDATION"], "sig": "prompt_risk_config_ext"},
        2: {"name": "WISDOM", "generator": "generate_section_2", "markers": ["WISDOM", "SOP"], "sig": "prompt_config"},
        3: {"name": "AWARENESS", "generator": "generate_section_3", "markers": ["AWARENESS"], "sig": "prompt_risk_config"},
        4: {"name": "DEEP_CONTEXT", "generator": "generate_section_4", "markers": ["CONTEXT", "LEARNING"], "sig": "prompt_risk_config"},
        5: {"name": "PROTOCOL", "generator": "generate_section_5", "markers": ["PROTOCOL", "COMMUNICATION"], "sig": "risk_config"},
        6: {"name": "HOLISTIC_CONTEXT", "generator": "generate_section_6", "markers": ["HOLISTIC_CONTEXT", "Synaptic"], "sig": "prompt_optional"},
        7: {"name": "FULL_LIBRARY", "generator": None, "markers": ["LIBRARY", "SKILL"], "sig": None},  # Special handling
        8: {"name": "EMERGENCE", "generator": "generate_section_8", "markers": ["8TH INTELLIGENCE", "Synaptic to Aaron"], "sig": "prompt_optional"},
    }

    def __init__(self):
        self._generators = {}
        self._config = None
        self._risk_level = RiskLevel.MODERATE  # Default risk level for testing
        self._load_generators()

    def _load_generators(self):
        """Load section generator functions and InjectionConfig."""
        try:
            from memory import persistent_hook_structure as phs
            self._config = InjectionConfig()  # Default config
            for sid, config in self.SECTIONS.items():
                gen_name = config["generator"]
                if gen_name and hasattr(phs, gen_name):
                    self._generators[sid] = getattr(phs, gen_name)
        except ImportError as e:
            print(f"Warning: Could not load generators: {e}")
            self._config = InjectionConfig()  # Use fallback config

    def _call_generator(self, section_id: int, prompt: str) -> str:
        """Call a section generator with the correct signature."""
        generator = self._generators.get(section_id)
        if not generator:
            return ""

        sig = self.SECTIONS[section_id].get("sig")
        config = self._config

        if sig == "prompt_config":
            # Section 0, 2: (prompt, config)
            return generator(prompt, config)
        elif sig == "prompt_risk_config":
            # Section 3, 4: (prompt, risk_level, config)
            return generator(prompt, self._risk_level, config)
        elif sig == "prompt_risk_config_ext":
            # Section 1: (prompt, risk_level, config, session_id, boundary_decision)
            return generator(prompt, self._risk_level, config)
        elif sig == "risk_config":
            # Section 5: (risk_level, config) - no prompt!
            return generator(self._risk_level, config)
        elif sig == "prompt_optional":
            # Section 6: (prompt, session_id=None, config=None)
            return generator(prompt, None, config)
        else:
            # Fallback: try with just prompt (may fail)
            return generator(prompt)

    @contextmanager
    def _mock_all_dependencies(self):
        """Context manager to mock all external dependencies."""
        mock_patches = []

        # Mock Context DNA client
        mock_patches.append(patch('memory.context_dna_client.ContextDNAClient', MagicMock()))

        # Mock agent service
        mock_agent = MagicMock()
        mock_agent.return_value.get_patterns.return_value = [
            {"name": "Test Pattern", "description": "Mock pattern for testing"}
        ]
        mock_patches.append(patch('memory.agent_service.AgentService', mock_agent))

        # Mock Synaptic health monitor
        mock_synaptic = MagicMock()
        mock_synaptic.return_value.get_health_summary.return_value = {
            "status": "healthy",
            "message": "All systems operational (mock)"
        }
        mock_patches.append(patch('memory.synaptic_health_monitor.SynapticHealthMonitor', mock_synaptic))

        # Mock system monitor
        mock_system = MagicMock()
        mock_system.return_value.get_status.return_value = {
            "cpu": 45,
            "memory": 60,
            "disk": 70
        }
        mock_patches.append(patch('memory.system_monitor.SystemMonitor', mock_system))

        # Enter all patches
        entered = []
        try:
            for p in mock_patches:
                entered.append(p.__enter__())
            yield
        finally:
            # Exit all patches in reverse order
            for p in reversed(mock_patches):
                p.__exit__(None, None, None)

    def _check_markers(self, section_id: int, output: str) -> bool:
        """Check if output contains expected markers for this section."""
        if section_id not in self.SECTIONS:
            return False

        markers = self.SECTIONS[section_id]["markers"]
        output_upper = output.upper()

        # At least one marker should be present
        return any(marker.upper() in output_upper for marker in markers)

    def test_section(self, section_id: int, prompt: str, mock_deps: bool = False) -> TestResult:
        """Test a single section with optional dependency mocking."""
        if section_id not in self.SECTIONS:
            return TestResult(
                section_id=section_id,
                section_name="UNKNOWN",
                prompt=prompt,
                output="",
                success=False,
                length=0,
                has_expected_markers=False,
                execution_time_ms=0,
                error=f"Unknown section: {section_id}"
            )

        config = self.SECTIONS[section_id]
        section_name = config["name"]

        # Get generator
        generator = self._generators.get(section_id)
        if not generator:
            return TestResult(
                section_id=section_id,
                section_name=section_name,
                prompt=prompt,
                output="",
                success=False,
                length=0,
                has_expected_markers=False,
                execution_time_ms=0,
                error=f"Generator not loaded for section {section_id}"
            )

        # Run test
        start_time = datetime.now()
        output = ""
        error = None

        try:
            if mock_deps:
                with self._mock_all_dependencies():
                    output = self._call_generator(section_id, prompt) or ""
            else:
                output = self._call_generator(section_id, prompt) or ""
        except Exception as e:
            error = str(e)[:200]

        end_time = datetime.now()
        execution_time_ms = (end_time - start_time).total_seconds() * 1000

        # Determine success
        success = len(output) > 0 and error is None

        return TestResult(
            section_id=section_id,
            section_name=section_name,
            prompt=prompt,
            output=output,
            success=success,
            length=len(output),
            has_expected_markers=self._check_markers(section_id, output),
            execution_time_ms=execution_time_ms,
            error=error,
            mocked_deps=mock_deps
        )

    def test_all_sections(self, prompt: str, mock_deps: bool = False) -> HarnessReport:
        """Test all sections with the same prompt."""
        results = {}

        for section_id in self.SECTIONS:
            results[section_id] = self.test_section(section_id, prompt, mock_deps)

        return HarnessReport(results=results, prompt=prompt)

    def test_critical_sections(self, prompt: str, mock_deps: bool = False) -> HarnessReport:
        """Test only critical sections (0 and 6)."""
        results = {}

        for section_id in [0, 6]:
            results[section_id] = self.test_section(section_id, prompt, mock_deps)

        return HarnessReport(results=results, prompt=prompt)


def main():
    parser = argparse.ArgumentParser(
        description="Test webhook sections in isolation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python section_test_harness.py --section 0 --prompt "deploy to production"
  python section_test_harness.py --section 6 --prompt "check health" --mock-deps
  python section_test_harness.py --all --prompt "test injection"
  python section_test_harness.py --critical --prompt "emergency check"
        """
    )

    parser.add_argument("--section", "-s", type=int, help="Section ID to test (0-7)")
    parser.add_argument("--prompt", "-p", type=str, default="test prompt", help="Prompt to test with")
    parser.add_argument("--all", "-a", action="store_true", help="Test all sections")
    parser.add_argument("--critical", "-c", action="store_true", help="Test critical sections only (0 and 6)")
    parser.add_argument("--mock-deps", "-m", action="store_true", help="Mock all dependencies")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    parser.add_argument("--show-output", "-o", action="store_true", help="Show full section output")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    harness = SectionTestHarness()

    # Determine what to test
    if args.all:
        report = harness.test_all_sections(args.prompt, args.mock_deps)
    elif args.critical:
        report = harness.test_critical_sections(args.prompt, args.mock_deps)
    elif args.section is not None:
        result = harness.test_section(args.section, args.prompt, args.mock_deps)
        report = HarnessReport(results={args.section: result}, prompt=args.prompt)
    else:
        # Default: test all sections
        report = harness.test_all_sections(args.prompt, args.mock_deps)

    # Output
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

        if args.show_output or args.verbose:
            print("\n" + "=" * 60)
            print("FULL OUTPUT:")
            print("=" * 60)
            for sid, result in sorted(report.results.items()):
                print(f"\n--- Section {sid} ({result.section_name}) ---")
                if result.success:
                    print(result.output)
                else:
                    print(f"ERROR: {result.error}")

    # Exit with appropriate code
    all_passed = all(r.success for r in report.results.values())
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
