"""TT2 (2026-05-12) — S6 ZSF observability tests.

The SS3 webhook coverage audit found that ``generate_section_6`` walks three
fallback paths and silently returns empty when all three fail:

    synaptic_deep_voice.generate_deep_s6
        -> butler_deep_query.butler_enhanced_section_6
            -> get_synaptic_voice()

Before TT2, each failure was swallowed at DEBUG and no counter incremented —
exactly the silent failure pattern CLAUDE.md "ZERO SILENT FAILURES" forbids.

These tests pin the TT2 fix:

* All 3 paths fail to import -> per-path counters bump, WARNING logged once,
  ``all_paths_empty`` increments, section still returns ``""`` (acceptable).
* Path 1 succeeds -> no counter bumps, no warning, no missing-section flag.
* Idempotent -> calling the section twice does not emit duplicate WARNINGs
  (the dedup ``_S6_FIRST_WARNING_EMITTED`` set works).
"""
from __future__ import annotations

import logging
import sys
import types

import pytest


# =========================================================================
# Helpers
# =========================================================================


def _import_hook(monkeypatch):
    """Import ``memory.persistent_hook_structure`` with counters reset.

    The module is imported lazily so each test gets a clean counter dict via
    the public test helper. We also evict any stale cached fallback modules
    so the import-fail path is genuine.
    """
    import memory.persistent_hook_structure as ph

    ph.reset_s6_empty_path_counters_for_test()
    # Force re-resolution of the cached get_synaptic_voice helper. Without
    # this, a prior test could leave the module marked importable.
    ph._synaptic_voice_available = None
    return ph


def _evict(modname: str) -> None:
    """Remove a module from ``sys.modules`` so the next import re-runs."""
    sys.modules.pop(modname, None)


def _force_modulenotfound(monkeypatch, modname: str) -> None:
    """Make ``import <modname>`` raise ``ModuleNotFoundError``.

    Implemented via a meta_path finder so subsequent imports under the same
    name keep failing — mirrors the production state where the file is
    skip-worktree + absent on disk.
    """
    _evict(modname)

    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == modname:
                raise ModuleNotFoundError(f"No module named {modname!r}")
            return None

    blocker = _Blocker()
    # Insert first so it wins over the real finders.
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])


# =========================================================================
# All-three-paths-fail: counters + warning + section empty
# =========================================================================


def test_all_three_paths_empty_bumps_counters_and_warns(monkeypatch, caplog):
    """All 3 fallback paths absent -> counters bump, WARNING emitted, S6 == ''."""
    ph = _import_hook(monkeypatch)

    _force_modulenotfound(monkeypatch, "memory.synaptic_deep_voice")
    _force_modulenotfound(monkeypatch, "memory.butler_deep_query")
    _force_modulenotfound(monkeypatch, "memory.synaptic_voice")

    caplog.set_level(logging.WARNING, logger="context_dna")

    result = ph.generate_section_6("audit S6 ZSF observability after SS3", session_id=None)

    # Section returns empty string (acceptable — only SILENT empty is banned).
    assert result == ""

    counters = ph.get_s6_empty_path_counters()
    assert counters["path_1_synaptic_deep_voice"] == 1, counters
    assert counters["path_2_butler_deep_query"] == 1, counters
    assert counters["path_3_get_synaptic_voice"] == 1, counters
    assert counters["all_paths_empty"] == 1, counters

    # WARNING-level log lines must surface in the context_dna logger so ops
    # can grep them and the daemon /health surface can correlate.
    warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    warn_messages = "\n".join(r.getMessage() for r in warn_records)
    assert "S6 path 1" in warn_messages, warn_messages
    assert "S6 path 2" in warn_messages, warn_messages
    assert "S6 path 3" in warn_messages, warn_messages
    assert "S6 HOLISTIC is DARK" in warn_messages, warn_messages


# =========================================================================
# Path 1 succeeds -> silent (no warnings, no counter bumps)
# =========================================================================


def test_path1_success_no_warnings_no_counters(monkeypatch, caplog):
    """When path 1 returns content, paths 2/3 must not run and counters stay zero."""
    ph = _import_hook(monkeypatch)

    # Inject a synaptic_deep_voice stub whose generate_deep_s6 returns content.
    stub = types.ModuleType("memory.synaptic_deep_voice")

    def _ok_generate(prompt, session_id=None, active_file=None):  # noqa: D401
        return "deep voice produced real S6 content here"

    stub.generate_deep_s6 = _ok_generate
    monkeypatch.setitem(sys.modules, "memory.synaptic_deep_voice", stub)

    # Block path 2 + 3 helpers so if path 1 didn't truly win, the test fails
    # loudly via the empty counters check at the end.
    _force_modulenotfound(monkeypatch, "memory.butler_deep_query")
    _force_modulenotfound(monkeypatch, "memory.synaptic_voice")

    caplog.set_level(logging.WARNING, logger="context_dna")

    result = ph.generate_section_6("happy path test", session_id=None)

    # Section returns wrapped, non-empty content.
    assert result, "path 1 success must yield non-empty S6"
    assert "HOLISTIC CONTEXT" in result

    counters = ph.get_s6_empty_path_counters()
    assert counters["path_1_synaptic_deep_voice"] == 0, counters
    assert counters["path_2_butler_deep_query"] == 0, counters
    assert counters["path_3_get_synaptic_voice"] == 0, counters
    assert counters["all_paths_empty"] == 0, counters

    # No WARNINGs about S6 paths.
    warn_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "S6" in r.getMessage()
    ]
    assert warn_records == [], [r.getMessage() for r in warn_records]


# =========================================================================
# Idempotent: 2nd call bumps counters again but doesn't re-warn
# =========================================================================


def test_idempotent_warning_dedup(monkeypatch, caplog):
    """Calling generate_section_6 twice in a row must NOT double-emit WARNINGs.

    Counters keep climbing (so the gap remains observable on /health) but
    the WARNING log line fires only once per path per process. This prevents
    log-flood while preserving signal — the canonical ZSF pattern.
    """
    ph = _import_hook(monkeypatch)

    _force_modulenotfound(monkeypatch, "memory.synaptic_deep_voice")
    _force_modulenotfound(monkeypatch, "memory.butler_deep_query")
    _force_modulenotfound(monkeypatch, "memory.synaptic_voice")

    caplog.set_level(logging.WARNING, logger="context_dna")

    first = ph.generate_section_6("first call — should warn", session_id=None)
    warn_count_after_first = sum(
        1
        for r in caplog.records
        if r.levelno >= logging.WARNING and "S6" in r.getMessage()
    )
    assert first == ""
    assert warn_count_after_first >= 4, (
        f"expected >=4 S6 warnings on first call (3 paths + all-empty), "
        f"got {warn_count_after_first}"
    )

    caplog.clear()
    second = ph.generate_section_6("second call — should be silent", session_id=None)
    warn_count_after_second = sum(
        1
        for r in caplog.records
        if r.levelno >= logging.WARNING and "S6" in r.getMessage()
    )
    assert second == ""
    assert warn_count_after_second == 0, (
        f"dedup broken: 2nd call re-warned {warn_count_after_second} times — "
        f"WARNING should fire once per path per process"
    )

    # Counters still climb — observability preserved.
    counters = ph.get_s6_empty_path_counters()
    assert counters["all_paths_empty"] == 2, counters
    assert counters["path_1_synaptic_deep_voice"] == 2, counters
    assert counters["path_2_butler_deep_query"] == 2, counters
    assert counters["path_3_get_synaptic_voice"] == 2, counters
