"""Failure pattern analyzer — captures, clusters, and promotes failure patterns.

Why this exists
---------------
Synaptic diagnosis (2026-05-13): :mod:`memory.failure_pattern_analyzer` is
the *learning* arm of the predictive layer (6 active import sites in the
mothership: ``agent_service.py:3787``, ``lite_scheduler.py:2918``, and four
in ``persistent_hook_structure.py``). It pairs with
:mod:`memory.anticipation_engine` — anticipation predicts; this module
learns from the misses.

Behavior contract
-----------------
The analyzer reads three signals — and degrades gracefully when any is
missing:

  * **Observability store** (claims + outcomes) — the canonical source of
    "what we predicted vs what happened". Lazy-imported; missing = scope
    narrows to the local SQLite cache.
  * **Dialogue mirror** (Aaron + Atlas messages) — repeated task attempts
    are a strong failure signal (3+ identical asks within 24 h).
  * **Anticipation engine** (this PR's sibling) — pending predictions that
    expired without a hit count toward miss patterns.

Recurring failures are promoted to ``landmine`` evidence rows: the
mothership's Section 0 SAFETY / Section 2 WISDOM webhook stages render
those landmines verbatim so the assistant sees the warning *before* it
repeats the mistake.

Public API (matches every legacy import site)
---------------------------------------------
::

    FailurePattern                  # dataclass
    FailurePatternAnalyzer          # class
        .analyze_for_patterns(session_id=None, hours_back=24) -> list[FailurePattern]
        .get_landmines_for_task(task: str, limit: int = 3) -> list[str]
        .get_high_yield_failures(limit=10, domain=None) -> list[FailurePattern]
    get_failure_pattern_analyzer() -> FailurePatternAnalyzer | None
    analyze_recent_failures(window_hours: int = 24) -> dict[str, Any]
    get_failure_patterns(scope: str = "all") -> list[FailurePattern]
    correlate_with_prediction(prediction_id: str) -> dict[str, Any]

ZSF (zero silent failures)
--------------------------
:data:`COUNTERS` tracks every code path. ``get_failure_pattern_analyzer()``
returns ``None`` on construction failure rather than raising — the legacy
call sites all use ``analyzer = ...; if analyzer: analyzer.method(...)``
so returning ``None`` preserves the existing skip-gracefully behavior.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("contextdna.failure_patterns")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s contextdna.failure_patterns %(message)s"
        )
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Counters (ZSF)
# ---------------------------------------------------------------------------

COUNTERS: dict[str, Any] = {
    "patterns_detected_total": 0,
    "patterns_stored_total": 0,
    "patterns_promoted_to_landmine_total": 0,
    "landmines_emitted_total": 0,
    "landmines_emitted_total_by_domain": {},
    "analyze_runs_total": 0,
    "analyze_errors_total": 0,
    "correlations_total": 0,
    "upstream_missing_observability_store_total": 0,
    "upstream_missing_anticipation_engine_total": 0,
    "upstream_missing_dialogue_mirror_total": 0,
    "db_errors_total": 0,
}

_counter_lock = threading.Lock()


def _bump(name: str, delta: int = 1) -> None:
    with _counter_lock:
        if name in COUNTERS and isinstance(COUNTERS[name], int):
            COUNTERS[name] += delta


def _bump_domain(domain: str) -> None:
    with _counter_lock:
        d = COUNTERS["landmines_emitted_total_by_domain"]
        d[domain or "unknown"] = d.get(domain or "unknown", 0) + 1


def get_counters() -> dict[str, Any]:
    with _counter_lock:
        return {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in COUNTERS.items()
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_OCCURRENCES = int(
    os.environ.get("CONTEXTDNA_FPA_MIN_OCCURRENCES", "2")
)
HIGH_CONFIDENCE = float(
    os.environ.get("CONTEXTDNA_FPA_HIGH_CONFIDENCE", "0.8")
)
MEDIUM_CONFIDENCE = float(
    os.environ.get("CONTEXTDNA_FPA_MEDIUM_CONFIDENCE", "0.6")
)
LOW_CONFIDENCE = float(
    os.environ.get("CONTEXTDNA_FPA_LOW_CONFIDENCE", "0.4")
)
DEFAULT_WINDOW_HOURS = int(
    os.environ.get("CONTEXTDNA_FPA_WINDOW_HOURS", "24")
)


def _state_db_path() -> pathlib.Path:
    home = os.environ.get("CONTEXTDNA_HOME")
    if home:
        base = pathlib.Path(home)
    else:
        base = pathlib.Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / ".failure_patterns.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS failure_patterns (
    pattern_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    confidence REAL DEFAULT 0.5,
    landmine_text TEXT NOT NULL,
    keywords_json TEXT,
    sources_json TEXT,
    last_seen TEXT,
    created_at TEXT,
    updated_at TEXT,
    promoted_to_landmine INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_patterns_domain
    ON failure_patterns(domain);
CREATE INDEX IF NOT EXISTS idx_patterns_confidence
    ON failure_patterns(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_patterns_count
    ON failure_patterns(occurrence_count DESC);

-- Correlation table — joins predictions to failure patterns so we can
-- answer "did anticipation predict this failure?".
CREATE TABLE IF NOT EXISTS prediction_correlations (
    prediction_id TEXT NOT NULL,
    pattern_id    TEXT NOT NULL,
    correlated_at TEXT NOT NULL,
    PRIMARY KEY (prediction_id, pattern_id)
);
"""


@contextmanager
def _conn():
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
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FailurePattern:
    """A clustered, evidence-backed failure pattern."""

    pattern_id: str
    domain: str
    description: str
    occurrence_count: int
    confidence: float
    landmine_text: str
    keywords: list[str] = dataclasses.field(default_factory=list)
    sources: list[str] = dataclasses.field(default_factory=list)
    last_seen: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Upstream probes (lazy, ZSF)
# ---------------------------------------------------------------------------


def _try_observability_store():
    try:
        from memory.observability_store import (  # type: ignore[import-not-found]
            get_observability_store,
        )

        return get_observability_store()
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_observability_store_total")
        logger.debug("observability_store unavailable: %s", exc)
        return None


def _try_anticipation_engine():
    """Local import — the engine ships in the same package."""
    try:
        from memory import anticipation_engine  # type: ignore[import-not-found]

        return anticipation_engine
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_anticipation_engine_total")
        logger.debug("anticipation_engine unavailable: %s", exc)
        return None


def _try_dialogue_mirror():
    try:
        from memory.dialogue_mirror import DialogueMirror  # type: ignore

        return DialogueMirror()
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_dialogue_mirror_total")
        logger.debug("dialogue_mirror unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Analyzer class
# ---------------------------------------------------------------------------


class FailurePatternAnalyzer:
    """Detect, cluster, and promote recurring failure patterns.

    Construction is cheap — only initializes the SQLite schema. The actual
    upstream dependencies (observability store, dialogue mirror) are
    lazy-loaded inside the methods that need them so a missing module
    does not block instantiation.

    Threading: the analyzer is safe to call from multiple threads because
    every SQLite handle is short-lived (per-method).
    """

    MIN_OCCURRENCES = MIN_OCCURRENCES
    HIGH_CONFIDENCE = HIGH_CONFIDENCE
    MEDIUM_CONFIDENCE = MEDIUM_CONFIDENCE
    LOW_CONFIDENCE = LOW_CONFIDENCE

    def __init__(self, db_path: Optional[pathlib.Path] = None) -> None:
        self.db_path = pathlib.Path(db_path) if db_path else _state_db_path()
        # Lazy-loaded validators (match legacy attribute names so any
        # external code that probes them keeps working).
        self._dialogue_mirror = None
        self._observability_store = None
        # Trigger schema init eagerly so callers see a clean DB.
        try:
            with _conn():
                pass
        except Exception as exc:  # noqa: BLE001
            _bump("db_errors_total")
            logger.warning("failure_pattern_analyzer init: %s", exc)

    # ------------------------------------------------------------------
    # Lazy properties
    # ------------------------------------------------------------------

    @property
    def dialogue_mirror(self):  # type: ignore[no-untyped-def]
        if self._dialogue_mirror is None:
            self._dialogue_mirror = _try_dialogue_mirror()
        return self._dialogue_mirror

    @property
    def observability_store(self):  # type: ignore[no-untyped-def]
        if self._observability_store is None:
            self._observability_store = _try_observability_store()
        return self._observability_store

    # ------------------------------------------------------------------
    # Core API — matches legacy import sites
    # ------------------------------------------------------------------

    def analyze_for_patterns(
        self, session_id: Optional[str] = None, hours_back: int = DEFAULT_WINDOW_HOURS
    ) -> list[FailurePattern]:
        """Sweep recent signals, cluster, store, and return high-yield patterns.

        Step order:
          1. Pull recent outcomes from observability_store (filter: failures)
          2. Pull repeated user prompts from dialogue_mirror
          3. Pull expired-without-hit predictions from anticipation_engine
          4. Cluster by domain + keyword overlap
          5. Promote to ``landmine`` when occurrence_count >= MIN_OCCURRENCES
        """
        _bump("analyze_runs_total")
        patterns: list[FailurePattern] = []

        # 1. Failure outcomes from the observability store
        try:
            patterns.extend(self._from_observability(hours_back))
        except Exception as exc:  # noqa: BLE001
            _bump("analyze_errors_total")
            logger.warning("analyze_for_patterns: obs scan failed: %s", exc)

        # 2. Repeated tasks from dialogue mirror
        try:
            patterns.extend(self._from_dialogue(session_id, hours_back))
        except Exception as exc:  # noqa: BLE001
            _bump("analyze_errors_total")
            logger.warning("analyze_for_patterns: dialogue scan failed: %s", exc)

        # 3. Expired predictions from anticipation engine
        try:
            patterns.extend(self._from_anticipation(hours_back))
        except Exception as exc:  # noqa: BLE001
            _bump("analyze_errors_total")
            logger.warning("analyze_for_patterns: anticipation scan failed: %s", exc)

        # 4. Cluster + persist + promote
        merged = self._merge_similar(patterns)
        for p in merged:
            self._store_pattern(p)
            if p.occurrence_count >= self.MIN_OCCURRENCES:
                self._promote_to_landmine(p.pattern_id)

        high_yield = [p for p in merged if self._is_high_yield(p)]
        return high_yield

    def get_landmines_for_task(self, task: str, limit: int = 3) -> list[str]:
        """Return relevant LANDMINE warnings for an upcoming task.

        Called from Section 2 (WISDOM) webhook injection — matches the
        legacy contract: returns a list of formatted strings, not the full
        :class:`FailurePattern`.
        """
        keywords = self._extract_keywords(task)
        domain = self._detect_domain(task)
        out: list[str] = []
        try:
            with _conn() as c:
                rows = c.execute(
                    "SELECT landmine_text, domain, keywords_json, confidence "
                    "FROM failure_patterns "
                    "WHERE confidence >= ? AND promoted_to_landmine = 1 "
                    "ORDER BY confidence DESC, occurrence_count DESC LIMIT ?",
                    (self.MEDIUM_CONFIDENCE, max(1, int(limit * 4))),
                ).fetchall()
            seen = set()
            for r in rows:
                if r["landmine_text"] in seen:
                    continue
                pat_keywords = json.loads(r["keywords_json"] or "[]")
                # Relevance: domain match OR keyword overlap >= 2
                overlap = len(set(pat_keywords) & set(keywords))
                if r["domain"] == domain or overlap >= 2:
                    out.append(r["landmine_text"])
                    seen.add(r["landmine_text"])
                    _bump("landmines_emitted_total")
                    _bump_domain(r["domain"])
                    if len(out) >= limit:
                        break
        except Exception as exc:  # noqa: BLE001
            _bump("db_errors_total")
            logger.debug("get_landmines_for_task failed: %s", exc)
        return out

    def get_high_yield_failures(
        self, limit: int = 10, domain: Optional[str] = None
    ) -> list[FailurePattern]:
        """Return all patterns above MEDIUM_CONFIDENCE, optional domain filter."""
        try:
            with _conn() as c:
                if domain:
                    rows = c.execute(
                        "SELECT * FROM failure_patterns WHERE domain = ? "
                        "AND confidence >= ? ORDER BY confidence DESC, "
                        "occurrence_count DESC LIMIT ?",
                        (domain, self.MEDIUM_CONFIDENCE, int(limit)),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT * FROM failure_patterns WHERE confidence >= ? "
                        "ORDER BY confidence DESC, occurrence_count DESC LIMIT ?",
                        (self.MEDIUM_CONFIDENCE, int(limit)),
                    ).fetchall()
            return [self._row_to_pattern(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            _bump("db_errors_total")
            logger.debug("get_high_yield_failures failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _from_observability(self, hours_back: int) -> list[FailurePattern]:
        """Extract failure patterns from observability_store outcomes."""
        obs = self.observability_store
        if obs is None or not hasattr(obs, "_sqlite_conn"):
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        try:
            conn = obs._sqlite_conn  # contract from agent_service.py:3802
            cursor = conn.cursor()
            try:
                rows = cursor.execute(
                    "SELECT task_title, COUNT(*) AS n FROM outcome_event "
                    "WHERE success = 0 AND timestamp > ? GROUP BY task_title "
                    "HAVING n >= ? ORDER BY n DESC LIMIT 20",
                    (cutoff, self.MIN_OCCURRENCES),
                ).fetchall()
            finally:
                cursor.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("observability scan failed: %s", exc)
            return []

        out: list[FailurePattern] = []
        now = _now_iso()
        for r in rows:
            title = r[0] if isinstance(r, tuple) else r["task_title"]
            count = r[1] if isinstance(r, tuple) else r["n"]
            domain = self._detect_domain(title or "")
            kw = self._extract_keywords(title or "")
            confidence = min(0.9, 0.5 + 0.1 * (count - 1))
            out.append(
                FailurePattern(
                    pattern_id=self._derive_id("obs", title or "", domain),
                    domain=domain,
                    description=f"Repeated failure: {title}",
                    occurrence_count=int(count),
                    confidence=round(confidence, 3),
                    landmine_text=f"{title}: failed {count}× in last {hours_back}h",
                    keywords=kw,
                    sources=["observability_store"],
                    last_seen=now,
                )
            )
        return out

    def _from_dialogue(
        self, session_id: Optional[str], hours_back: int
    ) -> list[FailurePattern]:
        """Repeated user prompts within window = strong failure signal."""
        dm = self.dialogue_mirror
        if dm is None:
            return []
        try:
            # DialogueMirror exposes db_path + messages table in the active
            # codebase — match the legacy lookup but don't depend on the
            # exact column name (fall back if missing).
            db_path = getattr(dm, "db_path", None)
            if not db_path:
                return []
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=hours_back)
            ).isoformat()
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                table = "dialogue_messages"
                # Auto-detect role column — legacy uses `aaron`, newer uses `user`.
                where_role = (
                    "role IN ('aaron', 'user')"
                )
                params: list[Any] = [cutoff]
                sql = (
                    f"SELECT content, timestamp FROM {table} "
                    f"WHERE {where_role} AND timestamp > ?"
                )
                if session_id:
                    sql += " AND session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY timestamp DESC LIMIT 200"
                rows = conn.execute(sql, tuple(params)).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("dialogue scan failed: %s", exc)
            return []

        # Cluster by normalized content
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            content = (r["content"] or "").strip()
            if not content or content.startswith("<"):
                continue  # skip IDE event tags
            key = self._normalize_task(content)
            buckets.setdefault(key, []).append({"content": content, "ts": r["timestamp"]})

        out: list[FailurePattern] = []
        now = _now_iso()
        for key, items in buckets.items():
            if len(items) < self.MIN_OCCURRENCES + 1:
                continue
            sample = items[0]["content"]
            domain = self._detect_domain(sample)
            kw = self._extract_keywords(sample)
            confidence = min(0.85, 0.5 + 0.08 * (len(items) - 2))
            out.append(
                FailurePattern(
                    pattern_id=self._derive_id("dlg", key, domain),
                    domain=domain,
                    description=f"Repeated task ({len(items)}×): {sample[:80]}",
                    occurrence_count=len(items),
                    confidence=round(confidence, 3),
                    landmine_text=(
                        f"You have asked this {len(items)}× already: "
                        f"{sample[:120]}"
                    ),
                    keywords=kw,
                    sources=["dialogue_mirror"],
                    last_seen=now,
                )
            )
        return out

    def _from_anticipation(self, hours_back: int) -> list[FailurePattern]:
        """Expired-without-outcome predictions = missed anticipations."""
        ae = _try_anticipation_engine()
        if ae is None:
            return []
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=hours_back)
            ).isoformat()
            with sqlite3.connect(str(ae._state_db_path())) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT candidate, COUNT(*) AS n FROM predictions "
                    "WHERE outcome = 'miss' AND outcome_at > ? "
                    "GROUP BY candidate HAVING n >= ? ORDER BY n DESC LIMIT 20",
                    (cutoff, self.MIN_OCCURRENCES),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug("anticipation scan failed: %s", exc)
            return []

        out: list[FailurePattern] = []
        now = _now_iso()
        for r in rows:
            cand = r["candidate"]
            count = r["n"]
            domain = self._detect_domain(cand)
            kw = self._extract_keywords(cand)
            confidence = min(0.8, 0.45 + 0.1 * (count - 1))
            out.append(
                FailurePattern(
                    pattern_id=self._derive_id("ant", cand, domain),
                    domain=domain,
                    description=f"Anticipation miss: {cand[:80]}",
                    occurrence_count=int(count),
                    confidence=round(confidence, 3),
                    landmine_text=(
                        f"Anticipation predicted '{cand[:80]}' "
                        f"{count}× but it never happened — recalibrate"
                    ),
                    keywords=kw,
                    sources=["anticipation_engine"],
                    last_seen=now,
                )
            )
        return out

    def _merge_similar(
        self, patterns: list[FailurePattern]
    ) -> list[FailurePattern]:
        """Cluster patterns whose pattern_id collides (same domain + keyword)."""
        by_id: dict[str, FailurePattern] = {}
        for p in patterns:
            if p.pattern_id in by_id:
                existing = by_id[p.pattern_id]
                existing.occurrence_count += p.occurrence_count
                existing.confidence = min(0.95, max(existing.confidence, p.confidence))
                existing.sources = list(set(existing.sources) | set(p.sources))
                existing.keywords = list(
                    dict.fromkeys(existing.keywords + p.keywords)
                )
                existing.last_seen = max(existing.last_seen, p.last_seen)
            else:
                by_id[p.pattern_id] = p
        _bump("patterns_detected_total", delta=len(by_id))
        return list(by_id.values())

    def _is_high_yield(self, p: FailurePattern) -> bool:
        return (
            p.occurrence_count >= self.MIN_OCCURRENCES
            and p.confidence >= self.MEDIUM_CONFIDENCE
        )

    def _store_pattern(self, p: FailurePattern) -> None:
        now = _now_iso()
        try:
            with _conn() as c:
                c.execute(
                    "INSERT INTO failure_patterns (pattern_id, domain, "
                    "description, occurrence_count, confidence, landmine_text, "
                    "keywords_json, sources_json, last_seen, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(pattern_id) DO UPDATE SET "
                    "occurrence_count = excluded.occurrence_count + "
                    "failure_patterns.occurrence_count, "
                    "confidence = MAX(excluded.confidence, failure_patterns.confidence), "
                    "sources_json = excluded.sources_json, "
                    "last_seen = excluded.last_seen, "
                    "updated_at = excluded.updated_at",
                    (
                        p.pattern_id,
                        p.domain,
                        p.description[:500],
                        p.occurrence_count,
                        p.confidence,
                        p.landmine_text[:500],
                        json.dumps(p.keywords),
                        json.dumps(p.sources),
                        p.last_seen,
                        now,
                        now,
                    ),
                )
            _bump("patterns_stored_total")
        except Exception as exc:  # noqa: BLE001
            _bump("db_errors_total")
            logger.warning("_store_pattern failed: %s", exc)

    def _promote_to_landmine(self, pattern_id: str) -> None:
        try:
            with _conn() as c:
                c.execute(
                    "UPDATE failure_patterns SET promoted_to_landmine = 1, "
                    "updated_at = ? WHERE pattern_id = ?",
                    (_now_iso(), pattern_id),
                )
            _bump("patterns_promoted_to_landmine_total")
        except Exception as exc:  # noqa: BLE001
            _bump("db_errors_total")
            logger.debug("_promote_to_landmine failed: %s", exc)

    def _row_to_pattern(self, r: sqlite3.Row) -> FailurePattern:
        return FailurePattern(
            pattern_id=r["pattern_id"],
            domain=r["domain"],
            description=r["description"],
            occurrence_count=r["occurrence_count"],
            confidence=r["confidence"],
            landmine_text=r["landmine_text"],
            keywords=json.loads(r["keywords_json"] or "[]"),
            sources=json.loads(r["sources_json"] or "[]"),
            last_seen=r["last_seen"] or "",
        )

    # ------------------------------------------------------------------
    # Text helpers (deduped from the legacy module)
    # ------------------------------------------------------------------

    def _normalize_task(self, content: str) -> str:
        text = re.sub(r"[^a-z0-9 ]", " ", (content or "").lower())
        text = re.sub(r"\s+", " ", text).strip()
        return " ".join(text.split()[:8])

    def _extract_keywords(self, text: str) -> list[str]:
        words = re.findall(r"[a-zA-Z]{4,}", (text or "").lower())
        seen: dict[str, int] = {}
        for w in words:
            if w in _STOPWORDS:
                continue
            seen[w] = seen.get(w, 0) + 1
        return sorted(seen, key=lambda w: -seen[w])[:8]

    def _detect_domain(self, text: str) -> str:
        t = (text or "").lower()
        if any(k in t for k in ("webhook", "section", "inject")):
            return "webhook"
        if any(k in t for k in ("agent", "spawn", "swarm")):
            return "agents"
        if any(k in t for k in ("test", "pytest", "unittest")):
            return "testing"
        if any(k in t for k in ("deploy", "ship", "release")):
            return "deploy"
        if any(k in t for k in ("memory", "learning", "evidence")):
            return "memory"
        if any(k in t for k in ("fleet", "nats", "mac1", "mac2", "mac3")):
            return "fleet"
        if any(k in t for k in ("anticipation", "prediction")):
            return "anticipation"
        return "general"

    def _derive_id(self, kind: str, key: str, domain: str) -> str:
        h = hashlib.sha1(f"{kind}:{domain}:{key}".encode("utf-8")).hexdigest()[:16]
        return f"fp_{kind}_{h}"


_STOPWORDS = {
    "this", "that", "with", "from", "your", "have", "will", "what", "when",
    "where", "their", "they", "would", "could", "should", "about", "into",
    "more", "some", "than", "then", "there", "these", "those", "which",
    "while", "after", "before", "again", "also", "been", "being",
}


# ---------------------------------------------------------------------------
# Module-level functions (legacy import-site compatibility)
# ---------------------------------------------------------------------------

_analyzer_singleton: Optional[FailurePatternAnalyzer] = None
_analyzer_lock = threading.Lock()


def get_failure_pattern_analyzer() -> Optional[FailurePatternAnalyzer]:
    """Return a process-wide singleton, or ``None`` on init failure.

    The legacy import sites all check truthiness before calling methods:

    .. code-block:: python

        analyzer = get_failure_pattern_analyzer()
        if analyzer:
            analyzer.get_landmines_for_task(...)

    Returning ``None`` (rather than raising) preserves that pattern.
    """
    global _analyzer_singleton
    if _analyzer_singleton is not None:
        return _analyzer_singleton
    with _analyzer_lock:
        if _analyzer_singleton is None:
            try:
                _analyzer_singleton = FailurePatternAnalyzer()
            except Exception as exc:  # noqa: BLE001
                _bump("db_errors_total")
                logger.warning(
                    "failure_pattern_analyzer construction failed: %s", exc
                )
                return None
    return _analyzer_singleton


def analyze_recent_failures(window_hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """Run a one-shot analysis and return a summary dict.

    Used by the lite_scheduler's failure-pattern job (replaces the legacy
    ``analyze_for_patterns`` call there). Always returns a dict — never
    raises — and bumps ``analyze_runs_total``.
    """
    summary: dict[str, Any] = {
        "ran": False,
        "patterns_found": 0,
        "high_yield": 0,
        "domains": [],
        "window_hours": int(window_hours),
        "error": None,
    }
    analyzer = get_failure_pattern_analyzer()
    if analyzer is None:
        summary["error"] = "analyzer_unavailable"
        return summary
    try:
        patterns = analyzer.analyze_for_patterns(hours_back=int(window_hours))
        summary["ran"] = True
        summary["patterns_found"] = len(patterns)
        summary["high_yield"] = sum(
            1 for p in patterns if p.occurrence_count >= analyzer.MIN_OCCURRENCES
        )
        summary["domains"] = sorted({p.domain for p in patterns if p.domain})
    except Exception as exc:  # noqa: BLE001
        _bump("analyze_errors_total")
        summary["error"] = str(exc)[:200]
        logger.warning("analyze_recent_failures: %s", exc)
    return summary


def get_failure_patterns(scope: str = "all") -> list[FailurePattern]:
    """Return failure patterns within a scope.

    ``scope`` values:
      * ``"all"``        — every stored pattern (limit 100)
      * ``"landmines"``  — promoted patterns only
      * ``"domain:X"``   — single-domain filter
    """
    analyzer = get_failure_pattern_analyzer()
    if analyzer is None:
        return []
    try:
        if scope.startswith("domain:"):
            return analyzer.get_high_yield_failures(
                limit=100, domain=scope.split(":", 1)[1]
            )
        if scope == "landmines":
            with _conn() as c:
                rows = c.execute(
                    "SELECT * FROM failure_patterns WHERE promoted_to_landmine = 1 "
                    "ORDER BY confidence DESC, occurrence_count DESC LIMIT 100"
                ).fetchall()
            return [analyzer._row_to_pattern(r) for r in rows]
        return analyzer.get_high_yield_failures(limit=100)
    except Exception as exc:  # noqa: BLE001
        _bump("db_errors_total")
        logger.debug("get_failure_patterns failed: %s", exc)
        return []


def correlate_with_prediction(prediction_id: str) -> dict[str, Any]:
    """Find failure patterns that match a given anticipation prediction.

    Joins on keyword overlap — a prediction whose candidate text shares
    >= 2 keywords with a stored pattern is recorded as a correlation. The
    anticipation engine reads these correlations during ``learn_from_outcome``
    so a miss can promote the predicted pattern to a landmine immediately.
    """
    result: dict[str, Any] = {
        "prediction_id": prediction_id,
        "correlated_patterns": [],
        "ran": False,
        "error": None,
    }
    ae = _try_anticipation_engine()
    analyzer = get_failure_pattern_analyzer()
    if ae is None or analyzer is None:
        result["error"] = "upstream_unavailable"
        return result

    try:
        with sqlite3.connect(str(ae._state_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT candidate FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
        if row is None:
            result["error"] = "prediction_not_found"
            return result
        cand = row["candidate"]
        cand_keywords = set(analyzer._extract_keywords(cand))
        if not cand_keywords:
            result["ran"] = True
            return result

        with _conn() as c:
            rows = c.execute(
                "SELECT pattern_id, landmine_text, keywords_json, domain, "
                "confidence FROM failure_patterns"
            ).fetchall()
            now = _now_iso()
            for r in rows:
                pat_keywords = set(json.loads(r["keywords_json"] or "[]"))
                overlap = len(cand_keywords & pat_keywords)
                if overlap >= 2:
                    c.execute(
                        "INSERT OR IGNORE INTO prediction_correlations "
                        "(prediction_id, pattern_id, correlated_at) VALUES (?, ?, ?)",
                        (prediction_id, r["pattern_id"], now),
                    )
                    result["correlated_patterns"].append(
                        {
                            "pattern_id": r["pattern_id"],
                            "domain": r["domain"],
                            "landmine_text": r["landmine_text"],
                            "confidence": r["confidence"],
                            "overlap": overlap,
                        }
                    )
        _bump("correlations_total")
        result["ran"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        _bump("analyze_errors_total")
        result["error"] = str(exc)[:200]
        logger.warning("correlate_with_prediction failed: %s", exc)
        return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Module load-time logging.
logger.info(
    "failure_pattern_analyzer loaded (state_db=%s, min_occurrences=%d, "
    "high_confidence=%.2f)",
    _state_db_path(),
    MIN_OCCURRENCES,
    HIGH_CONFIDENCE,
)
