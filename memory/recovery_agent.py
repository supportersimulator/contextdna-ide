#!/usr/bin/env python3
"""
RECOVERY AGENT - LLM-Assisted System Recovery Orchestration
============================================================

Consolidates app discovery, webhook integration health, and LLM-assisted
recovery into a unified intelligent recovery system.

ARCHITECTURE:
    ┌─────────────────────────────────────────────────────────────────┐
    │                      RECOVERY AGENT                              │
    │  "The IT Department That Never Sleeps"                           │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                  │
    │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
    │  │ APP RECOVERY     │  │ WEBHOOK HEALTH   │  │ LLM ADVISOR  │  │
    │  │ (app_recovery.py)│  │ (injection_      │  │ (Ollama/     │  │
    │  │                  │  │  health_monitor) │  │  OpenAI)     │  │
    │  │ - Docker         │  │                  │  │              │  │
    │  │ - Ollama         │  │ - VS Code ext    │  │ Analyzes:    │  │
    │  │ - PostgreSQL     │  │ - Cursor         │  │ - Errors     │  │
    │  │ - Redis          │  │ - ChatGPT        │  │ - Patterns   │  │
    │  │ - LiveKit        │  │ - Windsurf       │  │ - Solutions  │  │
    │  └──────────────────┘  └──────────────────┘  └──────────────┘  │
    │                            │                                    │
    │                            ▼                                    │
    │  ┌────────────────────────────────────────────────────────────┐│
    │  │               UNIFIED RECOVERY ORCHESTRATOR                 ││
    │  │                                                             ││
    │  │  1. Detect failures (apps + webhooks)                       ││
    │  │  2. Load cached configs from SQLite                         ││
    │  │  3. Consult LLM for recovery strategy                       ││
    │  │  4. Execute recovery steps                                  ││
    │  │  5. Record results (success/failure commands)               ││
    │  │  6. Learn for next time                                     ││
    │  └────────────────────────────────────────────────────────────┘│
    │                                                                  │
    │  Storage: ~/.context-dna/context_dna.db (SQLite)                │
    │           PostgreSQL (when Docker healthy)                       │
    │                                                                  │
    └─────────────────────────────────────────────────────────────────┘

CAPABILITIES:
1. App Recovery - Docker, Ollama, PostgreSQL, Redis, etc.
2. Webhook Health - VS Code Claude Code, Cursor, ChatGPT, Windsurf
3. LLM-Assisted Recovery - Use local LLM to suggest recovery steps
4. Self-Learning - Record what worked for future recovery

Usage:
    from memory.recovery_agent import RecoveryAgent

    agent = RecoveryAgent()

    # Run full system check
    health = agent.check_system_health()

    # Attempt intelligent recovery
    results = agent.recover_system()

    # Get LLM advice for specific issue
    advice = agent.get_llm_recovery_advice("ollama", "connection refused")

CLI:
    python memory/recovery_agent.py status
    python memory/recovery_agent.py recover
    python memory/recovery_agent.py advise ollama "connection refused"
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Literal
from enum import Enum

logger = logging.getLogger(__name__)

# =============================================================================
# IMPORTS WITH GRACEFUL FALLBACKS
# =============================================================================

# App Recovery
try:
    from context_dna.storage.app_recovery import AppRecoveryManager, KNOWN_APPS
    APP_RECOVERY_AVAILABLE = True
except ImportError:
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "context-dna" / "src"))
        from context_dna.storage.app_recovery import AppRecoveryManager, KNOWN_APPS
        APP_RECOVERY_AVAILABLE = True
    except ImportError:
        APP_RECOVERY_AVAILABLE = False
        AppRecoveryManager = None
        KNOWN_APPS = {}

# Injection Health Monitor
try:
    from memory.injection_health_monitor import (
        InjectionHealth, get_webhook_monitor,
        WebhookDestination, WebhookPhase
    )
    INJECTION_MONITOR_AVAILABLE = True
except ImportError:
    INJECTION_MONITOR_AVAILABLE = False

# Docker Recovery
try:
    from context_dna.storage.docker_recovery import DockerRecoveryHelper
    DOCKER_RECOVERY_AVAILABLE = True
except ImportError:
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "context-dna" / "src"))
        from context_dna.storage.docker_recovery import DockerRecoveryHelper
        DOCKER_RECOVERY_AVAILABLE = True
    except ImportError:
        DOCKER_RECOVERY_AVAILABLE = False
        DockerRecoveryHelper = None


# =============================================================================
# LLM INTERFACE
# =============================================================================

class LLMProvider(str, Enum):
    """Supported LLM providers for recovery assistance."""
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    NONE = "none"


@dataclass
class LLMConfig:
    """LLM configuration for recovery assistance."""
    provider: LLMProvider = LLMProvider.OLLAMA
    model: str = "qwen2.5:3b"  # Fast, local, good for recovery tasks
    ollama_url: str = "http://localhost:11434"
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    max_tokens: int = 500
    temperature: float = 0.3  # Low for deterministic recovery advice


class LLMAdvisor:
    """
    LLM-assisted recovery advisor.

    Provides intelligent suggestions for system recovery based on
    cached configurations and error patterns.
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self._check_availability()

    def _check_availability(self):
        """Check if configured LLM is available."""
        if self.config.provider == LLMProvider.OLLAMA:
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"{self.config.ollama_url}/api/version",
                    method='GET'
                )
                urllib.request.urlopen(req, timeout=2)
                self.available = True
            except Exception:
                self.available = False
        elif self.config.provider == LLMProvider.OPENAI:
            self.available = bool(self.config.openai_api_key or os.environ.get("Context_DNA_OPENAI"))
        elif self.config.provider == LLMProvider.ANTHROPIC:
            self.available = bool(self.config.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
        else:
            self.available = False

    def get_recovery_advice(
        self,
        app_name: str,
        error: str,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Get LLM-generated recovery advice for a failing app.

        Args:
            app_name: Name of the failing app (docker, ollama, etc.)
            error: Error message or symptom
            context: Cached config, discovery info, previous attempts

        Returns:
            Dict with advice, suggested_commands, confidence
        """
        if not self.available:
            return {
                "advice": "LLM advisor not available",
                "suggested_commands": [],
                "confidence": 0.0,
                "provider": "none"
            }

        prompt = self._build_recovery_prompt(app_name, error, context)

        try:
            if self.config.provider == LLMProvider.OLLAMA:
                response = self._query_ollama(prompt)
            elif self.config.provider == LLMProvider.OPENAI:
                response = self._query_openai(prompt)
            elif self.config.provider == LLMProvider.ANTHROPIC:
                response = self._query_anthropic(prompt)
            else:
                response = None

            if response:
                return self._parse_advice_response(response, self.config.provider.value)

        except Exception as e:
            logger.warning(f"LLM query failed: {e}")

        return {
            "advice": "Could not get LLM advice",
            "suggested_commands": [],
            "confidence": 0.0,
            "provider": self.config.provider.value,
            "error": str(e) if 'e' in dir() else "Unknown error"
        }

    def _build_recovery_prompt(
        self,
        app_name: str,
        error: str,
        context: Dict[str, Any]
    ) -> str:
        """Build a focused recovery prompt."""
        platform = context.get("platform", "unknown")
        cached_config = context.get("cached_config", {})
        previous_attempts = context.get("previous_attempts", [])

        prompt = f"""You are a system recovery assistant. Help recover a failing service.

Consider whatever seems relevant to you:

- **What's the actual problem?** What does this error indicate about what's broken?
- **Why might this be happening?** What are the likely root causes?
- **How can we fix it?** What specific steps would restore service?
- **What evidence would confirm success?** How do we verify the fix worked?

SERVICE: {app_name}
PLATFORM: {platform}
ERROR: {error}

CACHED CONFIGURATION:
- Executable: {cached_config.get('executable_path', 'unknown')}
- Launch command: {cached_config.get('launch_command', 'unknown')}
- Last healthy: {cached_config.get('last_healthy_at', 'unknown')}
- Recovery attempts: {cached_config.get('recovery_attempts', 0)}

PREVIOUS RECOVERY ATTEMPTS:
{json.dumps(previous_attempts[-3:], indent=2) if previous_attempts else 'None'}

Share your recovery suggestions in whatever way makes sense. You could:
- Provide specific commands to try
- Explain the approach and reasoning
- Suggest diagnostic steps
- Recommend both immediate fixes and long-term solutions

Format suggestion (if JSON makes sense):
{{
    "analysis": "Brief analysis of the issue",
    "suggested_commands": ["command1", "command2"],
    "explanation": "Why these commands should help",
    "confidence": 0.8
}}

Or natural language: just describe your analysis and recommended steps."""
        return prompt

    def _query_ollama(self, prompt: str) -> Optional[str]:
        """Query Ollama for advice."""
        import urllib.request
        import json

        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens
            }
        }

        req = urllib.request.Request(
            f"{self.config.ollama_url}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("response", "")

    def _query_openai(self, prompt: str) -> Optional[str]:
        """Query OpenAI-tier LLM for recovery advice via priority queue.

        Routed via LLM priority queue for DeepSeek fallback (2026-04-19).
        The queue handles provider selection (local mlx_lm → DeepSeek → OpenAI
        fallback) transparently, so recovery callers no longer embed an OpenAI
        API key or SDK. Keeps the public method name for backwards compatibility
        with LLMProvider.OPENAI dispatch.
        """
        # Routed via LLM priority queue for DeepSeek fallback (2026-04-19)
        from memory.llm_priority_queue import llm_generate, Priority

        return llm_generate(
            system_prompt="You are a system recovery assistant. Return JSON when requested.",
            user_prompt=prompt,
            priority=Priority.BACKGROUND,
            profile="extract",
            caller="recovery_agent._query_openai",
            timeout_s=30.0,
        )

    def _query_anthropic(self, prompt: str) -> Optional[str]:
        """Query Anthropic for advice."""
        import urllib.request
        import json

        api_key = self.config.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")

        payload = {
            "model": "claude-3-haiku-20240307",  # Fast and cheap
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config.max_tokens,
        }

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            },
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result["content"][0]["text"]

    def _parse_advice_response(self, response: str, provider: str) -> Dict[str, Any]:
        """Parse LLM response into structured advice."""
        # Try to extract JSON from response
        try:
            # Find JSON block
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                json_str = response[start:end]
                parsed = json.loads(json_str)
                parsed["provider"] = provider
                return parsed
        except json.JSONDecodeError as e:
            print(f"[WARN] Recovery plan JSON parse failed: {e}")

        # Fallback: extract commands from text
        lines = response.split('\n')
        commands = []
        for line in lines:
            line = line.strip()
            if line.startswith(('$', '>', '-', '*')) and len(line) > 3:
                cmd = line.lstrip('$>-* ').strip()
                if cmd and not cmd.startswith('#'):
                    commands.append(cmd)

        return {
            "advice": response[:500],
            "suggested_commands": commands[:5],
            "confidence": 0.5,
            "provider": provider
        }


# =============================================================================
# RECOVERY AGENT
# =============================================================================

@dataclass
class SystemHealth:
    """Overall system health status."""
    status: Literal["healthy", "degraded", "critical"]
    timestamp: str
    apps: Dict[str, Dict[str, Any]]
    webhooks: Optional[Dict[str, Any]]
    docker_healthy: bool
    llm_available: bool
    alerts: List[str]
    recommendations: List[str]


@dataclass
class RecoveryResult:
    """Result of a recovery attempt."""
    app_name: str
    success: bool
    method: str
    duration_ms: int
    llm_assisted: bool
    commands_tried: List[str]
    error: Optional[str]


class RecoveryAgent:
    """
    Unified recovery agent that orchestrates app recovery, webhook health
    monitoring, and LLM-assisted recovery.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        llm_config: Optional[LLMConfig] = None
    ):
        """
        Initialize recovery agent.

        Args:
            db_path: Path to SQLite database
            llm_config: LLM configuration for recovery assistance
        """
        if db_path is None:
            db_path = Path.home() / ".context-dna" / "context_dna.db"
        self.db_path = Path(db_path)

        # Initialize components
        self.app_manager = AppRecoveryManager(db_path) if APP_RECOVERY_AVAILABLE else None
        self.injection_monitor = get_webhook_monitor() if INJECTION_MONITOR_AVAILABLE else None
        self.docker_helper = DockerRecoveryHelper() if DOCKER_RECOVERY_AVAILABLE else None
        self.llm_advisor = LLMAdvisor(llm_config)

        logger.info(f"RecoveryAgent initialized - Apps: {APP_RECOVERY_AVAILABLE}, "
                   f"Webhooks: {INJECTION_MONITOR_AVAILABLE}, "
                   f"LLM: {self.llm_advisor.available}")

    # =========================================================================
    # HEALTH CHECKING
    # =========================================================================

    def check_system_health(self) -> SystemHealth:
        """
        Comprehensive system health check.

        Checks:
        - Docker status
        - All known apps (Ollama, Redis, PostgreSQL, etc.)
        - Webhook injection destinations
        - LLM availability
        """
        now = datetime.now(timezone.utc)
        alerts = []
        recommendations = []

        # Check Docker first (critical dependency)
        docker_healthy = False
        if self.docker_helper:
            docker_healthy = self.docker_helper.is_docker_running()
            if not docker_healthy:
                alerts.append("🚨 Docker is not running - heavy mode unavailable")
                recommendations.append("Run: open -a Docker")

        # Check apps
        apps = {}
        if self.app_manager:
            for app_name in KNOWN_APPS:
                is_healthy, status = self.app_manager.check_app_health(app_name)
                config = self.app_manager.load_config_from_sqlite(app_name)

                apps[app_name] = {
                    "healthy": is_healthy,
                    "status": status,
                    "last_healthy": config.get("last_healthy_at") if config else None,
                    "recovery_attempts": config.get("recovery_attempts", 0) if config else 0,
                    "is_critical": KNOWN_APPS[app_name].is_critical if app_name in KNOWN_APPS else False
                }

                if not is_healthy and apps[app_name]["is_critical"]:
                    alerts.append(f"🚨 CRITICAL: {app_name} is unhealthy: {status}")
                    recommendations.append(f"Run: python memory/recovery_agent.py recover {app_name}")

        # Check webhooks
        webhooks = None
        if self.injection_monitor:
            try:
                injection_health = self.injection_monitor.check_health()
                webhooks = {
                    "status": injection_health.status,
                    "eighth_intelligence": injection_health.eighth_intelligence_status,
                    "injections_24h": injection_health.total_injections_24h,
                    "destinations": {
                        d: {
                            "status": dh.status,
                            "count_24h": dh.injection_count_24h
                        }
                        for d, dh in injection_health.destinations.items()
                    }
                }

                if injection_health.status != "healthy":
                    alerts.append(f"⚠️ Webhook injection system: {injection_health.status}")

                if injection_health.eighth_intelligence_status != "active":
                    alerts.append(f"⚠️ 8th Intelligence: {injection_health.eighth_intelligence_status}")

            except Exception as e:
                logger.warning(f"Webhook health check failed: {e}")

        # Determine overall status
        if any("CRITICAL" in a for a in alerts):
            status = "critical"
        elif alerts:
            status = "degraded"
        else:
            status = "healthy"

        return SystemHealth(
            status=status,
            timestamp=now.isoformat(),
            apps=apps,
            webhooks=webhooks,
            docker_healthy=docker_healthy,
            llm_available=self.llm_advisor.available,
            alerts=alerts,
            recommendations=recommendations
        )

    # =========================================================================
    # RECOVERY
    # =========================================================================

    def recover_app(
        self,
        app_name: str,
        use_llm: bool = True
    ) -> RecoveryResult:
        """
        Attempt to recover a specific app.

        Args:
            app_name: Name of app to recover
            use_llm: Whether to consult LLM for recovery advice

        Returns:
            RecoveryResult with details
        """
        import time
        start = time.time()
        commands_tried = []

        if not self.app_manager:
            return RecoveryResult(
                app_name=app_name,
                success=False,
                method="none",
                duration_ms=0,
                llm_assisted=False,
                commands_tried=[],
                error="App recovery not available"
            )

        # Check if already healthy
        is_healthy, status = self.app_manager.check_app_health(app_name)
        if is_healthy:
            return RecoveryResult(
                app_name=app_name,
                success=True,
                method="already_healthy",
                duration_ms=int((time.time() - start) * 1000),
                llm_assisted=False,
                commands_tried=[],
                error=None
            )

        # Get cached config
        config = self.app_manager.load_config_from_sqlite(app_name)

        # Try standard recovery first
        if config and config.get("launch_command"):
            cmd = config["launch_command"]
            commands_tried.append(cmd)

            success = self.app_manager.recover_app(app_name)
            if success:
                return RecoveryResult(
                    app_name=app_name,
                    success=True,
                    method="cached_command",
                    duration_ms=int((time.time() - start) * 1000),
                    llm_assisted=False,
                    commands_tried=commands_tried,
                    error=None
                )

        # If standard recovery failed and LLM is available, get advice
        if use_llm and self.llm_advisor.available:
            context = self.app_manager.get_llm_recovery_context(app_name)
            advice = self.llm_advisor.get_recovery_advice(
                app_name=app_name,
                error=status,
                context=context
            )

            # Try LLM-suggested commands
            for cmd in advice.get("suggested_commands", [])[:3]:
                commands_tried.append(cmd)
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True,
                        timeout=30, text=True
                    )

                    # Check if app is now healthy
                    time.sleep(3)  # Give it time to start
                    is_healthy, _ = self.app_manager.check_app_health(app_name)

                    if is_healthy:
                        # Record successful command
                        self._record_successful_command(app_name, cmd)

                        return RecoveryResult(
                            app_name=app_name,
                            success=True,
                            method="llm_assisted",
                            duration_ms=int((time.time() - start) * 1000),
                            llm_assisted=True,
                            commands_tried=commands_tried,
                            error=None
                        )

                except Exception as e:
                    logger.warning(f"LLM command failed: {cmd} - {e}")

        return RecoveryResult(
            app_name=app_name,
            success=False,
            method="exhausted",
            duration_ms=int((time.time() - start) * 1000),
            llm_assisted=use_llm and self.llm_advisor.available,
            commands_tried=commands_tried,
            error=status
        )

    def recover_system(self, use_llm: bool = True) -> Dict[str, RecoveryResult]:
        """
        Attempt to recover all unhealthy critical apps.

        Args:
            use_llm: Whether to use LLM assistance

        Returns:
            Dict of app_name -> RecoveryResult
        """
        results = {}

        # Check health first
        health = self.check_system_health()

        # Recover Docker first if needed (critical dependency)
        if not health.docker_healthy and self.docker_helper:
            logger.info("Attempting Docker recovery...")
            docker_started = self.docker_helper.ensure_docker_running()
            results["docker"] = RecoveryResult(
                app_name="docker",
                success=docker_started,
                method="docker_helper",
                duration_ms=0,
                llm_assisted=False,
                commands_tried=[],
                error=None if docker_started else "Docker failed to start"
            )

        # Recover unhealthy apps in dependency order
        for app_name, app_status in sorted(
            health.apps.items(),
            key=lambda x: KNOWN_APPS.get(x[0], type('', (), {'startup_order': 100})()).startup_order
        ):
            if not app_status["healthy"] and app_status.get("is_critical"):
                logger.info(f"Attempting recovery: {app_name}")
                results[app_name] = self.recover_app(app_name, use_llm=use_llm)

        return results

    def _record_successful_command(self, app_name: str, command: str):
        """Record a command that successfully recovered an app."""
        if not self.app_manager:
            return

        try:
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            # Update successful recovery commands
            cursor.execute("""
                UPDATE app_recovery_configs
                SET
                    successful_launch_command = ?,
                    launch_command = ?,
                    recovery_successes = recovery_successes + 1,
                    last_recovery_at = ?,
                    last_healthy_at = ?,
                    updated_at = ?
                WHERE app_name = ?
            """, (
                command, command,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                app_name
            ))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.warning(f"Failed to record successful command: {e}")

    # =========================================================================
    # CAPTURE
    # =========================================================================

    def capture_healthy_state(self) -> Dict[str, bool]:
        """
        Capture configuration for all healthy apps.

        Run this periodically to keep cached configs up to date.
        """
        results = {}

        if not self.app_manager:
            return results

        for app_name in KNOWN_APPS:
            config = self.app_manager.capture_app_config(app_name)
            if config:
                success = self.app_manager.save_config_to_sqlite(config)
                results[app_name] = success
                if success:
                    logger.info(f"Captured config: {app_name}")

        return results

    # =========================================================================
    # LLM ADVICE
    # =========================================================================

    def get_llm_recovery_advice(
        self,
        app_name: str,
        error: str
    ) -> Dict[str, Any]:
        """
        Get LLM advice for recovering a specific app.

        Args:
            app_name: Name of the app
            error: Error message or description

        Returns:
            Structured advice from LLM
        """
        if not self.llm_advisor.available:
            return {
                "error": "LLM advisor not available",
                "suggestion": "Start Ollama with: ollama serve"
            }

        context = {}
        if self.app_manager:
            context = self.app_manager.get_llm_recovery_context(app_name)

        return self.llm_advisor.get_recovery_advice(
            app_name=app_name,
            error=error,
            context=context
        )


# =============================================================================
# CLI
# =============================================================================

def main():
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    agent = RecoveryAgent()

    if len(sys.argv) < 2:
        print("Usage: python memory/recovery_agent.py <command> [args]")
        print("\nCommands:")
        print("  status              - Show system health")
        print("  recover [app]       - Recover unhealthy apps (or specific app)")
        print("  capture             - Capture healthy app configs")
        print("  advise <app> <err>  - Get LLM recovery advice")
        return

    cmd = sys.argv[1]

    if cmd == "status":
        health = agent.check_system_health()

        print("=" * 70)
        print("RECOVERY AGENT - SYSTEM HEALTH")
        print("=" * 70)

        status_icon = {"healthy": "✅", "degraded": "⚠️", "critical": "🚨"}[health.status]
        print(f"\nOverall: {status_icon} {health.status.upper()}")
        print(f"Docker: {'✅' if health.docker_healthy else '❌'}")
        print(f"LLM Advisor: {'✅' if health.llm_available else '❌'}")

        print(f"\n📱 APPS")
        for name, status in health.apps.items():
            icon = "✅" if status["healthy"] else "❌"
            critical = " [CRITICAL]" if status.get("is_critical") else ""
            print(f"  {icon} {name}{critical}: {status['status']}")

        if health.webhooks:
            print(f"\n📡 WEBHOOKS")
            print(f"  Status: {health.webhooks['status']}")
            print(f"  8th Intelligence: {health.webhooks['eighth_intelligence']}")
            print(f"  Injections (24h): {health.webhooks['injections_24h']}")

        if health.alerts:
            print(f"\n🚨 ALERTS")
            for alert in health.alerts:
                print(f"  {alert}")

        if health.recommendations:
            print(f"\n💡 RECOMMENDATIONS")
            for rec in health.recommendations:
                print(f"  • {rec}")

    elif cmd == "recover":
        app = sys.argv[2] if len(sys.argv) > 2 else None

        if app:
            print(f"Attempting recovery: {app}...")
            result = agent.recover_app(app)
            print(f"\n{'✅' if result.success else '❌'} {app}: {result.method}")
            if result.commands_tried:
                print(f"  Commands tried: {result.commands_tried}")
            if result.error:
                print(f"  Error: {result.error}")
        else:
            print("Attempting system recovery...")
            results = agent.recover_system()
            print(f"\n{'=' * 50}")
            print("RECOVERY RESULTS")
            print("=" * 50)
            for name, result in results.items():
                icon = "✅" if result.success else "❌"
                llm = " (LLM)" if result.llm_assisted else ""
                print(f"{icon} {name}: {result.method}{llm}")

    elif cmd == "capture":
        print("Capturing healthy app configurations...")
        results = agent.capture_healthy_state()
        for name, success in results.items():
            icon = "✅" if success else "❌"
            print(f"  {icon} {name}")

    elif cmd == "advise":
        if len(sys.argv) < 4:
            print("Usage: ... advise <app> <error>")
            return
        app = sys.argv[2]
        error = " ".join(sys.argv[3:])

        print(f"Getting LLM advice for {app}: {error}...")
        advice = agent.get_llm_recovery_advice(app, error)
        print(json.dumps(advice, indent=2))

    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
