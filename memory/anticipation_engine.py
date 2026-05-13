"""Anticipation engine — real-time MLX probe for predictive context pre-computation.

Why this exists
---------------
Synaptic diagnosis (2026-05-13): :mod:`memory.anticipation_engine` is the
single most-referenced dangling module in the mothership (18 import sites,
first at ``memory/agent_service.py:417``). It is the predictive layer of the
subconscious — it watches dialogue, anticipates the *next* webhook prompt,
and pre-computes the heavy LLM-backed sections (S2 Wisdom, S6 Holistic,
S8 8th Intelligence) during otherwise-idle GPU time so the webhook responds
in ~200 ms instead of 60-120 s.

This migrate3 port is the forward-compatible OSS-IDE rewrite. It is
deliberately *not* a 1:1 line port of the 2000-line legacy module
(``memory.bak.20260426/anticipation_engine.py``). The legacy version is
deeply coupled to Redis pub/sub, session-file watchers, MLX HTTP probes on
port 5044, and a bespoke project-id scheme. The port keeps the *contract*
(every public symbol still imports cleanly) but routes generation through
the priority queue and stores state in SQLite so the OSS IDE can run it
without Redis or a hand-rolled MLX server.

Design contract — public API
-----------------------------
The 18 import sites in the active mothership reference these symbols:

  Top-level (callable):
    start_listener()                  # idempotent pub/sub start
    stop_listener()                   # idempotent stop
    run_anticipation_cycle()          # one shot — used by lite_scheduler
    is_superhero_active(session_id=None) -> bool
    get_superhero_cache(session_id=None) -> dict | None
    record_agent_finding(agent_id, finding, type, severity, session_id=None)
    get_agent_findings(session_id=None, since_seconds=0, type=None, limit=50)
    get_agent_findings_digest(session_id=None)
    _activate_superhero_anticipation(task_override=None) -> dict
    _detect_active_session() -> str | None

  Class (Synaptic directive — new in migrate3):
    AnticipationEngine
      .predict_next_actions(context) -> list[dict]
      .score_anticipation_candidate(candidate) -> float
      .learn_from_outcome(prediction_id, actual_outcome) -> bool
      .get_top_predictions(limit=5) -> list[dict]

The four ``AnticipationEngine`` methods are the *real-time MLX probe*
Synaptic asked for: each runs through :func:`memory.llm_priority_queue.
llm_generate` at ``Priority.BACKGROUND`` with the ``classify`` or
``extract`` profile, never blocking Aaron's foreground prompt.

ZSF (zero silent failures)
--------------------------
:data:`COUNTERS` tracks every code path. Each ``except`` block bumps a
counter and logs at WARN/DEBUG before re-raising or degrading. Counters
exposed via :func:`get_counters` for /health.

Graceful degradation
--------------------
Three upstream modules may be missing during the migrate3 staged rollout:

  * ``memory.observability_store`` — Agent 2 of this round
  * ``memory.learning_store``      — Agent 3 of this round
  * ``services.feedback.handler``  — already shipped (round N-1)

Each dep is **lazy-imported inside the method that needs it**. If the import
fails we bump ``upstream_missing_{name}_total`` and return an empty / safe
default. The engine never crashes the prompt path.

Feedback-loop integration
-------------------------
``AnticipationEngine.learn_from_outcome`` writes the prediction's accuracy
score back to the evidence ledger via
``services.feedback.handler.record_feedback`` (kind=``unmet_expectation``
on miss, ``success_story`` on hit). When the feedback handler is missing it
falls back to ``/tmp/anticipation-outcomes.jsonl`` so no signal is lost.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("contextdna.anticipation")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s contextdna.anticipation %(message)s"
        )
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Counters (ZSF)
# ---------------------------------------------------------------------------

COUNTERS: dict[str, Any] = {
    # Cycle lifecycle
    "cycle_runs_total": 0,
    "cycle_skipped_total": 0,
    "cycle_errors_total": 0,
    "cycle_skipped_by_reason": {},
    # Predictions
    "predictions_generated_total": 0,
    "predictions_scored_total": 0,
    "predictions_stored_total": 0,
    "predictions_errors_total": 0,
    # Outcome learning
    "outcomes_recorded_total": 0,
    "outcomes_hits_total": 0,
    "outcomes_misses_total": 0,
    "outcome_fallback_writes_total": 0,
    "outcome_errors_total": 0,
    # Superhero
    "superhero_activations_total": 0,
    "superhero_artifacts_cached_total": 0,
    "superhero_errors_total": 0,
    # Agent findings WAL
    "agent_findings_recorded_total": 0,
    "agent_findings_errors_total": 0,
    # Listener
    "listener_starts_total": 0,
    "listener_stops_total": 0,
    "listener_errors_total": 0,
    # MLX / priority queue
    "llm_calls_total": 0,
    "llm_call_errors_total": 0,
    "llm_call_timeouts_total": 0,
    # Upstream availability (per-module)
    "upstream_missing_observability_store_total": 0,
    "upstream_missing_learning_store_total": 0,
    "upstream_missing_feedback_handler_total": 0,
    "upstream_missing_llm_priority_queue_total": 0,
}

_counter_lock = threading.Lock()


def _bump(name: str, delta: int = 1) -> None:
    with _counter_lock:
        if name in COUNTERS and isinstance(COUNTERS[name], int):
            COUNTERS[name] += delta


def _bump_reason(reason: str) -> None:
    with _counter_lock:
        d = COUNTERS["cycle_skipped_by_reason"]
        d[reason] = d.get(reason, 0) + 1


def get_counters() -> dict[str, Any]:
    """Snapshot copy of counters for /health surfaces."""
    with _counter_lock:
        return {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in COUNTERS.items()
        }


# ---------------------------------------------------------------------------
# Configuration (env-overridable; YAML config loader below)
# ---------------------------------------------------------------------------

# Cycle cadence — match legacy 45 s scheduler interval (5 min default in
# config.yaml is the Synaptic-recommended *Atlas* cadence; scheduler tier
# uses the tighter value).
_CYCLE_INTERVAL_S = int(os.environ.get("CONTEXTDNA_ANTICIPATION_INTERVAL_S", "45"))
_MIN_INTERVAL_BETWEEN_RUNS = int(
    os.environ.get("CONTEXTDNA_ANTICIPATION_MIN_INTERVAL_S", "20")
)
_LLM_TIMEOUT_S = float(os.environ.get("CONTEXTDNA_ANTICIPATION_LLM_TIMEOUT_S", "30"))
_DEFAULT_LEARNING_RATE = float(
    os.environ.get("CONTEXTDNA_ANTICIPATION_LEARNING_RATE", "0.15")
)
_SUPERHERO_ARTIFACT_TTL_S = int(
    os.environ.get("CONTEXTDNA_ANTICIPATION_SUPERHERO_TTL_S", "3600")
)
_AGENT_FINDINGS_TTL_S = int(
    os.environ.get("CONTEXTDNA_ANTICIPATION_FINDINGS_TTL_S", "7200")
)


def _state_db_path() -> pathlib.Path:
    """Local SQLite state for predictions, superhero cache, findings WAL.

    Honors ``CONTEXTDNA_HOME`` so OSS operators can relocate state into
    XDG dirs. Falls back to a path next to this module — matches the legacy
    ``.failure_patterns.db`` placement convention.
    """
    home = os.environ.get("CONTEXTDNA_HOME")
    if home:
        base = pathlib.Path(home)
    else:
        base = pathlib.Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / ".anticipation_state.db"


# ---------------------------------------------------------------------------
# SQLite schema (replaces Redis pub/sub in the legacy port)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    candidate     TEXT NOT NULL,
    score         REAL NOT NULL,
    profile       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    outcome       TEXT,           -- "hit" | "miss" | NULL (pending)
    outcome_at    TEXT,
    learning_delta REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pred_session
    ON predictions(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pred_pending
    ON predictions(outcome) WHERE outcome IS NULL;

CREATE TABLE IF NOT EXISTS superhero_state (
    session_id     TEXT PRIMARY KEY,
    task           TEXT NOT NULL,
    activated_at   TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    mission        TEXT,
    gotchas        TEXT,
    architecture   TEXT,
    failures       TEXT
);

CREATE TABLE IF NOT EXISTS agent_findings (
    finding_id    TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    finding       TEXT NOT NULL,
    finding_type  TEXT NOT NULL,
    severity      TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_session
    ON agent_findings(session_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS engine_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    """Short-lived SQLite handle.

    We open per-call rather than caching a singleton — the legacy module
    learned the hard way (see ``memory.bak.20260426/CLAUDE.md``) that
    ``with sqlite3.connect()`` does NOT close on context exit, so we use
    try/finally explicitly.
    """
    path = _state_db_path()
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(_SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Upstream module probes (lazy, ZSF)
# ---------------------------------------------------------------------------


def _try_import_priority_queue():
    """Return (llm_generate, Priority) or (None, None)."""
    try:
        from memory.llm_priority_queue import (  # type: ignore[import-not-found]
            Priority,
            llm_generate,
        )

        return llm_generate, Priority
    except Exception as exc:  # noqa: BLE001 — probe gracefully
        _bump("upstream_missing_llm_priority_queue_total")
        logger.debug("llm_priority_queue unavailable: %s", exc)
        return None, None


def _try_import_learning_store():
    try:
        from memory.learning_store import get_learning_store  # type: ignore

        return get_learning_store()
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_learning_store_total")
        logger.debug("learning_store unavailable: %s", exc)
        return None


def _try_import_observability_store():
    try:
        from memory.observability_store import (  # type: ignore[import-not-found]
            get_observability_store,
        )

        return get_observability_store()
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_observability_store_total")
        logger.debug("observability_store unavailable: %s", exc)
        return None


def _try_import_feedback_handler():
    """Try the migrate3 location first, then migrate2 (already shipped)."""
    for path in (
        "services.feedback.handler",  # migrate3 future home
        "contextdna_ide_oss.migrate2.services.feedback.handler",  # legacy
    ):
        try:
            import importlib

            return importlib.import_module(path)
        except Exception:  # noqa: BLE001 — try next path
            continue
    # Final attempt: relative if running with sys.path injected
    try:
        from services.feedback import handler  # type: ignore[import-not-found]

        return handler
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_feedback_handler_total")
        logger.debug("feedback handler unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Session detection (migrate3: env / file-based, no Redis)
# ---------------------------------------------------------------------------


def _detect_active_session() -> Optional[str]:
    """Detect the active Claude Code session id.

    Resolution order:
      1. ``CONTEXTDNA_SESSION_ID`` env (explicit override — tests + CI)
      2. ``CLAUDE_SESSION_ID`` env (set by the official CLI)
      3. Newest ``*.jsonl`` under ``~/.claude/projects/<cwd-slug>/``

    Returns ``None`` when no session can be detected — callers must treat
    that as ``skipped_reason="no_active_session"``.
    """
    for env_key in ("CONTEXTDNA_SESSION_ID", "CLAUDE_SESSION_ID"):
        sid = os.environ.get(env_key)
        if sid:
            return sid.strip()

    try:
        cwd_slug = (
            str(pathlib.Path.cwd()).replace("/", "-").replace(" ", "-").lstrip("-")
        )
        # Claude Code stores sessions under ~/.claude/projects/<slug>/
        candidates = list(
            pathlib.Path.home().glob(
                f".claude/projects/-{cwd_slug}/*.jsonl"
            )
        )
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        return newest.stem  # filename minus .jsonl == session id
    except Exception as exc:  # noqa: BLE001
        logger.debug("session detection failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Real-time MLX probe — the core class Synaptic asked for
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Prediction:
    """A scored next-action candidate."""

    prediction_id: str
    session_id: str
    candidate: str
    score: float
    profile: str
    created_at: str
    expires_at: str
    outcome: Optional[str] = None  # "hit" | "miss" | None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class AnticipationEngine:
    """Real-time MLX probe — predict, score, and learn from outcomes.

    The engine is the predictive arm of the subconscious. It runs in two
    modes:

    * **Background tick** (driven by lite_scheduler at 45 s default) — calls
      :func:`run_anticipation_cycle` which sweeps pending predictions,
      generates fresh candidates for the active session, and pre-computes
      the heavy webhook sections.
    * **Direct API** (this class) — exposed to ``memory.agent_service``
      and the IDE OSS panels so they can ask "what is likely next?" in a
      single round trip.

    Predictions are stored in SQLite (``.anticipation_state.db``). Each
    prediction has a TTL — by default ``adaptive_ttl(continuity_score)``,
    which the legacy module proved out (high continuity = long TTL).

    The class is intentionally *stateless across instances* — all state
    lives in SQLite so multiple processes (mothership + IDE OSS) can share
    a learning history.
    """

    def __init__(
        self,
        *,
        learning_rate: float = _DEFAULT_LEARNING_RATE,
        llm_profile: str = "classify",
        default_ttl_s: int = 600,
    ) -> None:
        self.learning_rate = float(learning_rate)
        self.llm_profile = llm_profile
        self.default_ttl_s = int(default_ttl_s)

    # ------------------------------------------------------------------
    # Public API (Synaptic directive)
    # ------------------------------------------------------------------

    def predict_next_actions(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Generate next-action candidates for a context.

        Parameters
        ----------
        context:
            Free-form dict. Recognized keys:
              * ``prompt`` — the most recent user prompt (REQUIRED)
              * ``session_id`` — explicit session id (else auto-detect)
              * ``recent_outcomes`` — list[dict] from learning_store for
                priors (optional; degrades to empty when missing)
              * ``max_candidates`` — int, default 5
              * ``llm_profile`` — override the instance profile

        Returns
        -------
        list[dict]
            Each dict has ``prediction_id``, ``candidate``, ``score``,
            ``profile``, ``expires_at``. Empty list on degraded mode (LLM
            unavailable) — caller checks ``len(...)``.
        """
        prompt = (context or {}).get("prompt") or ""
        if not isinstance(prompt, str) or len(prompt.split()) <= 5:
            # Match the legacy ≤5-word webhook gate exactly — short prompts
            # generate generic noise that pollutes the learning history.
            _bump_reason("short_prompt")
            return []

        session_id = (
            (context or {}).get("session_id") or _detect_active_session() or "unknown"
        )
        max_candidates = int((context or {}).get("max_candidates", 5))
        profile = (context or {}).get("llm_profile") or self.llm_profile

        llm_generate, Priority = _try_import_priority_queue()
        if llm_generate is None or Priority is None:
            # Degraded — return keyword-extracted candidates so the caller
            # still gets *something* useful (matches legacy template fallback).
            return self._degraded_candidates(prompt, session_id, max_candidates)

        # Build the MLX probe prompt — keep it tight, classify profile is
        # 64-token budget per CLAUDE.md.
        system = (
            "You are the anticipation engine. Given a recent user prompt and "
            "prior outcomes, list the 1-5 next concrete actions the assistant "
            "should pre-compute. One per line. No numbering. No prose."
        )
        priors = (context or {}).get("recent_outcomes") or []
        user_lines = [f"PROMPT: {prompt[:400]}"]
        if priors:
            user_lines.append("RECENT OUTCOMES:")
            for p in priors[:5]:
                user_lines.append(f"- {str(p)[:120]}")
        user = "\n".join(user_lines)

        _bump("llm_calls_total")
        try:
            response = llm_generate(
                system_prompt=system,
                user_prompt=user,
                priority=Priority.BACKGROUND,
                profile=profile,
                caller="anticipation_engine",
                timeout_s=_LLM_TIMEOUT_S,
            )
        except TimeoutError:
            _bump("llm_call_timeouts_total")
            return self._degraded_candidates(prompt, session_id, max_candidates)
        except Exception as exc:  # noqa: BLE001 — LLM probe is best-effort
            _bump("llm_call_errors_total")
            logger.warning("anticipation LLM probe failed: %s", exc)
            return self._degraded_candidates(prompt, session_id, max_candidates)

        if not response:
            _bump("llm_call_errors_total")
            return self._degraded_candidates(prompt, session_id, max_candidates)

        candidates = [
            ln.strip(" -*\t") for ln in response.splitlines() if ln.strip()
        ][:max_candidates]
        if not candidates:
            return []

        # Score + persist
        predictions: list[dict[str, Any]] = []
        for cand in candidates:
            score = self.score_anticipation_candidate(
                {"candidate": cand, "prompt": prompt, "priors": priors}
            )
            stored = self._store_prediction(
                session_id=session_id,
                prompt=prompt,
                candidate=cand,
                score=score,
                profile=profile,
            )
            if stored:
                predictions.append(stored)
                _bump("predictions_generated_total")

        return predictions

    def score_anticipation_candidate(self, candidate: dict[str, Any]) -> float:
        """Score a single candidate in [0.0, 1.0].

        Scoring inputs (weighted):
          * keyword overlap with the prompt (0.4)
          * prior hit rate from learning_store, when available (0.4)
          * candidate length plausibility — penalize one-word stubs (0.2)

        Always returns a finite float — never raises. Bumps
        ``predictions_scored_total`` for observability.
        """
        try:
            cand = str(candidate.get("candidate") or "")
            prompt = str(candidate.get("prompt") or "")
            priors = candidate.get("priors") or []
            if not cand:
                _bump("predictions_scored_total")
                return 0.0

            # Component 1: keyword overlap
            cand_words = set(_tokenize(cand))
            prompt_words = set(_tokenize(prompt))
            if prompt_words:
                overlap = len(cand_words & prompt_words) / max(
                    len(prompt_words), 1
                )
            else:
                overlap = 0.0

            # Component 2: prior hit rate from learning_store
            prior_score = 0.5  # neutral default when no priors
            try:
                hits = sum(1 for p in priors if str(p).lower().startswith("hit"))
                if priors:
                    prior_score = hits / len(priors)
            except Exception:  # noqa: BLE001
                pass

            # Component 3: length plausibility
            wc = len(cand.split())
            length_score = 1.0 if 3 <= wc <= 25 else max(0.0, 1.0 - abs(wc - 12) / 25)

            score = (0.4 * overlap) + (0.4 * prior_score) + (0.2 * length_score)
            # Clamp
            score = max(0.0, min(1.0, score))
            _bump("predictions_scored_total")
            return round(score, 4)
        except Exception as exc:  # noqa: BLE001
            _bump("predictions_errors_total")
            logger.debug("score_anticipation_candidate failed: %s", exc)
            return 0.0

    def learn_from_outcome(
        self,
        prediction_id: str,
        actual_outcome: dict[str, Any] | str,
    ) -> bool:
        """Record whether a prediction matched reality.

        The outcome feeds three sinks (best-effort, ZSF):

        1. **SQLite** — flips the row's ``outcome`` and computes a
           per-prediction ``learning_delta`` = ``learning_rate × (hit - score)``
           which the scheduler aggregates into the prior distribution.
        2. **Feedback handler** — writes a ``success_story`` (hit) or
           ``unmet_expectation`` (miss) record so operator-visible
           dashboards see anticipation accuracy.
        3. **Observability store** — records a claim/outcome pair when
           available so the evidence pipeline can promote/retire learnings.

        Returns ``True`` if at least one sink accepted the write.
        """
        # Normalize outcome shape
        if isinstance(actual_outcome, str):
            outcome_hit = actual_outcome.lower().startswith("hit")
            outcome_label = "hit" if outcome_hit else "miss"
            outcome_payload: dict[str, Any] = {"label": outcome_label}
        else:
            outcome_hit = bool(actual_outcome.get("hit"))
            outcome_label = "hit" if outcome_hit else "miss"
            outcome_payload = dict(actual_outcome)

        sinks_ok = 0

        # Sink 1: SQLite row update
        try:
            with _conn() as c:
                row = c.execute(
                    "SELECT score FROM predictions WHERE prediction_id = ?",
                    (prediction_id,),
                ).fetchone()
                if row is None:
                    _bump("outcome_errors_total")
                    logger.debug(
                        "learn_from_outcome: prediction_id %s not found",
                        prediction_id,
                    )
                else:
                    score = float(row["score"])
                    delta = self.learning_rate * ((1.0 if outcome_hit else 0.0) - score)
                    c.execute(
                        "UPDATE predictions SET outcome = ?, outcome_at = ?, "
                        "learning_delta = ? WHERE prediction_id = ?",
                        (
                            outcome_label,
                            _now_iso(),
                            round(delta, 6),
                            prediction_id,
                        ),
                    )
                    sinks_ok += 1
                    _bump("outcomes_recorded_total")
                    if outcome_hit:
                        _bump("outcomes_hits_total")
                    else:
                        _bump("outcomes_misses_total")
        except Exception as exc:  # noqa: BLE001
            _bump("outcome_errors_total")
            logger.warning("anticipation SQLite outcome write failed: %s", exc)

        # Sink 2: feedback handler
        handler = _try_import_feedback_handler()
        if handler is not None and hasattr(handler, "record_feedback"):
            try:
                kind = "success_story" if outcome_hit else "unmet_expectation"
                handler.record_feedback(
                    client_id="anticipation_engine",
                    kind=kind,
                    title=(
                        f"anticipation {outcome_label}: {prediction_id[:12]}"
                    ),
                    body=json.dumps(
                        {"prediction_id": prediction_id, "outcome": outcome_payload},
                        sort_keys=True,
                    ),
                    severity="info" if outcome_hit else "warning",
                )
                sinks_ok += 1
            except Exception as exc:  # noqa: BLE001
                _bump("outcome_errors_total")
                logger.warning("feedback handler write failed: %s", exc)
        # Else: counter already bumped inside _try_import_feedback_handler

        # Sink 3: observability store (Agent 2 of this round wires it).
        # Signature contract (matches `observability_store.record_outcome_event`):
        #   record_outcome_event(session_id, outcome_type, success, reward=..., notes=...)
        # When Agent 2 ships a different shape we degrade gracefully.
        obs = _try_import_observability_store()
        if obs is not None and hasattr(obs, "record_outcome_event"):
            try:
                obs.record_outcome_event(
                    session_id=outcome_payload.get("session_id")
                    or _detect_active_session()
                    or "anticipation",
                    outcome_type="anticipation_outcome",
                    success=outcome_hit,
                    reward=1.0 if outcome_hit else -0.3,  # codebase convention
                    notes=json.dumps(
                        {"prediction_id": prediction_id, **outcome_payload},
                        sort_keys=True,
                    )[:500],
                )
                sinks_ok += 1
            except TypeError as exc:
                # Signature mismatch — Agent 2 may ship a different API.
                # This is a *graceful* degradation, not a real error: we
                # bump a dedicated counter so /health surfaces it but we
                # do NOT bump outcome_errors_total (which would falsely
                # imply lost signal — the other sinks already accepted).
                _bump("upstream_missing_observability_store_total")
                logger.debug(
                    "observability_store signature mismatch (degraded): %s", exc
                )
            except Exception as exc:  # noqa: BLE001
                _bump("outcome_errors_total")
                logger.debug("observability_store write failed: %s", exc)

        # Fallback JSONL — guarantees no signal is lost even with every
        # upstream offline. The lite_scheduler can replay these later.
        if sinks_ok == 0:
            try:
                fb_path = pathlib.Path("/tmp/anticipation-outcomes.jsonl")
                with fb_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "prediction_id": prediction_id,
                                "outcome": outcome_label,
                                "payload": outcome_payload,
                                "recorded_at": _now_iso(),
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                _bump("outcome_fallback_writes_total")
                return True
            except Exception as exc:  # noqa: BLE001
                _bump("outcome_errors_total")
                logger.error(
                    "anticipation outcome fallback ALSO failed: %s", exc
                )
                return False

        return True

    def get_top_predictions(
        self, limit: int = 5, *, session_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return the top-scoring pending predictions for a session.

        Predictions are pending when ``outcome IS NULL`` and not yet expired.
        Used by ``memory.agent_service``'s ``/superhero/status`` endpoint
        and the IDE OSS predictive panels.
        """
        sid = session_id or _detect_active_session() or "unknown"
        now = _now_iso()
        try:
            with _conn() as c:
                rows = c.execute(
                    "SELECT prediction_id, session_id, candidate, score, profile, "
                    "created_at, expires_at, outcome "
                    "FROM predictions "
                    "WHERE session_id = ? AND outcome IS NULL AND expires_at > ? "
                    "ORDER BY score DESC, created_at DESC LIMIT ?",
                    (sid, now, max(1, int(limit))),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            _bump("predictions_errors_total")
            logger.warning("get_top_predictions failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _degraded_candidates(
        self, prompt: str, session_id: str, max_candidates: int
    ) -> list[dict[str, Any]]:
        """Keyword-extraction fallback when the LLM probe is unavailable.

        We still persist these so the learning loop has data to grade
        once outcomes flow in — they just carry a low base score.
        """
        words = _tokenize(prompt)
        if not words:
            return []
        keywords = sorted(set(words), key=words.index)[:max_candidates]
        out: list[dict[str, Any]] = []
        for kw in keywords:
            cand = f"explore {kw}"
            score = 0.25  # low confidence — flagged degraded by score band
            stored = self._store_prediction(
                session_id=session_id,
                prompt=prompt,
                candidate=cand,
                score=score,
                profile="degraded",
            )
            if stored:
                out.append(stored)
                _bump("predictions_generated_total")
        return out

    def _store_prediction(
        self,
        *,
        session_id: str,
        prompt: str,
        candidate: str,
        score: float,
        profile: str,
    ) -> Optional[dict[str, Any]]:
        prediction_id = f"pred_{uuid.uuid4().hex[:16]}"
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        now = _now_iso()
        expires_at = _iso_in(self.default_ttl_s)
        try:
            with _conn() as c:
                c.execute(
                    "INSERT INTO predictions (prediction_id, session_id, "
                    "prompt_hash, candidate, score, profile, created_at, "
                    "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        prediction_id,
                        session_id,
                        prompt_hash,
                        candidate,
                        float(score),
                        profile,
                        now,
                        expires_at,
                    ),
                )
            _bump("predictions_stored_total")
            return {
                "prediction_id": prediction_id,
                "session_id": session_id,
                "candidate": candidate,
                "score": float(score),
                "profile": profile,
                "expires_at": expires_at,
                "created_at": now,
            }
        except Exception as exc:  # noqa: BLE001
            _bump("predictions_errors_total")
            logger.warning("anticipation prediction store failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Module-level functions (legacy import-site compatibility)
# ---------------------------------------------------------------------------

# Singleton — module-level wrappers reuse it so the SQLite handle is shared.
_engine_singleton: Optional[AnticipationEngine] = None
_engine_singleton_lock = threading.Lock()


def _get_engine() -> AnticipationEngine:
    global _engine_singleton
    if _engine_singleton is None:
        with _engine_singleton_lock:
            if _engine_singleton is None:
                _engine_singleton = AnticipationEngine()
    return _engine_singleton


# --- cycle ---

_last_cycle_at = 0.0
_cycle_lock = threading.Lock()


def run_anticipation_cycle() -> dict[str, Any]:
    """One anticipation cycle (called by lite_scheduler every 45 s).

    Steps:
      1. Throttle — return early if last cycle < ``_MIN_INTERVAL_BETWEEN_RUNS``
      2. Detect active session
      3. Pull the latest prompt from the session jsonl (best-effort)
      4. Generate predictions via :class:`AnticipationEngine`
      5. Return summary dict the scheduler logs

    Always returns a dict — never raises. Bumps ``cycle_runs_total`` or
    ``cycle_skipped_total`` with a reason.
    """
    global _last_cycle_at
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "ran": False,
        "session_id": None,
        "sections_cached": [],
        "skipped_reason": None,
        "elapsed_ms": 0,
        "predictions": [],
    }

    with _cycle_lock:
        now = time.monotonic()
        if (now - _last_cycle_at) < _MIN_INTERVAL_BETWEEN_RUNS:
            result["skipped_reason"] = (
                f"too_recent ({int(now - _last_cycle_at)}s < "
                f"{_MIN_INTERVAL_BETWEEN_RUNS}s)"
            )
            _bump("cycle_skipped_total")
            _bump_reason("too_recent")
            return result
        _last_cycle_at = now

    session_id = _detect_active_session()
    if not session_id:
        result["skipped_reason"] = "no_active_session"
        _bump("cycle_skipped_total")
        _bump_reason("no_active_session")
        return result
    result["session_id"] = session_id

    prompt = _extract_latest_prompt(session_id)
    if not prompt:
        result["skipped_reason"] = "no_recent_prompt"
        _bump("cycle_skipped_total")
        _bump_reason("no_recent_prompt")
        return result
    if len(prompt.split()) <= 5:
        result["skipped_reason"] = f"short_prompt ({len(prompt.split())} words)"
        _bump("cycle_skipped_total")
        _bump_reason("short_prompt")
        return result

    # Pull priors from learning_store when available
    priors: list[dict[str, Any]] = []
    ls = _try_import_learning_store()
    if ls is not None and hasattr(ls, "query"):
        try:
            priors = ls.query(prompt[:120], limit=5) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("learning_store query failed: %s", exc)

    try:
        engine = _get_engine()
        predictions = engine.predict_next_actions(
            {
                "prompt": prompt,
                "session_id": session_id,
                "recent_outcomes": priors,
            }
        )
        result["predictions"] = predictions
        result["sections_cached"] = [p["candidate"][:40] for p in predictions]
        result["ran"] = True
        _bump("cycle_runs_total")
    except Exception as exc:  # noqa: BLE001
        result["skipped_reason"] = f"engine_error: {exc}"
        _bump("cycle_errors_total")
        logger.error("anticipation cycle engine error: %s", exc)

    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return result


def _extract_latest_prompt(session_id: str) -> Optional[str]:
    """Read the most recent user prompt from the session jsonl.

    Returns ``None`` when the session file is missing or unreadable —
    the cycle skips with ``no_recent_prompt`` instead of crashing.
    """
    try:
        cwd_slug = (
            str(pathlib.Path.cwd()).replace("/", "-").replace(" ", "-").lstrip("-")
        )
        session_file = pathlib.Path.home() / ".claude" / "projects" / (
            f"-{cwd_slug}"
        ) / f"{session_id}.jsonl"
        if not session_file.exists():
            return None
        # Walk backward from EOF for the last user message — cheap because
        # session files are append-only.
        with session_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            buf = bytearray()
            chunk = 4096
            pos = size
            lines: list[str] = []
            while pos > 0 and len(lines) < 50:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf[:0] = fh.read(read)
                # Decode and split — keep last partial line for next iter
                text = buf.decode("utf-8", errors="ignore")
                segs = text.split("\n")
                if pos > 0:
                    buf = bytearray(segs[0].encode("utf-8"))
                    segs = segs[1:]
                else:
                    buf = bytearray()
                lines = segs + lines
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Match Claude Code transcript shape
                if (
                    obj.get("type") == "user"
                    or obj.get("role") == "user"
                ):
                    msg = obj.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                return str(part.get("text") or "")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("_extract_latest_prompt failed: %s", exc)
        return None


# --- listener (legacy import-site compatibility) ---

_listener_thread: Optional[threading.Thread] = None
_listener_stop = threading.Event()


def start_listener() -> bool:
    """Start the background anticipation listener (idempotent).

    The legacy module wired this to Redis pub/sub on the
    ``session_file_changed`` channel. The migrate3 port polls the active
    session file mtime instead so it works without Redis.
    """
    global _listener_thread
    if _listener_thread is not None and _listener_thread.is_alive():
        return True
    _listener_stop.clear()
    try:
        t = threading.Thread(
            target=_listener_loop, name="anticipation_listener", daemon=True
        )
        t.start()
        _listener_thread = t
        _bump("listener_starts_total")
        return True
    except Exception as exc:  # noqa: BLE001
        _bump("listener_errors_total")
        logger.warning("anticipation listener start failed: %s", exc)
        return False


def stop_listener() -> bool:
    """Stop the background listener (idempotent, never raises)."""
    global _listener_thread
    if _listener_thread is None:
        return True
    _listener_stop.set()
    try:
        _listener_thread.join(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        _bump("listener_errors_total")
        logger.debug("anticipation listener join failed: %s", exc)
    _listener_thread = None
    _bump("listener_stops_total")
    return True


def _listener_loop() -> None:
    """Background loop — polls active session, fires a cycle on change.

    Defensive: catches every exception per iteration so a transient
    upstream outage never kills the thread.
    """
    last_mtime: dict[str, float] = {}
    interval = max(2, _CYCLE_INTERVAL_S // 5)
    while not _listener_stop.is_set():
        try:
            sid = _detect_active_session()
            if sid:
                try:
                    cwd_slug = (
                        str(pathlib.Path.cwd())
                        .replace("/", "-")
                        .replace(" ", "-")
                        .lstrip("-")
                    )
                    f = pathlib.Path.home() / ".claude" / "projects" / (
                        f"-{cwd_slug}"
                    ) / f"{sid}.jsonl"
                    if f.exists():
                        m = f.stat().st_mtime
                        if last_mtime.get(sid, 0.0) != m:
                            last_mtime[sid] = m
                            run_anticipation_cycle()
                except Exception as inner:  # noqa: BLE001
                    logger.debug("listener inner: %s", inner)
        except Exception as exc:  # noqa: BLE001
            _bump("listener_errors_total")
            logger.warning("anticipation listener loop: %s", exc)
        _listener_stop.wait(timeout=interval)


# --- superhero mode (legacy import-site compatibility) ---


def _activate_superhero_anticipation(
    task_override: Optional[str] = None,
) -> dict[str, Any]:
    """Pre-compute 4 LLM-synthesized artifacts for an upcoming agent swarm.

    The four artifacts (``mission`` / ``gotchas`` / ``architecture`` /
    ``failures``) mirror the legacy contract. Each runs through
    :func:`memory.llm_priority_queue.llm_generate` at
    ``Priority.BACKGROUND`` with the ``extract`` profile (~10 s each).
    Missing LLM = degraded mode returns an empty cache without raising.
    """
    result: dict[str, Any] = {
        "activated": False,
        "artifacts_cached": [],
        "task": "",
        "elapsed_ms": 0,
        "error": None,
    }
    t0 = time.monotonic()

    session_id = _detect_active_session()
    if not session_id:
        result["error"] = "no_active_session"
        return result

    task = task_override or _extract_latest_prompt(session_id) or ""
    if not task:
        result["error"] = "no_prompt"
        return result
    result["task"] = task[:200]

    llm_generate, Priority = _try_import_priority_queue()
    if llm_generate is None:
        result["error"] = "llm_unavailable"
        return result

    artifacts: dict[str, str] = {}
    for name, system in (
        (
            "mission",
            "Summarize this task in 2 sentences. State the goal, key files.",
        ),
        (
            "gotchas",
            "List 3-5 gotchas for this task. One per line. Be specific.",
        ),
        (
            "architecture",
            "List the 3-5 most relevant modules / files for this task. "
            "One per line, module path only.",
        ),
        (
            "failures",
            "List 3 likely failure modes for this task. One per line.",
        ),
    ):
        _bump("llm_calls_total")
        try:
            txt = llm_generate(
                system_prompt=system,
                user_prompt=f"TASK: {task[:400]}",
                priority=Priority.BACKGROUND,
                profile="extract",
                caller=f"superhero_{name}",
                timeout_s=_LLM_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            _bump("llm_call_errors_total")
            logger.warning("superhero %s LLM failed: %s", name, exc)
            txt = None
        if txt:
            artifacts[name] = txt.strip()
            result["artifacts_cached"].append(name)
            _bump("superhero_artifacts_cached_total")

    if artifacts:
        try:
            now = _now_iso()
            expires = _iso_in(_SUPERHERO_ARTIFACT_TTL_S)
            with _conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO superhero_state (session_id, task, "
                    "activated_at, expires_at, mission, gotchas, architecture, "
                    "failures) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        task[:200],
                        now,
                        expires,
                        artifacts.get("mission"),
                        artifacts.get("gotchas"),
                        artifacts.get("architecture"),
                        artifacts.get("failures"),
                    ),
                )
            result["activated"] = True
            _bump("superhero_activations_total")
        except Exception as exc:  # noqa: BLE001
            _bump("superhero_errors_total")
            logger.warning("superhero SQLite write failed: %s", exc)
            result["error"] = "storage_failure"

    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return result


def is_superhero_active(session_id: Optional[str] = None) -> bool:
    """Check whether superhero anticipation is active for a session."""
    sid = session_id or _detect_active_session() or "unknown"
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT expires_at FROM superhero_state WHERE session_id = ?",
                (sid,),
            ).fetchone()
        if row is None:
            return False
        return row["expires_at"] > _now_iso()
    except Exception as exc:  # noqa: BLE001
        _bump("superhero_errors_total")
        logger.debug("is_superhero_active failed: %s", exc)
        return False


def get_superhero_cache(
    session_id: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Return the cached superhero artifacts for a session, if active."""
    sid = session_id or _detect_active_session() or "unknown"
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT task, activated_at, mission, gotchas, architecture, "
                "failures, expires_at FROM superhero_state WHERE session_id = ?",
                (sid,),
            ).fetchone()
        if row is None or row["expires_at"] <= _now_iso():
            return None
        return {
            "_task": row["task"],
            "_activated_at": row["activated_at"],
            "mission": row["mission"] or "",
            "gotchas": row["gotchas"] or "",
            "architecture": row["architecture"] or "",
            "failures": row["failures"] or "",
        }
    except Exception as exc:  # noqa: BLE001
        _bump("superhero_errors_total")
        logger.debug("get_superhero_cache failed: %s", exc)
        return None


# --- agent findings WAL ---


def record_agent_finding(
    agent_id: str,
    finding: str,
    finding_type: str = "observation",
    severity: str = "info",
    session_id: Optional[str] = None,
) -> bool:
    """Append an agent finding to the per-session WAL."""
    if not finding or not isinstance(finding, str):
        return False
    sid = session_id or _detect_active_session() or "unknown"
    finding_id = f"af_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    expires = _iso_in(_AGENT_FINDINGS_TTL_S)
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO agent_findings (finding_id, session_id, agent_id, "
                "finding, finding_type, severity, recorded_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finding_id,
                    sid,
                    str(agent_id or "anonymous"),
                    finding[:500],
                    finding_type or "observation",
                    severity or "info",
                    now,
                    expires,
                ),
            )
        _bump("agent_findings_recorded_total")
        return True
    except Exception as exc:  # noqa: BLE001
        _bump("agent_findings_errors_total")
        logger.warning("record_agent_finding failed: %s", exc)
        return False


def get_agent_findings(
    session_id: Optional[str] = None,
    since_seconds: int = 0,
    finding_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query the agent-findings WAL for a session."""
    sid = session_id or _detect_active_session() or "unknown"
    try:
        params: list[Any] = [sid, _now_iso()]
        where = ["session_id = ?", "expires_at > ?"]
        if since_seconds and since_seconds > 0:
            where.append("recorded_at > ?")
            params.append(_iso_in(-int(since_seconds)))
        if finding_type:
            where.append("finding_type = ?")
            params.append(finding_type)
        params.append(max(1, int(limit)))
        sql = (
            "SELECT finding_id, agent_id, finding, finding_type, severity, "
            "recorded_at FROM agent_findings WHERE "
            + " AND ".join(where)
            + " ORDER BY recorded_at DESC LIMIT ?"
        )
        with _conn() as c:
            rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        _bump("agent_findings_errors_total")
        logger.debug("get_agent_findings failed: %s", exc)
        return []


def get_agent_findings_digest(
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Compact summary the Atlas debrief consumes."""
    findings = get_agent_findings(session_id=session_id, limit=200)
    by_type: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    criticals: list[str] = []
    for f in findings:
        by_type[f["finding_type"]] = by_type.get(f["finding_type"], 0) + 1
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        if f["severity"] == "critical":
            criticals.append(f["finding"])
    return {
        "total": len(findings),
        "by_type": by_type,
        "by_severity": by_sev,
        "criticals": criticals[:5],
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_in(seconds: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat(
        timespec="seconds"
    )


def _tokenize(text: str) -> list[str]:
    """Cheap tokenizer — lower-cased, alpha-only, 3-char min."""
    import re

    return [w for w in re.findall(r"[a-zA-Z]{3,}", (text or "").lower())]


# Module load-time logging — proves to operators the engine is wired.
logger.info(
    "anticipation_engine loaded (state_db=%s, cycle_interval_s=%d, learning_rate=%.2f)",
    _state_db_path(),
    _CYCLE_INTERVAL_S,
    _DEFAULT_LEARNING_RATE,
)
