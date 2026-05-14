#!/usr/bin/env python3
"""
shadow_reader.py — Instrumented Production Shadow-Mode for the LearningStore
===========================================================================

Round 3 Neurologist verdict (2026-05-13) recommended deploying a non-invasive
shadow-reader that mirrors every ``store_learning`` / ``get_recent`` /
``get_by_id`` / ``promote`` / ``retire`` call to an in-memory reference
implementation, compares the two results, and emits a divergence record
without ever blocking — or perturbing — production.

The point of this module is **read-side invariant checking**: every divergence
between the real ``LearningStore`` and a simple, race-free, in-memory list is
strong evidence that the real backend has a race, a silent-empty bug, a
phantom write, or a stale read. The shadow side intentionally has zero
concurrency hardening so that if the *shadow* fails under load, that itself
proves the production code is racing.

Architecture
------------
::

    ┌──────────────────────────────────┐
    │ Caller (webhook, fleet, etc.)    │
    └──────────────┬───────────────────┘
                   │ store.store_learning(...)
                   ▼
    ┌──────────────────────────────────┐    ┌──────────────────────────────┐
    │ shadow_compare wrapper           │───▶│ ShadowStorage (in-memory)    │
    │  - calls prod synchronously      │    │  - list + id-index           │
    │  - schedules shadow in a thread  │    │  - NO concurrency hardening  │
    │  - compares + logs divergence    │    └──────────────────────────────┘
    │  - returns prod result           │
    └──────────────┬───────────────────┘
                   │ prod result
                   ▼

Key invariants
--------------
1.  **Production is never blocked.** The shadow call runs in a daemon thread.
    Production response is returned the instant the real backend returns.
2.  **Production is never perturbed.** A shadow exception is *always* caught
    and counted. It never propagates to the caller.
3.  **Sampling is per-method.** ``SHADOW_SAMPLE_RATE`` (default ``0.1``) is the
    global default; per-method overrides take precedence
    (e.g. ``SHADOW_SAMPLE_get_recent=1.0``).
4.  **Replay safety.** At startup the shadow is seeded from
    ``learning_store.get_recent(limit=10000)``. After seeding, every sampled
    write is mirrored. If production restarts, the shadow restarts too.
    Persistence is intentional non-goal; the shadow is ephemeral.
5.  **ZSF.** Every divergence and every shadow exception bumps a named
    counter. Divergences are also written as one JSON object per line to
    ``~/.context-dna/shadow-divergence.jsonl``.

CLI
---
::

    python -m services.shadow_mode.shadow_reader --report

Prints a table of divergences by ``(method, kind)``, sorted by frequency.

Self-test
---------
``test_shadow_reader_divergence`` (see the tests directory) feeds the wrapper
a known-divergent operation — production writes 1 row, shadow writes 2 — and
asserts the divergence is captured and the counter increments.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #


logger = logging.getLogger("contextdna.shadow_reader")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s contextdna.shadow_reader %(message)s"
        )
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Counters (ZSF — every observable failure bumps a counter)                    #
# --------------------------------------------------------------------------- #


COUNTERS: Dict[str, int] = {
    # Per-method invocation lifecycle
    "shadow_calls_total": 0,
    "shadow_calls_sampled_total": 0,
    "shadow_calls_skipped_sampling_total": 0,
    # Divergence categories — kept flat so /health can sum easily.
    "shadow_divergence_total": 0,
    "shadow_divergence_count_mismatch_total": 0,
    "shadow_divergence_id_set_mismatch_total": 0,
    "shadow_divergence_content_mismatch_total": 0,
    "shadow_divergence_exception_in_shadow_total": 0,
    "shadow_divergence_exception_in_prod_only_total": 0,
    # Logfile lifecycle
    "shadow_logfile_writes_total": 0,
    "shadow_logfile_errors_total": 0,
    # Seed lifecycle
    "shadow_seed_attempts_total": 0,
    "shadow_seed_failures_total": 0,
    "shadow_seed_rows_total": 0,
    # Temporal-stratified sampling (Round 5: 3-surgeon cross-exam ruling) —
    # observable per-strata lifecycle so a /health caller can verify the
    # right phase was active during a transition window without trusting
    # logs. Flat keys mirror the prom-style label convention
    # ``shadow_sample_strata_total{strata="..."}``.
    "shadow_sample_taken_total": 0,
    "shadow_sample_skipped_total": 0,
    "shadow_sample_strata_total::stable": 0,
    "shadow_sample_strata_total::lock_upgrade": 0,
    "shadow_sample_strata_total::hot_path": 0,
}

_COUNTER_LOCK = threading.Lock()


def _bump(key: str, by: int = 1) -> None:
    with _COUNTER_LOCK:
        COUNTERS[key] = COUNTERS.get(key, 0) + by


def get_counters() -> Dict[str, int]:
    with _COUNTER_LOCK:
        return dict(COUNTERS)


def reset_counters() -> None:
    """Test helper — never call in production paths."""
    with _COUNTER_LOCK:
        for k in list(COUNTERS.keys()):
            COUNTERS[k] = 0


# Per-(method, kind) sub-counter used by the CLI and the test. Stored flat to
# stay JSON-friendly: ``shadow_divergence_total{method=get_recent,kind=count_mismatch}``
# becomes the key ``"shadow_divergence_total::get_recent::count_mismatch"``.
def _bump_divergence(method: str, kind: str) -> None:
    with _COUNTER_LOCK:
        flat = f"shadow_divergence_total::{method}::{kind}"
        COUNTERS[flat] = COUNTERS.get(flat, 0) + 1
        # Roll up totals.
        COUNTERS["shadow_divergence_total"] = (
            COUNTERS.get("shadow_divergence_total", 0) + 1
        )
        kind_key = f"shadow_divergence_{kind}_total"
        COUNTERS[kind_key] = COUNTERS.get(kind_key, 0) + 1


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #


SHADOWED_METHODS: Tuple[str, ...] = (
    "store_learning",
    "get_recent",
    "get_by_id",
    "promote",
    "retire",
)

DEFAULT_SAMPLE_RATE = 0.1
SEED_LIMIT_DEFAULT = 10_000
DIVERGENCE_LOG_PATH_DEFAULT = Path.home() / ".context-dna" / "shadow-divergence.jsonl"

# Shadow thread pool: small, bounded, daemon-only. Replacing per-call thread
# creation prevents thread-storm under high traffic while still keeping the
# shadow off the caller's path.
_SHADOW_EXECUTOR_MAX_WORKERS = int(
    os.environ.get("SHADOW_EXECUTOR_MAX_WORKERS", "4")
)


def _per_method_sample_rate(method: str) -> float:
    """Return the sample rate for ``method``.

    Resolution order:
      1. ``SHADOW_SAMPLE_<method>``  (per-method override)
      2. ``SHADOW_SAMPLE_RATE``      (global default)
      3. ``DEFAULT_SAMPLE_RATE``     (module default = 0.1)
    """
    override = os.environ.get(f"SHADOW_SAMPLE_{method}")
    if override is not None:
        try:
            v = float(override)
        except ValueError:
            logger.warning(
                "Invalid SHADOW_SAMPLE_%s=%r — falling back to global rate",
                method,
                override,
            )
            v = float("nan")
        if v == v and 0.0 <= v <= 1.0:  # not NaN and in range
            return v
    raw = os.environ.get("SHADOW_SAMPLE_RATE")
    if raw is not None:
        try:
            v = float(raw)
            if 0.0 <= v <= 1.0:
                return v
        except ValueError:
            logger.warning(
                "Invalid SHADOW_SAMPLE_RATE=%r — falling back to default", raw
            )
    return DEFAULT_SAMPLE_RATE


def _should_sample(method: str) -> bool:
    rate = _per_method_sample_rate(method)
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return random.random() < rate


# --------------------------------------------------------------------------- #
# Temporal-stratified sampling (Round 5)                                       #
# --------------------------------------------------------------------------- #
#
# Round 5 3-surgeon cross-exam (2026-05-13) refused to sign off on uniform 10%
# sampling for production-critical, high-concurrency workloads. The verdict:
#
#   "10% shadow-mode sampling is insufficient for high-concurrency,
#    production-critical workloads."
#   "Implement temporal-stratified sampling during known transition
#    windows (lock upgrade phases) rather than uniform 100% sampling."
#
# The strata:
#   STABLE             — 10% sampling (default). Same coverage we had before;
#                        cheap continuous floor that still catches background
#                        drift between transition windows.
#   WINDOW_LOCK_UPGRADE — 100% sampling. Lock-level transitions are the highest
#                        race-risk window in the LearningStore. We want full
#                        coverage there, because *that* is when invariants
#                        slip and the shadow is most valuable.
#   WINDOW_HOT_PATH    — 50% sampling. Hot-path = high-frequency operations
#                        (e.g. recent.fail.huge bucket spiking). We don't go
#                        to 100% there because the cost is real and the race
#                        risk is lower than a lock transition; 50% is enough
#                        to surface a divergence within a handful of seconds
#                        of activity.
#
# Phase is *caller-set* — the caller is the only thing that knows when a
# lock upgrade has started or finished. ``SamplingStrategy`` is intentionally
# dumb about how phases are detected; it just trusts whatever phase the
# caller most recently set. This keeps the policy testable without coupling
# to LearningStore internals.


PHASE_STABLE = "stable"
PHASE_WINDOW_LOCK_UPGRADE = "lock_upgrade"
PHASE_WINDOW_HOT_PATH = "hot_path"

_STRATA_RATES: Dict[str, float] = {
    PHASE_STABLE: 0.1,
    PHASE_WINDOW_LOCK_UPGRADE: 1.0,
    PHASE_WINDOW_HOT_PATH: 0.5,
}


class SamplingStrategy:
    """Temporal-stratified shadow sampling policy.

    Default phase is :data:`PHASE_STABLE` (10%). Callers move the strategy
    into a higher-coverage window by calling :meth:`set_phase` — typically
    bracketing a lock upgrade or hot-path operation. The strategy is process-
    global on purpose: the shadow is a singleton, and the strategy that
    governs it must be observable from /health without threading the
    instance through every callsite.

    Per-method environment overrides (``SHADOW_SAMPLE_<method>``) still take
    precedence over the phase rate — operators must be able to force 100%
    or 0% sampling for a specific method without changing code. The strata
    rate is consulted only when no per-method override is set.
    """

    _lock = threading.Lock()
    _phase: str = PHASE_STABLE

    @classmethod
    def set_phase(cls, phase: str) -> None:
        """Set the current sampling phase.

        Unknown phases fall back to :data:`PHASE_STABLE` rather than raise —
        the shadow must never perturb production, even via a misconfigured
        phase. The fallback is also counted via the
        ``shadow_sample_strata_total::stable`` bucket so misuse is visible.
        """
        with cls._lock:
            cls._phase = phase if phase in _STRATA_RATES else PHASE_STABLE

    @classmethod
    def get_phase(cls) -> str:
        with cls._lock:
            return cls._phase

    @classmethod
    def reset(cls) -> None:
        """Test helper — restore the default phase."""
        with cls._lock:
            cls._phase = PHASE_STABLE

    @classmethod
    def _resolve_rate(cls, operation_type: str, current_phase: str) -> float:
        """Resolve the effective sample rate.

        Precedence:
          1. Per-method env override (``SHADOW_SAMPLE_<method>``)
          2. Strata rate for ``current_phase``
          3. ``DEFAULT_SAMPLE_RATE``
        """
        override = os.environ.get(f"SHADOW_SAMPLE_{operation_type}")
        if override is not None:
            try:
                v = float(override)
            except ValueError:
                v = float("nan")
            if v == v and 0.0 <= v <= 1.0:
                return v
        # Global env override (``SHADOW_SAMPLE_RATE``) is treated as a
        # hard knob too — preserves the existing operator contract.
        raw = os.environ.get("SHADOW_SAMPLE_RATE")
        if raw is not None:
            try:
                v = float(raw)
                if 0.0 <= v <= 1.0:
                    return v
            except ValueError:
                pass
        return _STRATA_RATES.get(current_phase, DEFAULT_SAMPLE_RATE)

    @classmethod
    def should_sample(
        cls,
        operation_type: str,
        current_phase: Optional[str] = None,
    ) -> bool:
        """Return True if this call should be shadowed.

        ``current_phase`` is normally omitted; the class-level phase set via
        :meth:`set_phase` is used. Tests pass an explicit phase to assert
        the rate per strata without touching shared state.
        """
        phase = current_phase if current_phase is not None else cls.get_phase()
        # Always count the strata bucket so /health proves which strata was
        # in effect, even when the per-method override forces 0/1. The
        # bucket key uses the actual phase even if it's unknown, normalized
        # to ``stable`` to keep the counter contract closed.
        bucket = phase if phase in _STRATA_RATES else PHASE_STABLE
        _bump(f"shadow_sample_strata_total::{bucket}")

        rate = cls._resolve_rate(operation_type, phase)
        if rate <= 0.0:
            _bump("shadow_sample_skipped_total")
            return False
        if rate >= 1.0:
            _bump("shadow_sample_taken_total")
            return True
        taken = random.random() < rate
        _bump("shadow_sample_taken_total" if taken else "shadow_sample_skipped_total")
        return taken


# --------------------------------------------------------------------------- #
# ShadowStorage — in-memory reference implementation                           #
# --------------------------------------------------------------------------- #


class ShadowStorage:
    """An intentionally simple, in-memory reference ``LearningStore``.

    Design notes
    ------------
    * Backed by a ``list`` (insertion-ordered) and a ``dict`` for ``id`` lookup.
    * **No concurrency hardening.** A coarse ``threading.Lock`` is held just
      long enough to mutate the list/index — there is no per-row locking, no
      transactional layering, no retry-on-busy. If shadow operations diverge
      under load that is itself evidence that production is racing in a way
      the simpler reference cannot reproduce; the divergence record captures
      it for analysis.
    * Storage is **ephemeral**. There is no flush, no on-disk persistence.
      When the process restarts the shadow restarts too — which is the right
      semantics, because production restarts also drop in-memory state.
    """

    __slots__ = ("_rows", "_index", "_lock", "_created_at")

    def __init__(self) -> None:
        # Most-recent-first to match LearningStore.get_recent() ordering.
        self._rows: List[Dict[str, Any]] = []
        self._index: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._created_at = time.time()

    # -- seeding --------------------------------------------------------- #

    def seed(self, rows: Sequence[Dict[str, Any]]) -> int:
        """Populate the shadow from a snapshot of production state.

        Returns the number of rows successfully ingested. Rows missing an
        ``id`` field are skipped and counted via the seed-failure counter.
        """
        ingested = 0
        with self._lock:
            self._rows.clear()
            self._index.clear()
            for row in rows:
                rid = row.get("id")
                if not rid:
                    _bump("shadow_seed_failures_total")
                    continue
                snap = dict(row)  # defensive copy — caller must not see us
                self._rows.append(snap)
                self._index[rid] = snap
                ingested += 1
        _bump("shadow_seed_rows_total", by=ingested)
        return ingested

    # -- API (matches LearningStore contract) --------------------------- #

    def store_learning(
        self,
        learning_data: Dict[str, Any],
        skip_dedup: bool = False,
        consolidate: bool = True,
    ) -> Dict[str, Any]:
        """Insert-or-update by ``id``. ``consolidate``/``skip_dedup`` ignored.

        The reference does NOT try to mirror the smart-merge consolidator —
        that is exactly the kind of behavior we want divergences for, since
        consolidator bugs are a known class of silent-corruption.
        """
        rid = learning_data.get("id") or f"shadow_auto_{uuid.uuid4().hex}"
        snap = dict(learning_data)
        snap["id"] = rid
        snap.setdefault(
            "timestamp",
            datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            existing = self._index.get(rid)
            if existing is not None:
                # In-place update: keep position to match an upsert.
                existing.clear()
                existing.update(snap)
                return dict(existing)
            self._rows.insert(0, snap)
            self._index[rid] = snap
            return dict(snap)

    def get_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            return [dict(r) for r in self._rows[:limit]]

    def get_by_id(self, learning_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._index.get(learning_id)
            return dict(row) if row is not None else None

    def promote(self, learning_id: str, **fields: Any) -> bool:
        with self._lock:
            row = self._index.get(learning_id)
            if row is None:
                return False
            metadata = dict(row.get("metadata") or {})
            metadata.update(fields)
            metadata.setdefault(
                "promoted_at",
                datetime.now(timezone.utc).isoformat(),
            )
            row["metadata"] = metadata
            row["type"] = fields.get("type", "sop")
            return True

    def retire(self, learning_id: str) -> bool:
        with self._lock:
            row = self._index.get(learning_id)
            if row is None:
                return False
            row["type"] = "retired"
            metadata = dict(row.get("metadata") or {})
            metadata["retired_at"] = datetime.now(timezone.utc).isoformat()
            row["metadata"] = metadata
            return True

    # -- introspection --------------------------------------------------- #

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


# --------------------------------------------------------------------------- #
# Divergence record + persistence                                              #
# --------------------------------------------------------------------------- #


def _ensure_logfile(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _bump("shadow_logfile_errors_total")
        logger.error("cannot create shadow log dir %s: %s", path.parent, exc)


def _append_divergence(record: Dict[str, Any], path: Path) -> None:
    """Append one JSON object per line. Best-effort — ZSF counts failures."""
    _ensure_logfile(path)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        _bump("shadow_logfile_writes_total")
    except OSError as exc:
        _bump("shadow_logfile_errors_total")
        logger.error("shadow divergence write failed (%s): %s", path, exc)


# --------------------------------------------------------------------------- #
# Comparison primitives                                                        #
# --------------------------------------------------------------------------- #


# Volatile fields that legitimately differ between prod and shadow. They are
# stripped before semantic equality.  See "Known false-positive categories" in
# the module docstring.
_VOLATILE_FIELDS = frozenset(
    {
        "timestamp",
        "created_at",
        "updated_at",
        "promoted_at",
        "retired_at",
    }
)


def _normalize_for_compare(row: Any) -> Any:
    """Strip volatile fields so semantic equality is stable.

    * Drops keys in ``_VOLATILE_FIELDS`` at the top level.
    * Also drops them from a top-level ``metadata`` dict.
    * Leaves everything else byte-equal.
    """
    if not isinstance(row, dict):
        return row
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if k in _VOLATILE_FIELDS:
            continue
        if k == "metadata" and isinstance(v, dict):
            out[k] = {mk: mv for mk, mv in v.items() if mk not in _VOLATILE_FIELDS}
        else:
            out[k] = v
    return out


def _row_ids(rows: Iterable[Any]) -> List[str]:
    ids: List[str] = []
    for r in rows:
        if isinstance(r, dict):
            rid = r.get("id")
            if rid is not None:
                ids.append(str(rid))
    return ids


def _classify_divergence(
    method: str,
    prod_result: Any,
    shadow_result: Any,
    prod_exc: Optional[BaseException],
    shadow_exc: Optional[BaseException],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Compare prod vs shadow. Returns ``(kind, details)`` or ``None``.

    ``kind`` is one of:
      * ``"count_mismatch"``
      * ``"id_set_mismatch"``
      * ``"content_mismatch"``
      * ``"exception_in_shadow"``
      * ``"exception_in_prod_only"``
    """
    # Exception axis takes precedence — those are the loudest signals.
    if prod_exc is not None and shadow_exc is None:
        return (
            "exception_in_prod_only",
            {
                "prod_exception": f"{type(prod_exc).__name__}: {prod_exc}",
            },
        )
    if shadow_exc is not None:
        # Note: if BOTH raised we still classify as in_shadow — the shadow
        # cannot be authoritative about prod failures, but a shadow exception
        # is always actionable on its own.
        return (
            "exception_in_shadow",
            {
                "shadow_exception": f"{type(shadow_exc).__name__}: {shadow_exc}",
                "prod_exception": (
                    f"{type(prod_exc).__name__}: {prod_exc}" if prod_exc else None
                ),
            },
        )

    # Both returned cleanly — compare results.
    # List-returning methods (get_recent) compare in three stages.
    if isinstance(prod_result, list) or isinstance(shadow_result, list):
        prod_list = prod_result if isinstance(prod_result, list) else []
        shadow_list = shadow_result if isinstance(shadow_result, list) else []
        if len(prod_list) != len(shadow_list):
            return (
                "count_mismatch",
                {
                    "prod_count": len(prod_list),
                    "shadow_count": len(shadow_list),
                },
            )
        pid = sorted(_row_ids(prod_list))
        sid = sorted(_row_ids(shadow_list))
        if pid != sid:
            return (
                "id_set_mismatch",
                {
                    "prod_ids": pid[:20],
                    "shadow_ids": sid[:20],
                    "prod_only": sorted(set(pid) - set(sid))[:10],
                    "shadow_only": sorted(set(sid) - set(pid))[:10],
                },
            )
        # Same IDs in same order — last check: contents.
        prod_norm = [_normalize_for_compare(r) for r in prod_list]
        shadow_norm = [_normalize_for_compare(r) for r in shadow_list]
        if prod_norm != shadow_norm:
            # Find first differing row for the log; keeps records small.
            diff_idx = next(
                (
                    i
                    for i in range(len(prod_norm))
                    if prod_norm[i] != shadow_norm[i]
                ),
                None,
            )
            return (
                "content_mismatch",
                {
                    "first_diff_index": diff_idx,
                    "prod_row": prod_norm[diff_idx] if diff_idx is not None else None,
                    "shadow_row": shadow_norm[diff_idx]
                    if diff_idx is not None
                    else None,
                },
            )
        return None

    # Scalar / dict results — store_learning, get_by_id, promote, retire.
    prod_norm = _normalize_for_compare(prod_result)
    shadow_norm = _normalize_for_compare(shadow_result)
    if prod_norm != shadow_norm:
        return (
            "content_mismatch",
            {"prod_result": prod_norm, "shadow_result": shadow_norm},
        )
    return None


# --------------------------------------------------------------------------- #
# The wrapper — ShadowComparator                                               #
# --------------------------------------------------------------------------- #


class ShadowComparator:
    """Stateful wrapper that runs prod synchronously + shadow asynchronously.

    Use either as a context manager that wraps a ``LearningStore`` instance, as
    a decorator on individual methods, or by calling :meth:`shadow_call` from
    a hand-written facade.
    """

    def __init__(
        self,
        shadow: Optional[ShadowStorage] = None,
        log_path: Optional[Path] = None,
        methods: Sequence[str] = SHADOWED_METHODS,
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        # NOTE: never use ``shadow or ShadowStorage()`` — ShadowStorage defines
        # ``__len__`` and an empty shadow is falsy, which would silently
        # replace a caller-supplied empty shadow with a fresh one.
        self.shadow = shadow if shadow is not None else ShadowStorage()
        self.log_path = Path(log_path) if log_path else DIVERGENCE_LOG_PATH_DEFAULT
        self.methods = tuple(methods)
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=_SHADOW_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="shadow",
        )

    # -- lifecycle ------------------------------------------------------- #

    def seed_from_store(self, store: Any, limit: int = SEED_LIMIT_DEFAULT) -> int:
        """Seed the shadow from ``store.get_recent(limit=...)``.

        Best-effort: a seed failure is counted but does not raise — better to
        run with an empty shadow than to crash the host process.
        """
        _bump("shadow_seed_attempts_total")
        try:
            rows = store.get_recent(limit=limit)
        except Exception as exc:  # noqa: BLE001 — ZSF
            _bump("shadow_seed_failures_total")
            logger.error("shadow seed_from_store failed: %s", exc)
            return 0
        if not isinstance(rows, list):
            _bump("shadow_seed_failures_total")
            logger.error(
                "shadow seed_from_store: expected list, got %s", type(rows).__name__
            )
            return 0
        return self.shadow.seed(rows)

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False)

    def __enter__(self) -> "ShadowComparator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- core call path -------------------------------------------------- #

    def shadow_call(
        self,
        method: str,
        prod_fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run ``prod_fn`` synchronously; run the shadow asynchronously.

        Returns whatever ``prod_fn`` returns (or re-raises its exception, so
        the caller sees production semantics unchanged). The shadow result is
        captured and compared off the caller's path.
        """
        _bump("shadow_calls_total")

        if method not in self.methods or not SamplingStrategy.should_sample(method):
            _bump("shadow_calls_skipped_sampling_total")
            return prod_fn(*args, **kwargs)

        _bump("shadow_calls_sampled_total")

        # Run prod.  Failures must still propagate to the caller — production
        # semantics are sacred. We capture the outcome so the comparator can
        # see prod-only exceptions.
        prod_result: Any = None
        prod_exc: Optional[BaseException] = None
        try:
            prod_result = prod_fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            prod_exc = exc

        # Schedule the shadow asynchronously so the caller is never blocked.
        # We snapshot args/kwargs by reference because they are not modified
        # by the shadow path (read-only, no mutation, no IO).
        try:
            self._executor.submit(
                self._run_shadow_and_compare,
                method,
                args,
                kwargs,
                prod_result,
                prod_exc,
            )
        except RuntimeError as exc:
            # Executor shut down mid-flight — count and proceed.
            _bump("shadow_logfile_errors_total")
            logger.warning("shadow submit failed (%s); skipping comparison", exc)

        if prod_exc is not None:
            raise prod_exc  # type: ignore[misc] — already a BaseException
        return prod_result

    # -- decorators / convenience --------------------------------------- #

    def decorate(self, method: str, prod_fn: Callable[..., Any]) -> Callable[..., Any]:
        """Return a callable that wraps ``prod_fn`` with shadow comparison."""

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return self.shadow_call(method, prod_fn, *args, **kwargs)

        _wrapped.__name__ = getattr(prod_fn, "__name__", "wrapped")
        _wrapped.__doc__ = getattr(prod_fn, "__doc__", None)
        _wrapped.__wrapped__ = prod_fn  # type: ignore[attr-defined]
        return _wrapped

    def wrap_store(self, store: Any) -> "_ShadowedStore":
        """Return a proxy that mirrors writes/reads against the shadow."""
        return _ShadowedStore(self, store)

    # -- internals ------------------------------------------------------- #

    def _run_shadow_and_compare(
        self,
        method: str,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        prod_result: Any,
        prod_exc: Optional[BaseException],
    ) -> None:
        """Worker body — never raises into the executor."""
        shadow_fn = getattr(self.shadow, method, None)
        shadow_exc: Optional[BaseException] = None
        shadow_result: Any = None
        if shadow_fn is None:
            shadow_exc = AttributeError(
                f"ShadowStorage has no method {method!r}"
            )
        else:
            try:
                shadow_result = shadow_fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — ZSF
                shadow_exc = exc

        diverged = _classify_divergence(
            method=method,
            prod_result=prod_result,
            shadow_result=shadow_result,
            prod_exc=prod_exc,
            shadow_exc=shadow_exc,
        )
        if diverged is None:
            return

        kind, details = diverged
        _bump_divergence(method, kind)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "kind": kind,
            "details": details,
            "args_summary": _summarize_args(args, kwargs),
        }
        try:
            _append_divergence(record, self.log_path)
        except Exception as exc:  # noqa: BLE001 — ZSF: never escape worker
            _bump("shadow_logfile_errors_total")
            logger.error("divergence record write failed: %s", exc)


def _summarize_args(
    args: Tuple[Any, ...], kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Produce a small, log-safe representation of the call args."""
    out: Dict[str, Any] = {}
    # Positional: only keep scalars; collapse dicts/lists to their length.
    pos_repr: List[Any] = []
    for v in args:
        if isinstance(v, (str, int, float, bool)) or v is None:
            pos_repr.append(v)
        elif isinstance(v, dict):
            pos_repr.append({"_dict_id": v.get("id"), "_dict_len": len(v)})
        elif isinstance(v, list):
            pos_repr.append({"_list_len": len(v)})
        else:
            pos_repr.append({"_type": type(v).__name__})
    if pos_repr:
        out["args"] = pos_repr
    if kwargs:
        # kwargs are usually small scalars (limit=20, skip_dedup=True).
        out["kwargs"] = {
            k: v
            for k, v in kwargs.items()
            if isinstance(v, (str, int, float, bool)) or v is None
        }
    return out


# --------------------------------------------------------------------------- #
# Convenience proxy: wrap a LearningStore so calls are auto-shadowed           #
# --------------------------------------------------------------------------- #


class _ShadowedStore:
    """Thin proxy: any of the shadowed methods are routed through the
    comparator; everything else passes straight through.

    This is the easiest production hook: instead of mutating call sites you
    just wrap the singleton once at startup::

        from memory.learning_store import get_learning_store
        from services.shadow_mode.shadow_reader import ShadowComparator

        cmp = ShadowComparator()
        cmp.seed_from_store(get_learning_store())
        store = cmp.wrap_store(get_learning_store())
    """

    __slots__ = ("_comparator", "_target")

    def __init__(self, comparator: ShadowComparator, target: Any) -> None:
        object.__setattr__(self, "_comparator", comparator)
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        target = self._target
        attr = getattr(target, name)
        if name not in self._comparator.methods or not callable(attr):
            return attr
        return self._comparator.decorate(name, attr)


# --------------------------------------------------------------------------- #
# Public decorator API                                                         #
# --------------------------------------------------------------------------- #


def shadow_compare(
    method: str,
    comparator: ShadowComparator,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory: ``@shadow_compare("get_recent", cmp)`` over a
    ``LearningStore`` method gives you per-call shadow comparison without
    swapping the singleton out wholesale.
    """

    def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        return comparator.decorate(method, fn)

    return _deco


# --------------------------------------------------------------------------- #
# CLI — divergence report                                                      #
# --------------------------------------------------------------------------- #


def _iter_records(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Bad line — don't crash the report.
                continue


def report(path: Path = DIVERGENCE_LOG_PATH_DEFAULT) -> Dict[str, Any]:
    """Read the JSONL log and summarize divergences by (method, kind).

    Returns the summary as a dict for programmatic callers; the CLI prints a
    human-readable table.
    """
    by_pair: Counter[Tuple[str, str]] = Counter()
    by_method: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    earliest: Optional[str] = None
    latest: Optional[str] = None
    n = 0
    for rec in _iter_records(path):
        n += 1
        method = str(rec.get("method") or "?")
        kind = str(rec.get("kind") or "?")
        by_pair[(method, kind)] += 1
        by_method[method] += 1
        by_kind[kind] += 1
        ts = rec.get("ts")
        if ts:
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
    return {
        "path": str(path),
        "total": n,
        "by_pair": [
            {"method": m, "kind": k, "count": c}
            for (m, k), c in by_pair.most_common()
        ],
        "by_method": dict(by_method.most_common()),
        "by_kind": dict(by_kind.most_common()),
        "earliest": earliest,
        "latest": latest,
    }


def _print_report(summary: Dict[str, Any]) -> None:
    print(f"Shadow divergence report — {summary['path']}")
    print(f"  records: {summary['total']}")
    if summary["earliest"]:
        print(f"  range:   {summary['earliest']}  ->  {summary['latest']}")
    if summary["total"] == 0:
        print("  (no divergences recorded)")
        return
    print()
    print("  By (method, kind), most-frequent first:")
    print(f"    {'method':<20} {'kind':<28} {'count':>6}")
    for entry in summary["by_pair"]:
        print(
            f"    {entry['method']:<20} {entry['kind']:<28} {entry['count']:>6}"
        )
    print()
    print("  By method:")
    for m, c in summary["by_method"].items():
        print(f"    {m:<20} {c:>6}")
    print()
    print("  By kind:")
    for k, c in summary["by_kind"].items():
        print(f"    {k:<28} {c:>6}")


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shadow_reader",
        description=(
            "Instrumented production shadow-mode reader for the LearningStore. "
            "Use --report to summarize the divergence log."
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Summarize the JSONL divergence log and exit.",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=DIVERGENCE_LOG_PATH_DEFAULT,
        help=f"Path to the divergence log (default: {DIVERGENCE_LOG_PATH_DEFAULT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of a table.",
    )
    args = parser.parse_args(argv)

    if not args.report:
        parser.print_help()
        return 0

    summary = report(args.log_path)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
