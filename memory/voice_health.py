#!/usr/bin/env python3
"""
Voice System Health Monitor for Synaptic

Monitors all components of the voice pipeline:
- STT (Speech-to-Text) via mlx-whisper
- TTS (Text-to-Speech) via edge-tts
- Voice Authentication via resemblyzer
- WebSocket /voice endpoint
- FormatterAgent dual-projection system

Design: "One Brain, Two Projections"
- Single LLM response projected to VOICE or DEV mode
- Voice mode: Terse, spoken-friendly (max 4 blocks, 200 words)
- Dev mode: Full output with voice narrator

Usage:
    from memory.voice_health import VoiceHealthMonitor

    monitor = VoiceHealthMonitor()
    health = monitor.check_all()
    print(health.summary())

    # Or quick check:
    from memory.voice_health import quick_voice_check
    print(quick_voice_check())
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Fix import path for direct execution (python memory/voice_health.py)
_script_dir = Path(__file__).parent.parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

logger = logging.getLogger(__name__)


class VoiceComponentStatus(Enum):
    """Status of a voice component."""
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass
class ComponentHealth:
    """Health of a single voice component."""
    name: str
    status: VoiceComponentStatus
    message: str
    fix_hint: str = ""
    latency_ms: Optional[float] = None
    is_critical: bool = False


@dataclass
class VoiceSystemHealth:
    """Overall voice system health."""
    timestamp: str
    overall_status: VoiceComponentStatus
    components: Dict[str, ComponentHealth]
    warnings: List[str] = field(default_factory=list)
    architecture_note: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status.value,
            "components": {
                name: {
                    "status": c.status.value,
                    "message": c.message,
                    "fix_hint": c.fix_hint,
                    "latency_ms": c.latency_ms,
                    "is_critical": c.is_critical,
                }
                for name, c in self.components.items()
            },
            "warnings": self.warnings,
            "architecture_note": self.architecture_note,
        }

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║  🎤 VOICE SYSTEM HEALTH                                              ║",
            "╠══════════════════════════════════════════════════════════════════════╣",
        ]

        status_icons = {
            VoiceComponentStatus.READY: "✅",
            VoiceComponentStatus.DEGRADED: "⚠️",
            VoiceComponentStatus.UNAVAILABLE: "❌",
            VoiceComponentStatus.ERROR: "🔴",
        }

        for name, component in self.components.items():
            icon = status_icons.get(component.status, "❓")
            critical = " [CRITICAL]" if component.is_critical else ""
            latency = f" ({component.latency_ms:.0f}ms)" if component.latency_ms else ""
            lines.append(f"   {icon} {name}{critical}: {component.message}{latency}")
            if component.status != VoiceComponentStatus.READY and component.fix_hint:
                lines.append(f"      → FIX: {component.fix_hint}")

        lines.append("╠══════════════════════════════════════════════════════════════════════╣")

        overall_icon = status_icons.get(self.overall_status, "❓")
        lines.append(f"   {overall_icon} Overall: {self.overall_status.value.upper()}")

        if self.warnings:
            lines.append("╠══════════════════════════════════════════════════════════════════════╣")
            for warning in self.warnings:
                lines.append(f"   ⚠️  {warning}")

        if self.architecture_note:
            lines.append("╠══════════════════════════════════════════════════════════════════════╣")
            lines.append(f"   📐 {self.architecture_note}")

        lines.append("╚══════════════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)


class VoiceHealthMonitor:
    """
    Monitor health of all voice pipeline components.

    Components monitored:
    1. STT (mlx-whisper) - Speech recognition
    2. TTS (edge-tts) - Speech synthesis
    3. Voice Auth (resemblyzer) - Speaker verification
    4. WebSocket (/voice) - Real-time audio streaming
    5. FormatterAgent - Dual-projection system
    6. Session Validator - Token validation
    """

    def __init__(self):
        self._import_results = {}
        self._check_imports()

    def _check_imports(self):
        """Check which voice modules are available."""
        # STT - mlx-whisper
        try:
            import mlx_whisper
            self._import_results["stt"] = True
        except ImportError:
            self._import_results["stt"] = False

        # TTS - edge-tts
        try:
            import edge_tts
            self._import_results["tts"] = True
        except ImportError:
            self._import_results["tts"] = False

        # Voice Auth - resemblyzer
        try:
            from memory.voice_auth import VOICE_AUTH_AVAILABLE
            self._import_results["voice_auth"] = VOICE_AUTH_AVAILABLE
        except ImportError:
            self._import_results["voice_auth"] = False

        # Session Validator
        try:
            from memory.voice_session_validator import VOICE_SESSION_VALIDATOR_AVAILABLE
            self._import_results["session_validator"] = True
        except ImportError:
            self._import_results["session_validator"] = False

        # FormatterAgent
        try:
            from memory.formatter_agent import FormatterAgent, DeliveryMode
            self._import_results["formatter"] = True
        except ImportError:
            self._import_results["formatter"] = False

    def check_stt(self) -> ComponentHealth:
        """Check STT (Speech-to-Text) availability.

        Checks both local import AND server health endpoint.
        Server may have STT even if local venv doesn't.
        """
        # First check if server reports STT available via health endpoint
        try:
            import urllib.request
            import json as json_lib
            req = urllib.request.Request("http://localhost:8888/health")
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json_lib.loads(response.read().decode())
                if data.get("voice_stt") == "mlx-whisper":
                    return ComponentHealth(
                        name="STT (mlx-whisper)",
                        status=VoiceComponentStatus.READY,
                        message=f"Server has STT: {data.get('voice_stt')}",
                        is_critical=True,
                    )
        except Exception:
            pass  # Fall through to local check

        # Local import check
        if not self._import_results.get("stt"):
            return ComponentHealth(
                name="STT (mlx-whisper)",
                status=VoiceComponentStatus.UNAVAILABLE,
                message="mlx-whisper not installed locally",
                fix_hint="pip install mlx-whisper (or server at 8888 has it)",
                is_critical=True,
            )

        try:
            import mlx_whisper
            model_name = "mlx-community/whisper-turbo"
            return ComponentHealth(
                name="STT (mlx-whisper)",
                status=VoiceComponentStatus.READY,
                message=f"Ready ({model_name})",
                is_critical=True,
            )
        except Exception as e:
            return ComponentHealth(
                name="STT (mlx-whisper)",
                status=VoiceComponentStatus.ERROR,
                message=f"Error: {type(e).__name__}",
                fix_hint=f"Check MLX installation: {str(e)[:50]}",
                is_critical=True,
            )

    def check_tts(self) -> ComponentHealth:
        """Check TTS (Text-to-Speech) availability."""
        if not self._import_results.get("tts"):
            return ComponentHealth(
                name="TTS (edge-tts)",
                status=VoiceComponentStatus.UNAVAILABLE,
                message="edge-tts not installed",
                fix_hint="pip install edge-tts",
                is_critical=True,
            )

        try:
            import edge_tts
            voice = "en-US-AriaNeural"
            return ComponentHealth(
                name="TTS (edge-tts)",
                status=VoiceComponentStatus.READY,
                message=f"Ready ({voice})",
                is_critical=True,
            )
        except Exception as e:
            return ComponentHealth(
                name="TTS (edge-tts)",
                status=VoiceComponentStatus.ERROR,
                message=f"Error: {type(e).__name__}",
                fix_hint=str(e)[:50],
                is_critical=True,
            )

    def check_voice_auth(self) -> ComponentHealth:
        """Check Voice Authentication availability."""
        if not self._import_results.get("voice_auth"):
            return ComponentHealth(
                name="Voice Auth",
                status=VoiceComponentStatus.DEGRADED,
                message="resemblyzer not installed (speaker verification disabled)",
                fix_hint="pip install resemblyzer soundfile",
                is_critical=False,  # System works without it
            )

        try:
            from memory.voice_auth import get_voice_auth_manager
            manager = get_voice_auth_manager()
            return ComponentHealth(
                name="Voice Auth",
                status=VoiceComponentStatus.READY,
                message="Speaker verification ready",
                is_critical=False,
            )
        except Exception as e:
            return ComponentHealth(
                name="Voice Auth",
                status=VoiceComponentStatus.ERROR,
                message=f"Error: {type(e).__name__}",
                fix_hint=str(e)[:50],
                is_critical=False,
            )

    def check_formatter(self) -> ComponentHealth:
        """Check FormatterAgent (dual-projection system)."""
        if not self._import_results.get("formatter"):
            return ComponentHealth(
                name="FormatterAgent",
                status=VoiceComponentStatus.UNAVAILABLE,
                message="formatter_agent.py not found",
                fix_hint="Check memory/formatter_agent.py exists",
                is_critical=False,
            )

        try:
            from memory.formatter_agent import (
                FormatterAgent, DeliveryMode, summary_block
            )
            formatter = FormatterAgent()

            # Quick functional test
            test_blocks = [summary_block("Test summary")]
            voice_out = formatter.project(test_blocks, DeliveryMode.VOICE)
            dev_out = formatter.project(test_blocks, DeliveryMode.DEV)

            if voice_out.blocks and dev_out.blocks:
                return ComponentHealth(
                    name="FormatterAgent",
                    status=VoiceComponentStatus.READY,
                    message="Dual-projection VOICE/DEV ready",
                    is_critical=False,
                )
            else:
                return ComponentHealth(
                    name="FormatterAgent",
                    status=VoiceComponentStatus.DEGRADED,
                    message="Projection returned empty blocks",
                    is_critical=False,
                )
        except Exception as e:
            return ComponentHealth(
                name="FormatterAgent",
                status=VoiceComponentStatus.ERROR,
                message=f"Error: {type(e).__name__}",
                fix_hint=str(e)[:50],
                is_critical=False,
            )

    def check_websocket_endpoint(self) -> ComponentHealth:
        """Check WebSocket /voice endpoint availability."""
        import socket

        try:
            # Quick TCP check on port 8888
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            start = time.time()
            result = sock.connect_ex(('127.0.0.1', 8888))
            latency = (time.time() - start) * 1000
            sock.close()

            if result == 0:
                return ComponentHealth(
                    name="WebSocket /voice",
                    status=VoiceComponentStatus.READY,
                    message="Port 8888 accepting connections",
                    latency_ms=latency,
                    is_critical=True,
                )
            else:
                return ComponentHealth(
                    name="WebSocket /voice",
                    status=VoiceComponentStatus.UNAVAILABLE,
                    message="Port 8888 not responding",
                    fix_hint="Start synaptic_chat_server.py",
                    is_critical=True,
                )
        except Exception as e:
            return ComponentHealth(
                name="WebSocket /voice",
                status=VoiceComponentStatus.ERROR,
                message=f"Error: {type(e).__name__}",
                fix_hint=str(e)[:50],
                is_critical=True,
            )

    def check_session_validator(self) -> ComponentHealth:
        """Check session token validator availability."""
        if not self._import_results.get("session_validator"):
            return ComponentHealth(
                name="Session Validator",
                status=VoiceComponentStatus.DEGRADED,
                message="Dev mode only (no token validation)",
                fix_hint="Install PyJWT for production token validation",
                is_critical=False,
            )

        return ComponentHealth(
            name="Session Validator",
            status=VoiceComponentStatus.READY,
            message="Token validation ready",
            is_critical=False,
        )

    def check_all(self) -> VoiceSystemHealth:
        """Run all health checks."""
        components = {
            "stt": self.check_stt(),
            "tts": self.check_tts(),
            "voice_auth": self.check_voice_auth(),
            "formatter": self.check_formatter(),
            "websocket": self.check_websocket_endpoint(),
            "session": self.check_session_validator(),
        }

        # Determine overall status
        has_critical_failure = any(
            c.status in (VoiceComponentStatus.UNAVAILABLE, VoiceComponentStatus.ERROR)
            and c.is_critical
            for c in components.values()
        )

        has_any_failure = any(
            c.status in (VoiceComponentStatus.UNAVAILABLE, VoiceComponentStatus.ERROR)
            for c in components.values()
        )

        has_degraded = any(
            c.status == VoiceComponentStatus.DEGRADED
            for c in components.values()
        )

        if has_critical_failure:
            overall = VoiceComponentStatus.UNAVAILABLE
        elif has_any_failure:
            overall = VoiceComponentStatus.ERROR
        elif has_degraded:
            overall = VoiceComponentStatus.DEGRADED
        else:
            overall = VoiceComponentStatus.READY

        # Gather warnings
        warnings = []
        for name, c in components.items():
            if c.status != VoiceComponentStatus.READY:
                warnings.append(f"{c.name}: {c.message}")

        return VoiceSystemHealth(
            timestamp=datetime.now().isoformat(),
            overall_status=overall,
            components=components,
            warnings=warnings,
            architecture_note="One Brain, Two Projections: VOICE (terse) ↔ DEV (full)",
        )


def quick_voice_check() -> str:
    """Quick voice system health check for CLI."""
    monitor = VoiceHealthMonitor()
    health = monitor.check_all()
    return health.summary()


# CLI interface
if __name__ == "__main__":
    import json

    monitor = VoiceHealthMonitor()

    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        health = monitor.check_all()
        print(json.dumps(health.to_dict(), indent=2))
    else:
        print(quick_voice_check())
