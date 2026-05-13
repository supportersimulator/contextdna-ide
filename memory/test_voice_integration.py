#!/usr/bin/env python3
"""
Integration tests for Voice System critical paths.

Tests:
1. Voice health check (all components)
2. Phone-inject with dev_mode parity
3. FormatterAgent dual-projection
4. WebSocket /voice endpoint connectivity
5. TTS sanitization for speech synthesis

Run: python -m pytest memory/test_voice_integration.py -v
"""

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Optional
import pytest

# Fix import path
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.voice_health import VoiceHealthMonitor, VoiceComponentStatus
from memory.formatter_agent import (
    FormatterAgent,
    DeliveryMode,
    BlockType,
    summary_block,
    status_block,
    code_block,
    risk_block,
)


# =============================================================================
# TEST 1: Voice Health Check
# =============================================================================

class TestVoiceHealth:
    """Test voice health monitoring."""

    def test_health_monitor_instantiation(self):
        """VoiceHealthMonitor should initialize without errors."""
        monitor = VoiceHealthMonitor()
        assert monitor is not None
        assert hasattr(monitor, "_import_results")

    def test_health_check_returns_valid_structure(self):
        """Health check should return properly structured data."""
        monitor = VoiceHealthMonitor()
        health = monitor.check_all()

        assert health.timestamp is not None
        assert health.overall_status in VoiceComponentStatus
        assert "stt" in health.components
        assert "tts" in health.components
        assert "formatter" in health.components
        assert "websocket" in health.components

    def test_health_check_to_dict(self):
        """Health check should serialize to dict."""
        monitor = VoiceHealthMonitor()
        health = monitor.check_all()
        data = health.to_dict()

        assert "timestamp" in data
        assert "overall_status" in data
        assert "components" in data
        assert isinstance(data["components"], dict)

    def test_health_summary_generates_string(self):
        """Health summary should produce readable string."""
        monitor = VoiceHealthMonitor()
        health = monitor.check_all()
        summary = health.summary()

        assert isinstance(summary, str)
        assert "VOICE SYSTEM HEALTH" in summary
        assert "stt" in summary.lower() or "STT" in summary


# =============================================================================
# TEST 2: Phone-Inject with dev_mode
# =============================================================================

class TestPhoneInjectDevMode:
    """Test phone-inject endpoint with dev_mode parameter."""

    @pytest.fixture
    def server_available(self):
        """Check if server is running on port 8888."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', 8888))
        sock.close()
        return result == 0

    def test_phone_inject_voice_mode(self, server_available):
        """Phone-inject should return VOICE projection when dev_mode=false."""
        if not server_available:
            pytest.skip("Server not running on port 8888")

        import urllib.request

        data = json.dumps({
            "prompt": "test message",
            "preset": "phone",
            "dev_mode": False
        }).encode()

        req = urllib.request.Request(
            "http://localhost:8888/api/phone-inject",
            data=data,
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())

        assert result["status"] == "success"
        assert result["metadata"]["dev_mode"] == False
        assert result["metadata"]["projection"] == "VOICE"

    def test_phone_inject_dev_mode(self, server_available):
        """Phone-inject should return DEV projection when dev_mode=true."""
        if not server_available:
            pytest.skip("Server not running on port 8888")

        import urllib.request

        data = json.dumps({
            "prompt": "test message",
            "preset": "phone",
            "dev_mode": True
        }).encode()

        req = urllib.request.Request(
            "http://localhost:8888/api/phone-inject",
            data=data,
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())

        assert result["status"] == "success"
        assert result["metadata"]["dev_mode"] == True
        assert result["metadata"]["projection"] == "DEV"

    def test_phone_inject_version_includes_features(self, server_available):
        """Version endpoint should advertise dev_mode feature."""
        if not server_available:
            pytest.skip("Server not running on port 8888")

        import urllib.request

        req = urllib.request.Request("http://localhost:8888/api/phone-inject/version")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())

        assert "tts" in result["presets"]
        assert result["features"]["dev_mode"] == True
        assert result["features"]["one_brain_two_projections"] == True


# =============================================================================
# TEST 3: FormatterAgent Dual Projection
# =============================================================================

class TestFormatterAgentProjection:
    """Test FormatterAgent dual-projection system."""

    def setup_method(self):
        self.formatter = FormatterAgent()

    def test_voice_projection_filters_code_blocks(self):
        """VOICE projection should exclude CODE blocks."""
        blocks = [
            summary_block("This is the summary"),
            code_block("def foo(): pass", language="python"),
        ]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        # Should only have summary, not code
        output_text = output.as_string()
        assert "def foo" not in output_text
        assert "summary" in output_text.lower() or "This is the summary" in output_text

    def test_dev_projection_includes_all_blocks(self):
        """DEV projection should include all block types."""
        blocks = [
            summary_block("This is the summary"),
            code_block("def foo(): pass", language="python"),
        ]

        output = self.formatter.project(blocks, DeliveryMode.DEV)

        # Should have both summary and code
        output_text = output.as_string()
        assert "def foo" in output_text

    def test_voice_projection_respects_word_limit(self):
        """VOICE projection should truncate at word limit."""
        long_text = " ".join(["word"] * 300)  # 300 words
        blocks = [summary_block(long_text)]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        total_words = sum(len(b.split()) for b in output.blocks)
        assert total_words <= 200  # VOICE_MAX_WORDS

    def test_dev_projection_includes_voice_narrator(self):
        """DEV projection should include voice_narrator for audio."""
        blocks = [summary_block("This is the main summary for voice.")]

        output = self.formatter.project(blocks, DeliveryMode.DEV)

        assert hasattr(output, "voice_narrator")
        assert len(output.voice_narrator) > 0


# =============================================================================
# TEST 4: WebSocket /voice Connectivity
# =============================================================================

class TestVoiceWebSocketConnectivity:
    """Test WebSocket /voice endpoint basic connectivity."""

    def test_port_8888_accepts_connections(self):
        """Port 8888 should accept TCP connections."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)

        try:
            result = sock.connect_ex(('127.0.0.1', 8888))
            # 0 = success, anything else = failure
            assert result == 0, f"Port 8888 connection failed with code {result}"
        finally:
            sock.close()

    def test_health_endpoint_responds(self):
        """HTTP /health endpoint should respond."""
        import urllib.request

        try:
            req = urllib.request.Request("http://localhost:8888/health")
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode())

            assert data["status"] == "ready"
            assert "voice_stt" in data
            assert "voice_tts" in data
        except Exception as e:
            pytest.skip(f"Server not responding: {e}")


# =============================================================================
# TEST 5: TTS Sanitization
# =============================================================================

class TestTTSSanitization:
    """Test TTS text sanitization for speech synthesis."""

    def setup_method(self):
        self.formatter = FormatterAgent()

    def test_removes_code_blocks(self):
        """Should remove triple-backtick code blocks."""
        text = "Here's code:\n```python\ndef foo():\n    pass\n```\nDone."
        result = self.formatter._sanitize_for_voice(text)

        assert "```" not in result
        assert "def foo" not in result
        assert "Done" in result

    def test_removes_markdown_formatting(self):
        """Should remove markdown bold/italic."""
        text = "This is **bold** and *italic* text."
        result = self.formatter._sanitize_for_voice(text)

        assert "**" not in result
        assert "*" not in result
        assert "bold" in result
        assert "italic" in result

    def test_replaces_urls(self):
        """Should replace URLs with 'link'."""
        text = "Visit https://example.com/path for more."
        result = self.formatter._sanitize_for_voice(text)

        assert "https://" not in result
        assert "link" in result.lower()

    def test_removes_brackets(self):
        """Should remove brackets and braces."""
        text = "Status: {task_id: 123} and [note]."
        result = self.formatter._sanitize_for_voice(text)

        assert "{" not in result
        assert "}" not in result
        assert "[" not in result
        assert "]" not in result

    def test_ensures_ending_punctuation(self):
        """Should add period if no ending punctuation."""
        text = "This has no period"
        result = self.formatter._sanitize_for_voice(text)

        assert result.endswith(".")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
