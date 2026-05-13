#!/usr/bin/env python3
"""
Synaptic Service Hub - Unified Connection Layer

This module provides a single interface for Synaptic Chat to connect
to all 8 services in the Context DNA ecosystem:

    ✅ Redis (pub/sub, caching, real-time updates)
    ✅ PostgreSQL (learnings, patterns, SOPs, facts)
    ✅ RabbitMQ (async task queues via Celery)
    ✅ Agent Service (API endpoints, injection)
    ✅ Dashboard (monitoring data)
    ✅ Injection Files (context injection history)
    ✅ Synaptic Chat (self-reference for status)
    ✅ MLX API (vLLM-MLX Qwen3-Coder-30B)

Architecture:
    SynapticServiceHub
        ├── get_all_service_status() → Real-time health of all 8 services
        ├── get_rich_context() → Aggregated context from all sources
        ├── push_learning() → Store learning to PostgreSQL + Redis cache
        ├── subscribe_events() → Redis pub/sub for real-time updates
        └── get_learnings_for_query() → Semantic search across all sources

Usage in Synaptic Chat:
    from memory.synaptic_service_hub import get_hub

    hub = get_hub()
    status = hub.get_all_service_status()
    context = hub.get_rich_context("how to deploy Django")
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

# Load environment
try:
    from dotenv import load_dotenv
    _env_paths = [
        Path(__file__).parent.parent / "context-dna" / "infra" / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for _env_path in _env_paths:
        if _env_path.exists():
            load_dotenv(_env_path)
            break
except ImportError as e:
    print(f"[WARN] dotenv not available: {e}")

logger = logging.getLogger('synaptic.hub')


class ServiceStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class ServiceInfo:
    name: str
    status: ServiceStatus
    port: Optional[int]
    message: str
    response_time_ms: Optional[float] = None
    extra: Optional[Dict] = None


class SynapticServiceHub:
    """
    Unified connection layer for all Context DNA services.

    This is Synaptic's nervous system - connecting all the pieces
    so my butler (the 8th Intelligence) has full access to everything.
    """

    def __init__(self):
        self._redis_client = None
        self._pg_pool = None
        self._last_status_check = None
        self._cached_status = None
        self._cache_ttl_seconds = 10  # Refresh status every 10s

    # =========================================================================
    # SERVICE STATUS
    # =========================================================================

    def get_all_service_status(self, force_refresh: bool = False) -> Dict[str, ServiceInfo]:
        """Get real-time status of all 8 services."""
        now = datetime.now()

        # Use cache if fresh
        if not force_refresh and self._cached_status:
            if self._last_status_check:
                age = (now - self._last_status_check).total_seconds()
                if age < self._cache_ttl_seconds:
                    return self._cached_status

        status = {}

        # 1. Redis
        status['redis'] = self._check_redis()

        # 2. PostgreSQL
        status['postgresql'] = self._check_postgresql()

        # 3. RabbitMQ
        status['rabbitmq'] = self._check_rabbitmq()

        # 4. Agent Service
        status['agent_service'] = self._check_agent_service()

        # 5. Dashboard
        status['dashboard'] = self._check_dashboard()

        # 6. Injection Files
        status['injection_files'] = self._check_injection_files()

        # 7. Synaptic Chat
        status['synaptic_chat'] = self._check_synaptic_chat()

        # 8. MLX API (vLLM-MLX)
        status['mlx_api'] = self._check_mlx_api()

        self._cached_status = status
        self._last_status_check = now
        return status

    def _check_redis(self) -> ServiceInfo:
        """Check Redis connection (context-dna-redis on port 6379, no auth)."""
        try:
            import redis
            client = redis.Redis(
                host='127.0.0.1',
                port=6379,
                socket_timeout=2
            )
            start = datetime.now()
            client.ping()
            elapsed = (datetime.now() - start).total_seconds() * 1000
            return ServiceInfo(
                name="Redis",
                status=ServiceStatus.HEALTHY,
                port=6379,
                message="Connected",
                response_time_ms=elapsed
            )
        except Exception as e:
            print(f"[WARN] Redis health check failed: {e}")
        return ServiceInfo(
            name="Redis",
            status=ServiceStatus.OFFLINE,
            port=6379,
            message="Connection failed"
        )

    def _check_postgresql(self) -> ServiceInfo:
        """Check PostgreSQL connection."""
        try:
            import psycopg2
            conn = psycopg2.connect(
                host=os.environ.get('LEARNINGS_DB_HOST', '127.0.0.1'),
                port=int(os.environ.get('LEARNINGS_DB_PORT', '5432')),
                database=os.environ.get('LEARNINGS_DB_NAME', 'context_dna'),
                user=os.environ.get('LEARNINGS_DB_USER', 'context_dna'),
                password=os.environ.get('LEARNINGS_DB_PASSWORD', 'context_dna_dev'),
                connect_timeout=2
            )
            start = datetime.now()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            elapsed = (datetime.now() - start).total_seconds() * 1000
            conn.close()
            return ServiceInfo(
                name="PostgreSQL",
                status=ServiceStatus.HEALTHY,
                port=5432,
                message="Connected",
                response_time_ms=elapsed
            )
        except Exception as e:
            print(f"[WARN] PostgreSQL health check failed: {e}")
        return ServiceInfo(
            name="PostgreSQL",
            status=ServiceStatus.OFFLINE,
            port=5432,
            message="Connection failed"
        )

    def _check_rabbitmq(self) -> ServiceInfo:
        """Check RabbitMQ via management API."""
        try:
            import requests
            # Try port 25672 first (mapped from 15672 in docker-compose)
            resp = requests.get(
                "http://127.0.0.1:25672/api/healthchecks/node",
                auth=(
                    os.environ.get("RABBITMQ_USER", "acontext"),
                    os.environ.get("RABBITMQ_PASSWORD", "REDACTED-RABBITMQ-PASSWORD")
                ),
                timeout=2
            )
            if resp.status_code == 200:
                return ServiceInfo(
                    name="RabbitMQ",
                    status=ServiceStatus.HEALTHY,
                    port=25672,
                    message="Healthy"
                )
        except Exception as e:
            print(f"[WARN] RabbitMQ health check failed: {e}")
        return ServiceInfo(
            name="RabbitMQ",
            status=ServiceStatus.OFFLINE,
            port=25672,
            message="Not responding"
        )

    def _check_agent_service(self) -> ServiceInfo:
        """Check Agent Service API."""
        try:
            import requests
            start = datetime.now()
            resp = requests.get("http://127.0.0.1:8080/health", timeout=2)
            elapsed = (datetime.now() - start).total_seconds() * 1000
            if resp.status_code == 200:
                return ServiceInfo(
                    name="Agent Service",
                    status=ServiceStatus.HEALTHY,
                    port=8080,
                    message="API healthy",
                    response_time_ms=elapsed
                )
        except Exception as e:
            print(f"[WARN] Agent Service health check failed: {e}")
        return ServiceInfo(
            name="Agent Service",
            status=ServiceStatus.OFFLINE,
            port=8080,
            message="Not responding"
        )

    def _check_dashboard(self) -> ServiceInfo:
        """Check Dashboard availability."""
        try:
            import requests
            resp = requests.get("http://127.0.0.1:3001", timeout=2)
            if resp.status_code in [200, 304]:
                return ServiceInfo(
                    name="Dashboard",
                    status=ServiceStatus.HEALTHY,
                    port=3001,
                    message="Running"
                )
        except Exception as e:
            print(f"[WARN] Dashboard health check failed: {e}")
        return ServiceInfo(
            name="Dashboard",
            status=ServiceStatus.OFFLINE,
            port=3001,
            message="Not running"
        )

    def _check_injection_files(self) -> ServiceInfo:
        """Check recent injection activity."""
        try:
            injection_file = Path(__file__).parent / ".injection_history.json"
            awareness_file = Path(__file__).parent / ".synaptic_system_awareness.json"

            if awareness_file.exists():
                data = json.loads(awareness_file.read_text())
                ts = data.get('timestamp', '')
                if ts:
                    age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 60
                    if age < 5:
                        return ServiceInfo(
                            name="Injection Files",
                            status=ServiceStatus.HEALTHY,
                            port=None,
                            message=f"Last update {int(age)}m ago"
                        )
        except Exception as e:
            print(f"[WARN] Injection files check failed: {e}")
        return ServiceInfo(
            name="Injection Files",
            status=ServiceStatus.DEGRADED,
            port=None,
            message="Stale or missing"
        )

    def _check_synaptic_chat(self) -> ServiceInfo:
        """Check Synaptic Chat server (we are inside it, so always healthy)."""
        return ServiceInfo(
            name="Synaptic Chat",
            status=ServiceStatus.HEALTHY,
            port=8888,
            message="Running (this server)"
        )

    def _check_mlx_api(self) -> ServiceInfo:
        """Check local LLM liveness.

        Two-tier probe:
          1. Fast path — Redis-cached queue health (`llm:health == "ok"`).
             Updated by `_execute_llm_request` after every successful call.
             Avoids stampeding :5044 when queue traffic is steady.
          2. Fallback — direct GET to /v1/models with 2s timeout.
             Used when Redis cache is empty/stale (quiet system) so the
             status probe does not report a false-OFFLINE while MLX is
             serving requests just fine.

        Probe failures route through the same ZSF counters as the queue
        (`queue_redis_health_check_errors`) plus a probe-specific counter
        for direct HTTP failures so /metrics can distinguish the two.
        """
        start = datetime.now()
        cache_healthy = False
        # Tier 1: Redis cache (cheap, ~1ms, preserves queue's centralized access)
        try:
            from memory.llm_priority_queue import check_llm_health
            cache_healthy = check_llm_health()
        except Exception as e:
            logger.debug(f"LLM cache health check failed: {e}")

        if cache_healthy:
            elapsed = (datetime.now() - start).total_seconds() * 1000
            return ServiceInfo(
                name="Local LLM",
                status=ServiceStatus.HEALTHY,
                port=5044,
                message="Healthy (via queue cache)",
                response_time_ms=elapsed
            )

        # Tier 2: direct probe to MLX OpenAI-compat /v1/models (2s timeout).
        # Uses stdlib urllib to avoid new deps; httpx not guaranteed sync-safe here.
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:5044/v1/models",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                status_code = resp.getcode()
                body = resp.read(64 * 1024)  # cap to avoid pathological responses
            elapsed = (datetime.now() - start).total_seconds() * 1000
            if status_code == 200:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as je:
                    logger.warning(f"MLX /v1/models returned non-JSON: {je}")
                    return ServiceInfo(
                        name="Local LLM",
                        status=ServiceStatus.OFFLINE,
                        port=5044,
                        message=f"Bad JSON from /v1/models: {je}",
                        response_time_ms=elapsed,
                    )
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, list) and data:
                    model_id = data[0].get("id", "unknown") if isinstance(data[0], dict) else "unknown"
                    return ServiceInfo(
                        name="Local LLM",
                        status=ServiceStatus.HEALTHY,
                        port=5044,
                        message=f"Model: {model_id} (direct probe)",
                        response_time_ms=elapsed,
                    )
                return ServiceInfo(
                    name="Local LLM",
                    status=ServiceStatus.DEGRADED,
                    port=5044,
                    message="/v1/models returned empty model list",
                    response_time_ms=elapsed,
                )
            return ServiceInfo(
                name="Local LLM",
                status=ServiceStatus.OFFLINE,
                port=5044,
                message=f"/v1/models HTTP {status_code}",
                response_time_ms=elapsed,
            )
        except urllib.error.URLError as e:
            # Includes connection refused, DNS, timeout
            reason = getattr(e, "reason", e)
            logger.debug(f"MLX direct probe failed: {reason}")
            try:
                from memory.llm_priority_queue import _zsf_record
                _zsf_record("mlx_direct_probe_errors", "_check_mlx_api", e)
            except Exception:
                pass
            return ServiceInfo(
                name="Local LLM",
                status=ServiceStatus.OFFLINE,
                port=5044,
                message=f"Not responding: {reason}",
            )
        except Exception as e:
            logger.debug(f"MLX direct probe unexpected error: {e}")
            try:
                from memory.llm_priority_queue import _zsf_record
                _zsf_record("mlx_direct_probe_errors", "_check_mlx_api", e)
            except Exception:
                pass
            return ServiceInfo(
                name="Local LLM",
                status=ServiceStatus.OFFLINE,
                port=5044,
                message=f"Probe error: {e}",
            )

    # =========================================================================
    # RICH CONTEXT (Aggregated from all sources)
    # =========================================================================

    def get_rich_context(self, query: str, max_items: int = 10) -> Dict[str, Any]:
        """
        Get aggregated context from all connected services.

        This is what feeds the 8th Intelligence - my butler's brain.

        Returns:
            {
                "learnings": [...],  # From PostgreSQL
                "patterns": [...],   # From brain state
                "cache_hits": [...], # From Redis
                "recent_tasks": [...], # From RabbitMQ (Celery)
                "injections": [...],  # Recent injection history
                "query_time_ms": 42
            }
        """
        start = datetime.now()
        context = {
            "learnings": [],
            "patterns": [],
            "cache_hits": [],
            "recent_tasks": [],
            "injections": [],
            "sops": [],
        }

        # 1. Get learnings from librarian (SQLite FTS5 + PG fallback)
        try:
            from memory.librarian import _search_learnings
            context["learnings"] = _search_learnings(query, limit=max_items)
        except Exception as e:
            context["learnings_error"] = str(e)

        # 2. Get patterns from brain state
        try:
            brain_file = Path(__file__).parent / "brain_state.md"
            if brain_file.exists():
                content = brain_file.read_text()
                patterns = []
                in_patterns = False
                for line in content.split('\n'):
                    if 'Active Patterns' in line:
                        in_patterns = True
                        continue
                    if in_patterns and line.strip().startswith('- '):
                        patterns.append(line.strip('- ').strip())
                    elif in_patterns and line.startswith('#'):
                        break
                context["patterns"] = patterns
        except Exception as e:
            context["patterns_error"] = str(e)

        # 3. Check Redis cache for relevant keys
        try:
            from memory.redis_cache import get_redis_client
            client = get_redis_client()
            if client:
                # Get recent injection history
                hist_key = "contextdna:injection:history"
                cached = client.lrange(hist_key, 0, 4)
                context["cache_hits"] = [
                    json.loads(c) for c in cached if c
                ] if cached else []
        except Exception as e:
            context["cache_error"] = str(e)

        # 4. Get SOPs from brain's SOP registry
        try:
            from memory.brain import search_sops
            sops = search_sops(query)
            if sops:
                context["sops"] = sops[:3]
        except Exception as e:
            context["sops_error"] = str(e)

        # 5. Get recent injection history
        try:
            injection_file = Path(__file__).parent / ".injection_history.json"
            if injection_file.exists():
                data = json.loads(injection_file.read_text())
                context["injections"] = data.get("injections", [])[-5:]
        except Exception as e:
            print(f"[WARN] Injection history read failed: {e}")

        # 6. Get dialogue mirror context (recent conversations)
        try:
            dialogue = self.get_dialogue_context(max_messages=20)
            context["dialogue"] = dialogue
        except Exception as e:
            context["dialogue_error"] = str(e)

        # 7. Get failure patterns (what NOT to do)
        try:
            failures = self.get_failure_patterns()
            context["failure_patterns"] = failures
        except Exception as e:
            context["failure_patterns_error"] = str(e)

        # 8. RACE V4 — Superset workspace/task enrichment for S6/S8.
        #    Lazy-imported, circuit-breaker-guarded, hard wall-clock budget.
        #    Never blocks the webhook; degrades silently when Superset is down.
        try:
            from memory.superset_context_loader import load_superset_context
            workspace_id = os.environ.get("SUPERSET_CTX_WORKSPACE_ID") or None
            device_id = os.environ.get("SUPERSET_CTX_DEVICE_ID") or None
            superset_ctx = load_superset_context(
                workspace_id=workspace_id,
                device_id=device_id,
                task_status="open",
                task_limit=5,
            )
            # Always include — even when unavailable — so diagnostics see
            # whether the breaker is open vs. a real failure.
            context["superset"] = superset_ctx
        except Exception as e:
            context["superset_error"] = str(e)

        elapsed = (datetime.now() - start).total_seconds() * 1000
        context["query_time_ms"] = elapsed
        context["timestamp"] = datetime.now().isoformat()

        return context

    # =========================================================================
    # DIALOGUE MIRROR (The Butler's Eyes and Ears)
    # =========================================================================

    def get_dialogue_context(self, max_messages: int = 50, hours_back: int = 24) -> Dict[str, Any]:
        """
        Get recent dialogue context from the dialogue mirror.

        This is my butler's memory of recent conversations - essential for
        understanding what worked, what didn't, and avoiding repeated mistakes.
        """
        try:
            from memory.dialogue_mirror import get_dialogue_mirror

            mirror = get_dialogue_mirror()
            context = mirror.get_context_for_synaptic(
                max_messages=max_messages,
                max_age_hours=hours_back
            )

            return {
                "message_count": len(context.get("dialogue_context", [])),
                "sources": context.get("sources", []),
                "time_range": context.get("time_range", {}),
                "recent_topics": self._extract_topics(context.get("dialogue_context", [])),
            }
        except Exception as e:
            logger.error(f"Failed to get dialogue context: {e}")
            return {"error": str(e)}

    def _extract_topics(self, messages: List[Dict], max_topics: int = 5) -> List[str]:
        """Extract recent conversation topics from messages."""
        topics = []
        for msg in messages:
            if msg.get("role") != "aaron":
                continue
            content = msg.get("content", "")
            if len(content) < 10:
                continue
            # First sentence or first 80 chars
            first = content.split(".")[0].strip()[:80]
            if first and first not in topics:
                topics.append(first)
                if len(topics) >= max_topics:
                    break
        return topics

    def get_failure_patterns(self, hours_back: int = 48) -> List[Dict]:
        """
        Get detected failure patterns from the dialogue mirror.

        These are the landmines - things that failed recently so we
        don't repeat the same mistakes.
        """
        try:
            import sqlite3
            from memory.db_utils import get_unified_db_path, unified_table
            db_path = get_unified_db_path(Path(__file__).parent / ".failure_patterns.db")
            if not db_path.exists():
                return []

            t_fp = unified_table(".failure_patterns.db", "failure_patterns")
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute(f"""
                SELECT pattern_id, domain, description, occurrence_count,
                       confidence, landmine_text, last_seen
                FROM {t_fp}
                ORDER BY last_seen DESC
                LIMIT 10
            """)
            rows = cur.fetchall()
            conn.close()

            return [
                {
                    "id": row[0],
                    "domain": row[1],
                    "description": row[2],
                    "occurrences": row[3],
                    "confidence": row[4],
                    "landmine": row[5],
                    "last_seen": row[6],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get failure patterns: {e}")
            return []

    def trigger_dialogue_analysis(self, session_id: str = None) -> Dict[str, Any]:
        """
        Trigger background dialogue analysis via Celery.

        This spawns a parallel task to analyze recent conversations
        for patterns, failures, and learnable insights.
        """
        try:
            from memory.celery_tasks import analyze_dialogue_patterns
            result = analyze_dialogue_patterns.delay(session_id=session_id)
            return {
                "status": "triggered",
                "task_id": result.id,
                "message": "Dialogue analysis running in background"
            }
        except Exception as e:
            logger.error(f"Failed to trigger dialogue analysis: {e}")
            return {"status": "error", "error": str(e)}

    def analyze_for_loops(self, query: str) -> Dict[str, Any]:
        """
        Check if this query looks like a repeated mistake loop.

        Returns warning if we've seen similar queries fail before.
        """
        try:
            failures = self.get_failure_patterns()
            query_lower = query.lower()

            matches = []
            for f in failures:
                # Check if any keywords from the failure match the query
                desc_lower = (f.get("description", "") or "").lower()
                if any(word in query_lower for word in desc_lower.split()[:5] if len(word) > 3):
                    matches.append({
                        "pattern": f["description"][:80],
                        "occurrences": f["occurrences"],
                        "landmine": f.get("landmine", ""),
                    })

            if matches:
                return {
                    "is_loop": True,
                    "matches": matches,
                    "warning": f"⚠️ Similar task failed {matches[0]['occurrences']}x recently"
                }
            return {"is_loop": False}
        except Exception as e:
            return {"is_loop": False, "error": str(e)}

    # =========================================================================
    # LEARNING STORAGE
    # =========================================================================

    def push_learning(
        self,
        title: str,
        content: str,
        learning_type: str = "gotcha",
        tags: List[str] = None,
        source: str = "synaptic_chat"
    ) -> Optional[str]:
        """
        Store a learning to PostgreSQL and update Redis cache.

        Args:
            title: Short title for the learning
            content: Full learning content
            learning_type: Type (gotcha, pattern, success, etc.)
            tags: List of tags for search
            source: Where the learning came from

        Returns:
            Learning ID if successful
        """
        try:
            from memory.postgres_storage import store_learning
            learning_id = store_learning(
                title=title,
                content=content,
                learning_type=learning_type,
                tags=tags or [],
                source=source
            )

            # Update Redis cache
            try:
                from memory.redis_cache import get_redis_client, publish_event
                client = get_redis_client()
                if client:
                    # Publish event for real-time UI update
                    publish_event("learning_added", {
                        "id": learning_id,
                        "title": title,
                        "type": learning_type
                    })
            except Exception as e:
                # Cache/pub-sub is optional — but record so the swallow is
                # observable per ZSF (CLAUDE.md "ZERO SILENT FAILURES").
                logger.debug("learning_added pub/sub skipped: %s", e)

            return learning_id
        except Exception as e:
            logger.error(f"Failed to store learning: {e}")
            return None

    # =========================================================================
    # REAL-TIME EVENTS
    # =========================================================================

    def subscribe_events(self, callback) -> None:
        """
        Subscribe to real-time events via Redis pub/sub.

        Events include:
        - learning_added
        - injection_complete
        - service_status_change
        - health_alert
        """
        try:
            from memory.redis_cache import subscribe_events
            subscribe_events(callback)
        except Exception as e:
            logger.error(f"Failed to subscribe to events: {e}")

    # =========================================================================
    # STATUS SUMMARY
    # =========================================================================

    def get_summary(self) -> Dict[str, Any]:
        """
        Get a summary suitable for injection into Synaptic's context.

        This is the "butler's briefing" - everything Synaptic needs
        to know about the current state of the system.
        """
        status = self.get_all_service_status()

        online = [name for name, info in status.items()
                  if info.status == ServiceStatus.HEALTHY]
        offline = [name for name, info in status.items()
                   if info.status == ServiceStatus.OFFLINE]
        degraded = [name for name, info in status.items()
                    if info.status == ServiceStatus.DEGRADED]

        # Calculate signal strength
        total = len(status)
        healthy = len(online)
        signal = "🟢 Clear" if healthy >= 7 else "🟡 Present" if healthy >= 5 else "🔴 Quiet"

        return {
            "signal_strength": signal,
            "online_count": healthy,
            "total_services": total,
            "online": online,
            "offline": offline,
            "degraded": degraded,
            "services": {name: asdict(info) for name, info in status.items()},
            "timestamp": datetime.now().isoformat()
        }


# =============================================================================
# SINGLETON
# =============================================================================

_hub_instance = None

def get_hub() -> SynapticServiceHub:
    """Get the singleton SynapticServiceHub instance."""
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = SynapticServiceHub()
    return _hub_instance


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    hub = get_hub()

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print("=" * 60)
        print("SYNAPTIC SERVICE HUB - Service Status")
        print("=" * 60)

        summary = hub.get_summary()
        print(f"\nSignal Strength: {summary['signal_strength']}")
        print(f"Online: {summary['online_count']}/{summary['total_services']}")

        print("\nServices:")
        for name, info in summary['services'].items():
            status_icon = "✅" if info['status'] == 'healthy' else "❌" if info['status'] == 'offline' else "⚠️"
            port = f":{info['port']}" if info['port'] else ""
            print(f"  {status_icon} {name}{port} - {info['message']}")

    elif len(sys.argv) > 2 and sys.argv[1] == "context":
        query = " ".join(sys.argv[2:])
        print(f"Getting context for: {query}")
        context = hub.get_rich_context(query)
        print(json.dumps(context, indent=2, default=str))

    else:
        print("Usage:")
        print("  python synaptic_service_hub.py status")
        print("  python synaptic_service_hub.py context <query>")
