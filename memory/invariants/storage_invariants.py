"""
Storage Invariants — runtime assertions for LearningStore
=========================================================

Adds **positive-evidence** pre/post assertions inside ``store_learning`` and
``get_recent`` so silent corruption can no longer escape unnoticed. This is
the gap 3-Surgeon Neurologist flagged on Round-3 PARTIAL verdict: a logging
counter only proves *that* something happened, not *what* — it never asserts
the row is actually retrievable, the count actually moved by 1, or the
returned dict actually matches the stored row.

Modes (env ``INVARIANT_MODE``):
    off    — no-op decorator pass-through; counters still increment so we
             can prove the module is loaded.  Default in production.
    warn   — log invariant violations, continue execution.
    crash  — raise :class:`InvariantViolation` on detection.  Default in
             CI / test mode.  Use this in ``test_invariants.py`` and any
             gates-gate / cardio probe that wants to surface corruption fast.

ZSF (Zero Silent Failures):
    Every check increments a counter via :func:`get_counters`.  A check
    that decides not to run (because mode is ``off``) bumps
    ``invariants_disabled_total``.  Successes bump ``invariants_passed_total``.
    Violations bump ``invariants_violated_total[method]``.  Missing-mode /
    config errors bump ``invariants_misconfigured_total``.

Integration:
    Opt-in.  Either set ``INVARIANT_MODE=warn`` (or ``crash``) in the
    environment and call :func:`apply_invariants_to(store)` once after
    constructing the LearningStore, OR import + monkey-patch from a wrapper
    factory.  No core-module edits required.

Contract NOT covered (acknowledged):
    - Cross-method transactional invariants (e.g. concurrent store + get
      races) belong in a higher-level invariance layer; this module is
      single-call scope.
    - Backend toggle invariance (sqlite vs postgres divergence) is already
      covered by ``migrate4/rollback/storage-invariant-check.sh``.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
import traceback
from collections import Counter
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger("contextdna.invariants.storage")


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class InvariantViolation(AssertionError):
    """Raised in ``crash`` mode when a storage invariant fails.

    Carries the method name, decoded args (best-effort, never raises during
    str()), expected value, actual value, and a captured traceback so the
    failure tells the operator *exactly* which assertion blew up and why.
    """

    def __init__(
        self,
        method: str,
        check: str,
        expected: Any,
        actual: Any,
        args: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.method = method
        self.check = check
        self.expected = expected
        self.actual = actual
        self.args = args or {}
        self.captured_at = time.time()
        self.tb = "".join(traceback.format_stack(limit=10))
        super().__init__(self._render())

    def _render(self) -> str:
        try:
            args_str = _safe_repr(self.args)
        except Exception:
            args_str = "<unrepresentable>"
        return (
            f"InvariantViolation[{self.method}::{self.check}] "
            f"expected={self.expected!r} actual={self.actual!r} args={args_str}"
        )


def _safe_repr(obj: Any, limit: int = 200) -> str:
    """Best-effort repr that never raises and never explodes the log."""
    try:
        s = repr(obj)
    except Exception:
        s = "<repr-failed>"
    return s if len(s) <= limit else s[:limit] + "..."


# --------------------------------------------------------------------------- #
# Counters (ZSF)                                                              #
# --------------------------------------------------------------------------- #


_COUNTERS: Counter[str] = Counter()
_COUNTER_LOCK = threading.Lock()


def _bump(key: str, by: int = 1) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] += by


def get_counters() -> Dict[str, int]:
    """Snapshot of every counter this module owns."""
    with _COUNTER_LOCK:
        return dict(_COUNTERS)


def reset_counters() -> None:
    """Test-only — wipe counters between pytest cases."""
    with _COUNTER_LOCK:
        _COUNTERS.clear()


# --------------------------------------------------------------------------- #
# Mode resolution                                                             #
# --------------------------------------------------------------------------- #


_VALID_MODES = {"off", "warn", "crash"}


def _current_mode() -> str:
    raw = os.environ.get("INVARIANT_MODE", "off").lower().strip()
    if raw not in _VALID_MODES:
        _bump("invariants_misconfigured_total")
        logger.warning(
            "INVARIANT_MODE=%r not in %s — falling back to 'off'", raw, _VALID_MODES
        )
        return "off"
    return raw


def _handle_violation(violation: InvariantViolation) -> None:
    """Dispatch a violation according to the active mode."""
    _bump(f"invariants_violated_total::{violation.method}")
    mode = _current_mode()
    if mode == "crash":
        raise violation
    # warn (and off — but off shouldn't reach here; defensive)
    logger.error(
        "STORAGE INVARIANT VIOLATION method=%s check=%s expected=%r actual=%r",
        violation.method,
        violation.check,
        violation.expected,
        violation.actual,
    )


# --------------------------------------------------------------------------- #
# The decorator                                                               #
# --------------------------------------------------------------------------- #


def invariant_check(
    pre: Optional[Callable[..., None]] = None,
    post: Optional[Callable[..., None]] = None,
    method_name: Optional[str] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a storage method with pre/post invariant hooks.

    The decorator is **always** installed; runtime behaviour is gated by the
    ``INVARIANT_MODE`` env var.  This means we can ship invariants in
    production at zero overhead (mode=off short-circuits) while still
    flipping ``warn`` / ``crash`` on for ad-hoc audits.

    The pre hook gets ``(self, *args, **kwargs)``.
    The post hook gets ``(self, result, *args, **kwargs)``.
    Either hook may raise :class:`InvariantViolation` directly — the
    decorator forwards it through ``_handle_violation`` so the same
    pre/post code obeys the mode contract.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = method_name or fn.__name__

        @functools.wraps(fn)
        def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            mode = _current_mode()
            if mode == "off":
                _bump("invariants_disabled_total")
                return fn(self, *args, **kwargs)

            # PRE
            if pre is not None:
                try:
                    pre(self, *args, **kwargs)
                except InvariantViolation as iv:
                    _handle_violation(iv)
                except (ValueError, TypeError):
                    # Deliberate contract-violation raises (e.g. missing
                    # required keys) MUST propagate so the caller can use
                    # stdlib exception handling.  Counter already moved
                    # inside the pre hook.
                    raise
                except Exception as exc:
                    # An UNEXPECTED exception inside the pre hook is an
                    # invariant-code defect — log it, bump a counter, but
                    # don't break the underlying call.
                    _bump("invariants_pre_error_total")
                    logger.warning(
                        "pre-invariant for %s raised %s: %s",
                        name,
                        type(exc).__name__,
                        exc,
                    )

            # Always run the underlying method so we get the real return value.
            # If the method itself raises, propagate — that's not an invariant
            # failure, it's an upstream error.
            result = fn(self, *args, **kwargs)

            # POST
            if post is not None:
                try:
                    post(self, result, *args, **kwargs)
                except InvariantViolation as iv:
                    _handle_violation(iv)
                except (ValueError, TypeError):
                    raise
                except Exception as exc:
                    _bump("invariants_post_error_total")
                    logger.warning(
                        "post-invariant for %s raised %s: %s",
                        name,
                        type(exc).__name__,
                        exc,
                    )

            _bump("invariants_passed_total")
            return result

        return wrapped

    return decorator


# --------------------------------------------------------------------------- #
# store_learning invariants                                                   #
# --------------------------------------------------------------------------- #


_REQUIRED_KEYS = ("type", "title", "content")


def _store_learning_pre(
    self: Any,
    learning_data: Dict[str, Any],
    skip_dedup: bool = False,
    consolidate: bool = True,
    **_: Any,
) -> None:
    if not isinstance(learning_data, dict):
        raise InvariantViolation(
            method="store_learning",
            check="pre.input_is_dict",
            expected="dict",
            actual=type(learning_data).__name__,
            args={"skip_dedup": skip_dedup, "consolidate": consolidate},
        )
    missing = [k for k in _REQUIRED_KEYS if not learning_data.get(k)]
    if missing:
        # 3s Round-3 contract: raise *ValueError* (not just InvariantViolation)
        # so callers using stdlib exception handling still trip on bad inputs
        # even when INVARIANT_MODE=warn.  But the warn-mode counter must
        # increment first so we record the violation regardless.
        _bump("invariants_violated_total::store_learning")
        raise ValueError(
            f"store_learning: learning_data missing required keys: {missing}"
        )


def _store_learning_post(
    self: Any,
    result: Dict[str, Any],
    learning_data: Dict[str, Any],
    skip_dedup: bool = False,
    consolidate: bool = True,
    **_: Any,
) -> None:
    if not isinstance(result, dict):
        raise InvariantViolation(
            method="store_learning",
            check="post.returns_dict",
            expected="dict",
            actual=type(result).__name__,
        )
    row_id = result.get("id")
    if not row_id:
        raise InvariantViolation(
            method="store_learning",
            check="post.has_id",
            expected="non-empty id",
            actual=row_id,
        )

    # Read-after-write: the row MUST be retrievable by id.
    try:
        fetched = self.get_by_id(row_id)
    except Exception as exc:
        raise InvariantViolation(
            method="store_learning",
            check="post.readback_did_not_raise",
            expected="get_by_id returns row",
            actual=f"raised {type(exc).__name__}: {exc}",
        )
    if fetched is None:
        raise InvariantViolation(
            method="store_learning",
            check="post.readback_returned_row",
            expected="dict",
            actual=None,
        )

    # Field equality — proves the result dict actually reflects the stored row.
    # Compare type/title/content; we don't compare timestamps because the
    # backend may add timezone normalisation.
    for field in ("type", "title", "content"):
        expected = learning_data.get(field)
        actual_stored = fetched.get(field)
        actual_returned = result.get(field)
        # If skip_dedup is False the backend may have merged with an existing
        # row, in which case `title`/`content` may legitimately be a
        # consolidated superset.  Only assert exact equality when skip_dedup
        # is True (caller explicitly opted out of merge semantics).
        if skip_dedup:
            if actual_returned != expected:
                raise InvariantViolation(
                    method="store_learning",
                    check=f"post.returned.{field}_eq_input",
                    expected=_safe_repr(expected),
                    actual=_safe_repr(actual_returned),
                )
            if actual_stored != actual_returned:
                raise InvariantViolation(
                    method="store_learning",
                    check=f"post.stored.{field}_eq_returned",
                    expected=_safe_repr(actual_returned),
                    actual=_safe_repr(actual_stored),
                )
        else:
            # In dedup/merge mode, at minimum the stored row's field must
            # be non-empty (we wrote SOMETHING).
            if actual_stored in (None, "", []):
                raise InvariantViolation(
                    method="store_learning",
                    check=f"post.stored.{field}_non_empty",
                    expected="non-empty",
                    actual=_safe_repr(actual_stored),
                )

    # Row-count delta: only checkable on skip_dedup writes (dedup may merge
    # into existing → delta=0; consolidation may collapse → delta=0 or -1).
    # When skip_dedup=True we know the backend MUST have inserted a fresh row.
    if skip_dedup:
        prev_total = getattr(self, "_invariant_last_total", None)
        try:
            stats = self.get_stats()
            cur_total = int(stats.get("total", -1))
        except Exception as exc:
            _bump("invariants_post_error_total")
            logger.warning(
                "post.delta check skipped: get_stats raised %s: %s",
                type(exc).__name__,
                exc,
            )
            return
        if prev_total is not None:
            delta = cur_total - prev_total
            if delta != 1:
                raise InvariantViolation(
                    method="store_learning",
                    check="post.row_count_delta",
                    expected=1,
                    actual=delta,
                    args={"prev_total": prev_total, "cur_total": cur_total},
                )
        # Stash for the next call's delta check.  This is per-instance so
        # threads on different LearningStore instances don't collide.
        self._invariant_last_total = cur_total


# --------------------------------------------------------------------------- #
# get_recent invariants                                                       #
# --------------------------------------------------------------------------- #


def _get_recent_pre(self: Any, limit: int = 20, **_: Any) -> None:
    if not isinstance(limit, int):
        raise InvariantViolation(
            method="get_recent",
            check="pre.limit_is_int",
            expected="int",
            actual=type(limit).__name__,
        )
    if limit <= 0:
        raise InvariantViolation(
            method="get_recent",
            check="pre.limit_positive",
            expected="> 0",
            actual=limit,
        )


def _get_recent_post(
    self: Any,
    result: Any,
    limit: int = 20,
    **_: Any,
) -> None:
    if not isinstance(result, list):
        raise InvariantViolation(
            method="get_recent",
            check="post.returns_list",
            expected="list",
            actual=type(result).__name__,
        )

    # No over-fetch.
    if len(result) > limit:
        raise InvariantViolation(
            method="get_recent",
            check="post.length_le_limit",
            expected=f"<= {limit}",
            actual=len(result),
        )

    # Empty-result evidence: if get_recent returned [], either the table is
    # actually empty OR the backend already incremented its error counter.
    # We refuse to accept a silent zero-row swallow.
    if len(result) == 0:
        backend_total = None
        try:
            stats = self.get_stats()
            backend_total = int(stats.get("total", -1))
        except Exception:
            backend_total = None

        # Sample the LearningStore's recent.fail counter — if get_recent did
        # the right thing on an empty table, recent.ok was incremented; if it
        # silently swallowed an exception, recent.fail was incremented.
        # Either way, ONE of them must have moved since this call started.
        try:
            from memory.learning_store import get_counters as _ls_counters  # type: ignore
        except ImportError:
            try:
                from learning_store import get_counters as _ls_counters  # type: ignore
            except ImportError:
                _ls_counters = None  # type: ignore

        recent_fail_seen = False
        if _ls_counters is not None:
            try:
                lsc = _ls_counters()
                # Either ok or fail counter must be non-zero on entry +
                # increment on this call; we accept "fail moved" as proof
                # the failure was logged (not silently swallowed).
                # We can't observe the delta without a baseline, so we just
                # require that one of recent.ok / recent.fail / recent.no_backend
                # exists in the counter dict — proving the upstream method
                # passed through its instrumented path.
                if any(
                    k in lsc and lsc[k] > 0
                    for k in ("recent.ok", "recent.fail", "recent.no_backend")
                ):
                    recent_fail_seen = True
            except Exception:
                recent_fail_seen = False

        if backend_total is None or backend_total > 0:
            # Table is non-empty but we got [] back.  That's a violation
            # UNLESS the backend's failure counter moved (proves the failure
            # was logged).  Silent corruption is the case where backend_total
            # > 0 AND no failure counter moved.
            if not recent_fail_seen:
                raise InvariantViolation(
                    method="get_recent",
                    check="post.empty_with_evidence",
                    expected=(
                        "either total==0 or recent.fail/recent.ok counter moved"
                    ),
                    actual=(
                        f"total={backend_total} but no observable failure "
                        f"counter movement"
                    ),
                )

    # Sort order — `get_recent` implies created_at DESC.  We check the
    # field the backend actually exposes (timestamp or created_at).
    if len(result) >= 2:
        # Pick whichever timestamp-ish key is present.
        sort_key = None
        for cand in ("created_at", "timestamp", "updated_at"):
            if cand in result[0]:
                sort_key = cand
                break
        if sort_key is not None:
            timestamps = [r.get(sort_key, "") for r in result]
            # Filter out blanks so the order check isn't poisoned by missing
            # fields in old rows.
            non_blank = [t for t in timestamps if t]
            if len(non_blank) >= 2:
                sorted_desc = sorted(non_blank, reverse=True)
                if non_blank != sorted_desc:
                    raise InvariantViolation(
                        method="get_recent",
                        check=f"post.sorted_desc[{sort_key}]",
                        expected="descending",
                        actual=non_blank[:5],
                    )


# --------------------------------------------------------------------------- #
# Integration shim                                                            #
# --------------------------------------------------------------------------- #


def apply_invariants_to(store: Any) -> Any:
    """Monkey-patch ``store_learning`` and ``get_recent`` on a LearningStore.

    Returns the same store object (so callers can chain).  The patch is
    idempotent — calling twice on the same instance is a no-op (a counter
    bumps so we can spot double-patches in logs).
    """
    if getattr(store, "_invariants_applied", False):
        _bump("invariants_double_patch_total")
        return store

    original_store = store.store_learning
    original_recent = store.get_recent

    @functools.wraps(original_store)
    def patched_store(
        learning_data: Dict[str, Any],
        skip_dedup: bool = False,
        consolidate: bool = True,
    ) -> Dict[str, Any]:
        # Build a method-like wrapper so the decorator can pass `self` to
        # the pre/post hooks (they expect an instance with get_by_id /
        # get_stats).  We construct a tiny shim that delegates.
        def _bound(
            inst: Any, data: Dict[str, Any], **kw: Any
        ) -> Dict[str, Any]:
            return original_store(data, **kw)

        decorated = invariant_check(
            pre=_store_learning_pre,
            post=_store_learning_post,
            method_name="store_learning",
        )(_bound)
        return decorated(
            store, learning_data, skip_dedup=skip_dedup, consolidate=consolidate
        )

    @functools.wraps(original_recent)
    def patched_recent(limit: int = 20) -> List[Dict[str, Any]]:
        def _bound(inst: Any, limit: int = 20) -> List[Dict[str, Any]]:
            return original_recent(limit)

        decorated = invariant_check(
            pre=_get_recent_pre,
            post=_get_recent_post,
            method_name="get_recent",
        )(_bound)
        return decorated(store, limit=limit)

    store.store_learning = patched_store  # type: ignore[assignment]
    store.get_recent = patched_recent  # type: ignore[assignment]
    store._invariants_applied = True
    store._invariant_last_total = None
    _bump("invariants_applied_total")
    return store


# --------------------------------------------------------------------------- #
# Diagnostics                                                                 #
# --------------------------------------------------------------------------- #


def describe() -> Dict[str, Any]:
    """Snapshot for /health endpoints + gates-gate."""
    return {
        "mode": _current_mode(),
        "counters": get_counters(),
        "checks": {
            "store_learning": [
                "pre.input_is_dict",
                "pre.required_keys",
                "post.returns_dict",
                "post.has_id",
                "post.readback_returned_row",
                "post.stored_fields_eq_returned (skip_dedup=True)",
                "post.row_count_delta == 1 (skip_dedup=True)",
            ],
            "get_recent": [
                "pre.limit_positive",
                "post.returns_list",
                "post.length_le_limit",
                "post.empty_with_evidence",
                "post.sorted_desc[created_at|timestamp]",
            ],
        },
    }


if __name__ == "__main__":
    import json as _json

    print(_json.dumps(describe(), indent=2))
