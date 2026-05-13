#!/usr/bin/env python3
"""
AUTONOMOUS A/B TESTING ENGINE

Self-enhancing A/B testing with 3-surgeon consensus.
Runs autonomously via lite_scheduler when Atlas is not present.

Flow: Evidence → Candidate → Design → Consensus → Grace → Activate → Monitor → Conclude → Evidence

Safety: auto-revert on degradation, config snapshots, forbidden parameters,
max 1 concurrent test, 48h max duration, $2/day budget cap, 30-min grace veto.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
MEMORY_DIR = Path(__file__).parent

# --- Constants ---

MAX_CONCURRENT_TESTS = 1
MAX_TEST_DURATION_HOURS = 48
GRACE_PERIOD_MINUTES = 30
AUTONOMOUS_BUDGET_USD = 2.00  # $2/day for autonomous ops
MIN_BUDGET_FOR_TEST = 0.10    # Need at least $0.10 to start a test

# Parameters autonomous tests MAY touch
ALLOWED_TEST_PARAMETERS = {
    "injection_config": ["section_4_enabled", "section_7_enabled", "depth_mode",
                         "abbrev_char_limit", "full_char_limit"],
    "llm_profile": ["temperature", "max_tokens"],
    "scheduler_param": ["gold_mining_interval", "pre_compute_interval"],
}

# Parameters autonomous tests must NEVER touch
FORBIDDEN_PARAMETERS = [
    "section_0_*", "section_2_*", "section_8_*",   # Safety, Professor, Synaptic
    "gpu_lock_*", "redis_*", "db_*",                # Infrastructure
    "port_*", "secret_*", "key_*", "password_*",    # Security
]

# Degradation thresholds — breach triggers auto-revert
DEGRADATION_THRESHOLDS = {
    "webhook_e2e_p95_ms": {"max_value": 8000, "window_hours": 1},
    "llm_error_rate_pct": {"max_value": 15, "window_hours": 2},
    "scheduler_failure_rate_pct": {"max_value": 10, "window_hours": 1},
}

# Status lifecycle
STATUS_QUEUED = "queued"
STATUS_GRACE = "grace"
STATUS_ACTIVE = "active"
STATUS_MONITORING = "monitoring"
STATUS_CONCLUDED = "concluded"
STATUS_REVERTED = "reverted"
STATUS_VETOED = "vetoed"


# --- LLM provider resolution (DeepSeek cutover; default: anthropic/openai path) ---

def _resolve_llm_provider() -> Dict[str, Any]:
    """Resolve current LLM provider from env.

    Returns dict: {provider, base_url, model, api_key}.

    **Default = DeepSeek** (Aaron cutover 2026-04-18: cheaper and primary).
    Set ``LLM_PROVIDER=openai`` to use OpenAI for users who prefer it.

    If ``LLM_PROVIDER`` is unset, this function auto-selects based on available
    credentials: DeepSeek if a DS key is present (env or Keychain), otherwise
    OpenAI if an OpenAI key is present, otherwise ``{api_key: ""}`` so callers
    can degrade gracefully instead of hard-failing.
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()

    def _resolve_deepseek() -> Dict[str, Any]:
        api_key = (
            os.environ.get("Context_DNA_Deep_Seek")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("DEEPSEEK_API_KEY", "")
        )
        if not api_key:
            try:
                r = subprocess.run(
                    ["security", "find-generic-password", "-s", "fleet-nerve",
                     "-a", "Context_DNA_Deep_Seek", "-w"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0:
                    api_key = r.stdout.strip()
            except Exception:
                api_key = ""
        return {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": api_key,
        }

    def _resolve_openai() -> Dict[str, Any]:
        return {
            "provider": "openai",
            "base_url": None,  # openai SDK default
            "model": "gpt-4.1-mini",
            "api_key": os.environ.get("Context_DNA_OPENAI", "")
                        or os.environ.get("OPENAI_API_KEY", ""),
        }

    if provider == "deepseek":
        return _resolve_deepseek()
    if provider == "openai":
        return _resolve_openai()

    # Auto-select: DeepSeek primary, OpenAI fallback, empty-key last-resort.
    ds_cfg = _resolve_deepseek()
    if ds_cfg["api_key"]:
        return ds_cfg
    oai_cfg = _resolve_openai()
    if oai_cfg["api_key"]:
        logger.info("_resolve_llm_provider: DeepSeek key unavailable, falling back to OpenAI")
        return oai_cfg
    # No keys at all — return DeepSeek config with empty key so caller can log + skip
    logger.warning(
        "_resolve_llm_provider: no DeepSeek or OpenAI key found — "
        "A/B consensus / conclusion calls will no-op until a key is configured"
    )
    return ds_cfg


def _get_openai_client(api_key: str, base_url: Optional[str] = None):
    """Create an OpenAI-compatible client, or ``None`` if the SDK is missing.

    The ``openai`` package is OPTIONAL (flip 2026-04-18: DeepSeek is primary).
    This helper lets callers degrade gracefully — an import failure logs once
    and returns None instead of raising ``ModuleNotFoundError`` at call time.

    Used for both openai-native calls and DeepSeek calls that piggy-back on
    the OpenAI-compatible SDK (``base_url=https://api.deepseek.com/v1``).
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning(
            "openai package not installed — ab_autonomous external-LLM calls "
            "will be skipped. Install with `pip install openai` if you want "
            "OpenAI or OpenAI-compatible DeepSeek access via this path."
        )
        return None
    try:
        return OpenAI(api_key=api_key, base_url=base_url)
    except Exception as exc:
        logger.warning("openai client init failed: %s", exc)
        return None


@dataclass
class AutonomousTest:
    """A single autonomous A/B test."""
    test_id: str
    hypothesis: str
    config_type: str
    config_key: str
    value_control: Any
    value_variant: Any
    consensus: Dict[str, Any] = field(default_factory=dict)
    pre_auth_rules: Dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_QUEUED
    created_at: float = 0.0
    grace_until: float = 0.0
    activated_at: float = 0.0
    max_duration_hours: float = MAX_TEST_DURATION_HOURS
    baseline_metrics: Dict[str, Any] = field(default_factory=dict)
    current_metrics: Dict[str, Any] = field(default_factory=dict)
    degradation_reason: str = ""
    conclusion: str = ""


# --- Budget Manager ---

class BudgetManager:
    """Track autonomous A/B testing spend. $2/day cap."""

    def __init__(self):
        self._rc = self._connect_redis()

    def _connect_redis(self):
        try:
            import redis
            rc = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
            rc.ping()
            return rc
        except Exception:
            return None

    def _key(self) -> str:
        return f"ab_autonomous:costs:{date.today().isoformat()}"

    def spent_today(self) -> float:
        if not self._rc:
            return 0.0
        return float(self._rc.get(self._key()) or 0)

    def remaining(self) -> float:
        return max(0, AUTONOMOUS_BUDGET_USD - self.spent_today())

    def can_afford(self, cost_usd: float) -> bool:
        return self.remaining() >= cost_usd

    def track(self, cost_usd: float, description: str):
        if not self._rc or cost_usd <= 0:
            return
        self._rc.incrbyfloat(self._key(), cost_usd)
        self._rc.expire(self._key(), 86400 * 3)
        event = json.dumps({
            "ts": time.time(), "cost_usd": cost_usd,
            "description": description[:100],
        })
        self._rc.lpush("ab_autonomous:events", event)
        self._rc.ltrim("ab_autonomous:events", 0, 99)


# --- Core Engine ---

def _get_db() -> sqlite3.Connection:
    """Get hook evolution DB (reuses same .pattern_evolution.db)."""
    from memory.db_utils import connect_wal
    db_path = MEMORY_DIR / ".pattern_evolution.db"
    return connect_wal(db_path, check_same_thread=False)


def _get_redis():
    try:
        import redis
        rc = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
        return rc
    except Exception:
        return None


def _is_parameter_forbidden(config_type: str, config_key: str) -> bool:
    """Check if a parameter is in the forbidden list."""
    full_key = f"{config_type}.{config_key}"
    for pattern in FORBIDDEN_PARAMETERS:
        # Convert glob to regex
        regex = pattern.replace("*", ".*")
        if re.match(regex, config_key) or re.match(regex, full_key):
            return True
    # Check if config_type is allowed
    allowed_keys = ALLOWED_TEST_PARAMETERS.get(config_type, [])
    return config_key not in allowed_keys


def get_active_test() -> Optional[AutonomousTest]:
    """Get the currently active/grace/monitoring test, if any."""
    db = _get_db()
    try:
        row = db.execute("""
            SELECT test_id, hypothesis, hook_type, status,
                   consensus_json, pre_auth_rules, grace_until,
                   degradation_metrics, max_duration_hours,
                   control_variant_id, variant_a_id,
                   started_at, created_at
            FROM hook_ab_tests
            WHERE status IN ('grace', 'active', 'monitoring')
              AND auto_revert_triggered = 0
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        if not row:
            return None

        # Recover config details from ab_config_versions
        cv = db.execute("""
            SELECT config_type, config_key, value_before, value_after
            FROM ab_config_versions WHERE test_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """, (row[0],)).fetchone()

        test = AutonomousTest(
            test_id=row[0],
            hypothesis=row[1] or "",
            config_type=cv[0] if cv else "",
            config_key=cv[1] if cv else "",
            value_control=json.loads(cv[2]) if cv else None,
            value_variant=json.loads(cv[3]) if cv else None,
            status=row[3],
            consensus=json.loads(row[4]) if row[4] else {},
            pre_auth_rules=json.loads(row[5]) if row[5] else {},
            grace_until=row[6] or 0,
            max_duration_hours=row[8] or MAX_TEST_DURATION_HOURS,
            created_at=time.mktime(datetime.fromisoformat(row[12]).timetuple()) if row[12] else 0,
        )
        if row[11]:  # started_at
            test.activated_at = time.mktime(datetime.fromisoformat(row[11]).timetuple())
        return test
    except Exception as e:
        logger.error(f"get_active_test error: {e}")
        return None
    finally:
        db.close()


def get_queued_tests() -> List[AutonomousTest]:
    """Get all tests in 'queued' status awaiting activation."""
    db = _get_db()
    try:
        rows = db.execute("""
            SELECT test_id, hypothesis, hook_type, status,
                   consensus_json, pre_auth_rules, grace_until
            FROM hook_ab_tests
            WHERE status = 'queued'
            ORDER BY created_at ASC
        """).fetchall()
        tests = []
        for row in rows:
            tests.append(AutonomousTest(
                test_id=row[0], hypothesis=row[1] or "",
                config_type="", config_key="",
                value_control=None, value_variant=None,
                status=row[3],
                consensus=json.loads(row[4]) if row[4] else {},
                pre_auth_rules=json.loads(row[5]) if row[5] else {},
                grace_until=row[6] or 0,
            ))
        return tests
    except Exception as e:
        logger.error(f"get_queued_tests error: {e}")
        return []
    finally:
        db.close()


# --- Phase 1: Scan for candidates ---

def scan_for_candidates(max_candidates: int = 5) -> List[Dict[str, Any]]:
    """
    Query evidence store for learnings with A/B test potential.
    Uses Qwen3-4B classify (64 tok) to score testability.
    Returns scored candidates sorted by potential.
    """
    try:
        from memory.learning_store import get_learning_store
    except ImportError:
        logger.warning("learning_store not available")
        return []

    store = get_learning_store()
    # Get recent learnings of types that suggest testable hypotheses
    candidates = []
    for ltype in ["pattern", "win", "insight", "fix"]:
        items = store.get_by_type(ltype, limit=20)
        candidates.extend(items)

    if not candidates:
        return []

    # Deduplicate by id
    seen = set()
    unique = []
    for c in candidates:
        cid = c.get("id", "")
        if cid not in seen:
            seen.add(cid)
            unique.append(c)
    candidates = unique[:50]  # Cap at 50 for LLM scoring

    # Score each with Qwen3-4B classify
    scored = []
    try:
        from memory.llm_priority_queue import llm_generate, Priority
    except ImportError:
        logger.warning("llm_priority_queue not available")
        return candidates[:max_candidates]

    for c in candidates:
        title = c.get("title", "")
        content = c.get("content", "")[:200]
        prompt = (
            f"Is this testable as an A/B config change? Score 0-3.\n"
            f"0=not testable, 1=maybe, 2=testable, 3=high-value candidate\n"
            f"Learning: {title}. {content}\n"
            f"Reply with ONLY a single digit 0-3."
        )
        try:
            result = llm_generate(
                "/no_think Score testability 0-3. Reply single digit only.",
                prompt, Priority.LOW, "classify", "ab_scan"
            )
            if result:
                # Extract digit
                digits = re.findall(r'[0-3]', result.strip())
                score = int(digits[0]) if digits else 0
            else:
                score = 0
        except Exception:
            score = 0

        if score >= 2:
            scored.append({**c, "_ab_score": score})

    scored.sort(key=lambda x: x.get("_ab_score", 0), reverse=True)
    logger.info(f"A/B scan: {len(scored)} testable candidates from {len(candidates)} learnings")
    return scored[:max_candidates]


# --- Phase 2: Design test ---

def design_test(candidate: Dict[str, Any], budget: BudgetManager) -> Optional[AutonomousTest]:
    """
    Use GPT-4.1 to design an A/B test from a candidate learning.
    Returns AutonomousTest ready for consensus, or None if budget exhausted.
    """
    est_cost = 0.04  # ~$0.04 for gpt-4.1 design call
    if not budget.can_afford(est_cost):
        logger.warning(f"A/B design: insufficient budget (${budget.remaining():.4f} remaining)")
        return None

    title = candidate.get("title", "")
    content = candidate.get("content", "")[:500]

    # Load env for OpenAI
    _ensure_env()

    system = (
        "You are an A/B test designer for a webhook injection system.\n"
        "Design a minimal, safe A/B test. Output JSON only:\n"
        '{"hypothesis":"If X then Y because Z",'
        '"config_type":"injection_config|llm_profile|scheduler_param",'
        '"config_key":"specific_parameter_name",'
        '"value_variant":"proposed new value",'
        '"success_metrics":["metric1","metric2"],'
        '"duration_hours":24,'
        '"risk_assessment":"low|medium"}\n'
        f"Allowed parameters: {json.dumps(ALLOWED_TEST_PARAMETERS)}\n"
        "Keep tests simple. One parameter change only."
    )
    prompt = f"Design an A/B test for this learning:\nTitle: {title}\nContent: {content}"

    try:
        _cfg = _resolve_llm_provider()
        if not _cfg["api_key"]:
            logger.warning(
                "A/B design: no external LLM key (%s) — skipping design step",
                _cfg["provider"],
            )
            return None
        client = _get_openai_client(api_key=_cfg["api_key"], base_url=_cfg["base_url"])
        if client is None:
            return None
        resp = client.chat.completions.create(
            model=_cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=512,
            temperature=0.7,
        )
        raw = resp.choices[0].message.content.strip()
        cost = _calc_cost(resp.usage, _cfg["model"])
        budget.track(cost, f"ab_design:{title[:40]}")

        # Parse JSON from response
        design = _extract_json(raw)
        if not design or "hypothesis" not in design:
            logger.warning(f"A/B design: invalid JSON from GPT-4.1")
            return None

        config_type = design.get("config_type", "")
        config_key = design.get("config_key", "")

        # Safety check
        if _is_parameter_forbidden(config_type, config_key):
            logger.warning(f"A/B design: forbidden parameter {config_type}.{config_key}")
            return None

        test_id = f"ab-{date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        return AutonomousTest(
            test_id=test_id,
            hypothesis=design["hypothesis"],
            config_type=config_type,
            config_key=config_key,
            value_control=_get_current_value(config_type, config_key),
            value_variant=design.get("value_variant"),
            max_duration_hours=min(design.get("duration_hours", 24), MAX_TEST_DURATION_HOURS),
            created_at=time.time(),
        )
    except Exception as e:
        logger.error(f"A/B design failed: {e}")
        return None


# --- Phase 3: Consensus ---

def request_consensus(test: AutonomousTest, budget: BudgetManager) -> Dict[str, Any]:
    """
    Run 3-surgeon consensus on test design.
    Returns consensus dict with status: approved|approved_with_caveats|needs_revision
    """
    est_cost = 0.06  # ~$0.06 for consensus round
    if not budget.can_afford(est_cost):
        return {"consensus_status": "budget_exhausted", "approved": False}

    _ensure_env()
    ts = int(time.time())
    discussion_id = f"ab-auto-{ts}"

    # --- Cardiologist (GPT-4.1) ---
    cardio_system = (
        "You are GPT-4.1, Cardiologist in the Surgery Team of 3.\n"
        "Evaluate this autonomous A/B test proposal. Consider safety, measurability, and value.\n"
        'Output JSON: {"confidence":0.0-1.0,"approve":true|false,"reasoning":"1-2 sentences",'
        '"risks":["risk1"],"suggestions":["suggestion1"]}'
    )
    cardio_prompt = (
        f"Test: {test.test_id}\n"
        f"Hypothesis: {test.hypothesis}\n"
        f"Config: {test.config_type}.{test.config_key}\n"
        f"Control: {json.dumps(test.value_control)}\n"
        f"Variant: {json.dumps(test.value_variant)}\n"
        f"Duration: {test.max_duration_hours}h\n"
        f"Max concurrent: {MAX_CONCURRENT_TESTS}"
    )

    cardio_result = {"confidence": 0.5, "approve": False, "reasoning": "unavailable"}
    cardio_cost = 0.0
    try:
        _cfg = _resolve_llm_provider()
        if not _cfg["api_key"]:
            raise RuntimeError(
                f"no external LLM key configured ({_cfg['provider']}) — "
                "Cardiologist unavailable"
            )
        client = _get_openai_client(api_key=_cfg["api_key"], base_url=_cfg["base_url"])
        if client is None:
            raise RuntimeError("openai-compat SDK unavailable")
        resp = client.chat.completions.create(
            model=_cfg["model"],
            messages=[
                {"role": "system", "content": cardio_system},
                {"role": "user", "content": cardio_prompt},
            ],
            max_tokens=512, temperature=0.5,
        )
        cardio_cost = _calc_cost(resp.usage, _cfg["model"])
        budget.track(cardio_cost, f"ab_consensus_cardio:{test.test_id}")
        parsed = _extract_json(resp.choices[0].message.content)
        if parsed:
            cardio_result = parsed
    except Exception as e:
        logger.error(f"Cardiologist consensus failed: {e}")

    # --- Neurologist (Qwen3-4B) ---
    neuro_system = (
        "/no_think You are Qwen3-4B, Neurologist. "
        "Evaluate A/B test feasibility. Score 0-3 each dimension.\n"
        'Output JSON ONLY: {"measurement_feasibility":N,"risk_level":N,'
        '"implementation_clarity":N,"approve":true|false,"concerns":["..."]}'
    )
    neuro_prompt = cardio_prompt

    neuro_result = {"measurement_feasibility": 1, "risk_level": 2, "implementation_clarity": 1, "approve": False}
    try:
        from memory.llm_priority_queue import llm_generate, Priority
        raw = llm_generate(neuro_system, neuro_prompt, Priority.ATLAS, "extract", "ab_consensus")
        if raw:
            parsed = _extract_json(raw)
            if parsed:
                neuro_result = parsed
    except Exception as e:
        logger.error(f"Neurologist consensus failed: {e}")

    # --- Atlas synthesis ---
    cardio_conf = cardio_result.get("confidence", 0.5)
    cardio_approve = cardio_result.get("approve", False)
    neuro_meas = neuro_result.get("measurement_feasibility", 1)
    neuro_risk = neuro_result.get("risk_level", 2)
    neuro_approve = neuro_result.get("approve", False)

    # UNANIMOUS CONSENSUS REQUIRED (Aaron: "all 3 surgeons required consensus to keep")
    # Both Cardiologist AND Neurologist must approve — Atlas follows only if both agree
    votes_for = sum([cardio_approve, neuro_approve])
    atlas_approve = votes_for == 2  # Both must approve for Atlas to agree

    # Blocking conditions
    dissent = []
    if neuro_meas < 2:
        dissent.append(f"Qwen3-4B: measurement feasibility too low ({neuro_meas}/3)")
        atlas_approve = False
    if neuro_risk > 2:
        dissent.append(f"Qwen3-4B: risk too high ({neuro_risk}/3)")
        atlas_approve = False
    if cardio_conf < 0.4:
        dissent.append(f"GPT-4.1: low confidence ({cardio_conf:.0%})")
        atlas_approve = False

    if atlas_approve and not dissent:
        status = "approved" if not neuro_result.get("concerns") else "approved_with_caveats"
    elif atlas_approve:
        status = "approved_with_caveats"
    else:
        status = "needs_revision"

    consensus = {
        "consensus_status": status,
        "approved": status in ("approved", "approved_with_caveats"),
        "discussion_id": discussion_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "votes": {"GPT-4.1": cardio_approve, "Qwen3-4B": neuro_approve, "Atlas": atlas_approve},
        "cardiologist": {
            "confidence": cardio_conf,
            "approve": cardio_approve,
            "reasoning": cardio_result.get("reasoning", ""),
            "risks": cardio_result.get("risks", []),
        },
        "neurologist": {
            "measurement_feasibility": neuro_meas,
            "risk_level": neuro_risk,
            "approve": neuro_approve,
            "concerns": neuro_result.get("concerns", []),
        },
        "dissent": dissent,
        "total_cost_usd": cardio_cost,
    }

    # Persist consensus to Redis
    rc = _get_redis()
    if rc:
        rc.set(f"ab_autonomous:consensus:{ts}", json.dumps(consensus), ex=86400 * 30)

    return consensus


# --- Phase 4: Queue & Grace ---

def queue_test(test: AutonomousTest, consensus: Dict[str, Any]) -> bool:
    """Store approved test in DB with grace period. Returns True on success."""
    if not consensus.get("approved"):
        logger.info(f"A/B {test.test_id}: not approved, skipping queue")
        return False

    # Check concurrent test limit
    active = get_active_test()
    if active:
        logger.info(f"A/B {test.test_id}: another test active ({active.test_id}), deferring")
        return False

    db = _get_db()
    try:
        grace_until = time.time() + (GRACE_PERIOD_MINUTES * 60)

        db.execute("""
            INSERT OR REPLACE INTO hook_ab_tests
            (test_id, test_name, hook_type, control_variant_id, variant_a_id,
             status, consensus_json, pre_auth_rules, grace_until,
             hypothesis, max_duration_hours, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            test.test_id,
            f"Auto: {test.hypothesis[:60]}",
            test.config_type,
            json.dumps(test.value_control),
            json.dumps(test.value_variant),
            STATUS_GRACE,
            json.dumps(consensus),
            json.dumps(test.pre_auth_rules),
            grace_until,
            test.hypothesis,
            test.max_duration_hours,
        ))

        # Snapshot config version
        version_id = f"cv-{uuid.uuid4().hex[:8]}"
        db.execute("""
            INSERT INTO ab_config_versions
            (version_id, test_id, config_type, config_key, value_before, value_after, snapshot_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            version_id, test.test_id, test.config_type, test.config_key,
            json.dumps(test.value_control), json.dumps(test.value_variant),
            time.time(),
        ))

        db.commit()
        test.status = STATUS_GRACE
        test.grace_until = grace_until

        # Notify
        _notify_test_event(test, "proposed",
                           f"A/B Test Proposed: {test.test_id}",
                           f"{test.hypothesis[:100]}\nGrace until: {datetime.fromtimestamp(grace_until).strftime('%H:%M')}")

        logger.info(f"A/B {test.test_id}: queued with grace until {datetime.fromtimestamp(grace_until).isoformat()}")
        return True
    except Exception as e:
        logger.error(f"queue_test error: {e}")
        return False
    finally:
        db.close()


# --- Phase 5: Activate ---

def activate_test(test: AutonomousTest) -> bool:
    """Activate a test after grace period. Apply variant config."""
    if test.status != STATUS_GRACE:
        return False
    if time.time() < test.grace_until:
        return False  # Still in grace

    # Capture baseline metrics before changing anything
    test.baseline_metrics = _capture_metrics()

    # Apply the variant value
    success = _apply_config(test.config_type, test.config_key, test.value_variant)
    if not success:
        logger.error(f"A/B {test.test_id}: failed to apply config")
        return False

    # Update DB
    db = _get_db()
    try:
        db.execute("""
            UPDATE hook_ab_tests SET status = ?, started_at = CURRENT_TIMESTAMP
            WHERE test_id = ?
        """, (STATUS_ACTIVE, test.test_id))
        db.commit()
    except Exception as e:
        logger.error(f"activate_test DB error: {e}")
        # Revert config on DB failure
        _apply_config(test.config_type, test.config_key, test.value_control)
        return False
    finally:
        db.close()

    test.status = STATUS_ACTIVE
    test.activated_at = time.time()

    _notify_test_event(test, "activated",
                       f"A/B Test Active: {test.test_id}",
                       f"Applied: {test.config_type}.{test.config_key} = {test.value_variant}")

    logger.info(f"A/B {test.test_id}: ACTIVATED")
    return True


# --- Phase 6: Monitor ---

def monitor_test(test: AutonomousTest) -> str:
    """
    Check active test for degradation. Returns status:
    'ok', 'degraded' (triggers revert), 'expired' (triggers conclude).
    """
    if test.status not in (STATUS_ACTIVE, STATUS_MONITORING):
        return "inactive"

    # Check max duration
    if test.activated_at > 0:
        elapsed_hours = (time.time() - test.activated_at) / 3600
        if elapsed_hours >= test.max_duration_hours:
            return "expired"

    # Capture current metrics
    test.current_metrics = _capture_metrics()

    # Check degradation thresholds
    reason = detect_degradation(test)
    if reason:
        test.degradation_reason = reason
        return "degraded"

    # Update status to monitoring after first successful check
    if test.status == STATUS_ACTIVE:
        db = _get_db()
        try:
            db.execute("UPDATE hook_ab_tests SET status = ? WHERE test_id = ?",
                        (STATUS_MONITORING, test.test_id))
            db.commit()
        except Exception:
            pass
        finally:
            db.close()
        test.status = STATUS_MONITORING

    return "ok"


def detect_degradation(test: AutonomousTest) -> Optional[str]:
    """Compare current metrics vs baseline. Return reason string if degraded, None if ok."""
    baseline = test.baseline_metrics
    current = test.current_metrics

    if not baseline or not current:
        return None  # No metrics to compare yet

    reasons = []

    # Check webhook E2E p95
    b_e2e = baseline.get("webhook_e2e_p95_ms", 0)
    c_e2e = current.get("webhook_e2e_p95_ms", 0)
    threshold = DEGRADATION_THRESHOLDS.get("webhook_e2e_p95_ms", {})
    if c_e2e > threshold.get("max_value", 8000) and b_e2e > 0 and c_e2e > b_e2e * 1.5:
        reasons.append(f"webhook_e2e_p95: {c_e2e:.0f}ms (was {b_e2e:.0f}ms)")

    # Check LLM error rate
    c_llm_err = current.get("llm_error_rate_pct", 0)
    if c_llm_err > DEGRADATION_THRESHOLDS.get("llm_error_rate_pct", {}).get("max_value", 15):
        reasons.append(f"llm_error_rate: {c_llm_err:.1f}%")

    # Check scheduler failure rate
    c_sched = current.get("scheduler_failure_rate_pct", 0)
    if c_sched > DEGRADATION_THRESHOLDS.get("scheduler_failure_rate_pct", {}).get("max_value", 10):
        reasons.append(f"scheduler_failure_rate: {c_sched:.1f}%")

    if reasons:
        return "; ".join(reasons)
    return None


# --- Phase 7: Auto-Revert ---

def auto_revert(test: AutonomousTest) -> bool:
    """Restore config from snapshot. Mark test as reverted. Notify critical."""
    # 1. Get snapshot
    db = _get_db()
    try:
        row = db.execute("""
            SELECT version_id, config_type, config_key, value_before
            FROM ab_config_versions WHERE test_id = ? AND reverted = 0
            ORDER BY snapshot_time DESC LIMIT 1
        """, (test.test_id,)).fetchone()

        if not row:
            logger.error(f"A/B {test.test_id}: no config snapshot to revert")
            return False

        version_id, config_type, config_key, value_before = row
        value_before_parsed = json.loads(value_before)

        # 2. Apply original config
        success = _apply_config(config_type, config_key, value_before_parsed)
        if not success:
            logger.error(f"A/B {test.test_id}: CRITICAL — revert failed!")
            return False

        # 3. Update DB
        now = time.time()
        db.execute("""
            UPDATE ab_config_versions SET reverted = 1, revert_time = ?
            WHERE version_id = ?
        """, (now, version_id))
        db.execute("""
            UPDATE hook_ab_tests SET status = ?, auto_revert_triggered = 1,
                   degradation_metrics = ?, ended_at = CURRENT_TIMESTAMP
            WHERE test_id = ?
        """, (STATUS_REVERTED, test.degradation_reason, test.test_id))
        db.commit()

        test.status = STATUS_REVERTED

        # 4. Notify (critical)
        _notify_test_event(test, "reverted",
                           f"A/B REVERTED: {test.test_id}",
                           f"Reason: {test.degradation_reason}\nRestored: {config_type}.{config_key}")

        # 5. Record as evidence
        _record_evidence(test, "gotcha",
                         f"A/B test failed: {test.hypothesis}. "
                         f"Degradation: {test.degradation_reason}. Auto-reverted.")

        logger.info(f"A/B {test.test_id}: REVERTED — {test.degradation_reason}")
        return True
    except Exception as e:
        logger.error(f"auto_revert error: {e}")
        return False
    finally:
        db.close()


# --- Phase 8: Conclude ---

def conclude_test(test: AutonomousTest, budget: BudgetManager) -> Dict[str, Any]:
    """
    Evaluate test results. Use GPT-4.1 for analysis.
    Write conclusion back to evidence store.
    """
    metrics_summary = {
        "baseline": test.baseline_metrics,
        "final": test.current_metrics or _capture_metrics(),
    }

    # GPT-4.1 conclusion analysis (optional, budget permitting)
    conclusion_text = ""
    est_cost = 0.03
    if budget.can_afford(est_cost):
        _ensure_env()
        try:
            _cfg = _resolve_llm_provider()
            if not _cfg["api_key"]:
                raise RuntimeError(
                    f"no external LLM key configured ({_cfg['provider']})"
                )
            client = _get_openai_client(api_key=_cfg["api_key"], base_url=_cfg["base_url"])
            if client is None:
                raise RuntimeError("openai-compat SDK unavailable")

            system = (
                "Analyze A/B test results. Determine winner (control or variant). "
                "Output JSON: {\"winner\":\"control|variant|inconclusive\","
                "\"confidence\":0.0-1.0,\"reasoning\":\"1-2 sentences\","
                "\"next_hypothesis\":\"suggested follow-up test or null\"}"
            )
            prompt = (
                f"Test: {test.test_id}\n"
                f"Hypothesis: {test.hypothesis}\n"
                f"Config: {test.config_type}.{test.config_key}\n"
                f"Control: {json.dumps(test.value_control)}\n"
                f"Variant: {json.dumps(test.value_variant)}\n"
                f"Duration: {test.max_duration_hours}h\n"
                f"Metrics: {json.dumps(metrics_summary)}"
            )

            resp = client.chat.completions.create(
                model=_cfg["model"],
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=512, temperature=0.3,
            )
            cost = _calc_cost(resp.usage, _cfg["model"])
            budget.track(cost, f"ab_conclude:{test.test_id}")
            parsed = _extract_json(resp.choices[0].message.content)
            if parsed:
                conclusion_text = parsed.get("reasoning", "")
                test.conclusion = json.dumps(parsed)
        except Exception as e:
            logger.error(f"conclude GPT-4.1 failed: {e}")
            test.conclusion = json.dumps({"winner": "inconclusive", "reasoning": str(e)})
    else:
        test.conclusion = json.dumps({"winner": "inconclusive", "reasoning": "budget exhausted"})

    # Decide: keep variant or revert to control
    try:
        conclusion = json.loads(test.conclusion)
    except Exception:
        conclusion = {"winner": "inconclusive"}

    winner = conclusion.get("winner", "inconclusive")

    if winner == "variant":
        # Keep the variant — it won
        evidence_type = "pattern"
        evidence_msg = f"A/B test confirmed: {test.hypothesis}. Variant kept."
    elif winner == "control":
        # Revert to control
        _apply_config(test.config_type, test.config_key, test.value_control)
        evidence_type = "gotcha"
        evidence_msg = f"A/B test disproved: {test.hypothesis}. Reverted to control."
    else:
        # Inconclusive — revert to be safe
        _apply_config(test.config_type, test.config_key, test.value_control)
        evidence_type = "insight"
        evidence_msg = f"A/B test inconclusive: {test.hypothesis}. Reverted to control."

    # Update DB
    db = _get_db()
    try:
        db.execute("""
            UPDATE hook_ab_tests SET status = ?, winner_variant_id = ?,
                   ended_at = CURRENT_TIMESTAMP
            WHERE test_id = ?
        """, (STATUS_CONCLUDED, winner, test.test_id))
        db.commit()
    except Exception as e:
        logger.error(f"conclude DB error: {e}")
    finally:
        db.close()

    test.status = STATUS_CONCLUDED

    # Record evidence
    _record_evidence(test, evidence_type, evidence_msg)

    # Notify
    _notify_test_event(test, "concluded",
                       f"A/B Concluded: {test.test_id}",
                       f"Winner: {winner}. {conclusion_text[:100]}")

    return conclusion


# --- Phase: Veto ---

def veto_test(test_id: str, reason: str = "manual veto") -> bool:
    """Veto a test during grace period. Reverts if already active."""
    db = _get_db()
    try:
        row = db.execute("SELECT status FROM hook_ab_tests WHERE test_id = ?", (test_id,)).fetchone()
        if not row:
            return False

        status = row[0]
        if status in (STATUS_ACTIVE, STATUS_MONITORING):
            # Need to revert config
            cv = db.execute("""
                SELECT config_type, config_key, value_before
                FROM ab_config_versions WHERE test_id = ? AND reverted = 0
                ORDER BY snapshot_time DESC LIMIT 1
            """, (test_id,)).fetchone()
            if cv:
                _apply_config(cv[0], cv[1], json.loads(cv[2]))
                db.execute("""
                    UPDATE ab_config_versions SET reverted = 1, revert_time = ?
                    WHERE test_id = ? AND reverted = 0
                """, (time.time(), test_id))

        db.execute("""
            UPDATE hook_ab_tests SET status = ?, degradation_metrics = ?,
                   ended_at = CURRENT_TIMESTAMP
            WHERE test_id = ?
        """, (STATUS_VETOED, reason, test_id))
        db.commit()
        logger.info(f"A/B {test_id}: VETOED — {reason}")
        return True
    except Exception as e:
        logger.error(f"veto_test error: {e}")
        return False
    finally:
        db.close()


# --- Scheduler Job Wrappers ---

def job_scan_candidates():
    """Scheduler job: scan evidence for A/B test candidates. Hourly."""
    budget = BudgetManager()
    if budget.remaining() < MIN_BUDGET_FOR_TEST:
        return {"status": "skipped", "reason": "budget_low"}

    active = get_active_test()
    if active:
        return {"status": "skipped", "reason": f"test_active:{active.test_id}"}

    candidates = scan_for_candidates(max_candidates=3)
    if not candidates:
        return {"status": "ok", "candidates": 0}

    # Design test from top candidate
    test = design_test(candidates[0], budget)
    if not test:
        return {"status": "ok", "candidates": len(candidates), "designed": False}

    # Request consensus
    consensus = request_consensus(test, budget)
    if consensus.get("approved"):
        queued = queue_test(test, consensus)
        return {"status": "queued" if queued else "queue_failed",
                "test_id": test.test_id, "consensus": consensus.get("consensus_status")}

    return {"status": "rejected", "consensus": consensus.get("consensus_status"),
            "dissent": consensus.get("dissent", [])}


def job_activate_approved():
    """Scheduler job: activate tests past grace period. Every 5 min."""
    test = get_active_test()
    if not test:
        return {"status": "no_test"}

    if test.status == STATUS_GRACE:
        if time.time() >= test.grace_until:
            activated = activate_test(test)
            return {"status": "activated" if activated else "activation_failed",
                    "test_id": test.test_id}
        else:
            remaining = int(test.grace_until - time.time())
            return {"status": "grace_period", "remaining_s": remaining}

    return {"status": "already_active", "test_id": test.test_id}


def job_monitor_active():
    """Scheduler job: monitor active test for degradation. Every 15 min."""
    test = get_active_test()
    if not test or test.status not in (STATUS_ACTIVE, STATUS_MONITORING):
        return {"status": "no_active_test"}

    result = monitor_test(test)

    if result == "degraded":
        reverted = auto_revert(test)
        return {"status": "reverted" if reverted else "revert_failed",
                "reason": test.degradation_reason}
    elif result == "expired":
        budget = BudgetManager()
        conclusion = conclude_test(test, budget)
        return {"status": "concluded", "winner": conclusion.get("winner")}

    return {"status": "ok", "test_id": test.test_id}


def job_conclude_ready():
    """Scheduler job: conclude tests that are ready. Hourly."""
    test = get_active_test()
    if not test or test.status not in (STATUS_ACTIVE, STATUS_MONITORING):
        return {"status": "no_test_to_conclude"}

    # Check if test has been running long enough (at least 4 hours for meaningful data)
    elapsed = 0
    if test.activated_at > 0:
        elapsed = (time.time() - test.activated_at) / 3600
        if elapsed >= test.max_duration_hours:
            budget = BudgetManager()
            conclusion = conclude_test(test, budget)
            return {"status": "concluded", "winner": conclusion.get("winner")}

    return {"status": "not_ready", "elapsed_hours": elapsed}


def job_safety_check():
    """Scheduler job: safety check — revert on sustained degradation. Every 5 min."""
    test = get_active_test()
    if not test or test.status not in (STATUS_ACTIVE, STATUS_MONITORING):
        return {"status": "safe"}

    # Quick metrics check — no LLM needed
    current = _capture_metrics()

    # Hard safety limits (stricter than degradation thresholds)
    if current.get("webhook_e2e_p95_ms", 0) > 15000:
        test.current_metrics = current
        test.degradation_reason = f"SAFETY: webhook_e2e_p95={current['webhook_e2e_p95_ms']:.0f}ms (>15s)"
        reverted = auto_revert(test)
        return {"status": "emergency_revert", "reason": test.degradation_reason}

    if current.get("llm_error_rate_pct", 0) > 30:
        test.current_metrics = current
        test.degradation_reason = f"SAFETY: llm_error_rate={current['llm_error_rate_pct']:.1f}% (>30%)"
        reverted = auto_revert(test)
        return {"status": "emergency_revert", "reason": test.degradation_reason}

    return {"status": "safe", "test_id": test.test_id}


def job_auto_validate():
    """Scheduler job: detect pending fix validations and run 3-surgeon consensus.

    Trigger: Redis key 'surgery:pending_validation' set by post-commit hook or manual trigger.
    Pipeline: gains-gate → ab-validate (3-surgeon unanimous) → verdict → cleanup signal.
    Runs every 5 min. If no pending validation, short-circuits immediately.
    """
    rc = _get_redis()
    if not rc:
        return {"status": "skipped", "reason": "no_redis"}

    # Check for pending validation signal
    pending = rc.get("surgery:pending_validation")
    if not pending:
        return {"status": "no_pending"}

    # Parse pending validation
    try:
        data = json.loads(pending)
        description = data.get("description", "unknown fix")
        commit_hash = data.get("commit", "")
        requested_at = data.get("requested_at", 0)
    except (json.JSONDecodeError, AttributeError):
        description = str(pending)
        commit_hash = ""
        requested_at = 0

    # Stale check: skip if older than 1 hour
    if requested_at and (time.time() - requested_at) > 3600:
        rc.delete("surgery:pending_validation")
        return {"status": "stale", "age_s": time.time() - requested_at}

    # Run ab-validate via surgery_bridge (direct import or subprocess fallback)
    # REVERSIBILITY: old subprocess path preserved below — uncomment to revert
    # import subprocess
    # result = subprocess.run(
    #     [sys.executable, str(REPO_ROOT / "scripts" / "surgery-team.py"),
    #      "ab-validate", description],
    #     capture_output=True, text=True, timeout=300,
    #     cwd=str(REPO_ROOT),
    #     env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    # )
    # output = result.stdout + result.stderr
    try:
        from memory.surgery_bridge import ab_validate as _bridge_ab_validate
        bridge_result = _bridge_ab_validate(description)

        # Parse verdict from bridge output
        verdict = "unknown"
        output = bridge_result.get("output", "")
        if isinstance(output, str):
            for line in output.split("\n"):
                if "VERDICT:" in line:
                    if "KEEP" in line:
                        verdict = "keep"
                    elif "REVERT" in line:
                        verdict = "revert"
                    elif "FLAG" in line:
                        verdict = "flag"
                    elif "BLOCKED" in line:
                        verdict = "blocked"
                    break
        elif isinstance(output, dict):
            verdict = output.get("verdict", "unknown")

        # Store result and clean up pending signal
        validation_result = {
            "description": description,
            "commit": commit_hash,
            "verdict": verdict,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": bridge_result.get("path", "unknown"),
        }
        rc.set("surgery:last_auto_validation", json.dumps(validation_result), ex=86400 * 7)
        rc.delete("surgery:pending_validation")

        # Notify based on verdict
        if verdict == "keep":
            _notify_test_event(None, "auto_validated",
                               f"Fix auto-validated: KEEP",
                               f"3-surgeon consensus KEEP for: {description[:80]}")
        elif verdict == "revert":
            _notify_test_event(None, "auto_validated",
                               f"Fix auto-validated: REVERT",
                               f"3-surgeon consensus REVERT for: {description[:80]}. "
                               f"Aaron: manual rollback needed.")
        elif verdict in ("flag", "blocked"):
            _notify_test_event(None, "auto_validated",
                               f"Fix needs review: {verdict.upper()}",
                               f"3-surgeon {verdict.upper()} for: {description[:80]}. "
                               f"Aaron: manual review needed.")

        return {"status": "validated", "verdict": verdict, "description": description[:80]}

    except Exception as e:
        logger.error(f"auto_validate failed: {e}")
        rc.delete("surgery:pending_validation")
        return {"status": "error", "error": str(e)}


def trigger_auto_validate(description: str, commit_hash: str = ""):
    """Set the pending validation signal in Redis. Called by post-commit hooks or manually."""
    rc = _get_redis()
    if not rc:
        return False
    data = json.dumps({
        "description": description,
        "commit": commit_hash,
        "requested_at": time.time(),
    })
    rc.set("surgery:pending_validation", data, ex=3600)  # 1h TTL
    return True


# --- Status / CLI ---

def get_status() -> Dict[str, Any]:
    """Get full A/B testing status for display."""
    budget = BudgetManager()
    active = get_active_test()
    queued = get_queued_tests()

    # Recent history
    db = _get_db()
    history = []
    try:
        rows = db.execute("""
            SELECT test_id, test_name, status, hypothesis, winner_variant_id,
                   created_at, ended_at, auto_revert_triggered
            FROM hook_ab_tests
            WHERE consensus_json IS NOT NULL
            ORDER BY created_at DESC LIMIT 10
        """).fetchall()
        for r in rows:
            history.append({
                "test_id": r[0], "name": r[1], "status": r[2],
                "hypothesis": r[3], "winner": r[4],
                "created": r[5], "ended": r[6], "reverted": bool(r[7]),
            })
    except Exception:
        pass
    finally:
        db.close()

    return {
        "budget_remaining": budget.remaining(),
        "budget_spent_today": budget.spent_today(),
        "active_test": asdict(active) if active else None,
        "queued_count": len(queued),
        "history": history,
    }


# --- Helpers ---

def _ensure_env():
    """Load .env for external LLM keys (DeepSeek primary, OpenAI optional).

    No-ops if a DeepSeek or OpenAI key is already in the environment.
    """
    if (os.environ.get("Context_DNA_Deep_Seek")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("Context_DNA_OPENAI")
            or os.environ.get("OPENAI_API_KEY")):
        return
    env_path = REPO_ROOT / "context-dna" / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()


def _extract_json(text: str) -> Optional[Dict]:
    """Extract JSON from LLM response text."""
    if not text:
        return None
    # Strip markdown code fences
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()
    try:
        if "{" in text:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _calc_cost(usage, model: str) -> float:
    """Calculate cost from token usage."""
    if not usage:
        return 0.0
    pricing = {
        "gpt-4.1": (2.0, 8.0),
        "gpt-4.1-mini": (0.4, 1.6),
        "gpt-4.1-nano": (0.1, 0.4),
        "deepseek-chat": (0.28, 1.10),
        "deepseek-reasoner": (0.55, 2.19),
    }
    in_price, out_price = pricing.get(model, (0.4, 1.6))
    return (usage.prompt_tokens * in_price + usage.completion_tokens * out_price) / 1_000_000


def _capture_metrics() -> Dict[str, Any]:
    """Capture current health metrics from Redis for comparison."""
    metrics = {}
    try:
        from memory.redis_cache import get_section_latency_stats
        total_stats = get_section_latency_stats("total_critical")
        metrics["webhook_e2e_p95_ms"] = total_stats.get("p95", 0)
        metrics["webhook_e2e_avg_ms"] = total_stats.get("avg", 0)
    except Exception:
        pass

    rc = _get_redis()
    if rc:
        try:
            # LLM error rate from recent scheduler data
            llm_errors = int(rc.get("scheduler:job_failures:llm_recent") or 0)
            llm_total = int(rc.get("scheduler:job_runs:llm_recent") or 0)
            metrics["llm_error_rate_pct"] = (llm_errors / max(llm_total, 1)) * 100

            # Scheduler failure rate
            sched_fails = int(rc.get("scheduler:failures:recent") or 0)
            sched_total = int(rc.get("scheduler:runs:recent") or 0)
            metrics["scheduler_failure_rate_pct"] = (sched_fails / max(sched_total, 1)) * 100
        except Exception:
            pass

    metrics["captured_at"] = time.time()
    return metrics


def _get_current_value(config_type: str, config_key: str) -> Any:
    """Get the current value of a config parameter."""
    rc = _get_redis()
    if rc:
        try:
            val = rc.get(f"config:{config_type}:{config_key}")
            if val:
                try:
                    return json.loads(val)
                except json.JSONDecodeError:
                    return val
        except Exception:
            pass
    return None


def _apply_config(config_type: str, config_key: str, value: Any) -> bool:
    """Apply a config value. Returns True on success."""
    rc = _get_redis()
    if not rc:
        logger.error("Cannot apply config: Redis unavailable")
        return False
    try:
        key = f"config:{config_type}:{config_key}"
        rc.set(key, json.dumps(value) if not isinstance(value, str) else value)
        logger.info(f"Config applied: {key} = {value}")
        return True
    except Exception as e:
        logger.error(f"Config apply failed: {e}")
        return False


def _notify_test_event(test: AutonomousTest, event_type: str, title: str, message: str):
    """Send macOS notification for A/B test event."""
    try:
        from memory.notification_manager import send_notification, NotificationCategory
        cat_map = {
            "proposed": NotificationCategory.AB_TEST_PROPOSED,
            "activated": NotificationCategory.AB_TEST_ACTIVATED,
            "degraded": NotificationCategory.AB_TEST_DEGRADED,
            "reverted": NotificationCategory.AB_TEST_REVERTED,
            "concluded": NotificationCategory.AB_TEST_CONCLUDED,
        }
        category = cat_map.get(event_type, NotificationCategory.INFO)
        send_notification(title, message, category=category)
    except Exception as e:
        logger.warning(f"Notification failed: {e}")


def _record_evidence(test: AutonomousTest, evidence_type: str, content: str):
    """Write test result back to evidence store."""
    try:
        from memory.learning_store import get_learning_store, build_learning_data
        store = get_learning_store()
        learning = build_learning_data(
            learning_type=evidence_type,
            title=f"A/B: {test.test_id}",
            content=content,
            tags=["ab_test", test.config_type, test.config_key],
            source="ab_autonomous",
        )
        store.store_learning(learning)
    except Exception as e:
        logger.warning(f"Evidence recording failed: {e}")


# --- CLI ---

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if len(sys.argv) < 2:
        print("Usage: python memory/ab_autonomous.py <command>")
        print("  status     — Show A/B testing status")
        print("  scan       — Scan for test candidates")
        print("  veto <id>  — Veto a test")
        print("  history    — Show test history")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        s = get_status()
        print(json.dumps(s, indent=2, default=str))

    elif cmd == "scan":
        candidates = scan_for_candidates()
        for c in candidates:
            print(f"  [{c.get('_ab_score', 0)}] {c.get('title', '?')}")
        print(f"\n{len(candidates)} candidates found")

    elif cmd == "veto" and len(sys.argv) > 2:
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "manual veto"
        success = veto_test(sys.argv[2], reason)
        print(f"Veto {'succeeded' if success else 'failed'}")

    elif cmd == "history":
        s = get_status()
        for h in s.get("history", []):
            rev = " [REVERTED]" if h.get("reverted") else ""
            print(f"  {h['status']:12s} {h['test_id']}  {h.get('hypothesis', '')[:60]}{rev}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
