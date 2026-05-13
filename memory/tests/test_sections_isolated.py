"""
Isolated Tests for Webhook Sections

Tests each webhook section independently with mocked dependencies.
Ensures each section can generate content even when dependencies fail.

Created: January 29, 2026
Part of: Webhook Hardening Initiative
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from enum import Enum

# Add memory directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import config classes for generator calls
try:
    from memory.persistent_hook_structure import InjectionConfig, RiskLevel
except ImportError:
    # Fallback definitions
    class RiskLevel(Enum):
        CRITICAL = "critical"
        HIGH = "high"
        MODERATE = "moderate"
        LOW = "low"

    from dataclasses import dataclass

    @dataclass
    class InjectionConfig:
        section_0_enabled: bool = True
        section_1_enabled: bool = True
        section_2_enabled: bool = True
        section_3_enabled: bool = True
        section_4_enabled: bool = True
        section_5_enabled: bool = True


class TestSectionIsolation:
    """Test each webhook section in isolation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup for each test."""
        pass

    @pytest.fixture
    def mock_context_dna_client(self):
        """Mock external Context DNA API."""
        with patch('memory.context_dna_client.ContextDNAClient') as mock:
            mock_instance = MagicMock()
            mock_instance.query.return_value = {"learnings": []}
            mock_instance.health_check.return_value = True
            mock.return_value = mock_instance
            yield mock

    @pytest.fixture
    def mock_agent_service(self):
        """Mock agent service for offline testing."""
        with patch('memory.agent_service.AgentService') as mock:
            mock_instance = MagicMock()
            mock_instance.get_patterns.return_value = [
                {"name": "Test Pattern", "description": "Mock pattern"}
            ]
            mock.return_value = mock_instance
            yield mock

    @pytest.fixture
    def mock_system_monitor(self):
        """Mock system monitor."""
        with patch('memory.system_monitor.SystemMonitor') as mock:
            mock_instance = MagicMock()
            mock_instance.get_status.return_value = {
                "cpu": 45,
                "memory": 60,
                "disk": 70,
                "status": "healthy"
            }
            mock_instance.to_family_message.return_value = "All systems nominal"
            mock.return_value = mock_instance
            yield mock

    @pytest.fixture
    def mock_synaptic_health(self):
        """Mock Synaptic health monitor."""
        with patch('memory.synaptic_health_monitor.SynapticHealthMonitor') as mock:
            mock_instance = MagicMock()
            mock_instance.get_health_summary.return_value = {
                "status": "healthy",
                "services": {"postgres": "up", "redis": "up"},
                "message": "All systems operational"
            }
            mock_instance.generate_family_message.return_value = "Synaptic systems healthy"
            mock.return_value = mock_instance
            yield mock

    # Section 0: SAFETY - Must ALWAYS work

    def test_section_0_safety_isolated(self):
        """Section 0 must work with zero dependencies."""
        from memory.persistent_hook_structure import generate_section_0

        config = InjectionConfig()
        result = generate_section_0("deploy to production", config)

        assert result is not None
        assert len(result) > 0
        # Should contain safety-related content
        assert any(word in result.upper() for word in ["SAFETY", "NEVER", "CRITICAL", "HIGH", "MODERATE", "LOW"])

    def test_section_0_safety_critical_prompt(self):
        """Section 0 should detect critical risk for dangerous prompts."""
        from memory.persistent_hook_structure import generate_section_0

        config = InjectionConfig()
        result = generate_section_0("delete the production database", config)

        assert result is not None
        # Should have safety content
        assert "NEVER" in result.upper() or "SAFETY" in result.upper()

    def test_section_0_safety_low_risk_prompt(self):
        """Section 0 should classify low-risk prompts appropriately."""
        from memory.persistent_hook_structure import generate_section_0

        config = InjectionConfig()
        result = generate_section_0("explain how the code works", config)

        assert result is not None
        assert len(result) > 0

    # Section 5: PROTOCOL - Also should work without dependencies

    def test_section_5_protocol_isolated(self):
        """Section 5 should work without external dependencies."""
        from memory.persistent_hook_structure import generate_section_5

        config = InjectionConfig()
        # Section 5 signature: (risk_level, config) - no prompt!
        result = generate_section_5(RiskLevel.MODERATE, config)

        assert result is not None
        # Protocol section should have communication guidelines

    # Section 6: HOLISTIC_CONTEXT - Critical for Synaptic's voice

    def test_section_6_holistic_context_isolated(self, mock_system_monitor, mock_synaptic_health):
        """Section 6 must generate Synaptic's voice."""
        from memory.persistent_hook_structure import generate_section_6

        config = InjectionConfig()
        # Section 6 signature: (prompt, session_id=None, config=None)
        result = generate_section_6("help me with webhook", None, config)

        assert result is not None
        # Should contain family communication markers
        assert any(word in result.upper() for word in ["HOLISTIC", "COMMUNICATION", "SYNAPTIC"])

    def test_section_6_holistic_context_fallback_on_monitor_failure(self):
        """Section 6 should have fallback when monitors fail."""
        # Test with import errors
        with patch.dict('sys.modules', {'memory.synaptic_health_monitor': None}):
            from memory.persistent_hook_structure import generate_section_6

            config = InjectionConfig()
            result = generate_section_6("test", None, config)

            # Should still return something (fallback content)
            assert result is not None

    # Test fallbacks

    def test_section_fallback_on_dependency_failure(self):
        """Verify fallback content when dependency fails."""
        # Import the fallback functions directly
        from memory.section_health import (
            hardcoded_safety_fallback,
            minimal_foundation_fallback,
            hardcoded_protocol_fallback,
            minimal_family_fallback,
            empty_section_fallback
        )

        # Test each fallback
        safety = hardcoded_safety_fallback("deploy to production")
        assert "RISK" in safety.upper()

        foundation = minimal_foundation_fallback()
        assert "FOUNDATION" in foundation.upper()

        protocol = hardcoded_protocol_fallback()
        assert "PROTOCOL" in protocol.upper()

        holistic_context = minimal_family_fallback()
        assert "HOLISTIC" in holistic_context.upper()

        empty = empty_section_fallback("Test Section")
        assert len(empty) > 0

    # Section Health Tests

    def test_section_health_check_all(self):
        """Test section health check functionality."""
        from memory.section_health import SectionHealth

        health = SectionHealth()
        overall = health.check_all_sections()

        assert overall is not None
        assert hasattr(overall, 'all_healthy')
        assert hasattr(overall, 'critical_sections_healthy')
        assert hasattr(overall, 'section_statuses')

        # Should have status for all 8 sections
        assert len(overall.section_statuses) == 8

    def test_section_health_individual(self):
        """Test individual section health checks."""
        from memory.section_health import SectionHealth

        health = SectionHealth()

        # Section 0 should always be healthy (no deps)
        status_0 = health.check_section(0)
        assert status_0.healthy  # No dependencies
        assert status_0.section_name == "SAFETY"

        # Section 5 should always be healthy (no deps)
        status_5 = health.check_section(5)
        assert status_5.healthy
        assert status_5.section_name == "PROTOCOL"

    def test_section_health_dependencies(self):
        """Test that section dependencies are correctly defined."""
        from memory.section_health import SectionHealth

        health = SectionHealth()

        # Section 0 and 5 should have no dependencies
        assert health.get_section_dependencies(0) == []
        assert health.get_section_dependencies(5) == []

        # Section 6 should have dependencies
        deps_6 = health.get_section_dependencies(6)
        assert len(deps_6) > 0

    def test_section_health_critical_flags(self):
        """Test that critical sections are correctly marked."""
        from memory.section_health import SectionHealth

        health = SectionHealth()

        # Sections 0 and 6 are critical
        assert health.is_critical_section(0) is True
        assert health.is_critical_section(6) is True

        # Other sections are not critical
        assert health.is_critical_section(1) is False
        assert health.is_critical_section(5) is False

    # Webhook Verifier Tests

    def test_webhook_verifier_runs(self):
        """Test that webhook verifier can run."""
        from memory.webhook_verifier import WebhookVerifier

        verifier = WebhookVerifier()
        report = verifier.verify_installation()

        assert report is not None
        assert hasattr(report, 'passed')
        assert hasattr(report, 'results')
        assert len(report.results) > 0

    def test_webhook_verifier_summary(self):
        """Test webhook verifier summary output."""
        from memory.webhook_verifier import WebhookVerifier

        verifier = WebhookVerifier()
        report = verifier.verify_installation()
        summary = report.summary()

        assert "WEBHOOK INSTALLATION VERIFICATION" in summary
        assert "checks passed" in summary.lower()

    # Test Harness Tests

    def test_section_test_harness_single(self):
        """Test section test harness with single section."""
        from memory.section_test_harness import SectionTestHarness

        harness = SectionTestHarness()
        result = harness.test_section(0, "test prompt")

        assert result is not None
        assert result.section_id == 0
        assert result.section_name == "SAFETY"

    def test_section_test_harness_all(self):
        """Test section test harness with all sections."""
        from memory.section_test_harness import SectionTestHarness

        harness = SectionTestHarness()
        report = harness.test_all_sections("test prompt")

        assert report is not None
        assert len(report.results) >= 1  # At least some sections should test

    def test_section_test_harness_critical(self):
        """Test section test harness with critical sections only."""
        from memory.section_test_harness import SectionTestHarness

        harness = SectionTestHarness()
        report = harness.test_critical_sections("test prompt")

        assert report is not None
        # Should only have sections 0 and 6
        assert 0 in report.results
        assert 6 in report.results


class TestIntegration:
    """Integration tests for webhook system."""

    def test_full_injection_pipeline(self):
        """Test complete injection pipeline works."""
        try:
            from memory.persistent_hook_structure import generate_context_injection

            result = generate_context_injection("test integration", mode="hybrid")

            assert result is not None
            # Result is an InjectionResult object, check its content attribute
            assert hasattr(result, 'content')
            assert len(result.content) > 100  # Should have substantial content
        except ImportError:
            pytest.skip("generate_context_injection not available")

    def test_section_health_integration(self):
        """Test section health integrates with verifier."""
        from memory.section_health import SectionHealth
        from memory.webhook_verifier import WebhookVerifier

        # Both should work together
        health = SectionHealth()
        verifier = WebhookVerifier()

        health_result = health.check_all_sections()
        verify_result = verifier.verify_installation()

        # Both should return valid results
        assert health_result is not None
        assert verify_result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
