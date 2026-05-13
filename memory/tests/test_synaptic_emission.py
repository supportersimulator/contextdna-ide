"""
Tests for Synaptic Emission Layer (memory/synaptic_emission.py).

Covers:
  - emit_to_s6 / emit_to_s8 produce non-empty, non-impersonating output
  - Output uses third-person "Synaptic noticed" / "Synaptic observed" form
  - Rate limit: same insight emitted twice within 6h returns cached;
    third call after 7h emits anew
  - emit_top_insights respects priority sort and limit
  - reset_emission_cache clears in-memory + on-disk
  - CLI list / emit-s6 / emit-s8 / reset-cache work end-to-end
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import memory.synaptic_emission as se
from memory.synaptic_emission import (
    emit_to_s6,
    emit_to_s8,
    emit_top_insights,
    reset_emission_cache,
    emission_status,
    _insight_key,
    _RATE_LIMIT_S,
)
from memory.synaptic_proactive import ProactiveInsight


# -----------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Each test gets its own emission cache file + fresh in-memory state."""
    cache_path = tmp_path / "emission_cache.json"
    monkeypatch.setattr(se, "_CACHE_PATH", cache_path)
    se._cache = {}
    se._cache_loaded = False
    se._failure_count = 0
    yield


def _make(insight_type="warning", source="outcome_tracker", content="NATS timeout 4 times",
          confidence=0.85, detail="Approaches tried: x; y; z. Root cause likely pool exhaustion."):
    return ProactiveInsight(
        type=insight_type,
        content=content,
        confidence=confidence,
        source=source,
        detail=detail,
    )


# -----------------------------------------------------------------
# emit_to_s6 / emit_to_s8 basics
# -----------------------------------------------------------------

class TestEmitToS6:
    def test_non_empty(self):
        out = emit_to_s6(_make())
        assert out
        assert isinstance(out, str)

    def test_uses_third_person_synaptic(self):
        out = emit_to_s6(_make())
        # MUST say "Synaptic noticed" not "I noticed" or "I am Synaptic"
        assert "Synaptic noticed" in out
        assert "I noticed" not in out
        assert "I am Synaptic" not in out
        assert "I'm Synaptic" not in out

    def test_includes_severity_and_source(self):
        out = emit_to_s6(_make(insight_type="alert", source="cross_session_drift"))
        assert "severity=critical" in out
        assert "source=cross_session_drift" in out

    def test_includes_action(self):
        out = emit_to_s6(_make(detail="Root cause likely connection pool. Step back to re-diagnose."))
        assert "Action:" in out

    def test_includes_evidence(self):
        out = emit_to_s6(_make())
        assert "Evidence:" in out


class TestEmitToS8:
    def test_non_empty(self):
        out = emit_to_s8(_make())
        assert out

    def test_no_first_person_synaptic_impersonation(self):
        """S8 must NEVER speak AS Synaptic (no first-person from Synaptic)."""
        out = emit_to_s8(_make())
        # Forbidden first-person phrases that would impersonate
        assert "I noticed" not in out
        assert "I am Synaptic" not in out
        assert "I'm Synaptic" not in out
        assert "I'm noticing" not in out
        # Required third-person indicator
        assert "Synaptic" in out

    def test_relational_tone(self):
        """S8 uses relational opener phrases (e.g., 'flagged', 'noticed', 'observed')."""
        out = emit_to_s8(_make(insight_type="alert"))
        assert any(verb in out for verb in ("flagged", "noticed", "observed"))

    def test_severity_visible(self):
        out = emit_to_s8(_make(insight_type="warning"))
        assert "warn" in out


# -----------------------------------------------------------------
# Rate limit: 6h per insight
# -----------------------------------------------------------------

class TestRateLimit:
    def test_same_insight_within_6h_returns_cached(self):
        ins = _make(content="bug repeats every session")
        first = emit_to_s6(ins)
        # Immediately re-emit
        second = emit_to_s6(ins)
        assert first == second

    def test_third_call_after_7h_emits_anew(self):
        ins = _make(content="drift on NATS reliability", source="cross_session_drift", insight_type="alert")
        with patch("memory.synaptic_emission.time.time", return_value=1000.0):
            first = emit_to_s6(ins)
        # Within 6h
        with patch("memory.synaptic_emission.time.time", return_value=1000.0 + 3600):
            second = emit_to_s6(ins)
            assert second == first  # cached
        # After 7h
        with patch("memory.synaptic_emission.time.time", return_value=1000.0 + 7 * 3600):
            third = emit_to_s6(ins)
            # Allowed to be identical content, but the cache entry was refreshed
            # (the rate limiter accepts a fresh emission). Verify cache ts moved.
            key = _insight_key(ins)
            entry = se._cache[key]
            assert entry["ts"] >= 1000.0 + 7 * 3600 - 1

    def test_s6_and_s8_both_cached_from_one_call(self):
        ins = _make()
        s6 = emit_to_s6(ins)
        s8 = emit_to_s8(ins)
        # s8 should be cached (rate-limited) since first call cached both rails
        key = _insight_key(ins)
        entry = se._cache[key]
        assert entry["formatted_s6"] == s6
        assert entry["formatted_s8"] == s8

    def test_distinct_insights_not_rate_limited(self):
        a = _make(content="bug A")
        b = _make(content="bug B")
        oa = emit_to_s6(a)
        ob = emit_to_s6(b)
        assert oa != ob


# -----------------------------------------------------------------
# emit_top_insights (multi)
# -----------------------------------------------------------------

class TestEmitTopInsights:
    def test_respects_limit(self):
        insights = [
            ProactiveInsight(type="warning", content=f"bug {i}", confidence=0.7, source="t")
            for i in range(5)
        ]
        block = emit_top_insights(insights, audience="s6", limit=2)
        # 2 lines plus header
        assert block.count("- Synaptic") == 2

    def test_priority_sort_alerts_first(self):
        insights = [
            ProactiveInsight(type="observation", content="obs", confidence=0.5, source="t"),
            ProactiveInsight(type="alert", content="ALERT_A", confidence=0.6, source="t"),
            ProactiveInsight(type="warning", content="WARN_A", confidence=0.9, source="t"),
        ]
        block = emit_top_insights(insights, audience="s6", limit=2)
        # ALERT must appear before WARN
        a_idx = block.find("ALERT_A")
        w_idx = block.find("WARN_A")
        assert a_idx > -1 and w_idx > -1
        assert a_idx < w_idx

    def test_empty_input_returns_empty(self):
        assert emit_top_insights([], audience="s6") == ""

    def test_s8_header_relational(self):
        insights = [_make()]
        block = emit_top_insights(insights, audience="s8", limit=1)
        assert "subconscious" in block.lower() or "synaptic" in block.lower()

    def test_dedupes_within_call(self):
        # Same insight provided twice — only emit once
        ins = _make(content="dup pattern")
        block = emit_top_insights([ins, ins], audience="s6", limit=5)
        assert block.count("- Synaptic") == 1


# -----------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------

class TestCacheManagement:
    def test_persists_to_disk(self, tmp_path):
        ins = _make()
        emit_to_s6(ins)
        # Cache file should exist now
        assert se._CACHE_PATH.exists()
        data = json.loads(se._CACHE_PATH.read_text())
        assert isinstance(data, dict)
        assert len(data) == 1

    def test_reset_clears_cache(self):
        ins = _make()
        emit_to_s6(ins)
        n = reset_emission_cache()
        assert n == 1
        assert se._cache == {}
        assert not se._CACHE_PATH.exists()

    def test_reset_idempotent(self):
        # Reset on empty cache returns 0
        assert reset_emission_cache() == 0

    def test_emission_status(self):
        ins = _make()
        emit_to_s6(ins)
        s = emission_status()
        assert s["cached_insights"] == 1
        assert s["rate_limit_seconds"] == _RATE_LIMIT_S


# -----------------------------------------------------------------
# CLI integration smoke
# -----------------------------------------------------------------

class TestCLI:
    def test_cli_list_runs(self, capsys):
        from memory.synaptic_proactive import _cli
        with patch("memory.synaptic_proactive.generate_proactive_insights", return_value=[]):
            rc = _cli(["list"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no proactive insights" in captured.out.lower()

    def test_cli_emit_s6_runs(self, capsys):
        from memory.synaptic_proactive import _cli
        ins = [_make(content="cli test pattern")]
        with patch("memory.synaptic_proactive.generate_proactive_insights", return_value=ins):
            rc = _cli(["emit-s6"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Synaptic noticed" in out

    def test_cli_emit_s8_runs(self, capsys):
        from memory.synaptic_proactive import _cli
        ins = [_make(content="cli test pattern s8")]
        with patch("memory.synaptic_proactive.generate_proactive_insights", return_value=ins):
            rc = _cli(["emit-s8"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Synaptic" in out

    def test_cli_reset_cache(self, capsys):
        from memory.synaptic_proactive import _cli
        # Seed cache
        emit_to_s6(_make(content="for reset"))
        rc = _cli(["reset-cache"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Cleared" in out

    def test_cli_status(self, capsys):
        from memory.synaptic_proactive import _cli
        rc = _cli(["status"])
        assert rc == 0
        # Output is JSON
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "emission" in data


# -----------------------------------------------------------------
# Integration: spec scenarios
# -----------------------------------------------------------------

class TestSpecScenarios:
    def test_repeated_failures_5_sessions_surfaces_warning(self):
        """Spec test: 5 sessions all hitting same bug → repeated failures detector
        returns 1 insight, severity=warn. Then emission produces non-empty S6."""
        from memory.synaptic_proactive import _insights_from_outcome_tracker
        from unittest.mock import MagicMock
        import sys

        mock_tracker = MagicMock()
        mock_tracker.get_completed_outcomes.return_value = [
            {"task": "Fix NATS connection", "approach": f"attempt {i}",
             "success": False, "actual_outcome": "Still timing out"}
            for i in range(5)
        ]
        mock_mod = MagicMock()
        mock_mod.get_outcome_tracker = lambda: mock_tracker
        with patch.dict(sys.modules, {"memory.outcome_tracker": mock_mod}):
            results = _insights_from_outcome_tracker()

        assert len(results) >= 1
        # Severity maps to warn for type="warning"
        assert results[0].type == "warning"

        s6 = emit_to_s6(results[0])
        assert "Synaptic noticed" in s6
        assert "5 times" in s6 or "Fix NATS" in s6

    def test_circling_emission(self):
        """Cross-session circling pattern emits via S8 with relational tone."""
        ins = ProactiveInsight(
            type="warning",
            content="Circling detected: topics recurring across sessions: 'deployment' (4 sessions)",
            confidence=0.7,
            source="session_historian",
            detail="The same topics keep appearing without resolution.",
        )
        s8 = emit_to_s8(ins)
        assert "Synaptic" in s8
        assert "Circling" in s8 or "deployment" in s8
        # No first-person impersonation
        assert "I noticed" not in s8

    def test_drift_alert_emission(self):
        """Cross-session drift produces alert-severity emission."""
        ins = ProactiveInsight(
            type="alert",
            content="Cross-session drift on 'no polling' principle: belief weakened by 40%",
            confidence=0.85,
            source="cross_session_drift",
            detail="Sessions 1-3 stated 'no polling'; sessions 7-9 reintroduced polling loops.",
        )
        s8 = emit_to_s8(ins)
        assert "critical" in s8  # severity label for alert
        assert "Synaptic" in s8
