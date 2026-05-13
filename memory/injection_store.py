#!/usr/bin/env python3
"""
Injection Store - Persists context injections for visualization

This module stores the latest context injections so they can be
retrieved and visualized by the frontend dashboard.

Storage Strategy (in order of priority):
1. PostgreSQL: Store to cd_injections table (full persistence, events pipeline)
2. Redis: Publish to channel for real-time WebSocket updates
3. File-based: Always write to .injection_latest.json (fallback, always reliable)

All three strategies run in parallel - file is ALWAYS written to ensure
the system works even when containers are down.

Usage:
    from memory.injection_store import InjectionStore, get_injection_store

    store = get_injection_store()
    store.store_injection(injection_data)
    latest = store.get_latest()
"""

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Base paths
MEMORY_DIR = Path(__file__).parent
INJECTION_FILE = MEMORY_DIR / ".injection_latest.json"
INJECTION_HISTORY_FILE = MEMORY_DIR / ".injection_history.json"
ACTIVE_SESSIONS_FILE = MEMORY_DIR / ".active_session_injections.json"  # Session→injection mapping for A/B outcome tracking
MAX_HISTORY = 50
MAX_SESSIONS = 100  # Keep recent sessions for outcome attribution

# Optional: Unified storage for PostgreSQL integration
try:
    from memory.unified_storage import get_storage, record_event
    HAS_UNIFIED_STORAGE = True
except ImportError:
    HAS_UNIFIED_STORAGE = False
    get_storage = None
    record_event = None


class InjectionStore:
    """
    Store and retrieve context injections for visualization.

    Stores:
    - Latest injection (for immediate display)
    - Injection history (for timeline view)
    """

    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._ensure_files()

    def _ensure_files(self):
        """Create storage files if they don't exist."""
        if not INJECTION_FILE.exists():
            self._write_json(INJECTION_FILE, None)
        if not INJECTION_HISTORY_FILE.exists():
            self._write_json(INJECTION_HISTORY_FILE, [])

    def _read_json(self, path: Path) -> Any:
        """Read JSON from file."""
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return None

    def _write_json(self, path: Path, data: Any):
        """Write JSON to file atomically with unique temp file (race-safe)."""
        import tempfile
        dir_path = path.parent
        try:
            fd, tmp_name = tempfile.mkstemp(
                suffix='.tmp', prefix=path.stem + '_', dir=str(dir_path)
            )
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            Path(tmp_name).replace(path)
        except OSError:
            # Fallback: direct write if atomic fails
            with open(path, 'w') as f:
                json.dump(data, f, indent=2, default=str)

    def store_injection(self, injection_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Store a new context injection.

        Storage strategy (all run in parallel for reliability):
        1. PostgreSQL: cd_injections table + event recording
        2. Redis: pub/sub for real-time dashboard updates
        3. File: .injection_latest.json (always, for fallback)

        Args:
            injection_data: The full injection data including:
                - timestamp: ISO timestamp
                - trigger: {hook, prompt, session_id}
                - analysis: {detected_domains, risk_level, ...}
                - silver_platter: {safety, wisdom, sops, protocol}
                - raw_output: The formatted text

        Returns:
            The stored injection with added id
        """
        # Add ID and ensure timestamp
        injection_data['id'] = f"inj_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        if 'timestamp' not in injection_data:
            injection_data['timestamp'] = datetime.now().isoformat()

        # Compute payload_sha256 for determinism verification (epistemic foundation)
        raw_output = injection_data.get('raw_output', '')
        if raw_output and 'payload_sha256' not in injection_data:
            injection_data['payload_sha256'] = hashlib.sha256(raw_output.encode()).hexdigest()

        # Extract common fields for PostgreSQL
        session_id = injection_data.get('trigger', {}).get('session_id')
        ide = injection_data.get('trigger', {}).get('ide', 'claude_code')
        injection_type = 'silver_platter'
        relevance_score = injection_data.get('analysis', {}).get('first_try_likelihood', 0.0)

        # === STRATEGY 1: PostgreSQL (if available) ===
        self._store_to_postgres(injection_data, session_id, ide, injection_type, relevance_score)

        # === Track session → injection mapping for A/B outcome attribution ===
        ab_variant = injection_data.get('analysis', {}).get('ab_variant', 'control')
        if session_id:
            self.track_session_injection(session_id, injection_data['id'], ab_variant)

        # === STRATEGY 2: File-based (always - fallback guarantee) ===
        self._write_json(INJECTION_FILE, injection_data)

        # Add full injection data to history (for dashboard navigation)
        history = self._read_json(INJECTION_HISTORY_FILE) or []
        history.insert(0, injection_data)  # Store full data for dashboard
        history = history[:MAX_HISTORY]  # Keep only recent
        self._write_json(INJECTION_HISTORY_FILE, history)

        # === TELEMETRY BRIDGE: Persist to observability store (best-effort) ===
        # Feeds 4 rollup systems: claim_outcome, section_outcome, experiment, A/B
        try:
            from memory.observability_store import get_observability_store
            prompt_text = injection_data.get('trigger', {}).get('prompt', '')
            prompt_sha = hashlib.sha256(prompt_text.encode()).hexdigest() if prompt_text else ''
            get_observability_store().record_injection_event(
                injection_id=injection_data['id'],
                payload_sha256=injection_data.get('payload_sha256', ''),
                total_latency_ms=injection_data.get('analysis', {}).get('generation_time_ms', 0),
                total_tokens=len(raw_output.split()) if raw_output else 0,
                session_id=session_id or '',
                task_type=injection_type,
                entrypoint='injection_store',
                user_prompt_sha256=prompt_sha,
                experiment_id=ab_variant if ab_variant != 'control' else None,
                variant_id=ab_variant,
            )
        except Exception as e:
            import logging
            logging.getLogger("context_dna").debug(f"Injection store telemetry bridge failed (best-effort): {e}")

        # === PER-LEARNING OUTCOME ATTRIBUTION ===
        # Record which FTS5 learnings were included in this injection
        # so their individual effectiveness can be measured
        learning_ids = injection_data.get('learning_ids', [])
        if learning_ids:
            try:
                from memory.observability_store import get_observability_store
                obs = get_observability_store()
                count = obs.record_learning_attribution(
                    injection_id=injection_data['id'],
                    learning_ids=learning_ids,
                    section_id="section_1",
                )
                if count:
                    import logging as _log
                    _log.getLogger("context_dna").debug(
                        f"Learning attribution: {count} learnings linked to injection {injection_data['id'][:12]}"
                    )
            except Exception:
                pass  # Non-blocking

        # === STRATEGY 3: Redis pub/sub (if available) ===
        if self.redis:
            try:
                import asyncio
                asyncio.create_task(self._publish_redis(injection_data))
            except Exception as e:
                print(f"[WARN] Redis pub/sub injection publish failed: {e}")

        return injection_data

    def _store_to_postgres(
        self,
        injection_data: Dict[str, Any],
        session_id: Optional[str],
        ide: str,
        injection_type: str,
        relevance_score: float
    ):
        """Store injection to PostgreSQL via unified storage."""
        if not HAS_UNIFIED_STORAGE:
            return

        try:
            storage = get_storage()
            pg_conn = storage._get_pg_conn()
            if not pg_conn:
                return

            cursor = pg_conn.cursor()

            # Insert into cd_injections table
            cursor.execute("""
                INSERT INTO cd_injections (
                    id, session_id, ide, injection_type, content,
                    relevance_score, generation_time_ms, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                str(uuid.uuid4()),
                session_id if session_id else None,
                ide,
                injection_type,
                json.dumps(injection_data),
                relevance_score,
                injection_data.get('_generation_time_ms'),
                injection_data.get('timestamp', datetime.now().isoformat())
            ))

            # Record event for Store→Observe→Learn pipeline
            record_event(
                'injection_created',
                {
                    'injection_id': injection_data['id'],
                    'risk_level': injection_data.get('analysis', {}).get('risk_level'),
                    'domains': injection_data.get('analysis', {}).get('detected_domains', []),
                    'ide': ide
                },
                source='webhook'
            )

        except Exception as e:
            # Non-blocking - file fallback ensures data is never lost
            pass

    async def _publish_redis(self, injection_data: Dict[str, Any]):
        """Publish injection to Redis channel."""
        try:
            await self.redis.publish(
                "context-dna:injection",
                json.dumps({
                    "event": "injection_complete",
                    "data": injection_data
                }, default=str)
            )
        except Exception as e:
            print(f"[WARN] Redis injection publish failed: {e}")

    def get_latest(self) -> Optional[Dict[str, Any]]:
        """Get the most recent injection."""
        return self._read_json(INJECTION_FILE)

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent injection summaries."""
        history = self._read_json(INJECTION_HISTORY_FILE) or []
        return history[:limit]

    def get_by_id(self, injection_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific injection by ID."""
        latest = self.get_latest()
        if latest and latest.get('id') == injection_id:
            return latest
        # Search history file
        history = self._read_json(INJECTION_HISTORY_FILE) or []
        for item in history:
            if item.get('id') == injection_id:
                return item
        return None

    # =========================================================================
    # SESSION → INJECTION MAPPING (For A/B Outcome Attribution)
    # =========================================================================

    def track_session_injection(self, session_id: str, injection_id: str, ab_variant: str):
        """
        Track which injection/variant is active for a session.

        This closes the A/B learning loop: injection → outcome → variant performance.
        When capture_success() is called, we look up which variant was active.

        Args:
            session_id: Claude session identifier
            injection_id: The injection ID that was generated
            ab_variant: The A/B variant used (control, a, b, c)
        """
        if not session_id:
            return

        sessions = self._read_json(ACTIVE_SESSIONS_FILE) or {}
        sessions[session_id] = {
            "injection_id": injection_id,
            "ab_variant": ab_variant or "control",
            "timestamp": datetime.now().isoformat()
        }

        # Keep only recent sessions (prevent unbounded growth)
        if len(sessions) > MAX_SESSIONS:
            # Sort by timestamp, keep newest
            sorted_sessions = sorted(
                sessions.items(),
                key=lambda x: x[1].get("timestamp", ""),
                reverse=True
            )[:MAX_SESSIONS]
            sessions = dict(sorted_sessions)

        self._write_json(ACTIVE_SESSIONS_FILE, sessions)

    def get_session_injection(self, session_id: str) -> Optional[Dict]:
        """
        Get the active injection info for a session.

        Used by capture_success() to attribute outcomes to A/B variants.

        Args:
            session_id: Claude session identifier

        Returns:
            Dict with injection_id, ab_variant, timestamp or None
        """
        if not session_id:
            return None
        sessions = self._read_json(ACTIVE_SESSIONS_FILE) or {}
        return sessions.get(session_id)

    # =========================================================================
    # BOUNDARY CLARIFICATION SUPPORT
    # =========================================================================

    def get_pending_clarifications(self, session_id: str = None) -> List[Dict[str, Any]]:
        """
        Get injections that need clarification (boundary detection uncertain).

        Args:
            session_id: Optional filter by session

        Returns:
            List of injections needing clarification
        """
        history = self._read_json(INJECTION_HISTORY_FILE) or []
        pending = []

        for item in history:
            boundary = item.get('boundary', {})
            if boundary.get('needs_clarification') and not boundary.get('clarification_response'):
                if session_id:
                    item_session = item.get('trigger', {}).get('session_id')
                    if item_session != session_id:
                        continue
                pending.append({
                    'id': item.get('id'),
                    'prompt': item.get('trigger', {}).get('prompt'),
                    'timestamp': item.get('timestamp'),
                    'clarification_prompt': boundary.get('clarification_prompt'),
                    'clarification_options': boundary.get('clarification_options'),
                    'boundary_confidence': boundary.get('confidence'),
                })

        return pending

    def record_clarification_response(
        self,
        injection_id: str,
        selected_project: str,
        session_id: str = None
    ) -> bool:
        """
        Record user's response to a clarification prompt.

        This is a strong learning signal - the user explicitly told us
        which project the work is about.

        Args:
            injection_id: The injection that needed clarification
            selected_project: User's selected project
            session_id: Session ID for the response

        Returns:
            True if recorded successfully
        """
        # Update in file storage
        history = self._read_json(INJECTION_HISTORY_FILE) or []
        updated = False

        for item in history:
            if item.get('id') == injection_id:
                if 'boundary' not in item:
                    item['boundary'] = {}
                item['boundary']['clarification_response'] = selected_project
                item['boundary']['clarification_timestamp'] = datetime.now().isoformat()
                updated = True
                break

        if updated:
            self._write_json(INJECTION_HISTORY_FILE, history)

            # Also update latest if it matches
            latest = self._read_json(INJECTION_FILE)
            if latest and latest.get('id') == injection_id:
                if 'boundary' not in latest:
                    latest['boundary'] = {}
                latest['boundary']['clarification_response'] = selected_project
                latest['boundary']['clarification_timestamp'] = datetime.now().isoformat()
                self._write_json(INJECTION_FILE, latest)

            # Trigger learning via Celery task
            self._trigger_clarification_learning(injection_id, selected_project, session_id)

            # Record event if available
            if HAS_UNIFIED_STORAGE and record_event:
                try:
                    record_event(
                        'boundary_clarification_response',
                        {
                            'injection_id': injection_id,
                            'selected_project': selected_project,
                            'session_id': session_id
                        },
                        source='dashboard'
                    )
                except Exception as e:
                    print(f"[WARN] Boundary intelligence event recording failed: {e}")

        return updated

    def _trigger_clarification_learning(
        self,
        injection_id: str,
        selected_project: str,
        session_id: str = None
    ):
        """Trigger Celery task to learn from clarification response."""
        try:
            from memory.celery_tasks import record_clarification_response
            record_clarification_response.delay(
                injection_id=injection_id,
                selected_project=selected_project,
                session_id=session_id
            )
        except ImportError:
            # Celery not available, try direct recording
            try:
                from memory.boundary_intelligence import get_boundary_intelligence
                bi = get_boundary_intelligence()
                bi.record_clarification_response(injection_id, selected_project)
            except Exception as e:
                print(f"[WARN] Direct clarification recording failed: {e}")
        except Exception as e:
            print(f"[WARN] Celery clarification task failed: {e}")

    def get_clarification_stats(self, session_id: str = None) -> Dict[str, Any]:
        """
        Get statistics about clarification responses.

        Useful for monitoring boundary detection accuracy.
        """
        history = self._read_json(INJECTION_HISTORY_FILE) or []

        total_with_boundary = 0
        needs_clarification = 0
        clarified = 0
        project_corrections = {}

        for item in history:
            boundary = item.get('boundary', {})
            if boundary.get('project') or boundary.get('needs_clarification'):
                if session_id:
                    item_session = item.get('trigger', {}).get('session_id')
                    if item_session != session_id:
                        continue

                total_with_boundary += 1

                if boundary.get('needs_clarification'):
                    needs_clarification += 1

                    if boundary.get('clarification_response'):
                        clarified += 1
                        selected = boundary['clarification_response']
                        original = boundary.get('project')

                        if original and original != selected:
                            key = f"{original}→{selected}"
                            project_corrections[key] = project_corrections.get(key, 0) + 1

        return {
            'total_with_boundary': total_with_boundary,
            'needs_clarification': needs_clarification,
            'clarified': clarified,
            'pending': needs_clarification - clarified,
            'project_corrections': project_corrections,
            'clarification_rate': (clarified / needs_clarification * 100) if needs_clarification > 0 else 0,
        }


# Singleton instance
_injection_store: Optional[InjectionStore] = None


def get_injection_store(redis_client=None) -> InjectionStore:
    """Get the singleton injection store instance."""
    global _injection_store
    if _injection_store is None:
        _injection_store = InjectionStore(redis_client)
        try:
            from memory.evented_write import get_evented_write_service
            get_evented_write_service().gate(_injection_store, "injection_store")
        except Exception:
            pass  # Fail-open — store works without event logging
    elif redis_client and not _injection_store.redis:
        _injection_store.redis = redis_client
    return _injection_store


# =============================================================================
# INJECTION HEALTH MONITOR BRIDGE
# =============================================================================

def record_injection_to_health_monitor(
    injection_data: Dict[str, Any],
    destination: str = "vs_code_claude_code",
    phase: str = "pre_message"
):
    """
    Bridge function to record injections to the health monitor.

    Call this AFTER store_injection() to enable webhook health tracking.

    Args:
        injection_data: The injection data dict from build_injection_data()
        destination: Which webhook (vs_code_claude_code, synaptic_chat, etc.)
        phase: pre_message or post_message

    Example:
        injection_data = build_injection_data(...)
        store.store_injection(injection_data)
        record_injection_to_health_monitor(injection_data, "vs_code_claude_code", "pre_message")
    """
    try:
        from memory.injection_health_monitor import record_webhook_injection

        # Map section names to IDs
        section_name_to_id = {
            "safety": 0,
            "foundation": 1,
            "wisdom": 2,
            "awareness": 3,
            "deep_context": 4,
            "protocol": 5,
            "synaptic_to_atlas": 6,
            "holistic_context": 6,
            "full_library": 7,
            "synaptic_8th_intelligence": 8,
            "8th_intelligence": 8,
        }

        # Get sections from injection data
        analysis = injection_data.get("analysis", {})
        section_names = analysis.get("sections_included", [])
        sections_included = [
            section_name_to_id[s]
            for s in section_names
            if s in section_name_to_id
        ]

        # Deduplicate and sort
        sections_included = sorted(set(sections_included))

        record_webhook_injection(
            injection_id=injection_data.get("id", "unknown"),
            sections_included=sections_included,
            total_latency_ms=analysis.get("generation_time_ms", 0),
            total_tokens=len(injection_data.get("raw_output", "").split()),
            destination=destination,
            phase=phase,
            eighth_intelligence_present=8 in sections_included
        )

    except ImportError:
        pass  # Health monitor not available
    except Exception as e:
        # Non-blocking - don't fail injection if monitoring fails
        import logging
        logging.getLogger(__name__).debug(f"Health monitor recording failed: {e}")


def build_injection_data(
    prompt: str,
    injection_result: Any,
    session_id: str = "",
    hook_name: str = "UserPromptSubmit",
    additional_data: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Build the full injection data structure for storage.

    Args:
        prompt: The user's original prompt
        injection_result: The InjectionResult from persistent_hook_structure
        session_id: The session ID if available
        hook_name: Which hook triggered this
        additional_data: Any extra data to include (like raw wisdom, sops, etc.)

    Returns:
        Full injection data structure ready for storage
    """
    # Extract domains from prompt (simple keyword matching)
    domains = _detect_domains(prompt)

    # Build per-section timing map
    section_timings = {}
    if hasattr(injection_result, 'section_timings') and injection_result.section_timings:
        section_timings = injection_result.section_timings

    data = {
        "timestamp": datetime.now().isoformat(),
        "trigger": {
            "hook": hook_name,
            "prompt": prompt,
            "session_id": session_id or "",
        },
        "analysis": {
            "detected_domains": domains,
            "risk_level": injection_result.risk_level.value if hasattr(injection_result.risk_level, 'value') else str(injection_result.risk_level),
            "first_try_likelihood": injection_result.first_try_likelihood,
            "generation_time_ms": injection_result.generation_time_ms,
            "sections_included": injection_result.sections_included,
            "section_timings": section_timings,
            "ab_variant": injection_result.ab_variant or "control",
            "mode": injection_result.mode.value if hasattr(injection_result.mode, 'value') else str(injection_result.mode),
        },
        "silver_platter": _extract_silver_platter(injection_result, additional_data),
        "raw_output": injection_result.content,
        # Per-section breakdown for visualization panel
        "sections": _build_sections_array(
            injection_result.content,
            injection_result.sections_included,
            section_timings,
        ),
    }

    # Add boundary intelligence data
    data["boundary"] = {
        "project": getattr(injection_result, 'boundary_project', None),
        "confidence": getattr(injection_result, 'boundary_confidence', 0.0),
        "action": getattr(injection_result, 'boundary_action', None),
        "filter_note": getattr(injection_result, 'boundary_filter_note', None),
        "needs_clarification": getattr(injection_result, 'needs_clarification', False),
        "clarification_prompt": getattr(injection_result, 'clarification_prompt', None),
        "clarification_options": getattr(injection_result, 'clarification_options', None),
        "clarification_response": None,  # Filled when user responds
        "clarification_timestamp": None,
    }

    # Per-learning outcome attribution — track which learnings were injected
    data["learning_ids"] = getattr(injection_result, 'learning_ids', []) or []

    return data


def _build_sections_array(
    raw_output: str,
    sections_included: List[str],
    section_timings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build structured sections array from raw injection output for visualization.

    Parses the raw_output text into individual sections, mapping each to its
    section ID, timing, token count, and content snippet.
    """
    # Map section names to IDs and display labels
    SECTION_MAP = {
        "safety": {"id": 0, "label": "SAFETY"},
        "foundation": {"id": 1, "label": "FOUNDATION"},
        "wisdom": {"id": 2, "label": "WISDOM"},
        "awareness": {"id": 3, "label": "AWARENESS"},
        "deep_context": {"id": 4, "label": "DEEP CONTEXT"},
        "protocol": {"id": 5, "label": "PROTOCOL"},
        "synaptic_to_atlas": {"id": 6, "label": "HOLISTIC"},
        "holistic_context": {"id": 6, "label": "HOLISTIC"},
        "full_library": {"id": 7, "label": "FULL LIBRARY"},
        "synaptic_8th_intelligence": {"id": 8, "label": "8TH INTELLIGENCE"},
        "8th_intelligence": {"id": 8, "label": "8TH INTELLIGENCE"},
    }

    # Section delimiters in raw_output (order matters for parsing)
    SECTION_MARKERS = [
        (0, ["NEVER DO", "Safety Rails"]),
        (1, ["FOUNDATION"]),
        (2, ["WISDOM", "PROFESSOR"]),
        (3, ["AWARENESS"]),
        (4, ["DEEP CONTEXT"]),
        (5, ["PROTOCOL"]),
        (6, ["BUTLER DEEP QUERY", "HOLISTIC CONTEXT"]),
        (7, ["FULL LIBRARY"]),
        (8, ["8TH INTELLIGENCE", "Synaptic → Aaron"]),
    ]

    # Parse raw_output into section content blocks
    section_contents: Dict[int, str] = {}
    if raw_output:
        lines = raw_output.split('\n')
        current_section_id = None
        current_lines: List[str] = []

        for line in lines:
            line_upper = line.upper()
            # Check if this line starts a new section
            matched = False
            for sec_id, markers in SECTION_MARKERS:
                if any(m.upper() in line_upper for m in markers):
                    # Save previous section
                    if current_section_id is not None:
                        section_contents[current_section_id] = '\n'.join(current_lines).strip()
                    current_section_id = sec_id
                    current_lines = []
                    matched = True
                    break
            if not matched and current_section_id is not None:
                current_lines.append(line)

        # Save last section
        if current_section_id is not None:
            section_contents[current_section_id] = '\n'.join(current_lines).strip()

    # Build sections array
    sections = []
    seen_ids = set()

    for sec_name in sections_included:
        meta = SECTION_MAP.get(sec_name, {"id": -1, "label": sec_name.upper()})
        sec_id = meta["id"]

        # Avoid duplicate section IDs
        if sec_id in seen_ids:
            continue
        seen_ids.add(sec_id)

        content = section_contents.get(sec_id, "")
        # Truncate content for storage (full text is in raw_output)
        content_preview = content[:2000] if content else "(not included)"

        timing_ms = section_timings.get(sec_name, 0)
        tokens = len(content.split()) if content else 0

        # Determine freshness based on timing
        if timing_ms == 0:
            freshness = "cached"
        elif timing_ms < 100:
            freshness = "cached"
        elif timing_ms < 500:
            freshness = "realtime"
        else:
            freshness = "realtime"  # LLM-generated sections are always "realtime"

        # Section 7 (FULL LIBRARY) is stale unless explicitly included with content
        if sec_id == 7 and tokens == 0:
            freshness = "stale"

        sections.append({
            "id": sec_id,
            "name": meta["label"],
            "tokens": tokens,
            "timingMs": timing_ms,
            "freshness": freshness,
            "content": content_preview,
        })

    # Sort by section ID
    sections.sort(key=lambda s: s["id"])
    return sections


def _detect_domains(prompt: str) -> List[str]:
    """Detect relevant domains using PROFESSOR's keyword mapping.

    UNIFIED DOMAIN DETECTION: This function now imports DOMAIN_KEYWORDS from
    professor.py to ensure consistent domain detection across the entire system.

    This fixes the 92% "general" fallback issue - professor.py has 12 domains
    with 50+ keywords covering: async_python, docker_ecs, webrtc_livekit,
    aws_infrastructure, voice_pipeline, django_backend, memory_system,
    frontend_react, git_version_control, database, testing, build_deploy.
    """
    prompt_lower = prompt.lower()
    domains = []

    # Import RICH domain keywords from professor.py (THE SOURCE OF TRUTH)
    # Professor has comprehensive 50+ keyword coverage across 12 domains
    try:
        from memory.professor import DOMAIN_KEYWORDS
    except ImportError:
        # Minimal fallback - professor.py should always be available
        DOMAIN_KEYWORDS = {
            "memory_system": [
                "memory", "acontext", "context-dna", "contextdna", "brain", "sop",
                "learning", "professor", "injection", "synaptic", "8th"
            ],
        }

    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            domains.append(domain)

    # Default to memory_system (not "general") for better default wisdom
    return domains if domains else ["memory_system"]


def _extract_silver_platter(injection_result: Any, additional_data: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Extract structured silver platter components from injection result.

    Parses the formatted content to extract individual sections.
    """
    content = injection_result.content
    additional = additional_data or {}

    # Parse sections from formatted content
    platter = {
        "safety": _extract_section(content, "NEVER DO", "Safety Rails"),
        "wisdom": _extract_wisdom(content, additional),
        "sops": additional.get("sops", []),
        "protocol": {
            "risk_level": injection_result.risk_level.value if hasattr(injection_result.risk_level, 'value') else str(injection_result.risk_level),
            "first_try_percent": _parse_first_try_percent(injection_result.first_try_likelihood),
            "recommendation": _get_risk_recommendation(injection_result.risk_level),
        }
    }

    return platter


def _extract_section(content: str, *markers: str) -> Dict[str, Any]:
    """Extract a section from content by looking for markers."""
    for marker in markers:
        if marker.lower() in content.lower():
            # Find the section
            lines = []
            in_section = False
            for line in content.split('\n'):
                if marker.lower() in line.lower():
                    in_section = True
                    continue
                if in_section:
                    if line.strip().startswith(('─', '═', '╔', '╚', '╠')):
                        break
                    if line.strip():
                        lines.append(line.strip())
            return {"found": True, "content": lines}
    return {"found": False, "content": []}


def _extract_wisdom(content: str, additional: Dict[str, Any]) -> Dict[str, Any]:
    """Extract wisdom components."""
    wisdom = {
        "the_one_thing": "",
        "landmines": [],
        "patterns": [],
        "context": "",
    }

    # Look for THE ONE THING
    if "ONE THING" in content.upper():
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if "ONE THING" in line.upper():
                # Get the next non-empty line
                for j in range(i+1, min(i+5, len(lines))):
                    if lines[j].strip() and not lines[j].strip().startswith(('─', '═')):
                        wisdom["the_one_thing"] = lines[j].strip()
                        break

    # Look for LANDMINES
    if "LANDMINE" in content.upper():
        lines = content.split('\n')
        in_landmines = False
        for line in lines:
            if "LANDMINE" in line.upper():
                in_landmines = True
                continue
            if in_landmines:
                if line.strip().startswith(('─', '═', '╔')):
                    break
                if line.strip().startswith(('•', '-', '💣')):
                    text = line.strip().lstrip('•-💣 ')
                    wisdom["landmines"].append({"icon": "💣", "text": text})

    # Add from additional data if provided
    if additional.get("professor_wisdom"):
        pw = additional["professor_wisdom"]
        if not wisdom["the_one_thing"] and pw.get("the_one_thing"):
            wisdom["the_one_thing"] = pw["the_one_thing"]
        if not wisdom["landmines"] and pw.get("landmines"):
            wisdom["landmines"] = [{"icon": "💣", "text": l} for l in pw["landmines"]]
        if not wisdom["patterns"] and pw.get("patterns"):
            wisdom["patterns"] = [{"text": p} for p in pw["patterns"]]
        if not wisdom["context"] and pw.get("context"):
            wisdom["context"] = pw["context"]

    return wisdom


def _parse_first_try_percent(likelihood: str) -> int:
    """Parse first-try likelihood to percentage."""
    if isinstance(likelihood, (int, float)):
        return int(likelihood)
    if isinstance(likelihood, str):
        # Extract number from string like "60%" or "60"
        import re
        match = re.search(r'(\d+)', likelihood)
        if match:
            return int(match.group(1))
    return 50  # Default


def _get_risk_recommendation(risk_level) -> str:
    """Get recommendation based on risk level."""
    level = risk_level.value if hasattr(risk_level, 'value') else str(risk_level)
    recommendations = {
        "critical": "Full SOP review required | Multiple verification steps",
        "high": "Read SOPs carefully | Verify each step",
        "moderate": "Query memory if unsure | Record wins on success",
        "low": "Standard workflow | Capture learnings",
    }
    return recommendations.get(level.lower(), "Follow standard protocol")
