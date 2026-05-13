"""
 Complexity Vector Sentinel — Continuous Ecosystem Drift Detection

Background watcher (300s cycle via lite_scheduler) that reads session historian
gold + MMOTW repair SOPs, classifies drift signals against 20 complexity vectors,
and injects warnings into S0 via critical_findings pathway.

The Neurologist's protective parent — $0 cost, single classify call per cycle.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cv_sentinel")

# ── Paths ──
VECTORS_DB = Path.home() / ".context-dna" / "complexity_vectors.db"
ARCHIVE_DB = Path.home() / ".context-dna" / "session_archive.db"
REPAIR_SOPS_DB = Path(__file__).parent / ".repair_sops.db"

# ── Redis keys ──
REDIS_CRITICAL_KEY = "contextdna:critical:recent"
REDIS_CRITICAL_TTL = 86400
REDIS_RISK_KEY = "contextdna:cv_sentinel:risk_level"
REDIS_LAST_RUN_KEY = "contextdna:cv_sentinel:last_run"
PASS_ID = "cv_sentinel"
DEDUP_HOURS = 4
MAX_TRIGGERS = 6  # Noise gate: if model fires more than this, it's not discriminating

# ── Vector seed data (from docs/complexity-vectors.md) ──
VECTOR_SEED = [
    ("V1", "Tool vs Project Paradox", "structural",
     ["persistent_hook_structure", "self-referential", "webhook freeze", "tool maintaining itself"],
     9.0, 100),
    ("V2", "Lite vs Heavy Mode", "operational",
     ["lite mode", "heavy mode", "postgres", "sqlite fallback", "mode transition"],
     7.0, 56),
    ("V3", "Bidirectional Sync", "structural",
     ["sync conflict", "source of truth", "bidirectional", "merge conflict"],
     6.0, 40),
    ("V4", "Shallow vs Deep Memory", "operational",
     ["MEMORY.md", "evidence store", "contradicts", "stale memory", "trust hierarchy"],
     6.0, 35),
    ("V5", "Three LLMs / GPU Contention", "resource",
     ["GPU", "stampede", "starvation", "priority queue", "Metal", "gpu_lock", "concurrent"],
     10.0, 72),
    ("V6", "IDE Platform Fragmentation", "operational",
     ["cursor", "vscode", "IDE", "hooks", "cursorrules", "extension"],
     6.0, 30),
    ("V7", "Project Border Bleed", "structural",
     ["sub-project", "focus mode", "namespace", "context bleed", "wrong project"],
     5.0, 25),
    ("V8", "Atlas Context Window", "operational",
     ["context overflow", "compaction", "cannot see", "partial information", "truncated"],
     7.0, 45),
    ("V9", "Redundant Agents / Dead Code", "structural",
     ["watchdog", "dead code", "anatomical agents", "dual restore", "unused"],
     7.0, 56),
    ("V10", "Container Name Variants", "identity",
     ["context-dna", "contextdna", "acontext", "docker name", "container variant"],
     5.0, 20),
    ("V11", "API Domain Sprawl", "operational",
     ["api.ersimulator", "admin.contextdna", "wrong domain", "auth token", "domain mismatch"],
     6.0, 22),
    ("V12", "Action Fragmentation", "structural",
     ["direct HTTP", "port 5044", "bypass queue", "multiple paths", "direct call"],
     7.0, 38),
    ("V13", "Identity Alias Confusion", "identity",
     ["Synaptic butler", "neurologist", "Atlas navigator", "Cardiologist", "role confusion"],
     6.0, 30),
    ("V14", "Message Broker Complexity", "operational",
     ["Redis pub/sub", "broker", "message routing", "channel", "duplicate message"],
     5.0, 25),
    ("V15", "Scheduler/Runner/Daemon Proliferation", "resource",
     ["launchd", "nohup", "daemon", "scheduler proliferation", "cron overlap"],
     7.0, 40),
    ("ES", "Error Swallowing", "structural",
     ["bare except", "except:", "except Exception: pass", "silent fail", "swallow error"],
     9.5, 85),
    ("TSD", "Temporal State Drift", "operational",
     ["stale cache", "anticipation expired", "pre-compute", "TTL", "cache invalidation"],
     7.5, 60),
    ("FLC", "Feedback Loop Contamination", "structural",
     ["false positive", "amplify error", "cold-start", "promoted wrong", "evidence contamination"],
     7.5, 60),
    ("PVS", "Python Version Skew", "operational",
     ["python3 wrong", "python 3.9", "python 3.14", "xcode python", "version mismatch"],
     7.5, 55),
    ("SCC", "SQLite Connection Chaos", "resource",
     ["raw sqlite3.connect", "WAL missing", "220 call sites", "FD leak", "db_utils"],
     7.0, 55),
    ("DTP", "Duplicate Tool Paths", "structural",
     ["duplicate mcp", "plugin double", "tool registration", "70 tools", "mcp.json plugin conflict"],
     6.0, 30),
    ("PMD", "Plugin Migration Drift", "operational",
     ["subprocess fallback", "surgery_bridge", "legacy path", "direct import", "migration incomplete"],
     6.5, 35),
]

VALID_IDS = {v[0] for v in VECTOR_SEED}


class ComplexityVectorSentinel:
    """Continuous ecosystem drift detector — the Neurologist's protective parent."""

    def __init__(self):
        self._vectors_loaded = False
        self._redis = None

    def _get_redis(self):
        if self._redis is not None:
            try:
                self._redis.ping()
                return self._redis
            except Exception:
                self._redis = None
        try:
            import redis
            self._redis = redis.Redis(
                host="127.0.0.1", port=6379,
                decode_responses=True, socket_timeout=2
            )
            self._redis.ping()
            return self._redis
        except Exception:
            return None

    def _ensure_vectors_db(self):
        """Create vectors DB and seed if empty."""
        if self._vectors_loaded:
            return
        try:
            from memory.db_utils import connect_wal
            VECTORS_DB.parent.mkdir(parents=True, exist_ok=True)
            conn = connect_wal(VECTORS_DB)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS complexity_vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vector_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    signal_keywords TEXT NOT NULL,
                    risk_score REAL DEFAULT 5.0,
                    drift_ranking_score REAL DEFAULT 0.0,
                    current_alert_level TEXT DEFAULT 'none',
                    last_triggered_at TEXT,
                    trigger_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            count = conn.execute("SELECT COUNT(*) FROM complexity_vectors").fetchone()[0]
            if count == 0:
                now = datetime.utcnow().isoformat()
                for vid, name, cat, kw, risk, drift in VECTOR_SEED:
                    conn.execute("""
                        INSERT INTO complexity_vectors
                        (vector_id, name, category, signal_keywords, risk_score,
                         drift_ranking_score, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (vid, name, cat, json.dumps(kw), risk, drift, now, now))
                logger.info(f"Seeded {len(VECTOR_SEED)} complexity vectors")
            conn.commit()
            conn.close()
            self._vectors_loaded = True
        except Exception as e:
            logger.error(f"Vectors DB init error: {e}")

    def _read_recent_gold(self, limit: int = 5) -> str:
        """Read recent session gold text from archive DB."""
        try:
            from memory.db_utils import connect_wal
            if not ARCHIVE_DB.exists():
                return ""
            conn = connect_wal(ARCHIVE_DB)
            rows = conn.execute("""
                SELECT gold_text FROM archived_sessions
                WHERE extracted_at > datetime('now', '-2 hours')
                ORDER BY extracted_at DESC LIMIT ?
            """, (limit,)).fetchall()
            conn.close()
            if not rows:
                return ""
            combined = "\n".join(r[0] for r in rows if r[0])
            return combined[:3000]
        except Exception as e:
            logger.warning(f"Read gold error: {e}")
            return ""

    def _read_recent_sops(self, limit: int = 10) -> str:
        """Read recent MMOTW repair SOPs."""
        try:
            from memory.db_utils import connect_wal
            if not REPAIR_SOPS_DB.exists():
                return ""
            conn = connect_wal(REPAIR_SOPS_DB)
            rows = conn.execute("""
                SELECT component, symptom, root_cause FROM repair_sops
                WHERE updated_at > datetime('now', '-24 hours')
                ORDER BY confidence DESC LIMIT ?
            """, (limit,)).fetchall()
            conn.close()
            if not rows:
                return ""
            parts = []
            for r in rows:
                parts.append(f"{r[0]}: {r[1]} → {r[2]}")
            return "\n".join(parts)[:1000]
        except Exception as e:
            logger.warning(f"Read SOPs error: {e}")
            return ""

    def _load_vectors(self) -> list:
        """Load all vectors from DB."""
        try:
            from memory.db_utils import connect_wal
            conn = connect_wal(VECTORS_DB)
            rows = conn.execute(
                "SELECT vector_id, name, signal_keywords, risk_score, drift_ranking_score "
                "FROM complexity_vectors"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Load vectors error: {e}")
            return []

    def _build_classify_prompt(self, gold: str, sops: str, vectors: list) -> tuple:
        """Build system + user prompts for single classify call."""
        system = (
            "Complexity drift detector. ONLY flag vectors with ACTIVE risk — "
            "problems happening NOW, not historical mentions. "
            "Be SELECTIVE: max 3-5 vectors. If text just describes system operations "
            "without problems: NONE. "
            "Return comma-separated IDs from: "
            "V1 V2 V3 V4 V5 V6 V7 V8 V9 V10 V11 V12 V13 V14 V15 ES TSD FLC PVS SCC. "
            "No explanation."
        )
        # Build compact signal reference
        signals = []
        for v in vectors:
            kw = json.loads(v["signal_keywords"]) if isinstance(v["signal_keywords"], str) else v["signal_keywords"]
            signals.append(f"{v['vector_id']}={','.join(kw[:3])}")
        signal_ref = " | ".join(signals)

        user = f"SIGNALS:\n{signal_ref}\n\n"
        if gold:
            user += f"GOLD (recent session activity):\n{gold[:800]}\n\n"
        if sops:
            user += f"SOPS (recent repairs):\n{sops[:300]}\n\n"
        user += "Which vectors show ACTIVE risk right now? (max 5, or NONE)"

        return system, user

    def _parse_response(self, response: str) -> list:
        """Extract vector IDs from classify response.
        Noise gate: if >MAX_TRIGGERS fired, model isn't discriminating — return empty.
        """
        if not response:
            return []
        cleaned = response.strip().upper()
        if "NONE" in cleaned and len(cleaned) < 10:
            return []
        # Extract valid IDs
        parts = [p.strip() for p in cleaned.replace("|", ",").replace(" ", ",").split(",")]
        triggered = [p for p in parts if p in VALID_IDS]
        # Noise gate: 4B triggers everything when overwhelmed → treat as noise
        if len(triggered) > MAX_TRIGGERS:
            logger.warning(
                f"CV Sentinel noise gate: {len(triggered)} vectors triggered (max {MAX_TRIGGERS}), "
                "treating as noise — model not discriminating"
            )
            return []
        return triggered

    def _compute_risk_level(self, triggered_ids: list, vectors: list) -> tuple:
        """Compute composite risk level. Returns (level, score)."""
        if not triggered_ids:
            return "none", 0.0
        v_map = {v["vector_id"]: v for v in vectors}
        score = 0.0
        for vid in triggered_ids:
            v = v_map.get(vid)
            if v:
                score += v["risk_score"] * (1 + v["drift_ranking_score"] / 100)
        if score >= 50:
            return "critical", score
        elif score >= 40:
            return "high", score
        elif score >= 25:
            return "medium", score
        elif score >= 10:
            return "low", score
        return "none", score

    def _update_vector_stats(self, triggered_ids: list, now: str):
        """Update trigger counts for hit vectors."""
        try:
            from memory.db_utils import connect_wal
            conn = connect_wal(VECTORS_DB)
            for vid in triggered_ids:
                conn.execute("""
                    UPDATE complexity_vectors
                    SET trigger_count = trigger_count + 1,
                        last_triggered_at = ?,
                        updated_at = ?
                    WHERE vector_id = ?
                """, (now, now, vid))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Update vector stats error: {e}")

    def _inject_critical(self, triggered_ids: list, vectors: list,
                         risk_level: str, now: str):
        """Write finding to critical_findings in ARCHIVE_DB + Redis."""
        v_map = {v["vector_id"]: v for v in vectors}
        details = ", ".join(
            f"{vid}: {v_map[vid]['name']}" for vid in triggered_ids if vid in v_map
        )
        severity = "critical" if risk_level in ("high", "critical") else "architectural"
        finding = (
            f"CV Sentinel [{risk_level}]: {len(triggered_ids)} vectors drifting — {details}. "
            f"Escalated to P3." if risk_level in ("high", "critical") else
            f"CV Sentinel [{risk_level}]: {len(triggered_ids)} vectors showing drift — {details}."
        )

        # 1. SQLite (authoritative) with dedup
        try:
            from memory.db_utils import connect_wal
            conn = connect_wal(ARCHIVE_DB)
            existing = conn.execute("""
                SELECT id FROM critical_findings
                WHERE pass_id = ? AND substr(finding, 1, 100) = substr(?, 1, 100)
                AND found_at > datetime(?, '-' || ? || ' hours')
                LIMIT 1
            """, (PASS_ID, finding, now, str(DEDUP_HOURS))).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO critical_findings
                    (pass_id, finding, severity, session_id, item_id, found_at,
                     promoted_from_tank, wired_to_anticipation, wired_to_bigpicture)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 1, 1)
                """, (PASS_ID, finding, severity, "sentinel",
                      f"cv_{int(time.time())}", now))
                conn.commit()
                logger.info(f"Injected CV critical: {finding[:80]}")
            else:
                logger.debug("CV critical deduped — same finding within window")
            conn.close()
        except Exception as e:
            logger.error(f"CV inject SQLite error: {e}")
            # Retry once after 1s on lock
            if "locked" in str(e).lower():
                time.sleep(1)
                try:
                    from memory.db_utils import connect_wal
                    conn = connect_wal(ARCHIVE_DB)
                    conn.execute("""
                        INSERT INTO critical_findings
                        (pass_id, finding, severity, session_id, item_id, found_at,
                         promoted_from_tank, wired_to_anticipation, wired_to_bigpicture)
                        VALUES (?, ?, ?, ?, ?, ?, 0, 1, 1)
                    """, (PASS_ID, finding, severity, "sentinel",
                          f"cv_{int(time.time())}", now))
                    conn.commit()
                    conn.close()
                except Exception as e2:
                    logger.error(f"CV inject SQLite retry failed: {e2}")

        # 2. Redis (cache — WAL-style append)
        rc = self._get_redis()
        if rc:
            try:
                existing_items = rc.lrange(REDIS_CRITICAL_KEY, 0, 49)
                already = False
                for item in existing_items:
                    try:
                        entry = json.loads(item)
                        if (entry.get("pass") == PASS_ID and
                                entry.get("finding", "")[:100] == finding[:100]):
                            already = True
                            break
                    except (json.JSONDecodeError, TypeError):
                        continue
                if not already:
                    _cv_payload = {
                        "pass": PASS_ID, "finding": finding,
                        "severity": severity, "found_at": now, "verified": True,
                    }
                    rc.lpush(REDIS_CRITICAL_KEY, json.dumps(_cv_payload))
                    rc.ltrim(REDIS_CRITICAL_KEY, 0, 49)
                    # WAL: additive sorted set (never trimmed)
                    try:
                        from memory.session_gold_passes import _wal_append_critical
                        _wal_append_critical(_cv_payload)
                    except Exception as e_wal:
                        logger.warning(f"CV WAL append error: {e_wal}")
                rc.expire(REDIS_CRITICAL_KEY, REDIS_CRITICAL_TTL)
            except Exception as e:
                logger.warning(f"CV inject Redis error: {e}")

    def _trigger_corrigibility_challenge(self, triggered_ids: list, vectors: list) -> bool:
        """Auto-fire neurologist-challenge when risk is high/critical.
        Builds a challenge topic from triggered vectors and runs surgery-team.py
        in background subprocess. Dedup: max 1 challenge per DEDUP_HOURS window.
        """
        # Dedup via Redis — don't spam challenges
        rc = self._get_redis()
        if rc:
            try:
                if rc.get("contextdna:cv_sentinel:last_challenge"):
                    logger.debug("CV Sentinel: challenge already fired within window, skipping")
                    return False
            except Exception as e_redis:
                logger.debug(f"CV challenge dedup Redis error: {e_redis}")

        # Build challenge topic from triggered vectors
        v_map = {v["vector_id"]: v for v in vectors}
        names = [v_map[vid]["name"] for vid in triggered_ids[:3] if vid in v_map]
        topic = f"complexity drift risk: {', '.join(names)}"

        # Fire neurologist-challenge via surgery_bridge (direct import or subprocess fallback)
        # REVERSIBILITY: old subprocess path preserved below — uncomment to revert
        # import subprocess
        # script = Path(__file__).parent.parent / "scripts" / "surgery-team.py"
        # if not script.exists():
        #     logger.warning("surgery-team.py not found, cannot escalate")
        #     return False
        # cmd = [str(Path(__file__).parent.parent / ".venv" / "bin" / "python3"),
        #        str(script), "neurologist-challenge", topic]
        # env = {**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent)}
        # proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # logger.info(f"CV Sentinel escalation: neurologist-challenge fired (pid={proc.pid}) topic={topic!r}")
        try:
            from memory.surgery_bridge import neurologist_challenge as _bridge_neuro
            result = _bridge_neuro(topic)
            if result.get("status") != "ok":
                logger.warning("CV Sentinel escalation returned non-ok: %s", result)
            logger.info(
                "CV Sentinel escalation: neurologist-challenge via bridge (path=%s) topic=%r",
                result.get("path", "unknown"), topic,
            )

            # Set dedup window
            if rc:
                try:
                    rc.set("contextdna:cv_sentinel:last_challenge",
                           json.dumps({"at": datetime.utcnow().isoformat(), "topic": topic}),
                           ex=DEDUP_HOURS * 3600)
                except Exception as e_dedup:
                    logger.debug(f"CV challenge dedup set error: {e_dedup}")
            return True
        except Exception as e:
            logger.error(f"CV Sentinel escalation error: {e}")
            return False

    def _set_priority_escalation(self, risk_level: str):
        """Write risk level to Redis for other subsystems to read."""
        rc = self._get_redis()
        if rc:
            try:
                rc.set(REDIS_RISK_KEY, risk_level, ex=600)
            except Exception as e_risk:
                logger.debug(f"CV risk level Redis error: {e_risk}")

    def run_cycle(self) -> dict:
        """Single sentinel cycle."""
        start = time.time()
        self._ensure_vectors_db()
        now = datetime.utcnow().isoformat()

        # Check LLM health
        try:
            from memory.llm_priority_queue import check_llm_health
            if not check_llm_health():
                logger.info("CV Sentinel: LLM down, skipping cycle")
                self._set_priority_escalation("none")
                return {"triggered": [], "risk_level": "none", "skipped": "llm_down"}
        except Exception as e_health:
            logger.debug(f"CV LLM health check error: {e_health}")

        gold = self._read_recent_gold()
        sops = self._read_recent_sops()

        if not gold and not sops:
            logger.debug("CV Sentinel: no recent data, skipping")
            self._set_priority_escalation("none")
            return {"triggered": [], "risk_level": "none", "skipped": "no_data"}

        vectors = self._load_vectors()
        if not vectors:
            return {"triggered": [], "risk_level": "none", "skipped": "no_vectors"}

        # Single classify call — P4 BACKGROUND, $0
        system_prompt, user_prompt = self._build_classify_prompt(gold, sops, vectors)
        try:
            from memory.llm_priority_queue import butler_query
            response = butler_query(system_prompt, user_prompt, profile="classify")
        except Exception as e:
            logger.error(f"CV Sentinel LLM error: {e}")
            return {"triggered": [], "risk_level": "none", "skipped": "llm_error"}

        triggered = self._parse_response(response)
        risk_level, risk_score = self._compute_risk_level(triggered, vectors)

        if triggered:
            self._update_vector_stats(triggered, now)

        injected = False
        if risk_level in ("medium", "high", "critical"):
            self._inject_critical(triggered, vectors, risk_level, now)
            injected = True

        # Escalation chain: medium+ → auto-trigger neurologist-challenge
        escalated = False
        if risk_level in ("high", "critical"):
            escalated = self._trigger_corrigibility_challenge(triggered, vectors)

        self._set_priority_escalation(risk_level)

        # Update last run
        rc = self._get_redis()
        if rc:
            try:
                rc.set(REDIS_LAST_RUN_KEY, json.dumps({
                    "at": now, "triggered": triggered, "risk": risk_level,
                    "score": round(risk_score, 1),
                    "duration_ms": int((time.time() - start) * 1000),
                }), ex=600)
            except Exception as e_last:
                logger.debug(f"CV last run Redis error: {e_last}")

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            f"CV Sentinel: risk={risk_level} score={risk_score:.1f} "
            f"triggered={triggered} injected={injected} {duration_ms}ms"
        )
        return {
            "triggered": triggered,
            "risk_level": risk_level,
            "risk_score": round(risk_score, 1),
            "injected": injected,
            "escalated": escalated,
            "duration_ms": duration_ms,
        }


# ── Module-level job function for lite_scheduler ──

_sentinel_instance = None


def job_cv_sentinel_cycle():
    """Lite scheduler entry point — complexity vector sentinel cycle."""
    global _sentinel_instance
    if _sentinel_instance is None:
        _sentinel_instance = ComplexityVectorSentinel()
    try:
        result = _sentinel_instance.run_cycle()
        logger.info(f"CV Sentinel: risk={result.get('risk_level')}, triggered={result.get('triggered')}")
        return result
    except Exception as e:
        logger.error(f"CV Sentinel cycle error: {e}")
        return {}
